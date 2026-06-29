"""
Fetch the LATEST DELIVERED shipment's photos for a VIN, via the official SD API.

A VIN can sit on several orders over time; we always take the most recent DELIVERED
one (older shipments aren't relevant). "Latest" = among the VIN's orders that are
delivered and have Delivery photos, the one whose newest Delivery photo is most
recent. Downloads that order's Delivery photos to --out.

MUST run in shipment-creator's venv (imports sd_api / sd_photos):
    shipment-creator/.venv/bin/python fetch_latest.py --vin <VIN> --out <dir> --info <json>
"""
from __future__ import annotations
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SC_DIR = os.path.join(REPO_ROOT, "shipment-creator")
sys.path.insert(0, SC_DIR)

import sd_api        # noqa: E402
import sd_photos     # noqa: E402


def latest_delivered(vin: str, ptype: str = "Delivery"):
    """Return (order, photo_items, latest_created_at, status) for the latest delivered
    shipment with photos of this VIN, or None."""
    cands = []
    for o in (sd_api.find_by_vin(vin) or []):
        g = o.get("guid")
        if not g:
            continue
        full = sd_api.get_order(g)
        items = sd_photos.photos(full, vin, ptype)
        if not items:
            continue
        latest = max((p.get("created_at") or "") for p in items)
        cands.append((full, items, latest, (full.get("status") or "").lower()))
    if not cands:
        return None
    # delivered first, then most-recent photo timestamp
    cands.sort(key=lambda c: (c[3] == "delivered", c[2]), reverse=True)
    return cands[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--type", default="Delivery")
    ap.add_argument("--info", required=True, help="write result JSON here")
    a = ap.parse_args()

    res = latest_delivered(a.vin.strip().upper(), a.type)
    info = {"ok": False, "vin": a.vin}
    if res:
        full, items, latest, status = res
        os.makedirs(a.out, exist_ok=True)
        saved = sd_photos.download(items, a.out)
        info = {"ok": bool(saved), "vin": a.vin, "guid": full.get("guid"),
                "number": full.get("number"), "status": status, "date": latest,
                "n_photos": len(saved), "out": a.out, "type": a.type}
    with open(a.info, "w") as fh:
        json.dump(info, fh)
    print(json.dumps(info))


if __name__ == "__main__":
    main()
