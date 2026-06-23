# direct-pickup-checks

A **reactive, always-on service** that listens for Super Dispatch pickup webhooks
and reacts the moment a shipment is marked picked up — unlike the sibling tools
(`tesla-reconcile`, `shipment-creator`), which are pull/cron scrapers. It is a
**self-contained project** with its own virtualenv, dependencies, `.env`, and
systemd units. It does **not** share a process with the other two and does not
import from their folders (it only reuses the shared credentials in
`../secrets/.env` and matches their conventions).

## What it does (two triggers, two different times)

1. **Shipment picked up** — on `order.picked_up` / `order.manually_marked_as_picked_up`:
   fetch order details, record/upsert the shipment (+ VINs), push a live UI update.
2. **Photos available → VIN check** — on `order.picked_up_bol` (fires *later*, once
   the pickup BOL/photos exist): open the order in the Super Dispatch **web app**,
   download the **pickup** inspection photos, read each shipment VIN off them with
   **easyOCR**, and apply tags to the order:
   - every VIN in the shipment found in the photos → tags **`VIN`** + **`CLAUDE`**
   - any VIN missing / unreadable / not matched → tags **`NO VIN`** + **`CLAUDE`**

> ⚠️ **The timing rule that's easy to get wrong:** the order's status flips to
> picked up **before** the driver's photos finish uploading. So the photo job is
> triggered by the *separate* `order.picked_up_bol` event — **never** by the status
> event, where the images usually don't exist yet.

> **Why the web app for step 2?** The public Shipper API can't *write* order tags
> (your `tesla-reconcile` tool scrapes the web UI for exactly this reason). So — for
> now — the worker reuses tesla-reconcile's Playwright sequence (open order →
> download pickup photos → edit tags), copied into this project. The status trigger
> and the order lookup still go through the API.

## Architecture

```
Super Dispatch ──HTTPS POST──▶ Cloudflare Tunnel ──▶ 127.0.0.1:8077 (Listener, FastAPI)
                                                            │ validate token, dedup,
                                                            │ enqueue, 200 fast
                                                            ▼
                                                   SQLite queue (durable)
                                                            │
                                                            ▼
                                                      Worker (separate process)
                              ─ status event → API get-order → upsert + UI push
                              ─ bol event    → API get-order (number+VINs)
                                               → Playwright: open order, download
                                                 pickup photos → easyOCR VIN check
                                                 → edit tags (VIN/NO VIN + CLAUDE)
```

Three long-running services, all under **systemd** (start on boot, restart on crash,
log to journald):

| Service | Unit | Role |
|---------|------|------|
| Tunnel | `cloudflared-direct-pickup.service` (or the packaged `cloudflared.service`) | Public HTTPS → loopback. Only the webhook path is exposed; no port-forwarding (server is behind residential NAT). |
| Listener | `direct-pickup-listener.service` | FastAPI/uvicorn on `127.0.0.1`. Validates, dedups, enqueues, acks fast. |
| Worker | `direct-pickup-worker.service` | Pulls the queue; enriches via API; drives Playwright to download pickup photos, OCR VINs, and tag. Needs a browser (headless or xvfb). |

**Queue choice:** a **SQLite** file (WAL mode) holds the queue, the dedup table, and
the shipment/photo/tag state. The sibling projects use no external services (they
persist to JSON/CSV), so this avoids standing up Redis/RQ. The listener and worker
are separate processes sharing the one DB file.

## Folder layout

```
direct-pickup-checks/
├── config.py            # env-driven settings (reads ../secrets/.env then ./.env)
├── logging_setup.py     # structured JSON logging to stdout (→ journald)
├── db.py                # SQLite: queue, seen_events, shipments, vins, photos, tags, ui_events
├── sd_client.py         # SD OAuth + get-order + find_by_vin + webhook subscribe/list/unsubscribe
├── listener.py          # FastAPI app: validate token, dedup, enqueue, ack; SSE UI feed
├── worker.py            # queue consumer: two-tier routing; API enrich + Playwright tag flow
├── browser.py           # Playwright CDP/launch context (copied from tesla-reconcile/auth.py)
├── sd_web.py            # SD web ops: find order, download PICKUP photos, edit tags (copied/adapted)
├── ocr.py               # easyOCR VIN reader (copied from tesla-reconcile/ocr.py)
├── tagging.py           # decide_order_tags(): per-shipment VIN/NO VIN decision via ocr.py
├── subscribe.py         # one-shot: actions / list / subscribe / unsubscribe
├── verify_api.py        # READ-ONLY probe: auth, webhook actions, order fields, photo shape
├── run_login.py         # one-time SD web login (saves the shared browser session)
├── run.sh               # main entrypoint: ./run.sh [login|worker|listener|subscribe|verify]
├── requirements.txt
├── .env.example
├── systemd/             # the three unit files
└── tests/               # listener, worker routing, tagging — all external layers mocked
```

## Setup (its own venv)

```bash
cd direct-pickup-checks
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # NOTE: easyocr pulls torch (~1GB download)
playwright install chromium          # browser for the web-app photo/tag steps
cp .env.example .env                 # fill in the webhook-specific values
python config.py                     # prints resolved config (secrets masked) — sanity check
python -m pytest -q                  # 21 tests; API + browser + OCR all mocked
python run_login.py                  # one-time: log into Super Dispatch (shared profile)
```

The browser profile is **shared** with `tesla-reconcile` / `shipment-creator`
(same `CDP_PROFILE_DIR`), so if you've already logged in there, `run_login.py` is a
no-op. On a **headless Linux server**, either set `AUTH_MODE=launch` + `HEADLESS=true`,
or run under `xvfb` (see the worker unit). Tests need neither a browser nor torch.

> **No Claude / external vision API.** VIN reading is 100% on-device (easyOCR /
> Tesseract in `ocr.py`); inspection photos are never sent to Claude or anywhere
> else. `CLAUDE` is only the literal tag label. (`anthropic` isn't a dependency.)

## Run

`./run.sh` is the main entrypoint — it bootstraps the venv + Chromium on first run,
then runs the service. **Browser steps default to headed (visible).**

```bash
./run.sh                 # the service: worker + listener together (headed); Ctrl+C stops both
./run.sh login           # one-time Super Dispatch login (headed browser)
./run.sh worker          # just the worker (queue consumer + Playwright photo/tag flow)
./run.sh listener        # just the webhook listener (uvicorn, from config host/port)
./run.sh subscribe list  # manage webhook subscriptions (actions/list/subscribe/...)
./run.sh verify --order <GUID>   # read-only API probe
HEADLESS=true ./run.sh worker    # override to headless (e.g. on a server)
```

In production the listener and worker run as separate **systemd** services (below);
`./run.sh` is the convenient way to run them together locally.

Credentials resolve like the siblings: real env vars > this folder's `.env` >
`../secrets/.env`. Your existing `SUPERDISPATCH_CLIENT_ID` / `_SECRET` in
`../secrets/.env` are picked up automatically — you only need to set the
webhook-specific keys below.

## Environment variables

| Key | Required | Purpose |
|-----|----------|---------|
| `SUPERDISPATCH_CLIENT_ID` / `_SECRET` | yes | OAuth client-credentials (inherited from `../secrets/.env`). |
| `SD_ENV` | no | `test` (default) or `production`. |
| `SD_API_BASE` | no | Override API base url. |
| `SD_WEBHOOK_VERIFICATION_TOKEN` | **yes** | Token validated on every inbound webhook (constant-time). Same value you register when subscribing. |
| `TUNNEL_PUBLIC_URL` | yes (to subscribe) | Cloudflare Tunnel origin (default `https://test.wastake.com`). Callback URL = this + `WEBHOOK_PATH`. |
| `TUNNEL_NAME` / `TUNNEL_HOSTNAME` | no | Named tunnel `run.sh` creates/routes (default `direct-pickup` / `test.wastake.com`). |
| `LISTENER_HOST` / `LISTENER_PORT` | no | Default `127.0.0.1:8077`. |
| `WEBHOOK_PATH` | no | Default `/webhooks/superdispatch`. |
| `DPC_DATA_DIR` | no | Where the SQLite DB + photos live (default `./data`). |
| `WORKER_POLL_SECONDS` / `WORKER_MAX_ATTEMPTS` | no | Worker tuning (default `2.0` / `5`). |
| `LOG_LEVEL` / `LOG_FORMAT` | no | `INFO` / `json` (use `text` for a readable dev console). |
| `SD_WEB_BASE` | no | SD web app base (default `https://shipper.superdispatch.com`). |
| `AUTH_MODE` | no | `launch` (Mac/Linux default) or `cdp` (attach to real Chrome). |
| `HEADLESS` / `WINDOW_MODE` | no | Browser visibility. Headless server → `HEADLESS=true`. |
| `CDP_PROFILE_DIR` / `USER_DATA_DIR` | no | Browser profile dirs (CDP profile shared with siblings). |
| `OCR_ENGINE` / `OCR_ROTATIONS` | no | `easyocr` (default) or `tesseract`; rotate-retry angles. |
| `TAG_VIN` / `TAG_NO_VIN` / `TAG_BOT` | no | Tag labels (default `VIN` / `NO VIN` / `CLAUDE`). |

Tunnel credentials are **never stored in this repo** — they live in `~/.cloudflared`
(created by `cloudflared tunnel login`/`create`, which `run.sh` runs for you).

## Cloudflare Tunnel — automated by `run.sh`

The tunnel is the only thing exposing this box. `run.sh` provisions and runs a
**named tunnel** (`direct-pickup`) routing **`test.wastake.com` → `127.0.0.1:8077`**.
It is idempotent — every step is skipped once done:

```bash
./run.sh tunnel     # provision + run only the tunnel
./run.sh            # provision tunnel, then run tunnel + worker + listener together
```

On the **first** run it will:
1. **install `cloudflared`** if missing (Homebrew on macOS, `.deb` on apt systems);
2. **`cloudflared tunnel login`** — a one-time browser step where *you* authorize
   your Cloudflare account and pick the **wastake.com** zone (only you can do this);
3. **create** the `direct-pickup` tunnel;
4. **route DNS** `test.wastake.com` → the tunnel (creates the CNAME on the zone);
5. **write** `.cloudflared/config.yml` (gitignored) with the ingress.

Prerequisite: **wastake.com must be on Cloudflare** (its nameservers point to
Cloudflare) so step 4 can create the DNS record. Change the host by editing
`TUNNEL_HOSTNAME` / `TUNNEL_PUBLIC_URL` in `.env`.

For the server, `systemd/cloudflared-direct-pickup.service` just *runs* the tunnel
that `./run.sh tunnel` already provisioned (point its `--config` at the generated
`.cloudflared/config.yml`).

## Installing the systemd units

```bash
# put the project somewhere stable, e.g. /opt/direct-pickup-checks, with its .venv built there
sudo cp systemd/direct-pickup-listener.service /etc/systemd/system/
sudo cp systemd/direct-pickup-worker.service   /etc/systemd/system/
# (only if using the named-tunnel config approach)
sudo cp systemd/cloudflared-direct-pickup.service /etc/systemd/system/

# EDIT each unit: set User/Group, WorkingDirectory, and the ExecStart venv paths.
sudo systemctl daemon-reload
sudo systemctl enable --now direct-pickup-listener direct-pickup-worker

# logs (structured JSON):
journalctl -u direct-pickup-listener -f
journalctl -u direct-pickup-worker -f
```

## Registering the webhook subscription

After the tunnel + listener are up and reachable over HTTPS:

```bash
source .venv/bin/activate
python subscribe.py actions       # print the LIVE action list — verify the names first
python subscribe.py subscribe     # register the callback URL for our 3 actions
python subscribe.py list          # confirm
python subscribe.py unsubscribe <subscription_guid>
```

> **Webhooks are forward-only.** Subscribing does *not* replay pickups that
> happened before the subscription existed — you only receive events from then on.

## The VIN check + tagging step

The decision lives in `tagging.decide_order_tags(groups, expected_vins)`:
- `expected_vins` come from the API order (authoritative).
- `groups` are the downloaded pickup photos grouped per vehicle.
- For each VIN it calls `ocr.scan_for_vin(images, vin)` (easyOCR with the smart
  rotation fallback, copied from `tesla-reconcile/ocr.py`). Non-empty → found.
- **All** VINs found → `["VIN", "CLAUDE"]`; otherwise → `["NO VIN", "CLAUDE"]`.

`worker.handle_bol_event` then drives the web app (`sd_web.py`) to apply those tags
on the order's edit page (`add_tags` — clear-then-set, SD caps an order at 3 tags).
The result is recorded in the `tags` table (one row per order, idempotent) and
pushed to the UI feed.

Per the isolation rule, `browser.py`, `sd_web.py`, and `ocr.py` are **copies** of
the tesla-reconcile logic, not imports. The one behavioral change in `sd_web.py` is
collecting the **Pickup** Inspection photos (the sibling collected Delivery).

## UI push

The worker appends rows to the `ui_events` table; the listener streams them over
**Server-Sent Events** at `GET /events/stream` (cross-process, durable). A UI
subscribes with `EventSource("https://…/events/stream")` and receives `picked_up`
and `photos_tagged` events. (This mirrors `shipment-creator/app.py`'s SSE style.)

## Tests

```bash
python -m pytest -q
```

Covers: the listener (validates token, dedups, acks fast, enqueues, makes no API
calls), worker routing (status event uses the API and never opens a browser; BOL
event runs the Playwright flow), the VIN-check decision (`VIN` vs `NO VIN` via a
mocked `ocr.scan_for_vin`), tag application, and idempotency (a redelivered BOL
event doesn't re-tag). Every external layer — the SD API, the browser, the SD web
ops, and OCR — is **mocked**: no live calls, no real browser, no torch.

---

## ⚠️ Assumptions & things to VERIFY against the live API reference

I followed your webhook facts exactly. The items below are inferences I could not
confirm without the live reference / a sandbox event — each is isolated in code so
fixing it is a one-line change. **Verify these before production:**

1. **Webhook action names** (`config.py` → `SUBSCRIBE_ACTIONS`). Used exactly as you
   specified, but run `python subscribe.py actions` to confirm they appear in the
   authoritative live list (the brief says don't assume the set is static).
2. **Webhook payload field names** (`listener.py` → `extract_fields`). Confirmed from
   your brief: `verification_token`, event `guid`, `occurred_at`, `order_guid`. The
   **action-name field** wasn't specified — I try `action`/`event`/`event_type`/`type`.
   Verify which one SD actually sends.
3. **Webhook management endpoints** (`sd_client.py` → `PATH_WEBHOOK_*`). The paths for
   *list actions* / *subscriptions CRUD* are inferred (`/v1/public/webhooks/...`) and
   the **subscribe request body shape** (one subscription with an `actions` array vs.
   one per action) is assumed. Verify both.
4. **get-order field names** (`worker._order_number/_order_status/_picked_up_at/_vins`).
   `number`/`status`/`vehicles[].vin` follow the sibling client; the picked-up
   timestamp field is a guess. The order **number** is critical — it's how the web
   flow locates the order (the API guid ≠ the web view uuid). Verify.
5. **The `data.object` / `data.objects` response envelope** (`sd_client._object/_objects`)
   is carried over from the sibling client; confirm it holds for these endpoints.

**Web-app flow (`sd_web.py`, copied from tesla-reconcile — verified there for the
Delivery flow; confirm these for Pickup):**

6. **The "Pickup Inspection" section heading** (`_COLLECT_ALL_PICKUP_JS`). The sibling
   matched `"Delivery Inspection"`; I switched it to `"Pickup Inspection"`. Confirm
   that's the exact heading text on the online BOL for pickup photos.
7. **"View Online BOL" order-actions menu item**, the **photo lightbox** paging, and
   the **Tags autocomplete** selectors (`add_tags`) are reused as-is from the sibling.
   They're stable there, but confirm against a live pickup BOL + edit page once.
8. **Order lookup by number** (`find_order_detail_url`) uses the orders search box +
   "All time" window. Confirm the searched **number** matches what's shown in the list.

**API photo endpoints are no longer in the live path.** `sd_client.get_inspection_photos`
/ `download_bytes` / `normalize_photo` remain only for `verify_api.py` and a possible
future all-API switch; the production flow downloads photos via the web app.
