# Tesla ↔ SuperDispatch delivery reconciliation

Playwright handles all the deterministic browser work (filtering, scraping,
the Tesla payment + claims lookups, applying tags). The Claude vision API is
called **only** for what needs eyes: reading the VIN off the vehicle in the
delivery photos and checking the location stamp. Edge cases are screenshotted
and pushed to a review queue instead of guessed at.

## What's here

| File | Role |
|------|------|
| `config.py` | Settings from `.env` (URLs, rules, tag labels). |
| `selectors.py` | **All** DOM selectors in one place. Calibrate these first. |
| `auth.py` / `run_login.py` | Persistent login profile; log in once, reuse. |
| `superdispatch.py` | URL-driven list nav, scraping, tags, BOL photo URLs. |
| `tesla.py` | Approved payment check + Claims/Filed check (pure DOM). |
| `vision.py` | The only model call — VIN/location judgment, returns JSON. |
| `main.py` | Orchestrator + CSV log + review queue. |

## Setup

```bash
cd tesla-reconcile
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env          # then edit .env
python run_login.py           # log into BOTH portals once
```

## Windows captcha fix (CDP attach — default on Windows)

Launching an automated browser (bundled Chromium, `navigator.webdriver=true`,
cold cookie-less profile) trips Tesla's bot detection on Windows and the captcha
never resolves. So on Windows `auth.py` defaults to `AUTH_MODE=cdp`: it attaches
over CDP to your REAL installed Chrome at `http://127.0.0.1:9222` (always
`127.0.0.1`, never `localhost` — that resolves to IPv6 and is refused),
auto-launching Chrome on the persistent profile `C:\tesla-profile` if none is
running and closing it after. Real fingerprint + real logged-in cookies means
the captcha only appears once, at the manual login (`run_login.py`); the profile
keeps the session afterwards. Override with `AUTH_MODE`, `CDP_URL`,
`CDP_PROFILE_DIR`, `CHROME_PATH` in `.env`. `AUTH_MODE=launch` is the original
Mac path.

## Run

```bash
python main.py --dry-run --max-orders 5   # decide on 5 orders, write nothing
python main.py --max-orders 5             # apply tags on 5 orders
python main.py                            # full window
```

Results: `output/actions.csv` (every decision), `output/review_queue.jsonl`
(orders needing a human), `output/screenshots/` (failure captures).

## ⚠️ Calibrate selectors before the first real run

The selectors in `selectors.py` were derived from the rendered UI, **not** the
live DOM, so anything marked `# VERIFY` must be confirmed once:

```bash
playwright codegen https://shipper.superdispatch.com
playwright codegen https://suppliers.teslamotors.com/logistics/invoicing/regular-fleet
```

Click each element you care about, copy the locator Playwright generates, and
paste it into `selectors.py`. Do this with `--dry-run` until the CSV looks right.
The URL-driven filtering/sort/pagination in `superdispatch.py` is already
confirmed and won't need this.

## The Claude vision call — what you asked to be advised on

**You need an Anthropic API key** (separate from your Claude subscription,
billed pay-as-you-go). Get it at `console.anthropic.com` → API Keys, add a
payment method, paste it into `.env` as `ANTHROPIC_API_KEY`. The script uses it
only in `vision.py`.

**Roughly what it costs.** Images are billed as input tokens by area
(~`(w×h)/750` tokens). A delivery set of ~8 photos at ~1000px is on the order of
10–15k input tokens per order, plus a tiny JSON output. On Sonnet (~$3 / 1M in,
~$15 / 1M out) that's roughly **$0.03–0.05 per order** — call it a few dollars
per 100 shipments. Ways to cut it, in order of impact:

- **Downscale photos** to ~1000px before sending (add Pillow; biggest lever).
- **Send only the delivery photos**, never the pickup set (already the case).
- **Try Haiku** (`VISION_MODEL=claude-haiku-4-5-...`) for a cheaper first pass;
  keep Sonnet as a fallback when confidence is low. Haiku is fine for clear VIN
  plates but weaker on blurry/edge shots — that's exactly what the review queue
  is for.

Because the two Tesla checks are pure DOM, **most orders never hit the API** —
anything with blank payment or a damage claim is decided before a photo is
looked at.

## Decision logic (mirrors the manual workflow)

1. Skip orders tagged OK / Paid / Delivery confirmed.
2. Per VIN: payment must be Paid / Sent for payment near delivery, else **skip &
   log** (blank = your manual-review case). Any filed Destination claim →
   **Damage claim** tag (one issue drives the whole multi-VIN shipment).
3. Otherwise vision on the delivery photos → **Delivery confirmed**, plus
   **No VIN photos** if no on-vehicle VIN shot is found. Location mismatch →
   flagged for review rather than auto-tagged.

## One caution

Automating the Tesla supplier portal may be subject to its terms of use. It's
your own work account, but confirm automated access is permitted on your side
before running at volume.
