"""
End-of-day Tesla "Dispatch Dashboard 2.0" cleanup  (selectors calibrated live).

Bumps every shipment showing an "ETA Today" badge to tomorrow 14:00 (reason
"Early Arrival") and every "Pickup Date Today" OR "Pickup Date Late" shipment to
tomorrow (reason "Other") — both pickup badges share one "Update Pickup Date"
action, so they're selected together and updated in one shot. Repeats until gone. ALSO assigns a driver
(DRIVER_NAME, default "JESSICA TFI 2246664226"; override with CLEANUP_DRIVER) to
every "Driver Needed" unit (its driver field reads "No Driver Selected") via the
per-card driver dropdown — search the name, pick the matching option.

SAFE BY DEFAULT:
  * Dry-run unless --apply (dry-run only counts + reports, never submits).
  * Hard safety check: if more than ~20 "today" units (work day not over),
    it ABORTS with no changes.

Usage (or via clean.bat / clean.sh / `run.bat cleanup`):
    python tesla_cleanup.py             # dry-run: counts + plan   (headless)
    python tesla_cleanup.py --apply     # actually submit          (headless)
    python tesla_cleanup.py --headed    # show the browser window
HEADLESS BY DEFAULT = a real Chrome parked OFF-SCREEN (WINDOW_MODE=ghost), which
Tesla treats as a normal browser (true headless gets flagged). --headed shows it.
Browser/CDP/login settings are shared with the rest of tesla-reconcile (auth.py,
the C:\\tesla-profile CDP profile), so one `run.bat login` covers this too.

CALIBRATED against the live page:
  * cards = .grid-entry ; badges are plain text ("ETA Today" / "Pickup Date Today";
    "ETA Late" is ignored because we only select cards containing the target text)
  * "Select All Stops" = the FIRST <tsl-checkbox> in a card (2nd is Shipment Stops)
  * action bar: buttons "Update ETA" / "Update Pickup Date"
  * ETA popup: date input placeholder "Choose date" (type "D Mon YYYY", real keys);
    entering the date triggers a spinner that loads Time/Reason; Time + Reason are
    <tsl-option> dropdowns ("14:00" ; reason list = only "Team Driver"/"Early Arrival")
  * Pickup popup: date input placeholder "Mon DD YYYY"; reason list includes "Other"
  * submit button in either popup = "UPDATE" (distinct from the action-bar buttons)
"""
from __future__ import annotations
import argparse
import datetime as dt
import os
import re

from playwright.sync_api import Page

from auth import browser_context

DASHBOARD_URL = "https://suppliers.teslamotors.com/logistics/dispatchdashboard2"
SAFETY_MAX = 20

BADGE_ETA_TODAY = "ETA Today"
BADGE_PICKUP_TODAY = "Pickup Date Today"
BADGE_PICKUP_LATE = "Pickup Date Late"          # same "Update Pickup Date" action as Today

# Driver assignment: every "Driver Needed" unit (its driver field reads "No Driver
# Selected") gets this driver. Override with CLEANUP_DRIVER in .env. The name is
# matched against the dropdown option text whitespace-tolerantly.
NO_DRIVER = "No Driver Selected"
DRIVER_NAME = os.getenv("CLEANUP_DRIVER", "JESSICA TFI 2246664226").strip()


# ----------------------- dates (computed every run) -----------------------
def compute_dates():
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    # Build with an UNPADDED day (no leading zero) PORTABLY. "%-d" works on
    # Linux/macOS but is invalid on Windows (which uses "%#d"), so format the day
    # by hand and only use strftime for the locale month abbreviation.
    mon = tomorrow.strftime("%b")
    eta_date = f"{tomorrow.day} {mon} {tomorrow.year}"        # ETA field:    "10 Jun 2026"
    pickup_date = f"{mon} {tomorrow.day} {tomorrow.year}"     # Pickup field: "Jun 10 2026"
    return eta_date, pickup_date


# ----------------------- generic helpers -----------------------
def _dialog(page: Page):
    """The open pop-up = the OUTERMOST container holding both UPDATE and CANCEL.

    Several nested divs match (the innermost is just the button footer with no
    fields), so we take .first — the outer modal that actually contains the
    date/time/reason fields. (Using .last grabbed the empty footer — the bug.)"""
    return page.locator(
        "xpath=//div[.//button[normalize-space()='UPDATE'] "
        "and .//button[normalize-space()='CANCEL']]"
    ).first


def _set_dropdowns(page: Page, dialog, trigger_text: str, option_text: str):
    """Set EVERY <tsl-select> in the dialog still showing `trigger_text` to
    `option_text` (one block per selected shipment). Triggers are <tsl-select>;
    options render as <tsl-option> in an overlay."""
    for _ in range(80):
        trig = dialog.locator("tsl-select").filter(has_text=trigger_text)
        if trig.count() == 0:
            break
        t = trig.first
        t.scroll_into_view_if_needed()
        t.click()
        # tsl-option text has surrounding whitespace (e.g. " 14:00 "), so the
        # match must tolerate it — an anchored ^14:00$ fails.
        opt = page.locator("tsl-option").filter(
            has_text=re.compile(rf"^\s*{re.escape(option_text)}\s*$"))
        opt.first.scroll_into_view_if_needed()
        opt.first.click()
        page.wait_for_timeout(350)


def _fill_dates(dialog, placeholder: str, value: str, page: Page):
    inputs = dialog.locator(f"input[placeholder='{placeholder}']")
    for i in range(inputs.count()):
        f = inputs.nth(i)
        f.scroll_into_view_if_needed()
        f.click()
        f.fill("")
        f.press_sequentially(value, delay=20)      # Angular needs real keystrokes
        page.wait_for_timeout(1800)                 # spinner loads Time/Reason options


# ----------------------- search filters (Alerts / Status) -----------------------
# Status has exactly four options (verified live): Tendered, In Transit, Delivered,
# At Destination. We pull the three in-flight ones and DELIBERATELY exclude Delivered
# (a delivered shipment is done — never bump its dates). Alerts is selected in full.
STATUS_WANT = ["Tendered", "In Transit", "At Destination"]      # NOT "Delivered"


def _close_overlay(page: Page) -> None:
    """Fully close any open dropdown overlay. After selecting options, this portal can
    leave a transparent `.cdk-overlay-backdrop` that swallows the NEXT click — so the
    second dropdown (Status) never opens. Escape, then click the backdrop away, until
    it's gone."""
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(200)
        bd = page.locator(".cdk-overlay-backdrop")
        try:
            if bd.count() == 0:
                return
            bd.first.click(timeout=800)
        except Exception:
            return
        page.wait_for_timeout(200)


def _open_filter(page: Page, label: str) -> bool:
    """Open the Alerts/Status multiselect that follows the given label text."""
    _close_overlay(page)                                # clear any stale overlay first
    ctrl = page.locator(
        f"xpath=(//*[normalize-space(text())={label!r}])[1]"
        "/following::*[self::tsl-multiselect or self::tsl-select][1]").first
    try:
        ctrl.scroll_into_view_if_needed()
        ctrl.click()
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace, dropping any trailing '[count]' badge."""
    return re.sub(r"\s*\[\d+\]\s*$", "", " ".join((s or "").split())).lower()


def _wait_options(page: Page, settle_ms: int = 500, timeout_ms: int = 8000) -> int:
    """Wait for the open dropdown's options to finish rendering. The alert options load
    their [count] badges asynchronously and can appear in batches, so we wait until the
    option count is non-zero AND stable across two checks (or the timeout). Returns the
    final option count."""
    opts = page.locator(".cdk-overlay-pane tsl-option")
    last, waited = -1, 0
    while waited < timeout_ms:
        c = opts.count()
        if c > 0 and c == last:
            return c
        last = c
        page.wait_for_timeout(settle_ms)
        waited += settle_ms
    return opts.count()


def _apply_multiselect(page: Page, label: str, want=None,
                       select_all: bool = False, exclude=None) -> None:
    """Open `label` and make its selection EXACTLY right, then close cleanly.

    - select_all=True selects every option EXCEPT any whose text is in `exclude`
      (so an excluded option gets unchecked if it was on, and never re-checked).
    - otherwise exactly the options whose text is in `want` are selected, all others
      deselected.
    Selected state is read from each tsl-option's 'tsl-option-selected' class; clicking
    toggles, so we only click the options that must change."""
    if not _open_filter(page, label):
        print(f"  WARN: '{label}' filter not found — leaving it as-is.")
        return
    _wait_options(page)                                     # let all options render first
    wants = {_norm(w) for w in (want or [])}
    excludes = {_norm(e) for e in (exclude or [])}
    opts = page.locator(".cdk-overlay-pane tsl-option")
    for i in range(opts.count()):
        o = opts.nth(i)
        try:
            t = _norm(o.inner_text())
        except Exception:
            continue
        if not t:
            continue
        selected = "tsl-option-selected" in (o.get_attribute("class") or "")
        should = (t not in excludes) if select_all else (t in wants)
        if should != selected:                              # only click what must change
            try:
                o.scroll_into_view_if_needed()
                o.click()
                page.wait_for_timeout(200)
            except Exception:
                pass
    _close_overlay(page)                                    # close cleanly for the next dropdown


def configure_filters(page: Page) -> None:
    """Alerts = every option EXCEPT 'No Action Needed'; Status = Tendered + In Transit
    + At Destination (NEVER Delivered)."""
    _apply_multiselect(page, "Alerts", select_all=True, exclude=["No Action Needed"])
    _apply_multiselect(page, "Status", STATUS_WANT)


def _click_search(page: Page) -> None:
    try:
        page.get_by_role("button", name="Search", exact=True).first.click()
    except Exception:
        try:
            page.locator("button.search-btn").first.click()
        except Exception:
            pass


# ----------------------- page actions -----------------------
def load_dashboard(page: Page):
    page.goto(DASHBOARD_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3500)
    # Set the filters every load (a fresh page may reset them): Alerts = all, Status =
    # Tendered / In Transit / At Destination (NOT Delivered), then run the search.
    configure_filters(page)
    _click_search(page)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def count_badges(page: Page) -> tuple[int, int]:
    """(ETA Today count, Pickup Date Today + Pickup Date Late count). Pickup includes
    LATE because both pickup badges use the same "Update Pickup Date" action and are
    bumped together — a late unit was being skipped before."""
    for _ in range(8):                # render lazy rows
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(350)
    page.mouse.wheel(0, -60000)
    eta = page.get_by_text(BADGE_ETA_TODAY, exact=True).count()
    pickup = (page.get_by_text(BADGE_PICKUP_TODAY, exact=True).count()
              + page.get_by_text(BADGE_PICKUP_LATE, exact=True).count())
    return eta, pickup


def select_cards(page: Page, badge_text: str) -> int:
    return select_cards_any(page, [badge_text])


def select_cards_any(page: Page, badge_texts: list[str]) -> int:
    """Check the 'Select All Stops' box on every card containing ANY of badge_texts,
    each card EXACTLY ONCE (so a card matching two badges isn't toggled back off).
    Lets us select all Pickup Date Today + Pickup Date Late units, then hit "Update
    Pickup Date" a single time."""
    cards = page.locator(".grid-entry")
    n = 0
    for i in range(cards.count()):
        c = cards.nth(i)
        try:
            text = c.inner_text()
        except Exception:
            continue
        if not any(b in text for b in badge_texts):
            continue
        try:
            cb = c.locator("tsl-checkbox").first          # Select All Stops
            cb.scroll_into_view_if_needed()
            cb.click()
            n += 1
        except Exception:
            pass
    return n


def submit_dialog(page: Page):
    page.get_by_role("button", name="UPDATE", exact=True).last.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def process_eta(page: Page, eta_date: str) -> int:
    n = select_cards(page, BADGE_ETA_TODAY)
    if n == 0:
        return 0
    page.get_by_role("button", name="Update ETA").first.click()
    page.wait_for_timeout(1200)
    dlg = _dialog(page)
    _fill_dates(dlg, "Choose date", eta_date, page)
    _set_dropdowns(page, dlg, "Select Time", "14:00")
    _set_dropdowns(page, dlg, "Select Reason", "Early Arrival")
    submit_dialog(page)
    return n


def process_pickup(page: Page, pickup_date: str) -> int:
    # Select Pickup Date Today AND Pickup Date Late together, then Update once.
    n = select_cards_any(page, [BADGE_PICKUP_TODAY, BADGE_PICKUP_LATE])
    if n == 0:
        return 0
    page.get_by_role("button", name="Update Pickup Date").first.click()
    page.wait_for_timeout(1200)
    dlg = _dialog(page)
    _fill_dates(dlg, "Mon DD YYYY", pickup_date, page)
    try:
        _set_dropdowns(page, dlg, "Select Reason", "Other")
    except Exception:
        print("  WARN: 'Other' unavailable for a pickup block — pick a reason manually")
    submit_dialog(page)
    return n


# ----------------------- driver assignment -----------------------
# Each card (.grid-entry) carries one driver field: a tsl-multiselect whose trigger
# reads "No Driver Selected" until a driver is picked. Clicking it opens
# .tsl-multiselect-panel with a .tsl-multiselect-search-input ("Search...") and the
# driver list as <tsl-option>s. The search is an Angular form, so it needs REAL
# keystrokes (set-value is ignored) — same as the rest of this portal.
def _unassigned_driver_fields(page: Page):
    """Locator for every card driver field still showing 'No Driver Selected'."""
    return page.locator(".grid-entry .tsl-multiselect-trigger").filter(
        has_text=re.compile(re.escape(NO_DRIVER), re.I))


def count_driver_needed(page: Page) -> int:
    for _ in range(8):                        # render lazy rows
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(300)
    page.mouse.wheel(0, -60000)
    return _unassigned_driver_fields(page).count()


def assign_drivers(page: Page, driver_name: str) -> int:
    """Assign `driver_name` to every 'No Driver Selected' unit, one card at a time
    (the grid re-renders after each pick, so we always act on the FIRST remaining
    unassigned field). Returns the count assigned."""
    # Match the option whitespace-tolerantly ("JESSICA TFI  2246664226" etc.); the
    # search term is the first word, which filters the 500-long list to the driver.
    parts = driver_name.split()
    opt_re = re.compile(r"\s+".join(re.escape(p) for p in parts), re.I)
    term = parts[0] if parts else driver_name
    fields = _unassigned_driver_fields(page)

    assigned = 0
    for _ in range(60):                       # hard cap so a stuck card can't loop forever
        n = fields.count()
        if n == 0:
            break
        trig = fields.first
        try:
            trig.scroll_into_view_if_needed()
            trig.click()
            search = page.locator(".tsl-multiselect-search-input").first
            search.wait_for(timeout=6000)
            search.click()
            search.fill("")
            search.press_sequentially(term, delay=25)     # Angular needs real keystrokes
            page.wait_for_timeout(1000)                    # let the list filter
            opt = page.locator(".tsl-multiselect-panel tsl-option").filter(has_text=opt_re).first
            opt.wait_for(timeout=6000)
            opt.scroll_into_view_if_needed()
            opt.click()
            page.wait_for_timeout(500)
            page.keyboard.press("Escape")                  # close the panel -> commit
            page.wait_for_timeout(700)
        except Exception as exc:
            print(f"  WARN: driver assign failed on a unit ({type(exc).__name__}: {exc})")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            break
        if fields.count() >= n:               # the field didn't clear -> stop (avoid a loop)
            print("  WARN: a unit's driver didn't stick — stopping the driver phase.")
            break
        assigned += 1
        print(f"  assigned {driver_name!r} ({assigned})")
    return assigned


# ----------------------- orchestration -----------------------
def main(apply: bool):
    import runlog
    print(f"Logging this run to {runlog.start('cleanup')}")
    eta_date, pickup_date = compute_dates()
    print(f"Tomorrow -> ETA '{eta_date}'  |  Pickup '{pickup_date}'")
    print(f"Driver for unassigned units: {DRIVER_NAME!r}")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}\n" + "=" * 56)

    with browser_context() as ctx:
        page = ctx.new_page()
        load_dashboard(page)

        # ---- Driver phase: assign DRIVER_NAME to every 'Driver Needed' unit ----
        # Independent of the ETA/Pickup safety abort below, so drivers still get
        # assigned on a busy board.
        driver_total = 0
        need_drv = count_driver_needed(page)
        print(f"Driver Needed (No Driver Selected): {need_drv}")
        if need_drv:
            if not apply:
                print(f"  [DRY-RUN] Would assign {need_drv} unit(s) to {DRIVER_NAME!r}.")
            else:
                driver_total = assign_drivers(page, DRIVER_NAME)
                load_dashboard(page)

        eta_total = pickup_total = passes = 0
        while True:
            eta_n, pickup_n = count_badges(page)
            print(f"Pass {passes + 1}: ETA Today={eta_n}  Pickup Date Today={pickup_n}")

            if passes == 0 and (eta_n + pickup_n) > SAFETY_MAX:
                print(f"\nABORT: {eta_n + pickup_n} 'today' units (> {SAFETY_MAX}). "
                      f"Work day isn't over — no changes made. Notify the user.")
                return
            if eta_n + pickup_n == 0:
                break
            if not apply:
                print(f"\n[DRY-RUN] Would bump {eta_n} ETA Today -> {eta_date} 14:00 "
                      f"(Early Arrival) and {pickup_n} Pickup Date Today/Late -> "
                      f"{pickup_date} (Other). Re-run with --apply to submit.")
                return

            eta_total += process_eta(page, eta_date)
            load_dashboard(page)
            pickup_total += process_pickup(page, pickup_date)
            load_dashboard(page)

            passes += 1
            if passes > 10:
                print("Stopping after 10 passes (safety cap)."); break

        print("\n" + "=" * 56)
        print(f"Done — no alerts left. Drivers assigned: {driver_total}, "
              f"ETA updated: {eta_total}, Pickup updated: {pickup_total}, passes: {passes}. "
              f"Tomorrow used: ETA '{eta_date}', Pickup '{pickup_date}'.")
        # No prompt — once the badges are cleared, exit and close the browser.


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually submit updates")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window. Default is headless: a real Chrome "
                         "parked off-screen (WINDOW_MODE=ghost) — Tesla-safe, unlike "
                         "true headless.")
    args = ap.parse_args()

    # Chrome window mode (mirrors the rest of tesla-reconcile). Default to the
    # off-screen "ghost" Chrome — our practical headless — and only show a real
    # window with --headed. Set before browser_context() reads it.
    import config
    if args.headed:
        config.WINDOW_MODE = "visible"
    else:
        config.WINDOW_MODE = "ghost"
    config.HEADLESS = False              # never true headless: Tesla flags it

    main(args.apply)
