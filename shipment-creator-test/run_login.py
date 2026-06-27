"""
One-time (and occasional) login helper.

    python run_login.py

Opens the persistent profile, navigates to the Tesla portal. Log in (incl. any
SSO/2FA), then press Enter here to save the session. No passwords are stored by
the script — you type them directly into Tesla's own page.
"""
import config
from auth import browser_context


def main():
    # Login must be interactive — always show a real, visible window regardless
    # of WINDOW_MODE=ghost / HEADLESS in .env.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(config.TESLA_DASHBOARD_URL)
        print("Log into the Tesla Logistics Vendor Portal in the opened window.")
        input("When you can see the Dispatch Dashboard, press Enter to save & exit... ")


if __name__ == "__main__":
    main()
