"""
Settings for direct-pickup-checks, read from the environment.

Credential resolution mirrors the sibling tools (shipment-creator/config.py):
shared creds live in <repo-root>/secrets/.env so all tools read one file; an
app-local .env in THIS folder can override; real environment variables win over
both. So your existing SUPERDISPATCH_CLIENT_ID / _SECRET in secrets/.env are
picked up automatically — this project only adds the webhook-specific settings.

    python config.py        # print the resolved config (secrets masked)
"""
from __future__ import annotations
import os

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

# Lowest priority first; load_dotenv never overrides an already-set var, so the
# call order here makes app-local .env win over the shared secrets file, and real
# environment variables (set before the process starts) win over everything.
load_dotenv(os.path.join(_HERE, ".env"))                       # app-local override
load_dotenv(os.path.join(_REPO_ROOT, "secrets", ".env"))       # shared authoritative


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# --- Super Dispatch API credentials (same names shipment-creator uses) ------
SD_ENV = os.getenv("SD_ENV", "test").strip().lower()
_PROD = SD_ENV in {"prod", "production", "live"}
SD_API_BASE = (os.getenv("SD_API_BASE")
               or "https://api.shipper.superdispatch.com").rstrip("/")
SD_CLIENT_ID = (os.getenv("SUPERDISPATCH_CLIENT_ID") or "").strip()
SD_CLIENT_SECRET = (os.getenv("SUPERDISPATCH_CLIENT_SECRET") or "").strip()

# --- Webhook verification ---------------------------------------------------
# Compared (constant-time) against the `verification_token` in each payload.
SD_WEBHOOK_VERIFICATION_TOKEN = (os.getenv("SD_WEBHOOK_VERIFICATION_TOKEN") or "").strip()
# Super Dispatch issues a SEPARATE verification_token per action/subscription, so the
# listener must accept ANY of them. SD_WEBHOOK_VERIFICATION_TOKENS is a comma-separated
# set (written by subscribe.py); the legacy single token is folded in for compatibility.
SD_WEBHOOK_VERIFICATION_TOKENS = {
    t.strip() for t in (os.getenv("SD_WEBHOOK_VERIFICATION_TOKENS") or "").split(",") if t.strip()
}
if SD_WEBHOOK_VERIFICATION_TOKEN:
    SD_WEBHOOK_VERIFICATION_TOKENS.add(SD_WEBHOOK_VERIFICATION_TOKEN)

# --- Listener ---------------------------------------------------------------
LISTENER_HOST = os.getenv("LISTENER_HOST", "127.0.0.1").strip()
LISTENER_PORT = int(os.getenv("LISTENER_PORT", "8077"))
WEBHOOK_PATH = "/" + os.getenv("WEBHOOK_PATH", "/webhooks/superdispatch").strip().lstrip("/")

# --- Cloudflare Tunnel ------------------------------------------------------
import urllib.parse as _urlparse
TUNNEL_PUBLIC_URL = (os.getenv("TUNNEL_PUBLIC_URL") or "").rstrip("/")
# Named-tunnel provisioning settings (used by run.sh).
TUNNEL_NAME = os.getenv("TUNNEL_NAME", "direct-pickup").strip()
# Hostname for the DNS route + ingress; defaults to the host part of the public URL.
TUNNEL_HOSTNAME = (os.getenv("TUNNEL_HOSTNAME")
                   or _urlparse.urlparse(TUNNEL_PUBLIC_URL).netloc).strip()

# ---------------------------------------------------------------------------
# Playwright / browser automation (the "for now" path)
#
# Downloading the pickup inspection photos and applying tags are done by driving
# the Super Dispatch WEB app, because the public API can't write tags. This reuses
# the sibling tools' approach. The persistent Chrome profile is SHARED with
# tesla-reconcile / shipment-creator (CDP_PROFILE_DIR), so one manual login covers
# all of them. See browser.py (copied from tesla-reconcile/auth.py).
# ---------------------------------------------------------------------------
SD_WEB_BASE = os.getenv("SD_WEB_BASE", "https://shipper.superdispatch.com").rstrip("/")

# "cdp" = attach to the REAL installed Chrome (Windows default — dodges bot
# detection); "launch" = Playwright's own persistent context (default elsewhere).
AUTH_MODE = os.getenv("AUTH_MODE", "cdp" if os.name == "nt" else "launch").strip().lower()
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip().rstrip("/")
# Shared with the sibling tools so ONE manual login covers all three. The default is
# an ABSOLUTE path computed from this file's location (…/tesla-super/tesla-reconcile/
# .auth). It must NOT be a CWD-relative literal like "C:\tesla-profile": on Linux that
# backslash path is relative, so it resolved to a DIFFERENT directory under each tool's
# working dir (…/direct-pickup-checks/C:\tesla-profile vs …/tesla-reconcile/…) — which
# silently split the login into separate profiles (the worker drove a blank session).
_SHARED_PROFILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tesla-reconcile", ".auth")
CDP_PROFILE_DIR = os.getenv("CDP_PROFILE_DIR", _SHARED_PROFILE_DEFAULT)
CHROME_PATH = os.getenv("CHROME_PATH", "").strip()
# "visible" or "ghost" (real headed Chrome parked off-screen — not detected like true
# headless). On a headless Linux SERVER you typically want AUTH_MODE=launch + HEADLESS
# OR a CDP Chrome under xvfb; see README.
WINDOW_MODE = os.getenv("WINDOW_MODE", "visible").strip().lower()
HEADLESS = _bool("HEADLESS", "false")
# Persistent profile dir for AUTH_MODE=launch (its own, separate from the CDP one).
USER_DATA_DIR = os.getenv("USER_DATA_DIR", os.path.join(_HERE, ".auth"))
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "").strip()

# --- Tag labels applied to the order ----------------------------------------
# The workflow: every shipment VIN found in the pickup photos -> TAG_VIN, else
# TAG_NO_VIN; TAG_BOT is always added to mark it was auto-processed. SD caps an
# order at 3 tags; we set exactly 2.
TAG_VIN = os.getenv("TAG_VIN", "VIN").strip()
TAG_NO_VIN = os.getenv("TAG_NO_VIN", "NO VIN").strip()
TAG_BOT = os.getenv("TAG_BOT", "CLAUDE").strip()

# Only orders whose number/name contains one of these markers (case-insensitive) are
# eligible for tagging — e.g. "A346511-direct" qualifies, "A443251" does not. Others
# are recorded but skipped for tagging. Blank -> no filter (tag everything).
TAG_NAME_MARKERS = tuple(
    m.strip().lower() for m in os.getenv("TAG_NAME_MARKERS", "trade,direct").split(",")
    if m.strip()
)


def callback_url() -> str:
    """The public callback URL registered with Super Dispatch (tunnel + path)."""
    if not TUNNEL_PUBLIC_URL:
        raise RuntimeError("TUNNEL_PUBLIC_URL is not set — needed to build the "
                           "webhook callback URL (see .env.example / README).")
    return f"{TUNNEL_PUBLIC_URL}{WEBHOOK_PATH}"


# --- Storage ----------------------------------------------------------------
DATA_DIR = (os.getenv("DPC_DATA_DIR") or os.path.join(_HERE, "data")).rstrip("/")
DB_PATH = os.path.join(DATA_DIR, "direct_pickup.db")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
# cleanup.py prunes photos + bookkeeping rows older than this (keeps shipments/vins/tags).
DPC_RETENTION_DAYS = int(os.getenv("DPC_RETENTION_DAYS", "30"))

# --- Worker -----------------------------------------------------------------
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "2.0"))
WORKER_MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "5"))

# --- Logging ----------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").strip().lower()

# ---------------------------------------------------------------------------
# Webhook actions we subscribe to.
#
# Two TRIGGERS at two DIFFERENT times (this is the easy thing to get wrong):
#   * status events  -> record the pickup + push to the UI. Photos are NOT ready yet.
#   * the BOL event  -> photos/BOL URLs now exist -> download + tag.
# See worker.py for the routing.
#
# ⚠️ VERIFY against the live "get list of all webhook actions" endpoint before
# production — the authoritative set is not assumed static. subscribe.py can print
# the current list (`python subscribe.py actions`).
PICKUP_MANUAL_ACTION = "order.manually_marked_as_picked_up"  # marked on the Shipper side
PICKUP_STATUS_ACTIONS = (
    "order.picked_up",                      # carrier/driver marked it picked up
    PICKUP_MANUAL_ACTION,                   # marked picked up on the Shipper side
)
PICKUP_BOL_ACTION = "order.picked_up_bol"   # pickup BOL/photo URLs now available
# Optional: sent when a carrier later picks up an order already manually marked.
PICKUP_IGNORED_ACTION = "order.picked_up.ignored"

# Actions that need the Super Dispatch WEB session (Playwright). When the auth gate
# trips (a login/captcha a human must clear), the worker PAUSES these and leaves the
# items queued; API-only status events (order.picked_up) keep flowing. Manual pickups
# also drive the browser (handle_bol_event), so they count as web work too.
WEB_ACTIONS = frozenset({PICKUP_BOL_ACTION, PICKUP_MANUAL_ACTION})

# --- Auth gate / circuit breaker --------------------------------------------
# While the gate is tripped (logged out + a human-needed captcha/2FA, or a transient
# vault error), the worker re-probes at most this often: it checks whether you've
# logged back in (or, for a transient error, retries auto-login) before resuming.
AUTH_PROBE_SECONDS = float(os.getenv("AUTH_PROBE_SECONDS", "180"))

# The full set subscribe.py registers.
SUBSCRIBE_ACTIONS = (*PICKUP_STATUS_ACTIONS, PICKUP_BOL_ACTION)


def require_sd_creds() -> None:
    missing = [n for n, v in (("SUPERDISPATCH_CLIENT_ID", SD_CLIENT_ID),
                              ("SUPERDISPATCH_CLIENT_SECRET", SD_CLIENT_SECRET)) if not v]
    if missing:
        raise RuntimeError(
            f"Missing Super Dispatch credentials: {missing}. Set them in "
            f"../secrets/.env (shared) or this folder's .env (see .env.example).")


def _mask(s: str) -> str:
    return f"{s[:4]}…({len(s)} chars)" if s else "MISSING"


if __name__ == "__main__":
    print(f"SD_ENV               = {SD_ENV}  ({'production' if _PROD else 'sandbox'})")
    print(f"SD_API_BASE          = {SD_API_BASE}")
    print(f"SUPERDISPATCH_CLIENT = id={_mask(SD_CLIENT_ID)}  secret={_mask(SD_CLIENT_SECRET)}")
    print(f"VERIFICATION_TOKEN   = {_mask(SD_WEBHOOK_VERIFICATION_TOKEN)}")
    print(f"LISTENER             = {LISTENER_HOST}:{LISTENER_PORT}{WEBHOOK_PATH}")
    print(f"TUNNEL_PUBLIC_URL    = {TUNNEL_PUBLIC_URL or 'MISSING'}")
    print(f"CALLBACK URL         = "
          f"{callback_url() if TUNNEL_PUBLIC_URL else '(set TUNNEL_PUBLIC_URL)'}")
    print(f"DATA_DIR             = {DATA_DIR}")
    print(f"DB_PATH              = {DB_PATH}")
    print(f"SUBSCRIBE_ACTIONS    = {', '.join(SUBSCRIBE_ACTIONS)}")
