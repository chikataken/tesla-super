# Handoff — build the Tesla-portal "driver_marked" Chrome extension

You have access to the whole `tesla-super` repo. This describes where a `driver_marked`
event must land, the (open, no-auth) endpoint to POST it to, and the exact JSON to send.
Background on how this data is used downstream: see
**`app-delivery/MARKING-AND-ERROR-HANDLING.md`** (identity rules, how it's compared to our
own drop-offs). **No auth/tokens for now — keep everything open**; we'll add a shared token
later (copy the SD-webhook `hmac.compare_digest` pattern in `direct-pickup-checks/listener.py`).

## What the extension does
Runs on the Tesla logistics portal (`https://suppliers.teslamotors.com/...`), detects when a
driver marks a shipment, and POSTs that event to a server, which stores it in the
**`driver_marks`** table. Later a matching step correlates each mark to our own drop-offs.

## Where it lands — the `driver_marks` table (ALREADY created)
Lives in **`app-delivery/dropoffs.db`** (same SQLite file the App-tab dashboard reads, so marks
can be joined to our `dropoffs`). Schema (also created by `app_drive.py:_ledger()`):

| column | who fills | meaning |
|---|---|---|
| `id` | auto | PK |
| `mark_id` | **extension** | stable idempotency key (`UNIQUE`); re-sends dedupe. Nullable. |
| `vin` | **extension** | 17-char VIN |
| `order_name` | **extension** | the Tesla shipment/order id as shown in the portal (e.g. `SHP2606-A50B233` or `A50B233`) |
| `order_base` | server | 7-char normalized base of `order_name` (for matching) |
| `order_guid` | **extension** if available | SD/Tesla order GUID, else null |
| `marked_at` | **extension** | when the driver marked it — ISO 8601, **UTC / tz-aware** |
| `status` | **extension** | `"driver_marked"` |
| `source` | **extension** | `"tesla-portal-ext"` |
| `raw` | server | full JSON payload (fidelity) |
| `received_at` | server | server receive time (UTC) |
| `matched_guid` / `matched_ok` | later | filled by the correlation step (leave null) |

## How to reach it — the endpoint (OPEN, no auth) — ALREADY BUILT
The endpoint is **already implemented** in `shipment-creator/app.py`: `POST /api/driver-marks`,
OPEN (no auth), which writes a row into the table above. Public URL:
**`https://shipments.wastake.com/api/driver-marks`**. You do **not** need to add server code —
just build the extension to POST to it. It only needs a one-time
**`sudo systemctl restart shipment-creator-web`** to go live (skip if it already responds).
The implementation, for reference:

```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["https://suppliers.teslamotors.com"],
                   allow_methods=["POST", "OPTIONS"], allow_headers=["Content-Type"])
```

```python
import os, re, json, sqlite3, datetime
_DROPOFFS_DB = os.getenv("DROPOFFS_DB",
                         os.path.join(_HERE, "..", "app-delivery", "dropoffs.db"))

def _order_base(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'^SHP\w*-', '', s.strip(), flags=re.I)
    m = re.search(r'[A-Za-z0-9]+', s)
    return m.group(0)[:7].upper() if m else ""

@app.post("/api/driver-marks")          # OPEN — no auth yet (add a token header later)
def api_driver_marks(body: dict = Body(...)):
    con = sqlite3.connect(_DROPOFFS_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS driver_marks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, mark_id TEXT UNIQUE, vin TEXT,
        order_name TEXT, order_base TEXT, order_guid TEXT, marked_at TEXT,
        status TEXT, source TEXT, raw TEXT, received_at TEXT,
        matched_guid TEXT, matched_ok INTEGER)""")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con.execute(
        "INSERT OR IGNORE INTO driver_marks"
        "(mark_id,vin,order_name,order_base,order_guid,marked_at,status,source,raw,received_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (body.get("mark_id"), (body.get("vin") or "").upper().strip(),
         body.get("order_name"), _order_base(body.get("order_name")),
         body.get("order_guid"), body.get("marked_at"),
         body.get("status") or "driver_marked", body.get("source") or "tesla-portal-ext",
         json.dumps(body), now))
    con.commit(); con.close()
    return {"ok": True}
```

To deploy the new route: **`sudo systemctl restart shipment-creator-web`** (system service).
`index.html` changes are served per-request, but new `app.py` routes need this restart.
Verify: `curl -sX POST localhost:8000/api/driver-marks -H 'Content-Type: application/json' -d '{"vin":"TESTVIN0000000001","order_name":"SHP2606-A50B233","marked_at":"2026-07-02T18:30:00Z","status":"driver_marked"}'`
→ `{"ok":true}`, then `python3 -c "import sqlite3;print(sqlite3.connect('app-delivery/dropoffs.db').execute('select vin,order_name,order_base from driver_marks').fetchall())"`.

## What the extension sends (exact JSON)
```json
{
  "mark_id":    "5YJ3E1EA0MF016891|2026-07-02T18:30:00Z",
  "vin":        "5YJ3E1EA0MF016891",
  "order_name": "SHP2606-A50B233",
  "order_guid": null,
  "marked_at":  "2026-07-02T18:30:00Z",
  "status":     "driver_marked",
  "source":     "tesla-portal-ext"
}
```
Rules that make downstream matching work (don't skip these):
- **`vin`** — the 17-char VIN (required).
- **`order_name`** — the Tesla order id exactly as the portal shows it. This is the key that
  ties a mark to a specific shipment (VIN alone is ambiguous — VINs get re-shipped). Send it
  even if it has a prefix/suffix; the server normalizes to a 7-char base.
- **`marked_at`** — ISO 8601, **UTC/tz-aware** (e.g. end in `Z` or `+00:00`). Used as the
  tie-breaker when a VIN maps to more than one shipment. Do **not** send a bare local time.
- **`mark_id`** — a stable id (e.g. `vin + "|" + marked_at`) so retries don't create duplicates.
- `order_guid` — include if the portal exposes the SD/Tesla order GUID; otherwise `null`.

## Extension architecture (MV3, recommended)
- `manifest.json`: `"host_permissions": ["https://shipments.wastake.com/*"]`, a content script
  matched on the portal, and a background **service worker**.
- Content script detects the mark and reads `{vin, order_name, marked_at, order_guid?}` from the
  portal DOM/network → `chrome.runtime.sendMessage(...)`.
- **Service worker** does the `fetch()` POST. A SW with `host_permissions` for the target host is
  **not** subject to page CORS, so no preflight issues (the endpoint's permissive CORS above is
  just a backup for content-script fetches). Fire-and-forget with a small retry/queue.

```js
// background service worker
async function sendMark(mark) {
  try {
    await fetch("https://shipments.wastake.com/api/driver-marks", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mark),
    });
  } catch (e) { /* queue + retry later */ }
}
```

## Not now (later)
- **Auth:** add a shared-secret header (e.g. `X-Mark-Token`) checked server-side with
  `hmac.compare_digest`, token in `secrets/.env` — the SD webhook in
  `direct-pickup-checks/listener.py` is the pattern. Keep it open until the flow works.
- **Correlation step:** matching `driver_marks` → `dropoffs` (by `order_guid`, else
  `order_base` + `marked_at` time-window) to fill `matched_guid`/`matched_ok`. See
  `app-delivery/MARKING-AND-ERROR-HANDLING.md`.
