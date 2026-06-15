"""Configuration + the header synonym map that makes parsing resilient to
'somewhat consistent' column names. Add a variant to a list and it just works."""
import os
from dotenv import load_dotenv

import paths

load_dotenv()
# Layer saved GUI settings (settings.json + Windows Credential Manager) underneath
# real environment variables, so a normal user can configure the app without a
# .env file. Real env vars and .env still win (setdefault).
try:
    import settings_store
    settings_store.apply_to_env()
except Exception:                                   # noqa: BLE001 - never block startup
    pass

# --- SuperDispatch API credentials ---------------------------------------
# Flip the whole integration between sandbox and live by changing ONE value:
#   SD_ENV=test         -> uses SD_TEST_* credentials   (default)
#   SD_ENV=production   -> uses SD_PROD_* credentials
# Each env has its own client id / secret / base url. You can also just set the
# generic SD_CLIENT_ID / SD_CLIENT_SECRET / SD_API_BASE and skip the test/prod
# split entirely.
SD_ENV = os.getenv("SD_ENV", "test").strip().lower()
_PROD = SD_ENV in {"prod", "production", "live"}


def _sd(name: str, default: str = "") -> str:
    """Prefer the env-specific value (SD_PROD_X / SD_TEST_X), then generic SD_X."""
    prefix = "SD_PROD_" if _PROD else "SD_TEST_"
    return (os.getenv(prefix + name) or os.getenv("SD_" + name, default)).strip()


SD_API_BASE = _sd("API_BASE", "https://api.shipper.superdispatch.com")
# Credentials: the documented names are SUPERDISPATCH_CLIENT_ID / _SECRET. We read
# those first; the legacy SD_(TEST|PROD)_CLIENT_ID / _SECRET names still work as a
# fallback so existing setups don't break.
SD_CLIENT_ID = (os.getenv("SUPERDISPATCH_CLIENT_ID") or _sd("CLIENT_ID")).strip()
SD_CLIENT_SECRET = (os.getenv("SUPERDISPATCH_CLIENT_SECRET") or _sd("CLIENT_SECRET")).strip()

# --- SuperDispatch web loadboard scan (browser-scraped, not the API) ---
# The API has no list-all / no route search, so existing live shipments on a route
# are discovered by scraping the Shipper TMS web UI (Posted + Accepted + Pending
# order-status tabs) during a --create run, filtered to the Excel's origin/dest zips.
SD_WEB_BASE = os.getenv("SD_WEB_BASE", "https://shipper.superdispatch.com").rstrip("/")
SD_SCAN = os.getenv("SD_SCAN", "true").strip().lower() in {"1", "true", "yes"}
SD_SCAN_MAX_PAGES = int(os.getenv("SD_SCAN_MAX_PAGES", "30"))   # safety cap per tab
SD_SCAN_THROTTLE_S = float(os.getenv("SD_SCAN_THROTTLE_S", "0.4"))

# --- Tesla ---
TESLA_BASE = os.getenv("TESLA_BASE", "https://suppliers.teslamotors.com")
TESLA_DASHBOARD_URL = f"{TESLA_BASE}/logistics/dispatchdashboard2"
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "./.auth")
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes"}

# --- Auth mode (Windows captcha fix) ---
# "cdp"    = attach over CDP to the REAL installed Chrome (default on Windows).
#            Real fingerprint + real logged-in profile => Tesla's captcha passes.
# "launch" = Playwright launches its own persistent context (default elsewhere).
AUTH_MODE = os.getenv("AUTH_MODE", "cdp" if os.name == "nt" else "launch").strip().lower()
# Must be 127.0.0.1, NOT localhost — on Windows localhost resolves to IPv6 and
# the connection is refused.
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip().rstrip("/")
# Persistent profile the auto-launched Chrome runs on; shared with
# tesla-reconcile so one manual login covers both tools.
CDP_PROFILE_DIR = os.getenv("CDP_PROFILE_DIR", r"C:\tesla-profile")
# Full path to chrome.exe; leave empty to auto-detect the standard locations.
CHROME_PATH = os.getenv("CHROME_PATH", "").strip()
# Window visibility for the auto-launched Chrome (cdp mode only):
#   "visible" (default) — normal window.
#   "ghost"             — still a real HEADED Chrome (so bot detection sees a
#                         normal browser, unlike HEADLESS which gets flagged),
#                         but parked off-screen and minimized. It still shows in
#                         the taskbar; don't click it mid-run.
WINDOW_MODE = os.getenv("WINDOW_MODE", "visible").strip().lower()
BOL_DIR = os.getenv("BOL_DIR", os.path.join(paths.OUTPUT_DIR, "bols"))
# Purge each downloaded BOL PDF as soon as its data has been read into a staged
# order (default). Set KEEP_BOLS=true to retain them in BOL_DIR (e.g. for the
# download-only flow, which never reads them and so never purges regardless).
KEEP_BOLS = os.getenv("KEEP_BOLS", "false").strip().lower() in {"1", "true", "yes"}

# Default spreadsheet used when none is specified (CLI --excel / GUI field):
# "formatted.xlsx" sitting next to this program (the shipment-creator folder).
# Override with EXCEL_PATH in .env to point elsewhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
# Dev: formatted.xlsx beside the source. Frozen: there's no source folder to read
# from, so fall back to the writable data dir — but normally the user picks their
# own sheet in Settings (EXCEL_PATH), which wins here.
DEFAULT_EXCEL = os.getenv("EXCEL_PATH") or os.path.join(
    paths.DATA_DIR if paths.is_frozen() else _HERE, "formatted.xlsx")

# --- Column mapping ---
# Canonical field  ->  list of accepted header variants (matched case-insensitively
# after normalization: lowercased, punctuation stripped, whitespace collapsed).
# A fuzzy match is the fallback for anything not listed here.
COLUMN_SYNONYMS = {
    # grouping / identity
    "group_id":         ["group", "group id", "load", "load id", "load number", "order id",
                         "ref", "reference", "shipment number", "shipment", "shipment id"],
    "vin":              ["vin", "vin number", "vin #", "vehicle vin", "full vin"],
    "year":             ["year", "yr", "model year"],
    "make":             ["make", "vehicle make"],
    "model":            ["model", "vehicle model"],

    # pickup (origin)
    "pickup_name":      ["pickup name", "origin name", "pu name", "pickup location", "origin location", "shipper"],
    "pickup_address":   ["pickup address", "origin address", "pu address", "pickup street", "origin street"],
    "pickup_city":      ["pickup city", "origin city", "pu city"],
    "pickup_state":     ["pickup state", "origin state", "pu state"],
    "pickup_zip":       ["pickup zip", "origin zip", "pu zip", "pickup zipcode", "origin postal", "pickup postal code"],
    "pickup_contact":   ["pickup contact", "origin contact", "pu contact", "pickup contact name"],
    "pickup_phone":     ["pickup phone", "origin phone", "pu phone", "pickup tel",
                         "origin contact phone", "pickup contact phone"],
    "pickup_date":      ["pickup date", "origin date", "pu date", "pickup", "ready date"],

    # delivery (destination)
    "delivery_name":    ["delivery name", "dest name", "destination name", "do name",
                         "delivery location", "destination location", "consignee"],
    "delivery_address": ["delivery address", "dest address", "destination address", "do address", "delivery street"],
    "delivery_city":    ["delivery city", "dest city", "destination city", "do city"],
    "delivery_state":   ["delivery state", "dest state", "destination state", "do state"],
    "delivery_zip":     ["delivery zip", "dest zip", "destination zip", "do zip", "delivery zipcode", "delivery postal code"],
    "delivery_contact": ["delivery contact", "dest contact", "destination contact", "do contact", "delivery contact name"],
    "delivery_phone":   ["delivery phone", "dest phone", "destination phone", "do phone",
                         "delivery tel", "destination contact phone", "delivery contact phone"],
    "delivery_date":    ["delivery date", "dest date", "destination date", "do date", "delivery", "due date"],

    # money / notes
    # Carrier cost comes from the "rate" column ONLY — never TotalCost.
    "price":            ["rate", "carrier rate", "carrier price"],
    "notes":            ["notes", "instructions", "comments", "remarks", "note"],
}

# Fields a row MUST have to be usable.
# For the current step (VIN -> Tesla BOL) the VIN is all we need; pickup/delivery
# become required later for SuperDispatch order creation.
REQUIRED_FIELDS = ["vin"]

# Minimum confidence (0-100) for a fuzzy header match to be accepted.
FUZZY_THRESHOLD = 86

# How rows are combined into one (possibly multi-vehicle) shipment.
#   "auto"  -> explicit group_id if present, else composite key below
#   "group" -> only the group_id column
#   "composite" -> only the composite key
GROUP_STRATEGY = os.getenv("GROUP_STRATEGY", "auto")
COMPOSITE_KEY = ["pickup_zip", "delivery_zip", "pickup_date"]
