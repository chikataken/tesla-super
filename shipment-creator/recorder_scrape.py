"""Scrape Super Dispatch order-status tabs into parsed order records.

Reuses the shared logged-in Chrome (auth.browser_context) and the loadboard card
extractor (sd_scrape._CARDS_JS). Unlike sd_scrape.scan_loadboard (which filters to
specific Excel routes), this takes EVERY order on a tab — that's what a backfill /
full-mirror needs.

Two scan modes per tab:
  * full  — paginate ?size=100&page=N until the tab is exhausted (for the small
            "active" tabs: new/posted/accepted/...).
  * window— restrict to a delivered-on-date range (for the huge history tabs:
            delivered/invoiced/paid) so we only pull the last N months. Same URL
            shape tesla-reconcile uses for its invoiced window.

parse_card() is pure (no browser) so it's unit-testable.
"""
from __future__ import annotations
import re
import time
from urllib.parse import quote

import config
import sd_scrape

# tab slug (URL) -> canonical status label we store
TAB_STATUS = {
    "new": "new", "on_hold": "on_hold", "posted_to_lb": "posted",
    "requests": "requests", "pending": "pending", "declined": "declined",
    "accepted": "accepted", "picked_up": "picked_up", "delivered": "delivered",
    "invoiced": "invoiced", "paid": "paid", "order_canceled": "canceled",
    "archived": "archived", "inactive": "deleted", "flagged": "flagged",
}

# "City Name, ST 12345"  -> (city, state, zip5). City stops at the comma.
_CSZ_RE = re.compile(r"([A-Za-z][A-Za-z .'\-]*?),\s*([A-Z]{2})\s+(\d{5})\b")
# "Jun 23" / "Jun 1"  (month abbrev + day)
_DATE_RE = re.compile(r"\b([A-Z][a-z]{2}\s+\d{1,2})\b")
_UUID_RE = re.compile(r"/orders/view/([0-9a-fA-F\-]{36})")
# lowercase status/tag badges that get concatenated into the row text as noise
_NOISE_TOKENS = {"tesla", "demo", "qpay", "asap", "rush", "team", "express"}


def _uuid(href: str) -> str:
    m = _UUID_RE.search(href or "")
    return m.group(1) if m else (href or "")


def _clean_city(raw: str) -> str:
    """The CSZ regex's city group can absorb the *previous* stop's terminal name
    (terminal and the next city are space-separated with no delimiter). Trim it:
    keep only the text after the last terminal separator (2+ spaces / · / •), then
    drop leading lowercase badge tokens (tesla/demo/qpay/...) so "Kenilworth  tesla
    San Antonio" -> "San Antonio"."""
    if not raw:
        return ""
    chunk = re.split(r"\s{2,}|·|•", raw)[-1].strip()
    toks = chunk.split()
    while toks and (toks[0].islower() or toks[0].lower() in _NOISE_TOKENS):
        toks.pop(0)
    return " ".join(toks).strip()


def parse_card(card: dict, status: str) -> dict | None:
    """Turn one raw _CARDS_JS record into a normalized order dict, or None if it
    has no usable identity (no uuid and no number)."""
    href = card.get("href", "") or ""
    web_uuid = _uuid(href)
    number = (card.get("id") or "").strip()
    if not web_uuid and not number:
        return None
    text = (card.get("text") or "").strip()
    vins = [v for v in (card.get("vins") or []) if v]

    # pickup = first "City, ST ZIP", delivery = last one (document order).
    csz = list(_CSZ_RE.finditer(text))
    pu = csz[0] if csz else None
    do = csz[-1] if len(csz) > 1 else None

    def _date_after(m) -> str:
        if not m:
            return ""
        d = _DATE_RE.search(text, m.end())
        return d.group(1) if d else ""

    def _terminal_after(m, nxt_start) -> str:
        """Text after this stop's '·' up to the next stop (or end), trimmed."""
        if not m:
            return ""
        seg = text[m.end(): (nxt_start if nxt_start is not None else len(text))]
        if "·" in seg:
            seg = seg.split("·", 1)[1]
        seg = seg.replace("•", " ").strip()
        # drop a leading date token if it slipped in
        seg = _DATE_RE.sub("", seg, count=1).strip(" ·-")
        return re.sub(r"\s{2,}", " ", seg)[:80]

    return {
        "web_uuid": web_uuid or number,
        "number": number,
        "status": status,
        "detail_url": href,
        "pickup_city": _clean_city(pu.group(1)) if pu else "",
        "pickup_state": pu.group(2) if pu else "",
        "pickup_zip": pu.group(3) if pu else "",
        "pickup_date": _date_after(pu),
        "pickup_terminal": _terminal_after(pu, do.start() if do else None),
        "delivery_city": _clean_city(do.group(1)) if do else "",
        "delivery_state": do.group(2) if do else "",
        "delivery_zip": do.group(3) if do else "",
        "delivery_date": _date_after(do),
        "delivery_terminal": _terminal_after(do, None),
        "vins": vins,
        "card_text": text,
    }


def _tab_url(tab: str, page: int, window: tuple[str, str] | None) -> str:
    base = f"{config.SD_WEB_BASE}/orders/{tab}"
    parts = [f"size={config.SD_SCAN_PAGE_SIZE}", f"page={page}"]
    if window:
        s = f"{window[0]}T00:00:00.000-0700"
        e = f"{window[1]}T23:59:59.000-0700"
        parts = [f"delivered_on_date%5B0%5D={quote(s)}",
                 f"delivered_on_date%5B1%5D={quote(e)}"] + parts
        parts += ["sort%5B0%5D=delivery.scheduledAt", "sort%5B1%5D=DESC"]
    return base + "?" + "&".join(parts)


def scan_tab(page, tab: str, window: tuple[str, str] | None = None,
             max_pages: int | None = None, log=print) -> list[dict]:
    """Paginate one status tab and return parsed order dicts (deduped by web_uuid
    within the tab). `window` = (start_iso_date, end_iso_date) limits history tabs."""
    status = TAB_STATUS.get(tab, tab)
    cap = max_pages or config.SD_SCAN_MAX_PAGES
    out: dict[str, dict] = {}
    seen_hrefs: set[str] = set()
    for pageno in range(1, cap + 1):
        url = _tab_url(tab, pageno, window)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(400)
        except Exception as e:
            log(f"  [{tab}] page {pageno}: nav error {e}")
            break
        if "login" in (page.url or "").lower():
            log(f"  [{tab}] hit login page — stopping (session lost)")
            break
        try:
            page.locator(sd_scrape.ORDER_LINK).first.wait_for(timeout=10000)
        except Exception:
            if pageno == 1:
                log(f"  [{tab}] page 1: no order cards (empty tab or selector miss)")
            break
        cards = page.evaluate(sd_scrape._CARDS_JS)
        hrefs = {c.get("href") for c in cards}
        if not cards or hrefs <= seen_hrefs:        # empty or nothing new -> done
            break
        seen_hrefs |= hrefs
        for c in cards:
            rec = parse_card(c, status)
            if rec:
                out[rec["web_uuid"]] = rec
        log(f"  [{tab}] page {pageno}: +{len(cards)} cards (running {len(out)})")
        time.sleep(config.SD_SCAN_THROTTLE_S)
    return list(out.values())
