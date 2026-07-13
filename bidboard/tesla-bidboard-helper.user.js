// ==UserScript==
// @name         Tesla Bid-Board Helper (live bidding)
// @namespace    wastake.bidboard
// @version      0.27.0
// @description  Split panel for the Tesla bid board, SPLICED INTO the page — it replaces Tesla's own board in-place (in-flow, no header bar), so it reads as part of the page; falls back to a fixed overlay if the container isn't found. Left: focused bidding cards (separate boxes for CT/CAB) with a recommended-ETA picker. Right: every route + its VINs (from the API). LIVE: pressing Enter to finish a card submits its prices to Tesla (UpdateOffer) for every VIN in the card.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/bidboard/tesla-bidboard-helper.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/bidboard/tesla-bidboard-helper.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * LIVE BIDDING — typing a price and pressing Enter to leave a card POSTs UpdateOffer for that card's VINs.
 *   Left half : route list (origin -> destination), each expanded with its VINs / list price / existing counter.
 *   Right half: one focused card per route — route, recommended-ETA date picker, and price box(es). CT and CAB
 *               (Cybercab, VIN starts 5YJA) each get their own box; one price -> every VIN in that subset.
 *   Submit    : Enter only sends boxes you've TYPED into; pickup = next weekday at 16:00Z, USD (ETA counts calendar days, may be a weekend).
 *               Card stays green on success (HTTP 200), red on failure. Only sent VINs are committed.
 *   Note      : the panel is a snapshot loaded once (+ Reload). After bidding, hit Reload to see updated counters.
 *
 * Data model (see findings.md): POST {skip,take} -> { data:{ items:Group[], totalRecords }, success }
 *   Group = { origin:{name,…}, destination:{name,…}, bids:{ items:Bid[], totalRecords } }
 *   Bid   = { vin, model, scheduledPickupDate, needByDate, price, currencyCode,
 *             carrierCounter:{ bidAmount, currencyCode, estimatedShipDate, neededByDate, … }|null }
 */

(function () {
  'use strict';
  const LOG = (...a) => console.log('%c[bidpanel]', 'color:#06c;font-weight:bold', ...a);

  const state = {
    endpoint: null, headers: null, groups: [], total: 0, loading: false, error: null,
    filter: '', sortByCount: false, prices: {}, dates: {}, todoOnly: false,
    floating: false,   // true once the user drags the panel off its docked position (overlay fallback only)
    embedded: false,   // true when the panel is spliced into Tesla's own layout (in-flow, not overlaid)
  };

  // ---- 1) Capture the groups POST (endpoint + auth) by hooking XHR ----------
  function isGroupsResponse(text) {
    if (!text || text.indexOf('"items"') === -1 || text.indexOf('"bids"') === -1) return null;
    try { const j = JSON.parse(text); const items = j && j.data && j.data.items; if (Array.isArray(items) && items[0] && items[0].origin && items[0].destination && items[0].bids) return j; } catch (_) {}
    return null;
  }
  const X = window.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send, oSetH = X.prototype.setRequestHeader;
  X.prototype.open = function (m, u) { this.__bp = { method: String(m || 'GET').toUpperCase(), url: u, headers: {} }; return oOpen.apply(this, arguments); };
  X.prototype.setRequestHeader = function (k, v) { if (this.__bp) this.__bp.headers[k] = v; return oSetH.apply(this, arguments); };
  X.prototype.send = function (b) {
    const xhr = this;
    if (xhr.__bp && xhr.__bp.method === 'POST') {
      xhr.addEventListener('load', function () {
        try { if (isGroupsResponse(xhr.responseText)) { const fresh = !state.endpoint; state.endpoint = xhr.__bp.url; state.headers = xhr.__bp.headers; if (fresh) { LOG('captured endpoint; loading all'); ensurePanel(); loadAll(); } } } catch (_) {}
      });
    }
    return oSend.apply(this, arguments);
  };

  // ---- 2) Replay the POST to pull ALL groups (skip/take paging) -------------
  const HEADER_DENY = new Set(['cookie', 'content-length', 'host', 'connection', 'accept-encoding', 'user-agent']);
  function replayHeaders() { const out = { 'Content-Type': 'application/json', 'Accept': 'application/json' }; if (state.headers) for (const k of Object.keys(state.headers)) if (!HEADER_DENY.has(k.toLowerCase())) out[k] = state.headers[k]; return out; }
  async function postGroups(skip, take) {
    const resp = await fetch(state.endpoint, { method: 'POST', headers: replayHeaders(), body: JSON.stringify({ skip, take }), credentials: 'omit' });
    if (!resp.ok) throw new Error('groups POST ' + resp.status);
    const j = await resp.json(); return (j && j.data) || { items: [], totalRecords: 0 };
  }
  async function loadAll() {
    if (state.loading || !state.endpoint) return;
    state.loading = true; state.error = null; render();
    try {
      const PAGE = 100; const first = await postGroups(0, PAGE);
      let items = (first.items || []).slice(); const total = first.totalRecords || items.length;
      while (items.length < total) { const next = await postGroups(items.length, PAGE); if (!next.items || !next.items.length) break; items = items.concat(next.items); }
      state.groups = items; state.total = total; LOG('loaded', items.length, 'routes');
    } catch (e) { state.error = String(e && e.message || e); LOG('loadAll error', e); }
    state.loading = false; render();
  }

  // ---- helpers --------------------------------------------------------------
  const shortLoc = (n) => String(n || '').replace(/^NA-US-/, '');
  const stOf = (n) => { const p = String(n || '').split('-'); return (p[2] || '').toUpperCase(); };
  const legKey = (g) => (g.origin && g.origin.name || '') + ' → ' + (g.destination && g.destination.name || '');
  const isCT = (b) => /^ct$/i.test(String(b && b.model || '').trim());
  const isCAB = (b) => /^5YJA/i.test(String(b && b.vin || ''));   // Cybercab: VIN begins 5YJA
  const klass = (b) => isCAB(b) ? 'cab' : (isCT(b) ? 'ct' : 'std');
  function modelCell(b) {
    if (isCAB(b)) return '<span class="badge cab">CAB</span>';
    if (isCT(b)) return '<span class="badge ct">CT</span>';
    const v = String(b && b.model || '').trim();
    if (/^m.$/i.test(v)) return v.charAt(1).toUpperCase();
    return v.toUpperCase();
  }
  const fmtDate = (s) => { if (!s) return ''; const d = new Date(s); return isNaN(d) ? String(s).slice(0, 10) : d.toLocaleDateString(undefined, { month: 'short', day: '2-digit' }); };
  const dash = '<span class="noctr">—</span>';
  const esc = (s) => String(s).replace(/"/g, '&quot;');
  function geoCmp(a, b) {
    const ao = stOf(a.origin && a.origin.name), bo = stOf(b.origin && b.origin.name); if (ao !== bo) return ao.localeCompare(bo);
    const aSame = stOf(a.destination && a.destination.name) === ao, bSame = stOf(b.destination && b.destination.name) === bo; if (aSame !== bSame) return aSame ? -1 : 1;
    const ad = stOf(a.destination && a.destination.name), bd = stOf(b.destination && b.destination.name); if (ad !== bd) return ad.localeCompare(bd);
    return legKey(a).localeCompare(legKey(b));
  }
  function needByLabel(bids) { const ds = [...new Set(bids.map((b) => b.needByDate).filter(Boolean))].map((t) => new Date(t)).filter((d) => !isNaN(d)).sort((a, b) => a - b); if (!ds.length) return '—'; const a = fmtDate(ds[0]), b = fmtDate(ds[ds.length - 1]); return a === b ? a : `${a} – ${b}`; }
  const hasCounter = (bids) => bids.some((b) => b.carrierCounter && b.carrierCounter.bidAmount != null);
  function centerInPane(pane, el, smooth) { if (!pane || !el) return; const pr = pane.getBoundingClientRect(), er = el.getBoundingClientRect(); const top = pane.scrollTop + (er.top - pr.top) - (pane.clientHeight / 2 - el.clientHeight / 2); pane.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' }); }
  const existingCounter = (subset) => { const b = subset.find((x) => x.carrierCounter && x.carrierCounter.bidAmount != null); return b ? b.carrierCounter.bidAmount : null; };
  // Most common existing counter price across the subset's VINs (for the faded placeholder).
  function existingMajority(subset) { const c = {}; let best = null, bn = 0; for (const b of subset) { const a = b.carrierCounter && b.carrierCounter.bidAmount; if (a == null) continue; c[a] = (c[a] || 0) + 1; if (c[a] > bn) { bn = c[a]; best = a; } } return best; }
  // A box is "done" (skipped by Enter) only when EVERY VIN already has a counter; partials are not skipped.
  const fullyPriced = (subset) => subset.length > 0 && subset.every((b) => b.carrierCounter && b.carrierCounter.bidAmount != null);

  // --- Pickup + recommended ETA ---------------------------------------------
  // Pickup is always 16:00Z (4 PM), USD. Pickup DATE = the NEXT WEEKDAY (tomorrow, or Monday if that
  // falls on a weekend). Pickup is never Sat/Sun (the ETA may be); there is no time-of-day cutoff.
  const isWeekend = (d) => d.getDay() === 0 || d.getDay() === 6;   // 0 = Sun, 6 = Sat
  // Advance `n` BUSINESS days from `d` (n may be negative), skipping Sat/Sun — weekends never count
  // toward n, and the result is always a weekday when `d` is a weekday.
  function addBusinessDays(d, n) { const x = new Date(d); const step = n < 0 ? -1 : 1; let rem = Math.abs(n); while (rem > 0) { x.setDate(x.getDate() + step); if (!isWeekend(x)) rem--; } return x; }
  function pickupDate() { const d = new Date(); d.setHours(0, 0, 0, 0); return addBusinessDays(d, 1); }   // next weekday
  const iso16 = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T16:00:00.000Z`;
  // transitDays scales with origin->destination distance
  // (US state centroids): <500mi:7  500-1000:9  1000-2000:11  >=2000:12  (intra-state -> 7).
  // Longer hauls lean a day later than the raw average (they ran ~1 day short vs actual bids).
  const STC = {AL:[32.8,-86.8],AZ:[34.3,-111.7],AR:[34.9,-92.4],CA:[37.2,-119.3],CO:[39.0,-105.5],CT:[41.6,-72.7],DE:[39.0,-75.5],FL:[28.6,-82.4],GA:[32.6,-83.4],ID:[44.2,-114.5],IL:[40.0,-89.2],IN:[39.9,-86.3],IA:[42.0,-93.5],KS:[38.5,-98.4],KY:[37.5,-85.3],LA:[31.0,-92.0],ME:[45.4,-69.2],MD:[39.0,-76.8],MA:[42.3,-71.8],MI:[44.3,-85.4],MN:[46.3,-94.3],MS:[32.7,-89.7],MO:[38.4,-92.5],MT:[47.0,-109.6],NE:[41.5,-99.8],NV:[39.3,-116.6],NH:[43.7,-71.6],NJ:[40.2,-74.7],NM:[34.4,-106.1],NY:[42.9,-75.5],NC:[35.6,-79.4],ND:[47.5,-100.3],OH:[40.3,-82.8],OK:[35.6,-97.5],OR:[43.9,-120.6],PA:[40.9,-77.8],RI:[41.7,-71.6],SC:[33.9,-80.9],SD:[44.4,-100.2],TN:[35.9,-86.4],TX:[31.5,-99.3],UT:[39.3,-111.7],VT:[44.1,-72.7],VA:[37.5,-78.9],WA:[47.4,-120.5],WV:[38.6,-80.6],WI:[44.6,-89.9],WY:[43.0,-107.6]};
  function milesBetween(a, b) { if (!a || !b) return null; const R = 3959, dLat = (b[0]-a[0])*Math.PI/180, dLon = (b[1]-a[1])*Math.PI/180, la1 = a[0]*Math.PI/180, la2 = b[0]*Math.PI/180; const h = Math.sin(dLat/2)**2 + Math.cos(la1)*Math.cos(la2)*Math.sin(dLon/2)**2; return 2*R*Math.asin(Math.sqrt(h)); }
  function transitDays(g) { const d = milesBetween(STC[stOf(g.origin && g.origin.name)], STC[stOf(g.destination && g.destination.name)]); if (d == null) return 7; if (d < 500) return 7; if (d < 1000) return 9; if (d < 2000) return 11; return 12; }
  // ETA = pickup + transitDays CALENDAR days (pickup day NOT counted); Sat/Sun count toward transit,
  // so the ETA may land on a weekend. The manual stepper offset is likewise in calendar days.
  // (Pickup itself is still forced to a weekday — see pickupDate.)
  function recommendedEta(g) { const t = pickupDate(); t.setDate(t.getDate() + transitDays(g)); return t; }
  function selectedEta(g) { const t = recommendedEta(g); t.setDate(t.getDate() + (state.dates[legKey(g)] || 0)); return t; }
  // Stepper picker: the CENTER box is always the selected date (highlighted); the flanking days step
  // the selection one day earlier/later and become the new center, so any date is reachable.
  function dateBoxesFromBase(base, off) {
    const sel = new Date(base); sel.setDate(base.getDate() + off);   // selected = recommended + off CALENDAR days
    const before = new Date(sel); before.setDate(sel.getDate() - 1); // flanks are the adjacent calendar days
    const after = new Date(sel); after.setDate(sel.getDate() + 1);
    return `<button class="dbox flank" data-dir="-1">${before.getDate()}</button>`
      + `<button class="dbox sel" data-dir="0">${sel.getDate()}</button>`
      + `<button class="dbox flank" data-dir="1">${after.getDate()}</button>`;
  }
  function dateSelector(g) {
    const base = recommendedEta(g), off = state.dates[legKey(g)] || 0;
    return `<div class="datesel" data-leg="${esc(legKey(g))}" data-base="${base.getTime()}">${dateBoxesFromBase(base, off)}</div>`;
  }

  // --- Bid submission (the captured UpdateOffer write) -----------------------
  // POST {base}/BidBoard/{bidId}/UpdateOffer  {CurrencyCode, BidAmount, EstimatedShipDate, NeededByDate, OfferExpiryDate}
  // verb: MakeOffer for a VIN with no offer yet, UpdateOffer to change an existing one (same id + payload).
  const writeUrl = (bidId, verb) => state.endpoint.replace(/groups(\?.*)?$/i, '') + bidId + '/' + verb;
  const MIN_BID = 50;
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
  function bidsForKey(key) {
    const sep = key.lastIndexOf('|'), leg = key.slice(0, sep), variant = key.slice(sep + 1);
    const g = state.groups.find((x) => legKey(x) === leg); if (!g) return { g: null, vins: [] };
    const all = (g.bids && g.bids.items) || [];
    const vins = variant === 'cab' ? all.filter(isCAB) : variant === 'ct' ? all.filter((b) => !isCAB(b) && isCT(b)) : all.filter((b) => !isCAB(b) && !isCT(b));
    return { g, vins };
  }
  async function postOffer(bidId, verb, body) {
    const resp = await fetch(writeUrl(bidId, verb), { method: 'POST', headers: Object.assign(replayHeaders(), { 'Content-Type': 'application/json' }), body: JSON.stringify(body), credentials: 'omit' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const j = await resp.json().catch(() => ({}));
    if (j && j.success === false) throw new Error('Tesla returned success:false'); // 200 but logically rejected
    return j;
  }
  // Send every TYPED box in a card; each box's price -> every VIN in its subset. Pickup/ETA per the rules.
  async function submitCard(cardEl) {
    if (!validateCardBids(cardEl, true)) return false;
    const inputs = typedPriceInputs(cardEl);
    if (!inputs.length) return;
    const oldTag = cardEl.querySelector('.subtag'); if (oldTag) oldTag.remove();
    cardEl.classList.remove('submitted', 'submit-err', 'bid-invalid'); cardEl.classList.add('sending');
    let sent = 0, failed = 0;
    for (const inp of inputs) {
      const { g, vins } = bidsForKey(inp.dataset.key); if (!g) continue;
      const body = { CurrencyCode: 'USD', BidAmount: String(bidValue(inp.value)), EstimatedShipDate: iso16(pickupDate()), NeededByDate: iso16(selectedEta(g)), OfferExpiryDate: null };
      for (const b of vins) { const verb = (b.carrierCounter && b.carrierCounter.bidAmount != null) ? 'UpdateOffer' : 'MakeOffer'; try { await postOffer(b.bidId, verb, body); sent++; } catch (e) { failed++; console.warn('[bidpanel] bid FAILED for', b.vin, '—', e && e.message); } }
    }
    cardEl.classList.remove('sending'); cardEl.classList.add(failed ? 'submit-err' : 'submitted');
    if (failed) {
      const tag = document.createElement('div'); tag.className = 'subtag'; cardEl.appendChild(tag);
      tag.textContent = `⚠ ${failed} offer${failed === 1 ? '' : 's'} failed`;
    }
    return failed === 0;
  }

  // ---- 3) Panel -------------------------------------------------------------
  let host, root, body, rafPending = 0, toastTimer = 0;
  let leftSelectionLockRi = null, leftSelectionUnlockTimer = 0;
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
    host.id = 'bidpanel-host';
    // Placement is applied by setupNav()/applyPlacement(): the panel is spliced INTO Tesla's own
    // layout as an in-flow element that replaces the board (embed), so it reads as part of the page.
    // If the container can't be found, it falls back to a fixed overlay docked to the content area.
    host.style.cssText = 'z-index:2147483647;';
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        *{box-sizing:border-box;font-family:Inter,system-ui,Arial,sans-serif}
        .panel{position:relative;display:flex;flex-direction:column;height:100%;background:#fff;color:#171a20;border:0;border-radius:0;box-shadow:none;overflow:hidden}
        .toast{position:absolute;left:50%;bottom:18px;transform:translateX(-50%) translateY(8px);background:#171a20;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;font-weight:800;letter-spacing:.02em;box-shadow:0 6px 20px rgba(0,0,0,.25);opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:6}
        .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
        .tools{display:flex;flex-direction:column;gap:8px;padding:8px 12px;border-bottom:1px solid #eee}
        /* trow spans the left 50% (symmetric 12px padding cancels out), so the filter's right edge lands on the middle divider */
        .trow{display:flex;align-items:center;gap:10px;width:50%}
        .tools input{flex:1;min-width:0;padding:6px 8px;border:1px solid #d0d3d6;border-radius:6px;font-size:13px}
        .tools label{font-size:12px;display:flex;align-items:center;gap:4px;white-space:nowrap;color:#5c5e62}
        .todobtn{background:#e6e8ea;color:#3a3f49;border:1px solid #cfd3d7;border-radius:6px;padding:6px 0;min-width:82px;text-align:center;font-size:12px;font-weight:700;cursor:pointer;font-family:Arial,Helvetica,sans-serif;letter-spacing:.04em}
        .todobtn:hover{background:#dcdfe2}
        .todobtn.on{background:#3457d5;color:#fff;border-color:#3457d5}
        .fcard.sending{border-color:#3457d5;opacity:.85}
        .fcard.submitted,.fcard.submitted.active{border-color:#0a7d33;box-shadow:0 0 0 2px rgba(10,125,51,.18)}
        .fcard.submit-err,.fcard.submit-err.active{border-color:#c0392b;box-shadow:0 0 0 2px rgba(192,57,43,.18)}
        .fcard.bid-invalid,.fcard.bid-invalid.active{border-color:#c0392b;background:#fff7f6;box-shadow:0 0 0 3px rgba(192,57,43,.22)}
        .subtag{margin-top:10px;font-size:12px;font-weight:800;color:#0a7d33}
        .fcard.submit-err .subtag{color:#c0392b}
        .bodywrap{display:flex;flex-direction:row-reverse;flex:1;overflow:hidden}
        .left{width:50%;overflow:auto;padding:6px}
        .right{width:50%;overflow:auto;padding:43vh 18px;background:#f6f7f9;border-right:1px solid #e6e8ea}
        .grp{border:1px solid #eceef0;border-radius:8px;margin:6px 4px;overflow:hidden;cursor:pointer}
        .grp:hover{background:#fafbfc}
        .grp.sel{border-color:#3457d5;box-shadow:0 0 0 3px rgba(52,87,213,.18)}.grp.sel>.row{background:#eaf0ff}
        .grp>.row{display:flex;align-items:center;gap:6px;padding:8px 10px;cursor:pointer;background:#fafbfc}
        .grp>.row:hover{background:#f1f3f5}
        .caret{background:none;border:0;color:#9a9da1;cursor:pointer;font-size:12px;width:14px;padding:0}
        .leg{flex:1;font-size:13px;font-weight:600;line-height:1.3}
        .cnt{font-size:12px;font-weight:700;color:#3457d5;background:#eaf0ff;border-radius:10px;padding:2px 8px;white-space:nowrap;font-family:Arial,Helvetica,sans-serif}
        .vins{padding:2px 8px 8px}
        table{width:100%;border-collapse:collapse;font-size:12px}
        th,td{text-align:left;padding:3px 6px;border-bottom:1px solid #f0f1f3;white-space:nowrap}
        th{color:#9a9da1;font-weight:600}td.vin{font-family:ui-monospace,Menlo,Consolas,monospace}
        .ctr{color:#0a7d33;font-weight:700}.noctr{color:#b0b3b7}td.model{font-weight:700}
        .badge{display:inline-block;color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:700;letter-spacing:.5px}
        .badge.ct{background:rgba(120,135,160,.18);color:#566072;border:2px solid #000;border-radius:3px;padding:1px 6px}  /* CT shading = shipment-creator .vbub.ct */
        .badge.cab{background:rgba(212,170,60,.22);color:#8a6d14;border:2px solid #000;border-radius:3px;padding:1px 6px} /* CAB shading = shipment-creator .vbub.cc */
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
        .price-row{display:flex;gap:18px;align-items:flex-end}
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
        /* text-shimmer (loading-ui style): a light band sweeps across the letters */
        .empty.shimmer{font-size:29px;font-weight:800;color:transparent;background:linear-gradient(90deg,#c2c5c9 0%,#c2c5c9 40%,#3a3f49 50%,#c2c5c9 60%,#c2c5c9 100%);background-size:200% auto;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;animation:bpshimmer 1.6s linear infinite}
        @keyframes bpshimmer{0%{background-position:200% center}100%{background-position:-200% center}}
      </style>
      <div class="panel">
        <div class="tools"><div class="trow"><button id="todo" class="todobtn">ALL</button><input id="filter" placeholder="Filter…" /></div></div>
        <div class="bodywrap"><div class="left" id="left"></div><div class="right" id="right"></div></div>
      </div>`;
    document.documentElement.appendChild(host);

    // No header bar; `sub` is a throwaway node so render()'s status writes stay harmless.
    body = { sub: document.createElement('span'), left: root.getElementById('left'), right: root.getElementById('right'), filter: root.getElementById('filter') };
    body.filter.addEventListener('input', () => { state.filter = body.filter.value.trim().toLowerCase(); render(); });
    root.getElementById('todo').addEventListener('click', (e) => { state.todoOnly = !state.todoOnly; e.target.textContent = state.todoOnly ? 'TO-DO' : 'ALL'; e.target.classList.toggle('on', state.todoOnly); render(); });
    body.right.addEventListener('scroll', () => {
      // A left-card click locks that selection while the right pane smooth-scrolls past intermediate cards.
      // Normal user scrolling has no lock, so the left pane continues following the focused card.
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
    // Enter -> smooth-scroll to the next UNPRICED box (skip routes that already have a price)
    body.right.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || !(e.target.classList && e.target.classList.contains('price'))) return;
      e.preventDefault();
      const curCard = e.target.closest('.fcard');
      // Never send or advance while any typed price on this card is below the minimum.
      if (curCard && !validateCardBids(curCard, true)) return;
      const inputs = [...body.right.querySelectorAll('.price')];
      let i = inputs.indexOf(e.target) + 1;
      while (i < inputs.length && inputs[i].dataset.priced === '1') i++;   // skip already-priced shipments
      const next = inputs[i];
      const nextCard = next ? next.closest('.fcard') : null;
      // Finishing a card (Enter moves to a different card, or the end) sends that card's bids — always live.
      if (curCard && nextCard !== curCard) submitCard(curCard);
      if (next) { if (nextCard) centerInPane(body.right, nextCard, true); next.focus({ preventScroll: true }); if (next.select) next.select(); }
      else { e.target.blur(); showToast('Bids Finished'); }   // reached the end of the list
    });
    window.addEventListener('resize', applyPlacement);
    setupNav();
  }

  // ---- Placement: embed into Tesla's own layout, or fall back to a fixed overlay --------------
  const PANEL_GAP = 10;
  let hiddenEl = null, observedParent = null, mo = null, moScheduled = false;

  // The board content is the tall, wide sibling of the left nav inside its parent (`.main`).
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
    const boxW = cs.width, boxH = cs.height;                 // measured BEFORE hiding (for non-flex parents)
    if (hiddenEl && hiddenEl !== content && hiddenEl.style) hiddenEl.style.display = '';   // release a stale one
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

  // Angular re-renders the region; a light observer re-splices us in whenever it does.
  function ensureObserver(parent) {
    if (mo && observedParent === parent) return;
    if (mo) mo.disconnect();
    observedParent = parent;
    mo = new MutationObserver(() => {
      if (!/\/logistics\/bidboard2/i.test(location.pathname)) return;
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

  // Prefer embedding; if the container isn't there, use the overlay so the panel never vanishes.
  function applyPlacement() {
    if (!host) return;
    if (embed()) return;
    state.embedded = false;
    dock();
  }

  // Show the panel only on the bid board; hide it (and give Tesla its board back) elsewhere.
  function setupNav() {
    const onBidBoard = () => /\/logistics\/bidboard2/i.test(location.pathname);
    const apply = () => {
      if (!host) return;
      if (onBidBoard()) { applyPlacement(); host.style.display = ''; }
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
    setInterval(() => { if (location.href !== last) { last = location.href; apply(); } }, 300);   // fallback for routers that bypass pushState
    setInterval(() => { if (host && host.style.display !== 'none') applyPlacement(); }, 500);   // re-splice / re-size after Angular re-renders or nav collapse
    setInterval(() => { const el = root && root.querySelector('.empty.clock'); if (el) el.textContent = nowHHMM(); }, 10000);   // keep the "nothing to bid" clock current
    apply();
  }

  // ---- 4) Render ------------------------------------------------------------
  function currentGroups() {
    let groups = state.groups.slice();
    if (state.filter) { const f = state.filter; groups = groups.filter((g) => legKey(g).toLowerCase().includes(f) || ((g.bids && g.bids.items) || []).some((b) => (b.vin || '').toLowerCase().includes(f) || (b.model || '').toLowerCase().includes(f))); }
    if (state.todoOnly) groups = groups.filter((g) => ((g.bids && g.bids.items) || []).some((b) => !(b.carrierCounter && b.carrierCounter.bidAmount != null)));   // TO-DO = has an un-priced VIN
    groups.sort(state.sortByCount ? (a, b) => ((b.bids && b.bids.totalRecords) || 0) - ((a.bids && a.bids.totalRecords) || 0) : geoCmp);
    return groups;
  }

  function priceBox(key, variant, subset) {
    const k = key + '|' + variant;
    const local = state.prices[k] != null ? state.prices[k] : '';
    const maj = existingMajority(subset);                 // majority existing price -> faded placeholder
    const ph = maj != null ? String(maj) : '';
    const done = fullyPriced(subset);                     // skip on Enter only when ALL VINs are already priced
    const cap = variant === 'ct'
      ? `<span class="badge ct">CT</span> <span class="num">${subset.length}</span>`
      : variant === 'cab'
      ? `<span class="badge cab">CAB</span> <span class="num">${subset.length}</span>`
      : `<span class="num">${subset.length}</span> VIN${subset.length === 1 ? '' : 's'}`;
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

  function render() {
    if (!root) return;
    body.left.classList.remove('center-empty'); body.right.classList.remove('center-empty');
    const totalVins = state.groups.reduce((s, g) => s + ((g.bids && g.bids.totalRecords) || 0), 0);
    body.sub.textContent = state.loading ? 'loading…' : state.error ? '' : `· ${state.groups.length} routes · ${totalVins} VINs`;

    if (state.error) { body.left.innerHTML = `<div class="empty err">Error: ${state.error}</div>`; body.right.innerHTML = ''; return; }
    if (state.loading && !state.groups.length) {
      // Panes are flipped (row-reverse): body.right renders LEFT. Put the shimmer center-left,
      // matching the "Nothing to Bid" position.
      body.left.classList.add('center-empty'); body.right.classList.add('center-empty');
      body.right.innerHTML = `<div class="empty shimmer">Scanning…</div>`; body.left.innerHTML = '';
      return;
    }

    const gs = currentGroups();
    if (!gs.length) {
      if (state.todoOnly && !state.filter) {
        // Genuinely nothing left to bid: center "Nothing to Bid" on the left, show the current time
        // (green) on the right, and drop the panes' scroll/padding.
        body.left.classList.add('center-empty'); body.right.classList.add('center-empty');
        // Panes are visually flipped (row-reverse): body.right renders LEFT, body.left renders RIGHT.
        // So "Nothing to Bid" -> body.right (left), the clock -> body.left (right).
        body.right.innerHTML = `<div class="empty done">✓ Nothing to Bid</div>`;
        body.left.innerHTML = `<div class="empty done clock">${nowHHMM()}</div>`;
        showToast('Nothing to Bid');
      } else {
        // A filter (in TO-DO or ALL) that matched nothing, or no routes captured yet — default message.
        body.left.innerHTML = `<div class="empty">No routes${state.filter ? ' match the filter' : ' captured yet'}.</div>`;
        body.right.innerHTML = '';
      }
      return;
    }

    // LEFT
    const lf = document.createDocumentFragment();
    gs.forEach((g, ri) => {
      const vins = (g.bids && g.bids.items) || [];
      const cnt = (g.bids && g.bids.totalRecords) || vins.length;
      const grp = document.createElement('div'); grp.className = 'grp'; grp.dataset.ri = ri;
      grp.addEventListener('click', () => {
        const fc = body.right.querySelector(`.fcard[data-ri="${ri}"]`);
        leftSelectionLockRi = ri;
        armLeftSelectionUnlock();
        selectRi(ri, false);                 // lock the clicked left card in place
        if (fc) centerInPane(body.right, fc, true); // only the focused-card pane moves
      });   // whole card selects
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = `<div class="leg"><span class="o">${shortLoc(g.origin && g.origin.name)}</span><span class="arrow">→</span><span class="d">${shortLoc(g.destination && g.destination.name)}</span></div>`
        + `<div class="cnt">${cnt} VIN${cnt === 1 ? '' : 's'}</div>`;
      grp.appendChild(row);
      if (vins.length) {
        const wrap = document.createElement('div'); wrap.className = 'vins';
        const rows = vins.map((b) => {
          const ctr = b.carrierCounter && (b.carrierCounter.bidAmount != null) ? `<span class="ctr">${b.carrierCounter.bidAmount} ${b.carrierCounter.currencyCode || ''}</span>` : dash;
          const cc = b.carrierCounter || {};
          const mp = cc.estimatedShipDate ? fmtDate(cc.estimatedShipDate) : dash;   // date only (time removed)
          const me = cc.neededByDate ? fmtDate(cc.neededByDate) : dash;             // date only (time removed)
          return `<tr><td class="vin">${b.vin || ''}</td><td class="model">${modelCell(b)}</td><td>${fmtDate(b.needByDate)}</td><td>${ctr}</td><td>${mp}</td><td>${me}</td></tr>`;
        }).join('');
        wrap.innerHTML = `<table><thead><tr><th>VIN</th><th>Model</th><th>Need by</th><th>My counter</th><th>Pickup</th><th>ETA</th></tr></thead><tbody>${rows}</tbody></table>`;
        grp.appendChild(wrap);
      }
      lf.appendChild(grp);
    });
    body.left.innerHTML = ''; body.left.appendChild(lf);

    // RIGHT — one card per route; CT and CAB (Cybercab) each get their own price box
    const rf = document.createDocumentFragment();
    gs.forEach((g, ri) => {
      const key = legKey(g), vins = (g.bids && g.bids.items) || [];
      const cab = vins.filter(isCAB);
      const ct = vins.filter((b) => !isCAB(b) && isCT(b));
      const std = vins.filter((b) => !isCAB(b) && !isCT(b));
      const O = shortLoc(g.origin && g.origin.name), D = shortLoc(g.destination && g.destination.name);
      const card = document.createElement('div'); card.className = 'fcard'; card.dataset.ri = ri;
      let boxes = '';
      if (std.length) boxes += priceBox(key, 'std', std);
      if (ct.length) boxes += priceBox(key, 'ct', ct);
      if (cab.length) boxes += priceBox(key, 'cab', cab);
      card.innerHTML = `<div class="froute"><span>${O}</span><span class="arrow">→</span><span>${D}</span></div>`
        + `<div class="fmeta"><div class="fneed">Need by <b>${needByLabel(vins)}</b></div></div>`
        + dateSelector(g)
        + `<div class="price-row">${boxes}</div>`;
      rf.appendChild(card);
    });
    body.right.innerHTML = ''; body.right.appendChild(rf);

    const firstCard = body.right.querySelector('.fcard');
    if (firstCard) centerInPane(body.right, firstCard);   // keep cards on screen after a filter/sort/TO-DO change (esp. short lists)
    syncFromRight();
  }

  function syncFromRight() {
    if (!body) return;
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

  window.__bidpanelState = state; window.__bidpanelRender = render; window.__bidpanelLoadAll = loadAll;

  if (document.documentElement) ensurePanel();
  else document.addEventListener('readystatechange', ensurePanel, { once: true });
  LOG('installed (read-only). Waiting for bid-board data…');
})();
