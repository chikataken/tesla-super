"""
Super Dispatch Shipper API client for this project.

Auth is the same OAuth 2.0 client-credentials flow shipment-creator uses: POST
clientID:clientSecret (HTTP Basic) to /oauth/token, cache the bearer access_token,
re-auth once on a 401, back off on 429/5xx. This is a SEPARATE copy on purpose —
there is no shared package across the three tools, and the brief says not to import
across project folders. Credentials are still the shared ones from ../secrets/.env
(resolved in config.py).

    python sd_client.py            # auth self-test: fetch a token (creates NOTHING)
    python sd_client.py actions    # print the live webhook action list

⚠️ ENDPOINTS TO VERIFY against the live API reference before production — every
path used here is collected at the top as a constant so you can confirm/fix it in
one place. The ones marked VERIFY were inferred, not confirmed:
  * get-order details: GET /v1/public/orders/{guid}            (confirmed in sibling)
  * vehicle/inspection photos                                  (VERIFY path + shape)
  * webhook actions list / subscriptions CRUD                  (VERIFY paths + shape)
"""
from __future__ import annotations
import base64
import time
from typing import Any, Optional

import requests

import config
from logging_setup import get_logger

log = get_logger(__name__)

_TIMEOUT = 30
_token: Optional[str] = None
_token_expiry = 0.0

# --- API paths (one place to verify/fix) -----------------------------------
PATH_TOKEN = "/oauth/token"
PATH_ORDER = "/v1/public/orders/{guid}"                       # get-order details
# VERIFY: inspection/vehicle photos for an order. Common candidates seen in SD docs:
#   /v1/public/orders/{guid}/inspection_photos
#   /v1/public/orders/{guid}/vehicles/photos
PATH_ORDER_PHOTOS = "/v1/public/orders/{guid}/inspection_photos"
# VERIFY: webhook management endpoints + payload shape.
PATH_WEBHOOK_ACTIONS = "/v1/public/webhooks/actions"          # "list of all webhook actions"
PATH_WEBHOOK_SUBSCRIPTIONS = "/v1/public/webhooks/subscriptions"
PATH_WEBHOOK_SUBSCRIPTION = "/v1/public/webhooks/subscriptions/{guid}"


class SDError(RuntimeError):
    pass


def get_token(force: bool = False) -> str:
    """Return a valid bearer access_token, fetching/caching as needed."""
    global _token, _token_expiry
    if _token and not force and time.time() < _token_expiry:
        return _token
    config.require_sd_creds()
    basic = base64.b64encode(
        f"{config.SD_CLIENT_ID}:{config.SD_CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        f"{config.SD_API_BASE}{PATH_TOKEN}",
        params={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SDError(f"Auth failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    _token = data["access_token"]
    _token_expiry = time.time() + max(60, int(data.get("expires_in", 3600)) - 300)
    return _token


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}",
            "Content-Type": "application/json; charset=utf-8"}


def _retry_after(resp, attempt: int) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(60.0, max(0.0, float(ra)))
        except (TypeError, ValueError):
            pass
    return min(30.0, 1.5 * attempt)


def _request(method: str, path: str, *, json=None, params=None, attempts: int = 3) -> Any:
    """HTTP with one token-refresh on 401 and backoff on 429/5xx (same policy as the
    sibling client)."""
    url = f"{config.SD_API_BASE}{path}"
    headers = _headers()
    last = ""
    for attempt in range(1, attempts + 1):
        resp = requests.request(method, url, headers=headers, json=json, params=params,
                                timeout=_TIMEOUT)
        if resp.status_code == 401 and attempt == 1:
            get_token(force=True)
            headers = _headers()
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"{resp.status_code}: {resp.text[:200]}"
            time.sleep(_retry_after(resp, attempt))
            continue
        if not resp.ok:
            raise SDError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}
    raise SDError(f"{method} {path} failed after {attempts} tries: {last}")


# --- envelope helpers (SD wraps resources as data.object / data.objects) ----
def _object(resp: Any) -> dict:
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and "object" in data:
            return data.get("object") or {}
    return resp or {}


def _objects(resp: Any) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict):
            for key in ("objects", "results", "actions", "subscriptions"):
                if isinstance(data.get(key), list):
                    return data[key]
        for key in ("objects", "results", "actions", "subscriptions"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


# --- orders ----------------------------------------------------------------
def get_order(order_guid: str) -> dict:
    """Full order details for an order GUID (the webhook payload carries only the
    GUID, never the full shipment — so the worker calls this)."""
    return _object(_request("GET", PATH_ORDER.format(guid=order_guid)))


def find_by_vin(vin: str) -> list[dict]:
    """Orders a VIN sits on (GET /v1/public/orders/find_by_vin/{vin}). A 404/empty
    result means no order for that VIN — returns [] rather than raising."""
    try:
        resp = _request("GET", f"/v1/public/orders/find_by_vin/{vin}")
    except SDError as e:
        if " -> 404:" in str(e):
            return []
        raise
    return _objects(resp)


def get_inspection_photos(order_guid: str) -> list[dict]:
    """Inspection photo records for an order. Each record carries metadata to tag
    against (id, step, subject, taken_at, latitude, longitude) and a file URL.

    ⚠️ VERIFY the endpoint AND the field names/shape against the live reference.
    Damage photos may come back under a different key (guid/original_url/taken_at);
    normalize_photo() below isolates the field-name assumptions."""
    return _objects(_request("GET", PATH_ORDER_PHOTOS.format(guid=order_guid)))


def normalize_photo(rec: dict) -> dict:
    """Map a raw photo record to the fields we store, tolerant of the two known
    shapes (standard inspection photo vs damage photo). VERIFY against live data and
    adjust the key lists if needed — this is the one place field names are assumed."""
    def first(*keys):
        for k in keys:
            if rec.get(k) not in (None, ""):
                return rec[k]
        return None
    return {
        "photo_id": str(first("id", "guid") or ""),
        "step": first("step"),
        "subject": first("subject"),
        "taken_at": first("taken_at"),
        "latitude": first("latitude", "lat"),
        "longitude": first("longitude", "lng", "lon"),
        "url": first("file_url", "url", "original_url"),
    }


def download_bytes(url: str) -> bytes:
    """Download a photo's bytes from its (likely time-limited / signed) file URL.

    No auth header: signed S3-style URLs reject extra headers and carry their own
    credentials in the query string. If your tenant serves photos behind the API
    auth instead, switch this to _request-style auth (VERIFY)."""
    resp = requests.get(url, timeout=_TIMEOUT)
    if not resp.ok:
        raise SDError(f"photo download -> {resp.status_code} for {url[:80]}…")
    return resp.content


# --- webhook subscriptions -------------------------------------------------
def list_webhook_actions() -> list[dict]:
    """The authoritative, current list of webhook actions (don't assume it's static).
    VERIFY the path/shape."""
    return _objects(_request("GET", PATH_WEBHOOK_ACTIONS))


def list_subscriptions() -> list[dict]:
    return _objects(_request("GET", PATH_WEBHOOK_SUBSCRIPTIONS))


def subscribe(callback_url: str, actions: list[str],
              verification_token: Optional[str] = None) -> dict:
    """Register a callback URL for the given actions. VERIFY the request body shape:
    some SD tenants take one subscription per action, others a single subscription
    with an `actions` array. This sends the array form; adjust if the reference
    differs."""
    body = {"url": callback_url, "actions": list(actions)}
    if verification_token:
        body["verification_token"] = verification_token
    return _object(_request("POST", PATH_WEBHOOK_SUBSCRIPTIONS, json=body))


def unsubscribe(subscription_guid: str) -> None:
    _request("DELETE", PATH_WEBHOOK_SUBSCRIPTION.format(guid=subscription_guid))


# --- self-test -------------------------------------------------------------
def _selftest() -> None:
    print(f"SD_ENV={config.SD_ENV}  base={config.SD_API_BASE}")
    have_id = bool(config.SD_CLIENT_ID)
    have_secret = bool(config.SD_CLIENT_SECRET)
    print(f"CREDENTIALS: client_id={'SET' if have_id else 'MISSING'}  "
          f"client_secret={'SET' if have_secret else 'MISSING'}")
    if not (have_id and have_secret):
        print("STATUS: NO API KEY — set SUPERDISPATCH_CLIENT_ID/_SECRET in "
              "../secrets/.env or this folder's .env (see .env.example).")
        return
    try:
        tok = get_token()
        print(f"STATUS: AUTH OK — token starts {tok[:16]}…  (no resources touched)")
    except SDError as e:
        print(f"STATUS: AUTH FAILED (credentials present but rejected): {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "actions":
        for a in list_webhook_actions():
            print(a)
    else:
        _selftest()
