// ==UserScript==
// @name         Tesla Dispatch Dashboard — Cleaner/Marker
// @namespace    wastake.dispatchdash
// @version      0.21.2
// @description  Defaults Dispatch Dashboard searches to Tesla's VIN API field without opening the selector, replaces each License Plate control with a native Tesla-styled Deliver / Andrew Enkh action, shows a SuperDispatch status bubble next to each shipment number (with a regular-fleet-style hover card), and provides Cleaner/Marker actions for pickups, ETAs, Driver Needed shipments, and Tesla-status reconciliation.
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
// @connect      api.shipper.superdispatch.com
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

  const JESSICA_DRIVER_ID = 67651;
  const ANDREW_DRIVER_ID = 136062;
  // Auth captured off the page's OWN requests (never asked for) — used by the Cleaner/Marker
  // write actions (Clean Pickups / Clean ETA / driver assignment).
  let apiAuth = null, apiCarrier = null, apiUrl = null;
  const shipmentMeta = new Map(); // shipment number -> {shipmentId, carrierId}
  // Default dashboard searches to VIN semantics. The visible selector is kept in sync below,
  // while the XHR hook guarantees that Tesla receives `vins`, never `shipmentNumbers`.
  let vinSearchMode = true;

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
        // SD status bubble: remember one VIN per shipment (the scan key for find_by_vin)
        const sdBase = sdOrderBase(stop.shipmentNumber);
        if (sdBase && stop.vins && stop.vins[0] && stop.vins[0].vin && !sdShipments.has(sdBase)) {
          sdShipments.set(sdBase, { vin: String(stop.vins[0].vin).toUpperCase(), shipmentNumber: stop.shipmentNumber });
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
    scheduleSdCheck();
    scheduleSdBubbles();
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
    // Grab the bearer token + carrier id off the page's own dispatch calls (for the write actions).
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
              }
            } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, arguments);
    };
  })();

  // ---- actions ---------------------------------------------------------------
  function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; }

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
      // dataset.ddShip holds the CLEAN number once the SD bubble is appended inside the node
      const shipmentNumber = shipmentNode ? (shipmentNode.dataset.ddShip || shipmentNode.textContent).trim() : '';
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
        scheduleSdBubbles();
      });
      deliverObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
    scheduleDeliverUi();
    scheduleVinDefault();
  }

  // ============================================================================
  // ---- SuperDispatch status bubbles ------------------------------------------
  // A small pill to the RIGHT of each shipment number showing the SD order status,
  // matched by scanning ONE VIN of the shipment through SD find_by_vin and requiring
  // the Tesla order base ("A1PA892" from "SHP2607-A1PA892") to appear in the SD order
  // name (which may be "A1PA892-2" etc). Hover shows the regular-fleet card, minus
  // the price. gray = accepted/posted, yellow = picked up, green = delivered/invoiced.
  // No positive match -> no bubble. "posted" -> gray bubble, no hover card.
  // SD credentials: same scheme as regular-fleet — asked once, stored ONLY in
  // Tampermonkey GM storage (menu ▸ "Set SuperDispatch credentials").
  const SD_BASE = 'https://api.shipper.superdispatch.com';
  const SD_CONCURRENCY = 3;
  const SD_REQ_GAP_MS = 120;
  const SD_CACHE_KEY = 'dd_sd_cache';
  const SD_CACHE_VERSION = 5;   // v5: cards carry posted flag + strict postedAt for the hours marker
  const SD_ORDER_URL = 'https://shipper.superdispatch.com/orders/view/';
  const sdShipments = new Map();   // order base -> {vin, shipmentNumber}
  const sdLog = (...a) => console.log('%c[dd-sd]', 'color:#0a7;font-weight:bold', ...a);

  function sdOrderBase(shipmentNumber) {
    const s = String(shipmentNumber || '').trim().toUpperCase();
    if (!s) return '';
    return s.replace(/^SHP[A-Z0-9]*-/, '');
  }
  const SD_GREEN = new Set(['delivered', 'invoiced', 'paid', 'completed', 'archived']);
  const SD_YELLOW = new Set(['picked_up', 'pickedup']);
  const SD_NOCARD = new Set(['posted', 'new']);   // gray bubble, no hover card
  function sdNormStatus(st) { return String(st || '').toLowerCase().trim().replace(/\s+/g, '_'); }
  function sdBubbleColor(st, card) {
    if (SD_GREEN.has(st)) {
      // Delivered to our own yard is NOT final delivery — flag it yellow.
      if (card && sdIsTfiYard(card)) return 'yellow';
      return 'green';
    }
    if (SD_YELLOW.has(st)) return 'yellow';
    return 'gray';
  }
  function sdIsTfiYard(card) {
    const d = (card && card.delivery) || {};
    return ((d.name || '') + ' ' + (d.line || '')).toUpperCase().indexOf('TFI TRANS YARD') !== -1;
  }
  function sdTitleCase(s) { return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
  // Status "new" is how the SD API reports BOTH loadboard-posted orders (the website's
  // "Posted") and genuinely-unposted ones; is_posted_to_loadboard (card.posted) splits them.
  function sdStatusLabel(status, card) {
    if (status === 'posted') return 'Posted';
    if (status === 'new') return (card && card.posted) ? 'Posted' : 'New';
    return sdTitleCase(status) || '—';
  }
  // Posted/Accepted bubbles carry a small "3H" extension: whole hours since
  // posted_to_loadboard_at — strictly the loadboard post time, never creation time.
  // No timestamp (unposted "new", direct offers) or any other status -> no marker.
  const SD_HOURS_STATUSES = new Set(['posted', 'new', 'accepted']);
  function sdHoursMarker(entry) {
    if (!entry || !SD_HOURS_STATUSES.has(entry.status)) return '';
    const ms = sdParseDate(entry.card && entry.card.postedAt);
    if (ms == null) return '';
    return Math.max(0, Math.floor((Date.now() - ms) / 3600000)) + 'H';
  }

  // ---- SD credentials (GM storage only — never in the source) ----
  function sdGetCreds() {
    const c = GM_getValue('sd_creds', null);
    return (c && c.id && c.secret) ? c : null;
  }
  function sdPromptCreds() {
    const cur = sdGetCreds() || {};
    const id = prompt('SuperDispatch API — Client ID:', cur.id || '');
    if (id === null) return false;
    const secret = prompt('SuperDispatch API — Client Secret:\n(stored locally in Tampermonkey, never uploaded)', '');
    if (secret === null) return false;
    if (!id.trim() || !secret.trim()) return false;
    GM_setValue('sd_creds', { id: id.trim(), secret: secret.trim() });
    GM_deleteValue('sd_token');
    sdLog('credentials saved');
    return true;
  }

  // ---- SD HTTP (copied from regular-fleet) ----
  function sdFetch(opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: opts.method, url: opts.url, headers: opts.headers, data: opts.data,
        timeout: 30000,
        onload: r => resolve(r),
        onerror: e => reject(new Error('network error: ' + ((e && e.error) || 'unknown'))),
        ontimeout: () => reject(new Error('timeout')),
      });
    });
  }
  async function sdToken(force) {
    if (!force) {
      const cached = GM_getValue('sd_token', null);
      if (cached && cached.token && cached.exp > Date.now() + 30000) return cached.token;
    }
    const creds = sdGetCreds();
    if (!creds) throw new Error('No SuperDispatch credentials set');
    const r = await sdFetch({
      method: 'POST',
      url: SD_BASE + '/oauth/token?grant_type=client_credentials',
      headers: { 'Authorization': 'Basic ' + btoa(creds.id + ':' + creds.secret) },
    });
    if (r.status !== 200) throw new Error('SD auth failed ' + r.status);
    const j = JSON.parse(r.responseText);
    const exp = Date.now() + Math.max(60, (parseInt(j.expires_in, 10) || 3600) - 300) * 1000;
    GM_setValue('sd_token', { token: j.access_token, exp });
    return j.access_token;
  }
  function sdUnwrapObjects(resp) {
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
  function sdUnwrapObject(resp) {
    if (resp && typeof resp === 'object') {
      const data = resp.data;
      if (data && typeof data === 'object' && 'object' in data) return data.object || {};
    }
    return resp || {};
  }
  async function sdGet(path, retry) {
    const token = await sdToken(retry === 'reauth');
    const r = await sdFetch({
      method: 'GET', url: SD_BASE + path,
      headers: { 'Authorization': 'Bearer ' + token, 'Accept': 'application/json' },
    });
    if (r.status === 401 && retry !== 'reauth') return sdGet(path, 'reauth');
    if (r.status === 404) return { _404: true };
    if (r.status !== 200) throw new Error('GET ' + path + ' -> ' + r.status);
    return JSON.parse(r.responseText || '{}');
  }
  async function sdFindByVin(vin) {
    const j = await sdGet('/v1/public/orders/find_by_vin/' + encodeURIComponent(vin));
    if (j._404) return [];
    return sdUnwrapObjects(j);
  }
  async function sdGetOrder(guid) {
    const j = await sdGet('/v1/public/orders/' + encodeURIComponent(guid));
    if (j._404) return {};
    return sdUnwrapObject(j);
  }
  async function sdFullOrder(o) {
    const detailed = o && o.pickup && o.delivery && Array.isArray(o.vehicles) && o.price != null;
    if (detailed) return o;
    if (o && o.guid) { try { return await sdGetOrder(o.guid); } catch (_) {} }
    return o || {};
  }

  // ---- hover-card record (regular-fleet's makeCard, minus the price) ----
  const SD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function sdParseDate(s) {
    s = String(s || '').trim();
    if (!s) return null;
    const t = s.replace('Z', '+00:00').replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
    let ms = Date.parse(t);
    if (isNaN(ms)) ms = Date.parse(s);
    return isNaN(ms) ? null : ms;
  }
  function sdFmtDate(s) {
    const ms = sdParseDate(s); if (ms == null) return '';
    const d = new Date(ms); return SD_MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
  }
  function sdCityLine(v) {
    v = v || {};
    const cs = [v.city, v.state].filter(Boolean).join(', ');
    return ([cs, v.zip].filter(Boolean).join(' ').trim()) || (v.name || '');
  }
  function sdStopDate(stop) {
    stop = stop || {};
    return stop.completed_at || stop.scheduled_at || stop.scheduled_ends_at || '';
  }
  function sdPerUnitCost(o) {
    const price = Number(o && o.price);
    const units = o && Array.isArray(o.vehicles) ? o.vehicles.length : 0;
    if (!Number.isFinite(price) || o.price == null || units < 1) return '';
    return '$' + Math.round(price / units);
  }
  function sdMakeCard(o) {
    o = o || {};
    const pv = (o.pickup && o.pickup.venue) || {};
    const dv = (o.delivery && o.delivery.venue) || {};
    return {
      number: o.number || o.order_number || '',
      status: sdNormStatus(o.status),
      unitCost: sdPerUnitCost(o),
      // status "new" covers both loadboard-posted and genuinely-unposted orders; the
      // flag tells them apart. postedAt is STRICTLY the loadboard post time — no
      // created_at fallback, so unposted orders never show an hours marker.
      posted: !!o.is_posted_to_loadboard,
      postedAt: o.posted_to_loadboard_at || '',
      pickup: { line: sdCityLine(pv), name: pv.name || '', date: sdFmtDate(sdStopDate(o.pickup)) },
      delivery: {
        line: sdCityLine(dv), name: dv.name || '',
        date: sdFmtDate((o.delivery && o.delivery.completed_at) || sdStopDate(o.delivery)),
      },
      vehicles: (o.vehicles || []).map(v => ({
        vin: String(v.vin || '').toUpperCase(),
        label: [v.year, v.make, v.model].filter(Boolean).join(' '),
      })),
    };
  }

  // ---- daily cache (green terminal; everything else re-checked each pass) ----
  function sdToday() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function sdLoadCache() {
    let c = GM_getValue(SD_CACHE_KEY, null);
    if (!c || c.day !== sdToday() || c.version !== SD_CACHE_VERSION) {
      c = { version: SD_CACHE_VERSION, day: sdToday(), bases: {} };
      GM_setValue(SD_CACHE_KEY, c);
    }
    return c;
  }
  function sdSaveCache(c) { c.version = SD_CACHE_VERSION; c.day = sdToday(); GM_setValue(SD_CACHE_KEY, c); }

  // ---- the scan pass ----
  const sdSleep = ms => new Promise(r => setTimeout(r, ms));
  let sdCheckTimer = null, sdChecking = false;
  function scheduleSdCheck() {
    if (!ON_DASH()) return;
    clearTimeout(sdCheckTimer);
    sdCheckTimer = setTimeout(runSdCheck, 400);
  }
  async function sdEvaluate(base, rec, cached) {
    // Recheck path: the daily cache already knows this base's order guid, so ONE
    // get_order refreshes status + card — no find_by_vin. (find_by_vin returns only
    // {number, guid, created_at} anyway; it never carried the status.) The guid is
    // validated by requiring the fetched order's number to still contain the Tesla
    // base — a canceled-and-recreated order (A1PA892 -> A1PA892-2) gets a NEW guid,
    // so a mismatch/404 falls through to a fresh find_by_vin. The cache resets each
    // day, so guids re-resolve from scratch every morning.
    if (cached && cached.matched && cached.guid) {
      try {
        const full = await sdGetOrder(cached.guid);
        const num = String(full.number || full.order_number || '').toUpperCase();
        if (num.indexOf(base) !== -1) {
          return { matched: true, status: sdNormStatus(full.status), card: sdMakeCard(full), guid: cached.guid };
        }
        sdLog('stale guid for', base, '— re-resolving via find_by_vin');
      } catch (e) { return { error: String((e && e.message) || e) }; }
    }
    let orders;
    try { orders = await sdFindByVin(rec.vin); }
    catch (e) { return { error: String((e && e.message) || e) }; }
    const match = orders.find(o => String(o.number || o.order_number || '').toUpperCase().indexOf(base) !== -1);
    if (!match) return { matched: false, status: '', card: null, guid: '' };
    const guid = String(match.guid || '');
    const full = await sdFullOrder(match);
    return { matched: true, status: sdNormStatus(full.status || match.status), card: sdMakeCard(full), guid };
  }
  async function runSdCheck() {
    if (sdChecking || !ON_DASH() || !sdShipments.size) return;
    if (!sdGetCreds()) { sdLog('no SuperDispatch credentials — set them via the Tampermonkey menu'); return; }
    sdChecking = true;
    try {
      const cache = sdLoadCache();
      const todo = [...sdShipments.entries()].filter(([base]) => {
        const e = cache.bases[base];
        return !e || !SD_GREEN.has(e.status);   // green is terminal; everything else re-checks
      });
      decorateSdBubbles();
      if (!todo.length) { sdChecking = false; return; }
      sdLog('scanning', todo.length, 'shipment(s) against SuperDispatch');
      let i = 0;
      async function worker() {
        while (i < todo.length) {
          const [base, rec] = todo[i++];
          const res = await sdEvaluate(base, rec, cache.bases[base]);
          if (!res.error) {
            cache.bases[base] = { matched: res.matched, status: res.status, card: res.card, vin: rec.vin, guid: res.guid || '' };
            sdSaveCache(cache);
            decorateSdBubbles();
          } else {
            sdLog('skip (retry next pass):', base, res.error);
          }
          await sdSleep(SD_REQ_GAP_MS);
        }
      }
      await Promise.all(Array.from({ length: SD_CONCURRENCY }, worker));
      decorateSdBubbles();
    } finally {
      sdChecking = false;
    }
  }

  // ---- bubble DOM ----
  let sdBubbleTimer = null;
  function scheduleSdBubbles() {
    if (!ON_DASH()) return;
    clearTimeout(sdBubbleTimer);
    sdBubbleTimer = setTimeout(decorateSdBubbles, 80);
  }
  // keep the hours-since-posted markers ticking on an idle tab (repaint only, no requests)
  setInterval(scheduleSdBubbles, 5 * 60 * 1000);
  function ensureSdStyles() {
    if (document.getElementById('dd-sd-style')) return;
    const style = document.createElement('style');
    style.id = 'dd-sd-style';
    style.textContent = [
      // font is !important so Tesla's own anchor styling can't restyle the bubble now that it is an <a>
      '.dd-sd-bubble{display:inline-block;margin-left:6px;padding:2px 10px;border-radius:12px;',
      'font:600 12px/1.35 system-ui,Segoe UI,Arial,sans-serif!important;white-space:nowrap;vertical-align:middle;text-decoration:none!important;}',
      '.dd-sd-bubble[href]{cursor:pointer;}',
      '.dd-sd-bubble[href]:hover{filter:brightness(.95);text-decoration:none;}',
      '.dd-sd-bubble.green{background:#e6f4ea;color:#1e7b34;}',
      '.dd-sd-bubble.yellow{background:#fbefc9;color:#8a6a00;}',
      '.dd-sd-bubble.gray{background:#eee;color:#666;}',
      '.dd-sd-bubble.posted{font-style:italic;}',
      // hours-since-posted extension, visually part of the pill (separator + smaller bold).
      // pointer-events:none — hover/click hit the stable <a> bubble, never this span,
      // which decorate passes destroy and recreate (a hovered span dying mid-hover
      // fires boundary events that kept killing the card's 80ms show timer).
      '.dd-sd-bubble .dd-sd-hrs{margin-left:7px;padding-left:7px;border-left:1px solid rgba(0,0,0,.18);',
      'font-style:normal;font-size:11px;font-weight:700;opacity:.85;pointer-events:none;}',
      '.dd-sd-bubble[data-card="1"]{cursor:help;}',
      // the shipment-number node is width-constrained; without this the bubble can wrap under the ID
      'dispatch-dashboard-grid1 .grid-entry .title-padding-grid-entry{white-space:nowrap;}',
      // hover card — IDENTICAL to regular-fleet's, minus the per-unit cost
      '#dd-sd-hover{position:fixed;z-index:2147483647;background:#fff;color:#1a1a1a;',
      'font:15px/1.4 "Segoe UI",system-ui,Arial,sans-serif;border:1px solid #e4e4e4;',
      'border-radius:12px;box-shadow:0 9px 32px rgba(0,0,0,.18);padding:16px 18px;',
      'width:max-content;max-width:min(94vw,660px);opacity:0;transition:opacity .12s ease;pointer-events:none;box-sizing:border-box;}',
      '#dd-sd-hover *{box-sizing:border-box;}',
      '#dd-sd-hover .sd-head{display:flex;align-items:center;gap:11px;margin-bottom:15px;}',
      '#dd-sd-hover .sd-num{font-size:22px;font-weight:700;color:#111;letter-spacing:.2px;}',
      '#dd-sd-hover .sd-pill{font-size:14px;font-weight:600;padding:3px 12px;border-radius:14px;white-space:nowrap;}',
      '#dd-sd-hover .sd-pill.green{background:#e6f4ea;color:#1e7b34;}',
      '#dd-sd-hover .sd-pill.yellow{background:#fbefc9;color:#8a6a00;}',
      '#dd-sd-hover .sd-pill.gray{background:#eee;color:#666;}',
      '#dd-sd-hover .sd-body{display:flex;gap:37px;align-items:stretch;}',
      '#dd-sd-hover .sd-col{flex:0 0 auto;white-space:nowrap;}',
      '#dd-sd-hover .sd-right{display:flex;flex-direction:column;min-width:150px;}',
      '#dd-sd-hover .sd-route{position:relative;padding-left:23px;}',
      '#dd-sd-hover .sd-stop{position:relative;}',
      '#dd-sd-hover .sd-stop + .sd-stop{margin-top:16px;}',
      '#dd-sd-hover .sd-stop:not(:last-child)::before{content:"";position:absolute;left:-17px;top:10px;bottom:-23px;border-left:2px dashed #cfcfcf;}',
      '#dd-sd-hover .sd-mark{position:absolute;left:-23px;top:3px;width:13px;height:13px;}',
      '#dd-sd-hover .sd-mark.dot{border-radius:50%;background:#e8730b;}',
      '#dd-sd-hover .sd-mark.sq{background:#2e8b3d;border-radius:2px;}',
      '#dd-sd-hover .sd-city{font-weight:700;color:#161616;max-width:345px;overflow:hidden;text-overflow:ellipsis;}',
      '#dd-sd-hover .sd-sub{color:#8c8c8c;font-size:14.5px;margin-top:3px;max-width:345px;overflow:hidden;text-overflow:ellipsis;}',
      '#dd-sd-hover .sd-model{font-weight:700;color:#161616;}',
      '#dd-sd-hover .sd-vin{display:inline-block;background:#fcf3d6;padding:2px 6px;border-radius:4px;margin-top:4px;font-size:14.5px;color:#222;}',
      '#dd-sd-hover .sd-more{color:#9a9a9a;font-size:12px;margin-top:4px;}',
      '#dd-sd-hover .sd-unit-cost{margin-top:auto;padding-top:14px;color:#161616;font-weight:700;font-size:14.5px;}',
    ].join('');
    (document.head || document.documentElement).appendChild(style);
  }
  function decorateSdBubbles() {
    if (!ON_DASH()) return;
    ensureSdStyles();
    const cache = sdLoadCache();
    document.querySelectorAll('dispatch-dashboard-grid1 .grid-entry .title-padding-grid-entry').forEach(node => {
      // The bubble lives INSIDE this node (hugging the shipment number), so the clean
      // number is remembered in a dataset before the bubble is ever appended.
      const shipmentNumber = (node.dataset.ddShip || node.textContent || '').trim();
      if (!shipmentNumber) return;
      node.dataset.ddShip = shipmentNumber;
      const base = sdOrderBase(shipmentNumber);
      const entry = cache.bases[base];
      let bubble = node.querySelector('.dd-sd-bubble');
      if (!entry || !entry.matched) {           // no positive match -> no bubble
        if (bubble) bubble.remove();
        return;
      }
      if (!bubble) {
        // an anchor: clicking the bubble opens the SD order in a new tab
        bubble = document.createElement('a');
        bubble.className = 'dd-sd-bubble';
        bubble.target = '_blank';
        bubble.rel = 'noopener';
        bubble.addEventListener('click', e => e.stopPropagation());   // don't poke Tesla's card
        node.appendChild(bubble);
      }
      // "Posted" (loadboard) vs genuinely-unposted "New" — both italic gray.
      const isPreLifecycle = SD_NOCARD.has(entry.status);
      const hasCard = entry.card ? '1' : '';
      const cls = 'dd-sd-bubble ' + sdBubbleColor(entry.status, entry.card) + (isPreLifecycle ? ' posted' : '');
      const label = sdStatusLabel(entry.status, entry.card);
      const hrs = sdHoursMarker(entry);
      // Rebuild only on real change: decorate runs constantly (the global
      // MutationObserver re-fires on our own writes), and gratuitously recreating
      // the bubble's children breaks an in-flight hover over them.
      const sig = [base, cls, label, hrs, hasCard, entry.guid || ''].join('|');
      if (bubble.dataset.sig === sig) return;
      bubble.dataset.sig = sig;
      bubble.className = cls;
      bubble.textContent = label;
      if (hrs) {
        const h = document.createElement('span');
        h.className = 'dd-sd-hrs';
        h.textContent = hrs;
        bubble.appendChild(h);
      }
      bubble.dataset.base = base;
      bubble.dataset.card = hasCard;
      if (entry.guid) bubble.href = SD_ORDER_URL + entry.guid;
      else bubble.removeAttribute('href');
    });
  }

  // ---- hover card (regular-fleet behavior, price omitted) ----
  const SD_HOVER_OPEN_MS = 80, SD_HOVER_CLOSE_MS = 90, SD_MAX_VENUE = 34;
  function sdTrunc(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1).replace(/\s+$/, '') + '…' : s;
  }
  function sdCardHtml(c, hoveredVin) {
    const pill = sdBubbleColor(c.status);
    const statusLabel = sdStatusLabel(c.status, c);
    const vehicles = c.vehicles || [];
    const hero = vehicles.find(v => v.vin && v.vin === hoveredVin) || vehicles[0] || { vin: '', label: '' };
    const others = Math.max(0, vehicles.length - 1);
    const vehHtml =
      (hero.label ? '<div class="sd-model">' + esc(hero.label) + '</div>' : '') +
      (hero.vin ? '<div class="sd-vin">' + esc(hero.vin) + '</div>' : '') +
      (others > 0 ? '<div class="sd-more">+' + others + ' more</div>' : '');
    // Posted/accepted cards show the per-unit carrier cost, exactly like regular-fleet.
    const showCost = SD_NOCARD.has(c.status) || c.status === 'accepted' || c.status === 'pending';
    const costHtml = (showCost && c.unitCost) ? '<div class="sd-unit-cost">' + esc(c.unitCost) + '</div>' : '';
    const psub = [c.pickup.date, sdTrunc(c.pickup.name, SD_MAX_VENUE)].filter(Boolean).join('  ·  ');
    const dsub = [c.delivery.date, sdTrunc(c.delivery.name, SD_MAX_VENUE)].filter(Boolean).join('  ·  ');
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
  let sdPanelEl = null, sdHideT = null, sdShowT = null, sdShownBase = null;
  function sdEnsurePanel() {
    if (sdPanelEl) return sdPanelEl;
    ensureSdStyles();
    sdPanelEl = document.createElement('div');
    sdPanelEl.id = 'dd-sd-hover';
    (document.body || document.documentElement).appendChild(sdPanelEl);
    return sdPanelEl;
  }
  function sdShowPanel(bubble) {
    clearTimeout(sdHideT);
    const base = bubble.dataset.base;
    if (sdShownBase === base && sdPanelEl && sdPanelEl.style.opacity === '1') return;
    clearTimeout(sdShowT);
    sdShowT = setTimeout(() => {
      const entry = sdLoadCache().bases[base];
      if (!entry || !entry.card) return;
      const p = sdEnsurePanel();
      p.innerHTML = sdCardHtml(entry.card, entry.vin || '');
      p.style.display = 'block'; p.style.opacity = '0';
      const r = bubble.getBoundingClientRect();
      const pw = p.offsetWidth, ph = p.offsetHeight;
      let left = r.right + 10;
      let top = r.top - ph - 6;
      if (left + pw > window.innerWidth - 8) left = Math.max(8, r.left - pw - 10);
      if (top < 8) top = r.bottom + 6;
      p.style.left = left + 'px'; p.style.top = top + 'px';
      p.style.opacity = '1';
      sdShownBase = base;
    }, SD_HOVER_OPEN_MS);
  }
  function sdHidePanel() {
    clearTimeout(sdShowT);
    sdHideT = setTimeout(() => { if (sdPanelEl) sdPanelEl.style.opacity = '0'; sdShownBase = null; }, SD_HOVER_CLOSE_MS);
  }
  function startSdHover() {
    document.addEventListener('mouseover', e => {
      const t = e.target;
      if (!t || !t.closest || !ON_DASH()) return;
      const bubble = t.closest('.dd-sd-bubble[data-card="1"]');
      if (bubble) sdShowPanel(bubble);
    }, true);
    document.addEventListener('mouseout', e => {
      const t = e.target;
      if (!t || !t.closest) return;
      const bubble = t.closest('.dd-sd-bubble');
      if (!bubble) return;
      const to = e.relatedTarget;
      if (to && bubble.contains(to)) return;
      sdHidePanel();
    }, true);
    window.addEventListener('scroll', () => { if (sdShownBase) sdHidePanel(); }, true);
    window.addEventListener('resize', () => { if (sdShownBase) sdHidePanel(); });
  }
  startSdHover();

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
    GM_registerMenuCommand('Clear captured VIN cache', () => clearStore());
    GM_registerMenuCommand('Set SuperDispatch credentials', () => { if (sdPromptCreds()) scheduleSdCheck(); });
    GM_registerMenuCommand('Clear SuperDispatch credentials', () => {
      GM_deleteValue('sd_creds'); GM_deleteValue('sd_token'); sdLog('SuperDispatch credentials cleared');
    });
    GM_registerMenuCommand('Re-scan SuperDispatch bubbles', () => {
      GM_setValue(SD_CACHE_KEY, { version: SD_CACHE_VERSION, day: sdToday(), bases: {} });
      document.querySelectorAll('.dd-sd-bubble').forEach(b => b.remove());
      scheduleSdCheck();
    });
  } catch (e) {}
})();
