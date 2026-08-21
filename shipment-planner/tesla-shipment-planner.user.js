// ==UserScript==
// @name         Tesla Shipment Planner Helper
// @namespace    wastake.shipment-planner
// @version      1.1.0
// @description  Bidboard-style split panel for Tesla's Shipment Planner, SPLICED INTO the page — replaces Tesla's planner board in-place. Left: every route + its shipments (from the API). Right: focused bidding cards with a recommended-ETA picker and one price box per shipment. LIVE: pressing Enter to finish a card PUTs UpsertBid for every typed shipment. REVIEW/CONFIRMED/REJECTED tabs show those boards read-only. EU shipments are hidden everywhere. Every submitted bid is recorded (fire-and-forget) to shipments.wastake.com for the local bid-audit DB.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * This is the bidboard helper (bidboard/tesla-bidboard-helper.user.js) re-targeted at the Shipment
 * Planner, where Tesla is migrating the bid board: the bid unit is now a whole SHIPMENT (a
 * multi-VIN load priced once), not a VIN. See shipment-planner/findings.md for the recon.
 *
 * LIVE BIDDING — typing a price and pressing Enter to leave a card PUTs /TMS/UpsertBid per shipment.
 *   Left half : route list (origin -> destination), each expanded with its shipments / models / my bid.
 *   Right half: one focused card per route — route, recommended-ETA date picker, and ONE PRICE BOX
 *               PER SHIPMENT (labeled VIN count, model mix, shipment number). Shipments differ in
 *               size, so prices are per shipment — nothing is fanned.
 *   Submit    : Enter only sends boxes you've TYPED into; pickup = today + 3 calendar days (rolled to Monday if weekend) 16:00 local,
 *               ETA = pickup + distance-based transit days (stepper offsets in calendar days), USD.
 *               Submissions are serialized per route; the newest Entered snapshot wins.
 *               Card green on success, red on failure.
 *   Views     : BID (Available To Bid, statusId 21) is the bidding surface. REVIEW (20),
 *               CONFIRMED (22), REJECTED (23) are read-only boards, re-fetched on each visit.
 *   EU        : shipments touching an EU location are hidden everywhere (origin/destination name
 *               starts "EU" or a location's country is outside NA).
 *
 * Differences from bidboard forced by the new API (findings.md):
 *   - One dashboard POST returns EVERYTHING (no skip/take paging). Only readyDateFrom/To filter
 *     server-side; we request today ± READY_DATE_DAYS.
 *   - One write verb: PUT /TMS/UpsertBid covers create AND edit (fvShipmentCarrierBidId null vs
 *     current id). bidStatus 0 = place, 5 = cancel (cancel not exposed in this panel).
 *   - EVERY upsert mints a NEW bid id and the response does not return it, so after each card's
 *     batch we silently re-POST the dashboard to refresh ids — a queued edit must never send a
 *     stale id. (This replaces bidboard's rememberSuccessfulOffer local patch.)
 *   - Dates go as local "YYYY-MM-DD HH:mm:ss" strings, not 16:00Z ISO.
 *   - No VINs anywhere: classification is by modelCount (cybertruck / model 3|S|X|Y). The old
 *     CAB/YL VIN-prefix splits are gone until a shipment-creator join exists.
 */

(function () {
  'use strict';
  const LOG = (...a) => console.log('%c[planpanel]', 'color:#06c;font-weight:bold', ...a);

  const READY_DATE_DAYS = 14;   // dashboard window = today ± this many days
  const VIEWS = {
    bid:       { selectedTab: 1, statusId: 21, label: 'BID' },
    review:    { selectedTab: 0, statusId: 20, label: 'REVIEW' },
    confirmed: { selectedTab: 2, statusId: 22, label: 'CONFIRMED' },
    rejected:  { selectedTab: 3, statusId: 23, label: 'REJECTED' },
  };
  const BID_STATUS_LABEL = { 0: 'Bid Placed', 1: 'Accepted', 2: 'Rejected', 3: 'Closed', 5: 'Cancelled' };

  const state = {
    endpoint: null, headers: null, carrierId: null,
    data: { bid: null, review: null, confirmed: null, rejected: null },   // raw shipment lists (EU-filtered)
    groups: [],            // bid view grouped by route
    view: 'bid',
    loading: false, error: null,
    filter: '', prices: {}, dates: {}, todoOnly: true,   // TO-DO is the default view
    embedded: false,
  };

  // ---- 1) Capture the dashboard POST (endpoint + auth) by hooking XHR -------
  const DASH_API = /\/TMS\/GetShipmentPlannerReviewDashboard/i;
  const X = window.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send, oSetH = X.prototype.setRequestHeader;
  X.prototype.open = function (m, u) { this.__pp = { method: String(m || 'GET').toUpperCase(), url: u, headers: {} }; return oOpen.apply(this, arguments); };
  X.prototype.setRequestHeader = function (k, v) { if (this.__pp) this.__pp.headers[k] = v; return oSetH.apply(this, arguments); };
  X.prototype.send = function (b) {
    const xhr = this;
    if (xhr.__pp && xhr.__pp.method === 'POST' && DASH_API.test(String(xhr.__pp.url))) {
      xhr.addEventListener('load', function () {
        try {
          const fresh = !state.endpoint;
          state.endpoint = xhr.__pp.url; state.headers = xhr.__pp.headers;
          const cid = Object.keys(xhr.__pp.headers).find((k) => k.toLowerCase() === 'x-selectedcarrierid');
          if (cid) state.carrierId = String(xhr.__pp.headers[cid]);
          if (fresh) { LOG('captured endpoint; loading'); ensurePanel(); loadView('bid'); }
        } catch (_) {}
      });
    }
    return oSend.apply(this, arguments);
  };

  // ---- 2) Replay the POST with our own (wide) window -------------------------
  const HEADER_DENY = new Set(['cookie', 'content-length', 'host', 'connection', 'accept-encoding', 'user-agent']);
  function replayHeaders() { const out = { 'Content-Type': 'application/json', 'Accept': 'application/json' }; if (state.headers) for (const k of Object.keys(state.headers)) if (!HEADER_DENY.has(k.toLowerCase())) out[k] = state.headers[k]; return out; }
  function readyDateWindow() {
    const now = new Date(); now.setHours(0, 0, 0, 0);
    const from = new Date(now); from.setDate(from.getDate() - READY_DATE_DAYS);
    const to = new Date(now); to.setDate(to.getDate() + READY_DATE_DAYS);
    const pad = (n) => String(n).padStart(2, '0');
    const iso = (d, end) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` + (end ? 'T23:59:59.000Z' : 'T00:00:00.000Z');
    return { from: iso(from, false), to: iso(to, true) };
  }
  async function postDashboard(view) {
    const v = VIEWS[view], w = readyDateWindow();
    const body = { selectedTab: v.selectedTab, activeStatusId: v.statusId, carrierId: -1, readyDateFrom: w.from, readyDateTo: w.to, reviewStatusIds: [v.statusId] };
    const resp = await fetch(state.endpoint, { method: 'POST', headers: replayHeaders(), body: JSON.stringify(body), credentials: 'omit' });
    if (!resp.ok) throw new Error('dashboard POST ' + resp.status);
    const j = await resp.json();
    if (j && j.success === false) throw new Error('dashboard success:false');
    return (j && j.data) || [];
  }

  // ---- helpers --------------------------------------------------------------
  // EU filter (decision: hidden EVERYWHERE, all views). A shipment is EU when either location's
  // name starts "EU" or its country is outside NA — planner names aren't always NA-US-… coded
  // (service centers, raw street addresses), but country is reliably present on both sides.
  const NA_COUNTRY = /united states|usa|canada|mexico/i;
  function isEuLoc(loc) {
    if (!loc) return false;
    if (String(loc.locationName || '').trim().slice(0, 2).toUpperCase() === 'EU') return true;
    const c = String(loc.country || '').trim();
    return c !== '' && !NA_COUNTRY.test(c);
  }
  const isEuShipment = (s) => isEuLoc(s.originLocation) || isEuLoc(s.destinationLocation);

  const shortLoc = (n) => String(n || '').replace(/^NA-US-/, '');
  const stOf = (n) => { const p = String(n || '').split('-'); return (p[2] || '').toUpperCase(); };
  const legKey = (g) => (g.origin && g.origin.name || '') + ' → ' + (g.destination && g.destination.name || '');
  const fmtDate = (s) => { if (!s) return ''; const d = new Date(s); return isNaN(d) ? String(s).slice(0, 10) : d.toLocaleDateString(undefined, { month: 'short', day: '2-digit' }); };
  const dash = '<span class="noctr">—</span>';
  const esc = (s) => String(s).replace(/"/g, '&quot;');
  const shortShip = (s) => String(s && s.shipmentNumber || '').replace(/^SHP\d*-/, '');
  function geoCmp(a, b) {
    const ao = stOf(a.origin && a.origin.name), bo = stOf(b.origin && b.origin.name); if (ao !== bo) return ao.localeCompare(bo);
    const aSame = stOf(a.destination && a.destination.name) === ao, bSame = stOf(b.destination && b.destination.name) === bo; if (aSame !== bSame) return aSame ? -1 : 1;
    const ad = stOf(a.destination && a.destination.name), bd = stOf(b.destination && b.destination.name); if (ad !== bd) return ad.localeCompare(bd);
    return legKey(a).localeCompare(legKey(b));
  }
  function needByLabel(shipments) { const ds = [...new Set(shipments.map((s) => s.needByDate).filter(Boolean))].map((t) => new Date(t)).filter((d) => !isNaN(d)).sort((a, b) => a - b); if (!ds.length) return '—'; const a = fmtDate(ds[0]), b = fmtDate(ds[ds.length - 1]); return a === b ? a : `${a} – ${b}`; }
  function centerInPane(pane, el, smooth) { if (!pane || !el) return; const pr = pane.getBoundingClientRect(), er = el.getBoundingClientRect(); const top = pane.scrollTop + (er.top - pr.top) - (pane.clientHeight / 2 - el.clientHeight / 2); pane.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' }); }

  // An "active" bid = an offer standing on the shipment. A cancelled bid comes back as bid:null
  // from the dashboard, so presence + amount is the whole test (bidStatus 5 guarded anyway).
  const hasActiveBid = (s) => !!(s.bid && s.bid.bidAmount != null && String(s.bid.bidStatus) !== '5');

  // Model mix, e.g. {"model Y":2, "cybertruck":1} -> "2Y·1CT" (CT keeps its bidboard badge look).
  const MODEL_LETTER = { 'model 3': '3', 'model s': 'S', 'model x': 'X', 'model y': 'Y', cybertruck: 'CT' };
  function modelBits(s) {
    const mc = s.modelCount || {};
    return Object.keys(mc).map((k) => ({ letter: MODEL_LETTER[k.toLowerCase().trim()] || k.toUpperCase(), n: mc[k] }))
      .sort((a, b) => a.letter.localeCompare(b.letter));
  }
  // Left-table Models cell: bidboard's badge/letter look, no counts (the VINs column has the count).
  // e.g. "3·Y", "[CT]·Y", "S·X·Y".
  function modelHtml(s) {
    const bits = modelBits(s);
    if (!bits.length) return dash;
    return bits.map((b) => b.letter === 'CT' ? '<span class="badge ct">CT</span>' : b.letter).join('·');
  }
  // vclass for the audit record: pure-CT loads 'ct', no-CT 'std', otherwise 'mix'.
  function vclassOf(s) {
    const bits = modelBits(s); if (!bits.length) return 'std';
    const ct = bits.some((b) => b.letter === 'CT');
    return ct ? (bits.length === 1 ? 'ct' : 'mix') : 'std';
  }

  function groupShipments(list) {
    const map = new Map();
    for (const s of list) {
      const key = (s.originLocation && s.originLocation.locationName || '') + ' → ' + (s.destinationLocation && s.destinationLocation.locationName || '');
      let g = map.get(key);
      if (!g) { g = { origin: { name: s.originLocation && s.originLocation.locationName || '' }, destination: { name: s.destinationLocation && s.destinationLocation.locationName || '' }, shipments: [] }; map.set(key, g); }
      g.shipments.push(s);
    }
    for (const g of map.values()) g.shipments.sort((a, b) => String(a.shipmentNumber).localeCompare(String(b.shipmentNumber)));
    return [...map.values()];
  }

  // ---- load -----------------------------------------------------------------
  async function loadView(view) {
    if (!state.endpoint) return;
    state.loading = true; state.error = null; render();
    try {
      const list = (await postDashboard(view)).filter((s) => !isEuShipment(s));
      state.data[view] = list;
      if (view === 'bid') state.groups = groupShipments(list);
      LOG('loaded', view, list.length, 'shipments');
    } catch (e) { state.error = String(e && e.message || e); LOG('loadView error', e); }
    state.loading = false; render();
  }

  // Silent refresh after a write batch: every UpsertBid mints a NEW fvShipmentCarrierBidId and the
  // response doesn't return it, so re-read the board to keep ids fresh for queued edits. No render —
  // the DOM keeps focus; keys (leg|shipmentNumber) resolve against the replaced state at send time.
  async function refreshBidsSilently() {
    try {
      const list = (await postDashboard('bid')).filter((s) => !isEuShipment(s));
      state.data.bid = list;
      state.groups = groupShipments(list);
    } catch (e) { LOG('silent refresh failed (stale bid ids until next load)', e && e.message); }
  }

  // --- Pickup + recommended ETA (bidboard logic, verbatim) --------------------
  // Pickup DATE = today + 3 CALENDAR days, rolled forward to Monday when that lands on a weekend
  // (Wed/Thu/Fri all bid for Monday; Tue bids for Friday), 16:00 local.
  const isWeekend = (d) => d.getDay() === 0 || d.getDay() === 6;
  function pickupDate() { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + 3); while (isWeekend(d)) d.setDate(d.getDate() + 1); return d; }
  // The planner wants local "YYYY-MM-DD HH:mm:ss" strings (findings.md), not bidboard's 16:00Z ISO.
  const local16 = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} 16:00:00`;
  // transitDays scales with origin->destination distance (US state centroids):
  // <500mi:4  500-1000:9  1000-2000:11  >=2000:12  (intra-state -> 4).
  const STC = {AL:[32.8,-86.8],AZ:[34.3,-111.7],AR:[34.9,-92.4],CA:[37.2,-119.3],CO:[39.0,-105.5],CT:[41.6,-72.7],DE:[39.0,-75.5],FL:[28.6,-82.4],GA:[32.6,-83.4],ID:[44.2,-114.5],IL:[40.0,-89.2],IN:[39.9,-86.3],IA:[42.0,-93.5],KS:[38.5,-98.4],KY:[37.5,-85.3],LA:[31.0,-92.0],ME:[45.4,-69.2],MD:[39.0,-76.8],MA:[42.3,-71.8],MI:[44.3,-85.4],MN:[46.3,-94.3],MS:[32.7,-89.7],MO:[38.4,-92.5],MT:[47.0,-109.6],NE:[41.5,-99.8],NV:[39.3,-116.6],NH:[43.7,-71.6],NJ:[40.2,-74.7],NM:[34.4,-106.1],NY:[42.9,-75.5],NC:[35.6,-79.4],ND:[47.5,-100.3],OH:[40.3,-82.8],OK:[35.6,-97.5],OR:[43.9,-120.6],PA:[40.9,-77.8],RI:[41.7,-71.6],SC:[33.9,-80.9],SD:[44.4,-100.2],TN:[35.9,-86.4],TX:[31.5,-99.3],UT:[39.3,-111.7],VT:[44.1,-72.7],VA:[37.5,-78.9],WA:[47.4,-120.5],WV:[38.6,-80.6],WI:[44.6,-89.9],WY:[43.0,-107.6]};
  function milesBetween(a, b) { if (!a || !b) return null; const R = 3959, dLat = (b[0]-a[0])*Math.PI/180, dLon = (b[1]-a[1])*Math.PI/180, la1 = a[0]*Math.PI/180, la2 = b[0]*Math.PI/180; const h = Math.sin(dLat/2)**2 + Math.cos(la1)*Math.cos(la2)*Math.sin(dLon/2)**2; return 2*R*Math.asin(Math.sqrt(h)); }
  const routeMiles = (g) => milesBetween(STC[stOf(g.origin && g.origin.name)], STC[stOf(g.destination && g.destination.name)]);
  function transitDays(g) { const d = routeMiles(g); if (d == null || d < 500) return 4; if (d < 1000) return 9; if (d < 2000) return 11; return 12; }
  function needByEta(g) { const ds = (g.shipments || []).map((s) => s.needByDate && new Date(s.needByDate)).filter((d) => d && !isNaN(d)); if (!ds.length) return null; const t = new Date(Math.min(...ds)); t.setHours(0, 0, 0, 0); return t; }
  // ETA = pickup + transitDays CALENDAR days (may land on a weekend). State unparseable (service
  // centers, street addresses): recommend Tesla's earliest need-by, floored at the pickup date.
  function recommendedEta(g) {
    if (routeMiles(g) == null) { const nb = needByEta(g), p = pickupDate(); if (nb) return nb < p ? p : nb; }
    const t = pickupDate(); t.setDate(t.getDate() + transitDays(g)); return t;
  }
  function selectedEta(g) { const t = recommendedEta(g); t.setDate(t.getDate() + (state.dates[legKey(g)] || 0)); return t; }
  function dateBoxesFromBase(base, off) {
    const sel = new Date(base); sel.setDate(base.getDate() + off);
    const before = new Date(sel); before.setDate(sel.getDate() - 1);
    const after = new Date(sel); after.setDate(sel.getDate() + 1);
    return `<button class="dbox flank" data-dir="-1">${before.getDate()}</button>`
      + `<button class="dbox sel" data-dir="0">${sel.getDate()}</button>`
      + `<button class="dbox flank" data-dir="1">${after.getDate()}</button>`;
  }
  function dateSelector(g) {
    const base = recommendedEta(g), off = state.dates[legKey(g)] || 0;
    return `<div class="datesel" data-leg="${esc(legKey(g))}" data-base="${base.getTime()}">${dateBoxesFromBase(base, off)}</div>`;
  }

  // --- Bid audit recording (server-side log of every bid we submit) ----------
  // Fire-and-forget: recording must NEVER block or change the live bidding path.
  const RECORDER_URL = 'https://shipments.wastake.com/api/bids';
  const newBatchId = () => (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 'b-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  function shipBidRecords(batchId, clientTs, records) {
    if (!records.length) return;
    try {
      fetch(RECORDER_URL, {
        method: 'POST', mode: 'cors', credentials: 'omit', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ batch_id: batchId, client_ts: clientTs, bids: records }),
      }).catch((e) => console.warn('[planpanel] bid-record POST failed', e && e.message));
    } catch (e) { console.warn('[planpanel] bid-record POST failed', e && e.message); }
  }

  // --- Bid submission (PUT /TMS/UpsertBid — verified live, findings.md) ------
  const upsertUrl = () => String(state.endpoint).replace(/TMS\/GetShipmentPlannerReviewDashboard.*$/i, 'TMS/UpsertBid');
  const MIN_BID = 50;
  const cardSubmissionSlots = new Map(); // route -> {running, pending, promise}; newest pending wins
  const typedPriceInputs = (cardEl) => [...cardEl.querySelectorAll('.price:not([readonly])')].filter((i) => (i.value || '').trim() !== '');
  const bidValue = (value) => Number(String(value || '').replace(/[$,\s]/g, ''));
  function validateCardBids(cardEl, focusInvalid = false) {
    const bad = typedPriceInputs(cardEl).find((i) => {
      const n = bidValue(i.value);
      return !Number.isFinite(n) || n < MIN_BID;
    });
    if (!bad) { cardEl.classList.remove('bid-invalid'); return true; }
    cardEl.classList.remove('submitted', 'sending');
    cardEl.classList.add('bid-invalid');
    if (focusInvalid) { bad.focus({ preventScroll: true }); if (bad.select) bad.select(); }
    return false;
  }
  // key = "<legKey>|<shipmentNumber>" -> the group and the ONE shipment behind a price box.
  // Always resolved against CURRENT state, so a post-refresh lookup sees fresh bid ids.
  function shipmentForKey(key) {
    const sep = key.lastIndexOf('|'), leg = key.slice(0, sep), num = key.slice(sep + 1);
    const g = state.groups.find((x) => legKey(x) === leg); if (!g) return { g: null, s: null };
    return { g, s: g.shipments.find((s) => s.shipmentNumber === num) || null };
  }
  function upsertPayload(s, priceStr, readyBy, neededBy) {
    return {
      bidAmount: Number(priceStr),
      // Raw form leftovers the portal dialog also sends; server reads readyByDate/neededByDate.
      readyDateOnly: s.readyDate || null, readyTime: null,
      needByDateOnly: s.needByDate || null, needByTime: null,
      fvShipmentCarrierBidId: (s.bid && s.bid.fvShipmentCarrierBidId != null) ? s.bid.fvShipmentCarrierBidId : null,
      shipmentId: s.shipmentId,
      originLocationId: s.originLocation && s.originLocation.locationId,
      destinationLocationId: s.destinationLocation && s.destinationLocation.locationId,
      carrierId: state.carrierId,
      shipmentNumber: s.shipmentNumber,
      currencyCode: 'USD',
      bidStatus: 0,
      neededByDate: neededBy,
      readyByDate: readyBy,
    };
  }
  async function putBid(body) {
    const resp = await fetch(upsertUrl(), { method: 'PUT', headers: replayHeaders(), body: JSON.stringify(body), credentials: 'omit' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const j = await resp.json().catch(() => ({}));
    if (j && j.success === false) throw new Error('Tesla returned success:false'); // 200 but logically rejected
    return j;
  }

  function inputRouteKey(inputKey) {
    const sep = String(inputKey || '').lastIndexOf('|');
    return sep >= 0 ? inputKey.slice(0, sep) : String(inputKey || '');
  }

  function currentCard(route, fallback) {
    if (fallback && fallback.isConnected) return fallback;
    if (!body || !body.right) return fallback;
    return [...body.right.querySelectorAll('.fcard')].find((card) => card.dataset.leg === route) || fallback;
  }

  // Freeze exactly what was on the card when Enter was pressed. Editing without pressing Enter
  // does not enqueue anything; pressing Enter again replaces any waiting snapshot with this one.
  function snapshotCard(cardEl) {
    const inputs = typedPriceInputs(cardEl);
    if (!inputs.length) return null;
    const offers = [];
    for (const inp of inputs) {
      const { g } = shipmentForKey(inp.dataset.key);
      if (!g) continue;
      offers.push({
        key: inp.dataset.key,
        price: String(bidValue(inp.value)),
        readyByDate: local16(pickupDate()),
        neededByDate: local16(selectedEta(g)),
      });
    }
    if (!offers.length) return null;
    return { route: inputRouteKey(offers[0].key), cardEl, offers };
  }

  function markCardSending(snapshot) {
    const card = currentCard(snapshot.route, snapshot.cardEl);
    if (!card) return;
    const oldTag = card.querySelector('.subtag'); if (oldTag) oldTag.remove();
    card.classList.remove('submitted', 'submit-err', 'bid-invalid');
    card.classList.add('sending');
  }

  function finishCard(snapshot, failed) {
    const card = currentCard(snapshot.route, snapshot.cardEl);
    if (!card) return;
    const oldTag = card.querySelector('.subtag'); if (oldTag) oldTag.remove();
    card.classList.remove('sending', 'submitted', 'submit-err', 'bid-invalid');
    card.classList.add(failed ? 'submit-err' : 'submitted');
    if (failed) {
      const tag = document.createElement('div'); tag.className = 'subtag'; card.appendChild(tag);
      tag.textContent = `⚠ ${failed} bid${failed === 1 ? '' : 's'} failed`;
    }
  }

  async function sendCardSnapshot(snapshot, slot) {
    let failed = 0, wrote = 0;
    // Audit trail: one record per shipment actually attempted, shipped fire-and-forget
    // on BOTH exit paths (normal finish and superseded-mid-batch).
    const batchId = newBatchId(), clientTs = new Date().toISOString(), records = [];
    for (const offer of snapshot.offers) {
      // The current request cannot be recalled, but once it finishes skip the rest of this stale
      // batch and move immediately to the newest Entered snapshot.
      if (slot.pending) { shipBidRecords(batchId, clientTs, records); if (wrote) await refreshBidsSilently(); return { failed, superseded: true }; }
      const { g, s } = shipmentForKey(offer.key); if (!g || !s) continue;
      const prevBid = (s.bid && s.bid.bidAmount != null) ? Number(s.bid.bidAmount) : null;
      const requestBody = upsertPayload(s, offer.price, offer.readyByDate, offer.neededByDate);
      const rec = {
        origin: (g.origin && g.origin.name) || null,
        destination: (g.destination && g.destination.name) || null,
        origin_state: stOf(g.origin && g.origin.name) || null,
        dest_state: stOf(g.destination && g.destination.name) || null,
        // vin column now carries the shipment number (the planner has no VINs, findings.md);
        // bid_id carries the shipmentId. Distinguishable from real 17-char VINs by the SHP prefix.
        vin: s.shipmentNumber, bid_id: s.shipmentId,
        model: modelBits(s).map((b) => b.n + 'x' + b.letter).join('+') || null,   // e.g. "1x3+3xY"
        vclass: vclassOf(s),
        price: offer.price, currency: 'USD',
        list_price: null,                    // the planner API exposes no list price
        prev_counter: prevBid, verb: 'UpsertBid',
        pickup_date: offer.readyByDate, eta_date: offer.neededByDate,
        eta_offset: state.dates[legKey(g)] || 0,
        need_by_date: s.needByDate || null,
        no_of_vins: s.noOfVins != null ? s.noOfVins : null,
        success: 0, error: null,
      };
      try {
        await putBid(requestBody);
        wrote++;
        // Optimistic local patch so TO-DO/placeholders reflect the write even before the silent
        // refresh lands. The refresh below replaces this with the REAL new bid id.
        s.bid = Object.assign({}, s.bid || {}, { bidStatus: '0', bidAmount: Number(offer.price), currencyCode: 'USD' });
        rec.success = 1;
      } catch (e) {
        failed++;
        rec.error = String((e && e.message) || e);
        console.warn('[planpanel] bid FAILED for', s.shipmentNumber, '—', e && e.message);
      }
      records.push(rec);
    }
    shipBidRecords(batchId, clientTs, records);
    // Ids are stale after ANY successful write (each upsert mints a new fvShipmentCarrierBidId).
    // Await so a queued correction reads fresh ids before it sends.
    if (wrote) await refreshBidsSilently();
    return { failed, superseded: false };
  }

  async function drainCardSubmissions(route, slot) {
    slot.running = true;
    let finalOk = false;
    try {
      while (slot.pending) {
        const snapshot = slot.pending;
        slot.pending = null;
        const result = await sendCardSnapshot(snapshot, slot);
        if (slot.pending || result.superseded) continue;
        finishCard(snapshot, result.failed);
        finalOk = result.failed === 0;
      }
      return finalOk;
    } finally {
      slot.running = false;
      if (cardSubmissionSlots.get(route) === slot) cardSubmissionSlots.delete(route);
    }
  }

  // Send every TYPED box in a card; each box is one shipment. Only one batch runs per route.
  // Repeated Enters while it runs collapse to the newest snapshot (last-write-wins).
  function submitCard(cardEl) {
    if (!validateCardBids(cardEl, true)) return Promise.resolve(false);
    const snapshot = snapshotCard(cardEl);
    if (!snapshot) return Promise.resolve(false);
    let slot = cardSubmissionSlots.get(snapshot.route);
    if (!slot) {
      slot = { running: false, pending: null, promise: null };
      cardSubmissionSlots.set(snapshot.route, slot);
    }
    slot.pending = snapshot;
    markCardSending(snapshot);
    if (!slot.running) slot.promise = drainCardSubmissions(snapshot.route, slot);
    return slot.promise;
  }

  // ---- 3) Panel -------------------------------------------------------------
  let host, root, body, rafPending = 0, toastTimer = 0;
  let leftSelectionLockRi = null, leftSelectionUnlockTimer = 0;
  // "Nothing to Bid" toast is edge-triggered: shown ONCE when the TO-DO list empties.
  let nothingShown = true;
  function armLeftSelectionUnlock() {
    clearTimeout(leftSelectionUnlockTimer);
    leftSelectionUnlockTimer = setTimeout(() => { leftSelectionLockRi = null; }, 220);
  }
  function showToast(msg) {
    if (!root) return;
    let t = root.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; root.querySelector('.panel').appendChild(t); }
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
  }
  function ensurePanel() {
    if (host || !document.documentElement) return;
    host = document.createElement('div');
    host.id = 'planpanel-host';
    host.style.cssText = 'z-index:2147483647;';
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        *{box-sizing:border-box;font-family:Inter,system-ui,Arial,sans-serif}
        .panel{position:relative;display:flex;flex-direction:column;height:100%;background:#fff;color:#171a20;border:0;border-radius:0;box-shadow:none;overflow:hidden}
        .toast{position:absolute;left:50%;bottom:18px;transform:translateX(-50%) translateY(8px);background:#171a20;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;font-weight:800;letter-spacing:.02em;box-shadow:0 6px 20px rgba(0,0,0,.25);opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:6}
        .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
        .tools{display:flex;flex-direction:column;gap:8px;padding:8px 12px;border-bottom:1px solid #eee}
        .trow{display:flex;align-items:center;gap:10px;width:50%}
        .vrow{display:flex;align-items:center;gap:8px}
        .tools input{flex:1;min-width:0;padding:6px 8px;border:1px solid #d0d3d6;border-radius:6px;font-size:13px}
        .todobtn,.vbtn{background:#e6e8ea;color:#3a3f49;border:1px solid #cfd3d7;border-radius:6px;padding:6px 0;min-width:82px;text-align:center;font-size:12px;font-weight:700;cursor:pointer;font-family:Arial,Helvetica,sans-serif;letter-spacing:.04em}
        .todobtn:hover,.vbtn:hover{background:#dcdfe2}
        .todobtn.on{background:#3457d5;color:#fff;border-color:#3457d5}
        .vbtn{min-width:96px}
        .vbtn.on{background:#171a20;color:#fff;border-color:#171a20}
        .fcard.sending{border-color:#3457d5;opacity:.85}
        .fcard.submitted,.fcard.submitted.active{border-color:#0a7d33;box-shadow:0 0 0 2px rgba(10,125,51,.18)}
        .fcard.submit-err,.fcard.submit-err.active{border-color:#c0392b;box-shadow:0 0 0 2px rgba(192,57,43,.18)}
        .fcard.bid-invalid,.fcard.bid-invalid.active{border-color:#c0392b;background:#fff7f6;box-shadow:0 0 0 3px rgba(192,57,43,.22)}
        .subtag{margin-top:10px;font-size:12px;font-weight:800;color:#0a7d33}
        .fcard.submit-err .subtag{color:#c0392b}
        .bodywrap{display:flex;flex-direction:row-reverse;flex:1;overflow:hidden}
        .left{width:50%;overflow:auto;padding:6px}
        .right{width:50%;overflow:auto;padding:43vh 18px;background:#f6f7f9;border-right:1px solid #e6e8ea}
        /* read-only views: one full-width pane */
        .bodywrap.single .left{width:100%}
        .bodywrap.single .right{display:none}
        .grp{border:1px solid #eceef0;border-radius:8px;margin:6px 4px;overflow:hidden;cursor:pointer}
        .grp:hover{background:#fafbfc}
        .grp.sel{border-color:#3457d5;box-shadow:0 0 0 3px rgba(52,87,213,.18)}.grp.sel>.row{background:#eaf0ff}
        .grp>.row{display:flex;align-items:center;gap:6px;padding:8px 10px;cursor:pointer;background:#fafbfc}
        .grp>.row:hover{background:#f1f3f5}
        .leg{flex:1;font-size:13px;font-weight:600;line-height:1.3}
        .cnt{font-size:12px;font-weight:700;color:#3457d5;background:#eaf0ff;border-radius:10px;padding:2px 8px;white-space:nowrap;font-family:Arial,Helvetica,sans-serif}
        .vins{padding:2px 8px 8px}
        table{width:100%;border-collapse:collapse;font-size:12px}
        th,td{text-align:left;padding:3px 6px;border-bottom:1px solid #f0f1f3;white-space:nowrap}
        th{color:#9a9da1;font-weight:600}td.shp{font-family:ui-monospace,Menlo,Consolas,monospace}
        .ctr{color:#0a7d33;font-weight:700}.noctr{color:#b0b3b7}td.model{font-weight:700}
        .stat{font-size:11px;font-weight:700;border-radius:10px;padding:1px 8px;background:#eef0f2;color:#5c5e62}
        .stat.placed{background:#eaf0ff;color:#3457d5}
        .stat.accepted{background:#e7f6ec;color:#0a7d33}
        .stat.rejected,.stat.cancelled{background:#fdeceae0;color:#c0392b}
        .badge{display:inline-block;color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:700;letter-spacing:.5px}
        .badge.ct{background:rgba(120,135,160,.18);color:#566072;border:2px solid #000;border-radius:3px;padding:1px 6px}  /* CT shading = shipment-creator .vbub.ct */
        /* focused bidding cards */
        .fcard{background:#fff;border:1px solid #e0e3e6;border-radius:14px;box-shadow:0 2px 10px rgba(0,0,0,.06);padding:18px 20px;margin:0 auto 16px;max-width:560px;transition:box-shadow .15s,border-color .15s,transform .15s}
        .fcard.active{border-color:#3457d5;box-shadow:0 10px 30px rgba(52,87,213,.22);transform:translateY(-1px)}
        .froute{display:flex;align-items:center;gap:8px;font-size:17px;font-weight:700;line-height:1.3;flex-wrap:wrap}.froute .arrow{color:#9a9da1}
        .fmeta{margin:12px 0 10px;color:#5c5e62;font-size:14px}.fneed b{color:#171a20}
        .datesel{display:flex;gap:6px;margin:0 0 14px}
        .dbox{width:42px;padding:6px 0;text-align:center;border:1px solid #cfd3d7;border-radius:8px;background:#fff;color:#5c5e62;font-size:14px;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-variant-numeric:tabular-nums;cursor:pointer}
        .dbox:hover{background:#f1f3f5}
        .dbox.flank{font-size:12px;color:#9a9da1}
        .dbox.sel{border-color:#3457d5;background:#eaf0ff;color:#3457d5;cursor:default;font-weight:800}
        .price-row{display:flex;gap:18px;align-items:flex-end;flex-wrap:wrap}
        .price-col{display:flex;flex-direction:column;gap:6px}
        .pcap{font-size:13px;font-weight:700;color:#5c5e62;display:flex;align-items:center;gap:6px;font-family:Arial,Helvetica,sans-serif}
        .pcap .num{color:#171a20;font-size:23px;line-height:1;font-family:Arial,Helvetica,sans-serif}
        /* compact $-prefixed price box (sized for ~4 digits) */
        .pin{display:flex;align-items:center;gap:1px;width:92px;padding:9px 10px;border:1px solid #cfd3d7;border-radius:10px;background:#fff}
        .pin:focus-within{border-color:#3457d5;box-shadow:0 0 0 3px rgba(52,87,213,.15)}
        .pin .cur{font-size:16px;font-weight:700;color:#0a7d33;opacity:.45}
        .pin.filled .cur{opacity:1}
        .pin input{flex:1;min-width:0;border:0;outline:0;background:transparent;font-size:16px;font-weight:700;color:#0a7d33;padding:0}
        .pin input::placeholder{color:#0a7d33;opacity:.45}
        .empty{padding:18px;text-align:center;color:#9a9da1;font-size:13px}.empty.done{color:#0a7d33;font-weight:800;font-size:29px;padding-top:40px}.err{color:#c0392b}.hidden{display:none}.arrow{color:#9a9da1;margin:0 2px}
        .left.center-empty,.right.center-empty{padding:0;overflow:hidden;display:flex;align-items:center;justify-content:center}.center-empty .empty{padding:0}.empty.clock{font-size:34px;letter-spacing:.04em}
        /* loading arc: a rounded gray arc spins */
        .arc{width:46px;height:46px;animation:pparcspin .9s linear infinite}
        .arc circle{stroke:#b0b3b7}
        @keyframes pparcspin{to{transform:rotate(360deg)}}
      </style>
      <div class="panel">
        <div class="tools">
          <div class="vrow" id="vrow"></div>
          <div class="trow"><button id="todo" class="todobtn on">TO-DO</button><input id="filter" placeholder="Filter…" /></div>
        </div>
        <div class="bodywrap" id="bodywrap"><div class="left" id="left"></div><div class="right" id="right"></div></div>
      </div>`;
    document.documentElement.appendChild(host);

    body = { wrap: root.getElementById('bodywrap'), left: root.getElementById('left'), right: root.getElementById('right'), filter: root.getElementById('filter'), todo: root.getElementById('todo'), vrow: root.getElementById('vrow') };
    body.vrow.innerHTML = Object.keys(VIEWS).map((v) => `<button class="vbtn${v === state.view ? ' on' : ''}" data-view="${v}">${VIEWS[v].label}</button>`).join('');
    body.vrow.addEventListener('click', (e) => {
      const b = e.target.closest && e.target.closest('.vbtn'); if (!b) return;
      const v = b.dataset.view; if (!v) return;
      state.view = v;
      body.vrow.querySelectorAll('.vbtn').forEach((x) => x.classList.toggle('on', x === b));
      loadView(v);   // always refetch on tab switch (cheap: one POST) so the board is fresh
      render();
    });
    body.filter.addEventListener('input', () => { state.filter = body.filter.value.trim().toLowerCase(); render(); });
    body.todo.addEventListener('click', (e) => { state.todoOnly = !state.todoOnly; e.target.textContent = state.todoOnly ? 'TO-DO' : 'ALL'; e.target.classList.toggle('on', state.todoOnly); render(); });
    body.right.addEventListener('scroll', () => {
      if (leftSelectionLockRi != null) armLeftSelectionUnlock();
      if (rafPending) return;
      rafPending = requestAnimationFrame(() => { rafPending = 0; syncFromRight(); });
    });
    body.right.addEventListener('input', (e) => { const t = e.target; if (t.classList && t.classList.contains('price')) { state.prices[t.dataset.key] = t.value; const pin = t.closest('.pin'); if (pin) pin.classList.toggle('filled', t.value.trim() !== ''); } });
    body.right.addEventListener('click', (e) => {
      const db = e.target.closest && e.target.closest('.dbox');
      if (db) { const dir = +db.dataset.dir; if (dir) { const cont = db.closest('.datesel'); const leg = cont.dataset.leg; state.dates[leg] = (state.dates[leg] || 0) + dir; cont.innerHTML = dateBoxesFromBase(new Date(+cont.dataset.base), state.dates[leg]); } return; }
      const c = e.target.closest && e.target.closest('.fcard');
      if (c && !(e.target.classList && e.target.classList.contains('price'))) { centerInPane(body.right, c, true); syncFromRight(); }
    });
    // Enter -> smooth-scroll to the next UNPRICED box (skip shipments that already have a bid)
    body.right.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || !(e.target.classList && e.target.classList.contains('price'))) return;
      e.preventDefault();
      const curCard = e.target.closest('.fcard');
      if (curCard && !validateCardBids(curCard, true)) return;
      const inputs = [...body.right.querySelectorAll('.price')];
      let i = inputs.indexOf(e.target) + 1;
      while (i < inputs.length && inputs[i].dataset.priced === '1') i++;
      const next = inputs[i];
      const nextCard = next ? next.closest('.fcard') : null;
      // Finishing a card (Enter moves to a different card, or the end) sends that card's bids — always live.
      if (curCard && nextCard !== curCard) submitCard(curCard);
      if (next) { if (nextCard) centerInPane(body.right, nextCard, true); next.focus({ preventScroll: true }); if (next.select) next.select(); }
      else { e.target.blur(); showToast('Bids Finished'); }
    });
    window.addEventListener('resize', applyPlacement);
    setupNav();
  }

  // ---- Placement: embed into Tesla's own layout, or fall back to a fixed overlay --------------
  const PANEL_GAP = 10;
  let hiddenEl = null, observedParent = null, mo = null, moScheduled = false;
  const PLANNER_PATH = /\/logistics\/fv-shipment-planner\/review/i;

  function findContent() {
    const nav = document.querySelector('tsl-nav, nav.main-nav, [class*="main-nav"]');
    if (!nav || !nav.parentElement) return null;
    const parent = nav.parentElement;
    const sibs = [...parent.children].filter((c) => c !== host && c !== nav);
    let best = null, bw = 0;
    for (const c of sibs) { const r = c.getBoundingClientRect(); if (r.height > 200 && r.width > bw) { bw = r.width; best = c; } }
    best = best || sibs[0] || null;
    return best ? { parent, nav, content: best } : null;
  }

  // Splice our panel into the page: hide Tesla's board (never remove it — Angular owns that DOM)
  // and insert `host` in its slot as a normal in-flow element. Returns false if no container found.
  function embed() {
    const f = findContent();
    if (!f) return false;
    const { parent, content } = f;
    if (content === host) return true;
    const cs = getComputedStyle(content);
    const boxW = cs.width, boxH = cs.height;
    if (hiddenEl && hiddenEl !== content && hiddenEl.style) hiddenEl.style.display = '';
    if (content.style.display !== 'none') content.style.display = 'none';
    hiddenEl = content;
    if (host.parentElement !== parent || host.nextElementSibling !== content) parent.insertBefore(host, content);
    const flexRow = /flex/.test(getComputedStyle(parent).display);
    host.style.cssText = flexRow
      ? `z-index:2147483647;flex:1 1 0%;min-width:0;align-self:stretch;display:block;padding-left:${PANEL_GAP}px;box-sizing:border-box;`
      : `z-index:2147483647;display:block;width:${boxW};height:${boxH};padding-left:${PANEL_GAP}px;box-sizing:border-box;`;
    state.embedded = true;
    ensureObserver(parent);
    return true;
  }
  function restoreContent() { if (hiddenEl && hiddenEl.style && hiddenEl.style.display === 'none') hiddenEl.style.display = ''; }

  function ensureObserver(parent) {
    if (mo && observedParent === parent) return;
    if (mo) mo.disconnect();
    observedParent = parent;
    mo = new MutationObserver(() => {
      if (!PLANNER_PATH.test(location.pathname)) return;
      if (moScheduled) return; moScheduled = true;
      setTimeout(() => { moScheduled = false; applyPlacement(); }, 80);
    });
    mo.observe(parent, { childList: true });
  }

  // Fallback only: fixed overlay filling the content area to the right of the nav.
  function dock() {
    const nav = document.querySelector('tsl-nav, nav.main-nav, [class*="main-nav"]');
    let left = 210, top = 56;
    if (nav) { const r = nav.getBoundingClientRect(); if (r.width > 40 && r.height > 200) { left = Math.max(0, Math.round(r.right)); top = Math.max(0, Math.round(r.top)); } }
    host.style.cssText = `position:fixed;z-index:2147483647;left:${left}px;top:${top}px;right:auto;transform:none;width:${Math.max(360, window.innerWidth - left)}px;height:${Math.max(240, window.innerHeight - top)}px;padding-left:${PANEL_GAP}px;box-sizing:border-box;`;
  }

  function applyPlacement() {
    if (!host) return;
    // Off the planner review page, always hide + give Tesla its content back instead of embedding.
    if (!PLANNER_PATH.test(location.pathname)) {
      host.style.display = 'none';
      restoreContent();
      if (host.parentElement && host.parentElement !== document.documentElement) document.documentElement.appendChild(host);
      return;
    }
    if (embed()) return;
    state.embedded = false;
    dock();
  }

  function setupNav() {
    const onPlanner = () => PLANNER_PATH.test(location.pathname);
    const apply = () => {
      if (!host) return;
      if (onPlanner()) { applyPlacement(); host.style.display = ''; }
      else {
        host.style.display = 'none';
        restoreContent();
        if (host.parentElement && host.parentElement !== document.documentElement) document.documentElement.appendChild(host);
      }
    };
    ['pushState', 'replaceState'].forEach((m) => { const o = history[m]; history[m] = function () { const r = o.apply(this, arguments); apply(); return r; }; });
    window.addEventListener('popstate', apply);
    window.addEventListener('hashchange', apply);
    let last = location.href;
    setInterval(() => { if (location.href !== last) { last = location.href; apply(); } }, 300);
    setInterval(() => { if (host && host.style.display !== 'none') applyPlacement(); }, 500);
    setInterval(() => { const el = root && root.querySelector('.empty.clock'); if (el) el.textContent = nowHHMM(); }, 10000);
    apply();
  }

  // ---- 4) Render ------------------------------------------------------------
  function matchesFilter(s) {
    const f = state.filter; if (!f) return true;
    return (s.shipmentNumber || '').toLowerCase().includes(f)
      || Object.keys(s.modelCount || {}).some((m) => m.toLowerCase().includes(f));
  }
  function currentGroups() {
    let groups = state.groups.slice();
    if (state.filter) { const f = state.filter; groups = groups.filter((g) => legKey(g).toLowerCase().includes(f) || g.shipments.some(matchesFilter)); }
    if (state.todoOnly) groups = groups.filter((g) => g.shipments.some((s) => !hasActiveBid(s)));   // TO-DO = has an un-bid shipment
    groups.sort(geoCmp);
    return groups;
  }

  function priceBox(key, s) {
    const k = key + '|' + s.shipmentNumber;
    const local = state.prices[k] != null ? state.prices[k] : '';
    const cur = hasActiveBid(s) ? s.bid.bidAmount : null;   // existing bid -> faded placeholder
    const ph = cur != null ? String(cur) : '';
    const done = hasActiveBid(s);                           // skip on Enter when already bid
    // Bidboard cap style: "<n> VINs", or "<n> [CT]" when the load carries a badge-worthy model.
    const n = s.noOfVins != null ? s.noOfVins : '?';
    const hasCT = modelBits(s).some((b) => b.letter === 'CT');
    const cap = `<span class="num">${n}</span> ` + (hasCT ? '<span class="badge ct">CT</span>' : `VIN${n === 1 ? '' : 's'}`);
    return `<div class="price-col"><div class="pcap">${cap}</div>`
      + `<div class="pin${local !== '' ? ' filled' : ''}"><span class="cur">$</span>`
      + `<input class="price" type="text" inputmode="decimal" placeholder="${esc(ph)}" value="${esc(local)}" data-key="${esc(k)}" data-priced="${done ? 1 : 0}"></div></div>`;
  }

  function nowHHMM() {
    const d = new Date();
    const suffix = d.getHours() >= 12 ? 'PM' : 'AM';
    const hour = d.getHours() % 12 || 12;
    return hour + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + suffix;
  }

  function bidCells(s) {
    const b = s.bid || {};
    const amt = (b.bidAmount != null) ? `<span class="ctr">${b.bidAmount} ${b.currencyCode || ''}</span>` : dash;
    const pu = b.readyByDate ? fmtDate(b.readyByDate) : dash;
    const eta = b.neededByDate ? fmtDate(b.neededByDate) : dash;
    return { amt, pu, eta };
  }
  const BID_STATUS_CLASS = { 0: 'placed', 1: 'accepted', 2: 'rejected', 3: 'closed', 5: 'cancelled' };
  function statusChip(s) {
    if (!s.bid || s.bid.bidAmount == null) return dash;
    const label = BID_STATUS_LABEL[String(s.bid.bidStatus)] || ('#' + s.bid.bidStatus);
    return `<span class="stat ${BID_STATUS_CLASS[String(s.bid.bidStatus)] || ''}">${label}</span>`;
  }

  function render() {
    if (!root) return;
    if (state.view !== 'bid') { renderReadonly(); return; }
    body.wrap.classList.remove('single');
    body.todo.style.display = '';
    body.left.classList.remove('center-empty'); body.right.classList.remove('center-empty');
    if (state.groups.some((g) => g.shipments.some((s) => !hasActiveBid(s)))) nothingShown = false;

    if (state.error) { body.left.innerHTML = `<div class="empty err">Error: ${state.error}</div>`; body.right.innerHTML = ''; return; }
    if (state.loading && !state.groups.length) {
      // Panes are flipped (row-reverse): body.right renders LEFT.
      body.left.classList.add('center-empty'); body.right.classList.add('center-empty');
      body.right.innerHTML = `<svg class="arc" viewBox="0 0 50 50"><circle cx="25" cy="25" r="20" fill="none" stroke-width="5" stroke-linecap="round" stroke-dasharray="94 32"/></svg>`;
      body.left.innerHTML = '';
      return;
    }

    const gs = currentGroups();
    if (!gs.length) {
      if (state.todoOnly && !state.filter) {
        body.left.classList.add('center-empty'); body.right.classList.add('center-empty');
        // Panes are visually flipped: "Nothing to Bid" -> body.right (left), clock -> body.left (right).
        body.right.innerHTML = `<div class="empty done">✓ Nothing to Bid</div>`;
        body.left.innerHTML = `<div class="empty done clock">${nowHHMM()}</div>`;
        if (!nothingShown) { showToast('Nothing to Bid'); nothingShown = true; }
      } else {
        body.left.innerHTML = `<div class="empty">No shipments${state.filter ? ' match the filter' : ' captured yet'}.</div>`;
        body.right.innerHTML = '';
      }
      return;
    }

    // LEFT — route groups, one table row per shipment
    const lf = document.createDocumentFragment();
    gs.forEach((g, ri) => {
      const cnt = g.shipments.length;
      const grp = document.createElement('div'); grp.className = 'grp'; grp.dataset.ri = ri;
      grp.addEventListener('click', () => {
        const fc = body.right.querySelector(`.fcard[data-ri="${ri}"]`);
        leftSelectionLockRi = ri;
        armLeftSelectionUnlock();
        selectRi(ri, false);
        if (fc) centerInPane(body.right, fc, true);
      });
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = `<div class="leg"><span class="o">${shortLoc(g.origin && g.origin.name)}</span><span class="arrow">→</span><span class="d">${shortLoc(g.destination && g.destination.name)}</span></div>`
        + `<div class="cnt">${cnt} SHP${cnt === 1 ? '' : 'S'}</div>`;
      grp.appendChild(row);
      const wrap = document.createElement('div'); wrap.className = 'vins';
      const rows = g.shipments.map((s) => {
        const c = bidCells(s);
        return `<tr><td class="shp">${shortShip(s)}</td><td>${s.noOfVins != null ? s.noOfVins : ''}</td><td class="model">${modelHtml(s)}</td><td>${fmtDate(s.needByDate)}</td><td>${c.amt}</td><td>${c.pu}</td><td>${c.eta}</td></tr>`;
      }).join('');
      wrap.innerHTML = `<table><thead><tr><th>Shipment</th><th>VINs</th><th>Models</th><th>Need by</th><th>My bid</th><th>Pickup</th><th>ETA</th></tr></thead><tbody>${rows}</tbody></table>`;
      grp.appendChild(wrap);
      lf.appendChild(grp);
    });
    body.left.innerHTML = ''; body.left.appendChild(lf);

    // RIGHT — one card per route; ONE PRICE BOX PER SHIPMENT
    const rf = document.createDocumentFragment();
    gs.forEach((g, ri) => {
      const key = legKey(g);
      const O = shortLoc(g.origin && g.origin.name), D = shortLoc(g.destination && g.destination.name);
      const card = document.createElement('div'); card.className = 'fcard'; card.dataset.ri = ri; card.dataset.leg = key;
      const boxes = g.shipments.map((s) => priceBox(key, s)).join('');
      card.innerHTML = `<div class="froute"><span>${O}</span><span class="arrow">→</span><span>${D}</span></div>`
        + `<div class="fmeta"><div class="fneed">Need by <b>${needByLabel(g.shipments)}</b></div></div>`
        + dateSelector(g)
        + `<div class="price-row">${boxes}</div>`;
      rf.appendChild(card);
    });
    body.right.innerHTML = ''; body.right.appendChild(rf);

    const firstCard = body.right.querySelector('.fcard');
    if (firstCard) centerInPane(body.right, firstCard);
    syncFromRight();
  }

  // Read-only boards (REVIEW / CONFIRMED / REJECTED): one full-width grouped list.
  function renderReadonly() {
    body.wrap.classList.add('single');
    body.todo.style.display = 'none';   // TO-DO only means something on the bid view
    body.left.classList.remove('center-empty');
    body.right.innerHTML = '';
    if (state.error) { body.left.innerHTML = `<div class="empty err">Error: ${state.error}</div>`; return; }
    const list = state.data[state.view];
    if (state.loading && !(list && list.length)) {
      body.left.classList.add('center-empty');
      body.left.innerHTML = `<svg class="arc" viewBox="0 0 50 50"><circle cx="25" cy="25" r="20" fill="none" stroke-width="5" stroke-linecap="round" stroke-dasharray="94 32"/></svg>`;
      return;
    }
    let groups = groupShipments(list || []);
    if (state.filter) { const f = state.filter; groups = groups.filter((g) => legKey(g).toLowerCase().includes(f) || g.shipments.some(matchesFilter)); }
    groups.sort(geoCmp);
    if (!groups.length) {
      body.left.innerHTML = `<div class="empty">Nothing in ${VIEWS[state.view].label.toLowerCase()}${state.filter ? ' matching the filter' : ''}.</div>`;
      return;
    }
    const lf = document.createDocumentFragment();
    groups.forEach((g) => {
      const cnt = g.shipments.length;
      const grp = document.createElement('div'); grp.className = 'grp';
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = `<div class="leg"><span class="o">${shortLoc(g.origin && g.origin.name)}</span><span class="arrow">→</span><span class="d">${shortLoc(g.destination && g.destination.name)}</span></div>`
        + `<div class="cnt">${cnt} SHP${cnt === 1 ? '' : 'S'}</div>`;
      grp.appendChild(row);
      const wrap = document.createElement('div'); wrap.className = 'vins';
      const rows = g.shipments.map((s) => {
        const c = bidCells(s);
        return `<tr><td class="shp">${shortShip(s)}</td><td>${s.noOfVins != null ? s.noOfVins : ''}</td><td class="model">${modelHtml(s)}</td><td>${fmtDate(s.readyDate)}</td><td>${fmtDate(s.needByDate)}</td><td>${c.amt}</td><td>${statusChip(s)}</td></tr>`;
      }).join('');
      wrap.innerHTML = `<table><thead><tr><th>Shipment</th><th>VINs</th><th>Models</th><th>Ready</th><th>Need by</th><th>My bid</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
      grp.appendChild(wrap);
      lf.appendChild(grp);
    });
    body.left.innerHTML = ''; body.left.appendChild(lf);
  }

  function syncFromRight() {
    if (!body || state.view !== 'bid') return;
    const cards = body.right.querySelectorAll('.fcard'); if (!cards.length) return;
    if (leftSelectionLockRi != null) {
      const locked = body.right.querySelector(`.fcard[data-ri="${leftSelectionLockRi}"]`);
      if (locked) {
        cards.forEach((c) => c.classList.toggle('active', c === locked));
        highlightLeft(leftSelectionLockRi, false);
        return;
      }
      leftSelectionLockRi = null;
    }
    const pr = body.right.getBoundingClientRect(), cy = pr.top + pr.height / 2;
    let best = null, bd = Infinity;
    cards.forEach((c) => { const r = c.getBoundingClientRect(); const d = Math.abs((r.top + r.height / 2) - cy); if (d < bd) { bd = d; best = c; } });
    if (!best) return;
    cards.forEach((c) => c.classList.toggle('active', c === best));
    highlightLeft(+best.dataset.ri);
  }
  function highlightLeft(ri, center = true) {
    body.left.querySelectorAll('.grp.sel').forEach((e) => e.classList.remove('sel'));
    const lg = body.left.querySelector(`.grp[data-ri="${ri}"]`);
    if (lg) { lg.classList.add('sel'); if (center) centerInPane(body.left, lg); }
  }
  function selectRi(ri, centerLeft = true) { body.right.querySelectorAll('.fcard').forEach((c) => c.classList.toggle('active', +c.dataset.ri === ri)); highlightLeft(ri, centerLeft); }

  window.__planpanelState = state; window.__planpanelRender = render; window.__planpanelLoad = loadView;

  if (document.documentElement) ensurePanel();
  else document.addEventListener('readystatechange', ensurePanel, { once: true });
  LOG('installed. Waiting for shipment-planner data…');
})();
