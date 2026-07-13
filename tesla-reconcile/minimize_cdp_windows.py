"""Minimize every window of the shared CDP Chrome (run_chrome_cdp.sh helper).

Same mechanism as auth.py's _place_window: --start-minimized doesn't stick at
launch, but a runtime Browser.setWindowBounds({"windowState": "minimized"})
does. The --disable-backgrounding/occlusion launch flags keep all tabs fully
live while minimized, so CDP automation is unaffected.
"""
import sys

from playwright.sync_api import sync_playwright

CDP_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        try:
            done: set[int] = set()
            for ctx in browser.contexts:
                pages = ctx.pages or [ctx.new_page()]
                for page in pages:
                    try:
                        sess = ctx.new_cdp_session(page)
                        wid = sess.send("Browser.getWindowForTarget")["windowId"]
                        if wid not in done:
                            sess.send("Browser.setWindowBounds",
                                      {"windowId": wid,
                                       "bounds": {"windowState": "minimized"}})
                            done.add(wid)
                    except Exception:                    # noqa: BLE001 - best-effort
                        pass
            print(f"minimized {len(done)} window(s)")
        finally:
            # connect_over_cdp: close() disconnects WITHOUT quitting Chrome.
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
