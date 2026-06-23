"""Automatic SuperDispatch re-login when a run finds the session logged out.

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
"""
from __future__ import annotations
import os
import random
import time

import config
import vault_totp


def log(msg: str) -> None:
    """Project convention: print (runlog tees stdout to the run's log file)."""
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


def ensure_logged_in(page) -> bool:
    """If `page` is on the SD login screen, log in from Vaultwarden creds.

    Returns True if it ends logged in (or already was), False if it couldn't (missing
    creds, a visible captcha, or a 2FA code page) — the caller should then fall back
    to a manual `run_login`. Never raises on the captcha/2FA path."""
    if not is_login_page(page):
        return True

    item = os.getenv("BW_SD_ITEM", "SuperDispatch").strip()
    try:
        creds = vault_totp.get_login(item)
    except Exception as e:                              # noqa: BLE001
        log(f"sd_login: vault fetch failed for {item!r}: {e}")
        return False
    user, pw = creds.get("username"), creds.get("password")
    if not user or not pw:
        log(f"sd_login: vault item {item!r} missing username/password")
        return False

    if _visible_captcha(page):
        log("sd_login: visible captcha on the login page — leaving it for a human")
        return False

    # Step 1: username. Some SD logins are one page (user+pass together), some are
    # two-step (user -> Next -> pass). Fill the username, then look for a password
    # field; if it's not there yet, advance and look again.
    ufield = _first_visible(page, _USER_SELECTORS)
    if ufield is None:
        log("sd_login: could not find the username field"); return False
    _human_fill(page, ufield, user)
    time.sleep(random.uniform(0.3, 0.7))

    pfield = _first_visible(page, _PASS_SELECTORS)
    if pfield is None:
        _click_submit(page)                            # two-step: advance to password
        page.wait_for_timeout(2500)
        if _visible_captcha(page):
            log("sd_login: captcha after username step — leaving it for a human"); return False
        pfield = _first_visible(page, _PASS_SELECTORS)
    if pfield is None:
        log("sd_login: could not find the password field"); return False
    _human_fill(page, pfield, pw)
    time.sleep(random.uniform(0.3, 0.7))

    if _visible_captcha(page):
        log("sd_login: captcha before submit — leaving it for a human"); return False
    _click_submit(page)
    page.wait_for_timeout(5000)

    # Verify outcome.
    if _visible_captcha(page):
        log("sd_login: captcha after submit — leaving it for a human"); return False
    if not is_login_page(page):
        log("sd_login: re-login succeeded")
        return True
    # Still on a login-ish page — likely a 2FA email/SMS code prompt we can't fill.
    if page.locator("input[autocomplete=one-time-code], input[name*=code], input[name*=otp]").count():
        log("sd_login: SD wants a 2FA code (email/SMS) — can't auto-fill; needs a human")
    else:
        log("sd_login: still on the login page after submit — wrong creds or unknown layout")
    return False


if __name__ == "__main__":
    # Manual test: open SD, attempt auto-login if logged out.
    from auth import browser_context
    with browser_context() as ctx:
        page = ctx.new_page()
        page.goto(config.SD_BASE + "/orders", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        print("on login page?:", is_login_page(page))
        print("ensure_logged_in ->", ensure_logged_in(page))
