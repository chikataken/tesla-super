"""Backfill the shipment recorder DB by scraping Super Dispatch order-status tabs.

Webhooks (the eventual live feed) are forward-only, so to mirror EXISTING shipments
we enumerate them from the Shipper TMS web UI — the API has no list endpoint (see
config.py). Each order sits on exactly one lifecycle tab:

    new -> posted -> requests/pending -> accepted -> picked_up -> delivered
        -> invoiced -> paid

ACTIVE tabs are small and current, so we scan them fully. HISTORY tabs (delivered/
invoiced/paid) are huge, so we restrict them to a delivered-on-date window of the
last N months. Cross-cutting tabs (flagged/archived/deleted/declined/on_hold) are
skipped — they aren't lifecycle states and would clobber an order's real status.

    python recorder_backfill.py                 # last 2 months
    python recorder_backfill.py --months 3
    python recorder_backfill.py --max-pages 40  # cap pages/tab (safety)
    HEADLESS=true python recorder_backfill.py    # server / no desktop
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys

import auth
import config
import recorder_db as rdb
import recorder_scrape as rscrape
import sd_login

ACTIVE_TABS = ["new", "on_hold", "posted_to_lb", "requests", "pending",
               "accepted", "picked_up"]
HISTORY_TABS = ["delivered", "invoiced", "paid"]      # windowed by delivery date


def run(months: int, max_pages: int | None, tabs: list[str] | None,
        history_tabs: list[str] | None, log=print) -> dict:
    today = dt.date.today()
    start = today - dt.timedelta(days=30 * months)
    window = (start.isoformat(), today.isoformat())
    occurred_day = today.isoformat()
    active = tabs if tabs is not None else ACTIVE_TABS
    history = history_tabs if history_tabs is not None else HISTORY_TABS

    rdb.init()
    con = rdb.connect()
    summary: dict[str, int] = {}
    new_events = 0
    try:
        with auth.browser_context() as ctx:
            page = ctx.new_page()
            try:
                st = sd_login.ensure_session(page)
                log(f"[login] {st}")
                if str(st).lower() not in ("ok", "login_ok"):
                    log("[login] no SuperDispatch session — aborting "
                        "(run: python sd_login.py to log in once)")
                    return {"error": f"not_logged_in:{st}"}

                plan = [(t, None) for t in active] + [(t, window) for t in history]
                for tab, win in plan:
                    label = rscrape.TAB_STATUS.get(tab, tab)
                    log(f"\n=== {tab} ({label}){' window ' + str(win) if win else ' full'} ===")
                    recs = rscrape.scan_tab(page, tab, window=win,
                                            max_pages=max_pages, log=log)
                    for o in recs:
                        rdb.upsert_order(con, o)
                        if rdb.add_event(con, o, occurred_day):
                            new_events += 1
                    con.commit()
                    summary[label] = len(recs)
                    log(f"--- {tab}: {len(recs)} orders")
            finally:
                try: page.close()
                except Exception: pass

        rdb.set_meta(con, "last_backfill", {
            "at": occurred_day, "months": months, "window": window,
            "summary": summary, "new_events": new_events,
            "total_orders": rdb.total(con)})
        con.commit()
        log("\n================ BACKFILL SUMMARY ================")
        for k, v in summary.items():
            log(f"  {k:12} {v}")
        log(f"  {'NEW EVENTS':12} {new_events}")
        log(f"  {'TOTAL ORDERS':12} {rdb.total(con)}")
        log(f"  by status: {rdb.counts_by_status(con)}")
        return {"summary": summary, "new_events": new_events,
                "total_orders": rdb.total(con), "window": window}
    finally:
        con.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=2)
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap pages per tab (default: config.SD_SCAN_MAX_PAGES)")
    ap.add_argument("--tabs", help="comma-sep override of ACTIVE tabs")
    ap.add_argument("--history-tabs", help="comma-sep override of HISTORY tabs")
    a = ap.parse_args(argv)
    tabs = ([t for t in a.tabs.split(",") if t]
            if a.tabs is not None else None)
    htabs = ([t for t in a.history_tabs.split(",") if t]
             if a.history_tabs is not None else None)
    res = run(a.months, a.max_pages, tabs, htabs)
    return 1 if res.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
