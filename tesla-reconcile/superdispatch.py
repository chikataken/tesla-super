"""
SuperDispatch interactions.

Navigation is URL-driven wherever possible (filter/sort/page are all query
params), which is much more robust than clicking the filter UI.
"""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PWTimeout

import config
import locators as S
from models import OrderRow, OrderDetail, Vehicle

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")          # 17-char VIN (no I/O/Q)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def invoiced_url(start: date, end: date, page: int = 1, ascending: bool = True) -> str:
    """Build the Invoiced list URL with a Delivered-On window and sort."""
    s = f"{start.isoformat()}T09:00:00.000-0700"
    e = f"{end.isoformat()}T09:00:00.000-0700"
    order = "ASC" if ascending else "DESC"
    return (
        f"{config.SD_BASE}/orders/invoiced"
        f"?delivered_on_date%5B0%5D={quote(s)}"
        f"&delivered_on_date%5B1%5D={quote(e)}"
        f"&page={page}"
        f"&sort%5B0%5D=delivery.scheduledAt&sort%5B1%5D={order}"
    )


def default_window(today: date | None = None) -> tuple[date, date]:
    """A delivery window centred ~1 month back (the '+/- 7 days' rule)."""
    today = today or date.today()
    end = today
    start = today - timedelta(days=config.LOOKBACK_DAYS)
    return start, end


# Scrape in-page: key off the reliable /orders/view/ links and the stable
# .SD-Tag-root chip class (text-matching tags is unreliable for short ones
# like "OK", which also appears as non-tag text on the card).
_SCRAPE_JS = r"""
() => {
  // Assign tags by DOCUMENT ORDER: each .SD-Tag-root chip belongs to the most
  // recent preceding order link. (Climbing the DOM to find a card boundary
  // grabbed neighbouring orders' tags — a real bug that corrupted skip logic.)
  // A red "order flagged" icon (aria-label) also follows its order link, like
  // tags — so it assigns to the most recent order the same way.
  const combined = [...document.querySelectorAll(
    "a[href*='/orders/view/'], .SD-Tag-root, [aria-label='order flagged']")];
  const map = new Map();
  let cur = null;
  for (const el of combined) {
    if (el.matches("a[href*='/orders/view/']")) {
      const href = el.getAttribute('href');
      if (!map.has(href)) {
        cur = {id: (el.textContent||'').trim().split('\n')[0], href, tags: [], flagged: false};
        map.set(href, cur);
      } else {
        cur = map.get(href);
      }
    } else if (cur) {
      if (el.getAttribute('aria-label') === 'order flagged') cur.flagged = true;
      else cur.tags.push((el.textContent||'').trim());
    }
  }
  return [...map.values()];
}
"""


def scrape_order_rows(page: Page) -> list[OrderRow]:
    """Read order id, detail link and tag chips for every card on the page."""
    page.wait_for_load_state("domcontentloaded")
    if "login" in page.url.lower() or page.locator("input[type=password]").count():
        raise RuntimeError(
            "Not logged into SuperDispatch in the Playwright profile. Run "
            "`python run_login.py`, log into shipper.superdispatch.com in that "
            "window, press Enter, then retry."
        )
    try:
        page.locator(S.SD_ORDER_LINK).first.wait_for(timeout=10000)
    except Exception:
        import os
        os.makedirs("./output", exist_ok=True)
        page.screenshot(path="./output/sd_scrape_fail.png")
        raise RuntimeError(
            f"No order links at {page.url}. Saved output/sd_scrape_fail.png — "
            f"check whether it's a login page or an empty filtered list."
        )
    raw = page.evaluate(_SCRAPE_JS)
    return [OrderRow(order_id=r["id"], detail_url=_abs(r["href"]),
                     tags=r["tags"], flagged=r.get("flagged", False))
            for r in raw]


# VINs render in isolated leaf cells. Whole-page text concatenates adjacent
# values and breaks the word-boundary regex, so match per element instead.
_VIN_JS = r"""
() => {
  const re = /^[A-HJ-NPR-Z0-9]{17}$/;
  const out = [];
  document.querySelectorAll('p,td,span,div').forEach(e => {
    if (e.children.length === 0) {
      const t = (e.textContent || '').trim();
      if (re.test(t)) out.push(t);
    }
  });
  return [...new Set(out)];
}
"""


# SCHEDULED delivery ZIP from the order detail. The page has a Pickup block and a
# Delivery block, each ending in "View Route"; the delivery block is the SECOND, so
# the ZIP just before the 2nd "View Route" is the scheduled delivery ZIP.
_SCHEDULED_DELIVERY_ZIP_JS = r"""
() => {
  const t = document.body.innerText || '';
  const re = /\b(\d{5})\b(?:-\d{4})?[\s\S]{0,15}?View Route/g;
  const zips = [];
  let m;
  while ((m = re.exec(t)) !== null) zips.push(m[1]);
  if (zips.length >= 2) return zips[1];   // [pickup, delivery]
  return zips[0] || null;
}
"""


# Per-VIN DELIVERY DATE from the order's vehicle grid. The grid columns are
# "Pickup Date" then "Delivery Date", rendered immediately before each row's VIN,
# so in the page text the two date tokens preceding a VIN are [pickup, delivery] and
# the delivery date is the LAST one before that VIN. (The grid is divs, not a <table>,
# so we parse the rendered text rather than rely on a column selector.)
_DATE_TOKEN = re.compile(r"[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}")     # "Jun 4, 2026"


def _delivery_dates_by_vin(page: Page) -> dict[str, date]:
    try:
        txt = page.locator("body").inner_text()
    except Exception:
        return {}
    toks = [t.strip() for t in re.split(r"[\t\n]", txt) if t.strip()]
    out: dict[str, date] = {}
    for i, tk in enumerate(toks):
        m = VIN_RE.search(tk)
        if not m:
            continue
        vin = m.group(0).upper()
        if vin in out:
            continue
        # dates in the few tokens right before the VIN: [pickup, delivery, (model)]
        dates = [d for t in toks[max(0, i - 5):i] for d in _DATE_TOKEN.findall(t)]
        if dates:
            try:
                out[vin] = datetime.strptime(dates[-1], "%b %d, %Y").date()
            except ValueError:
                pass
    return out


def open_order_detail(page: Page, row: OrderRow) -> OrderDetail:
    """Open an order and extract its VIN(s) + delivery zip."""
    page.goto(row.detail_url)
    page.wait_for_load_state("domcontentloaded")
    # The detail view hydrates late — wait for at least one VIN cell.
    try:
        page.wait_for_function(
            "() => [...document.querySelectorAll('p,td,span,div')]"
            ".some(e => e.children.length===0 && "
            "/^[A-HJ-NPR-Z0-9]{17}$/.test((e.textContent||'').trim()))",
            timeout=10000,
        )
    except Exception:
        pass

    vins = page.evaluate(_VIN_JS)
    # SCHEDULED delivery ZIP from the order's "Delivery" address block (e.g.
    # "8401 Westpark Dr McLean, VA 22102"). Falls back to the old last-zip heuristic.
    delivery_zip = (page.evaluate(_SCHEDULED_DELIVERY_ZIP_JS)
                    or _delivery_zip_from_text(page.locator("body").inner_text()))
    # Per-VIN actual delivery date (used by the claims date rule). Missing for a VIN
    # -> left None, and the claims check treats that VIN as indeterminate (manual review).
    dmap = _delivery_dates_by_vin(page)
    vehicles = [Vehicle(vin=v, delivery_zip=delivery_zip,
                        delivery_date=dmap.get(v.upper())) for v in vins]
    uuid = row.detail_url.rstrip("/").split("/")[-1]
    return OrderDetail(
        order_id=row.order_id,
        detail_url=row.detail_url,
        edit_url=f"{config.SD_BASE}/orders/edit/{uuid}",
        vehicles=vehicles,
        delivery_zip=delivery_zip,
    )


# Collect delivery photos GROUPED BY VEHICLE. Multi-vehicle orders render one
# "Delivery Inspection" section per vehicle; each is preceded (document order) by
# that vehicle's VIN heading. We return [{vin, urls}] so each VIN is later matched
# only against its OWN section's photos (no cross-vehicle duplicates).
_COLLECT_ALL_DELIVERY_JS = r"""
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const VINre = /\b[A-HJ-NPR-Z0-9]{17}\b/;
  const leaves = [...document.querySelectorAll('*')].filter(e => e.children.length === 0);
  let cur = null;
  const pairs = [];
  for (const e of leaves) {
    const t = (e.textContent||'').trim();
    const vm = t.match(VINre);
    if (vm) { cur = vm[0]; continue; }              // track most recent VIN heading
    if (/^Delivery Inspection$/i.test(t)) pairs.push({vin: cur, h: e});
  }
  const sections = [];
  for (const {vin, h} of pairs) {
    let s = h;
    for (let i = 0; i < 6 && s.parentElement; i++) {
      s = s.parentElement;
      if (s.querySelectorAll('img').length >= 1) break;
    }
    const result = new Set();
    [...s.querySelectorAll('img')].filter(im => (im.src||'').includes('storage.googleapis'))
      .forEach(im => result.add(im.src));
    const more = [...s.querySelectorAll('*')].find(
      e => e.children.length === 0 && /^\+\d+$/.test((e.textContent||'').trim()));
    if (more && result.size) {
      [...s.querySelectorAll('img')].find(im => (im.src||'').includes('storage.googleapis')).click();
      await sleep(1200);
      let total = 0;
      const c = [...document.querySelectorAll('button')].find(
        b => /^\d+\s*of\s*\d+$/i.test(b.getAttribute('aria-label')||''));
      if (c) { const m = c.getAttribute('aria-label').match(/of\s*(\d+)/i); if (m) total = +m[1]; }
      const next = () => [...document.querySelectorAll('button')].find(
        b => (b.getAttribute('aria-label')||'') === 'Next');
      for (let i = 0; i < (total || 15) + 2; i++) {
        document.querySelectorAll('button').forEach(b => {
          if (/^\d+\s*of\s*\d+$/i.test(b.getAttribute('aria-label')||'')) {
            const im = b.querySelector('img');
            if (im && (im.src||'').includes('storage.googleapis')) result.add(im.src);
          }
        });
        const nb = next(); if (!nb) break; nb.click(); await sleep(200);
      }
      const close = document.querySelector("button[aria-label='Close']");
      if (close) close.click();
      await sleep(500);
    }
    sections.push({vin: vin, urls: [...result]});
  }
  return sections;
}
"""

# ACTUAL delivered ZIP from the BOL TIMELINE. The delivered event shows the real
# drop address immediately before a "· Delivered" marker, e.g.
# "7903 Branch Ave, Clinton, MD, 20735 · Delivered". We grab the 5-digit ZIP that
# sits just before that marker. (The header "Delivered on <date>" has no nearby ZIP,
# so the short {0,8} gap can't accidentally match the scheduled ZIP.)
_DELIVERED_ZIP_JS = r"""
() => {
  const t = document.body.innerText || '';
  let m = t.match(/\b(\d{5})\b(?:-\d{4})?[\s,·•∙]{0,8}Delivered\b/);
  if (m) return m[1];
  // Fallback: last 5-digit ZIP on the BOL page (the delivered addr is last).
  const zips = t.match(/\b\d{5}\b/g);
  return zips ? zips[zips.length - 1] : null;
}
"""


def get_bol_photos(page: Page, detail_url: str) -> tuple[list[dict], str | None]:
    """Open the order's online BOL and return (sections, delivered_zip), where
    `sections` is a list of {'vin': <vehicle VIN>, 'urls': [photo urls]} — one per
    Delivery Inspection section (one per vehicle on multi-VIN orders) — and
    `delivered_zip` is the ACTUAL delivered ZIP read from the BOL timeline."""
    page.goto(detail_url)
    page.wait_for_load_state("domcontentloaded")
    page.get_by_role("button", name="order actions").click()
    bol_href = page.get_by_role("menuitem", name="View Online BOL").get_attribute("href")
    page.keyboard.press("Escape")
    if not bol_href:
        return [], None

    page.goto(bol_href)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)
    delivered_zip = page.evaluate(_DELIVERED_ZIP_JS)
    sections = page.evaluate(_COLLECT_ALL_DELIVERY_JS)
    for sec in sections:
        sec["urls"] = _unique(sec.get("urls", []))
    return sections, delivered_zip


def fetch_images(page: Page, urls: list[str]) -> list[bytes]:
    """Download photo bytes (public GCS URLs) via the browser context.

    Each APIResponse is DISPOSED right after its body is read. Playwright's
    APIRequestContext otherwise keeps every fetched response body in memory for the
    life of the context (one context spans the whole run), so over dozens of orders
    x dozens of full-res photos the buffered bytes grow into GBs and the machine
    starts thrashing — the program "freezes" after ~N orders with no error or
    timeout. Disposing frees each body immediately; the bytes already copied into
    `out` stay valid."""
    out = []
    for u in urls:
        resp = None
        try:
            resp = page.context.request.get(u)
            if resp.ok:
                out.append(resp.body())
        except Exception:
            pass
        finally:
            if resp is not None:
                try:
                    resp.dispose()      # free Playwright's retained copy of the body
                except Exception:
                    pass
    return out


# The edit form loads the order's data asynchronously a few seconds after the
# page loads. Editing/saving before that finishes races the hydration and the
# change may not stick — so wait until the Order ID field is populated.
_FORM_LOADED_JS = r"""
() => {
  const inp = [...document.querySelectorAll('input')].find(i => {
    const fc = i.closest('.MuiFormControl-root');
    const lab = fc && fc.querySelector('label');
    return lab && /Order ID/i.test(lab.textContent || '');
  });
  return !!(inp && (inp.value || '').trim());
}
"""


def _wait_form_loaded(page: Page) -> None:
    try:
        page.wait_for_function(_FORM_LOADED_JS, timeout=15000)
    except Exception:
        page.wait_for_timeout(3000)
    page.wait_for_timeout(600)


def add_tags(page: Page, edit_url: str, tags: list[str]) -> None:
    """Open the edit page, REMOVE all existing tags, add the given tags, Save.

    SuperDispatch caps a shipment at 3 tags, so this is a clear-then-set: every
    existing tag chip is removed first, then the desired tags are added. Each tag
    value renders as an `.SD-Tag-root` chip with an X `<button>`; clicking it
    removes that tag. (There's also an aria-label="Clear" clear-all.)

    The Tags <label> also contains an info-tooltip button, so get_by_label
    misses it — locate the autocomplete by the label text and use its input."""
    page.goto(edit_url)
    page.wait_for_load_state("domcontentloaded")
    _wait_form_loaded(page)                       # don't edit before the order loads
    tags_root = page.locator(".MuiAutocomplete-root").filter(
        has=page.locator("label", has_text="Tags")
    ).first
    tags_root.wait_for(timeout=10000)

    # Remove every existing tag chip first (each click re-renders the list).
    remove_btns = tags_root.locator(".SD-Tag-root button")
    for _ in range(8):                            # cap (SD allows at most 3, +slack)
        if remove_btns.count() == 0:
            break
        try:
            remove_btns.first.click()
        except Exception:
            break
        page.wait_for_timeout(200)                # let React drop the chip

    box = tags_root.locator("input").first
    for tag in tags:
        box.click()
        box.fill(tag)
        page.get_by_role("option", name=tag, exact=True).first.click()
        page.wait_for_timeout(250)
    page.get_by_role("button", name=re.compile(r"^\s*save\s*$", re.I)).first.click()
    # A successful save redirects to the order view — wait for that as confirmation.
    try:
        page.wait_for_url("**/orders/view/**", timeout=10000)
    except Exception:
        page.wait_for_load_state("domcontentloaded")


# A "Created" date-window dropdown (a MUI listbox button) appears to the RIGHT of
# the search box ONLY AFTER a search is run (verified live DOM 2026-06) — it is NOT
# on the default order list. It defaults to a narrow window (e.g. "3 months ago"),
# so orders created before that silently don't appear in search results. Options:
# "1 month ago", "3 months ago", "6 months ago", "1 year ago", "All time". We
# recognise the dropdown by its current value matching one of these relative-window
# phrases, so select_all_time never clicks an unrelated control (the field-scope
# "All" select, sort, page-size, or the Create button — clicking those previously
# opened an overlay and timed out the next search-box click). The choice is
# component state, NOT a URL param, so it must be set after every search.
# The dropdown's current value is a relative window ("3 months ago", "1 year ago",
# ...) or "All time". Match loosely on "ago"/"all time" so the trigger is found even
# if a "Created" label is folded into its text — while still excluding the
# field-scope "All", the Sort select, and the page-size select.
_TIME_WINDOW_RE = re.compile(r"\bago\b|all time", re.I)


def select_all_time(page: Page) -> bool:
    """Switch the orders "Created" date-window dropdown to "All time" so a search
    reaches OLD orders (it defaults to e.g. "1 year ago", hiding anything older).

    Call this AFTER running a search — the dropdown only renders in the results
    view. Robust + safe:
      - WAITS up to 12s for the dropdown to render (it appears a moment after the
        search); without this the switch silently no-ops — the original "time
        frame won't change" bug.
      - Only ever opens the listbox button whose text is a time window, never a
        plain button.
      - GUARANTEES the menu is dismissed (Escape) on any failure, so a half-open
        overlay can't block the next search-box click — the "won't find the order"
        bug. Returns True iff "All time" is confirmed active afterwards.
    """
    menu_opened = False
    try:
        trigger = page.locator(
            "[role='button'][aria-haspopup='listbox']"
        ).filter(has_text=_TIME_WINDOW_RE).first
        # Wait for hydration; if it never appears, there's no control — give up clean.
        trigger.wait_for(state="visible", timeout=12000)
        if "all time" in (trigger.inner_text() or "").strip().lower():
            return True
        trigger.click()
        menu_opened = True
        opt = page.get_by_role("option", name="All time", exact=True)
        opt.wait_for(state="visible", timeout=5000)
        opt.click()
        menu_opened = False                  # MUI closes the menu on selection
        page.wait_for_timeout(900)           # let the list re-query the wider window
        return "all time" in (trigger.inner_text() or "").strip().lower()
    except Exception:
        return False
    finally:
        if menu_opened:                      # never leave an overlay covering the page
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                pass


def find_order_by_vin(page: Page, vin: str) -> OrderRow | None:
    """Search the orders page by VIN and return the single matching order."""
    page.goto(f"{config.SD_BASE}/orders")
    page.wait_for_load_state("domcontentloaded")
    box = page.locator("input[type=search]").first
    box.wait_for(state="visible", timeout=15000)
    box.click()
    box.fill("")
    box.press_sequentially(vin, delay=15)
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    # The "Created" date-window dropdown renders ONLY after a search runs and defaults
    # to a narrow window (e.g. "3 months ago"), hiding older orders — so widen to All
    # time AFTER searching, then let the results re-query.
    select_all_time(page)
    page.wait_for_timeout(1000)
    link = page.locator("a[href*='/orders/view/']").first
    try:
        link.wait_for(timeout=8000)
    except Exception:
        return None
    href = link.get_attribute("href") or ""
    order_id = (link.inner_text() or "").strip().split("\n")[0]
    return OrderRow(order_id=order_id, detail_url=_abs(href), tags=[])


def find_order_by_id(page: Page, order_id: str) -> OrderRow | None:
    """Search the orders page and return the order whose id matches exactly.

    Prints a couple of progress lines so a failing run says *where* it broke
    (time-frame not switched vs. search returned nothing).
    """
    page.goto(f"{config.SD_BASE}/orders")
    page.wait_for_load_state("domcontentloaded")
    box = page.locator("input[type=search]").first
    box.wait_for(state="visible", timeout=15000)   # toolbar hydrates late
    box.click()
    box.fill("")
    box.press_sequentially(order_id, delay=15)
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    # The "Created" date-window dropdown renders ONLY after a search runs and defaults
    # to a narrow window (e.g. "3 months ago"), hiding older orders — so widen to All
    # time AFTER searching, then let the results re-query before reading them.
    applied = select_all_time(page)
    print(f"  [search] time frame -> All time: {'ok' if applied else 'n/a'}")
    page.wait_for_timeout(1000)
    links = page.locator("a[href*='/orders/view/']")
    try:
        links.first.wait_for(timeout=8000)
    except Exception:
        print(f"  [search] 0 results for {order_id!r} (even after All time)")
        return None
    n = links.count()
    print(f"  [search] {n} result(s) for {order_id!r}")
    for i in range(n):
        link = links.nth(i)
        txt = (link.inner_text() or "").strip().split("\n")[0]
        if txt == order_id:
            return OrderRow(order_id=txt, detail_url=_abs(link.get_attribute("href")), tags=[])
    # fall back to the first result
    link = links.first
    return OrderRow(order_id=(link.inner_text() or "").strip().split("\n")[0],
                    detail_url=_abs(link.get_attribute("href")), tags=[])


_READ_TAGS_JS = r"""
() => {
  const roots = [...document.querySelectorAll('.MuiAutocomplete-root')];
  for (const r of roots) {
    const l = r.querySelector('label');
    if (l && /Tags/.test((l.textContent||'').trim())) {
      // Selected tags render as TEXT (not MuiChip). Collect leaf text that
      // isn't the "Tags" label/legend.
      const out = [];
      r.querySelectorAll('*').forEach(e => {
        if (e.children.length === 0) {
          const t = (e.textContent||'').trim();
          if (t && t !== 'Tags' && !out.includes(t)) out.push(t);
        }
      });
      return out;
    }
  }
  return [];
}
"""


def read_edit_tags(page: Page, edit_url: str) -> list[str]:
    """Open the edit page and return the user tags currently applied."""
    page.goto(edit_url)
    page.wait_for_load_state("domcontentloaded")
    _wait_form_loaded(page)            # wait for the order data (incl. tags) to hydrate
    return page.evaluate(_READ_TAGS_JS)


def edit_url_for(detail_url: str) -> str:
    uuid = detail_url.rstrip("/").split("/")[-1]
    return f"{config.SD_BASE}/orders/edit/{uuid}"


# ----------------------- helpers -----------------------
def _abs(href: str) -> str:
    return href if href.startswith("http") else config.SD_BASE + href


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _delivery_zip_from_text(body: str) -> str | None:
    # Heuristic: the second address block is the delivery. Refine with a
    # scoped selector once you confirm the DOM (see selectors.py).
    zips = ZIP_RE.findall(body)
    return zips[-1] if zips else None
