"""
Tesla Transport Vendor Portal interactions (selectors derived from the live DOM).

The portal is built on TDS (Tesla's Angular design system) + Angular CDK:
  * form controls are <tds-label for="..."> paired with an input/select by id
  * dropdowns open a `.cdk-overlay-pane`; options are <tds-option>, and a
    selected option carries the class `tds-option-selected`
  * an open dropdown leaves a transparent `.cdk-overlay-backdrop` that
    intercepts the next click until dismissed

Two pure-DOM checks (no model needed):
  * payment_check  -> is there a Paid / Sent for payment row for the VIN?
  * claims_check   -> any Destination claim filed for the VIN?
"""
from __future__ import annotations
import re

from playwright.sync_api import Page, TimeoutError as PWTimeout

import config
from models import PaymentResult, ClaimResult


def _click_and_wait_response(page: Page, click, url_substr: str,
                             timeout: int = 15000):
    """Click `click` and wait for a response whose URL contains url_substr.

    Returns the playwright Response if it arrived (truthy — callers may also
    use it as a bool), else None. On timeout it does NOT raise — it settles
    briefly and returns None, so the caller can still try to read the DOM (the
    request may have been served from cache, or just slow). This is the
    difference between a brittle hard-dependency on one network event and a run
    that survives a slow/flaky portal for hours."""
    try:
        with page.expect_response(lambda r: url_substr in r.url,
                                  timeout=timeout) as info:
            click()
        return info.value
    except PWTimeout:
        # The click likely still happened; give the table a moment to render.
        page.wait_for_timeout(2500)
        return None


# ----------------------- payment (Regular Fleet > Approved) -----------------------
def ensure_approved(page: Page) -> None:
    """Make sure the Fleet > Approved tab is active. No-op if it already is, so
    repeated payment checks reuse the same tab and settings — only the VIN
    changes between VINs."""
    active = page.locator("div[role=tab].tsl-tab-label-active").filter(has_text="Approved")
    try:
        if active.count() and active.first.is_visible():
            return
    except Exception:
        pass
    page.goto(config.TESLA_FLEET_URL)
    page.wait_for_load_state("domcontentloaded")
    # The tabs are <div role="tab">; click the real tab and WAIT for it to become
    # active (class tsl-tab-label-active) before the Approved form is mounted.
    page.get_by_role("tab", name="Approved", exact=True).click()
    active.wait_for(timeout=8000)
    page.wait_for_load_state("domcontentloaded")


def payment_check(page: Page, vin: str, attempts: int = 3) -> PaymentResult:
    """Look up a VIN on Fleet > Approved and decide from the API RESPONSE BODY,
    not the rendered table.

    Verified live 2026-06-12: the searchApprovedInvoice response is
    {"data": [...], "total": N} where each record carries
    carrierFacingStatus (the exact status string the UI shows) and
    details[].vin (the VIN the record belongs to). That lets the verdict be
    tied to THIS VIN with zero DOM timing involved. Reading the rendered rows
    was racy in both directions: the table can briefly still show the PREVIOUS
    search's rows 500ms+ after the response, which produced false "payment
    good" (stale row counted) and false SUS (stale no-good row) readings.

    Retries transient failures; returns indeterminate=True if it can never get
    a trustworthy answer (caller -> manual review, never SUS)."""
    target = vin.strip().upper()
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            keep_session_alive(page)      # clear idle popup OR KMSI prompt first
            ensure_approved(page)
            # Type the VIN with real keystrokes (a set-value is ignored by the
            # Angular form), then wait for the filter API response.
            vin_field = _control_for_label(page, "Full Vin")
            vin_field.click()
            vin_field.fill("")
            vin_field.press_sequentially(vin, delay=20)
            resp = _click_and_wait_response(
                page,
                lambda: page.get_by_role(
                    "button", name=re.compile("apply", re.I)).first.click(),
                "searchApprovedInvoice",
            )

            # GUARD (verified live 2026-06-12): if the VIN keystrokes didn't land
            # in the Angular field (overlay/session popup/field remount), Apply
            # runs an UNFILTERED search — HTTP 200 with the newest invoices —
            # so first verify the search we just ran was actually for OUR VIN.
            try:
                typed = (vin_field.input_value() or "").strip().upper()
            except Exception:
                typed = ""
            if typed != target:
                last_err = (f"Full Vin field held {typed!r} after Apply — "
                            f"search did not run for {vin}")
                _recover(page, attempt)
                continue

            if resp is None:
                last_err = "searchApprovedInvoice response never arrived"
                _recover(page, attempt)
                continue
            try:
                payload = resp.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict) or "data" not in payload:
                last_err = (f"unreadable searchApprovedInvoice response "
                            f"(HTTP {resp.status})")
                _recover(page, attempt)
                continue

            records = payload.get("data") or []
            mine = [r for r in records
                    if any((d.get("vin") or "").strip().upper() == target
                           for d in (r.get("details") or []))]
            if records and not mine:
                # Records came back but none are for our VIN — a mixed-up or
                # unfiltered response. Never a verdict; retry.
                last_err = "API records did not include the searched VIN"
                _recover(page, attempt)
                continue
            if not mine:
                # Confirmed by the API itself: no approved-invoice record.
                return PaymentResult(ok=False,
                                     note="API: 0 approved-invoice records for VIN")
            for r in mine:
                s = (r.get("carrierFacingStatus") or "").strip().lower()
                if s in config.GOOD_PAYMENT_STATUSES or any(
                        g in s for g in config.GOOD_PAYMENT_STATUSES):
                    return PaymentResult(ok=True, status=s)
            bad = sorted({(r.get("carrierFacingStatus") or "?") for r in mine})
            return PaymentResult(
                ok=False, note=f"records found but no good status: {bad}")
        except Exception as exc:                # navigation/selector hiccup
            last_err = f"{type(exc).__name__}: {exc}"
            _recover(page, attempt)
    return PaymentResult(ok=False, indeterminate=True,
                         note=f"could not read payment after {attempts} tries: {last_err}")


# ----------------------- claims (Claims > Filed) -----------------------
def setup_claims_filters(page: Page) -> None:
    """One-time per session: open Filed, check ALL Claim Status boxes,
    set Origin/Destination Damage = Destination. Best-effort (non-fatal)."""
    page.set_default_timeout(12000)
    _open_filed_form(page)
    try:
        _check_all_claim_statuses(page)
        _set_destination(page)
        print("claims filters set (all statuses + Destination)")
    except Exception as exc:
        print(f"WARN: claim filter setup skipped: {exc}")


def claims_check(page: Page, vin: str, attempts: int = 3) -> ClaimResult:
    """Search Filed claims by VIN; 0 records => no damage claim. Retries
    transient failures; returns indeterminate=True if it can never read so the
    caller skips the order rather than guessing.
    Assumes setup_claims_filters() already ran this session."""
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            keep_session_alive(page)      # clear idle popup OR KMSI prompt first
            box = page.get_by_placeholder("Enter VIN").first
            box.wait_for(timeout=8000)
            box.click()
            box.fill("")
            box.press_sequentially(vin, delay=20)
            got = _click_and_wait_response(
                page,
                lambda: page.get_by_role(
                    "button", name=re.compile(r"^\s*search\s*$", re.I)).first.click(),
                "Claims/dashboard/getdashboard",
            )
            page.wait_for_timeout(500)        # let the records count update
            if not got and not _records_label_present(page):
                # Never saw the response and no count text rendered -> retry.
                last_err = "claims dashboard did not respond"
                _recover(page, attempt, claims=True)
                continue
            count = _read_total_records(page)
            return ClaimResult(has_claim=count > 0, record_count=count)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            _recover(page, attempt, claims=True)
    return ClaimResult(has_claim=False, indeterminate=True, record_count=-1)


# ----------------------- helpers -----------------------
# Tesla shows an idle <session-timer-popup> ("your session is about to expire")
# that renders a tds-modal-local overlay on top of everything and INTERCEPTS all
# clicks until you act on it. Left alone it (a) blocks every field/button and
# (b) eventually logs you out — fatal for a multi-hour run. We click its
# keep-alive button, which both clears the overlay and extends the session.
_SESSION_POPUP = "session-timer-popup"
_STAY = re.compile(r"stay|keep|continue|extend|remain|active|yes|i'?m here|still", re.I)
_LOGOUT = re.compile(r"log\s*out|sign\s*out|^\s*no", re.I)


def dismiss_session_popup(page: Page) -> bool:
    """If the Tesla session-timeout popup is up, click its keep-alive button to
    extend the session and clear the overlay. Returns True if it handled one.
    Best-effort and never raises."""
    try:
        popup = page.locator(_SESSION_POPUP)
        if not popup.count() or not popup.first.is_visible():
            return False
    except Exception:
        return False
    btns = popup.locator("button, tds-button, [role=button]")
    try:
        n = btns.count()
    except Exception:
        n = 0
    # Prefer an explicit "stay signed in" affirmative; never click a logout button.
    for want_affirmative in (True, False):
        for i in range(n):
            b = btns.nth(i)
            try:
                t = (b.inner_text() or "").strip()
                if not t or _LOGOUT.search(t) or not b.is_visible():
                    continue
                if want_affirmative and not _STAY.search(t):
                    continue
                b.click(timeout=4000)
                page.wait_for_timeout(500)
                print(f"    (handled Tesla session popup -> '{t}')")
                return True
            except Exception:
                pass
    return False


# A SECOND, distinct interruption appears on long runs (seen ~170 orders in): the
# identity provider's "Stay signed in?" KEEP-ME-SIGNED-IN (KMSI) prompt — a full
# page / modal with a blue affirmative button. It is NOT the <session-timer-popup>
# web component above (so dismiss_session_popup can't see it); it's rendered by the
# login provider, sometimes after a redirect or inside an auth iframe. Left alone
# it blocks every click and the next action times out. Clicking the affirmative
# persists the session and returns to the portal; we also tick "Don't show this
# again" when present so it stops interrupting the rest of the run.
_STAY_SIGNED_IN = re.compile(r"stay\s*signed\s*in", re.I)
_KMSI_YES = re.compile(r"^\s*(yes|stay signed in)\s*$", re.I)
# Microsoft/Entra KMSI ids (the most common provider): primary "Yes" button and
# the "Don't show this again" checkbox.
_KMSI_YES_ID = "#idSIButton9"
_KMSI_DONT_SHOW = "#KmsiCheckboxField, input[name='DontShowAgain']"


def dismiss_stay_signed_in(page: Page) -> bool:
    """If the IdP "Stay signed in?" (KMSI) prompt is up, click its affirmative to
    keep the session and clear it. Returns True if it handled one. Best-effort and
    never raises. Checks every frame, since the prompt can render on the identity
    provider's page or inside an auth iframe."""
    for frame in page.frames:                 # includes the main frame
        try:
            # Only act when this really is the KMSI prompt — require the "Stay
            # signed in" text to be present so we never click a stray "Yes".
            if not frame.get_by_text(_STAY_SIGNED_IN).count():
                continue
        except Exception:
            continue
        # Tick "Don't show this again" first so it won't keep interrupting the run.
        try:
            cb = frame.locator(_KMSI_DONT_SHOW).first
            if cb.count() and cb.is_visible() and not cb.is_checked():
                cb.check(timeout=2000)
        except Exception:
            pass
        # Click the affirmative: the literal "Stay signed in" button the user sees,
        # else Microsoft's primary "Yes" (#idSIButton9), else any "Yes" button.
        for target in (
            frame.get_by_role("button", name=_STAY_SIGNED_IN),
            frame.locator(_KMSI_YES_ID),
            frame.get_by_role("button", name=_KMSI_YES),
        ):
            try:
                if target.count() and target.first.is_visible():
                    target.first.click(timeout=4000)
                    page.wait_for_timeout(800)
                    try:                       # it usually redirects back to the portal
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    print("    (handled 'Stay signed in?' KMSI prompt)")
                    return True
            except Exception:
                pass
    return False


def keep_session_alive(page: Page) -> bool:
    """Clear whichever keep-session-alive interruption is up before interacting:
    the Tesla idle session-timer popup OR the IdP "Stay signed in?" (KMSI) prompt.
    Best-effort; safe to call liberally."""
    a = dismiss_session_popup(page)
    b = dismiss_stay_signed_in(page)
    return a or b


def _recover(page: Page, attempt: int, claims: bool = False) -> None:
    """Between retries: back off, clear the session popup / any stuck overlay,
    and re-prime the page so the next attempt starts from a known state."""
    page.wait_for_timeout(min(1500 * attempt, 6000))     # linear backoff, capped
    try:
        keep_session_alive(page)        # idle popup OR "Stay signed in?" KMSI prompt
    except Exception:
        pass
    try:
        _dismiss_overlay(page)
    except Exception:
        pass
    try:
        if claims:
            if not page.get_by_placeholder("Enter VIN").count():
                _open_filed_form(page)
        else:
            ensure_approved(page)
    except Exception:
        pass


def _records_label_present(page: Page) -> bool:
    try:
        return page.get_by_text(re.compile(r"Total Reco", re.I)).first.is_visible()
    except Exception:
        return False


def _open_filed_form(page: Page) -> None:
    page.goto(config.TESLA_CLAIMS_LANDING)
    page.wait_for_load_state("domcontentloaded")
    try:
        page.get_by_text("Filed", exact=True).first.click(timeout=8000)
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        page.goto(config.TESLA_CLAIMS_URL)
        page.wait_for_load_state("domcontentloaded")
    page.get_by_placeholder("Enter VIN").first.wait_for(timeout=8000)


def _control_for_label(page: Page, label_text: str):
    """Return the *visible* input/select bound to a label via its `for` -> id.

    The Fleet page renders a "Full Vin" field per tab (Pending/Approved/...).
    When you switch tabs the old tab's field detaches LAZILY, so for a moment
    two fields are visible and the inactive one is mid-detach. Poll until exactly
    one is visible (the active tab's) to avoid grabbing the detaching one.

    The `for` may sit on the <tds-label> (Claims) or a nested native <label>
    (Fleet), so check both element types."""
    def _visible_targets():
        labs = page.locator("tds-label, label").filter(has_text=label_text)
        out = []
        for i in range(labs.count()):
            fid = labs.nth(i).get_attribute("for")
            if not fid:
                continue
            ctrl = page.locator(f"#{fid}").first
            try:
                if ctrl.is_visible():
                    out.append(ctrl)
            except Exception:
                pass
        return out

    for _ in range(40):                    # up to ~8s
        vis = _visible_targets()
        if len(vis) == 1:
            return vis[0]
        page.wait_for_timeout(200)
    vis = _visible_targets()
    return vis[0] if vis else page.locator("tds-label, label").filter(
        has_text=label_text).first


def _check_all_claim_statuses(page: Page) -> None:
    _control_for_label(page, "Claim Status").click()
    panel = page.locator(".cdk-overlay-pane")
    panel.locator("tds-option").first.wait_for(timeout=6000)
    opts = panel.locator("tds-option:not(.hidden)")
    for i in range(opts.count()):
        o = opts.nth(i)
        if "tds-option-selected" not in (o.get_attribute("class") or ""):
            o.click()
    _dismiss_overlay(page)


def _set_destination(page: Page) -> None:
    _control_for_label(page, "Origin/Destination Damage").click()
    panel = page.locator(".cdk-overlay-pane")
    panel.get_by_text("Destination", exact=True).first.click()
    _dismiss_overlay(page)


def _dismiss_overlay(page: Page) -> None:
    """Close any open CDK overlay so its backdrop stops intercepting clicks.

    NOTE: clicking the backdrop element directly fails — Playwright targets its
    centre, which is where the dropdown panel sits, so the panel eats the click.
    Use Escape, then a top-left corner click that lands on empty backdrop."""
    backdrop = page.locator(".cdk-overlay-backdrop")
    page.keyboard.press("Escape")
    try:
        backdrop.first.wait_for(state="hidden", timeout=2500)
        return
    except Exception:
        pass
    try:
        page.mouse.click(3, 3)            # empty corner of the full-screen backdrop
        backdrop.first.wait_for(state="hidden", timeout=2500)
    except Exception:
        pass


def _read_total_records(page: Page) -> int:
    # Their UI renders "Total Recods: N" (sic).
    try:
        txt = page.get_by_text(re.compile(r"Total Reco", re.I)).first.inner_text(timeout=4000)
    except Exception:
        return 0
    m = re.search(r"(\d+)", txt)
    return int(m.group(1)) if m else 0
