# Tesla Regular Fleet — SuperDispatch Recolor

A Tampermonkey userscript that shades rows on the Tesla **Regular Fleet** tab
(`…/logistics/invoicing/regular-fleet`) by their SuperDispatch delivery status — so you don't have
to check each VIN by hand.

- **Green** — the VIN has an SD order that was **delivered within 3 days** of the Tesla delivery date
- **Yellow** — not green, but the VIN has an SD order that is `picked_up`, `accepted`, or `pending`
- **Red** — any other case, **or no SD order for the VIN**

## Install
1. Tampermonkey ▸ Create a new script, or open the raw file
   `regular-fleet/tesla-regular-fleet-recolor.user.js` and install.
   (Auto-update is wired to the GitHub raw URL; bump `@version` to push updates.)
2. Go to the Regular Fleet tab. The first time, it prompts for your **SuperDispatch API Client ID
   and Client Secret** (or use the Tampermonkey menu ▸ *Set SuperDispatch credentials*).
   These are stored **locally in Tampermonkey only** — never in the script, never on GitHub.

## How it works
- Hooks the page's own data request (XHR/fetch) at `document-start` to read **every VIN + Tesla
  shipment number** — not just the ~10 rows visible on the current page.
- For each VIN: `GET /v1/public/orders/find_by_vin/{vin}` on SuperDispatch, then applies the
  **match rule** and **color rule** below.
- Repaints rows via a `MutationObserver` **plus a staggered re-apply** on navigation/data-capture, so
  colors appear the instant the table renders (including SPA navigation, not just full refresh) and
  survive pagination and Angular re-renders.
- **Hover a VIN** to pop up its SuperDispatch order card — order #, status pill, pickup→delivery
  route, the hovered VIN's vehicle ("+N more" for others on the order), and the **per-unit carrier
  cost** — to the top-right of the VIN. Only the one relevant order is shown
  (delivery-date match, else in-transit / first). Over-long venue names are capped. Cached from the
  scan, so it's instant (no API call on hover).

### Per-unit carrier cost
The bottom-right value is the SuperDispatch order `price` divided by the number of VINs on that
order and rounded to the nearest whole dollar. For example, a $1,250 four-VIN order displays `$313`;
a one-VIN order displays `$1250`. The card shows only the number, with no label.

### Match rule (by delivery date)
`find_by_vin` returns every SD order the VIN has been on. For each, the script reads the actual
delivery date (`order.delivery.completed_at`). If **any** of them falls within **3 calendar days**
of the Tesla row's delivery date, the VIN is confirmed delivered → **green**. The VIN is always the
join key (find_by_vin queries that exact VIN). No delivery-date match, but an order that is
`picked_up` / `accepted` / `pending` → **yellow**; otherwise → **red**.

### Cache (per day)
Results are cached in GM storage keyed by calendar day. **Green (delivered) is terminal** — served
from cache and never re-queried. **Yellow and red are re-checked on every pass** — each page refresh,
portal Apply/filter, or pagination that reloads the fleet data — because their status can still
change. New VINs are always checked. The cache resets at the next day. Menu ▸ *Re-scan now* clears
today's cache and re-checks everything. Clicking the portal's **Apply/Search** button also forces a
re-check (belt-and-suspenders, on top of the automatic data-reload capture).

## Menu commands
- **Set SuperDispatch credentials** — enter / rotate Client ID + Secret
- **Re-scan now (clear today's cache)** — force a fresh check
- **Clear stored credentials** — wipe the local creds + cached token

## Assumptions to confirm (v0.18.0)
These were inferred from the sibling tools + recon; tell me if any are off and I'll adjust:
1. **Auth** = SuperDispatch OAuth client-credentials (Client ID + Secret). If you instead have a
   single pre-issued bearer token, say so and I'll switch the storage/auth.
2. **SD delivery date** = `order.delivery.completed_at` (the actual delivered timestamp, as used by
   shipment-creator). `DELIVERY_WINDOW_DAYS` at the top of the script sets the ±3-day tolerance.
3. **Yellow** = `picked_up`, `accepted`, or `pending`; edit the `YELLOW_STATUSES` set to adjust.
4. **Tesla record fields** on the fleet response are `vin` + `deliveryDate` (+ `shipmentNumber`);
   the VIN DOM cell is `.cdk-column-FullVin`.
