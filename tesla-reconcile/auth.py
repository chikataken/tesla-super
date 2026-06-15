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

from playwright.sync_api import sync_playwright

import config


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
    ]
    if config.WINDOW_MODE == "ghost":
        # Real headed Chrome, just parked OFF-SCREEN where you can't see it —
        # fingerprint stays normal (true headless gets detected).
        # NOT minimized: on Windows, Chrome's native occlusion detection treats
        # a minimized/hidden window as occluded and FREEZES or discards its tabs
        # (fatal when multiple tabs run at once). CalculateNativeWinOcclusion=off
        # plus the backgrounding flags keep every tab live while the window stays
        # hidden off-screen.
        args += [
            "--window-position=-32000,-32000",
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    elif not config.HEADLESS:
        # Visible (--headed): force an ON-SCREEN position. The persistent profile may
        # have a saved OFF-SCREEN placement from a previous ghost run, which Chrome
        # would otherwise restore — leaving the "visible" window where you can't see it.
        b = _onscreen_bounds()
        args.append(f"--window-position={b['left']},{b['top']}")
    if config.HEADLESS:
        args.append("--headless=new")
    proc = subprocess.Popen(args)
    deadline = time.time() + 30
    while time.time() < deadline:
        if _cdp_alive(config.CDP_URL):
            return proc
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"Chrome did not open the CDP endpoint {config.CDP_URL} within 30s")


def close_chrome(browser, proc) -> None:
    """Shut the automation Chrome down when the run ends — ALWAYS.

    CDP_PROFILE_DIR is a dedicated automation profile, so any Chrome serving the
    endpoint is ours — including one left over from a previous run that crashed
    before cleanup (we attach to it with proc=None, and merely detaching used to
    leave its window and tabs up forever). So: ask the browser to close, VERIFY
    the endpoint actually went dark, and only if it didn't, kill the process
    tree (Windows needs taskkill /T — proc.kill() hits just the parent)."""
    try:
        browser.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        browser.close()              # drop our CDP connection either way
    except Exception:
        pass
    deadline = time.time() + 8
    while time.time() < deadline and _cdp_alive(config.CDP_URL):
        time.sleep(0.25)
    if _cdp_alive(config.CDP_URL):
        if proc is not None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                proc.kill()
        else:
            print("WARN: automation Chrome did not close — close the window manually.")
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
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


def _show_onscreen(ctx) -> None:
    """Force the automation window ON-SCREEN via CDP (visible / --headed mode).

    Mirror of _park_offscreen: the --window-position launch flag can be ignored when a
    persistent profile restores an off-screen placement (left by a prior ghost run),
    and it can't help at all when we attach to an already-running Chrome. Setting the
    bounds over CDP after startup is authoritative, so the window is actually visible."""
    bounds = _onscreen_bounds()
    pages = list(ctx.pages) or [ctx.new_page()]
    moved: set = set()
    for page in pages:
        try:
            sess = ctx.new_cdp_session(page)
            wid = sess.send("Browser.getWindowForTarget")["windowId"]
            if wid in moved:
                continue
            sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
            moved.add(wid)
        except Exception:                                # noqa: BLE001 - best-effort show
            pass


def _park_offscreen(ctx) -> None:
    """Move the automation window off-screen via CDP.

    The --window-position launch flag is NOT enough on Windows: Chrome's startup
    WindowSizer drags any off-screen window back onto a connected display (so on a
    two-monitor setup a sliver stays visible), and a persistent profile can restore
    the last *visible* bounds and ignore the flag entirely. Setting the bounds over
    CDP after startup bypasses the sizer and is authoritative. windowState stays
    "normal" (NOT minimized) so Chrome's occlusion logic doesn't freeze the
    background tabs mid-run — the whole reason we hide rather than minimize."""
    # Need at least one page target to resolve a window id. An auto-launched
    # Chrome has its initial tab here; if not, anchor one so the window exists and
    # is parked before the caller opens its tabs (which reuse this same window).
    pages = list(ctx.pages) or [ctx.new_page()]
    moved: set = set()
    for page in pages:
        try:
            sess = ctx.new_cdp_session(page)
            wid = sess.send("Browser.getWindowForTarget")["windowId"]
            if wid in moved:
                continue
            sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": _OFFSCREEN})
            moved.add(wid)
        except Exception:                                # noqa: BLE001 - best-effort hide
            pass


# --------------------------------------------------------------------------
# The context manager every script uses
# --------------------------------------------------------------------------
@contextmanager
def browser_context():
    with sync_playwright() as p:
        if config.AUTH_MODE == "cdp":
            proc = ensure_chrome()
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            # Place the window: ghost -> off-screen (hidden); visible/--headed ->
            # on-screen (override any restored off-screen bounds). No-op under
            # true --headless (no window).
            if not config.HEADLESS:
                if config.WINDOW_MODE == "ghost":
                    _park_offscreen(ctx)
                else:
                    _show_onscreen(ctx)
            try:
                yield ctx
            finally:
                close_chrome(browser, proc)
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
