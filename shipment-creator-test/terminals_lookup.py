"""
The posting-side contract over the terminals cache (terminals_db).

resolve(location) takes the Tesla BOL venue NAME (the `location` field, e.g.
'NA-US-IN-Indianapolis') and, on an EXACT normalized-name match against a scraped
SuperDispatch terminal, returns that terminal as a ready-to-post SD venue object
plus its carrier notes. The caller then REPLACES the whole BOL venue (address,
city, state, zip, contact, phone) and notes with the terminal's data — the
agreed policy: exact match -> trust SD entirely; no match -> keep 100% of the
existing Tesla-BOL logic (resolve returns None, never worse than today).

FUTURE: a fuzzy / Claude-assisted layer that links "similar enough" BOL names to
SD names slots in right here — it would widen the candidate set before falling
back to None. terminals_db keeps `name_norm` + every raw field so that layer can
run over the cache without re-scraping. Until then, matching is exact-only.
"""
from __future__ import annotations
from typing import Optional

import terminals_db


def _to_bol_venue(row: dict) -> dict:
    """A terminals row -> the Tesla-BOL venue shape (location/street/contact/...), so a
    synthesized record looks exactly like one parsed from a real BOL."""
    addr = row.get("address", "") or ""
    city = row.get("city", "") or ""
    st = row.get("state", "") or ""
    zc = row.get("zip", "") or ""
    return {
        "location": row.get("name", "") or "",
        "contact": row.get("contact_name", "") or "",
        "phone": row.get("contact_phone", "") or "",
        "street": addr, "city": city, "state": st, "zip": zc,
        "address": ", ".join(p for p in (addr, city, f"{st} {zc}".strip()) if p),
    }


def build_synthetic_record(vin: str, pickup_name: str, delivery_name: str) -> Optional[dict]:
    """If BOTH terminals are already known by name, return a BOL-EQUIVALENT record built
    from the cache + VIN decode — so the caller can SKIP the Tesla portal for this VIN.
    None if either terminal is unknown/ambiguous (caller must fetch the BOL). The record
    matches pdf_read.extract_records' shape, so it flows through grouping/posting as-is."""
    try:
        pt, _ = terminals_db.resolve_row(pickup_name or "")    # follows address-links to
        dt, _ = terminals_db.resolve_row(delivery_name or "")  # the original terminal
    except Exception:
        return None
    if not (pt and dt):
        return None
    p_venue, d_venue = _to_bol_venue(pt), _to_bol_venue(dt)
    # Keep the matched (Excel/BOL) name as the stop's `location` — even when it's an alias
    # of an original — so the posting overlay re-resolves it as 'linked' and applies the
    # original-then-BOL contact/notes fallback. (Using the canonical name here would make
    # the overlay see a plain 'db' terminal and drop the fallback.)
    if (pickup_name or "").strip():
        p_venue["location"] = pickup_name.strip()
    if (delivery_name or "").strip():
        d_venue["location"] = delivery_name.strip()
    rec = {
        "vin": (vin or "").upper().replace(" ", ""),
        "pickup": p_venue,
        "delivery": d_venue,
        "pickup_notes": pt.get("carrier_notes", "") or "",
        "delivery_notes": dt.get("carrier_notes", "") or "",
        "from_cache": True,                # provenance: built from the terminal cache, no BOL
    }
    try:
        import pdf_read                    # lazy: VIN-decoded make/model/year, no portal
        rec.update(pdf_read._vehicle(rec["vin"]))
    except Exception:
        pass
    return rec


def build_record_from_excel(vin: str, fields: dict) -> dict:
    """Build a BOL-EQUIVALENT record straight from the Excel row — no Tesla portal. Each
    venue carries src_hint='excel' so the posting overlay, when a terminal name doesn't
    match the DB, keeps the Excel's own address/contact/phone and badges it 'Excel'. When
    the name DOES match, the overlay still upgrades the stop to the richer DB terminal.
    Used for every non-ALL dispatcher (they never touch Tesla — DB or Excel only)."""
    def _venue(prefix: str) -> dict:
        g = lambda k: (fields.get(f"{prefix}_{k}") or "").strip()
        addr, city, st, zc = g("address"), g("city"), g("state"), g("zip")
        return {
            "location": g("name"), "contact": g("contact"), "phone": g("phone"),
            "street": addr, "city": city, "state": st, "zip": zc,
            "address": ", ".join(p for p in (addr, city, f"{st} {zc}".strip()) if p),
            "src_hint": "excel",
        }
    rec = {
        "vin": (vin or "").upper().replace(" ", ""),
        "pickup": _venue("pickup"), "delivery": _venue("delivery"),
        "pickup_notes": (fields.get("notes") or "").strip(), "delivery_notes": "",
        "from_excel": True,
    }
    try:
        import pdf_read
        rec.update(pdf_read._vehicle(rec["vin"]))
    except Exception:
        pass
    return rec


def learn_from_bol_record(rec: dict, pickup_name: str = "", delivery_name: str = "") -> int:
    """After a BOL is parsed, save any not-yet-known terminal so it's skippable next time.
    Keyed on the EXCEL terminal name (the future match key) when provided, else the BOL's
    own name; venue + per-stop carrier note come from the BOL. Already-known terminals are
    a no-op. Returns how many were newly learned (0-2)."""
    learned = 0
    for side, xname in (("pickup", pickup_name), ("delivery", delivery_name)):
        v = rec.get(side) or {}
        name = (xname or "").strip() or (v.get("location") or "").strip()
        if not name:
            continue
        try:
            if terminals_db.learn_terminal(
                    name, address=v.get("street", ""), city=v.get("city", ""),
                    state=v.get("state", ""), zip=v.get("zip", ""),
                    contact_name=v.get("contact", ""), contact_phone=v.get("phone", ""),
                    carrier_notes=rec.get(f"{side}_notes", "") or "",
                    raw={"learned_from": "tesla_bol",
                         "bol_location": v.get("location", ""), "excel_name": xname}):
                learned += 1
        except Exception:
            pass
    return learned


def _to_venue(row: dict) -> dict:
    """A terminals row -> the SD venue object shape posting uses (see sd_api._venue)."""
    return {
        "name": row.get("name", "") or "",
        "address": row.get("address", "") or "",
        "city": row.get("city", "") or "",
        "state": row.get("state", "") or "",
        "zip": row.get("zip", "") or "",
        "contact_name": row.get("contact_name", "") or "",
        "contact_phone": row.get("contact_phone", "") or "",
    }


def resolve(location: str) -> Optional[dict]:
    """Exact-name terminal match for a BOL venue name. Returns
    {'venue': <SD venue dict>, 'carrier_notes': <str>, 'sd_id': <str>} or None.
    None means "no confident terminal" -> caller keeps the BOL venue untouched.
    Pure (no side effects) — use resolve_or_record on the posting path."""
    if not (location or "").strip():
        return None
    try:
        row, kind = terminals_db.resolve_row(location)   # follows address-links
    except Exception:
        return None                       # cache missing/locked -> fall back to BOL
    if not row:
        return None
    return {
        "venue": _to_venue(row),
        "carrier_notes": row.get("carrier_notes", "") or "",
        "sd_id": row.get("sd_id", "") or "",
        # badge token: 'db' original | 'linked' aliased-to-original | 'added' learned-new
        "term_source": kind,
    }


def resolve_or_record(location: str) -> Optional[dict]:
    """Posting-path variant: on a clean exact match return the terminal (as resolve);
    otherwise RECORD the unmatched name (deduped, with reason) and return None so the
    caller keeps the Tesla-BOL data. 'not_found' = no SD terminal; 'ambiguous' = the
    name is shared by several terminals (we won't guess which address to post)."""
    name = (location or "").strip()
    if not name:
        return None
    hit = resolve(name)
    if hit:
        return hit
    try:
        _, kind = terminals_db.resolve_row(name)
        if kind == "ambiguous_site":
            # hub-level code in a multi-terminal zip — refused on purpose (see
            # terminals_db.resolve_row); posting keeps the shipment's own venue.
            terminals_db.record_miss(name, "ambiguous_site")
        else:
            n = len(terminals_db.rows_by_name(name))
            terminals_db.record_miss(name, "ambiguous" if n > 1 else "not_found")
    except Exception:
        pass                              # never let bookkeeping break posting
    return None
