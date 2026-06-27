"""Automatic Tesla SSO re-login when a run finds the supplier session logged out.

When a flow detects the Tesla login page (``auth.is_login_page(page)`` — the
suppliers.teslamotors.com session expired and 302'd to auth.tesla.com), call
``ensure_logged_in(page)``. It pulls the Tesla username / password / TOTP seed from
Vaultwarden (item ``BW_TESLA_ITEM``, default "Tesla"), then walks the SSO flow the
way a human would:

    email (#identity) ─Next─▶ password ─Sign In─▶ passcode (live TOTP) ─▶ KMSI ─▶ portal

Every field is typed human-paced (jittered pointer move + per-key delay, reusing
``sd_login``'s helpers) and at every step we watch for a *visibly rendered* captcha
challenge — which we will NOT fight (return False so a human can finish) rather than
risk flagging the account.

Why a Tesla-specific captcha probe (not sd_login's): auth.tesla.com always embeds a
*passive* hCaptcha (a hidden 300×150 iframe + a pre-issued response token) even when
no challenge is shown. sd_login._visible_captcha keys off offsetWidth/Height and would
mark that passive frame as "visible", bailing every time. ``_visible_captcha`` below
walks the ancestor chain for display/visibility/opacity and requires a real on-screen
box, so it only trips on an actual challenge.

Notes:
* Tesla forces TOTP 2FA on every fresh login. We compute the code from the vault seed
  on demand and, if the current code is about to roll over, wait for the next window so
  we never submit an expiring code.
* The shared Chrome profile's genuine fingerprint + real logged-in history is the main
  captcha defense; the human-like input here is insurance.
* Leaves the "Stay signed in?" (KMSI) prompt to ``tesla.dismiss_stay_signed_in`` /
  ``keep_session_alive`` — same handler the long-run flows already use.
"""
from __future__ import annotations
import os
import time

import auth
import config
import vault_totp
# Reuse the careful-interaction helpers so Tesla and SD type/click identically.
from sd_login import _human_fill, _human_click, _first_visible, _click_submit


def log(msg: str) -> None:
    """Project convention: print (runlog tees stdout to the run's log file)."""
    print(f"[tesla_login] {msg}")


# --- step recording (so a live run captures each page even if a selector misses) ---
_DEBUG_DIR = os.path.join(os.path.dirname(__file__), "output", "screenshots")


def _record(page, tag: str) -> None:
    """Best-effort screenshot + a one-line DOM summary of the current step. Pure
    diagnostics — never affects the flow, never raises."""
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(_DEBUG_DIR, f"tesla_login_{tag}.png"))
    except Exception:
        pass
    try:
        summary = page.evaluate(
            """() => {
                const vis = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                    return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none'; };
                const inputs = [...document.querySelectorAll('input:not([type=hidden]),textarea')]
                    .filter(vis).map(e => ({type:e.type, id:e.id||null, name:e.getAttribute('name'),
                        ac:e.getAttribute('autocomplete'), ph:e.getAttribute('placeholder')}));
                const btns = [...document.querySelectorAll('button,[role=button],input[type=submit]')]
                    .filter(vis).map(e => (e.innerText||e.value||e.getAttribute('aria-label')||'').trim().slice(0,30))
                    .filter(Boolean);
                return {url: location.href, inputs, buttons: btns};
            }""")
        log(f"  step={tag} url={summary['url'][:60]} inputs={summary['inputs']} buttons={summary['buttons']}")
    except Exception:
        pass


# --- captcha: only a genuinely on-screen challenge counts (see module docstring) ---
def _visible_captcha(page) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
                const rx = /hcaptcha|recaptcha|arkose|turnstile|captcha/i;
                const shown = el => {
                    for (let n = el; n && n instanceof Element; n = n.parentElement) {
                        const cs = getComputedStyle(n);
                        if (cs.display === 'none' || cs.visibility === 'hidden'
                            || parseFloat(cs.opacity || '1') === 0) return false;
                    }
                    const r = el.getBoundingClientRect();
                    // a real challenge is a sizeable box actually inside the viewport
                    return r.width > 60 && r.height > 60 && r.bottom > 0 && r.right > 0
                        && r.top < innerHeight && r.left < innerWidth;
                };
                return [...document.querySelectorAll('iframe')]
                    .some(f => rx.test(f.src || '') && shown(f));
            }"""))
    except Exception:
        return False


# Selector fallbacks for each step (first visible one wins). Tesla's markup shifts,
# so we keep these broad rather than pin one id.
_EMAIL_SELECTORS = ["input#identity", "input[name=identity]", "input[type=email]",
                    "input[autocomplete=username]", "input[name=email]"]
_PASS_SELECTORS = ["input#credential", "input[name=credential]",
                   "input[type=password]", "input[name=password]"]
_TOTP_SELECTORS = ["input[name=passcode]", "input#passcode",
                   "input[autocomplete=one-time-code]", "input[name*=passcode]",
                   "input[name*=code]", "input[name*=otp]"]


def _on_totp_step(page) -> bool:
    """True when the current page is asking for the 2FA passcode."""
    if _first_visible(page, _TOTP_SELECTORS) is not None:
        return True
    try:
        return page.get_by_text("passcode", exact=False).count() > 0 or \
               page.get_by_text("authentication app", exact=False).count() > 0
    except Exception:
        return False


def _fresh_totp() -> str | None:
    """Current Tesla TOTP, waiting out a near-expiry window so we never submit a code
    that rolls over mid-submit. None if the vault has no seed."""
    try:
        code, remaining = vault_totp.get_tesla_totp()
    except Exception as e:                              # noqa: BLE001
        log(f"could not fetch TOTP from vault: {e}")
        return None
    if remaining < 5:                                  # about to roll over — wait it out
        log(f"TOTP has {remaining}s left; waiting for the next window")
        time.sleep(remaining + 1)
        try:
            code, remaining = vault_totp.get_tesla_totp()
        except Exception:                              # noqa: BLE001
            pass
    return code


def _bail_if_captcha(page, where: str) -> bool:
    if _visible_captcha(page):
        log(f"visible captcha {where} — leaving it for a human")
        _record(page, f"captcha_{where}")
        return True
    return False


def ensure_logged_in(page) -> bool:
    """If `page` is on Tesla's SSO sign-in screen, log in from the vault creds.

    Returns True if it ends logged in (or already was), False if it couldn't (missing
    creds/seed, a visible captcha, or an unrecognized step) — the caller should then
    fall back to a manual `run_login`. Never raises on the captcha path."""
    if not auth.is_login_page(page):
        return True

    # Creds: vault item is authoritative (it also holds the TOTP seed); fall back to
    # the TESLA_EMAIL/PASSWORD env for username/password only.
    item = os.getenv("BW_TESLA_ITEM", "Tesla").strip()
    try:
        creds = vault_totp.get_login(item)
    except Exception as e:                              # noqa: BLE001
        log(f"vault fetch failed for {item!r}: {e}")
        creds = {}
    user = creds.get("username") or config.TESLA_EMAIL
    pw = creds.get("password") or config.TESLA_PASSWORD
    if not user or not pw:
        log("no Tesla username/password (vault item empty and TESLA_EMAIL/PASSWORD unset)")
        return False
    if not creds.get("totp"):
        log(f"warning: vault item {item!r} has no TOTP seed — 2FA step will need a human")

    _record(page, "01_email")
    if _bail_if_captcha(page, "on_email_page"):
        return False

    # Step 1 — email -> Next.
    efield = _first_visible(page, _EMAIL_SELECTORS)
    if efield is None:
        log("could not find the email field"); _record(page, "no_email"); return False
    _human_fill(page, efield, user)
    time.sleep(0.4)
    _click_submit(page)                                # "Next" / "Sign In" / Enter fallback
    page.wait_for_timeout(3000)
    _record(page, "02_after_email")
    if _bail_if_captcha(page, "after_email"):
        return False

    # Step 2 — password -> Sign In.
    pfield = _first_visible(page, _PASS_SELECTORS)
    if pfield is None:
        log("password field did not appear after the email step"); _record(page, "no_password"); return False
    _human_fill(page, pfield, pw)
    time.sleep(0.4)
    if _bail_if_captcha(page, "before_password_submit"):
        return False
    _click_submit(page)
    page.wait_for_timeout(3500)
    _record(page, "03_after_password")
    if _bail_if_captcha(page, "after_password"):
        return False

    # Step 3 — TOTP passcode (Tesla forces it on every fresh login).
    if _on_totp_step(page):
        tfield = _first_visible(page, _TOTP_SELECTORS)
        code = _fresh_totp()
        if tfield is None or not code:
            log("on the 2FA step but no passcode field/seed — needs a human"); _record(page, "no_totp"); return False
        _human_fill(page, tfield, code)
        time.sleep(0.3)
        if _bail_if_captcha(page, "before_totp_submit"):
            return False
        _click_submit(page)
        page.wait_for_timeout(4000)
        _record(page, "04_after_totp")
        if _bail_if_captcha(page, "after_totp"):
            return False

    # Step 4 — "Stay signed in?" (KMSI), then settle the redirect back to the portal.
    try:
        import tesla
        tesla.keep_session_alive(page)                 # idle popup OR KMSI affirmative
    except Exception as e:                             # noqa: BLE001
        log(f"KMSI handler note: {e}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    _record(page, "05_final")

    if not auth.is_login_page(page):
        log("re-login succeeded")
        return True
    log("still on the Tesla login flow after submit — wrong creds, an unknown step, or 2FA stall")
    return False


if __name__ == "__main__":
    # Manual test: open the Tesla portal, attempt auto-login if logged out.
    config.WINDOW_MODE = "visible"
    config.HEADLESS = False
    with auth.browser_context() as ctx:
        page = ctx.new_page()
        page.goto(config.TESLA_FLEET_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        print("on login page?:", auth.is_login_page(page))
        print("ensure_logged_in ->", ensure_logged_in(page))
        print("final url:", page.url)
