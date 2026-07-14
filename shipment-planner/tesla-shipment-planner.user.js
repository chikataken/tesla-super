// ==UserScript==
// @name         Tesla Shipment Planner EU Filter
// @namespace    wastake.shipment-planner
// @version      0.2.0
// @description  Sorts NA-origin shipments first and hides EU-destination shipments on Shipment Planner's Available To Bid tab.
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
  let scheduled = false;

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

  function hookPlannerResponses() {
    const proto = XMLHttpRequest.prototype;
    const originalOpen = proto.open;

    proto.open = function (method, url, ...rest) {
      // Attach in open() so our listener registers before Angular's own load
      // handler (added between open() and send()) and reorders first.
      if (typeof url === 'string' && PLANNER_API.test(url)) {
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

  function availableToBidPanel() {
    const tab = Array.from(document.querySelectorAll('[role="tab"]')).find(
      (el) => (el.textContent || '').trim().toLowerCase() === 'available to bid'
    );
    if (!tab || tab.getAttribute('aria-selected') !== 'true') return null;

    const panelId = tab.getAttribute('aria-controls');
    return (panelId && document.getElementById(panelId)) || null;
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
      clearHiddenRows();
      return;
    }

    const panel = availableToBidPanel();
    if (!panel) {
      clearHiddenRows();
      return;
    }

    panel.querySelectorAll('tr[role="row"]').forEach((row) => {
      const destination = row.querySelector('.cdk-column-destination');
      if (!destination) return; // header and detail rows do not have a destination cell

      const destinationText = (destination.textContent || '').trim();
      const isEu = destinationText.slice(0, 2).toUpperCase() === 'EU';
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
