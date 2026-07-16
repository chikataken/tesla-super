# Tesla Shipment Planner Helper

Tampermonkey userscript for Tesla's Shipment Planner page.

## Behavior

- Runs only on `/logistics/fv-shipment-planner/review`.
- **Defaults:** on each visit, selects **Available To Bid** and changes **Show** to
  **25**. These are applied once, so deliberate tab or page-size changes remain
  respected for the rest of that visit.
  - **Show = 25 (v0.4.0 fix):** the "Show" control is a Tesla Design System
    `<tds-dropdown-select class="tds-pagination-page-size-select">`, **not** a native
    `select`/`mat-select`. Earlier versions looked for a `select` and silently failed,
    so the page size never changed. The script now opens that dropdown and clicks the
    `25` option (options are `5/10/15/25`). Page size is client-side only — the request
    carries no page-size param — so it must be driven through the control.
- **Ready Date = today ± 2 weeks (v0.4.0, no GUI):** the planner's
  `GetShipmentPlannerReviewDashboard` POST body carries `readyDateFrom` / `readyDateTo`
  and the server filters on them. The script rewrites those two fields in the request
  body to `today − 14d … today + 14d` (UTC calendar-day format, matching Tesla), so the
  window is widened **behind the scenes** — the calendar is never touched. This applies
  to **every** planner query (all four tabs). Change `READY_DATE_DAYS` at the top of
  the script to adjust the span.
  - **Ready Date picker hidden (v0.5.0):** because the window is forced in the request,
    the calendar filter is redundant and would only mislead (it can't change the actual
    result set). The script hides the whole field via CSS
    (`tds-form-field.date-picker-width{display:none}`), which is unique to it. The Ready
    Date **column** in the results table is left intact — only the filter control is removed.
- **NA origins first (v0.2.0):** the planner's `GetShipmentPlannerReviewDashboard`
  endpoint returns every shipment in one response and the table pages/sorts
  client-side, so the userscript intercepts that XHR response and stable-sorts
  `data` — origins not starting with `EU` first, EU origins last — before Angular
  renders it. NA shipments therefore fill the first pages instead of being pushed
  behind hidden EU rows. The reorder never drops records and leaves the rest of
  the response untouched; on any parse error the original response passes through.
- Filters only while **Available To Bid** is selected.
- Hides a shipment when either the trimmed origin or destination text starts with `EU`.
- Hides the shipment's paired expansion/detail row as well.
- Reapplies automatically after SPA navigation, tab changes, pagination, searches,
  and Angular table rerenders.
- Makes no API requests and does not bid or modify Tesla data.

## Install

[Install the userscript](https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js)

Tampermonkey will use the same raw GitHub URL for future update checks. Increment the
userscript `@version` whenever publishing a new version.
