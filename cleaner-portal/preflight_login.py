"""
Login pre-check for the Cleaner Portal app.

Attaches over CDP to the dedicated Chrome and opens the Tesla dashboard just long
enough to tell whether we're logged in or have been bounced to the "sign in again"
page (auth.tesla.com / the sign-in form). It changes nothing.

Exit codes:
  0   logged in  -> caller proceeds to run the cleanup
  10  signed out -> the Chrome window has been surfaced ON-SCREEN on the sign-in
                    page and LEFT OPEN so the user can sign in; caller tells them
  1   couldn't reach Tesla at all (network / launch problem)
"""
import sys

import config
# Stay off-screen ("ghost") while probing; only surface the window if we actually
# need a human to sign in.
config.WINDOW_MODE = "ghost"
config.HEADLESS = False

from playwright.sync_api import sync_playwright
from auth import ensure_chrome, is_login_page, _surface_window

DASHBOARD_URL = "https://suppliers.teslamotors.com/logistics/dispatchdashboard2"
NEED_LOGIN = 10


def main() -> int:
    with sync_playwright() as p:
        try:
            ensure_chrome()                      # launch the dedicated Chrome if needed
            browser = p.chromium.connect_over_cdp(config.CDP_URL)
        except Exception as e:                   # noqa: BLE001
            print(f"preflight: could not reach Chrome/Tesla: {e}")
            return 1
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()                    # raw page — we manage its lifecycle
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:                        # noqa: BLE001 - slow/redirecting is fine
            pass
        page.wait_for_timeout(2500)

        if is_login_page(page):
            _surface_window(page)                # bring the sign-in page on-screen
            try:
                page.bring_to_front()
            except Exception:                    # noqa: BLE001
                pass
            try:
                browser.close()                  # disconnect; LEAVE Chrome + login tab open
            except Exception:                    # noqa: BLE001
                pass
            print("preflight: signed out — sign-in window surfaced")
            return NEED_LOGIN

        # Logged in: tidy the probe tab and disconnect (Chrome stays running).
        try:
            page.close()
        except Exception:                        # noqa: BLE001
            pass
        try:
            browser.close()
        except Exception:                        # noqa: BLE001
            pass
        print("preflight: logged in")
        return 0


if __name__ == "__main__":
    sys.exit(main())
