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
