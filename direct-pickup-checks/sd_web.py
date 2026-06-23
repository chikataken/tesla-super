"""
Super Dispatch WEB-app operations via Playwright.

The public API can't write order tags and (for now) we drive the web app for the
photo + tagging steps. These functions are COPIED/ADAPTED from
tesla-reconcile/superdispatch.py (the verified selectors/JS) — kept in this project
per the no-cross-folder-import rule. The one substantive change: we collect the
**Pickup** Inspection photos (not Delivery), per the workflow.

Sequence the worker uses:
    find_order_by_id(page, number) -> open the order in the web UI
    get_pickup_photos(page, detail_url) -> [{vin, urls}] from the Pickup Inspection
    fetch_images(page, urls) -> photo bytes (downloaded via the browser context)
    add_tags(page, edit_url_for(detail_url), ["VIN" or "NO VIN"])
"""
from __future__ import annotations
import re

from playwright.sync_api import Page

import config

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")          # 17-char VIN (no I/O/Q)


# ---------------------------------------------------------------------------
# Find an order in the web UI by its number (we resolve the number from the API
# using the webhook's order_guid; the web view uuid differs from the API guid).
# ---------------------------------------------------------------------------
_TIME_WINDOW_RE = re.compile(r"\bago\b|all time", re.I)


def select_all_time(page: Page) -> bool:
    """Switch the orders "Created" date-window dropdown to "All time" so a search
    reaches OLD orders (it defaults to e.g. "1 year ago"). Call AFTER a search —
    the dropdown only renders in the results view. Always dismisses its menu on
    failure so a half-open overlay can't block the next click."""
    menu_opened = False
    try:
        trigger = page.locator(
            "[role='button'][aria-haspopup='listbox']"
        ).filter(has_text=_TIME_WINDOW_RE).first
        trigger.wait_for(state="visible", timeout=12000)
        if "all time" in (trigger.inner_text() or "").strip().lower():
            return True
        trigger.click()
        menu_opened = True
        opt = page.get_by_role("option", name="All time", exact=True)
        opt.wait_for(state="visible", timeout=5000)
        opt.click()
        menu_opened = False
        page.wait_for_timeout(900)
        return "all time" in (trigger.inner_text() or "").strip().lower()
    except Exception:
        return False
    finally:
        if menu_opened:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                pass


def find_order_detail_url(page: Page, order_number: str) -> str | None:
    """Search the orders page for an order number and return its /orders/view/ URL
    (or None). Widens the search window to All time so recent-but-not-newest orders
    are found."""
    page.goto(f"{config.SD_WEB_BASE}/orders")
    page.wait_for_load_state("domcontentloaded")
    if "login" in page.url.lower() or page.locator("input[type=password]").count():
        raise RuntimeError(
            "Not logged into Super Dispatch in the Playwright profile. Run "
            "`python run_login.py`, log in, then retry.")
    box = page.locator("input[type=search]").first
    box.wait_for(state="visible", timeout=15000)
    box.click()
    box.fill("")
    box.press_sequentially(order_number, delay=15)
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    select_all_time(page)
    page.wait_for_timeout(1000)
    links = page.locator("a[href*='/orders/view/']")
    try:
        links.first.wait_for(timeout=8000)
    except Exception:
        return None
    for i in range(links.count()):
        link = links.nth(i)
        txt = (link.inner_text() or "").strip().split("\n")[0]
        if txt == order_number:
            return _abs(link.get_attribute("href") or "")
    return _abs(links.first.get_attribute("href") or "")     # fallback: first result


# ---------------------------------------------------------------------------
# Pickup inspection photos, grouped by vehicle. One "Pickup Inspection" section
# per vehicle; each is preceded (document order) by that vehicle's VIN heading.
# (Adapted from tesla-reconcile's delivery collector — same DOM walk, "Pickup".)
# ---------------------------------------------------------------------------
_COLLECT_ALL_PICKUP_JS = r"""
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
    if (/^Pickup Inspection$/i.test(t)) pairs.push({vin: cur, h: e});
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


def get_pickup_photos(page: Page, detail_url: str) -> list[dict]:
    """Open the order's online BOL and return [{'vin': <VIN>, 'urls': [photo urls]}]
    — one entry per Pickup Inspection section (one per vehicle on multi-VIN orders).

    NOTE: the workflow says trigger this only once photos exist (the
    order.picked_up_bol webhook). If a section has no images yet, its urls list is
    empty and the worker treats that VIN as 'no VIN photo'.

    If the order-actions menu has NO "View Online BOL" item, there's no online BOL,
    which means there are no pickup photos -> return [] (the caller tags NO VIN).
    We use a short timeout so a missing item doesn't hang on the default 30s wait."""
    page.goto(detail_url)
    page.wait_for_load_state("domcontentloaded")
    page.get_by_role("button", name="order actions").click()
    try:
        bol_href = page.get_by_role("menuitem", name="View Online BOL").get_attribute(
            "href", timeout=4000)
    except Exception:                       # no "View Online BOL" item -> no photos
        bol_href = None
    page.keyboard.press("Escape")
    if not bol_href:
        return []
    page.goto(bol_href)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)
    sections = page.evaluate(_COLLECT_ALL_PICKUP_JS)
    for sec in sections:
        sec["urls"] = _unique(sec.get("urls", []))
    return sections


def fetch_images(page: Page, urls: list[str]) -> list[bytes]:
    """Download photo bytes (public GCS URLs) via the browser context, disposing
    each response right after reading so Playwright doesn't retain every body in
    memory across a long-lived context."""
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
                    resp.dispose()
                except Exception:
                    pass
    return out


# ---------------------------------------------------------------------------
# Tag editing.
# ---------------------------------------------------------------------------
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
    SuperDispatch caps a shipment at 3 tags, so this is clear-then-set."""
    page.goto(edit_url)
    page.wait_for_load_state("domcontentloaded")
    _wait_form_loaded(page)
    tags_root = page.locator(".MuiAutocomplete-root").filter(
        has=page.locator("label", has_text="Tags")
    ).first
    tags_root.wait_for(timeout=10000)

    remove_btns = tags_root.locator(".SD-Tag-root button")
    for _ in range(8):
        if remove_btns.count() == 0:
            break
        try:
            remove_btns.first.click()
        except Exception:
            break
        page.wait_for_timeout(200)

    box = tags_root.locator("input").first
    for tag in tags:
        box.click()
        box.fill(tag)
        page.get_by_role("option", name=tag, exact=True).first.click()
        page.wait_for_timeout(250)
    page.get_by_role("button", name=re.compile(r"^\s*save\s*$", re.I)).first.click()
    try:
        page.wait_for_url("**/orders/view/**", timeout=10000)
    except Exception:
        page.wait_for_load_state("domcontentloaded")


def edit_url_for(detail_url: str) -> str:
    uuid = detail_url.rstrip("/").split("/")[-1]
    return f"{config.SD_WEB_BASE}/orders/edit/{uuid}"


# ----------------------- helpers -----------------------
def _abs(href: str) -> str:
    return href if href.startswith("http") else config.SD_WEB_BASE + href


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
