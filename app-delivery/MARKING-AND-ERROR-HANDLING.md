# App-Delivery — Marking, APP-tagging, Validation & Error Handling

A reference for how the drop-off/pickup automation records what it does, how the App-tab
dashboard tags shipments **APP**, how the SD-order link is validated, and how the emulator
handles errors. Written so a future change is safe. Data counts are as of **2026-07-02** and
are illustrative (they grow over time), not fixed.

---

## 1. The three systems (and why identity is hard)

| System | What it is | Key id | Notes |
|---|---|---|---|
| **Tesla Logistics driver app** (Android emulator) | Where `app_drive.py` marks pickups/drop-offs | shipment id `SHP2606-A56J733` (last part = order name) | The automation drives the UI; no API. |
| **SuperDispatch (SD)** | The carrier TMS | **order GUID** (unique) + order **number** (`A56J733`, reusable) | Holds the delivery photos + real `delivery.completed_at` (UTC). |
| **SD recorder mirror** — `../shipment-creator/data/recorder.db` | Local webhook-fed copy of SD | `orders.api_guid` | **Fresh-started / partial history** — older orders are absent. |
| **App ledger** — `app-delivery/dropoffs.db` | What the automation actually did | `(order_guid, vin)` | Written by `record_dropoffs()` / `record_pickups()`. |

**Identity rules (important):**
- **VIN** is unique per physical vehicle but **rides multiple shipments over time** (~2.6% of delivered VINs, e.g. delivered, then re-shipped). VIN alone ≠ a shipment.
- **Order number** is **NOT unique** (~76/1347 numbers appear on 2+ distinct orders — all genuine, different GUIDs; even the *same* number can recur, e.g. `A4C2758`×2). The **base** is 7 chars: `[A-Z]\d[A-Z0-9]{5}` (e.g. `A56J733`); the rest is human decoration (`-3`, ` direct`, `vip`, `(duplicate)`, leading `-`).
- **GUID (`api_guid`)** is the **only** truly unique per-shipment key.
- Rule of thumb: **GUID for identity**, or **number/VIN + delivery-time window** when you only have those.

---

## 2. `dropoffs.db` — the automation's ledger (3 tables)

### `dropoffs` (one row per VIN successfully dropped off — `record_dropoffs()`)
`vin`, `shipment` (Tesla `SHP…`), `model`, `order_guid` (SD GUID the photos came from),
`photographed` (0/1), `option` (`Subject to Inspection`), `dropped_at` (ISO, **naive local**),
`exterior`/`vin_found`/`key_found` (0/1 photo-provenance), **`sd_number`** (SD order name the
photos came from), **`sd_delivered_at`** (that order's delivery/photo date — used by the link
check). `UNIQUE(vin, shipment)`, `INSERT OR IGNORE`.

### `pickups` (`record_pickups()`)
`vin`, `shipment`, `model`, `eta`, `picked_at` (ISO, naive local).

### `api_errors` (`_record_api_error()`) — **permanently parked** shipments
`shipment` (PK), `vin`, `stage` (e.g. `pickup_departure`), `detail`, `seen_at`.

> **Timezone gotcha:** `dropped_at`/`picked_at`/`seen_at` are **naive local** (`datetime.now().isoformat()`).
> SD times are **UTC (+0000)**. Normalize before comparing across systems. (Making these tz-aware
> was discussed but **not yet done** — only the validator handles both forms.)

---

## 3. `recorder.db` `orders` — the SD side we read

Columns used: `number`, `api_guid` (the join key), `status`, `delivery_city/state`, `vins`
(JSON array), `details` (full get-order JSON). Everything time-accurate lives in `details`:
- **`delivery.completed_at`** — real delivery timestamp (UTC). Sort/format the delivered list by this.
- **`pickup.completed_at`** — real pickup timestamp (UTC). Together they bound a shipment's lifecycle.
- **`vehicles[].vin` / `.model`** — model per VIN.

"Delivered" = orders with a real `delivery.completed_at`, i.e. **status in (`delivered`,`invoiced`,`paid`)**.
`api_guid` is populated on 100% of those. The top-level `delivery_date` column is only a coarse
card date — do **not** use it for time logic.

---

## 4. How a drop-off gets its SD GUID (and why it can be WRONG)

Chain: `_dropoff_open_detail` → `add_photos_for_unit(vin)` → `decode(vin)` →
`decode_vin.py` shells `fetch_latest.py --vin` → `fetch_latest.latest_delivered(vin)` →
`sd_api.find_by_vin(vin)` returns **every** SD order for that VIN → keeps only those with
Delivery photos → sorts by **(status==delivered, newest photo)** and takes the top → writes
`manifest.json` `shipment.{guid,number,status,date}` → `record_dropoffs()` stores
`order_guid` + `sd_number` + `sd_delivered_at`.

**So the stored GUID is "the order the photos were borrowed from," NOT a verified match to the
shipment being dropped off.** For a **reused VIN**, the current shipment usually has no delivery
photos yet (it's being delivered right now), so `latest_delivered` grabs a **previous** delivery.
Worked example (real): VIN `5YJ3E1EA0MF016891` dropped off Tesla `A50B233`, but the app fetched
order `A3CJ697` (delivered 12 days earlier) → stored `A3CJ697`'s guid/number. That guid isn't
even in the recorder mirror. This is exactly what the link validation (§6) catches.

---

## 5. The App tab dashboard (`dashboard.py`)

Served on `:8011`, proxied into both shipment-creator sites under `/app/`. Prod's iframe adds
`?hide_activity=1` → hides the live-activity section but keeps the rest (see `.noactivity` CSS +
`do_GET`). Polls `/api` every 4s; `/delivered` every ~30s (server-cached 60s).

**Live tracker (`_now`, STEP markers):** `app_drive.step(flow,n,label,…)` writes `STEP …` log
lines at the 5 pickup + 5 drop-off milestones; `_now()` parses the latest into a hero step tracker
(all additive log lines — **no** effect on automation). Commit → `ledger:` line → "✓ committed"
done-state; idle line → resets.

**Delivered tab ("Didi delivered list"):** `_delivered()` — every delivered vehicle from the
recorder, **UNSCOPED** (Didi = the bypass profile = no per-user state filter), newest first,
"When" as `JUL-01 2:30PM`.

**Marks tab:** `_history()` — the app's own pickups/drop-offs/api-errors.

---

## 6. APP tagging + the SD-order link validation

**APP match (committed):** a delivered row is **APP** when the exact **`(order_guid, vin)` pair**
is in `dropoffs` (`_app_marked()` + `_delivered()`). GUID pins the order, VIN pins the vehicle —
no VIN-reuse false positives. (Old VIN-only match over-tagged; the pair fixed it.)

**Link validation (`_validate_link()`, per your spec):** confirm the SD order the app linked is
the right one for the shipment marked:
1. **Order name first** — `order_base(tesla_shipment)` vs `order_base(sd_number)`, a **7-char**
   compare (`order_base()` strips a `SHP…-` prefix + leading junk, takes the first 7 chars of the
   first alnum token; normalizes `-AU49200`, `AU49200 direct`, `A56J733-3`, `A54Q241vip`).
2. **Time last resort** — if names differ, pass only if `sd_delivered_at` is **within 7 calendar
   days of today** (`_within_days`, local date compare — "within 1 week of the current day").
3. **Else → `error`.**

**Where each error surfaces:**
- **Marks tab** is authoritative: it validates **every** drop-off using the **stored** `sd_number`/
  `sd_delivered_at`, so it flags mislinks **even when the linked order isn't in the mirror** (the
  common case). Failing rows show a red **⚠ ERROR** pill + a header count.
- **Delivered tab** can only flag mislinks whose SD order *is* in the recorder (it's built from the
  recorder). A mislink typically shows the *correct* order as plain green "delivered" and the wrong
  order not at all — so delivered-tab errors are usually 0. Use the Marks tab for the real check.

> The validation is a **passive flag**. It does **not** stop, skip, or park anything in the
> automation. (As of 2026-07-02: 2 real link-errors flagged — `A50B233`/`016891`, `A4C2758`/`564385`.)

---

## 7. How the EMULATOR automation handles errors

Three unrelated meanings of "error": (a) the passive validation badge above — no effect;
(b) transient in-app API errors — retried; (c) permanent parking — avoided forever. Only (c)
makes the automation give up.

### Transient — retry, then defer (NEVER permanent)
- **Drop-off** (`drop_off`): on Confirm, an API-error dialog → dismiss OK → retry, up to **4
  attempts** per cycle. All 4 fail → `! drop off kept hitting API errors — leaving it for the next
  cycle` → returns False, **stays In-Transit, retried next cycle**. A wedged screen (`no 'Drop Off'
  button`) → caught → `restart_app()` → retried next cycle. Drop-offs are **never** permanently
  abandoned by the automation.
- **Pickup** (`do_pickup`): "Start Loading not enabled" → skip, try next / retry next cycle.
  Departure that doesn't reach home **without** an API error → `leaving for retry, not parking`.

### Permanent — avoided FOREVER (only two ways)
1. **A real API error at pickup *departure*** — the one automatic give-up. `do_pickup` calls
   `_record_api_error(shp, vin, "pickup_departure", …)` → row in `api_errors` (`INSERT OR IGNORE`)
   → never opened again, no ledger, shows as "API ERROR". (Deliberate: a departure API error means
   the order is broken server-side. Drop-off errors are treated as transient, hence retried.)
2. **`skip_pickups.json`** — a **user-managed** JSON list of `SHP…` numbers to never open (for
   shipments wedged server-side that would loop the queue).

Both merge each cycle: `tried = set(_load_skip_pickups()) | _api_error_shipments()` (`process_cycle`).

### Loop guards
- `process_cycle` adds each opened shipment to `tried` for the rest of that cycle (no double-open).
- `drain_queue` caps at `max_shipments + 4` attempts, then moves on.
- Night mode: polls every 10 min 20:00–07:00 (else 60s) — so a "stuck-looking" idle can just be the
  slow night cadence.

---

## 8. File / function map (where to change things)

**`app_drive.py`** (the automation)
- `serve()` / `process_cycle()` — the 24/7 loop; interleave pickup→dropoff, then `drain_queue()`.
- `do_pickup()` — verify → load → ETA → depart; parks on departure API error.
- `drop_off()` / `_dropoff_open_detail()` — options sheet → Subject to Inspection → Confirm; 4 retries.
- `add_photos_for_unit()` / `decode()` — fetch + push the 3 photo sections.
- `record_dropoffs()` / `record_pickups()` / `_ledger()` — write the ledger + schema/migrations.
- `_record_api_error()` / `_api_error_shipments()` / `_load_skip_pickups()` — permanent parking.
- `goto_in_transit_home()` / `restart_app()` — navigation + wedge recovery.
- `step()` — the STEP markers the dashboard tracker reads.

**`dashboard.py`** (the App tab)
- `snapshot()` / `_now()` — status + live step tracker.
- `_delivered()` / `_app_marked()` — delivered list + APP `(guid,vin)` match.
- `order_base()` / `_within_days()` / `_validate_link()` — the link validation.
- `_history()` — Marks tab (adds the per-drop-off `check`).
- `do_GET` `/delivered`, `?hide_activity=1` handling.

**`decode_vin.py`** / **`fetch_latest.py`** — how the SD order/photos/GUID are chosen
(`latest_delivered` = the heuristic that can mislink reused VINs, §4).

---

## 9. Known gaps / gotchas (candidates for future work)
- **Naive-local timestamps** in `dropoffs` vs UTC in SD — normalize before any cross-system time math.
- **The GUID heuristic mislinks reused VINs** (§4) — the link validation flags it but doesn't fix the
  stored GUID. A write-time fix would re-pick the order whose lifecycle window contains `dropped_at`.
- **Recorder mirror is partial** (fresh-started) — orders it lacks can't appear on the delivered tab,
  so APP under-reports for those (the correct order shows plain "delivered").
- **The validation is passive** — if you want it to change automation behavior (e.g. skip a bad
  photo-borrow), that's a separate change in `add_photos_for_unit`/`record_dropoffs`.
- **`delivery.completed_at` (SD) ≠ `dropped_at` (app mark)** — different systems/events; correlate
  with a window, never equality.
