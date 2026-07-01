"use strict";
// WISP Central console (Phase 10 Part C + Phase A management plane) — vanilla JS, no
// build step, no deps. Talks to the same JSON API the CLI/curl use; the session cookie
// is sent automatically.

const $ = (sel, el = document) => el.querySelector(sel);
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Kept in lockstep with central/inventory.py DEVICE_TYPES (and the edge's SPA dropdown).
const DEVICE_TYPES = ["core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul"];

let ME = null;             // current user
let TENANT = "";           // superadmin's selected org ("" = all); org users ignore this
let PAGE = "overview";     // overview | nodes | agents | team | settings
let NODE_EDIT = null;      // the org_devices row currently being edited on the Nodes page, or null
let AGENT_REVEAL = null;   // {node_id, token} shown once right after register/rotate, or null

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

async function boot() {
  try { const me = await api("/api/me"); ME = me.user; renderApp(); }
  catch { /* renderLogin already called on 401 */ }
}

function renderLogin(err = "") {
  document.body.querySelector("#app").replaceChildren(h(`
    <div class="login"><div class="card">
      <h1 style="color:var(--acc);margin:0">WISP Central</h1>
      <input id="u" placeholder="username" autocomplete="username">
      <input id="p" type="password" placeholder="password" autocomplete="current-password">
      <button id="go">Sign in</button>
      <div class="err">${esc(err)}</div>
    </div></div>`));
  const submit = async () => {
    try {
      const r = await api("/api/login", "POST", { username: $("#u").value, password: $("#p").value });
      ME = r.user; renderApp();
    } catch (e) { renderLogin(e.message); }
  };
  $("#go").onclick = submit;
  $("#p").onkeydown = (e) => { if (e.key === "Enter") submit(); };
}

const PAGES = [["overview", "Overview"], ["nodes", "Nodes"], ["agents", "Edge Nodes"],
              ["team", "Team"], ["settings", "Settings"]];

function renderApp() {
  const orgLabel = ME.is_superadmin ? "Superadmin" : `${esc(ME.tenant_id)} · ${esc(ME.role)}`;
  const app = document.body.querySelector("#app");
  app.replaceChildren(h(`
    <div>
      <header>
        <h1>WISP Central</h1>
        <span id="orgpick"></span>
        <span class="spacer"></span>
        <span class="muted">${esc(ME.username)} · ${orgLabel}</span>
        <button class="ghost" id="logout">Sign out</button>
      </header>
      <nav class="tabs" id="tabs">${PAGES.map(([key, label]) =>
        `<button class="tab ${PAGE === key ? "active" : ""}" data-page="${key}">${label}</button>`).join("")}</nav>
      <main class="grid" id="main"></main>
    </div>`));
  $("#logout").onclick = async () => { await api("/api/logout", "POST", {}); ME = null; renderLogin(); };
  $("#tabs").querySelectorAll("[data-page]").forEach(b => b.onclick = () => {
    PAGE = b.dataset.page; NODE_EDIT = null; AGENT_REVEAL = null; renderApp();
  });
  if (ME.is_superadmin) renderOrgPicker();
  refresh();
}

async function renderOrgPicker() {
  const { orgs } = await api("/api/orgs");
  const sel = h(`<select><option value="">All orgs</option>${
    orgs.map(o => `<option value="${esc(o.tenant_id)}">${esc(o.tenant_id)} (${o.node_count} nodes)</option>`).join("")
  }</select>`);
  sel.value = TENANT;
  sel.onchange = () => { TENANT = sel.value; NODE_EDIT = null; AGENT_REVEAL = null; refresh(); };
  $("#orgpick").replaceChildren(sel);
}

async function refresh() {
  const main = $("#main");
  main.replaceChildren(h(`<div class="card muted">Loading…</div>`));
  try {
    if (PAGE === "nodes") await renderNodesPage(main);
    else if (PAGE === "agents") await renderAgentsPage(main);
    else if (PAGE === "team") await renderTeamPage(main);
    else if (PAGE === "settings") await renderSettingsPage(main);
    else await renderOverviewPage(main);
  } catch (e) { main.replaceChildren(h(`<div class="card err">${esc(e.message)}</div>`)); }
}

function needsOrgCard() {
  return h(`<div class="card muted">Select an org above to manage it.</div>`);
}

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
  if (s < 90) return `${s | 0}s ago`;
  if (s < 5400) return `${(s / 60) | 0}m ago`;
  return `${(s / 3600) | 0}h ago`;
}

// --- Overview page: fleet health, the edge-ingest device registry, recent events -----
async function renderOverviewPage(main) {
  const fleet = await api(`/api/fleet${q()}`);
  const { devices } = await api(`/api/devices${q()}`);
  main.replaceChildren(nodesCard(fleet.nodes), devicesCard(devices), eventsCard(fleet.recent_events));
}

function nodesCard(nodes) {
  const stale = (n) => (Date.now() - Date.parse(n.last_seen)) / 1000 > 180;
  return h(`<div class="card"><h2>Edge nodes (${nodes.length})</h2><table>
    <tr><th>Org</th><th>Node</th><th>Version</th><th>Fleet</th><th>Open</th><th>Last seen</th></tr>
    ${nodes.map(n => `<tr>
      <td>${esc(n.tenant_id)}</td><td>${esc(n.node_id)}</td>
      <td class="muted">${esc(n.version || "—")}</td>
      <td class="num">${n.fleet_size ?? "—"}</td>
      <td class="num">${n.open_outages ? `<span class="pill DOWN">${n.open_outages}</span>` : "0"}</td>
      <td><span class="pill ${stale(n) ? "stale" : "ok"}">${ago(n.last_seen)}</span></td>
    </tr>`).join("") || `<tr><td colspan=6 class="muted">No nodes yet.</td></tr>`}
  </table></div>`);
}

function devicesCard(devices) {
  return h(`<div class="card"><h2>Live device registry (${devices.length})</h2>
    <div class="muted" style="margin-bottom:8px">Reported by connected edges. Configure topology on the <b>Nodes</b> page.</div>
    <table>
    <tr><th>#</th><th>Org / Node</th><th>Name</th><th>IP</th><th>State</th></tr>
    ${devices.map(d => `<tr>
      <td class="num muted">${d.id}</td>
      <td class="muted">${esc(d.tenant_id)} / ${esc(d.node_id)}</td>
      <td>${esc(d.name || "—")}</td><td class="muted">${esc(d.ip || "—")}</td>
      <td>${d.last_state ? `<span class="pill ${esc(d.last_state)}">${esc(d.last_state)}</span>` : "—"}</td>
    </tr>`).join("") || `<tr><td colspan=5 class="muted">No devices reported yet.</td></tr>`}
  </table></div>`);
}

function eventsCard(events) {
  return h(`<div class="card"><h2>Recent events</h2><table>
    <tr><th>Org / Node</th><th>Type</th><th>Device</th><th>State</th><th>When</th></tr>
    ${events.map(e => `<tr>
      <td class="muted">${esc(e.tenant_id)} / ${esc(e.node_id)}</td>
      <td>${esc(e.type || "—")}</td><td>${esc(e.device_name || e.device_id || "—")}</td>
      <td>${e.state ? `<span class="pill ${esc(e.state)}">${esc(e.state)}</span>` : "—"}</td>
      <td class="muted">${ago(e.occurred_at || e.received_at)}</td>
    </tr>`).join("") || `<tr><td colspan=5 class="muted">No events yet.</td></tr>`}
  </table></div>`);
}

// --- Nodes page: the ISP-managed device topology (management plane, Phase A) ---------
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
  // orphans (parent id set but not present, e.g. race with a concurrent delete) — still show
  for (const d of devices) if (!seen.has(d.id)) out.push({ ...d, _depth: 0 });
  return out;
}

function nodesPageCard(devices, tenant) {
  const ordered = treeOrder(devices);
  const write = canWrite();
  const editing = NODE_EDIT;
  const card = h(`<div class="card">
    <h2>Nodes — ${esc(tenant)} (${devices.length})</h2>
    <table><tr><th>Name</th><th>Type</th><th>IP</th><th>Region</th><th>Badges</th>${write ? "<th></th>" : ""}</tr>
    ${ordered.map(d => `<tr>
      <td style="padding-left:${8 + d._depth * 18}px">${d._depth ? "↳ " : ""}${esc(d.name)}</td>
      <td class="muted">${esc(d.device_type || "—")}</td>
      <td class="muted">${esc(d.ip_address)}</td>
      <td class="muted">${esc(d.region || "—")}</td>
      <td>${d.maintenance ? `<span class="pill maint">maintenance</span>` : ""}
          ${d.snmp_enabled ? `<span class="pill ok">SNMP</span>` : ""}
          ${d.child_count ? `<span class="pill muted-pill">${d.child_count} child</span>` : ""}</td>
      ${write ? `<td class="row">
          <button class="ghost" data-edit="${d.id}">edit</button>
          <button class="ghost" data-maint="${d.id}" data-on="${d.maintenance ? 0 : 1}">${d.maintenance ? "resume" : "pause"}</button>
          <button class="ghost" data-del="${d.id}">delete</button>
        </td>` : ""}
    </tr>`).join("") || `<tr><td colspan=${write ? 6 : 5} class="muted">No nodes yet — add one below.</td></tr>`}
    </table>
  </div>`);

  if (write) {
    const parentOpts = devices.filter(d => !editing || d.id !== editing.id)
      .map(d => `<option value="${d.id}" ${editing?.parent_device_id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("");
    const typeOpts = DEVICE_TYPES.map(tp => `<option ${editing?.device_type === tp ? "selected" : ""}>${tp}</option>`).join("");
    const form = h(`<div class="card" style="margin-top:12px">
      <h2>${editing ? `Edit — ${esc(editing.name)}` : "Add node"}</h2>
      <div class="row">
        <input id="fn" placeholder="name" value="${editing ? esc(editing.name) : ""}" style="width:160px">
        <input id="fip" placeholder="ip address" value="${editing ? esc(editing.ip_address) : ""}" style="width:140px">
        <select id="ftype"><option value="">(type)</option>${typeOpts}</select>
        <input id="freg" placeholder="region" value="${editing ? esc(editing.region || "") : ""}" style="width:120px">
        <select id="fparent"><option value="">— none (root) —</option>${parentOpts}</select>
      </div>
      ${editing ? `<div class="row" style="margin-top:8px">
        <label class="muted"><input type="checkbox" id="fsnmp" ${editing.snmp_enabled ? "checked" : ""}> SNMP enabled</label>
        <input id="fcomm" placeholder="community" value="${esc(editing.snmp_community || "")}" style="width:120px">
        <input id="fport" placeholder="port" value="${editing.snmp_port || 161}" style="width:70px">
      </div>` : ""}
      <div class="row" style="margin-top:10px">
        <button id="fsave">${editing ? "Save" : "Add"}</button>
        ${editing ? `<button class="ghost" id="fcancel">Cancel</button>` : ""}
      </div>
      <div class="err" id="ferr"></div>
    </div>`);
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
    card.append(form);

    card.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => {
      NODE_EDIT = devices.find(d => d.id === +b.dataset.edit) || null; refresh();
    });
    card.querySelectorAll("[data-maint]").forEach(b => b.onclick = async () => {
      await api("/api/inventory/maintenance", "POST", { id: +b.dataset.maint, on: b.dataset.on === "1" });
      refresh();
    });
    card.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm("Delete this node? This can't be undone.")) return;
      try { await api("/api/inventory/delete", "POST", { id: +b.dataset.del }); refresh(); }
      catch (e) { alert(e.message); }
    });
  }
  return card;
}

// --- Edge Nodes page: self-service enrollment for physical probes (distinct from the
// "Nodes" tab above, which is device TOPOLOGY — this page is just ingest credentials) --
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
  const status = (n) => n.revoked_at ? `<span class="pill DOWN">revoked</span>`
    : n.last_seen ? `<span class="pill ${stale(n) ? "stale" : "ok"}">${ago(n.last_seen)}</span>`
    : `<span class="pill muted-pill">never connected</span>`;
  const card = h(`<div class="card">
    <h2>Edge nodes — ${esc(tenant)} (${nodes.length})</h2>
    <div class="muted" style="margin-bottom:8px">The physical probes this org has registered,
      and their enrollment credentials. What each one MONITORS is configured on the
      <b>Nodes</b> tab — this page is only about a probe's identity/credential.</div>
    <table><tr><th>Node id</th><th>Status</th><th>Version</th><th>Registered</th>${write ? "<th></th>" : ""}</tr>
    ${nodes.map(n => `<tr>
      <td>${esc(n.node_id)}</td>
      <td>${status(n)}</td>
      <td class="muted">${esc(n.version || "—")}</td>
      <td class="muted">${ago(n.created_at)}</td>
      ${write ? `<td class="row">
          <button class="ghost" data-rotate="${esc(n.node_id)}">rotate</button>
          ${n.revoked_at ? "" : `<button class="ghost" data-revoke="${esc(n.node_id)}">revoke</button>`}
        </td>` : ""}
    </tr>`).join("") || `<tr><td colspan=${write ? 5 : 4} class="muted">No nodes registered yet — add one below.</td></tr>`}
    </table>
  </div>`);

  if (!write) return card;

  if (AGENT_REVEAL) {
    const reveal = h(`<div class="card" style="margin-top:12px">
      <h2>Save this now — it won't be shown again</h2>
      <div class="muted">Token for <b>${esc(AGENT_REVEAL.node_id)}</b>:</div>
      <div class="row" style="margin:6px 0"><code style="word-break:break-all">${esc(AGENT_REVEAL.token)}</code></div>
      <div class="muted" style="margin-top:8px">Run this on the edge box (or its Windows
        equivalent, <code>install-edge.ps1</code>):</div>
      <pre style="white-space:pre-wrap;background:#0b1220;border:1px solid var(--line);
                  border-radius:6px;padding:10px;font-size:12px;margin:6px 0">${esc(
                    installCmd(tenant, AGENT_REVEAL.node_id, AGENT_REVEAL.token))}</pre>
      <button class="ghost" id="agentdismiss">I've saved it</button>
    </div>`);
    $("#agentdismiss", reveal).onclick = () => { AGENT_REVEAL = null; refresh(); };
    card.append(reveal);
  }

  const form = h(`<div class="card" style="margin-top:12px">
    <h2>Register a new node</h2>
    <div class="row">
      <input id="anid" placeholder="node id, e.g. edge-a1" style="width:200px">
      <button id="anadd">Register</button>
    </div>
    <div class="err" id="anerr"></div>
  </div>`);
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
  card.append(form);

  card.querySelectorAll("[data-rotate]").forEach(b => b.onclick = async () => {
    if (!confirm(`Rotate ${b.dataset.rotate}'s token? The old one stops working immediately.`)) return;
    try {
      const r = await api("/api/nodes/rotate", "POST", { tenant_id: tenant, node_id: b.dataset.rotate });
      AGENT_REVEAL = { node_id: r.node_id, token: r.token };
      refresh();
    } catch (e) { alert(e.message); }
  });
  card.querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => {
    if (!confirm(`Revoke ${b.dataset.revoke}'s token? It will stop reporting until re-enrolled.`)) return;
    await api("/api/nodes/revoke", "POST", { tenant_id: tenant, node_id: b.dataset.revoke });
    refresh();
  });
  return card;
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
  const card = h(`<div class="card"><h2>Team — ${esc(tenant)}</h2><table id="teamtbl">
    <tr><th>Name</th><th>Role</th><th>Region</th><th></th></tr>
    ${team.map(w => `<tr>
      <td>${esc(w.name)}</td><td>${esc(w.role)}</td><td class="muted">${esc(w.region || "—")}</td>
      <td>${canWrite() ? `<button class="ghost" data-del="${w.id}">remove</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan=4 class="muted">No team members yet.</td></tr>`}
  </table></div>`);
  if (canWrite()) {
    const form = h(`<div class="row" style="margin-top:10px">
      <input id="wn" placeholder="name" style="width:140px">
      <select id="wr"><option>operator</option><option>owner</option><option>tech</option></select>
      <input id="wreg" placeholder="region" style="width:120px">
      <button id="wadd">Add</button></div>`);
    card.append(form);
    $("#wadd", card).onclick = async () => {
      const name = $("#wn", card).value.trim(); if (!name) return;
      await api("/api/team", "POST", { tenant_id: tenant, name, role: $("#wr", card).value, region: $("#wreg", card).value });
      refresh();
    };
    card.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      await api("/api/team/delete", "POST", { id: +b.dataset.del }); refresh();
    });
  }
  return card;
}

function attendanceCard(att, tenant) {
  const head = `<tr><th>Operator</th>${att.days.map(d => `<th>${d.slice(5)}</th>`).join("")}</tr>`;
  const rows = att.operators.map(op => `<tr><td>${esc(op.name)}</td>${
    att.days.map(d => `<td><span class="chip ${op.days[d] ? "on" : ""}" data-w="${op.id}" data-d="${d}">${op.days[d] ? "✓" : "·"}</span></td>`).join("")
  }</tr>`).join("") || `<tr><td class="muted">No operators on the roster.</td></tr>`;
  const card = h(`<div class="card"><h2>Attendance — ${esc(tenant)}</h2><table>${head}${rows}</table></div>`);
  if (canWrite()) {
    card.querySelectorAll(".chip").forEach(c => c.onclick = async () => {
      const present = !c.classList.contains("on");
      await api("/api/attendance", "POST", { worker_id: +c.dataset.w, day: c.dataset.d, present });
      refresh();
    });
  }
  return card;
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
  const card = h(`<div class="card"><h2>Settings — ${esc(org.tenant_id)}</h2>
    <div class="row">
      <label class="muted">Org name</label>
      <input id="sname" value="${esc(org.name || "")}" ${write ? "" : "disabled"} style="width:220px">
    </div>
    ${roles.map(([key, label]) => `
      <div class="row" style="margin-top:10px">
        <label class="muted" style="width:70px">${label} topic</label>
        <input id="stopic_${key}" value="${esc(org[`ntfy_topic_${key}`] || "")}" ${write ? "" : "disabled"}
               placeholder="ntfy topic for ${label.toLowerCase()} alerts" style="width:220px">
        ${write ? `<button class="ghost" data-test="${key}">Send test</button>` : ""}
        <span class="muted" id="stest_${key}"></span>
      </div>`).join("")}
    ${write ? `<div class="row" style="margin-top:12px"><button id="ssave">Save</button></div>` : ""}
    <div class="err" id="serr"></div>
  </div>`);
  if (write) {
    $("#ssave", card).onclick = async () => {
      const serr = $("#serr", card);
      serr.textContent = "";
      try {
        await api("/api/org", "POST", {
          tenant_id: org.tenant_id, name: $("#sname", card).value.trim() || null,
          ntfy_topic_owner: $("#stopic_owner", card).value.trim() || null,
          ntfy_topic_operator: $("#stopic_operator", card).value.trim() || null,
          ntfy_topic_tech: $("#stopic_tech", card).value.trim() || null,
        });
        refresh();
      } catch (e) { serr.textContent = e.message; }
    };
    card.querySelectorAll("[data-test]").forEach(b => b.onclick = async () => {
      const role = b.dataset.test;
      const out = $(`#stest_${role}`, card);
      out.textContent = "sending…";
      try {
        const r = await api("/api/test-alert", "POST", { tenant_id: org.tenant_id, role });
        out.textContent = r.ok ? "✓ sent" : `failed: ${r.detail || ""}`;
      } catch (e) { out.textContent = e.message; }
    });
  }
  return card;
}

boot();
