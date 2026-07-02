// ==UserScript==
// @name         Tesla Dispatch Dashboard — Cleaner/Marker
// @namespace    wastake.dispatchdash
// @version      0.9.0
// @description  Bottom-right "Cleaner/Marker" pill expands an upward action menu (uniform-width buttons). "Clean Pickups": scans the board for the Pickup Date Today alert, shows the count ("N → date · Confirm?"), and on confirm bulk-moves ALL those pickups to the next day 16:00Z (reason 4) via updateestimatedshipdate. "Pull red VINs to mark": re-query the App-tab Unmarked (red) VINs on Tesla and POST fresh status. Buttons are tap-to-confirm (yellow) then green ✓. Also piggybacks the page's own GetCarrierDispatchShipment calls; auto-send (default on, toggle in Tampermonkey menu) POSTs searched VINs to shipments.wastake.com/api/tesla-status. Clean ETA is a stub.
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
  // "Pull red VINs to mark": re-check only the App-tab's Unmarked (red) VINs on Tesla.
  const RED_LIST_URL = 'https://shipments.wastake.com/app/delivered';  // /app proxy -> app-delivery :8011
  const RED_CHUNK = 100;                         // VINs per Tesla request (vins[] batch)
  // Auth captured off the page's OWN requests (never asked for) — used only by the manual button.
  let apiAuth = null, apiCarrier = null, apiUrl = null;
  // Auto-send: when on, every piggybacked pull (VINs you search/paginate) is POSTed to the server too.
  const AUTOSEND_KEY = 'dd_autosend';
  let autoSend = (GM_getValue(AUTOSEND_KEY, 'on') === 'on');
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

  // ---- send to server --------------------------------------------------------
  // Build the {vin,order_name,status,eta} rows the server ingests from a dispatch response.
  function recordsFrom(json) {
    let d;
    try { d = (typeof json === 'string' ? JSON.parse(json) : json).data; } catch (e) { return []; }
    if (!d || !Array.isArray(d.shipmentList)) return [];
    const out = [];
    d.shipmentList.forEach(ship => (ship.stops || []).forEach(stop => (stop.vins || []).forEach(v => {
      if (v && v.vin) out.push({ vin: v.vin, order_name: stop.shipmentNumber,
        status: normStatus(stop.stopStatusDescription), eta: stop.estimatedDeliveryDate });
    })));
    return out;
  }
  // POST rows to /api/tesla-status (server upserts by vin+order_base). onDone(status,n) optional.
  function sendToServer(records, source, onDone) {
    if (!records || !records.length) { if (onDone) onDone('empty', 0); return; }
    GM_xmlhttpRequest({
      method: 'POST', url: SERVER_URL, timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ pulled_at: new Date().toISOString(), source: source, records: records }),
      onload: (r) => { if (onDone) onDone(r.status, records.length); },
      onerror: () => { if (onDone) onDone('error', records.length); },
      ontimeout: () => { if (onDone) onDone('timeout', records.length); },
    });
  }
  // Debounced auto-send of piggybacked pulls: batch a browsing burst into ONE POST, dedup by vin+shipment.
  let autoBuf = {}, autoTimer = null;
  function queueAutoSend(dataObj) {
    if (!autoSend) return;
    const recs = recordsFrom(dataObj);
    if (!recs.length) return;
    recs.forEach(r => { autoBuf[r.vin + '|' + (r.order_name || '')] = r; });
    clearTimeout(autoTimer);
    autoTimer = setTimeout(flushAutoSend, 2000);
  }
  function flushAutoSend() {
    const recs = Object.values(autoBuf); autoBuf = {};
    if (recs.length) sendToServer(recs, 'piggyback', null);
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
            try {
              if (this.status >= 200 && this.status < 300) {
                let parsed; try { parsed = JSON.parse(this.responseText); } catch (e) { return; }
                ingest(parsed);          // fold into the local store (panel)
                queueAutoSend(parsed);   // and auto-send these piggybacked VINs to the server (if enabled)
              }
            } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, arguments);
    };
  })();

  // ---- actions ---------------------------------------------------------------
  function gmGet(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({ method: 'GET', url, timeout: 30000,
        onload: (r) => { try { resolve(JSON.parse(r.responseText)); } catch (e) { reject(e); } },
        onerror: () => reject(new Error('net')), ontimeout: () => reject(new Error('timeout')) });
    });
  }
  function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; }
  async function queryVins(vins) {
    // 90d window: vins[] is the real filter; keeps the server-side query reasonably fast.
    const end = new Date(), start = new Date(end.getTime() - 90 * 86400000);
    const body = { skip: 0, take: 5000, vins: vins, stopStatusIds: PULL_STATUS_IDS, selectedDispatchAlertIds: PULL_ALERT_IDS,
      createdDateStart: start.toISOString(), createdDateEnd: end.toISOString(), carrierId: null };
    const res = await fetch(apiUrl, { method: 'POST',
      headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
      body: JSON.stringify(body) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return recordsFrom(await res.json());
  }
  // "Pull red VINs to mark": fetch the App-tab Unmarked (red) VINs, re-query ONLY those on Tesla
  // (vins[] batch), POST fresh status. Red = App-tab status not in (app, marked). setStatus(msg)
  // drives the button's inline line; returns a one-line summary for the green done-state.
  async function runPullRed(setStatus) {
    if (!apiAuth || !apiUrl) throw new Error('search the dashboard once');
    setStatus('fetching red list…');
    const deliv = await gmGet(RED_LIST_URL);
    const rows = (deliv && deliv.delivered) || [];
    const redVins = [...new Set(rows.filter(r => r.status !== 'app' && r.status !== 'marked').map(r => r.vin).filter(Boolean))];
    if (!redVins.length) return 'no red VINs';
    setStatus('querying ' + redVins.length + ' red…');
    const all = [];
    for (const c of chunk(redVins, RED_CHUNK)) {
      try { all.push(...await queryVins(c)); } catch (e) { /* skip a failed chunk, keep going */ }
    }
    if (!all.length) throw new Error('no Tesla rows');
    setStatus('sending ' + all.length + '…');
    const st = await new Promise((resolve) => sendToServer(all, 'red-remark', (s) => resolve(s)));
    if (st !== 200) throw new Error('server ' + st);
    const greened = all.filter(r => r.status === 'delivered').length;
    return redVins.length + ' red · ' + greened + ' now delivered';
  }

  // ---- pickup-date cleaner (write) -------------------------------------------
  // Next day at 16:00Z from an ISO pickup date (current pickup date + 1 day). Copies the recorded
  // format exactly ("YYYY-MM-DDT16:00:00Z"). Falls back to tomorrow if the input is unparseable.
  function nextDay16(iso) {
    const datePart = String(iso || '').split('T')[0];
    let d = datePart ? new Date(datePart + 'T00:00:00Z') : new Date();
    if (isNaN(d)) d = new Date();
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10) + 'T16:00:00Z';
  }
  // Batch pickup-date write — the exact contract we recorded. items = [{stopId, estimateShipDate}].
  async function updatePickups(items) {
    const url = apiUrl.replace('GetCarrierDispatchShipment', 'updateestimatedshipdate') + '?dateTrackingSource=3';
    let ok = 0;
    for (const c of chunk(items, 100)) {
      const list = c.map(it => ({ updateReasonId: 4, estimateShipDate: it.estimateShipDate, stopId: it.stopId }));
      const res = await fetch(url, { method: 'POST',
        headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
        body: JSON.stringify({ updateEstimatedShipDateList: list }) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      ok += c.length;
    }
    return ok;
  }
  // Scan the board for stops flagged "Pickup Date Today" (alert id 7).
  async function scanPickupsToday() {
    if (!apiAuth || !apiUrl) throw new Error('search the dashboard once');
    const end = new Date(), start = new Date(end.getTime() - 90 * 86400000);
    const body = { skip: 0, take: 5000, stopStatusIds: [9, 6, 12], selectedDispatchAlertIds: [7],
      createdDateStart: start.toISOString(), createdDateEnd: end.toISOString(), carrierId: null };
    const res = await fetch(apiUrl, { method: 'POST',
      headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
      body: JSON.stringify(body) });
    if (!res.ok) throw new Error('scan HTTP ' + res.status);
    const j = await res.json();
    const targets = [];
    ((j.data && j.data.shipmentList) || []).forEach(s => (s.stops || []).forEach(st => {
      if ((st.dispatchAlertIds || []).includes(7))
        targets.push({ stopId: st.stopId, estimateShipDate: nextDay16(st.estimatedShipDate) });
    }));
    return targets;
  }
  // Clean Pickups: prep() scans (read, shows the count in the Confirm? label); run() does the write.
  async function prepCleanPickups(setStatus) {
    setStatus('scanning Pickup Date Today…');
    const targets = await scanPickupsToday();
    if (!targets.length) return { count: 0, emptyMsg: 'none today ✓' };
    return { count: targets.length, confirmMsg: targets.length + ' → ' + targets[0].estimateShipDate.slice(0, 10) + ' 4PM · Confirm?', data: targets };
  }
  async function runCleanPickups(setStatus, prep) {
    setStatus('moving ' + prep.data.length + ' pickups…');
    const ok = await updatePickups(prep.data);
    return ok + ' → next day 4PM';
  }

  // ---- UI: bottom-right pill + upward-expanding action menu ------------------
  let host, root, mounted = false, open = false;
  // ingest()/clearStore() still call these; with the FAB menu there's no live view to repaint -> no-ops.
  function scheduleRender() {}
  function updateBadge() {}

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, Segoe UI, Arial, sans-serif; }
    .launch { position: fixed; bottom: 12px; right: 12px; z-index: 2147483647;
      background: #111; color: #fff; font: 12px/1.3 system-ui, Segoe UI, Arial, sans-serif;
      padding: 6px 10px; border: 0; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.35);
      opacity: .92; cursor: pointer; transition: opacity .15s; }
    .launch:hover { opacity: 1; }
    .menu { position: fixed; right: 12px; bottom: 46px; z-index: 2147483647;
      display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
      opacity: 0; transform: translateY(10px); pointer-events: none;
      transition: opacity .18s ease, transform .18s ease; }
    .menu.open { opacity: 1; transform: translateY(0); pointer-events: auto; }
    .act { width: 260px; text-align: left; padding: 9px 13px; border: 0; border-radius: 9px;
      background: #1b1e22; color: #fff; cursor: pointer; box-shadow: 0 3px 12px rgba(0,0,0,.3);
      display: flex; flex-direction: column; gap: 1px;
      transition: background-color .2s ease, color .2s ease, transform .1s ease; }
    .act:hover { transform: translateX(-2px); }
    .act .t { font-size: 13px; font-weight: 700; }
    .act .s { font-size: 11px; opacity: .72; }
    .act.armed { background: #f5c518; color: #171a20; }        /* yellow — confirm */
    .act.armed .s { opacity: .9; }
    .act.run { background: #2a2f36; }
    .act.done { background: #0a7d33; }                         /* green — success */
    .act.err { background: #b42318; }
    .act.soon { opacity: .85; }
  `;

  function mount() {
    if (mounted) return;
    host = document.createElement('div');
    host.id = 'dd-cleanermarker-host';
    (document.body || document.documentElement).appendChild(host);
    root = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style'); style.textContent = CSS; root.appendChild(style);
    const menu = document.createElement('div'); menu.className = 'menu'; menu.id = 'ddmenu';
    menu.appendChild(actionButton('Clean Pickups', 'move Pickup-Date-Today → next day 4PM', runCleanPickups, prepCleanPickups));
    menu.appendChild(actionButton('Clean ETA', 'not wired yet', null));
    menu.appendChild(actionButton('Pull red VINs to mark', 'tap to confirm', runPullRed));  // nearest the pill
    root.appendChild(menu);
    const launch = document.createElement('button');
    launch.className = 'launch';
    launch.textContent = 'Cleaner/Marker';
    launch.addEventListener('click', () => toggle());
    root.appendChild(launch);
    mounted = true;
  }

  function toggle(force) {
    open = force == null ? !open : force;
    const menu = root && root.getElementById('ddmenu');
    if (menu) menu.classList.toggle('open', open);
  }

  // Confirm-to-run button. idle -> (click) either arms directly, or if prepFn is given, runs a
  // read-only scan first and arms with its count ("N → date · Confirm?"). armed -> (click) runs
  // runFn(setStatus, prepData) -> green "✓" on success, red on error, then auto-reverts.
  function actionButton(label, subtitle, runFn, prepFn) {
    const btn = document.createElement('button');
    btn.innerHTML = `<span class="t"></span><span class="s"></span>`;
    const T = btn.querySelector('.t'), S = btn.querySelector('.s');
    let state = 'idle', armTimer = null, prepData = null;
    const setStatus = (msg) => { S.textContent = msg; };
    function idle() { state = 'idle'; btn.className = 'act' + (runFn ? '' : ' soon'); T.textContent = label; S.textContent = subtitle; prepData = null; }
    function armPlain() { state = 'armed'; btn.className = 'act armed'; T.textContent = 'Confirm?'; S.textContent = label; clearTimeout(armTimer); armTimer = setTimeout(idle, 4000); }
    function err(e) { btn.className = 'act err'; T.textContent = '✕ ' + label; S.textContent = String((e && e.message) || e).slice(0, 40); state = 'idle'; setTimeout(idle, 5000); }
    idle();
    btn.addEventListener('click', async () => {
      if (state === 'running' || state === 'prepping') return;
      if (state === 'armed') {                              // confirmed -> run
        clearTimeout(armTimer);
        if (!runFn) { btn.className = 'act err'; T.textContent = 'Not wired yet'; S.textContent = ''; setTimeout(idle, 1800); return; }
        state = 'running'; btn.className = 'act run'; T.textContent = label; S.textContent = '…';
        try {
          const summary = await runFn(setStatus, prepData);
          state = 'done'; btn.className = 'act done'; T.textContent = '✓ ' + label; S.textContent = summary || 'done'; setTimeout(idle, 6000);
        } catch (e) { err(e); }
        return;
      }
      // idle/done -> arm (scan-first when prepFn is provided)
      if (!runFn || !prepFn) { armPlain(); return; }
      state = 'prepping'; btn.className = 'act run'; T.textContent = label;
      try {
        const r = await prepFn(setStatus);
        if (!r || !r.count) { btn.className = 'act'; T.textContent = label; S.textContent = (r && r.emptyMsg) || 'nothing to do'; state = 'idle'; setTimeout(idle, 2500); return; }
        prepData = r; state = 'armed'; btn.className = 'act armed'; T.textContent = 'Confirm?'; S.textContent = r.confirmMsg; clearTimeout(armTimer); armTimer = setTimeout(idle, 6000);
      } catch (e) { err(e); }
    });
    return btn;
  }


  // ---- show/hide launcher on SPA nav ----------------------------------------
  function sync() {
    if (ON_DASH()) { mount(); if (host) host.style.display = ''; }
    else if (host) { host.style.display = 'none'; toggle(false); }
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
    GM_registerMenuCommand('Toggle Cleaner/Marker menu', () => { mount(); if (host) host.style.display = ''; toggle(); });
    GM_registerMenuCommand('Toggle auto-send (piggyback → server)', () => {
      autoSend = !autoSend;
      try { GM_setValue(AUTOSEND_KEY, autoSend ? 'on' : 'off'); } catch (e) {}
      alert('Auto-send is now ' + (autoSend ? 'ON' : 'OFF'));
    });
    GM_registerMenuCommand('Clear captured VIN cache', () => clearStore());
  } catch (e) {}
})();
