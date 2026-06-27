"""
Daily terminal sync (run by the systemd timer at 13:30 local).

ORDER MATTERS — our local changes go first so they win and persist:
  1) PUSH  every locally-edited SD terminal back to SuperDispatch (terminals_push --apply).
           If nothing is pending it does NOT even open Chrome — a true no-op.
  2) PULL  the full catalog back into the local cache (terminals_scrape full refresh),
           which preserves any still-unpushed local edits.

Both halves auto-relogin to SuperDispatch from Vaultwarden if the shared Chrome is logged
out (sd_login.ensure_session) — same as every other tool here. Safe to run by hand anytime.

Run:  python terminals_sync.py
"""
from __future__ import annotations
import traceback

import terminals_db as tdb
import terminals_push
import terminals_scrape


def main() -> None:
    tdb.init_db()
    print("=== terminal sync: PUSH phase ===", flush=True)
    try:
        res = terminals_push.main(apply=True)
        print(f"push summary: {res}", flush=True)
    except Exception:
        # A push failure must NOT abort the pull — the pull is the safety net that keeps the
        # cache current. Log loudly and continue.
        print("PUSH FAILED (continuing to pull):", flush=True)
        traceback.print_exc()

    print("\n=== terminal sync: PULL phase ===", flush=True)
    terminals_scrape.main()
    print("\n=== terminal sync: done ===", flush=True)


if __name__ == "__main__":
    main()
