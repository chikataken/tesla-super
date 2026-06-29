"""
Pick the 4 vehicle "corner" photos for the delivery upload from a set of
SuperDispatch inspection photos: the best FRONT, REAR, LEFT (driver) side, and
RIGHT (passenger) side full-vehicle shot.

Why a vision step: SD inspection sets are messy — night shots, lots of 3/4 angles,
plus VIN-sticker / key-card close-ups and junk frames — and the SD `photo_type`
field does NOT encode camera angle (see the sd-inspection-photos-api notes). So we
send the WHOLE set to Claude in ONE call: it classifies each photo's angle and
returns the single best full-vehicle shot for each of the 4 slots (seeing every
candidate at once is what lets it pick the *best* per angle and tell the two sides
apart).

Photos are EXIF-transposed (SD photos are rotation-flagged) and downscaled before
upload (cost control). `anthropic` is imported lazily so importing this module is
cheap and tests can mock `select_corner_photos`.

CLI:
    python photo_select.py <dir-of-jpgs|image ...> [--out picks.json] [--sheet sheet.png]
"""
from __future__ import annotations
import argparse
import base64
import glob
import json
import os
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Optional

from PIL import Image, ImageOps

import config

# The four slots we must fill, in upload order. left = driver side (vehicle's left),
# right = passenger side. Kept as a constant so the schema, prompt, and result agree.
SLOTS = ("front", "rear", "left_side", "right_side")

# Angle labels Claude may assign to each photo (superset of SLOTS).
ANGLES = (
    "front", "rear", "left_side", "right_side",
    "front_quarter", "rear_quarter",   # 3/4 views — usable fallbacks for a slot
    "interior", "detail_or_vin", "other",
)

_client: Any = None


@dataclass
class Selection:
    """The chosen photo index per slot (-1 = none found), plus the full per-photo
    classification and the model's reasoning."""
    picks: dict[str, int]                       # {slot: index or -1}
    photos: list[dict] = field(default_factory=list)   # per-photo classification
    reasoning: str = ""
    raw: str = ""

    def complete(self) -> bool:
        """True only if every slot got a distinct, real photo."""
        idxs = [self.picks.get(s, -1) for s in SLOTS]
        return all(i >= 0 for i in idxs) and len(set(idxs)) == len(idxs)

    def missing(self) -> list[str]:
        return [s for s in SLOTS if self.picks.get(s, -1) < 0]


_TOOL = {
    "name": "report",
    "description": "Classify each vehicle photo's camera angle and choose the best "
                   "full-vehicle shot for the front, rear, left and right sides.",
    "input_schema": {
        "type": "object",
        "properties": {
            "photos": {
                "type": "array",
                "description": "One entry per photo, in the SAME index order they were "
                               "given. Classify every photo.",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The photo's index as labeled."},
                        "angle": {"type": "string", "enum": list(ANGLES),
                                  "description": "Camera angle. left_side = the vehicle's "
                                                 "DRIVER side, right_side = the PASSENGER "
                                                 "side. Use front_quarter/rear_quarter for "
                                                 "3/4 views, detail_or_vin for VIN sticker / "
                                                 "key-card / paperwork close-ups, interior "
                                                 "for cabin shots, other for anything that "
                                                 "doesn't show the vehicle clearly."},
                        "is_full_vehicle": {"type": "boolean",
                                            "description": "True if most of the whole car is "
                                                           "in frame (not a close-up/crop)."},
                        "quality": {"type": "number",
                                    "description": "0..1 how usable this is as a clean "
                                                   "condition photo (framing, lighting, "
                                                   "little obstruction/glare)."},
                        "note": {"type": "string", "description": "Very short note (optional)."},
                    },
                    "required": ["index", "angle", "is_full_vehicle", "quality"],
                },
            },
            "selection": {
                "type": "object",
                "description": "The chosen photo INDEX for each slot, or -1 if no suitable "
                               "photo exists. The four indices must be DISTINCT, and "
                               "left_side / right_side must show OPPOSITE sides of the car.",
                "properties": {
                    "front": {"type": "integer"},
                    "rear": {"type": "integer"},
                    "left_side": {"type": "integer"},
                    "right_side": {"type": "integer"},
                },
                "required": ["front", "rear", "left_side", "right_side"],
            },
            "reasoning": {"type": "string",
                          "description": "One or two sentences on the picks — especially any "
                                         "slot set to -1 or any side you were unsure about."},
        },
        "required": ["photos", "selection", "reasoning"],
    },
}

_PROMPT = (
    "You are selecting vehicle condition photos for a delivery. You are given a set of "
    "inspection photos of ONE vehicle, each labeled with its index ('Photo i:'). The set "
    "is messy: night shots, many 3/4 angles, and some close-ups of a VIN sticker, a key "
    "card, or paperwork, plus possible junk frames.\n"
    "Do TWO things and report them with the `report` tool:\n"
    "1) Classify EVERY photo by camera angle (one entry per photo, same index order). "
    "Use: front, rear, left_side, right_side, front_quarter, rear_quarter, interior, "
    "detail_or_vin, other. IMPORTANT — sides are from the VEHICLE's own orientation: "
    "left_side = the DRIVER side, right_side = the PASSENGER side. Work out which side a "
    "photo shows from the car's orientation (which way it faces, near vs far flank, which "
    "headlight/taillight and mirror are visible).\n"
    "2) Choose the single BEST photo for each of four slots — front, rear, left_side, "
    "right_side — and report its index in `selection`. Prefer a clear, well-lit shot where "
    "MOST OF THE WHOLE CAR is in frame and the angle is unobstructed. A straight-on front/"
    "rear or a full side profile is best; if no clean straight/profile shot exists for a "
    "slot, fall back to the best 3/4 (quarter) view that most shows that face. The four "
    "chosen indices MUST be distinct, and left_side and right_side MUST be opposite sides "
    "of the car. If there is genuinely no usable photo for a slot, set it to -1.\n"
    "Never pick a VIN-sticker / key-card / paperwork close-up, an interior shot, or a junk "
    "frame for any of the four slots.\n"
    "Fill `reasoning` briefly, and call `report`."
)


def _get_client() -> Any:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in ../secrets/.env or "
                           "this folder's .env).")
    global _client
    if _client is None:
        import anthropic  # lazy: keeps `import photo_select` cheap and SDK-optional
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def prep_image(raw: bytes) -> bytes:
    """EXIF-transpose (SD photos are rotation-flagged) then downscale to a JPEG."""
    try:
        im = ImageOps.exif_transpose(Image.open(BytesIO(raw)).convert("RGB"))
        im.thumbnail((config.VISION_MAX_SIDE, config.VISION_MAX_SIDE))
        out = BytesIO()
        im.save(out, format="JPEG", quality=config.VISION_JPEG_QUALITY)
        return out.getvalue()
    except Exception:
        return raw


def select_corner_photos(images: list[bytes]) -> Selection:
    """Classify `images` and pick the best front/rear/left/right full-vehicle shot.

    `images` are raw photo bytes in index order. Returns a Selection whose `picks`
    map each slot to an index into `images` (or -1). One Claude call for the set."""
    images = images[:config.VISION_MAX_PHOTOS]
    blocks: list[dict] = [{"type": "text", "text": _PROMPT}]
    for i, raw in enumerate(images):
        small = prep_image(raw)
        blocks.append({"type": "text", "text": f"Photo {i}:"})
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(small).decode()},
        })

    resp = _get_client().messages.create(
        model=config.VISION_MODEL,
        max_tokens=1500,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "report"},
        messages=[{"role": "user", "content": blocks}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "report":
            d = block.input
            sel = d.get("selection") or {}
            n = len(images)

            def _idx(v) -> int:
                # Coerce to a valid in-range index, else -1 ("none").
                return v if isinstance(v, int) and 0 <= v < n else -1

            return Selection(
                picks={s: _idx(sel.get(s, -1)) for s in SLOTS},
                photos=d.get("photos") or [],
                reasoning=(d.get("reasoning") or "").strip(),
                raw=json.dumps(d),
            )
    return Selection(picks={s: -1 for s in SLOTS}, raw="no tool_use in response")


# --------------------------------- CLI -------------------------------------
def _gather_paths(args: list[str]) -> list[str]:
    """Accept a directory (its *.jpg/*.png, sorted) or explicit image paths."""
    if len(args) == 1 and os.path.isdir(args[0]):
        files: list[str] = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            files += glob.glob(os.path.join(args[0], ext))
        return sorted(files)
    return list(args)


def _annotated_sheet(paths: list[str], sel: Selection, out: str) -> None:
    """Save a contact sheet highlighting the 4 picks (for eyeballing results)."""
    from PIL import ImageDraw
    pick_of = {idx: slot for slot, idx in sel.picks.items() if idx >= 0}
    cols, cell, pad, lh = 5, 320, 6, 22
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
        slot = pick_of.get(i)
        color = (0, 230, 0) if slot else (210, 210, 0)
        label = f"[{i:02d}] {slot.upper()}" if slot else f"[{i:02d}]"
        draw.text((x + 2, y + 4), label, fill=color)
        if slot:                                   # green border on a pick
            draw.rectangle([x, y, x + cell, y + cell + lh], outline=(0, 230, 0), width=4)
    sheet.save(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", help="a directory of photos, or image paths")
    ap.add_argument("--out", help="write the selection JSON here")
    ap.add_argument("--sheet", help="write an annotated contact sheet PNG here")
    a = ap.parse_args()

    paths = _gather_paths(a.inputs)
    if not paths:
        print("no images found"); return
    images = [open(p, "rb").read() for p in paths]
    print(f"classifying {len(images)} photo(s)…")
    sel = select_corner_photos(images)

    print(f"\nreasoning: {sel.reasoning}\n")
    for slot in SLOTS:
        i = sel.picks.get(slot, -1)
        where = os.path.basename(paths[i]) if 0 <= i < len(paths) else "— none —"
        print(f"  {slot:11} -> [{i:>2}] {where}")
    if not sel.complete():
        print(f"\n  ⚠ incomplete: missing/duplicate for {sel.missing() or 'a slot'}")

    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"picks": sel.picks,
                       "files": {s: (paths[i] if 0 <= i < len(paths) else None)
                                 for s, i in sel.picks.items()},
                       "photos": sel.photos, "reasoning": sel.reasoning}, fh, indent=2)
        print(f"\nwrote {a.out}")
    if a.sheet:
        _annotated_sheet(paths, sel, a.sheet)
        print(f"wrote {a.sheet}")


if __name__ == "__main__":
    main()
