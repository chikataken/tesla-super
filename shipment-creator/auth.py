"""
Persistent-auth browser context (shared with the Tesla BOL step).

Two modes, selected by AUTH_MODE in .env:

* "cdp" (default on Windows) — attach over CDP to the REAL installed Chrome on
  a persistent, already-logged-in profile (auto-launched on CDP_PROFILE_DIR if
  none is running, closed again after). webdriver stays false, the fingerprint
  and cookies are real, so Tesla's captcha doesn't stall. See chrome_cdp.py.

* "launch" (default elsewhere; the original Mac path) — launch_persistent_context
  on USER_DATA_DIR.

Log in once with `python run_login.py`; the profile persists the session.
No passwords are typed by the script.
"""
from contextlib import contextmanager
from playwright.sync_api import sync_playwright
import chrome_cdp
import config


@contextmanager
def browser_context():
    with sync_playwright() as p:
        if config.AUTH_MODE == "cdp":
            proc = chrome_cdp.ensure_chrome()
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            try:
                yield ctx
            finally:
                chrome_cdp.close_chrome_sync(browser, proc)
            return

        ctx = p.chromium.launch_persistent_context(
            user_data_dir=config.USER_DATA_DIR,
            headless=config.HEADLESS,
            accept_downloads=True,                 # needed to capture the BOL PDF
            viewport={"width": 1560, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield ctx
        finally:
            ctx.close()
