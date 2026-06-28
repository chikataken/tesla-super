"""SuperDispatch session helpers — automatic Vaultwarden re-login + one-time manual setup.

Two entry points:

* ensure_session(page=None) / ensure_logged_in(page) — when a web flow finds the SD
  session logged out, pull the SD username/password from Vaultwarden (item BW_SD_ITEM)
  and carefully type them in (human-paced, with small pointer moves), watching for a
  VISIBLE captcha challenge — which it will NOT fight (returns 'captcha' so a human can
  finish) rather than risk flagging the account.

* interactive_login() — the original one-time manual login (`python sd_login.py`): opens
  the shared profile on the sign-in page so a human can log in once. No passwords are
  stored by this path — you type them into SuperDispatch's own page.

Notes:
* SD's 2FA is an email/SMS code (not a TOTP), so it can't be auto-filled. The shared
  Chrome profile's device-trust cookie normally skips 2FA on re-login; if a
  verification-code page appears anyway, we bail (return '2fa') for a human.
* The real logged-in Chrome profile + genuine fingerprint is the main captcha defense —
  the human-like input here is just insurance. ensure_logged_in only ever submits ONE
  login attempt and never retries, so it can't hammer SD into a captcha.

Mirrors tesla-reconcile/sd_login.py and direct-pickup-checks/sd_login.py (kept in-project
per the no-cross-folder-import rule).
"""
from __future__ import annotations
import os
import random
import time

import config
import vault_totp

# Outcome of an auto-login attempt. LOGIN_OK = logged in (or never logged out); every
# other value is a reason a human (or a wait) is needed before web work can resume.
LOGIN_OK = "ok"
# Reasons that genuinely need a human to act (captcha to solve, 2FA code to enter, or
# creds the vault can't provide). A recovery probe must NOT auto-login on these.
HUMAN_NEEDED = frozenset({"captcha", "2fa", "no_creds"})
# Other codes ('vault_error', 'unknown') are transient — safe to retry after a backoff.


class AuthBlocked(RuntimeError):
    """Raised by a web flow when it hit the login page and auto-login could not complete.
    `reason` is one of the codes above so the caller can fail fast / notify."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def log(msg: str) -> None:
    """Project convention: print (systemd/journald captures stdout)."""
    print(f"[sd_login] {msg}")


def is_login_page(page) -> bool:
    """True if `page` is on the SuperDispatch sign-in screen."""
    try:
        url = (page.url or "").lower()
        if "login" in url or "signin" in url:
            return True
        return page.locator("input[type=password]").count() > 0
    except Exception:
        return False


def _visible_captcha(page) -> bool:
    """Detect a *visible* captcha challenge (hCaptcha/recaptcha/Turnstile/Arkose) — not
    the passive/invisible kind that's always embedded."""
    try:
        return bool(page.evaluate(
            """() => [...document.querySelectorAll('iframe')]
                 .filter(f => /hcaptcha|recaptcha|arkose|turnstile|captcha/i.test(f.src||''))
                 .some(f => f.offsetWidth > 0 && f.offsetHeight > 0)"""))
    except Exception:
        return False


def _human_fill(page, locator, text: str) -> None:
    """Move to the field, click, and type with per-key jitter — looks human, not scripted."""
    try:
        box = locator.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                            box["y"] + box["height"] * random.uniform(0.3, 0.7),
                            steps=random.randint(8, 18))
    except Exception:
        pass
    locator.click()
    time.sleep(random.uniform(0.1, 0.3))
    try:
        locator.fill("")
    except Exception:
        pass
    for ch in text:
        page.keyboard.type(ch, delay=random.uniform(45, 130))


def _first_visible(page, selectors: list[str]):
    """Return the first visible locator among selectors, or None."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


_USER_SELECTORS = ["input[type=email]", "input[name=email]", "input[name=username]",
                   "input[autocomplete=username]", "input[type=text]"]
_PASS_SELECTORS = ["input[type=password]", "input[name=password]"]
_SUBMIT_NAMES = ["Log In", "Log in", "Sign In", "Sign in", "Continue", "Next"]


def _click_submit(page) -> None:
    for name in _SUBMIT_NAMES:
        try:
            btn = page.get_by_role("button", name=name, exact=False)
            if btn.count():
                btn.first.click()
                return
        except Exception:
            pass
    page.keyboard.press("Enter")    # fallback: submit the form


def _surface_window(page) -> None:
    """Bring OUR (ghost/off-screen) Chrome window on-screen and focus it so a person can
    resolve a captcha or enter a 2FA code. Best-effort; no-op under headless. Sync."""
    if getattr(config, "HEADLESS", False):
        return
    try:
        sess = page.context.new_cdp_session(page)
        wid = sess.send("Browser.getWindowForTarget")["windowId"]
        sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds":
            {"left": 120, "top": 60, "width": 1480, "height": 900, "windowState": "normal"}})
    except Exception:                                   # noqa: BLE001
        pass
    try:
        page.bring_to_front()
    except Exception:                                   # noqa: BLE001
        pass


def _needs_human(page, reason: str) -> str:
    """Surface the window (so a human can act on the captcha/2FA), then return the reason
    code unchanged. The single-attempt login still defers to the person + the caller's
    re-probe loop — surfacing just makes the challenge visible to solve."""
    _surface_window(page)
    return reason


def ensure_logged_in(page) -> str:
    """If `page` is on the SD login screen, log in from Vaultwarden creds.

    Returns LOGIN_OK if it ends logged in (or already was), otherwise a short reason
    code: 'captcha' | '2fa' | 'no_creds' | 'vault_error' | 'unknown'. Never raises.

    Captcha is checked FIRST — before any Vaultwarden call — so a captcha page never
    triggers a credential fetch (and so can't contribute to vault rate-limiting). Only a
    SINGLE login attempt is ever submitted (no retry loop), to avoid provoking a captcha."""
    if not is_login_page(page):
        return LOGIN_OK

    if _visible_captcha(page):
        log("visible captcha on the login page — leaving it for a human")
        return _needs_human(page, "captcha")

    item = os.getenv("BW_SD_ITEM", "SuperDispatch").strip()
    try:
        creds = vault_totp.get_login(item)
    except Exception as e:                              # noqa: BLE001
        log(f"vault fetch failed for {item!r}: {e}")
        return "vault_error"
    user, pw = creds.get("username"), creds.get("password")
    if not user or not pw:
        log(f"vault item {item!r} missing username/password")
        return "no_creds"

    # Step 1: username. Some SD logins are one page (user+pass together), some are two-step
    # (user -> Next -> pass). Fill the username, then look for a password field; if it's not
    # there yet, advance and look again.
    ufield = _first_visible(page, _USER_SELECTORS)
    if ufield is None:
        log("could not find the username field"); return "unknown"
    _human_fill(page, ufield, user)
    time.sleep(random.uniform(0.3, 0.7))

    pfield = _first_visible(page, _PASS_SELECTORS)
    if pfield is None:
        _click_submit(page)                            # two-step: advance to password
        page.wait_for_timeout(2500)
        if _visible_captcha(page):
            log("captcha after username step — leaving it for a human"); return _needs_human(page, "captcha")
        pfield = _first_visible(page, _PASS_SELECTORS)
    if pfield is None:
        log("could not find the password field"); return "unknown"
    _human_fill(page, pfield, pw)
    time.sleep(random.uniform(0.3, 0.7))

    if _visible_captcha(page):
        log("captcha before submit — leaving it for a human"); return _needs_human(page, "captcha")
    _click_submit(page)
    page.wait_for_timeout(5000)

    # Verify outcome.
    if _visible_captcha(page):
        log("captcha after submit — leaving it for a human"); return _needs_human(page, "captcha")
    if not is_login_page(page):
        log("re-login succeeded")
        return LOGIN_OK
    # Still on a login-ish page — likely a 2FA email/SMS code prompt we can't fill.
    if page.locator("input[autocomplete=one-time-code], input[name*=code], input[name*=otp]").count():
        log("SD wants a 2FA code (email/SMS) — can't auto-fill; needs a human")
        return _needs_human(page, "2fa")
    log("still on the login page after submit — wrong creds or unknown layout")
    return "unknown"


def ensure_session(page=None) -> str:
    """Make sure the shared Chrome has a live SuperDispatch session, auto-logging in from
    Vaultwarden if it's logged out.

    Pass an EXISTING (sync) page to reuse it, or omit it to open the shared browser just
    for this preflight (used by the async run, which can't drive the sync login itself).
    Returns LOGIN_OK or a reason code; never raises — callers decide whether a non-OK
    result is fatal or just means 'scan with no SD session'."""
    if page is not None:
        return _ensure_on(page)
    try:
        import auth
        with auth.browser_context() as ctx:
            # Own tab: another dispatcher may share this Chrome; don't disturb their page.
            p = ctx.new_page()
            try:
                return _ensure_on(p)
            finally:
                try: p.close()
                except Exception: pass
    except Exception as e:                              # noqa: BLE001
        log(f"session preflight could not open the browser: {e}")
        return "unknown"


def _ensure_on(page) -> str:
    """Navigate `page` to SD and ensure it's logged in. ensure_logged_in returns LOGIN_OK
    immediately when the page isn't a login screen, so this is a cheap no-op when already
    signed in."""
    try:
        page.goto(config.SD_WEB_BASE + "/orders", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except Exception:
        pass
    return ensure_logged_in(page)


def login_or_block(page) -> bool:
    """Convenience for web flows: detect the login page and attempt auto-login. Returns
    True if logged in (or never logged out); raises AuthBlocked(reason) otherwise."""
    status = ensure_logged_in(page)
    if status == LOGIN_OK:
        return True
    raise AuthBlocked(status, f"SuperDispatch auto-login could not complete ({status})")


def interactive_login():
    """One-time MANUAL login (`python sd_login.py`): open the shared profile on the SD
    sign-in page and let a human log in (incl. any 2FA). The same profile is reused by the
    BOL/scrape tools, so this only has to be done once — and again if auto-login bails for
    a captcha/2FA that needs a person."""
    from auth import browser_context
    # Login is interactive — force a real, visible window regardless of WINDOW_MODE=ghost.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(config.SD_WEB_BASE + "/signin")
        print(f"Log into SuperDispatch in the opened window ({config.SD_WEB_BASE}).")
        input("When you can see the SuperDispatch dashboard, press Enter to save & exit... ")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Probe: open SD and attempt auto-login if logged out (no manual prompt).
        print("ensure_session ->", ensure_session())   # 'ok' | reason code
    else:
        interactive_login()
