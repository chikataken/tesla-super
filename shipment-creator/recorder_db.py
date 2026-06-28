"""SQLite store for the shipment recorder.

Goal: a local mirror of the Super Dispatch shipment database — an append-only
**event log** (every status sighting/transition) plus a **current snapshot** per
order. This first cut is populated by the backfill scraper (recorder_backfill.py);
the same schema is what a future live-webhook feed would write into.

Three tables (WAL, like direct-pickup-checks):
  * orders   — one current-state row per order (keyed by the web view UUID, which
               is what the loadboard rows expose; the API GUID is added later when
               we enrich via get_order).
  * vehicles — one row per VIN on an order.
  * events   — append-only: every time we *see* an order on a status tab we log it,
               so the timeline (posted -> picked_up -> delivered -> ...) is preserved
               even though a card only shows the order's CURRENT status.

Dedup: an event is unique on (web_uuid, status, occurred_day) so re-running the
backfill the same day is idempotent and doesn't pile up duplicate sightings.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from typing import Iterable, Optional

import config

DB_PATH = os.getenv("RECORDER_DB",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data", "recorder.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    web_uuid       TEXT PRIMARY KEY,    -- /orders/view/<uuid> (loadboard row link)
    number         TEXT,                -- human order number (e.g. A46Y102)
    api_guid       TEXT,                -- SD API order GUID (filled when enriched)
    status         TEXT,                -- current status tab (new|posted|...|paid)
    pickup_city    TEXT,
    pickup_state   TEXT,
    pickup_zip     TEXT,
    pickup_date    TEXT,                -- as shown on the card (e.g. "Jun 23")
    pickup_terminal TEXT,
    delivery_city  TEXT,
    delivery_state TEXT,
    delivery_zip   TEXT,
    delivery_date  TEXT,
    delivery_terminal TEXT,
    vins           TEXT,                -- JSON array of VINs (denormalized for display)
    card_text      TEXT,                -- raw scraped row text (fidelity / re-parse)
    details        TEXT,                -- full get-order JSON once enriched (nullable)
    created_at     TEXT,                -- SD order creation timestamp (from details; nullable)
    first_seen     REAL,
    last_seen      REAL,
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS vehicles (
    web_uuid       TEXT NOT NULL,
    vin            TEXT NOT NULL,
    position       INTEGER,
    PRIMARY KEY (web_uuid, vin)
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    web_uuid       TEXT,
    number         TEXT,
    action         TEXT,                -- e.g. "backfill.delivered"
    status         TEXT,                -- status tab the sighting came from
    occurred_day   TEXT,                -- YYYY-MM-DD of the run (sighting day)
    source         TEXT,                -- backfill_scrape | webhook | ...
    payload        TEXT,                -- JSON of the parsed card (or webhook body)
    received_at    REAL,
    UNIQUE (web_uuid, status, occurred_day)
);

CREATE TABLE IF NOT EXISTS meta (
    key            TEXT PRIMARY KEY,
    value          TEXT,
    updated_at     REAL
);

CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_number ON orders(number);
CREATE INDEX IF NOT EXISTS ix_events_uuid   ON events(web_uuid);
"""


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


_migrated = False


def connect() -> sqlite3.Connection:
    global _migrated
    _ensure_dir()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    if not _migrated:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        if cols and "created_at" not in cols:
            try:
                con.execute("ALTER TABLE orders ADD COLUMN created_at TEXT")
                con.commit()
            except sqlite3.OperationalError:
                pass
        _migrated = True
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after the first backfill (SQLite has no IF NOT EXISTS
    for ADD COLUMN, so just try and ignore the 'duplicate column' error)."""
    for ddl in ("ALTER TABLE orders ADD COLUMN created_at TEXT",):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass


def init() -> None:
    con = connect()
    try:
        con.executescript(_SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


# --- writes ----------------------------------------------------------------
def upsert_order(con: sqlite3.Connection, o: dict) -> None:
    """Insert or update one order snapshot from a parsed card dict.

    Preserves first_seen; refreshes everything else. `o` uses the keys produced by
    recorder_scrape.parse_card (web_uuid, number, status, pickup_*, delivery_*,
    vins[list], card_text)."""
    now = time.time()
    vins = o.get("vins") or []
    con.execute(
        """
        INSERT INTO orders (web_uuid, number, status,
            pickup_city, pickup_state, pickup_zip, pickup_date, pickup_terminal,
            delivery_city, delivery_state, delivery_zip, delivery_date, delivery_terminal,
            vins, card_text, first_seen, last_seen, updated_at)
        VALUES (:web_uuid, :number, :status,
            :pickup_city, :pickup_state, :pickup_zip, :pickup_date, :pickup_terminal,
            :delivery_city, :delivery_state, :delivery_zip, :delivery_date, :delivery_terminal,
            :vins, :card_text, :now, :now, :now)
        ON CONFLICT(web_uuid) DO UPDATE SET
            number=excluded.number, status=excluded.status,
            pickup_city=excluded.pickup_city, pickup_state=excluded.pickup_state,
            pickup_zip=excluded.pickup_zip, pickup_date=excluded.pickup_date,
            pickup_terminal=excluded.pickup_terminal,
            delivery_city=excluded.delivery_city, delivery_state=excluded.delivery_state,
            delivery_zip=excluded.delivery_zip, delivery_date=excluded.delivery_date,
            delivery_terminal=excluded.delivery_terminal,
            vins=excluded.vins, card_text=excluded.card_text,
            last_seen=excluded.last_seen, updated_at=excluded.updated_at
        """,
        {
            "web_uuid": o["web_uuid"], "number": o.get("number"),
            "status": o.get("status"),
            "pickup_city": o.get("pickup_city"), "pickup_state": o.get("pickup_state"),
            "pickup_zip": o.get("pickup_zip"), "pickup_date": o.get("pickup_date"),
            "pickup_terminal": o.get("pickup_terminal"),
            "delivery_city": o.get("delivery_city"), "delivery_state": o.get("delivery_state"),
            "delivery_zip": o.get("delivery_zip"), "delivery_date": o.get("delivery_date"),
            "delivery_terminal": o.get("delivery_terminal"),
            "vins": json.dumps(vins), "card_text": o.get("card_text"), "now": now,
        },
    )
    for i, vin in enumerate(vins):
        con.execute(
            "INSERT OR IGNORE INTO vehicles (web_uuid, vin, position) VALUES (?,?,?)",
            (o["web_uuid"], vin, i),
        )


def _short_date(iso) -> str:
    import datetime as _dt
    if not iso:
        return ""
    try:
        d = _dt.datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        return d.strftime("%b ") + str(d.day)
    except Exception:
        return ""


def _derive_status(o: dict) -> str:
    if not o:
        return "unknown"
    if o.get("is_archived") or o.get("archived"):
        return "archived"
    s = (o.get("status") or "").strip().lower()
    if s in ("order_canceled", "canceled", "cancelled"):
        return "canceled"
    if s in ("", "new") and o.get("is_posted_to_loadboard"):
        return "posted"
    return s or "unknown"


def enrich_order(con: sqlite3.Connection, o: dict) -> None:
    """Update an existing row from a full get_order payload: fill created_at +
    api_guid + details, refine status, and replace pickup/delivery with the clean
    venue fields. Preserves card_text/first_seen. Keyed by guid (== web_uuid)."""
    guid = o.get("guid") or o.get("id")
    if not guid:
        return
    pv = (o.get("pickup") or {}); pvv = pv.get("venue") or {}
    dv = (o.get("delivery") or {}); dvv = dv.get("venue") or {}
    vins = [v.get("vin") for v in (o.get("vehicles") or []) if v.get("vin")]
    con.execute(
        """UPDATE orders SET
             number=?, api_guid=?, status=?, created_at=?,
             pickup_city=?, pickup_state=?, pickup_zip=?, pickup_date=?, pickup_terminal=?,
             delivery_city=?, delivery_state=?, delivery_zip=?, delivery_date=?, delivery_terminal=?,
             vins=?, details=?, updated_at=?
           WHERE web_uuid=?""",
        (o.get("number"), guid, _derive_status(o), o.get("created_at"),
         pvv.get("city"), pvv.get("state"), pvv.get("zip"),
         _short_date(pv.get("completed_at") or pv.get("scheduled_at")), pvv.get("name"),
         dvv.get("city"), dvv.get("state"), dvv.get("zip"),
         _short_date(dv.get("completed_at") or dv.get("scheduled_at")), dvv.get("name"),
         json.dumps(vins), json.dumps(o), time.time(), guid))


def add_event(con: sqlite3.Connection, o: dict, occurred_day: str,
              source: str = "backfill_scrape") -> bool:
    """Append a sighting to the event log. Idempotent per (uuid, status, day).
    Returns True if a new row was inserted."""
    cur = con.execute(
        """INSERT OR IGNORE INTO events
           (web_uuid, number, action, status, occurred_day, source, payload, received_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (o["web_uuid"], o.get("number"), f"{source}.{o.get('status')}",
         o.get("status"), occurred_day, source, json.dumps(o), time.time()),
    )
    return cur.rowcount > 0


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), time.time()),
    )


# --- reads (for the test page) ---------------------------------------------
def counts_by_status(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "SELECT status, COUNT(*) n FROM orders GROUP BY status ORDER BY n DESC"
    ).fetchall()
    return {(r["status"] or "unknown"): r["n"] for r in rows}


def total(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]


# sort key -> column. updated = when WE last touched the record (always present);
# created = the SD order's creation date (from details; null until enriched).
_SORT_COLS = {"updated": "updated_at", "created": "created_at",
              "number": "number", "status": "status"}


def list_orders(con: sqlite3.Connection, status: Optional[str] = None,
                q: Optional[str] = None, limit: int = 500, offset: int = 0,
                sort: str = "updated", direction: str = "desc") -> list[dict]:
    where, args = [], []
    if status and status != "all":
        where.append("status = ?"); args.append(status)
    if q:
        where.append("(number LIKE ? OR vins LIKE ? OR card_text LIKE ?)")
        like = f"%{q}%"; args += [like, like, like]
    sql = "SELECT * FROM orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    col = _SORT_COLS.get(sort, "updated_at")
    d = "ASC" if str(direction).lower() == "asc" else "DESC"
    # NULLs always last (e.g. created_at before enrichment), then by the chosen dir.
    sql += f" ORDER BY ({col} IS NULL), {col} {d}, updated_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    out = []
    for r in con.execute(sql, args).fetchall():
        d = dict(r)
        try:
            d["vins"] = json.loads(d.get("vins") or "[]")
        except Exception:
            d["vins"] = []
        d.pop("details", None)            # keep the list payload small
        out.append(d)
    return out


if __name__ == "__main__":
    init()
    con = connect()
    try:
        print(f"DB: {DB_PATH}")
        print(f"orders: {total(con)}")
        print(f"by status: {counts_by_status(con)}")
    finally:
        con.close()
