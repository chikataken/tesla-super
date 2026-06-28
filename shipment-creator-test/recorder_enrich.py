"""Enrich recorder rows from the SD API: fill created_at + api_guid + full details,
refine status, and replace pickup/delivery with clean venue fields (no scrape bleed).

The backfill scrape can't see an order's created date or full details, so this pass
calls get_order(guid) for each row. Idempotent / resumable: by default it only visits
rows missing created_at, so re-running continues where it left off.

    python recorder_enrich.py            # only rows missing created_at (resume)
    python recorder_enrich.py --all      # re-enrich every row
"""
from __future__ import annotations
import sys
import recorder_db as rdb
import sd_api


def main(argv: list[str]) -> int:
    all_rows = "--all" in argv
    rdb.init()
    con = rdb.connect()
    where = "" if all_rows else "WHERE created_at IS NULL"
    rows = con.execute(f"SELECT web_uuid FROM orders {where}").fetchall()
    total = len(rows)
    print(f"enriching {total} row(s){' (all)' if all_rows else ' (missing created_at)'}", flush=True)
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        wu = r["web_uuid"]
        try:
            o = sd_api.get_order(wu)
            rdb.enrich_order(con, o)
            ok += 1
        except Exception as e:                               # noqa: BLE001
            fail += 1
            if fail <= 10:
                print(f"  fail {wu[:8]}: {str(e)[:120]}", flush=True)
        if i % 50 == 0:
            con.commit()
            print(f"  {i}/{total}  ok={ok} fail={fail}", flush=True)
    con.commit()
    print(f"DONE {total}  ok={ok} fail={fail}  "
          f"created_at_set={con.execute('SELECT COUNT(*) n FROM orders WHERE created_at IS NOT NULL').fetchone()['n']}",
          flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
