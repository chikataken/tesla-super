"""
Consolidation matcher: given a list of VINs, discover the existing SuperDispatch
orders they sit on, and flag the ones already running a route we're about to post —
so new VINs can be added to an existing order instead of creating a duplicate.

Why it works this way: the Shipper API has NO list-all and NO route/geographic
search — order discovery is ONLY by a known identifier (here, VIN, via
sd_api.find_by_vin). So every candidate is reached through a VIN the caller
supplies, and route matching is done HERE, in our own code, by comparing the
addresses on the fetched orders.

Pipeline per run:
    VIN --find_by_vin--> [short orders] --get_order(guid)--> full order (addresses,
    is_posted_to_loadboard). Orders are cached by GUID for the run (a GUID shared by
    several VINs is fetched once). The loop is throttled and isolates per-VIN errors.

`routes_match` is the single, easily-adjustable matching strategy.
"""
from __future__ import annotations
import re
import time
from dataclasses import dataclass, field

import sd_api

# Conservative default throttle between API calls. SuperDispatch publishes no fixed
# rate limit, so this is intentionally gentle and configurable (do NOT copy numbers
# from other shipping APIs). 429s are handled with Retry-After inside sd_api.
DEFAULT_THROTTLE_S = 0.4

_WS = re.compile(r"\s+")


def normalize(s) -> str:
    """lowercase, trim, collapse internal whitespace runs to a single space."""
    return _WS.sub(" ", str(s or "").strip().lower())


def _venue_of(leg) -> dict:
    """An order leg ({'venue': {...}}) or a bare venue dict -> the venue dict."""
    if not isinstance(leg, dict):
        return {}
    v = leg.get("venue")
    return v if isinstance(v, dict) else leg


def route_endpoint(leg, *, use_street: bool = False) -> tuple:
    """Normalized comparable key for one end of a route (pickup or delivery).
    Default components: (city, state, zip). With use_street, prepend the street
    line for a tighter match."""
    v = _venue_of(leg)
    parts = [normalize(v.get("city")), normalize(v.get("state")), normalize(v.get("zip"))]
    if use_street:
        parts.insert(0, normalize(v.get("address") or v.get("street")))
    return tuple(parts)


def routes_match(a_pickup, a_delivery, b_pickup, b_delivery, *, use_street: bool = False) -> bool:
    """THE matching rule. A route is the ordered pair (origin, destination); a match
    requires origin<->origin AND destination<->destination to agree (never origin
    alone). Flip `use_street` (or edit route_endpoint) to tighten/loosen."""
    return (route_endpoint(a_pickup, use_street=use_street) == route_endpoint(b_pickup, use_street=use_street)
            and route_endpoint(a_delivery, use_street=use_street) == route_endpoint(b_delivery, use_street=use_street))


def _order_status(o: dict) -> str:
    return (o.get("status") or o.get("state") or "").strip()


# Once a carrier is involved (or the run is finished), the order is theirs — appending
# VINs would silently change a dispatch they already agreed to. Only new/posted orders
# are safely ours to modify.
NON_EDITABLE_STATUSES = ("accepted", "pending", "picked_up", "delivered",
                         "invoiced", "paid", "canceled")


def order_editable(o: dict) -> bool:
    """True when the order may still be modified on SD: NEITHER its lifecycle status nor
    the loadboard tab it was found on indicates a carrier holds it. Checked on both
    fields because SD clears loadboard_status to null once a carrier accepts — the
    lifecycle `status` is then the only live signal (and vice versa for tab scrapes)."""
    life = (o.get("status") or "").strip().lower()
    lb = (o.get("loadboard_status") or "").strip().lower()
    return life not in NON_EDITABLE_STATUSES and lb not in NON_EDITABLE_STATUSES


def normalize_order(o: dict) -> dict:
    """Full SD order -> the compact shape the UI needs."""
    vehicles = []
    for v in (o.get("vehicles") or []):
        vehicles.append({"vin": v.get("vin"), "guid": v.get("guid"),
                         "make": v.get("make"), "model": v.get("model"),
                         "year": v.get("year"), "price": v.get("price")})
    return {
        "guid": o.get("guid"),
        "number": o.get("number") or o.get("order_number"),
        "pickup": o.get("pickup"),
        "delivery": o.get("delivery"),
        "vehicles": vehicles,
        "price": o.get("price"),
        "status": _order_status(o),
        "is_posted_to_loadboard": bool(o.get("is_posted_to_loadboard")),
        "posted_to_loadboard_at": o.get("posted_to_loadboard_at"),
        # which loadboard tab the scrape found it on ("posted"/"accepted"); the
        # scan stamps this — the API status field alone doesn't distinguish them.
        "loadboard_status": o.get("loadboard_status"),
        # When the car was actually delivered (delivery.completed_at; falls back to the
        # order's last status change). Used to dedup recently-delivered VINs.
        "delivered_at": ((o.get("delivery") or {}).get("completed_at") or o.get("changed_at")),
    }


@dataclass
class ConsolidationResult:
    orders: list = field(default_factory=list)        # normalized, deduped by GUID
    checked_vins: int = 0
    found_vins: list = field(default_factory=list)    # VINs that hit >=1 order
    not_found_vins: list = field(default_factory=list)
    errors: list = field(default_factory=list)        # [{vin, error}] transient/per-VIN
    auth_error: str | None = None                      # set on 401/403 -> run stopped


def _is_auth_error(e: Exception) -> bool:
    s = str(e)
    return " -> 401:" in s or " -> 403:" in s or "credential" in s.lower()


def find_orders_for_vins(vins, *, throttle_s: float = DEFAULT_THROTTLE_S,
                         batch_size: int = 0, batch_pause_s: float = 0.0,
                         sd=sd_api) -> ConsolidationResult:
    """Discover every existing order the given VINs sit on.

    - Per-run cache keyed by GUID: a shared order is fetched once.
    - Pacing: `throttle_s` sleeps between EVERY VIN (legacy). `batch_size`/`batch_pause_s`
      instead sleep `batch_pause_s` once every `batch_size` VINs — far faster for big lists
      (SD served ~8 calls/s with no throttle in testing) while staying polite; the HTTP
      layer also self-throttles on 429. Auth errors (401/403) stop the run; a VIN with no
      order is normal; any other per-VIN error is recorded and the run continues.
    `sd` is injectable so the HTTP layer can be mocked in tests.
    """
    res = ConsolidationResult()
    by_guid: dict[str, dict] = {}                      # cache: guid -> normalized order
    seen = set()
    first = True
    for raw in vins:
        vin = (raw or "").strip().upper()
        if not vin or vin in seen:
            continue
        seen.add(vin)
        res.checked_vins += 1
        if not first and throttle_s:
            time.sleep(throttle_s)
        # Self-imposed batch limit: pause every `batch_size` VINs (e.g. 2s every 80).
        if (not first and batch_size and batch_pause_s
                and (res.checked_vins - 1) % batch_size == 0):
            time.sleep(batch_pause_s)
        first = False
        try:
            shorts = sd.find_by_vin(vin)
        except Exception as e:                          # noqa: BLE001 - isolate per VIN
            if _is_auth_error(e):
                res.auth_error = str(e)
                break
            res.errors.append({"vin": vin, "error": str(e)})
            continue
        if not shorts:
            res.not_found_vins.append(vin)
            continue
        res.found_vins.append(vin)
        for short in shorts:
            guid = short.get("guid") or short.get("order_guid")
            if not guid:
                continue
            if guid in by_guid:
                continue                                # already fetched this run
            try:
                full = sd.get_order(guid)
            except Exception as e:                      # noqa: BLE001
                if _is_auth_error(e):
                    res.auth_error = str(e)
                    res.orders = list(by_guid.values())
                    return res
                res.errors.append({"vin": vin, "guid": guid, "error": str(e)})
                continue
            by_guid[guid] = normalize_order(full)
    res.orders = list(by_guid.values())
    return res


def match_against_routes(orders, board_orders, *, use_street: bool = True,
                         my_vins=None) -> list:
    """Tag each discovered order with the board route(s) it matches and whether it's
    a usable consolidation candidate (route match AND posted to the loadboard).

    `board_orders` is the staged board (list of orders with pickup/delivery). Each
    returned item carries `matches_board_routes` (the board route keys it lines up
    with) and `already_on` (which of `my_vins` are already vehicles on it, so the UI
    doesn't suggest re-adding a VIN that's already there).
    Returns ALL discovered orders annotated; `is_candidate` marks the actionable ones.
    """
    my = {(v or "").strip().upper() for v in (my_vins or []) if v}
    out = []
    for o in orders:
        matched_keys = []
        for b in board_orders:
            if routes_match(o.get("pickup"), o.get("delivery"),
                            b.get("pickup"), b.get("delivery"), use_street=use_street):
                key = board_route_key(b)
                if key not in matched_keys:
                    matched_keys.append(key)
        # "live" = found on a scanned tab (Posted / Accepted / Pending) or flagged
        # posted-to-loadboard by the API. But only EDITABLE orders are candidates:
        # accepted/pending/picked-up loads belong to a carrier and are never modified
        # (see order_editable) — they used to be offered, which is how VINs got
        # appended onto already-accepted loads.
        status = (o.get("loadboard_status") or "").strip().lower()
        live = bool(o.get("is_posted_to_loadboard")) or status in ("posted", "accepted", "pending")
        on_order = {v.get("vin") for v in o.get("vehicles") or [] if v.get("vin")}
        annotated = dict(o)
        annotated["matches_board_routes"] = matched_keys
        annotated["already_on"] = sorted(my & on_order)
        annotated["editable"] = order_editable(o)
        annotated["is_candidate"] = bool(matched_keys) and live and annotated["editable"]
        out.append(annotated)
    return out


def board_route_key(order: dict) -> str:
    """The board's existing route key, byte-for-byte equal to the frontend's
    routeKey() (raw `pickup.zip|delivery.zip`), so the UI can line a discovered
    order up to the focused route."""
    pv = _venue_of(order.get("pickup"))
    dv = _venue_of(order.get("delivery"))
    return f"{pv.get('zip') or ''}|{dv.get('zip') or ''}"
