"""Retention cleanup for direct-pickup-checks (run weekly).

Prunes the high-volume / low-value data older than DPC_RETENTION_DAYS:
  * downloaded photo FILES (the biggest disk consumer) + their `photos` rows
  * `seen_events` (stores the raw event payload — bulk of the DB)
  * `ui_events` (the SSE feed history)
  * finished `queue` rows (status done|failed)
then VACUUMs to actually shrink the file.

KEEPS the small per-order record tables — `shipments`, `vins`, `tags` — they're
one row per order, are the audit trail, and `tags` backs the "already tagged"
idempotency check. (Webhook redelivery only happens within minutes, so pruning the
event-dedup rows after weeks is safe.)

    python cleanup.py            # prune using DPC_RETENTION_DAYS
    python cleanup.py --days 60
    python cleanup.py --dry-run  # report what WOULD be removed, change nothing
"""
from __future__ import annotations
import argparse
import os
import time

import config
import db
from logging_setup import setup, get_logger

setup("cleanup")
log = get_logger(__name__)


def _prune_empty_dirs(root: str) -> None:
    if not os.path.isdir(root):
        return
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def cleanup(days: int, dry_run: bool = False) -> dict:
    cutoff = time.time() - days * 86400
    stats: dict = {}
    conn = db.connect()
    try:
        # 1) Photo files (then rows). Delete bytes first so a crash can't orphan files.
        rows = conn.execute(
            "SELECT local_path FROM photos WHERE downloaded_at IS NOT NULL AND downloaded_at < ?",
            (cutoff,)).fetchall()
        files_removed = 0
        for r in rows:
            lp = r["local_path"]
            if lp and os.path.exists(lp):
                if dry_run:
                    files_removed += 1
                else:
                    try:
                        os.remove(lp)
                        files_removed += 1
                    except OSError:
                        pass
        stats["photo_files"] = files_removed
        stats["photo_rows"] = len(rows)

        # 2) Bulk bookkeeping tables, by age.
        def count_del(table: str, where: str, params: tuple) -> int:
            n = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]
            if n and not dry_run:
                conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            return n

        if not dry_run and rows:
            conn.execute("DELETE FROM photos WHERE downloaded_at IS NOT NULL AND downloaded_at < ?",
                         (cutoff,))
        stats["seen_events"] = count_del("seen_events", "received_at < ?", (cutoff,))
        stats["ui_events"] = count_del("ui_events", "created_at < ?", (cutoff,))
        stats["queue"] = count_del("queue",
                                   "status IN ('done','failed') AND updated_at < ?", (cutoff,))
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    if not dry_run:
        _prune_empty_dirs(config.PHOTO_DIR)
        # VACUUM can't run in a transaction and needs its own connection. Best-effort:
        # if the listener/worker hold a write lock, skip rather than fail the run.
        try:
            vc = db.connect()
            try:
                vc.execute("VACUUM")
            finally:
                vc.close()
            stats["vacuumed"] = True
        except Exception as e:               # noqa: BLE001
            stats["vacuumed"] = f"skipped ({e})"

    log.info("cleanup complete", extra={"days": days, "dry_run": dry_run, **stats})
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prune old direct-pickup-checks data + photos.")
    ap.add_argument("--days", type=int, default=config.DPC_RETENTION_DAYS,
                    help=f"retention window in days (default {config.DPC_RETENTION_DAYS})")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    a = ap.parse_args()
    s = cleanup(a.days, a.dry_run)
    print(("DRY-RUN: " if a.dry_run else "") + f"retention {a.days}d -> " +
          ", ".join(f"{k}={v}" for k, v in s.items()))
