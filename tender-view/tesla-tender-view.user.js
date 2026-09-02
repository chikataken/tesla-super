// ==UserScript==
// @name         Tesla Tender View — Ledger Overlay
// @namespace    wastake.tenderview
// @version      0.3.3
// @description  Adds a "Tender View" page to the Tesla supplier portal: the TFI tender ledger (shipments.wastake.com/api/tenders) rendered as an excel-style grid — one row per VIN grouped by shipment, our live SD-derived status, plus a column with Tesla's OWN stop status pulled through the Dispatch Dashboard 2.0 API (auth piggybacked off the page's own calls; opens with Alt+T or the floating button).
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/tender-view/tesla-tender-view.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/tender-view/tesla-tender-view.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      shipments.wastake.com
// ==/UserScript==

/*
 * CLEAN BASELINE (2026-09-02 rules restart): raw tender rows + Tesla column only —
 * all derived statuses/categories removed pending the new rule set.
 *
 * WHAT IT DOES
 *   - A full-page overlay ("Tender View") inside the Tesla supplier portal showing the
 *     TFI tender ledger: every tender-move (SHP + VIN) with our SD-derived status
 *     (Tendered / New / Posted / Accepted / Picked up / Delivered / Invoiced / Paid /
 *     Fleet), carrier, cost, need-by — the same grid as the /adv Tender tab.
 *   - NEW COLUMN "Tesla": Tesla's own stop status for each VIN, fetched via the
 *     Dispatch Dashboard 2.0 API (GetCarrierDispatchShipment, searched by VINs in
 *     chunks). Auth is piggybacked from the page's own dashboard calls — visit the
 *     Dispatch Dashboard once per session and the column fills in.
 *   - Ledger data comes from shipments.wastake.com/api/tenders (CORS-allowed for this
 *     origin, X-Profile: didi = unfiltered). Refreshes every 60s while open.
 *   - SHARED POOL: every Tesla status this instance fetches is POSTed back to
 *     /api/tenders/tesla-status, so all other instances (and the /adv tab) see the
 *     Tesla column too — one person's dashboard session feeds everyone. Pool entries
 *     render with their age ("Delivered · 3h") when not fetched locally.
 */

(function () {
  'use strict';

  const API = localStorage.getItem('tv_api') || 'https://shipments.wastake.com/api/tenders';
  const API_STATUS = API.replace(/\/api\/tenders$/, '/api/tenders/tesla-status');
  const ENDPOINT = 'GetCarrierDispatchShipment';
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const TODAY = (d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`)(new Date());

  // ---- Tesla API piggyback ---------------------------------------------------
  // Same pattern as the dispatch-dashboard script: capture the bearer + endpoint off
  // the page's own dashboard calls, and passively ingest any dashboard response the
  // user already loaded. tender-view then issues its OWN vins-searches when opened.
  let apiAuth = null, apiCarrier = null, apiUrl = null;
  const tesla = new Map();          // VIN -> {status, needBy, service, shipment}

  const W = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;

  (function hookXHR() {
    const XHR = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
    if (!XHR || XHR.__tvHooked) return;
    XHR.__tvHooked = true;
    const _open = XHR.open, _send = XHR.send, _set = XHR.setRequestHeader;
    XHR.open = function (m, u) { this.__tvUrl = u; return _open.apply(this, arguments); };
    XHR.setRequestHeader = function (k, v) {
      try {
        if (String(this.__tvUrl || '').indexOf(ENDPOINT) > -1) {
          const lk = String(k).toLowerCase();
          if (lk === 'authorization') apiAuth = v;
          else if (lk === 'x-selectedcarrierid') apiCarrier = v;
        }
      } catch (e) {}
      return _set.apply(this, arguments);
    };
    XHR.send = function () {
      try {
        if (String(this.__tvUrl || '').indexOf(ENDPOINT) > -1) {
          apiUrl = this.__tvUrl;
          this.addEventListener('load', function () {
            try { if (this.status >= 200 && this.status < 300) ingestTesla(JSON.parse(this.responseText)); } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, arguments);
    };
  })();

  function ingestTesla(json) {
    let d; try { d = (typeof json === 'string' ? JSON.parse(json) : json).data; } catch (e) { return; }
    if (!d || !Array.isArray(d.shipmentList)) return;
    for (const ship of d.shipmentList) for (const stop of (ship.stops || []))
      for (const v of (stop.vins || [])) {
        if (!v || !v.vin) continue;
        const vin = String(v.vin).toUpperCase();
        tesla.set(vin, {
          status: stop.stopStatusDescription || '',
          needBy: stop.needByDate || '', service: stop.serviceLevelDescription || '',
          shipment: stop.shipmentNumber || '',
        });
        dirty.add(vin);
      }
  }

  const chunk = (a, n) => { const o = []; for (let i = 0; i < a.length; i += n) o.push(a.slice(i, i + n)); return o; };

  // Self-issued VIN search (only while the overlay is open; ~7 calls for the active set)
  async function fetchTeslaStatuses(vins) {
    if (!apiAuth || !apiUrl) return false;
    const missing = vins.filter(v => !tesla.has(v));
    for (const part of chunk(missing, 150)) {
      try {
        const res = await fetch(apiUrl, { method: 'POST',
          headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json',
                     'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
          body: JSON.stringify({ skip: 0, take: 5000, vins: part, carrierId: null,
            // the API returns NOTHING without a created-date window
            createdDateStart: new Date(Date.now() - 90 * 86400000).toISOString(),
            createdDateEnd: new Date(Date.now() + 86400000).toISOString() }) });
        if (!res.ok) break;
        ingestTesla(await res.json());
      } catch (e) { break; }
      await new Promise(r => setTimeout(r, 250));
    }
    return true;
  }

  // ---- state -----------------------------------------------------------------
  let DATA = null;                  // {rows, orphans}
  let UI = { q: '', sort: { key: 'origin', dir: 1 } };
  const COLOR = { TENDERED: '#64748b', NEW: '#64748b', POSTED: '#2563eb', DISPATCHED: '#64748b',
    'PICKED UP': '#b45309', DELIVERED: '#0f766e', INVOICED: '#8b5cf6', PAID: '#166534',
    FLEET: '#7c3aed', CANCELED: '#d6453c', ARCHIVED: '#9aa1ab', 'RE-TENDERED': '#9aa1ab' };
  const LABEL = { DISPATCHED: 'Accepted' };
  const RANKS = { TENDERED: 0, NEW: 1, POSTED: 2, DISPATCHED: 3, 'PICKED UP': 4,
    DELIVERED: 5, INVOICED: 6, PAID: 7 };
  let refreshTimer = null;

  // Tesla's page CSP (connect-src 'self' ...) blocks page-context fetch to our
  // server, so the ledger call goes through GM_xmlhttpRequest (extension-level,
  // CSP-immune — same pattern as the dispatch-dashboard SD calls).
  function gmJson(url, headers, method, body) {
    method = method || 'GET';
    return new Promise((resolve, reject) => {
      const GM = (typeof GM_xmlhttpRequest !== 'undefined') ? GM_xmlhttpRequest
        : (typeof W.GM_xmlhttpRequest !== 'undefined') ? W.GM_xmlhttpRequest : null;
      if (GM) {
        GM({ method, url, headers, data: body || undefined, timeout: 25000,
          onload: r => { (r.status >= 200 && r.status < 300)
            ? resolve(JSON.parse(r.responseText))
            : reject(new Error('HTTP ' + r.status)); },
          onerror: () => reject(new Error('network error')),
          ontimeout: () => reject(new Error('timeout')) });
      } else {
        fetch(url, { method, headers, body }).then(r => {
          if (!r.ok) throw new Error('HTTP ' + r.status); return r.json();
        }).then(resolve, reject);
      }
    });
  }
  async function loadLedger() { DATA = await gmJson(API, { 'X-Profile': 'didi' }); }

  // ---- shared Tesla-status pool ---------------------------------------------
  // Everything ingested locally is pushed to the ledger so other instances (and
  // the /adv tab) get the Tesla column without their own dashboard auth.
  const dirty = new Set();
  async function flushTesla() {
    if (!dirty.size) return;
    const batch = [...dirty].slice(0, 2000);
    const statuses = batch.map(vin => { const t = tesla.get(vin) || {};
      return { vin, status: t.status, shipment: t.shipment, needBy: t.needBy, service: t.service }; });
    try {
      await gmJson(API_STATUS, { 'Content-Type': 'application/json' }, 'POST',
        JSON.stringify({ statuses }));
      batch.forEach(v => dirty.delete(v));
    } catch (e) { /* keep dirty; retried on the next flush tick */ }
  }
  setInterval(flushTesla, 20000);

  // ---- helpers (mirrors the /adv tender tab) ---------------------------------
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const cap = s => s.charAt(0) + s.slice(1).toLowerCase();
  const short = shp => esc((shp || '').replace(/^SHP\d+-/, ''));
  const iso = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? `${MON[+m[2] - 1]} ${+m[3]}` : '—'; };
  const money = v => (v == null || v === '') ? '—' : '$' + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const loc = (name, city, st) => name ? name.replace(/^NA-US-[A-Z]{2}-/, '').replace(/^TI\d+-/, '') : `${city || '?'}, ${st || ''}`;
  const stageTxt = r => {
    const col = COLOR[r.stage] || COLOR.FLEET;
    let lab = LABEL[r.stage] || (r.stage === 'FLEET?' ? 'Fleet?' : cap(r.stage || '?'));
    if (r.superseded_by) lab = 'Re-tendered → ' + esc(r.superseded_by.replace(/^SHP\d+-/, ''));
    const note = r.cancel_note ? ` <span class="tv-note" title="${esc(r.cancel_note)}">↩</span>` : '';
    return `<span style="color:${col};font-weight:700">${lab}</span>` + note;
  };
  const match = (r, q) => !q || [r.vin, r.shp, r.origin_city, r.dest_city, r.dest_name,
    r.origin_name, r.driver, r.origin_state, r.dest_state]
    .some(v => v && String(v).toLowerCase().includes(q));
  const tvAge = ep => { const h = (Date.now() / 1000 - ep) / 3600;
    return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`; };
  const teslaTxt = (vin, pooled) => {
    const t = tesla.get(vin);
    if (t) return `<span style="font-weight:700" title="${esc(t.shipment)} · ${esc(t.service)}">${esc(t.status || '?')}</span>`;
    if (pooled && pooled.status)
      return `<span style="font-weight:700" title="${esc(pooled.shipment || '')} · shared pool">${esc(pooled.status)}</span>` +
        ` <span class="tv-dim" style="font-size:10.5px">${tvAge(pooled.fetched_at)}</span>`;
    return `<span class="tv-dim">${apiAuth ? '—' : 'visit dashboard'}</span>`;
  };

  function shipments(rows) {
    const by = {};
    rows.forEach(r => {
      const o = by[r.shp] || (by[r.shp] = { shp: r.shp, sent_at: r.sent_at, vins: 0, rows: [],
        cost: 0, origin: `${r.origin_state || '~'} ${r.origin_city || '~'}`,
        dest: `${r.dest_state || '~'} ${r.dest_city || '~'}`, need_by: r.need_by, driver: r.driver });
      o.vins++; o.rows.push(r);
      o.cost += Number(r.cost_usd) || 0;
    });
    return Object.values(by);
  }

  const COLS = [
    { k: 'shp', lab: 'Shipment #', w: 92, v: o => o.shp },
    { k: 'vin', lab: 'VIN', w: 150, v: o => o.vins },
    { k: 'status', lab: 'Status', w: 92, v: o => Math.min(...o.rows.map(r => RANKS[r.stage] ?? -1)) },
    { k: 'tesla', lab: 'Tesla', w: 118, v: o => (tesla.get(o.rows[0].vin) || {}).status || '~' },
    { k: 'needby', lab: 'NeedByDate', w: 84, v: o => o.need_by || '~' },
    { k: 'driver', lab: 'Driver', w: 140, v: o => o.driver || '~' },
    { k: 'cost', lab: 'TotalCost', w: 78, v: o => o.cost },
    { k: 'origin', lab: 'Origin', w: 165, v: o => o.origin },
    { k: 'dest', lab: 'Destination', w: 0, v: o => o.dest },
  ];

  // ---- rendering -------------------------------------------------------------
  function render() {
    const root = document.getElementById('tv-body');
    if (!root || !DATA) return;
    const q = UI.q.toLowerCase();
    const rows = (DATA.rows || []).filter(r => match(r, q));
    const ships = shipments(rows);
    document.getElementById('tv-chips').innerHTML = '';
    document.getElementById('tv-stats').textContent =
      `${ships.length} shipments · ${rows.length} VINs · window ${DATA.window_days || 21}d`;
    if (!ships.length) { root.innerHTML = '<div class="tv-empty">No tenders match.</div>'; return; }
    const col = COLS.find(c => c.k === UI.sort.key) || COLS[6];
    ships.sort((a, b) => { const x = col.v(a), y = col.v(b);
      return (x < y ? -1 : x > y ? 1 : 0) * UI.sort.dir || b.sent_at - a.sent_at; });
    const isFleetShip = o => o.rows.every(r => r.stage === 'FLEET' || r.stage === 'FLEET?');
    const brok = ships.filter(o => !isFleetShip(o)), fleet = ships.filter(isFleetShip);
    const colgroup = COLS.map(c => `<col style="width:${c.w ? c.w + 'px' : 'auto'}">`).join('');
    const head = COLS.map(c => `<th data-sort="${c.k}">${c.lab}${UI.sort.key === c.k
      ? `<span class="tv-dir">${UI.sort.dir > 0 ? '▴' : '▾'}</span>` : ''}</th>`).join('');
    const secRow = (lab, n) => `<tr class="tv-sec"><td colspan="${COLS.length}">${lab} · ${n} shipments</td></tr>`;
    const bodyOf = list => list.map(o => o.rows.map((r, i) => {
      const shared = i === 0 ? `<td class="tv-num" rowspan="${o.rows.length}">${short(o.shp)}</td>` : '';
      return `<tr${i === 0 ? ' class="tv-grp"' : ''}>` + shared +
        `<td>${esc(r.vin)}</td>` +
        `<td>${stageTxt(r)}</td>` +
        `<td>${teslaTxt(r.vin, r.tesla)}</td>` +
        `<td class="tv-num" title="${esc(r.need_by || '')}">${iso(r.need_by)}</td>` +
        `<td class="tv-dim" title="${esc(r.driver || '')}">${esc(r.driver || '—')}</td>` +
        `<td class="tv-num">${money(r.cost_usd)}</td>` +
        `<td class="tv-dim" title="${esc(r.origin_name || '')}">${esc(loc(r.origin_name, r.origin_city, r.origin_state))}</td>` +
        `<td class="tv-dim" title="${esc(r.dest_name || '')}">${esc(loc(r.dest_name, r.dest_city, r.dest_state))}</td>` +
        `</tr>`;
    }).join('')).join('');
    const body = (brok.length ? secRow('BROKERED', brok.length) + bodyOf(brok) : '')
      + (fleet.length ? secRow('FLEET', fleet.length) + bodyOf(fleet) : '');
    root.innerHTML = `<table class="tv-xlt"><colgroup>${colgroup}</colgroup><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  // ---- overlay chrome --------------------------------------------------------
  const CSS = `
  #tv-launch{position:fixed;right:18px;bottom:18px;z-index:99998;background:#171a20;color:#fff;
    border:none;border-radius:22px;padding:10px 18px;font:600 13px -apple-system,BlinkMacSystemFont,
    'Segoe UI',Roboto,sans-serif;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25)}
  #tv-launch:hover{background:#000}
  #tv-page{position:fixed;inset:0;z-index:99999;background:#f4f6f8;display:flex;flex-direction:column;
    font:13px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1b2330}
  #tv-head{display:flex;align-items:center;gap:12px;padding:10px 16px;background:#fff;
    border-bottom:1px solid #dde2e8;flex:none}
  #tv-head h1{font-size:15px;font-weight:800;margin:0;letter-spacing:.02em}
  #tv-q{border:1px solid #dde2e8;border-radius:8px;padding:6px 12px;width:280px;font-size:13px}
  #tv-stats{color:#69707d;font-size:12px}
  #tv-x{margin-left:auto;border:1px solid #dde2e8;background:#fff;border-radius:8px;
    padding:5px 13px;font-size:14px;cursor:pointer}
  #tv-x:hover{background:#eef1f5}
  #tv-chipbar{display:flex;gap:8px;flex-wrap:wrap;padding:8px 16px 6px;flex:none}
  .tv-chip{border:1px solid #dde2e8;background:#fff;border-radius:999px;padding:3px 12px;
    font-size:12px;font-weight:600;color:#69707d;cursor:pointer}
  .tv-chip.on{background:#2563eb;border-color:#2563eb;color:#fff}
  .tv-chip span{font-weight:800;margin-left:5px;opacity:.75}
  #tv-body{flex:1;overflow:auto;padding:4px 8px 30px}
  .tv-xlt{width:100%;table-layout:fixed;border-collapse:collapse;font-size:12.5px;background:#fff;
    border:1px solid #dde2e8}
  .tv-xlt th{position:sticky;top:0;z-index:1;background:#eef1f5;border:1px solid #dde2e8;
    padding:6px 9px;text-align:left;font-size:10.5px;font-weight:800;letter-spacing:.03em;
    text-transform:uppercase;color:#69707d;cursor:pointer;user-select:none;white-space:nowrap}
  .tv-xlt td{border:1px solid #dde2e8;padding:5px 9px;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;background:#fff}
  .tv-xlt tbody tr:hover td{background:#eef1f5}
  .tv-xlt tr.tv-grp td{border-top:1.5px solid #b9c0cb}
  .tv-xlt tr.tv-sec td{background:#eef1f5;color:#69707d;font-size:10.5px;font-weight:800;
    letter-spacing:.05em;padding:4px 9px;border-top:2px solid #b9c0cb}
  .tv-num{text-align:right;font-variant-numeric:tabular-nums}
  .tv-ctr{text-align:center}
  .tv-dim{color:#69707d}
  .tv-dir{margin-left:4px;color:#2563eb}
  .tv-warn{cursor:help} .tv-warn.state{color:#d6453c} .tv-warn.city{color:#b9791a}
  .tv-note{cursor:help;color:#b9791a}
  .tv-empty{padding:40px;color:#69707d;text-align:center}`;

  function openPage() {
    if (document.getElementById('tv-page')) return;
    const page = document.createElement('div');
    page.id = 'tv-page';
    page.innerHTML = `
      <div id="tv-head"><h1>TENDER VIEW</h1>
        <input id="tv-q" type="search" placeholder="Filter by VIN, SHP, city, carrier…">
        <span id="tv-stats">loading…</span>
        <button id="tv-x" title="Close (Esc)">✕</button></div>
      <div id="tv-chipbar"><div id="tv-chips" style="display:contents"></div></div>
      <div id="tv-body"><div class="tv-empty">Loading tender ledger…</div></div>`;
    document.body.appendChild(page);
    page.querySelector('#tv-x').onclick = closePage;
    page.querySelector('#tv-q').oninput = e => { UI.q = e.target.value; render(); };
    page.addEventListener('click', e => {
      const chip = e.target.closest('.tv-chip');
      if (chip) {
        const act = chip.dataset.act;
        if (act.startsWith('view:')) { UI.view = act.slice(5); UI.orphans = false; }
        else UI[act] = !UI[act];
        render(); return;
      }
      const th = e.target.closest('th[data-sort]');
      if (th) { const k = th.dataset.sort;
        if (UI.sort.key === k) UI.sort.dir *= -1; else UI.sort = { key: k, dir: 1 };
        render(); }
    });
    refresh();
    refreshTimer = setInterval(refresh, 60000);
  }
  function closePage() {
    const p = document.getElementById('tv-page');
    if (p) p.remove();
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }
  async function refresh() {
    try { await loadLedger(); } catch (e) {
      const b = document.getElementById('tv-body');
      if (b) b.innerHTML = `<div class="tv-empty">Could not load the ledger — ${esc(e.message)}</div>`;
      return;
    }
    render();
    // Tesla's own statuses for the active + orphan VINs (needs auth piggybacked
    // from one Dispatch Dashboard visit this session)
    const want = [...new Set((DATA.rows || []).map(r => r.vin))]
      .filter(v => /^[A-HJ-NPR-Z0-9]{17}$/.test(v));
    if (await fetchTeslaStatuses(want)) render();
  }

  // ---- boot ------------------------------------------------------------------
  function boot() {
    if (!document.body || document.getElementById('tv-launch')) return;
    const st = document.createElement('style'); st.textContent = CSS;
    document.head.appendChild(st);
    const b = document.createElement('button');
    b.id = 'tv-launch'; b.textContent = 'Tender View';
    b.onclick = openPage;
    document.body.appendChild(b);
    document.addEventListener('keydown', e => {
      if (e.altKey && (e.key === 't' || e.key === 'T')) {
        document.getElementById('tv-page') ? closePage() : openPage();
      } else if (e.key === 'Escape') closePage();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
  new MutationObserver(() => boot()).observe(document.documentElement, { childList: true, subtree: true });
})();
