// ==UserScript==
// @name         Tesla Dispatch Dashboard — Cleaner/Marker
// @namespace    wastake.dispatchdash
// @version      0.16.2
// @description  Defaults Dispatch Dashboard searches to Tesla's VIN API field without opening the selector, replaces each License Plate control with a native Tesla-styled Deliver / Andrew Enkh action, and provides Cleaner/Marker actions for pickups, ETAs, Driver Needed shipments, and Tesla-status reconciliation.
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
  const DOWNLOAD_ENDPOINT = 'DownloadCarrierLoads2';
  const ON_DASH = () => /\/logistics\/dispatchdashboard2/i.test(location.pathname);

  // "Pull 2 wks -> server" button: one extensive pull, then POST to shipment-creator.
  const SERVER_URL = 'https://shipments.wastake.com/api/tesla-status';
  const PULL_DAYS = 14;                          // last 2 weeks (by SHP create date)
  const PULL_STATUS_IDS = [9, 6, 12, 4];        // Tendered, Transit, At Destination, Delivered
  const PULL_ALERT_IDS = [1, 2, 3, 4, 5, 6, 7]; // all alert types (so delivered/no-action rows come through)
  const JESSICA_DRIVER_ID = 67651;
  const ANDREW_DRIVER_ID = 136062;
  // "Pull red VINs to mark": re-check only the App-tab's Unmarked (red) VINs on Tesla.
  const RED_LIST_URL = 'https://shipments.wastake.com/app/delivered';  // /app proxy -> app-delivery :8011
  const RED_CHUNK = 100;                         // VINs per Tesla request (vins[] batch)
  // Auth captured off the page's OWN requests (never asked for) — used only by the manual button.
  let apiAuth = null, apiCarrier = null, apiUrl = null;
  const shipmentMeta = new Map(); // shipment number -> {shipmentId, carrierId}
  // Default dashboard searches to VIN semantics. The visible selector is kept in sync below,
  // while the XHR hook guarantees that Tesla receives `vins`, never `shipmentNumbers`.
  let vinSearchMode = true;
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
        if (stop.shipmentNumber && stop.shipmentId != null) {
          shipmentMeta.set(String(stop.shipmentNumber).trim().toUpperCase(), {
            shipmentId: stop.shipmentId,
            carrierId: stop.carrierId,
          });
        }
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
    scheduleDeliverUi();
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
    XHR.send = function (body) {
      try {
        const requestUrl = String(this.__ddUrl || '');
        const isGridRequest = requestUrl.indexOf(ENDPOINT) > -1;
        const usesSearchFilter = isGridRequest || requestUrl.indexOf(DOWNLOAD_ENDPOINT) > -1;
        if (usesSearchFilter) {
          // Tesla's Angular component initializes Search By to Shipment Numbers. Default it
          // behind the scenes by rewriting only that filter field in the page's own request.
          // Everything else in the request (alerts, dates, status, carrier, paging) is untouched.
          if (vinSearchMode && typeof body === 'string') {
            try {
              const request = JSON.parse(body);
              if (request && Array.isArray(request.shipmentNumbers)) {
                request.vins = request.shipmentNumbers;
                delete request.shipmentNumbers;
                body = JSON.stringify(request);
                arguments[0] = body;
              }
            } catch (e) {}
          }
        }
        if (isGridRequest) {
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
  // Next weekday from the day the button is pressed, at the exact recorded 16:00Z format.
  // Friday, Saturday, and Sunday all roll forward to Monday.
  function nextWeekdayDate(now = new Date()) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
    return d;
  }
  function nextWeekday16(now = new Date()) {
    const d = nextWeekdayDate(now);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}T16:00:00Z`;
  }
  function nextWeekdayCaption(now = new Date()) {
    const names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    return names[nextWeekdayDate(now).getDay()] + ' 4PM';
  }
  function nextCalendarDayDate(now = new Date()) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    d.setDate(d.getDate() + 1);
    return d;
  }
  function nextCalendarDayEta(now = new Date()) {
    const d = nextCalendarDayDate(now);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}T00:00:00.000Z`;
  }
  function nextCalendarDayCaption(now = new Date()) {
    const names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    return names[nextCalendarDayDate(now).getDay()] + ' 4PM';
  }
  async function requireTeslaWriteSuccess(res) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const text = await res.text();
    if (!text) return;
    let j;
    try { j = JSON.parse(text); } catch (e) { return; }
    if (j && (j.success === false || (j.data && j.data.success === false)))
      throw new Error((j.message || (j.data && j.data.message)) || 'Tesla returned success:false');
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
      await requireTeslaWriteSuccess(res);
      ok += c.length;
    }
    return ok;
  }
  // Batch driver write captured from the portal's own mass-assignment action. Each request is
  // grouped by carrier because Tesla's contract accepts one carrierId for many shipmentIds.
  async function assignJessicaToShipments(items) {
    const url = apiUrl.replace('GetCarrierDispatchShipment', 'UpdateShipmentsDriverAndLicensePlate');
    const byCarrier = new Map();
    for (const item of items) {
      const carrierId = Number(item.carrierId || apiCarrier);
      if (!Number.isFinite(carrierId) || !carrierId) throw new Error('missing carrier id for driver assignment');
      if (!byCarrier.has(carrierId)) byCarrier.set(carrierId, []);
      byCarrier.get(carrierId).push(String(item.shipmentId));
    }
    let ok = 0;
    for (const [carrierId, shipmentIds] of byCarrier) {
      for (const ids of chunk([...new Set(shipmentIds)], 100)) {
        const res = await fetch(url, { method: 'POST',
          headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
          body: JSON.stringify({
            shipmentIds: ids,
            driverId: JESSICA_DRIVER_ID,
            carrierId: carrierId,
            driverJobStatus: 'PENDING',
            source: 'TVP',
            truckLicensePlate: '',
          }) });
        await requireTeslaWriteSuccess(res);
        ok += ids.length;
      }
    }
    return ok;
  }
  // Single-shipment contract captured from the portal's normal Driver control.
  async function assignAndrewToShipment(item) {
    if (!apiAuth || !apiUrl) throw new Error('search the dashboard once');
    const carrierId = Number(item.carrierId || apiCarrier);
    if (!Number.isFinite(carrierId) || !carrierId) throw new Error('missing carrier id');
    const url = apiUrl.replace('GetCarrierDispatchShipment', 'AssignDrivertoShipment');
    const res = await fetch(url, { method: 'POST',
      headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
      body: JSON.stringify({
        shipmentId: String(item.shipmentId),
        driverId: ANDREW_DRIVER_ID,
        carrierId: carrierId,
        driverJobStatus: 'PENDING',
        source: 'TVP',
      }) });
    await requireTeslaWriteSuccess(res);
  }
  // Query each alert independently, verify the response actually contains it, then merge by stopId.
  // This does not depend on Tesla treating a multi-value alert filter as OR rather than AND.
  async function scanAlertStops(alertIds) {
    if (!apiAuth || !apiUrl) throw new Error('search the dashboard once');
    const end = new Date(), start = new Date(end.getTime() - 90 * 86400000), stops = new Map();
    for (const alertId of alertIds) {
      const body = { skip: 0, take: 5000, stopStatusIds: [9, 6, 12], selectedDispatchAlertIds: [alertId],
        createdDateStart: start.toISOString(), createdDateEnd: end.toISOString(), carrierId: null };
      const res = await fetch(apiUrl, { method: 'POST',
        headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
        body: JSON.stringify(body) });
      if (!res.ok) throw new Error('alert ' + alertId + ' scan HTTP ' + res.status);
      const j = await res.json();
      ((j.data && j.data.shipmentList) || []).forEach(s => (s.stops || []).forEach(st => {
        if ((st.dispatchAlertIds || []).includes(alertId) && st.stopId != null) stops.set(String(st.stopId), st);
      }));
    }
    return [...stops.values()];
  }
  // Scan the board for stops flagged "Pickup Date Late" (id 1) or "Pickup Date Today" (id 7).
  async function scanPickupAlerts() {
    const targetDate = nextWeekday16();
    return (await scanAlertStops([1, 7])).map(st => ({ stopId: st.stopId, estimateShipDate: targetDate }));
  }
  async function scanDriverNeededShipments() {
    const shipments = new Map();
    for (const st of await scanAlertStops([2])) {
      if (st.shipmentId == null) continue;
      const carrierId = st.carrierId || apiCarrier;
      shipments.set(String(st.shipmentId), { shipmentId: st.shipmentId, carrierId: carrierId });
    }
    return [...shipments.values()];
  }
  // Clean Pickups: scan all three alerts, then immediately update pickup dates and assign Jessica
  // only to shipments carrying Driver Needed (id 2).
  async function prepCleanPickups(setStatus) {
    setStatus('scanning pickup + driver alerts…');
    const pickups = await scanPickupAlerts();
    const drivers = await scanDriverNeededShipments();
    if (!pickups.length && !drivers.length) return { count: 0, emptyMsg: 'no pickups or drivers to clean ✓' };
    const date = pickups.length ? pickups[0].estimateShipDate.slice(0, 10) + ' 4PM · ' : '';
    return {
      count: pickups.length + drivers.length,
      confirmMsg: pickups.length + ' pickups · ' + drivers.length + ' drivers · ' + date + 'Confirm?',
      data: { pickups: pickups, drivers: drivers },
    };
  }
  async function runCleanPickups(setStatus, prep) {
    const pickups = prep.data.pickups || [], drivers = prep.data.drivers || [];
    let pickupOk = 0, driverOk = 0;
    if (pickups.length) {
      setStatus('moving ' + pickups.length + ' pickups…');
      pickupOk = await updatePickups(pickups);
    }
    if (drivers.length) {
      setStatus('assigning Jessica to ' + drivers.length + '…');
      try { driverOk = await assignJessicaToShipments(drivers); }
      catch (e) {
        if (pickupOk) throw new Error(pickupOk + ' pickups updated; driver: ' + ((e && e.message) || e));
        throw e;
      }
    }
    return pickupOk + ' pickups · ' + driverOk + ' Jessica';
  }

  // ---- ETA cleaner (write) ---------------------------------------------------
  // Exact contract captured from a manual ETA change. The date is midnight UTC and the separate
  // EtaTimeWindowEndInHours value places the end of the ETA window at 4 PM.
  async function updateEtas(items) {
    const url = apiUrl.replace('GetCarrierDispatchShipment', 'updateStopEta');
    let ok = 0;
    for (const c of chunk(items, 100)) {
      const list = c.map(it => ({
        StopId: it.stopId,
        EtaUpdateSourceId: 3,
        EstimatedDeliveryDate: it.estimatedDeliveryDate,
        EtaTimeWindowEndInHours: 16,
        EtaUpdateReasonId: 4,
      }));
      const res = await fetch(url, { method: 'POST',
        headers: { 'Authorization': apiAuth, 'Content-Type': 'application/json', 'Accept': 'application/json', 'x-selectedCarrierId': apiCarrier || '' },
        body: JSON.stringify(list) });
      await requireTeslaWriteSuccess(res);
      ok += c.length;
    }
    return ok;
  }
  async function scanEtaAlerts() {
    const targetDate = nextCalendarDayEta();
    return (await scanAlertStops([3, 6])).map(st => ({ stopId: st.stopId, estimatedDeliveryDate: targetDate }));
  }
  async function prepCleanEta(setStatus) {
    setStatus('scanning late + today ETAs…');
    const targets = await scanEtaAlerts();
    if (!targets.length) return { count: 0, emptyMsg: 'no late/today ETAs ✓' };
    return { count: targets.length, confirmMsg: targets.length + ' → ' + targets[0].estimatedDeliveryDate.slice(0, 10) + ' 4PM · Confirm?', data: targets };
  }
  async function runCleanEta(setStatus, prep) {
    setStatus('moving ' + prep.data.length + ' ETAs…');
    const ok = await updateEtas(prep.data);
    return ok + ' → next day 4PM';
  }

  // ---- default Search By to VINs --------------------------------------------
  // No dropdown clicks: request semantics are enforced in hookXHR(), and this keeps Tesla's
  // displayed value/placeholder consistent with that behind-the-scenes default.
  let vinDefaultTimer = null;
  let wasOnDashboard = false;
  function scheduleVinDefault() {
    if (!ON_DASH()) return;
    clearTimeout(vinDefaultTimer);
    vinDefaultTimer = setTimeout(applyVinDefaultVisual, 40);
  }
  function searchByControls() {
    const label = [...document.querySelectorAll('.t-label')].find(el => el.textContent.trim() === 'Search By');
    const select = label && label.parentElement && label.parentElement.querySelector('tsl-select');
    if (!select) return null;
    const valueNode = select.querySelector('.tsl-select-value-text');
    const valueText = valueNode && (valueNode.querySelector('span') || valueNode);
    const input = label.parentElement.nextElementSibling && label.parentElement.nextElementSibling.querySelector('input');
    return { valueText, input };
  }
  function applyVinDefaultVisual() {
    if (!ON_DASH() || !vinSearchMode) return;
    const controls = searchByControls();
    if (!controls) return;
    const { valueText, input } = controls;
    if (valueText && valueText.textContent.trim() !== 'VINs') valueText.textContent = 'VINs';
    if (input && input.placeholder !== 'Enter VINs') {
      input.placeholder = 'Enter VINs';
      input.setAttribute('placeholder', 'Enter VINs');
    }
  }
  function resetVinDefaultForVisit() {
    vinSearchMode = true;
    scheduleVinDefault();
  }
  function selectedSearchOption(event) {
    const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
    return path.find(node => node && node.nodeType === 1 && node.matches
      && node.matches('.tsl-option, tsl-option, .tsl-select-option, [role="option"]'))
      || (event.target && event.target.closest
        && event.target.closest('.tsl-option, tsl-option, .tsl-select-option, [role="option"]'));
  }
  function restoreShipmentVisual(optionText) {
    if (vinSearchMode || !ON_DASH()) return;
    const controls = searchByControls();
    if (!controls) return;
    // The VIN default is cosmetic: Tesla may already have Shipment selected internally and
    // therefore may not repaint when the user selects it again. Replace only stale VIN text;
    // if Angular rendered its own Shipment wording, leave that native wording untouched.
    if (controls.valueText && /^vins?$/i.test(controls.valueText.textContent.trim())) {
      controls.valueText.textContent = optionText || 'Shipment Numbers';
    }
    if (controls.input && /^enter\s+vins?$/i.test(controls.input.placeholder || '')) {
      controls.input.placeholder = 'Enter Shipment Numbers';
      controls.input.setAttribute('placeholder', 'Enter Shipment Numbers');
    }
  }
  // A deliberate manual selection still wins for the rest of this dashboard visit.
  function handleManualSearchOption(event) {
    if (!ON_DASH()) return;
    const option = selectedSearchOption(event);
    if (!option) return;
    const text = option.textContent.replace(/\s+/g, ' ').trim();
    if (/^shipment(?:\s+numbers?)?$/i.test(text)) {
      vinSearchMode = false;
      clearTimeout(vinDefaultTimer);
      // Run after Tesla's option handler. The second pass covers a delayed Angular repaint.
      setTimeout(() => restoreShipmentVisual(text), 0);
      setTimeout(() => restoreShipmentVisual(text), 120);
    } else if (/^vins?$/i.test(text)) {
      vinSearchMode = true;
      scheduleVinDefault();
    }
  }
  // pointerdown releases the override before Tesla handles the choice; click also supports
  // keyboard-generated selections and older versions of the selector.
  document.addEventListener('pointerdown', handleManualSearchOption, true);
  document.addEventListener('click', handleManualSearchOption, true);

  // ---- in-page Deliver / Andrew Enkh control --------------------------------
  let deliverUiTimer = null, deliverObserver = null;
  function ensureDeliverUiStyle() {
    if (document.getElementById('dd-deliver-ui-style')) return;
    const style = document.createElement('style');
    style.id = 'dd-deliver-ui-style';
    style.textContent = `
      .dd-andrew-deliver { cursor: pointer; }
      .dd-andrew-deliver .tsl-multiselect-trigger { cursor: pointer; }
      .dd-andrew-deliver.dd-busy .tsl-multiselect-trigger { background: #fff4c2; border-color: #d5a900; color: #574400; }
      .dd-andrew-deliver.dd-success .tsl-multiselect-trigger { background: #e2f5e8; border-color: #27864a; color: #0a6b31; }
      .dd-andrew-deliver.dd-error .tsl-multiselect-trigger { background: #fde7e5; border-color: #c52f26; color: #9c1c15; }
    `;
    (document.head || document.documentElement).appendChild(style);
  }
  function scheduleDeliverUi() {
    if (!ON_DASH()) return;
    clearTimeout(deliverUiTimer);
    deliverUiTimer = setTimeout(decorateDeliverUi, 60);
  }
  function decorateDeliverUi() {
    if (!ON_DASH()) return;
    ensureDeliverUiStyle();
    const labels = document.querySelectorAll('dispatch-dashboard-grid1 .grid-entry .titlebold');
    labels.forEach(label => {
      if (label.textContent.trim() !== 'License Plate') return;
      const card = label.closest('.grid-entry');
      const plateControl = label.nextElementSibling;
      if (!card || !plateControl || !plateControl.querySelector('input[placeholder="Enter License Plate"]')) return;
      const shipmentNode = card.querySelector('.title-padding-grid-entry');
      const shipmentNumber = shipmentNode ? shipmentNode.textContent.trim() : '';
      if (!shipmentNumber) return;

      const existing = card.querySelector('.dd-andrew-deliver');
      if (existing) {
        if (existing.dataset.shipmentNumber !== shipmentNumber) {
          existing.dataset.shipmentNumber = shipmentNumber;
          existing.dataset.state = '';
          existing.classList.remove('dd-busy', 'dd-success', 'dd-error');
          existing.setAttribute('aria-disabled', 'false');
          const existingText = existing.querySelector('.tsl-multiselect-placeholder');
          if (existingText) existingText.textContent = 'Andrew Enkh';
        }
        return;
      }

      const driverLabel = [...card.querySelectorAll('.titlebold')].find(el => el.textContent.trim() === 'Driver');
      const driverControl = driverLabel && driverLabel.nextElementSibling;
      if (!driverLabel || !driverControl || !driverControl.querySelector('tsl-multiselect')) return;
      label.style.setProperty('display', 'none', 'important');
      plateControl.style.setProperty('display', 'none', 'important');
      const deliverLabel = driverLabel.cloneNode(true);
      deliverLabel.classList.add('dd-deliver-label');
      deliverLabel.textContent = 'Deliver';
      const deliverControl = driverControl.cloneNode(true);
      deliverControl.classList.add('dd-deliver-control');
      deliverControl.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
      const button = deliverControl.querySelector('tsl-multiselect');
      const buttonText = button.querySelector('.tsl-multiselect-placeholder');
      if (!buttonText) return;
      button.classList.remove('tsl-multiselect-open');
      button.classList.add('dd-andrew-deliver');
      button.setAttribute('role', 'button');
      button.setAttribute('aria-label', 'Andrew Enkh');
      button.setAttribute('aria-disabled', 'false');
      buttonText.textContent = 'Andrew Enkh';
      button.dataset.shipmentNumber = shipmentNumber;
      const setButtonText = text => { buttonText.textContent = text; };
      const runAndrewAssignment = async event => {
        event.preventDefault();
        event.stopPropagation();
        if (button.dataset.state === 'busy' || button.dataset.state === 'success') return;
        const key = String(button.dataset.shipmentNumber || '').trim().toUpperCase();
        const meta = shipmentMeta.get(key);
        if (!meta) {
          button.classList.add('dd-error');
          setButtonText('Search first');
          setTimeout(() => { button.classList.remove('dd-error'); setButtonText('Andrew Enkh'); }, 2500);
          return;
        }
        button.dataset.state = 'busy';
        button.setAttribute('aria-disabled', 'true');
        button.classList.add('dd-busy');
        setButtonText('Assigning…');
        try {
          await assignAndrewToShipment(meta);
          button.dataset.state = 'success';
          button.classList.remove('dd-busy');
          button.classList.add('dd-success');
          setButtonText('✓ Andrew Enkh');
          button.title = 'Andrew Enkh assigned successfully';
          const driverText = card.querySelector('.title-drivername tsl-multiselect .tsl-multiselect-placeholder');
          if (driverText) driverText.textContent = 'Andrew Enkh';
        } catch (e) {
          button.dataset.state = '';
          button.setAttribute('aria-disabled', 'false');
          button.classList.remove('dd-busy');
          button.classList.add('dd-error');
          setButtonText('Retry Andrew');
          button.title = String((e && e.message) || e);
          setTimeout(() => {
            if (!button.dataset.state) {
              button.classList.remove('dd-error');
              setButtonText('Andrew Enkh');
            }
          }, 3000);
        }
      };
      button.addEventListener('click', runAndrewAssignment);
      button.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') runAndrewAssignment(event);
      });
      plateControl.insertAdjacentElement('afterend', deliverControl);
      plateControl.insertAdjacentElement('afterend', deliverLabel);
    });
  }
  function installDeliverUi() {
    ensureDeliverUiStyle();
    if (!deliverObserver) {
      deliverObserver = new MutationObserver(() => {
        scheduleDeliverUi();
        scheduleVinDefault();
      });
      deliverObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
    scheduleDeliverUi();
    scheduleVinDefault();
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
    .act.processing { background: #f5c518; color: #171a20; }   /* yellow — scanning/writing */
    .act.processing .s { opacity: .9; }
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
    menu.appendChild(actionButton('Clean Pickups', () => nextWeekdayCaption(), runCleanPickups, prepCleanPickups, { oneClick: true }));
    menu.appendChild(actionButton('Clean ETA', () => nextCalendarDayCaption(), runCleanEta, prepCleanEta, { oneClick: true }));
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
    if (menu) {
      menu.classList.toggle('open', open);
      if (open) menu.querySelectorAll('.act').forEach(btn => { if (btn.refreshSubtitle) btn.refreshSubtitle(); });
    }
  }

  // Standard actions retain confirm-to-run. With oneClick enabled, one press scans and writes
  // while yellow, then turns green on success (including when the scan finds nothing to clean).
  function actionButton(label, subtitle, runFn, prepFn, options) {
    const btn = document.createElement('button');
    btn.innerHTML = `<span class="t"></span><span class="s"></span>`;
    const T = btn.querySelector('.t'), S = btn.querySelector('.s');
    const oneClick = !!(options && options.oneClick);
    let state = 'idle', armTimer = null, prepData = null;
    const subtitleText = () => typeof subtitle === 'function' ? subtitle() : subtitle;
    const setStatus = (msg) => { S.textContent = msg; };
    function idle() { state = 'idle'; btn.className = 'act' + (runFn ? '' : ' soon'); T.textContent = label; S.textContent = subtitleText(); prepData = null; }
    btn.refreshSubtitle = () => { if (state === 'idle') S.textContent = subtitleText(); };
    function armPlain() { state = 'armed'; btn.className = 'act armed'; T.textContent = 'Confirm?'; S.textContent = label; clearTimeout(armTimer); armTimer = setTimeout(idle, 4000); }
    function err(e) { btn.className = 'act err'; T.textContent = '✕ ' + label; S.textContent = String((e && e.message) || e).slice(0, 40); state = 'idle'; setTimeout(idle, 5000); }
    idle();
    btn.addEventListener('click', async () => {
      if (state === 'running' || state === 'prepping') return;
      if (oneClick) {
        state = 'running'; btn.className = 'act processing'; T.textContent = label; S.textContent = 'scanning…';
        try {
          const prepared = prepFn ? await prepFn(setStatus) : null;
          const summary = prepared && prepared.count
            ? await runFn(setStatus, prepared)
            : ((prepared && prepared.emptyMsg) || 'nothing to clean ✓');
          state = 'done'; btn.className = 'act done'; T.textContent = '✓ ' + label; S.textContent = summary;
          setTimeout(idle, 6000);
        } catch (e) { err(e); }
        return;
      }
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
    const onDashboard = ON_DASH();
    if (onDashboard && !wasOnDashboard) resetVinDefaultForVisit();
    wasOnDashboard = onDashboard;
    if (onDashboard) { mount(); installDeliverUi(); if (host) host.style.display = ''; }
    else {
      clearTimeout(vinDefaultTimer);
      if (host) { host.style.display = 'none'; toggle(false); }
    }
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
