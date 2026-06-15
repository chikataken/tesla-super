"""
Real-Chrome-over-CDP helpers (shared by auth.py and tesla_bol.py).

Why: launching a fresh automated browser (bundled Chromium, webdriver=true,
cold cookie-less profile) trips Tesla's bot detection on Windows and the
captcha never resolves. Attaching to the REAL installed Chrome on a persistent,
already-logged-in profile keeps webdriver false and carries real cookies and
reputation, so the captcha waves the session through. It only appears once, at
the manual login, which a human does.

ensure_chrome() starts Chrome with a debug port on CDP_PROFILE_DIR if none is
already listening, and returns the Popen handle (or None if one was running).
close_chrome() detaches — and shuts Chrome down only if we started it.
"""
import asyncio
import os
import subprocess
import time
import urllib.request

import config


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


_WIN_W, _WIN_H = 1560, 920


def _primary_screen_size() -> tuple[int, int]:
    """Primary monitor size in pixels (Windows); a sane default elsewhere."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:
        return 1920, 1080


def _centered_position(w: int, h: int) -> tuple[int, int]:
    sw, sh = _primary_screen_size()
    return max(0, (sw - w) // 2), max(0, (sh - h) // 2)


def ensure_chrome():
    """Ensure a real Chrome is listening on config.CDP_URL.

    Returns the Popen handle if WE launched it (caller closes it when done),
    or None if one was already running (leave that one alone)."""
    if _cdp_alive(config.CDP_URL):
        return None
    port = config.CDP_URL.rsplit(":", 1)[-1].strip("/")
    args = [
        _find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={config.CDP_PROFILE_DIR}",
        f"--window-size={_WIN_W},{_WIN_H}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if config.WINDOW_MODE == "ghost":
        # Real headed Chrome, just parked OFF-SCREEN where you can't see it —
        # fingerprint stays normal (true headless gets detected).
        # NOT minimized: on Windows, Chrome's native occlusion detection treats
        # a minimized/hidden window as occluded and FREEZES or discards its tabs.
        # That's fatal for the BOL step, which drives 4 background tabs at once —
        # they get discarded mid-run and Playwright sees "Target page, context or
        # browser has been closed". CalculateNativeWinOcclusion=off plus the
        # backgrounding flags keep every tab live while the window stays hidden.
        args += [
            "--window-position=-32000,-32000",
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    elif not config.HEADLESS:
        # Visible window: force it on-screen and centered. The persistent profile may
        # have a saved OFF-SCREEN placement left by a previous ghost run, which Chrome
        # would otherwise restore — an explicit --window-position overrides it.
        x, y = _centered_position(_WIN_W, _WIN_H)
        args.append(f"--window-position={x},{y}")
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


def center_window() -> None:
    """Best-effort: move/resize the automation Chrome window onto the primary screen,
    centered. Covers the case where an already-running (e.g. off-screen ghost) instance
    is being reused, so the command-line --window-position can't help. Never raises and
    never closes the user's Chrome."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return
    x, y = _centered_position(_WIN_W, _WIN_H)
    bounds = {"left": x, "top": y, "width": _WIN_W, "height": _WIN_H, "windowState": "normal"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
            # exiting the `with` disconnects the driver WITHOUT closing Chrome
            ctx = browser.contexts[0] if browser.contexts else None
            page = (ctx.pages[0] if ctx and ctx.pages else (ctx.new_page() if ctx else None))
            if page is None:
                return
            cdp = ctx.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds",
                     {"windowId": win["windowId"], "bounds": bounds})
    except Exception:
        return


def open_urls(urls) -> list:
    """Open each URL as a tab in the persistent-profile Chrome, launching that Chrome
    (with the CDP port) first if it isn't already up. Used by the one-time login flow:
    a normal user signs in here once and the cookies persist in the profile for later
    automated runs. Returns the URLs handed to Chrome."""
    ensure_chrome()                                   # ensure the profile's Chrome is up
    chrome = _find_chrome()
    opened = []
    for url in urls:
        if not url:
            continue
        # launching chrome.exe with the SAME user-data-dir routes the URL into the
        # already-running instance as a new tab, then exits.
        subprocess.Popen([chrome, url, f"--user-data-dir={config.CDP_PROFILE_DIR}"])
        opened.append(url)
    center_window()                                   # make sure the window is on-screen
    return opened


def _kill_tree(proc) -> None:
    """Kill the Chrome process tree (Windows needs taskkill /T — proc.kill()
    hits just the parent and can orphan the actual browser windows)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        proc.kill()


# CDP_PROFILE_DIR is a dedicated automation profile, so any Chrome serving the
# endpoint is ours — including one left over from a previous run that crashed
# before cleanup (we attach to it with proc=None, and merely detaching used to
# leave its window and tabs up forever). So both closers ask the browser to
# close, VERIFY the endpoint actually went dark, and only then fall back to
# killing the process tree.
def close_chrome_sync(browser, proc) -> None:
    """Sync API: close the automation Chrome when the run ends — always."""
    try:
        browser.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass
    deadline = time.time() + 8
    while time.time() < deadline and _cdp_alive(config.CDP_URL):
        time.sleep(0.25)
    if _cdp_alive(config.CDP_URL):
        if proc is not None:
            _kill_tree(proc)
        else:
            print("WARN: automation Chrome did not close — close the window manually.")
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


async def close_chrome_async(browser, proc) -> None:
    """Async API: close the automation Chrome when the run ends — always."""
    try:
        session = await browser.new_browser_cdp_session()
        await session.send("Browser.close")
    except Exception:
        pass
    try:
        await browser.close()
    except Exception:
        pass
    deadline = time.time() + 8
    while time.time() < deadline and _cdp_alive(config.CDP_URL):
        await asyncio.sleep(0.25)
    if _cdp_alive(config.CDP_URL):
        if proc is not None:
            _kill_tree(proc)
        else:
            print("WARN: automation Chrome did not close — close the window manually.")
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
