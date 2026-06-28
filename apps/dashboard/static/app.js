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

  // Children dragged UNREACHABLE behind this DOWN node: they're topology-suppressed
  // (no card of their own), so name the blast radius here. Cap the list, count the rest.
  function affectedLine(children) {
    if (!children || !children.length) return "";
    const shown = children.slice(0, 4).map(esc).join(", ");
    const more = children.length > 4 ? ` +${children.length - 4} more` : "";
    return `<p class="font-label-xs text-label-xs text-on-surface-variant mt-1 flex items-start gap-1">
      ${icon("account_tree", { size: 13 })}
      <span><span class="text-error">${children.length} downstream node${children.length === 1 ? "" : "s"} unreachable:</span> ${shown}${more}</span>
    </p>`;
  }

  // Operators marked present on the day this outage began — "who was around when it
  // broke". Open outages started today say "today"; a recovered one names the day.
  function onDutyLine(o) {
    if (!o.on_duty || !o.on_duty.length) return "";
    const when = o.status === "pending_postmortem" ? "that day" : "today";
    return `<p class="font-label-xs text-label-xs text-on-surface-variant mt-1 flex items-start gap-1">
      ${icon("group", { size: 13 })}
      <span>On duty ${when}: ${o.on_duty.map(esc).join(", ")}</span>
    </p>`;
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
          ${affectedLine(o.affected_children)}
          ${onDutyLine(o)}
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
  const NODE_STATE_MAP = {
    UP: { dot: "bg-primary", text: "text-primary", icon: "cell_tower", glow: "0 0 4px #ffffff", op: "" },
    // DEGRADED = still UP and replying, just sustained packet loss (>= WISP_LOSS_DEGRADED,
    // default 5%) — a "flaky link" warning, not an outage (nobody is paged for it). Amber,
    // not red, so it reads as caution and isn't mistaken for a DOWN node.
    DEGRADED: { dot: "bg-amber-400", text: "text-amber-400", icon: "cell_tower", glow: "0 0 4px #fbbf24", op: "" },
    DOWN: { dot: "bg-error", text: "text-error", icon: "wifi_off", glow: "0 0 4px #ffb4ab", op: "" },
    UNREACHABLE: { dot: "bg-outline", text: "text-outline", icon: "router", glow: "", op: "opacity-60" },
  };

  // SNMP port health, surfaced live on a switch's row (was previously only visible
  // inside the edit modal). Red when a monitored uplink port is alarming; a muted count
  // when ports are merely watched; nothing for a switch whose ports aren't watched yet.
  function portBadge(n) {
    const p = n.ports;
    if (!p || !p.total) return "";
    if (p.down > 0) {
      return `<span title="${p.down} monitored port(s) down (SNMP)" class="font-label-xs text-label-xs text-error border border-error/30 bg-error/10 px-2 py-0.5 rounded-md whitespace-nowrap flex items-center gap-1">${icon("power_off", { size: 12 })} ${p.down}/${p.monitored} ports down</span>`;
    }
    if (p.monitored > 0) {
      return `<span title="${p.monitored} of ${p.total} discovered port(s) watched (SNMP)" class="font-label-xs text-label-xs text-outline border border-outline-variant px-2 py-0.5 rounded-md whitespace-nowrap hidden sm:flex items-center gap-1">${icon("visibility", { size: 12 })} ${p.monitored} watched</span>`;
    }
    return "";
  }

  // ctx: { depth, hasChildren, expanded, stats:{affected,total}, suppressedBy }
  function nodeRow(n, ctx = {}) {
    const { depth = 0, hasChildren = false, expanded = false, stats = null, suppressedBy = null } = ctx;
    const s = NODE_STATE_MAP[n.state] || NODE_STATE_MAP.UP;
    // Uptime % is a 24h stat, not the live state — keep red off it so only an actual
    // DOWN node reads red. Healthy = green, anything below = amber (caution).
    const pctColor = n.uptime_pct >= 99.9 ? "text-primary" : "text-amber-400";

    // disclosure caret for branches; an alignment spacer for leaves.
    const caret = hasChildren
      ? `<button data-toggle="${n.id}" title="${expanded ? "Collapse" : "Expand"} subtree"
           class="shrink-0 w-5 h-5 flex items-center justify-center text-on-surface-variant hover:text-primary">${icon(expanded ? "expand_more" : "chevron_right", { size: 18 })}</button>`
      : `<span class="shrink-0 w-5 h-5 inline-flex items-center justify-center text-outline">${depth > 0 ? icon("subdirectory_arrow_right", { size: 14 }) : ""}</span>`;

    // roll-up chip on a collapsed branch: surface hidden trouble, else fan-out size.
    let rollup = "";
    if (hasChildren && !expanded && stats) {
      rollup = stats.affected > 0
        ? `<span class="font-label-xs text-label-xs text-error border border-error/30 bg-error/10 px-2 py-0.5 rounded-md whitespace-nowrap">${stats.affected} affected</span>`
        : `<span class="font-label-xs text-label-xs text-outline whitespace-nowrap hidden sm:inline">${stats.total} downstream</span>`;
    }

    // why is this node UNREACHABLE? name the nearest DOWN ancestor.
    const why = (n.state === "UNREACHABLE" && suppressedBy)
      ? `<p class="font-label-xs text-label-xs text-outline mt-0.5 truncate flex items-center gap-1">${icon("subdirectory_arrow_right", { size: 12 })} suppressed · ${esc(suppressedBy.name)} is DOWN</p>`
      : `<p class="font-mono-data text-on-surface-variant mt-0.5 truncate text-[11px]">${esc(n.type || "node")} · ${esc(n.ip)} · ${esc(n.region)}</p>`;

    const indent = Math.min(depth, 6) * 16;
    return `<div data-edit="${n.id}" title="Click to edit" style="margin-left:${indent}px" class="bg-surface border border-outline-variant rounded-md p-3 flex flex-row items-center justify-between gap-3 hover:bg-surface-container-low transition-colors group cursor-pointer ${s.op}">
      <div class="flex items-center gap-2 min-w-0">
        ${caret}
        <div class="w-8 h-8 rounded-full bg-surface-container-high flex items-center justify-center shrink-0 ${s.text}">${icon(s.icon, { size: 18 })}</div>
        <div class="min-w-0">
          <h4 class="font-label-md text-label-md text-primary truncate">${esc(n.name)}</h4>
          ${why}
        </div>
      </div>
      <div class="flex items-center gap-3 shrink-0">
        ${rollup}
        ${portBadge(n)}
        ${n.on_backup ? `<span title="Primary uplink down — running on a backup path. Redundancy is gone." class="font-label-xs text-label-xs text-amber-400 border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 rounded-md whitespace-nowrap flex items-center gap-1">${icon("hub", { size: 12 })} on backup</span>` : ""}
        ${n.maintenance ? `<span class="font-label-xs text-label-xs text-amber-400 border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 rounded-md whitespace-nowrap flex items-center gap-1">${icon("pause_circle", { size: 12 })} maintenance</span>` : ""}
        <div class="text-right hidden sm:block"><p class="font-mono-data ${pctColor}">${fmtPct(n.uptime_pct)}</p></div>
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full ${s.dot}" style="box-shadow:${s.glow}"></span>
          <span class="font-label-md text-label-md ${s.text} hidden md:inline">${esc(n.state_label)}</span>
        </div>
        <span class="text-outline group-hover:text-primary transition-colors">${icon("edit", { size: 18 })}</span>
      </div>
    </div>`;
  }

  // --- node topology tree (collapse / expand, roll-up, suppression cause) ----
  const NODE_STATE_RANK = { UP: 1, UNREACHABLE: 2, DEGRADED: 3, DOWN: 4 };
  const NODE_EXPAND_KEY = "wisp.nodes.expand";  // { expanded:[ids], collapsed:[ids] }

  function loadExpandPrefs() {
    try {
      const raw = JSON.parse(localStorage.getItem(NODE_EXPAND_KEY) || "{}");
      return { expanded: new Set(raw.expanded || []), collapsed: new Set(raw.collapsed || []) };
    } catch { return { expanded: new Set(), collapsed: new Set() }; }
  }
  function saveExpandPrefs(prefs) {
    try {
      localStorage.setItem(NODE_EXPAND_KEY, JSON.stringify({
        expanded: [...prefs.expanded], collapsed: [...prefs.collapsed],
      }));
    } catch { /* private mode / quota — tree just won't persist */ }
  }

  // Flat node list -> ordered rows honoring expand state, with per-node context.
  function buildNodeRows(nodes, prefs) {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const children = new Map();
    const roots = [];
    for (const n of nodes) {
      const p = n.parent_device_id;
      if (p != null && byId.has(p)) (children.get(p) || children.set(p, []).get(p)).push(n);
      else roots.push(n);
    }
    // affected count + worst state across each subtree (descendants only).
    const statsCache = new Map();
    function stats(id) {
      if (statsCache.has(id)) return statsCache.get(id);
      const kids = children.get(id) || [];
      let affected = 0, total = 0;
      for (const k of kids) {
        total += 1;
        if (k.state !== "UP") affected += 1;
        const sub = stats(k.id);
        total += sub.total; affected += sub.affected;
      }
      const r = { affected, total };
      statsCache.set(id, r);
      return r;
    }
    // nearest ancestor that is itself DOWN (the real cause of a suppressed node).
    function downAncestor(n) {
      let p = n.parent_device_id;
      while (p != null && byId.has(p)) {
        const par = byId.get(p);
        if (par.state === "DOWN") return par;
        p = par.parent_device_id;
      }
      return null;
    }
    function isExpanded(id) {
      if (prefs.collapsed.has(id)) return false;
      if (prefs.expanded.has(id)) return true;
      return stats(id).affected > 0;  // default: open branches with trouble
    }

    const rows = [];
    function walk(list, depth) {
      for (const n of list) {
        const kids = children.get(n.id) || [];
        const hasChildren = kids.length > 0;
        const expanded = hasChildren && isExpanded(n.id);
        rows.push({ node: n, ctx: {
          depth, hasChildren, expanded,
          stats: hasChildren ? stats(n.id) : null,
          suppressedBy: n.state === "UNREACHABLE" ? downAncestor(n) : null,
        } });
        if (expanded) walk(kids, depth + 1);
      }
    }
    walk(roots, 0);
    return rows;
  }

  function toggleNodeExpand(id, prefs, currentlyExpanded) {
    // record an explicit override opposite to the current rendered state.
    prefs.expanded.delete(id);
    prefs.collapsed.delete(id);
    if (currentlyExpanded) prefs.collapsed.add(id);
    else prefs.expanded.add(id);
    saveExpandPrefs(prefs);
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
        ${isEdit ? `
        <div class="sm:col-span-2 space-y-2">
          <label class="font-label-xs text-label-xs text-on-surface-variant flex items-center gap-1">${icon("hub", { size: 13 })} Backup parents <span class="text-outline">— redundant uplinks (on-backup alerting)</span></label>
          <div data-backup-list class="flex flex-col gap-1.5"></div>
          <div class="flex items-center gap-2">
            <select data-backup-select class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary appearance-none flex-1"></select>
            <button type="button" data-backup-add class="inline-flex items-center justify-center gap-1 h-9 text-primary border border-outline-variant hover:bg-surface-container-high font-label-md text-label-md px-3 rounded-md transition-colors whitespace-nowrap">${icon("add", { size: 16 })} Add</button>
          </div>
        </div>` : ""}
        ${isEdit ? `
        <div class="sm:col-span-2 space-y-2 pt-2 border-t border-outline-variant">
          <label class="font-label-xs text-label-xs text-on-surface-variant flex items-center gap-1">${icon("hub", { size: 13 })} SNMP port status <span class="text-outline">— v2c; monitored uplink ports fold into outages</span></label>
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 items-center">
            <label class="flex items-center gap-2 text-body-sm text-primary px-1"><input type="checkbox" data-snmp-enabled ${d.snmp_enabled ? "checked" : ""} class="accent-primary w-4 h-4"> Enabled</label>
            <input data-snmp-community value="${esc(d.snmp_community || "")}" placeholder="community (e.g. public)" class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />
            <input data-snmp-port type="number" min="1" max="65535" value="${esc(d.snmp_port || 161)}" placeholder="161" class="w-full bg-surface border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />
          </div>
          <button type="button" data-snmp-save class="inline-flex items-center justify-center gap-1 h-9 text-primary border border-outline-variant hover:bg-surface-container-high font-label-md text-label-md px-3 rounded-md transition-colors whitespace-nowrap">${icon("settings", { size: 16 })} Save SNMP</button>
          <div data-ports-panel class="flex flex-col gap-1.5"></div>
        </div>` : ""}
        <div class="sm:col-span-2 flex flex-col-reverse sm:flex-row sm:items-center sm:justify-between gap-3 pt-3 mt-1 border-t border-outline-variant">
          <div class="flex items-center gap-2">${isEdit ? `<button type="button" data-delete class="inline-flex items-center justify-center gap-1.5 h-9 text-error hover:bg-error/10 border border-error/30 font-label-md text-label-md px-3 rounded-md transition-colors whitespace-nowrap">${icon("delete", { size: 16 })} Delete</button>
            <button type="button" data-maint class="inline-flex items-center justify-center gap-1.5 h-9 ${d.maintenance ? "text-primary border-primary/40 bg-primary/10" : "text-on-surface-variant border-outline-variant hover:bg-surface-container-high"} border font-label-md text-label-md px-3 rounded-md transition-colors whitespace-nowrap">${icon("pause_circle", { size: 16 })} ${d.maintenance ? "Resume" : "Maintenance"}</button>` : ""}</div>
          <div class="flex items-center justify-end gap-2">
            <button type="button" data-close class="inline-flex items-center justify-center h-9 text-on-surface-variant hover:text-primary font-label-md text-label-md px-4 rounded-md hover:bg-surface-container-high whitespace-nowrap">Cancel</button>
            <button type="submit" class="inline-flex items-center justify-center h-9 bg-primary text-surface font-label-md text-label-md px-4 rounded-md active:scale-95 transition-transform whitespace-nowrap">${isEdit ? "Save changes" : "Add node"}</button>
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

    // --- backup parents (redundant uplinks) — edit mode only ----------------
    const backupList = overlay.querySelector("[data-backup-list]");
    if (backupList) {
      let backups = (d.backup_parents || []).slice();   // [{id, name}]
      const select = overlay.querySelector("[data-backup-select]");
      const addBtn = overlay.querySelector("[data-backup-add]");
      const nameOf = (id) => (devices.find((x) => x.id === id) || {}).name || `#${id}`;

      function renderBackups() {
        backupList.innerHTML = backups.length
          ? backups.map((b) => `<div class="flex items-center justify-between gap-2 bg-surface border border-outline-variant rounded-md px-3 py-1.5">
              <span class="font-label-md text-label-md text-primary truncate flex items-center gap-1.5">${icon("hub", { size: 14, cls: "text-amber-400" })} ${esc(b.name)}</span>
              <button type="button" data-backup-remove="${b.id}" title="Remove backup link" class="text-on-surface-variant hover:text-error p-1 rounded-full hover:bg-error/10">${icon("close", { size: 16 })}</button></div>`).join("")
          : `<p class="font-label-xs text-label-xs text-outline">No backup uplinks — this node has a single path to the core.</p>`;
        // candidates: every other active node except self, the current primary, and
        // nodes already wired as a backup.
        const primaryId = Number(overlay.querySelector('[data-field="parent_device_id"]').value) || null;
        const taken = new Set([d.id, primaryId, ...backups.map((b) => b.id)]);
        const opts = devices.filter((x) => !taken.has(x.id));
        select.innerHTML = opts.length
          ? [`<option value="">Add a backup parent…</option>`].concat(
              opts.map((x) => `<option value="${x.id}">${esc(x.name)} (#${x.id})</option>`)).join("")
          : `<option value="">No eligible nodes</option>`;
        select.disabled = !opts.length;
        addBtn.disabled = !opts.length;
      }

      backupList.addEventListener("click", async (ev) => {
        const btn = ev.target.closest("[data-backup-remove]");
        if (!btn) return;
        const pid = Number(btn.getAttribute("data-backup-remove"));
        btn.disabled = true;
        const res = await sendJSON("DELETE", `/api/devices/${d.id}/links/${pid}`);
        if (res.ok && res.data.ok) {
          backups = backups.filter((b) => b.id !== pid);
          renderBackups(); toast("Backup link removed"); onDone();
        } else { toast(res.data.error || res.data.reason || "Couldn't remove link", "error"); btn.disabled = false; }
      });

      addBtn.addEventListener("click", async () => {
        const pid = Number(select.value);
        if (!pid) return;
        addBtn.disabled = true;
        const res = await sendJSON("POST", `/api/devices/${d.id}/links`, { parent_id: pid });
        if (res.ok && res.data.ok) {
          backups.push({ id: pid, name: nameOf(pid) });
          renderBackups(); toast("Backup link added"); onDone();
        } else { toast(res.data.error || res.data.reason || "Couldn't add link", "error"); addBtn.disabled = false; }
      });

      // the candidate list depends on the chosen primary, so refresh when it changes.
      const primarySel = overlay.querySelector('[data-field="parent_device_id"]');
      if (primarySel) primarySel.addEventListener("change", renderBackups);
      renderBackups();
    }

    // --- SNMP config + discovered ports panel — edit mode only --------------
    const portsPanel = overlay.querySelector("[data-ports-panel]");
    if (portsPanel) {
      const snmpSave = overlay.querySelector("[data-snmp-save]");
      const enabledEl = overlay.querySelector("[data-snmp-enabled]");
      const communityEl = overlay.querySelector("[data-snmp-community]");
      const portEl = overlay.querySelector("[data-snmp-port]");
      const OPER_CLS = { up: "text-primary", down: "text-error", lowerLayerDown: "text-error" };

      function portRow(p) {
        const oper = OPER_CLS[p.oper_status] || "text-amber-400";
        const label = esc(p.if_name || `if${p.if_index}`) + (p.if_alias ? ` <span class="text-outline">· ${esc(p.if_alias)}</span>` : "");
        const feedsOpts = [`<option value="">feeds: —</option>`].concat(
          devices.filter((x) => x.id !== d.id).map((x) =>
            `<option value="${x.id}" ${x.id === p.feeds_device_id ? "selected" : ""}>feeds: ${esc(x.name)}</option>`)).join("");
        return `<div class="flex items-center justify-between gap-2 bg-surface border border-outline-variant rounded-md px-3 py-1.5">
          <div class="min-w-0">
            <p class="font-label-md text-label-md text-primary truncate">${label}</p>
            <p class="font-mono-data text-[11px] ${oper}">oper ${esc(p.oper_status)} · admin ${esc(p.admin_status)}${p.alarm ? ' · <span class="text-error">ALARM</span>' : ""}</p>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <select data-port-feeds="${p.id}" class="bg-surface border border-outline-variant text-primary text-[11px] rounded-md px-1.5 py-1 appearance-none max-w-[8rem]">${feedsOpts}</select>
            <label title="Alarm if this port goes down" class="flex items-center gap-1 text-label-xs text-on-surface-variant"><input type="checkbox" data-port-mon="${p.id}" ${p.monitored ? "checked" : ""} class="accent-primary w-4 h-4"> watch</label>
          </div>
        </div>`;
      }

      async function loadPorts() {
        try {
          const ports = await getJSON(`/api/devices/${d.id}/ports`);
          portsPanel.innerHTML = ports.length
            ? `<p class="font-label-xs text-label-xs text-on-surface-variant mt-1">${ports.length} port(s) discovered — tick the uplink/infra ports to watch:</p>` + ports.map(portRow).join("")
            : `<p class="font-label-xs text-label-xs text-outline mt-1">No ports discovered yet${d.snmp_enabled ? " — the daemon walks SNMP on its own cadence." : " — enable SNMP and save."}</p>`;
        } catch (e) { portsPanel.innerHTML = `<p class="font-label-xs text-label-xs text-error">Couldn't load ports</p>`; }
      }

      snmpSave.addEventListener("click", async () => {
        snmpSave.disabled = true;
        snmpSave.innerHTML = spinner("Saving…");
        const res = await sendJSON("POST", `/api/devices/${d.id}/snmp`, {
          snmp_enabled: enabledEl.checked ? 1 : 0,
          snmp_version: "2c",
          snmp_community: communityEl.value.trim(),
          snmp_port: Number(portEl.value) || 161,
        });
        snmpSave.innerHTML = `${icon("settings", { size: 16 })} Save SNMP`;
        snmpSave.disabled = false;
        if (res.ok && res.data.ok) { toast("SNMP config saved"); d.snmp_enabled = enabledEl.checked ? 1 : 0; onDone(); }
        else { toast(res.data.error || "Couldn't save SNMP", "error"); }
      });

      portsPanel.addEventListener("change", async (ev) => {
        const mon = ev.target.closest("[data-port-mon]");
        const feeds = ev.target.closest("[data-port-feeds]");
        if (mon) {
          const res = await sendJSON("POST", `/api/ports/${mon.getAttribute("data-port-mon")}/monitored`, { monitored: mon.checked });
          if (res.ok && res.data.ok) { toast(mon.checked ? "Port watched" : "Port unwatched"); onDone(); }
          else { toast("Couldn't update port", "error"); mon.checked = !mon.checked; }
        } else if (feeds) {
          const res = await sendJSON("POST", `/api/ports/${feeds.getAttribute("data-port-feeds")}/feeds`, { feeds_device_id: feeds.value || null });
          if (res.ok && res.data.ok) toast("Port mapping saved");
          else toast(res.data.error || "Couldn't map port", "error");
        }
      });

      loadPorts();
    }

    const maintBtn = overlay.querySelector("[data-maint]");
    if (maintBtn) {
      maintBtn.addEventListener("click", async () => {
        const turnOn = !d.maintenance;
        maintBtn.disabled = true;
        maintBtn.innerHTML = spinner(turnOn ? "Pausing…" : "Resuming…");
        const res = await sendJSON("POST", `/api/devices/${d.id}/maintenance`, { maintenance: turnOn });
        if (res.ok && res.data.ok) {
          toast(turnOn ? "Node in maintenance — alerts paused" : "Monitoring resumed");
          close();
          onDone();
        } else {
          toast(res.data.error || res.data.reason || "Couldn't update maintenance", "error");
          maintBtn.disabled = false;
        }
      });
    }

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

  // --- topology map (interactive node-link graph) ---------------------------
  // The indented tree only draws the PRIMARY ping parent. The map draws all three
  // relationship layers at once — primary parent, backup uplinks, and the physical
  // switch-port→fed-device links SNMP discovered — over a tidy-tree layout, so an
  // operator can see the physical shape of the network during an incident. Pure SVG,
  // no library: layout is deterministic (children centered under their parent), pan/zoom
  // is viewBox math.
  const NODE_VIEW_KEY = "wisp.nodes.view";
  function loadNodesView() {
    try { return localStorage.getItem(NODE_VIEW_KEY) === "map" ? "map" : "tree"; }
    catch { return "tree"; }
  }
  function saveNodesView(v) { try { localStorage.setItem(NODE_VIEW_KEY, v); } catch {} }
  function nodeViewBtnCls(active) {
    return `flex items-center gap-1 px-2.5 py-1 rounded-[5px] font-label-md text-label-md transition-colors ${active ? "bg-surface-container text-primary" : "text-on-surface-variant hover:text-primary"}`;
  }

  // Calm-when-healthy, loud-when-broken: a neutral border for UP/UNREACHABLE so trouble
  // (amber DEGRADED, red DOWN) pops. The status dot still carries the exact state colour.
  const TOPO_BORDER = { UP: "#444748", DEGRADED: "#fbbf24", DOWN: "#ffb4ab", UNREACHABLE: "#33373a" };
  const TOPO_DOT = { UP: "#ffffff", DEGRADED: "#fbbf24", DOWN: "#ffb4ab", UNREACHABLE: "#8e9192" };
  function edgeColor(node) {
    if (!node) return "#444748";
    if (node.state === "DOWN" || node.state === "UNREACHABLE") return "#ffb4ab";
    if (node.state === "DEGRADED") return "#fbbf24";
    return "#444748";
  }
  function topoTrunc(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  // Tidy-tree layout over the PRIMARY topology: leaves get sequential x slots, a parent
  // centers over its children, each root's tree laid side by side. Backup/port edges are
  // overlays on top of these positions.
  function layoutTopology(nodes) {
    const NW = 172, NH = 54, HGAP = 28, LEVEL_Y = 112;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const kids = new Map();
    const roots = [];
    for (const n of nodes) {
      const p = n.parent_device_id;
      if (p != null && byId.has(p)) (kids.get(p) || kids.set(p, []).get(p)).push(n);
      else roots.push(n);
    }
    const byName = (a, b) => String(a.name).localeCompare(String(b.name));
    roots.sort(byName);
    for (const arr of kids.values()) arr.sort(byName);
    const pos = new Map();
    let cursor = 0;
    function place(n, depth) {
      const cs = kids.get(n.id) || [];
      let left;
      if (!cs.length) { left = cursor; cursor += NW + HGAP; }
      else {
        const ls = cs.map((c) => place(c, depth + 1));
        left = (ls[0] + ls[ls.length - 1]) / 2;
      }
      pos.set(n.id, { left, top: depth * LEVEL_Y });
      return left;
    }
    for (const r of roots) { place(r, 0); cursor += HGAP * 1.5; }
    let maxR = 0, maxB = 0;
    for (const p of pos.values()) { maxR = Math.max(maxR, p.left + NW); maxB = Math.max(maxB, p.top + NH); }
    return { pos, NW, NH, width: maxR, height: maxB };
  }

  function topoCurve(a, b, bow) {
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    return `M ${a.x} ${a.y} Q ${mx + bow} ${my} ${b.x} ${b.y}`;
  }

  function topoEdge(e, L, byId) {
    const a = L.pos.get(e.parent_id), b = L.pos.get(e.child_id);
    if (!a || !b) return "";
    const from = { x: a.left + L.NW / 2, y: a.top + L.NH };
    const to = { x: b.left + L.NW / 2, y: b.top };
    if (e.kind === "primary") {
      return `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" stroke="${edgeColor(byId.get(e.child_id))}" stroke-width="1.6"/>`;
    }
    if (e.kind === "backup") {
      return `<path d="${topoCurve(from, to, 55)}" fill="none" stroke="#fbbf24" stroke-width="1.6" stroke-dasharray="6 4" opacity="0.85"/>`;
    }
    // port-feed: physical link, teal (red when alarming), with the port label at the bow.
    const col = e.down ? "#ffb4ab" : "#2dd4bf";
    const mid = { x: (from.x + to.x) / 2 - 27, y: (from.y + to.y) / 2 };
    return `<path d="${topoCurve(from, to, -55)}" fill="none" stroke="${col}" stroke-width="1.6" stroke-dasharray="2 3" opacity="0.9"/>`
      + `<text x="${mid.x}" y="${mid.y}" fill="${col}" font-size="9.5" text-anchor="middle" font-weight="${e.down ? 700 : 400}">${esc(topoTrunc(e.port_label, 16))}</text>`;
  }

  function topoNode(n, L) {
    const p = L.pos.get(n.id);
    if (!p) return "";
    const { left: x, top: y } = p, W = L.NW, H = L.NH;
    const border = TOPO_BORDER[n.state] || "#444748";
    const dot = TOPO_DOT[n.state] || "#ffffff";
    const op = n.state === "UNREACHABLE" ? 0.6 : 1;
    const dash = n.maintenance ? ` stroke-dasharray="5 3"` : "";
    let bx = x + W - 13, badges = "";
    if (n.ports && n.ports.down > 0) {
      badges += `<circle cx="${bx}" cy="${y + 13}" r="7" fill="#ffb4ab"/><text x="${bx}" y="${y + 16.5}" fill="#141313" font-size="9" text-anchor="middle" font-weight="700">${n.ports.down}</text>`;
      bx -= 17;
    }
    if (n.on_backup) { badges += `<circle cx="${bx}" cy="${y + 13}" r="5" fill="#fbbf24"/>`; }
    const title = `${n.name} — ${n.state_label}`
      + (n.on_backup ? " · on backup" : "") + (n.maintenance ? " · maintenance" : "")
      + (n.ports && n.ports.down ? ` · ${n.ports.down} port(s) down` : "");
    return `<g data-node="${n.id}" style="cursor:pointer" opacity="${op}">
      <title>${esc(title)}</title>
      <rect x="${x}" y="${y}" width="${W}" height="${H}" rx="9" fill="#201f1f" stroke="${border}" stroke-width="1.6"${dash}/>
      <circle cx="${x + 15}" cy="${y + H / 2}" r="5" fill="${dot}"/>
      <text x="${x + 28}" y="${y + 22}" fill="#e5e2e1" font-size="12.5" font-weight="600">${esc(topoTrunc(n.name, 19))}</text>
      <text x="${x + 28}" y="${y + 39}" fill="#8e9192" font-size="10">${esc(topoTrunc((n.type || "node") + " · " + n.ip, 24))}</text>
      ${badges}
    </g>`;
  }

  function topoLegendRow(swatch, label) {
    return `<div class="flex items-center gap-1.5"><span class="inline-block w-3 h-0.5 shrink-0" style="${swatch}"></span>${label}</div>`;
  }
  function topologyMap(data) {
    const nodes = (data && data.nodes) || [];
    if (!nodes.length) {
      return `<div class="border border-outline-variant bg-surface-container-low rounded-md p-6 flex items-center gap-3 text-on-surface-variant">
        ${icon("hub", { cls: "text-outline" })}<span class="font-body-sm">No nodes to map yet — add devices below.</span></div>`;
    }
    const L = layoutTopology(nodes);
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = ((data && data.edges) || []).map((e) => topoEdge(e, L, byId)).join("");
    const ns = nodes.map((n) => topoNode(n, L)).join("");
    const pad = 52, vb = `${-pad} ${-pad} ${L.width + pad * 2} ${L.height + pad * 2}`;
    const btn = "w-8 h-8 flex items-center justify-center bg-surface-container border border-outline-variant rounded-md text-on-surface-variant hover:text-primary hover:bg-surface-container-high transition-colors";
    return `<div class="relative bg-surface-dim border border-outline-variant rounded-md overflow-hidden" style="height:min(70vh,640px)">
      <svg id="topo-svg" class="w-full h-full select-none" style="cursor:grab;touch-action:none" viewBox="${vb}">
        <g>${edges}</g><g>${ns}</g>
      </svg>
      <div class="absolute top-3 left-3 bg-surface-container/90 backdrop-blur border border-outline-variant rounded-md px-3 py-2 font-label-xs text-label-xs text-on-surface-variant flex flex-col gap-1 pointer-events-none">
        ${topoLegendRow("background:#444748", "primary uplink")}
        ${topoLegendRow("background:#fbbf24;height:0;border-top:2px dashed #fbbf24", "backup uplink")}
        ${topoLegendRow("background:#2dd4bf;height:0;border-top:2px dashed #2dd4bf", "SNMP port feed")}
        <div class="flex items-center gap-1.5"><span class="inline-block w-2 h-2 rounded-full" style="background:#ffb4ab"></span>down / port alarm</div>
      </div>
      <div class="absolute bottom-3 right-3 flex flex-col gap-1.5">
        <button data-zoom="in" title="Zoom in" class="${btn}">${icon("add", { size: 18 })}</button>
        <button data-zoom="out" title="Zoom out" class="${btn}">${icon("chevron_left", { size: 18, cls: "rotate-90" })}</button>
        <button data-zoom="reset" title="Reset view" class="${btn}">${icon("refresh", { size: 16 })}</button>
      </div>
      <div class="absolute bottom-3 left-3 font-label-xs text-label-xs text-outline pointer-events-none">drag to pan · scroll to zoom · click a node</div>
      <div id="topo-detail" class="hidden"></div>
    </div>`;
  }

  function wireTopology(box, data, reload, onView) {
    const svg = box.querySelector("#topo-svg");
    if (!svg) return;
    const p = svg.getAttribute("viewBox").split(" ").map(Number);
    let vb = { x: p[0], y: p[1], w: p[2], h: p[3] };
    const base = { ...vb };
    const apply = () => { svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); if (onView) onView(svg.getAttribute("viewBox")); };
    const zoomAt = (nx, ny, k) => {
      const mx = vb.x + nx * vb.w, my = vb.y + ny * vb.h;
      vb.w *= k; vb.h *= k; vb.x = mx - nx * vb.w; vb.y = my - ny * vb.h; apply();
    };
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      zoomAt((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height, e.deltaY > 0 ? 1.12 : 0.89);
    }, { passive: false });
    let drag = null, moved = 0;
    svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest("[data-zoom]")) return;
      drag = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y }; moved = 0;
      try { svg.setPointerCapture(e.pointerId); } catch {}
      svg.style.cursor = "grabbing";
    });
    svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      vb.x = drag.vx - (e.clientX - drag.x) / r.width * vb.w;
      vb.y = drag.vy - (e.clientY - drag.y) / r.height * vb.h;
      moved += Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y);
      apply();
    });
    const end = (e) => { if (drag) { try { svg.releasePointerCapture(e.pointerId); } catch {} } drag = null; svg.style.cursor = "grab"; };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("click", (e) => {
      if (moved > 6) return;   // it was a pan, not a click
      const g = e.target.closest("[data-node]");
      if (!g) return;
      const node = (data.nodes || []).find((n) => n.id === Number(g.getAttribute("data-node")));
      if (node) showTopoDetail(box, node, data, reload);
    });
    box.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      const k = b.getAttribute("data-zoom");
      if (k === "in") zoomAt(0.5, 0.5, 0.83);
      else if (k === "out") zoomAt(0.5, 0.5, 1.2);
      else { vb = { ...base }; apply(); }
    }));
  }

  // Read-only detail card (monitoring-first): live state + what it feeds + which ports
  // are down, with an Edit button into the full node modal where config lives.
  function showTopoDetail(box, node, data, reload) {
    const host = box.querySelector("#topo-detail");
    if (!host) return;
    const sm = NODE_STATE_MAP[node.state] || NODE_STATE_MAP.UP;
    const ups = (data.edges || []).filter((e) => e.child_id === node.id);
    const upHtml = ups.length ? ups.map((e) => {
      const par = (data.nodes || []).find((n) => n.id === e.parent_id);
      const nm = esc(par ? par.name : "#" + e.parent_id);
      const tag = e.kind === "primary" ? "primary"
        : e.kind === "backup" ? "backup" : "port" + (e.port_label ? " " + esc(topoTrunc(e.port_label, 12)) : "");
      const col = e.kind === "backup" ? "text-amber-400" : e.kind === "port" ? "text-[#2dd4bf]" : "text-on-surface-variant";
      return `<div class="flex items-center justify-between gap-2 text-label-xs"><span class="text-on-surface-variant truncate">${icon("subdirectory_arrow_right", { size: 12 })} ${nm}</span><span class="${col} shrink-0">${tag}</span></div>`;
    }).join("") : `<p class="font-label-xs text-label-xs text-outline">No upstream links recorded.</p>`;
    const flags = [];
    if (node.on_backup) flags.push(`<span class="text-amber-400">${icon("hub", { size: 12 })} on backup</span>`);
    if (node.perf) flags.push(`<span class="text-amber-400">${icon("bolt", { size: 12 })} slow link</span>`);
    if (node.maintenance) flags.push(`<span class="text-amber-400">${icon("pause_circle", { size: 12 })} maintenance</span>`);
    host.innerHTML = `<div class="absolute top-3 right-3 w-72 max-w-[88%] bg-surface-container border border-outline-variant rounded-md shadow-xl p-4 flex flex-col gap-3 z-20 animate-fade-in">
      <div class="flex items-start justify-between gap-2">
        <h4 class="font-label-md text-label-md text-primary truncate">${esc(node.name)}</h4>
        <button data-topo-close class="text-on-surface-variant hover:text-primary -mt-1 -mr-1 p-1 rounded-full hover:bg-surface-container-high">${icon("close", { size: 16 })}</button>
      </div>
      <div class="flex items-center justify-between gap-2 font-label-xs text-label-xs">
        <span class="font-mono-data text-on-surface-variant truncate">${esc(node.type || "node")} · ${esc(node.ip)} · ${esc(node.region)}</span>
        <span class="${sm.text} flex items-center gap-1 shrink-0"><span class="w-2 h-2 rounded-full ${sm.dot}"></span>${esc(node.state_label)}</span>
      </div>
      <div class="flex items-center justify-between gap-2 font-label-xs text-label-xs">
        <span class="text-on-surface-variant">24h uptime</span>
        <span class="font-mono-data ${node.uptime_pct >= 99.9 ? "text-primary" : "text-amber-400"}">${fmtPct(node.uptime_pct)}</span>
      </div>
      ${flags.length ? `<div class="flex flex-wrap gap-2 font-label-xs text-label-xs">${flags.join("")}</div>` : ""}
      <div class="flex flex-col gap-1 border-t border-outline-variant pt-2">
        <span class="font-label-xs text-label-xs text-outline uppercase tracking-wide">Uplinks</span>${upHtml}</div>
      ${node.ports && node.ports.total ? `<div id="topo-ports" class="flex flex-col gap-1 border-t border-outline-variant pt-2"><span class="font-label-xs text-label-xs text-outline">Loading ports…</span></div>` : ""}
      <button data-topo-edit class="mt-1 inline-flex items-center justify-center gap-1 h-9 text-primary border border-outline-variant hover:bg-surface-container-high font-label-md text-label-md rounded-md transition-colors">${icon("edit", { size: 16 })} Edit node</button>
    </div>`;
    host.classList.remove("hidden");
    host.querySelector("[data-topo-close]").addEventListener("click", () => { host.classList.add("hidden"); host.innerHTML = ""; });
    host.querySelector("[data-topo-edit]").addEventListener("click", async () => {
      try {
        const devices = await getJSON("/api/devices");
        const dev = devices.find((x) => x.id === node.id);
        if (dev) openNodeModal(dev, devices, reload);
      } catch { toast("Couldn't load node", "error"); }
    });
    if (node.ports && node.ports.total) loadTopoPorts(host, node);
  }

  async function loadTopoPorts(host, node) {
    const el = host.querySelector("#topo-ports");
    if (!el) return;
    try {
      const ports = await getJSON(`/api/devices/${node.id}/ports`);
      const watched = ports.filter((p) => p.monitored);
      if (!watched.length) {
        el.innerHTML = `<span class="font-label-xs text-label-xs text-outline">${ports.length} port(s) discovered, none watched.</span>`;
        return;
      }
      el.innerHTML = `<span class="font-label-xs text-label-xs text-outline uppercase tracking-wide">Watched ports</span>`
        + watched.map((p) => {
          const down = p.alarm;
          const fed = p.feeds_name ? ` → ${esc(p.feeds_name)}` : "";
          return `<div class="flex items-center justify-between gap-2 font-label-xs text-label-xs ${down ? "text-error" : "text-on-surface-variant"}">
            <span class="truncate flex items-center gap-1">${icon(down ? "power_off" : "visibility", { size: 11 })} ${esc(p.if_name || ("if" + p.if_index))}${fed}</span>
            <span class="shrink-0">${down ? "down" : esc(p.oper_status || "up")}</span></div>`;
        }).join("");
    } catch { el.innerHTML = `<span class="font-label-xs text-label-xs text-error">Couldn't load ports</span>`; }
  }

  function renderNodes(page) {
    let selectedDate = null;  // null = live view of all nodes; else a YYYY-MM-DD drill-down
    let view = loadNodesView();   // "tree" | "map" (persisted); a date drill-down forces the list
    let topoVB = null;            // preserved viewBox so a live refresh doesn't reset pan/zoom
    const expandPrefs = loadExpandPrefs();
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
        <div class="flex justify-between items-center gap-2 flex-wrap">
          <h3 id="nodes-title" class="font-headline-md text-headline-md text-primary">Active Nodes</h3>
          <div class="flex items-center gap-2">
            <div id="view-toggle" class="flex items-center bg-surface-container-low border border-outline-variant rounded-md p-0.5">
              <button data-view="tree" class="${nodeViewBtnCls(false)}">${icon("subdirectory_arrow_right", { size: 15 })} Tree</button>
              <button data-view="map" class="${nodeViewBtnCls(false)}">${icon("hub", { size: 15 })} Map</button>
            </div>
            <button id="nodes-clear" class="hidden items-center gap-1 text-on-surface-variant hover:text-primary font-label-md text-label-md px-3 py-1.5 rounded-md border border-outline-variant hover:bg-surface-container transition-colors">
              ${icon("close", { size: 16 })} Show all</button>
            <button id="add-node" class="flex items-center gap-1 bg-primary text-surface font-label-md text-label-md px-3 py-1.5 rounded-md active:scale-95 transition-transform">
              ${icon("add", { size: 16 })} Add node</button>
          </div>
        </div>
        <div id="nodes" class="grid grid-cols-1 gap-2">${loading("nodes")}</div>
        <div id="topo" class="hidden">${loading("topology")}</div>
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
          const rows = buildNodeRows(nodes, expandPrefs);
          box.innerHTML = rows.map((r) => nodeRow(r.node, r.ctx)).join("");
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
              applyView();    // a date drill-down forces the list; clearing it returns to the chosen view
              loadActive();
            });
          });
        };
        paint();
      } catch (e) { /* heatmap is best-effort; node list still loads */ }
    }

    function isListView() { return !!selectedDate || view === "tree"; }
    function loadActive() { return isListView() ? loadNodes() : loadTopology(); }

    function applyView() {
      const list = isListView();
      $("#nodes", page).classList.toggle("hidden", !list);
      $("#topo", page).classList.toggle("hidden", list);
      const t = page.querySelector('[data-view="tree"]'), m = page.querySelector('[data-view="map"]');
      if (t) t.className = nodeViewBtnCls(view === "tree");
      if (m) m.className = nodeViewBtnCls(view === "map");
    }

    async function loadTopology() {
      const box = $("#topo", page);
      $("#nodes-title", page).textContent = "Network Map";
      const clear = $("#nodes-clear", page);
      clear.classList.add("hidden"); clear.classList.remove("flex");
      const cur = box.querySelector("#topo-svg");
      if (cur) topoVB = cur.getAttribute("viewBox");   // keep pan/zoom across a live refresh
      try {
        const data = await getJSON("/api/topology");
        if (currentPath() !== "#/nodes") return;
        box.innerHTML = topologyMap(data);
        const svg = box.querySelector("#topo-svg");
        if (svg && topoVB) svg.setAttribute("viewBox", topoVB);
        wireTopology(box, data, afterChange, (vb) => { topoVB = vb; });
      } catch (e) { box.innerHTML = errorBox(e.message); }
    }

    $("#view-toggle", page).addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-view]");
      if (!b) return;
      const v = b.getAttribute("data-view");
      if (v === view) return;
      view = v; saveNodesView(v);
      if (v === "map" && selectedDate) { selectedDate = null; loadHeatmap(); }
      applyView();
      loadActive();
    });

    $("#nodes-clear", page).addEventListener("click", () => {
      selectedDate = null;
      loadHeatmap();
      applyView();
      loadActive();
    });

    const afterChange = () => { loadHeatmap(); loadActive(); };

    $("#add-node", page).addEventListener("click", async () => {
      try {
        const devices = await getJSON("/api/devices");
        openNodeModal(null, devices, afterChange);
      } catch (e) { toast("Couldn't load nodes", "error"); }
    });

    // Caret toggle: flip the subtree's expand state and re-render (delegated).
    $("#nodes", page).addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-toggle]");
      if (!btn) return;
      ev.stopPropagation();
      const id = Number(btn.getAttribute("data-toggle"));
      // a present "Collapse" title means it's currently expanded.
      toggleNodeExpand(id, expandPrefs, btn.getAttribute("title").startsWith("Collapse"));
      loadNodes();
    });

    // Edit on row click (delegated, so it survives auto-refresh re-renders).
    // Only the live all-nodes view has [data-edit]; drill-down rows don't.
    $("#nodes", page).addEventListener("click", async (ev) => {
      if (ev.target.closest("[data-toggle]")) return;  // caret handled above
      const row = ev.target.closest("[data-edit]");
      if (!row) return;
      const id = Number(row.getAttribute("data-edit"));
      try {
        const devices = await getJSON("/api/devices");
        const dev = devices.find((x) => x.id === id);
        if (dev) openNodeModal(dev, devices, afterChange);
      } catch (e) { toast("Couldn't load node", "error"); }
    });

    applyView();   // reflect the persisted Tree/Map choice before the first paint
    return async function load() {
      await Promise.all([loadHeatmap(), loadActive()]);
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
        <div class="sm:col-span-2 flex flex-col-reverse sm:flex-row sm:items-center sm:justify-between gap-3 pt-3 mt-1 border-t border-outline-variant">
          <div>${isEdit ? `<button type="button" data-delete class="inline-flex items-center justify-center gap-1.5 h-9 text-error hover:bg-error/10 border border-error/30 font-label-md text-label-md px-3 rounded-md transition-colors whitespace-nowrap">${icon("delete", { size: 16 })} Delete</button>` : ""}</div>
          <div class="flex items-center justify-end gap-2">
            <button type="button" data-close class="inline-flex items-center justify-center h-9 text-on-surface-variant hover:text-primary font-label-md text-label-md px-4 rounded-md hover:bg-surface-container-high whitespace-nowrap">Cancel</button>
            <button type="submit" class="inline-flex items-center justify-center h-9 bg-primary text-surface font-label-md text-label-md px-4 rounded-md active:scale-95 transition-transform whitespace-nowrap">${isEdit ? "Save changes" : "Add worker"}</button>
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

  // --- Attendance (daily operator roster) -----------------------------------
  // A "who showed up" board: tap a name to toggle today's presence, plus a
  // recent-days grid to visualise the pattern. Operators only (set by the API).
  function attendanceView(att) {
    const ops = att.operators || [];
    if (!ops.length) {
      return `<div class="border border-outline-variant bg-surface-container-low rounded-md p-5 flex items-center gap-3 text-on-surface-variant">
        ${icon("group", { cls: "text-outline" })}
        <span class="font-body-sm">No operators yet — add a worker with the <span class="text-primary">operator</span> role to track attendance.</span></div>`;
    }
    const presentCount = ops.filter((o) => o.present_today).length;
    const chips = ops.map((o) => {
      const on = o.present_today;
      const cls = on
        ? "text-emerald-400 border-emerald-400/40 bg-emerald-400/10"
        : "text-on-surface-variant border-outline-variant hover:bg-surface-container-high";
      return `<button data-att="${o.id}" data-present="${on ? 1 : 0}" type="button"
        class="inline-flex items-center gap-1.5 h-9 px-3 rounded-full border font-label-md text-label-md transition-colors whitespace-nowrap ${cls}">
        ${icon(on ? "check_circle" : "add", { size: 16 })} ${esc(o.name)}</button>`;
    }).join("");

    const days = att.days || [];
    // Compact operator × day grid: a filled square = present that day.
    const headCells = days.map((d) => {
      const isToday = d === att.today;
      return `<th class="px-1 pb-1 font-normal text-center ${isToday ? "text-primary" : "text-outline"}">${d.slice(8)}</th>`;
    }).join("");
    const bodyRows = ops.map((o) => {
      const set = new Set(o.present_days || []);
      const cells = days.map((d) => {
        const on = set.has(d);
        return `<td class="px-1 py-0.5 text-center"><span class="inline-block w-2.5 h-2.5 rounded-sm ${on ? "bg-emerald-400" : "bg-surface-container-high"}" title="${esc(o.name)} · ${esc(d)}${on ? " · present" : ""}"></span></td>`;
      }).join("");
      return `<tr><td class="pr-3 py-0.5 text-on-surface-variant whitespace-nowrap font-label-xs text-label-xs">${esc(o.name)}</td>${cells}</tr>`;
    }).join("");

    return `<div class="flex flex-col gap-4">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <span class="font-body-sm text-body-sm text-on-surface-variant">Today · ${esc(att.today)}</span>
        <span class="font-label-xs text-label-xs bg-surface-container px-2 py-1 rounded-md border border-outline-variant">${presentCount} / ${ops.length} present</span>
      </div>
      <div class="flex flex-wrap gap-2">${chips}</div>
      <div class="overflow-x-auto border-t border-outline-variant pt-3">
        <table class="font-mono-data text-mono-data border-separate" style="border-spacing:0">
          <thead><tr><th class="pr-3 pb-1 text-left font-normal text-outline font-label-xs text-label-xs">last ${days.length} days</th>${headCells}</tr></thead>
          <tbody>${bodyRows}</tbody>
        </table>
      </div>
    </div>`;
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

      <section class="bg-surface border border-outline-variant rounded-md p-4 md:p-5 flex flex-col gap-3">
        <div class="flex items-center gap-2">${icon("check_circle", { size: 18, cls: "text-on-surface-variant" })}
          <h3 class="font-headline-md text-headline-md text-primary">Attendance</h3></div>
        <div id="attendance-body">${loading("attendance")}</div>
      </section>

      <div id="team-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">${loading("team")}</div></div>`;

    async function loadTeam() {
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

    async function loadAttendance() {
      const body = $("#attendance-body", page);
      try {
        const att = await getJSON("/api/attendance");
        if (currentPath() !== "#/team") return;
        body.innerHTML = attendanceView(att);
      } catch (e) { body.innerHTML = errorBox(e.message); }
    }

    function load() { loadTeam(); loadAttendance(); }

    $("#attendance-body", page).addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-att]");
      if (!btn) return;
      const id = Number(btn.getAttribute("data-att"));
      const present = btn.getAttribute("data-present") !== "1";  // toggle
      btn.disabled = true;
      const res = await sendJSON("POST", "/api/attendance", { worker_id: id, present });
      if (res.ok && res.data.ok) loadAttendance();
      else { toast(res.data.error || res.data.reason || "Couldn't update attendance", "error"); btn.disabled = false; }
    });

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
    stopLive();   // drop the SSE stream while signed out (it would just 401-loop)
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
  let _events = null;     // EventSource (server push)
  let _liveOk = false;    // is the SSE stream currently connected?

  function formFocused() {
    const a = document.activeElement;
    return a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
  }

  // Re-render the current live view (skipped while a form is focused so a background
  // refresh never clobbers in-progress input).
  function liveReload() {
    if (_loginShown) return;
    if (AUTO_REFRESH.has(currentPath()) && !formFocused() && _activeLoad) _activeLoad();
  }

  // Push, not poll: subscribe once to the server's SSE stream. Every 'changed' event
  // means the daemon wrote new data (a poll, a DOWN, a recovery), so re-render right
  // then — the UI reflects state in ~1s instead of waiting for a 15s poll. EventSource
  // auto-reconnects if the stream drops.
  function startLive() {
    if (_events || !("EventSource" in window)) return;
    try {
      const es = new EventSource("/api/events");
      es.addEventListener("open", () => { _liveOk = true; });
      es.addEventListener("changed", liveReload);
      es.addEventListener("error", () => { _liveOk = false; });
      _events = es;
    } catch { _events = null; }
  }

  function stopLive() {
    if (_events) { try { _events.close(); } catch {} _events = null; }
    _liveOk = false;
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
    startLive();   // idempotent — one stream for the whole session
    // Fallback poll for the live views ONLY when push is unavailable (EventSource
    // unsupported, or a proxy stripped the stream). A no-op while SSE is healthy.
    if (AUTO_REFRESH.has(currentPath())) {
      _refreshTimer = setInterval(() => {
        if (!_liveOk && !formFocused() && _activeLoad === load) load();
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
