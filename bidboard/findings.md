# Tesla Bid-Board — live-page findings (recon)

Page: `https://suppliers.teslamotors.com/logistics/bidboard2`
Recon done in the logged-in tab via Claude-in-Chrome (read-only).

## Platform
- **Angular SPA**, Tesla "tsl" component library, Angular **CDK overlays** for popovers.
- Classes seen: `group-row`, `grid-cell`, `tsl-input-element`, `tsl-native-input`, `cdk-overlay-container`.

## Board layout
- A **paginated table** (NOT virtualized): footer shows `Items per page: 20` and `1 – 20 of 116`
  (~6 pages). Only the current page's rows are in the DOM.
- **Grouped by route** via section-header rows: `NA-US-NJ-Cherry Hill → NA-US-MA-Dedham … 1 VINs`.
  Group sizes vary (saw 1, 3, 5, 7, 8, 16 VINs).
- Columns: **VIN | Pickup Date | Need By Date | Weight | List Price | Actions**.
- Each VIN row's Actions cell shows the current offer inline (e.g. `499 USD Counter`,
  `Jun 30 2026 16:00:00 Pickup`, `Jul 07 2026 16:00:00 ETA`, `Good Forever`) plus an **edit pencil**.
- **No checkbox / select-all column** is visible → no native multi-select bulk-offer.
  (108 `visually-hidden` native inputs exist in the DOM but are component internals, not a select UI.)

## Offer editor (the thing we must drive)
Clicking the pencil opens a **"Counter Offer for VIN: <vin>"** popover (`cdk-overlay-container`):
- `input[name="proposedPrice"]` — type text, placeholder "Price".
- 2× `input.datepicker-field` — placeholder "Choose date": **Origin Pickup Date** and **Destination ETA**
  (plain text inputs → likely typeable directly; each paired with a custom **time dropdown**, default `16:00`).
- **Currency** — custom dropdown, default `USD - United States Dollar`.
- **Offer Valid for:** — radios `Forever / 6h / 12h` (Tesla custom radio = hidden native `input[type=radio]`).
- Buttons: **Cancel Offer** (red — likely *withdraws* an existing offer, do NOT use to close) and
  **Update** (disabled until the form is valid).
- Close the modal safely with **Escape**, not "Cancel Offer".

## Bid data API
- Endpoints exist (from `performance` resource list):
  - `…/logisticsportalapi/api/v1/BidBoard/groups` (the bid list)
  - `…/BidBoard/origins`, `…/BidBoard/destinations` (filter dropdowns)
- A direct `fetch()` from page JS **fails** ("Failed to fetch" — needs the app's bearer/CORS context).
  The app's own authenticated calls can be captured by hooking `fetch`/XHR **before** they fire
  (a userscript at `document-start` can; our after-the-fact console hook missed the initial load,
  and the Search button filters client-side without re-fetching).
- **Counter-offer WRITE API (captured from one real Update):**
  - `POST  {base}/logisticsportalapi/api/v1/BidBoard/{bidId}/UpdateOffer`
  - `{bidId}` = `bid.bidId` (equals `carrierCounter.legId` for all 220 offered VINs; present on all 24 unpriced too).
  - Body JSON: `{ CurrencyCode:"USD", BidAmount:"<price as string>", EstimatedShipDate:"<ISO …T16:00:00.000Z>",
    NeededByDate:"<ISO …T16:00:00.000Z>", OfferExpiryDate:null }`  (OfferExpiryDate null = "Forever").
  - Auth: same bearer + `x-selectedCarrierId`, `credentials:'omit'`; → 200, response `{ data, success }`.
  - Observed values: pickup = today 16:00Z, ETA = chosen date 16:00Z. So a bid is fully built from
    (price box) + (date-selector value) + today.
  - **Open:** captured on an already-offered VIN. Never-offered VINs all have bidIds, so UpdateOffer likely
    upserts — but the "Make Offer" button may hit a different endpoint; capture one to confirm before bidding new VINs.

## Implications for the tool
- "Multiply across a route" = drive the per-VIN modal (or POST the offer) for every VIN in the group.
- **Pagination wrinkle:** a route's VINs can span pages → either raise items-per-page, iterate pages,
  or go API (POST per VIN) to avoid the DOM entirely.
- Angular controlled inputs: use native setter + dispatched `input`/`change` (same as the React note).
