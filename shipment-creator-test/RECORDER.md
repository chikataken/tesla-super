# Shipment Recorder (local Super Dispatch mirror)

A local SQLite mirror of the Super Dispatch shipment database, plus a **Recorded**
tab on the test web app to browse it. This is the first slice of the larger
"record every shipment event" plan — for now it does a **backfill** (scrape the
last N months of orders) and displays them; the live webhook feed comes later.

## Why scrape (not the API)

Super Dispatch's public API has **no list/search-orders endpoint** (only
get-order-by-GUID, find-by-VIN, create, patch — see `config.py`). A live probe of
`GET /v1/public/orders` returns HTTP 400 "missing required parameter" for every
parameter shape tried, matching the codebase's existing note. So existing shipments
are enumerated the same way `sd_scrape.py` already does it: by reading the Shipper
TMS web order-status tabs (reusing the shared logged-in Chrome over CDP).

## Lifecycle / tabs

Each order sits on exactly one tab at a time:

    new → posted → requests/pending → accepted → picked_up → delivered → invoiced → paid

- **Active tabs** (small, current) are scanned fully:
  `new, on_hold, posted_to_lb, requests, pending, accepted, picked_up`.
- **History tabs** (huge) are restricted to a delivered-on-date window of the last
  N months: `delivered, invoiced, paid`.
- Cross-cutting tabs (`flagged, archived, inactive/deleted, declined`) are skipped —
  they aren't lifecycle states and would clobber an order's real status.

## Files

| File | Role |
|------|------|
| `recorder_db.py`      | SQLite (`data/recorder.db`, WAL): `orders` snapshot, `vehicles`, append-only `events`, `meta`. Upsert + query + counts. |
| `recorder_scrape.py`  | `parse_card()` (pure) turns a loadboard row into a normalized order; `scan_tab()` paginates one tab (optionally date-windowed). Reuses `sd_scrape._CARDS_JS`. |
| `recorder_backfill.py`| Orchestrator: scan active tabs fully + history tabs windowed; upsert orders + log a sighting event each. |
| `recorder_probe.py`   | Read-only discovery: confirm SD login, list the live tab routes, sanity-check card parsing. |
| `app.py`              | `GET /api/recorded` (list + filters + status counts + last-backfill meta). |
| `static/index.html`   | **Recorded** nav tab: status-count chips, search, a table (Order / Status / Route / VINs / Dates). |

## Run

```bash
# one-time: make sure the shared Chrome profile is logged into Super Dispatch
python sd_login.py

# backfill the last 2 months (default)
python recorder_backfill.py
python recorder_backfill.py --months 3 --max-pages 80
HEADLESS=true python recorder_backfill.py        # headless server

# inspect the DB
python recorder_db.py
```

The **Recorded** tab reads `data/recorder.db` live. The new `/api/recorded` route
requires the web service to be (re)started:

```bash
sudo systemctl restart shipment-creator-test-web
```

## Data quality notes

- Reliable per row: order number, web UUID, status, VIN(s), pickup/delivery
  **state + ZIP**, and the pickup/delivery dates.
- The loadboard row text has no delimiter between a stop's *terminal name* and the
  *next stop's city*, so `delivery_city` / `*_terminal` can include stray terminal
  words when the terminal is single-space-joined. The raw row is always kept in
  `orders.card_text`, and state+ZIP are authoritative.
- `api_guid` / `details` are null until a future enrichment step calls
  `sd_api.get_order` (the web UUID in the row link ≠ the API GUID).

## Not done yet (next steps from the plan)

- Live webhook feed (forward-only) to keep the mirror current without re-scraping.
- API enrichment (`get_order`) to fill `api_guid` + full order JSON.
- Promotion to its own standalone project with its own tunnel/systemd (this slice
  lives inside `shipment-creator-test` so it shows on the existing test page).
