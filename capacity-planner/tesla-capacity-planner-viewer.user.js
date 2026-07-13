// ==UserScript==
// @name         Tesla Capacity Planner — Viewer (read-only)
// @namespace    wastake.capacityplanner
// @version      0.1.0
// @description  Read-only popup for the Tesla Capacity Planner tab. Adds a launcher button; the popup shows EVERYTHING the page has — every origin → destination-group lane, each day's Confirmed / Scheduled / Requested capacity, conflict flags and last-requested time — in one formatted, scrollable grid. Nothing is editable; it only reads the portal's own two API responses (captured live).
// @author       wastake
// @updateURL    https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
// @downloadURL  https://raw.githubusercontent.com/chikataken/tesla-super/main/capacity-planner/tesla-capacity-planner-viewer.user.js
// @match        https://suppliers.teslamotors.com/logistics/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * READ-ONLY VIEWER — no writes, no editable fields. It hooks fetch/XHR at document-start and
 * captures the Capacity Planner tab's own two GET responses, then joins + formats them.
 *
 * Data model (see findings.md), both GET under …/api/v1/CapacityPlanner/carrier/ :
 *   getcapacityconfirmations -> data.locationCapacities[] = { originLocationId, isOriginGroup,
 *       groupCapacities[] = { destinationGroupId, confirmCapacities[] =
 *           { capacityDate, capacity (=Confirmed), scheduled, isConflict } } }
 *   requestcapacity          -> data.carrierName, locationRequests[] = { originLocationId,
 *       originLocationName, groupRequests[] = { destinationGroupId, destinationGroupName,
 *           capacityRequests[] = { date, capacity (=Requested), latestRequestDate } } }
 *   Join key = originLocationId + destinationGroupId + date. Names live only in requestcapacity.
 */

(function () {
  'use strict';
  const LOG = (...a) => console.log('%c[cap-viewer]', 'color:#3457d5;font-weight:bold', ...a);
  const ROUTE_RE = /\/logistics\/capacity-planner/i;

  const state = {
    conf: null,      // parsed getcapacityconfirmations JSON
    req: null,       // parsed requestcapacity JSON
    endpoints: {},   // name -> { url, headers } for Reload replay
    loading: false,
    error: null,
    filter: '',
    open: false,
  };

  // ---- 1) Capture the two GET responses (and their request headers) ---------
  function classify(url) {
    if (!url) return null;
    if (/getcapacityconfirmations/i.test(url)) return 'conf';
    if (/requestcapacity/i.test(url)) return 'req';
    return null;
  }
  function ingest(name, url, headers, text) {
    try {
      const j = JSON.parse(text);
      if (!j || !j.data) return;
      state[name] = j;
      state.endpoints[name] = { url, headers: headers || {} };
      LOG('captured', name);
      if (state.open) render();
      ensureLauncher();
    } catch (_) {}
  }

  // fetch hook
  const oFetch = window.fetch;
  if (oFetch) {
    window.fetch = function (input, init) {
      const url = (input && input.url) || input;
      const name = classify(url);
      const headers = name ? extractHeaders((init && init.headers) || (input && input.headers)) : null;
      const p = oFetch.apply(this, arguments);
      if (name) p.then((r) => { r.clone().text().then((t) => ingest(name, url, headers, t)).catch(() => {}); }).catch(() => {});
      return p;
    };
  }
  function extractHeaders(h) {
    const out = {};
    if (!h) return out;
    try {
      if (typeof Headers !== 'undefined' && h instanceof Headers) h.forEach((v, k) => { out[k] = v; });
      else if (Array.isArray(h)) h.forEach(([k, v]) => { out[k] = v; });
      else for (const k of Object.keys(h)) out[k] = h[k];
    } catch (_) {}
    return out;
  }

  // XHR hook
  const X = window.XMLHttpRequest;
  const oOpen = X.prototype.open, oSend = X.prototype.send, oSetH = X.prototype.setRequestHeader;
  X.prototype.open = function (m, u) { this.__cp = { url: u, headers: {} }; return oOpen.apply(this, arguments); };
  X.prototype.setRequestHeader = function (k, v) { if (this.__cp) this.__cp.headers[k] = v; return oSetH.apply(this, arguments); };
  X.prototype.send = function () {
    const xhr = this;
    if (xhr.__cp) {
      const name = classify(xhr.__cp.url);
      if (name) xhr.addEventListener('load', function () { try { ingest(name, xhr.__cp.url, xhr.__cp.headers, xhr.responseText); } catch (_) {} });
    }
    return oSend.apply(this, arguments);
  };

  // ---- 2) Reload = replay the captured GETs with their captured headers -----
  const HEADER_DENY = new Set(['cookie', 'content-length', 'host', 'connection', 'accept-encoding', 'user-agent']);
  function replayHeaders(h) { const out = { 'Accept': 'application/json' }; if (h) for (const k of Object.keys(h)) if (!HEADER_DENY.has(k.toLowerCase())) out[k] = h[k]; return out; }
  async function reload() {
    const names = Object.keys(state.endpoints);
    if (!names.length) { state.error = 'Nothing captured yet — open the Capacity Planner tab so its data loads, then reopen.'; render(); return; }
    state.loading = true; state.error = null; render();
    try {
      await Promise.all(names.map(async (name) => {
        const ep = state.endpoints[name];
        const r = await fetch(ep.url, { method: 'GET', headers: replayHeaders(ep.headers), credentials: 'omit' });
        if (!r.ok) throw new Error(name + ' HTTP ' + r.status);
        const j = await r.json();
        if (j && j.data) state[name] = j;
      }));
    } catch (e) { state.error = 'Reload failed (' + (e && e.message || e) + '). The passively-captured data is still shown; refresh the portal page for the latest.'; }
    state.loading = false; render();
  }

  // ---- helpers --------------------------------------------------------------
  const dayKey = (s) => String(s || '').slice(0, 10);            // "2026-07-10"
  function parseDay(k) { const p = k.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]); }   // local midnight
  const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  function fmtCol(k) { const d = parseDay(k); return { wd: WD[d.getDay()], md: (d.getMonth() + 1) + '/' + d.getDate(), we: d.getDay() === 0 || d.getDay() === 6 }; }
  const todayKey = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
  function fmtStamp(s) { if (!s) return ''; const d = new Date(s); return isNaN(d) ? String(s) : d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

  // ---- 3) Build the joined model -------------------------------------------
  // origins: [{ id, name, groups:[{ id, name, cells:{ dayKey:{c,s,r,conflict,lr} } }] }], dates:[dayKey…]
  function buildModel() {
    const req = state.req && state.req.data, conf = state.conf && state.conf.data;
    if (!req && !conf) return null;
    const dates = new Set();
    const originMap = new Map();   // id -> { id, name, groupMap: Map(id -> {id,name,cells}) }
    const getOrigin = (id, name) => {
      let o = originMap.get(id);
      if (!o) { o = { id, name: name || ('Origin ' + id), groupMap: new Map() }; originMap.set(id, o); }
      else if (name && (!o.name || /^Origin /.test(o.name))) o.name = name;
      return o;
    };
    const getGroup = (o, id, name) => {
      let g = o.groupMap.get(id);
      if (!g) { g = { id, name: name || ('Group ' + id), cells: {} }; o.groupMap.set(id, g); }
      else if (name && (!g.name || /^Group /.test(g.name))) g.name = name;
      return g;
    };

    // requests carry the names + Requested + last-requested time
    (req && req.locationRequests || []).forEach((L) => {
      const o = getOrigin(L.originLocationId, L.originLocationName);
      (L.groupRequests || []).forEach((G) => {
        const g = getGroup(o, G.destinationGroupId, G.destinationGroupName);
        (G.capacityRequests || []).forEach((c) => {
          const k = dayKey(c.date); dates.add(k);
          const cell = g.cells[k] || (g.cells[k] = {});
          cell.r = c.capacity; cell.lr = c.latestRequestDate;
        });
      });
    });
    // confirmations carry Confirmed + Scheduled + conflict flag
    (conf && conf.locationCapacities || []).forEach((L) => {
      const o = getOrigin(L.originLocationId, null);
      (L.groupCapacities || []).forEach((G) => {
        const g = getGroup(o, G.destinationGroupId, null);
        (G.confirmCapacities || []).forEach((c) => {
          const k = dayKey(c.capacityDate); dates.add(k);
          const cell = g.cells[k] || (g.cells[k] = {});
          cell.c = c.capacity; cell.s = c.scheduled; cell.conflict = !!c.isConflict;
        });
      });
    });

    const origins = [...originMap.values()].map((o) => ({ id: o.id, name: o.name, groups: [...o.groupMap.values()] }));
    return {
      carrierName: (req && req.carrierName) || (state.req && state.req.data && state.req.data.carrierName) || '',
      origins,
      dates: [...dates].sort(),
    };
  }

  // ---- 4) Panel -------------------------------------------------------------
  let host, root, body, launcher;

  function ensureLauncher() {
    if (launcher || !document.body) return;
    launcher = document.createElement('button');
    launcher.id = 'cap-launcher';
    launcher.textContent = 'Capacity Planner';
    // Matches the repo's bottom-right pill convention (see dispatch-dashboard .launch).
    launcher.style.cssText = 'position:fixed;bottom:12px;right:12px;z-index:2147483647;background:#111;color:#fff;font:12px/1.3 system-ui,Segoe UI,Arial,sans-serif;padding:6px 10px;border:0;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.35);opacity:.92;cursor:pointer;transition:opacity .15s;';
    launcher.addEventListener('mouseenter', () => launcher.style.opacity = '1');
    launcher.addEventListener('mouseleave', () => launcher.style.opacity = '.92');
    launcher.addEventListener('click', () => { state.open ? hidePanel() : showPanel(); });
    document.body.appendChild(launcher);
    applyRouteVisibility();
  }

  function showPanel() { ensurePanel(); state.open = true; host.style.display = ''; if (launcher) launcher.style.opacity = '1'; render(); }
  function hidePanel() { state.open = false; if (host) host.style.display = 'none'; if (launcher) launcher.style.opacity = '.92'; }

  function ensurePanel() {
    if (host || !document.documentElement) return;
    host = document.createElement('div');
    host.id = 'cap-viewer-host';
    const navW = 210, pw = Math.min(1320, window.innerWidth * 0.96);
    let left = navW + (window.innerWidth - navW - pw) / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    host.style.cssText = `position:fixed;top:52px;left:${Math.round(left)}px;width:${Math.round(pw)}px;height:86vh;z-index:2147483647;`;
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        *{box-sizing:border-box;font-family:Inter,system-ui,Arial,sans-serif}
        .panel{position:relative;display:flex;flex-direction:column;height:100%;background:#fff;color:#171a20;border:1px solid #d0d3d6;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.18);overflow:hidden}
        .hd{display:flex;align-items:center;gap:8px;padding:10px 12px;background:#171a20;color:#fff;cursor:move;user-select:none}
        .hd .ttl{font-weight:700;font-size:14px}.hd .sub{font-size:12px;opacity:.7;font-weight:500;flex:1}
        .hd .roflag{font-size:11px;font-weight:800;color:#7fd18b;letter-spacing:.06em;white-space:nowrap}
        .hd button{background:#2b2f37;color:#fff;border:0;border-radius:6px;padding:4px 9px;font-size:13px;cursor:pointer}.hd button:hover{background:#3a3f49}
        .tools{display:flex;align-items:center;gap:12px;padding:8px 12px;border-bottom:1px solid #eee;flex-wrap:wrap}
        .tools input{width:220px;padding:6px 8px;border:1px solid #d0d3d6;border-radius:6px;font-size:13px}
        .legend{display:flex;gap:14px;align-items:center;font-size:12px;color:#5c5e62;flex-wrap:wrap}
        .legend b{color:#171a20;font-variant-numeric:tabular-nums}
        .legend b.big{font-size:14px;font-weight:800}
        .legend i{font-style:normal;color:#8a8d92;margin-left:1px}
        .swatch{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:3px}
        .bodywrap{flex:1;overflow:auto;padding:0}
        table{border-collapse:separate;border-spacing:0;font-size:12px;width:max-content;min-width:100%}
        th,td{padding:4px 7px;border-bottom:1px solid #eef0f2;border-right:1px solid #f2f3f5;white-space:nowrap;text-align:center}
        thead th{position:sticky;top:0;background:#f6f7f9;z-index:3;font-weight:700;color:#5c5e62;border-bottom:1px solid #d8dbde}
        thead th.we{background:#eef1f6}
        thead th .md{font-weight:800;color:#171a20}
        thead th.today{background:#eaf0ff;color:#3457d5}
        th.lane,td.lane{position:sticky;left:0;background:#fff;z-index:2;text-align:left;min-width:150px;font-weight:600;border-right:1px solid #e2e4e7}
        thead th.lane{z-index:4}
        tr.orow td,tr.orow th{background:#171a20;color:#fff;font-weight:700;border-bottom:1px solid #171a20}
        tr.orow th.lane{background:#171a20;color:#fff;font-size:13px}
        tr.orow td .c{color:#fff}tr.orow td .sr{color:#c7cbd1}
        td.cell{font-variant-numeric:tabular-nums}
        td.cell.we{background:#fafbfc}
        td.cell.cf{background:#fff1f0}
        td.cell.cf .c{color:#c0392b}
        td.cell.empty{color:#c8cbcf}
        .c{display:block;font-size:14px;font-weight:800;line-height:1.1}
        .sr{display:block;font-size:10px;color:#8a8d92;line-height:1.15;margin-top:1px;white-space:nowrap}
        .sr i{font-style:normal;opacity:.55;margin-left:1px}
        td.lane.grp{padding-left:16px;font-weight:500;color:#33363b}
        .empty-msg{padding:26px;text-align:center;color:#9a9da1;font-size:13px}
        .empty-msg.err{color:#c0392b}
      </style>
      <div class="panel">
        <div class="hd" id="hd">
          <div class="ttl">Capacity Planner</div><div class="sub" id="sub"></div>
          <span class="roflag">● READ-ONLY</span>
          <button id="reload">Reload</button><button id="close">×</button>
        </div>
        <div class="tools">
          <input id="filter" placeholder="Filter lane / origin…" />
          <div class="legend">
            <span><b class="big">3</b> Confirmed — capacity you've committed</span>
            <span><b>3</b><i>s</i> Scheduled — loads actually booked</span>
            <span><b>3</b><i>r</i> Requested — capacity Tesla asked for</span>
            <span><span class="swatch" style="background:#fff1f0;border:1px solid #f3c4c0"></span>conflict (mismatch — needs review)</span>
            <span><span class="swatch" style="background:#eef1f6;border:1px solid #dfe3ea"></span>weekend</span>
          </div>
        </div>
        <div class="bodywrap" id="bodywrap"></div>
      </div>`;
    document.documentElement.appendChild(host);
    body = { sub: root.getElementById('sub'), wrap: root.getElementById('bodywrap'), filter: root.getElementById('filter') };
    root.getElementById('close').addEventListener('click', hidePanel);
    root.getElementById('reload').addEventListener('click', reload);
    body.filter.addEventListener('input', () => { state.filter = body.filter.value.trim().toLowerCase(); render(); });
    makeDraggable(root.getElementById('hd'), host);
  }

  function makeDraggable(handle, target) {
    let sx, sy, ox, oy, drag = false;
    handle.addEventListener('mousedown', (e) => { if (e.target.tagName === 'BUTTON') return; drag = true; sx = e.clientX; sy = e.clientY; const r = target.getBoundingClientRect(); ox = r.left; oy = r.top; target.style.left = ox + 'px'; target.style.top = oy + 'px'; e.preventDefault(); });
    window.addEventListener('mousemove', (e) => { if (!drag) return; target.style.left = (ox + e.clientX - sx) + 'px'; target.style.top = (oy + e.clientY - sy) + 'px'; });
    window.addEventListener('mouseup', () => { drag = false; });
  }

  // ---- 5) Render ------------------------------------------------------------
  function cellHtml(cell, we) {
    if (!cell || (cell.c == null && cell.s == null && cell.r == null)) return `<td class="cell empty${we ? ' we' : ''}">·</td>`;
    const c = cell.c != null ? cell.c : 0, s = cell.s != null ? cell.s : 0, r = cell.r != null ? cell.r : 0;
    const cls = 'cell' + (cell.conflict ? ' cf' : '') + (we ? ' we' : '');
    const title = `Confirmed ${c} · Scheduled ${s} · Requested ${r}` + (cell.lr ? ` · last requested ${fmtStamp(cell.lr)}` : '') + (cell.conflict ? ' · CONFLICT' : '');
    return `<td class="${cls}" title="${esc(title)}"><span class="c">${c}</span><span class="sr">${s}<i>s</i> ${r}<i>r</i></span></td>`;
  }
  function originSummaryCell(groups, k, we) {
    let c = 0, s = 0, r = 0, has = false;
    groups.forEach((g) => { const cell = g.cells[k]; if (cell) { has = true; if (cell.c != null) c += cell.c; if (cell.s != null) s += cell.s; if (cell.r != null) r += cell.r; } });
    if (!has) return `<td class="${we ? 'we' : ''}">·</td>`;
    return `<td class="${we ? 'we' : ''}"><span class="c">${c}</span><span class="sr">${s}<i>s</i> ${r}<i>r</i></span></td>`;
  }

  function render() {
    if (!root) return;
    const model = buildModel();
    if (state.error) { body.sub.textContent = ''; body.wrap.innerHTML = `<div class="empty-msg err">${esc(state.error)}</div>`; return; }
    if (state.loading) { body.sub.textContent = 'reloading…'; }
    if (!model) { body.sub.textContent = ''; body.wrap.innerHTML = `<div class="empty-msg">Waiting for the Capacity Planner data to load…<br>(open / refresh the Capacity Planner tab)</div>`; return; }

    const tk = todayKey();
    let origins = model.origins;
    if (state.filter) {
      const f = state.filter;
      origins = origins.map((o) => {
        const oMatch = o.name.toLowerCase().includes(f);
        const groups = oMatch ? o.groups : o.groups.filter((g) => g.name.toLowerCase().includes(f));
        return groups.length ? { ...o, groups } : null;
      }).filter(Boolean);
    }

    const nOrigins = model.origins.length;
    const nGroups = model.origins.reduce((s, o) => s + o.groups.length, 0);
    body.sub.textContent = `· ${model.carrierName || ''} · ${nOrigins} origin${nOrigins === 1 ? '' : 's'} · ${nGroups} lanes · ${model.dates.length} days`;

    if (!origins.length) { body.wrap.innerHTML = `<div class="empty-msg">No lane matches “${esc(state.filter)}”.</div>`; return; }

    const cols = model.dates.map(fmtCol);
    let thead = `<tr><th class="lane">Lane</th>` + model.dates.map((k, i) => {
      const c = cols[i], today = k === tk;
      return `<th class="${c.we ? 'we' : ''}${today ? ' today' : ''}"><div>${c.wd}</div><div class="md">${c.md}</div></th>`;
    }).join('') + `</tr>`;

    let rows = '';
    origins.forEach((o) => {
      // origin roll-up row
      rows += `<tr class="orow"><th class="lane">${esc(o.name)}</th>` + model.dates.map((k, i) => originSummaryCell(o.groups, k, cols[i].we)).join('') + `</tr>`;
      // one row per destination group
      o.groups.forEach((g) => {
        rows += `<tr><td class="lane grp">${esc(g.name)}</td>` + model.dates.map((k, i) => cellHtml(g.cells[k], cols[i].we)).join('') + `</tr>`;
      });
    });

    body.wrap.innerHTML = `<table><thead>${thead}</thead><tbody>${rows}</tbody></table>`;
  }

  // ---- 6) Show launcher/panel only on the Capacity Planner route ------------
  function applyRouteVisibility() {
    const on = ROUTE_RE.test(location.pathname);
    if (launcher) launcher.style.display = on ? '' : 'none';
    if (!on && state.open) hidePanel();
  }
  function setupNav() {
    ['pushState', 'replaceState'].forEach((m) => { const o = history[m]; history[m] = function () { const r = o.apply(this, arguments); applyRouteVisibility(); return r; }; });
    window.addEventListener('popstate', applyRouteVisibility);
    window.addEventListener('hashchange', applyRouteVisibility);
    let last = location.href;
    setInterval(() => { if (location.href !== last) { last = location.href; applyRouteVisibility(); } }, 300);
  }

  function boot() { ensureLauncher(); setupNav(); }
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot, { once: true });

  window.__capViewer = { state, render, reload, buildModel };
  LOG('installed (read-only). Waiting for Capacity Planner data…');
})();
