"""
SuperDispatch Shipper API — read-only client for tesla-reconcile.

Just enough of the official OAuth API to pull a delivered order's inspection
photos (replacing the old online-BOL web-scrape in superdispatch.get_bol_photos):

  * find_by_vin(vin)  -> GET /v1/public/orders/find_by_vin/{vin}  (orders a VIN sits on)
  * get_order(guid)   -> GET /v1/public/orders/{guid}             (vehicles[].photos[])

Auth is OAuth 2.0 client-credentials: POST clientID:clientSecret to /oauth/token for
a bearer token (cached until it nears expiry), sent on every request. Credentials are
the shared SUPERDISPATCH_CLIENT_ID/SECRET (config reads them from secrets/.env); the
base defaults to the PRODUCTION host so we read real delivery photos. This module
NEVER writes — no create/patch — so it carries no test-mode write guard.

    python sd_api.py                  # auth self-test (fetches a token, creates nothing)
    python sd_api.py <VIN>            # show the Delivery photo count for a VIN
"""
from __future__ import annotations
import base64
import time

import requests

import config

_TIMEOUT = 30
_token: str | None = None
_token_expiry = 0.0           # epoch seconds when the cached token should be refreshed


class SDError(RuntimeError):
    pass


def _require_creds() -> None:
    missing = [n for n, v in (("SUPERDISPATCH_CLIENT_ID", config.SD_CLIENT_ID),
                              ("SUPERDISPATCH_CLIENT_SECRET", config.SD_CLIENT_SECRET))
               if not v]
    if missing:
        raise SDError(
            f"Missing SuperDispatch API credentials: {missing}. Set them in the shared "
            f"secrets/.env (SUPERDISPATCH_CLIENT_ID / SUPERDISPATCH_CLIENT_SECRET).")


def get_token(force: bool = False) -> str:
    """Return a valid bearer access_token, fetching/caching as needed."""
    global _token, _token_expiry
    if _token and not force and time.time() < _token_expiry:
        return _token
    _require_creds()
    basic = base64.b64encode(
        f"{config.SD_CLIENT_ID}:{config.SD_CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        f"{config.SD_API_BASE}/oauth/token",
        params={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SDError(f"Auth failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    _token = data["access_token"]
    # Refresh a little before the real expiry (default ~10 days) to be safe.
    _token_expiry = time.time() + max(60, int(data.get("expires_in", 3600)) - 300)
    return _token


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}",
            "Content-Type": "application/json; charset=utf-8"}


def _retry_after_seconds(resp, attempt: int) -> float:
    """Honor a Retry-After header (seconds) on 429; else capped exponential backoff."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(60.0, max(0.0, float(ra)))
        except (TypeError, ValueError):
            pass
    return min(30.0, 1.5 * attempt)


def _get(path: str, attempts: int = 3):
    """GET with one token-refresh on 401 and backoff on 429/5xx."""
    url = f"{config.SD_API_BASE}{path}"
    headers = _headers()
    last = ""
    for attempt in range(1, attempts + 1):
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 401 and attempt == 1:
            get_token(force=True)                       # re-auth once, then rebuild headers
            headers = _headers()
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"{resp.status_code}: {resp.text[:200]}"
            time.sleep(_retry_after_seconds(resp, attempt))
            continue
        if not resp.ok:
            raise SDError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}
    raise SDError(f"GET {path} failed after {attempts} tries: {last}")


def _unwrap_object(resp: dict) -> dict:
    """SuperDispatch wraps single resources as {"data": {"object": {...}}}."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and "object" in data:
            return data.get("object") or {}
    return resp or {}


def _unwrap_objects(resp) -> list:
    """SuperDispatch wraps collections as {"data": {"objects": [...]}}."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict):
            for key in ("objects", "results", "orders"):
                if isinstance(data.get(key), list):
                    return data[key]
        for key in ("objects", "results", "orders"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def get_order(guid: str) -> dict:
    """Full order details (GET /v1/public/orders/{guid}) — unwrapped object.
    `vehicles[].photos[]` each carries photo_url, photo_type ('Delivery'|'Pickup'),
    latitude/longitude, created_at, guid."""
    return _unwrap_object(_get(f"/v1/public/orders/{guid}"))


def find_by_vin(vin: str) -> list:
    """Orders a VIN sits on (GET /v1/public/orders/find_by_vin/{vin}).

    Returns a LIST of short order records (a VIN can be on more than one order). A
    404 / empty result means no existing order — a normal case — so it returns []."""
    try:
        resp = _get(f"/v1/public/orders/find_by_vin/{vin}")
    except SDError as e:
        if " -> 404:" in str(e):                       # VIN not found is not an error
            return []
        raise
    return _unwrap_objects(resp)


def _selftest(vin: str | None) -> None:
    print(f"SD_API_BASE={config.SD_API_BASE}")
    print(f"client_id={config.SD_CLIENT_ID[:6]}…  "
          f"(secret {'set' if config.SD_CLIENT_SECRET else 'MISSING'})")
    try:
        tok = get_token()
        print(f"AUTH OK — token starts {tok[:18]}…")
    except SDError as e:
        print(f"AUTH FAILED: {e}")
        return
    if vin:
        orders = find_by_vin(vin)
        guid = (orders[0] or {}).get("guid") if orders else None
        print(f"find_by_vin({vin}) -> {len(orders)} order(s); first guid={guid}")
        if guid:
            order = get_order(guid)
            for veh in order.get("vehicles") or []:
                d = sum(1 for p in (veh.get("photos") or [])
                        if (p.get("photo_type") or "").lower() == "delivery")
                print(f"  {veh.get('vin')}: {d} Delivery photo(s)")


if __name__ == "__main__":
    import sys
    _selftest(sys.argv[1] if len(sys.argv) > 1 else None)
