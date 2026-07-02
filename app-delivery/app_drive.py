"""
Drive the Tesla Logistics app to complete In-Transit drop-offs (emulator, via adb).

For each In-Transit shipment, for EACH unit (multi-VIN aware):
  read the VIN off the unit card -> decode_vin (latest delivered shipment -> 4 sides
  + 1 VIN plate + 1 key) -> push the photos -> add them through the PictureSelector
  (All Four Sides / VIN Plate / Key) -> Add. Once every unit has photos:
  Drop Off -> "Subject to Inspection" -> Confirm.

Photo reliability: before each unit the gallery is CLEARED and that VIN's photos are
pushed in a FIXED order (4 sides, then vin, then key), so the picker's cells are
deterministic: indices 0-3 = sides, 4 = vin, 5 = key. The gallery is cleared again
after each unit, so VINs never cross-contaminate.

    python app_drive.py [--confirm] [--max-shipments N]
Without --confirm it does everything EXCEPT the final Confirm (safe dry run) and backs
out of the Drop Off Options sheet.
"""
from __future__ import annotations
import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as cfg                              # TESLA_APP_EMAIL / TESLA_APP_PASSWORD
ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
if not os.path.exists(ADB):
    ADB = "adb"
APP = "com.tesla.logisticsmobile"          # the only app this emulator runs
DROPOFF_DB = os.path.join(HERE, "dropoffs.db")   # ledger of every VIN we drop off
MODEL_BY_CHAR = {"3": "Model 3", "Y": "Model Y", "X": "Model X", "S": "Model S", "C": "Cybertruck"}

# UI dumps dominate the run's wall-clock. The shell `uiautomator dump` cold-starts the
# instrumentation on EVERY call (~2.1s measured, regardless of screen), so we instead
# keep a PERSISTENT uiautomator2 server warm — same accessibility tree, ~0.13s/dump
# (~16x). Set DRIVE_U2=0 to fall back to the shell dump (no agent needed).
SERIAL = os.environ.get("ANDROID_SERIAL", "emulator-5554")
USE_U2 = os.environ.get("DRIVE_U2", "1").strip().lower() not in ("0", "false", "no")
if USE_U2:
    try:
        import uiautomator2 as _u2mod
    except Exception:                          # lib missing -> stay on the shell dump
        _u2mod = None
        USE_U2 = False
_u2 = None                                     # lazily-connected device handle


def _ledger():
    con = sqlite3.connect(DROPOFF_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS dropoffs(
        vin TEXT, shipment TEXT, model TEXT, order_guid TEXT,
        photographed INTEGER, option TEXT, dropped_at TEXT,
        exterior INTEGER, vin_found INTEGER, key_found INTEGER,
        UNIQUE(vin, shipment))""")
    # migrate older DBs that predate the per-section found columns
    have = {r[1] for r in con.execute("PRAGMA table_info(dropoffs)")}
    for col in ("exterior", "vin_found", "key_found"):
        if col not in have:
            con.execute(f"ALTER TABLE dropoffs ADD COLUMN {col} INTEGER")
    return con


def record_dropoffs(units, option, photographed_vins):
    """units = [(vin, shipment_number)]. Records each dropped-off VIN to dropoffs.db,
    including whether each section's photo was actually FOUND (from the decode manifest):
    exterior = the 4 sides, vin_found = a real OCR VIN plate, key_found = a real key
    card (not a fallback)."""
    if units:
        step("dropoff", 5, "commit", vin=units[0][0], shp=units[0][1])
    con = _ledger()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for vin, shp in units:
        model = MODEL_BY_CHAR.get(vin[3].upper() if len(vin) >= 4 else "", "")
        guid = ext = vin_found = key_found = None
        mp = os.path.join(HERE, "out", vin, "manifest.json")
        if os.path.exists(mp):
            try:
                man = json.load(open(mp))
                guid = (man.get("shipment") or {}).get("guid")
                ext = int(man.get("n_sides", 0) >= 4)
                vin_found = int(bool(man.get("vin_plate_found")))
                ks = man.get("key_source") or ""
                key_found = int(ks.startswith("white_key") or ks.startswith("black_key"))
            except Exception:
                pass
        con.execute(
            "INSERT OR IGNORE INTO dropoffs"
            "(vin,shipment,model,order_guid,photographed,option,dropped_at,exterior,vin_found,key_found)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (vin, shp, model, guid, int(vin in photographed_vins), option, now,
             ext, vin_found, key_found))
        log(f"  ledger: {vin} ({model}) {shp} [ext={ext} vin={vin_found} key={key_found}]")
    con.commit()
    con.close()


def _shp_of(desc):
    m = re.search(r"SHP[\w-]+", desc or "")
    return m.group(0) if m else ""
PY = os.path.join(HERE, ".venv", "bin", "python")
START_EMU = os.path.join(HERE, "scripts", "start_emulator.sh")
GAL = "/sdcard/DCIM/Camera"
VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
# Debug screenshots: repo-relative by default; bounded so a 24/7 service never fills
# the disk (override the dir with DRIVE_DEBUG_DIR).
DBG = os.environ.get("DRIVE_DEBUG_DIR", os.path.join(HERE, "out", "_debug"))
DBG_KEEP = 400
os.makedirs(DBG, exist_ok=True)
_shot = 0
# Activity log the dashboard (dashboard.py / app.wastake.com) tails. Bounded so a 24/7
# service never fills the disk.
LOGFILE = os.environ.get("DRIVE_LOG_FILE", os.path.join(HERE, "out", "service.log"))
LOG_MAX_BYTES = 2_000_000


# ------------------------------- adb helpers -------------------------------
def adb(*args, timeout=60):
    return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)


def shell(cmd, timeout=60):
    return adb("shell", cmd, timeout=timeout).stdout


# Pause after a tap whose NEXT step is a wait_until() that polls for a brand-new
# element/screen. The wait_until does the real settling (and re-polls cheaply now that
# dumps are ~0.09s), so this only needs to be long enough for RN to START the
# transition before the first poll — far less than the old fixed 1.5–2.5s waits. NOT
# used where the pause itself gates real state (sheet-dismiss, picker wheels, or the
# option→Confirm tap, where Confirm pre-exists greyed).
NAV_PAUSE = 0.25


def tap(x, y, pause=1.2):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))
    time.sleep(pause)


def swipe(x1, y1, x2, y2, ms=300, pause=1.0):
    adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
    time.sleep(pause)


def back(pause=1.2):
    adb("shell", "input", "keyevent", "KEYCODE_BACK")
    time.sleep(pause)


def shot(tag=""):
    global _shot
    _shot += 1
    p = os.path.join(DBG, f"{_shot:06d}_{tag}.png")
    with open(p, "wb") as fh:
        fh.write(subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True).stdout)
    old = sorted(glob.glob(os.path.join(DBG, "*.png")))[:-DBG_KEEP]   # keep only the newest
    for f in old:
        try:
            os.remove(f)
        except OSError:
            pass
    return p


def _parse_nodes(xml):
    """uiautomator XML hierarchy -> our node dicts. Skips com.android.systemui nodes
    (status bar / notification shade) that u2's full-window dump includes — they're
    never navigation targets, so dropping them keeps the set the same as the old shell
    dump and avoids any stray text matches."""
    nodes = []
    for m in re.finditer(r"<node[^>]*?>", xml):
        s = m.group(0)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', s)
        if not b:
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        def g(attr):
            mm = re.search(attr + r'="([^"]*)"', s)
            return mm.group(1) if mm else ""
        if g("package") == "com.android.systemui":
            continue
        nodes.append({"rid": g("resource-id"), "text": g("text"), "desc": g("content-desc"),
                      "clickable": g("clickable") == "true", "enabled": g("enabled") == "true",
                      "bounds": (x1, y1, x2, y2), "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2})
    return nodes


def _u2_device():
    global _u2
    if _u2 is None:
        _u2 = _u2mod.connect(SERIAL)           # one-time ~0.8s: starts/attaches the ATX agent
    return _u2


def _dump_shell():
    """The original shell-based dump (rm; uiautomator dump; cat). ~2.1s/call but needs
    no agent — used as the DRIVE_U2=0 fallback and the per-call safety net when u2 errors.

    We delete the file FIRST so a failed dump (UI never idle -> 'could not get idle
    state', writes nothing) can't read the stale last-good tree and make a wedged app
    look healthy. '<node' absent -> [] (the wedge signal)."""
    shell("rm -f /sdcard/ui.xml")
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=25)
    xml = shell("cat /sdcard/ui.xml 2>/dev/null")
    if "<node" not in xml:
        return []
    return _parse_nodes(xml)


def dump():
    """Return list of nodes: {rid,text,desc,clickable,bounds,cx,cy}, or [] when the UI
    can't produce a usable tree — the wedge signal goto_in_transit_home() turns into a
    force-restart (an empty/system-only tree has none of the actionable nodes it looks
    for, so its 'stuck' counter still trips even though u2 doesn't error on a spinner).

    Fast path: a warm uiautomator2 server (~0.13s) instead of the shell `uiautomator
    dump` (~2.1s cold-start every call). If u2 errors — e.g. the emulator was rebooted
    by ensure_emulator() — we drop the handle so the next call reconnects, and fall back
    to the shell dump for THIS call so a hiccup never stalls the run."""
    if USE_U2:
        global _u2
        try:
            return _parse_nodes(_u2_device().dump_hierarchy())
        except Exception:
            _u2 = None                         # reconnect next call (emulator may have restarted)
    return _dump_shell()


def find_text(nodes, needle, contains=True):
    for n in nodes:
        hay = (n["text"] + " " + n["desc"])
        if (needle in hay) if contains else (needle == n["text"] or needle == n["desc"]):
            return n
    return None


def by(nodes, id_suffix=None, desc=None, desc_has=None, text=None):
    """Find a node by resource-id suffix (preferred), exact desc, desc substring, or
    exact text — labels are often in content-desc, not text."""
    for n in nodes:
        if id_suffix and n["rid"].endswith(id_suffix):
            return n
        if desc and n["desc"] == desc:
            return n
        if desc_has and desc_has in n["desc"]:
            return n
        if text and n["text"] == text:
            return n
    return None


def log(msg):
    print(msg, flush=True)
    try:                                          # mirror to the dashboard's log file
        with open(LOGFILE, "a") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
        if os.path.getsize(LOGFILE) > LOG_MAX_BYTES:
            with open(LOGFILE) as fh:
                tail = fh.readlines()[-1500:]
            with open(LOGFILE, "w") as fh:
                fh.writelines(tail)
    except OSError:
        pass


def step(flow, n, label, vin="", shp=""):
    """Emit a machine-readable milestone marker for the dashboard's step tracker.
    Purely additive (a normal log line) — does NOT change automation behavior.
    flow = 'pickup' | 'dropoff'; n = 1..5 (see the 5-step lists in dashboard.py)."""
    log(f"STEP {flow} {n}/5 {label} vin={vin} shp={shp}")


def current_focus():
    return shell("dumpsys window | grep -E 'mCurrentFocus'")


def ensure_emulator():
    """Make sure an emulator is connected and fully booted. If adb sees no device
    (host reboot, emulator crash, never started), boot the AVD via start_emulator.sh
    — which restores the saved snapshot, so the Tesla app stays logged in. Blocks
    until boot (the script waits) or returns False if it can't come up."""
    if adb("get-state").stdout.strip() == "device" and \
       shell("getprop sys.boot_completed").strip() == "1":
        return True
    log("  emulator not up — booting it (restoring snapshot)…")
    try:
        subprocess.run(["bash", START_EMU], cwd=HERE, timeout=1200)
    except subprocess.TimeoutExpired:
        log("  ! emulator boot timed out")
        return False
    ok = adb("get-state").stdout.strip() == "device"
    if ok:
        time.sleep(3)
    return ok


def ensure_app(pause=4.0):
    """Keep us in the Tesla Logistics app. Relaunch if some other app/launcher is
    foreground; a permission dialog (permissioncontroller) over our app is fine."""
    foc = current_focus()
    if APP in foc or "permission" in foc.lower():
        return
    log("  not in Tesla Logistics — launching it")
    shell(f"monkey -p {APP} -c android.intent.category.LAUNCHER 1")
    time.sleep(pause)


def handle_dialogs(nodes):
    """Dismiss prompts that can block the flow: the Android runtime permission dialog,
    the in-app 'Location Permission' dialog, the 'Dropped Off Successfully' dialog, and
    the 'After Hours Drop Off' confirmation (outside business hours). Returns True if it
    acted."""
    # After-hours drop-off confirmation (no Tesla rep present) -> Confirm. Pops up after
    # the final Drop Off Confirm when it's outside business hours; always correct to
    # confirm for our automation. Handled here so it's cleared wherever it appears.
    if any("after hours" in (n["text"] + n["desc"]).lower() for n in nodes):
        cf = next((n for n in nodes if (n["text"] == "Confirm" or n["desc"] == "Confirm")
                   and n["clickable"]), None)
        if cf:
            log("  confirming After Hours Drop Off")
            tap(cf["cx"], cf["cy"], pause=2.5)
            return True
    # Android runtime permission dialog -> grant while-using.
    b = (by(nodes, id_suffix="permission_allow_foreground_only_button")
         or by(nodes, id_suffix="permission_allow_one_time_button")
         or by(nodes, id_suffix="permission_allow_button")
         or by(nodes, text="While using the app", desc="While using the app")
         or by(nodes, text="Allow only while using the app"))
    if b:
        log("  granting runtime location permission")
        tap(b["cx"], b["cy"], pause=1.5)
        return True
    # In-app 'Location Permission' / drop-off success dialogs -> tap OK. Titles can be
    # in text OR content-desc, so scan both.
    titles = ("location permission", "enable location", "dropped off successfully")
    if any(any(t in (n["text"] + " " + n["desc"]).lower() for t in titles) for n in nodes):
        ok = next((n for n in nodes if n["text"] == "OK" or n["desc"] == "OK"), None)
        if ok:
            log("  dismissing dialog (OK)")
            tap(ok["cx"], ok["cy"], pause=1.5)
            return True
    return False


def wait_until(pred, tries=12, interval=0.5):
    """Re-dump until pred(nodes) is truthy (RN screens load async). Dismisses permission
    dialogs along the way. Returns last nodes.

    interval was 1.0s when each dump cost ~2.1s; with the warm-u2 dump (~0.13s) the sleep
    is the dominant per-poll cost, so 0.5s converges roughly twice as fast while still
    leaving a multi-second budget (tries*interval) for a genuinely slow screen load."""
    nodes = dump()
    for _ in range(tries):
        if handle_dialogs(nodes):
            nodes = dump()
        if pred(nodes):
            return nodes
        time.sleep(interval)
        nodes = dump()
    return nodes


def units_from(nodes):
    out = []
    for n in nodes:
        m = VIN_RE.search(n["desc"])
        if m and n["clickable"] and (n["bounds"][3] - n["bounds"][1]) > 120:
            out.append({"vin": m.group(0), "node": n, "attached": "Attached" in n["desc"]})
    return out


# ------------------------------- gallery -----------------------------------
def gallery_clear():
    for f in shell(f"ls {GAL}").split():
        f = f.strip()
        if f.lower().endswith((".jpg", ".jpeg", ".png")):
            path = f"{GAL}/{f}"
            shell(f"rm -f {path}")
            shell(f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "file://{path}"')
    time.sleep(1)


def gallery_push(files):
    """Push files in order; date_added increases so picker shows them in this order."""
    for i, f in enumerate(files):
        dst = f"{GAL}/{i:02d}_{os.path.basename(f)}"
        adb("push", f, dst)
        shell(f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "file://{dst}"')
        time.sleep(0.4)
    time.sleep(1.5)


# ------------------------------- picker ------------------------------------
def picker_select(indices, tries=3):
    """Select the given ivPicture cell indices (0-based, ascending push order), then
    tap the complete button. Verifies via the per-cell selection number (tvCheck)."""
    rid_iv = "ivPicture"
    for _ in range(tries):
        nodes = dump()
        ivs = [n for n in nodes if n["rid"].endswith(rid_iv)]
        tvs = [n for n in nodes if n["rid"].endswith("tvCheck")]
        sel = {i for i, tv in enumerate(tvs) if tv["text"].strip().isdigit()}
        missing = [i for i in indices if i < len(ivs) and i not in sel]
        if not missing:
            break
        for i in missing:
            x1, y1, x2, y2 = ivs[i]["bounds"]
            tap(x2 - 40, y1 + 40, pause=0.7)         # check circle = top-right of thumb
    nodes = dump()
    comp = next((n for n in nodes if n["rid"].endswith("ps_tv_complete")
                 or n["rid"].endswith("ps_complete_select")), None)
    if comp:
        tap(comp["cx"], comp["cy"], pause=2.0)
    else:
        tap(619, 1517, pause=2.0)                    # known fallback location


def picker_select_all(expected, tries=3):
    """Select ALL photos currently in the picker (the gallery holds only this section's
    files), then confirm. Order-independent — avoids any newest/oldest sort assumption."""
    for _ in range(tries):
        nodes = dump()
        ivs = [n for n in nodes if n["rid"].endswith("ivPicture")]
        tvs = [n for n in nodes if n["rid"].endswith("tvCheck")]
        sel = {i for i, tv in enumerate(tvs) if tv["text"].strip().isdigit()}
        targets = list(range(min(len(ivs), expected)))
        missing = [i for i in targets if i not in sel]
        if not missing and len(ivs) >= expected:
            break
        for i in missing:
            x1, y1, x2, y2 = ivs[i]["bounds"]
            tap(x2 - 40, y1 + 40, pause=0.7)
    nodes = dump()
    comp = next((n for n in nodes if n["rid"].endswith("ps_tv_complete")
                 or n["rid"].endswith("ps_complete_select")), None)
    tap(comp["cx"], comp["cy"], pause=NAV_PAUSE) if comp else tap(619, 1517, pause=NAV_PAUSE)


def remove_existing():
    """Delete any already-attached photos on the Add Photos screen (small X buttons at
    each tile's corner), so a unit can be re-photographed correctly."""
    for _ in range(15):
        nodes = dump()
        xs = [n for n in nodes if n["clickable"] and n["bounds"][1] >= 200
              and 30 <= (n["bounds"][2] - n["bounds"][0]) <= 80
              and 30 <= (n["bounds"][3] - n["bounds"][1]) <= 80]
        if not xs:
            break
        xs.sort(key=lambda n: (n["bounds"][1], n["bounds"][0]))
        tap(xs[0]["cx"], xs[0]["cy"], pause=0.8)


def first_plus_box(nodes):
    """The top-most empty '+' section box: clickable, left column (x1~35,x2~308), tall."""
    boxes = [n for n in nodes if n["clickable"] and 28 <= n["bounds"][0] <= 42
             and 298 <= n["bounds"][2] <= 318 and (n["bounds"][3] - n["bounds"][1]) >= 150]
    boxes.sort(key=lambda n: n["bounds"][1])
    return boxes[0] if boxes else None


# ------------------------------- decode ------------------------------------
def decode(vin):
    step("dropoff", 2, "decode", vin=vin)
    log(f"  decoding {vin} (fetch latest shipment + model + OCR)…")
    env = dict(os.environ, HF_HUB_DISABLE_TELEMETRY="1",
               CLIP_DEVICE=os.environ.get("CLIP_DEVICE", "cuda"),     # corner model on GPU
               OCR_GPU=os.environ.get("OCR_GPU", "true"))             # VIN-plate OCR on GPU
    if glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--*CLIP*")):
        env["HF_HUB_OFFLINE"] = "1"; env["TRANSFORMERS_OFFLINE"] = "1"
    r = subprocess.run([PY, os.path.join(HERE, "decode_vin.py"), "--vin", vin],
                       cwd=HERE, env=env, timeout=900)
    out = os.path.join(HERE, "out", vin)
    sides = sorted(glob.glob(os.path.join(out, "sides", "*.jpg")))
    vinp = glob.glob(os.path.join(out, "vin_plate", "*.jpg"))
    key = glob.glob(os.path.join(out, "key", "*.jpg"))
    if not (sides and vinp and key):
        raise RuntimeError(f"decode incomplete for {vin}: sides={len(sides)} vin={len(vinp)} key={len(key)}")
    return sides, vinp[0], key[0]


# ------------------------------- flow --------------------------------------
def add_photos_for_unit(vin, unit_node):
    sides, vinp, key = decode(vin)

    tap(unit_node["cx"], unit_node["cy"], pause=NAV_PAUSE)   # open unit menu
    nodes = wait_until(lambda ns: find_text(ns, "Add Drop Off Photo"), tries=6)
    m = find_text(nodes, "Add Drop Off Photo")
    if not m:
        raise RuntimeError("could not find 'Add Drop Off Photo'")
    shot("unit_menu")
    tap(m["cx"], m["cy"], pause=NAV_PAUSE)
    wait_until(lambda ns: find_text(ns, "All Four Sides"), tries=8)
    shot("add_photos")

    # One section at a time: push ONLY that section's photos, select them all. This is
    # order-independent (the picker's sort doesn't matter — only this section is present).
    for section, files in (("All Four Sides", sides[:4]), ("VIN Plate", [vinp]), ("Key", [key])):
        gallery_clear()
        gallery_push(files)
        # Find this section's empty '+' box. Once the earlier sections are filled (4 sides +
        # 1 VIN photo) the later ones — especially Key — sit WELL below the fold, so a single
        # swipe wasn't enough (the "no '+' box for section Key" failure). Snap to the top,
        # then page DOWN repeatedly until the box appears or the list stops moving.
        swipe(360, 500, 360, 1200, pause=0.8)          # scroll to top
        plus = first_plus_box(dump())
        prev_sig = None
        for _ in range(6):
            if plus:
                break
            swipe(360, 1200, 360, 500, ms=500, pause=0.7)   # page down ~one screen
            nodes = dump()
            plus = first_plus_box(nodes)
            sig = tuple((n["rid"], n["bounds"]) for n in nodes)
            if sig == prev_sig:                        # nothing new scrolled in -> at the bottom
                break
            prev_sig = sig
        if not plus:
            raise RuntimeError(f"no '+' box for section {section}")
        log(f"  section '{section}': pushing {len(files)} photo(s), selecting all")
        tap(plus["cx"], plus["cy"], pause=NAV_PAUSE)
        wait_until(lambda ns: any(n["rid"].endswith("ivPicture") for n in ns), tries=8)
        picker_select_all(len(files))
        wait_until(lambda ns: find_text(ns, "All Four Sides"), tries=6)   # back on Add Photos
        shot(f"after_{section.replace(' ', '_')}")

    add = by(dump(), text="Add", desc="Add")
    tap(add["cx"] if add else 360, add["cy"] if add else 1498, pause=NAV_PAUSE)
    step("dropoff", 3, "upload", vin=vin)
    log("  tapped Add; waiting for upload…")
    wait_until(_drop_off_btn, tries=14, interval=1.5)     # upload done -> back on detail
    shot("after_add")


def _drop_off_btn(nodes):
    return by(nodes, id_suffix="buttonDropOff", desc="Drop Off")


def _options_open(nodes):
    return by(nodes, id_suffix="cardSubjectToInspection", desc_has="Subject to Inspection")


def _api_error(nodes):
    """A transient backend error dialog that pops after Confirm (e.g. 'API error',
    'Something went wrong') with an OK button — it must be dismissed and the drop off
    RETRIED. Excludes the known benign OK dialogs (success / location)."""
    if not any(n["text"] == "OK" or n["desc"] == "OK" for n in nodes):
        return False
    blob = " ".join((n["text"] + " " + n["desc"]).lower() for n in nodes)
    if any(k in blob for k in ("api error", "something went wrong", "try again",
                               "an error occurred", "request failed", "unable to")):
        return True
    return ("error" in blob and not any(g in blob for g in
            ("after hours", "dropped off", "location permission", "enable location")))


def _api_error_shown(nodes) -> bool:
    """STRICT: the screen actually shows an error that says "API" (e.g. an 'API error' dialog).
    Used to decide whether a failed departure is a genuine API error worth PARKING the shipment
    (never retry) — as opposed to a generic stuck/slow/reverted departure, which should just be
    retried, not parked. Only the literal word 'API' alongside an error counts."""
    import re as _re
    blob = " ".join(((n.get("text") or "") + " " + (n.get("desc") or "")).lower() for n in nodes)
    return ("api error" in blob
            or (bool(_re.search(r"\bapi\b", blob)) and ("error" in blob or "failed" in blob)))


def drop_off(confirm):
    # Open the options sheet if it isn't already (an attached unit may auto-open it).
    nodes = dump()
    if not _options_open(nodes):
        nodes = wait_until(lambda ns: _drop_off_btn(ns) or _options_open(ns), tries=8)
        if not (_options_open(nodes) or _drop_off_btn(nodes)):
            nodes = _scroll_until(lambda ns: _drop_off_btn(ns) or _options_open(ns))  # below the fold on multi-unit
        if not _options_open(nodes):
            d = _drop_off_btn(nodes)
            if not d:
                raise RuntimeError("no 'Drop Off' button and no options sheet")
            tap(d["cx"], d["cy"], pause=NAV_PAUSE)
            nodes = wait_until(_options_open, tries=8)
    shot("dropoff_options")
    if not by(nodes, id_suffix="cardSubjectToInspection", desc_has="Subject to Inspection"):
        raise RuntimeError("Drop Off Options didn't open")
    if not confirm:
        log("  (dry run) reached Drop Off Options — NOT confirming. Backing out.")
        tap(65, 734)                                  # the sheet's own back arrow
        return False

    # Confirm, retrying on a transient API error (error dialog -> OK -> re-confirm).
    for attempt in range(4):
        nodes = dump()
        if not _options_open(nodes):                  # after an error/back we land on the detail
            d = _drop_off_btn(nodes)
            if d:
                tap(d["cx"], d["cy"], pause=NAV_PAUSE)
            nodes = wait_until(_options_open, tries=8)
        opt = by(nodes, id_suffix="cardSubjectToInspection", desc_has="Subject to Inspection")
        if not opt:
            raise RuntimeError("Drop Off Options didn't open")
        tap(opt["cx"], opt["cy"], pause=1.5)          # choose "Subject to Inspection"
        nodes = wait_until(lambda ns: by(ns, id_suffix="buttonConfirmDropOff", desc="Confirm"), tries=6)
        conf = by(nodes, id_suffix="buttonConfirmDropOff", desc="Confirm")
        if not conf:
            raise RuntimeError("no 'Confirm' button")
        step("dropoff", 4, "confirm")
        log("  CONFIRMING drop off (Subject to Inspection)…" + (f" [retry {attempt}]" if attempt else ""))
        tap(conf["cx"], conf["cy"], pause=NAV_PAUSE)
        # Outcome: success (home / Dropped Off; After-Hours is auto-confirmed by
        # wait_until's handle_dialogs), or a transient API-error dialog -> retry.
        nodes = wait_until(lambda ns: _home_now(ns) or find_text(ns, "Dropped Off")
                           or _api_error(ns), tries=10, interval=1.0)
        if _api_error(nodes):
            log("  API error on drop off — dismissing (OK) and retrying…")
            ok = next((n for n in nodes if n["text"] == "OK" or n["desc"] == "OK"), None)
            if ok:
                tap(ok["cx"], ok["cy"], pause=NAV_PAUSE)
            wait_until(lambda ns: _drop_off_btn(ns) or _options_open(ns), tries=8)
            continue
        shot("after_confirm")
        return True
    log("  ! drop off kept hitting API errors — leaving it for the next cycle")
    return False


def type_text(s):
    """Type `s` into the currently-focused input via adb, without the value being
    re-interpreted by the host or device shell (single-quote it; `input text` wants
    %s for spaces)."""
    esc = s.replace("'", "'\\''").replace(" ", "%s")
    adb("shell", f"input text '{esc}'")


def _home_now(nodes):
    return find_text(nodes, "In Transit (") is not None or find_text(nodes, "View Details") is not None


def _on_role(nodes):
    return find_text(nodes, "select your role") is not None


def _on_login(nodes):
    """True if we're on any post-logout screen we know how to drive: the in-app
    email/Next screen, the auth.tesla.com SSO password screen, or the role picker."""
    return (find_text(nodes, "Next") is not None
            or any(n["rid"].endswith("password") for n in nodes)
            or _on_role(nodes))


def login_app(tries=12):
    """Sign the carrier app back in after an inactivity logout. Drives every screen in
    the flow: in-app email (remembered — tap Next) -> auth.tesla.com SSO (type
    TESLA_APP_PASSWORD, Sign In) -> 'select your role' (pick TESLA_APP_ROLE, Confirm).
    Returns True once the In-Transit home is reached. Needs TESLA_APP_PASSWORD set."""
    if not cfg.TESLA_APP_PASSWORD:
        log("  ! TESLA_APP_PASSWORD unset — can't auto-login (fill secrets/.env).")
        return False
    for _ in range(tries):
        nodes = wait_until(lambda ns: _home_now(ns) or _on_login(ns), tries=6, interval=1.5)
        if _home_now(nodes):
            log("  signed in — back on In-Transit home.")
            return True
        pw = next((n for n in nodes if n["rid"].endswith("password")), None)
        if pw:                                         # SSO password screen
            log("  SSO sign-in: entering password…")
            tap(pw["cx"], pw["cy"], pause=0.8)         # focus the password field
            type_text(cfg.TESLA_APP_PASSWORD)
            time.sleep(0.6)
            btn = next((n for n in nodes if n["text"] == "Sign In" and n["clickable"]), None)
            tap(btn["cx"], btn["cy"], pause=4.0) if btn else tap(361, 677, pause=4.0)
            continue
        if _on_role(nodes):                            # 'Please select your role'
            role = (next((n for n in nodes if cfg.TESLA_APP_ROLE in (n["text"] + n["desc"])
                          and n["clickable"]), None))
            conf = next((n for n in nodes if "Confirm" in (n["text"] + n["desc"]) and n["clickable"]), None)
            log(f"  selecting role: {cfg.TESLA_APP_ROLE}…")
            if role:
                tap(role["cx"], role["cy"], pause=1.0)
            tap(conf["cx"], conf["cy"], pause=3.0) if conf else tap(360, 1479, pause=3.0)
            continue
        nxt = by(nodes, text="Next")
        if nxt:                                        # in-app email screen (email remembered)
            ef = next((n for n in nodes if n["rid"].endswith("Email") or n.get("text") == "Email"), None)
            if cfg.TESLA_APP_EMAIL and ef and not (ef.get("text") or "").strip():
                tap(ef["cx"], ef["cy"], pause=0.6); type_text(cfg.TESLA_APP_EMAIL); time.sleep(0.4)
            log("  app login: tapping Next…")
            tap(nxt["cx"], nxt["cy"], pause=3.0)
            continue
    return _home_now(dump())


def restart_app(wait=14):
    """Hard-restart the carrier app: force-stop, then relaunch. Use when it's stuck on
    an infinite loading spinner or got logged out after inactivity — a soft relaunch
    won't revive a running-but-wedged process. If the relaunch lands on the login
    screen (the app drops its session after inactivity), sign back in via login_app().
    Returns True once the In-Transit home is back."""
    log("  app wedged / logged out — force-stop + relaunch…")
    shell(f"am force-stop {APP}")
    time.sleep(2)
    shell(f"monkey -p {APP} -c android.intent.category.LAUNCHER 1")
    time.sleep(6)                                     # let the splash render
    nodes = wait_until(lambda ns: _home_now(ns) or _on_login(ns), tries=wait, interval=2.0)
    if _home_now(nodes):
        return True
    if _on_login(nodes):
        return login_app()
    log("  ! unexpected screen after restart (neither home nor login).")
    return False


def goto_in_transit_home():
    # Never use hardware BACK — it crashes this RN app. Use the in-app header back
    # arrow, the Drop Off Options back arrow, or a relaunch. If the app is wedged on an
    # infinite spinner or got logged out (nothing actionable for several polls in a
    # row), force-restart it — a soft relaunch can't fix a running-but-stuck process.
    stuck = restarts = 0
    for _ in range(14):
        ensure_app()
        nodes = dump()
        if handle_dialogs(nodes):
            stuck = 0; continue
        if by(nodes, id_suffix="buttonConfirmDropOff", desc_has="Subject to Inspection"):
            tap(65, 734); time.sleep(1.5); stuck = 0; continue   # dismiss a stuck Drop Off Options sheet
        tab = find_text(nodes, "In Transit (")
        if tab:
            tap(tab["cx"], tab["cy"], pause=NAV_PAUSE)
            wait_until(lambda ns: find_text(ns, "View Details") or find_text(ns, "In Transit ("), tries=6)
            return True
        # A wedged "Add Photos" drop-off screen can't be backed out cleanly (Back pops a
        # discard-photos dialog and we re-land here), so soft back-taps loop forever — this
        # is the wedge that once left the worker idle seeing "In Transit (0)". Force a clean
        # relaunch instead of tapping Back.
        if find_text(nodes, "Add Photos") and restarts < 2:
            restarts += 1; stuck = 0
            restart_app(); continue
        bb = by(nodes, id_suffix="Header.buttonGoBack")   # in-app back (detail -> home)
        if bb:
            tap(bb["cx"], bb["cy"], pause=1.5); stuck = 0; continue
        # Nothing actionable: an infinite loading spinner or a login screen. Transient
        # loads clear within a poll or two; if it persists, force-restart the app.
        stuck += 1
        if stuck >= 3 and restarts < 2:
            restarts += 1; stuck = 0
            restart_app()
            continue
        shell(f"monkey -p {APP} -c android.intent.category.LAUNCHER 1"); time.sleep(5)
    # Exhausted the loop still wedged — one last hard restart before giving up.
    return restart_app() if restarts < 2 else False


def grant_location():
    """Grant location at the OS level + enable location services so the in-app
    'Location Permission' prompt stops recurring."""
    shell(f"pm grant {APP} android.permission.ACCESS_FINE_LOCATION")
    shell(f"pm grant {APP} android.permission.ACCESS_COARSE_LOCATION")
    shell("cmd location set-location-enabled true")


def refresh_list(pause=3.0):
    """Pull-to-refresh the In-Transit list. The app does NOT poll the server on its
    own — a newly-assigned shipment only appears after a manual refresh — so the
    service pulls down every cycle. A slow downward swipe from the top of the list
    ScrollView triggers React Native's RefreshControl (verified: the spinner shows).
    Coords match this AVD's 720x1600 layout (list ScrollView starts ~y=593)."""
    swipe(360, 660, 360, 1380, ms=600, pause=pause)


def _intransit_count(nodes):
    tab = find_text(nodes, "In Transit (")
    m = re.search(r"In Transit \((\d+)\)", (tab["text"] + tab["desc"]) if tab else "")
    return int(m.group(1)) if m else 0


def _open_detail(vd):
    """Tap a View Details card and wait for the shipment detail to load (one retry)."""
    tap(vd["cx"], vd["cy"], pause=NAV_PAUSE)
    ok = lambda ns: units_from(ns) or _drop_off_btn(ns) or _options_open(ns)
    nodes = wait_until(ok, tries=8)
    if not ok(nodes):
        vd2 = find_text(dump(), "View Details")
        if vd2:
            tap(vd2["cx"], vd2["cy"], pause=NAV_PAUSE)
            nodes = wait_until(ok, tries=8)
    return nodes


def _dropoff_open_detail(confirm):
    """Drop off the In-Transit shipment whose DETAIL is currently open: photograph every
    pending unit (multi-VIN aware; uploaded units are left as-is), then Drop Off ->
    Subject to Inspection -> Confirm. Returns (committed, ship_units); ledgers on success."""
    nodes = dump()
    shot("detail")
    step("dropoff", 1, "open")
    log(f" detail loaded; {len(units_from(nodes))} unit(s) "
        f"({sum(u['attached'] for u in units_from(nodes))} already attached)")
    processed = set()
    seen = {}                                     # vin -> shipment across the WHOLE (scrolled) list
    stagnant = 0
    while True:
        nd = dump()
        if _options_open(nd):                     # attached unit auto-opens the sheet; dismiss
            tap(65, 734, pause=1.5)
            nd = dump()
        for u in units_from(nd):
            seen.setdefault(u["vin"], _shp_of(u["node"]["desc"]))
        todo = [u for u in units_from(nd) if u["vin"] not in processed and not u["attached"]]
        if not todo:
            # Nothing to photograph on screen — page down in case units are below the fold
            # (RN only mounts what's near the viewport). Bottom = the VISIBLE window no
            # longer moving; don't gate on the global seen-set (it saturates after one pass
            # and would stop us before reaching deep units).
            vis_before = {u["vin"] for u in units_from(nd)}
            _scroll_list_down()
            after = units_from(dump())
            for u in after:
                seen.setdefault(u["vin"], _shp_of(u["node"]["desc"]))
            vis_after = {u["vin"] for u in after}
            if vis_after and vis_after != vis_before:
                stagnant = 0
            else:
                stagnant += 1
                if stagnant >= 2:
                    break
            continue
        stagnant = 0
        u = todo[0]
        log(f" unit VIN {u['vin']}")
        node = _unit_in_clean_view(u["vin"], units_from) or u["node"]   # avoid a mis-tap on a clipped card
        try:
            add_photos_for_unit(u["vin"], node)
        except Exception as e:                    # don't let one unit stall the rest
            log(f"  ! unit {u['vin']} failed: {e}")
        processed.add(u["vin"])
        gallery_clear()
        wait_until(lambda ns: _drop_off_btn(ns) or units_from(ns) or _options_open(ns), tries=8)
        shot("back_on_detail")

    nd = dump()
    if _options_open(nd):
        tap(65, 734, pause=1.5); nd = dump()
    ship_units = list(seen.items())
    log(f" {len(processed)} photographed; dropping off shipment ({len(ship_units)} unit(s))…")
    try:
        committed = drop_off(confirm)
    except Exception as e:
        # The drop-off can fail with the app left on a wedged screen (e.g. "Add Photos"
        # after a photo-add that didn't commit) that soft navigation can't escape — which
        # once stranded a photographed shipment. Force a clean relaunch so the next drain
        # pass re-detects the still-In-Transit shipment and re-attempts from home.
        log(f" ! drop off failed: {e} — restarting the app to clear any wedged screen")
        restart_app()
        return (False, ship_units)
    if committed:
        record_dropoffs(ship_units, "Subject to Inspection", processed)
    return (committed, ship_units)


def drain_queue(confirm, max_shipments):
    """Drop off In-Transit shipments present right now (up to max_shipments). Returns the
    count dropped off."""
    if not goto_in_transit_home():
        log("couldn't reach the In-Transit home tab.")
        return 0
    done = attempts = 0
    while done < max_shipments and attempts < max_shipments + 4:   # cap retries on a stuck shipment
        attempts += 1
        ensure_app()
        nodes = dump()
        cnt = _intransit_count(nodes)
        vd = find_text(nodes, "View Details")
        if not vd or cnt == 0:
            break
        log(f"In Transit ({cnt}) — dropping off shipment {done + 1}")
        _open_detail(vd)
        committed, _ = _dropoff_open_detail(confirm)
        if not confirm:
            break                                  # dry run: one and done
        if not committed:
            goto_in_transit_home(); continue       # failed (e.g. API error) — leave for next cycle
        done += 1
        wait_until(lambda ns: handle_dialogs(ns) or find_text(ns, "In Transit ("), tries=8)
        goto_in_transit_home()
    return done


def dropoff_matching(confirm, want_vins, max_cards=8):
    """Drop off the In-Transit shipment whose units include any of `want_vins` — used by
    the interleave to drop off the shipment we JUST picked up. Opens View Details cards
    top-to-bottom until the detail's VINs match. Returns (committed, ship_units), or
    (None, []) if no matching In-Transit shipment is present."""
    want = set(want_vins or [])
    if not want or not goto_in_transit_home():
        return (None, [])
    for idx in range(max_cards):
        nodes = dump()
        if _intransit_count(nodes) == 0:
            return (None, [])
        vds = sorted([n for n in nodes if "View Details" in (n["text"] + n["desc"])],
                     key=lambda n: n["bounds"][1])
        if idx >= len(vds):
            return (None, [])
        _open_detail(vds[idx])
        here = {u["vin"] for u in units_from(dump())}
        if want & here:
            log(f"In Transit — found the just-picked-up shipment ({sorted(want & here)[0]})")
            return _dropoff_open_detail(confirm)
        goto_in_transit_home()                     # not this card; back out and try the next
    return (None, [])


# ------------------------------- Pick Up flow ------------------------------
# Pick Up is photo-free: verify each unit (tap-through, the emulator can't scan a QR),
# Start Loading, wait out a ~2-minute loading timer (Finish Loading is enabled=false
# until it elapses), set a formality ETA, then Ready-to-Depart -> Confirm. The shipment
# then moves to In Transit (where the drop-off flow takes over). Selectors below come
# from walking the live flow once.
ETA_HOUR = 11            # arrival = today 11 PM: late enough to be future in any US
                         # destination timezone (the app rejects a past destination time)


def _click(nodes, desc=None, text=None, contains=None, min_w=0):
    """Find a node by exact desc / exact text / substring; prefer a clickable match,
    else fall back to any match (labels are often a non-clickable child of the button)."""
    fallback = None
    for n in nodes:
        if desc is not None and n["desc"] != desc:
            continue
        if text is not None and n["text"] != text:
            continue
        if contains is not None and contains not in (n["text"] + " " + n["desc"]):
            continue
        if min_w and (n["bounds"][2] - n["bounds"][0]) < min_w:
            continue
        if n["clickable"]:
            return n
        fallback = fallback or n
    return fallback


def _tap_ok():
    n = _click(dump(), text="OK")
    if n:
        tap(n["cx"], n["cy"], pause=1.5)


def pickup_buttons(nodes):
    """The wide 'Pick Up' button on each Pick Up shipment card (not the tab label)."""
    return [n for n in nodes if n["clickable"]
            and (n["text"] == "Pick Up" or n["desc"] == "Pick Up")
            and (n["bounds"][2] - n["bounds"][0]) > 300]


def pickup_units(nodes):
    """Unit cards on a Pick Up detail: {vin, node, verified}."""
    out = []
    for n in nodes:
        m = VIN_RE.search(n["desc"])
        if m and n["clickable"] and "Verified" in n["desc"]:
            out.append({"vin": m.group(0), "node": n, "verified": "Not Verified" not in n["desc"]})
    return out


def _select_then_next(choice):
    """On a Verify question screen: tap the choice (Yes/No), then the enabled Next."""
    c = _click(dump(), desc=choice) or _click(dump(), text=choice)
    if c:
        tap(c["cx"], c["cy"], pause=1.0)
    nx = _click(dump(), desc="Next") or _click(dump(), text="Next")
    if nx:
        tap(nx["cx"], nx["cy"], pause=2.0)


def verify_unit(unit_node):
    """Drive one unit through verification: Verify -> Can't Scan QR Code -> Yes (fully
    inspect) -> No (no damages) -> Confirm (liability). Leaves it showing Verified.
    Generous wait retries: this software-rendered AVD often takes >6s to paint the scan /
    question screens, and a premature timeout here silently abandons the unit (Start
    Loading then stays disabled forever)."""
    tap(unit_node["cx"], unit_node["cy"], pause=NAV_PAUSE)           # open the Verify sheet
    nodes = wait_until(lambda ns: by(ns, desc="Verify") or find_text(ns, "Verify Method"), tries=12)
    v = by(nodes, desc="Verify")
    if v:
        tap(v["cx"], v["cy"], pause=NAV_PAUSE)
    # Manual entry (the emulator has no scanner): tap "Can't Scan QR Code" — NOT "Scan QR",
    # which opens the camera and a permission dialog that dead-ends back to the list.
    nodes = wait_until(lambda ns: find_text(ns, "Can't Scan QR"), tries=12)
    cs = _click(nodes, contains="Can't Scan QR")
    if cs:
        tap(cs["cx"], cs["cy"], pause=NAV_PAUSE)
    wait_until(lambda ns: find_text(ns, "fully inspect"), tries=12)
    _select_then_next("Yes")                                        # able to fully inspect
    wait_until(lambda ns: find_text(ns, "any damages"), tries=12)
    _select_then_next("No")                                         # no damages
    nodes = wait_until(lambda ns: by(ns, desc="Confirm") or find_text(ns, "finish the ver"), tries=12)
    cf = by(nodes, desc="Confirm")
    if cf:
        tap(cf["cx"], cf["cy"], pause=NAV_PAUSE)
    wait_until(lambda ns: find_text(ns, "Verified") or find_text(ns, "Start Loading"), tries=12)


# --- ETA date/time spinner helpers (native Android pickers) ---
def _picker_col(nodes, lo, hi):
    col = [(n["cy"], n) for n in nodes if lo < n["cx"] < hi and n["text"].isdigit()]
    col.sort()
    return [c[1] for c in col]


def _picker_set_hour(target, tries=14):
    """Set the time-picker hour column to `target` (1-12) by tapping the bottom visible
    cell to increment (with wraparound)."""
    for _ in range(tries):
        col = _picker_col(dump(), 150, 290)
        if len(col) < 2:
            return False
        center = col[len(col) // 2]
        if int(center["text"]) == target:
            return True
        tap(col[-1]["cx"], col[-1]["cy"], pause=0.5)
    return False


def _picker_set_pm():
    pm = next((n for n in dump() if n["text"] == "PM" and 440 < n["cx"] < 560), None)
    if pm:
        tap(pm["cx"], pm["cy"], pause=0.5)


def _picker_inc_day():
    col = _picker_col(dump(), 290, 420)            # day column (middle)
    if col:
        tap(col[-1]["cx"], col[-1]["cy"], pause=0.6)


def set_eta():
    """Update ETA: arrival = today at ETA_HOUR PM (a formality). If the app rejects it
    as past for the destination timezone, advance a day and retry. Leaves us on the
    'Ready to Depart?' step. Returns the ETA string for the ledger."""
    ad = by(dump(), desc="Arrival Date")
    if ad:
        tap(ad["cx"], ad["cy"], pause=1.5); _tap_ok()              # accept today
    at = by(dump(), desc="Arrival Time")
    if at:
        tap(at["cx"], at["cy"], pause=1.5)
        _picker_set_hour(ETA_HOUR); _picker_set_pm(); _tap_ok()
    nd = dump()
    dt = next((n["text"] for n in nd if re.search(r"\d{1,2}:\d{2}\s*[AP]M", n["text"])), "")
    dd = next((n["text"] for n in nd if re.search(r"[A-Z][a-z]+ \d{1,2}, \d{4}", n["text"])), "")
    eta = f"{dd} {dt}".strip() or f"today {ETA_HOUR}PM"
    for _ in range(2):
        cf = by(dump(), desc="Confirm")
        if cf:
            tap(cf["cx"], cf["cy"], pause=1.5)
        # Wait OUT the post-Confirm spinner: the ETA submit settles to the Ready-to-Depart
        # step, an 'in the past' error, or straight back to the home tabs. The old code
        # dumped once after 2.5s and returned even while the spinner was still up — that
        # raced the next step, which then tapped/navigated on a half-rendered screen and
        # tripped the false 'app wedged' relaunch that reverted the departure.
        nd = wait_until(lambda ns: find_text(ns, "Ready to Depart") or find_text(ns, "in the past")
                        or _home_now(ns), tries=15, interval=1.0)
        if not find_text(nd, "in the past"):
            return eta
        log("  ETA was in the past for the destination tz — advancing arrival a day")
        ok = _click(nd, desc="OK") or _click(nd, text="OK")
        if ok:
            tap(ok["cx"], ok["cy"], pause=1.0)
        ad = by(dump(), desc="Arrival Date")
        if ad:
            tap(ad["cx"], ad["cy"], pause=1.5); _picker_inc_day(); _tap_ok()
        eta = f"+1d {ETA_HOUR}PM"
    return eta


def _finish_enabled(nodes):
    return any("Finish Loading" in (n["text"] + n["desc"]) and n["clickable"] and n["enabled"]
               for n in nodes)


def _scroll_list_down(pause=1.0):
    """Page the current scrollable list down ~one screen (this AVD's 720x1600 layout).
    React Native only mounts rows near the viewport, so a multi-unit detail must be
    scrolled to reach every unit — and the action button that sits below them."""
    swipe(360, 1200, 360, 560, ms=500, pause=pause)


def _scroll_until(pred, max_scrolls=12):
    """Scroll down until pred(nodes) is truthy (e.g. a 'Start Loading' / 'Drop Off' button
    below the fold) or we've scrolled max_scrolls times. Returns the last dump."""
    nodes = dump()
    for _ in range(max_scrolls + 1):
        if pred(nodes):
            return nodes
        _scroll_list_down()
        nodes = dump()
    return nodes


def _unit_in_clean_view(vin, getter=None, tries=8):
    """Scroll until `vin`'s unit card is FULLY on-screen with sane bounds, and return that
    fresh node. RN renders cards at the list edges with inverted (top>bottom) or sliver
    bounds; tapping their computed centre misses the card, so the tap-through opens nothing
    and the unit is silently skipped — the exact bug that wedged multi-unit pickups (and the
    same risk for multi-unit drop-offs). A clean card is non-inverted, a full row tall, and
    clear of the very top/bottom. `getter` maps a dump to unit dicts (pickup_units for the
    pickup flow, units_from for drop-off)."""
    getter = getter or pickup_units
    def clean(n):
        t, b = n["bounds"][1], n["bounds"][3]
        return t < b and (b - t) >= 120 and t >= 150 and b <= 1380
    for _ in range(tries):
        node = next((u["node"] for u in getter(dump()) if u["vin"] == vin), None)
        if node and clean(node):
            return node
        _scroll_list_down(pause=0.8)
    return next((u["node"] for u in getter(dump()) if u["vin"] == vin), None)


def _collect_and_verify_units(max_passes=80):
    """Verify EVERY unit on the open Pick Up detail, scrolling through the WHOLE list so
    units below the fold aren't missed (the bug that left 'Start Loading' disabled on
    multi-unit shipments — only the on-screen units got verified). Returns the full,
    in-order [(vin, shipment)] list (for the ledger). Each pass: verify the first
    not-yet-attempted unverified visible unit and re-dump; when nothing visible is left to
    do, scroll down for more; stop once two scrolls in a row reveal no new VIN (bottom)."""
    ship = {}                 # vin -> shipment, insertion-ordered → the ledger list
    attempted = set()         # VINs already driven through verify (don't retry a bad one forever)
    stagnant = 0
    for _ in range(max_passes):
        units = pickup_units(dump())
        for u in units:
            ship.setdefault(u["vin"], _shp_of(u["node"]["desc"]))
        todo = [u for u in units if not u["verified"] and u["vin"] not in attempted]
        if todo:
            u = todo[0]
            attempted.add(u["vin"])
            step("pickup", 2, "verify", vin=u["vin"], shp=ship.get(u["vin"], ""))
            log(f"  verifying {u['vin']}…")
            node = _unit_in_clean_view(u["vin"]) or u["node"]   # avoid a mis-tap on a clipped card
            try:
                verify_unit(node)
            except Exception as e:                    # one bad unit shouldn't stall the rest
                log(f"  ! verify {u['vin']} failed: {e}")
            stagnant = 0
            continue
        # Nothing actionable in the CURRENT window — page down for more units. Bottom is
        # detected by the VISIBLE window no longer moving; we must NOT gate on the global
        # seen-set (it saturates after one pass, and verify_unit snaps the list back to the
        # top, so a single page-down keeps re-revealing the same near-top units → we'd quit
        # before deep units ever scroll into view as actionable).
        vis_before = {u["vin"] for u in units}
        _scroll_list_down()
        after = pickup_units(dump())
        for u in after:                               # fold in any newly-revealed units
            ship.setdefault(u["vin"], _shp_of(u["node"]["desc"]))
        vis_after = {u["vin"] for u in after}
        if vis_after and vis_after != vis_before:     # window advanced → keep going
            stagnant = 0
        else:
            stagnant += 1
            if stagnant >= 2:                         # window stopped moving → bottom
                break
    log(f"  verify pass complete — {len(ship)} unit(s) seen, {len(attempted)} attempted")
    return list(ship.items())


def do_pickup(confirm):
    """On a Pick Up detail: verify EVERY unit (scrolling the whole list), Start Loading,
    wait out the ~2-min timer, Finish Loading, set the ETA, then Ready-to-Depart ->
    Confirm. Returns (committed, [(vin, shipment)], eta). Without confirm, stops after
    verifying."""
    shot("pickup_detail")
    ship_units = _collect_and_verify_units()
    pvin = ship_units[0][0] if ship_units else ""      # representative VIN/shipment for the step tracker
    pshp = ship_units[0][1] if ship_units else ""
    if not confirm:
        log("  (dry run) units verified — not loading/departing.")
        return (False, ship_units, None)

    # Start Loading sits below the unit list — scroll to it (no-op if already in view).
    nodes = _scroll_until(lambda ns: _click(ns, desc="Start Loading", min_w=300)
                                     or _click(ns, text="Start Loading", min_w=300))
    sl = _click(nodes, desc="Start Loading", min_w=300) or _click(nodes, text="Start Loading", min_w=300)
    if not (sl and sl["enabled"]):
        log("  Start Loading not enabled (a unit didn't verify) — skipping this shipment.")
        return (False, ship_units, None)
    tap(sl["cx"], sl["cy"], pause=NAV_PAUSE)
    step("pickup", 3, "load", vin=pvin, shp=pshp)
    log("  loading — waiting out the ~2-min timer…")
    nodes = wait_until(_finish_enabled, tries=80, interval=2.0)     # timer is 2:00
    fl = _click(nodes, contains="Finish Loading", min_w=300)
    if not (fl and fl["enabled"]):
        raise RuntimeError("loading timer never enabled Finish Loading")
    tap(fl["cx"], fl["cy"], pause=NAV_PAUSE)
    wait_until(lambda ns: find_text(ns, "Update ETA") or by(ns, desc="Arrival Date"), tries=8)
    step("pickup", 4, "eta", vin=pvin, shp=pshp)
    eta = set_eta()                      # sets the ETA, taps its Confirm, waits out the spinner
    step("pickup", 5, "depart", vin=pvin, shp=pshp)
    # If a separate 'Ready to Depart?' step is shown, confirm it. set_eta already waited for
    # the ETA submit to settle, so this Confirm lands on a stable screen (no revert race).
    nodes = wait_until(lambda ns: find_text(ns, "Ready to Depart") or _home_now(ns), tries=8)
    if find_text(nodes, "Ready to Depart"):
        cf = by(nodes, desc="Confirm")
        if cf:
            log("  CONFIRMING departure (Ready to Depart)…")
            tap(cf["cx"], cf["cy"], pause=NAV_PAUSE)
    # VERIFY the departure actually committed: the app returns to the home tabs (In Transit).
    # Wait ONLY for home — do NOT treat a visible "Start Loading" as failure on sight. That
    # button belongs to the underlying pickup detail and stays on screen WHILE the departure
    # spinner runs, so matching it mid-commit declared a false 'reverted' API error on a
    # departure that was actually still processing (e.g. A56L187: the saved shot showed the
    # spinner still going). A genuine revert/timeout simply never reaches home and falls
    # through after the budget below. Generous budget so a slow server commit isn't cut off.
    # Wait for home (success) OR an actual API-error dialog. We park a shipment ONLY when a real
    # "API" error is shown — a generic stuck/slow/reverted departure that never reaches home is
    # NOT parked (it just fails this attempt and is retried next cycle), so we don't wrongly
    # blacklist shipments that were merely slow to commit.
    nodes = wait_until(lambda ns: _home_now(ns) or _api_error_shown(ns), tries=30, interval=2.0)
    shot("pickup_departed")
    if _home_now(nodes):
        return (True, ship_units, eta)
    shp = ship_units[0][1] if ship_units else ""
    vin = ship_units[0][0] if ship_units else ""
    if _api_error_shown(nodes):
        log("  departure hit an API error — parking the shipment (won't retry)")
        _record_api_error(shp, vin, "pickup_departure", "API error shown after ETA confirm")
        return (False, ship_units, None)
    # No API error on screen — departure just didn't reach home (stuck/slow/reverted). Leave it
    # for retry next cycle; don't park and don't write a pickup ledger.
    log("  departure did NOT reach home (no API error shown) — leaving for retry, not parking")
    return (False, ship_units, None)


def record_pickups(units, eta):
    """units = [(vin, shipment)]. Records each picked-up VIN to pickups (same DB)."""
    con = sqlite3.connect(DROPOFF_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS pickups(
        vin TEXT, shipment TEXT, model TEXT, eta TEXT, picked_at TEXT,
        UNIQUE(vin, shipment))""")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for vin, shp in units:
        model = MODEL_BY_CHAR.get(vin[3].upper() if len(vin) >= 4 else "", "")
        con.execute("INSERT OR IGNORE INTO pickups VALUES(?,?,?,?,?)", (vin, shp, model, eta or "", now))
        log(f"  ledger(pickup): {vin} ({model}) {shp}")
    con.commit()
    con.close()


def goto_pickup_tab():
    """Reach the home tabs (handling login/restart via goto_in_transit_home) then switch
    to the Pick Up tab."""
    if not goto_in_transit_home():
        return False
    tab = find_text(dump(), "Pick Up (")
    if tab:
        tap(tab["cx"], tab["cy"], pause=1.5)
        wait_until(lambda ns: pickup_buttons(ns) or find_text(ns, "no shipments"), tries=6)
        return True
    return False


def pickup_queue(confirm, max_shipments):
    """Pick up everything currently in the Pick Up tab. Assumes we're on the Pick Up tab
    (process_cycle switches + refreshes first). Returns the count picked up."""
    done = 0
    while done < max_shipments:
        ensure_app()
        nodes = dump()
        tab = find_text(nodes, "Pick Up (")
        m = re.search(r"Pick Up \((\d+)\)", (tab["text"] + tab["desc"]) if tab else "")
        cnt = int(m.group(1)) if m else 0
        btns = pickup_buttons(nodes)
        if cnt == 0 or not btns:
            if done == 0:
                log("Pick Up (0) — nothing to pick up.")
            break
        log(f"Pick Up ({cnt}) — shipment {done + 1}")
        tap(btns[0]["cx"], btns[0]["cy"], pause=NAV_PAUSE)           # open the pickup detail
        wait_until(lambda ns: pickup_units(ns) or find_text(ns, "Start Loading"), tries=8)
        try:
            committed, ship_units, eta = do_pickup(confirm)
        except Exception as e:
            log(f" ! pickup failed: {e}")
            goto_pickup_tab(); continue
        if committed:
            record_pickups(ship_units, eta)
            log(f"  picked up {len(ship_units)} unit(s); ETA {eta}")
        done += 1
        if not committed:
            break                                                   # dry run: stop after one
        wait_until(lambda ns: find_text(ns, "Pick Up (") or handle_dialogs(ns), tries=8)
        goto_pickup_tab()
    return done


def _skip_pickups_path():
    return os.path.join(HERE, "skip_pickups.json")


def _load_skip_pickups():
    """Shipment numbers (SHP…) to NEVER open in the Pick Up queue — shipments wedged
    server-side (e.g. the departure confirm spins forever and reverts) that would otherwise
    loop the queue endlessly. User-managed: edit skip_pickups.json (a JSON list of SHP
    numbers). Missing/invalid file -> no skips."""
    try:
        with open(_skip_pickups_path(), encoding="utf-8") as f:
            return {str(s).strip().upper() for s in json.load(f) if str(s).strip()}
    except (OSError, ValueError):
        return set()


def _shipment_for_button(nodes, btn):
    """The SHP shipment number on the same card as a Pick Up `btn`: the nearest SHP text
    rendered just ABOVE the button (each card is …SHP<num>… then its [Pick Up] button)."""
    cands = []
    for n in nodes:
        m = re.search(r"SHP[\w-]+", (n.get("text") or "") + " " + (n.get("desc") or ""))
        if m and n["bounds"][3] <= btn["cy"] + 20:
            cands.append((btn["cy"] - n["bounds"][3], m.group(0).upper()))
    return min(cands)[1] if cands else None


def _open_unskipped_pickup(skip, max_scrolls=8):
    """Open the first Pick Up card whose shipment isn't in `skip`, scrolling the queue as
    needed. Returns the opened SHP (or '?' if unreadable), or None when every remaining
    pickup is skipped. Skipping by shipment lets the OTHER pickups proceed instead of the
    queue wedging on one bad shipment."""
    for _ in range(max_scrolls + 1):
        ns = dump()
        btns = pickup_buttons(ns)
        before = {_shipment_for_button(ns, b) for b in btns}
        for b in btns:
            shp = _shipment_for_button(ns, b)
            if shp and shp in skip:
                continue
            tap(b["cx"], b["cy"], pause=2.5)
            return shp or "?"
        _scroll_list_down()
        if {_shipment_for_button(dump(), b) for b in pickup_buttons(dump())} == before:
            break                      # nothing new scrolled in → all remaining are skipped
    return None


def _record_api_error(shipment, vin="", stage="pickup_departure", detail=""):
    """Park a shipment that failed with an API error so it is NEVER retried and shows on the
    dashboard as 'API ERROR'. INSERT OR IGNORE: recorded once, never re-marked (no pickup /
    drop-off ledger is ever written for it)."""
    if not shipment:
        return
    try:
        con = sqlite3.connect(DROPOFF_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS api_errors(
            shipment TEXT PRIMARY KEY, vin TEXT, stage TEXT, detail TEXT, seen_at TEXT)""")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        con.execute("INSERT OR IGNORE INTO api_errors VALUES(?,?,?,?,?)",
                    (shipment, vin or "", stage, detail or "", now))
        con.commit(); con.close()
    except sqlite3.Error as e:
        log(f"  (could not record API error for {shipment}: {e})")
    log(f"  API ERROR — {shipment} recorded; it will NOT be attempted again.")


def _api_error_shipments():
    """Set of shipment numbers permanently parked as API errors (never retry)."""
    try:
        con = sqlite3.connect(DROPOFF_DB)
        rows = con.execute("SELECT shipment FROM api_errors").fetchall()
        con.close()
        return {(r[0] or "").strip().upper() for r in rows if r[0]}
    except sqlite3.Error:
        return set()


_all_skipped_logged = False   # so "all pickups skipped" prints once, not every idle cycle


def process_cycle(confirm, max_shipments):
    """Interleave the two queues: pick up ONE shipment, then immediately drop off that
    SAME shipment in In Transit, then go back to Pick Up and repeat. When no pickups
    remain, drain whatever is left in In Transit (pre-existing deliveries, or a pickup
    whose In-Transit entry wasn't found above). ONE pull-to-refresh per cycle.
    Returns (n_pickup, n_dropoff)."""
    global _all_skipped_logged
    if not goto_pickup_tab():
        log("couldn't reach the home tabs.")
        return (0, 0)
    refresh_list()                 # the ONE refresh/cycle (updates both tabs' data)
    shot("home")
    n_pick = n_drop = 0
    # `tried` starts with the persistent skip-list + every shipment already parked as an API
    # ERROR (never retried), and grows as we open each shipment, so a shipment is opened AT
    # MOST ONCE per cycle. That stops the infinite re-pick of a shipment that never leaves
    # the queue (e.g. a departure that spins/reverts server-side) and lets the OTHER pickups
    # get processed instead of starving behind it.
    tried = set(_load_skip_pickups()) | _api_error_shipments()
    while n_pick < max_shipments:
        if not goto_pickup_tab():
            break
        if not pickup_buttons(dump()):
            break                  # no more pickups -> fall through to drain In Transit
        opened = _open_unskipped_pickup(tried)
        if opened is None:
            # Log this ONCE; stay quiet on consecutive idle cycles where nothing changed.
            # Re-armed below as soon as a real pickup is opened, so it shows again later.
            if not _all_skipped_logged:
                log("  remaining pickups are all skipped (skip-list or already tried this cycle)")
                _all_skipped_logged = True
            break
        _all_skipped_logged = False   # a real pickup opened -> allow the notice again later
        tried.add(opened)          # never reopen the same shipment within this cycle
        step("pickup", 1, "select", shp=opened)
        log(f"Pick Up — picking up {opened}")
        wait_until(lambda ns: pickup_units(ns) or find_text(ns, "Start Loading"), tries=8)
        try:
            committed, ship_units, eta = do_pickup(confirm)
        except Exception as e:
            log(f" ! pickup failed: {e}")
            goto_pickup_tab(); continue
        if not committed:
            if not confirm:
                break              # dry run -> stop after one
            goto_pickup_tab(); continue   # couldn't load/depart -> skip it, try the next pickup
        record_pickups(ship_units, eta)
        n_pick += 1
        log(f"  picked up {len(ship_units)} unit(s); ETA {eta} — now dropping off that same shipment")
        try:                       # immediately drop off the SAME shipment in In Transit
            c2, _ = dropoff_matching(confirm, [v for v, _ in ship_units])
            if c2:
                n_drop += 1
            else:
                log("  (its In-Transit entry wasn't visible yet — the drain pass will catch it)")
        except Exception as e:
            log(f"  ! drop off of the picked-up shipment failed: {e}")
    n_drop += drain_queue(confirm, max_shipments)       # remaining In-Transit deliveries
    return (n_pick, n_drop)


# Overnight the queues are quiet, so poll far less often (the day cadence is --watch).
NIGHT_INTERVAL = int(os.environ.get("DRIVE_NIGHT_INTERVAL", "600"))   # 8PM–7AM: every 10 min
NIGHT_START_HOUR = int(os.environ.get("DRIVE_NIGHT_START", "20"))     # 8 PM (host local time)
NIGHT_END_HOUR = int(os.environ.get("DRIVE_NIGHT_END", "7"))          # 7 AM


def _is_night(hour=None):
    """True during the quiet overnight window (default 8PM–7AM, host local time)."""
    h = datetime.datetime.now().hour if hour is None else hour
    s, e = NIGHT_START_HOUR, NIGHT_END_HOUR
    return (h >= s or h < e) if s > e else (s <= h < e)


def poll_interval(day_interval):
    """Seconds to sleep between cycles: NIGHT_INTERVAL overnight, else the day value."""
    return NIGHT_INTERVAL if _is_night() else day_interval


def serve(interval, confirm, max_shipments):
    """Run forever: keep the emulator + app alive, poll, and PICK UP + DROP OFF whatever
    has arrived. Polls every `interval`s during the day and every NIGHT_INTERVAL overnight
    (8PM–7AM). Survives emulator crashes, app crashes, and per-cycle errors (each logged;
    the loop continues)."""
    log(f"service up: pick up + drop off (day every {interval}s, overnight "
        f"{NIGHT_START_HOUR:02d}:00–{NIGHT_END_HOUR:02d}:00 every {NIGHT_INTERVAL}s; "
        f"confirm={confirm}, max {max_shipments}/queue). Ctrl-C to stop.")
    idle = True
    cycle = 0
    while True:
        cycle += 1
        try:
            if not ensure_emulator():
                log("  emulator unavailable; retrying next cycle.")
                time.sleep(poll_interval(interval)); continue
            ensure_app()
            grant_location()
            n_pick, n_drop = process_cycle(confirm, max_shipments)
            if n_pick or n_drop:
                log(f"cycle {cycle}: picked up {n_pick}, dropped off {n_drop}. ledger -> {DROPOFF_DB}")
                idle = False
            else:
                if not idle:
                    log("cycle: both queues empty — waiting for new shipments.")
                idle = True
        except KeyboardInterrupt:
            log("service stopped."); return
        except Exception as e:
            import traceback
            log(f"  ! cycle {cycle} error: {e}\n{traceback.format_exc()}")
        time.sleep(poll_interval(interval))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually complete the drop off")
    ap.add_argument("--max-shipments", type=int, default=10,
                    help="max shipments per drain (per cycle in --watch mode)")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="run continuously as a service: poll every SECONDS, keeping the "
                         "emulator + app alive and picking up / dropping off as units arrive")
    a = ap.parse_args()

    if not os.path.exists(PY):
        sys.exit(f"app-delivery venv missing at {PY}")

    if a.watch:
        serve(a.watch, a.confirm, a.max_shipments)
        return

    # one-shot: pick up + drop off whatever is in the queues right now, then exit.
    if not ensure_emulator():
        sys.exit("no emulator/device and couldn't boot one.")
    ensure_app()
    grant_location()
    n_pick, n_drop = process_cycle(a.confirm, a.max_shipments)
    log(f"done (picked up {n_pick}, dropped off {n_drop}). ledger -> {DROPOFF_DB}")


if __name__ == "__main__":
    main()
