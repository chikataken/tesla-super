# wells-check — Wells Fargo check ↔ SuperDispatch reconciliation

Goal: every check written from Wells Fargo in a month must match SuperDispatch
paid orders — by the check number (SD's payment *reference number*) and by the
amount. Checks usually pay a BUNDLE of shipments at once, so a check's expected
amount is the SUM of carrier totals across every SD order sharing that
reference number.

## Phases

1. **Scrape + enrich** (`./run.sh`) — pages through the SD *Paid* tab (newest
   first, moving backwards in time) recording each card's order guid, order id
   and preview VIN — no order is ever opened — and, right behind each page,
   fetches those orders from the SD public API to record the carrier total
   (`price`), the payment **reference number** (the check #), method, sent date
   and all VINs. API fetch is `get_order(guid)` (the card link's guid IS the API
   guid); the preview-VIN search is the fallback for guids the API refuses.
   Resumable: the scan position commits after every page (Ctrl+C freely), and
   any pending API rows drain at the end of the next run. The enrichment runs as
   a subprocess because its shipment-creator modules can't share a process with
   the tesla-reconcile scraping modules (both define `config`).
   `--restart` rescans from page 1 (rows are keyed on guid, so harmless).
   `--topup` catches NEWLY-paid orders: scan-only from page 1, stops after a few
   pages with nothing new, leaves the backfill cursor alone — follow with
   `./run.sh enrich`.
2. **Match** (next step, not built yet) — ingest the WF statement's check list
   and compare: check # ↔ reference #, check amount ↔ SUM(price) per reference.

## Loss-safety notes

* Ctrl+C anywhere is safe: rows commit BEFORE the page cursor advances, enriched
  rows commit one-by-one, and pending rows drain on the next run.
* New paid orders only push the list DOWN — resuming re-walks shifted pages
  (deduped) and can't skip past unseen ones. The one theoretical skip is an
  order LEAVING the Paid list mid-scan (everything below shifts UP one slot,
  so one card can slide across a page boundary already scanned). Rare — paid is
  terminal — but after the backfill completes, one `--restart` sweep (cheap:
  all dedupe) verifies nothing slipped through.
* Enrichment only marks a row failed on a DEFINITIVE per-order answer
  (deleted/404 + no VIN match). Transient API trouble (429/5xx/network) leaves
  rows pending for the next run, and an auth failure aborts the run outright
  instead of blanket-failing rows.

## Viewing

The DB is surfaced on **test.wastake.com → Checks** tab (shipment-creator-test
serves `/api/wells-checks` straight from `data/wells.db`, read-only): scan
progress, per-order rows, and the per-check rollup (reference # → shipment
count + total amount).

## Shared plumbing

* Browser: tesla-reconcile's `auth.browser_context()` — the one logged-in Chrome
  on :9222 (`tesla-reconcile/.auth`), plus its SD auto-login. Imported from that
  project (sys.path), not copied.
* API: shipment-creator's `sd_api` client (same OAuth credentials).
* DB: `data/wells.db` (sqlite, WAL). `python db.py` prints stats.
