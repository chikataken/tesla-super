# Tesla Shipment Planner EU Filter

Tampermonkey userscript for Tesla's Shipment Planner page.

## Behavior

- Runs only on `/logistics/fv-shipment-planner/review`.
- **NA origins first (v0.2.0):** the planner's `GetShipmentPlannerReviewDashboard`
  endpoint returns every shipment in one response and the table pages/sorts
  client-side, so the userscript intercepts that XHR response and stable-sorts
  `data` — origins not starting with `EU` first, EU origins last — before Angular
  renders it. NA shipments therefore fill the first pages instead of being pushed
  behind hidden EU rows. The reorder never drops records and leaves the rest of
  the response untouched; on any parse error the original response passes through.
- Filters only while **Available To Bid** is selected.
- Hides a shipment when the trimmed destination text starts with `EU`.
- Hides the shipment's paired expansion/detail row as well.
- Reapplies automatically after SPA navigation, tab changes, pagination, searches,
  and Angular table rerenders.
- Makes no API requests and does not bid or modify Tesla data.

## Install

[Install the userscript](https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js)

Tampermonkey will use the same raw GitHub URL for future update checks. Increment the
userscript `@version` whenever publishing a new version.
