"""
Train the corner-photo classifier head on the labels from label_app.py.

Pipeline: CLIP image embedding (frozen, local) -> logistic-regression head over
{front, rear, left, right, reject}. The CLIP encoder is reused at inference, so this
head is tiny, trains in seconds, and runs 100% offline. This is the accuracy unlock
over zero-shot: it learns YOUR domain (night, watermark, the actual left/right cues).

    python trainer/train.py [--min-per-class 5] [--val 0.2]

Reads  trainer/pool/<VIN>/*.jpg  +  trainer/labels.json
Writes trainer/model.joblib  ({"clf", "classes", "clip_model", "clip_pretrained"})
and prints held-out accuracy + a per-class confusion matrix.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, APP_DIR)                 # import the sibling photo_select_clip + config

import config                               # noqa: E402
import photo_select_clip as psc            # noqa: E402

POOL = os.path.join(HERE, "pool")
LABELS_PATH = os.path.join(HERE, "labels.json")
MODEL_PATH = os.path.join(HERE, "model.joblib")
CACHE_PATH = os.path.join(HERE, "emb_cache.joblib")          # path -> CLIP embedding
CLASSES = ["front", "rear", "front_left", "front_right", "rear_left", "rear_right",
           "white_key", "black_key", "reject"]


def _load_labeled() -> tuple[list[str], list[str]]:
    if not os.path.exists(LABELS_PATH):
        sys.exit(f"no labels at {LABELS_PATH} — run label_app.py and label some photos first.")
    labels = json.load(open(LABELS_PATH))
    paths, ys = [], []
    for key, lab in labels.items():
        if lab not in CLASSES:
            continue
        p = os.path.join(POOL, key)
        if os.path.isfile(p):
            paths.append(p)
            ys.append(lab)
    return paths, ys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-per-class", type=int, default=5,
                    help="warn if any class has fewer labels than this")
    ap.add_argument("--val", type=float, default=0.2, help="held-out fraction for evaluation")
    a = ap.parse_args()

    from collections import Counter
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
    from sklearn.model_selection import train_test_split
    import joblib

    paths, ys = _load_labeled()
    if not paths:
        sys.exit("no usable labeled photos found.")
    counts = Counter(ys)
    print("labels:", dict(counts), f"(total {len(ys)})")
    thin = [c for c in CLASSES if counts.get(c, 0) < a.min_per_class]
    if thin:
        print(f"⚠ few/no labels for: {thin} — label more of these for a reliable model.")
    if len(counts) < 2:
        sys.exit("need at least 2 classes to train.")

    # Embed with CLIP, caching per-photo so re-trains only embed NEW photos (the
    # embedding is the slow part — minutes on CPU, seconds on GPU).
    cache = joblib.load(CACHE_PATH) if os.path.exists(CACHE_PATH) else {}
    missing = [p for p in paths if p not in cache]
    if missing:
        print(f"embedding {len(missing)} new photo(s) with CLIP ({config.CLIP_MODEL}); "
              f"{len(paths) - len(missing)} cached…")
        vecs = psc.embed_paths(missing)
        for p, v in zip(missing, vecs):
            cache[p] = v
        joblib.dump(cache, CACHE_PATH)
    else:
        print(f"all {len(paths)} embeddings cached.")
    X = np.stack([cache[p] for p in paths])
    y = np.array(ys)

    # Stratified split only when every class has >=2 samples and val is requested.
    can_split = a.val > 0 and all(v >= 2 for v in counts.values()) and len(ys) >= 10
    if can_split:
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=a.val, stratify=y, random_state=0)
    else:
        Xtr, ytr, Xte, yte = X, y, None, None
        print("(dataset small — training on all of it, no held-out eval)")

    clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0)
    clf.fit(Xtr, ytr)

    if Xte is not None:
        pred = clf.predict(Xte)
        acc = accuracy_score(yte, pred)
        print(f"\nheld-out accuracy: {acc:.1%}  (n={len(yte)})")
        labels_present = sorted(set(yte) | set(pred))
        print("confusion (rows=true, cols=pred):", labels_present)
        print(confusion_matrix(yte, pred, labels=labels_present))
        print(classification_report(yte, pred, labels=labels_present, zero_division=0))
    else:
        print(f"train accuracy: {accuracy_score(ytr, clf.predict(Xtr)):.1%}")

    joblib.dump({"clf": clf, "classes": list(clf.classes_),
                 "clip_model": config.CLIP_MODEL,
                 "clip_pretrained": config.CLIP_PRETRAINED}, MODEL_PATH)
    print(f"\nsaved {MODEL_PATH}")


if __name__ == "__main__":
    main()
