"""
Local mirror of SuperDispatch notification emails (broker.updates@superdispatch.com).

Every SD lifecycle/notification email didi@ receives becomes one row: order
state changes (accepted / picked up / delivered — with the exact timestamps SD
prints), the carrier request stream (bids WITH prices + $/mile — the only
market-pricing source we have), and operational exceptions (declines and
cancellations with their reasons). VINs go to a side table for joins against
tenders.db / bids.db.

STORAGE: parsed fields + a GZIPPED raw body. SD emails are ~50-100KB of
SendGrid tracking-link bloat that compresses ~10:1; parsed fields carry the
value, the gzip keeps re-parseability without the 18GB/yr raw would cost.

Same SQLite/WAL/idempotent-upsert conventions as tenders_db.py. Recording is
append-once keyed on the Gmail message id.
"""
from __future__ import annotations
import gzip
import html as H
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

import paths

DB_PATH = paths.data_path("sd_events.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sd_events (
    gmail_id    TEXT PRIMARY KEY,
    sent_at     REAL,               -- Gmail internalDate (unix seconds)
    event_type  TEXT,               -- accepted|picked_up|delivered|new_request|req_update|
                                    -- req_cancel|offer_cancel|declined|revised|other
    order_id    TEXT,               -- SD order number as shown (may carry ' DIRECT', '-1a' etc)
    order_base  TEXT,               -- first token, upper — joins tenders.db shp suffix
    carrier     TEXT,
    price       REAL,               -- the bid/request price when present (NEVER on lifecycle mails)
    price_per_mile REAL,
    event_at    TEXT,               -- ISO of the body's own 'Delivered on'/'Picked Up on' stamp
    reason      TEXT,               -- decline reason / carrier's free-text cancellation note
    origin      TEXT,               -- 'City, ST zip' as printed
    destination TEXT,
    pickup_date TEXT,
    delivery_date TEXT,
    contact     TEXT,               -- request emails: dispatcher name + phone
    subject     TEXT,
    raw_gz      BLOB,               -- gzipped body html
    ingested_at REAL
);
CREATE INDEX IF NOT EXISTS ix_sdev_base ON sd_events(order_base);
CREATE INDEX IF NOT EXISTS ix_sdev_type ON sd_events(event_type);
CREATE INDEX IF NOT EXISTS ix_sdev_sent ON sd_events(sent_at);

CREATE TABLE IF NOT EXISTS sd_event_vins (
    gmail_id TEXT NOT NULL REFERENCES sd_events(gmail_id) ON DELETE CASCADE,
    vin      TEXT NOT NULL,
    PRIMARY KEY (gmail_id, vin)
);
CREATE INDEX IF NOT EXISTS ix_sdev_vin ON sd_event_vins(vin);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(_SCHEMA)
    return con


def have(con: sqlite3.Connection, gmail_id: str) -> bool:
    return con.execute("SELECT 1 FROM sd_events WHERE gmail_id=?",
                       (gmail_id,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# (type, subject regex, which group is order-id, which group is carrier)
_SUBJECTS = [
    ("delivered",    r"^Order (.+?) has been Delivered$",                 1, None),
    ("picked_up",    r"^Order (.+?) has been Picked Up$",                 1, None),
    ("accepted",     r"^(?:Order|Offer) (.+?) has been Accepted$",        1, None),
    ("revised",      r"^Order (.+?) has been Revised by (.+)$",           1, 2),
    ("new_request",  r"^New request from (.+)$",                          None, 1),
    ("req_update",   r"^(.+?) updated the request$",                      None, 1),
    ("req_cancel",   r"^(.+?) canceled the request$",                     None, 1),
    ("offer_cancel", r"^Offer for (.+?) is Canceled by Carrier$",         None, 1),
    ("declined",     r"^Load Offer for (.+?) was (?:Declined|Canceled) by Carrier", 1, None),
]
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_CSZ_RE = re.compile(r"^(.+?, [A-Z]{2} \d{5}(?:-\d{4})?)$")
_DATE_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")
_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)(?:\s*·\s*\$([\d.]+)/mil)?")
_STAMP_RE = re.compile(r"(?:Delivered|Picked Up) on ([A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)")


def _text_lines(body_html: str) -> list[str]:
    t = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", body_html, flags=re.S | re.I)
    t = re.sub(r"<br[^>]*>|</tr>|</p>|</div>|</td>|</li>|</h\d>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = H.unescape(t)
    lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines()]
    return [l for l in lines if l and "http" not in l.lower()]


def _after_label(lines: list[str], label: str) -> Optional[str]:
    for i, l in enumerate(lines):
        if l == label and i + 1 < len(lines):
            return lines[i + 1]
    return None


def parse_event(subject: str, body_html: str) -> dict[str, Any]:
    """Best-effort structured parse. Never raises — unknown shapes land as
    event_type='other' with whatever fields matched."""
    out: dict[str, Any] = {"event_type": "other", "order_id": None, "carrier": None,
                           "price": None, "price_per_mile": None, "event_at": None,
                           "reason": None, "origin": None, "destination": None,
                           "pickup_date": None, "delivery_date": None,
                           "contact": None, "vins": []}
    subject = re.sub(r"\s+", " ", subject or "").strip()
    for typ, pat, oid_g, car_g in _SUBJECTS:
        m = re.match(pat, subject)
        if m:
            out["event_type"] = typ
            if oid_g:
                out["order_id"] = m.group(oid_g).strip()
            if car_g:
                out["carrier"] = m.group(car_g).strip()
            break

    lines = _text_lines(body_html or "")
    joined = "\n".join(lines)

    if not out["order_id"]:
        out["order_id"] = _after_label(lines, "Order ID")
    if not out["carrier"]:
        out["carrier"] = _after_label(lines, "Carrier")

    m = _STAMP_RE.search(joined)
    if m:
        try:
            out["event_at"] = datetime.strptime(m.group(1), "%b %d, %Y at %I:%M %p").isoformat()
        except ValueError:
            out["event_at"] = m.group(1)

    # price: labelled 'Price' / 'Bid' (request stream only)
    for label in ("Price", "Bid"):
        v = _after_label(lines, label)
        if v:
            pm = _PRICE_RE.search(v)
            if pm:
                out["price"] = float(pm.group(1).replace(",", ""))
                if pm.group(2):
                    out["price_per_mile"] = float(pm.group(2))
                break

    # decline / cancellation reasons
    r = _after_label(lines, "Reason(s)")
    if r:
        out["reason"] = r
    elif out["event_type"] == "offer_cancel":
        # free-text note sits between the 'has been Canceled' line and 'Order ID'
        for i, l in enumerate(lines):
            if "has been Canceled" in l and i + 1 < len(lines) and lines[i + 1] != "Order ID":
                out["reason"] = lines[i + 1]
                break

    # route: first two 'City, ST 12345' lines; a date line right after each is the stop date
    stops = []
    for i, l in enumerate(lines):
        m = _CSZ_RE.match(l)
        if m:
            date = lines[i + 1] if i + 1 < len(lines) and _DATE_RE.match(lines[i + 1]) else None
            stops.append((m.group(1), date))
        if len(stops) == 2:
            break
    if stops:
        out["origin"], out["pickup_date"] = stops[0]
    if len(stops) > 1:
        out["destination"], out["delivery_date"] = stops[1]

    # request contact: 'Name · phone' line
    for l in lines:
        if re.match(r"^[\w .'-]+ · \+?[\d()\- ]{7,}$", l):
            out["contact"] = l
            break

    out["vins"] = sorted(set(_VIN_RE.findall(joined)))
    return out


def record(con: sqlite3.Connection, gmail_id: str, sent_at: float,
           subject: str, body_html: str) -> str:
    """Parse + insert one email (idempotent). Returns the event_type recorded."""
    p = parse_event(subject, body_html)
    base = re.split(r"[\s-]", (p["order_id"] or "").strip())[0].upper() or None
    with con:
        con.execute(
            """INSERT OR REPLACE INTO sd_events
               (gmail_id, sent_at, event_type, order_id, order_base, carrier,
                price, price_per_mile, event_at, reason, origin, destination,
                pickup_date, delivery_date, contact, subject, raw_gz, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gmail_id, sent_at, p["event_type"], p["order_id"], base, p["carrier"],
             p["price"], p["price_per_mile"], p["event_at"], p["reason"],
             p["origin"], p["destination"], p["pickup_date"], p["delivery_date"],
             p["contact"], subject,
             gzip.compress((body_html or "").encode(), 6), time.time()))
        con.execute("DELETE FROM sd_event_vins WHERE gmail_id=?", (gmail_id,))
        for vin in p["vins"]:
            con.execute("INSERT OR IGNORE INTO sd_event_vins (gmail_id, vin) VALUES (?,?)",
                        (gmail_id, vin))
    return p["event_type"]
