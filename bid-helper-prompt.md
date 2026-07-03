# Tesla Bid-Board Helper — Claude Code kickoff prompt

Paste everything below the line into a fresh Claude Code session (ideally in a new
sub-directory, e.g. `tesla-super/bid-helper/`) to start building. It captures the goal,
the architecture decisions already made, the known gotchas, and a safe first step.

---

## What I'm building
A helper for my daily bidding on the Tesla logistics/carrier portal. The bid page is a list
of VINs; for each VIN I enter a **price** (text box) and a **date** (dropdown menu). Many VINs
are going on the **same route**, but today I have to fill them in one-by-one — tedious.

I want a tool that shows all the VINs I have to bid on, **grouped by route**, lets me enter the
price + date **once per route**, and then **propagates ("multiplies") that entry to every VIN on
that route** in the actual portal form. I review and hit Submit myself.

## Architecture decisions already made — do NOT re-litigate these
- **Build it as a browser extension / userscript that OVERLAYS the open portal** (a panel on top
  of the live page). NOT a reverse-proxy "better portal" site.
- **It must run inside my already-logged-in browser tab** — so there is *no separate login* and
  *no captcha* (it's my real session, my IP, human-paced). This is the #1 constraint. The reason
  my other tools (Playwright/CDP) sometimes hit login/captcha is that they drive a separate,
  more-detectable browser context; an in-page content script does not.
- **Cross-browser: Firefox AND Chrome.** Write once with the `browser.*` API + Mozilla's
  `webextension-polyfill`. (Note: Chrome loads unpacked extensions freely; Firefox needs signing
  via AMO "unlisted" for permanent install, or temporary load via `about:debugging`. The
  userscript path, Tampermonkey/Violentmonkey, needs no signing on either browser.)
- **Human stays on the final Submit.** The tool fills fields and shows a "filled N/M" confirmation;
  it does not bulk-submit bids.

## How it should work (target UX)
1. A content script sits quietly on the portal and **auto-detects the bid page by its DOM
   signature** (via a `MutationObserver`, NOT just the URL — the portal is a SPA, so navigating to
   the bid view often doesn't change the URL or reload). When the bid grid appears → mount a
   floating **panel**; when I leave → unmount it.
2. The panel lists **route groups**: e.g. `Houston → Dallas (6 VINs) [price ___] [date ▾] [Apply]`.
3. On **Apply**, the script fills the price box + date menu for every VIN row in that route group.
4. I review and Submit through the portal's own flow.

## Known hard parts — plan for these from the start
- **Virtualized list (likely):** the bid grid probably uses a windowed/virtualized scroller — only
  the ~visible rows exist in the DOM, with a spacer faking the scrollbar height. If so, a single
  `querySelectorAll` will NOT return all VINs. **Confirm this first** (see first step). If
  virtualized, **read the VINs from the network layer, not the DOM**: hook `fetch`/`XMLHttpRequest`
  to capture the bid-list API response (clean JSON, all VINs, no scrolling). For *filling*, rows
  still must be scrolled into view to materialize their inputs → scroll-to-row → fill → next.
- **Filling React/controlled inputs:** `input.value = x` won't register with the SPA. Use the
  native setter + a real event:
  ```js
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(priceInput, '1200');
  priceInput.dispatchEvent(new Event('input', { bubbles: true }));
  ```
- **The date "menu":** if it's a native `<select>`, set value + dispatch `change`; if it's a custom
  dropdown (divs), script the open→click-option sequence. Reverse-engineer this control once.
- **DOM fragility:** the SPA re-renders and lazy-loads. Use a `MutationObserver`, resilient
  selectors, and **fail loud** (highlight rows you couldn't fill) rather than silently skipping.

## Existing assets to reuse
- I already run **shipment-creator** locally (`http://127.0.0.1:8000`, public `shipments.wastake.com`)
  which has authoritative **route groupings per VIN**. Prefer pulling route groups from there (join
  by VIN) over parsing routes off the portal DOM — more reliable. (A read-only endpoint can be added
  if needed.)

## Build path
1. **Prototype as a Tampermonkey userscript** — fastest iteration, no packaging/signing, works on
   Firefox + Chrome.
2. Once the DOM-read + React-safe fill + date control all work on the real page, **graduate to an
   MV3 extension** (cross-browser via webextension-polyfill) if I want a persistent panel, stored
   default prices, or clean cross-origin fetches to shipment-creator.

## YOUR FIRST TASK (start here — do not build the whole thing yet)
Write me **one console snippet** I can paste on the live bid page (DevTools console) that:
1. Counts the bid rows currently in the DOM and prints it next to the total VIN count the page
   shows, so we know whether the list is **virtualized** (DOM count ≪ total) or fully rendered.
2. Installs lightweight hooks on `window.fetch` and `XMLHttpRequest` that log any response whose
   body looks like the bid list (contains VINs / route fields), so we can capture the **bid API
   endpoint + response shape**.

I'll paste the output back here. Based on whether it's virtualized and what the API returns, then
propose the concrete first prototype (userscript skeleton: bid-page detection via MutationObserver,
mount/unmount panel, route-grouping from the API/shipment-creator, and a React-safe price fill —
leaving the date-control selector as a TODO until I paste its DOM).

Keep each step small and verifiable on the real page; don't scaffold a big extension up front.
