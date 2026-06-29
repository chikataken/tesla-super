"""
LOCAL corner-photo selector using the TRAINED head (trainer/train.py output).

CLIP image embedding (frozen) -> trained logistic-regression head -> per-photo class
probabilities over {front, rear, left, right, reject}. Unlike zero-shot CLIP, the
head LEARNED your domain — including the left/right cues — so driver-vs-passenger is
an actual prediction, not best-effort. 100% local at inference.

Pick: best P(front) / P(rear) / P(left) / P(right) among non-reject photos, distinct.

CLI:
    python photo_select_trained.py <dir|imgs> [--model trainer/model.joblib]
                                   [--out picks.json] [--sheet s.png] [--copy-dir D]
"""
from __future__ import annotations
import argparse
import glob
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageOps

import config
import photo_select_clip as psc

# Corner scheme: one output photo per angle class (reject is excluded).
SLOTS = ("front", "rear", "front_left", "front_right", "rear_left", "rear_right")
SLOT_CLASS = {s: s for s in SLOTS}            # slot name == class name
DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "trainer", "model.joblib")
_bundle = None


@dataclass
class PhotoScore:
    index: int
    probs: dict = field(default_factory=dict)   # class -> probability

    def p(self, cls: str) -> float:
        return self.probs.get(cls, 0.0)

    @property
    def top(self) -> str:
        return max(self.probs, key=self.probs.get) if self.probs else "reject"

    @property
    def is_car(self) -> bool:
        return self.top != "reject"


@dataclass
class Selection:
    picks: dict
    scores: list = field(default_factory=list)
    reasoning: str = ""

    def complete(self) -> bool:
        idxs = [self.picks.get(s, -1) for s in SLOTS]
        return all(i >= 0 for i in idxs) and len(set(idxs)) == len(idxs)

    def missing(self) -> list:
        return [s for s in SLOTS if self.picks.get(s, -1) < 0]


def _load_model(path: str):
    global _bundle
    if _bundle is None:
        import joblib
        if not os.path.exists(path):
            raise FileNotFoundError(f"no trained model at {path} — run trainer/train.py first.")
        _bundle = joblib.load(path)
        # Keep the CLIP encoder in sync with what the head was trained on.
        config.CLIP_MODEL = _bundle.get("clip_model", config.CLIP_MODEL)
        config.CLIP_PRETRAINED = _bundle.get("clip_pretrained", config.CLIP_PRETRAINED)
    return _bundle


def score_photos(paths: list[str], model_path: str = DEFAULT_MODEL) -> list[PhotoScore]:
    bundle = _load_model(model_path)
    clf, classes = bundle["clf"], bundle["classes"]
    X = psc.embed_paths(paths)
    proba = clf.predict_proba(X)
    return [PhotoScore(index=i, probs={c: float(proba[i][j]) for j, c in enumerate(classes)})
            for i in range(len(paths))]


NON_SIDE = {"reject", "white_key", "black_key"}   # never valid as one of the 4 sides
KEY_CLASSES = ("white_key", "black_key")


KEY_MIN_PROB = 0.30        # a photo is a key candidate at this white/black-key prob


def best_key(scores, min_prob: float = KEY_MIN_PROB):
    """The single most-confident KEY photo. Ranks every photo by its key probability
    max(P(white_key), P(black_key)) and returns the best one that clears `min_prob` —
    NOT by argmax, so a real key card is still recovered when `reject` narrowly edges
    it out (a hand holding a card over paperwork, or a dark interior shot, is
    reject-like but still the key). The 0.30 floor keeps non-key junk — whose key
    prob sits well below it — out. Returns (index, class, prob), or (None, None, 0.0)
    when no photo looks like a key."""
    cands = []
    for s in scores:
        wk, bk = s.p("white_key"), s.p("black_key")
        kp = max(wk, bk)
        if kp >= min_prob:
            cands.append((s.index, "white_key" if wk >= bk else "black_key", kp))
    if not cands:
        return (None, None, 0.0)
    i, c, p = max(cands, key=lambda t: t[2])
    return (i, c, round(p, 3))


def select_from_scores(scores: list[PhotoScore], exclude: frozenset = frozenset()) -> Selection:
    # `exclude` = photo indices that must never be chosen as a corner (e.g. the OCR-
    # matched VIN-plate closeups). Also drop reject + key closeups (NON_SIDE classes).
    cars = [s for s in scores if s.top not in NON_SIDE and s.index not in exclude]
    picks = {s: -1 for s in SLOTS}
    for slot in SLOTS:
        cls = SLOT_CLASS[slot]
        used = {i for i in picks.values() if i >= 0}
        cands = [s for s in cars if s.index not in used]
        if cands:
            best = max(cands, key=lambda s: s.p(cls))
            if best.p(cls) > 0:
                picks[slot] = best.index
    miss = [s for s in SLOTS if picks[s] < 0]
    reasoning = "trained CLIP head" + (f"; missing {miss}" if miss else "")
    return Selection(picks=picks, scores=scores, reasoning=reasoning)


def select_corner_photos(paths: list[str], model_path: str = DEFAULT_MODEL,
                         exclude_paths=None) -> Selection:
    scores = score_photos(paths, model_path)
    ex = frozenset()
    if exclude_paths:
        exset = {os.path.abspath(p) for p in exclude_paths}
        ex = frozenset(i for i, p in enumerate(paths) if os.path.abspath(p) in exset)
    return select_from_scores(scores, exclude=ex)


# --------------------------------- CLI -------------------------------------
def _gather_paths(args: list[str]) -> list[str]:
    if len(args) == 1 and os.path.isdir(args[0]):
        files: list[str] = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            files += glob.glob(os.path.join(args[0], ext))
        return sorted(files)
    return list(args)


def _annotated_sheet(paths: list[str], sel: Selection, out: str) -> None:
    from PIL import ImageDraw
    by_index = {s.index: s for s in sel.scores}
    pick_of = {idx: slot for slot, idx in sel.picks.items() if idx >= 0}
    cols, cell, pad, lh = 5, 320, 6, 34
    rows = (len(paths) + cols - 1) // cols
    W = cols * cell + (cols + 1) * pad
    H = rows * (cell + lh) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (28, 28, 28))
    draw = ImageDraw.Draw(sheet)
    for i, f in enumerate(paths):
        im = ImageOps.exif_transpose(Image.open(f).convert("RGB"))
        im.thumbnail((cell, cell))
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = pad + r * (cell + lh + pad)
        sheet.paste(im, (x + (cell - im.width) // 2, y + lh))
        st = by_index.get(i)
        slot = pick_of.get(i)
        color = (0, 230, 0) if slot else (170, 170, 170)
        draw.text((x + 2, y + 2), f"[{i:02d}] {slot.upper()}" if slot else f"[{i:02d}]", fill=color)
        if st:
            draw.text((x + 2, y + 17),
                      " ".join(f"{k}:{st.p(k):.2f}" for k in st.probs
                               if st.p(k) >= 0.15),
                      fill=color)
        if slot:
            draw.rectangle([x, y, x + cell, y + cell + lh], outline=(0, 230, 0), width=4)
    sheet.save(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local trained-head corner-photo selector")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out")
    ap.add_argument("--sheet")
    ap.add_argument("--copy-dir")
    a = ap.parse_args()

    paths = _gather_paths(a.inputs)
    if not paths:
        print("no images found"); return
    print(f"scoring {len(paths)} photo(s) with the trained head…")
    sel = select_corner_photos(paths, a.model)

    print(f"\nreasoning: {sel.reasoning}\n")
    for slot in SLOTS:
        i = sel.picks.get(slot, -1)
        where = os.path.basename(paths[i]) if 0 <= i < len(paths) else "— none —"
        print(f"  {slot:11} -> [{i:>2}] {where}")
    if not sel.complete():
        print(f"\n  ⚠ incomplete: {sel.missing()}")

    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"picks": sel.picks,
                       "files": {s: (paths[i] if 0 <= i < len(paths) else None)
                                 for s, i in sel.picks.items()},
                       "reasoning": sel.reasoning,
                       "scores": [{"index": s.index, "probs": s.probs} for s in sel.scores]},
                      fh, indent=2)
        print(f"\nwrote {a.out}")
    if a.sheet:
        _annotated_sheet(paths, sel, a.sheet)
        print(f"wrote {a.sheet}")
    if a.copy_dir:
        import shutil
        os.makedirs(a.copy_dir, exist_ok=True)
        n = 0
        for slot in SLOTS:
            i = sel.picks.get(slot, -1)
            if 0 <= i < len(paths):
                shutil.copy2(paths[i], os.path.join(a.copy_dir, f"{slot}.jpg"))
                n += 1
        print(f"copied {n}/4 photo(s) to {a.copy_dir}")


if __name__ == "__main__":
    main()
