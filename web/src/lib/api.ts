import type {
  AccountUser, AdminOverview, AssignmentRoster, BillingInfo, Cable, GponProfilesResponse, IncidentShape, IssueKind, IssuesResponse, LinkPort, LinkRoute, LogEvent, MeResponse, NodesResponse, Org, OrgDevice,
  OnuCoverageResponse, OnuSearchResponse, OrgRegion, Outage, PerfSample, PerfState, Plan, OpticsResponse, ProxyAudit, ProxySession, ReliabilityRow, Role,
  OnuPlace, PonFault, PonSummary, SnmpProfilesResponse, SnmpStatusResponse, SnmpSubsystem, SnmpWalk, SnmpWalkResult,
  BranchFault, FibreTrace, PointFibre, SplitterLoad, Subscriber, SubscriberDrop,
  Summary, SwitchPort, SystemStats, TrayPort, TrendBucket, WebUiCredentials,
  RxStatusResponse, WebOpticsProfileSpec, WebOpticsProfilesResponse, WhatsappSettings,
  FieldTokensResponse, FieldWorkersResponse, ShiftState,
} from "./types"
import type { ThemeOverrides } from "./theme-tokens"
import type { MapDetail } from "@/map/detail"

export class ApiError extends Error {
  status: number
  body: Record<string, unknown>
  constructor(message: string, status = 0, body: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method ?? "GET",
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("wisp:unauthorized"))
  }
  if (res.status === 402) {
    window.dispatchEvent(new CustomEvent("wisp:payment-required"))
  }
  const isJson = res.headers.get("content-type")?.includes("json")
  const data = isJson ? await res.json() : {}
  if (!res.ok) {
    throw new ApiError(data.error || data.reason || `HTTP ${res.status}`, res.status, data)
  }
  return data as T
}

export function tq(org?: string | null): string {
  return org ? `?org=${encodeURIComponent(org)}` : ""
}

export function aq(org?: string | null): string {
  return org ? `&org=${encodeURIComponent(org)}` : ""
}

export const authApi = {
  me: () => request<MeResponse>("/api/me"),
  login: (username: string, password: string, remember = false,
          second?: { totp?: string; recovery?: string }) =>
    request<MeResponse>("/api/login", {
      method: "POST",
      body: { username, password, remember, ...(second ?? {}) },
    }),
  logout: () => request<{ ok: true }>("/api/logout", { method: "POST" }),
}

export const summaryApi = {
  get: (org?: string | null) => request<Summary>(`/api/summary${tq(org)}`),
}

export const systemApi = {
  get: () => request<SystemStats>("/api/system"),
}

export const adminApi = {
  overview: () => request<AdminOverview>("/api/admin/overview"),
  settings: () => request<{
    google_maps_key: string | null
    billing_gpay_number: string
    billing_qr_image: string | null
    whatsapp: WhatsappSettings
    theme_overrides: ThemeOverrides
    map_detail: MapDetail
  }>("/api/admin/settings"),
  saveSettings: (body: {
    google_maps_key?: string | null
    billing_gpay_number?: string | null
    billing_qr_image?: string | null
    whatsapp?: {
      enabled?: boolean
      phone_id?: string
      template?: string
      lang?: string
      api_version?: string
      admin_number?: string
      token?: string
      token_clear?: boolean
    }
    theme_overrides?: ThemeOverrides
    map_detail?: MapDetail
  }) =>
    request<{ ok: true }>("/api/admin/settings", { method: "POST", body }),
}

export const billingApi = {
  get: (org?: string | null) => request<BillingInfo>(`/api/billing${tq(org)}`),
  adminSave: (body: { org_id: string; plan?: Plan; month?: string; paid?: boolean }) =>
    request<{ ok: true } & BillingInfo>("/api/admin/billing", { method: "POST", body }),
  markPaid: (org?: string | null) =>
    request<{ ok: true; notified: boolean }>("/api/billing/paid", { method: "POST", body: { org_id: org } }),
  setPlan: (body: { org_id?: string | null; plan: Plan }) =>
    request<{ ok: true } & BillingInfo>("/api/billing/plan", { method: "POST", body }),
}

export const orgsApi = {
  list: (org?: string | null) => request<{ orgs: Org[] }>(`/api/orgs${tq(org)}`),
  create: (body: { org_id: string; name?: string | null }) =>
    request<{ org_id: string }>("/api/orgs", { method: "POST", body }),
  remove: (org_id: string) =>
    request<{ ok: true; org_id: string; deleted: Record<string, number> }>(
      "/api/orgs/delete", { method: "POST", body: { org_id, confirm: org_id } }),
  save: (body: {
    org_id: string; name?: string | null
    map_region?: string | null
    poll_interval_s?: number | null
    web_proxy?: boolean // superadmin-only capability flag
    auto_update?: boolean // fleet auto-update: central arms rollouts itself
  }) => request<{ ok: true }>("/api/org", { method: "POST", body }),
  testAlert: (org_id: string) =>
    request<{
      ok: boolean; detail?: string; channel: string; whatsapp_count: number
    }>("/api/test-alert", { method: "POST", body: { org_id } }),
}

export interface DevicePayload {
  org_id?: string
  name: string
  ip_address: string
  device_type?: string | null
  region?: string | null
  tags?: string[] | null
  parent_device_id?: number | null
  assigned_node_id?: string | null
  gpon_vendor?: string | null
  pon_port?: string | null
  split_ratio?: number | null
  split_inputs?: number | null
  onu_pon_limit?: number | null
}

export const inventoryApi = {
  list: (org?: string | null) =>
    request<{ devices: OrgDevice[]; tag_colors: Record<string, string> }>(`/api/inventory${tq(org)}`),
  setTagColor: (org_id: string, tag: string, color: string | null) =>
    request<{ ok: boolean }>("/api/inventory/tag-color", { method: "POST", body: { org_id, tag, color } }),
  create: (body: DevicePayload) => request<{ id: number }>("/api/inventory", { method: "POST", body }),
  update: (id: number, body: DevicePayload) =>
    request<{ ok: boolean }>("/api/inventory/update", { method: "POST", body: { id, ...body } }),
  remove: (id: number) =>
    request<{ ok: boolean; reason?: string }>("/api/inventory/delete", { method: "POST", body: { id } }),
  setMaintenance: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/maintenance", { method: "POST", body: { id, on } }),
  setTreeDetached: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/tree-detached", { method: "POST", body: { id, on } }),
  setLocation: (id: number, lat: number | null, lng: number | null) =>
    request<{ ok: boolean }>("/api/inventory/location", { method: "POST", body: { id, lat, lng } }),
  placeInField: (body: {
    id: number; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
  }) => request<{ ok: boolean }>("/api/inventory/field-location", { method: "POST", body }),
  createFieldPassive: (body: {
    name: string; device_type: string; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
    split_ratio?: string | null; split_inputs?: number | null
    region?: string | null; pon_port?: string | null
  }) => request<{ id: number }>("/api/inventory/field-passive", { method: "POST", body }),
  locateOnuInField: (body: {
    mac: string; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
    label: string; phone: string
  }) => request<{ ok: boolean }>("/api/inventory/field-onu", { method: "POST", body }),
  nameOnuInField: (body: { mac: string; label: string; phone: string }) =>
    request<{ ok: boolean }>("/api/inventory/field-onu-name", { method: "POST", body }),
  setSnmp: (id: number, body: {
    snmp_enabled: boolean; snmp_community?: string | null; snmp_port?: number | string
  }) => request<{ ok: boolean }>("/api/inventory/snmp", { method: "POST", body: { id, ...body } }),
  setWebAccess: (id: number, body: {
    web_ip: string | null; web_port: number | null; web_scheme: string | null
  }) => request<{ ok: boolean }>("/api/inventory/web-access", { method: "POST", body: { id, ...body } }),
  ports: (deviceId: number) => request<{ ports: SwitchPort[] }>(`/api/inventory/ports?device_id=${deviceId}`),
  perfSamples: (deviceId: number) =>
    request<{ samples: PerfSample[] }>(`/api/inventory/perf/samples?device_id=${deviceId}`),
  perf: (deviceId: number) =>
    request<{ perf: PerfState | null }>(`/api/inventory/perf?device_id=${deviceId}`),
  setPortMonitored: (id: number, on: boolean) =>
    request<{ ok: boolean }>("/api/inventory/ports/monitored", { method: "POST", body: { id, on } }),
  setPortFeeds: (id: number, feeds_device_id: number | null) =>
    request<{ ok: boolean }>("/api/inventory/ports/feeds", { method: "POST", body: { id, feeds_device_id } }),
  setPortUplink: (id: number, uplink_device_id: number | null) =>
    request<{ ok: boolean }>("/api/inventory/ports/uplink", { method: "POST", body: { id, uplink_device_id } }),
  linkPorts: (org?: string | null) =>
    request<{ ports: LinkPort[] }>(`/api/inventory/link-ports${tq(org)}`),
  setPortBandwidth: (
    id: number, threshold_mbps: number | null, direction: string, max_mbps: number | null,
  ) => request<{ ok: boolean }>("/api/inventory/ports/bandwidth", {
    method: "POST", body: { id, threshold_mbps, max_mbps, direction },
  }),
  assignments: (org?: string | null) =>
    request<AssignmentRoster>(`/api/inventory/assignments${tq(org)}`),
  setAssignees: (device_id: number, user_ids: number[]) =>
    request<{ ok: boolean; unreachable: string[] }>("/api/inventory/assign", {
      method: "POST", body: { device_id, user_ids },
    }),
  bulkAssign: (device_ids: number[], user_ids: number[], mode: "add" | "remove") =>
    request<{ ok: boolean; changed: number; unreachable: string[] }>(
      "/api/inventory/assign", { method: "POST", body: { device_ids, user_ids, mode } }),
  routes: (org?: string | null) =>
    request<{ routes: LinkRoute[] }>(`/api/inventory/routes${tq(org)}`),
  setRoute: (child_id: number, parent_id: number, waypoints: Array<[number, number]>) =>
    request<{ ok: boolean }>("/api/inventory/route",
      { method: "POST", body: { child_id, parent_id, waypoints } }),
  setDropRoute: (mac: string, waypoints: Array<[number, number]>, org?: string | null) =>
    request<{ ok: boolean; points: number }>("/api/inventory/drop-route",
      { method: "POST", body: { mac, waypoints, org_id: org ?? undefined } }),
  setLinkStyle: (
    child_id: number, parent_id: number, style: { label_pos?: number | null },
  ) => request<{ ok: boolean }>("/api/inventory/link-style",
    { method: "POST", body: { child_id, parent_id, ...style } }),
  cables: (org: string | null) =>
    request<{ cables: Cable[]; counts: number[] }>(`/api/inventory/cables${tq(org)}`),
  devicePorts: (org: string | null) =>
    request<{ ports: Record<string, TrayPort[]> }>(`/api/inventory/fibre/ports${tq(org)}`),
  saveCable: (
    cable: {
      id?: number; name: string; cores: number | null; notes?: string | null
      a_device_id?: number | null; a_mac?: string | null
      b_device_id?: number | null; b_mac?: string | null
    },
    org: string | null,
  ) => request<{ ok: boolean; id: number }>("/api/inventory/cable",
    { method: "POST", body: { ...cable, org_id: org ?? undefined } }),
  setCableCore: (cable_id: number, core_no: number, label: string | null) =>
    request<{ ok: boolean }>("/api/inventory/cable/core",
      { method: "POST", body: { cable_id, core_no, label } }),
  setCablePath: (cable_id: number, path: Array<[number, number]>) =>
    request<{ ok: boolean }>("/api/inventory/cable/path",
      { method: "POST", body: { cable_id, path } }),
  splitCable: (cable_id: number, lat: number, lng: number, name?: string | null) =>
    request<{
      ok: boolean; cable_id: number; new_cable_id: number
      closure_id: number; spliced: number
    }>("/api/inventory/cable/split",
      { method: "POST", body: { cable_id, lat, lng, name } }),
  deleteCable: (id: number) =>
    request<{ ok: boolean }>("/api/inventory/cable/delete",
      { method: "POST", body: { id } }),
  pointFibre: (point: { device_id?: number | null; mac?: string | null },
               org: string | null) =>
    request<PointFibre>(`/api/inventory/fibre?${
      point.device_id != null ? `device=${point.device_id}`
        : `onu=${encodeURIComponent(point.mac ?? "")}`}${aq(org)}`),
  traceFibre: (cable_id: number, core_no: number, org: string | null) =>
    request<FibreTrace>(
      `/api/inventory/fibre/trace?cable=${cable_id}&core=${core_no}${aq(org)}`),
  setFibreJoint: (joint: {
    device_id?: number | null; mac?: string | null
    a_cable_id: number; a_core_no: number
    b_cable_id?: number | null; b_core_no?: number | null
    port_kind?: string | null; port_no?: number | null
  }) => request<{ ok: boolean; id?: number; refused?: string; reason?: string }>(
    "/api/inventory/fibre/joint", { method: "POST", body: joint }),
  connectPort: (body: {
    device_id?: number | null; mac?: string | null
    port_kind?: string | null; port_no?: number | null
    to_device_id?: number | null; to_mac?: string | null
    to_port_kind?: string | null; to_port_no?: number | null
    org_id?: string | null
  }) => request<{
    ok: boolean; cable_id?: number; name?: string; far_port?: string | null
    refused?: string; reason?: string
  }>("/api/inventory/fibre/connect", { method: "POST", body }),
  takeCoreToBox: (tail: {
    device_id?: number | null; mac?: string | null
    a_cable_id: number; a_core_no: number
    to_device_id?: number | null; to_mac?: string | null
    port_kind?: string | null; port_no?: number | null
  }) => request<{
    ok: boolean; cable_id?: number; name?: string
    refused?: string; reason?: string
  }>("/api/inventory/fibre/tail", { method: "POST", body: tail }),
  spliceThrough: (through: {
    device_id?: number | null; mac?: string | null
    a_cable_id: number; b_cable_id: number
  }) => request<{ ok: boolean; spliced: number; skipped: number; reason?: string }>(
    "/api/inventory/fibre/through", { method: "POST", body: through }),
  clearFibreJoint: (clear: {
    device_id?: number | null; mac?: string | null
    cable_id: number; core_no: number
  }) => request<{ ok: boolean }>("/api/inventory/fibre/clear",
    { method: "POST", body: clear }),
  addBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: true }>("/api/inventory/links", { method: "POST", body: { child_id, parent_id } }),
  removeBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: boolean }>("/api/inventory/links/delete", { method: "POST", body: { child_id, parent_id } }),
  addPeerLink: (a_id: number, b_id: number) =>
    request<{ ok: true }>("/api/inventory/peers", { method: "POST", body: { a_id, b_id } }),
  removePeerLink: (a_id: number, b_id: number) =>
    request<{ ok: boolean }>("/api/inventory/peers/delete", { method: "POST", body: { a_id, b_id } }),

  optics: (deviceId: number) =>
    request<OpticsResponse>(`/api/inventory/optics?device_id=${deviceId}`),
  onuSearch: (org: string | null | undefined, q: string) =>
    request<OnuSearchResponse>(
      `/api/inventory/onu-search?q=${encodeURIComponent(q)}${tq(org).replace(/^\?/, "&")}`),
  onuCoverage: (org: string | null | undefined, deviceId?: number) => {
    const p = new URLSearchParams()
    if (org) p.set("org", org)
    if (deviceId) p.set("device_id", String(deviceId))
    const q = p.toString()
    return request<OnuCoverageResponse>(`/api/inventory/onu-coverage${q ? `?${q}` : ""}`)
  },
  onuPlaces: (org?: string | null) =>
    request<{ places: OnuPlace[] }>(`/api/inventory/onu-places${tq(org)}`),
  subscriber: (mac: string, org?: string | null) =>
    request<Subscriber>(`/api/inventory/subscriber?mac=${encodeURIComponent(mac)}`
      + tq(org).replace(/^\?/, "&")),
  setOnuContact: (body: {
    mac: string; label?: string | null; phone?: string | null
    notes?: string | null; org_id?: string | null
  }) => request<{ ok: boolean }>("/api/inventory/onu-contact", { method: "POST", body }),
  setOnuPlace: (body: {
    mac: string; lat: number | null; lng: number | null
    label?: string | null; phone?: string | null
    notes?: string | null; org_id?: string | null
  }) => request<{ ok: boolean }>("/api/inventory/onu-place", { method: "POST", body }),
  setOnuWitness: (body: { mac: string; witness: boolean; org_id?: string | null }) =>
    request<{ ok: boolean; witness: boolean }>("/api/inventory/onu-witness",
      { method: "POST", body }),
  drops: (org?: string | null) =>
    request<{
      splitters: SplitterLoad[]; faults: BranchFault[]
      recorded: number; unrecorded: number; outlier_db: number
    }>(`/api/inventory/drops${tq(org)}`),
  splitterDrops: (deviceId: number) =>
    request<{ drops: SubscriberDrop[]; load: SplitterLoad | null; outlier_db: number }>(
      `/api/inventory/drops/subscribers?device_id=${deviceId}`),
  setDrops: (body: {
    macs: string[]; passive_id: number | null; org_id?: string | null
    leg_no?: number | null
  }) => request<{ ok: boolean; attached?: number; detached?: number }>(
    "/api/inventory/drops/set", { method: "POST", body }),

  ponFaults: (deviceId: number) =>
    request<{ faults: PonFault[] }>(`/api/pon/faults?device_id=${deviceId}`),
  orgPonFaults: (org?: string | null) =>
    request<{ faults: PonFault[] }>(`/api/pon/faults${tq(org)}`),
  ponSummary: (org?: string | null) =>
    request<PonSummary>(`/api/pon/summary${tq(org)}`),
  incidentShape: (org?: string | null) =>
    request<{ incidents: IncidentShape[] }>(`/api/incident/shape${tq(org)}`),
  ackOnu: (id: number, hours: number | null) =>
    request<{ ok: boolean }>("/api/inventory/optics/ack",
      { method: "POST", body: hours == null ? { id, until: "clear" } : { id, hours } }),
  credentials: (deviceId: number) =>
    request<{ credentials: WebUiCredentials }>(`/api/inventory/credentials?device_id=${deviceId}`),
  setCredentials: (device_id: number, body: {
    username: string; password?: string | null; auth_mode?: "basic" | "form"
  }) => request<{ ok: boolean }>("/api/inventory/credentials",
    { method: "POST", body: { device_id, ...body } }),
  clearCredentials: (device_id: number) =>
    request<{ ok: boolean }>("/api/inventory/credentials/clear",
      { method: "POST", body: { device_id } }),
  setOpticalThresholds: (device_id: number, warn_dbm: number | null, crit_dbm: number | null,
    onu_pon_limit: number | null = null) =>
    request<{ ok: boolean }>("/api/inventory/optics/thresholds",
      { method: "POST", body: { device_id, warn_dbm, crit_dbm, onu_pon_limit } }),
}

export const snmpApi = {
  status: (deviceId: number) =>
    request<SnmpStatusResponse>(`/api/inventory/snmp-status?device_id=${deviceId}`),
  setCapability: (body: {
    device_id: number; subsystem: SnmpSubsystem; supported: boolean; note?: string | null
  }) => request<{ ok: boolean }>("/api/inventory/capability", { method: "POST", body }),
  walks: (deviceId: number) =>
    request<{ walks: SnmpWalk[] }>(`/api/inventory/snmp-walks?device_id=${deviceId}`),
  walkResult: (id: number) =>
    request<{ walk: SnmpWalkResult | null }>(`/api/inventory/snmp-walk/result?id=${id}`),
  startWalk: (device_id: number, root_oid: string, max_varbinds?: number) =>
    request<{ id: number }>("/api/inventory/snmp-walk",
      { method: "POST", body: { device_id, root_oid, max_varbinds } }),
  profiles: (org?: string | null) => request<SnmpProfilesResponse>(`/api/snmp-profiles${tq(org)}`),
  createProfile: (body: {
    org_id?: string; name: string; match_sysobjectid: string
    metrics: Record<string, { oid: string; decode: string; select: string }>
    enabled: boolean
  }) => request<{ id: number }>("/api/snmp-profiles", { method: "POST", body }),
  updateProfile: (id: number, body: {
    name: string; match_sysobjectid: string
    metrics: Record<string, { oid: string; decode: string; select: string }>
    enabled: boolean
  }) => request<{ ok: boolean }>("/api/snmp-profiles/update", { method: "POST", body: { id, ...body } }),
  removeProfile: (id: number) =>
    request<{ ok: boolean }>("/api/snmp-profiles/delete", { method: "POST", body: { id } }),
}

export interface GponProfilePayload {
  org_id?: string
  name: string
  match_sysobjectid: string
  oids: Record<string, string>
  scales: Record<string, number>
  state_map: Record<string, string>
  state_default: string
  pon_index: string
  pon_label: string
  enabled: boolean
}

export const gponApi = {
  profiles: (org?: string | null) => request<GponProfilesResponse>(`/api/gpon-profiles${tq(org)}`),
  createProfile: (body: GponProfilePayload) =>
    request<{ id: number }>("/api/gpon-profiles", { method: "POST", body }),
  updateProfile: (id: number, body: GponProfilePayload) =>
    request<{ ok: boolean }>("/api/gpon-profiles/update", { method: "POST", body: { id, ...body } }),
  removeProfile: (id: number) =>
    request<{ ok: boolean }>("/api/gpon-profiles/delete", { method: "POST", body: { id } }),
}

export interface WebOpticsProfilePayload extends WebOpticsProfileSpec {
  org_id?: string
  name: string
  enabled: boolean
}

export const webOpticsApi = {
  profiles: (org?: string | null) =>
    request<WebOpticsProfilesResponse>(`/api/web-optics-profiles${tq(org)}`),
  createProfile: (body: WebOpticsProfilePayload) =>
    request<{ id: number }>("/api/web-optics-profiles", { method: "POST", body }),
  updateProfile: (id: number, body: WebOpticsProfilePayload) =>
    request<{ ok: boolean }>("/api/web-optics-profiles/update",
      { method: "POST", body: { id, ...body } }),
  removeProfile: (id: number) =>
    request<{ ok: boolean }>("/api/web-optics-profiles/delete",
      { method: "POST", body: { id } }),
  rxStatus: (deviceId: number) =>
    request<RxStatusResponse>(`/api/inventory/rx-status?device_id=${deviceId}`),
  refresh: (deviceId: number) =>
    request<{ started: boolean }>("/api/inventory/rx-refresh",
      { method: "POST", body: { device_id: deviceId } }),
}

export const proxyApi = {
  open: (device_id: number, port: number) =>
    request<{ sid: string; url: string; device_id: number; expires_at: number }>(
      "/api/proxy/session", { method: "POST", body: { device_id, port } }),
  sessions: (org?: string | null) =>
    request<{ sessions: ProxySession[] }>(`/api/proxy/sessions${tq(org)}`),
  audit: (org: string | null | undefined, limit = 100) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    params.set("limit", String(limit))
    return request<{ audit: ProxyAudit[] }>(`/api/proxy/audit?${params.toString()}`)
  },
  close: (sid: string) =>
    request<{ ok: true; was_open: boolean }>("/api/proxy/close", { method: "POST", body: { sid } }),
}

export const analyticsApi = {
  trend: (deviceId: number, days = 1) =>
    request<{ since: string; until: string; buckets: TrendBucket[] }>(
      `/api/analytics/trend?device_id=${deviceId}&days=${days}`),
  reliability: (org: string | null | undefined, days = 7) =>
    request<{ since: string; until: string; devices: ReliabilityRow[] }>(
      `/api/analytics?days=${days}${org ? `&org=${encodeURIComponent(org)}` : ""}`),
}

export const issuesApi = {
  list: (org: string | null | undefined, kinds: IssueKind[] = []) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    if (kinds.length) params.set("kind", kinds.join(","))
    return request<IssuesResponse>(`/api/issues?${params.toString()}`)
  },
  download: async (format: "pdf" | "xlsx", org: string | null | undefined,
                   kinds: IssueKind[] = []) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    if (kinds.length) params.set("kind", kinds.join(","))
    const res = await fetch(`/api/issues/${format}?${params.toString()}`)
    if (res.status === 401) window.dispatchEvent(new CustomEvent("wisp:unauthorized"))
    if (!res.ok) {
      const data = res.headers.get("content-type")?.includes("json")
        ? await res.json() : {}
      throw new ApiError(data.error || `HTTP ${res.status}`, res.status, data)
    }
    const disp = res.headers.get("Content-Disposition") || ""
    const named = /filename="([^"]+)"/.exec(disp)?.[1]
    return { blob: await res.blob(), filename: named || `issues.${format}` }
  },
}

export const outagesApi = {
  list: (org?: string | null) => request<{ outages: Outage[] }>(`/api/outages${tq(org)}`),
  acknowledge: (outage_id: number) =>
    request<{ ok: boolean }>("/api/outages/acknowledge", { method: "POST", body: { outage_id } }),
  assign: (outage_id: number, usernames: string[]) =>
    request<{ ok: true; assigned_to: string[]; notified: number }>(
      "/api/outages/assign", { method: "POST", body: { outage_id, usernames } }),
  accept: (outage_id: number) =>
    request<{ ok: true; already: boolean; accepted_by: string[] }>(
      "/api/outages/accept", { method: "POST", body: { outage_id } }),
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
  restart: (org_id: string, node_id: string) =>
    request<{ ok: boolean }>("/api/nodes/restart", { method: "POST", body: { org_id, node_id } }),
  setColor: (org_id: string, node_id: string, color: string | null) =>
    request<{ ok: boolean }>("/api/nodes/color", { method: "POST", body: { org_id, node_id, color } }),
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

  setWhatsapp: (whatsapp_number: string, id?: number) =>
    request<{ ok: true; whatsapp_number: string | null }>(
      "/api/users/whatsapp", { method: "POST", body: { id, whatsapp_number } }),

  totpStart: () => request<{ secret: string; otpauth_uri: string }>(
    "/api/users/totp/start", { method: "POST", body: {} }),
  totpConfirm: (body: { password: string; code: string }) =>
    request<{ ok: true; recovery_codes: string[] }>(
      "/api/users/totp/confirm", { method: "POST", body }),
  totpDisable: (password: string) =>
    request<{ ok: true }>("/api/users/totp/disable", { method: "POST", body: { password } }),
  totpRegenerate: (body: { password: string; code: string }) =>
    request<{ ok: true; recovery_codes: string[] }>(
      "/api/users/totp/recovery", { method: "POST", body }),
}

export const fieldApi = {
  shift: () => request<ShiftState>("/api/field/shift"),
  setShift: (action: "start" | "end") =>
    request<{ ok: true; on_shift: boolean; started_at?: string; already: boolean }>(
      "/api/field/shift", { method: "POST", body: { action } }),

  workers: (org?: string | null) =>
    request<FieldWorkersResponse>(`/api/field/workers${tq(org)}`),

  tokens: (org?: string | null) =>
    request<FieldTokensResponse>(`/api/field/tokens${tq(org)}`),
  issueToken: (user_id: number, org_id?: string | null) =>
    request<{ ok: true; user_id: number; token: string; server_url: string }>(
      "/api/field/token", { method: "POST", body: { user_id, org_id } }),
  revokeToken: (user_id: number, org_id?: string | null) =>
    request<{ ok: boolean }>(
      "/api/field/token/revoke", { method: "POST", body: { user_id, org_id } }),
}
