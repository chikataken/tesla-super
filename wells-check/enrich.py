"""Enrich scraped paid orders with the payment block — phase 2.

For every scraped row without enrichment, fetch the full order from the SD public
API (client borrowed from shipment-creator — same OAuth creds) and record the
carrier total (price), the payment REFERENCE NUMBER (the Wells Fargo check #),
method, sent date and every VIN on the order.

Fetch strategy: the card's /orders/view/<guid> IS the public-API guid (verified
against the recorder mirror), so the primary path is one get_order(guid) call.
The find-by-VIN path the plan originally called for is kept as the FALLBACK for
guids the API refuses (rare: deleted/410) — the preview VIN is searched and the
result matched back by order number.

Resumable by nature: only rows with no enrichment are processed; each row commits
on completion. Rows that error are marked (enrich_error) and skipped next run —
clear the column to retry them.

    ./run.sh enrich                 # everything pending
    ./run.sh enrich --limit 200     # a bounded batch
"""
from __future__ import annotations
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPMENT_CREATOR = os.path.join(os.path.dirname(HERE), "shipment-creator")
sys.path.insert(1, SHIPMENT_CREATOR)   # [0] stays wells-check, so `import db` is OURS

import db                               # noqa: E402  (wells-check/db.py)
import sd_api                           # noqa: E402  (shipment-creator SD API client)

THROTTLE_S = 0.15                       # ~6-7 calls/s; sd_api also honors 429 Retry-After


def _is_auth(err: str) -> bool:
    s = err.lower()
    return " -> 401" in s or " -> 403" in s or "credential" in s


def _is_transient(err: str) -> bool:
    """Failures that say nothing about THIS order (rate limit, SD hiccup, network) —
    the row must stay PENDING for a later run, never be marked failed."""
    s = err.lower()
    return any(t in s for t in (" -> 429", " -> 500", " -> 502", " -> 503", " -> 504",
                                "timeout", "timed out", "connection", "failed after"))


def _fetch_by_vin(vin: str, order_id: str) -> dict | None:
    """Fallback: find the order through its preview VIN, matched by order number."""
    if not vin:
        return None
    for short in sd_api.find_by_vin(vin) or []:
        guid = short.get("guid") or short.get("order_guid")
        if not guid:
            continue
        try:
            o = sd_api.get_order(guid)
        except sd_api.SDError:
            continue
        if (o.get("number") or "").strip().upper() == (order_id or "").strip().upper():
            return o
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill price + check reference from the SD API")
    ap.add_argument("--limit", type=int, default=0, help="max rows this run; 0 = all pending")
    args = ap.parse_args()

    con = db.connect()
    todo = db.unenriched(con, args.limit)
    if not todo:
        print("Nothing pending — every scraped order is enriched (or marked failed).")
        return
    print(f"Enriching {len(todo)} order(s) via the SD API…")
    ok = fell_back = failed = deferred = 0
    for i, r in enumerate(todo, 1):
        if i > 1:
            time.sleep(THROTTLE_S)
        o = None
        err = ""
        try:
            o = sd_api.get_order(r["guid"])
        except sd_api.SDError as e:
            err = str(e)
            # An auth failure says nothing about this order and would repeat for every
            # remaining row — abort the RUN, leaving all untouched rows pending.
            if _is_auth(err):
                print(f"  ! auth error from the SD API — aborting, rows stay pending: {err}")
                break
            if _is_transient(err):
                deferred += 1                 # leave PENDING: retried automatically next run
            else:
                try:
                    o = _fetch_by_vin(r["vin_preview"], r["order_id"])
                    if o:
                        fell_back += 1
                except sd_api.SDError as e2:
                    err = f"{err} | vin fallback: {e2}"
        if o:
            db.save_enrichment(con, r["guid"], o)
            ok += 1
        elif err and not _is_transient(err):
            # Only a definitive per-order failure (deleted/404/410 + no VIN match) is
            # marked; cleared enrich_error columns get retried.
            db.mark_error(con, r["guid"], err or "order not found via guid or VIN")
            failed += 1
        if i % 50 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  ok={ok} (vin-fallback {fell_back})  "
                  f"failed={failed}  deferred={deferred}", flush=True)
    s = db.stats(con)
    print(f"Done. DB: {s['scraped']} scraped, {s['enriched']} enriched, "
          f"{s['with_reference']} with a check #, {s['distinct_references']} distinct checks, "
          f"{s['errors']} errors.")


if __name__ == "__main__":
    main()
