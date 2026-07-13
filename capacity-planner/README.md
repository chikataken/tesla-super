# Tesla Capacity Planner — Requested history

A background Tampermonkey userscript for Tesla **Capacity Planner**
(`…/logistics/capacity-planner`). It has no launcher and no popup panel. It quietly captures
Tesla's existing Requested-capacity response and annotates changes directly on the portal grid.

It is read-only toward Tesla: no editable fields, Tesla writes, extra Tesla requests, or
**Confirm Capacity** actions.

## Install

Install `capacity-planner/tesla-capacity-planner-viewer.user.js` in Tampermonkey. Auto-update is
wired to the GitHub raw URL; bump `@version` whenever publishing an update.

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
