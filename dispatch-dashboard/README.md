# Dispatch Dashboard — Recorder (dev tool)

A throwaway Tampermonkey userscript for **Dispatch Dashboard 2.0**
(`…/logistics/dispatchdashboard2`). It **piggybacks** on the page's own data calls and
records every VIN + shipment status it sees into a dev panel. **It makes no requests of
its own and sends nothing anywhere.** Meant to be deleted once we've learned what we need.

## What it does
- Hooks `XMLHttpRequest` at `document-start` and watches only for the dashboard's own
  `POST …/DispatchDashboard/GetCarrierDispatchShipment` responses — reading the JSON the
  browser already fetched (**zero extra load on Tesla, nothing anomalous to detect**).
- For every VIN in that response it records: **status** (Tendered / Transit / At Destination /
  Delivered), shipment #, service level, origin → destination, **derived dispatcher**
  (from the origin state, mirroring `shipment-creator/profiles.json`), pickup / need-by / ETA
  dates, ETA reason, alert ids, carrier id.
- Accumulates across every pull you look at, **keyed by VIN**, persisted in Tampermonkey
  storage (survives reloads). Since it only sees what the page fetches, paginate / re-search
  to accumulate more.

## Panel
A floating **"DD Recorder"** button (bottom-right, on the dashboard only). Click to open:
- Live counts (VINs captured, pulls piggybacked, last Tesla total, last capture time)
- Search box + status filter chips
- Full table of every captured VIN
- **Copy JSON** / **Download** (`dispatch-dashboard-vins.json`) / **Clear**

Tampermonkey menu: *Toggle recorder panel*, *Clear recorded data*.

## Install
Tampermonkey ▸ install `tesla-dispatch-dashboard-recorder.user.js` (auto-update wired to the
GitHub raw URL; bump `@version` to push updates).

## "Clean Pickups" (v0.9.0) — bulk pickup-date write
**Clean Pickups** (scan-first, tap-to-confirm): scans the board for the **Pickup Date Today**
alert (id 7) across all non-delivered stops, shows the count (`N → date · Confirm?`), and on
confirm bulk-moves **all** of them to the **next day at 16:00Z (4 PM)** with **reason 4** — the
exact contract we recorded: `POST …/updateestimatedshipdate?dateTrackingSource=3` with
`{updateEstimatedShipDateList:[{updateReasonId:4, estimateShipDate, stopId}]}` (chunked 100).
Verified live (read-only): the alert-7 scan returned 403 targets, dates computed correctly.
Bounds: 90-day SHP-create-date window + `take:5000` (no pagination).

Note: `updateReasonId:4` is copied verbatim from the recorded manual edit. Change it (and the
`16:00` time / next-day rule) at the top of `updatePickups`/`nextDay16` if the desired reason changes.

## "Pull red VINs to mark" (v0.6.0) — targeted reconciliation
Menu button that re-checks **only** the App-tab **Unmarked (red)** VINs on Tesla, instead of
scanning everything:
1. GETs the App-tab delivered list (`shipments.wastake.com/app/delivered`, via `GM_xmlhttpRequest`)
   and filters to **red** = rows whose status is **not** `app` and **not** `marked` (the exact
   `isGreen` rule the Unmarked tab uses).
2. Batch-queries those VINs on Tesla in one request each (`vins:[...]`, chunked at 100), 180-day
   window, all statuses incl. Delivered.
3. POSTs the fresh `{vin,order_name,status,eta}` to `/api/tesla-status` (upsert).

Any red VIN that Tesla now shows **Delivered** flips `delivered → marked` (green) on the next
App-tab refresh and drops out of the Unmarked tab. Verified live: 46 red VINs → 39 already
Delivered on Tesla (would green), ~9 s. Much lighter than the 2-week pull.

## Auto-send piggybacked VINs (v0.4.0)
When **auto-send** is on (default), every pull you passively piggyback — i.e. VINs you search
or paginate through on the dashboard — is also POSTed to `/api/tesla-status`, **debounced ~2 s**
so a browsing burst batches into one request and **deduped** by `vin+shipment`. This keeps the
server's `tesla_dispatch_status` continuously fresh from normal use (no button press needed for
tendered/transit). It still only covers what your current filter fetched, so **Delivered** still
needs the button (it's the heavy pull). Toggle it with the **`auto→srv: on/off`** button in the
panel header (persisted in Tampermonkey storage). Same `{vin,order_name,status,eta}` payload and
same upsert behavior as the button.

## "Pull 2 wks → server" button (v0.3.0)
The green header button does ONE deliberate extensive pull — last 2 weeks, **all** statuses
incl. **Delivered** — using the bearer token captured off the page's own requests, then POSTs
`{vin, order_name, status, eta}` for every VIN to **`https://shipments.wastake.com/api/tesla-status`**
(via `GM_xmlhttpRequest`, so no CORS issues). The server upserts them into a
`tesla_dispatch_status` table in `app-delivery/dropoffs.db`, and the **App-tab delivered list**
overlays them per row:

| Tesla dispatch status | App-tab pill |
|---|---|
| Delivered | **marked** |
| Tendered | **tendered** |
| Transit | **transit** |
| At Destination / VIN not found | delivered (unchanged) |
| (VIN drop-off in our `dropoffs`) | **APP** — overrides everything |

Matching = 7-char `order_base` (last segment of the shipment name), last resort within 7 days
(same rule as the drop-off link validation). Passive recording still sends nothing; only this
button POSTs. Requires the server route to be live: `sudo systemctl restart shipment-creator-web`.

## Notes
- **Alert labels** are wired in from the portal's own `getdispatchalertsbycarrier` endpoint:
  1 Pickup Date Late · 2 Driver Needed · 3 Late ETA · 4 Incorrect Driver ETA ·
  5 No Action Needed · 6 ETA Today · 7 Pickup Date Today. The Alerts column shows these names.
- Verified live: one Search captured 76 VINs (Tendered/Transit) with correct dispatcher +
  date parsing.
