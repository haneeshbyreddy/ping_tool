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
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  async function sendJSON(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    return { ok: r.ok, status: r.status, data };
  }

  const postJSON = (url, body) => sendJSON("POST", url, body || {});

  function fmtPct(n) { return (n == null ? "—" : Number(n).toFixed(2) + "%"); }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(String(ts).replace(" ", "T"));
    if (isNaN(d)) return ts;
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
      `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`;
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
          <h1 class="font-headline-lg text-headline-lg font-bold tracking-tighter text-primary">HANSA</h1>
        </a>
        <div class="flex items-center gap-2 text-on-surface-variant">
          <span id="uplink-chip" class="hidden"></span>
          <div class="p-2 rounded-full flex items-center justify-center w-10 h-10 hover:bg-surface-container">${icon("account_circle", { size: 28 })}</div>
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

  function triageCard(o) {
    const m = STATUS_META[o.status] || STATUS_META.unassigned;
    const head = `
      <div class="flex justify-between items-start">
        <div>
          <h3 class="font-body-lg text-body-lg text-primary font-medium">${esc(o.name)} <span class="text-on-surface-variant font-normal">· ${esc(o.region)}</span></h3>
          <div class="flex items-center gap-2 mt-1 flex-wrap">
            <span class="font-label-xs text-label-xs text-${m.text} border border-${m.text}/30 bg-${m.text}/10 px-2 py-0.5 rounded-sm">${m.tag}</span>
            <span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("schedule", { size: 14 })} ${esc(o.duration_label)}</span>
            <span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("person", { size: 14 })} ~${o.customer_count}</span>
          </div>
        </div>
      </div>`;

    let action = "";
    if (o.status === "unassigned") {
      action = `
        <div class="flex gap-2 w-full md:w-2/3" data-card="${o.id}">
          <input data-tech type="text" placeholder="Acknowledged by (your name)…" class="flex-1 bg-surface-container border border-outline-variant text-primary text-body-sm rounded-md px-3 py-2 outline-none focus:ring-1 focus:ring-primary" />
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
          <button data-action="postmortem" type="button" class="bg-transparent border border-outline-variant text-primary hover:bg-surface-container font-label-md text-label-md px-4 py-2 rounded-md transition-colors w-full">Submit Log</button>
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
        const [s, triage] = await Promise.all([
          getJSON("/api/summary"), getJSON("/api/triage"),
        ]);
        if (currentPath() !== "#/") return;
        $("#summary", page).innerHTML = summaryCards(s);
        updateUplinkChip(s.uplink_down);
        $("#triage-count", page).textContent = `${triage.length} ITEM${triage.length === 1 ? "" : "S"}`;
        $("#triage", page).innerHTML = triage.length
          ? triage.map((o) => triageCard(o)).join("")
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

  // Row variant for the heatmap drill-down: shows that day's downtime + cause
  // instead of the live state.
  function dayNodeRow(n) {
    const cause = n.cause === "power"
      ? `<span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("bolt", { size: 14 })} Power</span>`
      : (n.cause === "link"
        ? `<span class="font-mono-data text-mono-data text-on-surface-variant flex items-center gap-1">${icon("build", { size: 14 })} Link/Equipment</span>` : "");
    return `<div class="bg-surface border border-outline-variant rounded-md p-3 flex flex-row items-center justify-between gap-3">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-8 h-8 rounded-full bg-surface-container-high flex items-center justify-center shrink-0 text-error">${icon("wifi_off", { size: 18 })}</div>
        <div class="min-w-0">
          <h4 class="font-label-md text-label-md text-primary truncate">${esc(n.name)}</h4>
          <p class="font-mono-data text-on-surface-variant mt-0.5 truncate text-[11px]">${esc(n.type || "node")} · ${esc(n.ip)} · ${esc(n.region)}</p>
        </div>
      </div>
      <div class="flex items-center gap-3 shrink-0">${cause}
        <span class="font-mono-data text-mono-data text-error flex items-center gap-1">${icon("schedule", { size: 14 })} ${esc(n.down_label)} down</span>
      </div>
    </div>`;
  }

  // --- node inventory editor (add / edit / delete) --------------------------
  const DEVICE_TYPES = ["core", "tower", "relay", "sector", "backhaul"];
  const CRIT_LABELS = { 1: "1 · lowest", 2: "2 · low", 3: "3 · medium", 4: "4 · high", 5: "5 · core" };

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
    const critOpts = [1, 2, 3, 4, 5].map((n) => ({ value: n, label: CRIT_LABELS[n] }));
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
        ${field("Criticality", "criticality", d.criticality != null ? d.criticality : 3, { options: critOpts })}
        ${field("Parent node", "parent_device_id", d.parent_device_id, { options: parentOpts, full: true })}
        ${field("Power reference IP", "power_ref_ip", d.power_ref_ip, { placeholder: "same-mains node (power vs link)" })}
        ${field("Technician phone", "technician_phone", d.technician_phone, { placeholder: "+91…" })}
        ${field("Customers behind", "customer_count", d.customer_count != null ? d.customer_count : 0, { type: "number" })}
        ${field("Revenue impact (₹/hr)", "base_revenue_impact", d.base_revenue_impact != null ? d.base_revenue_impact : 0, { type: "number", step: "1" })}
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

    overlay.querySelector("[data-node-form]").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const payload = {};
      overlay.querySelectorAll("[data-field]").forEach((el) => { payload[el.getAttribute("data-field")] = el.value; });
      const submitBtn = ev.target.querySelector('[type="submit"]');
      submitBtn.disabled = true;
      const res = isEdit
        ? await sendJSON("PUT", `/api/devices/${d.id}`, payload)
        : await sendJSON("POST", "/api/devices", payload);
      if (res.ok && res.data.ok) {
        toast(isEdit ? "Node updated" : "Node added");
        close(); onDone();
      } else {
        toast(res.data.error || res.data.reason || "Couldn't save node", "error");
        submitBtn.disabled = false;
      }
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
  const SEV = {
    critical: { dot: "bg-error", text: "text-error", label: "Critical" },
    warning: { dot: "bg-amber-500", text: "text-amber-500", label: "Warning" },
    info: { dot: "bg-outline", text: "text-on-surface-variant", label: "Info" },
  };

  function logRows(entries) {
    if (!entries.length) {
      return `<tr><td colspan="7" class="px-4 text-center align-middle text-on-surface-variant font-body-sm" style="height:55vh">
        <div class="flex flex-col items-center gap-2">${icon("search", { size: 28, cls: "text-outline" })}<span>No matching incidents.</span></div>
      </td></tr>`;
    }
    return entries.map((e) => {
      const sv = SEV[e.severity] || SEV.info;
      return `<tr class="hover:bg-surface-container-high/50 transition-colors">
        <td class="px-4 py-3 text-on-surface">${esc(fmtTime(e.timestamp))}</td>
        <td class="px-4 py-3 text-primary">${esc(e.incident)}</td>
        <td class="px-4 py-3 text-on-surface-variant">${esc(e.region)} / ${esc(e.name)}</td>
        <td class="px-4 py-3"><div class="flex items-center gap-2"><div class="w-2 h-2 rounded-full ${sv.dot}"></div><span class="${sv.text}">${sv.label}</span></div></td>
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
              <th class="px-4 py-3 font-semibold">Severity</th>
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
        body.innerHTML = `<tr><td colspan="7">${errorBox(e.message)}</td></tr>`;
      }
    }

    let deb;
    $("#log-search", page).addEventListener("input", (ev) => {
      clearTimeout(deb);
      deb = setTimeout(() => { logState.q = ev.target.value; logState.offset = 0; load(); }, 250);
    });
    return load;
  }

  // --- router + auto-refresh ------------------------------------------------
  const ROUTES = { "#/": renderDashboard, "#/nodes": renderNodes, "#/logs": renderLogs };
  let _refreshTimer = null;
  let _activeLoad = null;

  function formFocused() {
    const a = document.activeElement;
    return a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
  }

  function route() {
    clearInterval(_refreshTimer);
    document.getElementById("root").innerHTML = shell();
    const page = $("#page");
    const render = ROUTES[currentPath()] || renderDashboard;
    const load = render(page);
    _activeLoad = load;
    load();
    // Logs is on-demand (search/paginate); Dashboard + Nodes auto-refresh.
    if (currentPath() !== "#/logs") {
      _refreshTimer = setInterval(() => {
        if (!formFocused() && _activeLoad === load) load();
      }, 15000);
    }
  }

  window.addEventListener("hashchange", route);
  window.addEventListener("DOMContentLoaded", () => {
    if (!location.hash) location.hash = "#/";
    route();
  });
})();
