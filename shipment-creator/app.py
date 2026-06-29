"""
Local web GUI for reviewing shipments that are ready to be created.

FastAPI backend that serves the staged order payloads (from output/orders/*.json,
produced by `main.py --create`) plus a single-page frontend. Barebones today, but
the structure (JSON API + frontend) is the durable base for the future app:
- adding actions (assign VINs, adjust rates, create on SuperDispatch) = new endpoints
- making it "very aesthetic" = swap static/index.html for a React/Tailwind frontend

Run:
    python app.py            # then open http://127.0.0.1:8000
"""
from __future__ import annotations
import asyncio
import contextvars
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, Response, StreamingResponse)

import paths

_HERE = os.path.dirname(os.path.abspath(__file__))

# The dispatcher THIS request is acting as, set per-request from the X-Profile header
# (or ?profile= for the SSE) by the global dependency below. This is what lets two
# browser windows, each on a different dispatcher, drive independent boards + runs.
_req_profile: contextvars.ContextVar = contextvars.ContextVar("req_profile", default=None)


# ---- per-dispatcher board location -------------------------------------------
# Each dispatcher profile (Kelly, Burte, …) keeps its OWN board, so switching the
# selected dispatcher shows a completely different set of shipments. Resolution order:
# the pipeline subprocess is told its profile via SC_PROFILE; a web request carries it
# in the X-Profile header (-> _req_profile); otherwise fall back to the globally-
# selected dispatcher. Empty => the legacy shared OUTPUT_DIR (single board).
def _pid() -> str:
    import profiles
    return ((os.getenv("SC_PROFILE") or "").strip()
            or (_req_profile.get() or "").strip()
            or (profiles.active_id() or ""))


def _is_all_profile() -> bool:
    """The ALL profile is a developer tool for feeding the terminal cache only: no
    NeedByDate requirement on upload, and posting to SuperDispatch is disabled.
    (Legacy — no 'all' profile ships anymore; kept defensively.)"""
    import profiles
    return _pid() == profiles.ALL_PROFILE_ID


def _is_didi_profile() -> bool:
    """The didi profile bypasses the pickup-state filter AND posts every VIN regardless
    of state: no NeedByDate requirement on upload. Posting stays ENABLED. Same-route
    SuperDispatch duplicate-removal still applies (it is not a state filter)."""
    import profiles
    return _pid() == profiles.DIDI_PROFILE_ID


def _skip_needby_requirement() -> bool:
    """Profiles allowed to upload without a NeedByDate column (receive every VIN)."""
    return _is_all_profile() or _is_didi_profile()


def _pdir() -> str:
    d = paths.profile_output_dir(_pid())
    os.makedirs(d, exist_ok=True)
    return d


def _orders_dir() -> str:
    d = os.path.join(_pdir(), "orders")
    os.makedirs(d, exist_ok=True)
    return d


# The spare workspace, the consolidation search results + staged-merge queue, and the
# active-Excel marker — all per dispatcher profile now.
def _spares_path() -> str: return os.path.join(_pdir(), "spares.json")
def _search_path() -> str: return os.path.join(_pdir(), "consolidation_search.json")
def _consol_path() -> str: return os.path.join(_pdir(), "consolidations.json")
def _active_excel_path() -> str: return os.path.join(_pdir(), "active_excel.json")
def _last_headers_path() -> str: return os.path.join(_pdir(), "last_headers.json")


async def _capture_profile(request: Request):
    """Global dependency: stamp each request with the dispatcher it's acting as, from
    the X-Profile header (every /api fetch sends it) or ?profile= (the SSE, which can't
    set headers). Runs in the request context, so sync endpoints inherit it too."""
    p = (request.headers.get("X-Profile") or request.query_params.get("profile") or "").strip()
    _req_profile.set(p or None)


app = FastAPI(title="Shipment Creator", dependencies=[Depends(_capture_profile)])


def _staged_files() -> list[str]:
    return sorted(glob.glob(os.path.join(_orders_dir(), "*.json")),
                  key=os.path.getmtime, reverse=True)


def _board_signature() -> tuple:
    """A cheap fingerprint of everything the board renders: the (name, mtime) of the
    active staged batch plus the spares + consolidation files. Changes whenever a run
    writes new shipments, a scan writes matches, or an edit lands — so the SSE stream
    can tell the page to refresh itself. Including the basename catches a brand-new
    staged batch file (new timestamped name), not just a rewrite of the current one."""
    paths = [_spares_path(), _search_path(), _consol_path()]
    staged = _staged_files()
    if staged:
        paths.append(staged[0])                 # the active (newest) staged batch
    sig = []
    for p in paths:
        try:
            sig.append((os.path.basename(p), os.path.getmtime(p)))
        except OSError:
            sig.append((os.path.basename(p), None))
    return tuple(sig)


@app.get("/api/events")
async def api_events():
    """Server-sent events: pings the page whenever the board's files change, so the
    UI live-updates without a manual Refresh (shipments appear as BOLs are pulled
    during a run; SD matches appear when the scan finishes; edits from another window
    sync). One-way, polls file mtimes ~1s, and the browser auto-reconnects.

    The dispatcher is resolved HERE in the request context (from ?profile=) and re-
    asserted inside the generator, so each window's stream watches ITS OWN board even
    though the contextvar set by the dependency is gone by the time gen() is iterated."""
    pid = _pid()

    async def gen():
        _req_profile.set(pid or None)            # pin this stream to its dispatcher's board
        last = _board_signature()
        yield ": connected\n\n"                  # SSE comment line opens the stream
        beat = 0
        while True:
            await asyncio.sleep(1.0)
            try:
                sig = _board_signature()
            except Exception:
                continue
            if sig != last:
                last = sig
                beat = 0
                yield "event: changed\ndata: 1\n\n"
            else:
                beat += 1
                if beat >= 20:                   # ~20s heartbeat to hold the connection
                    beat = 0
                    yield ": keep-alive\n\n"
    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/orders")
def api_orders(file: Optional[str] = None):
    """Latest staged batch (or a named one). Returns {file, count, orders}."""
    files = _staged_files()
    if not files:
        return {"file": None, "count": 0, "orders": [],
                "message": "No staged orders yet. Run: main.py --create"}
    # Before showing the latest board, DROP any VIN already live on SuperDispatch so it can't
    # be double-posted (idempotent — see _auto_remove_sd_duplicates). `dups_removed` is the
    # running list for this board (cleared on reset), surfaced as a tally.
    # Runs for EVERY profile incl. didi: a VIN already live on SD's own loadboard on the
    # same route is a true duplicate, never a legitimate re-post (different-route VINs still
    # pass through — see _auto_remove_sd_duplicates).
    dups_removed = []
    if not file:
        try:
            dups_removed = _auto_remove_sd_duplicates()
        except Exception:
            dups_removed = []
        files = _staged_files()
    path = os.path.join(_orders_dir(), file) if file else (files[0] if files else None)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "staged file not found")
    with open(path, encoding="utf-8") as fh:
        orders = json.load(fh)
    for o in orders:
        _annotate_dates(o)
    return {"file": os.path.basename(path), "count": len(orders), "orders": orders,
            "unpostable": _unpostable_vins(), "duplicates_removed": dups_removed}


def _z5(z) -> str:
    """First 5 digits of a ZIP (handles ZIP+4); '' if none. Used to compare routes."""
    import re as _re
    m = _re.search(r"\d{5}", str(z or ""))
    return m.group(0) if m else ""


def _sd_live_vin_routes() -> dict:
    """For each VIN already on a POSTED/ACCEPTED/PENDING/PICKED-UP SuperDispatch order —
    taken from the latest loadboard scan, which is enriched from the API (get_order) so
    EVERY VIN on a multi-car order is included, not just the one shown in the '+N' card
    preview — the set of (pickup_zip5, delivery_zip5) routes that order runs.

    posted/accepted/pending are live on the loadboard (is_posted_to_loadboard=True). A
    PICKED-UP car is off the loadboard (loadboard_status is null) but still in transit on its
    route — re-posting it would duplicate a car already being delivered, so it's treated as
    live too. The scrape stamps loadboard_status='picked_up'; the API-direct path leaves
    loadboard_status null but carries the lifecycle status='picked_up' — accept either.

    A board VIN is a true duplicate ONLY when its route matches one of these. A VIN already
    posted on a DIFFERENT route is a legitimate re-post (chained delivery: it's dropped off,
    then immediately shipped again), so it must pass through."""
    search = _load_json(_search_path(), {})
    out: dict[str, set] = {}
    for o in (search.get("orders") or []):
        lb = (o.get("loadboard_status") or "").strip().lower()
        life = (o.get("status") or "").strip().lower()
        if lb not in ("posted", "accepted", "pending", "picked_up") and life != "picked_up":
            continue
        route = (_z5(((o.get("pickup") or {}).get("venue") or {}).get("zip")),
                 _z5(((o.get("delivery") or {}).get("venue") or {}).get("zip")))
        for v in (o.get("vehicles") or []):
            vin = (v.get("vin") or "").strip().upper()
            if vin:
                out.setdefault(vin, set()).add(route)
    return out


def _dups_removed_path() -> str:
    return os.path.join(_pdir(), "duplicates_removed.json")


def _auto_remove_sd_duplicates() -> list:
    """DROP any board VIN that's already posted/accepted on SuperDispatch entirely (not to
    spares) so a re-post can't create a duplicate. Tracks the cumulative removed VINs for
    this board (cleared on /api/reset) and returns that running list for the tally.
    Idempotent: once removed, a VIN is off the board, so a second call adds nothing."""
    removed = list(_load_json(_dups_removed_path(), []))
    live_routes = _sd_live_vin_routes()
    files = _staged_files()
    if live_routes and files:
        path = files[0]
        with open(path, encoding="utf-8") as f:
            batch = json.load(f)
        dups: set = set()
        changed = False
        for o in batch:
            # this board order's route (origin zip5, dest zip5)
            broute = (_z5(((o.get("pickup") or {}).get("venue") or {}).get("zip")),
                      _z5(((o.get("delivery") or {}).get("venue") or {}).get("zip")))
            kept = []
            for v in (o.get("vehicles") or []):
                vin = (v.get("vin") or "").strip().upper()
                routes = live_routes.get(vin)
                # Drop ONLY when SD already has this VIN posted on the EXACT same route (both
                # zips known + equal). A different (or unknown) route passes through so a
                # chained re-delivery can be posted again.
                if vin and routes and broute[0] and broute[1] and broute in routes:
                    dups.add(vin)
                else:
                    kept.append(v)
            if len(kept) != len(o.get("vehicles") or []):
                o["vehicles"] = kept
                _recompute(o)
                changed = True
        if changed:
            batch = [o for o in batch if o["vehicles"]]
            _save_batch(path, batch)
        if dups:
            seen = {x.upper() for x in removed}
            for d in sorted(dups):
                if d not in seen:
                    removed.append(d)
                    seen.add(d)
            _save_json(_dups_removed_path(), removed)
    return removed


def _unpostable_vins() -> list[dict]:
    """Excel VINs that, AFTER the run finished, never made it onto the board or into
    spares — i.e. Tesla couldn't produce a BOL (no shipment found / download failed). Empty
    while a run is in progress (those VINs are still pending, not failed) or before any run.
    Each carries VIN-decoded make/model/year for display."""
    if _run_state(_pid())["progress"]["running"]:
        return []
    excel = _excel_vins()
    board = _board_vins()
    # Only flag misses once a run has actually produced a board — otherwise (no run yet, or
    # right after an Excel load resets the board) we'd wrongly flash EVERY VIN as unpostable.
    if not excel or not board:
        return []
    spares = {s.get("vin") for s in _load_spares() if s.get("vin")}
    # VINs we intentionally dropped as SD duplicates were removed from the board on purpose —
    # they're not Tesla-pull failures, so don't flag them as unpostable.
    dups = {x.strip().upper() for x in _load_json(_dups_removed_path(), []) if x}
    # VINs staged onto an existing SD order (drag-drop consolidation) were pulled off the
    # board on purpose, to be merged at post time — not Tesla-pull failures either.
    staged = {(a.get("vin") or "").strip().upper()
              for e in _load_json(_consol_path(), []) for a in (e.get("add") or [])}
    missing = {v for v in (excel - board - spares)
               if (v or "").strip().upper() not in dups
               and (v or "").strip().upper() not in staged}
    if not missing:
        return []
    try:
        import pdf_read
        return [{"vin": v, **pdf_read._vehicle(v)} for v in sorted(missing)]
    except Exception:
        return [{"vin": v} for v in sorted(missing)]


@app.get("/api/terminals")
def api_terminals():
    """The terminal cache for the Terminals tab. Each group is a CANONICAL terminal —
    an original scraped terminal ('db') or a standalone learned one ('added') — with the
    Tesla-BOL/linked names that resolve to it listed as `aliases`. Linked terminals are
    folded under their original, never shown on their own."""
    import terminals_db
    terminals_db.init_db()
    ts = terminals_db.all_terminals()
    aliases: dict[str, list[str]] = {}
    for t in ts:
        if t.get("linked_sd_id"):
            aliases.setdefault(t["linked_sd_id"], []).append(t.get("name", ""))
    groups = []
    for t in ts:
        src = t.get("source") or "sd"
        if src == "bol" and t.get("linked_sd_id"):
            continue                                   # shown as an alias under its original
        groups.append({
            "sd_id": t.get("sd_id", ""), "name": t.get("name", ""),
            "kind": "added" if src == "bol" else "db",
            "address": t.get("address", ""), "city": t.get("city", ""),
            "state": t.get("state", ""), "zip": t.get("zip", ""),
            "contact_name": t.get("contact_name", ""), "contact_phone": t.get("contact_phone", ""),
            "carrier_notes": t.get("carrier_notes", ""),
            "aliases": sorted(a for a in aliases.get(t.get("sd_id", ""), []) if a),
        })
    groups.sort(key=lambda g: (g["name"] or "").lower())
    n_sd = sum(1 for t in ts if (t.get("source") or "sd") == "sd")
    n_bol = sum(1 for t in ts if t.get("source") == "bol")
    n_linked = sum(1 for t in ts if t.get("linked_sd_id"))
    stats = {"total": len(ts), "originals": n_sd, "learned": n_bol,
             "linked": n_linked, "added": n_bol - n_linked}
    return {"count": len(groups), "stats": stats, "groups": groups}


@app.post("/api/terminals/update")
def api_terminals_update(body: dict = Body(...)):
    """Save a manual edit to one terminal straight into the LOCAL cache DB (terminals.db).
    Keyed on `sd_id`; only the editable fields are written. Returns the refreshed canonical
    group (same shape as /api/terminals groups) so the UI can update in place.

    NOTE: this writes to the local database ONLY — it does NOT push to SuperDispatch. A
    later full terminal refresh (scrape) of an SD-sourced row would overwrite these edits."""
    import terminals_db
    terminals_db.init_db()
    sd_id = (body or {}).get("sd_id", "")
    row = terminals_db.update_terminal_fields(sd_id, body or {})
    if row is None:
        raise HTTPException(status_code=404, detail="terminal not found")
    aliases = [t.get("name", "") for t in terminals_db.all_terminals()
               if t.get("linked_sd_id") == row["sd_id"]]
    src = row.get("source") or "sd"
    group = {
        "sd_id": row.get("sd_id", ""), "name": row.get("name", ""),
        "kind": "added" if src == "bol" else "db",
        "address": row.get("address", ""), "city": row.get("city", ""),
        "state": row.get("state", ""), "zip": row.get("zip", ""),
        "contact_name": row.get("contact_name", ""), "contact_phone": row.get("contact_phone", ""),
        "carrier_notes": row.get("carrier_notes", ""),
        "aliases": sorted(a for a in aliases if a),
    }
    return {"ok": True, "group": group}


def _annotate_dates(order: dict) -> dict:
    """Stamp an order with the computed pickup/delivery date windows (from its need-by
    + route states, via transit.py) so the GUI can show them and the post step reuse
    them. No-op if there's no need-by."""
    import transit
    nb = order.get("need_by_ts")
    if nb is None:
        return order
    pstate = ((order.get("pickup") or {}).get("venue") or {}).get("state")
    dstate = ((order.get("delivery") or {}).get("venue") or {}).get("state")
    w = transit.shipment_windows(nb, pstate, dstate)
    if w:
        order["pickup_window"] = w["pickup"]
        order["delivery_window"] = w["delivery"]
        order["transit_days"] = w["transit_days"]
    return order


@app.get("/api/batches")
def api_batches():
    """List available staged batches (filenames), newest first."""
    return [os.path.basename(p) for p in _staged_files()]


def _build_cmd(o: dict) -> list[str]:
    """Build the pipeline command from validated options (no shell, no injection:
    every value is a separate argv entry, flags are fixed strings)."""
    import config
    excel = (o.get("excel") or "").strip() or config.DEFAULT_EXCEL
    # Dev: run the pipeline as `python main.py ...`. Frozen: there is no python or
    # main.py on disk — sys.executable IS our bundled app, so re-invoke ourselves
    # with a --pipeline marker that the entrypoint routes to main.main().
    if paths.is_frozen():
        cmd = [sys.executable, "--pipeline", "--excel", excel]
    else:
        cmd = [sys.executable, "main.py", "--excel", excel]
    if o.get("sheet"):
        cmd += ["--sheet", str(o["sheet"])]
    if o.get("limit"):
        cmd += ["--limit", str(int(o["limit"]))]
    if o.get("workers"):
        cmd += ["--workers", str(int(o["workers"]))]
    if o.get("download_bols"):
        cmd += ["--download-bols"]
    if o.get("create"):
        cmd += ["--create"]
    if o.get("live"):
        cmd += ["--live"]
    return cmd


# Live run state, owned by the server (NOT the streaming client) so progress
# survives a page refresh: a background thread drains the subprocess, parses
# pulled/total VINs, and any page can read it from /api/run/status.
_TOTAL_RE = re.compile(r"(?:Fetching|Downloading) BOLs for (\d+)\s+(?:new\s+)?VIN")
_PULLED_RE = re.compile(r"(\d+) new vehicle\(s\) so far")

# CONCURRENCY: each dispatcher gets its OWN run state (lock + log + progress + proc),
# so Soyo, Kelly, Duka and Burte can each have a pull in flight at the same time
# without colliding. Keyed by profile id. _runs_guard protects the dict itself while
# a per-profile state record is being created.
_runs: dict = {}
_runs_guard = threading.Lock()


def _run_state(pid: str) -> dict:
    """The run-state record for one dispatcher (created on first use). 'lock' gates
    that dispatcher's own runs (one pull per dispatcher at a time); different
    dispatchers hold different locks, so their pulls run concurrently."""
    pid = pid or ""
    with _runs_guard:
        st = _runs.get(pid)
        if st is None:
            st = {
                "lock": threading.Lock(),
                "log": [],
                "proc": None,
                "progress": {"running": False, "pulled": 0, "total": 0},
            }
            _runs[pid] = st
        return st


# The shared automation Chrome. All dispatchers attach to the SAME logged-in Chrome
# over CDP (separate tabs), so we launch it at most once and DON'T let a finishing run
# close it out from under the others. We own the process only if WE launched it.
_chrome_proc = None
_chrome_guard = threading.Lock()


def _ensure_shared_chrome():
    """Make sure the one shared automation Chrome is up before a run attaches to it.
    Launches it at most once across all concurrent runs; a run that finds Chrome
    already up (launched by us earlier, or by the user's login flow) leaves it be."""
    global _chrome_proc
    import chrome_cdp
    with _chrome_guard:
        proc = chrome_cdp.ensure_chrome()             # None if one was already running
        if proc is not None and _chrome_proc is None:
            _chrome_proc = proc                        # remember it so shutdown can kill it


def _terminate_proc(proc):
    """Kill one pipeline subprocess and everything it spawned (taskkill /T walks the
    tree). No-op if it's None or already dead."""
    if proc is None:
        return
    try:
        if proc.poll() is None:                       # still alive
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                proc.terminate()
    except Exception:                                 # noqa: BLE001 - quitting regardless
        pass


def _terminate_run(pid: str | None = None):
    """Stop a dispatcher's in-progress pipeline (and its Chrome tabs). With no pid,
    stops the run for the request's current dispatcher. The shared Chrome process
    itself is left alone — other dispatchers may still be using it."""
    st = _run_state(pid if pid is not None else _pid())
    _terminate_proc(st.get("proc"))


def _terminate_all_runs():
    """Stop every dispatcher's in-flight pipeline. Used at shutdown so no pull is left
    orphaned. Snapshot under the guard so we don't iterate a mutating dict."""
    with _runs_guard:
        states = list(_runs.values())
    for st in states:
        _terminate_proc(st.get("proc"))


@app.on_event("shutdown")
def _on_shutdown():
    """When the server stops — Ctrl+C, tray Quit, or any exit — stop every in-flight
    pipeline subprocess. The shared automation Chrome is DETACHED and may be shared with
    tesla-reconcile, so we do NOT kill it here: each run already closes its own window
    when it ends, and Chrome quits on its own once its last window closes."""
    try:
        _terminate_all_runs()
    except Exception:                                 # noqa: BLE001
        pass


@app.post("/api/run")
def api_run(opts: dict = Body(...)):
    """Start the pipeline for THIS request's dispatcher and stream its console output.
    The subprocess is owned by a background thread (not this request), so it keeps
    running and reporting progress even if the client disconnects/refreshes. One run
    per dispatcher — but different dispatchers run concurrently (each on its own board,
    sharing the one logged-in Chrome as separate tabs)."""
    # Resolve the dispatcher ONCE, here in the request context, and pass it explicitly
    # into the thread + generator. The contextvar is reset when the request returns, so
    # the streaming generator can't rely on it — the captured `pid` is the source of truth.
    pid = _pid()
    if not pid:
        raise HTTPException(400, "Select a dispatcher profile before running an Excel.")
    st = _run_state(pid)
    if not st["lock"].acquire(blocking=False):
        raise HTTPException(409, f"A run is already in progress for {pid}.")
    try:
        cmd = _build_cmd(opts)
    except (ValueError, TypeError) as e:
        st["lock"].release()
        raise HTTPException(400, str(e))

    # remember which sheet this board is being built from, so /api/tally counts
    # against the Excel you actually ran (not the config default).
    import config
    _save_json(_active_excel_path(), {
        "path": (opts.get("excel") or "").strip() or config.DEFAULT_EXCEL,
        "sheet": (str(opts.get("sheet")).strip() or None) if opts.get("sheet") else None,
    })

    log = st["log"]
    progress = st["progress"]
    log.clear()
    progress.update(running=True, pulled=0, total=0)

    # bring up the shared Chrome before the pull attaches to it (at most once across
    # all concurrent dispatchers). Best-effort: if it fails, the pipeline will report it.
    try:
        _ensure_shared_chrome()
    except Exception as e:                            # noqa: BLE001
        log.append(f"[warn] could not pre-launch Chrome: {e}\n")

    # don't let the pipeline subprocess pop up its own console window (its output is
    # captured via the pipe anyway); harmless in dev, essential for the windowed build.
    _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def _drain():
        try:
            log.append(f"$ {' '.join(cmd)}\n\n")
            proc = subprocess.Popen(
                cmd, cwd=_HERE, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=_no_window,
                env={**os.environ, "SC_PROFILE": pid},   # write to THIS dispatcher's board
            )
            st["proc"] = proc                         # expose for Quit-time termination
            for line in proc.stdout:
                log.append(line)
                m = _TOTAL_RE.search(line)
                if m:
                    progress["total"] = int(m.group(1))
                m = _PULLED_RE.search(line)
                if m:
                    progress["pulled"] = int(m.group(1))
            proc.wait()
            log.append(f"\n[finished — exit code {proc.returncode}]\n")
        finally:
            st["proc"] = None
            progress["running"] = False
            st["lock"].release()

    threading.Thread(target=_drain, daemon=True).start()

    def gen():
        idx = 0
        while True:
            while idx < len(log):
                yield log[idx]
                idx += 1
            if not progress["running"]:
                while idx < len(log):          # flush any final lines after it stopped
                    yield log[idx]
                    idx += 1
                break
            time.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/api/run/status")
def api_run_status():
    """Current run progress for THIS request's dispatcher (server-owned), so a freshly-
    loaded page can show the progress bar mid-run instead of losing it on refresh."""
    return dict(_run_state(_pid())["progress"])


@app.get("/api/env")
def api_env():
    import config
    return {"sd_env": config.SD_ENV, "default_excel": config.DEFAULT_EXCEL,
            "test_mode": config.TEST_MODE}


# ----------------------- dispatcher profiles --------------------------------
@app.get("/api/profiles")
def api_profiles():
    """The dispatcher profiles (for the top-left dropdown) + the active selection."""
    import profiles
    return {"profiles": profiles.list_profiles(), "active": profiles.active_id()}


@app.post("/api/profile")
def api_profile_set(body: dict = Body(...)):
    """Select (persist) the active dispatcher. Pass {"id": null} to clear it."""
    import profiles
    try:
        profiles.set_active((body or {}).get("id"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _excel_cache_clear()                                # this dispatcher's filter changed
    return {"ok": True, "active": profiles.active_id()}


@app.post("/api/profile/save")
def api_profile_save(body: dict = Body(...)):
    """Save a dispatcher's phone (fills the <dispatcher> token) and pickup-state filter
    from the Settings tab. `states` accepts 'VA MD GA FL' or a list."""
    import profiles
    try:
        p = profiles.save_profile((body or {}).get("id"),
                                  phone=body.get("phone"), states=body.get("states"),
                                  name=body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _excel_cache_clear()                                # this dispatcher's state filter changed
    return {"ok": True, "profile": p}


@app.post("/api/profile/add")
def api_profile_add(body: dict = Body(...)):
    """Create a new dispatcher (user). Body: {"name": "..."}; id is derived from the name."""
    import profiles
    try:
        p = profiles.add_profile((body or {}).get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "profile": p}


@app.get("/api/profile-image/{pid}")
def api_profile_image(pid: str):
    """Serve a dispatcher's avatar from profiles/images/<id>.<ext> (case-insensitive
    on the filename, so 'Soyo.png' matches profile id 'soyo'), if present."""
    import profiles
    exts = {"png", "jpg", "jpeg", "webp", "gif"}
    for base in profiles.image_dirs():
        if not os.path.isdir(base):
            continue
        for fn in os.listdir(base):
            stem, ext = os.path.splitext(fn)
            if stem.lower() == pid.lower() and ext.lower().lstrip(".") in exts:
                return FileResponse(os.path.join(base, fn))
    raise HTTPException(404, "no image for that profile")


# ----------------------- settings + one-time login --------------------------
@app.get("/api/settings")
def api_settings_get():
    """Current settings for the GUI. The SuperDispatch secret is never returned —
    only `has_secret` tells the UI whether one is stored."""
    import settings_store
    return settings_store.public_view()


@app.post("/api/settings")
def api_settings_set(body: dict = Body(...)):
    """Save settings (non-secret -> settings.json, secret -> Windows Credential
    Manager) and hot-reload config so the new values take effect without a restart."""
    import importlib
    import settings_store
    import config
    settings_store.save(body or {})
    settings_store.apply_to_env(force=True)            # override env with the new values
    importlib.reload(config)                           # re-read credentials, env, excel
    _excel_cache_clear(all_profiles=True)               # config reload affects every profile
    return {"ok": True, **settings_store.public_view(),
            "sd_env": config.SD_ENV, "default_excel": config.DEFAULT_EXCEL}


@app.post("/api/login")
def api_login(body: dict = Body(default=None)):
    """Open the persistent-profile Chrome at BOTH the Tesla portal and SuperDispatch
    so the user can sign in once. Cookies persist in the profile for later automated
    runs. Returns the URLs opened so the UI can show a manual fallback."""
    import config
    import chrome_cdp
    config.WINDOW_MODE = "visible"                     # login is always interactive
    config.HEADLESS = False
    targets = [config.TESLA_DASHBOARD_URL, config.SD_WEB_BASE]
    try:
        opened = chrome_cdp.open_urls(targets)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))               # Chrome not found -> actionable
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(500, f"Couldn't open Chrome for login: {e}")
    return {"ok": True, "opened": opened}


@app.post("/api/upload")
async def api_upload(request: Request, name: str = "upload.xlsx"):
    """Receive an Excel chosen in the CLIENT's browser (raw file bytes in the POST body)
    and save it server-side, returning the path the run will use. This replaces the old
    native host-side 'Open file' dialog, which only worked when the browser ran on the
    same machine as the server — useless over the web (Cloudflare tunnel). The filename is
    reduced to a safe basename and forced to a spreadsheet extension."""
    safe = os.path.basename((name or "upload.xlsx").strip()) or "upload.xlsx"
    if not safe.lower().endswith((".xlsx", ".xlsm")):
        safe += ".xlsx"
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty upload — the file didn't come through.")
    dest = paths.data_path("uploads", safe)            # DATA_DIR/uploads/<name> (dir auto-created)
    with open(dest, "wb") as f:
        f.write(data)

    # Reject (and delete) any sheet without a NeedByDate column — every shipment needs a
    # need-by for transit windows, and cache-resolved ones can't fall back to the dashboard.
    # The ALL profile is exempt: it's just feeding terminals, so any sheet is fine.
    import excel_ingest
    try:
        _, report = excel_ingest.read_rows(dest)
    except Exception as e:                              # unreadable / not a real spreadsheet
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(400, f"Couldn't read that spreadsheet: {e}")
    if not _skip_needby_requirement() and "need_by" not in report.column_mapping:
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(400, "This Excel has no NeedByDate column. Add a "
                                 "'NeedByDate' column and re-upload.")
    # Remember this sheet's header row so DATA-only pasted rows can reuse it later.
    try:
        _save_json(_last_headers_path(), excel_ingest.read_headers(dest))
    except Exception:
        pass
    return {"path": dest, "name": safe}


@app.post("/api/upload-paste")
def api_upload_paste(body: dict = Body(...)):
    """Accept rows copied from Excel/Sheets (TSV on the clipboard) and turn them into a
    real .xlsx that flows through the exact same run/post pipeline as an upload. If the
    paste starts with a header row we use (and remember) it; otherwise we prepend the
    headers remembered from the last sheet — so day-to-day you paste DATA only."""
    import csv, io, re as _re
    text = ((body or {}).get("text") or "")
    if not text.strip():
        raise HTTPException(400, "Paste some rows first.")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter="\t")
            if any((c or "").strip() for c in r)]
    if not rows:
        raise HTTPException(400, "No rows found in the pasted text.")
    # A data row always carries a 17-char VIN; a header row doesn't — that's the tell.
    VIN = _re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
    is_header = lambda r: not any(VIN.match((c or "").strip().upper()) for c in r)
    if is_header(rows[0]):
        headers, data = rows[0], rows[1:]
        try: _save_json(_last_headers_path(), [str(h) for h in headers])
        except Exception: pass
    else:
        headers = _load_json(_last_headers_path(), None)
        if not headers:
            raise HTTPException(400, "No saved headers yet — include the header row in your "
                                     "paste, or upload a sheet once first so I can learn them.")
        data = rows
    if not data:
        raise HTTPException(400, "No data rows found (only a header row).")
    # Write everything as text (preserves leading-zero zips and avoids number reformatting).
    import openpyxl as _op
    wb = _op.Workbook(); ws = wb.active
    ws.append([str(h) for h in headers])
    width = len(headers)
    for r in data:
        ws.append([str(r[i]) if i < len(r) else "" for i in range(width)])
    dest = paths.data_path("uploads", "pasted.xlsx")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    wb.save(dest)
    import excel_ingest
    try:
        _, report = excel_ingest.read_rows(dest)
    except Exception as e:
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(400, f"Couldn't read those rows: {e}")
    if not _skip_needby_requirement() and "need_by" not in report.column_mapping:
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(400, "These rows have no NeedByDate column. Include a "
                                 "'NeedByDate' column (paste the header row once to teach it).")
    return {"path": dest, "name": "pasted.xlsx", "rows": len(data)}


@app.post("/api/run/terminate")
def api_run_terminate():
    """Stop THIS dispatcher's in-progress pipeline (other dispatchers' runs keep going).
    Waits briefly for the drain thread to release the run lock so a follow-up /api/run
    won't 409."""
    progress = _run_state(_pid())["progress"]
    _terminate_run()
    for _ in range(40):
        if not progress.get("running"):
            break
        time.sleep(0.1)
    return {"ok": True, "running": progress.get("running", False)}


@app.post("/api/reset")
def api_reset():
    """Start over: stop any run, then clear the staged board, the active-Excel marker,
    spares and consolidations — so the GUI returns to the blank 'Excel +' state and you
    can run a completely different sheet from scratch."""
    _terminate_run()
    removed = 0
    for f in glob.glob(os.path.join(_orders_dir(), "*.json")):
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    for p in (_active_excel_path(), _spares_path(), _search_path(), _consol_path(),
              _dups_removed_path()):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    _excel_cache_clear()                                # this dispatcher's board was reset
    return {"ok": True, "removed_batches": removed}


# ----------------------- tally + post (SuperDispatch) -----------------------
# CONCURRENCY: the Excel VIN count is PER-DISPATCHER — each profile filters the sheet to
# its own states, so Soyo's total differs from Kelly's even off the same file. The cache
# is therefore keyed by profile id (not a single global, which made every window show the
# first profile's count). Each entry: {"path","sheet","mtime","vins"}.
_excel_cache: dict = {}


def _excel_cache_clear(all_profiles: bool = False) -> None:
    """Invalidate the Excel VIN cache. By default just this request's dispatcher (its
    state filter or board changed); all_profiles=True wipes every entry (e.g. a settings
    reload that can change how every sheet is read)."""
    if all_profiles:
        _excel_cache.clear()
    else:
        _excel_cache.pop(_pid() or "", None)


def _active_excel() -> tuple[Optional[str], Optional[str]]:
    """(path, sheet) of the Excel the current board was built from, or (None, None) if
    no run has been started yet. We do NOT fall back to a default sheet: an empty active
    Excel makes the GUI show the 'Excel +' picker instead of counting against (and
    resuming) some sheet the user never chose."""
    saved = _load_json(_active_excel_path(), {})
    if not isinstance(saved, dict) or not saved.get("path"):
        return None, None
    return saved.get("path"), saved.get("sheet")


def _excel_vins() -> set:
    """The set of usable VINs in THIS dispatcher's active Excel — the sheet filtered to
    the request profile's own states (cached per profile by path+sheet+mtime). Empty when
    no Excel has been chosen/run yet."""
    path, sheet = _active_excel()
    if not path:
        return set()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return set()
    import profiles
    pid = _pid()                                        # whose total are we counting?
    c = _excel_cache.get(pid or "")
    if c and c["path"] == path and c["sheet"] == sheet and c["mtime"] == mt:
        return c["vins"]
    try:
        import excel_ingest
        rows, _ = excel_ingest.read_rows(path, sheet)
        rows = profiles.filter_rows(rows, profiles.get_profile(pid))   # THIS dispatcher's states
        vins = {r.vin for r in rows if r.ok and r.vin}
    except Exception:                                   # noqa: BLE001
        vins = set()
    _excel_cache[pid or ""] = {"path": path, "sheet": sheet, "mtime": mt, "vins": vins}
    return vins


def _board_vins() -> set:
    files = _staged_files()
    if not files:
        return set()
    try:
        with open(files[0], encoding="utf-8") as f:
            board = json.load(f)
    except (OSError, ValueError):
        return set()
    return {v.get("vin") for o in board for v in o.get("vehicles", []) if v.get("vin")}


@app.get("/api/tally")
def api_tally():
    """How many of the Excel's VINs are built onto the board. `done` (all read) flips
    the GUI's grayed counter into the green 'Post to SuperDispatch' button."""
    excel = _excel_vins()
    total = len(excel)
    board = _board_vins()
    # Spared VINs count as processed too: setting one aside must NOT drop the counter
    # or hide the Post button (spares simply won't be posted).
    spares = {s.get("vin") for s in _load_spares() if s.get("vin")}
    seen = board | spares
    # VINs dropped as SD duplicates are resolved-on-purpose: count them as processed so the
    # Post button still unlocks (they're not on the board/spares and aren't pull failures).
    dups = {x.strip().upper() for x in _load_json(_dups_removed_path(), []) if x}
    processed = (len([v for v in excel if v in seen or (v or "").strip().upper() in dups])
                 if total else len(seen))
    # VINs Tesla couldn't pull (no BOL) count toward completion as failures — otherwise a
    # finished run with 1-2 unpullable VINs would sit at total-1/total forever and never
    # unlock the Post button. They're shown grayed at the top of the board, never posted.
    unpostable = len(_unpostable_vins())
    path, _sheet = _active_excel()
    return {"processed": processed, "total": total, "unpostable": unpostable,
            "running": _run_state(_pid())["progress"]["running"],
            "active_excel": path,
            "active_excel_name": os.path.basename(path) if path else "",
            "done": total > 0 and (processed + unpostable) >= total}


@app.post("/api/post")
def api_post(body: dict = Body(...)):
    """TEMPLATE — builds the SuperDispatch create payloads for every staged shipment,
    each stamped with its computed pickup/delivery date windows, and returns them for
    review. It does NOT send anything yet (dry-run). Wire the real create + the
    consolidation PATCHes (consolidations.json) here later."""
    import sd_api
    import profiles
    files = _staged_files()
    board = []
    if files:
        try:
            with open(files[0], encoding="utf-8") as f:
                board = json.load(f)
        except (OSError, ValueError):
            board = []
    _prof = profiles.get_profile(_pid())               # posting profile (Didi piggybacks per state)
    payloads = []
    for o in board:
        total, _per = _effective_prices(o)             # override total, else sum of rates
        # The exact SuperDispatch create body: one carrier payment (no per-VIN price),
        # the check/15-day payment block, and the instruction templates. The <dispatcher>
        # phone is resolved per-order: Didi stamps the phone of whoever owns its pickup state.
        payloads.append(sd_api.to_sd_order(
            o, total=total, dispatcher=profiles.dispatcher_phone_for_order(o, _prof)))
    # TODO (next): also read consolidations.json and PATCH staged VINs onto the
    # matched existing SD orders (build_vehicles_merge), then actually POST these.
    consol = _load_json(_consol_path(), [])
    return {"ok": True, "dry_run": True, "count": len(payloads),
            "consolidations": len(consol), "payloads": payloads}


@app.post("/api/post-live")
def api_post_live(body: dict = Body(...)):
    """LIVE — actually POST ONE shipment to SuperDispatch (a prod test of a single
    order). Builds the exact create body for the given order number, fills the active
    dispatcher's phone into the instructions, sends it, and returns the new guid."""
    import config
    if config.TEST_MODE:
        raise HTTPException(403, "TEST MODE — posting to SuperDispatch is disabled on the "
                                 "test site (test.wastake.com). Nothing was sent.")
    if _is_all_profile():
        raise HTTPException(403, "Posting is disabled for the ALL profile (it only feeds "
                                 "terminals). Switch to a dispatcher profile to post.")
    import sd_api
    import profiles
    number = (body or {}).get("number")
    if not number:
        raise HTTPException(400, "number is required")
    _path, batch = _load_batch()
    o = _find(batch, number)
    if not o:
        raise HTTPException(404, "that shipment isn't on the board")
    total, _per = _effective_prices(o)
    payload = sd_api.to_sd_order(
        o, total=total,
        dispatcher=profiles.dispatcher_phone_for_order(o, profiles.get_profile(_pid())))
    try:
        res = sd_api.create_order(payload, dry_run=False)
    except sd_api.SDError as e:
        raise HTTPException(502, f"SuperDispatch rejected the order: {e}")
    return {"ok": True, "number": number, "vins": len(o.get("vehicles") or [])}


@app.post("/api/post-all")
def api_post_all(body: dict = Body(default=None)):
    """LIVE — post EVERY staged shipment to SuperDispatch, then clear the board
    (spares are kept). New shipments are created and any staged consolidations are
    merged onto their existing SD orders. Anything that fails to post stays on the
    board for a retry. Returns only the count of VINs posted (no guids/ids)."""
    import config
    if config.TEST_MODE:
        raise HTTPException(403, "TEST MODE — posting to SuperDispatch is disabled on the "
                                 "test site (test.wastake.com). The board was left untouched.")
    if _is_all_profile():
        raise HTTPException(403, "Posting is disabled for the ALL profile (it only feeds "
                                 "terminals). Switch to a dispatcher profile to post.")
    import sd_api
    import profiles
    _prof = profiles.get_profile(_pid())               # posting profile (Didi piggybacks per state)

    path = _staged_files()[0] if _staged_files() else None
    board = []
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                board = json.load(f)
        except (OSError, ValueError):
            board = []

    posted_vins, kept_orders, failures = 0, [], []
    for o in board:
        try:
            total, _per = _effective_prices(o)
            payload = sd_api.to_sd_order(
                o, total=total, dispatcher=profiles.dispatcher_phone_for_order(o, _prof))
            sd_api.create_order(payload, dry_run=False)
            posted_vins += len(o.get("vehicles") or [])
        except sd_api.SDError as e:
            kept_orders.append(o)
            failures.append({"number": o.get("number"), "error": str(e)})

    # Staged consolidations -> ADD the new VIN(s) onto an existing posted order. Only
    # TWO things change: the vehicles list (existing kept with their guids, new VINs
    # appended — no per-VIN price) and the order total. Everything else is left intact.
    kept_consol = []
    for entry in _load_json(_consol_path(), []):
        add = entry.get("add") or []
        try:
            existing = sd_api.get_order(entry.get("order_guid"))
            new_vehicles = [
                {**{k: a[k] for k in ("vin", "make", "model", "year") if a.get(k) is not None},
                 "type": config.vehicle_type(a.get("model"))}
                for a in add]
            merge = sd_api.build_vehicles_merge(existing, new_vehicles)
            # New total carrier payment: explicit override, else current SD total + added rates.
            if entry.get("price_override") is not None:
                merge["price"] = round(float(entry["price_override"]), 2)
            else:
                base = float(existing.get("price") or 0)
                added = sum(float(a["price"]) for a in add if a.get("price") is not None)
                merge["price"] = round(base + added, 2)
            sd_api.patch_order(entry["order_guid"], merge)   # merge-patch: only vehicles + price
            posted_vins += len(add)
        except Exception as e:                          # noqa: BLE001
            kept_consol.append(entry)
            failures.append({"number": entry.get("number"), "error": str(e)})

    # Clear the board (KEEP spares). Failed items are kept so they can be retried.
    if path:
        if kept_orders:
            _save_json(path, kept_orders)
        else:
            for fp in glob.glob(os.path.join(_orders_dir(), "*.json")):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    try:
        if os.path.exists(_search_path()):
            os.remove(_search_path())                      # SD matches are informational
    except OSError:
        pass
    if kept_consol:
        _save_json(_consol_path(), kept_consol)
    else:
        try:
            if os.path.exists(_consol_path()):
                os.remove(_consol_path())
        except OSError:
            pass
    # Fully posted with nothing left -> reset to the blank "Excel +" state (spares stay).
    if not kept_orders and not kept_consol:
        try:
            if os.path.exists(_active_excel_path()):
                os.remove(_active_excel_path())
        except OSError:
            pass
        _excel_cache_clear()                            # this dispatcher's board fully posted

    return {"ok": True, "posted_vins": posted_vins, "failed": failures}


# ----------------------- editing the staged batch -----------------------
MAX_TRUCK = 8


def _active_path() -> str:
    files = _staged_files()
    if not files:
        raise HTTPException(400, "No staged batch yet — run --create first.")
    return files[0]


def _load_batch():
    path = _active_path()
    with open(path, encoding="utf-8") as f:
        return path, json.load(f)


def _save_batch(path, batch):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(batch, f, indent=2, default=str)


# --------------------------- spare workspace I/O ---------------------------
# Keys an entry carries IN ADDITION to its vehicle fields (route snapshot + origin).
_SPARE_EXTRA = ("pickup", "delivery", "transport_type", "inspection_type",
                "instructions", "from_number")


def _load_spares() -> list:
    if os.path.exists(_spares_path()):
        try:
            with open(_spares_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []
    return []


def _save_spares(spares):
    os.makedirs(os.path.dirname(_spares_path()), exist_ok=True)
    with open(_spares_path(), "w", encoding="utf-8") as f:
        json.dump(spares, f, indent=2, default=str)


def _spare_vehicle(entry) -> dict:
    """Strip a spare entry back down to just its vehicle fields (drop the route
    snapshot) so it can be re-added to an order."""
    return {k: v for k, v in entry.items() if k not in _SPARE_EXTRA}


def _unique_number(batch, base) -> str:
    """A free order number: `base` if available, else base's root with a -N suffix."""
    existing = {o.get("number") for o in batch}
    if base and base not in existing:
        return base
    root = (base or "SPARE").split("-", 1)[0]
    n = 2
    while f"{root}-{n}" in existing:
        n += 1
    return f"{root}-{n}"


def _route_of(pickup, delivery) -> tuple:
    pv = (pickup or {}).get("venue") or {}
    dv = (delivery or {}).get("venue") or {}
    return (pv.get("zip", ""), dv.get("zip", ""))


def _route(o) -> tuple:
    return _route_of(o.get("pickup"), o.get("delivery"))


def _price(vehicles) -> float | None:
    ps = []
    for v in vehicles:
        p = v.get("price")
        if p is None:
            continue
        try:
            ps.append(float(p))
        except (TypeError, ValueError):
            pass
    return round(sum(ps), 2) if ps else None


def _needby(vehicles):
    """Soonest (need_by_ts, raw string) across an order's vehicles, or (None, None)."""
    import sd_api
    best = None
    for v in vehicles:
        ts = v.get("need_by_ts")
        if ts is None:
            dt = sd_api._parse_needby(v.get("need_by"))
            ts = dt.timestamp() if dt else None
        if ts is not None and (best is None or ts < best[0]):
            best = (ts, v.get("need_by"))
    return best if best else (None, None)


def _recompute(o):
    """Refresh an order's price + soonest need-by from its current vehicles."""
    o["price"] = _price(o["vehicles"])
    ts, raw = _needby(o["vehicles"])
    o["need_by_ts"], o["need_by"] = ts, raw


def _find(batch, number):
    return next((o for o in batch if o.get("number") == number), None)


def _orders_of(batch, vinset):
    """[(order, [its selected vehicles])] for every order holding any of `vinset`."""
    out = []
    for o in batch:
        m = [v for v in o["vehicles"] if v.get("vin") in vinset]
        if m:
            out.append((o, m))
    return out


@app.post("/api/split")
def api_split(body: dict = Body(...)):
    """Pull the selected VINs (from one or more orders on the SAME route) into a
    brand-new shipment, numbered off the first source (…-2/-3)."""
    vins = set(body.get("vins") or [])
    if not vins:
        raise HTTPException(400, "select one or more VINs first")
    path, batch = _load_batch()
    hits = _orders_of(batch, vins)
    if not hits:
        raise HTTPException(400, "those VINs aren't on the board")
    if len({_route(o) for o, _ in hits}) > 1:
        raise HTTPException(400, "those VINs are on different routes — they can't share a truck")
    moved = [v for _, m in hits for v in m]
    if len(moved) > MAX_TRUCK:
        raise HTTPException(400, f"a truck holds at most {MAX_TRUCK} — you picked {len(moved)}")
    if len(hits) == 1 and len(moved) == len(hits[0][0]["vehicles"]):
        raise HTTPException(400, "that's the entire shipment — nothing to split off")
    template = hits[0][0]
    root = template["number"].split("-", 1)[0]
    existing = {o["number"] for o in batch}
    n = 2
    while f"{root}-{n}" in existing:
        n += 1
    new_num = f"{root}-{n}"
    movedset = {v.get("vin") for v in moved}
    new_order = {k: v for k, v in template.items()
                 if k not in ("vehicles", "price", "number", "need_by", "need_by_ts")}
    new_order.update(number=new_num, vehicles=moved)
    _recompute(new_order)
    insert_at = batch.index(template) + 1
    for o, _ in hits:
        o["vehicles"] = [v for v in o["vehicles"] if v.get("vin") not in movedset]
        _recompute(o)
    batch.insert(insert_at, new_order)
    batch[:] = [o for o in batch if o["vehicles"] or o is new_order]
    _save_batch(path, batch)
    return {"ok": True, "new_number": new_num}


@app.post("/api/combine")
def api_combine(body: dict = Body(...)):
    """Merge the selected VINs (across orders, SAME route) into ONE existing
    shipment — the first source — keeping that shipment's number."""
    vins = set(body.get("vins") or [])
    if len(vins) < 2:
        raise HTTPException(400, "pick at least two VINs to combine")
    path, batch = _load_batch()
    hits = _orders_of(batch, vins)
    if not hits:
        raise HTTPException(400, "those VINs aren't on the board")
    if len({_route(o) for o, _ in hits}) > 1:
        raise HTTPException(400, "those VINs are on different routes — they can't share a truck")
    if len(hits) < 2:
        raise HTTPException(400, "those VINs are already on the same shipment")
    moved = [v for _, m in hits for v in m]
    target = hits[0][0]
    movedset = {v.get("vin") for v in moved}
    keep = [v for v in target["vehicles"] if v.get("vin") not in movedset]
    if len(keep) + len(moved) > MAX_TRUCK:
        raise HTTPException(400, f"that shipment would exceed {MAX_TRUCK} units")
    for o, _ in hits:
        o["vehicles"] = [v for v in o["vehicles"] if v.get("vin") not in movedset]
    target["vehicles"] = keep + moved
    for o, _ in hits:
        _recompute(o)
    _recompute(target)
    batch[:] = [o for o in batch if o["vehicles"]]
    _save_batch(path, batch)
    return {"ok": True, "into": target["number"]}


@app.post("/api/move")
def api_move(body: dict = Body(...)):
    """Move VINs from one order to another EXISTING order. Only allowed when both
    share the same pickup AND delivery, and the target stays within truck capacity."""
    frm, to, vins = body.get("from"), body.get("to"), set(body.get("vins") or [])
    if not frm or not to or not vins:
        raise HTTPException(400, "from, to and vins are required")
    if frm == to:
        return {"ok": True}
    path, batch = _load_batch()
    src, dst = _find(batch, frm), _find(batch, to)
    if not src or not dst:
        raise HTTPException(404, "order not found")
    if _route(src) != _route(dst):
        raise HTTPException(400, "different pickup/delivery — those can't share a truck")
    moving = [v for v in src["vehicles"] if v.get("vin") in vins]
    if not moving:
        raise HTTPException(400, "those VINs aren't in the source order")
    if len(dst["vehicles"]) + len(moving) > MAX_TRUCK:
        raise HTTPException(400, f"that would exceed {MAX_TRUCK} units on the target truck")
    src["vehicles"] = [v for v in src["vehicles"] if v.get("vin") not in vins]
    dst["vehicles"] += moving
    _recompute(src)
    _recompute(dst)
    if not src["vehicles"]:
        batch.remove(src)
    _save_batch(path, batch)
    return {"ok": True}


# ----------------------- spare workspace endpoints -----------------------
@app.get("/api/spares")
def api_spares():
    """The set-aside VINs (off the board, won't be posted), newest snapshot first."""
    spares = _load_spares()
    return {"count": len(spares), "spares": spares}


@app.post("/api/spare")
def api_spare(body: dict = Body(...)):
    """Pull the selected VINs off the board into the spare workspace. Each entry
    keeps a snapshot of its route + origin order so it can be returned later."""
    vins = set(body.get("vins") or [])
    if not vins:
        raise HTTPException(400, "select one or more VINs first")
    path, batch = _load_batch()
    spares = _load_spares()
    have = {s.get("vin") for s in spares}
    moved = []
    removed = 0
    for o in batch:
        keep = []
        for v in o["vehicles"]:
            vin = v.get("vin")
            if vin in vins:
                # A selected VIN is ALWAYS pulled off the board — every occurrence, even a
                # duplicate VIN that sits on two shipments, so the shipment actually
                # disappears. We only add ONE spare entry per VIN (the `have` guard), so a
                # VIN that's already spared just drops from the board without a dup entry.
                removed += 1
                if vin not in have:
                    entry = dict(v)
                    entry["pickup"] = o.get("pickup")
                    entry["delivery"] = o.get("delivery")
                    entry["transport_type"] = o.get("transport_type")
                    entry["inspection_type"] = o.get("inspection_type")
                    entry["instructions"] = o.get("instructions")
                    entry["from_number"] = o.get("number")
                    moved.append(entry)
                    have.add(vin)
            else:
                keep.append(v)
        o["vehicles"] = keep
    if not removed:
        raise HTTPException(400, "those VINs aren't on the board")
    for o in batch:
        _recompute(o)
    batch[:] = [o for o in batch if o["vehicles"]]
    spares = moved + spares           # newest first
    _save_batch(path, batch)
    _save_spares(spares)
    return {"ok": True, "moved": len(moved)}


@app.post("/api/spare/restore")
def api_spare_restore(body: dict = Body(...)):
    """Return spared VINs to the board. Placement:
      - `to`: drop into that existing order (same route, within capacity).
      - `mode="new"`: always start a FRESH shipment (no merge), reusing the spare's
        original number when it's free (i.e. it was the sole VIN in its shipment).
      - default: merge onto a same-route order if one exists, else a fresh shipment.
    Returns the order number(s) the VIN(s) landed in."""
    vins = set(body.get("vins") or [])
    to = body.get("to")
    mode = (body.get("mode") or "").strip().lower()
    if not vins:
        raise HTTPException(400, "select one or more spares")
    spares = _load_spares()
    take = [s for s in spares if s.get("vin") in vins]
    if not take:
        raise HTTPException(400, "those VINs aren't in the spare workspace")
    path, batch = _load_batch()
    landed = []
    for s in take:
        veh = _spare_vehicle(s)
        route = _route_of(s.get("pickup"), s.get("delivery"))
        target = None
        if to:
            target = _find(batch, to)
            if not target:
                raise HTTPException(404, "target order not found")
            if _route(target) != route:
                raise HTTPException(400, "different route — that VIN can't ride that truck")
            if len(target["vehicles"]) >= MAX_TRUCK:
                raise HTTPException(400, f"that truck is full ({MAX_TRUCK})")
        elif mode != "new":
            target = next((o for o in batch if _route(o) == route
                           and len(o["vehicles"]) < MAX_TRUCK), None)
        if target:
            target["vehicles"].append(veh)
            _recompute(target)
            landed.append(target["number"])
        else:
            num = _unique_number(batch, s.get("from_number"))
            new_order = {
                "number": num,
                "purchase_order_number": "",
                "transport_type": s.get("transport_type") or "OPEN",
                "inspection_type": s.get("inspection_type") or "standard",
                "price": None,
                "instructions": s.get("instructions") or "",
                "pickup": s.get("pickup"),
                "delivery": s.get("delivery"),
                "vehicles": [veh],
            }
            _recompute(new_order)
            batch.append(new_order)
            landed.append(num)
    spares = [s for s in spares if s.get("vin") not in vins]
    _save_batch(path, batch)
    _save_spares(spares)
    return {"ok": True, "restored": len(take),
            "number": landed[0] if landed else None, "numbers": landed}


# ----------------------- consolidation (Super Dispatch) -----------------------
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default
    return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def _current_board() -> list:
    """Newest staged board, or [] if none yet (search must tolerate an empty board)."""
    files = _staged_files()
    if not files:
        return []
    try:
        with open(files[0], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


@app.post("/api/consolidation/search")
def api_consolidation_search(body: dict = Body(...)):
    """Given a list of VINs (array or comma/space/newline-separated string), find the
    existing Super Dispatch orders they sit on, and flag posted orders already running
    one of the board's routes. Caches the result to consolidation_search.json so the
    route view can render matches without re-hitting the API."""
    import consolidation
    raw = body.get("vins")
    if isinstance(raw, str):
        vins = [v for v in re.split(r"[,\s]+", raw) if v]
    else:
        vins = [str(v).strip() for v in (raw or []) if str(v).strip()]
    if not vins:
        raise HTTPException(400, "paste one or more VINs to search")
    try:
        throttle = (float(body["throttle_s"]) if body.get("throttle_s") is not None
                    else consolidation.DEFAULT_THROTTLE_S)
    except (TypeError, ValueError):
        throttle = consolidation.DEFAULT_THROTTLE_S

    res = consolidation.find_orders_for_vins(vins, throttle_s=throttle)
    annotated = consolidation.match_against_routes(res.orders, _current_board(), my_vins=vins)
    payload = {
        "orders": annotated,
        "checked_vins": res.checked_vins,
        "found_vins": res.found_vins,
        "not_found_vins": res.not_found_vins,
        "errors": res.errors,
        "auth_error": res.auth_error,
    }
    _save_json(_search_path(), payload)
    return payload


@app.get("/api/consolidation/matches")
def api_consolidation_matches():
    """Latest search results + the staged consolidation queue (for the route view)."""
    return {"search": _load_json(_search_path(), {"orders": []}),
            "staged": _load_json(_consol_path(), [])}


@app.post("/api/consolidation/stage")
def api_consolidation_stage(body: dict = Body(...)):
    """Drag-drop staging: pull the given board VINs off the board and queue them to be
    added to the matched existing order `order_guid`. NOTHING is sent to Super Dispatch
    here — the queue (consolidations.json) is consumed later by the post-all flow."""
    guid = body.get("order_guid")
    vins = set(body.get("vins") or [])
    if not guid or not vins:
        raise HTTPException(400, "order_guid and vins are required")
    search = _load_json(_search_path(), {"orders": []})
    order = next((o for o in search.get("orders", []) if o.get("guid") == guid), None)
    if not order:
        raise HTTPException(400, "that order isn't in the latest search — run a VIN search first")
    if order.get("loadboard_status") == "accepted":
        raise HTTPException(400, "that order is already accepted by a carrier — VINs can't be added to it")

    path, batch = _load_batch()
    moved = []
    for o in batch:
        keep = []
        for v in o["vehicles"]:
            if v.get("vin") in vins:
                moved.append({**v, "from_number": o.get("number"),
                              "from_pickup": o.get("pickup"), "from_delivery": o.get("delivery")})
            else:
                keep.append(v)
        o["vehicles"] = keep
    if not moved:
        raise HTTPException(400, "those VINs aren't on the board")
    for o in batch:
        _recompute(o)
    batch[:] = [o for o in batch if o["vehicles"]]
    _save_batch(path, batch)

    staged = _load_json(_consol_path(), [])
    entry = next((s for s in staged if s.get("order_guid") == guid), None)
    if not entry:
        entry = {"order_guid": guid, "number": order.get("number"),
                 "pickup": order.get("pickup"), "delivery": order.get("delivery"),
                 "existing_vehicles": order.get("vehicles") or [], "add": []}
        staged.append(entry)
    have = {a.get("vin") for a in entry["add"]}
    for m in moved:
        if m.get("vin") not in have:
            entry["add"].append(m)
            have.add(m.get("vin"))
    _save_json(_consol_path(), staged)
    return {"ok": True, "staged": len(moved), "into": order.get("number")}


@app.post("/api/consolidation/unstage")
def api_consolidation_unstage(body: dict = Body(...)):
    """Undo staging: return the given VINs from the consolidation queue to the board
    (merge onto a same-route order, else a fresh one — mirrors spare restore)."""
    vins = set(body.get("vins") or [])
    if not vins:
        raise HTTPException(400, "select one or more staged VINs")
    staged = _load_json(_consol_path(), [])
    take = []
    for entry in staged:
        kept = []
        for a in entry.get("add", []):
            (take if a.get("vin") in vins else kept).append(a)
        entry["add"] = kept
    if not take:
        raise HTTPException(400, "those VINs aren't staged for consolidation")
    staged = [s for s in staged if s.get("add")]

    path, batch = _load_batch()
    for a in take:
        veh = {k: v for k, v in a.items()
               if k not in ("from_number", "from_pickup", "from_delivery")}
        route = _route_of(a.get("from_pickup"), a.get("from_delivery"))
        target = next((o for o in batch if _route(o) == route
                       and len(o["vehicles"]) < MAX_TRUCK), None)
        if target:
            target["vehicles"].append(veh)
            _recompute(target)
        else:
            new_order = {
                "number": _unique_number(batch, a.get("from_number")),
                "purchase_order_number": "",
                "transport_type": "OPEN", "inspection_type": "standard",
                "price": None, "instructions": "",
                "pickup": a.get("from_pickup"), "delivery": a.get("from_delivery"),
                "vehicles": [veh],
            }
            _recompute(new_order)
            batch.append(new_order)
    _save_batch(path, batch)
    _save_json(_consol_path(), staged)
    return {"ok": True, "restored": len(take)}


# ----------------------- pricing -----------------------
def _effective_prices(order: dict):
    """(total, per_unit). With a `price_override`, the total is that override split
    evenly across the order's units; otherwise the total is the per-VIN sum and
    per_unit is None (each vehicle keeps its own price)."""
    veh = order.get("vehicles") or []
    n = len(veh)
    ov = order.get("price_override")
    if ov is not None and n:
        ov = float(ov)
        return round(ov, 2), round(ov / n, 2)
    ps = [float(v["price"]) for v in veh if v.get("price") is not None]
    return (round(sum(ps), 2) if ps else order.get("price")), None


def _parse_price(total):
    if total in (None, "", False):
        return None
    try:
        return round(float(total), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid price")


@app.post("/api/price")
def api_price(body: dict = Body(...)):
    """Set (or clear, with null/blank) a shipment's TOTAL price override — it's split
    evenly across the order's units when posting. Clearing reverts to the per-VIN
    (Excel) prices, which are the placeholder."""
    number = body.get("number")
    if not number:
        raise HTTPException(400, "number is required")
    price = _parse_price(body.get("total"))
    path, batch = _load_batch()
    o = _find(batch, number)
    if not o:
        raise HTTPException(404, "order not found")
    if price is None:
        o.pop("price_override", None)
    else:
        o["price_override"] = price
    _save_batch(path, batch)
    return {"ok": True, "price_override": o.get("price_override")}


@app.post("/api/consolidation/price")
def api_consolidation_price(body: dict = Body(...)):
    """Set/clear the price override for a matched SD order — only allowed once at least
    one Excel VIN has been staged onto it (price is otherwise locked to SuperDispatch).
    Split evenly across all units (existing + added) when posting."""
    guid = body.get("order_guid")
    if not guid:
        raise HTTPException(400, "order_guid is required")
    staged = _load_json(_consol_path(), [])
    entry = next((s for s in staged if s.get("order_guid") == guid), None)
    if not entry or not entry.get("add"):
        raise HTTPException(400, "price is locked — add an Excel VIN to this order first")
    price = _parse_price(body.get("total"))
    if price is None:
        entry.pop("price_override", None)
    else:
        entry["price_override"] = price
    _save_json(_consol_path(), staged)
    return {"ok": True, "price_override": entry.get("price_override")}


# ---- Recorded shipments (the local Super Dispatch mirror) --------------------
# Read-only views over recorder.db, populated by recorder_backfill.py (and, later,
# the live webhook feed). Kept self-contained: a missing/empty DB returns empties
# rather than 500ing, so the tab degrades gracefully before the first backfill.
@app.get("/api/recorded")
def api_recorded(status: Optional[str] = None, q: Optional[str] = None,
                 limit: int = 500, offset: int = 0,
                 sort: str = "updated", dir: str = "desc"):
    try:
        import recorder_db as rdb
    except Exception as e:                                   # noqa: BLE001
        return {"orders": [], "total": 0, "counts": {}, "error": f"recorder unavailable: {e}"}
    try:
        con = rdb.connect()
    except Exception as e:                                   # noqa: BLE001
        return {"orders": [], "total": 0, "counts": {}, "error": str(e)}
    try:
        return {
            "orders": rdb.list_orders(con, status=status, q=q,
                                      limit=min(limit, 2000), offset=offset,
                                      sort=sort, direction=dir),
            "total": rdb.total(con),
            "counts": rdb.counts_by_status(con),
            "meta": _recorded_meta(con),
        }
    finally:
        con.close()


def _recorded_meta(con) -> dict:
    try:
        row = con.execute("SELECT value FROM meta WHERE key='last_backfill'").fetchone()
        import json as _json
        return _json.loads(row["value"]) if row else {}
    except Exception:                                        # noqa: BLE001
        return {}


# ---- Webhook passthrough to the direct-pickup listener -----------------------
# Super Dispatch posts webhooks to test.wastake.com/webhooks/superdispatch, but the
# Cloudflare tunnel routes that hostname here (:8001), not to the listener (:8077) —
# so the events 404'd. Forward them to the local listener, which validates the token,
# dedups, and fans out to BOTH the pickup pipeline and the recorder mirror. (Proper
# long-term fix: a path-based tunnel ingress rule /webhooks/* -> :8077.)
_LISTENER_WEBHOOK = "http://127.0.0.1:8077/webhooks/superdispatch"
_SD_TOKEN_HEADER = "x-super-dispatch-verification-token"


@app.post("/webhooks/superdispatch")
async def webhook_forward(request: Request):
    import requests as _requests
    body = await request.body()
    headers = {"Content-Type": request.headers.get("content-type", "application/json")}
    tok = request.headers.get(_SD_TOKEN_HEADER)
    if tok:
        headers[_SD_TOKEN_HEADER] = tok
    try:
        r = _requests.post(_LISTENER_WEBHOOK, data=body, headers=headers, timeout=10)
        return PlainTextResponse(r.text or "ok", status_code=r.status_code)
    except Exception as e:                                   # noqa: BLE001
        # Listener down/unreachable -> 503 so Super Dispatch retries later.
        return PlainTextResponse(f"listener unavailable: {e}", status_code=503)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(paths.resource_path("static", "index.html"), encoding="utf-8") as fh:
        return fh.read()


# ---- "App" tab: embed the app-delivery dashboard ----------------------------
# The Tesla drop-off/pickup dashboard runs as its own local service on :8011
# (app-delivery-web). We reverse-proxy it under /app/ so it appears as an "App" tab
# inside shipments.wastake.com instead of a separate subdomain. The dashboard uses
# relative URLs (api, photos, img), so they resolve under /app/ here.
_APP_DASH = os.getenv("APP_DASH_URL", "http://127.0.0.1:8011")


@app.get("/app")
def _app_index():
    return RedirectResponse("/app/")


@app.get("/app/{path:path}")
def _app_proxy(path: str, request: Request):
    import requests as _requests
    url = f"{_APP_DASH}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    try:
        r = _requests.get(url, timeout=20)
    except Exception as e:                       # the dashboard service is down
        return PlainTextResponse(f"App dashboard unreachable: {e}", status_code=502)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("Content-Type", "application/octet-stream"))


def _redirect_output_to_logfile():
    """The windowed (no-console) build has nowhere to print, so send stdout/stderr to
    a log file in the data dir — both to keep uvicorn's logging happy and to give us
    something to read if something goes wrong."""
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        f = open(os.path.join(paths.DATA_DIR, "app.log"), "a",
                 buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = f
        sys.stderr = f
    except Exception:                                   # noqa: BLE001 - never block startup
        pass


def _confirm_quit_during_run() -> bool:
    """Native Yes/No prompt before quitting mid-run. Returns True to proceed with the
    quit. If no dialog is available (non-Windows / no GUI), allows the quit."""
    try:
        import ctypes
        MB_YESNO, MB_ICONWARNING, MB_TOPMOST, IDYES = 0x4, 0x30, 0x40000, 6
        r = ctypes.windll.user32.MessageBoxW(
            0,
            "A run is in progress.\n\nQuitting will stop it and close the browser it's "
            "using. Any VINs not yet staged won't be saved.\n\nQuit anyway?",
            "TFI Shipment Creator",
            MB_YESNO | MB_ICONWARNING | MB_TOPMOST,
        )
        return r == IDYES
    except Exception:                                 # noqa: BLE001
        return True


def _run_tray(url, server):
    """Show a system-tray icon so the user can re-open the GUI or Quit — the windowed
    app has no console to close. Blocks until Quit; falls back to a plain wait loop if
    pystray isn't available."""
    import webbrowser
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:                                   # noqa: BLE001 - tray optional
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True
        return

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, 58, 58], radius=14, fill=(79, 140, 255, 255))
    d.text((24, 20), "S", fill="white")

    def _open(icon, item):
        webbrowser.open(url)

    def _quit(icon, item):
        # if ANY dispatcher's pull is running, confirm — then stop them all (and the
        # shared Chrome) cleanly so nothing keeps running orphaned after the app exits.
        any_running = any(st["progress"].get("running") for st in _runs.values())
        if any_running:
            if not _confirm_quit_during_run():
                return                                # user chose to keep it running
            _terminate_all_runs()
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Shipment Creator", _open, default=True),
        pystray.MenuItem("Quit", _quit),
    )
    pystray.Icon("ShipmentCreator", img, "TFI Shipment Creator", menu).run()


_mutex_handle = None        # keep the single-instance mutex alive for the process lifetime


def _claim_single_instance() -> bool:
    """First/only instance? Returns True to proceed. If another instance is already
    running, opens that one's GUI in the browser and returns False so the caller exits
    instead of starting a duplicate server. Frozen build only — dev runs are untouched."""
    global _mutex_handle
    if not paths.is_frozen() or os.name != "nt":
        return True
    import ctypes
    import webbrowser
    ERROR_ALREADY_EXISTS = 183
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                        "TFI_ShipmentCreator_singleton")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:                                          # point the user at the live instance
            with open(os.path.join(paths.DATA_DIR, "instance.json"), encoding="utf-8") as f:
                webbrowser.open(json.load(f)["url"])
        except Exception:                             # noqa: BLE001
            pass
        return False
    return True


def _write_instance_info(url: str, port: int) -> None:
    """Record where this instance is serving so a second launch can open it."""
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        with open(os.path.join(paths.DATA_DIR, "instance.json"), "w", encoding="utf-8") as f:
            json.dump({"url": url, "port": port}, f)
    except Exception:                                 # noqa: BLE001
        pass


def _serve():
    """Start the web server. The packaged (frozen) app runs WINDOWED: no console, the
    GUI auto-opens in the browser, and a tray icon (Open / Quit) controls it. It also
    falls back to a free port if the preferred one is busy. In dev this stays the plain
    console server on $PORT (web.bat opens the browser) so nothing changes for devs."""
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    windowed = paths.is_frozen()
    auto = windowed or os.getenv("SC_OPEN_BROWSER", "").lower() in {"1", "true", "yes"}

    url = f"http://127.0.0.1:{port}"
    if auto:
        import socket
        import webbrowser
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:  # preferred port already in use
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as f:
                    f.bind(("127.0.0.1", 0))
                    port = f.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    _write_instance_info(url, port)                     # so a 2nd launch opens this one

    # Cap the graceful-shutdown wait: the page holds an open /api/events SSE stream,
    # and uvicorn will otherwise wait forever for it to close on Ctrl+C — that's the
    # "stall". 3s, then it force-closes connections and exits.
    if windowed:
        _redirect_output_to_logfile()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                                timeout_graceful_shutdown=3)
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _run_tray(url, server)                          # blocks until Quit -> exit
    else:
        print(f"Shipment Creator GUI -> {url}  (Ctrl+C to stop)")
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                timeout_graceful_shutdown=3)
        uvicorn.Server(config).run()


if __name__ == "__main__":
    # When frozen, /api/run re-invokes this exe with --pipeline to run the pipeline
    # in a subprocess (same streamed-stdout progress model as the dev `python main.py`).
    if "--pipeline" in sys.argv:
        sys.argv.remove("--pipeline")
        import main as _pipeline
        _pipeline.main()
    elif not _claim_single_instance():
        sys.exit(0)                                     # another instance is already up
    else:
        _serve()
