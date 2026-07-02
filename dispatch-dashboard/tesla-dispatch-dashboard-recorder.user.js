// ==UserScript==
// @name         Tesla Dispatch Dashboard — Recorder (dev)
// @namespace    wastake.dispatchdash
// @version      0.3.0
// @description  DEV TOOL. Piggybacks on the Dispatch Dashboard 2.0 page's own data calls (hooks GetCarrierDispatchShipment via XHR at document-start) and records every VIN + its shipment status/dates/route it sees. Passive recording makes NO requests of its own; a manual "Pull 2 wks → server" button does one extensive last-2-weeks pull (all statuses incl. Delivered) and POSTs {vin,order_name,status,eta} to shipments.wastake.com/api/tesla-status. Pops up a panel showing everything captured.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/dispatch-dashboard/tesla-dispatch-dashboard-recorder.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/dispatch-dashboard/tesla-dispatch-dashboard-recorder.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      shipments.wastake.com
// ==/UserScript==

/*
 * WHAT IT DOES (and does NOT do)
 *   - Hooks XMLHttpRequest at document-start and watches ONLY for the dashboard's own
 *     POST .../DispatchDashboard/GetCarrierDispatchShipment responses. It reads the JSON
 *     the browser already fetched — it never issues its own request to Tesla, so it adds
 *     ZERO extra load and nothing anomalous to detect ("piggyback").
 *   - For every VIN in that response it records: status, shipment #, service level,
 *     origin -> destination, pickup / need-by / ETA dates, ETA reason, alert ids, carrier id,
 *     and a derived dispatcher (from the origin state). Accumulates across every pull you
 *     look at, keyed by VIN, persisted in Tampermonkey storage (survives reloads).
 *   - Shows a floating dev panel with everything captured (search + status filter, counts,
 *     copy/download JSON, clear). NOTHING is sent to any server.
 *
 *   Tampermonkey menu: "Toggle recorder panel", "Clear recorded data".
 */

(function () {
  'use strict';

  // ---- config ----------------------------------------------------------------
  const STORE_KEY = 'dd_store_v1';
  const ENDPOINT = 'GetCarrierDispatchShipment';
  const ON_DASH = () => /\/logistics\/dispatchdashboard2/i.test(location.pathname);

  // "Pull 2 wks -> server" button: one extensive pull, then POST to shipment-creator.
  const SERVER_URL = 'https://shipments.wastake.com/api/tesla-status';
  const PULL_DAYS = 14;                          // last 2 weeks (by SHP create date)
  const PULL_STATUS_IDS = [9, 6, 12, 4];        // Tendered, Transit, At Destination, Delivered
  const PULL_ALERT_IDS = [1, 2, 3, 4, 5, 6, 7]; // all alert types (so delivered/no-action rows come through)
  // Auth captured off the page's OWN requests (never asked for) — used only by the manual button.
  let apiAuth = null, apiCarrier = null, apiUrl = null;
  // Normalize a stopStatusDescription to the token the server + App tab expect.
  function normStatus(desc) {
    const s = String(desc || '').toLowerCase();
    if (s.indexOf('deliver') > -1) return 'delivered';
    if (s.indexOf('transit') > -1) return 'transit';
    if (s.indexOf('tender') > -1) return 'tendered';
    if (s.indexOf('destination') > -1) return 'at_destination';
    return s;
  }

  // Dispatcher-by-pickup-state (mirrors shipment-creator/profiles.json + regular-fleet).
  const DISPATCHER_STATES = {
    Soyo:  ['CT','WI','UT','IL','IN','OH','MI','KY','TN','MS','AL','SC','NC','NJ','RI','MA','NH','VT','ME','NY','PA'],
    Kelly: ['VA','MD','GA','FL','DE','WV','DC'],
    Duka:  ['CA'],
    Burte: ['NV','AZ','NM','CO','ID','WY','MT','ND','SD','NE','KS','OK','MO','IA','MN','AR','LA','TX','OR','WA'],
  };
  const STATE_DISPATCHER = {};
  for (const name in DISPATCHER_STATES) for (const st of DISPATCHER_STATES[name]) STATE_DISPATCHER[st] = name;

  // Status pill colors.
  const STATUS_COLOR = {
    'Tendered':       { bg: '#fff4e5', fg: '#8a5000', bd: '#f2c98a' },
    'Transit':        { bg: '#e8f1ff', fg: '#0b4aa2', bd: '#a9c8f5' },
    'In Transit':     { bg: '#e8f1ff', fg: '#0b4aa2', bd: '#a9c8f5' },
    'At Destination': { bg: '#e6f7f4', fg: '#0a6b5e', bd: '#98d9cf' },
    'Delivered':      { bg: '#e7f6ea', fg: '#0a7d33', bd: '#9bd6ac' },
  };
  // Alert id -> label, from the portal's own getdispatchalertsbycarrier definitions endpoint.
  const ALERT_LABELS = {
    1: 'Pickup Date Late',
    2: 'Driver Needed',
    3: 'Late ETA',
    4: 'Incorrect Driver ETA',
    5: 'No Action Needed',
    6: 'ETA Today',
    7: 'Pickup Date Today',
  };

  // ---- store -----------------------------------------------------------------
  // { vins: { [vin]: record }, pulls: n, lastAt: ms, lastTotalCount: n }
  let store = load();
  function load() {
    try {
      const s = JSON.parse(GM_getValue(STORE_KEY, '') || '{}');
      if (!s.vins) s.vins = {};
      return s;
    } catch (e) { return { vins: {}, pulls: 0, lastAt: 0, lastTotalCount: 0 }; }
  }
  let saveTimer = null;
  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { try { GM_setValue(STORE_KEY, JSON.stringify(store)); } catch (e) {} }, 250);
  }
  function clearStore() {
    store = { vins: {}, pulls: 0, lastAt: 0, lastTotalCount: 0 };
    try { GM_deleteValue(STORE_KEY); } catch (e) {}
    scheduleRender();
  }

  // ---- helpers ---------------------------------------------------------------
  function originState(loc) {
    const m = String(loc || '').match(/(?:^|-)US-([A-Z]{2})(?:-|$)/);
    return m ? m[1] : '';
  }
  function fmt(iso) {
    if (!iso) return '';
    const [d, t] = String(iso).split('T');
    if (!d) return '';
    const p = d.split('-'); if (p.length < 3) return d;
    const hm = t ? t.slice(0, 5) : '';
    return (+p[1]) + '/' + (+p[2]) + (hm && hm !== '00:00' ? ' ' + hm : '');
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  // ---- capture (piggyback) ---------------------------------------------------
  function ingest(json) {
    let d;
    try { d = (typeof json === 'string' ? JSON.parse(json) : json).data; } catch (e) { return; }
    if (!d || !Array.isArray(d.shipmentList)) return;
    const now = Date.now();
    let added = 0;
    for (const ship of d.shipmentList) {
      for (const stop of (ship.stops || [])) {
        const st = originState(stop.originLocation);
        for (const v of (stop.vins || [])) {
          if (!v || !v.vin) continue;
          const prev = store.vins[v.vin];
          store.vins[v.vin] = {
            vin: v.vin,
            legId: v.legId,
            status: stop.stopStatusDescription,
            statusId: stop.stopStatusId,
            shipment: stop.shipmentNumber,
            shipmentId: stop.shipmentId,
            stopId: stop.stopId,
            service: stop.serviceLevelDescription,
            origin: stop.originLocation,
            dest: stop.destinationLocation,
            state: st,
            dispatcher: STATE_DISPATCHER[st] || '',
            pickup: stop.estimatedShipDate,
            ready: stop.readyDate,
            needBy: stop.needByDate,
            eta: stop.estimatedDeliveryDate,
            etaReason: stop.etaUpdateReason,
            alerts: stop.dispatchAlertIds || [],
            carrierId: stop.carrierId,
            firstSeen: prev ? prev.firstSeen : now,
            lastSeen: now,
            seen: prev ? (prev.seen || 1) + 1 : 1,
          };
          if (!prev) added++;
        }
      }
    }
    store.pulls = (store.pulls || 0) + 1;
    store.lastAt = now;
    if (typeof d.totalCount === 'number') store.lastTotalCount = d.totalCount;
    save();
    scheduleRender();
    if (added) updateBadge();
  }

  // Hook the PAGE's XHR (Tampermonkey shares the XHR prototype with the page, so this
  // catches Tesla's own requests). We only READ responses — we never open/send our own.
  (function hookXHR() {
    const W = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
    const XHR = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
    if (!XHR || XHR.__ddHooked) return;
    XHR.__ddHooked = true;
    const _open = XHR.open, _send = XHR.send, _set = XHR.setRequestHeader;
    XHR.open = function (m, u) { this.__ddUrl = u; return _open.apply(this, arguments); };
    // Grab the bearer token + carrier id off the page's own dispatch calls (for the manual pull button).
    XHR.setRequestHeader = function (k, v) {
      try {
        if (String(this.__ddUrl || '').indexOf(ENDPOINT) > -1) {
          const lk = String(k).toLowerCase();
          if (lk === 'authorization') apiAuth = v;
          else if (lk === 'x-selectedcarrierid') apiCarrier = v;
        }
      } catch (e) {}
      return _set.apply(this, arguments);
    };
    XHR.send = function () {
      try {
        if (String(this.__ddUrl || '').indexOf(ENDPOINT) > -1) {
          apiUrl = this.__ddUrl;
          this.addEventListener('load', function () {
            try { if (this.status >= 200 && this.status < 300) ingest(this.responseText); } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, arguments);
    };
  })();

  // ---- manual "Pull 2 wks -> server" -----------------------------------------
  // ONE deliberate extensive pull (all statuses incl. Delivered, last 2 weeks) using the
  // token captured above, then POST the {vin,order_name,status,eta} rows to our server.
  function setBtn(btn, txt) { if (btn) btn.textContent = txt; }
  async function pullAndSend(btn) {
    if (!apiAuth || !apiUrl) { setBtn(btn, 'load a search first'); return; }
    setBtn(btn, 'pulling…');
    const end = new Date();
    const start = new Date(end.getTime() - PULL_DAYS * 86400000);
    const body = { skip: 0, take: 5000, stopStatusIds: PULL_STATUS_IDS, selectedDispatchAlertIds: PULL_ALERT_IDS,
      createdDateStart: start.toISOString(), createdDateEnd: end.toISOString(), carrierId: null };
    let data;
    try {
      const res = await fetch(apiUrl, { method: 'POST',
        headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json',
                   'x-selectedCarrierId': apiCarrier || '' },
        body: JSON.stringify(body) });
      if (!res.ok) { setBtn(btn, 'pull HTTP ' + res.status); return; }
      data = await res.json();
    } catch (e) { setBtn(btn, 'pull failed'); return; }

    ingest(data);                                   // fold into the local store -> panel updates
    const records = [];
    ((data.data && data.data.shipmentList) || []).forEach(ship => (ship.stops || []).forEach(stop => (stop.vins || []).forEach(v => {
      if (v && v.vin) records.push({ vin: v.vin, order_name: stop.shipmentNumber,
        status: normStatus(stop.stopStatusDescription), eta: stop.estimatedDeliveryDate });
    })));
    if (!records.length) { setBtn(btn, 'no rows'); return; }
    setBtn(btn, 'sending ' + records.length + '…');
    GM_xmlhttpRequest({
      method: 'POST', url: SERVER_URL, timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ pulled_at: end.toISOString(), window_days: PULL_DAYS, records: records }),
      onload: (r) => setBtn(btn, r.status === 200 ? ('sent ' + records.length + ' ✓') : ('server ' + r.status)),
      onerror: () => setBtn(btn, 'send failed'),
      ontimeout: () => setBtn(btn, 'send timeout'),
    });
    setTimeout(() => { const b2 = root && root.getElementById('ddpull'); if (b2 && /sent|server|failed|timeout/.test(b2.textContent)) setTimeout(()=>setBtn(b2,'Pull 2 wks → server'), 4000); }, 100);
  }

  // ---- panel (shadow DOM) ----------------------------------------------------
  let host, root, mounted = false, open = false, renderTimer = null;
  let uiFilter = '', uiStatus = '';

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
    .launch { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
      background: #111; color: #fff; border: 0; border-radius: 999px; padding: 10px 14px;
      font-size: 13px; font-weight: 700; cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.3); }
    .launch .badge { display: inline-block; margin-left: 6px; background: #0a7d33; color: #fff;
      border-radius: 999px; padding: 1px 7px; font-size: 11px; }
    .wrap { position: fixed; right: 16px; bottom: 62px; z-index: 2147483647; width: min(1120px, 96vw);
      height: min(640px, 82vh); background: #fff; border: 1px solid #d9dee5; border-radius: 12px;
      box-shadow: 0 12px 40px rgba(0,0,0,.28); display: flex; flex-direction: column; overflow: hidden; }
    .hd { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: #111; color: #fff; }
    .hd h1 { font-size: 13px; margin: 0; font-weight: 800; letter-spacing: .02em; }
    .hd .meta { font-size: 11px; color: #b9c0c9; }
    .hd .spacer { flex: 1; }
    .hd button { background: #2a2f36; color: #fff; border: 0; border-radius: 6px; padding: 6px 10px;
      font-size: 12px; cursor: pointer; }
    .hd button:hover { background: #3a414a; }
    .hd button.go { background: #0a7d33; font-weight: 700; }
    .hd button.go:hover { background: #0b8f3a; }
    .hd button.danger:hover { background: #7a1f1f; }
    .bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid #eef1f4; flex-wrap: wrap; }
    .bar input { flex: 1; min-width: 180px; padding: 6px 9px; border: 1px solid #cdd3da; border-radius: 6px; font-size: 13px; }
    .chip { border: 1px solid #cdd3da; background: #f6f7f9; border-radius: 999px; padding: 4px 10px;
      font-size: 12px; cursor: pointer; user-select: none; }
    .chip.on { background: #111; color: #fff; border-color: #111; }
    .counts { font-size: 12px; color: #566; padding: 6px 12px; border-bottom: 1px solid #eef1f4; }
    .counts b { color: #111; }
    .scroll { flex: 1; overflow: auto; }
    table { border-collapse: collapse; width: 100%; font-size: 12px; }
    th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #f0f2f5; white-space: nowrap; }
    th { position: sticky; top: 0; background: #f6f7f9; z-index: 1; font-size: 11px; color: #556;
      text-transform: uppercase; letter-spacing: .03em; }
    tr:hover td { background: #fafbfc; }
    td.vin { font-family: ui-monospace, Menlo, Consolas, monospace; font-weight: 600; }
    td.route { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
    .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; border: 1px solid #ccc; }
    .empty { padding: 30px; text-align: center; color: #889; font-size: 13px; }
    .dim { color: #99a; }
  `;

  function mount() {
    if (mounted) return;
    host = document.createElement('div');
    host.id = 'dd-recorder-host';
    (document.body || document.documentElement).appendChild(host);
    root = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style'); style.textContent = CSS; root.appendChild(style);
    const launch = document.createElement('button');
    launch.className = 'launch';
    launch.innerHTML = 'DD Recorder <span class="badge" id="ddbadge">0</span>';
    launch.addEventListener('click', () => toggle());
    root.appendChild(launch);
    mounted = true;
    updateBadge();
  }

  function updateBadge() {
    if (!mounted) return;
    const b = root.getElementById('ddbadge');
    if (b) b.textContent = String(Object.keys(store.vins).length);
  }

  function toggle(force) {
    open = force == null ? !open : force;
    render();
  }

  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, 120);
  }

  function currentRows() {
    const q = uiFilter.trim().toLowerCase();
    let rows = Object.values(store.vins);
    if (uiStatus) rows = rows.filter(r => (r.status || '') === uiStatus);
    if (q) rows = rows.filter(r =>
      (r.vin || '').toLowerCase().includes(q) ||
      (r.shipment || '').toLowerCase().includes(q) ||
      (r.dispatcher || '').toLowerCase().includes(q) ||
      (r.origin || '').toLowerCase().includes(q) ||
      (r.dest || '').toLowerCase().includes(q) ||
      (r.status || '').toLowerCase().includes(q));
    rows.sort((a, b) => (b.lastSeen || 0) - (a.lastSeen || 0));
    return rows;
  }

  function statusPill(s) {
    const c = STATUS_COLOR[s] || { bg: '#f2f3f5', fg: '#556', bd: '#dce0e6' };
    return `<span class="pill" style="background:${c.bg};color:${c.fg};border-color:${c.bd}">${esc(s || '—')}</span>`;
  }

  // The panel shell (header, search box, chip/counts/scroll containers) is built ONCE, so
  // typing and live captures only refresh the data parts — the input keeps focus + cursor.
  let shellBuilt = false;

  function render() {
    if (!mounted) return;
    updateBadge();
    if (!open) { const w = root.querySelector('.wrap'); if (w) w.remove(); shellBuilt = false; return; }
    if (!shellBuilt) buildShell();
    renderBody();
  }

  function buildShell() {
    const wrap = document.createElement('div');
    wrap.className = 'wrap';
    wrap.innerHTML = `
      <div class="hd">
        <h1>DISPATCH DASHBOARD · RECORDER</h1>
        <span class="meta">dev</span>
        <span class="spacer"></span>
        <button id="ddpull" class="go" title="One extensive pull of the last 2 weeks (all statuses incl. Delivered), then POST to shipments.wastake.com/api/tesla-status">Pull 2 wks → server</button>
        <button id="ddcopy">Copy JSON</button>
        <button id="dddl">Download</button>
        <button id="ddclear" class="danger">Clear</button>
        <button id="ddclose">✕</button>
      </div>
      <div class="bar">
        <input id="ddsearch" placeholder="Search VIN / shipment / dispatcher / route / status…" />
        <span id="ddchips"></span>
      </div>
      <div class="counts" id="ddcounts"></div>
      <div class="scroll" id="ddscroll"></div>`;
    root.appendChild(wrap);

    const s = root.getElementById('ddsearch');
    s.value = uiFilter;
    s.addEventListener('input', () => { uiFilter = s.value; renderBody(); });
    root.getElementById('ddclose').addEventListener('click', () => toggle(false));
    root.getElementById('ddpull').addEventListener('click', (e) => pullAndSend(e.currentTarget));
    root.getElementById('ddclear').addEventListener('click', () => { if (confirm('Clear all recorded VINs?')) clearStore(); });
    root.getElementById('ddcopy').addEventListener('click', () => {
      const txt = JSON.stringify(Object.values(store.vins), null, 2);
      if (navigator.clipboard) navigator.clipboard.writeText(txt).then(() => flash('ddcopy', 'Copied ✓'), () => flash('ddcopy', 'Copy failed'));
    });
    root.getElementById('dddl').addEventListener('click', () => {
      const blob = new Blob([JSON.stringify(Object.values(store.vins), null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'dispatch-dashboard-vins.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    });
    shellBuilt = true;
  }

  function renderBody() {
    if (!shellBuilt) return;
    const rows = currentRows();
    const all = Object.values(store.vins);
    const byStatus = {};
    all.forEach(r => { byStatus[r.status || '—'] = (byStatus[r.status || '—'] || 0) + 1; });
    const statusList = Object.keys(byStatus).sort();
    const last = store.lastAt ? new Date(store.lastAt).toLocaleTimeString() : '—';

    const chips = root.getElementById('ddchips');
    chips.innerHTML =
      `<span class="chip ${uiStatus === '' ? 'on' : ''}" data-st="">All</span>` +
      statusList.map(s => `<span class="chip ${uiStatus === s ? 'on' : ''}" data-st="${esc(s)}">${esc(s)} ${byStatus[s]}</span>`).join('');
    chips.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => { uiStatus = ch.getAttribute('data-st'); renderBody(); }));

    root.getElementById('ddcounts').innerHTML =
      `<b>${all.length}</b> VINs captured · <b>${store.pulls || 0}</b> pulls piggybacked · ` +
      `last Tesla total: <b>${store.lastTotalCount || 0}</b> · last capture: <b>${last}</b> · showing <b>${rows.length}</b>`;

    root.getElementById('ddscroll').innerHTML = rows.length ? `
      <table>
        <thead><tr>
          <th>VIN</th><th>Status</th><th>Dispatcher</th><th>Shipment</th><th>Service</th>
          <th>Origin → Dest</th><th>Pickup</th><th>Need By</th><th>ETA</th>
          <th>ETA Reason</th><th>Alerts</th><th>Carrier</th><th>Seen</th>
        </tr></thead>
        <tbody>
        ${rows.map(r => `
          <tr>
            <td class="vin">${esc(r.vin)}</td>
            <td>${statusPill(r.status)}</td>
            <td>${esc(r.dispatcher) || '<span class="dim">—</span>'}</td>
            <td>${esc(r.shipment)}</td>
            <td>${esc(r.service)}</td>
            <td class="route" title="${esc(r.origin)}  →  ${esc(r.dest)}">${esc(r.state || '?')}: ${esc(shortLoc(r.origin))} <span class="dim">→</span> ${esc(shortLoc(r.dest))}</td>
            <td>${esc(fmt(r.pickup))}</td>
            <td>${esc(fmt(r.needBy))}</td>
            <td>${esc(fmt(r.eta))}</td>
            <td>${esc(r.etaReason || '')}</td>
            <td>${esc((r.alerts || []).map(a => ALERT_LABELS[a] || a).join(', '))}</td>
            <td>${esc(r.carrierId != null ? r.carrierId : '')}</td>
            <td class="dim">${r.seen || 1}×</td>
          </tr>`).join('')}
        </tbody>
      </table>` : `<div class="empty">Nothing captured yet. Load / search the Dispatch Dashboard and rows will appear here as the page fetches them.</div>`;
  }

  function shortLoc(s) {
    s = String(s || '');
    if (s.length <= 26) return s;
    return s.slice(0, 24) + '…';
  }
  function flash(id, msg) {
    const b = root.getElementById(id); if (!b) return;
    const t = b.textContent; b.textContent = msg;
    setTimeout(() => { if (b) b.textContent = t; }, 1200);
  }

  // ---- show/hide launcher on SPA nav ----------------------------------------
  function sync() {
    if (ON_DASH()) { mount(); if (host) host.style.display = ''; }
    else if (host) { host.style.display = 'none'; open = false; }
  }
  function hookNav() {
    const fire = () => setTimeout(sync, 60);
    const _ps = history.pushState, _rs = history.replaceState;
    history.pushState = function () { const r = _ps.apply(this, arguments); fire(); return r; };
    history.replaceState = function () { const r = _rs.apply(this, arguments); fire(); return r; };
    window.addEventListener('popstate', fire);
    setInterval(sync, 800); // fallback for framework nav we didn't catch
  }

  function boot() {
    hookNav();
    sync();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  // ---- Tampermonkey menu -----------------------------------------------------
  try {
    GM_registerMenuCommand('Toggle recorder panel', () => { mount(); if (host) host.style.display = ''; toggle(); });
    GM_registerMenuCommand('Clear recorded data', () => clearStore());
  } catch (e) {}
})();
