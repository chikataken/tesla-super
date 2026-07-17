"""wells-check local store: every PAID SuperDispatch order, scraped page-by-page,
then enriched with the payment block via the API.

Two tables:
  paid_orders — one row per order guid. The scrape phase fills guid/order_id/
                vin_preview/page_seen; the enrich phase adds price + the payment
                reference (check #) etc. INSERT OR IGNORE keyed on guid, so pages
                shifting under the scan (new paid orders push everything down)
                just re-see known rows harmlessly.
  scan_state  — key/value: where the scan is (next_page), run bookkeeping. This is
                what makes ./run.sh resumable.

    python db.py        # print stats
"""
from __future__ import annotations
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WELLS_DB", os.path.join(HERE, "data", "wells.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_orders(
    guid             TEXT PRIMARY KEY,   -- /orders/view/<guid>; same guid the public API uses
    order_id         TEXT,               -- card id text (e.g. A55H890)
    vin_preview      TEXT,               -- first VIN visible on the card (no click-through)
    page_seen        INTEGER,            -- Paid-tab page the card was first seen on
    scraped_at       REAL,
    -- enrichment (SD public API)
    number           TEXT,               -- authoritative order number from the API
    price            REAL,               -- carrier total for the WHOLE order
    reference_number TEXT,               -- payment reference = the check number
    method           TEXT,               -- payment method (check / other / ...)
    payment_notes    TEXT,
    sent_date        TEXT,               -- payment sent date
    delivered_at     TEXT,
    vins             TEXT,               -- json array: every VIN on the order
    vehicle_count    INTEGER,
    enriched_at      REAL,
    enrich_error     TEXT
);
CREATE INDEX IF NOT EXISTS ix_paid_ref ON paid_orders(reference_number);
CREATE INDEX IF NOT EXISTS ix_paid_num ON paid_orders(order_id);
CREATE TABLE IF NOT EXISTS scan_state(
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Checks parsed off uploaded Wells Fargo statement PDFs (written by the test
-- site's /api/wells-checks/statement; matched against paid_orders by number+amount).
CREATE TABLE IF NOT EXISTS wf_checks(
    check_number TEXT PRIMARY KEY,
    amount       REAL,
    date         TEXT,
    source_file  TEXT,
    uploaded_at  REAL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM scan_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute("INSERT INTO scan_state(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def upsert_scraped(conn, guid: str, order_id: str, vin: str | None, page: int) -> bool:
    """Record a card sighting. Returns True when the guid is NEW. Existing rows only
    get blanks filled (a re-seen card never clobbers enrichment)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO paid_orders(guid, order_id, vin_preview, page_seen, scraped_at)"
        " VALUES(?,?,?,?,?)", (guid, order_id, vin, page, time.time()))
    if cur.rowcount:
        return True
    conn.execute(
        "UPDATE paid_orders SET order_id=COALESCE(order_id,?),"
        " vin_preview=COALESCE(vin_preview,?) WHERE guid=?", (order_id, vin, guid))
    return False


def unenriched(conn, limit: int = 0) -> list[dict]:
    """Rows still needing the API pass (never enriched and not permanently failed).
    Newest-seen first, so the most recent payments get reference numbers first."""
    sql = ("SELECT guid, order_id, vin_preview FROM paid_orders"
           " WHERE enriched_at IS NULL AND enrich_error IS NULL ORDER BY rowid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def save_enrichment(conn, guid: str, o: dict) -> None:
    pay = o.get("payment") or {}
    vins = [v.get("vin") for v in (o.get("vehicles") or []) if v.get("vin")]
    conn.execute(
        """UPDATE paid_orders SET number=?, price=?, reference_number=?, method=?,
           payment_notes=?, sent_date=?, delivered_at=?, vins=?, vehicle_count=?,
           enriched_at=?, enrich_error=NULL WHERE guid=?""",
        (o.get("number"), o.get("price"), pay.get("reference_number"),
         pay.get("method"), pay.get("notes"), pay.get("sent_date"),
         (o.get("delivery") or {}).get("completed_at"),
         json.dumps(vins), len(o.get("vehicles") or []), time.time(), guid))
    conn.commit()


def mark_error(conn, guid: str, err: str) -> None:
    conn.execute("UPDATE paid_orders SET enrich_error=? WHERE guid=?", (err[:300], guid))
    conn.commit()


def stats(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "scraped": q("SELECT COUNT(*) FROM paid_orders"),
        "enriched": q("SELECT COUNT(*) FROM paid_orders WHERE enriched_at IS NOT NULL"),
        "errors": q("SELECT COUNT(*) FROM paid_orders WHERE enrich_error IS NOT NULL"),
        "with_reference": q("SELECT COUNT(*) FROM paid_orders WHERE reference_number IS NOT NULL"
                            " AND reference_number != ''"),
        "distinct_references": q("SELECT COUNT(DISTINCT reference_number) FROM paid_orders"
                                 " WHERE reference_number IS NOT NULL AND reference_number != ''"),
        "next_page": int(get_state(conn, "next_page", "1")),
        "scan_done": get_state(conn, "scan_done", ""),
        "last_run_at": get_state(conn, "last_run_at", ""),
    }


if __name__ == "__main__":
    with connect() as c:
        for k, v in stats(c).items():
            print(f"{k:22} {v}")
