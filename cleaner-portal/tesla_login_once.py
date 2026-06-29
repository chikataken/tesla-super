"""
One-time Tesla login for cleaner-portal.

Opens the dedicated Chrome (the same CDP profile the cleanup uses, tesla-reconcile/.auth),
visible, on the Tesla vendor portal. You sign in once; the session is saved in the
profile and reused by every later cleanup run — so run-cleaner.command never has to
touch the login page.

Run from the tesla-reconcile directory with AUTH_MODE=cdp and PYTHONPATH pointed at
tesla-reconcile (login-once.command does this for you).
"""
import config
from auth import browser_context

DASHBOARD_URL = "https://suppliers.teslamotors.com/logistics/dispatchdashboard2"


def main():
    # Login is interactive — always a real, visible window regardless of ghost/headless.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with browser_context() as ctx:
        page = ctx.new_page()
        page.goto(DASHBOARD_URL)
        print("\n" + "-" * 58)
        print("Log into the Tesla vendor portal in the window that opened.")
        print("Wait until you can see the Dispatch Dashboard.")
        print("-" * 58)
        input("\nWhen you're logged in, press Return here to save the session... ")
        print(f"Session saved to {config.CDP_PROFILE_DIR}. You can close this window.")


if __name__ == "__main__":
    main()
