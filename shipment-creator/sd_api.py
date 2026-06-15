"""
SuperDispatch Shipper API client.

Auth is OAuth 2.0 client-credentials: POST clientID:clientSecret to /oauth/token
to get a bearer access_token (cached until it nears expiry), then send it on every
request. Which credentials/base url are used is decided by SD_ENV in config
(test vs production) — see config.py.

    python sd_api.py            # auth self-test: fetch a token, print scope/expiry
                                # (safe — creates NOTHING)
"""
from __future__ import annotations
import base64
import time
from datetime import datetime

import requests

import config


def _parse_needby(s: str | None) -> datetime | None:
    """'Jun 06, 2026 8:05PM' (timed) or 'Jun 09, 2026' (date-only) -> datetime;
    None if blank/unparseable."""
    if not s:
        return None
    s = " ".join(str(s).split())
    for fmt in ("%b %d, %Y %I:%M%p", "%b %d, %Y %I:%M %p", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

_TIMEOUT = 30
_token: str | None = None
_token_expiry = 0.0           # epoch seconds when the cached token should be refreshed


class SDError(RuntimeError):
    pass


def _require_creds() -> None:
    missing = [n for n, v in (("CLIENT_ID", config.SD_CLIENT_ID),
                              ("CLIENT_SECRET", config.SD_CLIENT_SECRET)) if not v]
    if missing:
        raise SDError(
            f"Missing SuperDispatch credentials: {missing}. Set "
            f"SUPERDISPATCH_CLIENT_ID and SUPERDISPATCH_CLIENT_SECRET in .env "
            f"(see .env.example).")


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
    # refresh a bit before the real expiry (default ~10 days) to be safe
    _token_expiry = time.time() + max(60, int(data.get("expires_in", 3600)) - 300)
    return _token


def _headers(content_type: str = "application/json; charset=utf-8") -> dict:
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": content_type}


def _retry_after_seconds(resp, attempt: int) -> float:
    """Honor a Retry-After header (seconds) on 429; else capped exponential backoff."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(60.0, max(0.0, float(ra)))
        except (TypeError, ValueError):
            pass
    return min(30.0, 1.5 * attempt)


def _request(method: str, path: str, *, json=None, content_type=None, attempts: int = 3):
    """HTTP with one token-refresh on 401 and backoff on 429/5xx.

    On 429 the Retry-After header is honored (SuperDispatch publishes no fixed
    limit, so we stay conservative). `content_type` overrides the request header —
    used for PATCH with application/merge-patch+json (RFC 7396)."""
    url = f"{config.SD_API_BASE}{path}"
    headers = _headers(content_type) if content_type else _headers()
    last = ""
    for attempt in range(1, attempts + 1):
        resp = requests.request(method, url, headers=headers, json=json, timeout=_TIMEOUT)
        if resp.status_code == 401 and attempt == 1:
            get_token(force=True)                       # re-auth once, then rebuild headers
            headers = _headers(content_type) if content_type else _headers()
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"{resp.status_code}: {resp.text[:200]}"
            time.sleep(_retry_after_seconds(resp, attempt))
            continue
        if not resp.ok:
            raise SDError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}
    raise SDError(f"{method} {path} failed after {attempts} tries: {last}")


def create_order(payload: dict, dry_run: bool = True) -> dict:
    """Create an order. dry_run=True (default) validates + returns the payload
    WITHOUT calling the API, so you can eyeball it first. Returns the created
    order (with its `guid`) when actually posted."""
    if dry_run:
        return {"dry_run": True, "would_post": payload}
    return _request("POST", "/v1/public/orders", json=payload)


_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def _state(s: str) -> str:
    s = (s or "").strip()
    return _STATE_ABBR.get(s.lower(), s)         # 2-letter; pass through if already a code


def _venue_bol(v: dict) -> dict:
    """A pdf_read BOL pickup/delivery dict -> SD venue object."""
    return {
        "name": v.get("location", ""),
        "address": v.get("street", ""),
        "city": v.get("city", ""),
        "state": _state(v.get("state", "")),
        "zip": v.get("zip", ""),
        "contact_name": v.get("contact", ""),
        "contact_phone": v.get("phone", ""),
    }


def group_bol_records(records: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group BOL records into SD orders by (shipment, delivery destination).
    Returns [(order_number, [records])]; number is the shipment id, suffixed by
    delivery zip only when that shipment splits across destinations."""
    from collections import defaultdict, OrderedDict
    buckets: "OrderedDict[tuple, list]" = OrderedDict()
    shp_dests: dict[str, set] = defaultdict(set)
    for r in records:
        shp = r.get("shipment_number", "")
        dz = (r.get("delivery") or {}).get("zip", "")
        buckets.setdefault((shp, dz), []).append(r)
        shp_dests[shp].add(dz)
    out = []
    for (shp, dz), recs in buckets.items():
        number = shp if len(shp_dests[shp]) <= 1 else f"{shp}-{dz}"
        out.append((number, recs))
    return out


def _short_number(shp: str) -> str:
    """SHP2606-A0WJ298 -> A0WJ298 (drop the SHP/date prefix)."""
    return shp.split("-", 1)[1] if "-" in shp else shp


def group_by_route(ordered: list, rec_by_vin: dict, max_size: int = 8,
                   reserved=None) -> list:
    """Group VINs that share a pickup→delivery route into truck-sized orders.

    `ordered` is [(vin, cost, shipment_number)] in spreadsheet order; `rec_by_vin`
    maps vin -> its BOL record. VINs going from the same pickup ZIP to the same
    delivery ZIP are grouped together (spreadsheet order preserved) and chunked to
    at most `max_size`. Each order is numbered from the FIRST VIN of the group
    (short form), made unique if needed. `reserved` is a set of order numbers that
    already exist on the board — new orders are numbered to avoid colliding with
    them (so newly-added VINs form their OWN orders, e.g. A0WJ298-2, instead of
    landing on a pre-existing shipment). Returns [(number, [(vin, cost, rec)])]."""
    from collections import OrderedDict
    routes: "OrderedDict[tuple, list]" = OrderedDict()
    for vin, cost, shp in ordered:
        rec = rec_by_vin.get(vin)
        if not rec:
            continue
        key = ((rec.get("pickup") or {}).get("zip", ""),
               (rec.get("delivery") or {}).get("zip", ""))
        routes.setdefault(key, []).append((vin, cost, shp, rec))

    out, taken = [], set(reserved or ())
    for items in routes.values():
        for i in range(0, len(items), max_size):
            chunk = items[i:i + max_size]
            base = _short_number(chunk[0][2] or chunk[0][0])
            number, n = base, 1
            while number in taken:                 # avoid existing + each other
                n += 1
                number = f"{base}-{n}"
            taken.add(number)
            out.append((number, chunk))
    return out


def order_payload_from_route(number: str, chunk: list, transport_type: str = "OPEN") -> dict:
    """Build an SD order from a route group: chunk = [(vin, cost, rec)]."""
    head = chunk[0][3]
    vehicles, total, have = [], 0.0, False
    soonest = None                                      # (datetime, raw string)
    for vin, cost, shp, rec in chunk:
        v = {"vin": vin}
        if rec.get("make"):
            v["make"] = rec["make"]
        if rec.get("model"):
            v["model"] = rec["model"]
        if rec.get("year"):
            try:
                v["year"] = int(rec["year"])
            except (TypeError, ValueError):
                pass
        if cost is not None:
            v["price"] = cost
            total += float(cost)
            have = True
        nb = rec.get("need_by")
        if nb:
            v["need_by"] = nb
            dt = _parse_needby(nb)
            if dt:
                v["need_by_ts"] = dt.timestamp()
                if soonest is None or dt < soonest[0]:
                    soonest = (dt, nb)
        vehicles.append(v)
    return {
        "number": number,
        "purchase_order_number": chunk[0][2],          # full SHP of the first VIN
        "transport_type": transport_type,
        "inspection_type": "standard",
        "price": round(total, 2) if have else None,
        "need_by": soonest[1] if soonest else None,     # soonest across the order's VINs
        "need_by_ts": soonest[0].timestamp() if soonest else None,
        "instructions": "",
        "pickup": {"date_type": "estimated", "venue": _venue_bol(head["pickup"])},
        "delivery": {"date_type": "estimated", "venue": _venue_bol(head["delivery"])},
        # carrier notes per stop, split out of the BOL's Shipment Comments
        "pickup_notes": head.get("pickup_notes", ""),
        "delivery_notes": head.get("delivery_notes", ""),
        "vehicles": vehicles,
    }


def order_payload_from_bol(number: str, records: list[dict],
                           vin_cost: dict, transport_type: str = "OPEN") -> dict:
    """Build an SD order from BOL records (venue/contact/vehicle) + per-car cost.
    Each vehicle is priced from the excel (its individual cost); the order price is
    the sum of its vehicles' costs."""
    head = records[0]
    vehicles, total, have_cost = [], 0.0, False
    for r in records:
        v = {"vin": r.get("vin")}
        if r.get("make"):
            v["make"] = r["make"]
        if r.get("model"):
            v["model"] = r["model"]
        if r.get("year"):
            try:
                v["year"] = int(r["year"])
            except (TypeError, ValueError):
                pass
        c = vin_cost.get(r.get("vin"))
        if c is not None:
            v["price"] = c
            total += float(c)
            have_cost = True
        vehicles.append(v)
    return {
        "number": number,
        "purchase_order_number": head.get("shipment_number", ""),   # Tesla shipment id (traceability)
        "transport_type": transport_type,
        "inspection_type": "standard",
        "price": round(total, 2) if have_cost else None,
        "instructions": "",
        "pickup": {"date_type": "estimated", "venue": _venue_bol(head["pickup"])},
        "delivery": {"date_type": "estimated", "venue": _venue_bol(head["delivery"])},
        "pickup_notes": head.get("pickup_notes", ""),
        "delivery_notes": head.get("delivery_notes", ""),
        "vehicles": vehicles,
    }


def _venue(v: dict) -> dict:
    """ShipmentDraft pickup/delivery dict -> SD venue object."""
    return {
        "name": v.get("name", ""),
        "address": v.get("address", ""),
        "city": v.get("city", ""),
        "state": v.get("state", ""),
        "zip": v.get("zip", ""),
        "contact_name": v.get("contact", ""),
        "contact_phone": v.get("phone", ""),
    }


def order_payload(ship, number: str | None = None, transport_type: str = "OPEN") -> dict:
    """Build the SuperDispatch create-order payload from a ShipmentDraft.
    make/model/year are omitted when unknown — SD auto-decodes them from the VIN."""
    vehicles = []
    for veh in ship.vehicles:
        v = {"vin": veh.vin}
        if getattr(veh, "make", None):
            v["make"] = veh.make
        if getattr(veh, "model", None):
            v["model"] = veh.model
        if getattr(veh, "year", None):
            try:
                v["year"] = int(veh.year)
            except (TypeError, ValueError):
                pass
        vehicles.append(v)
    return {
        "number": number or ship.number or ship.group_key,   # required; must be unique
        "transport_type": transport_type,
        "inspection_type": "standard",
        "price": ship.price,
        "instructions": ship.notes or "",
        "pickup": {"date_type": "estimated", "venue": _venue(ship.pickup)},
        "delivery": {"date_type": "estimated", "venue": _venue(ship.delivery)},
        "vehicles": vehicles,
    }


def _unwrap_object(resp: dict) -> dict:
    """SuperDispatch wraps single resources as {"data": {"object": {...}}}.
    Return the inner object (or the response itself if it isn't wrapped).
    NOTE: verify this envelope against the live reference; it's isolated here so
    adjusting it is a one-line change."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and "object" in data:
            return data.get("object") or {}
    return resp or {}


def _unwrap_objects(resp: dict) -> list:
    """SuperDispatch wraps collections as {"data": {"objects": [...]}}.
    Return the inner list (handles a few shapes defensively)."""
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
    """Get full order details (GET /v1/public/orders/{guid}) — unwrapped object."""
    return _unwrap_object(_request("GET", f"/v1/public/orders/{guid}"))


def find_by_vin(vin: str) -> list:
    """Find existing orders a VIN sits on (GET /v1/public/orders/find_by_vin/{vin}).

    Returns a LIST of short order records (a VIN can be on more than one order).
    A 404 / empty result means the VIN has no existing shipment — that's a normal,
    expected case, so it returns [] rather than raising."""
    try:
        resp = _request("GET", f"/v1/public/orders/find_by_vin/{vin}")
    except SDError as e:
        if " -> 404:" in str(e):                       # VIN not found is not an error
            return []
        raise
    return _unwrap_objects(resp)


def patch_order(guid: str, merge_patch: dict) -> dict:
    """Partial-update an order (PATCH /v1/public/orders/{guid}) using JSON Merge
    Patch (RFC 7396) — send ONLY the fields that change.

    CRITICAL: the `vehicles` array is all-or-nothing. If `merge_patch` includes
    `vehicles`, it REPLACES the whole list — build it with build_vehicles_merge()
    so existing vehicles keep their GUIDs and omitted ones aren't dropped."""
    return _unwrap_object(_request(
        "PATCH", f"/v1/public/orders/{guid}",
        json=merge_patch, content_type="application/merge-patch+json"))


def build_vehicles_merge(existing_order: dict, new_vehicles: list) -> dict:
    """Build the {"vehicles": [...]} body to ADD vehicles to an existing order
    without losing any.

    Because writing `vehicles` replaces the entire list, the result is the FULL
    intended list: every current vehicle (carried over WITH its `guid`) plus each
    new VIN as an object WITHOUT a guid. VINs already on the order are not added
    again. Pure function — no HTTP, so it's directly unit-testable."""
    current = existing_order.get("vehicles") or []
    out, have = [], set()
    for v in current:
        keep = {k: val for k, val in v.items() if val is not None}
        if v.get("guid"):
            keep["guid"] = v["guid"]                    # MUST include to retain it
        out.append(keep)
        if v.get("vin"):
            have.add(v["vin"])
    for nv in new_vehicles:
        vin = nv.get("vin")
        if not vin or vin in have:                      # skip blanks + already-present
            continue
        out.append({k: val for k, val in nv.items()
                    if val is not None and k != "guid"})  # new -> no guid
        have.add(vin)
    return {"vehicles": out}


def get_bol_url(guid: str) -> str | None:
    """The order's BOL PDF url, if available."""
    data = _request("GET", f"/v1/public/orders/{guid}/bol")
    return (((data or {}).get("data") or {}).get("object") or {}).get("url")


def _selftest() -> None:
    print(f"SD_ENV={config.SD_ENV}  base={config.SD_API_BASE}")
    print(f"client_id={config.SD_CLIENT_ID[:6]}…  (secret {'set' if config.SD_CLIENT_SECRET else 'MISSING'})")
    try:
        tok = get_token()
        print(f"AUTH OK — token starts {tok[:18]}…  (cached until "
              f"~{int((_token_expiry - time.time())/3600)}h)")
        print("Credentials work. No order was created.")
    except SDError as e:
        print(f"AUTH FAILED: {e}")


if __name__ == "__main__":
    _selftest()
