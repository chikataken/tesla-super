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

## Default Search By: VINs (v0.16.1)
Whenever Dispatch Dashboard 2.0 is entered, the userscript defaults searches to VINs without
opening or clicking Tesla's dropdown. The page's own `GetCarrierDispatchShipment` request is
intercepted before send and only its search field is translated from `shipmentNumbers:[...]` to
`vins:[...]`; the same translation applies to the dashboard's Excel download request. Alerts,
dates, statuses, carrier, and paging are left unchanged. The displayed
selector and neighboring placeholder are kept at **VINs** / **Enter VINs** so the UI matches the
actual request. A deliberate manual choice of **Shipment** or **Shipment Numbers** disables the
translation for the rest of that dashboard visit and restores the shipment label/placeholder even
when Tesla already had Shipment selected internally; leaving and re-entering restores the VIN
default.

## Deliver / Andrew Enkh control (v0.13.1)
On every rendered Dispatch Dashboard shipment card, the userscript visually replaces Tesla's
**License Plate** label and its input/save controls with a matching **Deliver** label and
**Andrew Enkh** action. Both replacement elements are deep clones of that card's native Driver
label and selector—not approximated CSS—so their font, height, border, arrow, spacing, and vertical
position are inherited directly from Tesla. Tesla's original Angular controls are hidden rather than deleted, which
keeps framework change detection intact. A MutationObserver reapplies the replacement after
searches, pagination, and SPA re-renders.

The button resolves the visible shipment number against the shipment metadata already captured
from `GetCarrierDispatchShipment`, then immediately assigns **Andrew Enkh** (`driverId:136062`)
with the recorded single-shipment contract: `POST …/AssignDrivertoShipment` and
`{shipmentId,driverId:136062,carrierId,driverJobStatus:"PENDING",source:"TVP"}`. The carrier ID
comes from that shipment, falling back to the selected-carrier request header. The button shows
yellow while assigning, green only after Tesla returns a successful response, and red/retry on
HTTP or `success:false` errors. It does not reload the dashboard.

## "Clean Pickups" (v0.16.2) — pickup dates + Driver Needed assignment
Clicking **Clean Pickups** once immediately scans the board for both **Pickup Date Late**
(id 1) and **Pickup Date Today** (id 7) across all non-delivered stops, then bulk-moves
**all** of them to the **next weekday at
16:00Z (4 PM)** with **reason 4** — the
exact contract we recorded: `POST …/updateestimatedshipdate?dateTrackingSource=3` with
`{updateEstimatedShipDateList:[{updateReasonId:4, estimateShipDate, stopId}]}` (chunked 100).
The target is based on the day the button is pressed. Friday through Sunday roll to Monday.
The idle button caption displays that calculated weekday (for example, `Monday 4PM`) and refreshes whenever the menu opens.
Bounds: 90-day SHP-create-date window + `take:5000` (no pagination).

The same click independently scans **Driver Needed** (id 2), deduplicates matches by
`shipmentId`, and assigns **JESSICA TFI** (`driverId:67651`) only to
those shipments. It uses the recorded mass endpoint
`POST …/UpdateShipmentsDriverAndLicensePlate`, groups requests by the carrier ID returned by
Tesla (falling back to the selected-carrier header), chunks shipment IDs 100 at a time, and sends
`{shipmentIds,driverId:67651,carrierId,driverJobStatus:"PENDING",source:"TVP",truckLicensePlate:""}`.
The button stays yellow while scanning and writing, then turns green only after every applicable
pickup and driver response passes HTTP and `success:false` validation. If driver assignment fails
after pickup dates succeeded, the red
error state explicitly reports that the pickup updates already landed.

Note: `updateReasonId:4` is copied verbatim from the recorded manual edit. Change it (and the
`16:00` time / next-weekday rule) at the top of `updatePickups`/`nextWeekday16` if the desired reason changes.

## "Clean ETA" (v0.16.2) — bulk ETA write
Clicking **Clean ETA** once immediately runs independent scans for
**Late ETA** (id 3) and **ETA Today** (id 6), verifies those alert ids on every returned stop,
merges duplicate `stopId` values, and moves all matches to the **next calendar day**, including
Saturday and Sunday, with a **4 PM ETA window**. The button stays yellow while scanning and
writing, then turns green once the work succeeds. Its idle caption uses the same next-day
calculation. The recorded write contract is `POST …/updateStopEta` with an array of:
`{StopId, EtaUpdateSourceId:3, EstimatedDeliveryDate, EtaTimeWindowEndInHours:16,
EtaUpdateReasonId:4}`. Writes are chunked 100 and HTTP-200 responses containing
`success:false` are treated as failures.

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
