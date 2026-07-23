"""
Backfill SuperDispatch notification emails into sd_events.db.

    python sd_events_ingest.py --days 7          # sweep the last N days
    python sd_events_ingest.py --after 2026/01/01  # sweep since a date

Idempotent on Gmail message id — reruns skip stored mail, an interrupted run
resumes. LIVE recording is not here: tenders_ingest.py --sync (the minute
timer) records SD emails as they arrive, sharing its Gmail history feed.
Auth: the same secrets/ OAuth client + didi token as the tender sync.
"""
from __future__ import annotations
import argparse
import sys
import time

import sd_events_db
from tenders_ingest import gmail_service, _html_body

QUERY = "from:broker.updates@superdispatch.com"


def ingest(query: str) -> None:
    svc = gmail_service()
    con = sd_events_db.connect()
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=query,
                                           pageToken=token, maxResults=500).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    print(f"{len(ids)} SD emails match: {query}", flush=True)

    new = skipped = 0
    t0 = time.time()
    from collections import Counter
    types = Counter()
    for i, mid in enumerate(ids, 1):
        if sd_events_db.have(con, mid):
            skipped += 1
            continue
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        types[sd_events_db.record(
            con, mid, int(msg["internalDate"]) / 1000.0,
            headers.get("subject", ""), _html_body(msg["payload"]))] += 1
        new += 1
        if new % 500 == 0:
            rate = new / max(time.time() - t0, 1) * 60
            print(f"  [{i}/{len(ids)}] {new} recorded ({rate:.0f}/min)", flush=True)
    n = con.execute("SELECT COUNT(*) FROM sd_events").fetchone()[0]
    print(f"\nrecorded {new} new ({skipped} already stored) -> {sd_events_db.DB_PATH}")
    print(f"db total: {n} events; this run by type: {dict(types.most_common())}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--after", help="Gmail date, e.g. 2026/01/01")
    args = ap.parse_args()
    q = QUERY
    if args.days:
        q += f" newer_than:{args.days}d"
    elif args.after:
        q += f" after:{args.after}"
    else:
        sys.exit("pass --days N or --after YYYY/MM/DD")
    try:
        ingest(q)
    except KeyboardInterrupt:
        sys.exit("interrupted — rerun to resume (idempotent)")
