"""
Find which photos show the VIN plate, by ON-DEVICE OCR (no Claude, ever).

Reuses the direct-pickup-checks easyOCR reader (ocr.scan_for_vin), which returns the
photos whose OCR text contains the expected VIN (ranked best-first, with a rotation
fallback for sideways stickers). A non-empty result = the VIN was matched.

MUST run in direct-pickup-checks' venv (has ocr.py + easyocr):
    direct-pickup-checks/.venv/bin/python ocr_vin.py --dir <photos> --vin <VIN> --out <json>
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DPC_DIR = os.path.join(REPO_ROOT, "direct-pickup-checks")
sys.path.insert(0, DPC_DIR)

import ocr          # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--vin", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=3, help="max VIN-plate photos to return")
    a = ap.parse_args()

    paths = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
        paths += glob.glob(os.path.join(a.dir, ext))
    paths = sorted(paths)
    imgs = [open(p, "rb").read() for p in paths]

    idxs = ocr.scan_for_vin(imgs, a.vin.strip().upper()) if imgs else []
    matched = [paths[i] for i in idxs][:a.max]
    out = {"vin": a.vin, "matched": matched,
           "best": matched[0] if matched else None, "scanned": len(paths)}
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    print(json.dumps({"matched": [os.path.basename(m) for m in matched],
                      "scanned": len(paths)}))


if __name__ == "__main__":
    main()
