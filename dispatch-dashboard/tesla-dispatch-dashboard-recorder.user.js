// ==UserScript==
// @name         Tesla Dispatch Dashboard — Cleaner/Marker
// @namespace    wastake.dispatchdash
// @version      0.23.2
// @description  Defaults Dispatch Dashboard searches to Tesla's VIN API field without opening the selector, replaces each License Plate control with a native Tesla-styled Deliver / Andrew Enkh action, shows a SuperDispatch status bubble next to each shipment number (with a regular-fleet-style hover card), and provides Cleaner/Marker actions for pickups, ETAs, and Driver Needed shipments.
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
 *
 * WHAT ELSE IT DOES (verified contracts, all captured from real manual actions):
 *   - SuperDispatch status bubble per card, matched by the ORDERPIN ruleset (0.23.0 — the same
 *     rule as the tender ledger and the app-delivery marker, see the ORDERPIN section): the
 *     card's VINs go through SD find_by_vin, candidates inside the tender window are pinned by
 *     the Tesla base in the order number or by route (alias > street > zip > city > near), a
 *     split shows one bubble per SD order, and a VIN with candidates but no rule match shows a
 *     dashed "?" pill with the reason (held, never guessed). Gray accepted/posted, yellow picked
 *     up, green delivered/invoiced/paid except a "TFI TRANS YARD" delivery = yellow; hover card;
 *     per-unit price on Posted/Accepted/Pending only; Posted/Accepted carry an hours-since-
 *     loadboard-post marker; bubble links to shipper.superdispatch.com/orders/view/{guid};
 *     rechecks re-validate each cached pin with one get_order. SD creds live only in GM storage
 *     (menu: Set SuperDispatch credentials). The Tesla-location, city and alias tables are
 *     GENERATED: run `python -m orderpin export-js` at the repo root before publishing, then bump
 *     @version. 0.23.1: dashboard names that differ from the tender's ("NA-US-UT-TA-Pleasant
 *     Grove") resolve by token match against the table, free-form customer addresses parse
 *     directly, and the dashboard name's own city counts as an alias of the tender stop.
 *   - Default Search By VINs: the page's own GetCarrierDispatchShipment request is rewritten from
 *     shipmentNumbers:[…] to vins:[…] before send (and the Excel download request likewise); a
 *     deliberate manual choice of Shipment disables that for the visit.
 *   - Deliver / Andrew Enkh control: replaces the card's License Plate control with an assign
 *     button -> POST …/AssignDrivertoShipment {shipmentId, driverId:136062, carrierId,
 *     driverJobStatus:"PENDING", source:"TVP"}.
 *   - Clean Pickups: alerts 1 (Pickup Date Late) + 7 (Pickup Date Today) -> POST
 *     …/updateestimatedshipdate?dateTrackingSource=3 {updateEstimatedShipDateList:[{updateReasonId:4,
 *     estimateShipDate = next weekday 16:00Z (Fri-Sun -> Monday), stopId}]} chunked 100; same click
 *     assigns JESSICA TFI (driverId 67651) to alert 2 (Driver Needed) via
 *     …/UpdateShipmentsDriverAndLicensePlate {shipmentIds, driverId, carrierId, driverJobStatus:
 *     "PENDING", source:"TVP", truckLicensePlate:""}.
 *   - Clean ETA: alerts 3 (Late ETA) + 6 (ETA Today) -> POST …/updateStopEta [{StopId,
 *     EtaUpdateSourceId:3, EstimatedDeliveryDate = next calendar day, EtaTimeWindowEndInHours:16,
 *     EtaUpdateReasonId:4}] chunked 100. HTTP 200 with success:false counts as failure everywhere.
 *   Alert ids (getdispatchalertsbycarrier): 1 Pickup Date Late · 2 Driver Needed · 3 Late ETA ·
 *   4 Incorrect Driver ETA · 5 No Action Needed · 6 ETA Today · 7 Pickup Date Today.
 *   DISPATCHER_STATES below MIRRORS shipment-creator/profiles/profiles.json — change both, bump
 *   @version. Removed history: the server piggyback / red-VIN pipeline (0.21.0) and the tender-pool
 *   mirror (0.22.0, removed in 0.22.1) — this script no longer talks to shipments.wastake.com.
 *
 * PUBLISHING: clients install/update from the PUBLIC repo chikataken/tesla-super (the
 * @updateURL above) and only pick up a change when @version increases — bump it, then run
 * ./publish_userscripts.sh at the repo root (copies the six scripts into that repo and pushes).
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
    Kelly: ['VA','MD','DC','GA','FL','DE','WV','PA','NY','NJ','CT','RI','MA','NH','VT','ME','NC','SC','TN','AL','KY'],
    Duka:  ['CA'],
    Burte: ['IL','IN','OH','MI','MS','UT','WI','NV','AZ','NM','CO','ID','WY','MT','ND','SD','NE','KS','OK','MO','IA','MN','AR','LA','TX','OR','WA'],
  };
  const STATE_DISPATCHER = {};
  for (const name in DISPATCHER_STATES) for (const st of DISPATCHER_STATES[name]) STATE_DISPATCHER[st] = name;

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
        // SD status bubble: remember every VIN of the shipment (orderpin resolves VIN by VIN;
        // a split lands the VINs on different SD orders)
        const sdBase = sdOrderBase(stop.shipmentNumber);
        if (sdBase && stop.vins && stop.vins.length) {
          const rec = sdShipments.get(sdBase) || { vin: '', vins: [], shipmentNumber: stop.shipmentNumber };
          for (const v of stop.vins) {
            const vin = String((v && v.vin) || '').toUpperCase();
            if (vin && rec.vins.indexOf(vin) === -1) rec.vins.push(vin);
          }
          if (!rec.vin && rec.vins.length) rec.vin = rec.vins[0];
          sdShipments.set(sdBase, rec);
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
  // A small pill to the RIGHT of each shipment number showing the SD order status. Which
  // SD order that is comes from the ORDERPIN ruleset below (the core package's rule, ported):
  // the card's VINs through SD find_by_vin, then the Tesla base in the order number or a
  // route match on both stops. Hover shows the regular-fleet card, minus the price.
  // gray = accepted/posted, yellow = picked up, green = delivered/invoiced. A split shows one
  // bubble per SD order; candidates but no rule match -> dashed "?" (held); nothing -> no bubble.
  // SD credentials: same scheme as regular-fleet — asked once, stored ONLY in
  // Tampermonkey GM storage (menu ▸ "Set SuperDispatch credentials").
  const SD_BASE = 'https://api.shipper.superdispatch.com';
  const SD_CONCURRENCY = 3;
  const SD_REQ_GAP_MS = 120;
  const SD_CACHE_KEY = 'dd_sd_cache';
  const SD_CACHE_VERSION = 6;   // v6: entries carry orderpin pins[] + holds[] (v5: posted flag + strict postedAt)
  const SD_ORDER_URL = 'https://shipper.superdispatch.com/orders/view/';
  const sdShipments = new Map();   // order base -> {vin, vins[], shipmentNumber}
  const sdLog = (...a) => console.log('%c[dd-sd]', 'color:#0a7;font-weight:bold', ...a);

  // ---- orderpin: the core ruleset (<repo>/orderpin/pin.py), ported ------------------
  // Which SD order carries a Tesla shipment's VIN? The SAME rule the tender ledger and the
  // app-delivery marker use, so all three legs agree on which order a tender-move rides:
  //   candidates = the VIN's SD orders (find_by_vin) created inside the window around the
  //                tender; never a "(dup" rename, never a canceled order
  //   name rule  = the Tesla base ("A2DT232" of SHP2609-A2DT232) inside the SD order number
  //                ("A2DT232-4", "A2DT232 NJ VIP") -> pinned
  //   route rule = each SD stop vs the tender stop at the strongest tier
  //                alias > street > zip > city > near (<= 30 mi centroid distance); accepted
  //                when both stops match and one is alias/street/zip, or both are city
  //   tie-break  = name match first, then (among name matches) the most progressed status,
  //                then the order created closest to the tender; an exact tie or no match =
  //                HOLD (dashed "?" pill carrying the reason) — never a guess
  // Tesla's dashboard carries only location NAMES per stop, so the tender stops come from
  // OP_LOCATIONS (generated from the tender emails by `python -m orderpin export-js`); a name
  // outside the table falls back to its "NA-US-ST-City" pattern (city tier only), else to the
  // name rule alone. The tender's sent_at is not on the dashboard either: the window is
  // anchored on the stop's readyDate (the tender is sent within -10..+8 d of it), widened
  // by that spread on each side so the reach around the true sent_at stays +/-21 d.
  // >>> ORDERPIN TABLES — generated 2026-09-15 by `python -m orderpin export-js --days 120`; do not hand-edit
  // Tesla location name -> [street, city, state, zip, lat, lon]  (965 names, last 120 days of tenders)
  const OP_LOCATIONS = {"-100 Piedmont Ct Ste , Atlanta, , United States of America, 30340":["100 Piedmont Ct Ste","Atlanta","GA","",33.817,-84.38],"-103 E 3RD ST , , N/A, United States of America, 41011":["103 E 3RD ST","Covington","KY","41011",39.071,-84.521],"-1170 Garfield Ave, Ste 2. , Lancaster, , United States of America, 17601":["1170 Garfield Ave, Ste 2","Lancaster","PA","17601",40.077,-76.311],"-12626 US-12 , Brooklyn, Connecticut, United States of America, N/A, United States of America, 49230":["12626 US-12","Brooklyn","MI","49230",42.104,-84.241],"-13020 Yukon Ave , , N/A, United States of America, 90250":["13020 Yukon Ave","Hawthorne","CA","90250",33.914,-118.349],"-135 OLD HERITAGE PL , , N/A, United States of America, 04963":["135 OLD HERITAGE PL","OAKLAND","ME","04963",44.517,-69.74],"-137 LITTLE ACRES DR , Cumberland, , United States of America, 04021":["137 LITTLE ACRES DR","CUMBERLAND","ME","04021",43.797,-70.265],"-1415 S MESILLA ST , Deming, , United States of America, 88030":["1415 S MESILLA ST","Deming","NM","88030",32.232,-107.747],"-14475 NE 24th St. , Bellevue, , United States of America, 98007":["14475 NE 24th St","Bellevue","WA","98007",47.617,-122.143],"-149 BEACH AVE , , N/A, United States of America, 04043":["149 BEACH AVE","Kennebunk","ME","04043",43.388,-70.548],"-1501 N Walton Walker Blvd , Dallas, , United States of America, 75211":["1501 N Walton Walker Blvd","Dallas","TX","",32.789,-96.787],"-16 International Drive , , N/A, United States of America,":["16 International Drive","Horry County","SC","",null,null],"-18808 N 32nd Street , Phoenix, , United States of America, 85050":["18808 N 32nd Street","Phoenix","AZ","85050",33.686,-111.996],"-1951 Swanson Drive Suite 130 , Charlottesville, , United States of America, 22901":["1951 Swanson Drive Suite 130","Charlottesville","VA","",38.047,-78.482],"-2 Lyman Lane , Northeast Harbor, , United States of America, 04662":["2 Lyman Lane","MOUNT DESERT","ME","04662",44.294,-68.285],"-201 Logistics Dr. Building , Kyle, , United States of America, 78640":["201 Logistics Dr. Building","Kyle","TX","",29.997,-97.834],"-201 Logistics Dr. Building 1. , Kyle, , United States of America, 78640":["201 Logistics Dr. Building 1","Kyle","TX","78640",29.997,-97.834],"-2161 LAGOON AVE , Crescent City, , United States of America, 95531":["2161 LAGOON AVE","Crescent City","CA","95531",41.782,-124.133],"-22290 Hathaway Ave , Hayward, , United States of America, 95134":["22290 Hathaway Ave","San Jose","CA","95134",37.409,-121.941],"-22290 Hathaway Ave Hayward , Hayward, , United States of America, 94541":["22290 Hathaway Ave Hayward","Hayward","CA","94541",37.674,-122.089],"-223 W JASPER ST , , N/A, United States of America, 49841":["223 W JASPER ST","Gwinn","MI","49841",46.331,-87.44],"-2751 Rockfill Rd , Fort Myers, , United States of America, 33916":["2751 Rockfill Rd","Fort Myers","FL","33916",26.647,-81.843],"-3051 N PALMILLA DR , Fayetteville, , United States of America, 72703":["3051 N PALMILLA DR","Fayetteville","AR","72703",36.099,-94.172],"-3340 E Mossman Rd, , Tucson, , United States of America, 85706":["3340 E Mossman Rd","Tucson","AZ","85706",32.139,-110.945],"-3415 COUNTRY LN , Hays, , United States of America, 67601":["3415 COUNTRY LN","Hays","KS","67601",38.878,-99.335],"-3817 E TOWNSHIP ST , , N/A, United States of America, 72703":["3817 E TOWNSHIP ST","Fayetteville","AR","72703",36.099,-94.172],"-4 ITIC Dr. , Greenville, , United States of America, 29605":["4 ITIC Dr","Greenville","SC","",34.855,-82.412],"-4103 SW Cornerstone Rd , , , United States of America, 72712":["4103 SW Cornerstone Rd","Bentonville","AR","72712",36.358,-94.222],"-414 VANGUARD DR , White Sands Missile Range, , United States of America, 88002":["414 VANGUARD DR","White Sands Missile Range","NM","88002",32.384,-106.494],"-425 E. Pinnacle Peak Road. Suite 140. , Arizona, , United States of America, 85027":["425 E. Pinnacle Peak Road. Suite 140","Arizona","AZ","",32.756,-111.671],"-425 E. Pinnacle Peak Road. Suite 140. , Phoenix, , United States of America, 85027":["425 E. Pinnacle Peak Road. Suite 140","Phoenix","AZ","",33.492,-112.079],"-4331 W GLACIER ST , , , United States of America, 72704":["4331 W GLACIER ST","Fayetteville","AR","72704",36.088,-94.309],"-4520B Copper Sage St , Las Vegas, , United States of America, 89115":["4520B Copper Sage St","Las Vegas","NV","89115",36.216,-115.067],"-45401 Research Ave Fremont, , Fremont, , United States of America, 94539":["45401 Research Ave Fremont","Fremont","CA","94539",37.518,-121.929],"-5 Hercules Way , Greenville, , United States of America, 29605":["5 Hercules Way","Greenville","SC","29605",34.8,-82.393],"-5012 Joanne Kearney Blvd. , Tampa, FL, US, 33619":["5012 Joanne Kearney Blvd.","Tampa","FL","33619",27.938,-82.376],"-5206 Young St Suite B Bakersfield , Baker, , United States of America, 93727":["5206 Young St Suite B Bakersfield","Bakersfield","CA","93727",36.753,-119.706],"-61 SEACOAST TER , Kittery, , United States of America, 03904":["61 SEACOAST TER","Kittery","ME","03904",43.092,-70.743],"-7337 TRADE STREET , San Diego, , United States of America, 92121":["7337 TRADE STREET","San Diego","CA","",32.755,-117.147],"-943 N 32ND ST US BILLINGS 59101 MT , Billings, , United States of America, 59101":["943 N 32ND ST US BILLINGS 59101 MT","Billings","MT","59101",45.775,-108.501],"-: 425 E. Pinnacle Peak Road. Suite 140. , Phoenix, , United States of America, 85027":[": 425 E. Pinnacle Peak Road. Suite 140","Phoenix","AZ","",33.492,-112.079],"16775 State Road 50., CLERMONT, FL, US, 34711":["16775 State Road 50","Clermont","FL","34711",28.552,-81.757],"2801 Barranca Pkwy, Irvine, CA, US, 92606":["2801 Barranca Pkwy","Irvine","CA","92606",33.695,-117.822],"4170 Business Center Dr":["4170 Business Center Dr","Fremont","CA","",37.573,-121.974],"651 N Armstrong Ave Ste 108, Fresno, CA, US, 93727":["651 N Armstrong Ave","Fresno","CA","93727",36.753,-119.706],"8550 Case Road":["8550 Case Road","McGregor","TX","",null,null],"ADESA CHICAGO":["2785 Beverly Road","Hoffman Estates","IL","60192",42.043,-88.08],"ADESA Dallas - Hutchins":["3501 North Lancaster Hutchins Road","Hutchins","TX","75141",32.64,-96.707],"ALAN BANGERTER scottbang2004@yahoo.com 7759807603":["2985 FAIRWAY VIEW DR","WEST WENDOVER","NV","",40.739,-114.073],"AMANDA ORABONE amanda.orabone@astrazeneca.com 8608108759":["25 WINDMILL HILL RD","BRANFORD","CT","",41.28,-72.811],"AMIRA LEATON jasmineleaton19@icloud.com 9088480632":["4213 CAPISTRANO AVE","LAS CRUCES","NM","",32.38,-106.769],"Alessio Boscaro alessio.boscaro@unox.com 7045347981":["987 AIRLIE PKWY (Can only receive delivery Mon - Fri 8:30 am - 4:30 pm)","DENVER","NC","",35.484,-80.99],"Alexander Jones alexander.jones1@astrazeneca.com 4192664023":["228 ASTON RD","PERRYSBURG","OH","",41.55,-83.61],"Andrew Raduechel andrew.raduechel@alexion.com 4022129028":["1124 N 130TH ST","OMAHA","NE","",41.256,-96.006],"Andy Bailey andy.bailey1@astrazeneca.com 6106138094":["100 W OXFORD ST","PHILADELPHIA","PA","",39.991,-75.143],"Anthony Parker anthony.parker@kingspan.com 9173298165":["576 6TH AVE","BROOKLYN","NY","",40.652,-73.955],"Anthony Tommelleo anthony.tommelleo@astrazeneca.com 3305653468":["5380 STRUTHERS RD","STRUTHERS","OH","",41.051,-80.599],"BEAU BARRETT beau@bfeoffice.com 4793660696":["960 NW C ST","BENTONVILLE","AR","",36.347,-94.26],"BEAU LANGEVIN":["10 SUNNY DR","Biddeford","ME","04005",43.493,-70.488],"BEAU LANGEVIN langevinplumbingandheating@gmail.com 2074506667":["10 SUNNY DR","BIDDEFORD","ME","",43.476,-70.497],"BERT LANE blanemt@yahoo.com 4066986220":["3354 CASTLE PINES DR","BILLINGS","MT","",45.797,-108.516],"BIJIAO CHEN primaveramaine@gmail.com 9175029680":["474 BUCKSPORT RD","ELLSWORTH","ME","",44.555,-68.412],"BNSF - Richmond - Tank Lot":["980 Hensley st","Richmond","CA","",37.936,-122.344],"BOBBI BLAIN bobbiblain@gmail.com 4068601208":["518 SHADOW LAWN CT","BILLINGS","MT","",45.797,-108.516],"BRYANT MENDEL":["2 MERGANSER LN","WINDHAM","ME","04062",43.796,-70.414],"BRYANT MENDEL bryantmendel2017@yahoo.com 2074200321":["2 MERGANSER LN","WINDHAM","ME","",43.796,-70.414],"Bakersfield-Young Street":["5206 Young St","Bakersfield","CA","",35.335,-118.986],"Beltsvile Energy Ops WH":["9000 Virginia Manor Road, Suite 250","Beltsville","MD","",39.04,-76.916],"Bernalillo Albuquerque Energy Service Storage":["1300 Jemez Canyon Dam Rd","Bernalillo","NM","",35.328,-106.531],"Bethpage Energy OC - Tesla":["15 Grumman Road West, Unit 4","Bethpage","NY","",40.74,-73.486],"Blackwood Energy Ops WH":["1001 Lower Landing Road Suite 601","Blackwood","NJ","08012",39.79,-75.037],"Bradley Cole bradley.cole@siemens-healthineers.com 7245545974":["7026 CHURCH AVE","BEN AVON","PA","",null,null],"Bryan Derrickson bryan.derrickson@novartis.com 7039634484":["239 BAYVIEW DR","MOUNT PLEASANT","SC","",32.836,-79.829],"Bryce Hansen hansen@lincolnradiology.com 4028413801":["2239 Smith Street","LINCOLN","NE","",40.818,-96.689],"Burbank Energy Ops WH":["3022 KENWOOD ST","Burbank","CA","",34.181,-118.313],"CARLOS DE LA HUERTA carlosone111@yahoo.com 9157403173":["905 HOLLY PARK AVE","SANTA TERESA","NM","",31.839,-106.682],"CAROLINE FOUST-WRIGHT c.foustwright@gmail.com 4056121476":["7 WESTON POINT RD","FREEPORT","ME","",43.857,-70.103],"CARRIE MCNEELYSCHENONE ccwisewoman@sbcglobal.net 5306046135":["139 CHARLES ST","ETNA","CA","",41.446,-123.01],"CHAD SCHOENFELDER shoei8996@yahoo.com 7125749611":["191 N CHURCHILL CIR","NORTH SIOUX CITY","SD","",42.525,-96.507],"CHARLES REDDING credding2@yahoo.com 4797519284":["19457 DAVIS FORD RD","SPRINGDALE","AR","",36.179,-94.125],"CHARLES SHEPHERD darinshepherd@gmail.com 4064516926":["4411 BEMBRICK ST","BOZEMAN","MT","",45.659,-111.046],"CHARLES WEST ctwest@gmail.com 9704269541":["30 CRAZY HORSE DR","DURANGO","CO","",37.226,-107.878],"CHRISTOPHER MARSHALL chris@chrisdavidmarshall.com 2074090426":["30 EXCHANGE ST","PORTLAND","ME","",43.666,-70.257],"CJ Elmore":["1362 N McDowell Blvd","Petaluma","CA","94954",38.251,-122.615],"CLAYTON LUMPKIN":["13154 EL MONTANO CIR","Rogers","AR","72758",36.317,-94.154],"CLAYTON LUMPKIN clumpkin87@gmail.com 8324723353":["13154 EL MONTANO CIR","ROGERS","AR","",36.328,-94.129],"COM-AWKPG8DPXB-1":["2131 S. Hall St","Visalia","CA","",36.331,-119.296],"COREY BRIMACOMBE heatherbrim@hotmail.com 7152124577":["2111 WOODLAND RIDGE RD","WAUSAU","WI","",44.94,-89.67],"Caroline Englebright belle.englebright@astrazeneca.com 2709632804":["103 E 3RD ST","COVINGTON","KY","",39.022,-84.525],"Charles Shepherd":["4411 Bembrick St","Bozeman","MT","59718",45.668,-111.24],"Clorris Sthole clorris.sthole@novartis.com 7038989468":["101 BEACON FALLS CT","CARY","NC","",35.781,-78.815],"Conway Ford Collision":["2380 Church Street","Conway","SC","29526",33.873,-79.056],"Curt Vigrass curt.vigrass@astrazeneca.com 4122660031":["874 MACARTHUR DR","PITTSBURGH","PA","",40.441,-80.004],"DANIEL HIRSCHFELD dan.hirschfeld@hvrtrust.com 3082937744":["3606 4TH AVE","KEARNEY","NE","",40.75,-99.088],"DANIEL KONRAD sales@htplumb.com 6056821048":["205 W DAKOTA ST","TRIPP","SD","",43.24,-97.971],"DANIEL MORGAN djmorgan77@gmail.com 2072170600":["727 HUDSON HILL RD","HUDSON","ME","",44.991,-68.888],"DANIEL SCOTT daniel@bigflathead.com 4065150500":["1160 HOLT DR","BIGFORK","MT","",48.063,-114.073],"DARRAN BALDEA thinkdarran@yahoo.com +14109803412":["6 HILLTOP DR","WISCASSET","ME","",44.007,-69.683],"DAVID BROOME dab77@humboldt.edu 5108613068":["1819 SANDPIPER LN","MCKINLEYVILLE","CA","",40.947,-124.083],"DAVID GRAHAM":["1680 NORWOOD LN","Billings","MT","59102",45.781,-108.573],"DAVID GRAHAM david@fiphysician.com 4066971518":["1680 NORWOOD LN","BILLINGS","MT","",45.797,-108.516],"DAVID HARTLEY dbrucehart@gmail.com 6057302727":["2000 E 4TH ST UNIT 6","PIERRE","SD","",44.37,-100.321],"DENETRIAS CHARLEMAGNE dee.charlemagne@gmail.com 9144830794":["910 NW 8th st","Bentonville","AR","",36.347,-94.26],"DEREK GERSTNER derek@simonswealthmanagement.com 7853170737":["4108 HARRISON ST","HAYS","KS","",38.878,-99.335],"DIANE BOSTOW debostow@aol.com 5402309770":["223 W JASPER ST","GWINN","MI","",46.331,-87.44],"DO NOT USE NA-US-TX-League City-2455 Tuscan Lakes Blvd-OFFSITE":["2455 Tuscan Lakes Blvd","League City","TX","77573",29.517,-95.096],"DOROTHY HANBY chanby@aol.com 4798418822":["2766 CARLEY RD","SPRINGDALE","AR","",36.179,-94.125],"DOUGLAS McRAE douglas.mcrae@ucb.com 8647751068":["100 SANDERLING LN","GREENVILLE","SC","",34.855,-82.412],"Derek Hasty derek.hasty@astrazeneca.com 5023214188":["247 CHAMPIONS WAY","SIMPSONVILLE","KY","",38.231,-85.355],"Donna Huffstater donna.huffstater@novartis.com 7202810006":["1016 MAJESTIC OAKS WAY","SIMPSONVILLE","KY","",38.231,-85.355],"Douglas Lloyd rlloyd@clear-lake.com 6202786905":["909-B Kingswood lane","Hutchinson","KS","",38.041,-97.97],"EDWARD JOHNSTON nedjo@mac.com 2074600770":["66 YOUNGS MOUNTAIN RD","BAR HARBOR","ME","",44.374,-68.245],"ELIJAH DURHAM elijahdurham148@gmail.com 7702350485":["61 SEACOAST TER","KITTERY","ME","",43.092,-70.743],"ELMERS AUTO BODY":["203 Crescent Blvd","Mount Ephraim","NJ","08059",39.883,-75.093],"ERIC STEWART hadroncoldiron@gmail.com 4794453397":["3051 N PALMILLA DR","FAYETTEVILLE","AR","",36.075,-94.198],"EdwardJohnston - 66 Youngs Mountain Rd, BAR HARBOR, ME, US, 04609":["66 Youngs Mountain Rd","Bar Harbor","ME","04609",44.374,-68.245],"Emmanuel Castillo":["9000 Virginia Manor Rd","Beltsville","MD","20705",39.045,-76.924],"Energy FST Staging Area - NA-US-FL-Orlando-9424 Southridge Park Ct":["9424 SOUTHRIDGE PARK CT","Orlando","FL","",28.518,-81.307],"Energy Fremont Operational Center":["45401 Research Ave","Fremont","CA","94539",37.518,-121.929],"Fort Lauderdale Energy Ops WH":["5350 NW 35th Terrace Suite 100A","Fort Lauderdale","FL","",26.128,-80.212],"Frederick Nowell nick_nowell@yahoo.com 6179530145":["88 CAMERONS LN","WELLS","ME","",43.314,-70.597],"Fremont Factory":["45500 Fremont Blvd","Fremont","CA","94538",37.531,-121.971],"Fremont NPI - Blackbird":["48401 Fremont Blvd","Fremont","CA","",37.573,-121.974],"Fresno Armstrong Energy Ops WH":["651 N. Armstrong Ave, Suite 108","Fresno","CA","93727",36.753,-119.706],"GARY SMITH smith_garyw@yahoo.com 7817263617":["32 SUNSET RDG","OGUNQUIT","ME","",43.254,-70.609],"GCLR - Lithium Refinery":["4518 Co Rd 28","Robstown","TX","",27.798,-97.7],"GEORGE MATHEWS georgemathews789@yahoo.com 7165811972":["8398 SCHULTZ DR","WESTFIELD","NY","",42.322,-79.573],"Gary Mccormick gary-1.mccormick@novartis.com 9085817732":["32 WOOLSTON WAY","WASHINGTON","NJ","",40.758,-74.991],"Gary Smith":["32 SUNSET RDG","Ogunquit","ME","03907",43.254,-70.609],"George Emery pieceofcake1@gmail.com 2077038886":["346 Haley Rd","Kittery","ME","",43.092,-70.743],"Goodyear Proving Ground TX":["11570 N US Hwy 277","San Angelo","TX","76905",31.465,-100.39],"Greg Huda gregory@vetssecuringamerica.com 8129685218":["1941 BISHOP LN","LOUISVILLE","KY","",38.208,-85.696],"HAILEY WOODRUFF haileywoodruff22@gmail.com +13607757542":["130 TAXI LITS","LA PUSH","WA","",47.905,-124.626],"HAYLEY HORTON hayleyhorton@ymail.com 7147933535":["5066 US-6","BISHOP","CA","",37.432,-118.4],"HIREN AHIR hahir00@icloud.com 2137093755":["2930 MAIN AVE","DURANGO","CO","",37.226,-107.878],"HOLLY WONG cyclinggirl505@gmail.com 4085056558":["4691 ALBANY CIR 122","San Jose","CA","",37.32,-121.879],"Hayward Energy WH Huntwood":["31353 HUNTWOOD AVE","Hayward","CA","",37.662,-122.032],"Henrietta":["3535 West Henrietta Road","Rochester","NY","14623",43.083,-77.634],"Houston-Manheim Texas Hobby":["8215 Kopman Rd","Houston","TX","",29.798,-95.419],"IIHS":["988 Dairy Road","Ruckersville","VA","",38.259,-78.407],"ISABELLA RODRIGUEZ bellarodriguez00216@gmail.com 5756529232":["4367 ROSE GOLD ST","LAS CRUCES","NM","",32.38,-106.769],"Inland Empire Energy Ops WH":["1755 Iowa Avenue, Building B","Riverside","CA","92507",33.976,-117.339],"Izac Vargas flijoji@gmail.com 9706286499":["43200 CO-141","GATEWAY","CO","",38.678,-108.972],"JACOB FULLER jacob.fuller@astrazeneca.com 3363827323":["1081 LUNA CRK CT","KERNERSVILLE","NC","",36.118,-80.078],"JACY MASK jacysbags@gmail.com 4798066507":["220 MARINA DR","HACKETT","AR","",35.194,-94.398],"JAMES DECKARD jaydeckard@aol.com 7144743660":["29 STARFLOWER LN","BRUNSWICK","ME","",43.897,-69.978],"JAMES SIVILS jsivils@environmentalworks.com 4178617794":["1455 E CHESTNUT EXPRESSWAY","SPRINGFIELD","MO","",37.216,-93.303],"JASON SIVAK jasonsivak@hotmail.com 8146888996":["4320 BAYVIEW RD","BEMUS POINT","NY","",42.151,-79.358],"JAVIER MARIN-SORE javier@tiempocompany.com 6172815435":["75 CEDAR LN","OGUNQUIT","ME","",43.254,-70.609],"JEREMY ANNIS andria.annis@gmail.com 7204128177":["3184 W MAPLE RIDGE 37 RD","ROCK","MI","",46.05,-87.133],"JESSE PARKS jesse@veritasav.com 9707690548":["25552 RD N 6 LOOP","CORTEZ","CO","",37.355,-108.584],"JOHN KEENAN jmkacademics@gmail.com 4792002078":["1184 N AMBERWOOD LN","FAYETTEVILLE","AR","",36.075,-94.198],"JOHN WAKEFIELD skyways@mlgc.com 7017890666":["1649 JACOB DR","BINFORD","ND","",47.574,-98.355],"JOSEPH PAGAN joe_pagan@hotmail.com 2074601280":["26 DEGREGOIRE PARK","BAR HARBOR","ME","",44.374,-68.245],"JUAN BARRAZA jjbarraza88030@gmail.com 5755928555":["1415 S MESILLA ST","DEMING","NM","",32.236,-107.743],"JUAN ORNELAS araceli_87_7@hotmail.com 5753908434":["336 W TUCKER ST","HOBBS","NM","",32.746,-103.162],"Jacqueline Butkovic jacqueline.butkovic@novartis.com 2032321380":["16 PEACH ORCHARD LANE","BANTAM","CT","",41.721,-73.252],"Jason Middleton jason.middleton@siemens-energy.com 5738238431":["1485 Sound Avenue","Baiting Hollow","NY","",null,null],"Jeffrey Boone jeffrey.boone@varian.com 7047379395":["15031 BEATTIES FORD RD","HUNTERSVILLE","NC","",35.406,-80.856],"Jeffrey Roh jeffrey.roh@astrazeneca.com 4026577014":["17476 K ST","OMAHA","NE","",41.256,-96.006],"Jennifer Moses jennifer.moses@novartis.com 8434259652":["4 DALTON ST","DANIEL ISLAND","SC","",null,null],"Jerry Mcswain jerry.mcswain@siemens-healthineers.com 8437438458":["134 COOSAWATCHIE ST","SUMMERVILLE","SC","",33.006,-80.19],"Jessica Shue jessica.shue@novartis.com 7047736098":["4000 Kilbourne Rd","Columbia","SC","",34.016,-81.008],"Jessica White jessica.perry@astrazeneca.com 4194815677":["3647 BARCELONA DR","TOLEDO","OH","",41.676,-83.531],"Joe Hudson - 241 Tresca Rd":["241 Tresca Road","Jacksonville","FL","32225",30.351,-81.506],"Jonathan Hendricks jonathan.hendricks@astrazeneca.com 3049602209":["426 SAXON PL","BLUEFIELD","WV","",37.27,-81.222],"Joseph Morgart":["7256 S SAM HOUSTON PKWY W STE 200","Houston","TX","77085",29.622,-95.482],"Joseph Pagan":["26 Degregoire Park","Bar Harbor","ME","04609",44.374,-68.245],"Julia Compton julia.compton@novartis.com 2059992151":["2218 ENGLISH VILLAGE LN","MOUNTAIN BRK","AL","",null,null],"KARA VERA kara_kartchner@me.com 8015603511":["3720 BRADEN WAY","ELKO","NV","",40.857,-115.687],"KATHLEEN HELMING helmingkathy@gmail.com 2072322845":["137 LITTLE ACRES DR","CUMBERLAND","ME","",null,null],"KATHRYN RIEDEL kwriedel@yahoo.com 5019153408":["86 MAJORCA DR","HOT SPRINGS","AR","",34.66,-92.991],"KELBY KLEINSASSER kelbyk@gmail.com 6052619179":["1201 W KILLARNEY ST","SIOUX FALLS","SD","",43.59,-96.751],"KELSEY PATTON austinpatton777@gmail.com 9189318375":["807 KAUFMAN AVE","TAHLEQUAH","OK","",35.905,-95.009],"KENT GRAEVE kentgraeve@gmail.com 2059993474":["400 UNIVERSITY PARK DR","BIRMINGHAM","AL","",33.516,-86.844],"KEVIN GATERE kevingatere@gmail.com 8583663497":["4331 W GLACIER ST","FAYETTEVILLE","AR","",36.075,-94.198],"KRISTINE DELANO":["111 ELM ST","Saco","ME","04072",43.521,-70.455],"KRISTINE DELANO krisdelano@yahoo.com 2072339374":["111 ELM ST","SACO","ME","",43.521,-70.455],"Karl Borchers karl.borchers@astrazeneca.com 6186048982":["6541 MADENA DR","GLEN CARBON","IL","",38.761,-89.971],"Kristen Zimmerman kristen.zimmerman@novartis.com 4843541993":["1713 FROST LN","WEST CHESTER","PA","",39.963,-75.6],"LANDON JOSEPH hisandhersautonevada@gmail.com 7759348984":["1391 IDAHO ST","ELKO","NV","",40.857,-115.687],"LARRY MCCANNA lpmc1986@gmail.com 7079514727":["2161 LAGOON AVE","CRESCENT CITY","CA","",41.769,-124.167],"LAURA ZITO":["4642 SUNBEAM CIR","Billings","MT","59106",45.775,-108.652],"LAURA ZITO llzito19@gmail.com 4066729564":["4642 SUNBEAM CIR","BILLINGS","MT","",45.797,-108.516],"LAUREN HALLOCK lauren.hallock@astrazeneca.com 3304410338":["5619 KRAUS RD","CLARENCE","NY","",42.981,-78.616],"LEON OLACIO olacioleon23@gmail.com +15755020462":["5032 TRUMBULL AVE","LAS CRUCES","NM","",32.38,-106.769],"LESLEY MAHANEY lesleymahaney@hotmail.com 2074600530":["2 Lyman Lane","NORTHEAST HARBOR","ME","",44.294,-68.285],"LHI Charging 459311":["4511 N Midkiff Rd","Midland","TX","",31.939,-102.067],"LISA PALLADINO lilipalla@yahoo.com 2072954947":["541 E MAIN ST","YARMOUTH","ME","",43.801,-70.175],"LOIS ADRIAN lachocolategulch@gmail.com 2084208396":["11589 RED CANYON RD","HOT SPRINGS","SD","",43.422,-103.477],"Las Vegas - West":["7077 W SAHARA AVE","Las Vegas","NV","89117",36.13,-115.275],"Las Vegas Body Repair":["6260 Badura Ave","Las Vegas","NV","89118",36.081,-115.217],"Las Vegas Post Rd - Virtual FST":["6561 W Post Rd - FST","Las Vegas","NV","89118",36.081,-115.217],"LizMahsem - 200 West Beltline Hwy, Madison, WI, US, 53713":["200 W Beltline Hwy","Madison","WI","53713",43.037,-89.397],"MANHEIM PALM BEACH":["600 SANSBURY WAY","West Palm Beach","FL","33411",26.664,-80.174],"MARGARET IVANESCU soldby.margaret@yahoo.com 5756934599":["811 NM-77","CLOVIS","NM","",34.409,-103.213],"MARLA HARDY freedom226@msn.com 5756311477":["1822 W CENTRAL AVE","LOVINGTON","NM","",32.951,-103.349],"MARTHA GILES dgiles@brook-valley.com 2058214490":["2124 LAKE HEATHER WAY","BIRMINGHAM","AL","",33.516,-86.844],"MARTHA SHEPARDSON-KILLAM mskillam@gmail.com 6033390566":["149 BEACH AVE","KENNEBUNK","ME","",43.388,-70.548],"MATTHEW FRANCISCI matthew.francisci1@astrazeneca.com 8563641424":["201 E GAY ST","WEST CHESTER","PA","",39.963,-75.6],"MEGAN KROUSE toby21163@gmail.com 6107420701":["116 FIELD RD","FALMOUTH","ME","",43.734,-70.263],"MEGAN PHAN thanh1danh@yahoo.com 9418097647":["3816 BALER LN","CARLSBAD","NM","",32.377,-104.267],"MELVILLE CONNER mmconner@yahoo.com 5409357363":["490 HARPSWELL ISLANDS RD","HARPSWELL","ME","",43.781,-69.996],"MICHAEL PADILLA dualghost@gmail.com 6232099407":["414 VANGUARD DR","WHITE SANDS MISSILE RANGE","NM","",32.384,-106.494],"MICHAEL SAMUEL":["101 OWLS NEST RD","Portland","ME","04102",43.66,-70.29],"MICHAEL SAMUEL michael_samowel@yahoo.com 5512003062":["101 OWLS NEST RD","PORTLAND","ME","",43.666,-70.257],"MICHELE VINICK michele.vinicki@astrazeneca.com 6109521738":["170 MORNINGSIDE CIR","WAYNE","PA","",40.021,-75.395],"MICHELLE DOSTAL michelle.j.dostal@gmail.com 7076729152":["315 GREEN RD","KNEELAND","CA","",40.641,-123.883],"Mallorie Roberts mallorie.roberts@astrazeneca.com 8124535794":["4119 LINCOLN AVE","EVANSVILLE","IN","",37.996,-87.57],"Marc Montana - 12450 Universal Drive, Taylor, MI, US, 48180":["12450 Universal Drive","Taylor","MI","",42.232,-83.267],"Marlborough Energy Ops WH":["24 St. Martin Drive, Building 2-11","Marlborough","MA","01752",42.351,-71.543],"Mary Caroline Martin carolinemartin@charter.net 7572887508":["135 OLD HERITAGE PL","OAKLAND","ME","",44.517,-69.74],"Matthew Mclaughlin matthew.mclaughlin@siemens-healthineers.com 6105517180":["1425 Bentley Dr","Warrington","PA","",40.246,-75.135],"Matthew Simpson matthew.simpson@siemens-healthineers.com 5674699386":["2228 ROBINWOOD AVE","TOLEDO","OH","",41.676,-83.531],"Megan Krouse":["116 Field Rd","Falmouth","ME","04105",43.734,-70.263],"Melanie Vlasic melanie.vlasic@astrazeneca.com 2673287118":["152 LEVERING ST","PHILADELPHIA","PA","",39.991,-75.143],"Melissa Mejico mmejico@pointbank.com 9406863228":["525 S Interstate 35","Denton","TX","",33.205,-97.12],"Michael Clark michael.clark@erlangerpd.com 8599404519":["505 Commonwealth Avenue","Erlanger","KY","",39.034,-84.614],"Michael Sanders michael.sanders@siemens-healthineers.com 9289204410":["481 OLD MOUNT PLEASANT RD","BUNCOMBE","IL","",37.464,-88.981],"Michelle Dostal - 315 GREEN RD, KNEELAND, CA, US, 95549-9014":["315 GREEN RD","Kneeland","CA","95549",40.641,-123.883],"Mike Baltazar TESLA":["660 E Louise Ave","Lathrop","CA","",37.821,-121.283],"Morey Shad":["201 Logistics Dr bldg 1","Kyle","TX","78640",29.997,-97.834],"Murphy 2 Storage Megafactory Lathrop":["17100 Murphy Pkwy Dock 88","Lathrop","CA","",37.821,-121.283],"NA-US-AL-Birmingham-2625 4th Ave S":["2625 4th Ave S","Birmingham","AL","35233",33.506,-86.8],"NA-US-AL-RR-Birmingham-BNSF":["401 Finley Blvd","Birmingham","AL","35204",33.518,-86.837],"NA-US-AL-Wetumpka-100 River Oaks Dr":["100 River Oaks Dr","Wetumpka","AL","",32.577,-86.157],"NA-US-AZ-Deer Valley":["21030 N 19TH AVE","Phoenix","AZ","",33.492,-112.079],"NA-US-AZ-Gilbert-2156 E Williams Field Rd":["2156 E Williams Field Rd","Gilbert","AZ","",33.316,-111.754],"NA-US-AZ-Glendale-6770 N Sunrise Blvd":["6770 N Sunrise Blvd","Glendale","AZ","85305",33.529,-112.248],"NA-US-AZ-Litchfield Park":["6302 N Litchfield Rd","Litchfield Park","AZ","",33.51,-112.413],"NA-US-AZ-Litchfield Park-6302 N Litchfield Rd - 20194":["6302 N Litchfield Rd","Litchfield Park","AZ","",33.51,-112.413],"NA-US-AZ-Mesa-7427 E Hampton Ave":["7427 E Hampton Ave","Mesa","AZ","85209",33.378,-111.641],"NA-US-AZ-Phoenix Buckeye Rd":["1875 E Sky Harbor Cir N","Phoenix","AZ","85034",33.441,-112.042],"NA-US-AZ-Scottsdale":["8300 East Raintree Drive","Scottsdale","AZ","",33.574,-111.888],"NA-US-AZ-Scottsdale-Hayden":["15301 N Hayden Rd","Scottsdale","AZ","85260",33.601,-111.887],"NA-US-AZ-TA-Mesa":["7444 E Hampton Ave","Mesa","AZ","",33.414,-111.771],"NA-US-AZ-TA-Tucson":["5081 N Oracle Rd","Tucson","AZ","",32.225,-110.945],"NA-US-AZ-Tempe":["2077 E University Dr","Tempe","AZ","85288",33.442,-111.924],"NA-US-AZ-Tempe-615 S River Dr":["615 S River Dr","Tempe","AZ","85281",33.423,-111.926],"NA-US-AZ-Tucson":["5081 N Oracle Rd","Tucson","AZ","",32.225,-110.945],"NA-US-AZ-UVR-Phoenix Collision":["18808 N 32nd St","Phoenix","AZ","85050",33.686,-111.996],"NA-US-CA-Adesa Riverside":["9700 Galena St","Riverside","CA","92509",34.003,-117.445],"NA-US-CA-Aliso Viejo":["26501 ALISO CREEK RD","Aliso Viejo","CA","",33.619,-117.774],"NA-US-CA-Anaheim-La Palma":["5635 E LA PALMA AVE","Anaheim","CA","92807",33.854,-117.786],"NA-US-CA-Aptos-Seascape Resort Dr":["1 Seascape Resort Dr","Aptos","CA","",36.979,-121.894],"NA-US-CA-Bakersfield":["5206 Young St","Bakersfield","CA","93311",35.304,-119.106],"NA-US-CA-Bakersfield-2720 Auto Mall Dr":["2720 Auto Mall Dr","Bakersfield","CA","93313",35.297,-119.051],"NA-US-CA-Buena Park-Buena Park Downtown":["8308 On the Mall","Buena Park","CA","",33.856,-118.001],"NA-US-CA-Camarillo-4600 Calle Bolero":["4605 Calle Bolero","Camarillo","CA","",34.223,-119.024],"NA-US-CA-Camarillo-4605 Calle Quetzal":["4605 Calle Quetzal","Camarillo","CA","",34.223,-119.024],"NA-US-CA-Centinela-West Centinela Avenue":["5840 West Centinela Avenue","Centinela","CA","90045",33.963,-118.394],"NA-US-CA-Colma":["1500 Collins Ave","Colma","CA","",null,null],"NA-US-CA-Colma-255 D St":["255 D St","Colma","CA","95112",37.348,-121.887],"NA-US-CA-Costa Mesa-3565 Cadillac Ave":["3565 CADILLAC AVE","Costa Mesa","CA","",33.656,-117.913],"NA-US-CA-Covina-W San Bernardino":["137 W San Bernardino Rd","Covina","CA","",34.092,-117.882],"NA-US-CA-Culver City-6000 Sepulveda Blvd":["6000 Sepulveda Blvd","Culver City","CA","",34.013,-118.397],"NA-US-CA-Davis-1640 Research Park Dr":["1640 Research Park Dr","Davis","CA","95618",38.545,-121.74],"NA-US-CA-Dublin-6270 Houston Pl":["6270 HOUSTON PL","Dublin","CA","",37.717,-121.923],"NA-US-CA-Emeryville-Remote Test Drive":["5800 Shellmound St","Emeryville","CA","94608",37.837,-122.28],"NA-US-CA-Eureka-3300 Broadway St":["3300 Broadway St","Eureka","CA","95501",40.794,-124.157],"NA-US-CA-Fremont-40445 Albrae St - 442366":["40445 Albrae St","Fremont","CA","",37.573,-121.974],"NA-US-CA-Fremont-43800 Osgood Rd":["43800 Osgood Rd","Fremont","CA","94539",37.518,-121.929],"NA-US-CA-Fremont-47700 Kato Road":["47700 Kato Road","Fremont","CA","94306",37.418,-122.127],"NA-US-CA-Fremont-Delivery Offsite":["48401 Fremont Blvd","Fremont","CA","94306",37.418,-122.127],"NA-US-CA-Fremont-Grimmer":["44710 Fremont Blvd","Fremont","CA","94538",37.531,-121.971],"NA-US-CA-Fremont-Osgood Rd":["43801 Osgood Rd","Fremont","CA","94539",37.518,-121.929],"NA-US-CA-Fresno-4603 N Brawley Ave":["4603 N Brawley Ave","Fresno","CA","",36.756,-119.693],"NA-US-CA-Fresno-711-W Palmdon Dr":["711 W Palmdon Dr","Fresno","CA","",36.756,-119.693],"NA-US-CA-Fresno-777 W Palmdon Dr":["777 W Palmdon Dr","Fresno","CA","",36.756,-119.693],"NA-US-CA-Gilroy-400 Automall Dr":["400 Automall Dr","Gilroy","CA","",37.012,-121.574],"NA-US-CA-Gilroy-500 Automall Dr-500 Automall Dr , Gilroy, CA, US, 95020":["500 Automall Dr","Gilroy","CA","95020",37.014,-121.577],"NA-US-CA-Gilroy-Automall":["500 Automall Dr","Gilroy","CA","95020",37.014,-121.577],"NA-US-CA-Hawthorne-13020 Yukon Ave":["13020 Yukon Ave Suite A2/A3","Hawthorne","CA","",33.915,-118.351],"NA-US-CA-Hawthorne-13040 Cerise Ave":["13040 Cerise Ave","Hawthorne","CA","",33.915,-118.351],"NA-US-CA-Hayward-22290 Hathaway Ave":["22290 Hathaway Ave","Hayward","CA","",37.662,-122.032],"NA-US-CA-Hub-Prune":["2875 PRUNE AVE","Fremont","CA","94539",37.518,-121.929],"NA-US-CA-Huntington Beach":["15461 Springdale St","Huntington Beach","CA","",33.692,-118.001],"NA-US-CA-Irvine-18011 Mitchell S":["18011 Mitchell S","Irvine","CA","",33.678,-117.797],"NA-US-CA-Irvine-Costa Mesa-Gillette Ave-Offsite Parking":["17832 Gillette Ave","Irvine","CA","92614",33.683,-117.83],"NA-US-CA-Lathrop-5150 Glacier St":["5150 Glacier St","Lathrop","CA","95330",37.821,-121.283],"NA-US-CA-Long Beach-3900 Kilroy Airport Way OFFSITE":["3900 Kilroy Airport Way","Long Beach","CA","",33.788,-118.195],"NA-US-CA-Los Angeles-11120 Peoria St OFFSITE":["11120 Peoria St","Los Angeles","CA","",34.034,-118.281],"NA-US-CA-Los Angeles-Century City":["10250 Santa Monica Blvd","Los Angeles","CA","90067",34.055,-118.409],"NA-US-CA-McClellan Park-3131 Peacekeeper Way":["3131 Peacekeeper Way","McClellan Park","CA","95652",38.662,-121.395],"NA-US-CA-Mira Loma-Adesa Los Angeles":["11625 Nino Way","Mira Loma","CA","91752",33.994,-117.524],"NA-US-CA-Mobile Service Los Gatos":["15500 LOS GATOS BLVD","Los Gatos","CA","95133",37.373,-121.856],"NA-US-CA-Montebello":["1345 N Montebello Blvd","Montebello","CA","",34.013,-118.113],"NA-US-CA-Montebello Mobile Service":["1345 N Montebello Blvd","Montebello","CA","90640",34.013,-118.113],"NA-US-CA-Montebello-1445 N Montebello Blvd":["1445 N Montebello Blvd","Montebello","CA","",34.013,-118.113],"NA-US-CA-National City-Pasha-National City":["1309 Bay Marina Drive","National City","CA","",32.677,-117.094],"NA-US-CA-North Hollywood-Sherman Wy":["12120 Sherman Wy","North Hollywood","CA","91605",34.206,-118.4],"NA-US-CA-Ontario-2837 E Cedar St":["2837 E Cedar St","Ontario","CA","",34.057,-117.64],"NA-US-CA-Palm Springs":["68080 Perez Road","Cathedral City","CA","",33.795,-116.466],"NA-US-CA-Palo Alto":["4180 El Camino Real","Palo Alto","CA","",37.441,-122.148],"NA-US-CA-Palo Alto-Hillview Ave-TIBCO-Parking":["3307 Hillview Ave","Palo Alto","CA","94304",37.433,-122.184],"NA-US-CA-Pleasanton-Stoneridge Mall":["1 Stoneridge Mall Rd","Pleasanton","CA","",37.677,-121.886],"NA-US-CA-Port Hueneme-Port Hueneme - Yokohama":["Pacific RoRo Terminal, Berth #5 (end of Hueneme Road)","Port Hueneme","CA","93041",34.163,-119.197],"NA-US-CA-ROCKLIN-1104 TINKER RD":["1104 Tinker Rd","Rocklin","CA","",38.801,-121.252],"NA-US-CA-RR-Milpitas-UP":["650 Hammond Way, Milpitas, CA","Milpitas","CA","",37.43,-121.9],"NA-US-CA-RR-Mira Loma-UP":["4500 Etiwanda Ave","JURUPA VALLEY","CA","91752",33.994,-117.524],"NA-US-CA-Rancho Cordova-Decommission":["3374 Fitzgerald Rd","Rancho Cordova","CA","",38.598,-121.264],"NA-US-CA-Redding-Hilltop Dr":["1900 Hilltop Dr","Redding","CA","96002",40.549,-122.334],"NA-US-CA-Riverside":["7920 Lindbergh Drive","Riverside","CA","",33.951,-117.397],"NA-US-CA-Riverside-Manheim Riverside":["6446 Fremont St","Riverside","CA","92504",33.931,-117.412],"NA-US-CA-Riverside/Inland Empire-Iowa Avenue":["1755 Iowa Avenue, Bldg B","Riverside","CA","92507",33.976,-117.339],"NA-US-CA-Rocklin":["Granite Dr 4361","Rocklin","CA","95677",38.788,-121.237],"NA-US-CA-Rocklin-4361 Granite Dr":["4361 Granite Dr","Rocklin","CA","95677",38.788,-121.237],"NA-US-CA-Sacramento-2535 Arden Way,":["2535 Arden Way","Sacramento","CA","95825",38.589,-121.406],"NA-US-CA-Sacramento-8470 Belvedere Ave":["8470 Belvedere Ave","Sacramento","CA","95826",38.554,-121.369],"NA-US-CA-San Bernardino-Collision":["424 W Orange Show Rd","San Bernardino","CA","92408",34.083,-117.271],"NA-US-CA-San Bernardino-Harry Sheppard":["1616 Harry Sheppard Blvd","San Bernardino","CA","92408",34.083,-117.271],"NA-US-CA-San Diego-Adesa San Diego":["2175 Cactus Rd","San Diego","CA","92154",32.575,-117.071],"NA-US-CA-San Diego-Kearny Mesa":["5600 Kearny Mesa Road","San Diego","CA","92111",32.797,-117.171],"NA-US-CA-San Jose-1055 Commercial Ct":["1055 Commercial Ct","San Jose","CA","95112",37.348,-121.887],"NA-US-CA-San Jose-1620 Berryessa Rd":["1620 Berryessa Rd","San Jose","CA","",37.32,-121.879],"NA-US-CA-San Jose-2371 South Evergreen Loop Circle - 35823":["2371 South Evergreen Loop Circle","San Jose","CA","95122",37.329,-121.834],"NA-US-CA-San Jose-Santana Row":["333 Santana Row Suite 1015","San Jose","CA","95128",37.316,-121.936],"NA-US-CA-San Luis Obispo":["1381 CALLE JOAQUIN","San Luis Obispo","CA","",35.282,-120.633],"NA-US-CA-San Mateo-60 31st Ave":["60 31st Ave","San Mateo","CA","94403",37.539,-122.3],"NA-US-CA-San Rafael":["454 DU BOIS STREET","San Rafael","CA","",38.005,-122.544],"NA-US-CA-San Rafael-2350 Kerner Blvd":["2350 Kerner Blvd","San Rafael","CA","94901",37.969,-122.51],"NA-US-CA-Santa Barbara":["400 HITCHCOCK WAY","Santa Barbara","CA","",34.426,-119.724],"NA-US-CA-Santa Clarita-24201 Valencia Blvd":["24201 Valencia Blvd","Santa Clarita","CA","",34.415,-118.531],"NA-US-CA-Santa Monica 1100 Colorado Ave":["1100 Colorado Ave","Santa Monica","CA","90401",34.018,-118.491],"NA-US-CA-Santa Monica-2719 Pennsylvania Ave":["2719 Pennsylvania Ave","Santa Monica","CA","90404",34.027,-118.473],"NA-US-CA-Santa Rosa-3304 Industrial Dr":["3304 Industrial Dr","Santa Rosa","CA","95403",38.482,-122.747],"NA-US-CA-South San Francisco-175 Sylvester Road":["175 Sylvester Rd","South San Francisco","CA","94080",37.657,-122.424],"NA-US-CA-Stockton-Auto Center Circle":["3131 Auto Center Circle","Stockton","CA","95212",38.032,-121.259],"NA-US-CA-Sunnyvale-680 E El Camino Real":["680 E El Camino Real","Sunnyvale","CA","94087",37.35,-122.035],"NA-US-CA-Sunnyvale-Body Repair":["1235 ELKO DR","Sunnyvale","CA","",37.376,-122.023],"NA-US-CA-Sunnyvale-Offsite":["710 E El Camino Real","Sunnyvale","CA","94087",37.35,-122.035],"NA-US-CA-TA-Corte Madera":["201 Casa Buena Dr","Corte Madera","CA","",37.924,-122.52],"NA-US-CA-TA-Los Gatos":["15500 LOS GATOS BLVD","Los Gatos","CA","",37.196,-121.972],"NA-US-CA-TA-Monterey-Seaside":["1901 1901 Del Monte Blvd Seaside, CA 93955","Seaside","CA","93955",36.622,-121.793],"NA-US-CA-TA-Palm Springs":["68080 Perez Road","Cathedral City","CA","92234",33.81,-116.466],"NA-US-CA-TA-Sacramento-Arden":["2535 Arden Wy","Sacramento","CA","95825",38.589,-121.406],"NA-US-CA-TA-Sacramento-Rocklin":["4361 Granite Dr","Rocklin","CA","95677",38.788,-121.237],"NA-US-CA-TA-San Diego-Trade-offsite":["7337 Trade St","San Diego","CA","",32.755,-117.147],"NA-US-CA-TA1-Burlingame":["50 Edwards Ct","Burlingame","CA","94010",37.567,-122.368],"NA-US-CA-TA1-Fremont Service":["48370 Kato Rd","Fremont","CA","94538",37.531,-121.971],"NA-US-CA-Temecula":["43191 Rancho Way","Temecula","CA","92590",33.49,-117.182],"NA-US-CA-Temecula-":["27635 Diaz Rd","Temecula","CA","",33.499,-117.141],"NA-US-CA-TempHub-Boyce":["41777 Boyce Rd","Fremont","CA","94538",37.531,-121.971],"NA-US-CA-TempHub-Ontario":["2202 S Milliken Ave","Ontario","CA","91761",34.032,-117.619],"NA-US-CA-Tesla Lathrop-700 D'Arcy Pkwy,":["700 D'Arcy Pkwy","Lathrop","CA","",37.821,-121.283],"NA-US-CA-Tesla Service Berkeley-901 Gilman":["901 Gilman St","Berkeley","CA","94710",37.87,-122.296],"NA-US-CA-Thousand Oaks":["2000 Corporate Center Dr","Thousand Oaks","CA","",34.192,-118.845],"NA-US-CA-Thousand Oaks-375 Conejo Ridge Ave":["375 Conejo Ridge Ave","Thousand Oaks","CA","",34.192,-118.845],"NA-US-CA-Torrance-Del Amo Mall":["3525 W Carson St. Space 419","Torrance","CA","90503",33.84,-118.354],"NA-US-CA-Tracy-1150 Arbor Ave":["1150 Arbor Ave","Tracy","CA","",37.715,-121.462],"NA-US-CA-Tracy-Adesa Golden Gate":["18501 W. Stanford Road","Tracy","CA","95377",37.657,-121.496],"NA-US-CA-UVR-Norwalk":["11729 Imperial Hwy.","Norwalk","CA","",33.903,-118.082],"NA-US-CA-UVR-Norwalk Collision":["11729 Imperial Hwy.","Norwalk","CA","",33.903,-118.082],"NA-US-CA-Ukiah-1050 S State St":["1050 S State St","Ukiah","CA","",39.155,-123.195],"NA-US-CA-Upland":["1018 E 20TH ST","Upland","CA","",34.123,-117.658],"NA-US-CA-Vallejo-900 Fairgrounds Dr":["900 Fairgrounds Dr","Vallejo","CA","94589",38.158,-122.28],"NA-US-CA-Visalia-9300 W Airport Dr":["9300 W Airport Dr","Visalia","CA","",36.331,-119.296],"NA-US-CA-Vista-Commerce OFFSITE":["2611 Commerce Way","Vista","CA","",33.194,-117.239],"NA-US-CA-Vista-Oak Ridge Way":["2370 Oak Ridge Way","Vista","CA","92081",33.164,-117.24],"NA-US-CO-Aurora":["11951 EAST 33RD AVENUE","Aurora","CO","",39.709,-104.706],"NA-US-CO-Aurora-14800 E 35th Pl Tesla":["14800 E 35th Pl","Aurora","CO","",39.709,-104.706],"NA-US-CO-Centennial":["8677 Double Helix Ct","Englewood","CO","80112",39.581,-104.901],"NA-US-CO-Colorado Springs":["1323 Motor City Dr","Colorado Springs","CO","80905",38.838,-104.837],"NA-US-CO-Denver Body Shop":["450 E 52ND AVE","Denver","CO","",39.739,-104.982],"NA-US-CO-Fountain-Adesa Colorado Springs":["10680 Charter Oak Ranch Rd","Fountain","CO","80817",38.7,-104.701],"NA-US-CO-Gypsum":["550 Plane St.","Gypsum","CO","81637",39.662,-106.967],"NA-US-CO-Littleton":["5700 S Broadway","Littleton","CO","80121",39.611,-104.953],"NA-US-CO-Loveland":["1606 N Lincoln Ave","Loveland","CO","80538",40.426,-105.09],"NA-US-CO-RR-Henderson-UP":["9900 I-76 Service Road","Henderson","CO","80640",39.898,-104.872],"NA-US-CO-Superior":["2 S Marshall Rd","Superior","CO","80027",39.979,-105.146],"NA-US-CO-UVR-Frederick Collision":["4076 Salazar Way","Longmont","CO","80504",40.131,-104.95],"NA-US-CT-Hartford-International Drive":["16 International Drive","East Granby","CT","06026",41.932,-72.746],"NA-US-CT-Milford":["881 Boston Post Rd","Milford","CT","06460",41.218,-73.055],"NA-US-CT-Old Saybrook-Bridge St":["2 Bridge St","Old Saybrook","CT","06475",41.291,-72.385],"NA-US-CT-Rocky Hill-Cromwell Ave":["685 Cromwell Ave","Rocky Hill","CT","",41.658,-72.663],"NA-US-CT-Stamford":["106 Commerce Rd","Stamford","CT","06902",41.06,-73.544],"NA-US-CT-Uncasville-Mohegan":["1 Mohegan Sun Blvd","Uncasville","CT","",41.462,-72.113],"NA-US-CT-West Hartford-1500 New Britain Ave":["1500 New Britain Ave","West Hartford","CT","06110",41.733,-72.734],"NA-US-DE-TA1-Wilmington":["600 First State Boulevard","Wilmington","DE","19804",39.717,-75.618],"NA-US-FL-Clermont":["16775 State Road 50.","Clermont","FL","",28.538,-81.763],"NA-US-FL-Coral Gables-4425 Ponce de Leon":["4425 Ponce de Leon","Coral Gables","FL","33146",25.721,-80.273],"NA-US-FL-Crestview-905 Southcrest Dr":["905 Southcrest Dr","Crestview","FL","32536",30.764,-86.592],"NA-US-FL-Daytona Beach":["1221 N Williamson Blvd.","Daytona Beach","FL","",29.196,-81.033],"NA-US-FL-DeLand-180 Fenway Dr":["180 Fenway Dr","DELAND","FL","32724",29.042,-81.286],"NA-US-FL-Delray Beach":["3000 S Federal Hwy","Delray Beach","FL","33483",26.455,-80.066],"NA-US-FL-Destin-14060-Emerald Coast Parkway":["14060 Emerald Coast Pkwy","Destin","FL","32541",30.395,-86.469],"NA-US-FL-Eatonville-Orlando Body Repair":["100 SOUTH LAKE DESTINY DRIVE","Orlando","FL","32810",28.621,-81.429],"NA-US-FL-Fort Lauderdale Collision":["700 W Sunrise Blvd","Fort Lauderdale","FL","",26.128,-80.212],"NA-US-FL-Fort Lauderdale-700 W Sunrise Blvd":["700 W Sunrise Blvd","Fort Lauderdale","FL","",26.128,-80.212],"NA-US-FL-Fort Lauderdale-E Sunrise Blvd":["2414 E Sunrise Blvd","Fort Lauderdale","FL","",26.128,-80.212],"NA-US-FL-Fort Myers":["8900 Colonial Center Dr","Fort Myers","FL","33905",26.669,-81.76],"NA-US-FL-Fort Walton Beach-Maryesther Cutoff NW":["522 Maryesther Cutoff NW","Fort Walton Beach","FL","32548",30.421,-86.629],"NA-US-FL-Gainesville":["2501 N MAIN ST","Gainesville","FL","32609",29.701,-82.308],"NA-US-FL-Homestead-2601-NE 9th Court":["2601 NE 9th Ct","Homestead","FL","",25.488,-80.469],"NA-US-FL-Hub-Jacksonville":["6720 W 12th St","Jacksonville","FL","",30.312,-81.653],"NA-US-FL-Hub-Orlando-Jurassic Park":["1208 Pine Ave","Orlando","FL","32824",28.393,-81.362],"NA-US-FL-Jacksonville":["11650 Abess Blvd","Jacksonville","FL","",30.312,-81.653],"NA-US-FL-Jacksonville-1153 Airport Rd":["1153 Airport Rd","Jacksonville","FL","32218",30.451,-81.663],"NA-US-FL-Jacksonville-6655 Blanding Blvd":["6655 Blanding Blvd","Jacksonville","FL","",30.312,-81.653],"NA-US-FL-Jupiter-6748 W Indiantown Rd":["6748 W Indiantown Rd","Jupiter","FL","",26.939,-80.123],"NA-US-FL-Kissimmee-800 Mary Louis Ln":["800 Mary Louis Ln","Kissimmee","FL","34744",28.308,-81.368],"NA-US-FL-Kissimmee-Maingate Ln":["3011 Maingate Ln","Kissimmee","FL","34747",28.304,-81.59],"NA-US-FL-Lakeland-3370 US Hwy 98 N":["3370 US Hwy 98 N","Lakeland","FL","33805",28.072,-81.961],"NA-US-FL-Melbourne-747 Air Terminal Pkwy":["747 Air Terminal Pkwy","Melbourne","FL","32901",28.069,-80.62],"NA-US-FL-Melbourne-N Wickham Rd":["8298 N Wickham Rd","Melbourne","FL","32940",28.206,-80.685],"NA-US-FL-Merritt Island":["1545 E Merritt Island Causeway","Merritt Island","FL","",28.401,-80.686],"NA-US-FL-Miami-1313 NW 167th St":["1313 NW 167th St","Miami","FL","",25.769,-80.259],"NA-US-FL-Miami-3200 NW 67th Ave":["3200 NW 67th Ave","Miami","FL","",25.769,-80.259],"NA-US-FL-Naples":["4555 Radio Rd","Naples","FL","",26.174,-81.729],"NA-US-FL-Ocala-4100 SW 40th St":["4100 SW 40th St","Ocala","FL","34474",29.157,-82.21],"NA-US-FL-Offsite-Altamonte Springs-Hillview":["601 Hillview Dr","Altamonte Springs","FL","32714",28.663,-81.412],"NA-US-FL-Offsite-Opa Locka":["5580 NW 145th St","Opa-locka","FL","33054",25.91,-80.247],"NA-US-FL-Opa Locka":["5580 NW 145th St","Opa-locka","FL","",25.929,-80.262],"NA-US-FL-Opa Locka-14499 NW 57th Ave":["14499 NW 57th Ave","Miami Gardens","FL","",25.942,-80.246],"NA-US-FL-Opa-locka-5499 NW 145th St":["5499 NW 145th St","Opa-locka","FL","",25.929,-80.262],"NA-US-FL-Orlando Collision":["1051 SAND LAKE RD","Orlando","FL","32809",28.464,-81.395],"NA-US-FL-Orlando Eatonville Offsite -251 Rio Dr":["251 Rio Dr","Orlando","FL","32810",28.621,-81.429],"NA-US-FL-Orlando-1051 W Sand Lake Rd":["1051 SAND LAKE RD","Orlando","FL","",28.518,-81.307],"NA-US-FL-Orlando-14901 S Orange Blossom Trl":["14901 S Orange Blossom Trl","Orlando","FL","32837",28.395,-81.418],"NA-US-FL-Orlando-Eatonville":["100 S Lake Destiny Dr","Eatonville","FL","",null,null],"NA-US-FL-Orlando-John Young":["2214 John Young Pkwy","Orlando","FL","32804",28.575,-81.395],"NA-US-FL-Orlando-Lee Vista":["6855 Lee Vista Blvd","Orlando","FL","",28.518,-81.307],"NA-US-FL-Orlando-Lee Vista Blvd":["6855 Lee Vista Blvd","Orlando","FL","32822",28.494,-81.29],"NA-US-FL-Palm Bay-1206 Malabar Rd":["1206 Malabar Rd","Palm Bay","FL","32907",28.017,-80.674],"NA-US-FL-Pensacola":["312 E 9 Mile Rd","Pensacola","FL","",30.435,-87.252],"NA-US-FL-Pinellas Park Collision":["10280 US Hwy 19 N","Pinellas Park","FL","33782",27.868,-82.709],"NA-US-FL-Plant City-2402 W Baker St":["2402 W Baker St","Plant City","FL","33563",28.013,-82.134],"NA-US-FL-Port St Lucie-SW Fountainview Blvd":["1920 SW Fountainview Blvd","PORT ST LUCIE","FL","34986",27.322,-80.403],"NA-US-FL-Punta Gorda-101 Harborside Ave":["101 Harborside Ave","Punta Gorda","FL","",26.936,-82.001],"NA-US-FL-RR-Jacksonville-CSX":["5761 W 12th St","Jacksonville","FL","32254",30.341,-81.736],"NA-US-FL-RR-Jacksonville-NS":["7330 Old Kings Rd","Jacksonville","FL","32219",30.403,-81.763],"NA-US-FL-RR-Orlando-CSX":["1604 Pine Ave","Orlando","FL","32824",28.393,-81.362],"NA-US-FL-Riverview-9903 Alafia Preserve Ave":["9903 Alafia Preserve Ave","Riverview","FL","33578",27.863,-82.35],"NA-US-FL-Saint Petersburg":["4601 34TH ST N","Saint Petersburg","FL","",27.827,-82.7],"NA-US-FL-Sanford-4764 FL-46":["4764 FL-46","Sanford","FL","32771",28.801,-81.285],"NA-US-FL-Sarasota":["135 University Town Center","Sarasota","FL","",27.318,-82.499],"NA-US-FL-Sarasota-Lake Osprey Dr":["6231 Lake Osprey Dr","Sarasota","FL","34240",27.339,-82.347],"NA-US-FL-St. Petersburg":["Saint Petersburg","Saint Petersburg","FL","",27.827,-82.7],"NA-US-FL-St. Petersburg-":["Saint Petersburg","Saint Petersburg","FL","",27.827,-82.7],"NA-US-FL-TL5-HU-Adesa-Orlando-Sanford":["2851 St. Johns Parkway","Sanford","FL","32772",28.807,-81.25],"NA-US-FL-Tallahassee":["2412 W TENNESSEE ST","Tallahassee","FL","32304",30.448,-84.321],"NA-US-FL-Tamarac-6800 NW 88th Ave":["6800 NW 88th Ave","Tamarac","FL","33321",26.212,-80.27],"NA-US-FL-Tampa":["11945 North Florida Avenue","Tampa","FL","",27.943,-82.462],"NA-US-FL-Tampa Body Repair Center":["1500 E Busch Blvd","Tampa","FL","33612",28.05,-82.45],"NA-US-FL-Tampa-4636 N Dale Mabry Hwy":["4636 N Dale Mabry Hwy","Tampa","FL","",27.943,-82.462],"NA-US-FL-Tarpon Springs-39284-U.S. Hwy 19 N":["39284 US Hwy 19 N","Tarpon Springs","FL","34689",28.139,-82.743],"NA-US-FL-UVR-Fort Myers":["16180 LEE RD","Fort Myers","FL","",26.562,-81.853],"NA-US-FL-UVR-Kissimmee":["2935 N Orange Blossom Trail","Kissimmee","FL","34744",28.308,-81.368],"NA-US-FL-Wesley Chapel":["4980 Eagleston Blvd","Wesley Chapel","FL","",28.25,-82.315],"NA-US-FL-Wesley Chapel-4980 Eagleston Blvd - 425007":["4980 Eagleston Blvd","Wesley Chapel","FL","33544",28.24,-82.328],"NA-US-FL-West Palm Beach Collision":["655 N Military Trl","West Palm Beach","FL","",26.712,-80.097],"NA-US-FL-West Palm Beach-655 N. Military":["655 N Military Trl","West Palm Beach","FL","",26.712,-80.097],"NA-US-FL-West Palm Beach-6801 Southern Blvd":["6801 Southern Blvd","West Palm Beach","FL","",26.712,-80.097],"NA-US-Florida-Pinellas Park-10280 US Hwy 19 N":["10280 US Hwy 19 N","Pinellas Park","FL","",27.866,-82.716],"NA-US-GA-Alpharetta-Roswell":["1400 Upper Hembree Road","Roswell","GA","30076",34.021,-84.31],"NA-US-GA-Athens-156 Classic Rd":["156 Classic Rd","Bogart","GA","30622",33.934,-83.505],"NA-US-GA-Atlanta-100 Piedmont Ct NW":["100 PIEDMONT CT","Atlanta","GA","30340",33.893,-84.254],"NA-US-GA-Augusta-1080 Claussen Rd":["1080 Claussen Rd","Augusta","GA","30907",33.523,-82.085],"NA-US-GA-Augusta-3450 Wrightsboro Rd":["3450 Wrightsboro Rd","Augusta","GA","30909",33.472,-82.083],"NA-US-GA-Briarcliff":["2121 Briarcliff Rd NE","Atlanta","GA","30329",33.824,-84.321],"NA-US-GA-Columbus-1678 Whittlesey Rd":["1678 Whittlesey Rd","Columbus","GA","31904",32.516,-84.978],"NA-US-GA-Decatur":["1580 Church St.","Decatur","GA","",33.759,-84.274],"NA-US-GA-Duluth":["3380 Satellite Blvd","Duluth","GA","",33.991,-84.115],"NA-US-GA-Fayetteville-193 Walker Pkwy":["193 Walker Pkwy","Fayetteville","GA","",33.431,-84.477],"NA-US-GA-Kennesaw":["1875 Greers Chapel Rd NW","Kennesaw","GA","",34.016,-84.625],"NA-US-GA-Kingsland-110 Crown Pointe Pkwy":["110 Crown Pointe Pkwy","Kingsland","GA","",30.798,-81.707],"NA-US-GA-Marietta-":["2285 NW Pkwy SE","Marietta","GA","30067",33.928,-84.473],"NA-US-GA-Marietta-1165 Northchase Pkwy SE":["1165 Northchase Pkwy SE","Marietta","GA","30067",33.928,-84.473],"NA-US-GA-Savannah":["8805 Abercorn St","Savannah","GA","",32.018,-81.094],"NA-US-GA-TA1-Tesla Service Alpharetta-Roswell":["1400 Upper Hembree Road","Roswell","GA","30076",34.021,-84.31],"NA-US-GA-Tucker-2110 Tucker Industrial Rd":["2110 Tucker Industrial Rd","Tucker","GA","",33.856,-84.217],"NA-US-GA-Valdosta-3026 James Cir":["3026 James Cir","Valdosta","GA","31601",30.754,-83.332],"NA-US-GA-Warner Robins-4031 Watson Blvd":["4031 Watson Blvd","Warner Robins","GA","",32.596,-83.635],"NA-US-IA-Coralville-1220 1st Ave":["1220 1st Ave","Coralville","IA","",41.694,-91.591],"NA-US-IA-Council Bluffs-2421-Mid America Dr":["2421 Mid America Dr","Council Bluffs","IA","",41.252,-95.854],"NA-US-IA-Council Bluffs-2701 23rd Ave":["2701 23rd Ave","Council Bluffs","IA","51501",41.232,-95.875],"NA-US-IA-Des Moines-Urbandale-2601 104th":["2601 104th St","Urbandale","IA","",41.629,-93.736],"NA-US-ID-Meridian":["2554 W Franklin Rd","Meridian","ID","",43.626,-116.407],"NA-US-ID-Pocatello-1415 Bench Rd":["1415 Bench Rd","Pocatello","ID","83201",42.888,-112.438],"NA-US-IL-Batavia":["501 N Randall Rd","Batavia","IL","",41.848,-88.31],"NA-US-IL-Bloomington":["420 Olympia Dr","Bloomington","IL","",40.482,-88.947],"NA-US-IL-Buffalo Grove":["915 Dundee Rd","Buffalo Grove","IL","",42.16,-87.964],"NA-US-IL-Chicago-South Loop":["717 S DESPLAINES ST","Chicago","IL","",41.854,-87.676],"NA-US-IL-Collinsville-6 Gateway Dr":["6 Gateway Dr","Collinsville","IL","62234",38.684,-89.985],"NA-US-IL-Hub-Winfield":["704 W Washington St","West Chicago","IL","60185",41.889,-88.202],"NA-US-IL-Libertyville":["1121 S Milwaukee Ave","Libertyville","IL","",42.281,-87.95],"NA-US-IL-Lisle":["3200 Ogden Ave","Lisle","IL","",41.786,-88.088],"NA-US-IL-Mt. Vernon-Potomac Boulevard-Holiday Inn-Doubletree Hotel":["222 Potomac Blvd.","Mount Vernon","IL","62864",38.317,-88.91],"NA-US-IL-Northbrook":["1200 Skokie Blvd","Northbrook","IL","",42.126,-87.838],"NA-US-IL-Northbrook-Skokie":["1200 Skokie Blvd","Northbrook","IL","",42.126,-87.838],"NA-US-IL-Orland Park":["8601 W 159th st","Orland Park","IL","",41.611,-87.866],"NA-US-IL-RR-West Chicago-UP":["225 Kress Rd","Chicago","IL","60185",41.889,-88.202],"NA-US-IL-Schaumburg":["320 West Golf Road","Schaumburg","IL","",42.043,-88.087],"NA-US-IL-South Loop":["717 S DESPLAINES ST","Chicago","IL","",41.854,-87.676],"NA-US-IL-TA1-Chicago-Elston":["3067 N Elston Avenue","Chicago","IL","",41.854,-87.676],"NA-US-IL-TL5-HU-Adesa-Chicago-Hoffman Estates":["2785 Beverly Rd","Hoffman Estates","IL","60169",42.049,-88.106],"NA-US-IL-TempHub-Elgin":["70 Airport Rd","Elgin","IL","60123",42.038,-88.319],"NA-US-IL-UVR-Elk Grove Township-2010 E Higgins Rd":["2010 E Higgins Rd","Elk Grove Village","IL","",42.006,-87.982],"NA-US-IN-Bloomington-1710 N Kinser Pike":["1710 N Kinser Pike","Bloomington","IN","47404",39.195,-86.576],"NA-US-IN-Fort Wayne-818 Ave of Autos":["818 Ave of Autos","Fort Wayne","IN","46804",41.051,-85.256],"NA-US-IN-Indianapolis":["8280 Castleton Corner Drive","Indianapolis","IN","",39.8,-86.136],"NA-US-IN-Indianapolis-2608 Founders Sq Dr":["2608 Founders Sq Dr","Indianapolis","IN","46224",39.794,-86.271],"NA-US-IN-Indianapolis-Body Repair":["8841 Zionsville Rd","Indianapolis","IN","46268",39.868,-86.212],"NA-US-IN-Indianapolis-Castleton":["8280 Castleton Corner Drive","Indianapolis","IN","",39.8,-86.136],"NA-US-IN-Indianapolis-Greenwood":["5290 Claybrooke Cmns Dr","Indianapolis","IN","",39.8,-86.136],"NA-US-IN-Knight Township-800 N Green River Rd":["800 N Green River Rd","Evansville","IN","47715",37.968,-87.486],"NA-US-IN-Mishawaka-Grape Road":["6501 Grape Rd","Mishawaka","IN","46545",41.684,-86.168],"NA-US-IN-New Albany Township-4005 Earnings Way":["4005 Earnings Way","New Albany Township","IN","47150",38.309,-85.822],"NA-US-IN-Richmond-533 W Eaton Pike":["533 W Eaton Pike","Richmond","IN","47374",39.832,-84.894],"NA-US-IN-Terre Haute-4141 S US Hwy 41":["4141 S US Hwy 41","Terre Haute","IN","47802",39.407,-87.402],"NA-US-KS-Overland Park-10801 Mastin St":["10801 Mastin St","Overland Park","KS","",38.914,-94.729],"NA-US-KY-Berea-227 Paint Lick Rd":["227 Paint Lick Rd","Berea","KY","40403",37.58,-84.275],"NA-US-KY-Louisville":["11701 Gateworth Way","Louisville","KY","40299",38.177,-85.522],"NA-US-KY-Newport-":["5245 Ridge Ave","Cincinnati","OH","",39.171,-84.505],"NA-US-KY-Wilder-8 Hampton Ln":["8 Hampton Ln","Wilder","KY","41076",39.026,-84.441],"NA-US-LA-New Orleans - Service Lite":["2801 Tchoupitoulas Street","New Orleans","LA","",29.957,-90.07],"NA-US-MA-Berkley-15 Grove St":["15 Grove St","Berkley","MA","02779",41.835,-71.076],"NA-US-MA-Beverly-48 Dunham Rd":["48 Dunham Rdg Rd","Beverly","MA","",42.561,-70.876],"NA-US-MA-Boston-48 Industrial Drive":["48 INDUSTRIAL DR","Boston","MA","",42.352,-71.039],"NA-US-MA-Burlington-5-Wheeler Road":["5 Wheeler Rd","Burlington","MA","01803",42.509,-71.2],"NA-US-MA-Dedham-820 Boston Providence Hwy":["820 Boston Providence Hwy","Dedham","MA","",42.212,-71.126],"NA-US-MA-Marlborough-St. Martin Drive":["24 St Martin Dr, Building 2, Unit 11","Marlborough","MA","",42.351,-71.543],"NA-US-MA-Norwell":["98 Accord Park Dr","Norwell","MA","",42.16,-70.822],"NA-US-MA-Springfield":["365 Cadwell Dr","Springfield","MA","01104",42.129,-72.578],"NA-US-MA-Springfield-365 Cadwell Dr":["365 Cadwell Dr","Springfield","MA","01104",42.129,-72.578],"NA-US-MA-Walpole-295-Union-St-Other- Delivery":["295 Union St","East Walpole","MA","",42.153,-71.218],"NA-US-MD-Baltimore-Port of Baltimore":["1920 Frankfurst Ave","Baltimore","MD","21226",39.211,-76.56],"NA-US-MD-Grasonville-1020 Kent Narrows Rd":["1020 Kent Narrows Rd","Grasonville","MD","21638",38.946,-76.2],"NA-US-MD-Hub-Baltimore":["3410 Fairfield Rd","Baltimore","MD","21226",39.211,-76.56],"NA-US-MD-Linthicum Heights-1020 Andover Rd":["1020 Andover Rd","Linthicum Heights","MD","21090",39.209,-76.668],"NA-US-MD-Owings Mills Collision":["9800 Reisterstown Road","Owings Mills","MD","",39.427,-76.777],"NA-US-MD-Owings Mills-9750 Reisterstown Rd":["9750 Reisterstown Rd","Owings Mills","MD","",39.427,-76.777],"NA-US-MD-Owings Mills-9800 Reisterstown Rd":["9800 Reisterstown Road","Owings Mills","MD","",39.427,-76.777],"NA-US-MD-Prince Frederick-355 Merrimac Ct":["355 Merrimac Ct","Prince Frederick","MD","20678",38.534,-76.596],"NA-US-MD-Rockville":["1300 Rockville Pike","Rockville","MD","",39.087,-77.147],"NA-US-MD-Rockville-330 Hungerford Dr":["330 Hungerford Dr","Rockville","MD","20850",39.087,-77.168],"NA-US-MD-Silver Spring-Offsite":["13100 Columbia Pike","Silver Spring","MD","20904",39.067,-76.997],"NA-US-MD-UVR-Rockville Collision":["202 Mason Drive","Rockville","MD","20850",39.087,-77.168],"NA-US-MD-Waldorf-11770 Business Park":["11770 Business Park Dr","11770 Business Park Dr","WA","20601",38.637,-76.878],"NA-US-ME-South Portland-50-Maine Mall Road":["50 Maine Mall Rd","South Portland","ME","",43.637,-70.256],"NA-US-MI-Ann Arbor":["3530 Jackson Rd","Ann Arbor","MI","",42.265,-83.771],"NA-US-MI-Ann Arbor-3530 Jackson Rd":["3530 Jackson Rd","Ann Arbor","MI","48103",42.279,-83.784],"NA-US-MI-Clarkston-8105 Big Lake Rd":["8105 Big Lake Rd.","Clarkston","MI","",42.715,-83.404],"NA-US-MI-Detroit-Clarkston":["8105 Big Lake Rd","Clarkston","MI","48346",42.724,-83.423],"NA-US-MI-Grand Rapids":["2919 29th St SE","Grand Rapids","MI","49512",42.88,-85.535],"NA-US-MI-Grand Rapids-Mobile Service":["2919 29th St SE","Grand Rapids","MI","",42.98,-85.613],"NA-US-MI-Grandville-3675 Potomac Cir":["3675 Potomac Cir","Grandville","MI","49418",42.894,-85.762],"NA-US-MI-Holland-587 E 8th St":["587 E 8th St","Holland","MI","49423",42.769,-86.116],"NA-US-MI-Orion Township-4919 Interpark Dr":["4919 Interpark Dr","Orion Township","MI","48359",42.723,-83.277],"NA-US-MI-South Haven-04299 Cecilia Dr":["04299 Cecilia Dr","South Haven","MI","49090",42.404,-86.254],"NA-US-MI-Southfield":["24625 W 12 Mile Rd","Southfield","MI","",42.495,-83.231],"NA-US-MI-Stevensville-5050 Red Arrow Hwy":["5050 Red Arrow Hwy","Stevensville","MI","49127",42.022,-86.512],"NA-US-MI-TA1-Troy-Somerset":["2800 W. Big Beaver Road","Troy","MI","48084",42.563,-83.18],"NA-US-MI-West Bloomfield Township":["6800 Orchard Lake Road","West Bloomfield","MI","",42.592,-83.382],"NA-US-MN-Baxter-Lake Forest Road":["6967 Lake Forest Road","Baxter","MN","56401",46.35,-94.1],"NA-US-MN-Bloomington-Mall of America":["60 E Broadway","Bloomington","MN","55425",44.843,-93.236],"NA-US-MN-Brooklyn Park-":["9400 Decatur Dr N","Brooklyn Park","MN","",null,null],"NA-US-MN-Eagan-Rahncliff Ct":["1975 Rahncliff Ct","Eagan","MN","55122",44.786,-93.22],"NA-US-MN-Eden Prairie":["8001 Wallace Rd","Eden Prairie","MN","",44.856,-93.453],"NA-US-MN-Golden Valley":["700 Ottawa Ave N","Golden Valley","MN","",null,null],"NA-US-MN-Lake Elmo":["9800 Hudson Blvd N","Lake Elmo","MN","",44.995,-92.906],"NA-US-MN-Minneapolis-Eden Prairie Offsite":["6801 WASHINGTON AVE S","Minneapolis","MN","55439",44.874,-93.375],"NA-US-MN-Minneapolis-St. Paul":["2590 West Maplewood Drive","Maplewood","MN","",null,null],"NA-US-MN-Rochester-333 Apache Mall":["333 Apache Mall","Rochester","MN","55902",44.003,-92.484],"NA-US-MN-Rogers":["22015 S Diamond Lake Rd","Rogers","MN","",45.172,-93.581],"NA-US-MO-Cape Girardeau-601 Morgan Oak St":["601 Morgan Oak St","Cape Girardeau","MO","63703",37.306,-89.518],"NA-US-MO-Chesterfield-291 Chesterfield Center":["291 Chesterfield Center","Chesterfield","MO","63017",38.649,-90.536],"NA-US-MO-Kansas City-Body Repair":["15125 W 101st Terrace","Lenexa","KS","",38.954,-94.734],"NA-US-MO-South County":["5711 S LINDBERGH BLVD","Saint Louis","MO","",38.64,-90.286],"NA-US-MO-St. Louis-Mobile Service":["16955 Chesterfield Airport Road","Chesterfield","MO","63005",38.632,-90.614],"NA-US-MO-Stateline Road":["10111 State Line Rd","Kansas City","MO","64114",38.962,-94.596],"NA-US-MS-Brandon":["255 Mar-Lyn-Mr","Brandon","MS","",32.32,-89.97],"NA-US-MT-Bozeman-5 E Baxter Ln":["5 E Baxter Ln","Bozeman","MT","59715",45.669,-111.043],"NA-US-NC-Asheville-43-Town Square Blvd":["43 Town Square Blvd","Asheville","NC","28803",35.539,-82.518],"NA-US-NC-Charlotte-Body Repair":["1845 Sardis Rd N","Charlotte","NC","",35.229,-80.824],"NA-US-NC-Charlotte-Twin Lakes":["10615 TWIN LAKES PKWY","Charlotte","NC","",35.229,-80.824],"NA-US-NC-Fayetteville-1725 Jim Johnson Rd":["1725 Jim Johnson Rd","Fayetteville","NC","28312",34.955,-78.741],"NA-US-NC-Gastonia-444-Cox Road":["444 Cox Rd","Gastonia","NC","28054",35.249,-81.133],"NA-US-NC-Greensboro":["2620 N Main St","High Point","NC","",36.0,-79.998],"NA-US-NC-Greensboro-High Point":["2620 N Main St","High Point","NC","",36.0,-79.998],"NA-US-NC-Jacksonville-130 Workshop Ln":["130 Workshop Ln","Jacksonville","NC","28546",34.774,-77.378],"NA-US-NC-Matthews-Offsite-NortheastCT":["9508 Northeast Ct","Matthews","NC","",35.145,-80.735],"NA-US-NC-Morrisville-1021 Carrington Mill Blvd":["1021 Carrington Mill Blvd","Morrisville","NC","27560",35.834,-78.847],"NA-US-NC-Raleigh":["2641 Sumner Blvd","Raleigh","NC","",35.809,-78.634],"NA-US-NC-Raleigh-3950 Junction Blvd":["1700 Garner Station Blvd","Raleigh","NC","",35.809,-78.634],"NA-US-NC-Raleigh-Sumner Blvd":["2641 Sumner Blvd","Raleigh","NC","",35.809,-78.634],"NA-US-NC-Statesville-715 Sullivan Rd":["715 Sullivan Rd","Statesville","NC","28677",35.799,-80.894],"NA-US-ND-Fargo-1652-44th Street South":["1652 44th St S","Fargo","ND","58103",46.856,-96.812],"NA-US-NE-Grand Island-228 Lake St":["228 Lake St","Grand Island","NE","68801",40.922,-98.341],"NA-US-NE-Lincoln-6400 O St":["6400 O St","Lincoln","NE","68510",40.806,-96.654],"NA-US-NH-Epsom-910 Suncook Valley Hwy S":["910 Suncook Valley Hwy S","Epsom","NH","03234",43.217,-71.355],"NA-US-NH-Hampton-815 Lafayette Rd":["815 Lafayette Rd","Hampton","NH","03842",42.936,-70.824],"NA-US-NH-Keene-126-Key Rd":["126 Key Rd","Keene","NH","03431",42.963,-72.296],"NA-US-NH-Londonderry":["36 Industrial Dr","Londonderry","NH","03053",42.866,-71.377],"NA-US-NH-Manchester-860 S Porter St":["860 S Porter St","Manchester","NH","03103",42.966,-71.449],"NA-US-NJ--Brunswick Pike":["3320 Brunswick Pike","Lawrence Township","NJ","08648",40.217,-74.743],"NA-US-NJ-Cherry Hill-1840 Old Cuthbert Road":["1840 Old Cuthbert Road","Cherry Hill","NJ","08034",39.907,-75.001],"NA-US-NJ-Eatontown":["269 HIGHWAY 35","Eatontown","NJ","07724",40.303,-74.07],"NA-US-NJ-Englewood":["45 Cedar Ln","Englewood","NJ","",40.894,-73.977],"NA-US-NJ-Hub-Clifton Kingsland":["90 Kingsland Ave","Clifton","NJ","07014",40.834,-74.138],"NA-US-NJ-Manville-Adesa New Jersey":["200 N Main Street","Manville","NJ","08835",40.54,-74.593],"NA-US-NJ-Paramus-34 E Ridgewood Ave":["34 E Ridgewood Ave","Paramus","NJ","07652",40.948,-74.067],"NA-US-NJ-Parsippany-Troy Hills-1100 Edwards Rd":["1100 Edwards Rd","Parsippany-Troy Hills","NJ","07054",40.862,-74.412],"NA-US-NJ-Pine Brook-Chapin Road":["1 Chapin Road","Pine Brook","NJ","07085",40.874,-74.35],"NA-US-NJ-Princeton":["3371 BRUNSWICK PIKE","LAWRENCE TWP","NJ","08648",40.217,-74.743],"NA-US-NJ-TA1-Springfield-Kenilworth":["527 Springfield Rd","Kenilworth","NJ","",40.676,-74.294],"NA-US-NJ-TempHub-Mount Laurel":["538 Fellowship Rd","Mount Laurel Township","NJ","08054",39.948,-74.904],"NA-US-NJ-UVR-Cherry Hill-Body Repair":["2040 SPRINGDALE RD","Cherry Hill","NJ","08003",39.88,-74.971],"NA-US-NJ-UVR-Old Bridge":["1324 US-9","Old Bridge","NJ","",40.398,-74.324],"NA-US-NJ-UVR-Paramus Collision":["404 Sette Dr.","Paramus","NJ","07652",40.948,-74.067],"NA-US-NM-Albuquerque":["1300 Jemez Canyon Dam Rd","Bernalillo","NM","",35.328,-106.531],"NA-US-NM-Santa Fe":["17730 US-84 FRONTAGE","Santa Fe","NM","87506",35.819,-105.989],"NA-US-NV-Las Vegas-2121 E Sahara - offsite":["2121 E Sahara Ave","Las Vegas","NV","",36.16,-115.188],"NA-US-NV-Las Vegas-3338 E Fremont St":["3338 E Fremont St","Las Vegas","NV","89104",36.152,-115.109],"NA-US-NV-Las Vegas-Collision":["6215 Annie Oakley Dr","Las Vegas","NV","89120",36.091,-115.088],"NA-US-NV-Las Vegas-E Sahara - offsite":["2975 E Sahara Ave","Las Vegas","NV","",36.16,-115.188],"NA-US-NV-Las Vegas-East":["3250 E SAHARA AVE","Las Vegas","NV","89104",36.152,-115.109],"NA-US-NV-Las Vegas-Montessouri Street":["2555-2595 Montessouri Street","Las Vegas","NV","89117",36.13,-115.275],"NA-US-NV-Reno-1195 Corporate Blvd":["1195 Corporate Blvd","Reno","NV","",39.536,-119.815],"NA-US-NV-Reno-9390 Gateway Dr":["9390 Gateway Dr","Reno","NV","",39.536,-119.815],"NA-US-NV-Sparks-550 Milan Dr":["550 Milan Dr","Sparks","NV","",39.583,-119.727],"NA-US-NY White Plains":["250 Tarrytown Rd","White Plains","NY","",41.045,-73.769],"NA-US-NY--65 9th St":["65 9th St","Brooklyn","NY","11215",40.667,-73.983],"NA-US-NY-Beacon-11 Mirbeau Ln":["11 Mirbeau Ln","Beacon","NY","",41.51,-73.963],"NA-US-NY-Bedford Hills-34 Norm Ave":["34 Norm Ave","Bedford Hills","NY","",41.234,-73.692],"NA-US-NY-Bridgehampton-1 Bridgehampton-Sag Harbor Turnpike":["1 Bridgehampton-Sag Harbor Turnpike","Bridgehampton","NY","",40.934,-72.308],"NA-US-NY-Brooklyn-42 2nd Ave":["42 2nd Ave","Brooklyn","NY","",40.652,-73.955],"NA-US-NY-Buffalo":["1216 South Park Ave","Buffalo","NY","14220",42.844,-78.818],"NA-US-NY-Buffalo-1216 South Park Ave":["1216 South Park Ave","Buffalo","NY","14220",42.844,-78.818],"NA-US-NY-Flushing":["30-02 Whitestone Expressway","Queens","NY","",40.719,-73.744],"NA-US-NY-Flushing-30-02 Whitestone Expy-13974":["30-02 Whitestone Expressway","Queens","NY","11356",40.785,-73.845],"NA-US-NY-Garden City-630 Old Country Rd":["630 Old Country Rd Roosevelt Field Mall","Garden City","NY","11530",40.724,-73.649],"NA-US-NY-Garden City-North Ave":["1 North Ave","Garden City","NY","11530",40.724,-73.649],"NA-US-NY-Garden City-Old Country Road-Roosevelt Field":["630 Old Country Rd","Garden City","NY","11530",40.724,-73.649],"NA-US-NY-Long Island-Carle Place":["40 VOICE RD","Carle Place","NY","",40.751,-73.612],"NA-US-NY-Oneida":["5218 Patrick Rd","Verona","NY","13478",43.147,-75.572],"NA-US-NY-Rochester":["3535 W Henrietta Road","Rochester","NY","14623",43.083,-77.634],"NA-US-NY-Smithtown":["1000 Nesconset Hwy","Nesconset","NY","11767",40.846,-73.148],"NA-US-NY-Syracuse":["5427 N BURDICK ST","Fayetteville","NY","13066",43.027,-76.014],"NA-US-NY-TA1-Henrietta Service":["3535 W Henrietta Rd.","Rochester","NY","14623",43.083,-77.634],"NA-US-NY-Westbury":["1350 CORPORATE DR","Westbury","NY","11590",40.756,-73.572],"NA-US-NY-Westhampton Beach-7 Beach Ln":["7 Beach Ln","Westhampton Beach","NY","",40.83,-72.647],"NA-US-OH-Akron":["52 Springside Dr","Akron","OH","",41.079,-81.528],"NA-US-OH-Beavercreek-2677 Fairfield Cmns":["2677 Fairfield Cmns","Beavercreek","OH","45431",39.757,-84.057],"NA-US-OH-Cincinnati":["9111 Blue Ash Road","Blue Ash","OH","45242",39.245,-84.346],"NA-US-OH-Cincinnati-Oakley":["5245 Ridge Ave","Cincinnati","OH","",39.171,-84.505],"NA-US-OH-Cleveland":["5180 Mayfield Rd","Lyndhurst","OH","",null,null],"NA-US-OH-Columbus-Body Repair":["5600 Britton Pkwy","Dublin","OH","",40.104,-83.134],"NA-US-OH-Columbus-Offsite":["4249 Easton Way","Columbus","OH","",39.992,-82.992],"NA-US-OH-Cuyahoga Falls-Front St":["1989 Front St","Cuyahoga Falls","OH","",41.14,-81.491],"NA-US-OH-Dayton-Moraine":["1927 WEST DOROTHY LANE","Moraine","OH","45439",39.701,-84.219],"NA-US-OH-Elyria-645 Griswold Rd":["645 Griswold Rd","Elyria","OH","44035",41.372,-82.105],"NA-US-OH-Franklin Township-4400 William C Good Blvd":["4400 William C Good Blvd","Franklin Township","OH","45005",39.536,-84.303],"NA-US-OH-Howland Township-5555 Youngstown Warren Rd":["5555 Youngstown Warren Rd","Niles","OH","44446",41.182,-80.756],"NA-US-OH-Maumee-6425 Kit Ln":["6425 Kit Ln","Maumee","OH","",41.582,-83.663],"NA-US-OH-North Canton-Landmark Blvd":["5251 Landmark Blvd","North Canton","OH","44720",40.799,-81.378],"NA-US-OH-Tesla-Akron":["52 Springside Dr","Akron","OH","44333",41.155,-81.631],"NA-US-OK-Oklahoma City":["1125 N Broadway Ave","Oklahoma City","OK","",35.495,-97.489],"NA-US-OK-Tulsa":["6010 S 129TH EAST AVE","Tulsa","OK","",36.138,-95.97],"NA-US-OK-Tulsa Mobile Service":["6010 S 129TH EAST AVE","Tulsa","OK","74134",36.116,-95.823],"NA-US-OR-Portland-":["690 S Bancroft St","Portland","OR","",45.519,-122.664],"NA-US-OR-Portland-9800 SW Washington Square Rd":["9800 SW Washington Square Rd","Portland","OR","",45.519,-122.664],"NA-US-OR-TA-Bend-N Hwy 97":["63040 N HIGHWAY 97","Bend","OR","",44.028,-121.368],"NA-US-OR-TA-Portland-Tigard offsite":["9585 SW Washington Square Rd","Portland","OR","",45.519,-122.664],"NA-US-OR-TA-Salem-Mission":["2755 Mission Street","Salem","OR","",44.944,-123.009],"NA-US-PA-Devon":["470 W. Lancaster Avenue","Devon","PA","",40.045,-75.423],"NA-US-PA-Harrisburg":["6458 Carlisle Pike","Mechanicsburg","PA","",40.196,-77.015],"NA-US-PA-King of Prussia":["201 S Gulph Rd","King of Prussia","PA","",40.096,-75.374],"NA-US-PA-Langhorne-2021 Cabot Blvd W":["2021 Cabot Blvd W","Langhorne","PA","",40.176,-74.919],"NA-US-PA-Pittsburgh":["1400 Brockwell St","Bridgeville","PA","15017",40.347,-80.115],"NA-US-PA-TL5-HU-Manheim-Pennsylvania":["1190 Lancaster Rd","Manheim","PA","17545",40.17,-76.417],"NA-US-PA-Warminster":["700 York Rd","Warminster","PA","",40.268,-75.097],"NA-US-PA-West Chester":["1568 W CHESTER PIKE","West Chester","PA","",39.963,-75.6],"NA-US-PA-Whitehall":["955 Grape St","Whitehall","PA","",40.657,-75.504],"NA-US-RI-Providence":["77 Reservoir Ave","Providence","RI","02907",41.797,-71.425],"NA-US-SC-Bluffton-23 Towne Dr":["23 Towne Dr","Bluffton","SC","29910",32.251,-80.872],"NA-US-SC-Carolina Auto Auction, Inc":["140 Webb Rd","Williamston","SC","29697",34.621,-82.511],"NA-US-SC-Columbia-7340-Garners Ferry Road":["7340 Garners Ferry Rd","Columbia","SC","",34.016,-81.008],"NA-US-SC-Columbia-Collision":["6301 Two Notch Rd","Columbia","SC","29223",34.085,-80.917],"NA-US-SC-Greenville-Haywood Rd":["605 Haywood Rd","Greenville","SC","29607",34.828,-82.352],"NA-US-SC-Greenville-Market Point Dr":["31 Market Point Dr","Greenville","SC","29607",34.828,-82.352],"NA-US-SC-Mount Pleasant-1430 Midtown Ave":["1430 Midtown Ave","Mount Pleasant","SC","29464",32.847,-79.821],"NA-US-SC-Myrtle Beach-2000 Coastal Grand Cir":["2000 Coastal Grand Cir","Myrtle Beach","SC","29577",33.699,-78.914],"NA-US-SC-Piedmont-623 Cooper Rd":["623 Cooper Rd","Piedmont","SC","29673",34.724,-82.47],"NA-US-SC-Rock Hill-1285-Old Springdale Road":["1285 Old Springdale Rd","Rock Hill","SC","29730",34.915,-81.013],"NA-US-TN-Bartlett":["3020 N Germantown Rd","Bartlett","TN","",null,null],"NA-US-TN-Chattanooga":["2415 Elam Ln","Chattanooga","TN","",35.041,-85.284],"NA-US-TN-Clarksville-Mr. \"C\" Dr":["3020 Mr. \"C\" Dr","Clarksville","TN","37040",36.522,-87.349],"NA-US-TN-Cookeville-1025-Interstate Drive":["1025 Interstate Dr","Cookeville","TN","38501",36.218,-85.542],"NA-US-TN-Franklin-9009 Carothers Pkwy":["9009 Carothers Pkwy","Franklin","TN","37067",35.912,-86.766],"NA-US-TN-Jackson-1923 Emporium Dr":["1923 Emporium Dr","Jackson","TN","38305",35.683,-88.828],"NA-US-TN-Knoxville":["216 Montvue Rd","Knoxville","TN","",35.972,-83.965],"NA-US-TN-Kodak-2863 Winfield Dunn Pkwy":["2863 Winfield Dunn Pkwy","Sevierville","TN","37764",35.972,-83.617],"NA-US-TN-Murfreesboro-2435 S Church St":["2435 S Church St","Murfreesboro","TN","37127",35.763,-86.372],"NA-US-TN-Murfreesboro-909-North Thompson Lane Bldg A":["909 N Thompson Ln","Murfreesboro","TN","37129",35.871,-86.418],"NA-US-TN-Nashville":["122 Market Exchange CT","Franklin","TN","",35.925,-86.866],"NA-US-TN-Nashville-3451-Dickerson Pike":["3451 Dickerson Pike","Nashville","TN","37207",36.219,-86.774],"NA-US-TN-Nashville-500-Rep John Lewis Way South":["500 Rep. John Lewis Way S","Nashville","TN","37203",36.15,-86.792],"NA-US-TN-Nashville-Body Repair":["7256 CENTENNIAL PL","Nashville","TN","",36.166,-86.786],"NA-US-TN-Nashville-Mobile Service":["122 Market Exchange CT","Franklin","TN","37067",35.912,-86.766],"NA-US-TN-Smyrna-411 Potomac Pl":["411 Potomac Pl","Smyrna","TN","",35.966,-86.505],"NA-US-TN-TA1-Nashville-Brentwood":["1641 WESTGATE CIR","Brentwood","TN","37027",36.006,-86.791],"NA-US-TX-Alvarado-1165 US-67":["1165 US-67","Alvarado","TX","76009",32.44,-97.213],"NA-US-TX-Amarillo-W Amarillo Blvd2":["8330 W Amarillo Blvd","Amarillo","TX","",35.253,-101.866],"NA-US-TX-Austin-2323 Ridgepoint Dr":["2323 Ridgepoint Dr","Austin","TX","78754",30.342,-97.667],"NA-US-TX-Austin-405 E St Elmo Rd.":["405 E St Elmo Rd","Austin","TX","78745",30.206,-97.796],"NA-US-TX-Austin-500 E St Elmo Rd":["500 E SAINT ELMO RD","Austin","TX","78745",30.206,-97.796],"NA-US-TX-Austin-6320-E Stassney Ln":["6320 E Stassney Ln","Austin","TX","78744",30.188,-97.747],"NA-US-TX-Austin-7010 State Hwy 71":["7010 State Hwy 71","Austin","TX","78735",30.249,-97.841],"NA-US-TX-Austin-7104 McNeil Dr OFFSITE":["12845 Research Blvd","Austin","TX","",30.309,-97.762],"NA-US-TX-Austin-Direct Delivery":["1 TESLA RD.","Austin","TX","",30.309,-97.762],"NA-US-TX-Austin-TX-3520 Wadley Place":["3520 Wadley Place","Austin","TX","78728",30.442,-97.681],"NA-US-TX-Austin-TX-Body Repair":["3520 WADLEY PL","Austin","TX","",30.309,-97.762],"NA-US-TX-Austin-Tesla Inc. 5900 E Ben White Blvd Suite A 120":["5900 E Ben White Blvd a120","Austin","TX","78741",30.232,-97.722],"NA-US-TX-Baytown-7211-Garth Rd.":["7211 Garth Rd","Baytown","TX","77521",29.77,-94.969],"NA-US-TX-Beaumont-6155 Eastex Fwy":["6155 Eastex Fwy","Beaumont","TX","77706",30.095,-94.165],"NA-US-TX-Brazoria-21155 TX-36":["21155 TX-36","BRAZORIA","TX","77422",29.024,-95.587],"NA-US-TX-Brownsville":["7045 Old Highway 77","Olmito","TX","",26.035,-97.55],"NA-US-TX-Brownsville-2370 North Expy":["2370 North Expy","Brownsville","TX","78521",25.922,-97.461],"NA-US-TX-Brownsville-Mobile Service":["7045 Old Highway 77","Olmito","TX","78575",26.035,-97.55],"NA-US-TX-Cedar Springs-Body Repair (NOT 6500)":["6519 CEDAR SPRINGS ROAD","Dallas","TX","",32.789,-96.787],"NA-US-TX-College Station-University Dr E":["950 University Dr E","College Station","TX","77840",30.605,-96.312],"NA-US-TX-Corpus Christi-S Padre Island Dr":["5488 S Padre Island Dr","Corpus Christi","TX","78411",27.731,-97.388],"NA-US-TX-Dallas Walnut Hill-Irving-4450 W Walnut Hill Ln":["4450 WEST WALNUT HILL LANE","Irving","TX","",32.85,-96.934],"NA-US-TX-Dallas-1501 N Walton Walker Blvd - Robotaxi":["1501 N Walton Walker Blvd","1501 N Walton Walker Blvd","DA","",null,null],"NA-US-TX-Dallas-6114 Forest Park Rd":["6114 Forest Park Rd","Dallas","TX","",32.789,-96.787],"NA-US-TX-Dallas-Adesa Hutchins":["3501 Lancaster-Hutchins Rd","Dallas","TX","75141",32.64,-96.707],"NA-US-TX-Dallas-Cedar Springs Road":["6500 Cedar Springs Road","Dallas","TX","",32.789,-96.787],"NA-US-TX-Dallas-Decommission-Pick-n-Pull":["5301 S Second Ave","Dallas","TX","",32.789,-96.787],"NA-US-TX-Edinburg-502 W Trenton Rd":["502 W Trenton Rd","Edinburg","TX","78539",26.279,-98.183],"NA-US-TX-El Paso":["7825 Helen of Troy Dr","El Paso","TX","",31.722,-106.343],"NA-US-TX-Farmers Branch-Training":["13725 Welch Road","Farmers Branch","TX","",null,null],"NA-US-TX-Flower Mound":["1805 JUSTIN RD","Flower Mound","TX","",33.091,-97.103],"NA-US-TX-Fort Worth":["5812 North Freeway","Fort Worth","TX","",32.76,-97.315],"NA-US-TX-Fort Worth-15853 N Fwy":["15853 N Fwy","Fort Worth","TX","76177",32.945,-97.312],"NA-US-TX-Fort Worth-Gallery":["15853 N Fwy","Fort Worth","TX","76177",32.945,-97.312],"NA-US-TX-Fort Worth-North Freeway":["5812 North Freeway","Fort Worth","TX","",32.76,-97.315],"NA-US-TX-Fredericksburg-808 S Adams St":["808 S Adams St","Fredericksburg","TX","",30.282,-98.88],"NA-US-TX-Grand Prairie-2123 I-20":["2123 I-20","Grand Prairie","TX","75052",32.66,-97.031],"NA-US-TX-Grapevine-3000 Grapevine Mills Parkway":["3000 Grapevine Mills Pkwy","Grapevine","TX","76051",32.933,-97.081],"NA-US-TX-Houston North-Body Repair Center":["14520 Wagg Way Rd","Houston","TX","",29.798,-95.419],"NA-US-TX-Houston-11560 FM 1960":["11560 FM 1960","Houston","TX","77065",29.932,-95.611],"NA-US-TX-Houston-14520 Wagg Way Rd":["14520 Wagg Way Rd","Houston","TX","77041",29.86,-95.582],"NA-US-TX-Houston-454-Fallbrook Dr":["454 Fallbrook Dr","Houston","TX","77038",29.92,-95.439],"NA-US-TX-Houston-Westchase-Westheimer":["9633 Westheimer Rd","Houston","TX","",29.798,-95.419],"NA-US-TX-Humble-17900 US-59":["17900 US-59","Humble","TX","77396",29.951,-95.262],"NA-US-TX-Jersey Village-11900 FM 529":["11900 FM 529","Jersey Village","TX","",null,null],"NA-US-TX-Katy-21010 Katy Fwy":["21010 Katy Fwy","Katy","TX","",29.793,-95.796],"NA-US-TX-Killeen-2100 S W S Young Dr":["2100 S W S Young Dr","Killeen","TX","76543",31.117,-97.665],"NA-US-TX-Kyle-201 Logistics Dr (Kyle 1 - Cerebrum)":["201 Logistics Dr","Kyle","TX","",29.997,-97.834],"NA-US-TX-Lake Jackson-100 TX-332":["100 TX-332","Lake Jackson","TX","77566",29.039,-95.44],"NA-US-TX-Laredo-8218 Casa Verde Rd":["8218 Casa Verde Rd","Laredo","TX","78041",27.557,-99.491],"NA-US-TX-League City":["400 GULF FWY S","League City","TX","",29.514,-95.077],"NA-US-TX-Lubbock":["6544 82ND ST","Lubbock","TX","",33.574,-101.871],"NA-US-TX-New Braunfels-201 TX-337 Loop":["201 TX-337 Loop","New Braunfels","TX","78130",29.723,-98.074],"NA-US-TX-New Caney-22118 Market Pl Dr":["22118 Market Pl Dr","New Caney","TX","77357",30.158,-95.198],"NA-US-TX-Pearland-13931 South Fwy":["13931 South Fwy","Houston","TX","77047",29.625,-95.375],"NA-US-TX-Plano-2701 Premier Dr":["2701 Premier Dr","Plano","TX","",33.038,-96.719],"NA-US-TX-Plano-300 Lexington Dr":["300 LEXINGTON DR","Plano","TX","75075",33.025,-96.74],"NA-US-TX-Plano-5340 Legacy Dr":["5340 Legacy Dr","Plano","TX","75024",33.075,-96.784],"NA-US-TX-Plano-Democracy Drive":["5800 DEMOCRACY DR 100","Plano","TX","75024",33.075,-96.784],"NA-US-TX-RR-Mesquite-UP":["9211 Forney Rd","Dallas","TX","75227",32.767,-96.684],"NA-US-TX-RR-RCR Taylor":["201 FM3349","Taylor","TX","",30.571,-97.409],"NA-US-TX-Richmond":["21555 Southwest Fwy","Houston","TX","77099",29.671,-95.587],"NA-US-TX-Richmond-21555 Southwest Fwy":["21555 Southwest Fwy","Houston","TX","77099",29.671,-95.587],"NA-US-TX-Rockport-609 Hwy 35 N":["609 Hwy 35 N","Rockport","TX","78382",28.031,-97.069],"NA-US-TX-Rosenberg-26706 Southwest Fwy":["26706 SOUTHWEST FWY","Rosenberg","TX","77471",29.55,-95.798],"NA-US-TX-San Antonio-7159 San Pedro Ave":["7159 San Pedro Ave","San Antonio","TX","",29.465,-98.498],"NA-US-TX-San Antonio-Central":["8320-8434 Airport Blvd","San Antonio","TX","78216",29.533,-98.498],"NA-US-TX-San Antonio-Repair":["10954 Laureate Dr","San Antonio","TX","",29.465,-98.498],"NA-US-TX-San Marcos-625 Commercial Lp":["625 Commercial Lp","San Marcos","TX","78666",29.875,-97.94],"NA-US-TX-Sanger-600 N Stemmons St":["600 N Stemmons St","Sanger","TX","",33.356,-97.181],"NA-US-TX-TI0-Tesla Service San Antonio-Dominion":["23011 IH-10 West","San Antonio","TX","",29.465,-98.498],"NA-US-TX-TL5-HU-Manheim-Dallas Forth Worth-Euless":["12101 Trinity Blvd","Euless","TX","76040",32.826,-97.097],"NA-US-TX-Temple-1415 N General Bruce Dr":["1415 N General Bruce Dr","Temple","TX","",31.069,-97.38],"NA-US-TX-Tesla Inc. Austin-2323 Ridgepoint Dr":["2323 RIDGEPOINT DR","Austin","TX","",30.309,-97.762],"NA-US-TX-The Woodlands":["9420 College Park Dr","The Woodlands","TX","77384",30.226,-95.492],"NA-US-TX-Trophy Club-96 Trophy Club Dr":["96 Trophy Club Dr","Trophy Club","TX","76262",33.021,-97.213],"NA-US-TX-Tyler":["3408 S SW Loop 323","Tyler","TX","",32.369,-95.289],"NA-US-TX-Waco-320 S 8th St":["320 S 8th St","Waco","TX","76701",31.552,-97.14],"NA-US-TX-Waco-Mobile Service":["3801 Campus Dr","Waco","TX","",31.555,-97.163],"NA-US-UT-Pleasant Grove-2100 W Pleasant Grove Blvd":["2100 W Pleasant Grove Blvd","American Fork","UT","",40.393,-111.794],"NA-US-UT-Price-Westwood Boulevard":["925 Westwood Blvd.","Price","UT","",39.602,-110.808],"NA-US-UT-Riverdale":["4851 S 1500 W","Riverdale","UT","",null,null],"NA-US-UT-Riverdale-4851 S 1500 W":["4851 S 1500 W","Riverdale","UT","84405",41.174,-111.981],"NA-US-UT-Salt Lake City-50 S Main St":["50 S Main St","Salt Lake City","UT","",40.71,-111.889],"NA-US-UT-St. George-S 270 E":["1644 S 270 E","St. George","UT","84010",40.877,-111.873],"NA-US-UT-TA-Salt Lake City":["2312 S. State Street","South Salt Lake City","UT","84115",40.715,-111.893],"NA-US-UT-UVR-Salt Lake City Collision":["3530 W 2100 S","Salt Lake City","UT","84119",40.692,-112.001],"NA-US-VA-Arlington":["2710 S GLEBE RD","Arlington","VA","",38.874,-77.098],"NA-US-VA-Charlottesville":["1951 Swanson Dr","Charlottesville","VA","22901",38.094,-78.561],"NA-US-VA-Charlottesville-1951 Swanson Drive":["1951 Swanson Dr","Charlottesville","VA","",38.047,-78.482],"NA-US-VA-McLean-8401 Westpark Dr":["8401 Westpark Dr","McLean","VA","",null,null],"NA-US-VA-Norfolk":["7520 N Military Hwy","Norfolk","VA","",36.897,-76.258],"NA-US-VA-Richmond-2810 N Parham Rd":["2810 N Parham Rd","Richmond","VA","",37.528,-77.474],"NA-US-VA-Richmond-4401 S Laburnum Ave":["4401 S Laburnum Ave","Richmond","VA","23231",37.464,-77.398],"NA-US-VA-Roanoke-4802 Valley View Blvd NW":["4802 Valley View Blvd NW","Roanoke","VA","24012",37.303,-79.932],"NA-US-VA-Sterling":["22400 Davis Dr","Sterling","VA","",39.013,-77.423],"NA-US-VA-Sterling-ADESA Washington":["43375 Old Ox Rd","Sterling","VA","20166",38.981,-77.472],"NA-US-VA-TA1-Sterling-22400 Davis":["22400 Davis Dr","Sterling","VA","",39.013,-77.423],"NA-US-VA-Yorktown-511 Commonwealth Dr":["511 Commonwealth Dr","Yorktown","VA","",37.194,-76.5],"NA-US-VT-Burlington":["218 Hannafords Dr","South Burlington","VT","",44.447,-73.131],"NA-US-WA-Arlington-Stoluckquamish Ln":["3438 Stoluckquamish Ln","Arlington","WA","98223",48.183,-122.112],"NA-US-WA-Bellevue-14475 NE 24th St":["14475 NE 24th St","Bellevue","WA","",47.606,-122.17],"NA-US-WA-Bellevue-Body Repair Center":["1762 133RD PL NE","Bellevue","WA","",47.606,-122.17],"NA-US-WA-Fife-4700 Steelhead St E":["4700 Steelhead St E","Fife","WA","",null,null],"NA-US-WA-Kennewick-3715-Plaza Way":["3715 Plaza Way","Kennewick","WA","",46.183,-119.19],"NA-US-WA-Liberty Lake":["1805 N Pepper Ln","Liberty Lake","WA","",47.652,-117.084],"NA-US-WA-Liberty Lake-1805 N Pepper Ln":["1805 N Pepper Ln","Liberty Lake","WA","",47.652,-117.084],"NA-US-WA-Lynnwood-17731 Pacific Hwy":["17731 Pacific Hwy","Lynnwood","WA","98037",47.839,-122.285],"NA-US-WA-Lynnwood-44th ave":["20800 44th Ave W","Lynnwood","WA","",47.832,-122.285],"NA-US-WA-Olympia-415-Capitol Way North":["415 Capitol Way N","Olympia","WA","98501",47.013,-122.876],"NA-US-WA-Renton":["600 SW 10th St","Renton","WA","",47.479,-122.169],"NA-US-WA-Renton-600 SW 10th St":["600 SW 10th St","Renton","WA","",47.479,-122.169],"NA-US-WA-Renton-Offsite":["600 SW 10th St","Renton","WA","",47.479,-122.169],"NA-US-WA-Seattle-4901 Airport Way S":["655 S Edmunds St","Seattle","WA","",47.604,-122.331],"NA-US-WA-Sequim-East Washington Street-Holiday Inn Express":["1441 E Washington Street","Sequim","WA","",48.088,-123.12],"NA-US-WA-TA-Tesla Service Bellevue":["14408 NE 20th Street","Bellevue","WA","98424",47.233,-122.359],"NA-US-WA-Tacoma-5950 N 9th St":["5950 N 9th St","Tacoma","WA","",47.226,-122.454],"NA-US-WA-Vancouver-2711 NE Andresen Rd":["2711 NE Andresen Rd","Vancouver","WA","",45.656,-122.617],"NA-US-WI-Eau Claire-3614 Gateway Dr":["3614 Gateway Dr","Eau Claire","WI","54701",44.784,-91.488],"NA-US-WI-Holmen-3928 Circle Dr":["3928 Circle Dr","Holmen","WI","",43.976,-91.25],"NA-US-WI-Lake Geneva-7036 Grand Geneva Way":["7036 Grand Geneva Way","Lake Geneva","WI","53147",42.588,-88.455],"NA-US-WI-Madison-6624 Seybold Road":["6624 Seybold Rd","Madison","WI","53719",43.032,-89.499],"NA-US-WI-Milwaukee":["12011 W Silver Spring Dr","Milwaukee","WI","",43.05,-87.953],"NA-US-WV-Charleston-100 Kanawha Blvd E":["100 Kanawha Blvd E","CHARLESTON","WV","25301",38.349,-81.631],"NA-US-WV-Morgantown - MVD":["9500 Mall Rd","Morgantown","WV","26501",39.61,-79.983],"NA-US-WY-Casper-300 W F St":["300 W F St","Casper","WY","82601",42.846,-106.317],"NA-US-WY-Cheyenne-1628 W Lincolnway":["1628 W Lincolnway","Cheyenne","WY","82001",41.144,-104.796],"NABID SALEHIN nowshin.sultana18@gmail.com 8647223630":["3245 E UNIVERSITY AVE","LAS CRUCES","NM","",32.38,-106.769],"NASER UWEIS nuweis1@msn.com 5059791191":["504 DEFIANCE AVE","GALLUP","NM","",35.495,-108.752],"NATALIA PONOMARENKO pono.natalia@gmail.com 6039660877":["731 BRANCH RD","WELLS","ME","",43.314,-70.597],"NEIL CONNOLE neilconnole@gmail.com +14064317424":["485 S PARK AVE","HELENA","MT","",46.62,-112.017],"NICHOLAS BRADISH NICK@JRSCDIGITAL.COM 7166403941":["1611 FOOTE AVE","JAMESTOWN","NY","",42.095,-79.24],"National Vehicle and Fuel Emissions Laboratory (U.S. EPA)":["2565 Plymouth Road","Ann Arbor","MI","48105",42.304,-83.707],"Orlando Southridge Park Ops Warehouse":["9424 SOUTHRIDGE PARK CT Suite 800","Orlando","FL","32819",28.452,-81.468],"Orlando-Eatonville":["100 S Lake Destiny Dr","Eatonville","FL","32751",28.625,-81.365],"P.J. McMahon pmcmahon@thebancorp.com 6104761435":["548 N TROOPER RD","WEST NORRITON","PA","",null,null],"PAUL MILLSAP mmillsap@tesla.com 4792095888":["8422 TIMBER RIDGE RD","OZARK","AR","",35.525,-93.837],"PENROSE ODONNELL rosiestovell@hotmail.com 8082260607":["314 LITTLEJOHN RD","YARMOUTH","ME","",43.801,-70.175],"PETER ANASTOS peter.anastos@mchg.com 2079392520":["56 SPRUCE POINT RD","YARMOUTH","ME","",43.801,-70.175],"PETER COHEN petercohen64@gmail.com 2076531969":["3 SILVA DRIVE","CAPE ELIZABETH","ME","",43.602,-70.23],"PHOUTHONG THAMMAVONGSA kipt1@yahoo.com 8156708133":["1899 EL SEGUNDO TRAIL","LAS CRUCES","NM","",32.38,-106.769],"Paul Flora paul.m.flora@em.com 3333333333":["2006 S 146th Street","Seatac","WA","",null,null],"Penrose Odonnell":["314 Littlejohn Rd","Yarmouth","ME","04096",43.801,-70.175],"Petaluma Energy Ops WH":["1362 North McDowell Boulevard, Suite A & B","Petaluma","CA","",38.243,-122.644],"Phoenix Energy Logistics":["425 E Pinnacle Peak Rd","Phoenix","AZ","",33.492,-112.079],"Pine Brook Energy FV":["1 Chapin Rd","Pine Brook","NJ","",40.874,-74.35],"Pittsburgh Energy Service Storage":["14010 Perry Hwy","Wexford","PA","15090",40.612,-80.065],"RENEE FREID reneefreid@yahoo.com 2078374756":["709 CHANDLERS WHARF","PORTLAND","ME","",43.666,-70.257],"RICHARD FARR":["6810 BRIGHT VIEW RD","Las Cruces","NM","88007",32.322,-106.804],"RICHARD FARR ibdbanker@gmail.com 5753397299":["6810 BRIGHT VIEW RD","LAS CRUCES","NM","",32.38,-106.769],"RICHARD THOMPSON richardpalmerthompson@gmail.com +12076502618":["19 CHANNEL RD","SOUTH PORTLAND","ME","",43.637,-70.256],"ROBERT EMERY remery352@gmail.com 2074758700":["352 HALEY RD","KITTERY","ME","",43.092,-70.743],"ROBERT GIPSON rggipson17@gmail.com 5019442319":["23 WOODBERRY RD","LITTLE ROCK","AR","",34.759,-92.345],"ROBERT PAYNE Rpayne2000@yahoo.com 6502452131":["943 N 32ND ST","BILLINGS","MT","",45.797,-108.516],"ROBERT ST-GERMAIN bobst5941@gmail.com 9062392337":["908 N MILWAUKEE AVE","IRON MOUNTAIN","MI","",45.822,-88.068],"ROD IVERSON rodiverson@icloud.com 7017977365":["9831 8TH ST NE","BINFORD","ND","",47.574,-98.355],"RUTH RAMIREZ rjramirez95502@gmail.com 7076163202":["2100 FOXWOOD DR","EUREKA","CA","",40.783,-124.163],"RYAN SMART ryan.smart@yahoo.com 8572252047":["509 LIBERTY LN","HORACE","ND","",46.71,-96.885],"Ramon Reynoso ramon.reynoso1@astrazeneca.com 9172258442":["556A MACDONOUGH ST","BROOKLYN","NY","",40.652,-73.955],"Richard Neugebauer rneugebauer@elecnorhawkeyellc.com 5165094120":["831 Southard Street","Baldwin","NY","",40.655,-73.61],"Robert Emery":["352 Haley Rd","Kittery","ME","03905",43.098,-70.712],"Robert Peele robert.peele@astrazeneca.com 2514636174":["33092 STEELWOOD RIDGE RD","LOXLEY","AL","",30.618,-87.756],"Roush Industries":["12011 Market Street","Livonia","MI","48150",42.361,-83.365],"Run Mozealous stella@mozealous.com 3035706965":["52 HANOVER ST","PORTLAND","ME","",43.666,-70.257],"S1912 Adesa Orlando":["2851 St Johns Parkway","Sanford","FL","32771",28.801,-81.285],"SAMUEL DYK samueld@ruachresources.com 7014211977":["14136 PETROLEUM PK DR","WILLISTON","ND","",48.18,-103.628],"SCOTT SCHNEIDER weskanrentals@gmail.com 7856393869":["3415 COUNTRY LN","HAYS","KS","",38.878,-99.335],"SEAN GRAVES seanmichaelgraves@gmail.com +14068608056":["3040 HOLLOW TREE RD","BILLINGS","MT","",45.797,-108.516],"STEPHEN HUGHES smhbc2001@gmail.com 5019447754":["3817 E TOWNSHIP ST","FAYETTEVILLE","AR","",36.075,-94.198],"STEPHEN LORGE stephenlorge@gmail.com 5018319118":["4007 LAKESHORE DR","NORTH LITTLE ROCK","AR","",34.786,-92.286],"STEVEN BEIGHT ssbeight@me.com 5018129022":["1601 N HARRISON ST","LITTLE ROCK","AR","",34.759,-92.345],"STEVEN FAUGHT taylorfaught@gmail.com 4795495051":["22 W PINNACLE DR","ROGERS","AR","",36.328,-94.129],"STEVEN TIMMERMANS steve@sjctgroup.com 4795495600":["5756 DORNOCH DR","FAYETTEVILLE","AR","",36.075,-94.198],"SUNTRAX Test Facility Toll Operations":["100 Transformation Wy","Auburndale","FL","",28.072,-81.812],"SUSAN YOACHIM ryoachim@cox.net 6204413440":["2116 EASTRIDGE DR","ARKANSAS CITY","KS","",37.068,-97.036],"Salt Lake City-South 300 West":["1038 South 300 West","Salt Lake City","UT","",40.71,-111.889],"San Diego-1240 Morena Blvd":["1240 W Morena Blvd","San Diego","CA","92110",32.764,-117.203],"Sandra Webb sgwebb1955@gmail.com 4793664776":["30 W OXFORD DR","ROGERS","AR","",36.328,-94.129],"Sean Graves":["3040 Hollow Tree Rd","Billings","MT","59101",45.775,-108.501],"Serena Warren serena.warren@novartis.com 6103342934":["5 HASKELL DR","LANCASTER","PA","",40.041,-76.309],"Seth Nettles seth.nettles@novartis.com 9198154225":["11192 BAYBERRY HILLS DR","RALEIGH","NC","",35.809,-78.634],"Shane Olson olsonfarms@ndsupernet.com 7012603231":["11089 ND-200","KILLDEER","ND","",47.411,-102.776],"South Tucson Energy Ops WH":["2901 E ELVIRA RD Suite 135","Tucson","AZ","",32.225,-110.945],"Stephanie Broome stephanie.broome@novartis.com 2295634841":["4213 WHISPERWOOD CIR","VALDOSTA","GA","",30.842,-83.27],"Steve LeWarne steve.lewarne@astrazeneca.com 5153182950":["12883 CLARK ST","CLIVE","IA","",41.607,-93.746],"Sugarland Energy Ops WH":["7256 S Sam Houston Pkwy W Suite 200","Houston","TX","77085",29.622,-95.482],"Suntrax - Tesla - attn: Paul Rubert":["100 Transformation Way","Auburndale","FL","",28.072,-81.812],"Susan Yordt susan.yordt@novartis.com 4026510478":["14858 JAYNES ST","OMAHA","NE","",41.256,-96.006],"TARA PEEL tara0602@hotmail.com 8477789020":["2389 CLARKS POINT DR","LAUREL","MT","",45.675,-108.769],"TESLA - DIETRICH GABASA":["1150 W ARBOR AVE STE 101","Tracy","CA","",37.715,-121.462],"THOMAS NEWHALL thomasnewhall@gmail.com 2078388357":["3 BIGELOW WAY","CAPE ELIZABETH","ME","",43.602,-70.23],"TITAN FAN titan@beaconkits.com 2077303227":["49 MINOTT SHORE RD","BRUNSWICK","ME","",43.897,-69.978],"TRACY RHODES rhodes_tracy@hotmail.com 5014128008":["2 CHELLE COVE","LITTLE ROCK","AR","",34.759,-92.345],"TROY CRUTCHFIELD tcrutchfield4@gmail.com 5055507207":["1647 AGUA DULCE DR SE","RIO RANCHO","NM","",35.206,-106.688],"Tampa Energy Ops WH":["5012 Joanne Kearney Blvd.","Tampa","FL","33619",27.938,-82.376],"Tesla":["3022 KENWOOD ST","Burbank","CA","",34.181,-118.313],"Tesla - Houston - 111 Empire Blvd Building 9":["111 Empire Blvd","Pattison","TX","",29.817,-96.007],"Tesla - New Windsor, NY Ops WH":["15 Tarkett Dr","New Windsor","NY","12553",41.472,-74.057],"Tesla BR Aliso Viejo":["41 COLUMBIA","Aliso Viejo","CA","",33.619,-117.774],"Tesla BR Oceanside":["1825 CORPORATE CENTER","Oceanside","CA","",33.204,-117.352],"Tesla BR Tempe":["7015 S Harl Ave","Tempe","AZ","85283",33.367,-111.931],"Tesla Collision Arlington":["6000 Coping Ln","Arlington","TX","",32.72,-97.16],"Tesla Collision Central Houston":["6010 Richmond Ave","Houston","TX","",29.798,-95.419],"Tesla Collision Fallbrook Houston":["454 Fallbrook Dr","Houston","TX","",29.798,-95.419],"Tesla Collision Fort Myers":["16180 LEE RD","Fort Myers","FL","",26.562,-81.853],"Tesla Collision Fort Worth":["3901 N Sylvania Ave","Fort Worth","TX","",32.76,-97.315],"Tesla Collision Plano":["7040 Plano Pkwy","Plano","TX","75093",33.03,-96.789],"Tesla Collision Tampa":["4913 W Knox St.","Tampa","FL","",27.943,-82.462],"Tesla Energy - Lancaster":["1170 Garfield Avenue, Gateway Business Center #2","Lancaster","PA","",40.041,-76.309],"Tesla Inc .Deer Creek, Palo Alto":["3500 Deer Creek Rd","Palo Alto","CA","",37.441,-122.148],"Tesla Inc .Palo Alto-Deer Creek-Hanover":["1501 PAGE MILL RD","Palo Alto","CA","",37.441,-122.148],"Tesla Inc Austin Body Shop":["6320 E Stassney Ln (Bldg 4)","Austin","TX","",30.309,-97.762],"Tesla Inc California-Palo Alto-Deer Creek Rd.":["3500 Deer Creek Rd.","Palo Alto","CA","",37.441,-122.148],"Tesla Inc Gigafactory Texas":["1 TESLA RD.","Austin","TX","78725",30.256,-97.624],"Tesla Inc Henderson-2445 St Rose Pkwy":["2445 SAINT ROSE PKWY","Henderson","NV","89074",36.038,-115.086],"Tesla Inc NA-US-CA-Fremont-47700 Kato Road":["47700 Kato Road","Fremont","CA","94538",37.531,-121.971],"Tesla Inc NA-US-CA-Tesla Palo Alto-3000 Hanover St":["3000 Hanover St","Palo Alto","CA","",37.441,-122.148],"Tesla Inc, Fremont-48401 Fremont Blvd-Lakeview":["48401 Fremont Blvd","Fremont","CA","",37.573,-121.974],"Tesla Inc. Design Studio - Hawthorne":["3203 JACK NORTHROP AVE","Hawthorne","CA","",33.915,-118.351],"Tesla Inc. Fremont Engineering Hub":["47623 Fremont Boulevard","Fremont","CA","94538",37.531,-121.971],"Tesla Inc. Kato Road - Tesla Engineering":["47400, Kato Road","Fremont","CA","",37.573,-121.974],"Tesla Inc. NA-US-CA-Palo Alto-Deer Creek-Office":["3500 Deer Creek Rd","Palo Alto","CA","",37.441,-122.148],"Tesla Inc. Page Mill Rd-Vehicle Charging Lab":["1501 PAGE MILL RD","Palo Alto","CA","94304",37.433,-122.184],"Tesla Inc. Palo Alto - 1501 Page Mill Rd":["1501 PAGE MILL RD","Palo Alto","CA","",37.441,-122.148],"Tesla Inc. Sparks-Electric Avenue":["1 Electric Ave","Sparks","NV","",39.583,-119.727],"Tesla Plano Collision":["7040 W Plano Pkwy","Plano","TX","",33.038,-96.719],"Tesla SC Alhambra":["1200 W Main St","Alhambra","CA","91801",34.091,-118.129],"Tesla SC Centinela LA":["5840 W Centinela Avenue","Los Angeles","CA","90045",33.963,-118.394],"Tesla SC Santa Ana":["3240 South Standard Ave","Santa Ana","CA","92705",33.754,-117.792],"Tesla SC West Sta Monica-LA":["11163 Santa Monica Blvd","Los Angeles","CA","90025",34.045,-118.449],"Tesla Service Chico-Huss":["349 Huss Drive","Chico","CA","95928",39.722,-121.811],"Tesla Service Fort Worth":["5812 North Freeway","Fort Worth","TX","",32.76,-97.315],"Tesla Service Long Island-Syosset":["7 Aerial Way","Syosset","NY","11791",40.815,-73.502],"Tesla Service North Little Rock":["5045 Warden Rd","North Little Rock","AR","",34.786,-92.286],"Tesla Service West Austin":["7010 State Hwy 71","Austin","TX","",30.309,-97.762],"Tesla Service- Briarcliff":["2121 Briarcliff Rd NE","Atlanta","GA","30329",33.824,-84.321],"Tesla Service-Lite Corpus Christi":["3605 S Padre Island Dr","Corpus Christi","TX","78415",27.726,-97.408],"Tesla Service-Lite Latham":["326 Old Niskayuna Road","Latham","NY","12110",42.746,-73.763],"Tesla Transportation Research Center":["10820 Ohio Hwy 347","East Liberty","OH","",40.308,-83.586],"Tesla inc. NA-US-CA-Fremont-47400 Kato Road":["47400 Kato Road","Fremont","CA","",37.573,-121.974],"Tesla-14010 Perry Hwy , Wexford, PA, US, 15090":["14010 Perry Hwy","Marshall Township","PA","15090",40.612,-80.065],"Tesla-Dallas":["4450 W Walnut Hill Ln STE 150","Irving","TX","75038",32.865,-96.99],"Tigard - Washington Square (Portland)":["9681 SW Washington Square Rd.","Portland","OR","97223",45.44,-122.779],"Todd Carroll todd.carroll@novartis.com 4043099245":["2051 Moores Mill Rd. Unit D","AUBURN","AL","",32.583,-85.489],"Todd Lynch eric.lynch@astrazeneca.com 7042415502":["10621 MIDWAY PARK DR","CHARLOTTE","NC","",35.229,-80.824],"Tolleson-Manheim Phoenix":["201 N 83rd Ave","Tolleson","AZ","85353",33.435,-112.277],"Travis Templeton travis.templeton@astrazeneca.com 5637770202":["110 1/2 N RIVERVIEW ST","BELLEVUE","IA","",42.258,-90.436],"US-CA-North Hollywood-13005 Sherman Way-13005 Sherman Way , North Hollywood, CA, US, 91605":["13005 Sherman Way","Los Angeles","CA","91605",34.206,-118.4],"Ursus Garcia Ursus.Garcia@dmv.ca.gov 9168098959":["2415 1st Ave","Sacramento","CA","",38.581,-121.474],"VIRGINIA ALFARO virginia.alfaro@norvartis.com 7326162981":["2164 CHAPEL CT","TOMS RIVER","NJ","",39.947,-74.214],"WILLIAM DRAGSETH drag21960@gmail.com 9162231019":["904 FIRENZE","MCCLOUD","CA","",41.248,-122.113],"WILLIAM TERRELL":["5701 HAYMEADOW RDG","Hastings","NE","68901",40.588,-98.391],"WILLIAM TERRELL billyterrell1@gmail.com 3046381880":["5701 HAYMEADOW RDG","HASTINGS","NE","",40.589,-98.394],"West Palm Beach-Manheim Palm Beach":["600 Sansbury's Way","West Palm Beach","FL","33411",26.664,-80.174],"William Mackison mackey0929@hotmail.com 5133175029":["543 LEXINGTON AVE","NEWPORT","KY","",39.003,-84.414],"XINLEI KANG":["63 RICHARD RD","Sidney","ME","04330",44.323,-69.766],"XINLEI KANG xinlei88@gmail.com 2153854588":["63 RICHARD RD","SIDNEY","ME","",null,null],"YEN PHAM phamthuhaiyen@gmail.com 6579006528":["6200 WATKINS AVE # J107","SPRINGDALE","AR","",36.179,-94.125],"Yvette Gomez Yvette.MoraGomez@edd.ca.gov 9162343141":["1733 Sports Drive","Sacramento","CA","",38.581,-121.474],"Zach Diamond zdiamond@diamondtechnicalservices.com 7248401253":["9152 ROUTE 22","BLAIRSVILLE","PA","",40.441,-79.253],"brian dunbar BRIAN.DUNBAR@SJWATER.COM 4083097228":["1923 W Winton Ave","Hayward","CA","",37.662,-122.032],"brian dunbar brian.dunbar@sjwater.com 4083097228":["1923 W Winton Ave","Hayward","CA","",37.662,-122.032],"no driver no driver noemail@email.com 5555555555":["965 FIR ST","CHICO","CA","",39.758,-121.856]};
  // tender-stop key 'street|city|state' (normalised) -> SD venues it lands at  (16 aliases, manual + learned)
  const OP_ALIASES = {"100 transformation wy|auburndale|FL":[["100 Transformation Way","Auburndale","FL","33823"]],"1051 sand lake rd|orlando|FL":[["1051 W Sand Lake Rd","Orlando","FL","32809"]],"14901 s orange blossom trl|orlando|FL":[["2935 N Orange Blossom Trail","Kissimmee","FL","34744"]],"1501 n walton walker blvd|dallas|TX":[["6114 Forest Park Rd","Dallas","TX","75235"]],"1951 swanson dr|charlottesville|VA":[["1951 Swanson Dr 130","Charlottesville","VA","22901"]],"2214 john young pkwy|orlando|FL":[["2214-N. John Young Pkwy","Orlando","FL","32804"]],"2415 1st ave|sacramento|CA":[["2415 1st Ave Docks","Sacramento","CA","95818"]],"2535 arden wy|sacramento|CA":[["2535 Arden Way","Sacramento","CA","95825"]],"2800 w big beaver rd|troy|MI":[["2800 W. Big Beaver Road Space #N-114","Troy","MI","48084"]],"3307 hillview ave|palo alto|CA":[["1501 Page Mill Rd","Palo Alto","CA","94304"]],"400 automall dr|gilroy|CA":[["500 Automall Dr","Gilroy","CA","95020"]],"5290 claybrooke cmns dr|indianapolis|IN":[["5290 Claybrooke Cmns DrSC","Indianapolis","IN","46237"]],"5900 e ben white blvd a120|austin|TX":[["5900 E Ben White Blvd","Austin","TX","78741"]],"630 old country rd roosevelt field mall|garden|NY":[["1350 CORPORATE DR","Westbury","NY","11590"]],"800 mary louis ln|kissimmee|FL":[["2935 N Orange Blossom Trail","Kissimmee","FL","34744"]],"9009 carothers pkwy|franklin|TN":[["122 Market Exchange CT","Franklin","TN","37067"]]};
  // 'ST|city' (normalised) -> [lat, lon] centroid  (553 cities)
  const OP_CITIES = {"AL|auburn":[32.583,-85.489],"AL|birmingham":[33.516,-86.844],"AL|loxley":[30.618,-87.756],"AL|wetumpka":[32.577,-86.157],"AR|bentonville":[36.358,-94.222],"AR|fayetteville":[36.099,-94.172],"AR|hackett":[35.194,-94.398],"AR|hot springs":[34.66,-92.991],"AR|little rock":[34.759,-92.345],"AR|n little rock":[34.786,-92.286],"AR|ozark":[35.525,-93.837],"AR|rogers":[36.317,-94.154],"AR|springdale":[36.179,-94.125],"AZ|arizona":[32.756,-111.671],"AZ|gilbert":[33.316,-111.754],"AZ|glendale":[33.529,-112.248],"AZ|litchfield park":[33.51,-112.413],"AZ|mesa":[33.378,-111.641],"AZ|phoenix":[33.686,-111.996],"AZ|scottsdale":[33.574,-111.888],"AZ|tempe":[33.442,-111.924],"AZ|tolleson":[33.435,-112.277],"AZ|tucson":[32.139,-110.945],"CA|alhambra":[34.091,-118.129],"CA|aliso viejo":[33.619,-117.774],"CA|anaheim":[33.854,-117.786],"CA|aptos":[36.979,-121.894],"CA|bakersfield":[36.753,-119.706],"CA|berkeley":[37.87,-122.296],"CA|bishop":[37.432,-118.4],"CA|buena park":[33.856,-118.001],"CA|burbank":[34.181,-118.313],"CA|burlingame":[37.567,-122.368],"CA|camarillo":[34.223,-119.024],"CA|cathedral":[33.795,-116.466],"CA|centinela":[33.963,-118.394],"CA|chico":[39.722,-121.811],"CA|colma":[37.348,-121.887],"CA|corte madera":[37.924,-122.52],"CA|costa mesa":[33.656,-117.913],"CA|covina":[34.092,-117.882],"CA|crescent":[41.782,-124.133],"CA|culver":[34.013,-118.397],"CA|davis":[38.545,-121.74],"CA|dublin":[37.717,-121.923],"CA|emeryville":[37.837,-122.28],"CA|etna":[41.446,-123.01],"CA|eureka":[40.794,-124.157],"CA|fremont":[37.518,-121.929],"CA|fresno":[36.753,-119.706],"CA|gilroy":[37.012,-121.574],"CA|hawthorne":[33.914,-118.349],"CA|hayward":[37.674,-122.089],"CA|huntington beach":[33.692,-118.001],"CA|irvine":[33.695,-117.822],"CA|jurupa valley":[33.994,-117.524],"CA|kneeland":[40.641,-123.883],"CA|lathrop":[37.821,-121.283],"CA|long beach":[33.788,-118.195],"CA|los angeles":[34.034,-118.281],"CA|los gatos":[37.373,-121.856],"CA|mcclellan park":[38.662,-121.395],"CA|mccloud":[41.248,-122.113],"CA|mckinleyville":[40.947,-124.083],"CA|milpitas":[37.43,-121.9],"CA|mira loma":[33.994,-117.524],"CA|montebello":[34.013,-118.113],"CA|n hollywood":[34.206,-118.4],"CA|national":[32.677,-117.094],"CA|norwalk":[33.903,-118.082],"CA|oceanside":[33.204,-117.352],"CA|ontario":[34.057,-117.64],"CA|palo alto":[37.441,-122.148],"CA|petaluma":[38.251,-122.615],"CA|pleasanton":[37.677,-121.886],"CA|port hueneme":[34.163,-119.197],"CA|rancho cordova":[38.598,-121.264],"CA|redding":[40.549,-122.334],"CA|richmond":[37.936,-122.344],"CA|riverside":[33.976,-117.339],"CA|rocklin":[38.801,-121.252],"CA|s san francisco":[37.657,-122.424],"CA|sacramento":[38.589,-121.406],"CA|san bernardino":[34.083,-117.271],"CA|san diego":[32.755,-117.147],"CA|san jose":[37.409,-121.941],"CA|san luis obispo":[35.282,-120.633],"CA|san mateo":[37.539,-122.3],"CA|san rafael":[38.005,-122.544],"CA|santa ana":[33.754,-117.792],"CA|santa barbara":[34.426,-119.724],"CA|santa clarita":[34.415,-118.531],"CA|santa monica":[34.018,-118.491],"CA|santa rosa":[38.482,-122.747],"CA|seaside":[36.622,-121.793],"CA|stockton":[38.032,-121.259],"CA|sunnyvale":[37.35,-122.035],"CA|temecula":[33.49,-117.182],"CA|thousand oaks":[34.192,-118.845],"CA|torrance":[33.84,-118.354],"CA|tracy":[37.715,-121.462],"CA|ukiah":[39.155,-123.195],"CA|upland":[34.123,-117.658],"CA|vallejo":[38.158,-122.28],"CA|visalia":[36.331,-119.296],"CA|vista":[33.194,-117.239],"CO|aurora":[39.709,-104.706],"CO|colorado springs":[38.838,-104.837],"CO|cortez":[37.355,-108.584],"CO|denver":[39.739,-104.982],"CO|durango":[37.226,-107.878],"CO|englewood":[39.581,-104.901],"CO|fountain":[38.7,-104.701],"CO|gateway":[38.678,-108.972],"CO|gypsum":[39.662,-106.967],"CO|henderson":[39.898,-104.872],"CO|littleton":[39.611,-104.953],"CO|longmont":[40.131,-104.95],"CO|loveland":[40.426,-105.09],"CO|superior":[39.979,-105.146],"CT|bantam":[41.721,-73.252],"CT|branford":[41.28,-72.811],"CT|e granby":[41.932,-72.746],"CT|milford":[41.218,-73.055],"CT|old saybrook":[41.291,-72.385],"CT|rocky hill":[41.658,-72.663],"CT|stamford":[41.06,-73.544],"CT|uncasville":[41.462,-72.113],"CT|w hartford":[41.733,-72.734],"DE|wilmington":[39.717,-75.618],"FL|altamonte springs":[28.663,-81.412],"FL|auburndale":[28.072,-81.812],"FL|clermont":[28.552,-81.757],"FL|coral gables":[25.721,-80.273],"FL|crestview":[30.764,-86.592],"FL|daytona beach":[29.196,-81.033],"FL|deland":[29.042,-81.286],"FL|delray beach":[26.455,-80.066],"FL|destin":[30.395,-86.469],"FL|eatonville":[28.625,-81.365],"FL|ft lauderdale":[26.128,-80.212],"FL|ft myers":[26.647,-81.843],"FL|ft walton beach":[30.421,-86.629],"FL|gainesville":[29.701,-82.308],"FL|homestead":[25.488,-80.469],"FL|jacksonville":[30.351,-81.506],"FL|jupiter":[26.939,-80.123],"FL|kissimmee":[28.308,-81.368],"FL|lakeland":[28.072,-81.961],"FL|melbourne":[28.069,-80.62],"FL|merritt island":[28.401,-80.686],"FL|miami":[25.769,-80.259],"FL|miami gardens":[25.942,-80.246],"FL|naples":[26.174,-81.729],"FL|ocala":[29.157,-82.21],"FL|opa locka":[25.91,-80.247],"FL|orlando":[28.518,-81.307],"FL|palm bay":[28.017,-80.674],"FL|pensacola":[30.435,-87.252],"FL|pinellas park":[27.868,-82.709],"FL|plant":[28.013,-82.134],"FL|port st lucie":[27.322,-80.403],"FL|punta gorda":[26.936,-82.001],"FL|riverview":[27.863,-82.35],"FL|sanford":[28.801,-81.285],"FL|sarasota":[27.318,-82.499],"FL|st petersburg":[27.827,-82.7],"FL|tallahassee":[30.448,-84.321],"FL|tamarac":[26.212,-80.27],"FL|tampa":[27.938,-82.376],"FL|tarpon springs":[28.139,-82.743],"FL|w palm beach":[26.664,-80.174],"FL|wesley chapel":[28.25,-82.315],"GA|atlanta":[33.817,-84.38],"GA|augusta":[33.523,-82.085],"GA|bogart":[33.934,-83.505],"GA|columbus":[32.516,-84.978],"GA|decatur":[33.759,-84.274],"GA|duluth":[33.991,-84.115],"GA|fayetteville":[33.431,-84.477],"GA|kennesaw":[34.016,-84.625],"GA|kingsland":[30.798,-81.707],"GA|marietta":[33.928,-84.473],"GA|roswell":[34.021,-84.31],"GA|savannah":[32.018,-81.094],"GA|tucker":[33.856,-84.217],"GA|valdosta":[30.754,-83.332],"GA|warner robins":[32.596,-83.635],"IA|bellevue":[42.258,-90.436],"IA|clive":[41.607,-93.746],"IA|coralville":[41.694,-91.591],"IA|council bluffs":[41.252,-95.854],"IA|urbandale":[41.629,-93.736],"ID|meridian":[43.626,-116.407],"ID|pocatello":[42.888,-112.438],"IL|batavia":[41.848,-88.31],"IL|bloomington":[40.482,-88.947],"IL|buffalo grove":[42.16,-87.964],"IL|buncombe":[37.464,-88.981],"IL|chicago":[41.854,-87.676],"IL|collinsville":[38.684,-89.985],"IL|elgin":[42.038,-88.319],"IL|elk grove":[42.006,-87.982],"IL|glen carbon":[38.761,-89.971],"IL|hoffman estates":[42.043,-88.08],"IL|libertyville":[42.281,-87.95],"IL|lisle":[41.786,-88.088],"IL|mt vernon":[38.317,-88.91],"IL|northbrook":[42.126,-87.838],"IL|orland park":[41.611,-87.866],"IL|schaumburg":[42.043,-88.087],"IL|w chicago":[41.889,-88.202],"IN|bloomington":[39.195,-86.576],"IN|evansville":[37.996,-87.57],"IN|ft wayne":[41.051,-85.256],"IN|indianapolis":[39.8,-86.136],"IN|mishawaka":[41.684,-86.168],"IN|new albany":[38.309,-85.822],"IN|richmond":[39.832,-84.894],"IN|terre haute":[39.407,-87.402],"KS|arkansas":[37.068,-97.036],"KS|hays":[38.878,-99.335],"KS|hutchinson":[38.041,-97.97],"KS|lenexa":[38.954,-94.734],"KS|overland park":[38.914,-94.729],"KY|berea":[37.58,-84.275],"KY|covington":[39.071,-84.521],"KY|erlanger":[39.034,-84.614],"KY|louisville":[38.208,-85.696],"KY|newport":[39.003,-84.414],"KY|simpsonville":[38.231,-85.355],"KY|wilder":[39.026,-84.441],"LA|new orleans":[29.957,-90.07],"MA|berkley":[41.835,-71.076],"MA|beverly":[42.561,-70.876],"MA|boston":[42.352,-71.039],"MA|burlington":[42.509,-71.2],"MA|dedham":[42.212,-71.126],"MA|e walpole":[42.153,-71.218],"MA|marlborough":[42.351,-71.543],"MA|norwell":[42.16,-70.822],"MA|springfield":[42.129,-72.578],"MD|baltimore":[39.211,-76.56],"MD|beltsville":[39.04,-76.916],"MD|grasonville":[38.946,-76.2],"MD|linthicum heights":[39.209,-76.668],"MD|owings mills":[39.427,-76.777],"MD|prince frederick":[38.534,-76.596],"MD|rockville":[39.087,-77.147],"MD|silver spring":[39.067,-76.997],"ME|bar harbor":[44.374,-68.245],"ME|biddeford":[43.493,-70.488],"ME|brunswick":[43.897,-69.978],"ME|cape elizabeth":[43.602,-70.23],"ME|cumberland":[43.797,-70.265],"ME|ellsworth":[44.555,-68.412],"ME|falmouth":[43.734,-70.263],"ME|freeport":[43.857,-70.103],"ME|harpswell":[43.781,-69.996],"ME|hudson":[44.991,-68.888],"ME|kennebunk":[43.388,-70.548],"ME|kittery":[43.092,-70.743],"ME|mt desert":[44.294,-68.285],"ME|northeast harbor":[44.294,-68.285],"ME|oakland":[44.517,-69.74],"ME|ogunquit":[43.254,-70.609],"ME|portland":[43.666,-70.257],"ME|s portland":[43.637,-70.256],"ME|saco":[43.521,-70.455],"ME|sidney":[44.323,-69.766],"ME|wells":[43.314,-70.597],"ME|windham":[43.796,-70.414],"ME|wiscasset":[44.007,-69.683],"ME|yarmouth":[43.801,-70.175],"MI|ann arbor":[42.265,-83.771],"MI|brooklyn":[42.104,-84.241],"MI|clarkston":[42.715,-83.404],"MI|grand rapids":[42.88,-85.535],"MI|grandville":[42.894,-85.762],"MI|gwinn":[46.331,-87.44],"MI|holland":[42.769,-86.116],"MI|iron mountain":[45.822,-88.068],"MI|livonia":[42.361,-83.365],"MI|orion":[42.723,-83.277],"MI|rock":[46.05,-87.133],"MI|s haven":[42.404,-86.254],"MI|southfield":[42.495,-83.231],"MI|stevensville":[42.022,-86.512],"MI|taylor":[42.232,-83.267],"MI|troy":[42.563,-83.18],"MI|w bloomfield":[42.592,-83.382],"MN|baxter":[46.35,-94.1],"MN|bloomington":[44.843,-93.236],"MN|eagan":[44.786,-93.22],"MN|eden prairie":[44.856,-93.453],"MN|lake elmo":[44.995,-92.906],"MN|minneapolis":[44.874,-93.375],"MN|rochester":[44.003,-92.484],"MN|rogers":[45.172,-93.581],"MO|cape girardeau":[37.306,-89.518],"MO|chesterfield":[38.649,-90.536],"MO|kansas":[38.962,-94.596],"MO|springfield":[37.216,-93.303],"MO|st louis":[38.64,-90.286],"MS|brandon":[32.32,-89.97],"MT|bigfork":[48.063,-114.073],"MT|billings":[45.775,-108.501],"MT|bozeman":[45.659,-111.046],"MT|helena":[46.62,-112.017],"MT|laurel":[45.675,-108.769],"NC|asheville":[35.539,-82.518],"NC|cary":[35.781,-78.815],"NC|charlotte":[35.229,-80.824],"NC|denver":[35.484,-80.99],"NC|fayetteville":[34.955,-78.741],"NC|gastonia":[35.249,-81.133],"NC|high point":[36.0,-79.998],"NC|huntersville":[35.406,-80.856],"NC|jacksonville":[34.774,-77.378],"NC|kernersville":[36.118,-80.078],"NC|matthews":[35.145,-80.735],"NC|morrisville":[35.834,-78.847],"NC|raleigh":[35.809,-78.634],"NC|statesville":[35.799,-80.894],"ND|binford":[47.574,-98.355],"ND|fargo":[46.856,-96.812],"ND|horace":[46.71,-96.885],"ND|killdeer":[47.411,-102.776],"ND|williston":[48.18,-103.628],"NE|grand island":[40.922,-98.341],"NE|hastings":[40.588,-98.391],"NE|kearney":[40.75,-99.088],"NE|lincoln":[40.818,-96.689],"NE|omaha":[41.256,-96.006],"NH|epsom":[43.217,-71.355],"NH|hampton":[42.936,-70.824],"NH|keene":[42.963,-72.296],"NH|londonderry":[42.866,-71.377],"NH|manchester":[42.966,-71.449],"NJ|blackwood":[39.79,-75.037],"NJ|cherry hill":[39.907,-75.001],"NJ|clifton":[40.834,-74.138],"NJ|eatontown":[40.303,-74.07],"NJ|englewood":[40.894,-73.977],"NJ|kenilworth":[40.676,-74.294],"NJ|lawrence":[40.217,-74.743],"NJ|manville":[40.54,-74.593],"NJ|mt ephraim":[39.883,-75.093],"NJ|mt laurel":[39.948,-74.904],"NJ|old bridge":[40.398,-74.324],"NJ|paramus":[40.948,-74.067],"NJ|parsippany troy hills":[40.862,-74.412],"NJ|pine brook":[40.874,-74.35],"NJ|toms river":[39.947,-74.214],"NJ|washington":[40.758,-74.991],"NM|bernalillo":[35.328,-106.531],"NM|carlsbad":[32.377,-104.267],"NM|clovis":[34.409,-103.213],"NM|deming":[32.232,-107.747],"NM|gallup":[35.495,-108.752],"NM|hobbs":[32.746,-103.162],"NM|las cruces":[32.38,-106.769],"NM|lovington":[32.951,-103.349],"NM|rio rancho":[35.206,-106.688],"NM|santa fe":[35.819,-105.989],"NM|santa teresa":[31.839,-106.682],"NM|white sands missile range":[32.384,-106.494],"NV|elko":[40.857,-115.687],"NV|henderson":[36.038,-115.086],"NV|las vegas":[36.216,-115.067],"NV|reno":[39.536,-119.815],"NV|sparks":[39.583,-119.727],"NV|w wendover":[40.739,-114.073],"NY|baldwin":[40.655,-73.61],"NY|beacon":[41.51,-73.963],"NY|bedford hills":[41.234,-73.692],"NY|bemus point":[42.151,-79.358],"NY|bethpage":[40.74,-73.486],"NY|bridgehampton":[40.934,-72.308],"NY|brooklyn":[40.652,-73.955],"NY|buffalo":[42.844,-78.818],"NY|carle place":[40.751,-73.612],"NY|clarence":[42.981,-78.616],"NY|fayetteville":[43.027,-76.014],"NY|garden":[40.724,-73.649],"NY|jamestown":[42.095,-79.24],"NY|latham":[42.746,-73.763],"NY|nesconset":[40.846,-73.148],"NY|new windsor":[41.472,-74.057],"NY|queens":[40.719,-73.744],"NY|rochester":[43.083,-77.634],"NY|syosset":[40.815,-73.502],"NY|verona":[43.147,-75.572],"NY|westbury":[40.756,-73.572],"NY|westfield":[42.322,-79.573],"NY|westhampton beach":[40.83,-72.647],"NY|white plains":[41.045,-73.769],"OH|akron":[41.079,-81.528],"OH|beavercreek":[39.757,-84.057],"OH|blue ash":[39.245,-84.346],"OH|cincinnati":[39.171,-84.505],"OH|columbus":[39.992,-82.992],"OH|cuyahoga falls":[41.14,-81.491],"OH|dublin":[40.104,-83.134],"OH|e liberty":[40.308,-83.586],"OH|elyria":[41.372,-82.105],"OH|franklin":[39.536,-84.303],"OH|maumee":[41.582,-83.663],"OH|moraine":[39.701,-84.219],"OH|n canton":[40.799,-81.378],"OH|niles":[41.182,-80.756],"OH|perrysburg":[41.55,-83.61],"OH|struthers":[41.051,-80.599],"OH|toledo":[41.676,-83.531],"OK|oklahoma":[35.495,-97.489],"OK|tahlequah":[35.905,-95.009],"OK|tulsa":[36.138,-95.97],"OR|bend":[44.028,-121.368],"OR|portland":[45.519,-122.664],"OR|salem":[44.944,-123.009],"PA|blairsville":[40.441,-79.253],"PA|bridgeville":[40.347,-80.115],"PA|devon":[40.045,-75.423],"PA|king of prussia":[40.096,-75.374],"PA|lancaster":[40.077,-76.311],"PA|langhorne":[40.176,-74.919],"PA|manheim":[40.17,-76.417],"PA|marshall":[40.612,-80.065],"PA|mechanicsburg":[40.196,-77.015],"PA|philadelphia":[39.991,-75.143],"PA|pittsburgh":[40.441,-80.004],"PA|w chester":[39.963,-75.6],"PA|warminster":[40.268,-75.097],"PA|warrington":[40.246,-75.135],"PA|wayne":[40.021,-75.395],"PA|wexford":[40.612,-80.065],"PA|whitehall":[40.657,-75.504],"RI|providence":[41.797,-71.425],"SC|bluffton":[32.251,-80.872],"SC|columbia":[34.016,-81.008],"SC|conway":[33.873,-79.056],"SC|greenville":[34.855,-82.412],"SC|mt pleasant":[32.836,-79.829],"SC|myrtle beach":[33.699,-78.914],"SC|piedmont":[34.724,-82.47],"SC|rock hill":[34.915,-81.013],"SC|summerville":[33.006,-80.19],"SC|williamston":[34.621,-82.511],"SD|hot springs":[43.422,-103.477],"SD|n sioux":[42.525,-96.507],"SD|pierre":[44.37,-100.321],"SD|sioux falls":[43.59,-96.751],"SD|tripp":[43.24,-97.971],"TN|brentwood":[36.006,-86.791],"TN|chattanooga":[35.041,-85.284],"TN|clarksville":[36.522,-87.349],"TN|cookeville":[36.218,-85.542],"TN|franklin":[35.912,-86.766],"TN|jackson":[35.683,-88.828],"TN|knoxville":[35.972,-83.965],"TN|murfreesboro":[35.763,-86.372],"TN|nashville":[36.219,-86.774],"TN|sevierville":[35.972,-83.617],"TN|smyrna":[35.966,-86.505],"TX|alvarado":[32.44,-97.213],"TX|amarillo":[35.253,-101.866],"TX|arlington":[32.72,-97.16],"TX|austin":[30.342,-97.667],"TX|baytown":[29.77,-94.969],"TX|beaumont":[30.095,-94.165],"TX|brazoria":[29.024,-95.587],"TX|brownsville":[25.922,-97.461],"TX|college station":[30.605,-96.312],"TX|corpus christi":[27.731,-97.388],"TX|dallas":[32.789,-96.787],"TX|denton":[33.205,-97.12],"TX|edinburg":[26.279,-98.183],"TX|el paso":[31.722,-106.343],"TX|euless":[32.826,-97.097],"TX|flower mound":[33.091,-97.103],"TX|fredericksburg":[30.282,-98.88],"TX|ft worth":[32.76,-97.315],"TX|grand prairie":[32.66,-97.031],"TX|grapevine":[32.933,-97.081],"TX|houston":[29.798,-95.419],"TX|humble":[29.951,-95.262],"TX|hutchins":[32.64,-96.707],"TX|irving":[32.85,-96.934],"TX|katy":[29.793,-95.796],"TX|killeen":[31.117,-97.665],"TX|kyle":[29.997,-97.834],"TX|lake jackson":[29.039,-95.44],"TX|laredo":[27.557,-99.491],"TX|league":[29.517,-95.096],"TX|lubbock":[33.574,-101.871],"TX|midland":[31.939,-102.067],"TX|new braunfels":[29.723,-98.074],"TX|new caney":[30.158,-95.198],"TX|olmito":[26.035,-97.55],"TX|pattison":[29.817,-96.007],"TX|plano":[33.038,-96.719],"TX|robstown":[27.798,-97.7],"TX|rockport":[28.031,-97.069],"TX|rosenberg":[29.55,-95.798],"TX|san angelo":[31.465,-100.39],"TX|san antonio":[29.465,-98.498],"TX|san marcos":[29.875,-97.94],"TX|sanger":[33.356,-97.181],"TX|taylor":[30.571,-97.409],"TX|temple":[31.069,-97.38],"TX|the woodlands":[30.226,-95.492],"TX|trophy club":[33.021,-97.213],"TX|tyler":[32.369,-95.289],"TX|waco":[31.552,-97.14],"UT|american fork":[40.393,-111.794],"UT|price":[39.602,-110.808],"UT|riverdale":[41.174,-111.981],"UT|s salt lake":[40.715,-111.893],"UT|salt lake":[40.71,-111.889],"UT|st george":[40.877,-111.873],"VA|arlington":[38.874,-77.098],"VA|charlottesville":[38.047,-78.482],"VA|norfolk":[36.897,-76.258],"VA|richmond":[37.528,-77.474],"VA|roanoke":[37.303,-79.932],"VA|ruckersville":[38.259,-78.407],"VA|sterling":[39.013,-77.423],"VA|yorktown":[37.194,-76.5],"VT|s burlington":[44.447,-73.131],"WA|arlington":[48.183,-122.112],"WA|bellevue":[47.617,-122.143],"WA|business park dr":[38.637,-76.878],"WA|kennewick":[46.183,-119.19],"WA|la push":[47.905,-124.626],"WA|liberty lake":[47.652,-117.084],"WA|lynnwood":[47.839,-122.285],"WA|olympia":[47.013,-122.876],"WA|renton":[47.479,-122.169],"WA|seattle":[47.604,-122.331],"WA|sequim":[48.088,-123.12],"WA|tacoma":[47.226,-122.454],"WA|vancouver":[45.656,-122.617],"WI|eau claire":[44.784,-91.488],"WI|holmen":[43.976,-91.25],"WI|lake geneva":[42.588,-88.455],"WI|madison":[43.037,-89.397],"WI|milwaukee":[43.05,-87.953],"WI|wausau":[44.94,-89.67],"WV|bluefield":[37.27,-81.222],"WV|charleston":[38.349,-81.631],"WV|morgantown":[39.61,-79.983],"WY|casper":[42.846,-106.317],"WY|cheyenne":[41.144,-104.796]};
  const OP_TABLES_DATE = '2026-09-15';
  // <<< ORDERPIN TABLES
  // >>> ORDERPIN RULES (hand-ported from orderpin/normalize.py + pin.py — keep in step)
  // pin.py's window is +/-21 d around the tender's sent_at; the dashboard's readyDate sits
  // within [-10 d, +8 d] of sent_at (p1..p99 over 413 stops), so the same reach around it is
  const OP_PRE_DAYS = 21 + 10, OP_POST_DAYS = 21 + 8, OP_NEAR_MILES = 30;
  const OP_TIERS = ['alias', 'street', 'zip', 'city', 'near'];
  const OP_STRONG = new Set(['alias', 'street', 'zip']);
  // pin.STATUS_RANK: among name matches the copy that actually moved wins (a re-post left at
  // 'new' next to its picked-up '-2a' twin)
  const OP_STATUS_RANK = { new: 0, posted: 1, requests: 1, pending: 1, accepted: 2, picked_up: 3, delivered: 4, invoiced: 5, paid: 6, archived: 6 };
  const SD_MAX_VINS_PER_SHIPMENT = 6;    // VINs resolved per card at most (each costs SD calls)
  // normalize.norm_street: same substitutions, same order
  const OP_ABBR = [
    [/\bboulevard\b/g, 'blvd'], [/\bstreet\b/g, 'st'], [/\bavenue\b/g, 'ave'], [/\bdrive\b/g, 'dr'],
    [/\broad\b/g, 'rd'], [/\bhighway\b/g, 'hwy'], [/\bfreeway\b/g, 'fwy'], [/\blane\b/g, 'ln'],
    [/\bcourt\b/g, 'ct'], [/\bparkway\b/g, 'pkwy'], [/\bsuite\b/g, 'ste'], [/\bnorth\b/g, 'n'],
    [/\bsouth\b/g, 's'], [/\beast\b/g, 'e'], [/\bwest\b/g, 'w'], [/\bplace\b/g, 'pl'],
    [/\bcircle\b/g, 'cir'], [/\bexpressway\b/g, 'expy'], [/\bturnpike\b/g, 'tpke'],
    [/\bnortheast\b/g, 'ne'], [/\bnorthwest\b/g, 'nw'], [/\bsoutheast\b/g, 'se'],
    [/\bsouthwest\b/g, 'sw'], [/\bterrace\b/g, 'ter'], [/\btrail\b/g, 'trl'], [/\bpike\b/g, 'pike'],
    [/\bcommons\b/g, 'cmns'], [/\bcenter\b/g, 'ctr'], [/\bcentre\b/g, 'ctr'], [/\bmount\b/g, 'mt'],
    [/\bsaint\b/g, 'st'], [/\bfort\b/g, 'ft'], [/\broute\b/g, 'rte'], [/\bus route\b/g, 'us'],
    [/\bstate route\b/g, 'sr'], [/\bloop\b/g, 'loop'],
  ];
  const OP_UNIT_RE = /\b(ste|suite|unit|apt|bldg|building|#)\s*[a-z0-9-]+\b/g;
  const OP_STATES = {
    alabama: 'AL', alaska: 'AK', arizona: 'AZ', arkansas: 'AR', california: 'CA', colorado: 'CO',
    connecticut: 'CT', delaware: 'DE', florida: 'FL', georgia: 'GA', hawaii: 'HI', idaho: 'ID',
    illinois: 'IL', indiana: 'IN', iowa: 'IA', kansas: 'KS', kentucky: 'KY', louisiana: 'LA',
    maine: 'ME', maryland: 'MD', massachusetts: 'MA', michigan: 'MI', minnesota: 'MN',
    mississippi: 'MS', missouri: 'MO', montana: 'MT', nebraska: 'NE', nevada: 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', ohio: 'OH', oklahoma: 'OK', oregon: 'OR',
    pennsylvania: 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    tennessee: 'TN', texas: 'TX', utah: 'UT', vermont: 'VT', virginia: 'VA', washington: 'WA',
    'west virginia': 'WV', wisconsin: 'WI', wyoming: 'WY', 'district of columbia': 'DC',
  };
  function opNormStreet(a) {
    let s = String(a || '').toLowerCase().replace(/&/g, ' and ');
    s = s.replace(/[^a-z0-9 ]/g, ' ');
    for (const [re, rep] of OP_ABBR) s = s.replace(re, rep);
    s = s.replace(OP_UNIT_RE, ' ');
    return s.split(/\s+/).filter(Boolean).join(' ');
  }
  function opNormCity(c) {
    let s = String(c || '').toLowerCase().replace(/[^a-z ]/g, ' ');
    for (const [a, b] of [['saint', 'st'], ['mount', 'mt'], ['fort', 'ft'], ['west', 'w'], ['east', 'e'], ['north', 'n'], ['south', 's']]) {
      s = s.replace(new RegExp('\\b' + a + '\\b', 'g'), b);
    }
    s = s.replace(/\b(township|twp|city|village|borough)\b/g, ' ');
    return s.split(/\s+/).filter(Boolean).join(' ');
  }
  function opNormState(s) {
    s = String(s || '').trim();
    if (s.length === 2) return s.toUpperCase();
    return OP_STATES[s.toLowerCase()] || s.toUpperCase().slice(0, 2);
  }
  function opZip5(z) { const m = /\d{5}/.exec(String(z == null ? '' : z)); return m ? m[0] : ''; }
  // normalize.shp_base: 'SHP2609-A2DT232' / 'A2DT232-4' / 'A2DT232 (dup)' / 'A4J8642- Jacksonville VIP' -> 'A2DT232'
  function opShpBase(s) {
    s = String(s || '').trim();
    if (/^SHP/i.test(s) && s.indexOf('-') !== -1) s = s.slice(s.indexOf('-') + 1);
    s = s.split(/[ (]/)[0];
    return s.replace(/-(\d+[a-z]?)?$/i, '').toUpperCase();
  }
  function opKey(street, city, state) { return opNormStreet(street) + '|' + opNormCity(city) + '|' + opNormState(state); }
  function opMiles(a, b) {
    const r = Math.PI / 180, p1 = a[0] * r, p2 = b[0] * r;
    const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((b[1] - a[1]) * r / 2) ** 2;
    return 2 * 3958.8 * Math.asin(Math.sqrt(h));
  }
  function opPoint(s) { return (s && typeof s.lat === 'number' && typeof s.lon === 'number') ? [s.lat, s.lon] : null; }
  function opLabel(s) { return [s.street, s.city, (s.state + ' ' + s.zip).trim()].filter(Boolean).join(', '); }
  // pin.stop_tier: strongest tier at which an SD stop matches a tender stop, or null
  function opStopTier(t, o) {
    // the dashboard's own name for the stop is an alias of the tender stop: "NA-US-NJ-Totowa"
    // on a Parsippany tender names the venue the order really uses, so the best tier over the
    // stop and its alts wins (alts are empty for an exact table hit — identical to pin.py then)
    let best = opStopTier1(t, o);
    for (const alt of (t.alts || [])) {
      const tier = opStopTier1(alt, o);
      if (tier && (!best || OP_TIERS.indexOf(tier) < OP_TIERS.indexOf(best))) best = tier;
    }
    return best;
  }
  function opStopTier1(t, o) {
    const ts = opNormStreet(t.street), os = opNormStreet(o.street);
    const sameState = !t.state || !o.state || t.state === o.state;
    for (const alt of (OP_ALIASES[opKey(t.street, t.city, t.state)] || [])) {
      if ((os && opNormStreet(alt[0]) === os) || (alt[3] && alt[3] === o.zip)) return 'alias';
    }
    if (ts && os && ts === os && sameState) return 'street';
    if (t.zip && o.zip && opZip5(t.zip) === opZip5(o.zip)) return 'zip';
    if (t.city && o.city && opNormCity(t.city) === opNormCity(o.city) && sameState) return 'city';
    const a = opPoint(t), b = opPoint(o);
    if (a && b && opMiles(a, b) <= OP_NEAR_MILES) return 'near';
    return null;
  }
  function opAccepted(p, d) { if (!p || !d) return false; return OP_STRONG.has(p) || OP_STRONG.has(d) || (p === 'city' && d === 'city'); }
  function opRank(p, d) { const i = x => { const k = OP_TIERS.indexOf(x || ''); return k < 0 ? 9 : k; }; return i(p) + i(d); }
  // the tender side: a dashboard location name -> stop. The dashboard and the tender emails
  // name the same terminal differently ("NA-US-UT-TA-Pleasant Grove" on the dashboard is
  // "NA-US-UT-Pleasant Grove-2100 W Pleasant Grove Blvd" in the tender), so after the exact
  // table hit the name is resolved by its tokens against the table entries of the same state:
  // every best-scoring entry agreeing on the street gives the full stop, agreeing on the city
  // gives a city-level stop (with its centroid, so 'near' still works); free-form customer
  // addresses ("111 ELM ST, SACO, ME, 04072, United States of America") are parsed directly;
  // the last resort is the "NA-US-ST-City" pattern with the city centroid index.
  const OP_NAME_RE = /^NA-US-([A-Z]{2})-(.+)$/;
  const OP_GENERIC = new Set(['ta', 'sc', 'temphub', 'temp', 'hub', 'offsite', 'off', 'site', 'mvd', 'vpc', 'dc', 'wh', 'yard', 'the', 'and', 'of']);
  const OP_ADDR_RE = /^(?:[^,]*? - )?([^,]+),\s*([^,]*),\s*([A-Za-z ]{2,}),\s*(?:(\d{5})(?:-\d{4})?|US|N\/A|United States of America)(?:,\s*(?:(\d{5})(?:-\d{4})?|US|United States of America))*\s*$/;
  const opStopEmpty = name => ({ name, street: '', city: '', state: '', zip: '', lat: null, lon: null, known: false });
  function opTokens(name) {
    // tokens of a dashboard/tender name after "NA-US-ST-": lower-cased words of each "-" segment
    const m = OP_NAME_RE.exec(String(name || '').trim());
    if (!m) return { state: '', toks: new Set(), segs: [] };
    const segs = m[2].split('-').map(s => s.trim()).filter(Boolean);
    const toks = new Set();
    for (const seg of segs) for (const w of seg.toLowerCase().split(/[^a-z0-9]+/)) if (w && !OP_GENERIC.has(w) && !/^\d+$/.test(w)) toks.add(w);
    return { state: m[1], toks, segs };
  }
  let opIndex = null;    // built once: state -> [{name, toks, row}]
  function opBuildIndex() {
    opIndex = {};
    for (const name of Object.keys(OP_LOCATIONS)) {
      const t = opTokens(name);
      if (!t.state || !t.toks.size) continue;
      (opIndex[t.state] = opIndex[t.state] || []).push({ name, toks: t.toks, row: OP_LOCATIONS[name] });
    }
  }
  function opRowStop(name, row, known) {
    return { name, street: row[0] || '', city: row[1] || '', state: opNormState(row[2]), zip: row[3] || '', lat: row[4], lon: row[5], known: !!known };
  }
  function opNameAlts(t) {
    // the dashboard name's own segments as city-level stops (the venue the order really uses is
    // often named right there: "NA-US-NJ-Totowa", "NA-US-FL-UVR-Kissimmee")
    const alts = [];
    for (const seg of t.segs) {
      const words = seg.toLowerCase().split(/[^a-z0-9]+/).filter(w => w && !OP_GENERIC.has(w) && !/^\d+$/.test(w));
      if (!words.length) continue;
      const c = OP_CITIES[t.state + '|' + opNormCity(seg)];
      alts.push({ name: seg, street: '', city: seg, state: t.state, zip: '', lat: c ? c[0] : null, lon: c ? c[1] : null });
    }
    return alts;
  }
  function opTeslaStop(name) {
    name = String(name || '').trim();
    const row = OP_LOCATIONS[name];
    if (row) return opRowStop(name, row, true);           // exact: no alts, identical to pin.py
    // free-form address ("Name - 200 West Beltline Hwy, Madison, WI, US, 53713" / "111 ELM ST, SACO, ME, 04072, United States of America")
    const a = OP_ADDR_RE.exec(name);
    if (a) {
      const st = opNormState(a[3]), zip = a[4] || a[5] || '';
      const c = OP_CITIES[st + '|' + opNormCity(a[2])];
      return { name, street: a[1].trim(), city: a[2].trim(), state: st, zip, lat: c ? c[0] : null, lon: c ? c[1] : null, known: false };
    }
    const t = opTokens(name);
    if (!t.state) return opStopEmpty(name);
    if (!opIndex) opBuildIndex();
    // token match against the table entries of the same state
    let best = 0, hits = [];
    for (const e of (opIndex[t.state] || [])) {
      let n = 0;
      for (const w of t.toks) if (e.toks.has(w) && w.length >= 3) n++;
      if (!n) continue;
      if (n > best) { best = n; hits = [e]; } else if (n === best) hits.push(e);
    }
    if (hits.length) {
      const streets = new Set(hits.map(h => opNormStreet(h.row[0]))), cities = new Set(hits.map(h => opNormCity(h.row[1])));
      if (streets.size === 1 && hits[0].row[0]) return Object.assign(opRowStop(name, hits[0].row, false), { alts: opNameAlts(t) });
      if (cities.size === 1) { const r = hits[0].row; return { name, street: '', city: r[1] || '', state: opNormState(r[2]), zip: '', lat: r[4], lon: r[5], known: false, alts: opNameAlts(t) }; }
    }
    // last resort: a segment that is a known city of that state (else the first segment)
    for (const seg of t.segs) {
      const c = OP_CITIES[t.state + '|' + opNormCity(seg)];
      if (c) return { name, street: '', city: seg, state: t.state, zip: '', lat: c[0], lon: c[1], known: false, alts: opNameAlts(t) };
    }
    return { name, street: '', city: t.segs[0] || '', state: t.state, zip: '', lat: null, lon: null, known: false, alts: opNameAlts(t) };
  }
  // the SD side: a public-API order stop -> stop (venue address + the stop's own coordinates)
  function opSdStop(stop) {
    stop = stop || {};
    const v = stop.venue || {};
    return { name: String(v.name || '').trim(), street: String(v.address || '').trim(), city: String(v.city || '').trim(),
             state: opNormState(v.state), zip: opZip5(v.zip),
             lat: typeof stop.latitude === 'number' ? stop.latitude : null, lon: typeof stop.longitude === 'number' ? stop.longitude : null };
  }
  function opOrderFromApi(o, short) {
    o = o || {};
    return { raw: o, guid: String(o.guid || (short && short.guid) || ''),
             number: String(o.number || o.order_number || (short && short.number) || '').trim(),
             status: sdNormStatus(o.status), created: sdParseDate(o.created_at || (short && short.created_at)),
             pickup: opSdStop(o.pickup), delivery: opSdStop(o.delivery),
             vins: (o.vehicles || []).map(v => String((v && v.vin) || '').toUpperCase()).filter(Boolean) };
  }
  function opUsable(o) { return o.number.toLowerCase().indexOf('(dup') === -1 && o.status !== 'canceled'; }
  // pin._choose: score every candidate; name first, then (among name matches) the most
  // progressed status, then tier rank, then creation closest to the tender anchor; an exact tie
  // is ambiguous -> no pin
  function opChoose(t, orders) {
    const scored = [], cands = [];
    for (const o of orders) {
      const pk = opStopTier(t.origin, o.pickup), dl = opStopTier(t.dest, o.delivery);
      const name = !!t.base && opShpBase(o.number) === t.base;
      const acc = opAccepted(pk, dl);
      cands.push({ order: o.number, guid: o.guid, status: o.status, created: o.created, name, pickup: pk, delivery: dl,
                   accepted: name || acc, sd_pickup: opLabel(o.pickup), sd_delivery: opLabel(o.delivery) });
      if (name || acc) {
        const dist = (t.anchor != null && o.created != null) ? Math.abs(o.created - t.anchor) : 0;
        const progressed = name ? (OP_STATUS_RANK[o.status] || 0) : 0;
        scored.push({ key: [name ? 0 : 1, -progressed, opRank(pk, dl), dist], o, pk, dl, name });
      }
    }
    if (!scored.length) {
      if (!orders.length) return { win: null, cands, reason: 'no candidate in window' };
      const near = cands.slice().sort((a, b) => ((a.pickup == null) + (a.delivery == null)) - ((b.pickup == null) + (b.delivery == null)))[0];
      return { win: null, cands, reason: 'no route match: nearest ' + near.order + ' pickup=' + near.pickup + ' delivery=' + near.delivery +
                                        ' (' + near.sd_pickup + ' -> ' + near.sd_delivery + ')' };
    }
    scored.sort((a, b) => (a.key[0] - b.key[0]) || (a.key[1] - b.key[1]) || (a.key[2] - b.key[2]) || (a.key[3] - b.key[3]));
    if (scored.length > 1 && scored[0].key.every((v, i) => v === scored[1].key[i])) {
      return { win: null, cands, reason: 'ambiguous: ' + scored[0].o.number + ' vs ' + scored[1].o.number + ' tie' };
    }
    const w = scored[0];
    return { win: w, cands, reason: w.name ? 'name match' : 'route pickup=' + w.pk + ' delivery=' + w.dl };
  }
  // the tender-move as the dashboard knows it: Tesla base + stops (by location name) + the
  // window anchor (ready date, else the pickup estimate, else need-by)
  function opTenderFor(base, tv) {
    tv = tv || {};
    let anchor = sdParseDate(tv.ready);
    if (anchor == null) anchor = sdParseDate(tv.pickup);
    if (anchor == null) anchor = sdParseDate(tv.needBy);
    return { base, anchor, origin: opTeslaStop(tv.origin), dest: opTeslaStop(tv.dest) };
  }
  // pin.pin for one VIN: find_by_vin -> window -> full orders -> choose. Throws on an SD
  // error so the caller retries next pass rather than deciding on partial candidates.
  async function opPinVin(vin, t) {
    const shorts = await sdFindByVin(vin);
    const inWindow = [];
    for (const s of shorts) {
      const num = String((s && (s.number || s.order_number)) || '');
      if (num.toLowerCase().indexOf('(dup') !== -1) continue;
      const created = sdParseDate(s && s.created_at);
      if (t.anchor != null && created != null &&
          (created < t.anchor - OP_PRE_DAYS * 864e5 || created > t.anchor + OP_POST_DAYS * 864e5)) continue;
      if (s && s.guid) inWindow.push(s);
    }
    const orders = [];
    for (const s of inWindow) {
      const o = opOrderFromApi(await sdGetOrder(s.guid), s);
      // membership re-checked on the full order: the VIN can move off between the two calls
      if (o.guid && opUsable(o) && (!o.vins.length || o.vins.indexOf(vin) !== -1)) orders.push(o);
      await sdSleep(SD_REQ_GAP_MS);
    }
    const r = opChoose(t, orders);
    if (!r.win) return { pin: null, reason: r.reason, candidates: r.cands };
    const w = r.win;
    return { pin: { guid: w.o.guid, number: w.o.number, status: w.o.status, card: sdMakeCard(w.o.raw), vin,
                    method: w.name ? 'name' : 'route', pickup: w.pk, delivery: w.dl, reason: r.reason, vehicles: w.o.vins },
             reason: r.reason, candidates: r.cands };
  }
  // <<< ORDERPIN RULES

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
  function opEntry(rec, pins, holds) {
    const p0 = pins[0] || null;
    return { version: SD_CACHE_VERSION, matched: !!p0, status: p0 ? p0.status : '', card: p0 ? p0.card : null,
             vin: p0 ? p0.vin : rec.vin, guid: p0 ? p0.guid : '', method: p0 ? p0.method : '',
             pins, holds, tablesDate: OP_TABLES_DATE };
  }
  async function sdEvaluate(base, rec, cached) {
    const vins = (rec.vins && rec.vins.length) ? rec.vins : (rec.vin ? [rec.vin] : []);
    const tenderOf = vin => opTenderFor(base, store.vins[vin]);
    // Recheck path: every cached pin is re-validated with ONE get_order — the order must
    // still be usable, still carry the VIN and still pass the same rule (name or route);
    // otherwise the whole card re-resolves through find_by_vin. The cache resets daily.
    if (cached && cached.version === SD_CACHE_VERSION && cached.pins && cached.pins.length) {
      const pins = [];
      let stale = false;
      for (const pin of cached.pins) {
        let full;
        try { full = await sdGetOrder(pin.guid); }
        catch (e) { return { error: String((e && e.message) || e) }; }
        const o = opOrderFromApi(full, pin);
        const t = tenderOf(pin.vin);
        const pk = opStopTier(t.origin, o.pickup), dl = opStopTier(t.dest, o.delivery);
        const name = !!t.base && opShpBase(o.number) === t.base;
        if (o.guid && opUsable(o) && o.vins.indexOf(pin.vin) !== -1 && (name || opAccepted(pk, dl))) {
          pins.push(Object.assign({}, pin, { number: o.number, status: o.status, card: sdMakeCard(full),
                                             method: name ? 'name' : 'route', pickup: pk, delivery: dl, vehicles: o.vins }));
        } else { stale = true; break; }
      }
      if (!stale) return { entry: opEntry(rec, pins, cached.holds || []) };
      sdLog('stale pin for', base, '— re-resolving');
    }
    // Fresh resolve: VIN by VIN, skipping VINs already carried by a pinned order, so a split
    // (A0PM328 + A0PM328-2) yields one pin per order and a lone unmatched VIN is reported.
    const pins = [], holds = [], covered = new Set();
    let budget = SD_MAX_VINS_PER_SHIPMENT;
    try {
      for (const vin of vins) {
        if (covered.has(vin) || budget <= 0) continue;
        budget--;
        const r = await opPinVin(vin, tenderOf(vin));
        if (r.pin) {
          if (!pins.some(p => p.guid === r.pin.guid)) pins.push(r.pin);
          for (const v of r.pin.vehicles) covered.add(v);
          covered.add(vin);
        } else {
          holds.push({ vin, reason: r.reason, candidates: r.candidates.length });
        }
      }
    } catch (e) { return { error: String((e && e.message) || e) }; }
    return { entry: opEntry(rec, pins, holds) };
  }
  async function runSdCheck() {
    if (sdChecking || !ON_DASH() || !sdShipments.size) return;
    if (!sdGetCreds()) { sdLog('no SuperDispatch credentials — set them via the Tampermonkey menu'); return; }
    sdChecking = true;
    try {
      const cache = sdLoadCache();
      const todo = [...sdShipments.entries()].filter(([base]) => {
        const e = cache.bases[base];
        // green is terminal; everything else (including holds and no-match) re-checks each pass
        return !e || e.version !== SD_CACHE_VERSION || !(e.pins || []).length || e.pins.some(p => !SD_GREEN.has(p.status));
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
            cache.bases[base] = res.entry;
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
      // held: SD candidates exist but none passes the rule — surfaced, not guessed
      '.dd-sd-bubble.hold{background:#fff;color:#8a6a00;border:1px dashed #d9b64a;padding:1px 8px;cursor:help;}',
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
      const pins = (entry && entry.version === SD_CACHE_VERSION && entry.pins) || [];
      const heldWithCands = !!entry && !pins.length && (entry.holds || []).some(h => h.candidates > 0);
      const want = pins.length || (heldWithCands ? 1 : 0);
      const bubbles = Array.from(node.querySelectorAll('.dd-sd-bubble'));
      while (bubbles.length > want) bubbles.pop().remove();
      if (!want) return;                        // no candidates at all -> no bubble
      for (let i = 0; i < want; i++) {
        let bubble = bubbles[i];
        if (!bubble) {
          // an anchor: clicking the bubble opens the SD order in a new tab
          bubble = document.createElement('a');
          bubble.className = 'dd-sd-bubble';
          bubble.target = '_blank';
          bubble.rel = 'noopener';
          bubble.addEventListener('click', e => e.stopPropagation());   // don't poke Tesla's card
          node.appendChild(bubble);
          bubbles[i] = bubble;
        }
        if (!pins.length) {                     // held: candidates but no rule match
          const reasons = entry.holds.filter(h => h.candidates > 0).map(h => '…' + h.vin.slice(-6) + ': ' + h.reason).join('\n');
          const sig = [base, 'hold', reasons].join('|');
          if (bubble.dataset.sig === sig) continue;
          bubble.dataset.sig = sig;
          bubble.className = 'dd-sd-bubble hold';
          bubble.textContent = '?';
          bubble.title = 'SuperDispatch: no order passes the match rule (held, not guessed)\n' + reasons;
          bubble.dataset.base = base; bubble.dataset.pin = ''; bubble.dataset.card = '';
          bubble.removeAttribute('href');
          continue;
        }
        const pin = pins[i];
        // "Posted" (loadboard) vs genuinely-unposted "New" — both italic gray.
        const isPreLifecycle = SD_NOCARD.has(pin.status);
        const hasCard = pin.card ? '1' : '';
        const cls = 'dd-sd-bubble ' + sdBubbleColor(pin.status, pin.card) + (isPreLifecycle ? ' posted' : '');
        const label = sdStatusLabel(pin.status, pin.card);
        const hrs = sdHoursMarker(pin);
        const held = (entry.holds || []).filter(h => h.candidates > 0).length;
        const how = pin.method === 'name' ? 'Tesla number in the SD order name'
                                          : 'route (pickup ' + pin.pickup + ', delivery ' + pin.delivery + ')';
        // Rebuild only on real change: decorate runs constantly (the global
        // MutationObserver re-fires on our own writes), and gratuitously recreating
        // the bubble's children breaks an in-flight hover over them.
        const sig = [base, i, cls, label, hrs, hasCard, pin.guid || '', how, held].join('|');
        if (bubble.dataset.sig === sig) continue;
        bubble.dataset.sig = sig;
        bubble.className = cls;
        bubble.textContent = label;
        if (hrs) {
          const h = document.createElement('span');
          h.className = 'dd-sd-hrs';
          h.textContent = hrs;
          bubble.appendChild(h);
        }
        bubble.title = 'SD ' + pin.number + ' · matched by ' + how + (held ? ' · ' + held + ' VIN(s) of this shipment held (no match)' : '');
        bubble.dataset.base = base;
        bubble.dataset.pin = String(i);
        bubble.dataset.card = hasCard;
        if (pin.guid) bubble.href = SD_ORDER_URL + pin.guid;
        else bubble.removeAttribute('href');
      }
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
    const key = base + '#' + (bubble.dataset.pin || '0');
    if (sdShownBase === key && sdPanelEl && sdPanelEl.style.opacity === '1') return;
    clearTimeout(sdShowT);
    sdShowT = setTimeout(() => {
      const entry = sdLoadCache().bases[base];
      const pin = (entry && entry.pins) ? entry.pins[Number(bubble.dataset.pin) || 0] : null;
      const card = pin ? pin.card : (entry && entry.card);
      if (!card) return;
      const p = sdEnsurePanel();
      p.innerHTML = sdCardHtml(card, (pin ? pin.vin : entry.vin) || '');
      p.style.display = 'block'; p.style.opacity = '0';
      const r = bubble.getBoundingClientRect();
      const pw = p.offsetWidth, ph = p.offsetHeight;
      let left = r.right + 10;
      let top = r.top - ph - 6;
      if (left + pw > window.innerWidth - 8) left = Math.max(8, r.left - pw - 10);
      if (top < 8) top = r.bottom + 6;
      p.style.left = left + 'px'; p.style.top = top + 'px';
      p.style.opacity = '1';
      sdShownBase = key;
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
