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
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

import paths

_HERE = os.path.dirname(os.path.abspath(__file__))
ORDERS_DIR = os.path.join(paths.OUTPUT_DIR, "orders")
# The spare workspace lives off the board in its own file: VINs the user set aside
# to keep but not post. Each entry snapshots its route so it can still show where
# it was headed and be returned to the board later.
SPARES_PATH = os.path.join(paths.OUTPUT_DIR, "spares.json")
# Consolidation: last VIN-search results, and the queue of VIN->existing-order merges
# the user has staged (sent later by the not-yet-built "post all shipments" flow).
SEARCH_PATH = os.path.join(paths.OUTPUT_DIR, "consolidation_search.json")
CONSOL_PATH = os.path.join(paths.OUTPUT_DIR, "consolidations.json")
# Which Excel the current board was built from (path + sheet). Stamped when a run
# starts so the tally denominator reflects the sheet you actually ran, not the
# config default; persisted so it survives a server restart / page refresh.
ACTIVE_EXCEL_PATH = os.path.join(paths.OUTPUT_DIR, "active_excel.json")

app = FastAPI(title="Shipment Creator")


def _staged_files() -> list[str]:
    return sorted(glob.glob(os.path.join(ORDERS_DIR, "*.json")),
                  key=os.path.getmtime, reverse=True)


def _board_signature() -> tuple:
    """A cheap fingerprint of everything the board renders: the (name, mtime) of the
    active staged batch plus the spares + consolidation files. Changes whenever a run
    writes new shipments, a scan writes matches, or an edit lands — so the SSE stream
    can tell the page to refresh itself. Including the basename catches a brand-new
    staged batch file (new timestamped name), not just a rewrite of the current one."""
    paths = [SPARES_PATH, SEARCH_PATH, CONSOL_PATH]
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
    sync). One-way, polls file mtimes ~1s, and the browser auto-reconnects."""
    async def gen():
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
    path = os.path.join(ORDERS_DIR, file) if file else files[0]
    if not os.path.exists(path):
        raise HTTPException(404, "staged file not found")
    with open(path, encoding="utf-8") as fh:
        orders = json.load(fh)
    for o in orders:
        _annotate_dates(o)
    return {"file": os.path.basename(path), "count": len(orders), "orders": orders}


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


_run_lock = threading.Lock()


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
_run_log: list = []                                   # this run's console output (for the stream)
_run_progress = {"running": False, "pulled": 0, "total": 0}
_run_proc = None                                      # the live pipeline subprocess (for clean kill on Quit)


def _terminate_run():
    """Kill the in-progress pipeline subprocess and everything it spawned (its Chrome
    included — taskkill /T walks the whole tree). No-op if nothing is running. Used by
    the tray Quit so a mid-pull exit doesn't orphan a background process."""
    proc = _run_proc
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


@app.post("/api/run")
def api_run(opts: dict = Body(...)):
    """Start the pipeline and stream its console output. The subprocess is owned by
    a background thread (not this request), so it keeps running and reporting
    progress even if the client disconnects/refreshes. Only one run at a time."""
    import profiles
    if not profiles.active_id():
        raise HTTPException(400, "Select a dispatcher profile before running an Excel.")
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "A run is already in progress.")
    try:
        cmd = _build_cmd(opts)
    except (ValueError, TypeError) as e:
        _run_lock.release()
        raise HTTPException(400, str(e))

    # remember which sheet this board is being built from, so /api/tally counts
    # against the Excel you actually ran (not the config default).
    import config
    _save_json(ACTIVE_EXCEL_PATH, {
        "path": (opts.get("excel") or "").strip() or config.DEFAULT_EXCEL,
        "sheet": (str(opts.get("sheet")).strip() or None) if opts.get("sheet") else None,
    })

    _run_log.clear()
    _run_progress.update(running=True, pulled=0, total=0)

    # don't let the pipeline subprocess pop up its own console window (its output is
    # captured via the pipe anyway); harmless in dev, essential for the windowed build.
    _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def _drain():
        global _run_proc
        try:
            _run_log.append(f"$ {' '.join(cmd)}\n\n")
            proc = subprocess.Popen(
                cmd, cwd=_HERE, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=_no_window,
            )
            _run_proc = proc                          # expose for Quit-time termination
            for line in proc.stdout:
                _run_log.append(line)
                m = _TOTAL_RE.search(line)
                if m:
                    _run_progress["total"] = int(m.group(1))
                m = _PULLED_RE.search(line)
                if m:
                    _run_progress["pulled"] = int(m.group(1))
            proc.wait()
            _run_log.append(f"\n[finished — exit code {proc.returncode}]\n")
        finally:
            _run_proc = None
            _run_progress["running"] = False
            _run_lock.release()

    threading.Thread(target=_drain, daemon=True).start()

    def gen():
        idx = 0
        while True:
            while idx < len(_run_log):
                yield _run_log[idx]
                idx += 1
            if not _run_progress["running"]:
                while idx < len(_run_log):     # flush any final lines after it stopped
                    yield _run_log[idx]
                    idx += 1
                break
            time.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/api/run/status")
def api_run_status():
    """Current run progress (server-owned), so a freshly-loaded page can show the
    progress bar mid-run instead of losing it on refresh."""
    return dict(_run_progress)


@app.get("/api/env")
def api_env():
    import config
    return {"sd_env": config.SD_ENV, "default_excel": config.DEFAULT_EXCEL}


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
    _excel_cache.update(path=None, sheet=None, mtime=None, vins=set())   # filter changed
    return {"ok": True, "active": profiles.active_id()}


@app.post("/api/profile/save")
def api_profile_save(body: dict = Body(...)):
    """Save a dispatcher's phone (fills the <dispatcher> token) and pickup-state filter
    from the Settings tab. `states` accepts 'VA MD GA FL' or a list."""
    import profiles
    try:
        p = profiles.save_profile((body or {}).get("id"),
                                  phone=body.get("phone"), states=body.get("states"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _excel_cache.update(path=None, sheet=None, mtime=None, vins=set())   # state filter changed
    return {"ok": True, "profile": p}


@app.get("/api/profile-image/{pid}")
def api_profile_image(pid: str):
    """Serve a dispatcher's avatar from profiles/images/<id>.<ext> (case-insensitive
    on the filename, so 'Soyo.png' matches profile id 'soyo'), if present."""
    import profiles
    base = os.path.join(profiles.PROFILES_DIR, "images")
    exts = {"png", "jpg", "jpeg", "webp", "gif"}
    if os.path.isdir(base):
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
    _excel_cache.update(path=None, sheet=None, mtime=None, vins=set())   # force recount
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


def _pick_excel_native() -> Optional[str]:
    """Open the native Windows 'Open file' dialog filtered to spreadsheets and return
    the chosen absolute path, or None if cancelled. This IS the user's machine, so a
    server-side dialog gives us the real filesystem path a browser <input> can't."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID), ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(2048)
    flt = ctypes.create_unicode_buffer("Excel files\0*.xlsx;*.xlsm\0All files\0*.*\0\0")
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 2048
    ofn.lpstrFilter = ctypes.cast(flt, wintypes.LPCWSTR)
    ofn.lpstrTitle = "Select the Excel sheet to run"
    ofn.Flags = 0x1000 | 0x800 | 0x80000              # FILEMUSTEXIST | PATHMUSTEXIST | EXPLORER
    ctypes.windll.ole32.CoInitialize(None)
    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    finally:
        ctypes.windll.ole32.CoUninitialize()
    return buf.value or None if ok else None


@app.post("/api/pick-excel")
def api_pick_excel():
    """Pop the native file dialog so the user can choose the Excel to run. Returns the
    absolute path (or path=null if they cancelled)."""
    try:
        path = _pick_excel_native()
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(500, f"Couldn't open the file picker: {e}")
    return {"path": path, "name": os.path.basename(path) if path else ""}


@app.post("/api/run/terminate")
def api_run_terminate():
    """Stop the in-progress pipeline (and its Chrome). Waits briefly for the drain
    thread to release the run lock so a follow-up /api/run won't 409."""
    _terminate_run()
    for _ in range(40):
        if not _run_progress.get("running"):
            break
        time.sleep(0.1)
    return {"ok": True, "running": _run_progress.get("running", False)}


@app.post("/api/reset")
def api_reset():
    """Start over: stop any run, then clear the staged board, the active-Excel marker,
    spares and consolidations — so the GUI returns to the blank 'Excel +' state and you
    can run a completely different sheet from scratch."""
    _terminate_run()
    removed = 0
    for f in glob.glob(os.path.join(ORDERS_DIR, "*.json")):
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    for p in (ACTIVE_EXCEL_PATH, SPARES_PATH, SEARCH_PATH, CONSOL_PATH):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    _excel_cache.update(path=None, sheet=None, mtime=None, vins=set())
    return {"ok": True, "removed_batches": removed}


# ----------------------- tally + post (SuperDispatch) -----------------------
_excel_cache = {"path": None, "sheet": None, "mtime": None, "vins": set()}


def _active_excel() -> tuple[Optional[str], Optional[str]]:
    """(path, sheet) of the Excel the current board was built from, or (None, None) if
    no run has been started yet. We do NOT fall back to a default sheet: an empty active
    Excel makes the GUI show the 'Excel +' picker instead of counting against (and
    resuming) some sheet the user never chose."""
    saved = _load_json(ACTIVE_EXCEL_PATH, {})
    if not isinstance(saved, dict) or not saved.get("path"):
        return None, None
    return saved.get("path"), saved.get("sheet")


def _excel_vins() -> set:
    """The set of usable VINs in the active Excel (cached by path+sheet+mtime). Empty
    when no Excel has been chosen/run yet."""
    path, sheet = _active_excel()
    if not path:
        return set()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return set()
    if (_excel_cache["path"] == path and _excel_cache["sheet"] == sheet
            and _excel_cache["mtime"] == mt):
        return _excel_cache["vins"]
    try:
        import excel_ingest
        import profiles
        rows, _ = excel_ingest.read_rows(path, sheet)
        rows = profiles.filter_rows(rows, profiles.active_profile())   # dispatcher's states only
        vins = {r.vin for r in rows if r.ok and r.vin}
    except Exception:                                   # noqa: BLE001
        vins = set()
    _excel_cache.update(path=path, sheet=sheet, mtime=mt, vins=vins)
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
    processed = len(excel & seen) if total else len(seen)
    path, _sheet = _active_excel()
    return {"processed": processed, "total": total,
            "running": _run_progress["running"],
            "active_excel": path,
            "active_excel_name": os.path.basename(path) if path else "",
            "done": total > 0 and processed >= total}


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
    dispatcher = profiles.dispatcher_phone()
    payloads = []
    for o in board:
        total, _per = _effective_prices(o)             # override total, else sum of rates
        # The exact SuperDispatch create body: one carrier payment (no per-VIN price),
        # the check/15-day payment block, and the instruction templates.
        payloads.append(sd_api.to_sd_order(o, total=total, dispatcher=dispatcher))
    # TODO (next): also read consolidations.json and PATCH staged VINs onto the
    # matched existing SD orders (build_vehicles_merge), then actually POST these.
    consol = _load_json(CONSOL_PATH, [])
    return {"ok": True, "dry_run": True, "count": len(payloads),
            "consolidations": len(consol), "payloads": payloads}


@app.post("/api/post-live")
def api_post_live(body: dict = Body(...)):
    """LIVE — actually POST ONE shipment to SuperDispatch (a prod test of a single
    order). Builds the exact create body for the given order number, fills the active
    dispatcher's phone into the instructions, sends it, and returns the new guid."""
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
    payload = sd_api.to_sd_order(o, total=total, dispatcher=profiles.dispatcher_phone())
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
    import sd_api
    import profiles
    import config
    dispatcher = profiles.dispatcher_phone()

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
            payload = sd_api.to_sd_order(o, total=total, dispatcher=dispatcher)
            sd_api.create_order(payload, dry_run=False)
            posted_vins += len(o.get("vehicles") or [])
        except sd_api.SDError as e:
            kept_orders.append(o)
            failures.append({"number": o.get("number"), "error": str(e)})

    # Staged consolidations -> ADD the new VIN(s) onto an existing posted order. Only
    # TWO things change: the vehicles list (existing kept with their guids, new VINs
    # appended — no per-VIN price) and the order total. Everything else is left intact.
    kept_consol = []
    for entry in _load_json(CONSOL_PATH, []):
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
            for fp in glob.glob(os.path.join(ORDERS_DIR, "*.json")):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    try:
        if os.path.exists(SEARCH_PATH):
            os.remove(SEARCH_PATH)                      # SD matches are informational
    except OSError:
        pass
    if kept_consol:
        _save_json(CONSOL_PATH, kept_consol)
    else:
        try:
            if os.path.exists(CONSOL_PATH):
                os.remove(CONSOL_PATH)
        except OSError:
            pass
    # Fully posted with nothing left -> reset to the blank "Excel +" state (spares stay).
    if not kept_orders and not kept_consol:
        try:
            if os.path.exists(ACTIVE_EXCEL_PATH):
                os.remove(ACTIVE_EXCEL_PATH)
        except OSError:
            pass
        _excel_cache.update(path=None, sheet=None, mtime=None, vins=set())

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
    if os.path.exists(SPARES_PATH):
        try:
            with open(SPARES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []
    return []


def _save_spares(spares):
    os.makedirs(os.path.dirname(SPARES_PATH), exist_ok=True)
    with open(SPARES_PATH, "w", encoding="utf-8") as f:
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
    for o in batch:
        keep = []
        for v in o["vehicles"]:
            vin = v.get("vin")
            if vin in vins and vin not in have:
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
    if not moved:
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
    _save_json(SEARCH_PATH, payload)
    return payload


@app.get("/api/consolidation/matches")
def api_consolidation_matches():
    """Latest search results + the staged consolidation queue (for the route view)."""
    return {"search": _load_json(SEARCH_PATH, {"orders": []}),
            "staged": _load_json(CONSOL_PATH, [])}


@app.post("/api/consolidation/stage")
def api_consolidation_stage(body: dict = Body(...)):
    """Drag-drop staging: pull the given board VINs off the board and queue them to be
    added to the matched existing order `order_guid`. NOTHING is sent to Super Dispatch
    here — the queue (consolidations.json) is consumed later by the post-all flow."""
    guid = body.get("order_guid")
    vins = set(body.get("vins") or [])
    if not guid or not vins:
        raise HTTPException(400, "order_guid and vins are required")
    search = _load_json(SEARCH_PATH, {"orders": []})
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

    staged = _load_json(CONSOL_PATH, [])
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
    _save_json(CONSOL_PATH, staged)
    return {"ok": True, "staged": len(moved), "into": order.get("number")}


@app.post("/api/consolidation/unstage")
def api_consolidation_unstage(body: dict = Body(...)):
    """Undo staging: return the given VINs from the consolidation queue to the board
    (merge onto a same-route order, else a fresh one — mirrors spare restore)."""
    vins = set(body.get("vins") or [])
    if not vins:
        raise HTTPException(400, "select one or more staged VINs")
    staged = _load_json(CONSOL_PATH, [])
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
    _save_json(CONSOL_PATH, staged)
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
    staged = _load_json(CONSOL_PATH, [])
    entry = next((s for s in staged if s.get("order_guid") == guid), None)
    if not entry or not entry.get("add"):
        raise HTTPException(400, "price is locked — add an Excel VIN to this order first")
    price = _parse_price(body.get("total"))
    if price is None:
        entry.pop("price_override", None)
    else:
        entry["price_override"] = price
    _save_json(CONSOL_PATH, staged)
    return {"ok": True, "price_override": entry.get("price_override")}


@app.get("/", response_class=HTMLResponse)
def index():
    with open(paths.resource_path("static", "index.html"), encoding="utf-8") as fh:
        return fh.read()


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
        # if a pull is running, confirm — then stop it (and its Chrome) cleanly so it
        # doesn't keep running orphaned after the app exits.
        if _run_progress.get("running"):
            if not _confirm_quit_during_run():
                return                                # user chose to keep it running
            _terminate_run()
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

    if windowed:
        _redirect_output_to_logfile()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _run_tray(url, server)                          # blocks until Quit -> exit
    else:
        print(f"Shipment Creator GUI -> {url}  (Ctrl+C to stop)")
        uvicorn.run(app, host="127.0.0.1", port=port)


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
