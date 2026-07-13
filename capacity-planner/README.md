# Tesla Capacity Planner — Viewer (read-only)

A Tampermonkey userscript for the Tesla **Capacity Planner** tab
(`…/logistics/capacity-planner`). It adds a launcher button; clicking it opens a
draggable popup that shows **everything the page holds** — every origin →
destination-group lane, and for each day the **Confirmed / Scheduled / Requested**
capacity, conflict flags, and last-requested time — in one formatted, scrollable
grid.

**It is strictly read-only.** No editable fields, no writes, no "Confirm
Capacity". It only reads the portal's own two API responses (captured live from
the page) and reformats them. See [findings.md](findings.md) for the recon.

## Install
1. Tampermonkey ▸ Create a new script, or open the raw file
   `capacity-planner/tesla-capacity-planner-viewer.user.js` and install.
   (Auto-update is wired to the GitHub raw URL; bump `@version` to push updates.)
2. Go to the **Capacity Planner** tab. A small **Capacity Planner** pill appears
   bottom-right (same convention as the dispatch-dashboard script). Click it to
   open the popup (click again, or the ×, to close).

## What the numbers in each box mean
Every lane/day box stacks three numbers — the **Confirmed** figure big, with
`scheduled / requested` beneath it:

| Mark | Name | Meaning |
|------|------|---------|
| big number | **Confirmed** | The capacity **you** have committed to for that lane that day — the editable "Capacity Confirmed" box on the portal. |
| `…s` | **Scheduled** | Loads **actually booked / assigned** on that lane that day (the portal's "N Scheduled"). |
| `…r` | **Requested** | The capacity **Tesla asked you** to cover (the portal's `/ Y`). |

The dark **origin row** is the roll-up: each of its numbers is the sum of the
groups below it for that day. A cell is tinted **pink (conflict)** when the
portal flags a mismatch — typically **Confirmed < Requested** (you've committed
less than Tesla asked), i.e. the ⚠️ on the portal. Hover any cell for the exact
`Confirmed · Scheduled · Requested` plus the **last-requested** timestamp.

## What the popup shows
- **Rows** = lanes, grouped under each **origin location** (`Fremont Factory`,
  `Tesla Inc Gigafactory Texas`, …). The dark origin row is a **roll-up** (sum of
  its groups per day); the rows beneath are the destination groups (`SoCal FCP`,
  `Austin Local`, `North Texas`, …).
- **Columns** = every day in the data (~20 days — wider than the portal's visible
  week). Weekends are shaded; today's column is highlighted blue.
- **Each cell** shows the **Confirmed** number big, with **`Ns Nr`** below it =
  `scheduled` and `requested`. Hover a cell for the full breakdown incl. the
  **last-requested** timestamp. Cells the portal flags as a **conflict** are
  pink with a red number.
- **Filter box** narrows to lanes/origins by name. **Reload** re-pulls the two
  endpoints (falls back to the passively-captured snapshot if the replay is
  refused — refresh the portal page for the freshest data).

## How it works
- Hooks `fetch`/XHR at `document-start` and captures the tab's own two GETs:
  `getcapacityconfirmations` (Confirmed + Scheduled + conflict flag) and
  `requestcapacity` (Requested + the human-readable origin/group **names** +
  last-requested time). Both live under
  `…/logisticsportalapi/api/v1/CapacityPlanner/carrier/`.
- Joins them on `originLocationId` + `destinationGroupId` + date and renders the
  matrix. No SuperDispatch or other external call — this is entirely
  Tesla-portal data for your own carrier.
- The button and popup only appear on the `capacity-planner` route and hide
  themselves the instant the SPA navigates elsewhere.

## Legend
`C` Confirmed · `s` Scheduled · `r` Requested · pink = conflict · shaded = weekend.
