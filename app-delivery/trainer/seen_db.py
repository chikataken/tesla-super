"""
Tiny sqlite ledger of shipments (orders) already pulled into the labeling pool, so
random pulls don't repeat the SAME shipment. Keyed on the order's unique id, NOT the
VIN — the same VIN on a DIFFERENT shipment is allowed (different photos).

Pure stdlib (sqlite3), so it imports in any venv (app-delivery for the labeler,
tesla-reconcile for the puller).
"""
from __future__ import annotations
import sqlite3
import time


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS seen(
        order_key TEXT PRIMARY KEY,   -- unique shipment id (order uuid / detail url)
        vins      TEXT,               -- comma-joined VINs pulled from this shipment
        n_photos  INTEGER,
        pulled_at REAL)""")
    con.commit()
    return con


def is_seen(con: sqlite3.Connection, order_key: str) -> bool:
    return con.execute("SELECT 1 FROM seen WHERE order_key=?", (order_key,)).fetchone() is not None


def mark(con: sqlite3.Connection, order_key: str, vins: list[str], n_photos: int) -> None:
    con.execute("INSERT OR REPLACE INTO seen(order_key, vins, n_photos, pulled_at) VALUES(?,?,?,?)",
                (order_key, ",".join(vins), n_photos, time.time()))
    con.commit()


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
