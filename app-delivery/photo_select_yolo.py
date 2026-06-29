"""
LOCAL (no-API) corner-photo selector — picks the 4 Drop-Off exterior shots
(front / rear / 2 sides) from an SD inspection set using a car-PARTS detector plus
geometry rules. Nothing leaves the machine.

Model: a YOLO11n car-parts segmentation checkpoint (Ultralytics carparts-seg
classes: hood, front_bumper, front_light, front_glass, trunk, tailgate, back_bumper,
back_light, back_glass, wheel, doors, mirrors, …). Default weights:
    models/carparts_yolo11n_seg.pt
    (from HF konst22/yolo11n-carparts-seg — see README for the download command)

How it decides (validated on real night SD photos):
  * FRONT parts (hood/front_bumper/front_light/front_glass) vs REAR parts
    (trunk/tailgate/back_bumper/back_light/back_glass) reliably separate front/rear.
  * Multiple WHEELS + a door => a side/profile shot.
  * A "full vehicle" gate (enough distinct parts spread across the frame) rejects
    VIN-sticker / key-card / interior / junk close-ups.
  * The model's own left/right part labels are NOT reliable, so we do NOT trust them
    for driver-vs-passenger. Instead we force the two side picks to face OPPOSITE
    directions in-frame (front-parts left-of vs right-of the rear-parts), which gives
    two opposite flanks.

CLI:
    python photo_select_yolo.py <dir|imgs> [--weights W] [--out picks.json] [--sheet s.png]
"""
from __future__ import annotations
import argparse
import glob
import json
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

import config

SLOTS = ("front", "rear", "left_side", "right_side")
DEFAULT_WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "models", "carparts_yolo11n_seg.pt")

# Part-class groups (Ultralytics carparts-seg names). left/right variants are folded
# into the base group — we use them for front/rear/side, not for driver/passenger.
FRONT_PARTS = {"hood", "front_bumper", "front_glass",
               "front_light", "front_left_light", "front_right_light"}
REAR_PARTS = {"trunk", "tailgate", "back_bumper", "back_glass",
              "back_light", "back_left_light", "back_right_light"}
DOOR_PARTS = {"front_door", "back_door", "front_left_door", "front_right_door",
              "back_left_door", "back_right_door"}
MIRROR_PARTS = {"left_mirror", "right_mirror"}

_model = None


@dataclass
class PhotoStat:
    index: int
    n_box: int = 0
    n_distinct: int = 0
    spread: float = 0.0            # horizontal span of parts / image width (0..1)
    front: float = 0.0            # summed conf of distinct front parts
    rear: float = 0.0
    wheels: int = 0
    doors: float = 0.0
    facing: Optional[str] = None  # 'left'/'right' = which way the nose points in-frame
    is_full: bool = False         # passed the full-vehicle gate
    angle: str = "other"          # front|rear|side|front_quarter|rear_quarter|other
    parts: dict = field(default_factory=dict)


@dataclass
class Selection:
    picks: dict
    stats: list = field(default_factory=list)
    reasoning: str = ""

    def complete(self) -> bool:
        idxs = [self.picks.get(s, -1) for s in SLOTS]
        return all(i >= 0 for i in idxs) and len(set(idxs)) == len(idxs)

    def missing(self) -> list:
        return [s for s in SLOTS if self.picks.get(s, -1) < 0]


def _get_model(weights: str):
    global _model
    if _model is None:
        from ultralytics import YOLO  # lazy: heavy import (torch)
        _model = YOLO(weights)
    return _model


def _load_rgb(path: str) -> np.ndarray:
    # EXIF-transpose first — SD photos are rotation-flagged and YOLO won't honor it.
    return np.array(ImageOps.exif_transpose(Image.open(path).convert("RGB")))


def _analyze(index: int, result, img_w: int) -> PhotoStat:
    """Turn one YOLO result into a PhotoStat (part-group scores + geometry)."""
    names = result.names
    st = PhotoStat(index=index)
    # max-conf per class (dedupe duplicate boxes), and x-centroids per group.
    best_conf: dict[str, float] = {}
    xs_all: list[float] = []
    fx: list[float] = []   # front-part x centers
    rx: list[float] = []   # rear-part x centers
    for b in result.boxes:
        cls = names[int(b.cls)]
        conf = float(b.conf)
        x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
        cx = (x1 + x2) / 2.0
        xs_all.append(cx)
        best_conf[cls] = max(best_conf.get(cls, 0.0), conf)
        st.parts[cls] = st.parts.get(cls, 0) + 1
        if cls in FRONT_PARTS:
            fx.append(cx)
        elif cls in REAR_PARTS:
            rx.append(cx)
        if cls == "wheel":
            st.wheels += 1

    st.n_box = len(result.boxes)
    st.n_distinct = len(best_conf)
    st.front = sum(c for k, c in best_conf.items() if k in FRONT_PARTS)
    st.rear = sum(c for k, c in best_conf.items() if k in REAR_PARTS)
    st.doors = sum(c for k, c in best_conf.items() if k in DOOR_PARTS)
    if xs_all and img_w:
        st.spread = (max(xs_all) - min(xs_all)) / img_w
    # Facing: if both ends are visible, the nose is on the side whose parts sit further
    # toward one edge. front-centroid left of rear-centroid => car faces LEFT in-frame.
    if fx and rx:
        st.facing = "left" if (sum(fx) / len(fx)) < (sum(rx) / len(rx)) else "right"

    # Full-vehicle gate: enough distinct parts AND they span a good width (rejects
    # close-ups of a VIN sticker / key card / a single panel, and junk frames).
    st.is_full = st.n_distinct >= 3 and st.spread >= 0.30

    # Angle label.
    side_like = st.wheels >= 2 and (st.doors > 0 or len(MIRROR_PARTS & set(st.parts)) > 0)
    if not st.is_full:
        st.angle = "other"
    elif side_like and st.front > 0 and st.rear > 0:
        st.angle = "side"                       # both ends + wheels => profile
    elif side_like and st.front > st.rear:
        st.angle = "front_quarter"
    elif side_like and st.rear >= st.front:
        st.angle = "rear_quarter"
    elif st.front > st.rear:
        st.angle = "front"
    elif st.rear > st.front:
        st.angle = "rear"
    else:
        st.angle = "other"
    return st


def select_corner_photos(paths: list[str], weights: str = DEFAULT_WEIGHTS) -> Selection:
    """Run the parts detector on `paths` and pick the best front/rear/2-side shots."""
    model = _get_model(weights)
    imgs = [_load_rgb(p) for p in paths]
    results = model.predict(imgs, conf=0.25, imgsz=1024, verbose=False)
    stats = [_analyze(i, r, imgs[i].shape[1]) for i, r in enumerate(results)]
    return select_from_stats(stats)


def select_from_stats(stats: list[PhotoStat]) -> Selection:
    """Pure selection logic over per-photo stats (no model) — kept separate so the
    ranking rules are unit-testable without torch/weights."""
    full = [s for s in stats if s.is_full]

    def _pick(pref, fallback, key):
        """Best of the preferred class; fall back to the secondary class if empty.
        Prefers a CANONICAL angle (straight front/rear, full side profile) over a
        3/4 quarter view — a quarter exposes more parts and would otherwise win."""
        pool = pref or fallback
        return max(pool, key=key).index if pool else -1

    # FRONT: prefer a straight-on `front`; fall back to a front 3/4. Reward strong
    # front parts, penalize visible rear, mild bonus for coverage.
    front_key = lambda s: s.front - 0.5 * s.rear + 0.3 * s.spread
    front_i = _pick([s for s in full if s.angle == "front"],
                    [s for s in full if s.angle == "front_quarter"], front_key)
    # REAR: prefer a straight-on `rear`; fall back to a rear 3/4.
    rear_key = lambda s: s.rear - 0.5 * s.front + 0.3 * s.spread
    rear_i = _pick([s for s in full if s.angle == "rear"],
                   [s for s in full if s.angle == "rear_quarter"], rear_key)

    # SIDES: prefer true `side` profiles (both ends + wheels). Only if fewer than two
    # exist, allow 3/4 quarter views (wheels>=2) as fillers. Pick the two strongest;
    # use opposite facing to order them into left/right when it's confidently known.
    used = {front_i, rear_i} - {-1}
    side_key = lambda s: s.wheels + s.doors + s.spread
    profiles = sorted([s for s in full if s.index not in used and s.angle == "side"],
                      key=side_key, reverse=True)
    side_c = list(profiles)
    if len(side_c) < 2:                       # not enough clean profiles — allow quarters
        quarters = sorted(
            [s for s in full if s.index not in used and s not in side_c
             and s.angle in ("front_quarter", "rear_quarter") and s.wheels >= 2],
            key=side_key, reverse=True)
        side_c += quarters
    side1 = side_c[0] if side_c else None
    side2 = None
    if side1 and len(side_c) > 1:
        opp = [s for s in side_c[1:] if s.facing and side1.facing and s.facing != side1.facing]
        side2 = opp[0] if opp else side_c[1]

    # Map the two side shots to left_side/right_side by facing when known (a car facing
    # LEFT in-frame shows its far flank; we just need them distinct + opposite). Without
    # reliable L/R, assign deterministically: facing 'left' -> left_side slot.
    picks = {s: -1 for s in SLOTS}
    picks["front"] = front_i
    picks["rear"] = rear_i
    sides = [s for s in (side1, side2) if s is not None]
    if len(sides) == 2 and sides[0].facing and sides[1].facing and \
            sides[0].facing != sides[1].facing:
        for s in sides:
            picks["left_side" if s.facing == "left" else "right_side"] = s.index
    else:                                   # fallback: just fill both side slots
        for slot, s in zip(("left_side", "right_side"), sides):
            picks[slot] = s.index

    notes = []
    if front_i < 0:
        notes.append("no clear front")
    if rear_i < 0:
        notes.append("no clear rear")
    if picks["left_side"] < 0 or picks["right_side"] < 0:
        notes.append("could not get two distinct/opposite sides")
    reasoning = ("; ".join(notes) if notes
                 else "front/rear from part groups; two sides forced to opposite facing")
    return Selection(picks=picks, stats=stats, reasoning=reasoning)


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
    by_index = {s.index: s for s in sel.stats}
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
        l1 = f"[{i:02d}] {slot.upper()}" if slot else f"[{i:02d}]"
        l2 = (f"{st.angle} F{st.front:.1f} R{st.rear:.1f} w{st.wheels} "
              f"{st.facing or '-'}" if st else "")
        draw.text((x + 2, y + 2), l1, fill=color)
        draw.text((x + 2, y + 17), l2, fill=color)
        if slot:
            draw.rectangle([x, y, x + cell, y + cell + lh], outline=(0, 230, 0), width=4)
    sheet.save(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local YOLO car-parts corner-photo selector")
    ap.add_argument("inputs", nargs="+", help="a directory of photos, or image paths")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--out")
    ap.add_argument("--sheet")
    ap.add_argument("--copy-dir", help="copy the 4 chosen photos here as "
                                       "front/rear/left_side/right_side.jpg")
    a = ap.parse_args()

    paths = _gather_paths(a.inputs)
    if not paths:
        print("no images found"); return
    print(f"running car-parts detector on {len(paths)} photo(s)…")
    sel = select_corner_photos(paths, a.weights)

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
                       "stats": [vars(s) for s in sel.stats]}, fh, indent=2)
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
                dst = os.path.join(a.copy_dir, f"{slot}.jpg")
                shutil.copy2(paths[i], dst)
                n += 1
                print(f"  {slot:11} -> {dst}")
        print(f"copied {n}/4 photo(s) to {a.copy_dir}")


if __name__ == "__main__":
    main()
