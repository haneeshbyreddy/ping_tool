/* HANSA dashboard — vanilla SPA (no framework, no build step).
 * Renders the three screens against the dashboard's /api JSON layer and re-fetches on a
 * light interval. Tailwind classes are generated at runtime by the vendored
 * Play CDN observing DOM mutations, so plain innerHTML markup gets styled. */
(function () {
  "use strict";

  // --- tiny helpers ---------------------------------------------------------
  const $ = (sel, root = document) => root.querySelector(sel);

  function icon(name, opts) {
    opts = opts || {};
    const size = opts.size || 20;
    const cls = opts.cls || "";
    const p = (window.ICON_PATHS || {})[name] || "";
    return `<svg class="ic ${cls}" style="width:${size}px;height:${size}px" ` +
      `viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">${p}</svg>`;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (r.status === 401) { requireLogin(); throw new Error("session expired"); }
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  async function sendJSON(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (r.status === 401) { requireLogin(); return { ok: false, status: 401, data: {} }; }
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    return { ok: r.ok, status: r.status, data };
  }

  const postJSON = (url, body) => sendJSON("POST", url, body || {});

  function fmtPct(n) { return (n == null ? "—" : Number(n).toFixed(2) + "%"); }

  // Mirror of core/analytics._fmt_dur so an open outage's elapsed time can tick
  // client-side every second instead of waiting on the next server re-fetch.
  function fmtDur(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }

  // Repaint every [data-since] element from its outage start so durations count
  // up live. Cheap no-op when nothing is open; runs on a single global 1s timer.
  function tickDurations() {
    const now = Date.now();
    document.querySelectorAll("[data-since]").forEach((el) => {
      const started = toUtcDate(el.getAttribute("data-since"));
      if (isNaN(started)) return;
      el.textContent = fmtDur((now - started.getTime()) / 1000);
    });
  }

  // Org/locale branding, populated from /api/auth/status (defaults until then).
  const BRAND = { org_name: "HANSA", timezone: "UTC",
    channels: { owner: "owner", operator: "operator", tech: "tech" } };

  // Stored stamps are UTC: ISO with +00:00 (polls/outages) or space-separated naive
  // (acks). Normalise to a real UTC instant, then render in the configured timezone.
  function toUtcDate(ts) {
    let s = String(ts).trim().replace(" ", "T");
    if (!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";  // naive → treat as UTC
    return new Date(s);
  }

  function fmtTime(ts, opts = {}) {
    if (!ts) return "—";
    const d = toUtcDate(ts);
    if (isNaN(d)) return ts;
    const showTz = opts.tz !== false;   // pass {tz:false} to drop the zone suffix
    try {
      const fmt = {
        timeZone: BRAND.timezone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      };
      if (showTz) fmt.timeZoneName = "short";
      return new Intl.DateTimeFormat("en-GB", fmt).format(d).replace(",", "");
    } catch (e) {           // invalid tz → fall back to UTC
      const s = d.toISOString().replace("T", " ").slice(0, 19);
      return showTz ? s + " UTC" : s;
    }
  }

  let _toastTimer = null;
  function toast(msg, kind) {
    let t = $("#toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast";
      t.className = "fixed z-[60] bottom-20 md:bottom-6 left-1/2 -translate-x-1/2 " +
        "px-4 py-2 rounded-md text-label-md font-label-md shadow-lg transition-opacity";
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

  // --- shell ----------------------------------------------------------------
  const NAV = [
    { path: "#/", icon: "dashboard", label: "Dashboard" },
    { path: "#/nodes", icon: "router", label: "Nodes" },
    { path: "#/team", icon: "group", label: "Team" },
    { path: "#/settings", icon: "settings", label: "Settings" },
    { path: "#/logs", icon: "terminal", label: "Logs" },
  ];

  function currentPath() {
    const h = location.hash || "#/";
    return h.split("?")[0];
  }

  function sidebar() {
    const cur = currentPath();
    const items = NAV.map((it) => {
      const active = cur === it.path;
      const cls = active
        ? "bg-surface-container text-primary font-bold"
        : "text-outline hover:text-on-surface hover:bg-surface-container-low";
      return `<a href="${it.path}" class="flex items-center gap-3 px-4 py-3 rounded-md transition-colors cursor-pointer ${cls}">
        ${icon(it.icon)}<span class="font-label-md text-label-md">${it.label}</span></a>`;
    }).join("");
    return `<aside class="hidden md:flex flex-col w-[240px] shrink-0 border-r border-outline-variant pr-container-margin pl-4 pt-6 gap-2 sticky top-[57px] h-[calc(100vh-57px)] bg-surface-dim">
      <nav class="flex flex-col gap-2">${items}</nav>
      <div class="mt-auto pb-6 text-outline font-label-xs text-label-xs flex items-center gap-2">
        ${icon("refresh", { size: 14 })}<span id="refresh-status">live · auto-refresh</span></div>
    </aside>`;
  }

  function bottomNav() {
    const cur = currentPath();
    const items = NAV.map((it) => {
      const active = cur === it.path;
      const c = active ? "text-primary" : "text-outline hover:text-on-surface";
      const pill = active ? "bg-surface-container" : "";
      return `<a href="${it.path}" class="flex flex-col items-center justify-center w-16 h-full transition-all active:scale-95 ${c}">
        <div class="flex items-center justify-center w-12 h-8 rounded-full mb-1 ${pill}">${icon(it.icon, { size: 24 })}</div>
        <span class="font-label-xs text-label-xs ${active ? "font-bold" : "font-medium"}">${it.label}</span></a>`;
    }).join("");
    return `<nav class="md:hidden fixed bottom-0 w-full z-50 bg-background border-t border-outline-variant flex justify-around items-center h-16 px-2 pb-safe">${items}</nav>`;
  }

  function shell() {
    return `
      <header class="w-full top-0 sticky bg-background border-b border-outline-variant flex justify-between items-center px-container-margin py-component-padding-y z-40 h-16 md:h-[57px]">
        <a href="#/" class="flex items-center gap-2 cursor-pointer active:opacity-80">
          ${icon("hub", { size: 28, cls: "text-primary" })}
          <h1 class="font-headline-lg text-headline-lg font-bold tracking-tighter text-primary">${esc(BRAND.org_name)}</h1>
        </a>
        <div class="flex items-center gap-2 text-on-surface-variant">
          <span id="uplink-chip" class="hidden"></span>
          <div class="relative">
            <button id="account-btn" class="p-2 rounded-full flex items-center justify-center w-10 h-10 hover:bg-surface-container active:scale-95 transition-transform">${icon("account_circle", { size: 28 })}</button>
            <div id="account-menu" class="hidden absolute right-0 mt-2 w-56 bg-surface-container border border-outline-variant rounded-md shadow-lg z-50 py-1">
              <div id="heartbeat" class="px-4 py-2.5 border-b border-outline-variant font-label-xs text-label-xs text-on-surface-variant flex items-center gap-2">${icon("monitoring", { size: 14 })} Monitor: checking…</div>
              <button id="logout-btn" class="w-full text-left px-4 py-2.5 font-label-md text-label-md text-on-surface hover:bg-surface-container-high flex items-center gap-2">${icon("logout", { size: 18 })} Log out</button>
            </div>
          </div>
        </div>
      </header>
      <div class="flex flex-1 flex-col md:flex-row w-full max-w-[1920px] mx-auto pb-[80px] md:pb-0">
        ${sidebar()}
        <main id="page" class="flex-1 min-w-0 flex flex-col"></main>
      </div>
      ${bottomNav()}`;
  }

  function loading(label) {
    return `<div class="flex items-center justify-center gap-3 text-on-surface-variant py-20">
      ${icon("refresh", { size: 20, cls: "text-outline" })}
      <span class="font-body-sm text-body-sm">Loading ${esc(label)}…</span></div>`;
  }

  function errorBox(msg) {
    return `<div class="m-6 border border-outline-variant bg-error/5 rounded-md p-4 flex items-center gap-3 text-error">
      ${icon("warning")}<span class="font-body-sm text-body-sm">Couldn't load data: ${esc(msg)}</span></div>`;
  }

  // --- Dashboard ------------------------------------------------------------
  const STATUS_META = {
    unassigned: { accent: "amber", tag: "UNASSIGNED", text: "amber-400" },
    in_progress: { accent: "blue", tag: "IN PROGRESS", text: "blue-400" },
    pending_postmortem: { accent: "emerald", tag: "PENDING POST-MORTEM", text: "emerald-400" },
  };

  function summaryCards(s) {
    const uplink = s.uplink_down
      ? `<span class="font-label-xs text-label-xs text-error border border-error/30 bg-error/10 px-2 py-0.5 rounded-sm">UPLINK DOWN</span>`
      : "";
    return `<div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="col-span-2 border border-outline-variant bg-surface-container-low rounded-md p-4 flex flex-col justify-between relative overflow-hidden">
        <div class="flex justify-between items-start mb-2 relative z-10">
          <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">System Health</span>
          ${icon("shield", { size: 16, cls: "text-primary" })}
        </div>
        <div class="font-display text-display text-primary relative z-10">${fmtPct(s.system_health_pct)}</div>
        <div class="font-label-xs text-label-xs text-outline relative z-10 mt-1">last ${s.window_hours}h overall uptime</div>
      </div>
      <div class="border border-outline-variant bg-surface rounded-md p-4 flex flex-col justify-between">
        <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-2">Active Nodes</span>
        <div class="flex items-baseline gap-1">
          <span class="font-headline-lg text-headline-lg text-primary">${s.active_nodes}</span>
          <span class="font-mono-data text-mono-data text-on-surface-variant">/${s.total_nodes}</span>
        </div>
      </div>
      <div class="border border-outline-variant bg-surface rounded-md p-4 flex flex-col justify-between">
        <div class="flex items-center gap-2 mb-2">
          <span class="flex h-2 w-2 rounded-full ${s.outages ? "bg-error" : "bg-outline"}"></span>
          <span class="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">Outages</span>
        </div>
        <div class="flex items-center justify-between">
          <span class="font-headline-lg text-headline-lg ${s.outages ? "text-error" : "text-primary"}">${s.outages}</span>
          ${uplink}
        </div>
      </div>
    </div>`;
  }

  function triageCard(o, team = []) {
    const m = STATUS_META[o.status] || STATUS_META.unassigned;
    // Open outages keep counting up (live ticker); a recovered one is final.
    const live = o.status !== "pending_postmortem";
    const durLabel = live
      ? `<span data-since="${esc(o.started_at)}">${esc(o.duration_label)}</span>`
      : esc(o.duration_label);
    const head = `
      <div class="flex justify-between items-start">
        <div>
          <h3 class="font-body-lg text-body-lg text-primary font-medium">${esc(o.name)} <span class="text-on-surface-variant font-normal">· ${esc(o.region)}</span></h3>
          <div class="flex items-center gap-2 mt-1 flex-wrap">
            <span class="font-label-xs text-label-xs text-${m.text} border border-${m.text}/30 bg-${m.text}/10 px-2 py-0.5 rounded-sm">${m.tag}</span>
            <span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("schedule", { size: 14 })} ${durLabel}</span>
            <span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("event", { size: 14 })} ${fmtTime(o.started_at, { tz: false })}</span>
          </div>
        </div>
      </div>`;

    let action = "";
    if (o.status === "unassigned") {
      const picker = team.length
        ? `<select data-tech class="flex-1 bg-surface-container border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary appearance-none">
             <option value="">Acknowledged by…</option>
             ${team.map((w) => `<option value="${esc(w.name)}">${esc(w.name)} · ${esc(w.role)}</option>`).join("")}
           </select>`
        : `<input data-tech type="text" placeholder="Acknowledged by (your name)…" class="flex-1 bg-surface-container border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />`;
      action = `
        <div class="flex gap-2 w-full md:w-2/3" data-card="${o.id}">
          ${picker}
          <button data-action="ack" class="bg-primary text-surface font-label-md text-label-md px-4 py-2 rounded-md active:scale-95 transition-transform whitespace-nowrap">Acknowledge</button>
        </div>`;
    } else if (o.status === "in_progress") {
      action = `<div class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1 border-t border-outline-variant pt-3">${icon("assignment", { size: 14 })} Acknowledged by: ${esc(o.assigned_to)}</div>`;
    } else if (o.status === "pending_postmortem") {
      action = `
        <form data-card="${o.id}" class="flex flex-col gap-3 border-t border-outline-variant pt-3 md:w-2/3">
          <div class="space-y-1">
            <label class="font-label-xs text-label-xs text-on-surface-variant">What was the issue?${o.assigned_to ? ` <span class="text-outline">(resolved by ${esc(o.assigned_to)})</span>` : ""}</label>
            <select data-root class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary appearance-none">
              <option value="">Select confirmed cause…</option>
              <option value="Power Failure">Power Failure</option>
              <option value="Fiber/Backhaul Cut">Fiber/Backhaul Cut</option>
              <option value="Hardware Fault">Hardware Fault</option>
              <option value="Weather/RF Interference">Weather/RF Interference</option>
              <option value="Other">Other</option>
            </select>
          </div>
          <div class="space-y-1">
            <label class="font-label-xs text-label-xs text-on-surface-variant">Resolution Details</label>
            <textarea data-notes rows="2" placeholder="Brief summary of the fix…" class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary resize-none"></textarea>
          </div>
          <div class="flex gap-2">
            <button data-action="postmortem" type="button" class="flex-1 bg-transparent border border-outline-variant text-primary hover:bg-surface-container font-label-md text-label-md px-4 py-2 rounded-md transition-colors">Submit Log</button>
            <button data-action="dismiss" type="button" class="bg-transparent border border-error/40 text-error hover:bg-error/10 font-label-md text-label-md px-4 py-2 rounded-md transition-colors flex items-center gap-1" title="Discard without logging a post-mortem">${icon("delete", { size: 16 })} Delete</button>
          </div>
        </form>`;
    }

    const accentMap = { amber: "border-l-amber-500 bg-amber-500/5", blue: "border-l-blue-500 bg-blue-500/5", emerald: "border-l-emerald-500 bg-emerald-500/5" };
    return `<div class="border border-outline-variant bg-surface rounded-md p-4 flex flex-col gap-4 border-l-4 ${accentMap[m.accent]}">${head}${action}</div>`;
  }

  function renderDashboard(page) {
    page.innerHTML = `<div class="px-4 md:px-8 py-6 md:py-8 space-y-section-gap overflow-x-hidden">
      <section class="animate-fade-in">
        <div class="flex items-center gap-2 mb-4">${icon("monitoring", { size: 20, cls: "text-on-surface-variant" })}
          <h2 class="font-headline-md text-headline-md text-primary">Network Analytics</h2></div>
        <div id="summary">${loading("analytics")}</div>
      </section>
      <section class="animate-fade-in">
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-2">${icon("warning", { size: 20, cls: "text-error" })}
            <h2 class="font-headline-md text-headline-md text-primary">Active Outage Triage</h2></div>
          <span id="triage-count" class="font-label-xs text-label-xs bg-surface-container px-2 py-1 rounded-md border border-outline-variant">…</span>
        </div>
        <div id="triage" class="flex flex-col gap-3">${loading("triage")}</div>
      </section>
    </div>`;

    return async function load() {
      try {
        const [s, triage, team] = await Promise.all([
          getJSON("/api/summary"), getJSON("/api/triage"),
          getJSON("/api/workers").catch(() => []),
        ]);
        if (currentPath() !== "#/") return;
        const responders = team.filter((w) => w.is_active);
        $("#summary", page).innerHTML = summaryCards(s);
        updateUplinkChip(s.uplink_down);
        $("#triage-count", page).textContent = `${triage.length} ITEM${triage.length === 1 ? "" : "S"}`;
        $("#triage", page).innerHTML = triage.length
          ? triage.map((o) => triageCard(o, responders)).join("")
          : `<div class="border border-outline-variant bg-surface-container-low rounded-md p-6 flex items-center gap-3 text-on-surface-variant">
              ${icon("task_alt", { cls: "text-emerald-400" })}<span class="font-body-sm">All clear — no active outages.</span></div>`;
        wireTriage(page, load);
      } catch (e) {
        $("#summary", page).innerHTML = errorBox(e.message);
        $("#triage", page).innerHTML = "";
      }
    };
  }

  function wireTriage(page, reload) {
    page.querySelectorAll('[data-action="ack"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const card = btn.closest("[data-card]");
        const id = card.getAttribute("data-card");
        const name = $("[data-tech]", card).value.trim();
        if (!name) { toast("Enter your name to acknowledge", "error"); return; }
        btn.disabled = true;
        const res = await postJSON(`/api/outages/${id}/ack`, { technician: name });
        if (res.ok && res.data.ok) { toast("Acknowledged — escalation stopped"); reload(); }
        else { toast("Acknowledge failed (already resolved?)", "error"); btn.disabled = false; }
      });
    });
    page.querySelectorAll('[data-action="postmortem"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const form = btn.closest("[data-card]");
        const id = form.getAttribute("data-card");
        const root = $("[data-root]", form).value;
        const notes = $("[data-notes]", form).value;
        if (!root) { toast("Select a root cause", "error"); return; }
        btn.disabled = true;
        const res = await postJSON(`/api/outages/${id}/postmortem`, { root_cause: root, notes });
        if (res.ok && res.data.ok) { toast("Post-mortem logged"); reload(); }
        else { toast("Submit failed", "error"); btn.disabled = false; }
      });
    });
    page.querySelectorAll('[data-action="dismiss"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const form = btn.closest("[data-card]");
        const id = form.getAttribute("data-card");
        if (!confirm("Delete this outage from the list without logging a post-mortem? It stays in downtime history.")) return;
        btn.disabled = true;
        const res = await sendJSON("DELETE", `/api/outages/${id}`);
        if (res.ok && res.data.ok) { toast("Outage dismissed"); reload(); }
        else { toast("Dismiss failed (already logged?)", "error"); btn.disabled = false; }
      });
    });
  }

  function updateUplinkChip(down) {
    const chip = $("#uplink-chip");
    if (!chip) return;
    if (down) {
      chip.className = "font-label-xs text-label-xs text-error border border-error/30 bg-error/10 px-2 py-1 rounded-md flex items-center gap-1";
      chip.innerHTML = `${icon("wifi_off", { size: 14 })} UPLINK DOWN`;
    } else {
      chip.className = "hidden";
      chip.innerHTML = "";
    }
  }

  // --- Nodes ----------------------------------------------------------------
  function nodeRow(n) {
    const map = {
      UP: { dot: "bg-primary", text: "text-primary", icon: "cell_tower", glow: "0 0 4px #ffffff", op: "" },
      DEGRADED: { dot: "bg-error", text: "text-error", icon: "cell_tower", glow: "0 0 4px #ffb4ab", op: "" },
      DOWN: { dot: "bg-error", text: "text-error", icon: "wifi_off", glow: "0 0 4px #ffb4ab", op: "" },
      UNREACHABLE: { dot: "bg-outline", text: "text-outline", icon: "router", glow: "", op: "opacity-60" },
    };
    const s = map[n.state] || map.UP;
    const pctColor = n.uptime_pct >= 99.9 ? "text-primary" : (n.uptime_pct >= 95 ? "text-amber-400" : "text-error");
    return `<div data-edit="${n.id}" title="Click to edit" class="bg-surface border border-outline-variant rounded-md p-3 flex flex-row items-center justify-between gap-3 hover:bg-surface-container-low transition-colors group cursor-pointer ${s.op}">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-8 h-8 rounded-full bg-surface-container-high flex items-center justify-center shrink-0 ${s.text}">${icon(s.icon, { size: 18 })}</div>
        <div class="min-w-0">
          <h4 class="font-label-md text-label-md text-primary truncate">${esc(n.name)}</h4>
          <p class="font-mono-data text-on-surface-variant mt-0.5 truncate text-[11px]">${esc(n.type || "node")} · ${esc(n.ip)} · ${esc(n.region)}</p>
        </div>
      </div>
      <div class="flex items-center gap-3 shrink-0">
        <div class="text-right hidden sm:block"><p class="font-mono-data ${pctColor}">${fmtPct(n.uptime_pct)}</p></div>
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full ${s.dot}" style="box-shadow:${s.glow}"></span>
          <span class="font-label-md text-label-md ${s.text} hidden md:inline">${esc(n.state_label)}</span>
        </div>
        <span class="text-outline group-hover:text-primary transition-colors">${icon("edit", { size: 18 })}</span>
      </div>
    </div>`;
  }

  function heatmapCells(cells, selected) {
    return cells.map((c) => {
      const cls = c.state === "ok" ? "active-ok" : (c.state === "outage" ? "active-outage" : "");
      const clickable = c.state !== "nodata";
      const sel = selected === c.date ? "ring-2 ring-primary ring-offset-1 ring-offset-surface" : "";
      const hint = clickable ? "cursor-pointer hover:opacity-70" : "";
      return `<div data-date="${esc(c.date)}" data-state="${esc(c.state)}" title="${esc(c.date)}: ${esc(c.state)}"
        class="heatmap-cell ${cls} ${sel} ${hint}"></div>`;
    }).join("");
  }

  // Row variant for the heatmap drill-down: shows that day's downtime
  // instead of the live state.
  function dayNodeRow(n) {
    return `<div class="bg-surface border border-outline-variant rounded-md p-3 flex flex-row items-center justify-between gap-3">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-8 h-8 rounded-full bg-surface-container-high flex items-center justify-center shrink-0 text-error">${icon("wifi_off", { size: 18 })}</div>
        <div class="min-w-0">
          <h4 class="font-label-md text-label-md text-primary truncate">${esc(n.name)}</h4>
          <p class="font-mono-data text-on-surface-variant mt-0.5 truncate text-[11px]">${esc(n.type || "node")} · ${esc(n.ip)} · ${esc(n.region)}</p>
        </div>
      </div>
      <div class="flex items-center gap-3 shrink-0">
        <span class="font-mono-data text-mono-data text-error flex items-center gap-1">${icon("schedule", { size: 14 })} ${esc(n.down_label)} down</span>
      </div>
    </div>`;
  }

  // --- node inventory editor (add / edit / delete) --------------------------
  const DEVICE_TYPES = ["core", "tower", "relay", "sector", "backhaul"];

  function openModal(innerHtml) {
    const overlay = document.createElement("div");
    overlay.className = "fixed inset-0 z-[70] bg-black/60 flex items-end md:items-center justify-center md:p-4";
    overlay.innerHTML = `<div class="bg-surface-container border border-outline-variant rounded-t-xl md:rounded-xl w-full md:max-w-lg max-h-[92dvh] overflow-y-auto animate-fade-in">${innerHtml}</div>`;
    const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    return { overlay, close };
  }

  function field(label, name, value, opts) {
    opts = opts || {};
    const v = value == null ? "" : value;
    const span = opts.full ? "sm:col-span-2" : "";
    const inputCls = "w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary";
    let control;
    if (opts.options) {
      const optionsHtml = opts.options.map((o) =>
        `<option value="${esc(o.value)}" ${String(o.value) === String(v) ? "selected" : ""}>${esc(o.label)}</option>`).join("");
      control = `<select data-field="${name}" class="${inputCls} appearance-none">${optionsHtml}</select>`;
    } else {
      const type = opts.type || "text";
      const extra = opts.type === "number" ? `min="0" ${opts.step ? `step="${opts.step}"` : ""}` : "";
      control = `<input data-field="${name}" type="${type}" value="${esc(v)}" ${extra} placeholder="${esc(opts.placeholder || "")}" class="${inputCls}" />`;
    }
    return `<div class="space-y-1 ${span}">
      <label class="font-label-xs text-label-xs text-on-surface-variant">${esc(label)}${opts.required ? ' <span class="text-error">*</span>' : ""}</label>
      ${control}</div>`;
  }

  function openNodeModal(device, devices, onDone) {
    const isEdit = !!device;
    const d = device || {};
    const typeOpts = [{ value: "", label: "—" }].concat(DEVICE_TYPES.map((t) => ({ value: t, label: t })));
    const parentOpts = [{ value: "", label: "None (root)" }].concat(
      devices.filter((x) => x.id !== d.id).map((x) => ({ value: x.id, label: `${x.name} (#${x.id})` })));

    const form = `
      <div class="sticky top-0 bg-surface-container border-b border-outline-variant px-5 py-4 flex items-center justify-between">
        <h3 class="font-headline-md text-headline-md text-primary flex items-center gap-2">
          ${icon(isEdit ? "edit" : "add_circle", { size: 20 })} ${isEdit ? "Edit node" : "Add node"}</h3>
        <button data-close class="text-on-surface-variant hover:text-primary p-1 rounded-full hover:bg-surface-container-high">${icon("close", { size: 20 })}</button>
      </div>
      <form data-node-form class="p-5 grid grid-cols-1 sm:grid-cols-2 gap-4">
        ${field("Name", "name", d.name, { required: true, full: true, placeholder: "e.g. Rampur Main Tower" })}
        ${field("IP address", "ip_address", d.ip_address, { required: true, placeholder: "192.0.2.10" })}
        ${field("Type", "device_type", d.device_type, { options: typeOpts })}
        ${field("Region", "region", d.region, { placeholder: "village / area" })}
        ${field("Parent node", "parent_device_id", d.parent_device_id, { options: parentOpts, full: true })}
        <div class="sm:col-span-2 flex items-center justify-between gap-2 pt-2 border-t border-outline-variant">
          <div>${isEdit ? `<button type="button" data-delete class="flex items-center gap-1 text-error hover:bg-error/10 border border-error/30 font-label-md text-label-md px-3 py-2 rounded-md transition-colors">${icon("delete", { size: 16 })} Delete</button>` : ""}</div>
          <div class="flex items-center gap-2">
            <button type="button" data-close class="text-on-surface-variant hover:text-primary font-label-md text-label-md px-4 py-2 rounded-md hover:bg-surface-container-high">Cancel</button>
            <button type="submit" class="bg-primary text-surface font-label-md text-label-md px-4 py-2 rounded-md active:scale-95 transition-transform">${isEdit ? "Save changes" : "Add node"}</button>
          </div>
        </div>
      </form>`;

    const { overlay, close } = openModal(form);
    overlay.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));

    const submitBtn = overlay.querySelector('[type="submit"]');
    const origLabel = submitBtn.innerHTML;
    const spinner = (label) => `${icon("refresh", { size: 16, cls: "animate-spin" })} ${esc(label)}`;

    async function saveDevice(payload) {
      submitBtn.innerHTML = spinner("Saving…");
      const res = isEdit
        ? await sendJSON("PUT", `/api/devices/${d.id}`, payload)
        : await sendJSON("POST", "/api/devices", payload);
      if (res.ok && res.data.ok) { toast(isEdit ? "Node updated" : "Node added"); close(); onDone(); return; }
      toast(res.data.error || res.data.reason || "Couldn't save node", "error");
      submitBtn.innerHTML = origLabel;
      submitBtn.disabled = false;
    }

    overlay.querySelector("[data-node-form]").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const payload = {};
      overlay.querySelectorAll("[data-field]").forEach((el) => { payload[el.getAttribute("data-field")] = el.value.trim(); });
      submitBtn.disabled = true;

      // On add, ping the address first and refuse the node if it doesn't answer —
      // a wrong/typo'd address would otherwise sit forever looking like a permanent
      // outage. The host must be up at provisioning time to be accepted.
      if (!isEdit && payload.ip_address) {
        submitBtn.innerHTML = spinner(`Pinging ${payload.ip_address}…`);
        const chk = await postJSON("/api/devices/check", { ip_address: payload.ip_address });
        if (chk.ok && chk.data.reachable === false) {
          toast(`Couldn't reach ${payload.ip_address} — ${chk.data.detail}. Node not added.`, "error");
          submitBtn.innerHTML = origLabel;
          submitBtn.disabled = false;
          return;
        }
        if (chk.ok && chk.data.reachable === true) {
          toast(chk.data.detail);   // e.g. "host is up — 12.3 ms avg"
        }
      }
      await saveDevice(payload);
    });

    const delBtn = overlay.querySelector("[data-delete]");
    if (delBtn) {
      let armed = false, t;
      delBtn.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          delBtn.innerHTML = `${icon("delete", { size: 16 })} Tap again to confirm`;
          delBtn.classList.add("bg-error", "text-on-error");
          t = setTimeout(() => {
            armed = false;
            delBtn.innerHTML = `${icon("delete", { size: 16 })} Delete`;
            delBtn.classList.remove("bg-error", "text-on-error");
          }, 3000);
          return;
        }
        clearTimeout(t);
        delBtn.disabled = true;
        const res = await sendJSON("DELETE", `/api/devices/${d.id}`);
        if (res.ok && res.data.ok) { toast("Node deleted"); close(); onDone(); }
        else { toast(res.data.reason || res.data.error || "Couldn't delete", "error"); delBtn.disabled = false; }
      });
    }
  }

  function renderNodes(page) {
    let selectedDate = null;  // null = live view of all nodes; else a YYYY-MM-DD drill-down
    page.innerHTML = `<div class="w-full max-w-5xl mx-auto flex flex-col gap-section-gap px-4 md:px-8 py-6 md:py-8">
      <header>
        <h2 class="font-display text-display text-primary mb-2">Nodes Management</h2>
        <p class="font-body-lg text-body-lg text-on-surface-variant">Live state and uptime across the shared infrastructure.</p>
      </header>
      <section class="bg-surface border border-outline-variant rounded-md p-4 md:p-container-margin">
        <div class="flex flex-col md:flex-row md:justify-between md:items-center mb-4 gap-2">
          <h3 class="font-headline-md text-headline-md text-primary">Network Health (30 Days)</h3>
          <div class="flex items-center gap-4 text-on-surface-variant font-label-xs text-label-xs">
            <div class="flex items-center gap-1.5"><div class="w-3 h-3 bg-surface-container rounded-[2px]"></div><span>No Data</span></div>
            <div class="flex items-center gap-1.5"><div class="w-3 h-3 bg-surface-container-high rounded-[2px]"></div><span>Operational</span></div>
            <div class="flex items-center gap-1.5"><div class="w-3 h-3 bg-[#450a0a] rounded-[2px]"></div><span>Outage</span></div>
          </div>
        </div>
        <div id="heatmap" class="heatmap-grid"></div>
        <div class="flex justify-between mt-3 text-on-surface-variant font-label-xs text-label-xs uppercase tracking-widest"><span>30 Days Ago</span><span>Today</span></div>
        <p class="text-outline font-label-xs text-label-xs mt-2">Tip: click a day to see which nodes were down then.</p>
      </section>
      <section class="flex flex-col gap-4">
        <div class="flex justify-between items-center gap-2">
          <h3 id="nodes-title" class="font-headline-md text-headline-md text-primary">Active Nodes</h3>
          <div class="flex items-center gap-2">
            <button id="nodes-clear" class="hidden items-center gap-1 text-on-surface-variant hover:text-primary font-label-md text-label-md px-3 py-1.5 rounded-md border border-outline-variant hover:bg-surface-container transition-colors">
              ${icon("close", { size: 16 })} Show all</button>
            <button id="add-node" class="flex items-center gap-1 bg-primary text-surface font-label-md text-label-md px-3 py-1.5 rounded-md active:scale-95 transition-transform">
              ${icon("add", { size: 16 })} Add node</button>
          </div>
        </div>
        <div id="nodes" class="grid grid-cols-1 gap-2">${loading("nodes")}</div>
      </section>
    </div>`;

    async function loadNodes() {
      const title = $("#nodes-title", page);
      const clear = $("#nodes-clear", page);
      const box = $("#nodes", page);
      try {
        if (selectedDate) {
          title.textContent = `Nodes down on ${selectedDate}`;
          clear.classList.remove("hidden");
          clear.classList.add("flex");
          const nodes = await getJSON(`/api/nodes?day=${encodeURIComponent(selectedDate)}`);
          if (currentPath() !== "#/nodes") return;
          box.innerHTML = nodes.length
            ? nodes.map(dayNodeRow).join("")
            : `<div class="border border-outline-variant bg-surface-container-low rounded-md p-6 flex items-center gap-3 text-on-surface-variant">
                ${icon("task_alt", { cls: "text-emerald-400" })}<span class="font-body-sm">No nodes were down on ${esc(selectedDate)}.</span></div>`;
        } else {
          title.textContent = "Active Nodes";
          clear.classList.add("hidden");
          clear.classList.remove("flex");
          const nodes = await getJSON("/api/nodes");
          if (currentPath() !== "#/nodes") return;
          box.innerHTML = nodes.map(nodeRow).join("");
        }
      } catch (e) {
        box.innerHTML = errorBox(e.message);
      }
    }

    async function loadHeatmap() {
      try {
        const cells = await getJSON("/api/heatmap");
        if (currentPath() !== "#/nodes") return;
        const grid = $("#heatmap", page);
        const paint = () => {
          grid.innerHTML = heatmapCells(cells, selectedDate);
          grid.querySelectorAll('[data-date]:not([data-state="nodata"])').forEach((el) => {
            el.addEventListener("click", () => {
              const date = el.getAttribute("data-date");
              selectedDate = selectedDate === date ? null : date;
              paint();
              loadNodes();
            });
          });
        };
        paint();
      } catch (e) { /* heatmap is best-effort; node list still loads */ }
    }

    $("#nodes-clear", page).addEventListener("click", () => {
      selectedDate = null;
      loadHeatmap();
      loadNodes();
    });

    const afterChange = () => { loadHeatmap(); loadNodes(); };

    $("#add-node", page).addEventListener("click", async () => {
      try {
        const devices = await getJSON("/api/devices");
        openNodeModal(null, devices, afterChange);
      } catch (e) { toast("Couldn't load nodes", "error"); }
    });

    // Edit on row click (delegated, so it survives auto-refresh re-renders).
    // Only the live all-nodes view has [data-edit]; drill-down rows don't.
    $("#nodes", page).addEventListener("click", async (ev) => {
      const row = ev.target.closest("[data-edit]");
      if (!row) return;
      const id = Number(row.getAttribute("data-edit"));
      try {
        const devices = await getJSON("/api/devices");
        const dev = devices.find((x) => x.id === id);
        if (dev) openNodeModal(dev, devices, afterChange);
      } catch (e) { toast("Couldn't load node", "error"); }
    });

    return async function load() {
      await Promise.all([loadHeatmap(), loadNodes()]);
    };
  }

  // --- Logs -----------------------------------------------------------------
  const logState = { q: "", offset: 0, limit: 25 };

  function logRows(entries) {
    if (!entries.length) {
      return `<tr><td colspan="6" class="px-4 text-center align-middle text-on-surface-variant font-body-sm" style="height:55vh">
        <div class="flex flex-col items-center gap-2">${icon("search", { size: 28, cls: "text-outline" })}<span>No matching incidents.</span></div>
      </td></tr>`;
    }
    return entries.map((e) => {
      return `<tr class="hover:bg-surface-container-high/50 transition-colors">
        <td class="px-4 py-3 text-on-surface">${esc(fmtTime(e.timestamp))}</td>
        <td class="px-4 py-3 text-primary">${esc(e.incident)}</td>
        <td class="px-4 py-3 text-on-surface-variant">${esc(e.region)} / ${esc(e.name)}</td>
        <td class="px-4 py-3 text-on-surface-variant">${esc(e.duration_label)}</td>
        <td class="px-4 py-3 text-on-surface truncate max-w-[220px]" title="${esc(e.resolution_notes || "")}">${esc(e.root_cause)}</td>
        <td class="px-4 py-3 text-on-surface-variant">${esc(e.acknowledged_by || "—")}</td>
      </tr>`;
    }).join("");
  }

  function pager(data) {
    const start = data.total === 0 ? 0 : data.offset + 1;
    const end = Math.min(data.offset + data.limit, data.total);
    const prevDis = data.offset <= 0 ? "disabled" : "";
    const nextDis = end >= data.total ? "disabled" : "";
    return `<div class="border-t border-outline-variant bg-surface-container-low px-4 py-3 flex items-center justify-between">
      <span class="font-body-sm text-body-sm text-on-surface-variant">Showing ${start} to ${end} of ${data.total} entries</span>
      <div class="flex items-center gap-2">
        <button data-page="prev" class="p-1 text-outline hover:text-primary disabled:opacity-50 disabled:cursor-not-allowed transition-colors" ${prevDis}>${icon("chevron_left", { size: 20 })}</button>
        <button data-page="next" class="p-1 text-outline hover:text-primary disabled:opacity-50 disabled:cursor-not-allowed transition-colors" ${nextDis}>${icon("chevron_right", { size: 20 })}</button>
      </div>
    </div>`;
  }

  function renderLogs(page) {
    logState.offset = 0;
    page.innerHTML = `<div class="w-full px-4 md:px-8 py-6 md:py-8 flex flex-col gap-6 flex-1 min-h-0">
      <div class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 class="font-display text-display text-primary">Historical Logs</h1>
          <p class="font-body-sm text-body-sm text-on-surface-variant mt-1">Resolved incidents, causes, and durations across the network.</p>
        </div>
        <div class="relative w-full md:w-72">
          <span class="absolute left-3 top-1/2 -translate-y-1/2 text-outline">${icon("search", { size: 18 })}</span>
          <input id="log-search" type="text" placeholder="Search node, region, cause…" class="w-full bg-surface border border-outline-variant rounded-md py-2 pl-9 pr-3 text-body-sm text-on-surface placeholder-outline focus:border-primary focus:outline-none transition-colors" />
        </div>
      </div>
      <div class="bg-surface-dim border border-outline-variant rounded-md overflow-hidden flex flex-col flex-1 min-h-[400px]">
        <div class="overflow-auto flex-1 min-h-0">
          <table class="w-full text-left border-collapse min-w-[760px]">
            <thead><tr class="bg-surface-container border-b border-outline-variant text-on-surface-variant font-label-xs text-label-xs uppercase tracking-wider">
              <th class="px-4 py-3 font-semibold">Timestamp</th>
              <th class="px-4 py-3 font-semibold">Incident</th>
              <th class="px-4 py-3 font-semibold">Region / Node</th>
              <th class="px-4 py-3 font-semibold">Duration</th>
              <th class="px-4 py-3 font-semibold">Root Cause</th>
              <th class="px-4 py-3 font-semibold">Acked By</th>
            </tr></thead>
            <tbody id="log-body" class="font-mono-data text-mono-data divide-y divide-surface-container-high"></tbody>
          </table>
        </div>
        <div id="log-pager" class="mt-auto"></div>
      </div>
    </div>`;

    async function load() {
      const body = $("#log-body", page);
      try {
        const data = await getJSON(`/api/logs?q=${encodeURIComponent(logState.q)}&limit=${logState.limit}&offset=${logState.offset}`);
        if (currentPath() !== "#/logs") return;
        body.innerHTML = logRows(data.entries);
        $("#log-pager", page).innerHTML = pager(data);
        $('[data-page="prev"]', page)?.addEventListener("click", () => { logState.offset = Math.max(0, logState.offset - logState.limit); load(); });
        $('[data-page="next"]', page)?.addEventListener("click", () => { logState.offset += logState.limit; load(); });
      } catch (e) {
        body.innerHTML = `<tr><td colspan="6">${errorBox(e.message)}</td></tr>`;
      }
    }

    let deb;
    $("#log-search", page).addEventListener("input", (ev) => {
      clearTimeout(deb);
      deb = setTimeout(() => { logState.q = ev.target.value; logState.offset = 0; load(); }, 250);
    });
    return load;
  }

  // --- Team (worker directory; plan §8.5) -----------------------------------
  const WORKER_ROLES = ["owner", "operator", "tech"];
  const ROLE_CHIP = {
    owner: "text-amber-400 border-amber-400/30 bg-amber-400/10",
    operator: "text-blue-400 border-blue-400/30 bg-blue-400/10",
    tech: "text-emerald-400 border-emerald-400/30 bg-emerald-400/10",
  };

  function workerCard(w) {
    const chip = ROLE_CHIP[w.role] || ROLE_CHIP.tech;
    const channel = BRAND.channels[w.role] || w.role;
    const dim = w.is_active ? "" : "opacity-50";
    return `<div data-worker="${w.id}" class="bg-surface border border-outline-variant rounded-md p-4 flex flex-col gap-2 hover:bg-surface-container-low transition-colors cursor-pointer ${dim}">
      <div class="flex items-center justify-between gap-2">
        <h4 class="font-body-lg text-body-lg text-primary font-medium truncate">${esc(w.name)}</h4>
        <span class="font-label-xs text-label-xs ${chip} border px-2 py-0.5 rounded-sm uppercase">${esc(w.role)}</span>
      </div>
      <p class="font-body-sm text-body-sm text-on-surface-variant">${esc(w.region || "all regions")}${w.is_active ? "" : " · inactive"}</p>
      <p class="font-mono-data text-on-surface-variant text-[11px] truncate">${icon("notifications", { size: 12, cls: "inline align-text-bottom" })} ${esc(channel)}</p>
    </div>`;
  }

  function openWorkerModal(worker, onDone) {
    const isEdit = !!worker;
    const w = worker || {};
    const roleOpts = WORKER_ROLES.map((r) => ({ value: r, label: r }));
    const activeOpts = [{ value: 1, label: "Active" }, { value: 0, label: "Inactive" }];
    const form = `
      <div class="sticky top-0 bg-surface-container border-b border-outline-variant px-5 py-4 flex items-center justify-between">
        <h3 class="font-headline-md text-headline-md text-primary flex items-center gap-2">
          ${icon(isEdit ? "edit" : "add_circle", { size: 20 })} ${isEdit ? "Edit worker" : "Add worker"}</h3>
        <button data-close class="text-on-surface-variant hover:text-primary p-1 rounded-full hover:bg-surface-container-high">${icon("close", { size: 20 })}</button>
      </div>
      <form data-worker-form class="p-5 grid grid-cols-1 sm:grid-cols-2 gap-4">
        ${field("Name", "name", w.name, { required: true, full: true, placeholder: "e.g. Suresh" })}
        ${field("Role", "role", w.role || "tech", { options: roleOpts })}
        ${field("Status", "is_active", w.is_active != null ? w.is_active : 1, { options: activeOpts })}
        ${field("Region", "region", w.region, { placeholder: "village / area (blank = all)" })}
        ${field("Notes", "notes", w.notes, { full: true, placeholder: "optional" })}
        <div class="sm:col-span-2 text-label-xs text-on-surface-variant bg-surface-container-low border border-outline-variant rounded-md px-3 py-2">
          ${icon("notifications", { size: 14, cls: "inline align-text-bottom" })}
          Alerts route by <span class="text-primary">role</span> — this person subscribes to the
          <span class="text-primary font-mono-data" data-role-channel>${esc(BRAND.channels[w.role || "tech"] || (w.role || "tech"))}</span> channel on ntfy.
        </div>
        <div class="sm:col-span-2 flex items-center justify-between gap-2 pt-2 border-t border-outline-variant">
          <div>${isEdit ? `<button type="button" data-delete class="flex items-center gap-1 text-error hover:bg-error/10 border border-error/30 font-label-md text-label-md px-3 py-2 rounded-md transition-colors">${icon("delete", { size: 16 })} Delete</button>` : ""}</div>
          <div class="flex items-center gap-2">
            <button type="button" data-close class="text-on-surface-variant hover:text-primary font-label-md text-label-md px-4 py-2 rounded-md hover:bg-surface-container-high">Cancel</button>
            <button type="submit" class="bg-primary text-surface font-label-md text-label-md px-4 py-2 rounded-md active:scale-95 transition-transform">${isEdit ? "Save changes" : "Add worker"}</button>
          </div>
        </div>
      </form>`;

    const { overlay, close } = openModal(form);
    overlay.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
    const roleSel = overlay.querySelector('[data-field="role"]');
    const chanHint = overlay.querySelector("[data-role-channel]");
    if (roleSel && chanHint) roleSel.addEventListener("change", () => {
      chanHint.textContent = BRAND.channels[roleSel.value] || roleSel.value;
    });
    overlay.querySelector("[data-worker-form]").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const payload = {};
      overlay.querySelectorAll("[data-field]").forEach((el) => { payload[el.getAttribute("data-field")] = el.value; });
      const submitBtn = ev.target.querySelector('[type="submit"]');
      submitBtn.disabled = true;
      const res = isEdit
        ? await sendJSON("PUT", `/api/workers/${w.id}`, payload)
        : await sendJSON("POST", "/api/workers", payload);
      if (res.ok && res.data.ok) { toast(isEdit ? "Worker updated" : "Worker added"); close(); onDone(); }
      else { toast(res.data.error || res.data.reason || "Couldn't save worker", "error"); submitBtn.disabled = false; }
    });

    const delBtn = overlay.querySelector("[data-delete]");
    if (delBtn) {
      let armed = false, t;
      delBtn.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          delBtn.innerHTML = `${icon("delete", { size: 16 })} Tap again to confirm`;
          delBtn.classList.add("bg-error", "text-on-error");
          t = setTimeout(() => { armed = false; delBtn.innerHTML = `${icon("delete", { size: 16 })} Delete`; delBtn.classList.remove("bg-error", "text-on-error"); }, 3000);
          return;
        }
        clearTimeout(t);
        delBtn.disabled = true;
        const res = await sendJSON("DELETE", `/api/workers/${w.id}`);
        if (res.ok && res.data.ok) { toast("Worker deleted"); close(); onDone(); }
        else { toast(res.data.error || res.data.reason || "Couldn't delete", "error"); delBtn.disabled = false; }
      });
    }
  }

  function renderTeam(page) {
    page.innerHTML = `<div class="w-full max-w-5xl mx-auto px-4 md:px-8 py-6 md:py-8 flex flex-col gap-6">
      <header class="flex items-center justify-between gap-2">
        <div>
          <h2 class="font-display text-display text-primary">Team</h2>
          <p class="font-body-lg text-body-lg text-on-surface-variant">Workers, roles, and alert routing.</p>
        </div>
        <button id="add-worker" class="flex items-center gap-1 bg-primary text-surface font-label-md text-label-md px-3 py-2 rounded-md active:scale-95 transition-transform whitespace-nowrap">${icon("add", { size: 16 })} Add worker</button>
      </header>
      <div id="team-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">${loading("team")}</div></div>`;

    async function load() {
      const grid = $("#team-grid", page);
      try {
        const workers = await getJSON("/api/workers");
        if (currentPath() !== "#/team") return;
        grid.innerHTML = workers.length
          ? workers.map(workerCard).join("")
          : `<div class="sm:col-span-2 lg:col-span-3 border border-outline-variant bg-surface-container-low rounded-md p-6 flex items-center gap-3 text-on-surface-variant">
              ${icon("group", { cls: "text-outline" })}<span class="font-body-sm">No workers yet — add your owner and field technicians.</span></div>`;
      } catch (e) { grid.innerHTML = errorBox(e.message); }
    }

    $("#add-worker", page).addEventListener("click", () => openWorkerModal(null, load));
    $("#team-grid", page).addEventListener("click", async (ev) => {
      const card = ev.target.closest("[data-worker]");
      if (!card) return;
      const id = Number(card.getAttribute("data-worker"));
      try {
        const workers = await getJSON("/api/workers");
        const w = workers.find((x) => x.id === id);
        if (w) openWorkerModal(w, load);
      } catch (e) { toast("Couldn't load worker", "error"); }
    });

    return load;
  }

  // --- Settings (account / security / channel test) -------------------------
  // Operational tunables (poll interval, thresholds, escalation timings, ntfy URL,
  // org/locale) are configured via WISP_* environment variables and applied on
  // restart — see config.py. This page keeps the in-UI essentials: PIN, channel
  // test, and backup.
  function renderSettings(page) {
    page.innerHTML = `<div class="w-full max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-8 flex flex-col gap-6">
      <header><h2 class="font-display text-display text-primary">Settings</h2>
        <p class="font-body-lg text-body-lg text-on-surface-variant">Access, channel checks, and backup. Detection &amp; alerting tunables are set via environment variables (restart to apply).</p></header>
      <div id="settings-body">${loading("settings")}</div></div>`;

    let workers = [];

    function channelsBlock() {
      const base = (BRAND.ntfy_base_url || "https://ntfy.sh").replace(/\/$/, "");
      const ch = BRAND.channels || {};
      const row = (role, desc) => `<div class="flex items-center justify-between gap-2 py-1.5 border-b border-outline-variant/50 last:border-0">
        <div><span class="font-label-xs text-label-xs uppercase ${(ROLE_CHIP[role] || ROLE_CHIP.tech)} border px-1.5 py-0.5 rounded-sm">${role}</span>
          <span class="font-body-sm text-on-surface-variant ml-2">${desc}</span></div>
        <code class="font-mono-data text-[11px] text-primary truncate max-w-[45%]" title="${esc(base)}/${esc(ch[role] || role)}">${esc(ch[role] || role)}</code>
      </div>`;
      return `<div class="mt-6 pt-5 border-t border-outline-variant flex flex-col gap-2">
        <h4 class="font-headline-md text-headline-md text-primary flex items-center gap-2">${icon("hub", { size: 18 })} Alert channels</h4>
        <p class="font-body-sm text-body-sm text-on-surface-variant">Each person subscribes in the ntfy app (server <code class="font-mono-data text-[11px]">${esc(base)}</code>) to the topic for their role.</p>
        <div class="bg-surface-container-low border border-outline-variant rounded-md px-3 py-1">
          ${row("owner", "escalations + uplink alerts")}
          ${row("operator", "everything (full visibility)")}
          ${row("tech", "device down / still-down / restored")}
        </div>
      </div>`;
    }

    function testSendBlock() {
      const opts = WORKER_ROLES.map((r) =>
        `<option value="${r}">${r} channel</option>`).join("");
      return `<div class="mt-6 pt-5 border-t border-outline-variant flex flex-col gap-3">
        <h4 class="font-headline-md text-headline-md text-primary flex items-center gap-2">${icon("notifications", { size: 18 })} Send test alert</h4>
        <p class="font-body-sm text-body-sm text-on-surface-variant">Confirm a channel works before a real outage depends on it — fires a test push to that topic.</p>
        <div class="flex flex-col sm:flex-row gap-2">
          <select data-test-target class="flex-1 bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary appearance-none">${opts}</select>
          <button id="send-test" class="bg-transparent border border-outline-variant text-primary hover:bg-surface-container font-label-md text-label-md px-4 py-2 rounded-md transition-colors whitespace-nowrap">Send test</button>
        </div>
        <div id="test-result" class="font-label-xs text-label-xs"></div>
      </div>`;
    }

    function backupBlock() {
      return `<div class="mt-6 pt-5 border-t border-outline-variant flex flex-col gap-3">
        <h4 class="font-headline-md text-headline-md text-primary flex items-center gap-2">${icon("download", { size: 18 })} Backup</h4>
        <p class="font-body-sm text-body-sm text-on-surface-variant">Download a full copy of the database — config, PIN, team directory, and history.</p>
        <a href="/api/backup" class="self-start inline-flex items-center gap-1 bg-transparent border border-outline-variant text-primary hover:bg-surface-container font-label-md text-label-md px-4 py-2 rounded-md transition-colors">${icon("download", { size: 16 })} Download backup</a>
      </div>`;
    }

    function pinForm() {
      return `<div class="mt-6 pt-5 border-t border-outline-variant flex flex-col gap-3">
        <h4 class="font-headline-md text-headline-md text-primary flex items-center gap-2">${icon("vpn_key", { size: 18 })} Change PIN</h4>
        <form data-pin-form class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input data-pin="old" type="password" placeholder="Current PIN" autocomplete="off" class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />
          <input data-pin="new" type="password" placeholder="New PIN (min 4 digits)" autocomplete="new-password" class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />
          <button type="submit" class="sm:col-span-2 bg-transparent border border-outline-variant text-primary hover:bg-surface-container font-label-md text-label-md px-4 py-2 rounded-md transition-colors">Update PIN</button>
        </form></div>`;
    }

    function paint() {
      if (currentPath() !== "#/settings") return;
      $("#settings-body", page).innerHTML =
        `<div class="flex flex-col gap-1">${pinForm()}${channelsBlock()}${testSendBlock()}${backupBlock()}</div>`;
      wire();
    }

    function wire() {
      const box = $("#settings-body", page);
      const pf = $("[data-pin-form]", box);
      if (pf) pf.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const oldp = $('[data-pin="old"]', pf).value;
        const newp = $('[data-pin="new"]', pf).value;
        const res = await postJSON("/api/settings/pin", { old: oldp, new: newp });
        if (res.ok && res.data.ok) { toast("PIN updated"); pf.reset(); }
        else { toast(res.data.error || "Couldn't update PIN", "error"); }
      });
      const st = $("#send-test", box);
      if (st) st.addEventListener("click", async () => {
        const target = $("[data-test-target]", box).value;
        const rl = $("#test-result", box);
        st.disabled = true;
        rl.textContent = "Sending…";
        rl.className = "font-label-xs text-label-xs text-on-surface-variant";
        const res = await postJSON("/api/channels/test", { target });
        if (res.ok) {
          const ok = res.data.ok;
          rl.textContent = (ok ? "✓ " : "✗ ") + (res.data.detail || "");
          rl.className = "font-label-xs text-label-xs " + (ok ? "text-emerald-400" : "text-error");
        } else {
          rl.textContent = res.data.error || "Test failed";
          rl.className = "font-label-xs text-label-xs text-error";
        }
        st.disabled = false;
      });
    }

    return async function load() {
      try { workers = await getJSON("/api/workers"); } catch (e) { workers = []; }
      paint();
    };
  }

  // --- auth gate (shared-PIN login; plan §8.2) ------------------------------
  let _loginShown = false;

  async function fetchAuthStatus() {
    try {
      const r = await fetch("/api/auth/status", { headers: { Accept: "application/json" } });
      if (!r.ok) return { pin_set: true, authed: false };
      const st = await r.json();
      if (st.org_name) BRAND.org_name = st.org_name;
      if (st.timezone) BRAND.timezone = st.timezone;
      if (st.channels) BRAND.channels = st.channels;
      if (st.ntfy_base_url) BRAND.ntfy_base_url = st.ntfy_base_url;
      document.title = `${BRAND.org_name} — Network Monitor`;
      return st;
    } catch (e) { return { pin_set: true, authed: false }; }
  }

  // Called on a 401 anywhere, or at startup when not signed in. Replaces the whole
  // app shell with a full-page PIN gate (first-run variant when no PIN exists yet).
  async function requireLogin() {
    if (_loginShown) return;
    _loginShown = true;
    clearInterval(_refreshTimer);
    const st = await fetchAuthStatus();
    renderLogin(!st.pin_set);
  }

  function renderLogin(setupMode) {
    const root = document.getElementById("root");
    const title = setupMode ? "Set a PIN" : "Enter PIN";
    const hint = setupMode
      ? "Choose a numeric PIN (at least 4 digits) to protect this dashboard."
      : "Enter the shared PIN to access the dashboard.";
    root.innerHTML = `
      <div class="min-h-screen flex items-center justify-center px-4 bg-background">
        <div class="w-full max-w-xs flex flex-col items-center gap-6">
          <div class="flex items-center gap-2">${icon("hub", { size: 30, cls: "text-primary" })}
            <h1 class="font-headline-lg text-headline-lg font-bold tracking-tighter text-primary">${esc(BRAND.org_name)}</h1></div>
          <div class="w-full bg-surface-container border border-outline-variant rounded-xl p-6 flex flex-col gap-5">
            <div class="flex flex-col items-center gap-2 text-center">
              <div class="w-12 h-12 rounded-full bg-surface-container-high flex items-center justify-center text-primary">${icon(setupMode ? "vpn_key" : "lock", { size: 24 })}</div>
              <h2 class="font-headline-md text-headline-md text-primary">${title}</h2>
              <p class="font-body-sm text-body-sm text-on-surface-variant">${hint}</p>
            </div>
            <form id="pin-form" class="flex flex-col gap-4">
              <input id="pin-input" type="password" inputmode="numeric" pattern="[0-9]*"
                autocomplete="${setupMode ? "new-password" : "one-time-code"}" maxlength="12"
                placeholder="••••" aria-label="PIN"
                class="w-full text-center tracking-[0.5em] bg-surface border border-outline-variant text-primary text-headline-md rounded-md px-3 py-3 outline-none focus:ring-1 focus:ring-primary" />
              <div id="login-err" class="hidden text-center font-label-xs text-label-xs text-error"></div>
              <button id="pin-submit" type="submit" class="bg-primary text-surface font-label-md text-label-md py-3 rounded-md active:scale-95 transition disabled:opacity-40">${setupMode ? "Set PIN & continue" : "Unlock"}</button>
            </form>
          </div>
        </div>
      </div>`;

    const input = $("#pin-input", root);
    const err = $("#login-err", root);
    const submit = $("#pin-submit", root);
    const form = $("#pin-form", root);
    const showErr = (m) => { err.textContent = m; err.classList.remove("hidden"); };

    submit.disabled = true;
    setTimeout(() => input.focus(), 50);   // pop the system keyboard
    input.addEventListener("input", () => {
      const cleaned = input.value.replace(/\D/g, "").slice(0, 12);  // digits only
      if (cleaned !== input.value) input.value = cleaned;
      err.classList.add("hidden");
      submit.disabled = input.value.length < 4;
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const pin = input.value;
      if (pin.length < 4) return;
      submit.disabled = true;
      const url = setupMode ? "/api/auth/setup" : "/api/login";
      let r;
      try {
        r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
      } catch (e) { showErr("Network error — try again"); submit.disabled = false; return; }
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        _loginShown = false;
        if (!location.hash) location.hash = "#/";
        route();
        return;
      }
      input.value = ""; submit.disabled = true;
      showErr(data.error || (setupMode ? "Couldn't set PIN" : "Incorrect PIN"));
    });
  }

  function fmtAge(s) {
    if (s == null) return "no data yet";
    if (s < 90) return `${s}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    return `${Math.round(s / 3600)}h ago`;
  }

  async function refreshHeartbeat() {
    const hb = document.getElementById("heartbeat");
    if (!hb) return;
    try {
      const s = await getJSON("/api/summary");
      const age = s.monitor_age_s;
      // Staleness is decided server-side (Config.stale_threshold_s) so the banner,
      // the summary, and the watchdog that pages the owner all agree. Fall back to
      // the old ~5-min heuristic only if an older server omits the flag.
      const stale = s.monitor_stale != null
        ? s.monitor_stale
        : (age == null || age > 300);
      hb.innerHTML = `${icon("monitoring", { size: 14 })} <span class="${stale ? "text-error" : "text-emerald-400"}">Monitor: ${stale ? "⚠ stale" : "healthy"}</span> · ${fmtAge(age)}`;
    } catch (e) { /* leave the placeholder */ }
  }

  function wireHeader() {
    const btn = document.getElementById("account-btn");
    const menu = document.getElementById("account-menu");
    if (!btn || !menu) return;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.toggle("hidden");
      if (!menu.classList.contains("hidden")) refreshHeartbeat();
    });
    document.addEventListener("click", () => menu.classList.add("hidden"));
    const lo = document.getElementById("logout-btn");
    if (lo) lo.addEventListener("click", async () => {
      await postJSON("/api/logout");
      requireLogin();
    });
  }

  // --- router + auto-refresh ------------------------------------------------
  const ROUTES = {
    "#/": renderDashboard,
    "#/nodes": renderNodes,
    "#/team": renderTeam,
    "#/settings": renderSettings,
    "#/logs": renderLogs,
  };
  const AUTO_REFRESH = new Set(["#/", "#/nodes"]);
  let _refreshTimer = null;
  let _activeLoad = null;

  function formFocused() {
    const a = document.activeElement;
    return a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
  }

  function route() {
    clearInterval(_refreshTimer);
    document.getElementById("root").innerHTML = shell();
    wireHeader();
    const page = $("#page");
    const render = ROUTES[currentPath()] || renderDashboard;
    const load = render(page);
    _activeLoad = load;
    load();
    // Only the live views auto-refresh; Settings/Team/Logs are edited or on-demand,
    // so a background reload would clobber in-progress form input.
    if (AUTO_REFRESH.has(currentPath())) {
      _refreshTimer = setInterval(() => {
        if (!formFocused() && _activeLoad === load) load();
      }, 15000);
    }
  }

  window.addEventListener("hashchange", () => { if (!_loginShown) route(); });
  // One global timer ticks every visible outage duration; harmless when none exist.
  setInterval(tickDurations, 1000);
  window.addEventListener("DOMContentLoaded", async () => {
    const st = await fetchAuthStatus();
    if (!st.authed) { renderLogin(!st.pin_set); return; }
    if (!location.hash) location.hash = "#/";
    route();
  });
})();
