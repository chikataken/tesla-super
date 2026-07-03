"""
Decode a VIN into the photo sets the Tesla Logistics app's Drop-Off needs.

Given a VIN (the app tells us which one):
  1. fetch the LATEST DELIVERED shipment's Delivery photos via the official SD API
     (fetch_latest.py, shipment-creator venv),
  2. run the trained corner model to pick the 6 exterior shots — front, rear,
     front_left, front_right, rear_left, rear_right (photo_select_trained),
  3. find the VIN-plate photo(s) by ON-DEVICE OCR matching the VIN
     (ocr_vin.py, direct-pickup-checks venv — no Claude),

then lay them out as the 3 app sections:
  out/<VIN>/sides/      6 corner photos   (the "all four sides" section)
  out/<VIN>/vin_plate/  OCR VIN matches   (the "VIN plate" section)
  out/<VIN>/key/        same as vin_plate  (the "key" section, less important)
  out/<VIN>/manifest.json + picks.png (annotated corner sheet)

App navigation/upload is a later step; this just produces the sets.

    python decode_vin.py --vin <VIN> [--out DIR]
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SC_PY = os.path.join(REPO_ROOT, "shipment-creator", ".venv", "bin", "python")
SC_DIR = os.path.join(REPO_ROOT, "shipment-creator")
DPC_PY = os.path.join(REPO_ROOT, "direct-pickup-checks", ".venv", "bin", "python")
DPC_DIR = os.path.join(REPO_ROOT, "direct-pickup-checks")

sys.path.insert(0, HERE)
import photo_select_trained as pst         # noqa: E402  (trained corner model, this venv)


def _imgs(d):
    out = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
        out += glob.glob(os.path.join(d, ext))
    return sorted(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--vin-max", type=int, default=3, help="max VIN-plate photos")
    a = ap.parse_args()
    vin = a.vin.strip().upper()

    dest = os.path.join(a.out, vin)
    src = os.path.join(dest, "_source")
    os.makedirs(src, exist_ok=True)

    # 1) fetch latest delivered shipment's Delivery photos (shipment-creator venv)
    if not os.path.exists(SC_PY):
        sys.exit(f"shipment-creator venv missing ({SC_PY}) — needed to fetch via the SD API.")
    info_path = os.path.join(dest, "_fetch.json")
    print(f"[1/3] fetching latest delivered shipment for {vin}…")
    subprocess.run([SC_PY, os.path.join(HERE, "fetch_latest.py"),
                    "--vin", vin, "--out", src, "--info", info_path],
                   cwd=SC_DIR, check=False)
    info = json.load(open(info_path)) if os.path.exists(info_path) else {"ok": False}
    if not info.get("ok"):
        sys.exit(f"no delivered shipment with photos found for {vin}.")
    print(f"      order {info.get('number')} ({info.get('status')}, {info.get('date')}): "
          f"{info.get('n_photos')} photos")
    paths = _imgs(src)
    if not paths:
        sys.exit("no photos downloaded.")

    # 2) VIN-plate via on-device OCR FIRST (separate process frees its GPU before CLIP
    #    loads), no Claude (direct-pickup-checks venv)
    print(f"[2/3] OCR: looking for the VIN plate among {len(paths)} photo(s)…")
    ocr_out = os.path.join(dest, "_ocr.json")
    ocr_res = {"matched": [], "best": None}
    if os.path.exists(DPC_PY):
        subprocess.run([DPC_PY, os.path.join(HERE, "ocr_vin.py"),
                        "--dir", src, "--vin", vin, "--out", ocr_out, "--max", str(a.vin_max)],
                       cwd=DPC_DIR, check=False)
        if os.path.exists(ocr_out):
            ocr_res = json.load(open(ocr_out))
    else:
        print(f"      (skipped — direct-pickup-checks venv missing at {DPC_PY})")

    # 3) corner model -> sides (this venv, trained head). Exclude the OCR-matched VIN
    #    photos so a VIN/closeup can never be picked as one of the four sides.
    print(f"[3/3] selecting corner photos with the trained model "
          f"(excluding {len(ocr_res.get('matched', []))} VIN photo(s))…")
    sel = pst.select_corner_photos(paths, exclude_paths=ocr_res.get("matched", []))

    # ---- assemble the 3 sections ----
    sides_d = os.path.join(dest, "sides")
    vin_d = os.path.join(dest, "vin_plate")
    key_d = os.path.join(dest, "key")
    for d in (sides_d, vin_d, key_d):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    # The app's "All Four Sides" accepts EXACTLY 4 photos. Pick the top 4 the model
    # found, in this priority (most important first). Files are numbered so they push
    # in a deterministic order for the picker.
    SIDES_PRIORITY = ["front", "rear", "front_left", "rear_right", "front_right", "rear_left"]
    chosen = [s for s in SIDES_PRIORITY if 0 <= sel.picks.get(s, -1) < len(paths)][:4]
    sides = []
    for order_i, slot in enumerate(chosen):
        shutil.copy2(paths[sel.picks[slot]], os.path.join(sides_d, f"{order_i}_{slot}.jpg"))
        sides.append(slot)

    # Key candidate FIRST (so the VIN slot can fall back to it): the most-confident
    # white/black key photo, or None if none was positively identified.
    ki, kcls, kp = pst.best_key(sel.scores)

    # VIN plate: the single best on-device OCR match. If OCR matched nothing, fall back
    # (min-1 must still be met, flagged) in this order:
    #   1) the KEY photo, if a key was positively identified,
    #   2) else the REAR (then front) photo,
    #   3) else the top side.
    best = ocr_res.get("best")
    vin_fallback = False
    vin_fallback_source = None
    if not best:
        vin_fallback = True
        if ki is not None:                                  # no VIN plate -> use the key photo
            best = paths[ki]
            vin_fallback_source = "key"
        else:
            ri, fi = sel.picks.get("rear", -1), sel.picks.get("front", -1)
            idx = ri if ri >= 0 else fi
            if idx >= 0:
                best = paths[idx]
                vin_fallback_source = "rear" if ri >= 0 else "front"
            elif chosen:
                best = os.path.join(sides_d, f"0_{chosen[0]}.jpg")
                vin_fallback_source = chosen[0]
    vin_plate = []
    if best:
        shutil.copy2(best, os.path.join(vin_d, "vin.jpg"))
        vin_plate = [os.path.basename(best)]

    # Key: the most-confident key photo (white/black) if identified; else the VIN photo
    # (which itself may be the rear fallback). Only ONE key photo is used.
    key_src = paths[ki] if ki is not None else best
    key_used = (f"{kcls} ({kp})" if ki is not None
                else "vin_plate" if ocr_res.get("best") else (vin_fallback_source or "rear"))
    key = []
    if key_src:
        shutil.copy2(key_src, os.path.join(key_d, "key.jpg"))
        key = [os.path.basename(key_src)]

    pst._annotated_sheet(paths, sel, os.path.join(dest, "picks.png"))
    manifest = {"vin": vin, "shipment": info, "sides": sides, "vin_plate": vin_plate,
                "key": key, "key_source": key_used, "n_sides": len(sides),
                "vin_plate_found": bool(ocr_res.get("best")), "vin_fallback": vin_fallback,
                "vin_fallback_source": vin_fallback_source}
    json.dump(manifest, open(os.path.join(dest, "manifest.json"), "w"), indent=2)

    # ---- report ----
    print(f"\n=== {vin} -> {dest} ===")
    print(f"  sides ({len(sides)}/4): {sides}")
    print(f"  vin_plate: {vin_plate or '— none —'}" + (f"  (FALLBACK: no VIN plate -> {vin_fallback_source})" if vin_fallback else ""))
    print(f"  key:       {key or '— none —'}  (source: {key_used})")
    if len(sides) < 4:
        print(f"  ⚠ only {len(sides)} sides — model found fewer than 4 of the priority corners.")


if __name__ == "__main__":
    main()
