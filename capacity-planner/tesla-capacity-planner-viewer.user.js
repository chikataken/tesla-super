// ==UserScript==
// @name         Tesla Capacity Planner — Requested History
// @namespace    wastake.capacityplanner
// @version      0.3.0
// @description  Persistently tracks Tesla Requested-capacity changes. Changed / Y values are marked red directly on Capacity Planner and show their previous value and delta on hover. No launcher or popup panel.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
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

  const state = { req: null };

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

  // ---- capture requestcapacity ----------------------------------------------
  function classify(url) {
    return url && /requestcapacity/i.test(url) ? 'req' : null;
  }
  function ingest(text) {
    try {
      const j = JSON.parse(text);
      if (!j || !j.data) return;
      state.req = j;
      trackRequested(j);
      LOG('captured requestcapacity');
    } catch (_) {}
  }

  // fetch hook
  const oFetch = PAGE.fetch;
  if (oFetch) {
    PAGE.fetch = function (input, init) {
      const url = (input && input.url) || input;
      const name = classify(url);
      const p = oFetch.apply(this, arguments);
      if (name) p.then((r) => { r.clone().text().then(ingest).catch(() => {}); }).catch(() => {});
      return p;
    };
  }

  // XHR hook
  const X = PAGE.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send;
  X.prototype.open = function (m, u) { this.__cpHistoryUrl = u; return oOpen.apply(this, arguments); };
  X.prototype.send = function () {
    const xhr = this;
    if (classify(xhr.__cpHistoryUrl)) xhr.addEventListener('load', function () { try { ingest(xhr.responseText); } catch (_) {} });
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

  // ---- activate only on the Capacity Planner route --------------------------
  function applyRouteVisibility() {
    const on = ROUTE_RE.test(location.pathname);
    if (on) installRequestedTrackingUi();
    else hideRequestedTip();
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

  PAGE.__capRequestedHistory = { state, requestedHistory: () => requestedHistory, applyRequestedAnnotations };
  LOG('installed (read-only Tesla access + local Requested history). Waiting for Capacity Planner data…');
})();
