"""
ZIP-to-ZIP distance / driving-time check — no Claude, no API key.

Layered so a single missing library or a down service never breaks a run:

  geocode(zip) -> (lat, lon)
     1. pgeocode        (offline US ZIP dataset; `pip install pgeocode`)
     2. Zippopotam.us   (free, no key) as a network fallback

  drive_minutes(a, b)
     1. OSRM public routing server (free, no key) -> real driving time
     2. straight-line (haversine) miles -> estimated minutes, if OSRM is down

`check()` returns a ZipCheckResult; on total failure it returns
indeterminate (too_far=False) and a note, so an order is never mis-flagged.
"""
from __future__ import annotations
import json
import math
import os
import urllib.request

import config
from models import ZipCheckResult

_HTTP_TIMEOUT = float(os.getenv("ZIP_HTTP_TIMEOUT", "6"))
_USE_OSRM = os.getenv("ZIP_USE_OSRM", "true").strip().lower() in {"1", "true", "yes", "y"}
# Straight-line -> driving-time estimate (only used if OSRM is unavailable):
# minutes ≈ miles * detour / avg_mph * 60.
_AVG_MPH = float(os.getenv("ZIP_AVG_MPH", "35"))
_DETOUR = float(os.getenv("ZIP_DETOUR", "1.3"))

_nomi = None              # cached pgeocode.Nominatim (lazy)
_geo_cache: dict[str, tuple[float, float] | None] = {}


def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "tesla-reconcile/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode())


def _geocode_pgeocode(zip5: str):
    global _nomi
    try:
        import pgeocode
        if _nomi is None:
            _nomi = pgeocode.Nominatim("us")
        rec = _nomi.query_postal_code(zip5)
        lat, lon = float(rec.latitude), float(rec.longitude)
        if math.isnan(lat) or math.isnan(lon):
            return None
        return (lat, lon)
    except Exception:
        return None


def _geocode_zippopotam(zip5: str):
    try:
        d = _http_json(f"https://api.zippopotam.us/us/{zip5}")
        p = (d.get("places") or [None])[0]
        if not p:
            return None
        return (float(p["latitude"]), float(p["longitude"]))
    except Exception:
        return None


def geocode(zip5: str):
    """(lat, lon) for a US ZIP, or None. Tries offline pgeocode, then Zippopotam."""
    zip5 = (zip5 or "").strip()[:5]
    if not zip5.isdigit() or len(zip5) != 5:
        return None
    if zip5 in _geo_cache:
        return _geo_cache[zip5]
    coord = _geocode_pgeocode(zip5) or _geocode_zippopotam(zip5)
    _geo_cache[zip5] = coord
    return coord


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _osrm_minutes(a: tuple[float, float], b: tuple[float, float]):
    """Real driving time in minutes via OSRM, or None if unavailable."""
    try:
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{a[1]},{a[0]};{b[1]},{b[0]}?overview=false")
        d = _http_json(url)
        route = (d.get("routes") or [None])[0]
        if not route:
            return None
        return route["duration"] / 60.0
    except Exception:
        return None


def check(scheduled_zip: str, delivered_zip: str,
          threshold_min: int | None = None) -> ZipCheckResult:
    """Compare two ZIPs. too_far=True when they're >= threshold driving minutes
    apart. Only meant to be called when the ZIPs already differ."""
    threshold = config.ZIP_DRIVE_MINUTES if threshold_min is None else threshold_min
    ca, cb = geocode(scheduled_zip), geocode(delivered_zip)
    if not ca or not cb:
        which = "scheduled" if not ca else "delivered"
        return ZipCheckResult(too_far=False,
                              reasoning=f"could not geocode {which} ZIP -> no flag")

    miles = haversine_miles(ca, cb)
    mins = _osrm_minutes(ca, cb) if _USE_OSRM else None
    if mins is None:
        mins = miles * _DETOUR / _AVG_MPH * 60.0
        src = "estimate from straight-line"
    else:
        src = "OSRM driving"
    too_far = mins >= threshold
    return ZipCheckResult(
        too_far=too_far,
        drive_minutes=int(round(mins)),
        same_metro=(miles < 12),
        reasoning=f"{miles:.1f} mi straight-line, ~{mins:.0f} min ({src}); "
                  f"threshold {threshold} min",
        raw=json.dumps({"miles": round(miles, 1), "minutes": round(mins, 1),
                        "source": src, "threshold": threshold}),
    )
