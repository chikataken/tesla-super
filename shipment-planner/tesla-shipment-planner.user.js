// ==UserScript==
// @name         Tesla Shipment Planner Helper
// @namespace    wastake.shipment-planner
// @version      0.4.0
// @description  Opens Available To Bid with 25 rows, forces the Ready Date window to ±2 weeks of today (rewritten in the request, no GUI), and hides EU-origin or EU-destination shipments in Tesla Shipment Planner.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/shipment-planner/tesla-shipment-planner.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const PLANNER_PATH = /\/logistics\/fv-shipment-planner\/review/i;
  const PLANNER_API = /\/TMS\/GetShipmentPlannerReviewDashboard/i;
  const HIDDEN_CLASS = 'tfi-shipment-planner-eu-hidden';
  const STYLE_ID = 'tfi-shipment-planner-eu-style';
  const DEFAULT_PAGE_SIZE = '25';
  const READY_DATE_DAYS = 14;   // Ready Date window = today ± this many days
  let scheduled = false;
  let plannerVisitActive = false;
  let defaultTabApplied = false;
  let pageSizeApplied = false;
  let pageSizeInFlight = false;

  // ---- NA-first sorting -------------------------------------------------
  // The planner endpoint returns EVERY shipment in one response and the table
  // pages/sorts client-side, so reordering the JSON before Angular sees it is
  // enough to put NA origins on the first pages. EU shipments keep their
  // relative order but move to the back (where the DOM filter hides them).

  function isEuShipment(shipment) {
    const origin = shipment && shipment.originLocation;
    const name = (origin && origin.locationName) || '';
    return name.trim().slice(0, 2).toUpperCase() === 'EU';
  }

  function sortNaFirst(payload) {
    if (!payload || !Array.isArray(payload.data)) return false;
    payload.data.sort((a, b) => isEuShipment(a) - isEuShipment(b)); // stable: NA first, EU last
    return true;
  }

  // ---- Ready Date window ------------------------------------------------
  // The planner POSTs readyDateFrom/readyDateTo in its JSON body and the server
  // filters on them, so widening the window to today ± READY_DATE_DAYS is done
  // purely by rewriting the request body — no calendar clicking. Applies to
  // every planner query (all four tabs), which keeps the range consistent.

  function readyDateWindow() {
    const now = new Date();
    now.setHours(0, 0, 0, 0);
    const from = new Date(now); from.setDate(from.getDate() - READY_DATE_DAYS);
    const to = new Date(now); to.setDate(to.getDate() + READY_DATE_DAYS);
    const pad = (n) => String(n).padStart(2, '0');
    // Match Tesla's format: calendar day at UTC-midnight / end-of-day (…Z).
    const iso = (d, end) =>
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      (end ? 'T23:59:59.000Z' : 'T00:00:00.000Z');
    return { from: iso(from, false), to: iso(to, true) };
  }

  function rewriteReadyDate(body) {
    if (typeof body !== 'string') return body; // only the JSON string form is rewritten
    try {
      const payload = JSON.parse(body);
      if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
        const win = readyDateWindow();
        payload.readyDateFrom = win.from;
        payload.readyDateTo = win.to;
        return JSON.stringify(payload);
      }
    } catch (e) {
      /* not JSON we understand — send unchanged */
    }
    return body;
  }

  function hookPlannerResponses() {
    const proto = XMLHttpRequest.prototype;
    const originalOpen = proto.open;
    const originalSend = proto.send;

    proto.open = function (method, url, ...rest) {
      // Flag planner requests in open() so send() can rewrite the body, and
      // register the response listener before Angular's own load handler
      // (added between open() and send()) so we reorder first.
      this.__tfiPlanner = typeof url === 'string' && PLANNER_API.test(url);
      if (this.__tfiPlanner) {
        this.addEventListener('readystatechange', function () {
          if (this.readyState !== 4 || this.status !== 200) return;
          try {
            if (this.responseType === 'json') {
              // Browser-parsed object is cached; in-place sort is visible to the page.
              sortNaFirst(this.response);
            } else {
              const payload = JSON.parse(this.responseText);
              if (sortNaFirst(payload)) {
                const text = JSON.stringify(payload);
                Object.defineProperty(this, 'responseText', { get: () => text });
                Object.defineProperty(this, 'response', { get: () => text });
              }
            }
          } catch (e) {
            /* leave the original response untouched */
          }
        });
      }
      return originalOpen.call(this, method, url, ...rest);
    };

    proto.send = function (body) {
      if (this.__tfiPlanner) body = rewriteReadyDate(body);
      return originalSend.call(this, body);
    };
  }

  function onShipmentPlanner() {
    return PLANNER_PATH.test(location.pathname);
  }

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `.${HIDDEN_CLASS}{display:none!important}`;
    (document.head || document.documentElement).appendChild(style);
  }

  function availableToBidTab() {
    return Array.from(document.querySelectorAll('[role="tab"]')).find(
      (el) => (el.textContent || '').trim().toLowerCase() === 'available to bid'
    );
  }

  function availableToBidPanel() {
    const tab = availableToBidTab();
    if (!tab || tab.getAttribute('aria-selected') !== 'true') return null;

    const panelId = tab.getAttribute('aria-controls');
    return (panelId && document.getElementById(panelId)) || null;
  }

  function resetVisitDefaults() {
    plannerVisitActive = false;
    defaultTabApplied = false;
    pageSizeApplied = false;
    pageSizeInFlight = false;
  }

  function ensureAvailableToBidDefault() {
    if (!plannerVisitActive) plannerVisitActive = true;
    const tab = availableToBidTab();
    if (!tab) return;

    // Apply once per visit so a later deliberate tab change is respected.
    if (!defaultTabApplied) {
      defaultTabApplied = true;
      if (tab.getAttribute('aria-selected') !== 'true') tab.click();
    }
  }

  // The "Show:" page-size control is a Tesla Design System <tds-dropdown-select>
  // (class .tds-pagination-page-size-select), NOT a native/mat/tsl select — so it
  // has to be opened and its option clicked. Options render in a .cdk-overlay-pane
  // .tds-select-panel as <tds-dropdown-option class="tds-option"> with a
  // .tds-option-text child. Page size is client-side only (no request param).
  const PAGE_SIZE_SELECT = 'tds-dropdown-select.tds-pagination-page-size-select, .tds-pagination-page-size-select';

  function showControl(panel) {
    return panel.querySelector(PAGE_SIZE_SELECT) || document.querySelector(PAGE_SIZE_SELECT);
  }

  function selectedPageSize(control) {
    const value = control.querySelector('.tds-select-value-text, .tds-select-value');
    const match = ((value && value.textContent) || control.textContent || '').trim().match(/\d+/);
    return match ? match[0] : '';
  }

  function choosePageSizeOption(attempt = 0) {
    if (!onShipmentPlanner() || !availableToBidPanel()) {
      pageSizeInFlight = false;
      return;
    }
    const options = Array.from(document.querySelectorAll(
      '.cdk-overlay-pane .tds-select-panel .tds-option, .cdk-overlay-pane tds-dropdown-option, .cdk-overlay-pane [role="option"]'
    ));
    const option = options.find((el) => {
      const text = el.querySelector('.tds-option-text');
      return ((text && text.textContent) || el.textContent || '').trim() === DEFAULT_PAGE_SIZE;
    });
    if (option) {
      option.click();
      pageSizeApplied = true;
      pageSizeInFlight = false;
      return;
    }
    if (attempt < 8) {
      setTimeout(() => choosePageSizeOption(attempt + 1), 100);
      return;
    }
    pageSizeInFlight = false;
  }

  function ensurePageSizeDefault(panel) {
    if (pageSizeApplied || pageSizeInFlight) return;
    const control = showControl(panel);
    if (!control) return;

    if (selectedPageSize(control) === DEFAULT_PAGE_SIZE) {
      pageSizeApplied = true;
      return;
    }

    pageSizeInFlight = true;
    const trigger = control.querySelector('.tds-select-trigger') || control;
    trigger.click();
    setTimeout(choosePageSizeOption, 80);
  }

  function setShipmentHidden(row, hidden) {
    row.classList.toggle(HIDDEN_CLASS, hidden);

    // Tesla renders a separate expansion/detail row immediately after every shipment.
    const detail = row.nextElementSibling;
    if (detail && detail.classList.contains('detail-row')) {
      detail.classList.toggle(HIDDEN_CLASS, hidden);
    }
  }

  function clearHiddenRows() {
    document.querySelectorAll(`.${HIDDEN_CLASS}`).forEach((el) => {
      el.classList.remove(HIDDEN_CLASS);
    });
  }

  function applyFilter() {
    scheduled = false;
    installStyle();

    if (!onShipmentPlanner()) {
      resetVisitDefaults();
      clearHiddenRows();
      return;
    }

    ensureAvailableToBidDefault();
    const panel = availableToBidPanel();
    if (!panel) {
      clearHiddenRows();
      return;
    }
    ensurePageSizeDefault(panel);

    panel.querySelectorAll('tr[role="row"]').forEach((row) => {
      const origin = row.querySelector('.cdk-column-origin, [class*="cdk-column-origin"]');
      const destination = row.querySelector('.cdk-column-destination');
      if (!destination) return; // header and detail rows do not have a destination cell

      const originText = (origin && origin.textContent || '').trim();
      const destinationText = (destination.textContent || '').trim();
      const isEu = originText.slice(0, 2).toUpperCase() === 'EU' ||
        destinationText.slice(0, 2).toUpperCase() === 'EU';
      setShipmentHidden(row, isEu);
    });
  }

  function scheduleFilter() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(applyFilter);
  }

  function hookHistory() {
    for (const method of ['pushState', 'replaceState']) {
      const original = history[method];
      history[method] = function (...args) {
        const result = original.apply(this, args);
        scheduleFilter();
        return result;
      };
    }
    addEventListener('popstate', scheduleFilter);
    addEventListener('hashchange', scheduleFilter);
  }

  function start() {
    hookPlannerResponses();
    installStyle();
    hookHistory();

    const observer = new MutationObserver(scheduleFilter);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['aria-selected'],
    });

    scheduleFilter();
  }

  if (document.documentElement) start();
  else addEventListener('DOMContentLoaded', start, { once: true });
})();
