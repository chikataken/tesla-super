/* ============================================================================
 * Tesla Bid-Board — READ-ONLY diagnostic snippet
 * ----------------------------------------------------------------------------
 * Paste into the DevTools Console on the live bid page. It changes NOTHING:
 *   1. Counts VIN-like rows currently in the DOM and tries to find the total
 *      VIN/row count the page advertises  -> tells us if the list is VIRTUALIZED.
 *   2. Sketches the GROUP structure (which container holds the route groups,
 *      how many rows per group) so we know how groups are expressed in the DOM.
 *   3. Hooks fetch + XMLHttpRequest to log any response that looks like the
 *      bid list (contains VINs / route fields) -> captures the bid API endpoint
 *      + response shape. After pasting, just interact with the page normally
 *      (or reload the bid view) and watch the console for [BIDAPI] lines.
 *
 * Re-pasting is safe (guards against double-install).
 * To stop the network logging later: __bidDiag.uninstall()
 * ==========================================================================*/
(() => {
  const VIN_RE = /\b[A-HJ-NPR-Z0-9]{17}\b/g; // 17 chars, no I/O/Q -> VIN shape
  const looksLikeVin = (s) => typeof s === 'string' && /^[A-HJ-NPR-Z0-9]{17}$/.test(s) && /\d/.test(s) && /[A-Z]/.test(s);

  // ---- 1. DOM row / VIN count -------------------------------------------
  const bodyText = document.body ? document.body.innerText : '';
  const domVins = new Set((bodyText.match(VIN_RE) || []).filter(looksLikeVin));
  console.log('%c[DIAG] VIN-like strings visible in DOM text:', 'font-weight:bold', domVins.size);

  // Find the DOM elements that actually CONTAIN a VIN (leaf-most), call them "rows"
  const vinEls = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  while (walker.nextNode()) {
    const el = walker.currentNode;
    // only consider elements whose OWN text (excluding deep children) holds a VIN
    const ownText = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent)
      .join(' ');
    if (VIN_RE.test(ownText)) vinEls.push(el);
    VIN_RE.lastIndex = 0;
  }
  console.log('[DIAG] elements whose own text contains a VIN:', vinEls.length);

  // ---- 2. Total count the page advertises -------------------------------
  // Grab short text bits that pair a number with row-ish words.
  const countHints = [];
  const re = /(\d[\d,]*)\s*(vins?|vehicles?|results?|rows?|items?|bids?|loads?)\b/gi;
  let m;
  while ((m = re.exec(bodyText)) !== null) countHints.push(m[0].trim());
  console.log('[DIAG] page "N <thing>" count hints:', countHints.length ? countHints : '(none found — look manually)');

  // ---- 3. Group structure sketch ----------------------------------------
  // Walk each VIN element up to a plausible repeating "row", then bucket rows
  // by their parent container to reveal grouping.
  function plausibleRow(el) {
    let cur = el;
    for (let i = 0; i < 6 && cur && cur.parentElement; i++) {
      const sib = cur.parentElement.children.length;
      if (sib >= 2 && (cur.matches('tr,li,[role="row"]') || sib >= 3)) return cur;
      cur = cur.parentElement;
    }
    return el;
  }
  const rows = vinEls.map(plausibleRow);
  const byContainer = new Map();
  rows.forEach((r) => {
    const c = r.parentElement;
    if (!byContainer.has(c)) byContainer.set(c, []);
    byContainer.get(c).push(r);
  });
  console.log('[DIAG] distinct row-containers:', byContainer.size);
  let gi = 0;
  byContainer.forEach((rs, c) => {
    if (gi++ > 12) return;
    const desc = `${c.tagName.toLowerCase()}${c.id ? '#' + c.id : ''}${c.className && typeof c.className === 'string' ? '.' + c.className.trim().split(/\s+/).slice(0, 3).join('.') : ''}`;
    console.log(`   • container ${desc} -> ${rs.length} VIN rows`);
  });
  // Show one row's HTML so we can read its structure / the price + date controls
  if (rows[0]) {
    console.log('[DIAG] sample row.outerHTML (first VIN row):');
    console.log(rows[0].outerHTML.slice(0, 4000));
  }

  // ---- 4. Network hooks (capture the bid API) ---------------------------
  if (window.__bidDiag && window.__bidDiag.installed) {
    console.log('%c[DIAG] network hooks already installed; skipping reinstall.', 'color:#888');
  } else {
    const bodyLooksLikeBidList = (text) => {
      if (!text || text.length < 20) return false;
      const vins = text.match(VIN_RE);
      const hasVins = vins && vins.filter(looksLikeVin).length >= 1;
      const hasRoute = /\broute|origin|destination|lane|pickup|dropoff|deliver/i.test(text);
      return hasVins || hasRoute;
    };
    const summarize = (url, method, status, text) => {
      let shape = '(non-JSON)';
      let topKeys = [];
      try {
        const j = JSON.parse(text);
        const probe = Array.isArray(j) ? j[0] : (j.data && Array.isArray(j.data) ? j.data[0] : j);
        topKeys = probe && typeof probe === 'object' ? Object.keys(probe).slice(0, 40) : [];
        shape = Array.isArray(j) ? `array[${j.length}]` : `object{${Object.keys(j).slice(0, 12).join(',')}}`;
      } catch (_) {}
      const vinCount = (text.match(VIN_RE) || []).filter(looksLikeVin).length;
      console.log(`%c[BIDAPI] ${method} ${status} ${url}`, 'color:#0a0;font-weight:bold');
      console.log(`         shape=${shape}  VINs=${vinCount}  recordKeys=[${topKeys.join(', ')}]`);
      console.log('         body preview:', text.slice(0, 800));
      window.__bidDiag.captures.push({ url, method, status, shape, vinCount, recordKeys: topKeys, sample: text.slice(0, 4000) });
    };

    const origFetch = window.fetch;
    window.fetch = function (...args) {
      const req = args[0];
      const url = typeof req === 'string' ? req : (req && req.url) || '';
      const method = (args[1] && args[1].method) || (req && req.method) || 'GET';
      return origFetch.apply(this, args).then((resp) => {
        try {
          resp.clone().text().then((t) => { if (bodyLooksLikeBidList(t)) summarize(url, method, resp.status, t); }).catch(() => {});
        } catch (_) {}
        return resp;
      });
    };

    const OrigXHR = window.XMLHttpRequest;
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function (method, url) { this.__bd = { method, url }; return origOpen.apply(this, arguments); };
    OrigXHR.prototype.send = function () {
      this.addEventListener('load', function () {
        try {
          const t = this.responseType === '' || this.responseType === 'text' ? this.responseText : '';
          if (t && bodyLooksLikeBidList(t)) summarize(this.__bd.url, this.__bd.method, this.status, t);
        } catch (_) {}
      });
      return origSend.apply(this, arguments);
    };

    window.__bidDiag = {
      installed: true,
      captures: [],
      uninstall() { window.fetch = origFetch; OrigXHR.prototype.open = origOpen; OrigXHR.prototype.send = origSend; this.installed = false; console.log('[DIAG] network hooks removed.'); },
    };
    console.log('%c[DIAG] network hooks installed. Reload the bid view or interact with the page; watch for [BIDAPI] lines.', 'color:#06c;font-weight:bold');
  }

  console.log('%c[DIAG] done. If DOM VIN count << page total -> list is VIRTUALIZED. Inspect captures via __bidDiag.captures', 'color:#06c');
})();
