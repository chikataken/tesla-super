"""Central configuration, loaded from environment.

Credentials are read from the shared repo-root ``secrets/.env`` first (one place
for both tools), then any app-local ``.env`` as a fallback. Real environment
variables always win over both.
"""
import os
from dotenv import load_dotenv

# Shared credentials live in <repo-root>/secrets/.env so both tools read one file.
_SECRETS_ENV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "secrets", ".env")
load_dotenv(_SECRETS_ENV)   # shared, authoritative source
load_dotenv()               # app-local .env fallback (legacy)


def _install_sigint_handler() -> None:
    """Make Ctrl+C a clean exit for every reconcile script.

    1st Ctrl+C: print a one-line notice and raise KeyboardInterrupt so the normal
    `finally` cleanup runs (browser_context closes THIS run's tabs / window). A paired
    sys.excepthook then swallows the KeyboardInterrupt so the console shows the notice,
    not a traceback. 2nd Ctrl+C: force-quit (os._exit) — an escape hatch so a wedged
    close can never trap you. We never ignore SIGINT during cleanup."""
    import signal
    import sys
    state = {"n": 0}

    def _handler(signum, frame):
        state["n"] += 1
        if state["n"] == 1:
            sys.stderr.write("\n^C — closing this run's window and exiting "
                             "(Ctrl+C again to force-quit)...\n")
            sys.stderr.flush()
            raise KeyboardInterrupt
        os._exit(130)

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):           # not the main thread (e.g. under pytest)
        return

    _orig_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return                          # notice already printed; skip the traceback
        _orig_hook(exc_type, exc, tb)

    sys.excepthook = _hook


_install_sigint_handler()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


# --- Auth / runtime ---
# "cdp"    = attach over CDP to the REAL installed Chrome (default on Windows).
#            Real fingerprint + real logged-in profile => Tesla's captcha passes.
# "launch" = Playwright launches its own persistent context (default elsewhere).
AUTH_MODE = os.getenv("AUTH_MODE", "cdp" if os.name == "nt" else "launch").strip().lower()
# Must be 127.0.0.1, NOT localhost — on Windows localhost resolves to IPv6 and
# the connection is refused.
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip().rstrip("/")
# Persistent profile the auto-launched Chrome runs on. Log in once (run_login.py
# or by hand); the profile keeps you logged in — and trusted — between runs. Default
# is an ABSOLUTE path (…/tesla-super/tesla-reconcile/.auth) so every sibling tool
# resolves the SAME profile regardless of its working dir — a CWD-relative literal
# like "C:\tesla-profile" splits into a separate per-tool profile and breaks sharing.
_SHARED_PROFILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tesla-reconcile", ".auth")
CDP_PROFILE_DIR = os.getenv("CDP_PROFILE_DIR", _SHARED_PROFILE_DEFAULT)
# Full path to chrome.exe; leave empty to auto-detect the standard locations.
CHROME_PATH = os.getenv("CHROME_PATH", "").strip()
# Window visibility for the auto-launched Chrome (cdp mode only):
#   "visible" (default) — normal window.
#   "ghost"             — still a real HEADED Chrome (so bot detection sees a
#                         normal browser, unlike HEADLESS which gets flagged),
#                         but dragged off-screen via CDP after startup so it shows
#                         on no monitor (NOT minimized — minimizing would let
#                         occlusion logic freeze the tabs). It still shows in the
#                         taskbar; don't click it mid-run.
WINDOW_MODE = os.getenv("WINDOW_MODE", "visible").strip().lower()
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "./.auth")
HEADLESS = _bool("HEADLESS", "false")
# Browser to drive. "" = Playwright's bundled Chromium (default). Set to "chrome"
# or "msedge" to use the real installed browser — better networking and far less
# likely to trip hCaptcha / bot-detection (helps the Tesla login on Windows).
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "").strip()

# --- Portals ---
SD_BASE = os.getenv("SD_BASE", "https://shipper.superdispatch.com")
TESLA_BASE = os.getenv("TESLA_BASE", "https://suppliers.teslamotors.com")
TESLA_FLEET_URL = f"{TESLA_BASE}/logistics/invoicing/regular-fleet"
TESLA_CLAIMS_LANDING = f"{TESLA_BASE}/logistics/claims"          # has the Filed card
TESLA_CLAIMS_URL = f"{TESLA_BASE}/logistics/claims/dashboard"    # the Filed filter form

# Tesla SSO credentials for automated re-login when the session expires (see
# auth.is_login_page). Live ONLY in the shared, gitignored secrets/.env — never
# committed. Left blank, auto-login is disabled and a run just notifies/waits for
# a human. Password is NOT .strip()'d (could end in a meaningful space).
TESLA_EMAIL = os.getenv("TESLA_EMAIL", "").strip()
TESLA_PASSWORD = os.getenv("TESLA_PASSWORD", "")

# --- Model / vision ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-6")

# --- Business rules ---
PAYMENT_WINDOW_DAYS = int(os.getenv("PAYMENT_WINDOW_DAYS", "7"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "14"))

# Tags (visible in the list next to each order) that mean "already handled" —
# skip these orders entirely, no need to open them. Includes this program's own
# tags (CLAUDE is on everything it touches, so any shipment it already processed
# is skipped) so re-runs don't re-process or double-tag. "vin mismatch" alt
# spelling included defensively.
SKIP_TAGS = {
    "ok", "paid", "delivery confirmed", "damage claim",
    "claude", "sus", "zip code", "vins mismatch", "vin mismatch",
}

# Payment statuses that count as "paid / in flight". These are ALL five statuses
# the Approved tab's Status dropdown offers (verified live 2026-06-12) — i.e. the
# whole payment pipeline. An invoice in ANY of these states means Tesla has the
# invoice and it's progressing, so SUS is reserved for VINs with NO approved-
# invoice record at all. (Matched as lowercase substrings of the row text.)
GOOD_PAYMENT_STATUSES = {
    "invoice pending review", "amount confirmed", "processing",
    "sent for payment", "paid",
}

# Exact tag labels as they appear in the SuperDispatch Tags dropdown
# (verified against the live edit form).
TAG_DELIVERY_CONFIRMED = "Delivery confirmed"
TAG_DAMAGE_CLAIM = "Damage claim"
# Exact label is "No VIN photo" (singular) — a distinct tag from "Missing Photos".
TAG_NO_VIN_PHOTOS = "No VIN photo"
TAG_CLAUDE = "CLAUDE"     # added to EVERY shipment this script tags
TAG_SUS = "SUS"           # applied (with CLAUDE only) when payment is missing
TAG_ZIP = "ZIP CODE"      # applied (with CLAUDE only) when the actual delivered ZIP
                          # is too far from the scheduled delivery ZIP (wrong site)
TAG_VIN_MISMATCH = "VINS MISMATCH"   # applied (with CLAUDE only) when a VIN is legibly
                          # read in the photos but does NOT match the assigned VIN

# A delivered ZIP this many *driving minutes* (or more) from the scheduled ZIP is
# treated as a wrong-site delivery.
ZIP_DRIVE_MINUTES = int(os.getenv("ZIP_DRIVE_MINUTES", "20"))

# Output locations
LOG_CSV = "./output/actions.csv"
REVIEW_QUEUE = "./output/review_queue.jsonl"
SCREENSHOT_DIR = "./output/screenshots"
