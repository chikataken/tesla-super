"""
Pull Tesla Load Tender emails from didi@tfitrans.com (Gmail API, read-only)
into the local tenders.db mirror. Idempotent: keyed on Gmail message id, so
re-runs skip already-recorded emails and a crashed run resumes cleanly.

    python tenders_ingest.py --sync      # incremental tick (the minute timer)
    python tenders_ingest.py             # sweep the last 7 days
    python tenders_ingest.py --days 30   # deeper backfill
    python tenders_ingest.py --refetch   # re-parse even already-seen ids

--sync uses Gmail's history API: it asks only for mailbox changes since the
last recorded historyId (sync_state row in tenders.db), so a quiet tick is one
tiny API call. First run — or when Gmail says the stored id is too old (~a
week, HTTP 404) — it falls back to a 2-day sweep and reseeds the cursor.
Driven every minute by systemd (tenders-sync.timer).

Auth: secrets/gmail_credentials.json (OAuth client) + secrets/didi_gmail_token.json
(refresh token, minted once via a browser consent). Read-only scope only.
"""
from __future__ import annotations
import argparse
import base64
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import tenders_db

_SECRETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "secrets")
CLIENT_FILE = os.path.join(_SECRETS_DIR, "gmail_credentials.json")
TOKEN_FILE = os.path.join(_SECRETS_DIR, "didi_gmail_token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

QUERY = 'from:SA-AppUser@tesla.com subject:"Tesla Load Tender"'


def gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
            print(">>> browser consent: sign in as didi@tfitrans.com <<<", flush=True)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
    return build("gmail", "v1", credentials=creds)


def _html_body(payload) -> str:
    """The tender's HTML part (tenders are multipart/related html+logo)."""
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        found = _html_body(part)
        if found:
            return found
    return ""


def _record(svc, con, mid: str, msg=None) -> bool:
    """Fetch (if needed), parse and store one message. True if recorded."""
    if msg is None:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    headers = {h["name"].lower(): h["value"]
               for h in msg["payload"].get("headers", [])}
    html = _html_body(msg["payload"])
    try:
        parsed = tenders_db.parse_tender(html)
    except ValueError as e:
        print(f"  PARSE FAIL {mid}: {e} (subject: {headers.get('subject')})")
        return False
    tenders_db.upsert_email(
        con, mid,
        sent_at=int(msg["internalDate"]) / 1000.0,
        subject=headers.get("subject", ""),
        recipients=headers.get("to", ""),
        raw_html=html, parsed=parsed)
    return True


def sync(con=None, svc=None) -> None:
    """One incremental tick: process only mailbox changes since the stored
    historyId. Quiet no-op when nothing new arrived."""
    svc = svc or gmail_service()
    con = con or tenders_db.connect()

    start = tenders_db.get_history_id(con)
    if not start:
        # First tick: capture the cursor BEFORE sweeping so anything arriving
        # mid-sweep is re-covered by the next tick (idempotent either way).
        seed = svc.users().getProfile(userId="me").execute()["historyId"]
        print(f"no sync cursor — seeding at historyId {seed} after a 2-day sweep")
        ingest(days=2, svc=svc, con=con)
        tenders_db.set_history_id(con, str(seed), "seeded")
        return

    new_hist, added, token = start, [], None
    try:
        while True:
            resp = svc.users().history().list(
                userId="me", startHistoryId=start, historyTypes=["messageAdded"],
                pageToken=token, maxResults=500).execute()
            new_hist = resp.get("historyId", new_hist)
            for h in resp.get("history", []):
                added += [m["message"]["id"] for m in h.get("messagesAdded", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
    except HttpError as e:
        if e.resp.status == 404:   # cursor older than Gmail keeps history
            print("history cursor expired — falling back to a 2-day sweep")
            seed = svc.users().getProfile(userId="me").execute()["historyId"]
            ingest(days=2, svc=svc, con=con)
            tenders_db.set_history_id(con, str(seed), "reseeded after 404")
            return
        raise

    import sd_events_db
    sd_con = None
    new = sd_new = 0
    for mid in dict.fromkeys(added):          # dedupe, keep order
        if tenders_db.have_gmail_id(con, mid):
            continue
        try:
            msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            if e.resp.status == 404:          # deleted before we fetched it
                continue
            raise
        hdrs = {h["name"].lower(): h["value"]
                for h in msg["payload"].get("headers", [])}
        sender = hdrs.get("from", "").lower()
        if ("sa-appuser@tesla.com" in sender
                and "tesla load tender" in hdrs.get("subject", "").lower()):
            if _record(svc, con, mid, msg):
                new += 1
                print(f"  recorded {hdrs.get('subject')}")
        elif "broker.updates@superdispatch.com" in sender:
            # every SD notification email -> sd_events.db (parsed + gzipped raw)
            sd_con = sd_con or sd_events_db.connect()
            if not sd_events_db.have(sd_con, mid):
                sd_events_db.record(sd_con, mid, int(msg["internalDate"]) / 1000.0,
                                    hdrs.get("subject", ""), _html_body(msg["payload"]))
                sd_new += 1
    tenders_db.set_history_id(con, str(new_hist),
                              f"+{new} tenders, +{sd_new} sd events of {len(added)} msgs")
    print(f"sync ok: {len(added)} mailbox additions, {new} tenders + {sd_new} sd events "
          f"recorded (history {start} -> {new_hist})")


def ingest(days: int, refetch: bool = False, svc=None, con=None) -> None:
    svc = svc or gmail_service()
    con = con or tenders_db.connect()

    ids, token = [], None
    q = f"{QUERY} newer_than:{days}d"
    while True:
        resp = svc.users().messages().list(
            userId="me", q=q, pageToken=token, maxResults=500).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    print(f"{len(ids)} tender emails match: {q}")

    new = skipped = failed = 0
    for i, mid in enumerate(ids, 1):
        if not refetch and tenders_db.have_gmail_id(con, mid):
            skipped += 1
            continue
        if _record(svc, con, mid):
            new += 1
        else:
            failed += 1
        if new and new % 50 == 0:
            print(f"  [{i}/{len(ids)}] {new} recorded…")

    n_shp = con.execute("SELECT COUNT(DISTINCT shp) FROM tender_emails").fetchone()[0]
    n_email = con.execute("SELECT COUNT(*) FROM tender_emails").fetchone()[0]
    n_vins = con.execute("SELECT COUNT(*) FROM tender_vins").fetchone()[0]
    n_cur = con.execute("SELECT COUNT(*) FROM current_vins").fetchone()[0]
    print(f"\nrecorded {new} new ({skipped} already stored, {failed} parse failures)")
    print(f"db totals: {n_email} emails, {n_shp} shipments, "
          f"{n_vins} vin rows ({n_cur} current) -> {tenders_db.DB_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true",
                    help="incremental tick via Gmail history API (the timer mode)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--refetch", action="store_true",
                    help="re-parse emails already in the db")
    args = ap.parse_args()
    try:
        if args.sync:
            sync()
        else:
            ingest(args.days, args.refetch)
    except KeyboardInterrupt:
        sys.exit("interrupted — rerun to resume (idempotent)")
