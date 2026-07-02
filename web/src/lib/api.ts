// Typed fetch wrapper + endpoint functions against src/wisp/central/server.py's JSON API.
// Session auth rides the browser's cookie automatically (see central/auth.py) — no token
// handling needed here, just same-origin fetch. Mirrors the old static/app.js's `api()`
// helper (401 handling, error surfacing), rebuilt for react-query.
import type {
  AccountUser, AttendanceOverview, LogEvent, MeResponse, NodeToken, Org, OrgDevice,
  Outage, Role, Summary, SwitchPort, Worker,
} from "./types"

export class ApiError extends Error {}

async function request<T>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method ?? "GET",
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("wisp:unauthorized"))
  }
  const isJson = res.headers.get("content-type")?.includes("json")
  const data = isJson ? await res.json() : {}
  if (!res.ok) {
    throw new ApiError(data.error || data.reason || `HTTP ${res.status}`)
  }
  return data as T
}

// The org query param a superadmin uses to scope a read/write to one org; org users
// are pinned server-side and ignore it, so it's safe to always include when present.
export function tq(org?: string | null): string {
  return org ? `?org=${encodeURIComponent(org)}` : ""
}

// --- auth -------------------------------------------------------------------------
export const authApi = {
  me: () => request<MeResponse>("/api/me"),
  login: (username: string, password: string) =>
    request<MeResponse>("/api/login", { method: "POST", body: { username, password } }),
  logout: () => request<{ ok: true }>("/api/logout", { method: "POST" }),
}

// --- summary / orgs -----------------------------------------------------------------
export const summaryApi = {
  get: (org?: string | null) => request<Summary>(`/api/summary${tq(org)}`),
}

export const orgsApi = {
  list: (org?: string | null) => request<{ orgs: Org[] }>(`/api/orgs${tq(org)}`),
  create: (body: { org_id: string; name?: string | null }) =>
    request<{ org_id: string }>("/api/orgs", { method: "POST", body }),
  save: (body: {
    org_id: string; name?: string | null
    ntfy_topic_owner?: string | null; ntfy_topic_operator?: string | null; ntfy_topic_tech?: string | null
  }) => request<{ ok: true }>("/api/org", { method: "POST", body }),
  testAlert: (org_id: string, role: Role) =>
    request<{ ok: boolean; detail?: string; channel: string; recipient: string; role: Role }>(
      "/api/test-alert", { method: "POST", body: { org_id, role } }),
}

// --- device inventory / topology -----------------------------------------------------
export interface DevicePayload {
  org_id?: string
  name: string
  ip_address: string
  device_type?: string | null
  region?: string | null
  parent_device_id?: number | null
  assigned_node_id?: string | null
}

export const inventoryApi = {
  list: (org?: string | null) => request<{ devices: OrgDevice[] }>(`/api/inventory${tq(org)}`),
  create: (body: DevicePayload) => request<{ id: number }>("/api/inventory", { method: "POST", body }),
  update: (id: number, body: DevicePayload) =>
    request<{ ok: boolean }>("/api/inventory/update", { method: "POST", body: { id, ...body } }),
  remove: (id: number) =>
    request<{ ok: boolean; reason?: string }>("/api/inventory/delete", { method: "POST", body: { id } }),
  setMaintenance: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/maintenance", { method: "POST", body: { id, on } }),
  setSnmp: (id: number, body: {
    snmp_enabled: boolean; snmp_community?: string | null; snmp_port?: number | string
  }) => request<{ ok: boolean }>("/api/inventory/snmp", { method: "POST", body: { id, ...body } }),
  ports: (deviceId: number) => request<{ ports: SwitchPort[] }>(`/api/inventory/ports?device_id=${deviceId}`),
  setPortMonitored: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/ports/monitored", { method: "POST", body: { id, on } }),
  setPortFeeds: (id: number, feeds_device_id: number | null) =>
    request<{ ok: boolean }>("/api/inventory/ports/feeds", { method: "POST", body: { id, feeds_device_id } }),
  setPortBandwidth: (id: number, threshold_mbps: number | null, direction: string) =>
    request<{ ok: boolean }>("/api/inventory/ports/bandwidth", { method: "POST", body: { id, threshold_mbps, direction } }),
  addBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: true }>("/api/inventory/links", { method: "POST", body: { child_id, parent_id } }),
  removeBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: boolean }>("/api/inventory/links/delete", { method: "POST", body: { child_id, parent_id } }),
}

// --- outages / triage ----------------------------------------------------------------
export const outagesApi = {
  list: (org?: string | null) => request<{ outages: Outage[] }>(`/api/outages${tq(org)}`),
  acknowledge: (outage_id: number) =>
    request<{ ok: boolean }>("/api/outages/acknowledge", { method: "POST", body: { outage_id } }),
  postmortem: (outage_id: number, root_cause: string, resolution_notes?: string) =>
    request<{ ok: boolean }>("/api/outages/postmortem",
      { method: "POST", body: { outage_id, root_cause, resolution_notes } }),
}

// --- edge node enrollment -------------------------------------------------------------
export const nodesApi = {
  list: (org?: string | null) => request<{ nodes: NodeToken[] }>(`/api/nodes${tq(org)}`),
  register: (org_id: string, node_id: string) =>
    request<{ node_id: string; token: string }>("/api/nodes", { method: "POST", body: { org_id, node_id } }),
  rotate: (org_id: string, node_id: string) =>
    request<{ node_id: string; token: string }>("/api/nodes/rotate", { method: "POST", body: { org_id, node_id } }),
  revoke: (org_id: string, node_id: string) =>
    request<{ ok: boolean }>("/api/nodes/revoke", { method: "POST", body: { org_id, node_id } }),
  remove: (org_id: string, node_id: string) =>
    request<{ ok: boolean; error?: string }>("/api/nodes/delete", { method: "POST", body: { org_id, node_id } }),
}

// --- team + attendance -----------------------------------------------------------------
export const teamApi = {
  list: (org?: string | null) => request<{ team: Worker[] }>(`/api/team${tq(org)}`),
  add: (body: { org_id: string; name: string; role: Role; region?: string; notes?: string }) =>
    request<{ id: number }>("/api/team", { method: "POST", body }),
  update: (id: number, body: { name?: string; role?: Role; region?: string; notes?: string }) =>
    request<{ ok: true }>("/api/team/update", { method: "POST", body: { id, ...body } }),
  remove: (id: number) => request<{ ok: true }>("/api/team/delete", { method: "POST", body: { id } }),
  attendance: (org?: string | null) => request<AttendanceOverview>(`/api/attendance${tq(org)}`),
  setPresent: (worker_id: number, present: boolean, day?: string) =>
    request<{ ok: true }>("/api/attendance", { method: "POST", body: { worker_id, present, day } }),
}

// --- logs ------------------------------------------------------------------------------
export const logsApi = {
  list: (org: string | null | undefined, limit = 100, before?: number) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    params.set("limit", String(limit))
    if (before != null) params.set("before", String(before))
    return request<{ events: LogEvent[] }>(`/api/logs?${params.toString()}`)
  },
}

// --- user provisioning -------------------------------------------------------------------
export const usersApi = {
  list: (org?: string | null) => request<{ users: AccountUser[] }>(`/api/users${tq(org)}`),
  create: (body: { org_id?: string; username: string; password: string; role: Role }) =>
    request<{ id: number }>("/api/users", { method: "POST", body }),
  setActive: (id: number, active: boolean) =>
    request<{ ok: true }>("/api/users/deactivate", { method: "POST", body: { id, active } }),
  remove: (id: number) => request<{ ok: true }>("/api/users/delete", { method: "POST", body: { id } }),
  // Self-service (own account — needs current_password) or an owner/superadmin
  // resetting a teammate's (pass id, no current_password required).
  changePassword: (body: { id?: number; current_password?: string; new_password: string }) =>
    request<{ ok: true }>("/api/users/password", { method: "POST", body }),
}
