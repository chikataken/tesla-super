"""Monitoring + email alerts for direct-pickup-checks.

    python monitor.py summary      # daily digest (orders tagged in last 24h, etc.) -> email
    python monitor.py health       # probe services/tunnel/queue; email on DOWN / RECOVERED
    python monitor.py test-email    # send a test email to confirm SMTP works

`health` only emails on a STATE CHANGE (healthy->down, down->healthy), tracked in a
state file, so a 15-minute timer won't spam you. `summary` always sends.

SMTP config (put in the shared secrets/.env):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       587 (STARTTLS, default) or 465 (SSL)
    SMTP_USER       full email / username
    SMTP_PASS       password or app-password (Gmail: an App Password, not your login)
    ALERT_TO        where alerts go (comma-separated ok)
    ALERT_FROM      optional; defaults to SMTP_USER
"""
from __future__ import annotations
import glob
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

import config
import db

# Webhooks arrive via the shipment-creator tunnel (test.wastake.com -> :8001), whose
# app.py forwards /webhooks/* to the listener — the dedicated
# cloudflared-direct-pickup tunnel is retired.
SERVICES = ["cloudflared-shipment-creator", "shipment-creator-test-web",
            "direct-pickup-listener", "direct-pickup-worker"]
STATE_FILE = os.path.join(config.DATA_DIR, "monitor_state.json")


# --- email -----------------------------------------------------------------
def _send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    pw = os.getenv("SMTP_PASS", "")
    to = os.getenv("ALERT_TO", "").strip()
    frm = os.getenv("ALERT_FROM", "").strip() or user
    missing = [k for k, v in {"SMTP_HOST": host, "SMTP_USER": user,
               "SMTP_PASS": pw, "ALERT_TO": to}.items() if not v]
    if missing:
        raise RuntimeError("email not configured — missing " + ", ".join(missing))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(user, pw); s.send_message(msg)


# --- probes ----------------------------------------------------------------
def _svc_active(name: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", name],
                              timeout=10).returncode == 0
    except Exception:
        return False


def _http_alive(url: str, timeout: int = 10) -> bool:
    """True if the URL returns ANY HTTP response (even 4xx/5xx => server is up).
    False only on connection refused / DNS / timeout (server actually down)."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _check_problems() -> list[str]:
    problems = []
    for svc in SERVICES:
        if not _svc_active(svc):
            problems.append(f"service '{svc}' is not active")
    if not _http_alive(f"http://127.0.0.1:{config.LISTENER_PORT}{config.WEBHOOK_PATH}"):
        problems.append(f"listener not responding on 127.0.0.1:{config.LISTENER_PORT}")
    if config.TUNNEL_PUBLIC_URL and not _http_alive(config.TUNNEL_PUBLIC_URL + config.WEBHOOK_PATH, 15):
        problems.append(f"public tunnel {config.TUNNEL_PUBLIC_URL} not reachable")
    try:
        with db.connect() as c:
            stuck = c.execute("SELECT count(*) FROM queue WHERE status='processing' "
                              "AND updated_at < ?", (time.time() - 1800,)).fetchone()[0]
            if stuck:
                problems.append(f"{stuck} queue item(s) stuck 'processing' >30min (worker hung?)")
    except Exception as e:                          # noqa: BLE001
        problems.append(f"cannot read the queue DB: {e}")
    return problems


# --- commands --------------------------------------------------------------
# Sibling project logs (read-only; no cross-import per the project rule).
RECONCILE_LOG_DIR = "/home/mbdtf/projects/tesla-super/tesla-reconcile/output/logs"


def _recent_logs(prefix: str, hours: int = 24) -> list[str]:
    cutoff = time.time() - hours * 3600
    fs = [f for f in glob.glob(os.path.join(RECONCILE_LOG_DIR, prefix + "*.log"))
          if os.path.getmtime(f) >= cutoff]
    return sorted(fs, key=os.path.getmtime)


def _hhmm(path: str) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))


# --- section: direct-pickup-checks (always-on) -----------------------------
def _section_direct_pickup() -> tuple[str, int, int]:
    since = time.time() - 86400
    with db.connect() as c:
        rows = c.execute("SELECT vin_result, count(*) AS n FROM tags WHERE tagged_at>=? "
                         "GROUP BY vin_result", (since,)).fetchall()
        tagged = sum(r["n"] for r in rows)
        breakdown = ", ".join(f"{r['vin_result']}={r['n']}" for r in rows) or "none"
        events = c.execute("SELECT count(*) FROM seen_events WHERE received_at>=?", (since,)).fetchone()[0]
        failed = c.execute("SELECT count(*) FROM queue WHERE status='failed' AND updated_at>=?", (since,)).fetchone()[0]
        pending = c.execute("SELECT count(*) FROM queue WHERE status IN ('pending','processing')").fetchone()[0]
    up = sum(_svc_active(s) for s in SERVICES)
    body = (f"== direct-pickup-checks (always-on) ==\n"
            f"  Orders tagged : {tagged}  ({breakdown})\n"
            f"  Events recv'd : {events}\n"
            f"  Failed items  : {failed}\n"
            f"  Queue backlog : {pending}\n"
            f"  Services up   : {up}/{len(SERVICES)}")
    return body, tagged, failed


# --- section: tesla-reconcile (nightly runs + portal cleanup) ---------------
def _parse_reconcile(path: str) -> dict:
    t = open(path, errors="replace").read()
    err = re.search(r"(\d+)/\d+ shipment\(s\) errored", t)
    return {"crashed": "Traceback (most recent call last)" in t,
            "done": ("Done — closing the browser" in t) or bool(re.search(r"Pass \d+ complete", t)),
            "processed": len(re.findall(r"(?m)^\[\d+/\d+\]", t)),
            "marked": len(re.findall(r"(?m)^\s*-> set ", t)),
            "damage": len(re.findall(r"(?mi)^\s*-> set .*damage claim", t)),
            "no_vin": len(re.findall(r"(?mi)^\s*-> set .*no vin", t)),
            "errored": int(err.group(1)) if err else 0}


def _parse_cleanup(path: str) -> dict:
    t = open(path, errors="replace").read()
    done = re.search(r"Done\. Drivers assigned: (\d+).*?Pickup updated: (\d+)", t)
    return {"crashed": "Traceback (most recent call last)" in t,
            "mode": "APPLY" if "Mode: APPLY" in t else ("DRY-RUN" if "Mode:" in t else "?"),
            "done": bool(done),
            "drivers": int(done.group(1)) if done else 0,
            "pickups": int(done.group(2)) if done else 0}


# The nightly tesla-reconcile-run.timer slots; a run is "scheduled" if its START
# time (from the filename) is within tolerance of one of these.
SCHEDULED_RECONCILE = [(3, 0), (4, 30), (6, 0)]
_SCHED_TOL_MIN = 8


def _log_start(path: str):
    """Run START time from the reconcile_/cleanup_ filename (…YYYYMMDD-HHMMSS.log).
    The filename is stamped at run start; mtime is when it FINISHED."""
    m = re.search(r"_(\d{8})-(\d{6})\.log$", os.path.basename(path))
    return time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None


def _scheduled_slot(st):
    if not st:
        return None
    mins = st.tm_hour * 60 + st.tm_min
    for hh, mm in SCHEDULED_RECONCILE:
        if abs(mins - (hh * 60 + mm)) <= _SCHED_TOL_MIN:
            return (hh, mm)
    return None


def _fmt_run(r: dict) -> str:
    return (f"{r['processed']} processed, {r['marked']} marked "
            f"({r['damage']} damage, {r['no_vin']} no-VIN), {r['errored']} errored")


def _section_tesla_reconcile() -> tuple[str, int]:
    runs = _recent_logs("reconcile_")
    cleans = _recent_logs("cleanup_")
    crashed = 0
    sched, manual = {}, []
    for f in runs:
        slot = _scheduled_slot(_log_start(f))
        if slot and slot not in sched:
            sched[slot] = f
        else:
            manual.append(f)

    lines = ["== tesla-reconcile (nightly) =="]
    lines.append(f"  Scheduled runs (expected {len(SCHEDULED_RECONCILE)}):")
    for hh, mm in SCHEDULED_RECONCILE:
        label = f"{hh:02d}:{mm:02d}"
        f = sched.get((hh, mm))
        if not f:
            lines.append(f"    {label}  ⚠ MISSING — did not run")
            continue
        r = _parse_reconcile(f)
        if r["crashed"]:
            crashed += 1
            lines.append(f"    {label}  ✗ CRASHED — {os.path.basename(f)}")
        else:
            lines.append(f"    {label}  {'✓' if r['done'] else '…'} {_fmt_run(r)}")

    if manual:
        lines.append(f"  Manual runs: {len(manual)}")
        for f in manual:
            r = _parse_reconcile(f)
            if r["crashed"]:
                crashed += 1
                lines.append(f"    {_hhmm(f)}  ✗ CRASHED — {os.path.basename(f)}")
            else:
                lines.append(f"    {_hhmm(f)}  {'✓' if r['done'] else '…'} {_fmt_run(r)}")

    lines.append(f"  Portal cleanup (24h): {len(cleans)}")
    for f in cleans:
        c = _parse_cleanup(f)
        if c["crashed"]:
            crashed += 1
            lines.append(f"    {_hhmm(f)}  ✗ CRASHED — {os.path.basename(f)}")
        else:
            lines.append(f"    {_hhmm(f)}  ✓ {c['mode']} — drivers {c['drivers']}, pickups bumped {c['pickups']}")
    return "\n".join(lines), crashed


def _check_run_crashes(prev_alerted: list) -> tuple[list, list]:
    """Scan recent reconcile/cleanup logs for a Traceback. Alert ONCE per log
    (tracked in `prev_alerted`). Returns (newly-crashed basenames, pruned alerted set)."""
    prev = set(prev_alerted)
    window, new = set(), []
    for prefix in ("reconcile_", "cleanup_"):
        for f in _recent_logs(prefix, hours=12):
            bn = os.path.basename(f)
            window.add(bn)
            if bn in prev:
                continue
            if "Traceback (most recent call last)" in open(f, errors="replace").read():
                new.append(bn)
    # keep only entries still in the 12h window (+ new) so the set stays bounded
    return new, sorted((prev & window) | set(new))


def cmd_summary() -> None:
    # Simple daily digest: just the total VINs marked by each pipeline in the last 24h.
    # (Liveness/crash alerts are handled separately by cmd_health, not here.)
    since = time.time() - 86400
    with db.connect() as c:
        direct = c.execute("SELECT count(*) FROM tags WHERE tagged_at>=?", (since,)).fetchone()[0]
    reconcile = sum(_parse_reconcile(f)["marked"] for f in _recent_logs("reconcile_"))
    body = (f"Daily VIN digest — {time.strftime('%Y-%m-%d')}\n\n"
            f"  Direct pickup   : {direct} VIN(s) marked\n"
            f"  Tesla reconcile : {reconcile} VIN(s) marked\n")
    subj = f"[server] VINs marked — direct {direct}, reconcile {reconcile}"
    _send_email(subj, body)
    print("SUBJECT:", subj)
    print(body)


def cmd_health() -> None:
    state = {}
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        pass

    # (a) liveness of the always-on stack — up/down transition email
    problems = _check_problems()
    prev_down = bool(state.get("down"))
    now_down = bool(problems)
    if now_down and not prev_down:
        _send_email("[server] ⚠ DOWN (direct-pickup)",
                    "Problems detected:\n\n" + "\n".join("  - " + p for p in problems))
        print("ALERT sent (down):", problems)
    elif not now_down and prev_down:
        _send_email("[server] ✅ recovered", "All liveness checks passing again.")
        print("recovery email sent")
    else:
        print("liveness:", "ok" if not now_down else f"still down: {problems}")

    # (b) tesla-reconcile / portal-cleanup run crashes — one email per crashed log
    new_crashes, alerted = _check_run_crashes(state.get("alerted_crashes", []))
    if new_crashes:
        _send_email("[server] ⚠ tesla-reconcile run CRASHED",
                    "A run logged a Traceback (check the log on the server):\n\n"
                    + "\n".join("  - " + c for c in new_crashes))
        print("crash alert sent:", new_crashes)

    state.update({"down": now_down, "ts": time.time(),
                  "problems": problems, "alerted_crashes": alerted})
    try:
        json.dump(state, open(STATE_FILE, "w"))
    except Exception:
        pass


def cmd_test_email() -> None:
    _send_email("[direct-pickup] test email", "If you can read this, SMTP works.")
    print("test email sent to", os.getenv("ALERT_TO"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    {"summary": cmd_summary, "health": cmd_health, "test-email": cmd_test_email}.get(
        cmd, lambda: print(__doc__))()
