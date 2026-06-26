"""
SQLite store: the durable queue, the dedup table, and the state tables.

Why SQLite (not Redis/RQ): the sibling tools use no external services — they
persist to JSON/CSV files. A single SQLite file with WAL mode gives us a durable
listener->worker handoff plus the shipment/photo/tag state in one place, with no
extra daemon to run. The listener and worker are separate processes that each open
this same file; WAL + a busy timeout make the concurrent access safe.

Tables
------
seen_events : every event GUID we've accepted -> duplicate detection + raw payload.
queue       : durable work items the worker pulls (claim/done/fail, attempt count).
shipments   : one row per order GUID (status, picked-up time, full details json).
vins        : VINs per order (order_guid, vin).
photos      : downloaded inspection photos (idempotent on photo_id), with metadata.
tags        : tagging results (idempotent on photo_id).
ui_events   : append-only feed the listener's SSE endpoint streams to the UI.

Everything that can be re-delivered is idempotent: seen_events dedups events,
photos/tags are keyed on photo_id, shipments upsert on order_guid.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from typing import Any, Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
    guid         TEXT PRIMARY KEY,         -- event guid (dedup key)
    action       TEXT,
    order_guid   TEXT,
    occurred_at  TEXT,                      -- SD event ordering timestamp
    received_at  REAL,                      -- our epoch receive time
    payload      TEXT                       -- raw JSON as received
);

CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_guid   TEXT UNIQUE,              -- 1:1 with the accepted event (idempotent enqueue)
    action       TEXT NOT NULL,
    order_guid   TEXT,
    occurred_at  TEXT,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending|processing|done|failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_queue_status ON queue(status, occurred_at, id);

CREATE TABLE IF NOT EXISTS shipments (
    order_guid   TEXT PRIMARY KEY,
    number       TEXT,
    status       TEXT,
    picked_up_at TEXT,
    details      TEXT,                      -- full get-order JSON
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS vins (
    order_guid   TEXT NOT NULL,
    vin          TEXT NOT NULL,
    PRIMARY KEY (order_guid, vin)
);

CREATE TABLE IF NOT EXISTS photos (
    photo_id     TEXT PRIMARY KEY,         -- SD photo id (idempotency key)
    order_guid   TEXT NOT NULL,
    vin          TEXT,
    step         TEXT,                      -- pickup | delivery
    subject      TEXT,                      -- what it depicts
    taken_at     TEXT,
    latitude     REAL,
    longitude    REAL,
    source_url   TEXT,                      -- the (time-limited) file URL we fetched
    local_path   TEXT,                      -- where we saved the bytes
    downloaded_at REAL
);
CREATE INDEX IF NOT EXISTS ix_photos_order ON photos(order_guid);

-- One row per ORDER: the VIN/NO VIN decision is per-shipment, and the tags are
-- applied to the whole order. Idempotent on order_guid (skip re-tagging on a
-- redelivered webhook).
CREATE TABLE IF NOT EXISTS tags (
    order_guid   TEXT PRIMARY KEY,
    vin_result   TEXT,                      -- 'VIN' | 'NO VIN'
    applied_tags TEXT,                      -- JSON list of tags actually applied
    detail       TEXT,                      -- JSON: per-VIN found map, photo count
    tagged_at    REAL
);

CREATE TABLE IF NOT EXISTS ui_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_guid   TEXT,
    kind         TEXT,                      -- e.g. picked_up | photos_tagged
    payload      TEXT,
    created_at   REAL
);

-- Single-row circuit breaker for the SD WEB session. When the worker can't get
-- logged in (a captcha/2FA a human must clear, or a transient vault error) it trips
-- this gate: web/BOL items stay queued (parked, attempts NOT burned) and the worker
-- stops hammering login until the session is restored. API-only events keep flowing.
CREATE TABLE IF NOT EXISTS auth_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    blocked      INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,                      -- captcha | 2fa | no_creds | vault_error | unknown
    detail       TEXT,
    blocked_at   REAL,                      -- when it FIRST tripped (kept across re-trips)
    updated_at   REAL
);
INSERT OR IGNORE INTO auth_state (id, blocked, updated_at) VALUES (1, 0, 0);
"""


def connect() -> sqlite3.Connection:
    """Open a connection with WAL + a busy timeout so the listener and worker can
    both write without 'database is locked'. Caller closes (or use a `with` block)."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    """Create the schema if it doesn't exist. Safe to call on every process start."""
    with connect() as conn:
        conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------
# Listener path: dedup + enqueue, in ONE transaction.
# --------------------------------------------------------------------------
def accept_event(*, guid: str, action: str, order_guid: Optional[str],
                 occurred_at: Optional[str], raw_payload: str) -> bool:
    """Record the event and enqueue it, atomically. Returns True if newly accepted,
    False if it's a duplicate (already seen) — in which case nothing is enqueued.

    This is the listener's whole job besides token validation: idempotent by the
    event guid, so Super Dispatch's repeat deliveries don't double-queue work."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_events "
            "(guid, action, order_guid, occurred_at, received_at, payload) "
            "VALUES (?,?,?,?,?,?)",
            (guid, action, order_guid, occurred_at, now, raw_payload),
        )
        if cur.rowcount == 0:
            return False                      # duplicate delivery — drop it
        conn.execute(
            "INSERT OR IGNORE INTO queue "
            "(event_guid, action, order_guid, occurred_at, payload, "
            " status, created_at, updated_at) "
            "VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (guid, action, order_guid, occurred_at, raw_payload, now, now),
        )
        return True


# --------------------------------------------------------------------------
# Worker path: claim -> done/fail.
# --------------------------------------------------------------------------
def claim_next(exclude_actions: tuple[str, ...] = ()) -> Optional[sqlite3.Row]:
    """Atomically claim the oldest pending item (by occurred_at, then id) and mark
    it 'processing'. Returns the claimed row, or None if the queue is empty.

    `exclude_actions` skips those actions when claiming — the worker passes the
    web/BOL actions while the auth gate is tripped, so those items stay parked
    (pending) while API-only status events keep being processed.

    The UPDATE...WHERE status='pending' guard makes the claim safe even if more than
    one worker runs."""
    now = time.time()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if exclude_actions:
            ph = ",".join("?" * len(exclude_actions))
            row = conn.execute(
                f"SELECT * FROM queue WHERE status='pending' AND action NOT IN ({ph}) "
                "ORDER BY occurred_at IS NULL, occurred_at, id LIMIT 1",
                tuple(exclude_actions),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM queue WHERE status='pending' "
                "ORDER BY occurred_at IS NULL, occurred_at, id LIMIT 1"
            ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE queue SET status='processing', attempts=attempts+1, updated_at=? "
            "WHERE id=? AND status='pending'",
            (now, row["id"]),
        )
        conn.execute("COMMIT")
        return row


def mark_done(item_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE queue SET status='done', updated_at=? WHERE id=?",
                     (time.time(), item_id))


def mark_failed(item_id: int, error: str, *, max_attempts: int) -> str:
    """Record an error. If the item still has attempts left, return it to 'pending'
    so the worker retries it later; otherwise park it as 'failed'. Returns the new
    status."""
    now = time.time()
    with connect() as conn:
        row = conn.execute("SELECT attempts FROM queue WHERE id=?", (item_id,)).fetchone()
        attempts = row["attempts"] if row else max_attempts
        status = "pending" if attempts < max_attempts else "failed"
        conn.execute("UPDATE queue SET status=?, last_error=?, updated_at=? WHERE id=?",
                     (status, error[:1000], now, item_id))
        return status


def requeue(item_id: int, *, note: Optional[str] = None) -> None:
    """Return a claimed item to 'pending' WITHOUT counting the claim as an attempt
    (undoes claim_next's increment). Used when the auth gate trips: the item is
    parked, not failed, so it drains untouched once the session is restored."""
    with connect() as conn:
        conn.execute(
            "UPDATE queue SET status='pending', attempts=MAX(attempts-1, 0), "
            "last_error=?, updated_at=? WHERE id=?",
            (note, time.time(), item_id))


# --------------------------------------------------------------------------
# Auth gate (circuit breaker): single row, shared by the worker across polls.
# --------------------------------------------------------------------------
def set_auth_block(reason: str, detail: str = "") -> None:
    """Trip the gate. Keeps the original blocked_at across repeated trips."""
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE auth_state SET blocked=1, reason=?, detail=?, "
            "blocked_at=COALESCE(NULLIF(blocked_at, 0), ?), updated_at=? WHERE id=1",
            (reason, (detail or "")[:500], now, now))


def clear_auth_block() -> None:
    with connect() as conn:
        conn.execute("UPDATE auth_state SET blocked=0, reason=NULL, detail=NULL, "
                     "blocked_at=NULL, updated_at=? WHERE id=1", (time.time(),))


def auth_blocked() -> bool:
    with connect() as conn:
        r = conn.execute("SELECT blocked FROM auth_state WHERE id=1").fetchone()
        return bool(r and r["blocked"])


def auth_block_info() -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM auth_state WHERE id=1").fetchone()


def parked_web_guids(actions: tuple[str, ...]) -> list[str]:
    """order_guids of pending web items currently parked behind the gate (for logging)."""
    if not actions:
        return []
    ph = ",".join("?" * len(actions))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT order_guid FROM queue WHERE status='pending' AND action IN ({ph})",
            tuple(actions)).fetchall()
    return [r["order_guid"] for r in rows if r["order_guid"]]


# --------------------------------------------------------------------------
# State upserts (worker).
# --------------------------------------------------------------------------
def upsert_shipment(*, order_guid: str, number: Optional[str], status: Optional[str],
                    picked_up_at: Optional[str], details: dict,
                    vins: Optional[list[str]] = None) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO shipments (order_guid, number, status, picked_up_at, details, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(order_guid) DO UPDATE SET "
            "  number=excluded.number, status=excluded.status, "
            "  picked_up_at=excluded.picked_up_at, details=excluded.details, "
            "  updated_at=excluded.updated_at",
            (order_guid, number, status, picked_up_at, json.dumps(details), now),
        )
        for vin in (vins or []):
            if vin:
                conn.execute("INSERT OR IGNORE INTO vins (order_guid, vin) VALUES (?,?)",
                             (order_guid, vin))


def photo_exists(photo_id: str) -> bool:
    """True if this photo was already downloaded (idempotency: skip re-download)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM photos WHERE photo_id=? AND local_path IS NOT NULL",
            (photo_id,)).fetchone()
        return row is not None


def record_photo(*, photo_id: str, order_guid: str, vin: Optional[str], step: Optional[str],
                 subject: Optional[str], taken_at: Optional[str],
                 latitude: Optional[float], longitude: Optional[float],
                 source_url: Optional[str], local_path: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO photos (photo_id, order_guid, vin, step, subject, taken_at, "
            "  latitude, longitude, source_url, local_path, downloaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(photo_id) DO UPDATE SET "
            "  local_path=excluded.local_path, source_url=excluded.source_url, "
            "  downloaded_at=excluded.downloaded_at",
            (photo_id, order_guid, vin, step, subject, taken_at, latitude, longitude,
             source_url, local_path, time.time()),
        )


def order_tagged(order_guid: str) -> bool:
    """True if this order was already tagged (idempotency for the BOL event)."""
    with connect() as conn:
        return conn.execute("SELECT 1 FROM tags WHERE order_guid=?",
                            (order_guid,)).fetchone() is not None


def record_order_tagging(*, order_guid: str, vin_result: str, applied_tags: list[str],
                         detail: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO tags (order_guid, vin_result, applied_tags, detail, tagged_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(order_guid) DO UPDATE SET vin_result=excluded.vin_result, "
            "  applied_tags=excluded.applied_tags, detail=excluded.detail, "
            "  tagged_at=excluded.tagged_at",
            (order_guid, vin_result, json.dumps(applied_tags),
             json.dumps(detail, default=str), time.time()),
        )


# --------------------------------------------------------------------------
# UI push: append-only feed the listener streams over SSE (cross-process).
# --------------------------------------------------------------------------
def push_ui_event(*, order_guid: Optional[str], kind: str, payload: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO ui_events (order_guid, kind, payload, created_at) VALUES (?,?,?,?)",
            (order_guid, kind, json.dumps(payload, default=str), time.time()),
        )
        return int(cur.lastrowid)


def ui_events_after(last_id: int, limit: int = 100) -> list[sqlite3.Row]:
    """Rows with id > last_id (the SSE endpoint tails this)."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM ui_events WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit)).fetchall()


def ui_events_max_id() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ui_events").fetchone()
        return int(row["m"])


if __name__ == "__main__":
    init_db()
    print(f"Initialized {config.DB_PATH}")
