"""
Fetch Delivery photos for a list of VINs via the OFFICIAL SuperDispatch API (no
scraping, no browser) — the same API path the other tools use. Dedups on the order
(shipment) GUID via seen_db so the SAME shipment is never pulled twice; the same VIN
on a DIFFERENT shipment is allowed (different photos).

MUST run in shipment-creator's venv (it imports sd_photos / sd_api):
    shipment-creator/.venv/bin/python trainer/fetch_api.py \
        --pool trainer/pool --db trainer/seen.db --max-new 20 --vins-json /tmp/vins.json

Writes photos to <pool>/<VIN>__<guid8>/NN_*.jpg and {"new":k,"checked":m} to --out.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SC_DIR = os.path.join(REPO_ROOT, "shipment-creator")
sys.path.insert(0, SC_DIR)        # sd_photos / sd_api (official API client)
sys.path.insert(0, HERE)          # seen_db

import seen_db                                       # noqa: E402


def _short(guid: str) -> str:
    return "".join(c for c in (guid or "") if c.isalnum())[:8] or "ship"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--max-new", type=int, default=20, help="stop after this many NEW shipments")
    ap.add_argument("--vins-json", required=True, help='JSON file: {"vins":[...]}')
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import sd_photos

    vins = json.load(open(a.vins_json)).get("vins", [])
    os.makedirs(a.pool, exist_ok=True)
    con = seen_db.connect(a.db)
    new = checked = 0
    for vin in vins:
        if new >= a.max_new:
            break
        checked += 1
        try:
            order = sd_photos.order_for_vin(vin)          # find_by_vin -> get_order (API)
            if not order:
                continue
            guid = order.get("guid")
            if not guid or seen_db.is_seen(con, guid):     # shipment already pulled
                continue
            items = sd_photos.photos(order, vin, "Delivery")
            if not items:
                seen_db.mark(con, guid, [vin], 0)          # no Delivery photos -> don't retry
                continue
            dest = os.path.join(a.pool, f"{vin}__{_short(guid)}")
            saved = sd_photos.download(items, dest)
            seen_db.mark(con, guid, [vin], len(saved))
            if saved:
                new += 1
                _log(f"  + {vin} (order {guid[:8]}): {len(saved)} photo(s)")
        except Exception as e:                             # noqa: BLE001
            _log(f"  ! {vin}: {e}")

    with open(a.out, "w") as fh:
        json.dump({"new": new, "checked": checked, "total_seen": seen_db.count(con)}, fh)
    _log(f"fetched {new} new shipment(s) from {checked} VIN(s)")


if __name__ == "__main__":
    main()
