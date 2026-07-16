// ==UserScript==
// @name         Tesla Capacity Planner — Requested History
// @namespace    wastake.capacityplanner
// @version      0.14.0
// @description  Full-screen replacement UI for Tesla Capacity Planner in the bidboard theme: This Week / Next Week grid, per-day cells with a schedule entry box (placeholder = Tesla's current confirmed) next to the requested number, hover history cards from the server change log, dummy Confirm Capacity buttons, and a bottom-right Tesla grid / Planner UI toggle. Still read-only toward Tesla; mirrors both capacity feeds to the shipment-creator change log.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
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
 * READ-ONLY TESLA ACCESS — no writes and no editable fields. It hooks fetch/XHR at document-start,
 * captures requestcapacity, and stores Requested-value history privately in Tampermonkey.
 *
 * Data model (see findings.md), GET requestcapacity under …/api/v1/CapacityPlanner/carrier/ :
 *   data = { carrierId, locationRequests[] = { originLocationId,
 *       originLocationName, groupRequests[] = { destinationGroupId, destinationGroupName,
 *           capacityRequests[] = { date, capacity (=Requested), latestRequestDate } } }
 *   Persistent key = carrierId + originLocationId + destinationGroupId + date.
 */

(function () {
  'use strict';
  const LOG = (...a) => console.log('%c[cap-history]', 'color:#c62828;font-weight:bold', ...a);
  const ROUTE_RE = /\/logistics\/capacity-planner/i;
  const PAGE = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
  const REQUEST_HISTORY_KEY = 'cp_requested_history_v1';
  const HISTORY_LIMIT = 25;

  const state = { req: null, conf: null };

  // Persistent per carrier + origin + destination-group + date history. The first value is
  // a baseline; only a later different Requested value creates a change and a red marker.
  let requestedHistory = loadRequestedHistory();
  function loadRequestedHistory() {
    try {
      let value = GM_getValue(REQUEST_HISTORY_KEY, null);
      if (typeof value === 'string') value = JSON.parse(value);
      if (value && value.entries && typeof value.entries === 'object') return value;
    } catch (_) {}
    return { version: 1, entries: {} };
  }
  function saveRequestedHistory() {
    try { GM_setValue(REQUEST_HISTORY_KEY, requestedHistory); } catch (_) {}
  }
  function historyKey(carrierId, originId, groupId, date) {
    return [carrierId, originId, groupId, date].join('|');
  }
  function trackRequested(json) {
    const data = json && json.data;
    if (!data || !Array.isArray(data.locationRequests)) return;
    const observedAt = new Date().toISOString();
    const carrierId = data.carrierId == null ? '' : String(data.carrierId);
    let touched = false, changed = 0;
    data.locationRequests.forEach((location) => {
      (location.groupRequests || []).forEach((group) => {
        (group.capacityRequests || []).forEach((request) => {
          const date = dayKey(request.date);
          const value = Number(request.capacity);
          if (!date || !Number.isFinite(value)) return;
          const key = historyKey(carrierId, location.originLocationId, group.destinationGroupId, date);
          let entry = requestedHistory.entries[key];
          if (!entry) {
            entry = requestedHistory.entries[key] = {
              carrierId,
              originId: location.originLocationId,
              originName: location.originLocationName || '',
              groupId: group.destinationGroupId,
              laneName: group.destinationGroupName || '',
              date,
              current: value,
              previous: null,
              firstSeen: observedAt,
              lastSeen: observedAt,
              changedAt: null,
              changes: [],
            };
          } else {
            entry.originName = location.originLocationName || entry.originName || '';
            entry.laneName = group.destinationGroupName || entry.laneName || '';
            entry.lastSeen = observedAt;
            if (Number(entry.current) !== value) {
              const previous = Number(entry.current);
              entry.previous = Number.isFinite(previous) ? previous : null;
              entry.current = value;
              entry.changedAt = observedAt;
              if (!Array.isArray(entry.changes)) entry.changes = [];
              entry.changes.push({ from: entry.previous, to: value, at: observedAt });
              if (entry.changes.length > HISTORY_LIMIT) entry.changes.splice(0, entry.changes.length - HISTORY_LIMIT);
              changed++;
            }
          }
          entry.latestRequestDate = request.latestRequestDate || entry.latestRequestDate || null;
          touched = true;
        });
      });
    });
    if (touched) saveRequestedHistory();
    if (changed) LOG('requested capacity changes detected', changed);
    scheduleRequestedAnnotations();
  }

  // ---- mirror both feeds to the shipment-creator change log -----------------
  // The server appends a row only when a value differs from the last one it logged, so
  // re-sending the same snapshot is harmless; we still debounce ~2s and skip byte-identical
  // payloads to keep normal browsing quiet.
  const SNAPSHOT_URL = 'https://shipments.wastake.com/api/capacity-snapshot';
  let pushTimer = null, lastPushSig = '';
  function buildSnapshot() {
    const req = state.req && state.req.data, conf = state.conf && state.conf.data;
    const carrierId = (req && req.carrierId) != null ? req.carrierId : (conf && conf.carrierId);
    const requested = [], confirmed = [];
    (((req || {}).locationRequests) || []).forEach((location) => {
      (location.groupRequests || []).forEach((group) => {
        (group.capacityRequests || []).forEach((r) => {
          requested.push({
            origin_id: location.originLocationId, origin_name: location.originLocationName || '',
            dest_group_id: group.destinationGroupId, dest_group_name: group.destinationGroupName || '',
            date: r.date, capacity: r.capacity, latest_request_date: r.latestRequestDate || null,
          });
        });
      });
    });
    (((conf || {}).locationCapacities) || []).forEach((location) => {
      (location.groupCapacities || []).forEach((group) => {
        (group.confirmCapacities || []).forEach((c) => {
          confirmed.push({
            origin_id: location.originLocationId, dest_group_id: group.destinationGroupId,
            date: c.capacityDate, capacity: c.capacity, scheduled: c.scheduled,
            is_conflict: !!c.isConflict,
          });
        });
      });
    });
    if (!requested.length && !confirmed.length) return null;
    return { carrier_id: carrierId, requested, confirmed };
  }
  function scheduleServerPush() {
    if (typeof GM_xmlhttpRequest !== 'function') return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(() => {
      const snap = buildSnapshot();
      if (!snap) return;
      const sig = JSON.stringify(snap);
      if (sig === lastPushSig) return;
      GM_xmlhttpRequest({
        method: 'POST',
        url: SNAPSHOT_URL,
        headers: { 'Content-Type': 'application/json' },
        data: sig,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            lastPushSig = sig;
            try {
              const j = JSON.parse(r.responseText);
              if (j.requested_changes || j.confirmed_changes) {
                LOG('server logged changes', { requested: j.requested_changes, confirmed: j.confirmed_changes });
                fetchServerHistory(true);   // pull the fresh rows so new badges show immediately
              }
            } catch (_) {}
          } else {
            LOG('capacity-snapshot HTTP ' + r.status);
          }
        },
        onerror: () => LOG('capacity-snapshot send failed'),
      });
    }, 2000);
  }

  // ---- capture requestcapacity + getcapacityconfirmations -------------------
  function classify(url) {
    if (!url) return null;
    if (/requestcapacity/i.test(url)) return 'req';
    if (/getcapacityconfirmations/i.test(url)) return 'conf';
    return null;
  }
  function ingest(text, name) {
    try {
      const j = JSON.parse(text);
      if (!j || !j.data) return;
      state[name] = j;
      if (name === 'req') trackRequested(j);
      LOG('captured ' + (name === 'req' ? 'requestcapacity' : 'getcapacityconfirmations'));
      scheduleServerPush();
      scheduleRender();
    } catch (_) {}
  }

  // fetch hook
  const oFetch = PAGE.fetch;
  if (oFetch) {
    PAGE.fetch = function (input, init) {
      const url = (input && input.url) || input;
      const name = classify(url);
      const p = oFetch.apply(this, arguments);
      if (name) p.then((r) => { r.clone().text().then((t) => ingest(t, name)).catch(() => {}); }).catch(() => {});
      return p;
    };
  }

  // XHR hook
  const X = PAGE.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send;
  X.prototype.open = function (m, u) { this.__cpHistoryUrl = u; return oOpen.apply(this, arguments); };
  X.prototype.send = function () {
    const xhr = this, name = classify(xhr.__cpHistoryUrl);
    if (name) xhr.addEventListener('load', function () { try { ingest(xhr.responseText, name); } catch (_) {} });
    return oSend.apply(this, arguments);
  };

  // ---- helpers --------------------------------------------------------------
  const dayKey = (s) => String(s || '').slice(0, 10);            // "2026-07-10"
  function parseDay(k) { const p = k.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]); }   // local midnight
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

  // ---- persistent Requested-change markers on Tesla's own grid ------------
  const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
  let requestedAnnotationTimer = null, requestedObserver = null, requestedTip = null;
  const normName = (s) => String(s || '').trim().replace(/\s+/g, ' ').toLowerCase();
  function dateKeyFromLocal(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  function visibleWeekStart() {
    const chip = [...document.querySelectorAll('tsl-chip span')].find((el) =>
      /^[A-Za-z]{3}\s+\d{1,2}\s*-\s*[A-Za-z]{3}\s+\d{1,2}\s+\d{4}$/.test(el.textContent.trim()));
    if (!chip) return null;
    const match = chip.textContent.trim().match(/^([A-Za-z]{3})\s+(\d{1,2})\s*-\s*([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})$/);
    if (!match) return null;
    const startMonth = MONTHS[match[1].toLowerCase()], endMonth = MONTHS[match[3].toLowerCase()];
    if (startMonth == null || endMonth == null) return null;
    const endYear = Number(match[5]);
    const startYear = startMonth > endMonth ? endYear - 1 : endYear;
    return new Date(startYear, startMonth, Number(match[2]));
  }
  function requestLaneIndex() {
    const data = state.req && state.req.data, index = new Map();
    if (!data) return index;
    (data.locationRequests || []).forEach((location) => {
      (location.groupRequests || []).forEach((group) => {
        index.set(normName(location.originLocationName) + '|' + normName(group.destinationGroupName), {
          carrierId: data.carrierId == null ? '' : String(data.carrierId),
          originId: location.originLocationId,
          groupId: group.destinationGroupId,
        });
      });
    });
    return index;
  }
  function ensureRequestedUi() {
    if (!document.getElementById('cp-requested-history-style')) {
      const style = document.createElement('style');
      style.id = 'cp-requested-history-style';
      style.textContent = `
        .cp-request-changed{display:inline-flex!important;align-items:center!important;gap:4px!important;background:transparent!important;
          color:transparent!important;border:0!important;padding:0!important;margin-left:2px!important;font-size:0!important;cursor:help!important}
        .cp-request-changed::before{content:'/';color:#171a20;font-size:14px;font-weight:400;line-height:1.25}
        .cp-request-changed::after{content:attr(data-cp-current);display:inline-block;background:#c62828;color:#fff;
          border:1px solid #9d1717;border-radius:4px;padding:2px 6px;font-size:14px;font-weight:700;line-height:1.25;
          box-shadow:0 1px 3px rgba(120,0,0,.25)}
        #cp-request-history-tip{position:fixed;display:none;z-index:2147483647;width:292px;padding:12px 13px;
          background:#171a20;color:#fff;border:1px solid #34383f;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.3);
          font:13px/1.35 system-ui,Segoe UI,Arial,sans-serif;pointer-events:none}
        #cp-request-history-tip .cp-tip-title{font-weight:800;font-size:13px;color:#ff9b95;margin-bottom:5px}
        #cp-request-history-tip .cp-tip-route{font-weight:650;white-space:normal;margin-bottom:2px}
        #cp-request-history-tip .cp-tip-date{font-size:12px;color:#b9bec6;margin-bottom:10px}
        #cp-request-history-tip .cp-tip-values{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
        #cp-request-history-tip .cp-tip-values span{display:block;color:#aeb4bd;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
        #cp-request-history-tip .cp-tip-values b{display:block;color:#fff;font-size:18px;margin-top:1px}
        #cp-request-history-tip .cp-tip-values b.delta-up{color:#ff9089}
        #cp-request-history-tip .cp-tip-values b.delta-down{color:#8fc4ff}
        #cp-request-history-tip .cp-tip-observed{margin-top:9px;padding-top:8px;border-top:1px solid #34383f;color:#b9bec6;font-size:11px}
      `;
      (document.head || document.documentElement).appendChild(style);
    }
    if (!requestedTip) {
      requestedTip = document.createElement('div');
      requestedTip.id = 'cp-request-history-tip';
      document.documentElement.appendChild(requestedTip);
    }
  }
  function scheduleRequestedAnnotations() {
    if (!ROUTE_RE.test(location.pathname)) return;
    clearTimeout(requestedAnnotationTimer);
    requestedAnnotationTimer = setTimeout(applyRequestedAnnotations, 80);
  }
  function applyRequestedAnnotations() {
    if (!ROUTE_RE.test(location.pathname) || !state.req) return;
    ensureRequestedUi();
    const start = visibleWeekStart();
    if (!start) return;
    const lanes = requestLaneIndex();
    let currentOrigin = '';
    document.querySelectorAll('.grid-row.region-background, .grid-row.white-background').forEach((row) => {
      const nameNode = row.querySelector('.region-name span');
      const name = nameNode ? nameNode.textContent.trim() : '';
      if (row.classList.contains('region-background')) {
        currentOrigin = name;
        return;
      }
      if (!row.classList.contains('white-background') || !currentOrigin || !name) return;
      const lane = lanes.get(normName(currentOrigin) + '|' + normName(name));
      if (!lane) return;
      row.querySelectorAll('.region-cell').forEach((cell, index) => {
        const flag = cell.querySelector('.demand .demand-flag');
        if (!flag) return;
        const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
        const key = historyKey(lane.carrierId, lane.originId, lane.groupId, dateKeyFromLocal(date));
        const entry = requestedHistory.entries[key];
        const changed = entry && Array.isArray(entry.changes) && entry.changes.length > 0;
        flag.classList.toggle('cp-request-changed', !!changed);
        if (changed) {
          flag.dataset.cpHistoryKey = key;
          flag.dataset.cpCurrent = entry.current;
          flag.setAttribute('aria-label', `Requested capacity changed from ${entry.previous} to ${entry.current}`);
        } else {
          delete flag.dataset.cpHistoryKey;
          delete flag.dataset.cpCurrent;
          flag.removeAttribute('aria-label');
        }
      });
    });
  }
  function showRequestedTip(target) {
    const entry = requestedHistory.entries[target.dataset.cpHistoryKey];
    if (!entry || !entry.changes || !entry.changes.length) return;
    ensureRequestedUi();
    const latest = entry.changes[entry.changes.length - 1];
    const delta = Number(latest.to) - Number(latest.from);
    const deltaText = (delta > 0 ? '+' : '') + delta;
    const date = parseDay(entry.date);
    const dateText = isNaN(date) ? entry.date : date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
    const observed = new Date(latest.at);
    requestedTip.innerHTML = `
      <div class="cp-tip-title">Requested capacity changed</div>
      <div class="cp-tip-route">${esc(entry.originName)} → ${esc(entry.laneName)}</div>
      <div class="cp-tip-date">${esc(dateText)}</div>
      <div class="cp-tip-values">
        <div><span>Previous</span><b>${esc(latest.from)}</b></div>
        <div><span>Current</span><b>${esc(latest.to)}</b></div>
        <div><span>Change</span><b class="${delta > 0 ? 'delta-up' : delta < 0 ? 'delta-down' : ''}">${esc(deltaText)}</b></div>
      </div>
      <div class="cp-tip-observed">Detected ${esc(isNaN(observed) ? latest.at : observed.toLocaleString())}</div>`;
    requestedTip.style.display = 'block';
    const targetRect = target.getBoundingClientRect(), tipRect = requestedTip.getBoundingClientRect();
    let left = targetRect.right + 10;
    if (left + tipRect.width > window.innerWidth - 8) left = targetRect.left - tipRect.width - 10;
    let top = targetRect.top - 8;
    top = Math.max(8, Math.min(top, window.innerHeight - tipRect.height - 8));
    requestedTip.style.left = Math.max(8, left) + 'px';
    requestedTip.style.top = top + 'px';
  }
  function hideRequestedTip() { if (requestedTip) requestedTip.style.display = 'none'; }
  function installRequestedTrackingUi() {
    ensureRequestedUi();
    if (!requestedObserver) {
      requestedObserver = new MutationObserver(scheduleRequestedAnnotations);
      requestedObserver.observe(document.documentElement, { childList: true, subtree: true });
      document.addEventListener('mouseover', (event) => {
        const target = event.target && event.target.closest && event.target.closest('.cp-request-changed');
        if (target) showRequestedTip(target);
      }, true);
      document.addEventListener('mouseout', (event) => {
        const target = event.target && event.target.closest && event.target.closest('.cp-request-changed');
        if (target && (!event.relatedTarget || !target.contains(event.relatedTarget))) hideRequestedTip();
      }, true);
      PAGE.addEventListener('scroll', hideRequestedTip, true);
    }
    scheduleRequestedAnnotations();
  }

  // ==========================================================================
  // ---- Full-screen planner panel (v0.5.0) -----------------------------------
  // Bidboard-style replacement UI spliced over Tesla's own grid (which keeps
  // running hidden underneath — its fetches are what feed us). Read-only toward
  // Tesla: the Confirm Capacity buttons are dummies until the write contract is
  // captured; the "Tesla grid" button unhides the native page for real confirms.
  const HISTORY_URL = 'https://shipments.wastake.com/api/capacity-history?days=14';
  const PANEL_GAP = 0;   // flush with Tesla's layout — no gutter line between panel and page
  let host = null, root = null, pill = null;
  const panelState = { week: 0, nativeMode: false, embedded: false };
  let hiddenEl = null, observedParent = null, mo = null, moScheduled = false;
  let serverHist = null, serverHistAt = 0, serverHistPending = false;
  let renderTimer = null, panelToastTimer = null;

  // Typed schedule amounts (the left box in each cell) — kept locally like bidboard's
  // price drafts, keyed carrier|origin|group|date, until the confirm write is wired up.
  const SCHED_PLAN_KEY = 'cp_sched_plan_v1';
  let schedPlan = (() => {
    try {
      let v = GM_getValue(SCHED_PLAN_KEY, null);
      if (typeof v === 'string') v = JSON.parse(v);
      if (v && typeof v === 'object') return v;
    } catch (_) {}
    return {};
  })();
  function saveSchedPlan() { try { GM_setValue(SCHED_PLAN_KEY, schedPlan); } catch (_) {} }

  // Acknowledged requested-changes: hk -> observed_at of the change that was acknowledged.
  // A NEWER change on the same lane-day re-reds the box until it is clicked again.
  const REQ_ACK_KEY = 'cp_req_ack_v1';
  let reqAck = (() => {
    try {
      let v = GM_getValue(REQ_ACK_KEY, null);
      if (typeof v === 'string') v = JSON.parse(v);
      if (v && typeof v === 'object') return v;
    } catch (_) {}
    return {};
  })();
  function saveReqAck() { try { GM_setValue(REQ_ACK_KEY, reqAck); } catch (_) {} }

  function mondayOf(d) {
    const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
    return x;
  }
  const fmtDay = (d) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const fmtWhen = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? String(iso || '') : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  };

  // ---- server change-log history --------------------------------------------
  function fetchServerHistory(force) {
    if (typeof GM_xmlhttpRequest !== 'function') return;
    if (serverHistPending) return;
    if (!force && Date.now() - serverHistAt < 180000) return;   // 3-min throttle
    serverHistPending = true;
    GM_xmlhttpRequest({
      method: 'GET',
      url: HISTORY_URL,
      onload: (r) => {
        serverHistPending = false;
        serverHistAt = Date.now();
        try {
          const j = JSON.parse(r.responseText);
          const map = new Map();
          (j.requested || []).forEach((row) => {
            const key = historyKey(String(row.carrier_id), row.origin_id, row.dest_group_id, row.capacity_date);
            if (!map.has(key)) map.set(key, []);
            map.get(key).push({ capacity: row.capacity, observed_at: row.observed_at, latest_request_date: row.latest_request_date });
          });
          serverHist = map;
          LOG('server history loaded', map.size, 'lane-days');
          scheduleRender();
        } catch (_) {}
      },
      onerror: () => { serverHistPending = false; serverHistAt = Date.now(); },
    });
  }
  // Timeline for one lane-day: prefer the shared server log, fall back to this
  // browser's local GM history so badges still work offline.
  function timelineFor(hk) {
    if (serverHist && serverHist.has(hk)) return serverHist.get(hk);
    const e = requestedHistory.entries[hk];
    if (!e) return [];
    const first = (e.changes && e.changes.length) ? e.changes[0].from : e.current;
    const tl = [{ capacity: first, observed_at: e.firstSeen, latest_request_date: null }];
    (e.changes || []).forEach((c) => tl.push({ capacity: c.to, observed_at: c.at, latest_request_date: null }));
    return tl;
  }

  // ---- lanes: join requestcapacity (names + asks) with confirmations --------
  function buildLanes() {
    const req = state.req && state.req.data;
    if (!req || !Array.isArray(req.locationRequests)) return null;
    const conf = state.conf && state.conf.data;
    const confIndex = new Map();
    (((conf || {}).locationCapacities) || []).forEach((location) => {
      (location.groupCapacities || []).forEach((group) => {
        (group.confirmCapacities || []).forEach((c) => {
          confIndex.set(location.originLocationId + '|' + group.destinationGroupId + '|' + dayKey(c.capacityDate),
            { capacity: c.capacity, scheduled: c.scheduled, isConflict: !!c.isConflict });
        });
      });
    });
    const carrierId = req.carrierId == null ? '' : String(req.carrierId);
    const origins = [];
    req.locationRequests.forEach((location) => {
      const lanes = [];
      (location.groupRequests || []).forEach((group) => {
        const reqByDay = new Map();
        (group.capacityRequests || []).forEach((r) => {
          reqByDay.set(dayKey(r.date), { capacity: r.capacity, latestRequestDate: r.latestRequestDate || null });
        });
        lanes.push({
          originId: location.originLocationId, groupId: group.destinationGroupId,
          name: (group.destinationGroupName || '').trim() || ('#' + group.destinationGroupId),
          reqByDay,
          confFor: (k) => confIndex.get(location.originLocationId + '|' + group.destinationGroupId + '|' + k),
        });
      });
      origins.push({
        id: location.originLocationId,
        name: (location.originLocationName || '').trim() || ('#' + location.originLocationId),
        isOriginGroup: !!location.isOriginGroup,
        lanes,
      });
    });
    return { carrierId, origins };
  }

  // Same lane page Tesla's own grid opens when a lane name is clicked (captured from the
  // native click handler: window.open('calendar-view/{originId}/{groupId}?isOriginGroup=…')).
  function laneUrl(origin, lane) {
    return location.origin + '/logistics/calendar-view/' + lane.originId + '/' + lane.groupId +
      '?isOriginGroup=' + origin.isOriginGroup;
  }

  // ---- panel shell -----------------------------------------------------------
  function ensurePanel() {
    if (host || !document.documentElement) return;
    host = document.createElement('div');
    host.id = 'cp-panel-host';
    host.style.cssText = 'z-index:2147483647;display:none;';
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        /* Tesla's own fonts are loaded by the page, so the shadow DOM can use them —
           the panel inherits the portal's type instead of looking like a foreign widget. */
        *{box-sizing:border-box;font-family:"Universal Sans Text",Inter,system-ui,Arial,sans-serif}
        .panel{position:relative;display:flex;flex-direction:column;height:100%;background:#fff;color:#393c41;border:0;border-radius:0;box-shadow:none;overflow:hidden}
        .tools{display:flex;align-items:center;gap:12px;padding:14px 24px 10px;background:#fff;border-bottom:1px solid #ececec;flex-wrap:wrap}
        .title{font-family:"Universal Sans Display","Universal Sans Text",Inter,sans-serif;font-size:18px;font-weight:500;color:#171a20}
        .chip{font-size:12px;font-weight:700;color:#3457d5;background:#eaf0ff;border-radius:10px;padding:2px 8px;white-space:nowrap}
        .chip.hot{color:#8a6d14;background:#fff3cd}
        .legend{font-size:12px;color:#5c5e62}
        .legend b{color:#171a20;font-weight:700}
        .grow{flex:1}
        .gridwrap{flex:1;overflow:auto;background:#fff;padding:0 24px 24px}
        /* fixed layout + colgroup widths: the date and Total columns are pinned narrow and
           the lane columns share the remaining width evenly, so the grid always fits a
           full-size browser window exactly — no horizontal scrolling */
        table{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;background:#fff;border:0}
        th,td{border-bottom:1px solid #ececec;border-right:1px solid #f2f2f2;padding:0;text-align:center;vertical-align:middle;overflow:hidden}
        th:last-child,td:last-child{border-right:0}
        /* two stacked sticky header rows: origin groups on top, lane names under */
        thead tr:first-child th{position:sticky;top:0;background:#fff;z-index:3;height:30px;padding:4px 6px}
        thead tr:nth-child(2) th{position:sticky;top:30px;background:#fff;z-index:3;border-bottom:1px solid #e2e2e2;padding:8px 4px}
        th.og{font-size:12px;font-weight:700;color:#171a20;background:#f7f7f7!important;border-bottom:1px solid #e2e2e2;white-space:nowrap;text-overflow:ellipsis}
        th.lh{font-size:12px;font-weight:600;color:#171a20;white-space:nowrap;text-overflow:ellipsis}
        .lanelink{color:inherit;text-decoration:none}
        .lanelink:hover{color:#3457d5;text-decoration:underline}
        th.lh.tot,td.tot{background:#fbfbfb}
        th.corner{z-index:5!important}
        th.lane,td.lane{position:sticky;left:0;background:#fff;z-index:2;text-align:center;padding:4px 6px;font-size:13px;font-weight:600;color:#171a20}
        /* thin blue outline around the whole current-day row: the cells' own borders are
           outside the shaded halves, so they stay visible; :has() colors the row above's
           bottom border to draw the outline's top edge without doubling lines */
        tr.today td{background:#f6f9ff}
        tr.today td.lane{background:#f6f9ff}
        tr:has(+ tr.today) td,tr:has(+ tr.today) th{border-bottom-color:#3457d5}
        tr.today td{border-bottom-color:#3457d5}
        tr.today td:first-child{border-left:1px solid #3457d5}
        tr.today td:last-child{border-right:1px solid #3457d5}
        tr.wksep td{background:#fafafa;color:#9a9da1;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;text-align:left;padding:4px 12px}
        /* the whole date cell is the Confirm Capacity control: the ENTIRE cell is light
           blue while the day still needs confirming; confirmed days keep a white date
           cell (the rest of the row grays out) */
        .datepill{display:inline-block;font-size:14px;font-weight:700;color:#171a20;white-space:nowrap}
        tr td.lane.confirmable{cursor:pointer;background:#d9e7fd}
        tr td.lane.confirmable:hover{background:#c8dcfb}
        tr.confirmed td{background:#ececec}
        tr.confirmed td.lane{background:#fff}
        tr.confirmed .datepill{color:#8d9096}
        tr.confirmed .bx{background:#e3e3e3;color:#8d9096}
        tr.confirmed .osum{color:#b0b3b7}
        .osum{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
        .osum.u{color:#c0392b}
        td.cell{padding:0;background:#fff;height:40px}
        td.tot{height:40px}
        tr.today td.cell{background:#eaf1ff}
        td.cell.zero .bx{color:#b7bac0}
        /* Cell split into two fully-shaded halves that fill the ENTIRE cell height:
           left = scheduled entry, right = requested. Base tints tell them apart;
           red = scheduled off requested, amber = requested changed (click to ack).
           Highlight text stays black — only the ▲/▼ ticker carries color. */
        .cw{display:flex;align-items:stretch;height:100%}
        .bx{flex:1;min-width:0;height:100%;border:0;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;text-align:center;color:#171a20}
        .bx-s{background:#eceef1;outline:0;padding:0 3px;border-radius:0}
        .bx-s::placeholder{color:inherit;opacity:.45}
        .bx-s:focus{box-shadow:inset 0 0 0 2px #3457d5}
        .bx-s.off{background:#f5c6cb;color:#171a20}
        .bx-r{position:relative;background:#e1e4e9;border-left:1px solid #d3d6dc;display:inline-flex;align-items:center;justify-content:center}
        .bx-r[data-hk]{cursor:help}
        .bx-r.chg{background:#ffe08a;color:#171a20;cursor:pointer}
        .bx-r.ackflash{animation:cpackpulse .5s ease}
        @keyframes cpackpulse{0%{background:#ffe08a;transform:scale(1)}40%{background:#ffd34d;transform:scale(1.1)}100%{background:#e1e4e9;transform:scale(1)}}
        /* single-direction ticker: pinned to the left edge, out of flow, so the
           requested number stays perfectly centered. Survives acknowledgement. */
        .tick{position:absolute;left:5px;top:50%;transform:translateY(-50%);font-size:10px;font-weight:700}
        .tick.up{color:#0a7d33}
        .tick.down{color:#c0392b}
        .empty,.loadwrap{display:flex;align-items:center;justify-content:center;height:100%;min-height:300px;color:#9a9da1;font-size:13px}
        .arc{width:46px;height:46px;animation:cparcspin .9s linear infinite}
        .arc circle{stroke:#b0b3b7}
        @keyframes cparcspin{to{transform:rotate(360deg)}}
        .pop{position:fixed;display:none;z-index:2147483647;width:236px;padding:11px 13px;background:#171a20;color:#fff;border:1px solid #34383f;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.3);font-size:13px;line-height:1.35;pointer-events:none}
        .pop .p-route{font-weight:650;margin-bottom:1px;font-size:12px}
        .pop .p-date{font-size:11px;color:#b9bec6;margin-bottom:8px}
        .pop .p-step{display:flex;align-items:center;gap:10px;padding:3px 0;border-top:1px solid #26292f}
        .pop .p-step:first-of-type{border-top:0}
        .pop .p-step b{font-size:16px;min-width:24px;text-align:right;font-variant-numeric:tabular-nums}
        .pop .p-step .up{color:#7ddb96}
        .pop .p-step .down{color:#ff9089}
        .pop .p-step span{color:#b9bec6;font-size:11px}
        .toast{position:absolute;left:50%;bottom:18px;transform:translateX(-50%) translateY(8px);background:#171a20;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;font-weight:800;letter-spacing:.02em;box-shadow:0 6px 20px rgba(0,0,0,.25);opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;z-index:6}
        .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
      </style>
      <div class="panel">
        <div class="tools">
          <div class="title">Capacity Planner</div>
          <div class="chip hot" id="chgchip" style="display:none"></div>
          <div class="grow"></div>
          <div class="legend"><b>scheduled</b> | <b>requested</b> — amber = requested changed (click to acknowledge) · red = scheduled off requested · hover requested for history</div>
        </div>
        <div class="gridwrap" id="gridwrap"></div>
        <div class="pop" id="pop"></div>
        <div class="toast" id="toast"></div>
      </div>`;
    document.documentElement.appendChild(host);

    root.addEventListener('click', (event) => {
      if (event.target.closest('td.lane.confirmable')) { panelToast('Confirm Capacity — not wired yet'); return; }
      // Acknowledge a requested change: amber half fades back to the base tint with a
      // pulse. Persisted per change timestamp, so a NEWER change re-ambers the box.
      const reqBox = event.target.closest('.bx-r.chg');
      if (reqBox && reqBox.dataset.stamp) {
        reqAck[reqBox.dataset.hk] = reqBox.dataset.stamp;
        saveReqAck();
        reqBox.classList.remove('chg');
        reqBox.classList.add('ackflash');   // the ▲/▼ ticker deliberately stays
        setTimeout(renderPanel, 600);   // solidify (and refresh the changes chip) after the pulse
        return;
      }
    });
    root.addEventListener('input', (event) => {
      const t = event.target;
      if (!(t.classList && t.classList.contains('bx-s'))) return;
      const hk = t.dataset.hk;
      const v = (t.value || '').trim();
      if (v === '') delete schedPlan[hk];
      else schedPlan[hk] = v;
      saveSchedPlan();
      // live re-shade while typing (no re-render, so focus/caret survive): red only
      // when the effective value (typed, else confirmed) is off requested
      const R = t.dataset.req === '' ? null : Number(t.dataset.req);
      const C = t.dataset.conf === '' ? null : Number(t.dataset.conf);
      const effective = v !== '' ? Number(v) : C;
      t.classList.toggle('off', R != null && !(effective != null && Number(effective) === R));
    });
    root.addEventListener('mouseover', (event) => {
      const target = event.target.closest && event.target.closest('.bx-r[data-hk]');
      if (target) showPanelPop(target);
    });
    root.addEventListener('mouseout', (event) => {
      const target = event.target.closest && event.target.closest('.bx-r[data-hk]');
      if (target && (!event.relatedTarget || !target.contains(event.relatedTarget))) hidePanelPop();
    });
    root.getElementById('gridwrap').addEventListener('scroll', hidePanelPop, true);
    window.addEventListener('resize', () => { if (isPanelVisible()) applyPanelPlacement(); });
    setInterval(() => { if (isPanelVisible()) { applyPanelPlacement(); fetchServerHistory(false); } }, 500);
  }
  function isPanelVisible() { return !!(host && host.style.display !== 'none'); }
  function panelToast(msg) {
    const t = root && root.getElementById('toast');
    if (!t) return;
    t.textContent = msg; t.classList.add('show');
    clearTimeout(panelToastTimer);
    panelToastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }

  // ---- render ----------------------------------------------------------------
  function scheduleRender() { clearTimeout(renderTimer); renderTimer = setTimeout(renderPanel, 60); }
  function renderPanel() {
    if (!root || !isPanelVisible()) return;
    const wrap = root.getElementById('gridwrap');
    const model = buildLanes();
    if (!model) {
      wrap.innerHTML = '<div class="loadwrap"><svg class="arc" viewBox="0 0 50 50"><circle cx="25" cy="25" r="20" fill="none" stroke-width="5" stroke-linecap="round" stroke-dasharray="94 32"/></svg></div>';
      return;
    }
    // Transposed grid: 14 date ROWS (Monday of this week through Sunday of next week),
    // one COLUMN per lane grouped under its origin, plus a per-origin Total column.
    const today = new Date();
    const todayKey = dateKeyFromLocal(today);
    const monday = mondayOf(today);
    const days = [];
    for (let i = 0; i < 14; i++) {
      const d = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i);
      days.push({ d, key: dateKeyFromLocal(d) });
    }
    const laneCount = model.origins.reduce((n, o) => n + o.lanes.length, 0);
    const totalCols = 1 + laneCount + model.origins.length;

    // colgroup pins the date + Total columns narrow; lane columns share the rest evenly
    // (table-layout:fixed), so the grid always fills the window width exactly.
    let html = '<table><colgroup><col style="width:104px">';
    model.origins.forEach((origin) => {
      origin.lanes.forEach(() => { html += '<col>'; });
      html += '<col style="width:64px">';
    });
    html += '</colgroup><thead><tr><th class="lane corner" rowspan="2">Date</th>';
    model.origins.forEach((origin) => {
      html += `<th class="og" colspan="${origin.lanes.length + 1}">${esc(origin.name)}</th>`;
    });
    html += '</tr><tr>';
    model.origins.forEach((origin) => {
      origin.lanes.forEach((lane) => {
        html += `<th class="lh"><a class="lanelink" href="${esc(laneUrl(origin, lane))}" target="_blank" rel="noopener">${esc(lane.name)}</a></th>`;
      });
      html += '<th class="lh tot">Total</th>';
    });
    html += '</tr></thead><tbody>';

    let changed48 = 0;
    const cutoff48 = Date.now() - 48 * 3600 * 1000;
    days.forEach(({ d, key }, di) => {
      if (di === 7) {
        const end = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 6);
        html += `<tr class="wksep"><td colspan="${totalCols}">Next Week · ${esc(fmtDay(d))} – ${esc(fmtDay(end))}</td></tr>`;
      }
      const isToday = key === todayKey;
      const dow = d.toLocaleDateString(undefined, { weekday: 'short' });
      // Heuristic until the real confirm flag is captured with the write path: a day
      // counts as confirmed when it is past, or when Tesla already returns confirmed
      // capacity for it. Confirmed rows gray out; the rest keep the blue date bubble
      // (the whole date cell is the dummy Confirm Capacity control).
      let dayConfirmedCapacity = 0;
      model.origins.forEach((origin) => origin.lanes.forEach((lane) => {
        const c = lane.confFor(key);
        dayConfirmedCapacity += (c && Number(c.capacity)) || 0;
      }));
      const confirmed = key < todayKey || dayConfirmedCapacity > 0;
      const confirmable = !confirmed;
      html += `<tr class="${isToday ? 'today' : ''}${confirmed ? ' confirmed' : ''}">`;
      html += `<td class="lane${confirmable ? ' confirmable' : ''}"${confirmable ? ` data-day="${esc(key)}" title="Confirm Capacity"` : ''}>` +
        `<span class="datepill">${esc(dow)} ${d.getDate()}</span></td>`;
      model.origins.forEach((origin) => {
        let sumR = 0, sumC = 0, sumS = 0;
        origin.lanes.forEach((lane) => {
          const r = lane.reqByDay.get(key); const c = lane.confFor(key);
          const R = r ? Number(r.capacity) : null;
          const C = c ? Number(c.capacity) : null;
          const S = c ? Number(c.scheduled) : null;
          sumR += R || 0; sumC += C || 0; sumS += S || 0;
          const hk = historyKey(model.carrierId, lane.originId, lane.groupId, key);
          const tl = timelineFor(hk);
          const isChanged = tl.length >= 2;
          const lastStamp = tl.length ? tl[tl.length - 1].observed_at : null;
          const acked = isChanged && reqAck[hk] === lastStamp;
          if (isChanged && !acked && new Date(lastStamp).getTime() >= cutoff48) changed48++;
          const zero = !(R || C || S);
          // Left half = scheduled entry; red only when it doesn't match requested.
          // Right half = requested; amber while a change is unacknowledged. The ▲/▼
          // ticker (green up / red down vs the previous value) is pinned to the left
          // edge and PERSISTS after the amber is acknowledged.
          const typed = (schedPlan[hk] || '').trim();
          const effective = typed !== '' ? Number(typed) : C;
          const off = !zero && R != null && !(effective != null && Number(effective) === R);
          let tick = '';
          if (isChanged) {
            const df = Number(tl[tl.length - 1].capacity) - Number(tl[tl.length - 2].capacity);
            tick = `<span class="tick ${df > 0 ? 'up' : 'down'}">${df > 0 ? '▲' : '▼'}</span>`;
          }
          html += `<td class="cell${zero ? ' zero' : ''}"><div class="cw">` +
            `<input class="bx bx-s${off ? ' off' : ''}" type="text" inputmode="numeric" data-hk="${esc(hk)}" data-req="${R == null ? '' : R}" data-conf="${C == null ? '' : C}" placeholder="${C == null ? '' : C}" value="${esc(typed)}">` +
            `<span class="bx bx-r${isChanged && !acked ? ' chg' : ''}"${tl.length ? ` data-hk="${esc(hk)}"` : ''}${lastStamp ? ` data-stamp="${esc(lastStamp)}"` : ''}>${tick}${R == null ? '–' : R}</span>` +
            `</div></td>`;
        });
        html += `<td class="tot"><div class="osum${sumC < sumR ? ' u' : ''}">${sumC} / ${sumR}</div></td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;

    const chip = root.getElementById('chgchip');
    if (changed48 > 0) {
      chip.style.display = '';
      chip.className = 'chip hot';
      chip.textContent = changed48 + ' requested change' + (changed48 === 1 ? '' : 's') + ' · 48h';
    } else {
      chip.style.display = 'none';
    }
  }

  // ---- history popover --------------------------------------------------------
  function laneLabelFromKey(hk) {
    const model = buildLanes();
    if (!model) return { route: '', date: '' };
    const parts = hk.split('|');
    for (const origin of model.origins) {
      for (const lane of origin.lanes) {
        if (String(lane.originId) === parts[1] && String(lane.groupId) === parts[2]) {
          return { route: origin.name + ' → ' + lane.name, date: parts[3] };
        }
      }
    }
    return { route: '', date: parts[3] || '' };
  }
  function showPanelPop(target) {
    const hk = target.dataset.hk;
    const tl = timelineFor(hk);
    if (!tl.length) return;   // hover works on ANY requested # with recorded history
    const pop = root.getElementById('pop');
    const { route, date } = laneLabelFromKey(hk);
    const d = parseDay(date);
    const dateText = isNaN(d) ? date : d.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
    // Vertical value column, most recent at the top, each with its date + time.
    // Direction color compares each value to the chronologically previous one.
    const rows = [...tl].reverse();
    let steps = '';
    rows.forEach((step, i) => {
      const prevChrono = i < rows.length - 1 ? Number(rows[i + 1].capacity) : null;
      const cls = prevChrono == null ? '' : (Number(step.capacity) > prevChrono ? 'up' : Number(step.capacity) < prevChrono ? 'down' : '');
      steps += `<div class="p-step"><b class="${cls}">${esc(step.capacity)}</b><span>${esc(fmtWhen(step.observed_at))}</span></div>`;
    });
    pop.innerHTML = `<div class="p-route">${esc(route)}</div><div class="p-date">${esc(dateText)}</div>` + steps;
    pop.style.display = 'block';
    const tr = target.getBoundingClientRect(), pr = pop.getBoundingClientRect();
    let left = tr.right + 10;
    if (left + pr.width > window.innerWidth - 8) left = tr.left - pr.width - 10;
    let top = Math.max(8, Math.min(tr.top - 8, window.innerHeight - pr.height - 8));
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = top + 'px';
  }
  function hidePanelPop() { const pop = root && root.getElementById('pop'); if (pop) pop.style.display = 'none'; }

  // ---- placement: splice into Tesla's layout (from bidboard, with the guard) --
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
  function embedPanel() {
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
    panelState.embedded = true;
    ensurePanelObserver(parent);
    return true;
  }
  function restorePanelContent() { if (hiddenEl && hiddenEl.style && hiddenEl.style.display === 'none') hiddenEl.style.display = ''; }
  function ensurePanelObserver(parent) {
    if (mo && observedParent === parent) return;
    if (mo) mo.disconnect();
    observedParent = parent;
    mo = new MutationObserver(() => {
      if (!ROUTE_RE.test(location.pathname)) return;
      if (moScheduled) return; moScheduled = true;
      setTimeout(() => { moScheduled = false; applyPanelPlacement(); }, 80);
    });
    mo.observe(parent, { childList: true });
  }
  function dockPanel() {
    const nav = document.querySelector('tsl-nav, nav.main-nav, [class*="main-nav"]');
    let left = 210, top = 56;
    if (nav) { const r = nav.getBoundingClientRect(); if (r.width > 40 && r.height > 200) { left = Math.max(0, Math.round(r.right)); top = Math.max(0, Math.round(r.top)); } }
    host.style.cssText = `position:fixed;z-index:2147483647;left:${left}px;top:${top}px;width:${Math.max(360, window.innerWidth - left)}px;height:${Math.max(240, window.innerHeight - top)}px;padding-left:${PANEL_GAP}px;box-sizing:border-box;`;
  }
  // The choke point is self-guarded (lesson from bidboard's panel leak): every caller
  // funnels through here, and off the planner route — or in native mode — this always
  // hides the panel and gives Tesla its content back instead of embedding.
  function applyPanelPlacement() {
    if (!host) return;
    if (!ROUTE_RE.test(location.pathname) || panelState.nativeMode) {
      host.style.display = 'none';
      restorePanelContent();
      if (host.parentElement && host.parentElement !== document.documentElement) document.documentElement.appendChild(host);
      return;
    }
    if (embedPanel()) return;
    panelState.embedded = false;
    dockPanel();
  }

  // ---- native-grid escape hatch: the bottom-right button ------------------------
  // Same recipe as the other extensions' bottom-right launcher (DD Recorder): small
  // dark pill, fixed bottom:12 right:12. Label flips with the mode — "Tesla grid"
  // while our panel is up, "Planner UI" while Tesla's native grid is showing.
  function ensurePill() {
    if (pill) return;
    pill = document.createElement('button');
    pill.id = 'cp-return-pill';
    pill.style.cssText = 'position:fixed;bottom:12px;right:12px;z-index:2147483647;display:none;' +
      'background:#111;color:#fff;font:12px/1.3 system-ui,Segoe UI,Arial,sans-serif;padding:6px 10px;' +
      'border:0;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.35);opacity:.92;cursor:pointer;transition:opacity .15s;';
    pill.addEventListener('mouseenter', () => { pill.style.opacity = '1'; });
    pill.addEventListener('mouseleave', () => { pill.style.opacity = '.92'; });
    pill.addEventListener('click', () => setNativeMode(!panelState.nativeMode));
    document.documentElement.appendChild(pill);
  }
  function setNativeMode(on) {
    panelState.nativeMode = !!on;
    panelSync();
  }

  // ---- show/hide with the route ----------------------------------------------
  function panelSync() {
    ensurePanel();
    ensurePill();
    const onRoute = ROUTE_RE.test(location.pathname);
    pill.style.display = onRoute ? '' : 'none';
    pill.textContent = panelState.nativeMode ? 'Planner UI' : 'Tesla grid';
    if (onRoute && !panelState.nativeMode) {
      applyPanelPlacement();
      host.style.display = '';
      fetchServerHistory(false);
      scheduleRender();
    } else {
      host.style.display = 'none';
      restorePanelContent();
      if (host.parentElement && host.parentElement !== document.documentElement) document.documentElement.appendChild(host);
      hidePanelPop();
    }
  }

  // ---- activate only on the Capacity Planner route --------------------------
  function applyRouteVisibility() {
    const on = ROUTE_RE.test(location.pathname);
    if (on) installRequestedTrackingUi();
    else hideRequestedTip();
    if (document.body) panelSync();
  }
  function setupNav() {
    ['pushState', 'replaceState'].forEach((m) => { const o = history[m]; history[m] = function () { const r = o.apply(this, arguments); applyRouteVisibility(); return r; }; });
    window.addEventListener('popstate', applyRouteVisibility);
    window.addEventListener('hashchange', applyRouteVisibility);
    let last = location.href;
    setInterval(() => { if (location.href !== last) { last = location.href; applyRouteVisibility(); } }, 300);
  }

  function boot() { setupNav(); applyRouteVisibility(); }
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot, { once: true });

  try {
    GM_registerMenuCommand('Clear Capacity Planner requested history', () => {
      if (!PAGE.confirm('Clear all locally recorded Requested-capacity history?')) return;
      requestedHistory = { version: 1, entries: {} };
      try { GM_deleteValue(REQUEST_HISTORY_KEY); } catch (_) {}
      hideRequestedTip();
      applyRequestedAnnotations();
    });
  } catch (_) {}

  try {
    GM_registerMenuCommand('Toggle Tesla native grid', () => setNativeMode(!panelState.nativeMode));
  } catch (_) {}

  PAGE.__capRequestedHistory = { state, requestedHistory: () => requestedHistory, applyRequestedAnnotations };
  PAGE.__capPanel = { panelState, renderPanel, panelSync, fetchServerHistory, buildLanes, setNativeMode };
  LOG('installed (read-only Tesla access + planner panel + server change log). Waiting for Capacity Planner data…');
})();
