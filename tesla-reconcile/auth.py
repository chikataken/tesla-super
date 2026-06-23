"""
Persistent-auth browser context.

Two modes, selected by AUTH_MODE in .env:

* "cdp" (default on Windows) — attach over CDP to the REAL installed Chrome.
  If nothing is listening on the debug port, a Chrome is auto-launched on a
  dedicated persistent profile (CDP_PROFILE_DIR) and closed again when done.
  Because it's the real browser on a real, logged-in profile,
  navigator.webdriver stays false, the fingerprint/codecs/cookies are genuine,
  and Tesla's captcha doesn't stall the run. The captcha only appears once, at
  the manual login, which a human does (run_login.py).

* "launch" (default elsewhere; what worked on the Mac) — Playwright launches a
  persistent context on USER_DATA_DIR (bundled Chromium, or the real installed
  browser if BROWSER_CHANNEL is set).

Either way: log in manually once and the profile persists the session.
No passwords are ever stored or typed by the script.
"""
from contextlib import contextmanager
import os
import subprocess
import time
import urllib.request

from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import config


# --------------------------------------------------------------------------
# Login-page detection
# --------------------------------------------------------------------------
# When the Tesla SSO session expires, every suppliers.teslamotors.com page
# silently 302-redirects to auth.tesla.com's OAuth2 sign-in screen. Without an
# explicit check a run just blows its `wait_for_load_state("networkidle")`
# timeout (~30s) and crashes with an opaque TimeoutError. Detect it instead so a
# caller can fail fast, notify, or trigger re-login.
TESLA_AUTH_HOST = "auth.tesla.com"          # OAuth2 redirect target when logged out
TESLA_EMAIL_BOX = "input#identity"          # the Sign-In email field (name/id="identity")


def is_login_page(page) -> bool:
    """True when `page` is sitting on Tesla's SSO sign-in screen, not the app.

    Two independent signals, EITHER of which is sufficient (so it works even if
    the OAuth redirect is still mid-flight or the host scheme shifts):
      * the page URL is on auth.tesla.com (the OAuth2 redirect target), or
      * the sign-in email box (input#identity) is present in the DOM.
    Cheap and DOM-light — safe to call right after a goto(), before any
    networkidle wait."""
    try:
        if urlparse(page.url).hostname == TESLA_AUTH_HOST:
            return True
    except Exception:                        # noqa: BLE001 - page may be navigating
        pass
    try:
        return page.locator(TESLA_EMAIL_BOX).count() > 0
    except Exception:                        # noqa: BLE001 - frame detached mid-nav
        return False


def require_logged_in(page, where: str = "") -> None:
    """Raise immediately if `page` is on the Tesla SSO login screen — i.e. the session
    expired or was killed mid-run (e.g. Akamai bot-detection). Call right after a
    goto() so a run fails fast with a clear message instead of hanging on a load wait
    or scraping an empty board."""
    if is_login_page(page):
        loc = f" at {where}" if where else ""
        raise RuntimeError(
            f"Tesla session is logged out{loc} (redirected to the auth.tesla.com "
            "sign-in page). Re-login (run the login flow), then retry.")


# --------------------------------------------------------------------------
# CDP helpers
# --------------------------------------------------------------------------
def _cdp_alive(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint + "/json/version", timeout=1):
            return True
    except Exception:
        return False


def _find_chrome() -> str:
    if config.CHROME_PATH:
        return config.CHROME_PATH
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
    ]
    for c in candidates:
        if c and "%" not in c and os.path.exists(c):
            return c
    raise FileNotFoundError(
        "Google Chrome not found — set CHROME_PATH in .env to the full path of chrome.exe"
    )


def ensure_chrome():
    """Make sure a real Chrome is listening on config.CDP_URL.

    Returns the Popen handle if WE launched it (so the caller closes it when
    done), or None if one was already running (we leave that one alone).
    """
    if _cdp_alive(config.CDP_URL):
        return None
    port = config.CDP_URL.rsplit(":", 1)[-1].strip("/")
    args = [
        _find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={config.CDP_PROFILE_DIR}",
        "--window-size=1560,920",
        "--no-first-run",
        "--no-default-browser-check",
        # This tool drives FOUR tabs in one window at once; only one is ever the
        # foreground tab, so the other three are "background"/occluded. Chrome
        # then throttles and de-prioritizes them — their layout/compositor goes
        # stale, so Playwright reads a collapsed viewport and every click on a
        # genuinely-on-screen element fails with "element is outside of the
        # viewport" until it times out. These flags keep EVERY tab fully live
        # regardless of which window/tab is in front, so they apply in all modes
        # (CalculateNativeWinOcclusion is a Windows-only no-op elsewhere).
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
    ]
    if config.WINDOW_MODE == "ghost":
        # Real headed Chrome, just parked OFF-SCREEN where you can't see it —
        # fingerprint stays normal (true headless gets detected). NOT minimized:
        # a minimized/hidden window reads as occluded; the backgrounding flags
        # above keep its tabs live while it stays hidden off-screen.
        args += ["--window-position=-32000,-32000"]
    elif not config.HEADLESS:
        # Visible (--headed): force an ON-SCREEN position. The persistent profile may
        # have a saved OFF-SCREEN placement from a previous ghost run, which Chrome
        # would otherwise restore — leaving the "visible" window where you can't see it.
        b = _onscreen_bounds()
        args.append(f"--window-position={b['left']},{b['top']}")
    if config.HEADLESS:
        args.append("--headless=new")
    # Launch DETACHED in its own session so the shared Chrome outlives whichever tool
    # started it: otherwise it sits in the tool's process group and a Ctrl+C in that
    # terminal would kill Chrome (and every other tool's tabs). DEVNULL keeps Chrome's
    # own log spam out of the tool's console.
    proc = subprocess.Popen(args, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        if _cdp_alive(config.CDP_URL):
            return proc
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"Chrome did not open the CDP endpoint {config.CDP_URL} within 30s")


# --------------------------------------------------------------------------
# Shared-Chrome window management
# --------------------------------------------------------------------------
# One detached Chrome serves both tools (they share the logged-in profile, which only
# one Chrome process can open). A run opens its tabs in its OWN window and, on exit,
# closes ONLY those tabs — leaving the browser and any other tool's window running.
def _safe_close(page) -> None:
    try:
        page.close(run_before_unload=False)              # no beforeunload dialog -> can't hang
    except Exception:                                    # noqa: BLE001 - already gone
        pass


def _await_startup_tabs(ctx) -> list:
    """The blank launch tab(s) Chrome opens. Just after launch they may not have
    registered in ctx.pages yet, so poll briefly."""
    for _ in range(40):                                  # up to ~2s
        pages = list(ctx.pages)
        if pages:
            return pages
        time.sleep(0.05)
    return []


# Chrome's blank launch tab is ALWAYS at one of these URLs; a tool's working tabs are
# navigated away immediately and a freshly opened tab is "about:blank" (not new-tab-
# page). So adopting one of these as our first tab can never steal another tool's tab.
_LAUNCH_URLS = {"chrome://new-tab-page/", "chrome://newtab/", "chrome://new-tab-page/#"}


def _adopt_launch_tab(ctx):
    """Return an UNCLAIMED blank launch tab to reuse as our window's first tab, or
    None. Lets the run that opens first reuse Chrome's launch window (no extra window,
    no leftover blank); a later run finds none and opens its own new window instead."""
    for pg in list(ctx.pages):
        try:
            if pg.url in _LAUNCH_URLS:
                return pg
        except Exception:                                # noqa: BLE001 - page vanished
            pass
    return None


def _open_new_window(browser, ctx):
    """Open a page in a brand-new Chrome WINDOW (not a tab of an existing one) and
    return it, so an attaching run's tabs are separate from the other tool's window.

    Uses ctx.expect_page() with a unique-marker predicate. This is CRITICAL in sync
    Playwright: a busy `time.sleep()` poll of ctx.pages does NOT advance Playwright's
    event loop, so the new page never registers, the poll times out, and we'd silently
    fall back to a plain tab in whatever window is focused (the OTHER tool's). The
    predicate keeps it race-free; on failure the created target is closed so no window
    leaks."""
    import uuid
    marker = "tfi-" + uuid.uuid4().hex
    sess = browser.new_browser_cdp_session()
    tid = None
    try:
        with ctx.expect_page(predicate=lambda pg: marker in pg.url, timeout=10000) as info:
            res = sess.send("Target.createTarget", {"url": "about:blank#" + marker, "newWindow": True})
            tid = res.get("targetId")
        return info.value
    except Exception:                                    # noqa: BLE001 - fall back to a tab
        if tid is not None:
            try:
                browser.new_browser_cdp_session().send("Target.closeTarget", {"targetId": tid})
            except Exception:                            # noqa: BLE001
                pass
        return None


def _place_window(ctx, page) -> None:
    """Park OUR window off-screen (ghost) or force it on-screen (visible), scoped to
    just this page's window so a concurrent tool's window is never moved. No-op under
    true --headless (no window)."""
    if config.HEADLESS:
        return
    bounds = _OFFSCREEN if config.WINDOW_MODE == "ghost" else _onscreen_bounds()
    try:
        sess = ctx.new_cdp_session(page)
        wid = sess.send("Browser.getWindowForTarget")["windowId"]
        sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
    except Exception:                                    # noqa: BLE001 - best-effort placement
        pass


def _close_own_and_detach(browser, own_pages) -> None:
    """Close ONLY this run's tabs (which closes its window), then drop the CDP
    connection — leaving the shared Chrome and any other tool's window running.
    browser.close() on a connect_over_cdp browser disconnects WITHOUT quitting it."""
    for pg in own_pages:
        _safe_close(pg)
    try:
        browser.close()
    except Exception:                                    # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Ghost mode: force the window off-screen *after* startup
# --------------------------------------------------------------------------
# Coordinates far off any monitor on the Windows virtual desktop (valid range is
# roughly ±32767). Width/height are kept normal so the window is a real, painted
# window — just nowhere a display can show it.
_OFFSCREEN = {"left": -32000, "top": -32000, "width": 1560, "height": 920,
              "windowState": "normal"}


def _onscreen_bounds() -> dict:
    """A visible, centered window placement on the primary monitor (safe default if
    the screen size can't be read)."""
    sw, sh = 1920, 1080
    try:
        import ctypes
        u = ctypes.windll.user32
        try:
            u.SetProcessDPIAware()
        except Exception:                                # noqa: BLE001
            pass
        sw, sh = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:                                    # noqa: BLE001 - non-Windows / no ctypes
        pass
    return {"left": max(0, (sw - 1560) // 2), "top": max(0, (sh - 920) // 2),
            "width": 1560, "height": 920, "windowState": "normal"}


# --------------------------------------------------------------------------
# The context manager every script uses
# --------------------------------------------------------------------------
@contextmanager
def browser_context():
    with sync_playwright() as p:
        if config.AUTH_MODE == "cdp":
            proc = ensure_chrome()
            launched = proc is not None
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()

            # SHARED CHROME. This run opens ONLY its own tabs, in its OWN window, and
            # on exit closes just those (its window) — the browser and any other tool's
            # window keep running. Two cases for the first tab:
            #   * launched (we started Chrome): adopt Chrome's single blank launch tab
            #     as our first tab — no extra window, no leftover blank tab.
            #   * attached (Chrome already up, the other tool is using it): open our
            #     first tab in a NEW window so we don't share the other tool's window.
            # Scripts just call ctx.new_page() as before; we wrap it to do the above.
            own_pages: list = []
            if launched:
                _await_startup_tabs(ctx)                  # let Chrome's launch tab register
            _orig_new_page = ctx.new_page

            def _new_page(*a, **k):
                first = not own_pages
                if first:
                    # First tab -> our OWN window. Adopt Chrome's blank launch tab ONLY
                    # when WE launched Chrome: then it's our own fresh, uncontested
                    # window. When ATTACHING to a Chrome the other tool already started,
                    # NEVER adopt — a stray blank tab belongs to the OTHER tool's window,
                    # and adopting it pulls all our tabs into their window (the overlap
                    # bug). Open our own NEW window instead; plain tab only as a last
                    # resort.
                    pg = ((launched and _adopt_launch_tab(ctx))
                          or _open_new_window(browser, ctx)
                          or _orig_new_page(*a, **k))
                else:
                    # Later tabs: ctx.new_page() opens in whatever window currently has
                    # focus — which may be the OTHER tool's. Focus OUR window first so
                    # the new tab is created in it, never in a concurrent tool's window.
                    try:
                        own_pages[0].bring_to_front()
                    except Exception:
                        pass
                    pg = _orig_new_page(*a, **k)
                own_pages.append(pg)
                if first:
                    _place_window(ctx, pg)                # ghost/visible — OUR window only
                return pg
            ctx.new_page = _new_page

            try:
                yield ctx
            finally:
                _close_own_and_detach(browser, own_pages)
            return

        # --- "launch" mode (the original Mac path) ---
        kwargs = dict(
            user_data_dir=config.USER_DATA_DIR,
            headless=config.HEADLESS,
            viewport={"width": 1560, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Optionally drive the REAL installed browser (e.g. BROWSER_CHANNEL=chrome)
        # instead of Playwright's bundled Chromium — full networking/codecs and far
        # less likely to be flagged by hCaptcha / bot-detection. Empty = bundled.
        if config.BROWSER_CHANNEL:
            kwargs["channel"] = config.BROWSER_CHANNEL
        ctx = p.chromium.launch_persistent_context(**kwargs)
        try:
            yield ctx
        finally:
            ctx.close()
