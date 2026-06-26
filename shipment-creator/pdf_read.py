"""
Read a Tesla "Bill of Lading" PDF into per-VIN records with everything needed to
post a shipment on SuperDispatch.

The BOL is a two-column layout (Origin | Destination | Carrier) repeated once per
vehicle/page. pdfplumber merges columns onto each line, so we split by word
x-positions: each value column sits between its label and the next label. Bold
labels render with doubled characters ("DDeessttiinnaattiioonn"); values are clean.

    python pdf_read.py output/bols/SHP2606-A035212.pdf
"""
from __future__ import annotations
import re
import sys

import pdfplumber

_STATES = {
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
    "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
    "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
    "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
    "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
    "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
    "District of Columbia",
}
# A US phone with OR without separators: optional +1, 3-digit area (optionally
# parenthesized), then 3 + 4 digits, each group optionally split by space/dot/dash.
# Catches "6026205933", "(602)620-5933", "602-620-5933", "602.620.5933", "602 620 5933".
_PHONE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
_ZIP = re.compile(r"\b\d{5}\b")
# Tesla VIN -> model and model-year. The 4th character is the model line (the same
# across WMIs — Fremont 5YJ, Austin 7SA/7G2), so decode on it once we know it's a Tesla.
_TESLA_LINE = {"3": "Model 3", "Y": "Model Y", "S": "Model S", "X": "Model X",
               "A": "Cybercab", "C": "Cybertruck"}
_VIN_YEAR = {"L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024, "S": 2025,
             "T": 2026, "V": 2027, "W": 2028}


def _dd(s: str) -> str:
    """De-double a bold label, e.g. 'DDeessttiinnaattiioonn::' -> 'Destination:'."""
    return s[::2]


def _label(words, name: str):
    return next((w for w in words if _dd(w["text"]).startswith(name)), None)


def _value_after(words, dd_label: str):
    for i, w in enumerate(words):
        if _dd(w["text"]).startswith(dd_label):
            return words[i + 1]["text"] if i + 1 < len(words) else None
    return None


def _column_rows(words, left, right, top0, bottom) -> list[str]:
    block = [w for w in words if left <= w["x0"] < right and top0 <= w["top"] < bottom]
    lines = {}
    for w in sorted(block, key=lambda w: (round(w["top"]), w["x0"])):
        lines.setdefault(round(w["top"]), []).append(w["text"])
    return [" ".join(v) for _, v in sorted(lines.items())]


def _parse_csz(s: str):
    toks = s.split()
    zc = toks[-1] if toks and toks[-1][:5].isdigit() else ""
    rest = toks[:-1] if zc else toks
    state, city_toks = "", rest
    for n in (2, 1):
        cand = " ".join(rest[-n:]) if len(rest) >= n else ""
        if cand in _STATES:
            state, city_toks = cand, rest[:-n]
            break
    return " ".join(city_toks), state, zc[:5]


def _venue(rows: list[str]) -> dict:
    location = rows[0] if rows else ""
    rest = rows[1:]
    csz_i = next((i for i in range(len(rest) - 1, -1, -1) if _ZIP.search(rest[i])), None)
    csz = rest[csz_i] if csz_i is not None else ""
    street = rest[csz_i - 1] if (csz_i and csz_i - 1 >= 0) else ""
    phone = next((r for r in rest if _PHONE.search(r)), "")
    contact = next((r for r in rest if r not in (csz, street, phone)), "")
    city, state, zc = _parse_csz(csz)
    return {"location": location, "contact": contact, "phone": phone,
            "street": street, "city": city, "state": state, "zip": zc,
            "address": ", ".join(p for p in (street, city, f"{state} {zc}".strip()) if p)}


# Internal email-routing instructions that must never leak into carrier notes, e.g.
#   "send BOL to KEthridge@tesla.com,Didi@tfitrans.com,dispatch@tfitrans.com"
# Matches "send BOL ... to" (a short gap, no '@'), then the contiguous run of
# comma/space-separated email addresses that follows. Anything outside this segment
# (real pickup/delivery instructions) is left intact.
_EMAIL = r"[\w.+-]+@[\w.-]+\.\w+"
_BOL_EMAILS = re.compile(
    rf"send\s*BOL\b[^@]{{0,60}}?{_EMAIL}(?:[\s,]+{_EMAIL})*",
    re.IGNORECASE,
)


def _scrub_bol_emails(s: str) -> str:
    """Strip every 'send BOL to <emails>' segment, then tidy the separators it
    leaves behind, so the surrounding note text stays readable."""
    s = _BOL_EMAILS.sub("", s or "")
    s = re.sub(r"\s*,(?:\s*,)+", ", ", s)      # collapse ",," / ", ," left by removal
    s = re.sub(r"\s{2,}", " ", s)              # collapse double spaces
    return s


def _clean_note(s: str) -> str:
    """Trim a parsed note: stop at the next BOLD label, which renders with doubled
    characters (e.g. 'SShhiippmmeenntt IIdd'), strip internal 'send BOL to <emails>'
    routing lines, and drop surrounding commas/space."""
    s = (s or "").strip()
    m = re.search(r"(?:(\w)\1){4,}", s)        # a run of doubled chars == a bold label
    if m:
        s = s[:m.start()]
    s = _scrub_bol_emails(s)
    return s.strip(" ,;\t")


def split_comments(text: str, origin_name: str, dest_name: str) -> dict:
    """Pull pickup vs delivery notes out of the BOL 'Shipment Comments' paragraph.
    Each block is prefixed by its venue name, e.g.
        'NA-US-IN-Indianapolis: <pickup notes>, NA-US-MO-St. Louis: <delivery notes>'
    Either block may be absent (sometimes only one venue has notes). The venue names
    appear earlier in the BOL too, but only the comments occurrences are immediately
    followed by ':', so matching 'name:' lands in the right place. Pure (testable).
    Returns {'pickup': str, 'delivery': str}."""
    t = " ".join((text or "").split())
    on, dn = (origin_name or "").strip(), (dest_name or "").strip()
    oi = t.find(on + ":") if on else -1
    di = t.find(dn + ":") if dn else -1

    def seg(idx, name, end):
        return _clean_note(t[idx + len(name) + 1: end])

    pickup = delivery = ""
    if oi != -1 and di != -1:
        if oi <= di:
            pickup, delivery = seg(oi, on, di), seg(di, dn, len(t))
        else:
            delivery, pickup = seg(di, dn, oi), seg(oi, on, len(t))
    elif oi != -1:
        pickup = seg(oi, on, len(t))
    elif di != -1:
        delivery = seg(di, dn, len(t))
    return {"pickup": pickup, "delivery": delivery}


def _vehicle(vin: str) -> dict:
    vin = (vin or "").upper()
    make = "Tesla" if (vin[:2] in ("5Y", "7S") or vin[:3] == "7G2") else ""
    # Model line is the 4th char (3/Y/S/X/A=Cybercab/C=Cybertruck), decoded only once
    # we've confirmed a Tesla WMI so non-Tesla VINs never get a bogus model.
    model = _TESLA_LINE.get(vin[3], "") if (make and len(vin) >= 4) else ""
    return {"make": make,
            "model": model,
            "year": _VIN_YEAR.get(vin[9]) if len(vin) >= 10 else None}


def extract_records(path: str) -> list[dict]:
    recs = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            o, d, c, s = (_label(words, x) for x in ("Origin", "Destination", "Carrier", "Shipment"))
            if not (o and d):
                continue
            top0, bottom = o["top"] - 2, (s["top"] if s else o["top"] + 70)
            pickup = _venue(_column_rows(words, o["x1"] + 2, d["x0"] - 5, top0, bottom))
            delivery = _venue(_column_rows(words, d["x1"] + 2,
                                           (c["x0"] - 5 if c else d["x1"] + 220), top0, bottom))
            text = page.extract_text() or ""
            vin = _value_after(words, "VIN") or ""
            bol_date = _dd(_value_after(words, "LADING") or "")
            year_m = re.search(r"Year of (\d{4})", text)
            notes = split_comments(text, pickup["location"], delivery["location"])
            recs.append({
                "vin": vin,
                **_vehicle(vin),
                "shipment_number": _value_after(words, "Number") or "",
                "shipment_id": _value_after(words, "Id") or "",
                "leg_id": _value_after(words, "ID") or "",
                "odometer": _value_after(words, "Odometer") or "",
                "bol_date": bol_date,
                "carrier": " ".join(_column_rows(words, c["x1"] + 2, page.width,
                                                 o["top"] - 2, o["top"] + 12)) if c else "",
                "pickup": pickup,
                "delivery": delivery,
                "pickup_notes": notes["pickup"],        # carrier notes from Shipment Comments
                "delivery_notes": notes["delivery"],
            })
    return recs


def record_for_vin(path: str, vin: str) -> dict | None:
    v = (vin or "").upper().replace(" ", "")
    return next((r for r in extract_records(path) if (r.get("vin") or "").upper() == v), None)


def format_record(r: dict) -> str:
    veh = f"{r.get('year') or '?'} {r.get('make','')} {r.get('model','')}".strip()
    p, d = r["pickup"], r["delivery"]
    return (
        f"  VIN {r['vin']}   {veh}\n"
        f"  Shipment {r['shipment_number']}  (Id {r['shipment_id']}, leg {r['leg_id']})"
        f"   Odometer {r['odometer']}   BOL {r['bol_date']}   Carrier {r['carrier']}\n"
        f"  PICKUP   : {p['location']}\n"
        f"             {p['address']}\n"
        f"             Contact: {p['contact']}   {p['phone']}\n"
        f"  DELIVERY : {d['location']}\n"
        f"             {d['address']}\n"
        f"             Contact: {d['contact']}   {d['phone']}"
    )


def dump(path: str) -> None:
    recs = extract_records(path)
    print(f"{len(recs)} vehicle(s) on this BOL:\n")
    for r in recs:
        print(format_record(r), "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python pdf_read.py <bol.pdf>")
        sys.exit(1)
    dump(sys.argv[1])
