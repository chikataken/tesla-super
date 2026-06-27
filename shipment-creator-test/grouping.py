"""
Combine parsed rows (one VIN each) into ShipmentDrafts (one pickup→delivery move
carrying 1+ vehicles).

Strategy (config.GROUP_STRATEGY):
  - "group"      group only by the explicit group_id column
  - "composite"  group only by COMPOSITE_KEY (pickup_zip + delivery_zip + pickup_date)
  - "auto"       use group_id where present, else fall back to the composite key
Venue/price/notes are taken from the first row of each group (assumed consistent);
mismatches within a group are surfaced as warnings rather than silently merged.
"""
from __future__ import annotations

import re

import config
from models import RawRow, Vehicle, ShipmentDraft

_PICKUP = ["pickup_name", "pickup_address", "pickup_city", "pickup_state",
           "pickup_zip", "pickup_contact", "pickup_phone", "pickup_date"]
_DELIVERY = ["delivery_name", "delivery_address", "delivery_city", "delivery_state",
             "delivery_zip", "delivery_contact", "delivery_phone", "delivery_date"]


# Common USPS street-type + directional abbreviations, each mapped to ONE
# canonical token so "Ave"/"Avenue"/"Ave." all collapse to the same thing. This
# makes the address match tolerant of how the SAME physical site is abbreviated,
# so a spelling difference doesn't split one site into two orders.
_ADDR_SYNONYMS = {
    "avenue": "ave", "ave": "ave", "av": "ave",
    "street": "st", "st": "st", "str": "st",
    "boulevard": "blvd", "blvd": "blvd", "blv": "blvd",
    "drive": "dr", "dr": "dr",
    "road": "rd", "rd": "rd",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "parkway": "pkwy", "pkwy": "pkwy", "pky": "pkwy",
    "highway": "hwy", "hwy": "hwy",
    "freeway": "fwy", "fwy": "fwy",
    "circle": "cir", "cir": "cir",
    "terrace": "ter", "ter": "ter", "terr": "ter",
    "trail": "trl", "trl": "trl",
    "square": "sq", "sq": "sq",
    "expressway": "expy", "expy": "expy",
    "suite": "ste", "ste": "ste", "unit": "ste",
    "building": "bldg", "bldg": "bldg",
    "north": "n", "n": "n", "south": "s", "s": "s",
    "east": "e", "e": "e", "west": "w", "w": "w",
    "northeast": "ne", "ne": "ne", "northwest": "nw", "nw": "nw",
    "southeast": "se", "se": "se", "southwest": "sw", "sw": "sw",
}


def _norm_addr(s: str) -> str:
    """Canonicalize a street address: lowercase, '#'->'ste', drop dots/commas,
    collapse whitespace, and map street-type/directional abbreviations to one
    form. So '6010 Richmond Ave', '6010 Richmond Avenue', and '6010 Richmond
    Ave.' all normalize to the same string (a suite NUMBER like 'ste 200' is
    preserved, so genuinely different units still stay separate)."""
    s = (s or "").lower().replace("#", " ste ")
    s = re.sub(r"[.,]", " ", s)
    return " ".join(_ADDR_SYNONYMS.get(t, t) for t in s.split() if t)


def _norm_plain(s: str) -> str:
    """Lowercase + collapse whitespace (for name/city/state)."""
    return " ".join((s or "").lower().split())


def _norm_zip(s: str) -> str:
    """First 5 digits, so '77384' and '77384-1234' match."""
    m = re.search(r"\d{5}", s or "")
    return m.group(0) if m else _norm_plain(s)


def _addr_sig(row: RawRow, prefix: str) -> str:
    """Normalized signature of ONE leg's SPECIFIC address: site name + street +
    city + state + zip. Two units match only when ALL of these agree (after
    abbreviation/format normalization), so two different centers that merely
    share a city or ZIP are never treated as the same place — e.g. 6010 Richmond
    Ave is distinct from 9420 College Park Dr even in the same ZIP — while the
    same site spelled two ways still matches."""
    return "/".join([
        _norm_plain(row.get(f"{prefix}_name")),
        _norm_addr(row.get(f"{prefix}_address")),
        _norm_plain(row.get(f"{prefix}_city")),
        _norm_plain(row.get(f"{prefix}_state")),
        _norm_zip(row.get(f"{prefix}_zip")),
    ])


def _group_key(row: RawRow) -> str:
    """One SD order per (shipment, SPECIFIC from-address, SPECIFIC to-address).
    SD orders are a single pickup->delivery leg, so VINs are merged only when
    they share the exact same pickup address AND the exact same delivery address
    — the full from/to pair, not just the city or ZIP. A shipment that touches
    more than one distinct address pair is split into one order per pair. With no
    shipment id, the address pair alone is the key so unrelated rows never merge."""
    gid = (row.get("group_id") or "").strip()
    route = _addr_sig(row, "pickup") + "->" + _addr_sig(row, "delivery")
    if gid:
        return f"{gid}|{route}"
    return route or f"__row{row.row_number}"


def _venue(row: RawRow, keys) -> dict:
    return {k.split("_", 1)[1]: row.get(k, "") for k in keys}


def build_shipments(rows: list[RawRow]) -> tuple[list[ShipmentDraft], list[str]]:
    """Return (shipments, warnings). Only well-formed rows are grouped."""
    from collections import defaultdict
    warnings: list[str] = []
    buckets: dict[str, list[RawRow]] = {}
    gid_all: dict[str, list[RawRow]] = defaultdict(list)   # all rows per shipment id
    for r in rows:
        if not r.ok:
            continue
        buckets.setdefault(_group_key(r), []).append(r)
        gid = (r.get("group_id") or "").strip()
        if gid:
            gid_all[gid].append(r)

    gid_keys: dict[str, set] = defaultdict(set)            # delivery-groups per shipment
    for key, group in buckets.items():
        gid = (group[0].get("group_id") or "").strip()
        if gid:
            gid_keys[gid].add(key)

    def _price(bucket: list[RawRow], whole: list[RawRow]):
        """Order price for this bucket. `whole` is all rows of the shipment.
        If TotalCost is constant across the shipment it's the per-SHIPMENT total
        (repeated per VIN) -> allocate it proportionally by vehicle share. If it
        varies per VIN it's per-vehicle -> sum just this bucket's vehicles."""
        costs = [r.get("price") for r in whole if r.get("price") is not None]
        if not costs:
            return None
        if len(set(costs)) == 1:                           # per-shipment total
            return round(costs[0] * len(bucket) / len(whole), 2)
        bcosts = [r.get("price") for r in bucket if r.get("price") is not None]
        return round(sum(bcosts), 2) if bcosts else None

    shipments: list[ShipmentDraft] = []
    for key, group in buckets.items():
        head = group[0]
        gid = (head.get("group_id") or "").strip()
        # clean order number: the shipment id, suffixed only when that shipment
        # is split across multiple distinct (pickup,delivery) address pairs. The
        # suffix is a STABLE 1-based index (sorted key order), NOT the ZIP — two
        # centers can share a ZIP, so a ZIP suffix could collide; an index can't.
        if gid and len(gid_keys[gid]) <= 1:
            number = gid
        elif gid:
            number = f"{gid}-{sorted(gid_keys[gid]).index(key) + 1}"
        else:
            number = key
        whole = gid_all[gid] if gid else group
        ship = ShipmentDraft(
            group_key=key,
            number=number,
            vehicles=[Vehicle(vin=g.vin, year=g.get("year"), make=g.get("make"),
                              model=g.get("model")) for g in group],
            pickup=_venue(head, _PICKUP),
            delivery=_venue(head, _DELIVERY),
            price=_price(group, whole),
            notes=head.get("notes", ""),
            source_rows=[g.row_number for g in group],
        )
        shipments.append(ship)
    return shipments, warnings
