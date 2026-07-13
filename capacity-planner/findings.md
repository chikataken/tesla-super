# Tesla Capacity Planner — live-page findings (recon)

Page: `https://suppliers.teslamotors.com/logistics/capacity-planner`
Recon done in the logged-in tab via Claude-in-Chrome (read-only — no writes fired).
Account: **TFI Trans Inc** (carrierId `378`).

## What the page is
A **week grid** of confirmed vs. scheduled trucking capacity, by lane.

- **Rows = lanes.** Grouped under an **origin location** (e.g. `Fremont Factory`,
  `Tesla Inc Gigafactory Texas`), each expanded into its **destination groups**
  (`SoCal FCP`, `Southwest FCP`, `PNW FCP`, `Austin Local`, `North Texas`,
  `South Texas`, `Southeast`, `Southwest`, `Lubbock`, …).
- **Columns = days** of the selected week (Mon…Sun), pageable ‹ › one week at a
  time, with a date-range chip (e.g. `Jul 06 – Jul 12 2026`). The two trailing
  days show `+1 Days` / `+2 Days` labels.
- **Origin summary rows** (`Fremont Factory`, `Tesla Inc Gigafactory Texas`) show a
  rolled-up `N Scheduled` and an `X / Y` per day.
- Each destination cell shows an **editable number input** (the confirmed
  capacity) next to a **`/ Y`** (the requested/needed figure), e.g. `1 / 1`,
  `6 / 6`, `0 / 2`. A ⚠️ warning icon marks conflict cells; a ● blue dot marks
  some weekend/trailing cells.
- Top-right: **Export** button, **Legends** link, and a blue **CONFIRM CAPACITY**
  button (the write action — not exercised during recon).

## Platform
- Same stack as the other portal tools: **Angular SPA**, Tesla "tsl"/OS design
  system, `apigateway-logisticsportalapi.tesla.com` backend.
- Auth is the app's bearer token in `localStorage['logisticsportal:token']` plus
  carrier context (`logisticsportal:attrs:CarrierId` = `"378"`,
  `logisticsportal:attrs:carrierIds`). A **direct `fetch()` from page JS fails**
  ("Failed to fetch") — same CORS/context wall as bidboard; the app's own calls
  must be captured by hooking `fetch`/XHR (survives SPA nav; a full reload wipes
  the hook, so re-inject then SPA-navigate away+back to force a fresh fetch).

## APIs (both GET, captured live)
Base: `…/logisticsportalapi/api/v1/CapacityPlanner/carrier/`

### 1. `getcapacityconfirmations` — the confirmed/scheduled numbers (the grid values)
```
GET  …/CapacityPlanner/carrier/getcapacityconfirmations   → { data, success, message }
data = {
  carrierId: 378,
  locationCapacities: [                 // one per origin location
    {
      originLocationId: 200646,
      isOriginGroup: false,
      groupCapacities: [                 // one per destination group
        {
          destinationGroupId: 394,
          confirmCapacities: [           // one per day (a ~19-day span, not just the visible week)
            { capacityDate: "2026-07-10T00:00:00", capacity: 1, scheduled: 1, isConflict: false },
            …
          ]
        }, …
      ]
    }, …
  ]
}
```
- `capacity` = the **confirmed** number (left, editable cell); `scheduled` = what's
  actually booked; `isConflict` = the ⚠️ flag (drives the warning icon).
- **No names here** — only numeric `originLocationId` / `destinationGroupId`.
  The names come from `requestcapacity` (below); join on those IDs.

### 2. `requestcapacity` — the requested/needed numbers **and the labels**
```
GET  …/CapacityPlanner/carrier/requestcapacity   → { data, success, message }
data = {
  carrierId: 378, carrierName: "TFI Trans Inc",
  locationRequests: [
    {
      originLocationId: 200646,
      originLocationName: "Fremont Factory",
      isOriginGroup: false,
      groupRequests: [
        {
          destinationGroupId: 394,
          destinationGroupName: "SoCal FCP",
          capacityRequests: [            // one per day
            { date: "2026-07-10T00:00:00", capacity: 1, latestRequestDate: "…" },
            …
          ]
        }, …
      ]
    }, …
  ]
}
```
- This is the **`/ Y` (requested / needed)** side of each cell, and it carries the
  human-readable `originLocationName` / `destinationGroupName` that
  `getcapacityconfirmations` omits.
- `latestRequestDate` = when Tesla last (re)requested that day's capacity.

### Observed structure for TFI (378)
- **2 origin locations:**
  - `Fremont Factory` (id 200646) → 3 dest groups: SoCal FCP (394), Southwest FCP
    (402), PNW FCP (399).
  - `Tesla Inc Gigafactory Texas` (id 925220) → 6 dest groups: Austin Local (410),
    North Texas (411), South Texas, Southeast, Southwest, Lubbock.

## The write path (NOT captured — needs a real confirm to observe)
The **CONFIRM CAPACITY** button and the editable per-cell inputs imply a write
endpoint (almost certainly a `POST` under the same `CapacityPlanner/carrier/`
base — likely `confirmcapacity` / `savecapacity` or similar). It was **not fired**
during read-only recon. To wire up an editing/auto-confirm tool later, capture one
real confirm with the XHR hook to get the exact verb, path, and body — same method
the bidboard recon used for `UpdateOffer`/`MakeOffer`.

## Implications for a tool
- A recolor/annotation userscript (à la regular-fleet) is straightforward from the
  **two GETs alone**: join `getcapacityconfirmations` + `requestcapacity` by
  `originLocationId` + `destinationGroupId` + date, then flag cells where
  `confirmed < requested` (under-confirmed) or `scheduled > confirmed`, or surface
  `isConflict`. No SuperDispatch call needed — this is entirely Tesla-portal data.
- Because `confirmCapacities` spans ~19 days (wider than the visible week), a tool
  could show a rolling multi-week view without extra requests.
- Any auto-*confirm* feature requires the write endpoint above — get that first.
