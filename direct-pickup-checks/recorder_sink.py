"""Write Super Dispatch order state into the shipment-recorder DB.

Used by the listener's webhook fan-out: on each accepted event we fetch the full
order (sd_client.get_order) and upsert it here, so the recorder mirror stays live.

This module is intentionally **self-contained** — it does NOT import anything from
the sibling `shipment-creator-test` project (both projects have a `config.py`, so
importing across them would collide). It only needs the recorder DB *path* and a
copy of the schema (CREATE IF NOT EXISTS, so it's a no-op once backfill made it).

The recorder primary key is the order's web/view UUID, which we confirmed equals
the API order GUID — so a webhook (carrying order_guid) updates the SAME row the
backfill scrape created, in place. The upsert only writes the columns it owns and
preserves `card_text` / `first_seen` from the scrape.
"""
from __future__ import annotations
import datetime as _dt
import json
import os
import sqlite3
import time

# Default to the test site's DB; override with RECORDER_DB.
RECORDER_DB = os.getenv(
    "RECORDER_DB",
    "/home/mbdtf/projects/tesla-super/shipment-creator-test/data/recorder.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    web_uuid TEXT PRIMARY KEY, number TEXT, api_guid TEXT, status TEXT,
    pickup_city TEXT, pickup_state TEXT, pickup_zip TEXT, pickup_date TEXT, pickup_terminal TEXT,
    delivery_city TEXT, delivery_state TEXT, delivery_zip TEXT, delivery_date TEXT, delivery_terminal TEXT,
    vins TEXT, card_text TEXT, details TEXT, created_at TEXT,
    first_seen REAL, last_seen REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS vehicles (
    web_uuid TEXT NOT NULL, vin TEXT NOT NULL, position INTEGER, PRIMARY KEY (web_uuid, vin)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, web_uuid TEXT, number TEXT, action TEXT,
    status TEXT, occurred_day TEXT, source TEXT, payload TEXT, received_at REAL,
    UNIQUE (web_uuid, status, occurred_day)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_number ON orders(number);
CREATE INDEX IF NOT EXISTS ix_events_uuid   ON events(web_uuid);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(RECORDER_DB), exist_ok=True)
    con = sqlite3.connect(RECORDER_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(_SCHEMA)            # idempotent; safe if backfill already made it
    # created_at was added after the first backfill; add it to a pre-existing table.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
    if "created_at" not in cols:
        try:
            con.execute("ALTER TABLE orders ADD COLUMN created_at TEXT")
        except sqlite3.OperationalError:
            pass
    return con


def _short_date(iso: str | None) -> str:
    """'2026-05-20T16:00:00.000+0000' -> 'May 20' (matches the scraped card style)."""
    if not iso:
        return ""
    try:
        d = _dt.datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        return d.strftime("%b ") + str(d.day)
    except Exception:                                       # noqa: BLE001
        return ""


def derive_status(o: dict) -> str:
    """Map a full order to the recorder's status vocabulary (same labels the
    backfill tabs use), so webhook rows group with scraped ones."""
    if not o:
        return "unknown"
    if o.get("is_archived") or o.get("archived"):
        return "archived"
    s = (o.get("status") or "").strip().lower()
    if s in ("order_canceled", "canceled", "cancelled"):
        return "canceled"
    # "posted" is a flag, not an API status value — surface it like the loadboard tab.
    if s in ("", "new") and o.get("is_posted_to_loadboard"):
        return "posted"
    return s or "unknown"


def _vins(o: dict) -> list[str]:
    return [v.get("vin") for v in (o.get("vehicles") or []) if v.get("vin")]


def record_event(con: sqlite3.Connection, order_guid: str, action: str | None,
                 occurred_at: str | None, number: str | None, status: str | None,
                 payload_text: str | None) -> bool:
    """Append a webhook sighting. Idempotent per (order, status, event-timestamp) —
    so SD's retries of one event collapse, but distinct events are all logged."""
    cur = con.execute(
        """INSERT OR IGNORE INTO events
           (web_uuid, number, action, status, occurred_day, source, payload, received_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (order_guid, number, action or "", status,
         occurred_at or _dt.date.today().isoformat(), "webhook",
         payload_text, time.time()))
    return cur.rowcount > 0


def upsert_from_order(con: sqlite3.Connection, o: dict) -> None:
    """Upsert one order snapshot from a full get_order payload, keyed by its GUID
    (== the recorder web_uuid). Writes only API-owned columns; preserves the scrape's
    card_text and the original first_seen."""
    guid = o.get("guid") or o.get("id")
    if not guid:
        return
    now = time.time()
    pv = (o.get("pickup") or {}); pvv = pv.get("venue") or {}
    dv = (o.get("delivery") or {}); dvv = dv.get("venue") or {}
    vins = _vins(o)
    row = {
        "web_uuid": guid, "number": o.get("number"), "api_guid": guid,
        "status": derive_status(o), "created_at": o.get("created_at"),
        "pickup_city": pvv.get("city"), "pickup_state": pvv.get("state"),
        "pickup_zip": pvv.get("zip"),
        "pickup_date": _short_date(pv.get("completed_at") or pv.get("scheduled_at")),
        "pickup_terminal": pvv.get("name"),
        "delivery_city": dvv.get("city"), "delivery_state": dvv.get("state"),
        "delivery_zip": dvv.get("zip"),
        "delivery_date": _short_date(dv.get("completed_at") or dv.get("scheduled_at")),
        "delivery_terminal": dvv.get("name"),
        "vins": json.dumps(vins), "details": json.dumps(o), "now": now,
    }
    con.execute(
        """
        INSERT INTO orders (web_uuid, number, api_guid, status, created_at,
            pickup_city, pickup_state, pickup_zip, pickup_date, pickup_terminal,
            delivery_city, delivery_state, delivery_zip, delivery_date, delivery_terminal,
            vins, details, first_seen, last_seen, updated_at)
        VALUES (:web_uuid, :number, :api_guid, :status, :created_at,
            :pickup_city, :pickup_state, :pickup_zip, :pickup_date, :pickup_terminal,
            :delivery_city, :delivery_state, :delivery_zip, :delivery_date, :delivery_terminal,
            :vins, :details, :now, :now, :now)
        ON CONFLICT(web_uuid) DO UPDATE SET
            number=excluded.number, api_guid=excluded.api_guid, status=excluded.status,
            created_at=excluded.created_at,
            pickup_city=excluded.pickup_city, pickup_state=excluded.pickup_state,
            pickup_zip=excluded.pickup_zip, pickup_date=excluded.pickup_date,
            pickup_terminal=excluded.pickup_terminal,
            delivery_city=excluded.delivery_city, delivery_state=excluded.delivery_state,
            delivery_zip=excluded.delivery_zip, delivery_date=excluded.delivery_date,
            delivery_terminal=excluded.delivery_terminal,
            vins=excluded.vins, details=excluded.details,
            last_seen=excluded.last_seen, updated_at=excluded.updated_at
        """, row)
    for i, vin in enumerate(vins):
        con.execute("INSERT OR IGNORE INTO vehicles (web_uuid, vin, position) VALUES (?,?,?)",
                    (guid, vin, i))


def record_and_upsert(order_guid: str, action: str | None, occurred_at: str | None,
                      payload_text: str | None, order: dict | None) -> None:
    """One-call helper for the listener fan-out: open, log the event, upsert the
    snapshot (if we fetched the order), commit, close. Never raises."""
    con = connect()
    try:
        number = (order or {}).get("number")
        status = derive_status(order) if order else None
        record_event(con, order_guid, action, occurred_at, number, status, payload_text)
        if order:
            upsert_from_order(con, order)
        con.commit()
    finally:
        con.close()
