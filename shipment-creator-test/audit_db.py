"""Durable, append-only audit ledger for SuperDispatch posting runs.

The ledger intentionally stores operational metadata only: dispatcher, shipment
number, VINs, route, price, inspection type, outcome and returned SD GUID. API
credentials, bearer tokens, request headers and full raw payloads are never stored.

Production and the read-only test site can share a database by setting
``SC_AUDIT_DB`` to the same path. SQLite WAL mode keeps the production writer and
test-site readers from blocking one another.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from contextlib import contextmanager

import paths


def db_path() -> str:
    configured = (os.getenv("SC_AUDIT_DB") or "").strip()
    return os.path.abspath(os.path.expanduser(configured)) if configured else paths.data_path(
        "posting_audit.db")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    target = os.path.abspath(path or db_path())
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Fail quickly if the ledger is ever locked; callers deliberately continue the SD post.
    con = sqlite3.connect(target, timeout=1)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=1000")
    con.execute("PRAGMA foreign_keys=ON")
    _init(con)
    return con


@contextmanager
def _session(path: str | None = None):
    con = connect(path)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _init(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS posting_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            profile_id TEXT,
            profile_name TEXT,
            action TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            attempted_orders INTEGER NOT NULL DEFAULT 0,
            attempted_units INTEGER NOT NULL DEFAULT 0,
            posted_orders INTEGER NOT NULL DEFAULT 0,
            posted_units INTEGER NOT NULL DEFAULT 0,
            duplicate_units INTEGER NOT NULL DEFAULT 0,
            failed_orders INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS posting_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES posting_runs(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            action TEXT NOT NULL,
            shipment_number TEXT,
            vins_json TEXT NOT NULL DEFAULT '[]',
            unit_count INTEGER NOT NULL DEFAULT 0,
            pickup TEXT,
            delivery TEXT,
            price REAL,
            inspection_type TEXT,
            status TEXT NOT NULL DEFAULT 'attempting',
            sd_guid TEXT,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_posting_runs_started
            ON posting_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_posting_items_run
            ON posting_items(run_id, id);
    """)
    con.commit()


def start_run(*, profile_id: str, profile_name: str, action: str,
              attempted_orders: int, attempted_units: int,
              duplicate_units: int = 0) -> int:
    with _session() as con:
        cur = con.execute("""
            INSERT INTO posting_runs(
                started_at, profile_id, profile_name, action, status,
                attempted_orders, attempted_units, duplicate_units
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (_now(), profile_id, profile_name, action, "running",
              int(attempted_orders or 0), int(attempted_units or 0),
              int(duplicate_units or 0)))
        return int(cur.lastrowid)


def start_item(run_id: int, *, action: str, shipment_number: str | None,
               vins: list[str], pickup: str | None = None,
               delivery: str | None = None, price: float | None = None,
               inspection_type: str | None = None) -> int:
    clean_vins = [str(v).strip().upper() for v in (vins or []) if str(v).strip()]
    with _session() as con:
        cur = con.execute("""
            INSERT INTO posting_items(
                run_id, started_at, action, shipment_number, vins_json,
                unit_count, pickup, delivery, price, inspection_type, status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (int(run_id), _now(), action, shipment_number,
              json.dumps(clean_vins, separators=(",", ":")), len(clean_vins),
              pickup, delivery, price, inspection_type, "attempting"))
        return int(cur.lastrowid)


def finish_item(item_id: int, *, status: str, sd_guid: str | None = None,
                error: str | None = None) -> None:
    clean_error = str(error)[:1000] if error else None
    with _session() as con:
        con.execute("""
            UPDATE posting_items
               SET finished_at=?, status=?, sd_guid=?, error=?
             WHERE id=?
        """, (_now(), status, sd_guid, clean_error, int(item_id)))


def finish_run(run_id: int, *, status: str, posted_orders: int,
               posted_units: int, failed_orders: int) -> None:
    with _session() as con:
        con.execute("""
            UPDATE posting_runs
               SET finished_at=?, status=?, posted_orders=?, posted_units=?, failed_orders=?
             WHERE id=?
        """, (_now(), status, int(posted_orders or 0), int(posted_units or 0),
              int(failed_orders or 0), int(run_id)))


def _decode_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["vins"] = json.loads(item.pop("vins_json") or "[]")
    except (TypeError, ValueError):
        item["vins"] = []
        item.pop("vins_json", None)
    return item


def list_runs(*, limit: int = 200, offset: int = 0,
              path: str | None = None) -> dict:
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    with _session(path) as con:
        rows = con.execute("""
            SELECT * FROM posting_runs
             ORDER BY id DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        runs = [dict(r) for r in rows]
        by_id = {r["id"]: r for r in runs}
        for run in runs:
            run["items"] = []
        if by_id:
            marks = ",".join("?" for _ in by_id)
            items = con.execute(
                f"SELECT * FROM posting_items WHERE run_id IN ({marks}) ORDER BY id",
                tuple(by_id),
            ).fetchall()
            for row in items:
                item = _decode_item(row)
                by_id[item["run_id"]]["items"].append(item)
        total = int(con.execute("SELECT COUNT(*) FROM posting_runs").fetchone()[0])
        s = con.execute("""
            SELECT COALESCE(SUM(posted_units),0) AS posted_units,
                   COALESCE(SUM(duplicate_units),0) AS duplicate_units,
                   COALESCE(SUM(failed_orders),0) AS failed_orders,
                   COUNT(DISTINCT CASE WHEN posted_units > 0 THEN profile_id END) AS dispatchers
              FROM posting_runs
        """).fetchone()
        summary = dict(s) if s else {
            "posted_units": 0, "duplicate_units": 0,
            "failed_orders": 0, "dispatchers": 0,
        }
        summary["runs"] = total
        return {"runs": runs, "total": total, "summary": summary}
