// ==UserScript==
// @name         Tesla Regular Fleet — SuperDispatch Recolor
// @namespace    wastake.regularfleet
// @version      0.18.0
// @description  Shades Regular Fleet rows by SuperDispatch delivery status. Pulls every VIN off the page, looks each up in SuperDispatch (your own API creds), and matches by DELIVERY DATE: if any of the VIN's SD orders was delivered within 3 days of the Tesla delivery date -> green; else picked up/accepted/pending -> yellow; else (incl. no match) -> red. Results cached per day. Hover a VIN to see its SuperDispatch order card(s).
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/regular-fleet/tesla-regular-fleet-recolor.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/regular-fleet/tesla-regular-fleet-recolor.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// @connect      api.shipper.superdispatch.com
// ==/UserScript==

/*
 * WHAT IT DOES
 *   On the Regular Fleet tab (…/logistics/invoicing/regular-fleet) it captures the page's own
 *   data response (all VINs + Tesla shipment numbers) by hooking XHR at document-start, then for
 *   each VIN calls SuperDispatch's find_by_vin. The match rule (see below) decides the row color.
 *
 * MATCH RULE (by delivery date)
 *   find_by_vin returns every SD order the VIN has been on. For each, we read the ACTUAL
 *   delivery date (order.delivery.completed_at). If ANY of them is within 3 calendar days of
 *   the Tesla row's delivery date, the VIN is confirmed delivered -> GREEN. The VIN itself is
 *   always the join key (find_by_vin queries that exact VIN), so "VIN still has to match".
 *
 * COLOR RULE
 *   any SD order delivered within 3 days of the Tesla delivery date        -> GREEN
 *   else, VIN has an SD order that is picked_up / accepted / pending        -> YELLOW
 *   else (no order, delivered on a different date, other status)            -> RED
 *
 * CREDENTIALS
 *   SuperDispatch's API is OAuth client-credentials: a Client ID + Client Secret. They are asked
 *   for once and stored LOCALLY in Tampermonkey (GM storage) — never in the script, never pushed
 *   to GitHub. Use the Tampermonkey menu ▸ "Set SuperDispatch credentials" to enter/rotate them.
 *
 * CACHE
 *   Results are cached per calendar day (GM storage). GREEN (delivered) is terminal -> served from
 *   cache and never re-queried. YELLOW/RED are re-checked on EVERY pass — each page refresh, portal
 *   Apply/filter, or pagination that reloads the fleet data — since their status can still change.
 *   New VINs are always checked. At the next day the cache resets. Menu ▸ "Re-scan now" clears
 *   today's cache and re-checks immediately. The cache also holds a compact "card" per SD order.
 *
 * HOVER CARD
 *   Hovering a VIN cell pops up ONE SuperDispatch order card (the order matching the delivery-date
 *   window, else the in-transit / first one) to the top-right of the VIN: order #, status pill,
 *   pickup->delivery route, the hovered VIN's vehicle ("+N more" for others on the order), and the
 *   PER-UNIT CARRIER COST (order price divided by the number of VINs, rounded to a whole dollar).
 *   Over-long venue names are capped. Read from the cache -> instant, no API call on hover.
 */

(function () {
  'use strict';

  const LOG = (...a) => console.log('%c[fleet-recolor]', 'color:#0a7;font-weight:bold', ...a);

  const SD_BASE = 'https://api.shipper.superdispatch.com';
  const CONCURRENCY = 4;          // parallel SD lookups
  const REQ_GAP_MS = 120;         // small pause between a worker's calls (be nice to the API)
  const DELIVERY_WINDOW_DAYS = 3; // SD delivery date must be within this many days of Tesla's
  const CACHE_VERSION = 2;        // bump whenever the cached hover-card shape changes

  const COLORS = {
    green:  'rgba(35, 160, 70, 0.28)',
    yellow: 'rgba(230, 185, 30, 0.34)',
    red:    'rgba(220, 55, 55, 0.26)',
  };

  const state = { rows: {}, checking: false, checkTimer: null };

  // ============================ small helpers ==============================
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isFleetPage = () => location.pathname.toLowerCase().indexOf('regular-fleet') !== -1;

  function today() {
    const d = new Date();
    return d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
  }

  // Parse an SD timestamp ('2026-06-28T11:10:49.000+0000' / 'Z' / '+00:00') OR a plain
  // Tesla date, to epoch ms; null if unparseable.
  function parseDate(s) {
    s = String(s || '').trim();
    if (!s) return null;
    const t = s.replace('Z', '+00:00').replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
    let ms = Date.parse(t);
    if (isNaN(ms)) ms = Date.parse(s);
    return isNaN(ms) ? null : ms;
  }
  // Whole-calendar-day distance between two epoch-ms values (floored in UTC — a timezone
  // shift is far inside the 3-day window, so it can't flip a real match).
  function dayDiff(aMs, bMs) {
    const a = new Date(aMs), b = new Date(bMs);
    const ua = Date.UTC(a.getUTCFullYear(), a.getUTCMonth(), a.getUTCDate());
    const ub = Date.UTC(b.getUTCFullYear(), b.getUTCMonth(), b.getUTCDate());
    return Math.abs(Math.round((ua - ub) / 86400000));
  }
  // Statuses that shade YELLOW (in-transit / not-yet-delivered but active).
  const YELLOW_STATUSES = new Set(['picked_up', 'pickedup', 'accepted', 'pending']);
  function normStatus(st) {
    return String(st || '').toLowerCase().trim().replace(/\s+/g, '_');
  }
  function isYellowStatus(st) {
    return YELLOW_STATUSES.has(normStatus(st));
  }
  // Actual delivery date of an SD order: delivery.completed_at, with sensible fallbacks.
  function sdDeliveryDate(o) {
    const del = o && o.delivery;
    return (del && (del.completed_at || del.actual_delivery_date || del.delivered_at)) ||
      o && o.delivered_at || null;
  }

  // ---- hover-card field helpers ----
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtDate(s) {
    const ms = parseDate(s); if (ms == null) return '';
    const d = new Date(ms); return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
  }
  function titleCase(s) {
    return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }
  function cityLine(v) {
    v = v || {};
    const cs = [v.city, v.state].filter(Boolean).join(', ');
    return ([cs, v.zip].filter(Boolean).join(' ').trim()) || (v.name || '');
  }
  function stopDate(stop) {
    stop = stop || {};
    return stop.completed_at || stop.scheduled_at || stop.scheduled_ends_at || '';
  }
  const GREEN_PILL = new Set(['delivered', 'paid', 'invoiced', 'completed', 'archived']);
  function pillClass(status) {
    const n = normStatus(status);
    if (GREEN_PILL.has(n)) return 'green';
    if (YELLOW_STATUSES.has(n)) return 'yellow';
    return 'gray';
  }

  function perUnitCost(order) {
    const price = Number(order && order.price);
    const units = order && Array.isArray(order.vehicles) ? order.vehicles.length : 0;
    if (!Number.isFinite(price) || order.price == null || units < 1) return '';
    return '$' + Math.round(price / units);
  }

  // Compact display record for one SD order (what the hover panel renders).
  function makeCard(o) {
    o = o || {};
    const pv = (o.pickup && o.pickup.venue) || {};
    const dv = (o.delivery && o.delivery.venue) || {};
    return {
      number: o.number || o.order_number || '',
      status: normStatus(o.status),
      unitCost: perUnitCost(o),
      pickup: { line: cityLine(pv), name: pv.name || '', date: fmtDate(stopDate(o.pickup)) },
      delivery: {
        line: cityLine(dv), name: dv.name || '',
        date: fmtDate((o.delivery && o.delivery.completed_at) || stopDate(o.delivery)),
      },
      vehicles: (o.vehicles || []).map((v) => ({
        vin: String(v.vin || '').toUpperCase(),
        label: [v.year, v.make, v.model].filter(Boolean).join(' '),
      })),
    };
  }

  // ============================ credentials ================================
  function getCreds() {
    const c = GM_getValue('sd_creds', null);
    return (c && c.id && c.secret) ? c : null;
  }
  function promptCreds() {
    const cur = getCreds() || {};
    const id = prompt('SuperDispatch API — Client ID:', cur.id || '');
    if (id === null) return false;
    const secret = prompt('SuperDispatch API — Client Secret:\n(stored locally in Tampermonkey, never uploaded)', '');
    if (secret === null) return false;
    if (!id.trim() || !secret.trim()) { toast('Credentials not saved (blank field)'); return false; }
    GM_setValue('sd_creds', { id: id.trim(), secret: secret.trim() });
    GM_deleteValue('sd_token');            // force re-auth with the new creds
    toast('SuperDispatch credentials saved');
    return true;
  }
  function ensureCreds() {
    if (getCreds()) return true;
    toast('Enter your SuperDispatch API credentials to begin');
    return promptCreds();
  }

  // ============================ SD HTTP ====================================
  function gmFetch(opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: opts.method, url: opts.url, headers: opts.headers, data: opts.data,
        timeout: 30000,
        onload: (r) => resolve(r),
        onerror: (e) => reject(new Error('network error: ' + ((e && e.error) || 'unknown'))),
        ontimeout: () => reject(new Error('timeout')),
      });
    });
  }

  async function getToken(force) {
    if (!force) {
      const cached = GM_getValue('sd_token', null);
      if (cached && cached.token && cached.exp > Date.now() + 30000) return cached.token;
    }
    const creds = getCreds();
    if (!creds) throw new Error('No SuperDispatch credentials set');
    const basic = btoa(creds.id + ':' + creds.secret);
    const r = await gmFetch({
      method: 'POST',
      url: SD_BASE + '/oauth/token?grant_type=client_credentials',
      headers: { 'Authorization': 'Basic ' + basic },
    });
    if (r.status !== 200) throw new Error('SD auth failed ' + r.status + ': ' + String(r.responseText || '').slice(0, 200));
    const j = JSON.parse(r.responseText);
    const token = j.access_token;
    const exp = Date.now() + Math.max(60, (parseInt(j.expires_in, 10) || 3600) - 300) * 1000;
    GM_setValue('sd_token', { token, exp });
    return token;
  }

  // SD wraps collections as {data:{objects:[...]}} and single resources as {data:{object:{...}}}.
  function unwrapObjects(resp) {
    if (Array.isArray(resp)) return resp;
    if (resp && typeof resp === 'object') {
      const data = resp.data;
      if (data && typeof data === 'object') {
        for (const k of ['objects', 'results', 'orders']) if (Array.isArray(data[k])) return data[k];
      }
      for (const k of ['objects', 'results', 'orders']) if (Array.isArray(resp[k])) return resp[k];
    }
    return [];
  }
  function unwrapObject(resp) {
    if (resp && typeof resp === 'object') {
      const data = resp.data;
      if (data && typeof data === 'object' && 'object' in data) return data.object || {};
    }
    return resp || {};
  }

  async function sdGet(path, retry) {
    const token = await getToken(retry === 'reauth');
    const r = await gmFetch({
      method: 'GET', url: SD_BASE + path,
      headers: { 'Authorization': 'Bearer ' + token, 'Accept': 'application/json' },
    });
    if (r.status === 401 && retry !== 'reauth') return sdGet(path, 'reauth');
    if (r.status === 404) return { _404: true };
    if (r.status !== 200) throw new Error('GET ' + path + ' -> ' + r.status + ': ' + String(r.responseText || '').slice(0, 160));
    return JSON.parse(r.responseText || '{}');
  }

  async function findByVin(vin) {
    const j = await sdGet('/v1/public/orders/find_by_vin/' + encodeURIComponent(vin));
    if (j._404) return [];
    return unwrapObjects(j);
  }
  async function getOrder(guid) {
    const j = await sdGet('/v1/public/orders/' + encodeURIComponent(guid));
    if (j._404) return {};
    return unwrapObject(j);
  }

  // Full order object (find_by_vin summaries can be thin — fetch the detail for the hover card).
  async function fullOrder(o) {
    const detailed = o && o.pickup && o.delivery && Array.isArray(o.vehicles) && o.price != null;
    if (detailed) return o;
    if (o && o.guid) { try { return await getOrder(o.guid); } catch (_) {} }
    return o || {};
  }

  // Decide a VIN's color by DELIVERY DATE and build the hover-card list from its SD orders.
  // Returns {color, status, deliveredAt, matched, cards} or {error} (leave uncached -> retry).
  async function evaluateVin(vin, teslaDeliveryDate) {
    const tdd = parseDate(teslaDeliveryDate);
    let orders;
    try { orders = await findByVin(vin); }
    catch (e) { return { error: String((e && e.message) || e) }; }
    if (!orders.length) return { color: 'red', status: '', deliveredAt: '', matched: false, card: null };

    let green = null, greenCard = null, yellowStatus = '', yellowCard = null, firstCard = null;
    for (const o0 of orders) {
      const o = await fullOrder(o0);
      const c = makeCard(o);
      if (!firstCard) firstCard = c;
      // GREEN: this order was actually delivered within the window of the Tesla delivery date.
      if (!green && tdd != null) {
        const dd = parseDate(sdDeliveryDate(o));
        if (dd != null && dayDiff(dd, tdd) <= DELIVERY_WINDOW_DAYS) {
          green = { status: String(o.status || 'delivered'), deliveredAt: String(sdDeliveryDate(o) || '') };
          greenCard = c;
        }
      }
      if (!yellowStatus && isYellowStatus(o.status)) { yellowStatus = normStatus(o.status); yellowCard = c; }
    }
    // Show only the ONE relevant order: the date-matched (green) one, else in-transit (yellow), else the first.
    const card = greenCard || yellowCard || firstCard || null;
    if (green) return { color: 'green', status: green.status, deliveredAt: green.deliveredAt, matched: true, card };
    if (yellowStatus) return { color: 'yellow', status: yellowStatus, deliveredAt: '', matched: true, card };
    return { color: 'red', status: '', deliveredAt: '', matched: false, card };
  }

  // ============================ daily cache ================================
  function loadCache() {
    let c = GM_getValue('sd_cache', null);
    if (!c || c.day !== today() || c.version !== CACHE_VERSION) {
      c = { version: CACHE_VERSION, day: today(), vins: {} };
      GM_setValue('sd_cache', c);
    }
    return c;
  }
  function saveCache(c) { c.version = CACHE_VERSION; c.day = today(); GM_setValue('sd_cache', c); }

  // ============================ the check pass =============================
  function scheduleCheck() {
    if (state.checkTimer) clearTimeout(state.checkTimer);
    state.checkTimer = setTimeout(runCheck, 300);   // debounce bursts of captured pages
  }

  async function runCheck() {
    if (state.checking) return;
    if (!isFleetPage()) return;
    const rows = Object.values(state.rows);
    if (!rows.length) return;
    if (!ensureCreds()) { pill('SuperDispatch credentials needed — see Tampermonkey menu'); return; }

    state.checking = true;
    try {
      const cache = loadCache();
      // Green is terminal (delivered) -> served from cache forever. Yellow/red can still change, so
      // they are RE-CHECKED every pass (page refresh, portal Apply/filter, pagination). New VINs too.
      const todo = rows.filter((r) => {
        const e = cache.vins[r.vin];
        return !e || e.color === 'yellow' || e.color === 'red';
      });
      applyColors();                                   // paint whatever is already cached
      pill(progressText(rows.length, cache));
      if (!todo.length) { state.checking = false; return; }

      let i = 0, done = 0;
      async function worker() {
        while (i < todo.length) {
          const r = todo[i++];
          const res = await evaluateVin(r.vin, r.deliveryDate);
          if (!res.error) {
            cache.vins[r.vin] = { color: res.color, status: res.status, deliveredAt: res.deliveredAt, matched: res.matched, card: res.card || null };
            saveCache(cache);
            applyColors();
          } else {
            LOG('skip (will retry next load):', r.vin, res.error);
          }
          done++;
          pill(progressText(rows.length, cache));
          await sleep(REQ_GAP_MS);
        }
      }
      await Promise.all(Array.from({ length: CONCURRENCY }, worker));
      reapply();   // rows may have rendered after the scan started (first visit) -> paint them now
      pill(progressText(rows.length, cache) + ' — done');
    } finally {
      state.checking = false;
    }
  }

  function progressText(total, cache) {
    const checked = Object.keys(cache.vins).length;
    return 'SuperDispatch: ' + Math.min(checked, total) + '/' + total + ' checked';
  }

  // ============================ DOM recolor ===============================
  const VIN_RE = /[A-HJ-NPR-Z0-9]{17}/;
  function rowVin(tr) {
    const cell = tr.querySelector('.cdk-column-FullVin');
    const txt = ((cell ? cell.textContent : tr.textContent) || '');
    const m = txt.match(VIN_RE);
    return m ? m[0].toUpperCase() : null;
  }
  function paintRow(tr, color) {
    const bg = COLORS[color];
    if (!bg) return;
    if (tr.dataset.sdColor === color) return;          // already painted this color
    tr.style.setProperty('background-color', bg, 'important');
    // let the row background show through the (often white) Angular cell backgrounds
    tr.querySelectorAll('td').forEach((td) => td.style.setProperty('background-color', 'transparent', 'important'));
    tr.dataset.sdColor = color;
  }
  function applyColors() {
    if (!isFleetPage()) return;
    const cache = loadCache();
    document.querySelectorAll('tr').forEach((tr) => {
      const vin = rowVin(tr);
      if (!vin) return;
      const e = cache.vins[vin];
      if (e && e.color) paintRow(tr, e.color);
    });
  }
  // Paint several times over the first few seconds so rows get colored the moment Angular renders
  // them from the (warm) cache — this is what makes SPA navigation color instantly instead of only
  // after a full refresh. Cheap: each pass is one querySelectorAll over ~170 rows.
  const REAPPLY_DELAYS = [0, 120, 300, 600, 1200, 2500, 4500];
  function reapply() { REAPPLY_DELAYS.forEach((d) => setTimeout(applyColors, d)); }

  // ============================ Tesla capture =============================
  // Accept the Regular Fleet data response by SHAPE (so it never grabs bidboard/other data):
  // a JSON body with an array of records that each carry a `vin` and a delivery-date / shipment field.
  function extractFleetRows(text) {
    if (!text || text.indexOf('"vin"') === -1) return null;
    let j; try { j = JSON.parse(text); } catch (_) { return null; }
    const arr = Array.isArray(j) ? j
      : Array.isArray(j.data) ? j.data
      : (j.data && Array.isArray(j.data.items)) ? j.data.items
      : (j.data && Array.isArray(j.data.objects)) ? j.data.objects : null;
    if (!arr || !arr.length) return null;
    const r0 = arr[0];
    if (!r0 || typeof r0 !== 'object' || !('vin' in r0)) return null;
    const hasFleetField = ['deliveryDate', 'shipmentNumber', 'shipment', 'shipmentName']
      .some((k) => k in r0);
    if (!hasFleetField) return null;
    return arr;
  }

  function onFleetRows(arr) {
    let added = 0;
    for (const rec of arr) {
      const vin = String(rec.vin || '').toUpperCase();
      if (!/^[A-HJ-NPR-Z0-9]{17}$/.test(vin)) continue;
      const shp = String(rec.shipmentNumber || rec.shipment || rec.shipmentName || '');
      const dd = String(rec.deliveryDate || rec.deliveryDateTime || rec.deliveredDate || '');
      if (!state.rows[vin]) { state.rows[vin] = { vin, shipment: shp, deliveryDate: dd }; added++; }
      else {
        if (shp && !state.rows[vin].shipment) state.rows[vin].shipment = shp;
        if (dd && !state.rows[vin].deliveryDate) state.rows[vin].deliveryDate = dd;
      }
    }
    if (added) LOG('captured', added, 'new VIN(s); total', Object.keys(state.rows).length);
    // Run on EVERY capture (refresh / Apply / filter / paginate), not just when new VINs appear —
    // this is what re-checks the yellow/red VINs each time the fleet data reloads.
    scheduleCheck();
    reapply();   // paint already-cached rows as soon as the table renders (instant on navigation)
  }

  // Hook XHR at document-start (installed before the app makes its calls).
  const X = window.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send;
  X.prototype.open = function (m, u) { this.__rf = { method: String(m || 'GET').toUpperCase(), url: u }; return oOpen.apply(this, arguments); };
  X.prototype.send = function () {
    const xhr = this;
    if (xhr.__rf) {
      xhr.addEventListener('load', function () {
        try {
          const t = (xhr.responseType === '' || xhr.responseType === 'text') ? xhr.responseText : '';
          const arr = extractFleetRows(t);
          if (arr) onFleetRows(arr);
        } catch (_) { /* ignore */ }
      });
    }
    return oSend.apply(this, arguments);
  };
  // Also hook fetch, in case the app uses it for this grid.
  const oFetch = window.fetch;
  if (oFetch) {
    window.fetch = function () {
      return oFetch.apply(this, arguments).then((resp) => {
        try {
          resp.clone().text().then((t) => { const arr = extractFleetRows(t); if (arr) onFleetRows(arr); }).catch(() => {});
        } catch (_) {}
        return resp;
      });
    };
  }

  // ============================ tiny status pill ==========================
  let pillEl = null;
  function pill(text) {
    if (!isFleetPage()) return;
    if (!pillEl) {
      pillEl = document.createElement('div');
      pillEl.style.cssText = [
        'position:fixed', 'bottom:12px', 'right:12px', 'z-index:2147483647',
        'background:#111', 'color:#fff', 'font:12px/1.3 system-ui,Segoe UI,Arial,sans-serif',
        'padding:6px 10px', 'border-radius:8px', 'box-shadow:0 2px 8px rgba(0,0,0,.35)',
        'opacity:.92', 'pointer-events:none', 'max-width:260px',
      ].join(';');
      (document.body || document.documentElement).appendChild(pillEl);
    }
    pillEl.style.display = '';
    pillEl.textContent = text;
  }
  function toast(text) { pill(text); }

  // ============================ observers / nav ===========================
  function startObserver() {
    let pending = false;
    const obs = new MutationObserver(() => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; applyColors(); });
    });
    const attach = () => obs.observe(document.body, { childList: true, subtree: true });
    if (document.body) attach();
    else document.addEventListener('DOMContentLoaded', attach, { once: true });
  }

  // Belt-and-suspenders: also force a re-check when the portal's "Apply" (or "Search") button is
  // clicked. The click's own data refetch is normally captured by the XHR hook, but this guarantees
  // the yellow/red re-check fires even if that capture is missed for any reason.
  function hookApply() {
    document.addEventListener('click', (e) => {
      if (!isFleetPage()) return;
      const t = e.target;
      const btn = t && t.closest && t.closest('button,[role="button"],tsl-button,.tsl-button');
      if (!btn) return;
      const label = (btn.textContent || '').trim().toLowerCase();
      if (label.indexOf('apply') !== -1 || label.indexOf('search') !== -1) {
        LOG('Apply/Search clicked -> forcing re-check after refetch');
        setTimeout(scheduleCheck, 1200);   // give the portal time to refetch, then re-check yellow/red
      }
    }, true);
  }

  // Re-apply on SPA route changes (URL changes without a reload). The portal keeps the DOM
  // across routes, so the pill must be hidden when leaving the fleet page.
  function hookHistory() {
    const fire = () => {
      reapply();
      if (isFleetPage()) scheduleCheck();
      else if (pillEl) pillEl.style.display = 'none';
    };
    const wrap = (name) => { const o = history[name]; history[name] = function () { const r = o.apply(this, arguments); fire(); return r; }; };
    wrap('pushState'); wrap('replaceState');
    window.addEventListener('popstate', fire);
  }

  // ============================ hover card panel ==========================
  const HOVER_OPEN_MS = 80;    // tiny open delay so sweeping across VINs doesn't flash
  const HOVER_CLOSE_MS = 90;

  const PANEL_CSS = [
    '#sd-hover{position:fixed;z-index:2147483647;background:#fff;color:#1a1a1a;',
    'font:15px/1.4 "Segoe UI",system-ui,Arial,sans-serif;border:1px solid #e4e4e4;',
    'border-radius:12px;box-shadow:0 9px 32px rgba(0,0,0,.18);padding:16px 18px;',
    'width:max-content;max-width:min(94vw,660px);opacity:0;transition:opacity .12s ease;pointer-events:none;box-sizing:border-box;}',
    '#sd-hover *{box-sizing:border-box;}',
    '#sd-hover .sd-head{display:flex;align-items:center;gap:11px;margin-bottom:15px;}',
    '#sd-hover .sd-num{font-size:22px;font-weight:700;color:#111;letter-spacing:.2px;}',
    '#sd-hover .sd-pill{font-size:14px;font-weight:600;padding:3px 12px;border-radius:14px;white-space:nowrap;}',
    '#sd-hover .sd-pill.green{background:#e6f4ea;color:#1e7b34;}',
    '#sd-hover .sd-pill.yellow{background:#fbefc9;color:#8a6a00;}',
    '#sd-hover .sd-pill.gray{background:#eee;color:#666;}',
    // The right column stretches to the left column's height so the per-unit carrier cost
    // drops to the bottom, in line with the destination row.
    '#sd-hover .sd-body{display:flex;gap:37px;align-items:stretch;}',
    '#sd-hover .sd-col{flex:0 0 auto;white-space:nowrap;}',
    '#sd-hover .sd-right{display:flex;flex-direction:column;min-width:150px;}',
    '#sd-hover .sd-route{position:relative;padding-left:23px;}',
    '#sd-hover .sd-stop{position:relative;}',
    '#sd-hover .sd-stop + .sd-stop{margin-top:16px;}',
    '#sd-hover .sd-stop:not(:last-child)::before{content:"";position:absolute;left:-17px;top:10px;bottom:-23px;border-left:2px dashed #cfcfcf;}',
    '#sd-hover .sd-mark{position:absolute;left:-23px;top:3px;width:13px;height:13px;}',
    '#sd-hover .sd-mark.dot{border-radius:50%;background:#e8730b;}',
    '#sd-hover .sd-mark.sq{background:#2e8b3d;border-radius:2px;}',
    '#sd-hover .sd-city{font-weight:700;color:#161616;max-width:345px;overflow:hidden;text-overflow:ellipsis;}',
    '#sd-hover .sd-sub{color:#8c8c8c;font-size:14.5px;margin-top:3px;max-width:345px;overflow:hidden;text-overflow:ellipsis;}',
    '#sd-hover .sd-model{font-weight:700;color:#161616;}',
    '#sd-hover .sd-vin{display:inline-block;background:#fcf3d6;padding:2px 6px;border-radius:4px;margin-top:4px;font-size:14.5px;color:#222;}',
    '#sd-hover .sd-more{color:#9a9a9a;font-size:12px;margin-top:4px;}',
    '#sd-hover .sd-unit-cost{margin-top:auto;padding-top:14px;color:#161616;font-weight:700;font-size:14.5px;}',
    '.cdk-column-FullVin{cursor:help;}',
  ].join('');

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  // Cap over-long venue names so a stray note ("...TAKE PICTURES OF THE VIN...") can't blow out the
  // card width; 34 keeps the real names (e.g. "NA-US-PA-Pittsburgh 1400 SHOWROOM") and trims outliers.
  const MAX_VENUE = 34;
  function trunc(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1).replace(/\s+$/, '') + '…' : s;
  }

  function cardHtml(c, hoveredVin) {
    const pill = pillClass(c.status);
    const statusLabel = titleCase(c.status) || '—';
    const vehicles = c.vehicles || [];
    // Show ONLY the hovered VIN; any other vehicles on the order collapse to "+N more".
    const hero = vehicles.find((v) => v.vin && v.vin === hoveredVin) || vehicles[0] || { vin: '', label: '' };
    const others = Math.max(0, vehicles.length - 1);
    const vehHtml =
      (hero.label ? '<div class="sd-model">' + esc(hero.label) + '</div>' : '') +
      (hero.vin ? '<div class="sd-vin">' + esc(hero.vin) + '</div>' : '') +
      (others > 0 ? '<div class="sd-more">+' + others + ' more</div>' : '');
    const costHtml = c.unitCost ? '<div class="sd-unit-cost">' + esc(c.unitCost) + '</div>' : '';
    const psub = [c.pickup.date, trunc(c.pickup.name, MAX_VENUE)].filter(Boolean).join('  ·  ');
    const dsub = [c.delivery.date, trunc(c.delivery.name, MAX_VENUE)].filter(Boolean).join('  ·  ');
    return '<div class="sd-card">' +
        '<div class="sd-head"><span class="sd-num">' + esc(c.number || '—') + '</span>' +
          '<span class="sd-pill ' + pill + '">' + esc(statusLabel) + '</span></div>' +
        '<div class="sd-body">' +
          '<div class="sd-col"><div class="sd-route">' +
            '<div class="sd-stop"><span class="sd-mark dot"></span>' +
              '<div class="sd-city">' + esc(c.pickup.line || '—') + '</div>' +
              '<div class="sd-sub">' + esc(psub) + '</div></div>' +
            '<div class="sd-stop"><span class="sd-mark sq"></span>' +
              '<div class="sd-city">' + esc(c.delivery.line || '—') + '</div>' +
              '<div class="sd-sub">' + esc(dsub) + '</div></div>' +
          '</div></div>' +
          '<div class="sd-col sd-right"><div class="sd-veh">' + vehHtml + '</div>' + costHtml + '</div>' +
        '</div></div>';
  }

  let panelEl = null, hideT = null, showT = null, shownVin = null;
  function ensurePanel() {
    if (panelEl) return panelEl;
    const style = document.createElement('style');
    style.textContent = PANEL_CSS;
    (document.head || document.documentElement).appendChild(style);
    panelEl = document.createElement('div');
    panelEl.id = 'sd-hover';
    (document.body || document.documentElement).appendChild(panelEl);
    return panelEl;
  }
  function showPanel(cell, vin, entry) {
    clearTimeout(hideT);
    if (shownVin === vin && panelEl && panelEl.style.opacity === '1') return;
    clearTimeout(showT);
    showT = setTimeout(() => {
      const card = entry.card || (entry.cards && entry.cards[0]);   // fallback for pre-0.5 cache
      if (!card) return;
      const p = ensurePanel();
      p.innerHTML = cardHtml(card, vin);
      p.style.display = 'block'; p.style.opacity = '0';       // render hidden to measure
      const r = cell.getBoundingClientRect();
      const pw = p.offsetWidth, ph = p.offsetHeight;
      let left = r.right + 10;                                 // to the RIGHT of the VIN
      let top = r.top - ph - 6;                                // popping UP
      if (left + pw > window.innerWidth - 8) left = Math.max(8, r.left - pw - 10);
      if (top < 8) top = r.bottom + 6;                         // flip below if no room above
      p.style.left = left + 'px'; p.style.top = top + 'px';
      p.style.opacity = '1';
      shownVin = vin;
    }, HOVER_OPEN_MS);
  }
  function hidePanel() {
    clearTimeout(showT);
    hideT = setTimeout(() => { if (panelEl) panelEl.style.opacity = '0'; shownVin = null; }, HOVER_CLOSE_MS);
  }
  function vinFromCell(cell) {
    const m = (cell.textContent || '').match(VIN_RE);
    return m ? m[0].toUpperCase() : null;
  }
  function startHover() {
    document.addEventListener('mouseover', (e) => {
      const t = e.target;
      if (!t || !t.closest || !isFleetPage()) return;
      const cell = t.closest('.cdk-column-FullVin');
      if (!cell) return;
      const vin = vinFromCell(cell);
      if (!vin) return;
      const entry = loadCache().vins[vin];
      if (!entry || !(entry.card || (entry.cards && entry.cards.length))) return;
      showPanel(cell, vin, entry);
    }, true);
    document.addEventListener('mouseout', (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      const cell = t.closest('.cdk-column-FullVin');
      if (!cell) return;
      const to = e.relatedTarget;
      if (to && cell.contains(to)) return;                    // still inside the same VIN cell
      hidePanel();
    }, true);
    window.addEventListener('scroll', () => { if (shownVin) hidePanel(); }, true);
    window.addEventListener('resize', () => { if (shownVin) hidePanel(); });
  }

  // ============================ menu commands =============================
  GM_registerMenuCommand('Set SuperDispatch credentials', () => { promptCreds(); });
  GM_registerMenuCommand('Re-scan now (clear today\'s cache)', () => {
    GM_setValue('sd_cache', { version: CACHE_VERSION, day: today(), vins: {} });
    document.querySelectorAll('tr[data-sd-color]').forEach((tr) => { tr.style.removeProperty('background-color'); delete tr.dataset.sdColor; });
    scheduleCheck();
  });
  GM_registerMenuCommand('Clear stored credentials', () => {
    GM_deleteValue('sd_creds'); GM_deleteValue('sd_token'); toast('SuperDispatch credentials cleared');
  });

  // ============================ boot ======================================
  startObserver();
  hookHistory();
  hookApply();
  startHover();
  reapply();     // paint on initial load too, in case rows are already present
  LOG('installed (v0.18.0)');
})();
