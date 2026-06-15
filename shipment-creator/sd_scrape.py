"""
SuperDispatch web order-list scan (Posted + Accepted tabs) — browser-scraped.

WHY THIS EXISTS: the Shipper API has no list-all and no route search, so to find
existing live shipments already running a route, we have to read the web UI. This
pages the Posted and Accepted order-status tabs, reads each row's route zips + a
VIN, and keeps only rows whose (pickup_zip, dropoff_zip) match one of the Excel's
(originZip, destinationZip) pairs (a cheap zip-level heuristic). The kept VINs are
resolved to GUIDs + EXACT addresses later, by the caller, via the API
(sd_api.find_by_vin / get_order). Exact-address matching happens there, not here.

NOTE ON TABS: "Accepted" (/orders/accepted) is what the user calls "Approved" — a
carrier's offer was approved/accepted. "Posted" (/orders/posted_to_lb) = posted to
the SuperDispatch loadboard. There is no separate "Approved" tab in the UI.

Reuses the shared logged-in Chrome (auth.browser_context — CDP attach on Windows).
You must be logged into SuperDispatch in that profile once.

Selectors VERIFIED against the live site 2026-06: the tabs are the order-status
lists (NOT a /loadboard route — that redirects to /orders), URL-paginated with
?page=N, and each row carries the order link, "City, ST ZIP" for pickup then
delivery, and the VIN, all in document order. On a miss the scan saves
output/sd_scan_*.png and logs which selector to adjust. The pure parsing
(_cards_to_hits) is unit-tested; the DOM walk is not.
"""
from __future__ import annotations
import os
import re
import time

import auth
import config

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
ZIP_RE = re.compile(r"\b\d{5}\b")
# A ZIP that follows a 2-letter state, i.e. the "City, ST 08857" venue format the
# live rows use. Preferred over a bare 5-digit run so a stray 5-digit number on the
# row (an id, a price like 12500) can't be mistaken for a route zip.
STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+(\d{5})\b")

# ===================== SELECTORS (verified live 2026-06) =====================
# The "Posted" and "Accepted" order-status tabs. Both are URL-addressable and
# paginate with ?page=N (a /loadboard route does NOT exist — it redirects to
# /orders). "Accepted" is the tab the user refers to as "Approved".
TAB_URL = {
    "posted":   config.SD_WEB_BASE + "/orders/posted_to_lb",
    "accepted": config.SD_WEB_BASE + "/orders/accepted",
    "pending":  config.SD_WEB_BASE + "/orders/pending",
}
TAB_LABEL = {"posted": "Posted", "accepted": "Accepted", "pending": "Pending"}
# The tabs scanned, in order. ("Accepted" is the user's "Approved".)
SCAN_TABS = ("posted", "accepted", "pending")
LOADBOARD_URL = config.SD_WEB_BASE + "/orders"               # tab-click fallback base
ORDER_LINK = "a[href*='/orders/view/']"                      # the per-order row link

# Walk the page in DOCUMENT ORDER, attaching each VIN/leaf-text to the most recent
# preceding order link (do NOT climb to a 'card' container — that grabs neighbours,
# the exact bug tesla-reconcile documents). Each row renders, in order: the order
# link, pickup "City, ST ZIP", delivery "City, ST ZIP", then the VIN — so the leaf
# text after a link and before the next belongs to that order. Returns one record
# per order. Scoped to /orders/view/ so the page's nav links (/orders/posted_to_lb,
# /orders/new, ...) are NOT mistaken for order rows.
_CARDS_JS = r"""
() => {
  const isVin = /^[A-HJ-NPR-Z0-9]{17}$/;
  const out = []; let cur = null;
  const sel = 'a[href*="/orders/view/"], p, span, div, td';
  document.querySelectorAll(sel).forEach(el => {
    if (el.matches('a[href*="/orders/view/"]')) {
      cur = {href: el.href, id: (el.textContent||'').trim().split('\n')[0], vins: [], text: ''};
      out.push(cur);
      return;
    }
    if (!cur || el.children.length) return;          // leaf nodes only
    const t = (el.textContent || '').trim();
    if (!t) return;
    if (isVin.test(t) && !cur.vins.includes(t)) cur.vins.push(t);
    cur.text += ' ' + t;
  });
  return out;
}
"""
# =============================================================================


def _cards_to_hits(cards: list, status: str, zip_pairs: set) -> list:
    """PURE: turn raw card records (from _CARDS_JS) into matched hits. A card is kept
    only when it has a VIN and its (first zip, last zip) equals an Excel route pair.
    Unit-tested without a browser."""
    hits = []
    for c in cards:
        text = c.get("text", "") or ""
        # Prefer "City, ST ZIP" venue zips (the live row format); fall back to any
        # 5-digit run if no state-anchored zip is present (keeps the bare-zip unit
        # tests working). Either way: first=origin, last=dest in document order.
        zips = STATE_ZIP_RE.findall(text)
        if len(zips) < 2:
            zips = ZIP_RE.findall(text)
        vins = c.get("vins") or []
        if len(zips) < 2 or not vins:
            continue
        pu, do = zips[0], zips[-1]                    # first=origin, last=dest (doc order)
        if (pu, do) not in zip_pairs:
            continue
        hits.append({"loadboard_status": status, "vin": vins[0],
                     "pickup_zip": pu, "dropoff_zip": do,
                     "order_id": c.get("id", ""), "detail_url": c.get("href", "")})
    return hits


def _check_login(page) -> None:
    if "login" in (page.url or "").lower() or page.locator("input[type=password]").count():
        raise RuntimeError(
            "Not logged into SuperDispatch. Open the shared Chrome profile "
            f"({config.CDP_PROFILE_DIR}) and log into {config.SD_WEB_BASE} once, then re-run.")


def _scan_tab(page, status: str, zip_pairs: set) -> list:
    base = TAB_URL.get(status)
    found, seen = [], set()
    for pageno in range(1, config.SD_SCAN_MAX_PAGES + 1):
        if base:
            page.goto(f"{base}{'&' if '?' in base else '?'}page={pageno}")
        elif pageno == 1:
            page.goto(LOADBOARD_URL)
            try:
                page.get_by_role("tab", name=TAB_LABEL[status]).first.click()
            except Exception:
                pass
        else:
            break                                    # no URL pagination available
        page.wait_for_load_state("domcontentloaded")
        _check_login(page)
        try:
            page.locator(ORDER_LINK).first.wait_for(timeout=10000)
        except Exception:
            os.makedirs("./output", exist_ok=True)
            shot = f"./output/sd_scan_{status}_p{pageno}.png"
            try:
                page.screenshot(path=shot)
            except Exception:
                pass
            print(f"  [{status}] page {pageno}: no preview cards found at {page.url} "
                  f"(ORDER_LINK selector likely needs tuning) -> {shot}")
            break
        cards = page.evaluate(_CARDS_JS)
        ids = {c.get("href") for c in cards}
        if not cards or ids <= seen:                 # empty or nothing new -> stop paging
            break
        seen |= ids
        found.extend(_cards_to_hits(cards, status, zip_pairs))
        time.sleep(config.SD_SCAN_THROTTLE_S)
    return found


def scan_loadboard(zip_pairs) -> list:
    """Scan Posted + Accepted + Pending for shipments on the given (origin_zip,
    dest_zip) pairs. SYNC path (own browser); the --create run uses scan_tabs_async.

    Returns deduped [{loadboard_status, vin, pickup_zip, dropoff_zip, order_id,
    detail_url}]. Never raises out of the per-tab scan — a tab failure is logged and
    skipped so the pipeline keeps going."""
    pairs = {(str(a).strip(), str(b).strip()) for a, b in (zip_pairs or []) if a and b}
    if not pairs:
        print("  loadboard scan skipped — no origin/dest zip pairs in the Excel.")
        return []
    out = []
    with auth.browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for status in SCAN_TABS:
            try:
                hits = _scan_tab(page, status, pairs)
                print(f"  [{status}] {len(hits)} card(s) match an Excel route zip-pair")
                out.extend(hits)
            except Exception as e:                   # noqa: BLE001 - isolate per tab
                print(f"  [{status}] scan error: {e}")
    uniq, seen = [], set()
    for h in out:
        k = (h["vin"], h["loadboard_status"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


def _dedupe(hits: list) -> list:
    uniq, seen = [], set()
    for h in hits:
        k = (h["vin"], h["loadboard_status"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


# ===================== ASYNC variant (shares the BOL run's browser) =====================
# Used during a --create run so the SD scan runs CONCURRENTLY with the Tesla BOL
# downloads on the SAME CDP browser, in its own tab. The caller (tesla_bol._run)
# passes an already-open async `page` and CLOSES that tab when this returns. The
# pure parsing (_cards_to_hits) and the DOM walk (_CARDS_JS) are shared with the
# sync path; only the Playwright calls differ (async/await).
async def _check_login_async(page) -> None:
    if "login" in (page.url or "").lower() or await page.locator("input[type=password]").count():
        raise RuntimeError(
            "Not logged into SuperDispatch. Open the shared Chrome profile "
            f"({config.CDP_PROFILE_DIR}) and log into {config.SD_WEB_BASE} once, then re-run.")


async def _scan_tab_async(page, status: str, zip_pairs: set) -> list:
    base = TAB_URL.get(status)
    found, seen = [], set()
    for pageno in range(1, config.SD_SCAN_MAX_PAGES + 1):
        if base:
            await page.goto(f"{base}{'&' if '?' in base else '?'}page={pageno}")
        elif pageno == 1:
            await page.goto(LOADBOARD_URL)
            try:
                await page.get_by_role("tab", name=TAB_LABEL[status]).first.click()
            except Exception:
                pass
        else:
            break
        await page.wait_for_load_state("domcontentloaded")
        await _check_login_async(page)
        try:
            await page.locator(ORDER_LINK).first.wait_for(timeout=10000)
        except Exception:
            os.makedirs("./output", exist_ok=True)
            shot = f"./output/sd_scan_{status}_p{pageno}.png"
            try:
                await page.screenshot(path=shot)
            except Exception:
                pass
            print(f"  [{status}] page {pageno}: no order rows at {page.url} "
                  f"(ORDER_LINK selector likely needs tuning) -> {shot}")
            break
        cards = await page.evaluate(_CARDS_JS)
        ids = {c.get("href") for c in cards}
        if not cards or ids <= seen:                 # empty or nothing new -> stop paging
            break
        seen |= ids
        found.extend(_cards_to_hits(cards, status, zip_pairs))
        await page.wait_for_timeout(int(config.SD_SCAN_THROTTLE_S * 1000))
    return found


async def scan_tabs_async(page, zip_pairs) -> list:
    """Async scan of Posted + Accepted + Pending on an EXISTING async `page` (shares
    the BOL run's CDP browser). Returns deduped hits in the same shape as
    scan_loadboard. Never raises out of a per-tab scan. The CALLER owns the page and
    should close that tab when this returns."""
    pairs = {(str(a).strip(), str(b).strip()) for a, b in (zip_pairs or []) if a and b}
    if not pairs:
        print("  SD scan skipped — no origin/dest zip pairs in the Excel.")
        return []
    out = []
    for status in SCAN_TABS:
        try:
            hits = await _scan_tab_async(page, status, pairs)
            print(f"  [{status}] {len(hits)} order(s) match an Excel route zip-pair")
            out.extend(hits)
        except Exception as e:                       # noqa: BLE001 - isolate per tab
            print(f"  [{status}] scan error: {e}")
    return _dedupe(out)
