"""
One-time (and occasional) login helper.

Run:  python run_login.py
Opens Super Dispatch in a visible browser using the persistent profile. Log in,
then press Enter here. The session is saved and reused by the worker's Playwright
steps (open order, download pickup photos, edit tags).

The profile is SHARED with tesla-reconcile / shipment-creator (same CDP_PROFILE_DIR),
so if you've already logged in there, this is effectively a no-op.
"""
import config
from browser import browser_context


def main():
    # Login must be interactive — always show a real, visible window regardless of
    # WINDOW_MODE=ghost / HEADLESS in .env.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with browser_context() as ctx:
        page = ctx.new_page()
        page.goto(config.SD_WEB_BASE + "/orders")
        print("\n--- Log into Super Dispatch in the opened browser. ---")
        print("Super Dispatch:", config.SD_WEB_BASE)
        input("\nWhen logged in, press Enter to save the session and exit... ")
        print("Session saved (profile reused on the next run).")


if __name__ == "__main__":
    main()
