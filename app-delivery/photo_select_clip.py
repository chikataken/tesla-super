"""
LOCAL (no-API) corner-photo selector using CLIP zero-shot — picks the 4 Drop-Off
exterior shots (front / rear / 2 sides) from an SD inspection set. Nothing leaves
the machine.

Why CLIP over the parts-detector: a big contrastive model's semantic prior
generalizes across different cars / lighting far better than hand-tuned part-geometry
rules. We don't train anything — each photo is scored against text prompts.

Method (calibrated zero-shot — raw cosine sims are too compressed to rank on):
  * Ensemble several prompt templates per class and average the text features.
  * Per photo: softmax over {front, rear, side, reject} using the model's logit_scale
    so the margins are meaningful, then rank photos by P(class).
  * `reject` (VIN-sticker / key-card / interior / junk) gates non-car frames out.
  * Driver-vs-passenger (left/right) is BEST-EFFORT: CLIP can't reliably tell them
    apart, so we take the two strongest side shots and order them by a left-vs-right
    prompt score (two distinct flanks; which is literally the driver side isn't sure).

Model: open_clip ViT-L-14 / laion2b (config.CLIP_MODEL / CLIP_PRETRAINED), downloaded
from HF on first use (no account). torch/open_clip are imported lazily.

CLI:
    python photo_select_clip.py <dir|imgs> [--out picks.json] [--sheet s.png] [--copy-dir D]
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

SLOTS = ("front", "rear", "left_side", "right_side")

# Prompt ensemble. Templates × per-class phrasings are averaged into one text vector
# per class (standard open_clip zero-shot trick — lifts accuracy a lot).
_TEMPLATES = ["a photo of {}", "a blurry night photo of {}",
              "a photo of a white SUV, {}", "{}"]
_CLASS_PROMPTS = {
    "front": ["the front of a car",
              "the front of a car with the grille and both headlights facing the camera"],
    "rear":  ["the rear of a car",
              "the back of a car with both taillights and the license plate"],
    "side":  ["the side profile of a car",
              "a car seen from the side showing the doors and wheels"],
    "reject": ["a close-up of a small VIN sticker or label",
               "a hand holding a card or paperwork",
               "the interior of a car",
               "a dark blurry photo of trees and a parking lot"],
}
# Secondary prompts used ONLY to order the two side picks into left/right (best-effort).
_LR_PROMPTS = {
    "left_side": ["the driver side of a car", "the left side of a car"],
    "right_side": ["the passenger side of a car", "the right side of a car"],
}
_CLASSES = list(_CLASS_PROMPTS)

_model = _preprocess = _tokenizer = _logit_scale = None
_txt = _lr_txt = None


@dataclass
class PhotoScore:
    index: int
    front: float = 0.0
    rear: float = 0.0
    side: float = 0.0
    reject: float = 0.0
    rightness: float = 0.5     # P(right | side) from the L/R prompts (0..1)

    @property
    def is_car(self) -> bool:
        return self.reject < config.CLIP_REJECT_THRESH

    @property
    def top(self) -> str:
        return max(_CLASSES, key=lambda k: getattr(self, k))


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


_device = "cpu"


def _load():
    """Lazy-load the CLIP model (on GPU if available) + precompute class text features."""
    global _model, _preprocess, _tokenizer, _logit_scale, _txt, _lr_txt, _device
    if _model is not None:
        return
    import torch
    import open_clip
    want = os.getenv("CLIP_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        config.CLIP_MODEL, pretrained=config.CLIP_PRETRAINED)
    _model.eval()
    try:
        _model.to(want)
        _device = want
    except (torch.OutOfMemoryError, RuntimeError) as e:        # GPU busy/OOM -> CPU
        print(f"[clip] GPU unavailable ({type(e).__name__}); using CPU", flush=True)
        _model.to("cpu")
        _device = "cpu"
    _tokenizer = open_clip.get_tokenizer(config.CLIP_MODEL)
    _logit_scale = _model.logit_scale.exp().item()

    def feats(prompts):
        with torch.no_grad():
            t = _model.encode_text(_tokenizer(prompts).to(_device))
            t /= t.norm(dim=-1, keepdim=True)
            m = t.mean(0)
            m /= m.norm()
        return m

    _txt = torch.stack([feats([tpl.format(p) for p in _CLASS_PROMPTS[k] for tpl in _TEMPLATES])
                        for k in _CLASSES])
    _lr_txt = torch.stack([feats([tpl.format(p) for p in _LR_PROMPTS[k] for tpl in _TEMPLATES])
                          for k in ("left_side", "right_side")])


def embed_paths(paths: list[str]):
    """CLIP image embeddings (L2-normalized) for `paths` as an (N, D) numpy array.
    Shared by the trainer (train a head on these) and the trained selector."""
    global _device
    import numpy as np
    import torch
    _load()
    out, B, i = [], 64, 0
    while i < len(paths):
        ims = [_preprocess(ImageOps.exif_transpose(Image.open(p).convert("RGB")))
               for p in paths[i:i + B]]
        try:
            with torch.no_grad():
                v = _model.encode_image(torch.stack(ims).to(_device))
                v /= v.norm(dim=-1, keepdim=True)
        except (torch.OutOfMemoryError, RuntimeError) as e:
            if _device != "cuda":
                raise
            print(f"[clip] GPU OOM mid-embed ({type(e).__name__}); falling back to CPU", flush=True)
            _model.to("cpu"); _device = "cpu"
            torch.cuda.empty_cache()
            continue                                 # retry this batch on CPU
        out.append(v.cpu().numpy())
        i += B
    return np.concatenate(out) if out else np.zeros((0, 1))


def score_photos(paths: list[str]) -> list[PhotoScore]:
    """CLIP-score every photo into {front,rear,side,reject} probs + a side rightness."""
    import torch
    _load()
    out: list[PhotoScore] = []
    for i, p in enumerate(paths):
        im = ImageOps.exif_transpose(Image.open(p).convert("RGB"))
        with torch.no_grad():
            v = _model.encode_image(_preprocess(im).unsqueeze(0).to(_device))
            v /= v.norm(dim=-1, keepdim=True)
            probs = torch.softmax(_logit_scale * (v @ _txt.T).squeeze(0), dim=0)
            lr = torch.softmax(_logit_scale * (v @ _lr_txt.T).squeeze(0), dim=0)
        d = {k: float(probs[j]) for j, k in enumerate(_CLASSES)}
        out.append(PhotoScore(index=i, rightness=float(lr[1]), **d))
    return out


def select_from_scores(scores: list[PhotoScore]) -> Selection:
    """Pure selection over CLIP scores (no model) — unit-testable."""
    cars = [s for s in scores if s.is_car]

    def _best(cls):
        cands = [s for s in cars if s.top == cls]
        return max(cands, key=lambda s: getattr(s, cls)).index if cands else -1

    front_i = _best("front")
    rear_i = _best("rear")

    used = {front_i, rear_i} - {-1}
    side_c = sorted([s for s in cars if s.top == "side" and s.index not in used],
                    key=lambda s: s.side, reverse=True)
    picks = {s: -1 for s in SLOTS}
    picks["front"] = front_i
    picks["rear"] = rear_i
    if len(side_c) >= 2:
        # Two strongest side shots; order by rightness (best-effort driver/passenger).
        a, b = side_c[0], side_c[1]
        right, left = (a, b) if a.rightness >= b.rightness else (b, a)
        picks["right_side"] = right.index
        picks["left_side"] = left.index
    elif len(side_c) == 1:
        picks["right_side" if side_c[0].rightness >= 0.5 else "left_side"] = side_c[0].index

    notes = []
    if front_i < 0:
        notes.append("no front")
    if rear_i < 0:
        notes.append("no rear")
    if picks["left_side"] < 0 or picks["right_side"] < 0:
        notes.append("fewer than two side shots")
    reasoning = "; ".join(notes) if notes else "CLIP zero-shot; sides ordered by L/R prompt (best-effort)"
    return Selection(picks=picks, scores=scores, reasoning=reasoning)


def select_corner_photos(paths: list[str]) -> Selection:
    return select_from_scores(score_photos(paths))


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
                      f"F{st.front:.2f} R{st.rear:.2f} S{st.side:.2f} x{st.reject:.2f}", fill=color)
        if slot:
            draw.rectangle([x, y, x + cell, y + cell + lh], outline=(0, 230, 0), width=4)
    sheet.save(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local CLIP zero-shot corner-photo selector")
    ap.add_argument("inputs", nargs="+", help="a directory of photos, or image paths")
    ap.add_argument("--out")
    ap.add_argument("--sheet")
    ap.add_argument("--copy-dir", help="copy the 4 chosen photos here as "
                                       "front/rear/left_side/right_side.jpg")
    a = ap.parse_args()

    paths = _gather_paths(a.inputs)
    if not paths:
        print("no images found"); return
    print(f"CLIP-scoring {len(paths)} photo(s) with {config.CLIP_MODEL}/{config.CLIP_PRETRAINED}…")
    sel = select_corner_photos(paths)

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
                       "scores": [vars(s) for s in sel.scores]}, fh, indent=2)
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
