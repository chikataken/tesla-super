"""
Scrape VINs only (no photos) from the SuperDispatch Invoiced/delivered orders list.

The public SD API has no list endpoint, so we use tesla-reconcile's logged-in web
session just to READ VIN strings off the orders list over a randomized delivered-on
window. Photos are NOT downloaded here — fetch_api.py pulls them via the official API.

MUST run in tesla-reconcile's venv (Playwright + the shared SD login):
    tesla-reconcile/.venv/bin/python trainer/scrape_vins.py --n 60 --out /tmp/vins.json

Writes {"vins": [...], "window": ["start","end"]} to --out (stdout stays clean so
the orchestrator can capture it; progress goes to stderr). Headless by default.
"""
from __future__ import annotations
import argparse
import json
import os
import random
import re
import sys
from datetime import date, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TR_DIR = os.path.join(REPO_ROOT, "tesla-reconcile")
# Attach to the SAME shared Chrome session all the other tools use (CDP on :9222 with
# the shared profile) — set BEFORE importing tesla-reconcile's config (it reads env at
# import). Pinning cdp here guarantees we attach even if cwd/.env resolution differs;
# never fall back to a separate headless 'launch' browser (which isn't logged in).
os.environ.setdefault("AUTH_MODE", "cdp")
os.environ.setdefault("CDP_URL", "http://127.0.0.1:9222")
os.environ.setdefault("CDP_PROFILE_DIR", os.path.join(TR_DIR, ".auth"))
os.environ.setdefault("WINDOW_MODE", "ghost")    # off-screen real window (background pulls)
sys.path.insert(0, TR_DIR)

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")   # 17 alnum, no I/O/Q (VIN charset)


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="target number of VINs to collect")
    ap.add_argument("--pages", type=int, default=8, help="max list pages to scan")
    ap.add_argument("--window-days", type=int, default=45)
    ap.add_argument("--lookback-days", type=int, default=700)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.seed is not None:
        random.seed(a.seed)

    import superdispatch as sd
    from auth import browser_context

    end = date.today() - timedelta(days=random.randint(0, max(0, a.lookback_days)))
    start = end - timedelta(days=a.window_days)
    _log(f"scrape window {start} .. {end}")

    vins: set[str] = set()
    with browser_context() as ctx:
        page = ctx.new_page()
        for pn in range(1, a.pages + 1):
            if len(vins) >= a.n:
                break
            page.goto(sd.invoiced_url(start, end, page=pn, ascending=True))
            page.wait_for_timeout(1200)
            try:
                txt = page.inner_text("body")
            except Exception:
                txt = page.content()
            found = set(VIN_RE.findall(txt))
            _log(f"  page {pn}: +{len(found - vins)} VIN(s)")
            if not found and pn == 1:
                _log("  (no VINs on page 1 — empty window or not logged in)")
                break
            vins |= found

    with open(a.out, "w") as fh:
        json.dump({"vins": sorted(vins)[:max(a.n, len(vins))],
                   "window": [str(start), str(end)]}, fh)
    _log(f"scraped {len(vins)} VIN(s) -> {a.out}")


if __name__ == "__main__":
    main()
