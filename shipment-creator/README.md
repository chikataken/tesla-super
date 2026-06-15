# shipment-creator

Reads an Excel sheet of vehicle moves (pickup, delivery, cost, contact, VINs),
groups VINs into shipments, pulls a PDF BOL per shipment from the Tesla portal,
and (later) creates the shipments in SuperDispatch via its API.

Sibling to `../tesla-reconcile` but **completely separate** — its own virtualenv
and dependencies (no Playwright/PyTorch/OCR bloat shared between them).

## Pipeline

```
Excel sheet ──> parse + normalize headers ──> validate ──> group VINs into shipments
                                                                   │
                                                                   ▼
                                          (Tesla) download a PDF BOL per shipment
                                                                   │
                                                                   ▼
                                          (SuperDispatch API) create the shipments   ← deferred
```

## Status

- [x] Excel parsing (flexible headers) + validation
- [x] Grouping VINs into multi-vehicle shipments
- [ ] Tesla BOL PDF download  (needs the live BOL screen mapped first)
- [ ] SuperDispatch API order creation  (deferred — needs API docs)

## Windows captcha fix (CDP attach — default on Windows)

Same fix as tesla-reconcile: on Windows, `auth.py` and `tesla_bol.py` default to
`AUTH_MODE=cdp` and attach over CDP to your REAL installed Chrome at
`http://127.0.0.1:9222` (always `127.0.0.1`, never `localhost`), auto-launching
it on the persistent profile `C:\tesla-profile` (shared with tesla-reconcile, so
one manual login covers both tools) and closing it after. Real fingerprint +
real logged-in cookies = no captcha stall. See `chrome_cdp.py`. Override with
`AUTH_MODE`, `CDP_URL`, `CDP_PROFILE_DIR`, `CHROME_PATH` in `.env`;
`AUTH_MODE=launch` is the original Mac path.

## Setup (separate venv)

```bash
cd shipment-creator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # only needed once the Tesla step is wired
cp .env.example .env                 # fill in tokens when needed
```

## Use

```bash
python main.py --excel /path/to/sheet.xlsx           # parse + preview (writes nothing)
python main.py --excel sheet.xlsx --sheet "Loads"    # pick a worksheet by name
```
