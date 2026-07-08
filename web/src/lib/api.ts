import type {
  AccountUser, AttendanceOverview, LogEvent, MeResponse, NodesResponse, Org, OrgDevice,
  OrgRegion, Outage, PerfSample, PerfState, OpticsResponse, ReliabilityRow, Role, Summary,
  SwitchPort, SystemStats, TrendBucket, Worker,
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

export function tq(org?: string | null): string {
  return org ? `?org=${encodeURIComponent(org)}` : ""
}

export const authApi = {
  me: () => request<MeResponse>("/api/me"),
  login: (username: string, password: string) =>
    request<MeResponse>("/api/login", { method: "POST", body: { username, password } }),
  logout: () => request<{ ok: true }>("/api/logout", { method: "POST" }),
}

export const summaryApi = {
  get: (org?: string | null) => request<Summary>(`/api/summary${tq(org)}`),
}

export const systemApi = {
  get: () => request<SystemStats>("/api/system"),
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

export interface DevicePayload {
  org_id?: string
  name: string
  ip_address: string
  device_type?: string | null
  region?: string | null
  parent_device_id?: number | null
  assigned_node_id?: string | null
  gpon_vendor?: string | null
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
  perfSamples: (deviceId: number) =>
    request<{ samples: PerfSample[] }>(`/api/inventory/perf/samples?device_id=${deviceId}`),
  perf: (deviceId: number) =>
    request<{ perf: PerfState | null }>(`/api/inventory/perf?device_id=${deviceId}`),
  setPortMonitored: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/ports/monitored", { method: "POST", body: { id, on } }),
  setPortFeeds: (id: number, feeds_device_id: number | null) =>
    request<{ ok: boolean }>("/api/inventory/ports/feeds", { method: "POST", body: { id, feeds_device_id } }),
  setPortBandwidth: (
    id: number, threshold_mbps: number | null, direction: string, max_mbps: number | null,
  ) => request<{ ok: boolean }>("/api/inventory/ports/bandwidth", {
    method: "POST", body: { id, threshold_mbps, max_mbps, direction },
  }),
  addBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: true }>("/api/inventory/links", { method: "POST", body: { child_id, parent_id } }),
  removeBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: boolean }>("/api/inventory/links/delete", { method: "POST", body: { child_id, parent_id } }),

  optics: (deviceId: number) =>
    request<OpticsResponse>(`/api/inventory/optics?device_id=${deviceId}`),
  ackOnu: (id: number, hours: number | null) =>
    request<{ ok: boolean }>("/api/inventory/optics/ack",
      { method: "POST", body: hours == null ? { id, until: "clear" } : { id, hours } }),
  setOpticalThresholds: (device_id: number, warn_dbm: number | null, crit_dbm: number | null) =>
    request<{ ok: boolean }>("/api/inventory/optics/thresholds",
      { method: "POST", body: { device_id, warn_dbm, crit_dbm } }),
}

export const analyticsApi = {
  trend: (deviceId: number, days = 1) =>
    request<{ since: string; until: string; buckets: TrendBucket[] }>(
      `/api/analytics/trend?device_id=${deviceId}&days=${days}`),
  reliability: (org: string | null | undefined, days = 7) =>
    request<{ since: string; until: string; devices: ReliabilityRow[] }>(
      `/api/analytics?days=${days}${org ? `&org=${encodeURIComponent(org)}` : ""}`),
}

export const outagesApi = {
  list: (org?: string | null) => request<{ outages: Outage[] }>(`/api/outages${tq(org)}`),
  acknowledge: (outage_id: number) =>
    request<{ ok: boolean }>("/api/outages/acknowledge", { method: "POST", body: { outage_id } }),
  postmortem: (outage_id: number, root_cause: string, resolution_notes?: string) =>
    request<{ ok: boolean }>("/api/outages/postmortem",
      { method: "POST", body: { outage_id, root_cause, resolution_notes } }),
  clearPostmortems: (org: string | null, root_cause?: string) =>
    request<{ ok: boolean; cleared: number }>("/api/outages/clear-postmortems",
      { method: "POST", body: { org, root_cause } }),
}

export const nodesApi = {
  list: (org?: string | null) => request<NodesResponse>(`/api/nodes${tq(org)}`),
  update: (org_id: string, node_id: string) =>
    request<{ ok: boolean; target_version: string }>("/api/nodes/update", { method: "POST", body: { org_id, node_id } }),
  register: (org_id: string, node_id: string) =>
    request<{ node_id: string; token: string }>("/api/nodes", { method: "POST", body: { org_id, node_id } }),
  rotate: (org_id: string, node_id: string) =>
    request<{ node_id: string; token: string }>("/api/nodes/rotate", { method: "POST", body: { org_id, node_id } }),
  revoke: (org_id: string, node_id: string) =>
    request<{ ok: boolean }>("/api/nodes/revoke", { method: "POST", body: { org_id, node_id } }),
  remove: (org_id: string, node_id: string) =>
    request<{ ok: boolean; error?: string }>("/api/nodes/delete", { method: "POST", body: { org_id, node_id } }),
}

export const regionsApi = {
  list: (org?: string | null) => request<{ regions: OrgRegion[] }>(`/api/regions${tq(org)}`),
  create: (org_id: string, name: string) =>
    request<{ ok: true }>("/api/regions", { method: "POST", body: { org_id, name } }),
  rename: (org_id: string, from: string, to: string) =>
    request<{ ok: true }>("/api/regions/rename", { method: "POST", body: { org_id, old: from, new: to } }),
  remove: (org_id: string, name: string) =>
    request<{ ok: boolean; reason?: string }>("/api/regions/delete", { method: "POST", body: { org_id, name } }),
}

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

export const logsApi = {
  list: (org: string | null | undefined, limit = 100, before?: number) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    params.set("limit", String(limit))
    if (before != null) params.set("before", String(before))
    return request<{ events: LogEvent[] }>(`/api/logs?${params.toString()}`)
  },
}

export const usersApi = {
  list: (org?: string | null) => request<{ users: AccountUser[] }>(`/api/users${tq(org)}`),
  create: (body: { org_id?: string; username: string; password: string; role: Role }) =>
    request<{ id: number }>("/api/users", { method: "POST", body }),
  setActive: (id: number, active: boolean) =>
    request<{ ok: true }>("/api/users/deactivate", { method: "POST", body: { id, active } }),
  remove: (id: number) => request<{ ok: true }>("/api/users/delete", { method: "POST", body: { id } }),

  changePassword: (body: { id?: number; current_password?: string; new_password: string }) =>
    request<{ ok: true }>("/api/users/password", { method: "POST", body }),
}
