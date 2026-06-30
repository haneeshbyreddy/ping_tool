"use strict";
// WISP Central console (Phase 10 Part C) — vanilla JS, no build step, no deps.
// Talks to the same JSON API the CLI/curl use; the session cookie is sent automatically.

const $ = (sel, el = document) => el.querySelector(sel);
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let ME = null;            // current user
let TENANT = "";          // superadmin's selected org ("" = all); org users ignore this

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
      <main class="grid" id="main"></main>
    </div>`));
  $("#logout").onclick = async () => { await api("/api/logout", "POST", {}); ME = null; renderLogin(); };
  if (ME.is_superadmin) renderOrgPicker();
  refresh();
}

async function renderOrgPicker() {
  const { orgs } = await api("/api/orgs");
  const sel = h(`<select><option value="">All orgs</option>${
    orgs.map(o => `<option value="${esc(o.tenant_id)}">${esc(o.tenant_id)} (${o.node_count} nodes)</option>`).join("")
  }</select>`);
  sel.value = TENANT;
  sel.onchange = () => { TENANT = sel.value; refresh(); };
  $("#orgpick").replaceChildren(sel);
}

async function refresh() {
  const main = $("#main");
  main.replaceChildren(h(`<div class="card muted">Loading…</div>`));
  try {
    const fleet = await api(`/api/fleet${q()}`);
    const { devices } = await api(`/api/devices${q()}`);
    const cards = [nodesCard(fleet.nodes), devicesCard(devices), eventsCard(fleet.recent_events)];
    // Team + attendance need a concrete tenant (an org user always has one).
    const t = scope();
    if (t) {
      const team = await api(`/api/team${q() || "?tenant=" + encodeURIComponent(t)}`);
      const att = await api(`/api/attendance${q() || "?tenant=" + encodeURIComponent(t)}`);
      cards.push(teamCard(team.team, t), attendanceCard(att, t));
    } else if (ME.is_superadmin) {
      cards.push(h(`<div class="card muted">Select an org to manage its team & attendance.</div>`));
    }
    main.replaceChildren(...cards);
  } catch (e) { main.replaceChildren(h(`<div class="card err">${esc(e.message)}</div>`)); }
}

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
  if (s < 90) return `${s | 0}s ago`;
  if (s < 5400) return `${(s / 60) | 0}m ago`;
  return `${(s / 3600) | 0}h ago`;
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
  return h(`<div class="card"><h2>Devices (${devices.length})</h2><table>
    <tr><th>#</th><th>Org / Node</th><th>Name</th><th>IP</th><th>State</th></tr>
    ${devices.map(d => `<tr>
      <td class="num muted">${d.id}</td>
      <td class="muted">${esc(d.tenant_id)} / ${esc(d.node_id)}</td>
      <td>${esc(d.name || "—")}</td><td class="muted">${esc(d.ip || "—")}</td>
      <td>${d.last_state ? `<span class="pill ${esc(d.last_state)}">${esc(d.last_state)}</span>` : "—"}</td>
    </tr>`).join("") || `<tr><td colspan=5 class="muted">No devices yet.</td></tr>`}
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

boot();
