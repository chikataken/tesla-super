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

_DONE_RE = re.compile(r"ledger:|ledger\(pickup\):|queue empty|queues empty|nothing to do|nothing to pick")
_VIN_RE = re.compile(r"(?:unit VIN|verifying)\s+([A-HJ-NPR-Z0-9]{17})")
# Idle / heartbeat lines NOT shown in the dashboard log — only show actual marking work.
_HIDE_RE = re.compile(r"nothing to (do|pick)|queues? empty|waiting for new|service up|"
                      r"emulator unavailable|both queues empty", re.I)


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
        for v, m, s, o, p, t, ext, vf, kf in con.execute(
                "SELECT vin, model, shipment, option, photographed, dropped_at, "
                "exterior, vin_found, key_found FROM dropoffs"):
            rows.append({"action": "Drop Off", "vin": v, "model": m, "shipment": s,
                         "exterior": ext, "vin_found": vf, "key_found": kf, "at": t})
    except sqlite3.Error:
        pass
    try:
        for v, m, s, e, t in con.execute(
                "SELECT vin, model, shipment, eta, picked_at FROM pickups"):
            rows.append({"action": "Pick Up", "vin": v, "model": m, "shipment": s,
                         "exterior": None, "vin_found": None, "key_found": None, "at": t})
    except sqlite3.Error:
        pass
    try:
        for s, v, stage, detail, t in con.execute(
                "SELECT shipment, vin, stage, detail, seen_at FROM api_errors"):
            rows.append({"action": "API ERROR", "vin": v or "", "model": detail or stage,
                         "shipment": s, "exterior": None, "vin_found": None,
                         "key_found": None, "at": t})
    except sqlite3.Error:
        pass
    con.close()
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows[:limit]


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
    log_lines = [ln for ln in raw if ln.strip() and not _HIDE_RE.search(ln)]   # marking only
    last_ts = next((t for t in (_parse_ts(ln) for ln in reversed(log_lines)) if t), None)
    age = (datetime.datetime.now() - last_ts).total_seconds() if last_ts else None
    proc_alive = _service_running()
    history = _history()
    today = datetime.date.today().isoformat()
    return {
        "running": proc_alive,
        "log_fresh": age is not None and age < ALIVE_SECONDS,
        "last_seen": last_ts.isoformat(timespec="seconds") if last_ts else None,
        "last_seen_age_s": int(age) if age is not None else None,
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
 figcaption{font-size:12px;color:var(--mut);margin-top:5px;text-align:center}
</style></head><body><div class=wrap>
 <h1>Tesla Delivery Service</h1>
 <div class=sub>shipments.wastake.com › App · auto-refreshes every 4s · <span id=gen></span></div>
 <div class=row>
   <div class=card><div class=k>Status</div><div class=v id=status>…</div></div>
   <div class=card><div class=k>Picked up</div><div class=v id=pickups>…</div></div>
   <div class=card><div class=k>Dropped off</div><div class=v id=dropoffs>…</div></div>
   <div class=card><div class=k>API errors</div><div class=v id=apierrors>…</div></div>
   <div class=card><div class=k>Last activity</div><div class=v id=last style=font-size:15px>…</div></div>
 </div>
 <div class="card now"><div class=k>Currently marking</div><div class=v id=current>—</div></div>
 <h2>Live activity</h2><pre id=log>loading…</pre>
 <h2>History — past marks <span class=mut style=text-transform:none>(Exterior / VIN / Key = photo found?)</span></h2>
 <table><thead><tr><th>When</th><th>Action</th><th>VIN</th><th>Model</th><th>Shipment</th>
   <th>Exterior</th><th>VIN</th><th>Key</th></tr></thead>
 <tbody id=hist></tbody></table>
</div>
<div id=modal class=modal onclick="if(event.target===this)closeModal()">
 <div class=mcard>
  <div class=mhead><span id=mtitle></span><button class=x onclick=closeModal()>✕</button></div>
  <div id=mbody class=mbody></div>
 </div>
</div>
<script>
const esc=s=>(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
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
   s.photos.map(p=>`<figure><img src="${esc(p.url)}" loading=lazy><figcaption>${esc(p.label)}</figcaption></figure>`).join('')
   +`</div>`).join('')
   : '<div class=mut style=padding:14px-0>No photos on disk'+(action==='Pick Up'?' (pickups are photo-free).':'.')+'</div>';
}
function closeModal(){ document.getElementById('modal').style.display='none'; }
document.addEventListener('keydown',e=>{ if(e.key==='Escape')closeModal(); });
async function tick(){
 let d; try{ d=await (await fetch('api',{cache:'no-store'})).json(); }catch(e){ return; }
 const up = d.running || d.log_fresh;
 document.getElementById('status').innerHTML =
   `<span class="dot ${up?'on':'off'}"></span>${up?'Running':'Stopped'}`;
 document.getElementById('pickups').textContent = d.stats.pickups;
 document.getElementById('dropoffs').textContent = d.stats.dropoffs;
 document.getElementById('apierrors').textContent = d.stats.api_errors ?? 0;
 document.getElementById('last').textContent = d.last_seen
   ? (d.last_seen_age_s<60?`${d.last_seen_age_s}s ago`:d.last_seen.replace('T',' ')) : '—';
 document.getElementById('current').textContent = d.current_vin || (up?'idle - add "Andrew Enkh 3106925984" as driver to auto mark':'—');
 document.getElementById('gen').textContent = 'updated '+(d.generated_at||'').replace('T',' ');
 document.getElementById('log').textContent = (d.log||[]).join('\\n') || '(no shipment marked yet)';
 const lg=document.getElementById('log'); lg.scrollTop=lg.scrollHeight;
 const pillCls=a=>a==='Pick Up'?'p':(a==='API ERROR'?'e':'d');
 document.getElementById('hist').innerHTML = (d.history||[]).map(h=>`<tr
   onclick="showPhotos('${esc(h.vin)}','${esc(h.action)}')" title="click to see the photos used">
   <td class=mut>${esc((h.at||'').replace('T',' '))}</td>
   <td><span class="pill ${pillCls(h.action)}">${esc(h.action)}</span></td>
   <td class=vin>${esc(h.vin)}</td><td>${esc(h.model||'')}</td>
   <td class=mut>${esc(h.shipment||'')}</td>
   <td>${yn(h.exterior)}</td><td>${vinCell(h.vin_found)}</td><td>${keyCell(h.key_found,h.vin_found)}</td></tr>`).join('');
}
tick(); setInterval(tick, 4000);
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
        elif self.path.startswith("/photos"):
            self._photos()
        elif self.path.startswith("/img"):
            self._img()
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
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
