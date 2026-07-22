"""
Local mirror of Tesla *Load Tender* emails (SA-AppUser@tesla.com -> didi@).

WHY: the tender email is the earliest, richest machine-readable source for a
shipment — per-VIN origin/destination, pickup + need-by dates, per-VIN cost and
LegID — arriving before anything is visible on the SuperDispatch side and
without scraping the Tesla portal. Recording every tender gives future features
(auto-board fill, cost checks, extensions) a queryable history.

Same SQLite/WAL/UPSERT pattern as terminals_db.py: ingest walks Gmail messages
one-by-one and upserts each the moment it's parsed, keyed on the Gmail message
id — a partial run leaves a valid file and resumes cleanly.

REVISION MODEL: Tesla re-sends a tender (same SHP) when costs change or VINs
are removed; each email is kept verbatim in `tender_emails`, and the
`current_tenders` / `current_vins` views expose only the LATEST email per SHP.
Consumers should read the views; history stays in the tables.

FIELD RELIABILITY (measured on a live day of 141 tenders / 340 VIN rows):
vin, origin, destination, scheduled pickup, cost, leg_id are always present;
required_delivery is empty on ~26% of rows (nullable — same fallback story as
Excel `need_by`); staging_location and driver/plate are almost never present.
"""
from __future__ import annotations
import re
import sqlite3
import time
from html.parser import HTMLParser
from typing import Any, Optional

import paths

DB_PATH = paths.data_path("tenders.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tender_emails (
    gmail_id       TEXT PRIMARY KEY,   -- Gmail message id (stable dedupe key)
    shp            TEXT NOT NULL,      -- e.g. SHP2607-A3WW630
    shipment_id    TEXT,               -- Tesla numeric ShipmentId (e.g. 44170946)
    tender_ts      TEXT,               -- timestamp printed inside the email body
    sent_at        REAL NOT NULL,      -- Gmail internalDate (unix seconds) -> revision order
    subject        TEXT,
    origin_facility TEXT,              -- header "From:" facility (name + address, newline-joined)
    origin_contact TEXT,
    origin_contact_email TEXT,
    service_level  TEXT,
    carrier        TEXT,
    driver         TEXT,
    driver_phone   TEXT,
    license_plate  TEXT,
    comments       TEXT,               -- gate codes / hours / key-box notes
    recipients     TEXT,
    raw_html       TEXT,               -- verbatim body -> re-parse later without Gmail
    ingested_at    REAL
);
CREATE INDEX IF NOT EXISTS ix_tender_emails_shp ON tender_emails(shp);

CREATE TABLE IF NOT EXISTS tender_vins (
    gmail_id       TEXT NOT NULL REFERENCES tender_emails(gmail_id) ON DELETE CASCADE,
    vin            TEXT NOT NULL,
    body_color     TEXT,
    staging_location TEXT,
    origin         TEXT,               -- full cell, newline-joined (name/street/city/state/country)
    origin_name    TEXT,
    origin_city    TEXT,
    origin_state   TEXT,
    destination    TEXT,
    destination_name  TEXT,
    destination_city  TEXT,
    destination_state TEXT,
    scheduled_pickup  TEXT,            -- ISO yyyy-mm-dd (always present)
    required_delivery TEXT,            -- ISO yyyy-mm-dd or NULL (~26% of live rows)
    routing_service_center TEXT,
    cost_usd       REAL,               -- per-VIN; 0.00 occurs on real tenders — flag, don't trust
    leg_id         TEXT,
    PRIMARY KEY (gmail_id, vin)
);
CREATE INDEX IF NOT EXISTS ix_tender_vins_vin ON tender_vins(vin);
CREATE INDEX IF NOT EXISTS ix_tender_vins_leg ON tender_vins(leg_id);

-- Single-row incremental-sync cursor (Gmail history API). last_history_id is
-- the mailbox historyId already processed; the minute tick asks Gmail only for
-- changes after it. NULL -> next sync does a full day-sweep and reseeds.
CREATE TABLE IF NOT EXISTS sync_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_history_id TEXT,
    last_sync_at    REAL,
    note            TEXT
);

-- Latest email per SHP wins; earlier ones remain as history.
CREATE VIEW IF NOT EXISTS current_tenders AS
    SELECT * FROM tender_emails e
    WHERE sent_at = (SELECT MAX(sent_at) FROM tender_emails WHERE shp = e.shp);
CREATE VIEW IF NOT EXISTS current_vins AS
    SELECT v.* FROM tender_vins v
    JOIN current_tenders c ON c.gmail_id = v.gmail_id;
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    # The minute sync tick and a long backfill can write concurrently; wait out
    # the other writer's (short) transaction instead of erroring immediately.
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(_SCHEMA)
    return con


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TABLE_HDR = ["VIN", "Body Color", "Staging Location", "Origin", "Destination",
              "Scheduled Pickup Date", "Required Delivery Date",
              "Routing Service Center", "Cost", "LegID"]
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


class _Cells(HTMLParser):
    """Two views of the email in one pass: `rows` = table cells (the VIN table;
    <br> keeps line structure inside a cell) and `chunks` = the FULL text with a
    newline at every tag boundary — the header block (Shipment #, From, Comments…)
    lives in <div>/<span> markup, NOT table cells, so header parsing needs this."""

    _BREAKERS = {"div", "p", "br", "tr", "td", "th", "table", "span",
                 "h1", "h2", "h3", "h4", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.chunks: list[str] = []
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag in self._BREAKERS:
            self.chunks.append("\n")
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BREAKERS:
            self.chunks.append("\n")
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = "".join(self._cell)
            lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
            self._row.append("\n".join(l for l in lines if l))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c.strip() for c in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        self.chunks.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def text(self) -> str:
        """All text, one logical line per tag-delimited fragment; lines that are
        bare punctuation (the ',' separators between city/state/zip) dropped."""
        lines = [re.sub(r"\s+", " ", l).strip() for l in "".join(self.chunks).splitlines()]
        return "\n".join(l for l in lines if re.search(r"[A-Za-z0-9]", l))


def _iso(us_date: str) -> Optional[str]:
    m = re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", (us_date or "").strip())
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def _addr_parts(cell: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Origin/Destination cells read: name / street / city / state / country."""
    lines = [l for l in (cell or "").split("\n") if l]
    name = lines[0] if lines else None
    city = state = None
    if len(lines) >= 2 and lines[-1] in ("US", "USA"):
        lines = lines[:-1]
    if len(lines) >= 3:
        city, state = lines[-2], lines[-1]
    return name, city, state


def _label(text: str, label: str, stop: str) -> Optional[str]:
    m = re.search(rf"{re.escape(label)}\s*\n(.*?)\n{re.escape(stop)}", text, re.S)
    if not m:
        return None
    val = "\n".join(l.strip() for l in m.group(1).splitlines() if l.strip())
    return val or None


def parse_tender(html: str) -> dict[str, Any]:
    """Parse one Load Tender email body -> header dict + list of VIN rows.
    Raises ValueError if the SHP id or the 10-column VIN table is missing."""
    p = _Cells()
    p.feed(html)
    flat = p.text()

    m = re.search(r"(SHP\d{4}-[A-Z0-9]+)\s*/\s*(\d+)", flat)
    if not m:
        raise ValueError("no SHP id found")
    hdr: dict[str, Any] = {"shp": m.group(1), "shipment_id": m.group(2)}
    t = re.search(r"Date:\s*([\d/]+ [\d:]+)", flat)
    hdr["tender_ts"] = t.group(1) if t else None

    contact = _label(flat, "Origin Contact:", "Service Level:") or ""
    cm = re.search(r"\S+@\S+", contact)
    hdr["origin_contact_email"] = cm.group(0) if cm else None
    hdr["origin_contact"] = contact.replace(hdr["origin_contact_email"] or "", "").strip() or None
    hdr["origin_facility"] = _label(flat, "From:", "Origin Contact:")
    hdr["service_level"] = _label(flat, "Service Level:", "Carrier:")
    hdr["carrier"] = _label(flat, "Carrier:", "Driver:")
    hdr["driver"] = _label(flat, "Driver:", "Driver Phone:")
    hdr["driver_phone"] = _label(flat, "Driver Phone:", "Email:")
    hdr["license_plate"] = _label(flat, "License Plate:", "Comments:")
    hdr["comments"] = _label(flat, "Comments:", "VIN")

    vins = []
    for row in p.rows:
        if len(row) != len(_TABLE_HDR) or not _VIN_RE.fullmatch(row[0].strip()):
            continue
        (vin, color, staging, origin, dest, pickup, need_by, rsc, cost, leg) = (
            c.strip() for c in row)
        cost_m = re.search(r"([\d.]+)\s*USD", cost)
        o_name, o_city, o_state = _addr_parts(origin)
        d_name, d_city, d_state = _addr_parts(dest)
        vins.append({
            "vin": vin, "body_color": color or None,
            "staging_location": staging or None,
            "origin": origin or None, "origin_name": o_name,
            "origin_city": o_city, "origin_state": o_state,
            "destination": dest or None, "destination_name": d_name,
            "destination_city": d_city, "destination_state": d_state,
            "scheduled_pickup": _iso(pickup),
            "required_delivery": _iso(need_by),
            "routing_service_center": rsc or None,
            "cost_usd": float(cost_m.group(1)) if cost_m else None,
            "leg_id": leg or None,
        })
    if not vins:
        raise ValueError(f"no VIN rows parsed for {hdr['shp']}")
    hdr["vins"] = vins
    return hdr


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def upsert_email(con: sqlite3.Connection, gmail_id: str, sent_at: float,
                 subject: str, recipients: str, raw_html: str,
                 parsed: dict[str, Any]) -> None:
    """Insert-or-replace one email + its VIN rows atomically."""
    with con:
        con.execute(
            """INSERT OR REPLACE INTO tender_emails
               (gmail_id, shp, shipment_id, tender_ts, sent_at, subject,
                origin_facility, origin_contact, origin_contact_email,
                service_level, carrier, driver, driver_phone, license_plate,
                comments, recipients, raw_html, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gmail_id, parsed["shp"], parsed.get("shipment_id"),
             parsed.get("tender_ts"), sent_at, subject,
             parsed.get("origin_facility"), parsed.get("origin_contact"),
             parsed.get("origin_contact_email"), parsed.get("service_level"),
             parsed.get("carrier"), parsed.get("driver"),
             parsed.get("driver_phone"), parsed.get("license_plate"),
             parsed.get("comments"), recipients, raw_html, time.time()))
        con.execute("DELETE FROM tender_vins WHERE gmail_id = ?", (gmail_id,))
        for v in parsed["vins"]:
            con.execute(
                """INSERT INTO tender_vins
                   (gmail_id, vin, body_color, staging_location,
                    origin, origin_name, origin_city, origin_state,
                    destination, destination_name, destination_city, destination_state,
                    scheduled_pickup, required_delivery, routing_service_center,
                    cost_usd, leg_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gmail_id, v["vin"], v["body_color"], v["staging_location"],
                 v["origin"], v["origin_name"], v["origin_city"], v["origin_state"],
                 v["destination"], v["destination_name"], v["destination_city"],
                 v["destination_state"], v["scheduled_pickup"],
                 v["required_delivery"], v["routing_service_center"],
                 v["cost_usd"], v["leg_id"]))


def have_gmail_id(con: sqlite3.Connection, gmail_id: str) -> bool:
    return con.execute("SELECT 1 FROM tender_emails WHERE gmail_id = ?",
                       (gmail_id,)).fetchone() is not None


def get_history_id(con: sqlite3.Connection) -> Optional[str]:
    row = con.execute("SELECT last_history_id FROM sync_state WHERE id = 1").fetchone()
    return row["last_history_id"] if row else None


def set_history_id(con: sqlite3.Connection, history_id: Optional[str],
                   note: str = "") -> None:
    with con:
        con.execute(
            """INSERT INTO sync_state (id, last_history_id, last_sync_at, note)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_history_id = excluded.last_history_id,
                   last_sync_at = excluded.last_sync_at, note = excluded.note""",
            (history_id, time.time(), note))
