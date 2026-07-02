"""
Tiny status dashboard for the Tesla drop-off service (served at app.wastake.com).

Shows, with no external deps (stdlib http.server only):
  * whether the service is running and when it was last active,
  * what it's marking RIGHT NOW (the in-flight VIN, live from the log),
  * a live activity log (tail of out/service.log, written by app_drive.log()),
  * the history of past marks (every drop-off from dropoffs.db).

    python dashboard.py [--port 8011]
The cloudflared tunnel routes app.wastake.com -> 127.0.0.1:8011 (see README).
"""
from __future__ import annotations
import argparse
import datetime
import glob
import html
import json
import os
import re
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dropoffs.db")
LOGFILE = os.environ.get("DRIVE_LOG_FILE", os.path.join(HERE, "out", "service.log"))
LOG_TAIL = 60               # log lines to surface
ALIVE_SECONDS = 180         # "running" if a worker process exists OR the log moved this recently

# The SD recorder mirror (shipment-creator's DB) — source of the "Didi delivered list".
_REC_DB = os.environ.get("RECORDER_DB",
                         os.path.join(HERE, "..", "shipment-creator", "data", "recorder.db"))
_DELIVERED_TTL = 60                        # recompute the delivered list at most this often
_delivered_cache = {"at": 0.0, "data": []}

_DONE_RE = re.compile(r"ledger:|ledger\(pickup\):|queue empty|queues empty|nothing to do|nothing to pick")
_VIN_RE = re.compile(r"(?:unit VIN|verifying)\s+([A-HJ-NPR-Z0-9]{17})")
# Idle / heartbeat lines NOT shown in the dashboard log — only show actual marking work.
_HIDE_RE = re.compile(r"nothing to (do|pick)|queues? empty|waiting for new|service up|"
                      r"emulator unavailable|both queues empty", re.I)

# --- live step tracker: fed by app_drive.step() lines "STEP <flow> <n>/5 <label> vin= shp=" ---
# The 5 canonical major steps per flow (labels drive the tracker UI, order = step 1..5).
STEPS = {
    "pickup":  ["Select shipment", "Verify units", "Load", "Set ETA", "Depart"],
    "dropoff": ["Open shipment", "Decode photos", "Upload photos", "Confirm", "Commit"],
}
FLOW_LABEL = {"pickup": "Pick Up", "dropoff": "Drop Off"}
_STEP_RE = re.compile(r"STEP (pickup|dropoff) ([1-5])/5 (\S+) vin=(\S*) shp=(\S*)")
# Per-(flow, step) "taking longer than usual" budget in seconds — photo decode + the ~2-min
# loading timer legitimately run long, so they get a bigger budget before we flag a stall.
_STALL = {("pickup", 3): 210, ("pickup", 4): 90, ("pickup", 5): 150,
          ("dropoff", 2): 300, ("dropoff", 3): 240, ("dropoff", 4): 120}
_STALL_DEFAULT = 90
# A flow's commit line (fires right after step 5) — marks the mark as DONE, not stuck.
_COMPLETE_RE = re.compile(r"ledger:|ledger\(pickup\):")


def _service_running() -> bool:
    """True if the app_drive worker process is alive (scan /proc, no deps). Matches the
    real python interpreter running app_drive.py --watch — NOT a shell wrapper or grep
    that merely mentions it on its command line."""
    for d in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(d, "rb") as fh:
                argv = [p.decode("utf-8", "ignore") for p in fh.read().split(b"\0") if p]
        except OSError:
            continue
        if (argv and "python" in os.path.basename(argv[0])
                and any(a.endswith("app_drive.py") for a in argv) and "--watch" in argv):
            return True
    return False


def _tail(path: str, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except OSError:
        return []


def _parse_ts(line: str):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return datetime.datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _history(limit: int = 300) -> list[dict]:
    """Unified marks: Drop Off (dropoffs table) + Pick Up (pickups table), newest first."""
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB)
    rows = []
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(dropoffs)")}
        sd = ", sd_number, sd_delivered_at" if {"sd_number", "sd_delivered_at"} <= have else ""
        for row in con.execute(
                "SELECT vin, model, shipment, option, photographed, dropped_at, "
                "exterior, vin_found, key_found" + sd + " FROM dropoffs"):
            if sd:
                v, m, s, o, p, t, ext, vf, kf, sdn, sdd = row
            else:
                v, m, s, o, p, t, ext, vf, kf = row
                sdn = sdd = None
            rows.append({"action": "Drop Off", "vin": v, "model": m, "shipment": s,
                         "exterior": ext, "vin_found": vf, "key_found": kf,
                         "at": t, "when": _fmt_when(t), "check": _validate_link(s, sdn, sdd)})
    except sqlite3.Error:
        pass
    try:
        for v, m, s, e, t in con.execute(
                "SELECT vin, model, shipment, eta, picked_at FROM pickups"):
            rows.append({"action": "Pick Up", "vin": v, "model": m, "shipment": s,
                         "exterior": None, "vin_found": None, "key_found": None,
                         "at": t, "when": _fmt_when(t)})
    except sqlite3.Error:
        pass
    try:
        for s, v, stage, detail, t in con.execute(
                "SELECT shipment, vin, stage, detail, seen_at FROM api_errors"):
            rows.append({"action": "API ERROR", "vin": v or "", "model": detail or stage,
                         "shipment": s, "exterior": None, "vin_found": None,
                         "key_found": None, "at": t, "when": _fmt_when(t)})
    except sqlite3.Error:
        pass
    con.close()
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows[:limit]


def _app_marked() -> dict:
    """{(order_guid, vin): tesla_shipment} for every drop-off our automation recorded.
    Keyed on the (GUID, VIN) pair (GUID pins the SD order, VIN pins the vehicle); the value
    is the Tesla driver-app shipment id (e.g. 'SHP2606-A56J733'), whose order-name we use to
    validate that the SD order we linked is actually the right one (see _delivered)."""
    if not os.path.exists(DB):
        return {}
    con = sqlite3.connect(DB)
    try:
        return {(g, v): (s or "") for g, v, s in con.execute(
            "SELECT order_guid, vin, shipment FROM dropoffs "
            "WHERE order_guid IS NOT NULL AND order_guid != ''")}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def order_base(s: str) -> str:
    """The 7-char Tesla order base from a shipment/order string: drop a leading 'SHPxxxx-'
    batch prefix and any leading junk, then take the first 7 chars of the first alphanumeric
    token. Normalizes 'SHP2606-A56J733', '-AU49200', 'AU49200 direct', 'A56J733-3',
    'A54Q241vip' all to their 7-char core so they compare equal."""
    if not s:
        return ""
    s = re.sub(r'^SHP\w*-', '', s.strip(), flags=re.I)
    m = re.search(r'[A-Za-z0-9]+', s)
    return m.group(0)[:7].upper() if m else ""


def _within_days(ts: str, days: int) -> bool:
    """True if the SD delivery date is within `days` CALENDAR days of the current day (local) —
    'within 1 week of the current day'. ts is SD's delivery date (tz-aware, e.g. +0000)."""
    if not ts:
        return False
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.datetime.strptime(ts, fmt).astimezone().date()
            return abs((datetime.date.today() - d).days) <= days
        except ValueError:
            continue
    return False


def _validate_link(shipment: str, sd_number: str, sd_delivered_at: str):
    """Validate that the SD order a drop-off linked is the right one for the shipment the
    driver app actually marked (stored on the drop-off at marking time). Returns 'error'
    when the order-name doesn't match AND that order's delivery is >7 days old; None when
    the name matches, the delivery is recent, or there's nothing to check (old row)."""
    if not sd_number:
        return None                                   # nothing fetched to compare (pre-migration row)
    if order_base(shipment) and order_base(shipment) == order_base(sd_number):
        return None                                   # order-name matches (primary check)
    if _within_days(sd_delivered_at, 7):
        return None                                   # last resort: delivered within a week
    return "error"                                    # name mismatch AND stale -> wrong SD order linked


def _fmt_when(ts: str) -> str:
    """Any ISO timestamp -> 'JUL-01 2:30PM' (local). Handles tz-aware (SD delivery, e.g.
    +0000 -> converted to local) and naive-local (our ledger) forms alike."""
    if not ts:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(ts, fmt)
        except ValueError:
            continue
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%b-%d %-I:%M%p").upper()
    return ts[:16]


def _delivered(limit: int = 800) -> list[dict]:
    """The 'Didi delivered list': every delivered vehicle from the SD recorder mirror —
    delivered/invoiced/paid orders (those with a real delivery.completed_at), UNSCOPED
    (Didi = all users, no per-dispatcher state filter). Newest delivery first; each VIN
    flagged app=True when our automation dropped it off. Cached (_DELIVERED_TTL) since the
    delivered history changes slowly and parsing every order's details JSON is not cheap."""
    now = datetime.datetime.now().timestamp()
    if _delivered_cache["data"] and now - _delivered_cache["at"] < _DELIVERED_TTL:
        return _delivered_cache["data"]
    rows = []
    if os.path.exists(_REC_DB):
        marked = _app_marked()
        try:
            con = sqlite3.connect(f"file:{_REC_DB}?mode=ro", uri=True)
            for number, api_guid, dcity, dstate, vins_json, details in con.execute(
                    "SELECT number, api_guid, delivery_city, delivery_state, vins, details FROM orders "
                    "WHERE status IN ('delivered','invoiced','paid') "
                    "AND details IS NOT NULL AND details != ''"):
                try:
                    det = json.loads(details)
                except (ValueError, TypeError):
                    continue
                cat = (det.get("delivery") or {}).get("completed_at")
                if not cat:
                    continue
                models = {v.get("vin"): (v.get("model") or "") for v in (det.get("vehicles") or [])}
                try:
                    vins = json.loads(vins_json or "[]")
                except (ValueError, TypeError):
                    vins = []
                dest = ", ".join(p for p in (dcity, dstate) if p)
                for vin in vins:
                    ship = marked.get((api_guid, vin))          # Tesla shipment id if we marked it
                    if ship is None:
                        status = "delivered"                    # not one of ours
                    elif order_base(ship) and order_base(ship) == order_base(number):
                        status = "app"                          # order-name matches (primary check)
                    elif _within_days(cat, 7):
                        status = "app"                          # last resort: delivered within a week
                    else:
                        status = "error"                        # name mismatch AND stale -> wrong SD order linked
                    rows.append({"when": _fmt_when(cat), "sort": cat, "vin": vin,
                                 "model": models.get(vin, ""), "dest": dest,
                                 "number": number, "status": status})
            con.close()
        except sqlite3.Error:
            pass
        rows.sort(key=lambda r: r["sort"], reverse=True)
        rows = rows[:limit]
    _delivered_cache["at"] = now
    _delivered_cache["data"] = rows
    return rows


def _current(log_lines: list[str]) -> str | None:
    """The VIN being marked right now (drop-off 'unit VIN X' or pickup 'verifying X')
    with no later ledger/idle line — i.e. not yet committed."""
    last_vin = None
    for ln in log_lines:
        mm = _VIN_RE.search(ln)
        if mm:
            last_vin = mm.group(1)
        if "ledger" in ln and last_vin and last_vin in ln:
            last_vin = None
        elif _DONE_RE.search(ln) and "ledger" not in ln:
            last_vin = None
    return last_vin


def _now(raw_lines: list[str]) -> dict:
    """Live step-tracker state from the RAW log tail (STEP + idle lines both matter, so
    this scans the unfiltered lines). The most recent STEP marker sets flow/step/label;
    vin & shipment are STICKY within a flow run (later markers may omit them). An idle
    line after the last marker means the worker went quiet -> clear to idle (flow=None)."""
    flow = label = None
    step = None
    vin = shp = ""
    started = None
    done = False
    for ln in raw_lines:
        m = _STEP_RE.search(ln)
        if m:
            f, n, lab, v, s = m.group(1), int(m.group(2)), m.group(3), m.group(4), m.group(5)
            if f != flow or n == 1:             # new flow run OR a new shipment -> reset sticky id
                vin = shp = ""
            flow, step, label, done = f, n, lab, False
            if v:
                vin = v
            if s:
                shp = s
            started = _parse_ts(ln) or started
        elif flow and _HIDE_RE.search(ln):      # worker went idle after the last marker -> reset
            flow = label = None
            step = None
            vin = shp = ""
            started = None
            done = False
        elif flow and not done and _COMPLETE_RE.search(ln):   # this mark committed (ledger written)
            done = True
    if not flow:
        return {"flow": None}
    age = int((datetime.datetime.now() - started).total_seconds()) if started else None
    thr = _STALL.get((flow, step), _STALL_DEFAULT)
    return {
        "flow": flow, "flow_label": FLOW_LABEL[flow], "steps": STEPS[flow],
        "step": step, "label": label, "vin": vin, "shp": shp, "model": "", "done": done,
        "step_started_at": started.isoformat(timespec="seconds") if started else None,
        "step_age_s": age,
        "stalled": (not done) and age is not None and age > thr,   # a committed mark is never "stalled"
    }


SECTIONS = (("Exterior", "sides"), ("VIN plate", "vin_plate"), ("Key", "key"))
OUT = os.path.join(HERE, "out")


def _label_for(sec: str, fname: str) -> str:
    base = os.path.splitext(fname)[0]
    if sec == "sides":                       # "0_front" -> "Front", "2_front_left" -> "Front Left"
        parts = base.split("_", 1)
        name = parts[1] if len(parts) > 1 and parts[0].isdigit() else base
        return name.replace("_", " ").title()
    return {"vin_plate": "VIN plate", "key": "Key"}.get(sec, base)


def photos_for(vin: str) -> dict:
    """The photos that were used for a VIN's drop-off, grouped + labeled by section."""
    out = {"vin": vin, "sections": []}
    for title, sec in SECTIONS:
        d = os.path.join(OUT, vin, sec)
        pics = []
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    pics.append({"label": _label_for(sec, f),
                                 "url": f"img?vin={vin}&sec={sec}&file={urllib.parse.quote(f)}"})
        out["sections"].append({"name": title, "photos": pics})
    return out


def _fmt_log_line(ln: str) -> str:
    """Rewrite the leading ISO timestamp to 'June 28, 2026 - 9:37 PM  <message>'."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(.*)", ln)
    if not m:
        return ln
    try:
        t = datetime.datetime.fromisoformat(m.group(1))
    except ValueError:
        return ln
    return f"{t.strftime('%B %-d, %Y - %-I:%M %p')}  {m.group(2)}"


def snapshot() -> dict:
    raw = _tail(LOGFILE, 800)
    # Human log: real marking work only — drop idle heartbeats AND the machine STEP markers.
    log_lines = [ln for ln in raw
                 if ln.strip() and not _HIDE_RE.search(ln) and not _STEP_RE.search(ln)]
    last_ts = next((t for t in (_parse_ts(ln) for ln in reversed(log_lines)) if t), None)
    age = (datetime.datetime.now() - last_ts).total_seconds() if last_ts else None
    proc_alive = _service_running()
    history = _history()
    now = _now(raw)                                   # live step tracker (uses the raw tail)
    if now.get("flow") and now.get("vin") and not now.get("model"):
        now["model"] = next((h["model"] for h in history
                             if h["vin"] == now["vin"] and h.get("model")), "")
    today = datetime.date.today().isoformat()
    return {
        "running": proc_alive,
        "log_fresh": age is not None and age < ALIVE_SECONDS,
        "last_seen": last_ts.isoformat(timespec="seconds") if last_ts else None,
        "last_seen_age_s": int(age) if age is not None else None,
        "now": now,
        "current_vin": _current(log_lines),
        "log": [_fmt_log_line(ln) for ln in log_lines[-LOG_TAIL:]],
        "stats": {"total": len(history),
                  "today": sum(1 for h in history if (h["at"] or "").startswith(today)),
                  "pickups": sum(1 for h in history if h["action"] == "Pick Up"),
                  "dropoffs": sum(1 for h in history if h["action"] == "Drop Off"),
                  "api_errors": sum(1 for h in history if h["action"] == "API ERROR")},
        "history": history,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Tesla Drop-Off Service</title>
<style>
 :root{--bg:#f6f8fa;--card:#ffffff;--bd:#d0d7de;--fg:#1f2328;--mut:#656d76;--ok:#1a7f37;--bad:#cf222e;--acc:#0969da}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
 .wrap{max-width:1040px;margin:0 auto;padding:20px}
 h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:18px}
 .row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;flex:1;min-width:150px;
       box-shadow:0 1px 2px rgba(31,35,40,.04)}
 .card .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
 .card .v{font-size:22px;font-weight:600;margin-top:4px}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
 .on{background:var(--ok)}.off{background:var(--bad)}
 .now{background:linear-gradient(90deg,#ddf4ff,#ffffff);border-color:#54aeff}
 .now .v{color:var(--acc);font-family:ui-monospace,monospace;font-size:18px}
 /* live step tracker (hero) */
 .hero.idle{background:linear-gradient(90deg,#f6f8fa,#fff)}
 .hero.pickup{background:linear-gradient(90deg,#ddf4ff,#fff);border-color:#54aeff}
 .hero.dropoff{background:linear-gradient(90deg,#fff,#dafbe1);border-color:#4ac26b}
 .hero .htop{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:16px}
 .fbadge{padding:2px 11px;border-radius:20px;font-size:12px;font-weight:700;color:#fff}
 .fbadge.pickup{background:var(--acc)}.fbadge.dropoff{background:#2da44e}
 .hero .vin{font-family:ui-monospace,monospace;font-size:16px;font-weight:600}
 .hero .shp{color:var(--mut);font-size:13px}
 .stall{margin-left:auto;background:#fff8c5;color:#9a6700;font-weight:600;padding:2px 10px;border-radius:20px;font-size:12px}
 .donebadge{margin-left:auto;background:#dafbe1;color:var(--ok);font-weight:700;padding:2px 11px;border-radius:20px;font-size:12px}
 .track{display:flex;align-items:flex-start}
 .st{flex:1;display:flex;flex-direction:column;align-items:center;text-align:center;position:relative}
 .st::before{content:"";position:absolute;top:15px;left:-50%;width:100%;height:3px;background:var(--bd);z-index:0}
 .st:first-child::before{display:none}
 .st.done::before,.st.active::before{background:var(--ok)}
 .st .num{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;
   font-size:13px;font-weight:700;background:#fff;border:3px solid var(--bd);color:var(--mut);z-index:1;position:relative}
 .st.done .num{background:var(--ok);border-color:var(--ok);color:#fff}
 .st.active .num{border-color:var(--acc);color:var(--acc);box-shadow:0 0 0 4px rgba(9,105,218,.15);animation:pulse 1.5s infinite}
 @keyframes pulse{0%,100%{box-shadow:0 0 0 4px rgba(9,105,218,.16)}50%{box-shadow:0 0 0 8px rgba(9,105,218,.04)}}
 .st .lbl{font-size:11px;margin-top:7px;color:var(--mut);max-width:92px;line-height:1.25}
 .st.active .lbl{color:var(--fg);font-weight:600}
 .st .el{font-size:10px;color:var(--acc);margin-top:2px;font-variant-numeric:tabular-nums}
 details#logwrap{margin-top:8px}
 details#logwrap>summary{cursor:pointer;color:var(--mut);font-size:13px;user-select:none;padding:4px 0}
 /* prod (?hide_activity=1) keeps the App tab + the rich hero tracker, drops only the raw log — see do_GET */
 .noactivity #liveact,.noactivity #logwrap{display:none}
 h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:22px 0 8px}
 pre{background:#ffffff;border:1px solid var(--bd);border-radius:8px;padding:12px;overflow:auto;max-height:330px;
     font:12px/1.5 ui-monospace,monospace;color:#1f2328;white-space:pre-wrap;word-break:break-word}
 table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
 th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--bd)}
 th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase}
 td.vin{font-family:ui-monospace,monospace}
 .pill{padding:1px 8px;border-radius:20px;font-size:11px;white-space:nowrap}
 .pill.y{background:#dafbe1;color:var(--ok)}.pill.n{background:#ffebe9;color:var(--bad)}
 .pill.p{background:#ddf4ff;color:var(--acc)}.pill.d{background:#fbefff;color:#8250df}
 .pill.w{background:#fff8c5;color:#9a6700;font-weight:600}
 .pill.e{background:#ffebe9;color:var(--bad);font-weight:600}
 .pill.dl{background:#dafbe1;color:var(--ok)}
 .pill.err{background:var(--bad);color:#fff;font-weight:700}
 .histhead{display:flex;align-items:center;gap:8px;margin:22px 0 8px}
 .htab{background:#fff;border:1px solid var(--bd);color:var(--mut);border-radius:8px;padding:5px 14px;font-size:13px;font-weight:600;cursor:pointer}
 .htab.on{background:var(--acc);color:#fff;border-color:var(--acc)}
 .mut{color:var(--mut)}
 tbody tr{cursor:pointer}tbody tr:hover{background:#f6f8fa}
 .modal{display:none;position:fixed;inset:0;background:rgba(31,35,40,.5);z-index:10;
        align-items:flex-start;justify-content:center;padding:28px;overflow:auto}
 .mcard{background:#fff;border:1px solid var(--bd);border-radius:12px;max-width:920px;width:100%;
        box-shadow:0 8px 24px rgba(31,35,40,.2)}
 .mhead{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;
        border-bottom:1px solid var(--bd);font-weight:600;font-family:ui-monospace,monospace}
 .x{border:none;background:none;font-size:18px;cursor:pointer;color:var(--mut);line-height:1}
 .mbody{padding:8px 18px 18px}
 .mbody h3{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:16px 0 8px}
 .imgs{display:flex;gap:12px;flex-wrap:wrap}
 figure{margin:0;width:168px}
 figure img{width:168px;height:126px;object-fit:cover;border:1px solid var(--bd);border-radius:8px;background:#f6f8fa}
</style></head><body><div class=wrap>
 <h1>Tesla Delivery Service</h1>
 <div class=sub>shipments.wastake.com › App · auto-refreshes every 4s · <span id=gen></span></div>
 <div class=row>
   <div class=card><div class=k>Status</div><div class=v id=status>…</div></div>
   <div class=card><div class=k>Picked up</div><div class=v id=pickups>…</div></div>
   <div class=card><div class=k>Dropped off</div><div class=v id=dropoffs>…</div></div>
   <div class=card><div class=k>API errors</div><div class=v id=apierrors>…</div></div>
 </div>
 <div class="card hero idle" id=hero>loading…</div>
 <h2 id=liveact>Live activity</h2>
 <details id=logwrap open><summary>Show raw log</summary><pre id=log>loading…</pre></details>
 <div class=histhead>
   <button id=tabDelivered class="htab on">Delivered</button>
   <button id=tabMarks class="htab">Marks</button>
   <span class=mut id=histsub style="margin-left:auto;font-size:12px"></span>
 </div>
 <div id=viewDelivered>
   <table><thead><tr><th>When</th><th>VIN</th><th>Model</th><th>Destination</th><th>Status</th></tr></thead>
   <tbody id=deliv><tr><td colspan=5 class=mut style=padding:14px>loading…</td></tr></tbody></table>
 </div>
 <div id=viewMarks hidden>
   <div class=mut style="font-size:12px;margin:0 0 8px">Exterior / VIN / Key = photo found?</div>
   <table><thead><tr><th>When</th><th>Action</th><th>VIN</th><th>Model</th><th>Shipment</th>
     <th>Exterior</th><th>VIN</th><th>Key</th></tr></thead>
   <tbody id=hist></tbody></table>
 </div>
</div>
<div id=modal class=modal onclick="if(event.target===this)closeModal()">
 <div class=mcard>
  <div class=mhead><span id=mtitle></span><button class=x onclick=closeModal()>✕</button></div>
  <div id=mbody class=mbody></div>
 </div>
</div>
<script>
const esc=s=>(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let histTab='delivered', lastDeliv=0;
const yn=v=>v==null?'<span class=mut>—</span>'
   :(v?'<span class="pill y">yes</span>':'<span class="pill n">no</span>');
// VIN: real OCR plate = yes; pulled from rear/front fallback = yellow "!"
const vinCell=v=>v==null?'<span class=mut>—</span>'
   :(v?'<span class="pill y">yes</span>':'<span class="pill w">!</span>');
// Key: real key card = yes; fell back to the VIN photo = yellow "VIN"; else yellow "!"
const keyCell=(k,vf)=>k==null?'<span class=mut>—</span>'
   :(k?'<span class="pill y">yes</span>':(vf?'<span class="pill w">VIN</span>':'<span class="pill w">!</span>'));
async function showPhotos(vin,action){
 const m=document.getElementById('modal'),b=document.getElementById('mbody');
 document.getElementById('mtitle').textContent=vin+' — photos used';
 b.innerHTML='loading…'; m.style.display='flex';
 let d; try{ d=await (await fetch('photos?vin='+encodeURIComponent(vin))).json(); }catch(e){ b.innerHTML='error loading photos'; return; }
 const secs=(d.sections||[]).filter(s=>s.photos.length);
 b.innerHTML = secs.length ? secs.map(s=>`<h3>${esc(s.name)}</h3><div class=imgs>`+
   s.photos.map(p=>`<figure><img src="${esc(p.url)}" loading=lazy></figure>`).join('')
   +`</div>`).join('')
   : '<div class=mut style=padding:14px-0>No photos on disk'+(action==='Pick Up'?' (pickups are photo-free).':'.')+'</div>';
}
function closeModal(){ document.getElementById('modal').style.display='none'; }
document.addEventListener('keydown',e=>{ if(e.key==='Escape')closeModal(); });
const fmtAge=s=>s==null?'':(s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s');
function renderNow(now,up,curVin){
 const el=document.getElementById('hero');
 if(!now||!now.flow){
   // Idle (or a VIN in-flight without STEP markers) -> the classic blue box with the
   // driver phone hint (curVin shows the VIN being marked when there's no active flow).
   el.className='card hero now';
   const msg = up ? (curVin || 'idle - add "Andrew Enkh 3106925984" as driver to auto mark') : '—';
   el.innerHTML = `<div class=k>Currently marking</div><div class=v>${esc(msg)}</div>`;
   return;
 }
 el.className='card hero '+now.flow;
 const isDone=!!now.done;
 const track=(now.steps||[]).map((lbl,i)=>{
   const n=i+1, cls=isDone?'done':(n<now.step?'done':(n===now.step?'active':''));
   const mark=(isDone||n<now.step)?'✓':n;
   const el2=(!isDone&&n===now.step&&now.step_age_s!=null)?`<div class=el>${fmtAge(now.step_age_s)}</div>`:'';
   return `<div class="st ${cls}"><div class=num>${mark}</div><div class=lbl>${esc(lbl)}</div>${el2}</div>`;
 }).join('');
 const id=[now.vin?`<span class=vin>${esc(now.vin)}</span>`:'',
   now.model?`<span class=shp>${esc(now.model)}</span>`:'',
   now.shp?`<span class=shp>· ${esc(now.shp)}</span>`:''].filter(Boolean).join(' ');
 const badge=isDone?'<span class=donebadge>✓ committed</span>'
   :(now.stalled?'<span class=stall>⏳ taking longer than usual</span>':'');
 el.innerHTML=`<div class=htop><span class="fbadge ${now.flow}">${esc(now.flow_label)}</span>${id}${badge}</div>`
   +`<div class=track>${track}</div>`;
}
async function loadDelivered(){
 let d; try{ d=await (await fetch('delivered',{cache:'no-store'})).json(); }catch(e){ return; }
 const rows=d.delivered||[];
 const nErr=rows.filter(r=>r.status==='error').length;
 document.getElementById('histsub').textContent = rows.length+' delivered'+(nErr?` · ${nErr} error`:'');
 document.getElementById('deliv').innerHTML = rows.length ? rows.map(r=>{
   const badge = r.status==='app'   ? '<span class="pill e">APP</span>'
               : r.status==='error' ? '<span class="pill err">⚠ ERROR</span>'
               :                       '<span class="pill dl">delivered</span>';
   const clickable = r.status==='app' || r.status==='error';
   const click = clickable ? `onclick="showPhotos('${esc(r.vin)}','Drop Off')" title="click to see the photos used" style=cursor:pointer` : '';
   return `<tr ${click}><td class=mut>${esc(r.when)}</td><td class=vin>${esc(r.vin)}</td>`
     +`<td>${esc(r.model||'')}</td><td class=mut>${esc(r.dest||'')}</td><td>${badge}</td></tr>`;
 }).join('') : '<tr><td colspan=5 class=mut style=padding:14px>No delivered shipments yet.</td></tr>';
}
function setHistTab(t){
 histTab=t;
 document.getElementById('tabDelivered').classList.toggle('on', t==='delivered');
 document.getElementById('tabMarks').classList.toggle('on', t==='marks');
 document.getElementById('viewDelivered').hidden = t!=='delivered';
 document.getElementById('viewMarks').hidden = t!=='marks';
 document.getElementById('histsub').textContent = t==='delivered' ? '' : '';
 if(t==='delivered'){ lastDeliv=Date.now(); loadDelivered(); }
}
document.getElementById('tabDelivered').addEventListener('click',()=>setHistTab('delivered'));
document.getElementById('tabMarks').addEventListener('click',()=>setHistTab('marks'));
async function tick(){
 let d; try{ d=await (await fetch('api',{cache:'no-store'})).json(); }catch(e){ return; }
 const up = d.running || d.log_fresh;
 document.getElementById('status').innerHTML =
   `<span class="dot ${up?'on':'off'}"></span>${up?'Running':'Stopped'}`;
 document.getElementById('pickups').textContent = d.stats.pickups;
 document.getElementById('dropoffs').textContent = d.stats.dropoffs;
 document.getElementById('apierrors').textContent = d.stats.api_errors ?? 0;
 renderNow(d.now, up, d.current_vin);
 document.getElementById('gen').textContent = 'updated '+(d.generated_at||'').replace('T',' ');
 document.getElementById('log').textContent = (d.log||[]).join('\\n') || '(no shipment marked yet)';
 const lg=document.getElementById('log'); lg.scrollTop=lg.scrollHeight;
 const pillCls=a=>a==='Pick Up'?'p':(a==='API ERROR'?'e':'d');
 document.getElementById('hist').innerHTML = (d.history||[]).map(h=>`<tr
   onclick="showPhotos('${esc(h.vin)}','${esc(h.action)}')" title="click to see the photos used">
   <td class=mut>${esc(h.when||'')}</td>
   <td><span class="pill ${pillCls(h.action)}">${esc(h.action)}</span>${h.check==='error'?' <span class="pill err">⚠ ERROR</span>':''}</td>
   <td class=vin>${esc(h.vin)}</td><td>${esc(h.model||'')}</td>
   <td class=mut>${esc(h.shipment||'')}</td>
   <td>${yn(h.exterior)}</td><td>${vinCell(h.vin_found)}</td><td>${keyCell(h.key_found,h.vin_found)}</td></tr>`).join('');
 if(histTab==='marks'){
   const n=(d.history||[]).filter(h=>h.check==='error').length;
   document.getElementById('histsub').textContent = n?`${n} link error${n>1?'s':''}`:'';
 }
}
loadDelivered(); tick(); setInterval(tick, 4000);
setInterval(()=>{ if(histTab==='delivered') loadDelivered(); }, 30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api"):
            self._send(200, json.dumps(snapshot()), "application/json")
        elif self.path.startswith("/delivered"):
            self._send(200, json.dumps({"delivered": _delivered()}), "application/json")
        elif self.path.startswith("/photos"):
            self._photos()
        elif self.path.startswith("/img"):
            self._img()
        elif urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
            # ?hide_activity=1 (prod's App-tab iframe) keeps the page but hides the
            # live-activity section; test's iframe omits the flag and shows it in full.
            page = PAGE.replace("<body>", "<body class=noactivity>", 1) if self._q("hide_activity") else PAGE
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def _q(self, key):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get(key, [""])[0]

    def _photos(self):
        vin = self._q("vin")
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            self._send(400, "bad vin", "text/plain"); return
        self._send(200, json.dumps(photos_for(vin)), "application/json")

    def _img(self):
        vin, sec, fn = self._q("vin"), self._q("sec"), self._q("file")
        if not (re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and sec in ("sides", "vin_plate", "key")
                and re.fullmatch(r"[\w.\-]+\.(jpg|jpeg|png)", fn, re.I)):
            self._send(404, "no", "text/plain"); return
        rp = os.path.realpath(os.path.join(OUT, vin, sec, fn))
        if not rp.startswith(os.path.realpath(OUT) + os.sep) or not os.path.isfile(rp):
            self._send(404, "no", "text/plain"); return
        with open(rp, "rb") as fh:
            self._send(200, fh.read(), "image/png" if fn.lower().endswith(".png") else "image/jpeg")

    def log_message(self, *a):     # silence per-request stderr noise
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8011")))
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"dashboard on http://{a.host}:{a.port}  (db={DB}, log={LOGFILE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
