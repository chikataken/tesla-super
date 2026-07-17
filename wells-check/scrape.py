"""Scrape the SuperDispatch PAID tab into the local wells.db — phase 1 of the
Wells Fargo check reconciliation.

Reuses tesla-reconcile's battle-tested pieces (imported straight from that project,
not copied): the shared logged-in Chrome over CDP (auth.browser_context, one window
of the one Chrome on :9222) and its SD auto-login (sd_login).

Per Paid-tab page it records, for every order card, WITHOUT opening the order:
  * the order guid (from the card's /orders/view/<guid> link — the same guid the
    public API uses, so enrich.py can fetch the order directly)
  * the order id text (e.g. A55H890)
  * the first VIN visible on the card (the API-lookup fallback key)

The Paid tab's default sort is Updated (newest first), so the scan starts at the
most recently paid orders and pages BACKWARDS in time. Progress is committed to
scan_state after every page — Ctrl+C any time; the next run resumes at that page.
New paid orders arriving mid-scan shift pages down; the guid PRIMARY KEY makes
the resulting re-sightings harmless.

    ./run.sh                    # scan until the end of the Paid tab (Ctrl+C to pause)
    ./run.sh --pages 50         # scan at most 50 pages this run
    ./run.sh --restart          # forget the scan position, start over from page 1
"""
from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RECONCILE = os.path.join(os.path.dirname(HERE), "tesla-reconcile")
sys.path.insert(1, RECONCILE)          # [0] stays wells-check, so `import db` is OURS

import db                               # noqa: E402  (wells-check/db.py)
import auth                             # noqa: E402  (tesla-reconcile)
import config as rc                     # noqa: E402  (tesla-reconcile config: SD_BASE, CDP)
import sd_login                         # noqa: E402  (tesla-reconcile SD auto-login)

PAID_URL = rc.SD_BASE.rstrip("/") + "/orders/paid?page={page}"
_GUID_RE = re.compile(r"/orders/view/([0-9a-f\-]{32,36})")

# Recycle the SD tab this often: a long run of SD-SPA navigations piles up renderer
# memory that only DROPPING THE TAB returns (same lesson as tesla-reconcile's
# SCAN_RECYCLE_EVERY — a soft reload or CDP purge doesn't cut it, and the purge
# crashes the SD renderer). Keeps an indefinite scan flat on RAM.
RECYCLE_EVERY = 25

# In-page card reader, document-order technique (same idea as tesla-reconcile's
# _SCRAPE_JS): every element is either an order link (starts a card) or, if it's a
# childless node whose text is exactly a 17-char VIN, the preview VIN of the most
# recent preceding card. No click-throughs, one evaluate per page.
_CARDS_JS = r"""
() => {
  const vinRe = /^[A-HJ-NPR-Z0-9]{17}$/;
  const els = [...document.querySelectorAll("a[href*='/orders/view/'], p, td, span, div")];
  const seen = new Map(); const out = []; let cur = null;
  for (const el of els) {
    if (el.matches("a[href*='/orders/view/']")) {
      const href = el.getAttribute('href') || '';
      if (!seen.has(href)) {
        cur = {id: (el.textContent || '').trim().split('\n')[0], href, vin: null};
        seen.set(href, cur); out.push(cur);
      } else { cur = seen.get(href); }
    } else if (cur && !cur.vin && el.children.length === 0) {
      const t = (el.textContent || '').trim();
      if (vinRe.test(t)) cur.vin = t;
    }
  }
  return out;
}
"""


def _ensure_logged_in(page, url: str) -> None:
    if not sd_login.is_login_page(page):
        return
    if sd_login.ensure_logged_in(page):
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
    if sd_login.is_login_page(page):
        raise RuntimeError(
            "Not logged into SuperDispatch and auto-login did not complete. "
            "Run tesla-reconcile's `./run.sh login`, sign in, then retry.")


def scrape_page(page, pageno: int) -> list[dict]:
    """One Paid-tab page -> [{guid, id, vin}]. [] means the page is past the end."""
    url = PAID_URL.format(page=pageno)
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")
    _ensure_logged_in(page, url)
    try:
        # The Paid list is huge (45k+ orders) — its first query can be slow.
        page.locator("a[href*='/orders/view/']").first.wait_for(timeout=45000)
    except Exception:
        body = ""
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            pass
        if "0 orders" in body or "No orders" in body.lower():
            return []                                   # past the last page — scan complete
        raise RuntimeError(f"No order cards at {url} and it doesn't look like the empty "
                           f"page — login/layout problem? (body starts: {body[:120]!r})")
    rows = []
    for r in page.evaluate(_CARDS_JS):
        m = _GUID_RE.search(r.get("href") or "")
        if m and r.get("id"):
            rows.append({"guid": m.group(1), "id": r["id"], "vin": r.get("vin")})
    return rows


def _enrich_pending(limit: int, timeout: int | None = 900) -> None:
    """Run the API enrichment (enrich.py) in a SUBPROCESS. It must not share this
    process: enrich imports shipment-creator's sd_api + config, which would collide
    with the tesla-reconcile config/auth modules already loaded here. WAL + busy
    timeouts make the concurrent sqlite access safe."""
    cmd = [sys.executable, os.path.join(HERE, "enrich.py")]
    if limit:
        cmd += ["--limit", str(limit)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
        if lines:
            print("  enrich: " + lines[-1].strip(), flush=True)
        if r.returncode != 0:
            err = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
            print("  ! enrich failed: " + (err[-1] if err else f"exit {r.returncode}"),
                  flush=True)
    except Exception as e:                               # noqa: BLE001 - never kill the scan
        print(f"  ! enrich failed: {e}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape SD Paid tab into wells.db (resumable)")
    ap.add_argument("--pages", type=int, default=0,
                    help="max pages this run; 0 = until the end of the Paid tab")
    ap.add_argument("--restart", action="store_true",
                    help="reset the scan position and start over from page 1")
    ap.add_argument("--topup", action="store_true",
                    help="catch NEWLY-paid orders only: scan from page 1 and stop after "
                         "--topup-stale consecutive pages with no new guids. Does NOT "
                         "move the deep backfill cursor — run any time, even mid-backfill.")
    ap.add_argument("--topup-stale", type=int, default=3,
                    help="consecutive all-known pages that end a --topup run (default 3)")
    ap.add_argument("--recycle", type=int, default=RECYCLE_EVERY,
                    help=f"drop+reopen the SD tab every N pages to keep renderer RAM flat "
                         f"(default {RECYCLE_EVERY}; 0 = never)")
    args = ap.parse_args()

    con = db.connect()
    if args.restart:
        db.set_state(con, "next_page", 1)
        db.set_state(con, "scan_done", "")
    start = 1 if args.topup else int(db.get_state(con, "next_page", "1"))
    db.set_state(con, "last_run_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    if args.topup:
        print(f"Top-up: scanning newly-paid orders from page 1 until {args.topup_stale} "
              f"consecutive pages show nothing new. The backfill cursor stays put.")
    else:
        print(f"Scanning SD Paid tab from page {start} "
              f"({'all remaining pages' if not args.pages else f'up to {args.pages} pages this run'}). "
              f"Ctrl+C to pause — the position is saved after every page.")

    done_pages = new_total = stale = 0
    with auth.browser_context() as ctx:
        # The blank ANCHOR tab exists so mid-run tab recycling stays in OUR window:
        # auth's shared-Chrome wrapper opens "later" tabs in the window of the first
        # still-open tab — if the scan tab (recycled = closed) were the only one, the
        # replacement could land in a concurrent tool's window.
        anchor = ctx.new_page()                          # noqa: F841 - kept open on purpose
        page = ctx.new_page()
        since_recycle = 0
        pageno = start
        while True:
            if args.pages and done_pages >= args.pages:
                print(f"Reached this run's --pages limit. Resume later from page {pageno}.")
                break
            rows = scrape_page(page, pageno)
            if not rows:
                db.set_state(con, "scan_done", time.strftime("%Y-%m-%dT%H:%M:%S"))
                print(f"Page {pageno} is empty — reached the end of the Paid tab. Scan complete.")
                break
            new = sum(db.upsert_scraped(con, r["guid"], r["id"], r["vin"], pageno) for r in rows)
            con.commit()
            new_total += new
            done_pages += 1
            pageno += 1
            if not args.topup:
                db.set_state(con, "next_page", pageno)
            s = db.stats(con)
            print(f"  page {pageno - 1}: {len(rows)} cards, +{new} new "
                  f"(db total {s['scraped']})", flush=True)
            if args.topup:
                # Stop once the front of the list is all known: a page with 0 new guids
                # means we've reached territory the backfill (or a prior top-up) covered.
                # A few stale pages are tolerated because a just-UPDATED old order can
                # resurface at the front without being new.
                stale = stale + 1 if new == 0 else 0
                if stale >= args.topup_stale:
                    print(f"  {stale} consecutive pages with nothing new — top-up complete.")
                    break
            else:
                # Interleaved API pass: enrich right behind the scan so the DB (and the
                # Checks tab) fills with amounts + check #s as pages land, and a Ctrl+C
                # loses nothing. Limit > page size so any backlog steadily catches up.
                _enrich_pending(60)
            since_recycle += 1
            if args.recycle and since_recycle >= args.recycle:
                print("  [mem] recycling the SD tab (drops its renderer to reclaim RAM)")
                try:
                    page.close(run_before_unload=False)
                except Exception:                        # noqa: BLE001 - already gone
                    pass
                page = ctx.new_page()
                since_recycle = 0
            time.sleep(0.4)                             # be gentle with the site
    if not args.topup:
        # Drain whatever the per-page passes didn't finish (incl. rows from older runs).
        print("Draining remaining API enrichment…")
        _enrich_pending(0, timeout=None)
    s = db.stats(con)
    print(f"Run done: {done_pages} page(s), +{new_total} new orders. DB: {s['scraped']} scraped, "
          f"{s['enriched']} enriched, {s['distinct_references']} distinct check #s."
          + ("" if not args.topup else " (top-up: run ./run.sh enrich to fill new rows)"))


if __name__ == "__main__":
    main()
