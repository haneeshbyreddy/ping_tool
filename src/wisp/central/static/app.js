"use strict";
// WISP Central console — vanilla JS, no build step, no deps. Talks to the same JSON
// API the CLI/curl use; the session cookie is sent automatically. Visual language is
// ported from the old single-box edge dashboard (dark Material-ish Tailwind, icons,
// toasts, live SSE push) — see CLAUDE.md's "Removed" section for why that dashboard
// itself was retired; only its LOOK is being restored here, rewired against central's
// tenant-scoped API.

const $ = (sel, el = document) => el.querySelector(sel);
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function icon(name, opts) {
  opts = opts || {};
  const size = opts.size || 20;
  const cls = opts.cls || "";
  const p = (window.ICON_PATHS || {})[name] || "";
  return `<svg class="ic ${cls}" style="width:${size}px;height:${size}px" ` +
    `viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">${p}</svg>`;
}

let _toastTimer = null;
function toast(msg, kind) {
  let t = $("#toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.className = "fixed z-[60] bottom-6 left-1/2 -translate-x-1/2 px-4 py-2 rounded-md " +
      "text-label-md font-label-md shadow-lg transition-opacity";
    document.body.appendChild(t);
  }
  const ok = kind !== "error";
  t.className = t.className.replace(/ bg-\S+| text-\S+/g, "");
  t.classList.add(ok ? "bg-primary" : "bg-error", ok ? "text-surface" : "text-on-error");
  t.textContent = msg;
  t.style.opacity = "1";
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.style.opacity = "0"; }, 2600);
}

// Kept in lockstep with central/inventory.py DEVICE_TYPES (and the edge's SPA dropdown).
const DEVICE_TYPES = ["core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul"];

let ME = null;             // current user
let TENANT = "";           // superadmin's selected org ("" = all); org users ignore this
let PAGE = "overview";     // overview | nodes | agents | team | settings
let NODE_EDIT = null;      // the org_devices row currently being edited on the Nodes page, or null
let AGENT_REVEAL = null;   // {node_id, token} shown once right after register/rotate, or null
let SUMMARY = null;        // last {uplink_down, low_bandwidth} fetch, or null (no tenant scoped)
let _seenLowBw = null;     // port_ids already low-bandwidth (null until first paint, so a
                            // reload doesn't toast every standing alarm)
let _events = null;        // EventSource (server push), rebuilt whenever the tenant scope changes

async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { ME = null; renderLogin(); throw new Error("unauthorized"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : {};
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// tenant the views should query: superadmin uses the picker, an org user is pinned server-side.
const scope = () => ME.is_superadmin ? TENANT : ME.tenant_id;
const q = () => (ME.is_superadmin && TENANT) ? `?tenant=${encodeURIComponent(TENANT)}` : "";
const tq = (t) => q() || `?tenant=${encodeURIComponent(t)}`;
const canWrite = () => ME.is_superadmin || ME.role === "owner";

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
  if (s < 90) return `${s | 0}s ago`;
  if (s < 5400) return `${(s / 60) | 0}m ago`;
  return `${(s / 3600) | 0}h ago`;
}

function fmtMbps(n) { return n == null ? "—" : `${n} Mbps`; }
function fmtPct(n) { return n == null ? "—" : `${Number(n).toFixed(1)}%`; }

// Stored stamps are UTC: ISO with +00:00 (polls/outages), so a naive-looking string
// still parses correctly everywhere but Safari — this normalises it first.
function toUtcDate(ts) {
  let s = String(ts).trim().replace(" ", "T");
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";
  return new Date(s);
}

function fmtDur(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const hh = Math.floor(seconds / 3600);
  const mm = Math.floor((seconds % 3600) / 60);
  const ss = seconds % 60;
  if (hh) return `${hh}h ${mm}m`;
  if (mm) return `${mm}m ${ss}s`;
  return `${ss}s`;
}

// Repaint every [data-since] element so durations tick up client-side between refetches.
function tickDurations() {
  const now = Date.now();
  document.querySelectorAll("[data-since]").forEach((el) => {
    const started = toUtcDate(el.getAttribute("data-since"));
    if (isNaN(started)) return;
    el.textContent = fmtDur((now - started.getTime()) / 1000);
  });
}
setInterval(tickDurations, 1000);

// --- small visual primitives, shared by every page --------------------------------
function card(inner, cls = "") {
  return `<div class="border border-outline-variant bg-surface-container-low rounded-md p-4 md:p-5 animate-fade-in ${cls}">${inner}</div>`;
}

function sectionHeader(title, iconName, right = "") {
  return `<div class="flex items-center justify-between mb-4">
    <div class="flex items-center gap-2">${icon(iconName, { size: 20, cls: "text-primary" })}
      <h2 class="font-headline-md text-headline-md text-primary">${esc(title)}</h2></div>
    ${right}</div>`;
}

const PILL_TONES = {
  ok: "text-emerald-400 border-emerald-400/30 bg-emerald-400/10",
  down: "text-error border-error/30 bg-error/10",
  warn: "text-amber-400 border-amber-400/30 bg-amber-400/10",
  muted: "text-on-surface-variant border-outline-variant bg-surface-container",
};
function pill(text, tone) {
  return `<span class="font-label-xs text-label-xs ${PILL_TONES[tone] || PILL_TONES.muted} border px-2 py-0.5 rounded-md whitespace-nowrap">${esc(text)}</span>`;
}
const STATE_TONE = { UP: "ok", DOWN: "down", UNREACHABLE: "down", DEGRADED: "warn" };

const TH = "text-left font-label-xs text-label-xs text-on-surface-variant uppercase tracking-wider py-2 px-3 border-b border-outline-variant";
const TD = "py-2 px-3 border-b border-outline-variant/50 align-middle";
const BTN = "px-3 py-1.5 rounded-md bg-primary text-surface font-label-md text-label-md hover:opacity-90 active:scale-95 transition-transform disabled:opacity-40";
const GHOST = "px-3 py-1.5 rounded-md border border-outline-variant text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface font-label-md text-label-md active:scale-95 transition-transform";
const FIELD = "bg-surface border border-outline-variant rounded-md px-3 py-1.5 text-on-surface font-body-sm text-body-sm placeholder:text-outline focus:outline-none focus:border-primary";

function loading(label) {
  return `<div class="flex items-center justify-center gap-3 text-on-surface-variant py-16">
    ${icon("refresh", { size: 20, cls: "text-outline" })}
    <span class="font-body-sm text-body-sm">Loading ${esc(label)}…</span></div>`;
}

// --- boot / login -------------------------------------------------------------------
async function boot() {
  try { const me = await api("/api/me"); ME = me.user; renderApp(); }
  catch { /* renderLogin already called on 401 */ }
}

function renderLogin(err = "") {
  document.body.querySelector("#app").replaceChildren(h(`
    <div class="min-h-screen flex items-center justify-center px-4">
      <div class="w-full max-w-sm border border-outline-variant bg-surface-container-low rounded-md p-6 animate-fade-in">
        <div class="flex items-center gap-2 mb-6">${icon("hub", { size: 26, cls: "text-primary" })}
          <h1 class="font-headline-lg text-headline-lg font-bold tracking-tighter text-primary">WISP Central</h1></div>
        <div class="flex flex-col gap-3">
          <input id="u" placeholder="username" autocomplete="username" class="${FIELD}">
          <input id="p" type="password" placeholder="password" autocomplete="current-password" class="${FIELD}">
          <button id="go" class="${BTN} w-full py-2">Sign in</button>
          <div class="font-label-xs text-label-xs text-error min-h-[14px]">${esc(err)}</div>
        </div>
      </div>
    </div>`));
  const submit = async () => {
    try {
      const r = await api("/api/login", "POST", { username: $("#u").value, password: $("#p").value });
      ME = r.user; renderApp();
    } catch (e) { renderLogin(e.message); }
  };
  $("#go").onclick = submit;
  $("#p").onkeydown = (e) => { if (e.key === "Enter") submit(); };
}

// --- shell ----------------------------------------------------------------
const PAGES = [
  ["overview", "Overview", "dashboard"],
  ["nodes", "Nodes", "router"],
  ["agents", "Edge Nodes", "cell_tower"],
  ["team", "Team", "group"],
  ["settings", "Settings", "settings"],
];

function updateUplinkChip(down) {
  const chip = $("#uplink-chip");
  if (!chip) return;
  if (down) {
    chip.className = "font-label-xs text-label-xs text-error border border-error/30 bg-error/10 px-2 py-1 rounded-md flex items-center gap-1";
    chip.innerHTML = `${icon("warning", { size: 14 })} UPLINK DOWN`;
  } else {
    chip.className = "hidden";
    chip.innerHTML = "";
  }
}

function updateLowBwChip(list) {
  const chip = $("#lowbw-chip");
  if (!chip) return;
  const n = (list || []).length;
  if (n) {
    chip.className = "font-label-xs text-label-xs text-amber-400 border border-amber-400/30 bg-amber-400/10 px-2 py-1 rounded-md flex items-center gap-1 cursor-pointer hover:bg-amber-400/20";
    chip.innerHTML = `${icon("arrow_downward", { size: 14 })} ${n} LOW BW`;
  } else {
    chip.className = "hidden";
    chip.innerHTML = "";
  }
}

function renderApp() {
  const orgLabel = ME.is_superadmin ? "Superadmin" : `${esc(ME.tenant_id)} · ${esc(ME.role)}`;
  const app = document.body.querySelector("#app");
  app.replaceChildren(h(`
    <div class="flex flex-col min-h-screen w-full">
      <header class="w-full sticky top-0 bg-background border-b border-outline-variant flex justify-between items-center px-container-margin py-component-padding-y z-40 h-[57px]">
        <a href="#" class="flex items-center gap-2 cursor-pointer active:opacity-80" id="brand">
          ${icon("hub", { size: 26, cls: "text-primary" })}
          <h1 class="font-headline-lg text-headline-lg font-bold tracking-tighter text-primary">WISP Central</h1></a>
        <span id="orgpick" class="ml-4"></span>
        <span class="flex-1"></span>
        <div class="flex items-center gap-2 text-on-surface-variant">
          <a href="#" id="lowbw-chip" class="hidden"></a>
          <span id="uplink-chip" class="hidden"></span>
          <span class="font-label-xs text-label-xs text-on-surface-variant hidden md:inline">${esc(ME.username)} · ${orgLabel}</span>
          <button id="logout" class="${GHOST} flex items-center gap-1">${icon("logout", { size: 16 })}<span class="hidden md:inline">Sign out</span></button>
        </div>
      </header>
      <nav class="flex gap-1 px-container-margin pt-3 overflow-x-auto" id="tabs">${PAGES.map(([key, label, ic]) =>
        `<button data-page="${key}" class="flex items-center gap-1.5 px-3 py-2 rounded-md font-label-md text-label-md whitespace-nowrap transition-colors ${
          PAGE === key ? "bg-surface-container-low text-primary" : "text-on-surface-variant hover:bg-surface-container"
        }">${icon(ic, { size: 16 })}${esc(label)}</button>`).join("")}
      </nav>
      <main class="flex-1 min-w-0 flex flex-col gap-4 p-container-margin max-w-[1400px] w-full mx-auto" id="main"></main>
    </div>`));
  $("#logout").onclick = async () => { await api("/api/logout", "POST", {}); ME = null; if (_events) { _events.close(); _events = null; } renderLogin(); };
  $("#brand").onclick = (e) => { e.preventDefault(); PAGE = "overview"; renderApp(); };
  $("#tabs").querySelectorAll("[data-page]").forEach(b => b.onclick = () => {
    PAGE = b.dataset.page; NODE_EDIT = null; AGENT_REVEAL = null; renderApp();
  });
  if (ME.is_superadmin) renderOrgPicker(); else connectEvents();
  refresh();
}

async function renderOrgPicker() {
  const { orgs } = await api("/api/orgs");
  const sel = h(`<select class="${FIELD}"><option value="">All orgs</option>${
    orgs.map(o => `<option value="${esc(o.tenant_id)}">${esc(o.tenant_id)} (${o.node_count} nodes)</option>`).join("")
  }</select>`);
  sel.value = TENANT;
  sel.onchange = () => { TENANT = sel.value; NODE_EDIT = null; AGENT_REVEAL = null; connectEvents(); refresh(); };
  $("#orgpick").replaceChildren(sel);
  connectEvents();
}

// Live push: refetch the current page (and the header chips) the instant central's data
// changes, instead of waiting on a manual reload. Rebuilt whenever the tenant scope
// changes since the SSE fingerprint itself is tenant-scoped server-side.
function connectEvents() {
  if (_events) { _events.close(); _events = null; }
  try {
    const es = new EventSource(`/api/events${q()}`);
    es.addEventListener("changed", () => refresh());
    _events = es;
  } catch { /* EventSource unsupported/blocked — the app still works, just not live */ }
}

async function refreshSummary() {
  const t = scope();
  if (!t) { SUMMARY = null; updateUplinkChip(false); updateLowBwChip([]); return; }
  try {
    SUMMARY = await api(`/api/summary${tq(t)}`);
  } catch { SUMMARY = null; }
  const low = (SUMMARY && SUMMARY.low_bandwidth) || [];
  updateUplinkChip(SUMMARY ? SUMMARY.uplink_down : false);
  updateLowBwChip(low);
  const seen = new Set(low.map((p) => p.port_id));
  if (_seenLowBw) {
    for (const p of low) {
      if (!_seenLowBw.has(p.port_id)) toast(`Low bandwidth — ${p.switch_name} · ${p.label}`, "error");
    }
  }
  _seenLowBw = seen;
}

async function refresh() {
  const main = $("#main");
  main.replaceChildren(h(`<div>${loading("dashboard")}</div>`));
  try {
    await refreshSummary();
    if (PAGE === "nodes") await renderNodesPage(main);
    else if (PAGE === "agents") await renderAgentsPage(main);
    else if (PAGE === "team") await renderTeamPage(main);
    else if (PAGE === "settings") await renderSettingsPage(main);
    else await renderOverviewPage(main);
  } catch (e) {
    main.replaceChildren(h(`<div class="border border-error/30 bg-error/5 rounded-md p-4 flex items-center gap-3 text-error">
      ${icon("warning")}<span class="font-body-sm text-body-sm">Couldn't load data: ${esc(e.message)}</span></div>`));
  }
}

function needsOrgCard() {
  return h(card(`<span class="font-body-sm text-body-sm text-on-surface-variant">Select an org above to manage it.</span>`));
}

// --- Overview page: fleet health, the edge-ingest device registry, recent events -----
async function renderOverviewPage(main) {
  const t = scope();
  const fleet = await api(`/api/fleet${q()}`);
  const { devices } = await api(`/api/devices${q()}`);
  const children = [];
  if (t) children.push(statsCard(fleet.nodes, SUMMARY));
  if (t && SUMMARY && SUMMARY.low_bandwidth.length) children.push(lowBandwidthCard(SUMMARY.low_bandwidth));
  children.push(nodesCard(fleet.nodes), devicesCard(devices), eventsCard(fleet.recent_events));
  main.replaceChildren(...children);
}

function statsCard(nodes, summary) {
  const stale = (n) => (Date.now() - Date.parse(n.last_seen)) / 1000 > 180;
  const active = nodes.filter((n) => n.last_seen && !stale(n)).length;
  const outages = nodes.reduce((sum, n) => sum + (n.open_outages || 0), 0);
  const uplink = summary && summary.uplink_down ? pill("UPLINK DOWN", "down") : "";
  return h(`<div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="col-span-2 border border-outline-variant bg-surface-container-low rounded-md p-4 flex flex-col justify-between">
      <div class="flex justify-between items-start mb-2">
        <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">Edge Nodes</span>
        ${icon("shield", { size: 16, cls: "text-primary" })}
      </div>
      <div class="flex items-baseline gap-1">
        <span class="font-display text-display text-primary">${active}</span>
        <span class="font-mono-data text-mono-data text-on-surface-variant">/${nodes.length} online</span>
      </div>
    </div>
    <div class="border border-outline-variant bg-surface rounded-md p-4 flex flex-col justify-between">
      <div class="flex items-center gap-2 mb-2">
        <span class="flex h-2 w-2 rounded-full ${outages ? "bg-error" : "bg-outline"}"></span>
        <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">Outages</span>
      </div>
      <span class="font-headline-lg text-headline-lg ${outages ? "text-error" : "text-primary"}">${outages}</span>
    </div>
    <div class="border border-outline-variant bg-surface rounded-md p-4 flex flex-col justify-between">
      <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-2">Uplink</span>
      <div>${uplink || pill("OK", "ok")}</div>
    </div>
  </div>`);
}

// The dashboard "Low Bandwidth" card: shown only when monitored ports are below limit.
function lowBandwidthCard(list) {
  const rows = list.map((p) => {
    const dir = p.direction && p.direction !== "either" ? ` ${esc(p.direction)}` : "";
    const limit = p.threshold_mbps != null ? `limit ${p.threshold_mbps} Mbps${dir}` : "";
    const since = p.since ? ` · low for <span data-since="${esc(p.since)}">—</span>` : "";
    return `<div class="flex items-center justify-between gap-3 py-2 border-t border-amber-400/15 first:border-t-0 first:pt-0">
      <div class="min-w-0">
        <p class="font-label-md text-label-md text-primary truncate">${esc(p.switch_name)} <span class="text-on-surface-variant">· ${esc(p.label)}</span></p>
        <p class="font-mono-data text-[11px] text-on-surface-variant">↓ ${fmtMbps(p.in_mbps)} · ↑ ${fmtMbps(p.out_mbps)} <span class="text-outline">(${limit})</span>${since}</p>
      </div>
      <span class="font-label-xs text-label-xs text-amber-400 border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 rounded-md shrink-0 whitespace-nowrap flex items-center gap-1">${icon("arrow_downward", { size: 12 })} LOW BW</span>
    </div>`;
  }).join("");
  return h(`<div class="border border-amber-400/30 bg-amber-400/[0.04] rounded-md p-4 animate-fade-in">
    ${sectionHeader("Low Bandwidth", "monitoring", pill(`${list.length} PORT${list.length === 1 ? "" : "S"}`, "warn"))}
    <div>${rows}</div>
  </div>`);
}

function nodesCard(nodes) {
  const stale = (n) => n.last_seen && (Date.now() - Date.parse(n.last_seen)) / 1000 > 180;
  return h(card(`${sectionHeader(`Edge nodes (${nodes.length})`, "cell_tower")}
    <table class="w-full border-collapse"><tr>
      <th class="${TH}">Org</th><th class="${TH}">Node</th><th class="${TH}">Version</th>
      <th class="${TH}">Fleet</th><th class="${TH}">Open</th><th class="${TH}">Last seen</th></tr>
    ${nodes.map(n => `<tr>
      <td class="${TD}">${esc(n.tenant_id)}</td><td class="${TD}">${esc(n.node_id)}</td>
      <td class="${TD} text-on-surface-variant">${esc(n.version || "—")}</td>
      <td class="${TD} font-mono-data text-mono-data">${n.fleet_size ?? "—"}</td>
      <td class="${TD}">${n.open_outages ? pill(n.open_outages, "down") : "0"}</td>
      <td class="${TD}">${pill(ago(n.last_seen), stale(n) ? "down" : "ok")}</td>
    </tr>`).join("") || `<tr><td colspan=6 class="${TD} text-on-surface-variant">No nodes yet.</td></tr>`}
  </table>`));
}

function devicesCard(devices) {
  return h(card(`${sectionHeader(`Live device registry (${devices.length})`, "router")}
    <div class="font-label-xs text-label-xs text-on-surface-variant mb-3">Reported by connected edges. Configure topology on the <b>Nodes</b> page.</div>
    <table class="w-full border-collapse">
    <tr><th class="${TH}">#</th><th class="${TH}">Org / Node</th><th class="${TH}">Name</th><th class="${TH}">IP</th><th class="${TH}">State</th></tr>
    ${devices.map(d => `<tr>
      <td class="${TD} font-mono-data text-mono-data text-on-surface-variant">${d.id}</td>
      <td class="${TD} text-on-surface-variant">${esc(d.tenant_id)} / ${esc(d.node_id)}</td>
      <td class="${TD}">${esc(d.name || "—")}</td><td class="${TD} font-mono-data text-mono-data text-on-surface-variant">${esc(d.ip || "—")}</td>
      <td class="${TD}">${d.last_state ? pill(d.last_state, STATE_TONE[d.last_state] || "muted") : "—"}</td>
    </tr>`).join("") || `<tr><td colspan=5 class="${TD} text-on-surface-variant">No devices reported yet.</td></tr>`}
  </table>`));
}

function eventsCard(events) {
  return h(card(`${sectionHeader("Recent events", "schedule")}
    <table class="w-full border-collapse">
    <tr><th class="${TH}">Org / Node</th><th class="${TH}">Type</th><th class="${TH}">Device</th><th class="${TH}">State</th><th class="${TH}">When</th></tr>
    ${events.map(e => `<tr>
      <td class="${TD} text-on-surface-variant">${esc(e.tenant_id)} / ${esc(e.node_id)}</td>
      <td class="${TD}">${esc(e.type || "—")}</td><td class="${TD}">${esc(e.device_name || e.device_id || "—")}</td>
      <td class="${TD}">${e.state ? pill(e.state, STATE_TONE[e.state] || "muted") : "—"}</td>
      <td class="${TD} text-on-surface-variant">${ago(e.occurred_at || e.received_at)}</td>
    </tr>`).join("") || `<tr><td colspan=5 class="${TD} text-on-surface-variant">No events yet.</td></tr>`}
  </table>`));
}

// --- Nodes page: the ISP-managed device topology (management plane) -----------------
async function renderNodesPage(main) {
  const t = scope();
  if (!t) { main.replaceChildren(needsOrgCard()); return; }
  const { devices } = await api(`/api/inventory${tq(t)}`);
  main.replaceChildren(nodesPageCard(devices, t));
}

function treeOrder(devices) {
  const children = new Map();
  for (const d of devices) {
    const key = d.parent_device_id ?? null;
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(d);
  }
  const out = [];
  const seen = new Set();
  const walk = (parentId, depth) => {
    for (const d of children.get(parentId) || []) {
      out.push({ ...d, _depth: depth }); seen.add(d.id); walk(d.id, depth + 1);
    }
  };
  walk(null, 0);
  for (const d of devices) if (!seen.has(d.id)) out.push({ ...d, _depth: 0 });
  return out;
}

function nodesPageCard(devices, tenant) {
  const ordered = treeOrder(devices);
  const write = canWrite();
  const editing = NODE_EDIT;
  const card_ = h(card(`${sectionHeader(`Nodes — ${tenant} (${devices.length})`, "router")}
    <table class="w-full border-collapse">
    <tr><th class="${TH}">Name</th><th class="${TH}">Type</th><th class="${TH}">IP</th><th class="${TH}">Region</th><th class="${TH}">Badges</th>${write ? `<th class="${TH}"></th>` : ""}</tr>
    ${ordered.map(d => `<tr>
      <td class="${TD}" style="padding-left:${12 + d._depth * 18}px">${d._depth ? `${icon("subdirectory_arrow_right", { size: 14, cls: "text-outline inline -mt-1" })} ` : ""}${esc(d.name)}</td>
      <td class="${TD} text-on-surface-variant">${esc(d.device_type || "—")}</td>
      <td class="${TD} font-mono-data text-mono-data text-on-surface-variant">${esc(d.ip_address)}</td>
      <td class="${TD} text-on-surface-variant">${esc(d.region || "—")}</td>
      <td class="${TD} flex flex-wrap gap-1">${d.maintenance ? pill("maintenance", "warn") : ""}
          ${d.snmp_enabled ? pill("SNMP", "ok") : ""}
          ${d.child_count ? pill(`${d.child_count} child`, "muted") : ""}</td>
      ${write ? `<td class="${TD}"><div class="flex gap-1.5">
          <button class="${GHOST}" data-edit="${d.id}">${icon("edit", { size: 14 })}</button>
          <button class="${GHOST}" data-maint="${d.id}" data-on="${d.maintenance ? 0 : 1}">${icon("pause_circle", { size: 14 })}</button>
          <button class="${GHOST}" data-del="${d.id}">${icon("delete", { size: 14 })}</button>
        </div></td>` : ""}
    </tr>`).join("") || `<tr><td colspan=${write ? 6 : 5} class="${TD} text-on-surface-variant">No nodes yet — add one below.</td></tr>`}
    </table>`));

  if (write) {
    const parentOpts = devices.filter(d => !editing || d.id !== editing.id)
      .map(d => `<option value="${d.id}" ${editing?.parent_device_id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("");
    const typeOpts = DEVICE_TYPES.map(tp => `<option ${editing?.device_type === tp ? "selected" : ""}>${tp}</option>`).join("");
    const form = h(card(`${sectionHeader(editing ? `Edit — ${editing.name}` : "Add node", "add_circle")}
      <div class="flex flex-wrap gap-2">
        <input id="fn" placeholder="name" value="${editing ? esc(editing.name) : ""}" class="${FIELD}" style="width:160px">
        <input id="fip" placeholder="ip address" value="${editing ? esc(editing.ip_address) : ""}" class="${FIELD}" style="width:140px">
        <select id="ftype" class="${FIELD}"><option value="">(type)</option>${typeOpts}</select>
        <input id="freg" placeholder="region" value="${editing ? esc(editing.region || "") : ""}" class="${FIELD}" style="width:120px">
        <select id="fparent" class="${FIELD}"><option value="">— none (root) —</option>${parentOpts}</select>
      </div>
      ${editing ? `<div class="flex flex-wrap items-center gap-2 mt-3">
        <label class="font-label-md text-label-md text-on-surface-variant flex items-center gap-1.5"><input type="checkbox" id="fsnmp" ${editing.snmp_enabled ? "checked" : ""}> SNMP enabled</label>
        <input id="fcomm" placeholder="community" value="${esc(editing.snmp_community || "")}" class="${FIELD}" style="width:120px">
        <input id="fport" placeholder="port" value="${editing.snmp_port || 161}" class="${FIELD}" style="width:70px">
      </div>` : ""}
      <div class="flex gap-2 mt-4">
        <button id="fsave" class="${BTN}">${editing ? "Save" : "Add"}</button>
        ${editing ? `<button class="${GHOST}" id="fcancel">Cancel</button>` : ""}
      </div>
      <div class="font-label-xs text-label-xs text-error mt-2 min-h-[14px]" id="ferr"></div>`, "mt-4"));
    $("#fsave", form).onclick = async () => {
      const ferr = $("#ferr", form);
      ferr.textContent = "";
      const payload = {
        tenant_id: tenant, name: $("#fn", form).value.trim(), ip_address: $("#fip", form).value.trim(),
        device_type: $("#ftype", form).value || null, region: $("#freg", form).value.trim() || null,
        parent_device_id: $("#fparent", form).value || null,
      };
      try {
        if (editing) {
          payload.id = editing.id;
          await api("/api/inventory/update", "POST", payload);
          if ($("#fsnmp", form)) {
            await api("/api/inventory/snmp", "POST", {
              id: editing.id, snmp_enabled: $("#fsnmp", form).checked,
              snmp_community: $("#fcomm", form).value.trim(), snmp_port: $("#fport", form).value,
            });
          }
        } else {
          await api("/api/inventory", "POST", payload);
        }
        NODE_EDIT = null;
        refresh();
      } catch (e) { ferr.textContent = e.message; }
    };
    if (editing) $("#fcancel", form).onclick = () => { NODE_EDIT = null; refresh(); };

    card_.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => {
      NODE_EDIT = devices.find(d => d.id === +b.dataset.edit) || null; refresh();
    });
    card_.querySelectorAll("[data-maint]").forEach(b => b.onclick = async () => {
      await api("/api/inventory/maintenance", "POST", { id: +b.dataset.maint, on: b.dataset.on === "1" });
      refresh();
    });
    card_.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm("Delete this node? This can't be undone.")) return;
      try { await api("/api/inventory/delete", "POST", { id: +b.dataset.del }); refresh(); }
      catch (e) { alert(e.message); }
    });
    const wrap = document.createElement("div");
    wrap.className = "flex flex-col gap-4";
    wrap.append(card_, form);
    return wrap;
  }
  return card_;
}

// --- Edge Nodes page: self-service enrollment for physical probes -------------------
async function renderAgentsPage(main) {
  const t = scope();
  if (!t) { main.replaceChildren(needsOrgCard()); return; }
  const { nodes } = await api(`/api/nodes${tq(t)}`);
  main.replaceChildren(agentsPageCard(nodes, t));
}

function installCmd(tenant, nodeId, tok) {
  return `curl -fsSL https://YOUR-CENTRAL/install-edge.sh | sudo sh -s -- \\\n`
       + `    --central https://YOUR-CENTRAL --token ${tok} --tenant ${tenant} --node ${nodeId}`;
}

function agentsPageCard(nodes, tenant) {
  const write = canWrite();
  const stale = (n) => n.last_seen && (Date.now() - Date.parse(n.last_seen)) / 1000 > 180;
  const status = (n) => n.revoked_at ? pill("revoked", "down")
    : n.last_seen ? pill(ago(n.last_seen), stale(n) ? "down" : "ok")
    : pill("never connected", "muted");
  const wrap = document.createElement("div");
  wrap.className = "flex flex-col gap-4";
  const listCard = h(card(`${sectionHeader(`Edge nodes — ${tenant} (${nodes.length})`, "cell_tower")}
    <div class="font-label-xs text-label-xs text-on-surface-variant mb-3">The physical probes this org has registered,
      and their enrollment credentials. What each one MONITORS is configured on the
      <b>Nodes</b> tab — this page is only about a probe's identity/credential.</div>
    <table class="w-full border-collapse"><tr><th class="${TH}">Node id</th><th class="${TH}">Status</th><th class="${TH}">Version</th><th class="${TH}">Registered</th>${write ? `<th class="${TH}"></th>` : ""}</tr>
    ${nodes.map(n => `<tr>
      <td class="${TD}">${esc(n.node_id)}</td>
      <td class="${TD}">${status(n)}</td>
      <td class="${TD} text-on-surface-variant">${esc(n.version || "—")}</td>
      <td class="${TD} text-on-surface-variant">${ago(n.created_at)}</td>
      ${write ? `<td class="${TD}"><div class="flex gap-1.5">
          <button class="${GHOST}" data-rotate="${esc(n.node_id)}">${icon("vpn_key", { size: 14 })}</button>
          ${n.revoked_at ? "" : `<button class="${GHOST}" data-revoke="${esc(n.node_id)}">${icon("power_off", { size: 14 })}</button>`}
        </div></td>` : ""}
    </tr>`).join("") || `<tr><td colspan=${write ? 5 : 4} class="${TD} text-on-surface-variant">No nodes registered yet — add one below.</td></tr>`}
    </table>`));
  wrap.append(listCard);

  if (!write) return wrap;

  if (AGENT_REVEAL) {
    const reveal = h(card(`${sectionHeader("Save this now — it won't be shown again", "vpn_key")}
      <div class="font-body-sm text-body-sm text-on-surface-variant">Token for <b class="text-primary">${esc(AGENT_REVEAL.node_id)}</b>:</div>
      <div class="my-2"><code class="font-mono-data text-mono-data bg-surface border border-outline-variant rounded-md px-2 py-1 break-all">${esc(AGENT_REVEAL.token)}</code></div>
      <div class="font-label-xs text-label-xs text-on-surface-variant mt-2">Run this on the edge box (or its Windows
        equivalent, <code>install-edge.ps1</code>):</div>
      <pre class="whitespace-pre-wrap bg-surface border border-outline-variant rounded-md p-3 font-mono-data text-[12px] my-2">${esc(
        installCmd(tenant, AGENT_REVEAL.node_id, AGENT_REVEAL.token))}</pre>
      <button class="${GHOST}" id="agentdismiss">I've saved it</button>`));
    $("#agentdismiss", reveal).onclick = () => { AGENT_REVEAL = null; refresh(); };
    wrap.append(reveal);
  }

  const form = h(card(`${sectionHeader("Register a new node", "add_circle")}
    <div class="flex gap-2">
      <input id="anid" placeholder="node id, e.g. edge-a1" class="${FIELD}" style="width:200px">
      <button id="anadd" class="${BTN}">Register</button>
    </div>
    <div class="font-label-xs text-label-xs text-error mt-2 min-h-[14px]" id="anerr"></div>`));
  $("#anadd", form).onclick = async () => {
    const anerr = $("#anerr", form);
    anerr.textContent = "";
    const nodeId = $("#anid", form).value.trim();
    if (!nodeId) { anerr.textContent = "node id is required"; return; }
    try {
      const r = await api("/api/nodes", "POST", { tenant_id: tenant, node_id: nodeId });
      AGENT_REVEAL = { node_id: r.node_id, token: r.token };
      refresh();
    } catch (e) { anerr.textContent = e.message; }
  };
  wrap.append(form);

  listCard.querySelectorAll("[data-rotate]").forEach(b => b.onclick = async () => {
    if (!confirm(`Rotate ${b.dataset.rotate}'s token? The old one stops working immediately.`)) return;
    try {
      const r = await api("/api/nodes/rotate", "POST", { tenant_id: tenant, node_id: b.dataset.rotate });
      AGENT_REVEAL = { node_id: r.node_id, token: r.token };
      refresh();
    } catch (e) { alert(e.message); }
  });
  listCard.querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => {
    if (!confirm(`Revoke ${b.dataset.revoke}'s token? It will stop reporting until re-enrolled.`)) return;
    await api("/api/nodes/revoke", "POST", { tenant_id: tenant, node_id: b.dataset.revoke });
    refresh();
  });
  return wrap;
}

// --- Team page: workers + attendance for the scoped org ------------------------------
async function renderTeamPage(main) {
  const t = scope();
  if (!t) { main.replaceChildren(needsOrgCard()); return; }
  const team = await api(`/api/team${tq(t)}`);
  const att = await api(`/api/attendance${tq(t)}`);
  main.replaceChildren(teamCard(team.team, t), attendanceCard(att, t));
}

function teamCard(team, tenant) {
  const card_ = h(card(`${sectionHeader(`Team — ${tenant}`, "group")}
    <table class="w-full border-collapse" id="teamtbl">
    <tr><th class="${TH}">Name</th><th class="${TH}">Role</th><th class="${TH}">Region</th><th class="${TH}"></th></tr>
    ${team.map(w => `<tr>
      <td class="${TD}">${esc(w.name)}</td><td class="${TD}">${esc(w.role)}</td><td class="${TD} text-on-surface-variant">${esc(w.region || "—")}</td>
      <td class="${TD}">${canWrite() ? `<button class="${GHOST}" data-del="${w.id}">${icon("delete", { size: 14 })}</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan=4 class="${TD} text-on-surface-variant">No team members yet.</td></tr>`}
  </table>`));
  if (canWrite()) {
    const form = h(`<div class="flex flex-wrap gap-2 mt-4">
      <input id="wn" placeholder="name" class="${FIELD}" style="width:140px">
      <select id="wr" class="${FIELD}"><option>operator</option><option>owner</option><option>tech</option></select>
      <input id="wreg" placeholder="region" class="${FIELD}" style="width:120px">
      <button id="wadd" class="${BTN}">Add</button></div>`);
    card_.append(form);
    $("#wadd", card_).onclick = async () => {
      const name = $("#wn", card_).value.trim(); if (!name) return;
      await api("/api/team", "POST", { tenant_id: tenant, name, role: $("#wr", card_).value, region: $("#wreg", card_).value });
      refresh();
    };
    card_.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      await api("/api/team/delete", "POST", { id: +b.dataset.del }); refresh();
    });
  }
  return card_;
}

function attendanceCard(att, tenant) {
  const head = `<tr><th class="${TH}">Operator</th>${att.days.map(d => `<th class="${TH}">${d.slice(5)}</th>`).join("")}</tr>`;
  const rows = att.operators.map(op => `<tr><td class="${TD}">${esc(op.name)}</td>${
    att.days.map(d => `<td class="${TD}"><span class="chip inline-flex items-center justify-center w-6 h-6 rounded-full border cursor-pointer font-label-xs text-label-xs ${
      op.days[d] ? "bg-emerald-400/10 text-emerald-400 border-emerald-400/30" : "bg-surface border-outline-variant text-outline"
    }" data-w="${op.id}" data-d="${d}">${op.days[d] ? "✓" : "·"}</span></td>`).join("")
  }</tr>`).join("") || `<tr><td class="${TD} text-on-surface-variant">No operators on the roster.</td></tr>`;
  const card_ = h(card(`${sectionHeader(`Attendance — ${tenant}`, "task_alt")}<table class="w-full border-collapse">${head}${rows}</table>`, "mt-4"));
  if (canWrite()) {
    card_.querySelectorAll(".chip").forEach(c => c.onclick = async () => {
      const present = !c.classList.contains("bg-emerald-400/10");
      await api("/api/attendance", "POST", { worker_id: +c.dataset.w, day: c.dataset.d, present });
      refresh();
    });
  }
  return card_;
}

// --- Settings page: org identity + the three role alert channels ---------------------
async function renderSettingsPage(main) {
  const t = scope();
  if (!t) { main.replaceChildren(needsOrgCard()); return; }
  const { orgs } = await api(`/api/orgs${tq(t)}`);
  const org = orgs.find(o => o.tenant_id === t) || { tenant_id: t };
  main.replaceChildren(settingsCard(org));
}

function settingsCard(org) {
  const roles = [["owner", "Owner"], ["operator", "Operator"], ["tech", "Tech"]];
  const write = canWrite();
  const card_ = h(card(`${sectionHeader(`Settings — ${org.tenant_id}`, "settings")}
    <div class="flex items-center gap-2">
      <label class="font-label-md text-label-md text-on-surface-variant w-28">Org name</label>
      <input id="sname" value="${esc(org.name || "")}" ${write ? "" : "disabled"} class="${FIELD}" style="width:220px">
    </div>
    ${roles.map(([key, label]) => `
      <div class="flex items-center gap-2 mt-3">
        <label class="font-label-md text-label-md text-on-surface-variant w-28">${label} topic</label>
        <input id="stopic_${key}" value="${esc(org[`ntfy_topic_${key}`] || "")}" ${write ? "" : "disabled"}
               placeholder="ntfy topic for ${label.toLowerCase()} alerts" class="${FIELD}" style="width:220px">
        ${write ? `<button class="${GHOST}" data-test="${key}">Send test</button>` : ""}
        <span class="font-label-xs text-label-xs text-on-surface-variant" id="stest_${key}"></span>
      </div>`).join("")}
    ${write ? `<div class="mt-4"><button id="ssave" class="${BTN}">Save</button></div>` : ""}
    <div class="font-label-xs text-label-xs text-error mt-2 min-h-[14px]" id="serr"></div>`));
  if (write) {
    $("#ssave", card_).onclick = async () => {
      const serr = $("#serr", card_);
      serr.textContent = "";
      try {
        await api("/api/org", "POST", {
          tenant_id: org.tenant_id, name: $("#sname", card_).value.trim() || null,
          ntfy_topic_owner: $("#stopic_owner", card_).value.trim() || null,
          ntfy_topic_operator: $("#stopic_operator", card_).value.trim() || null,
          ntfy_topic_tech: $("#stopic_tech", card_).value.trim() || null,
        });
        refresh();
      } catch (e) { serr.textContent = e.message; }
    };
    card_.querySelectorAll("[data-test]").forEach(b => b.onclick = async () => {
      const role = b.dataset.test;
      const out = $(`#stest_${role}`, card_);
      out.textContent = "sending…";
      try {
        const r = await api("/api/test-alert", "POST", { tenant_id: org.tenant_id, role });
        out.textContent = r.ok ? "✓ sent" : `failed: ${r.detail || ""}`;
      } catch (e) { out.textContent = e.message; }
    });
  }
  return card_;
}

boot();
