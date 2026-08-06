import type {
  AccountUser, AdminOverview, AssignmentRoster, BillingInfo, GponProfilesResponse, IncidentShape, IssueKind, IssuesResponse, LinkPort, LinkRoute, LogEvent, MeResponse, NodesResponse, Org, OrgDevice,
  OnuCoverageResponse, OnuSearchResponse, OrgRegion, Outage, PerfSample, PerfState, Plan, OpticsResponse, ProxyAudit, ProxySession, ReliabilityRow, Role,
  OnuPlace, PonFault, PonSummary, SnmpProfilesResponse, SnmpStatusResponse, SnmpSubsystem, SnmpWalk, SnmpWalkResult,
  BranchFault, SplitterLoad, Subscriber, SubscriberDrop,
  Summary, SwitchPort, SystemStats, TrendBucket, WebUiCredentials,
  RxStatusResponse, WebOpticsProfileSpec, WebOpticsProfilesResponse, WhatsappSettings,
  FieldTokensResponse, FieldWorkersResponse, ShiftState,
} from "./types"
import type { ThemeOverrides } from "./theme-tokens"
import type { MapDetail } from "@/map/detail"

export class ApiError extends Error {
  status: number
  // The parsed JSON body, so callers can read fields beyond the message —
  // e.g. the login flow reads `body.totp_required` off a 401.
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
    // paywall lock hit mid-session (month rolled over unpaid) — the app shell
    // listens and re-checks /api/billing, which flips it to the lock screen
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
  // server-wide settings, superadmin-only; the Google key applies to every org
  settings: () => request<{
    google_maps_key: string | null
    billing_gpay_number: string
    billing_qr_image: string | null
    // WhatsApp channel config (token never echoed — token_set only)
    whatsapp: WhatsappSettings
    // sparse colour diff over the shipped palette, per theme mode; `{}` is a
    // stock theme. See lib/theme-tokens.ts and central/theme.py.
    theme_overrides: ThemeOverrides
    // EFFECTIVE map zoom floors (defaults filled in, unlike the sparse theme
    // diff above) — one setting for every org. See central/mapdetail.py.
    map_detail: MapDetail
  }>("/api/admin/settings"),
  saveSettings: (body: {
    google_maps_key?: string | null
    billing_gpay_number?: string | null
    billing_qr_image?: string | null
    // WhatsApp config. `token` write-only: omit to leave the stored one alone,
    // send a value to set it, or `token_clear: true` to remove it. `admin_number`
    // is the superadmin ops recipient (org 'I've paid' / churn / release-sync).
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
    // omit to leave colours alone; `{}` resets every org to the shipped palette
    theme_overrides?: ThemeOverrides
    // omit to leave map density alone; posting the shipped defaults IS the reset
    // (central clears the row rather than storing a copy of them)
    map_detail?: MapDetail
  }) =>
    request<{ ok: true }>("/api/admin/settings", { method: "POST", body }),
}

export const billingApi = {
  get: (org?: string | null) => request<BillingInfo>(`/api/billing${tq(org)}`),
  // superadmin: set the plan and/or toggle one paid month
  adminSave: (body: { org_id: string; plan?: Plan; month?: string; paid?: boolean }) =>
    request<{ ok: true } & BillingInfo>("/api/admin/billing", { method: "POST", body }),
  // "I've paid": pings the admin's payments channel with the org name so they
  // verify and mark the month. Reachable while locked — the lock-screen tap.
  markPaid: (org?: string | null) =>
    request<{ ok: true; notified: boolean }>("/api/billing/paid", { method: "POST", body: { org_id: org } }),
  // self-serve, no payment: only "free" is accepted (paid plans are entered
  // by paying the admin); reachable while locked — the escape hatch
  setPlan: (body: { org_id?: string | null; plan: Plan }) =>
    request<{ ok: true } & BillingInfo>("/api/billing/plan", { method: "POST", body }),
}

export const orgsApi = {
  list: (org?: string | null) => request<{ orgs: Org[] }>(`/api/orgs${tq(org)}`),
  create: (body: { org_id: string; name?: string | null }) =>
    request<{ org_id: string }>("/api/orgs", { method: "POST", body }),
  // irreversible: `confirm` must echo the org id — the server enforces it too
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
  /** passive plant only: 2 | 4 | 8 (see SPLIT_RATIOS), null = not recorded */
  split_ratio?: number | null
  /** OLT only: ONUs per PON before "at capacity" (EPON 64 / GPON 128),
      null = the server's global cap */
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
  // The field-survey pair — the ONLY inventory writes a worker session may make.
  // Separate from setLocation because they are different operations, not the
  // same one with a wider audience: neither can clear a pin, and the passive
  // create carries no parent, IP or probe (see server.py:_WORKER_POST).
  placeInField: (body: {
    id: number; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
  }) => request<{ ok: boolean }>("/api/inventory/field-location", { method: "POST", body }),
  createFieldPassive: (body: {
    name: string; device_type: string; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
    split_ratio?: string | null; region?: string | null; pon_port?: string | null
  }) => request<{ id: number }>("/api/inventory/field-passive", { method: "POST", body }),
  // Locating a subscriber's ONU from the field. Deliberately carries no
  // `witness` key: placing a REFERENCE ONU is the operator's claim about a power
  // supply and flips PON mass-drop verdicts, so it stays on setOnuPlace and
  // owner-only. This one records where the box is, and nothing more.
  // `label` and `phone` are REQUIRED by the server, not merely accepted: a
  // survey row a crew can't act on isn't worth the pin. Typed optional-free.
  locateOnuInField: (body: {
    mac: string; lat: number; lng: number
    accuracy_m: number | null; source: "gps" | "manual"
    label: string; phone: string
  }) => request<{ ok: boolean }>("/api/inventory/field-onu", { method: "POST", body }),
  // Contact details only. Writes `onu_places.label`/`.phone` — NOT the roster's
  // name, which the SNMP walk rewrites every sweep. Separate from the placement
  // call so fixing a spelling can't restamp the pin's accuracy or reattribute
  // who placed it.
  nameOnuInField: (body: { mac: string; label: string; phone: string }) =>
    request<{ ok: boolean }>("/api/inventory/field-onu-name", { method: "POST", body }),
  setSnmp: (id: number, body: {
    snmp_enabled: boolean; snmp_community?: string | null; snmp_port?: number | string
  }) => request<{ ok: boolean }>("/api/inventory/snmp", { method: "POST", body: { id, ...body } }),
  // Web-UI proxy address override (owner-only). Blank/null fields clear that part;
  // all blank clears the override (back to the probe IP on 80/443).
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
  // every port bound to a link (either side), org-wide — the map's bandwidth labels
  linkPorts: (org?: string | null) =>
    request<{ ports: LinkPort[] }>(`/api/inventory/link-ports${tq(org)}`),
  setPortBandwidth: (
    id: number, threshold_mbps: number | null, direction: string, max_mbps: number | null,
  ) => request<{ ok: boolean }>("/api/inventory/ports/bandwidth", {
    method: "POST", body: { id, threshold_mbps, max_mbps, direction },
  }),
  // ----- paging responsibility ------------------------------------------------
  // Who gets paged about a device. NOT a permission: every account still sees the
  // whole fleet, so nothing here belongs in a read path.
  assignments: (org?: string | null) =>
    request<AssignmentRoster>(`/api/inventory/assignments${tq(org)}`),
  /** REPLACE one device's assignees. An empty list is meaningful — it hands the
      device back to "every worker gets paged", the safe default. */
  setAssignees: (device_id: number, user_ids: number[]) =>
    request<{ ok: boolean; unreachable: string[] }>("/api/inventory/assign", {
      method: "POST", body: { device_id, user_ids },
    }),
  /** ADD or REMOVE accounts across many devices, leaving other assignees on those
      devices alone — handing over a region must not strip whoever else was on it. */
  bulkAssign: (device_ids: number[], user_ids: number[], mode: "add" | "remove") =>
    request<{ ok: boolean; changed: number; unreachable: string[] }>(
      "/api/inventory/assign", { method: "POST", body: { device_ids, user_ids, mode } }),
  routes: (org?: string | null) =>
    request<{ routes: LinkRoute[] }>(`/api/inventory/routes${tq(org)}`),
  setRoute: (child_id: number, parent_id: number, waypoints: Array<[number, number]>) =>
    request<{ ok: boolean }>("/api/inventory/route",
      { method: "POST", body: { child_id, parent_id, waypoints } }),
  // A link's map styling. SPARSE on purpose — omit a key to leave it alone, so
  // dragging a label can't clear a colour and vice versa.
  setLinkStyle: (
    child_id: number, parent_id: number,
    style: { color?: string | null; label_pos?: number | null },
  ) => request<{ ok: boolean }>("/api/inventory/link-style",
    { method: "POST", body: { child_id, parent_id, ...style } }),
  addBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: true }>("/api/inventory/links", { method: "POST", body: { child_id, parent_id } }),
  removeBackupLink: (child_id: number, parent_id: number) =>
    request<{ ok: boolean }>("/api/inventory/links/delete", { method: "POST", body: { child_id, parent_id } }),
  // switch-to-switch cross-link; undirected, so either order works
  addPeerLink: (a_id: number, b_id: number) =>
    request<{ ok: true }>("/api/inventory/peers", { method: "POST", body: { a_id, b_id } }),
  removePeerLink: (a_id: number, b_id: number) =>
    request<{ ok: boolean }>("/api/inventory/peers/delete", { method: "POST", body: { a_id, b_id } }),

  optics: (deviceId: number) =>
    request<OpticsResponse>(`/api/inventory/optics?device_id=${deviceId}`),
  // ONU lookup (serial/MAC or provisioned name) for the Network search box.
  // Punctuation-blind server side, so the raw needle goes over as typed.
  onuSearch: (org: string | null | undefined, q: string) =>
    request<OnuSearchResponse>(
      `/api/inventory/onu-search?q=${encodeURIComponent(q)}${tq(org).replace(/^\?/, "&")}`),
  // Reference ONUs: the subscribers an operator has vouched for as reliably
  // powered, placed on the map. Keyed on the MAC (identity), so a re-homed drop
  // keeps its point. Passing lat/lng null CLEARS it — the table is sparse.
  // Survey coverage. Without a device_id it is counts only (cheap); with one it
  // also returns that OLT's unplaced rows — the list a tech works down.
  onuCoverage: (org: string | null | undefined, deviceId?: number) => {
    const p = new URLSearchParams()
    if (org) p.set("org", org)
    if (deviceId) p.set("device_id", String(deviceId))
    const q = p.toString()
    return request<OnuCoverageResponse>(`/api/inventory/onu-coverage${q ? `?${q}` : ""}`)
  },
  onuPlaces: (org?: string | null) =>
    request<{ places: OnuPlace[] }>(`/api/inventory/onu-places${tq(org)}`),
  /** ONE subscriber, whole — the object `subscriber-detail.tsx` renders. Keyed
   *  on the sticker MAC, which is the only identity that survives a re-homed
   *  drop (a slot key rots: `onu_optics` never deletes a vacated one). */
  subscriber: (mac: string, org?: string | null) =>
    request<Subscriber>(`/api/inventory/subscriber?mac=${encodeURIComponent(mac)}`
      + tq(org).replace(/^\?/, "&")),
  /** Record who a subscriber is from the desk — no coordinate involved.
   *
   *  Deliberately NOT `setOnuPlace` with null coordinates: that call's meaning is
   *  the map pin (and, when it clears one, the retraction of a power claim).
   *  This one only ever touches what the operator typed, which is why an ISP can
   *  finally name the 2,150 subscribers nobody has stood at. A blank field is
   *  written as NULL rather than skipped — the form SHOWS what is stored, so
   *  emptying one is deliberate. */
  setOnuContact: (body: {
    mac: string; label?: string | null; phone?: string | null
    notes?: string | null; org_id?: string | null
  }) => request<{ ok: boolean }>("/api/inventory/onu-contact", { method: "POST", body }),
  /** Place, move or clear a subscriber's pin. A LOCATION and nothing else.
   *
   *  **There is deliberately no `witness` key** — the server payload cannot
   *  spell one either. This route used to force the claim TRUE, so moving a
   *  surveyed pin or reopening the dialog to add a phone number silently
   *  promoted an ordinary customer to a power-backed witness, and a dark witness
   *  makes ponfault call a fibre cut and roll a crew. The claim has exactly one
   *  verb, `setOnuWitness`. */
  setOnuPlace: (body: {
    mac: string; lat: number | null; lng: number | null
    label?: string | null; phone?: string | null
    notes?: string | null; org_id?: string | null
  }) => request<{ ok: boolean }>("/api/inventory/onu-place", { method: "POST", body }),
  /** The power-supply claim ALONE — no coordinate moves, no provenance is
   *  restamped. What makes "on the map but not vouched for" expressible, which
   *  is the state a surveyed fleet is mostly in. 404s on a subscriber nobody has
   *  recorded yet. */
  setOnuWitness: (body: { mac: string; witness: boolean; org_id?: string | null }) =>
    request<{ ok: boolean; witness: boolean }>("/api/inventory/onu-witness",
      { method: "POST", body }),
  // Subscriber drops: which passive box each ONU hangs off. Map-only like
  // `routes` — every page lists devices, only the map and a splitter's own
  // panel need to know what is behind each box.
  drops: (org?: string | null) =>
    request<{
      splitters: SplitterLoad[]; faults: BranchFault[]
      recorded: number; unrecorded: number; outlier_db: number
    }>(`/api/inventory/drops${tq(org)}`),
  splitterDrops: (deviceId: number) =>
    request<{ drops: SubscriberDrop[]; load: SplitterLoad | null; outlier_db: number }>(
      `/api/inventory/drops/subscribers?device_id=${deviceId}`),
  // Bulk by design: the question is "which customers hang off this splitter",
  // asked once per box. `passive_id: null` DETACHES the listed MACs.
  setDrops: (body: {
    macs: string[]; passive_id: number | null; org_id?: string | null
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
  // Per-device web-UI login (owner-only). `password`: omit/null to leave a
  // stored one untouched, "" to clear it, a string to set it.
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

// Web-UI optics vendor recipes: the OLTs whose per-ONU Rx exists in no SNMP OID
// and can only be read off the box's own page. A profile is what turns
// onboarding one of those from a central deploy into a dashboard row.
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
  // Why this OLT shows no dBm. Read-side: rendering the diagnosis must never
  // poke the OLT — the scrape stays on its own slow clock.
  rxStatus: (deviceId: number) =>
    request<RxStatusResponse>(`/api/inventory/rx-status?device_id=${deviceId}`),
  // Read this OLT's optical page NOW rather than at the next sweep — for the
  // hour someone is at the pole with the fibre in their hand. Answers at once;
  // the read runs server-side and lands in the scrape status the panel already
  // watches, so there is one story about what happened either way.
  refresh: (deviceId: number) =>
    request<{ started: boolean }>("/api/inventory/rx-refresh",
      { method: "POST", body: { device_id: deviceId } }),
}

// Device web-UI proxy tunnel (webplan.md M3). A session is opened against one
// device; the browser then drives the device's own UI at the returned url.
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

// The ISSUE plane: the same trouble the Home tiles count, listed one row per
// problem instead of one row per device.
export const issuesApi = {
  list: (org: string | null | undefined, kinds: IssueKind[] = []) => {
    const params = new URLSearchParams()
    if (org) params.set("org", org)
    if (kinds.length) params.set("kind", kinds.join(","))
    return request<IssuesResponse>(`/api/issues?${params.toString()}`)
  },
  // Server-rendered PDF (central/pdf.py) or .xlsx (central/xlsx.py) — both pure
  // stdlib, both filtered by the same `kinds`. Fetched rather than linked so a 401
  // still runs through the unauthorized handling instead of navigating the tab to
  // a JSON error, and so the caller can show a real toast.
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
  // Owner-only. Replaces the whole assignee set; the server refuses an empty
  // list (there is no "assigned to nobody" state) and pages exactly the named
  // accounts, reporting how many actually had a WhatsApp number.
  assign: (outage_id: number, usernames: string[]) =>
    request<{ ok: true; assigned_to: string[]; notified: number }>(
      "/api/outages/assign", { method: "POST", body: { outage_id, usernames } }),
  // The assignee answering yes. Any named account may call it (worker included);
  // the server refuses anyone who isn't on the outage. Idempotent — `already`
  // means this person had accepted before, which is not an error (the WhatsApp
  // button and this one press the same thing).
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

  // Set/clear an account's WhatsApp page number (blank clears). Omit `id` to set
  // your own — self-service, so a worker can add it too.
  setWhatsapp: (whatsapp_number: string, id?: number) =>
    request<{ ok: true; whatsapp_number: string | null }>(
      "/api/users/whatsapp", { method: "POST", body: { id, whatsapp_number } }),

  // TOTP second factor (self-service, owner/superadmin). start → confirm turns it
  // on and returns the one-time recovery codes; disable/regenerate need the
  // password (regenerate also needs a live code).
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

// Worker location tracking. The INGEST is not here — that is the tracker app
// POSTing to the public /field/track, with no cookie and no SPA involved.
export const fieldApi = {
  /** the caller's OWN shift (worker-readable) */
  shift: () => request<ShiftState>("/api/field/shift"),
  setShift: (action: "start" | "end") =>
    request<{ ok: true; on_shift: boolean; started_at?: string; already: boolean }>(
      "/api/field/shift", { method: "POST", body: { action } }),

  /** where the crew is (owner-only) */
  workers: (org?: string | null) =>
    request<FieldWorkersResponse>(`/api/field/workers${tq(org)}`),

  /** tracker credentials (owner-only). `issueToken` returns the plaintext ONCE. */
  tokens: (org?: string | null) =>
    request<FieldTokensResponse>(`/api/field/tokens${tq(org)}`),
  issueToken: (user_id: number, org_id?: string | null) =>
    request<{ ok: true; user_id: number; token: string; server_url: string }>(
      "/api/field/token", { method: "POST", body: { user_id, org_id } }),
  revokeToken: (user_id: number, org_id?: string | null) =>
    request<{ ok: boolean }>(
      "/api/field/token/revoke", { method: "POST", body: { user_id, org_id } }),
}
