# Tesla Capacity Planner — full-screen planner UI + Requested history

A Tampermonkey userscript for Tesla **Capacity Planner** (`…/logistics/capacity-planner`).
Since v0.5.0 it **replaces the whole page** with its own bidboard-style panel (Tesla's grid
keeps running hidden underneath — its own fetches are what feed the panel).

It is read-only toward Tesla: no editable fields, Tesla writes, extra Tesla requests, or
**Confirm Capacity** actions (the panel's Confirm Capacity buttons are dummies until the
write contract is captured).

## Full-screen planner panel (v0.7.0)

Spliced over Tesla's own grid on the capacity-planner route only (same embed technique
and self-guarded placement as bidboard, so it never leaks onto other portal pages).
Since v0.7.0 it is styled to read as part of the Tesla portal itself: no panel borders or
gutters (the host sits flush in Tesla's layout), white-on-white with only hairline row
separators like the native grid, and Tesla's own **Universal Sans Text / Display** fonts
(already loaded by the page, so the shadow DOM inherits them):

- **Transposed grid (v0.9.0): rows = dates, columns = lanes.** All 14 days — Monday of
  this week through Sunday of next — are rows in one continuous view (no week tabs), with
  a NEXT WEEK divider row. Lane columns are grouped under their origin (Tesla Inc
  Gigafactory Texas · Fremont Factory), each group ending in a **Total** column
  (confirmed/requested + scheduled, red when under-confirmed).
- **Date column = the Confirm Capacity control (v0.10.0).** Each date is a single
  centered `Fri 17` label. While the day still needs confirming the **entire date cell
  is highlighted light blue** (v0.13.0) and is the (dummy) Confirm Capacity click
  target. Once a day counts as confirmed — past date, or Tesla already returns
  confirmed capacity for it (heuristic until the real flag is captured with the write
  path) — the date cell goes **white** and the rest of the **row grays out**.
- **Lane names link to Tesla's lane pages (v0.13.0).** Column headers are real links
  opening `…/logistics/calendar-view/{originId}/{destGroupId}?isOriginGroup=…` in a
  new tab — the exact URL Tesla's own grid opens when a lane name is clicked
  (captured from the native `window.open` handler).
- **Cells are two fully-shaded halves** filling the entire cell height (v0.11.0; the
  table uses a fixed layout whose lane columns share the window width evenly, so the
  grid always fits a full-size browser window with no horizontal scrolling, and the
  Total column shows just `confirmed / requested`): **left half = scheduled** — an
  editable entry on a light tint, placeholder = Tesla's current confirmed amount;
  typed drafts persist in Tampermonkey storage until the confirm write is wired. It
  shades **red when the effective value doesn't match requested** (no green), live
  while typing. **Right half = requested** on a slightly darker tint — it highlights
  **amber while a change is unacknowledged**, with a single **▲/▼ ticker to the LEFT
  of the current number** (green = increased, red = decreased vs the previous value).
  Highlighted cells keep **black text** — only the ticker carries color. The ticker is
  pinned to the half's left edge (out of flow, so the number stays centered) and
  **remains visible after the amber is acknowledged** (v0.14.0). **Clicking the amber
  half acknowledges the change** (pulse back to the base tint); acknowledgements
  persist per change timestamp, so a newer change re-ambers the box. The current
  day's row carries a **thin blue outline** all the way around (v0.14.0).
- **Hover any requested box** with recorded history for the **history card** (v0.12.0):
  a vertical column of the recorded values, **most recent at the top**, each with its
  date + time; values are tinted green/red by direction vs the chronologically previous
  one. A header chip counts **unacknowledged** requested changes from the last 48 h.
  History comes from the server change log (`GET …/api/capacity-history?days=14`,
  refreshed ~3 min; local GM history is the offline fallback).
- **Confirm Capacity buttons** per remaining day — dummies for now (toast only); real
  confirming happens on the native grid.
- **Bottom-right button** (same dark launcher style as the other extensions): **Tesla grid**
  hides the panel and restores Tesla's original page; it then reads **Planner UI** to come
  back. Also in the Tampermonkey menu: *Toggle Tesla native grid*.

## Install

Install `capacity-planner/tesla-capacity-planner-viewer.user.js` in Tampermonkey. Auto-update is
wired to the GitHub raw URL; bump `@version` whenever publishing an update.

## Server change log (v0.4.0)

Besides the local GM history below, the userscript now also mirrors **both** capacity feeds
(`requestcapacity` and `getcapacityconfirmations`) to the shipment-creator server:

- `POST https://shipments.wastake.com/api/capacity-snapshot` (via `GM_xmlhttpRequest`,
  debounced ~2 s, byte-identical payloads skipped). Still zero extra Tesla requests —
  it only forwards what the page already fetched.
- The server keeps **append-only change logs** in `app-delivery/dropoffs.db`:
  `capacity_request_log` (requested amounts) and `capacity_confirm_log`
  (confirmed / scheduled / conflict). A row is written only when a value differs from
  the last recorded one for that `carrier + origin + destination group + date`, so the
  log is a compact timeline of every observed change — e.g. `6 → 3 (Jul 12) → 9 (Jul 14)`.
- `GET https://shipments.wastake.com/api/capacity-history?days=14` returns the logged
  changes for the upcoming full-screen planner UI (this week / next week, requested vs
  scheduled, per-lane change timelines).

History density is gated by visits: Tesla only exposes the *latest* value plus
`latestRequestDate`, so a change is recorded when someone next opens Capacity Planner.
Intermediate values between visits are unobservable. Requires the server route to be
live: `sudo systemctl restart shipment-creator-web` after deploying `shipment-creator/app.py`.

## Requested-capacity history (v0.3.0)

On every Capacity Planner load, the first observed Requested value for each
`carrier + origin + destination group + date` becomes its baseline. If Tesla later changes that
same Requested value, only the Requested number in the portal's `/ Y` display becomes a **red
badge with white text**.

Hovering the badge shows:

- Origin and destination lane
- Capacity date
- Previous value
- Current value
- Signed increase or decrease
- When the extension detected the change

History is stored with Tampermonkey `GM_setValue`, so it survives page refreshes, browser and
computer restarts, and userscript updates. It remains local to that browser profile. Changed
values stay marked across visits until the Tampermonkey menu command **Clear Capacity Planner
requested history** is used. Newly encountered lane/dates establish a baseline and are not red.

## How it works

- Hooks `fetch` and XHR at `document-start`.
- Passively captures the page's existing
  `…/CapacityPlanner/carrier/requestcapacity` response.
- Records all Requested values returned by Tesla, including dates outside the currently visible
  week.
- Uses Tesla's stable carrier, origin-location, destination-group, and date IDs for matching.
- Reapplies red markers after Angular rerenders, week changes, refreshes, and SPA navigation.
- Makes no SuperDispatch or external-server calls.

See [findings.md](findings.md) for the original Capacity Planner API reconnaissance.
