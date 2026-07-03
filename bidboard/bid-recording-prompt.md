# Bidboard — resume prompt: record every submitted bid to a server DB

Paste everything below the line into a fresh Claude Code session in `tesla-super/` to resume the
bid-recording feature (designed, not yet built). It captures the goal, the locked decisions, and the
exact server + client changes.

---

## Goal
Continue the bidboard feature. The userscript `bidboard/tesla-bidboard-helper.user.js` overlays the
Tesla carrier portal and, in `submitCard()` (line ~156), already POSTs a `MakeOffer`/`UpdateOffer`
to Tesla for every VIN in a priced route card — but persists nothing. Capture every bid it submits
to a server DB so I can build a dataset of my bidding behaviour over time (price vs. Tesla list,
new-vs-update re-bids, chosen ETA vs. recommended, per-route/per-model patterns).

## Decisions already made — do NOT re-litigate
- **Host on the running shipment-creator app** (`shipments.wastake.com` → `127.0.0.1:8000`), reusing
  its Cloudflare tunnel + systemd service. No new subdomain/service.
- **Record-only** for now (no viewer UI yet). **Rich** per-bid records.
- Recording is **fire-and-forget** — it must never block or change the live bidding path. A down
  server or failed POST logs a warning and is otherwise ignored.

## Server changes (in `shipment-creator/`)
- New `bids_db.py` mirroring `recorder_db.py` conventions (WAL SQLite at `data/bids.db`, idempotent
  `CREATE TABLE IF NOT EXISTS`, lazy `connect()`), one **append-only** `bids` table — no dedup /
  unique constraint, because a re-bid on the same VIN is a wanted new row. Columns:
  `id, batch_id, client_ts, received_at, origin, destination, origin_state, dest_state, vin, bid_id,
  model, vclass (std|ct|cab), price, currency, list_price, prev_counter, verb (MakeOffer|UpdateOffer),
  pickup_date, eta_date, eta_offset, need_by_date, success, error, raw`.
  Function `insert_bids(batch_id, client_ts, records) -> int` (one transaction; coerce numeric fields
  defensively — prices arrive as strings; missing fields → NULL).
- `app.py`: add `CORSMiddleware` scoped to `https://suppliers.teslamotors.com`
  (`allow_methods=["POST","OPTIONS"], allow_headers=["Content-Type"]`) so the browser can post
  cross-origin (handles the JSON preflight). Add a route following the `/api/price` (line ~1791)
  `Body(...)` pattern:
  ```python
  @app.post("/api/bids")
  def api_bids(body: dict = Body(...)):
      recs = body.get("bids") or []
      n = bids_db.insert_bids(body.get("batch_id"), body.get("client_ts"), recs)
      return {"ok": True, "recorded": n}
  ```
  No profile needed — the global `_capture_profile` dependency only reads the `X-Profile` header and
  never rejects.

## Client changes (`bidboard/tesla-bidboard-helper.user.js`)
Keep `@grant none` so the script stays in the page's JS context and its `window.XMLHttpRequest` hook
(which captures Tesla's endpoint/auth, lines ~44–56) keeps working. Do **NOT** switch to
`GM_xmlHttpRequest` — that sandboxes the script and breaks the hook.
- Bump `@version` (0.18.0 → 0.19.0); note "records every submitted bid to shipments.wastake.com".
- Add near the top: `const RECORDER_URL = 'https://shipments.wastake.com/api/bids';`
- In `submitCard()` — the bidding logic is untouched; only accumulate + send:
  - Make a `batchId` once per call (`crypto.randomUUID()` with a fallback).
  - Inside the existing `for (const b of vins)` loop, push one record per VIN (all columns above,
    from vars already in scope: `g`, `inp`, `b`, `verb`, `body`, `state.dates[legKey(g)]`), stamping
    `success`/`error` from the same try/catch that tallies `sent`/`failed`.
  - After the loops, fire-and-forget:
    ```js
    if (records.length) fetch(RECORDER_URL, { method:'POST', mode:'cors', credentials:'omit',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ batch_id: batchId, client_ts: new Date().toISOString(), bids: records }) })
      .catch(e => console.warn('[bidpanel] bid-record POST failed', e && e.message));
    ```

## Verify
1. Restart the service to load the new module/route: `sudo systemctl restart shipment-creator-web`.
2. `curl -sX POST localhost:8000/api/bids -H 'Content-Type: application/json' -d '{"batch_id":"t1","client_ts":"2026-07-01T00:00:00Z","bids":[{"vin":"TESTVIN","price":"499","verb":"MakeOffer","origin":"NA-US-NJ-X","destination":"NA-US-MA-Y","vclass":"std","success":1}]}'`
   → expect `{"ok":true,"recorded":1}`.
3. `sqlite3 shipment-creator/data/bids.db 'select vin,price,verb,success from bids;'` → shows the row.
4. CORS: `curl -si -X OPTIONS localhost:8000/api/bids -H 'Origin: https://suppliers.teslamotors.com' -H 'Access-Control-Request-Method: POST'` → `access-control-allow-origin` header present.
5. Client end-to-end: reinstall the bumped userscript, price one route, press Enter. DevTools → Network
   shows `POST /api/bids` returning 200 after the Tesla offers; the row count matches the card's
   "sent N"; recording failure must not affect the green/red submit state.

## Out of scope (later)
- Viewer / analytics UI (`GET /api/bids` + a tab).
- Backfilling past bids (no historical source; the log starts now).
