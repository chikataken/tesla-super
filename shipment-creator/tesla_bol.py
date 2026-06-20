"""
Tesla BOL download (Dispatch Dashboard 2.0) — parallel, async.

One browser on your existing login, with N tabs (config: --workers) each driving
its own slice of VINs concurrently. Shipments are deduped across all tabs (many
VINs share a shipment/BOL) via a shared cache guarded by an asyncio lock.

Per tab: Alerts -> Select All, Status -> Select All, Search By -> VINs, then per
VIN: search, pick the most-recent shipment, click "Download BOL". The click fires
POST .../DownloadShipmentBOL returning JSON {data:{result:<base64 PDF>}}; we capture
that response and decode it (robust, no bearer-token handling). The saved PDF is
then read for the per-VIN record (pdf_read).
"""
from __future__ import annotations
import asyncio
import base64
import os
import re
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import chrome_cdp
import config

# A date with an OPTIONAL time — 'Jun 15, 2026 04:00 PM' or just 'Jun 09, 2026'.
_DATE_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}(?: \d{1,2}:\d{2}\s*[AP]M)?")
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

# The dispatch grid lays each VIN out as a ROW under shared column HEADERS:
#   VIN | Ready Date | Pickup Date | Need By Date | ETA | … | Received Date
# So 'Need By Date' is a header, not next to its value — the value is the 3rd date
# in the VIN's row (Ready, Pickup, Need By, ETA…). 'Received Date' is ISO, so it
# isn't matched by _DATE_RE. We compute Need By's index from the header order (so
# it survives column reordering) and read that date out of the VIN's own row.
_DATE_COLS = ("Ready Date", "Pickup Date", "ETA")        # the OTHER M/d date columns


def _needby_col_index(text: str) -> int:
    """How many date-valued columns precede 'Need By Date' in the header row.
    Defaults to 2 (Ready, Pickup) when the header isn't in the captured text."""
    nb = text.find("Need By")
    if nb < 0:
        return 2
    return sum(1 for h in _DATE_COLS if 0 <= text.find(h) < nb)


def _need_by(text: str, vin: str = "") -> str | None:
    """Read the 'Need By Date' value from this VIN's row, positionally. Returns the
    date with or without a time ('Jun 15, 2026 04:00 PM' / 'Jun 09, 2026'), or None."""
    if not text:
        return None
    seg = text
    if vin and vin in text:                              # isolate THIS VIN's row
        seg = text[text.index(vin) + len(vin):]
        nxt = _VIN_RE.search(seg)                        # stop before the next VIN row
        if nxt:
            seg = seg[:nxt.start()]
    dates = _DATE_RE.findall(seg)
    idx = _needby_col_index(text)
    return dates[idx].strip() if len(dates) > idx else None


def _shp_from_text(text: str) -> str:
    m = re.search(r"SHP[\w-]+", text or "")
    return m.group(0) if m else "UNKNOWN"


# ----------------------- filter setup (once per tab) -----------------------
async def _open_dropdown(page, label: str) -> None:
    """Open Alerts/Status/Search By: click the first tsl-multiselect/tsl-select
    that follows the label text (their markup is inconsistent)."""
    ctrl = page.locator(
        "xpath=(//*[normalize-space(text())=" + repr(label) + "])[1]"
        "/following::*[self::tsl-multiselect or self::tsl-select][1]"
    ).first
    await ctrl.scroll_into_view_if_needed()
    await ctrl.click()
    await page.wait_for_timeout(400)


async def _select_all(page, label: str) -> None:
    await _open_dropdown(page, label)
    sa = page.get_by_text("Select All", exact=True)
    try:
        if await sa.count() and await sa.first.is_visible():
            await sa.first.click()
            await page.wait_for_timeout(200)
    except Exception:
        pass
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)


async def _configure_filters(page) -> None:
    await _select_all(page, "Alerts")
    await _select_all(page, "Status")
    await _open_dropdown(page, "Search By")
    await page.get_by_text("VINs", exact=True).first.click()
    await page.wait_for_timeout(300)


# ----------------------- per-VIN search + download -----------------------
def _card_recency(card_text: str) -> datetime:
    best = datetime.min
    for m in _DATE_RE.findall(card_text or ""):
        try:
            d = datetime.strptime(m, "%b %d, %Y %I:%M %p")
            best = max(best, d)
        except ValueError:
            pass
    return best


async def _search_vin(page, vin: str):
    box = page.get_by_placeholder("Enter VINs").first
    await box.click()
    await box.fill("")
    await box.press_sequentially(vin, delay=15)
    await page.locator("button.search-btn").first.click()
    cards = page.locator(".grid-entry").filter(has_text=vin)
    try:
        await cards.first.wait_for(timeout=15000)
    except PWTimeout:
        return None
    n = await cards.count()
    if n == 0:
        return None
    if n == 1:
        return cards.first
    best_i, best_dt = 0, datetime.min
    for i in range(n):
        dt = _card_recency(await cards.nth(i).inner_text())
        if dt > best_dt:
            best_dt, best_i = dt, i
    return cards.nth(best_i)


async def _shp_of(card) -> str:
    m = re.search(r"SHP[\w-]+", await card.inner_text() or "")
    return m.group(0) if m else "UNKNOWN"


def _print_record(path: str, shp: str, vin: str) -> None:
    try:
        import pdf_read
        r = pdf_read.record_for_vin(path, vin) if vin else None
        if r:
            print(pdf_read.format_record(r))
        else:
            for e in pdf_read.extract_records(path):
                print(pdf_read.format_record(e))
    except Exception as exc:
        print(f"    {shp}: could not read PDF text: {exc}")


async def _download_bol(tag: str, page, card, shp: str, vin: str = "",
                        attempts: int = 3) -> str | None:
    os.makedirs(config.BOL_DIR, exist_ok=True)
    path = os.path.join(config.BOL_DIR, f"{shp}.pdf")
    for attempt in range(1, attempts + 1):
        try:
            link = card.locator("a", has_text="Download BOL").first
            if not await link.count():
                link = page.locator("a", has_text="Download BOL").first
            if not await link.count():
                print(f"{tag} {shp}: no 'Download BOL' link")
                return None
            await link.scroll_into_view_if_needed(timeout=5000)
            # Stable handle + neutralize the empty href (a plain click on
            # <a href=""> reloads the page and aborts Angular's BOL fetch).
            handle = await link.element_handle(timeout=5000)
            await handle.evaluate("el => el.setAttribute('href', 'javascript:void(0)')")
            async with page.expect_response(
                lambda r: "DownloadShipmentBOL" in r.url, timeout=45000
            ) as resp_info:
                await handle.click()
            data = await (await resp_info.value).json()
            b64 = (data.get("data") or {}).get("result")
            if not b64:
                print(f"{tag} {shp}: BOL response had no PDF payload")
                return None
            pdf = base64.b64decode(b64)
            if not pdf.startswith(b"%PDF"):
                print(f"{tag} {shp}: decoded payload isn't a PDF")
                return None
            with open(path, "wb") as f:
                f.write(pdf)
            print(f"{tag} {shp}: saved {len(pdf):,}-byte BOL -> {path}")
            _print_record(path, shp, vin)
            return path
        except Exception as exc:
            print(f"{tag} {shp}: attempt {attempt}/{attempts} got no BOL "
                  f"({type(exc).__name__}); retrying...")
            await page.wait_for_timeout(1500)
    print(f"{tag} {shp}: Download BOL produced no response after {attempts} attempts")
    return None


# ----------------------- worker + orchestration -----------------------
async def _worker(wid: int, page, vins: list[str], seen: dict, lock, results: dict,
                  on_bol=None, need_by=None) -> None:
    tag = f"[w{wid}]"
    try:
        await page.goto(config.TESLA_DASHBOARD_URL)
        await page.wait_for_load_state("domcontentloaded")
        await page.locator("button.search-btn").first.wait_for(timeout=20000)
        await _configure_filters(page)
        print(f"{tag} ready — {len(vins)} VIN(s)")
    except Exception as exc:
        print(f"{tag} setup failed: {exc}")
        for v in vins:
            results[v] = {"shp": None, "path": None, "status": "worker setup failed"}
        return

    for vin in vins:
        try:
            card = await _search_vin(page, vin)
            if card is None:
                results[vin] = {"shp": None, "path": None, "need_by": None,
                                "status": "no shipment found"}
                print(f"{tag} {vin}: no shipment found")
                continue
            txt = await card.inner_text()
            shp = _shp_from_text(txt)
            nb = _need_by(txt, vin)                   # 'Need By' from this VIN's row
            if need_by is not None:
                need_by[vin] = nb
            print(f"{tag} {vin}: need by {nb or '— (not found)'}")
            async with lock:
                dup = shp in seen
                if dup:
                    results[vin] = {"shp": shp, "path": seen[shp], "need_by": nb,
                                    "status": "reused (shared shipment)"}
            if dup:
                print(f"{tag} {vin}: shares {shp} (BOL already downloaded)")
                continue
            path = await _download_bol(tag, page, card, shp, vin)
            async with lock:
                if path:                              # only cache successful BOLs —
                    seen[shp] = path                  # caching None poisons sibling VINs
            results[vin] = {"shp": shp, "path": path, "need_by": nb,
                            "status": "downloaded" if path else "download failed"}
            if path and on_bol:                       # parse this BOL as it lands
                try:
                    on_bol(shp, path)
                except Exception as exc:
                    print(f"{tag} {shp}: parse callback error: {exc}")
        except Exception as exc:
            results[vin] = {"shp": None, "path": None, "need_by": None,
                            "status": f"error: {exc}"}
            print(f"{tag} {vin}: ERROR {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Shared-Chrome window management (mirrors tesla-reconcile/auth.py)
# --------------------------------------------------------------------------
# Chrome's blank launch tab is ALWAYS at one of these; a tool's working tabs are
# navigated away and a fresh tab is "about:blank" — so adopting one as our first tab
# can never steal another tool's tab.
_LAUNCH_URLS = {"chrome://new-tab-page/", "chrome://newtab/", "chrome://new-tab-page/#"}


async def _adopt_launch_tab(ctx):
    """Return an UNCLAIMED blank launch tab to reuse as our window's first tab, or
    None — so the run that opens first reuses Chrome's launch window (no extra window,
    no leftover blank) and a later run opens its own new window instead."""
    for pg in list(ctx.pages):
        try:
            if pg.url in _LAUNCH_URLS:
                return pg
        except Exception:                               # noqa: BLE001
            pass
    return None


async def _await_launch_tab(ctx):
    """Just after launch the blank tab may not be in ctx.pages yet; poll briefly."""
    for _ in range(40):                                 # up to ~2s
        if list(ctx.pages):
            return
        await asyncio.sleep(0.05)


async def _open_new_window(browser, ctx):
    """Open a page in a brand-new Chrome WINDOW and return it, so this run's tabs are
    separate from another tool's window. Race-free via a unique url marker; the created
    target is closed on failure so a window never leaks."""
    import uuid
    tid = None
    try:
        sess = await browser.new_browser_cdp_session()
        marker = "tfi-" + uuid.uuid4().hex
        res = await sess.send("Target.createTarget", {"url": "about:blank#" + marker, "newWindow": True})
        tid = res.get("targetId")
        for _ in range(100):                            # ~5s for the page to register
            for pg in ctx.pages:
                if marker in pg.url:
                    return pg
            await asyncio.sleep(0.05)
    except Exception:                                   # noqa: BLE001 - fall back to a tab
        pass
    if tid is not None:
        try:
            s2 = await browser.new_browser_cdp_session()
            await s2.send("Target.closeTarget", {"targetId": tid})
        except Exception:                               # noqa: BLE001
            pass
    return None


async def _place_window(ctx, page):
    """Park OUR window off-screen (ghost) or on-screen (visible), scoped to just this
    page's window so a concurrent tool's window is never moved. No-op under headless."""
    if config.HEADLESS:
        return
    if config.WINDOW_MODE == "ghost":
        bounds = {"left": -32000, "top": -32000, "width": 1480, "height": 900, "windowState": "normal"}
    else:
        return                                          # visible: leave Chrome's placement
    try:
        sess = await ctx.new_cdp_session(page)
        wid = (await sess.send("Browser.getWindowForTarget"))["windowId"]
        await sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
    except Exception:                                   # noqa: BLE001 - best-effort
        pass


async def _open_run_window(browser, ctx, workers, launched):
    """Open this run's `workers` tabs in ONE dedicated window and return them. First
    tab reuses an unclaimed blank launch tab or opens a new window; the rest follow
    into it. Then the window is placed (ghost/visible)."""
    if launched:
        await _await_launch_tab(ctx)
    first = None
    if config.AUTH_MODE == "cdp" and browser is not None:
        first = await _adopt_launch_tab(ctx) or await _open_new_window(browser, ctx)
    if first is None:
        pages = [await ctx.new_page() for _ in range(workers)]
    else:
        pages = [first] + [await ctx.new_page() for _ in range(workers - 1)]
    await _place_window(ctx, pages[0])
    return pages


async def _run(vins: list[str], workers: int, on_bol=None, need_by=None,
               sd_scan_pairs=None, sd_hits=None) -> dict:
    vins = list(dict.fromkeys(v for v in vins if v))   # dedupe, PRESERVE caller order
                                                       # (already state→city sorted)
    if not vins:
        return {}
    workers = max(1, min(workers, len(vins)))
    chunks = [vins[i::workers] for i in range(workers)]    # round-robin split
    results: dict = {}
    seen: dict = {}
    lock = asyncio.Lock()
    async with async_playwright() as p:
        proc = browser = None
        if config.AUTH_MODE == "cdp":
            # Attach to the REAL installed Chrome on the persistent, logged-in
            # profile (auto-launched if needed) — see chrome_cdp.py. This is
            # what keeps Tesla's captcha from stalling the run on Windows.
            proc = chrome_cdp.ensure_chrome()
            browser = await p.chromium.connect_over_cdp(config.CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=config.USER_DATA_DIR,
                headless=config.HEADLESS,
                accept_downloads=True,
                viewport={"width": 1480, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        own_pages: list = []                       # tabs THIS run created (so we can close them)
        try:
            # SHARED CHROME: this run opens its `workers` tabs in its OWN window (so it's
            # separate from tesla-reconcile's / another dispatcher's window), drives only
            # those tabs, and closes just them on exit. Never adopt ctx.pages wholesale —
            # that would steal another in-flight run's tabs. Window is placed off-screen
            # in ghost mode inside _open_run_window.
            pages = await _open_run_window(browser, ctx, workers, proc is not None)
            own_pages.extend(pages)

            tasks = [
                _worker(i + 1, pages[i], chunks[i], seen, lock, results, on_bol, need_by)
                for i in range(workers)
            ]

            # CONCURRENT SuperDispatch scan: in its OWN extra tab, alongside the BOL
            # downloads on the same CDP browser. It closes that tab when done and can
            # never break the BOL run (fully isolated). Results land in `sd_hits`.
            sd_page = None
            if sd_scan_pairs and sd_hits is not None:
                import sd_scrape
                sd_page = await ctx.new_page()

                async def _sd_scan(p):
                    try:
                        hits = await sd_scrape.scan_tabs_async(p, sd_scan_pairs)
                        sd_hits.extend(hits)
                        print(f"  SD scan done: {len(hits)} route-matched order(s).")
                    except Exception as exc:                # noqa: BLE001
                        print(f"  SD scan failed (skipped): {type(exc).__name__}: {exc}")
                    finally:
                        try:
                            await p.close()                 # close the SD tab when done
                        except Exception:
                            pass

                tasks.append(_sd_scan(sd_page))

            extra = " + 1 SD-scan tab" if sd_page else ""
            print(f"Running {workers} tab(s) over {len(vins)} VIN(s){extra}...")
            await asyncio.gather(*tasks)

            # Retry recoverable misses within the SAME run so a few transient failures
            # (search/download timeouts, a momentarily wedged tab) don't leave
            # stragglers that force a manual "resume". Each round re-navigates the tab,
            # which clears a stuck state; already-downloaded VINs are skipped.
            for rnd in range(1, config.BOL_RETRY_ROUNDS + 1):
                failed = [v for v in vins if not (results.get(v) or {}).get("path")]
                if not failed:
                    break
                print(f"\nRetry {rnd}/{config.BOL_RETRY_ROUNDS}: re-attempting "
                      f"{len(failed)} VIN(s) that didn't land yet: {failed}")
                rw = max(1, min(workers, len(failed)))
                rchunks = [failed[i::rw] for i in range(rw)]
                await asyncio.gather(*[
                    _worker(i + 1, pages[i], rchunks[i], seen, lock, results, on_bol, need_by)
                    for i in range(rw)
                ])
            leftover = [v for v in vins if not (results.get(v) or {}).get("path")]
            if leftover:
                print(f"\n{len(leftover)} VIN(s) still have no BOL after "
                      f"{config.BOL_RETRY_ROUNDS} retr(ies): {leftover}")
        finally:
            print("\nDone. Press Enter to close the browser...")
            try:
                await asyncio.get_event_loop().run_in_executor(None, input)
            except (EOFError, RuntimeError):
                pass
            # Close only THIS run's own tabs, so a concurrent dispatcher's tabs in the
            # shared Chrome keep running. Then detach (cdp) — which leaves Chrome up for
            # the other runs — or close our private context (non-cdp).
            if browser is not None:                  # cdp mode: shared Chrome
                for pg in own_pages:
                    try:
                        await pg.close()
                    except Exception:                # noqa: BLE001
                        pass
                await chrome_cdp.close_chrome_async(browser, proc)
            else:
                await ctx.close()
    return results


def download_for(vins: list[str], workers: int = 4, on_bol=None, need_by=None,
                 sd_scan_pairs=None, sd_hits=None) -> dict:
    """Download a BOL per VIN's most-recent shipment using `workers` parallel tabs,
    deduped by shipment. `on_bol(shp, path)` is called as each BOL is saved, so the
    caller can parse it as it lands instead of waiting for the whole batch. If a
    `need_by` dict is passed, it's filled with {vin: 'Need By' date string} captured
    from each VIN's dashboard row.

    If `sd_scan_pairs` (a set of (origin_zip, dest_zip)) and a mutable `sd_hits` list
    are given, the SuperDispatch Posted/Accepted/Pending scan runs CONCURRENTLY in
    its own tab on the same browser and appends its route-matched hits to `sd_hits`
    (that tab is closed when the scan finishes).

    Returns {vin: {'shp', 'path', 'need_by', 'status'}}."""
    return asyncio.run(_run(vins, workers, on_bol, need_by, sd_scan_pairs, sd_hits))
