"""
Transit-time ruleset + shipment date windows (used when posting to SuperDispatch).

The "ruleset" is a per-state geography: each state (+ DC) has an approximate
centroid, and the transit time for a route is derived from the great-circle
distance between the origin and destination state centroids (bumped ~1.2x for road
miles) divided by a typical car-hauler's daily progress, plus a dispatch buffer. So
a same-state move is the floor (MIN_TRANSIT_DAYS) and a cross-country haul is a week+
— "longer trip = more days" falls straight out of distance. Tune MILES_PER_DAY /
PICKUP_BUFFER_DAYS / MIN_TRANSIT_DAYS in one place.

Date windows:
  - delivery: a 2-day window ending ON the need-by date  ->  [need_by-1, need_by]
  - pickup:   opens TODAY (the day it's posted) and runs to TOMORROW -> [today,
              today+1], a 2-day range (may coincide with the delivery dates).
              Collapses to a single day [today, today] if tomorrow is past need-by.
"""
from __future__ import annotations
import datetime
import math

# --- tunables -------------------------------------------------------------
MILES_PER_DAY = 480.0       # typical car-hauler progress per day
ROAD_FACTOR = 1.2           # great-circle -> approx road miles
PICKUP_BUFFER_DAYS = 1      # dispatch/loading buffer on top of pure drive time
MIN_TRANSIT_DAYS = 2        # floor for DIFFERENT-state moves
UNKNOWN_TRANSIT_DAYS = 5    # fallback when a state can't be resolved
PICKUP_WINDOW_DAYS = 4      # pickup is up to a 4-day range (liberal; clamps near-term)
DELIVERY_WINDOW_DAYS = 2    # delivery is a 2-day range ending on the need-by date

# --- state geography (approx centroids: lat, lon) -------------------------
STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.0, -152.0), "AZ": (34.2, -111.7), "AR": (34.9, -92.4),
    "CA": (37.2, -119.5), "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5),
    "DC": (38.9, -77.0), "FL": (28.6, -82.4), "GA": (32.6, -83.4), "HI": (20.3, -156.4),
    "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.0, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.0, -92.0), "ME": (45.4, -69.2),
    "MD": (39.0, -76.8), "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3),
    "MS": (32.7, -89.7), "MO": (38.4, -92.5), "MT": (47.0, -109.6), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.1, -74.7), "NM": (34.4, -106.1),
    "NY": (42.9, -75.6), "NC": (35.5, -79.4), "ND": (47.4, -100.5), "OH": (40.3, -82.8),
    "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.6),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3),
    "UT": (39.3, -111.7), "VT": (44.1, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.5),
    "WV": (38.6, -80.6), "WI": (44.6, -90.0), "WY": (43.0, -107.6),
}

_FULL_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _abbr(state) -> str | None:
    """Normalize a state name (full or 2-letter, any case) to a 2-letter code."""
    s = (state or "").strip()
    if not s:
        return None
    up = s.upper()
    if up in STATE_CENTROIDS:
        return up
    return _FULL_TO_ABBR.get(s.lower())


def _haversine(a, b) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    r = 3958.8                                   # earth radius (miles)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def transit_days(origin_state, dest_state) -> int:
    """Estimated days for a car hauler to run origin_state -> dest_state. Same state
    is the floor; cross-country is a week or more."""
    a, b = _abbr(origin_state), _abbr(dest_state)
    if a is None or b is None or a not in STATE_CENTROIDS or b not in STATE_CENTROIDS:
        return UNKNOWN_TRANSIT_DAYS
    if a == b:
        return 0            # same state: a hauler can pick up and deliver the same day
    miles = _haversine(STATE_CENTROIDS[a], STATE_CENTROIDS[b]) * ROAD_FACTOR
    drive = miles / MILES_PER_DAY
    return max(MIN_TRANSIT_DAYS, math.ceil(drive) + PICKUP_BUFFER_DAYS)


def _to_date(need_by) -> datetime.date | None:
    """need_by may be epoch seconds, a datetime, a date, or None."""
    if need_by is None:
        return None
    if isinstance(need_by, datetime.datetime):
        return need_by.date()
    if isinstance(need_by, datetime.date):
        return need_by
    try:
        return datetime.datetime.fromtimestamp(float(need_by)).date()
    except (TypeError, ValueError, OSError):
        return None


def shipment_windows(need_by, origin_state, dest_state, today=None) -> dict | None:
    """Pickup + delivery date windows for a shipment, derived from its need-by date
    and the route's transit time. Returns ISO date strings, or None if need_by is
    unusable. `today` is injectable for testing."""
    nb = _to_date(need_by)
    if nb is None:
        return None
    today = today or datetime.date.today()
    days = transit_days(origin_state, dest_state)
    day = datetime.timedelta(days=1)

    delivery_latest = nb                                   # latest delivery = the need-by date
    delivery_earliest = nb - (DELIVERY_WINDOW_DAYS - 1) * day

    # Pickup window: opens TODAY (the day it's posted) and runs to the NEXT day — a
    # 2-day range. It may coincide with the delivery dates (no ordering rule). If
    # tomorrow would fall past the need-by date, collapse to a single day (today).
    pickup_earliest = today
    pickup_latest = today + day
    if pickup_latest > nb:                                 # tomorrow is past need-by
        pickup_latest = today

    return {
        "transit_days": days,
        "pickup": {"earliest": pickup_earliest.isoformat(), "latest": pickup_latest.isoformat()},
        "delivery": {"earliest": delivery_earliest.isoformat(), "latest": delivery_latest.isoformat()},
    }
