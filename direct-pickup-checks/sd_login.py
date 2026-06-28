"""Automatic SuperDispatch re-login when the worker finds the session logged out.

When a flow detects the SD login page, call `ensure_logged_in(page)`: it pulls the
SD username/password from Vaultwarden (item BW_SD_ITEM) and carefully types them in
(human-paced, with small pointer moves), watching for a VISIBLE captcha challenge —
which it will NOT fight (returns False so a human can finish) rather than risk
flagging the account.

Notes:
* SD's 2FA is an email/SMS code (not a TOTP), so it can't be auto-filled here. The
  shared Chrome profile's device-trust cookie normally skips 2FA on re-login; if a
  verification-code page appears anyway, we bail (return False) for a human.
* The real logged-in Chrome profile + genuine fingerprint is the main captcha
  defense — the human-like input here is just insurance.

This is a near-verbatim copy of tesla-reconcile/sd_login.py (kept in-project per the
no-cross-folder-import rule); the only differences are config.SD_WEB_BASE (this
project's name for the web base URL) and the local `browser` module in __main__.
"""
from __future__ import annotations
import os
import random
import time

import config
import vault_totp

# Outcome of an auto-login attempt. LOGIN_OK = logged in (or never logged out);
# every other value is a reason a human (or a wait) is needed before web work can
# resume — the caller turns these into an AuthBlocked that trips the worker's gate.
LOGIN_OK = "ok"
# Reasons that genuinely need a human to act (captcha to solve, 2FA code to enter,
# or creds the vault can't provide). A recovery probe must NOT auto-login on these.
HUMAN_NEEDED = frozenset({"captcha", "2fa", "no_creds"})
# Reasons that are transient (vault unreachable / rate-limited, or an unrecognised
# post-submit state) — safe to retry auto-login after a backoff, no human required.


class AuthBlocked(RuntimeError):
    """Raised by a web flow when it hit the login page and auto-login could not
    complete. `reason` is one of the codes above; the worker trips its auth gate and
    parks the order until the session is restored."""

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
        if "login" in (page.url or "").lower():
            return True
        return page.locator("input[type=password]").count() > 0
    except Exception:
        return False


def _visible_captcha(page) -> bool:
    """Detect a *visible* captcha challenge (hCaptcha/recaptcha/Turnstile/Arkose) —
    not the passive/invisible kind that's always embedded."""
    try:
        return bool(page.evaluate(
            """() => [...document.querySelectorAll('iframe')]
                 .filter(f => /hcaptcha|recaptcha|arkose|turnstile|captcha/i.test(f.src||''))
                 .some(f => f.offsetWidth > 0 && f.offsetHeight > 0)"""))
    except Exception:
        return False


def _human_fill(page, locator, text: str) -> None:
    """Move to the field, click, and type with per-key jitter."""
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
    code unchanged. The worker's auth-gate re-probe loop resumes once it's resolved."""
    _surface_window(page)
    return reason


def ensure_logged_in(page) -> str:
    """If `page` is on the SD login screen, log in from Vaultwarden creds.

    Returns LOGIN_OK if it ends logged in (or already was), otherwise a short reason
    code: 'captcha' | '2fa' | 'no_creds' | 'vault_error' | 'unknown'. Never raises.

    Captcha is checked FIRST — before any Vaultwarden call — so a captcha page never
    triggers a credential fetch (and so can't contribute to vault rate-limiting)."""
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

    # Step 1: username. Some SD logins are one page (user+pass together), some are
    # two-step (user -> Next -> pass). Fill the username, then look for a password
    # field; if it's not there yet, advance and look again.
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


def login_or_block(page) -> bool:
    """Convenience for web flows: detect the login page and attempt auto-login.

    Returns True if logged in (or never logged out). If auto-login can't complete,
    raises AuthBlocked(reason) so the worker trips its gate and parks the order until
    the session is restored — instead of looping and hammering login."""
    status = ensure_logged_in(page)
    if status == LOGIN_OK:
        return True
    raise AuthBlocked(status, f"SuperDispatch auto-login could not complete ({status})")


if __name__ == "__main__":
    # Manual test: open SD, attempt auto-login if logged out.
    from browser import browser_context
    with browser_context() as ctx:
        page = ctx.new_page()
        page.goto(config.SD_WEB_BASE + "/orders", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        print("on login page?:", is_login_page(page))
        print("ensure_logged_in ->", ensure_logged_in(page))   # 'ok' | reason code
