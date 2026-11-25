// ==UserScript==
// @name         Tesla Tender View — Ledger Overlay
// @namespace    wastake.tenderview
// @version      0.7.0
// @description  Retired. This version removes the "Tender View" button and overlay from the Tesla supplier portal and does nothing else. Safe to uninstall.
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/tender-view/tesla-tender-view.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/tender-view/tesla-tender-view.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * RETIRED (0.7.0). Tender View was dropped from this repo on 2026-09-16, but the public
 * repo kept serving 0.6.1, so every browser that had installed it kept the bottom-right
 * "Tender View" button. Tampermonkey never removes a script by itself: deleting the file
 * from the public repo would only stop updates and leave the button in place. The only
 * way to take it off installed browsers is a higher @version at the same @updateURL that
 * does nothing, which is this file.
 *
 * It keeps the old @name, @namespace, @updateURL and @match so Tampermonkey treats it as
 * the update of the installed script. It asks for no grants, hooks nothing on the page,
 * draws nothing and calls nothing.
 *
 * Once every browser has taken 0.7.0 (an update can take a few days to arrive), delete
 * tender-view/ from the public repo and from this one.
 */

(function () {
  'use strict';
  // the one thing older versions could leave on the Tesla origin: a hand-set API override
  try { localStorage.removeItem('tv_api'); } catch (_) {}
})();
