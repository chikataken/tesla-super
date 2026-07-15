# Tesla Capacity Planner — Requested history

A background Tampermonkey userscript for Tesla **Capacity Planner**
(`…/logistics/capacity-planner`). It has no launcher and no popup panel. It quietly captures
Tesla's existing Requested-capacity response and annotates changes directly on the portal grid.

It is read-only toward Tesla: no editable fields, Tesla writes, extra Tesla requests, or
**Confirm Capacity** actions.

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
