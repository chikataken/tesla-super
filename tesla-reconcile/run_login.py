"""
One-time (and occasional) login helper.

Run:  python run_login.py
It opens SuperDispatch and the Tesla portal in a visible browser using the
persistent profile. Log into BOTH, then press Enter here. The session is saved
in USER_DATA_DIR and reused by main.py.
"""
import config
from auth import browser_context


def main():
    # Login must be interactive — always show a real, visible window regardless
    # of WINDOW_MODE=ghost / HEADLESS in .env.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with browser_context() as ctx:
        page = ctx.new_page()
        page.goto(config.SD_BASE + "/orders/invoiced")
        t = ctx.new_page()
        t.goto(config.TESLA_FLEET_URL)
        print("\n--- Log into BOTH tabs in the opened browser. ---")
        print("SuperDispatch:", config.SD_BASE)
        print("Tesla:", config.TESLA_FLEET_URL)
        input("\nWhen both are logged in, press Enter to save the session and exit... ")
        import sd_login
        sd_login.clear_2fa_lock()          # manual login done -> auto-logins may resume
        print("Session saved to", config.USER_DATA_DIR)


if __name__ == "__main__":
    main()
