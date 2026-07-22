"""
Audit log of every bid the bidboard userscript submits to Tesla.

The Tampermonkey helper (bidboard/tesla-bidboard-helper.user.js) POSTs a
MakeOffer/UpdateOffer to Tesla per VIN when a route card is submitted, then
fire-and-forgets the same records here (POST /api/bids in app.py). Recording is
strictly an observer: a down server or failed insert never affects live bidding.

APPEND-ONLY by design — no unique constraint on (vin) or (vin, day): re-bidding
the same VIN is a wanted new row; the table IS the bidding timeline. Joins to
tenders.db on VIN: a bid whose VIN later appears in a Tesla tender was won.

Same SQLite/WAL conventions as terminals_db / tenders_db.
"""
from __future__ import annotations
import json
import sqlite3
import time
from typing import Any

import paths

DB_PATH = paths.data_path("bids.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT,               -- one per Enter-press (all VINs of that card action)
    client_ts    TEXT,               -- browser ISO timestamp at the Enter-press (bid placement time)
    received_at  REAL,               -- server unix time the record landed
    origin       TEXT,               -- Tesla facility name, e.g. NA-US-NJ-Cherry Hill
    destination  TEXT,
    origin_state TEXT,
    dest_state   TEXT,
    vin          TEXT,
    bid_id       TEXT,               -- Tesla bidId (== legId)
    model        TEXT,
    vclass       TEXT,               -- std | ct | cab | yl (the userscript's price-box subsets)
    price        REAL,               -- OUR bid amount
    currency     TEXT,
    list_price   REAL,               -- Tesla's list price on the bid row
    prev_counter REAL,               -- our previous offer on this VIN (NULL = first offer)
    verb         TEXT,               -- MakeOffer | UpdateOffer
    pickup_date  TEXT,               -- EstimatedShipDate sent to Tesla (ISO)
    eta_date     TEXT,               -- NeededByDate sent to Tesla (ISO)
    eta_offset   INTEGER,            -- days the chosen ETA differs from the recommended one
    need_by_date TEXT,               -- Tesla's need-by on the bid row
    success      INTEGER,            -- 1 = Tesla accepted the POST, 0 = it failed
    error        TEXT,
    raw          TEXT                -- full client record (re-parse later without a schema change)
);
CREATE INDEX IF NOT EXISTS ix_bids_vin ON bids(vin);
CREATE INDEX IF NOT EXISTS ix_bids_batch ON bids(batch_id);
CREATE INDEX IF NOT EXISTS ix_bids_client_ts ON bids(client_ts);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(_SCHEMA)
    return con


def _num(v: Any) -> float | None:
    """Prices arrive as strings ('499', '$499', 499). None/'' -> NULL."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def insert_bids(batch_id: str | None, client_ts: str | None,
                records: list[dict]) -> int:
    """Append one row per record in a single transaction. Returns rows written.
    Unknown/missing fields become NULL — never reject a record for shape."""
    con = connect()
    now = time.time()
    n = 0
    with con:
        for r in records:
            if not isinstance(r, dict):
                continue
            con.execute(
                """INSERT INTO bids (batch_id, client_ts, received_at,
                     origin, destination, origin_state, dest_state,
                     vin, bid_id, model, vclass, price, currency, list_price,
                     prev_counter, verb, pickup_date, eta_date, eta_offset,
                     need_by_date, success, error, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, client_ts, now,
                 r.get("origin"), r.get("destination"),
                 r.get("origin_state"), r.get("dest_state"),
                 r.get("vin"), str(r.get("bid_id") or "") or None,
                 r.get("model"), r.get("vclass"),
                 _num(r.get("price")), r.get("currency") or "USD",
                 _num(r.get("list_price")), _num(r.get("prev_counter")),
                 r.get("verb"), r.get("pickup_date"), r.get("eta_date"),
                 _int(r.get("eta_offset")), r.get("need_by_date"),
                 1 if r.get("success") else 0, r.get("error"),
                 json.dumps(r, default=str)))
            n += 1
    con.close()
    return n
