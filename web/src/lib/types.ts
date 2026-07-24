export type Role = "owner" | "worker"

export interface User {
  id: number
  username: string
  org_id: string | null
  org_name: string | null
  role: Role
  // experimental WhatsApp channel: this account's own page number (E.164)
  whatsapp_number: string | null
  // TOTP second factor active on this account (owner/superadmin only)
  totp_enabled: boolean
  is_superadmin: boolean
}

export interface MeResponse {
  user: User
  channels: { central: string | null }
}

export interface Org {
  org_id: string
  name: string | null
  ntfy_topic: string | null
  ntfy_topic_owner: string | null
  ntfy_topic_worker: string | null
  map_region: string | null
  // the superadmin's server-wide Map Tiles key, injected into every org row
  google_maps_key: string | null
  // dashboard-set probe cadence for this org's edges; null = automatic
  poll_interval_s: number | null
  // paywall tier (central/billing.py PLANS) — superadmin-set only
  plan: Plan
  // device web-UI proxy capability (webplan.md §6.7) — superadmin-set only
  web_proxy: number
  node_count: number
  // stated on the delete confirmation — an org wipe has no undo
  device_count: number
  user_count: number
}

/** One web-UI proxy tunnel session (GET /api/proxy/sessions). `live` means the
    in-memory hub still holds it — an 'open' DB row after a central restart is
    a zombie and reports live: false. */
export interface ProxySession {
  sid: string
  org_id: string
  device_id: number
  node_id: string
  created_by: number | null
  created_at: string
  expires_at: string
  status: "open" | "closed" | "expired"
  last_active_at: string | null
  device_name: string | null
  live: boolean
}

/** One proxied browser request (GET /api/proxy/audit, owner-only). */
export interface ProxyAudit {
  id: number
  sid: string
  device_id: number
  user_id: number | null
  method: string
  path: string
  status: number | null
  ts: string
  device_name: string | null
}

/** A device's stored web-UI login (GET /api/inventory/credentials, owner-only).
    The password itself never crosses the wire — only whether one is set. */
export interface WebUiCredentials {
  username: string
  has_password: boolean
  /** basic = central injects Authorization: Basic into the proxy fetch (no
      popup, password never reaches the browser); form = login-form device. */
  auth_mode: "basic" | "form"
  updated_by: string | null
  updated_at: string | null
}

export type Plan = "free" | "pro" | "vip"
/** free = no billing; due_soon = ≤3 days of paid runway; locked = current
    month unpaid → the server 402s everything but /api/me + /api/billing. */
export type BillingStatus = "free" | "active" | "due_soon" | "locked"

export interface PlanSpec {
  label: string
  price_inr: number
  device_cap: number | null // null = unlimited
  node_cap: number | null // edge probes; null = unlimited
  features: string[]
}

/** GET /api/billing — plan + payment verdict for one org. Months are
    'YYYY-MM' UTC keys; readable even while locked (the lock screen renders
    from this). */
export interface BillingInfo {
  plan: Plan
  status: BillingStatus
  locked: boolean
  current_month: string
  paid_through: string | null
  due_month: string | null
  days_left: number | null
  paid_months: string[]
  device_count: number
  device_cap: number | null
  /** registered, un-revoked edge-probe credentials */
  node_count: number
  node_cap: number | null
  gpay_number: string
  /** optional payment QR (a data URI) the admin uploaded; null = none, show
      just the GPay number */
  qr_image: string | null
  plans: Record<Plan, PlanSpec>
}

export const DEVICE_TYPES = [
  "core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul",
] as const
/** Passive plant: on the map and in the tree, never probed — no IP, no FSM. */
export const PASSIVE_DEVICE_TYPES = ["splitter", "fdb", "closure"] as const
export type DeviceType =
  (typeof DEVICE_TYPES)[number] | (typeof PASSIVE_DEVICE_TYPES)[number]
export const isPassiveType = (t: string | null | undefined): boolean =>
  !!t && (PASSIVE_DEVICE_TYPES as readonly string[]).includes(t)

export type DeviceState = "UP" | "DOWN" | "DEGRADED" | "UNREACHABLE"

/** PON mass-drop verdict (central/ponfault.py) — power vs fiber, with a cut
    distance interval off EPON ranging when it's fiber. Read-side, never pages. */
export interface PonFault {
  device_id: number
  device_name: string
  pon_port: string | null
  onus_total: number
  dark: number
  dying_gasp: number
  since: string | null
  kind: "power" | "fiber"
  cut_low_m: number | null
  cut_high_m: number | null
  /** named passive (splitter/FDB) whose route distance sits in the cut interval */
  suspect: string | null
}

/** Org-wide optical/PON rollup for the dashboard KPI strip
    (GET /api/pon/summary). Read-side, never pages. */
export interface PonSummary {
  olts: number
  onus_total: number
  onus_online: number
  onus_offline: number
  fiber_cuts: number
  /** PONs at or over their ONU cap (per-OLT override → default) */
  pons_over_cap: number
  /** OLTs carrying at least one over-cap PON — for the KPI tile's drill-down */
  over_cap_device_ids: number[]
  /** the org default ONU-per-PON cap (per-OLT overrides not reflected here) */
  pon_cap: number
  /** ONU count on the busiest over-cap PON, 0 when none */
  pon_cap_worst: number
  /** MACs with ≥2 slots ONLINE at once — cloned CPE or a bridging loop */
  dup_macs_live: number
  /** every MAC on ≥2 slots, live or reg-table history */
  dup_macs_total: number
  /** ONLINE ONUs below the critical Rx floor — a subscriber about to lose sync */
  onus_crit: number
  /** ONLINE ONUs in the warning Rx band */
  onus_warn: number
  /** roster slots carrying a real Rx figure. 0 with a non-zero onus_total means
      NOTHING is measured on this fleet, which is a very different statement
      from "0 critical" — the tiles have to be able to tell them apart. */
  onus_rx: number
  /** OLTs contributing at least one Rx reading */
  olts_rx: number
}

/** Open-outage wave verdict (central/incidents.py): topology × geography.
    Annotation only — it never mutes or reroutes a page. */
export interface IncidentShape {
  kind: "power" | "upstream"
  device_ids: number[]
  count: number
  branches: number
  since: string | null
  center: [number, number] | null
  radius_km: number | null
  root_name: string | null
}

/** Map presentation for one link: the drawn cable path (intermediate vertices
    only, parent→child order) plus the operator's cartography — a palette colour
    and where the bandwidth chip sits along the line. */
export interface LinkRoute {
  child_id: number
  parent_id: number
  waypoints: Array<[number, number]>
  /** LinkColor name; null = the line's own tone */
  color: string | null
  /** 0..1 along the rendered path; null = midpoint */
  label_pos: number | null
  updated_at: string
  updated_by: string | null
}

export interface OrgRegion {
  name: string
  declared: boolean
  device_count: number
}

export interface OrgDevice {
  id: number
  org_id: string
  name: string
  ip_address: string
  device_type: DeviceType | null
  region: string | null
  /** free-form labels for Network-page filtering (≤8, cosmetic only) */
  tags: string[]
  parent_device_id: number | null
  /** tree presentation only: render at the top level, not inside the parent's
   *  subtree. The parent link stays real everywhere else (map, suppression). */
  tree_detached: 0 | 1
  assigned_node_id: string | null
  maintenance: 0 | 1
  snmp_enabled: 0 | 1
  snmp_version: string
  snmp_community: string | null
  snmp_port: number

  gpon_vendor: string | null
  /** passive plant only: which PON this splitter/FDB serves (e.g. "0/6") */
  pon_port: string | null
  /** web-UI proxy address override: where the admin page actually lives when
      it isn't at ip_address:80/443 (port-forwarding / a separate mgmt IP).
      Any set = "Open web UI" targets (web_ip||ip_address):(web_port||default)
      over web_scheme; all null = classic behavior. */
  web_ip: string | null
  web_port: number | null
  web_scheme: string | null
  lat: number | null
  lng: number | null
  child_count: number
  backup_parents: number[]
  /** switch-to-switch cross-links (undirected, no dependency). Stored once per
      cable and expanded symmetrically server-side, so BOTH ends list each other. */
  peer_ids: number[]

  ports_down: number
  ports_bw_low: number
  ports_bw_high: number

  onus_total: number | null
  onus_online: number | null
  onus_warn: number | null
  onus_crit: number | null
  /** roster slots on this OLT carrying a real per-ONU Rx figure. The optics
      badge alone can't answer "is dBm working here": a C-Data/DBC OLT walks a
      full roster with every rx_dbm NULL, so optics reads green on a box that
      reports no optical power at all. */
  onus_rx: number | null
  /** suspected fiber-cut PON mass-drops on this OLT (row chip → Optical tab) */
  fiber_cuts: number
  /** live duplicate-MAC groups touching this OLT (≥2 slots online at once) */
  dup_macs: number
  optics_updated_at: string | null
  ports_updated_at: string | null
  /** started_at of the still-open outage, if any — "down for 43m" on the map */
  outage_started_at: string | null

  state: DeviceState | null
  latency_ms: number | null
  packet_loss: number | null
  jitter_ms: number | null
  state_updated_at: string | null

  health_cpu_pct: number | null
  health_mem_pct: number | null
  health_mem_used_bytes: number | null
  health_mem_total_bytes: number | null
  health_temp_c: number | null
  health_updated_at: string | null
}

export interface SwitchPort {
  id: number
  org_id: string
  device_id: number
  if_index: number
  if_name: string | null
  if_alias: string | null
  admin_status: string | null
  oper_status: string | null
  last_change: string | null
  monitored: 0 | 1
  feeds_device_id: number | null
  down_streak: number
  alarm: 0 | 1
  alarm_since: string | null
  updated_at: string | null
  bw_threshold_mbps: number | null
  bw_max_mbps: number | null
  bw_direction: "in" | "out" | "either" | "total" | null
  in_bps: number | null
  out_bps: number | null
  bw_low_streak: number
  bw_alarm: 0 | 1
  bw_alarm_since: string | null
  bw_high_streak: number
  bw_high_alarm: 0 | 1
  bw_high_alarm_since: string | null
  /** operator-declared cabling, child side: this port faces that parent device
      (primary or backup uplink) — the mirror of feeds_device_id */
  uplink_device_id: number | null
}

/** One side of a physical link (`/api/inventory/link-ports`): a switch_ports row
    bound to a link either as the parent's downstream port (feeds_device_id names
    the child) or the child's uplink port (uplink_device_id names the parent).
    Feeds the map's per-link bandwidth labels in one org-wide query. */
export interface LinkPort {
  id: number
  device_id: number
  if_index: number
  if_name: string | null
  if_alias: string | null
  admin_status: string | null
  oper_status: string | null
  monitored: 0 | 1
  alarm: 0 | 1
  bw_alarm: 0 | 1
  bw_high_alarm: 0 | 1
  in_bps: number | null
  out_bps: number | null
  updated_at: string | null
  feeds_device_id: number | null
  uplink_device_id: number | null
}

export interface PerfSample {
  ts: string
  latency_ms: number | null
  packet_loss: number | null
  jitter_ms: number | null
  state: string
}

export interface TrendBucket {
  bucket: string
  samples: number
  avg_latency_ms: number | null
  avg_loss_pct: number | null
  down_pct: number | null
}

export interface PerfState {
  degraded: 0 | 1
  metric: "latency" | "jitter" | null
  baseline_ms: number | null
  current_ms: number | null
  since: string | null
}

export interface OnuOptic {
  id: number
  device_id: number
  onu_key: string
  pon_port: string | null
  onu_id: number | null
  name: string | null
  serial: string | null
  state: "online" | "offline" | "dying_gasp" | "los" | "unknown" | null
  rx_dbm: number | null
  tx_dbm: number | null
  olt_rx_dbm: number | null
  distance_m: number | null
  rx_ref_dbm: number | null
  rx_ref_at: string | null
  severity: "ok" | "warn" | "crit" | null
  ack_until: string | null
  updated_at: string
  /** frozen at the moment the ONU left `online` (store upsert CASE) */
  last_online_at: string | null
}

export interface OltOptics {
  device_id: number
  onus_total: number
  onus_online: number
  warn_count: number
  crit_count: number
  alarm: 0 | 1
  alarm_since: string | null
  updated_at: string
}
/** one ONU slot sharing a duplicated MAC (central/onuroster.py) */
export interface DupMacMember {
  device_id: number
  device_name: string
  pon_port: string | null
  onu_id: number | null
  onu_key: string
  state: string | null
}
export interface DupMac {
  mac: string
  members: DupMacMember[]
}
/** one ONU matched by a serial/MAC or name search, with just the fields the
    Network page's result list renders — a slim projection of OnuOptic. */
export interface OnuSearchHit {
  id: number
  onu_key: string
  pon_port: string | null
  onu_id: number | null
  name: string | null
  serial: string | null
  state: OnuOptic["state"]
  severity: OnuOptic["severity"]
  rx_dbm: number | null
  distance_m: number | null
  last_online_at: string | null
  updated_at: string
}
export interface OnuSearchMatch {
  device_id: number
  device_name: string
  onus: OnuSearchHit[]
}
export interface OnuSearchResponse {
  matches: OnuSearchMatch[]
  /** hit the server's result cap — the needle is too broad, type more */
  truncated: boolean
}

export interface OpticsResponse {
  onus: OnuOptic[]
  olt: OltOptics | null
  warn_dbm: number
  crit_dbm: number
  /** effective per-PON ONU cap: OLT override ?? global default */
  onu_pon_limit: number
  /** redundant-MAC groups touching this OLT (org-wide detection) */
  dup_macs: DupMac[]
}

export interface ReliabilityRow {
  device_id: number
  name: string
  region: string | null
  downtime_seconds: number
  uptime_pct: number
  outage_count: number
}

export type OutageStatus = "unassigned" | "in_progress" | "pending_postmortem"

export interface Outage {
  id: number
  org_id: string
  device_id: number
  device_name: string
  region: string | null
  started_at: string
  resolved_at: string | null
  final_state: DeviceState
  acknowledged_by: string | null
  acknowledged_at: string | null
  root_cause: string | null
  resolution_notes: string | null
  status: OutageStatus
}

export const ROOT_CAUSES = [
  "Power Loss", "Fiber Cut", "Hardware Failure", "Config Error", "Weather", "Other",
] as const

export interface NodeToken {
  node_id: string

  registered: boolean
  created_at: string | null
  revoked_at: string | null
  version: string | null
  last_seen: string | null
  fleet_size: number | null
  open_outages: number | null

  rss_bytes: number | null
  mem_total_bytes: number | null
  mem_available_bytes: number | null
}

export interface OrgRollout {
  org_id: string
  target_version: string
  canary: string[]
  state: "canary" | "promoted" | "done" | "halted"
  started_at: string
  updated_at: string
  note: string | null
}

export interface NodesResponse {
  nodes: NodeToken[]
  latest_version: string | null
  rollout: OrgRollout | null
  auto_update: boolean
  /** operator colour per node_id, sparse — see lib/palette.ts */
  node_colors: Record<string, string>
}

export interface LogEvent {
  id: number
  org_id: string
  node_id: string
  type: string
  device_id: number | null
  device_name: string | null
  device_ip: string | null
  device_region: string | null
  state: string | null
  occurred_at: string | null
  received_at: string
  payload: Record<string, unknown> | null
}

export interface Summary {
  uplink_down: boolean
  low_bandwidth: Array<{
    port_id: number
    device_id: number
    switch_name: string
    label: string
    in_mbps: number | null
    out_mbps: number | null
    threshold_mbps: number | null
    direction: string
    since: string | null
  }>
  high_bandwidth: Array<{
    port_id: number
    device_id: number
    switch_name: string
    label: string
    in_mbps: number | null
    out_mbps: number | null
    max_mbps: number | null
    direction: string
    since: string | null
  }>
}

export interface AccountUser {
  id: number
  org_id: string | null
  username: string
  role: Role
  is_active: 0 | 1
  // experimental WhatsApp channel: where this account is paged (E.164) or null
  whatsapp_number: string | null
  created_at: string
}

// Server-wide WhatsApp channel config (Settings → Platform). The token is never
// echoed — only whether one is stored.
export interface WhatsappSettings {
  enabled: boolean
  phone_id: string
  template: string
  lang: string
  api_version: string
  token_set: boolean
}

export type SnmpWalkStatus = "pending" | "done" | "error"

export interface SnmpWalk {
  id: number
  node_id: string
  root_oid: string
  max_varbinds: number
  status: SnmpWalkStatus
  requested_by: string | null
  error: string | null
  varbind_count: number | null
  /** Walk stopped at the edge's varbind cap or time budget — the subtree is
   *  only partly dumped, so an absent OID proves nothing. SQLite flag, so
   *  0 | 1 like tree_detached, not a JSON boolean. */
  truncated: 0 | 1
  created_at: string
  completed_at: string | null
}

export interface SnmpWalkResult extends SnmpWalk {
  result: Array<[string, string]> | null
}

export interface ProfileMetricSpec {
  oid: string
  decode: string
  select: string
}

export interface SnmpProfile {
  id: number
  org_id: string | null // null = global (every org's edges receive it)
  name: string
  match_sysobjectid: string
  metrics: Record<string, ProfileMetricSpec>
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface SnmpProfilesResponse {
  profiles: SnmpProfile[]
  metrics: string[]
  decodes: string[]
  selects: string[]
}

/** Closed-vocabulary GPON/EPON vendor spec — what the edge's
 *  gpon_profile_from_dict validates. All fields optional except oids. */
export interface GponProfileSpec {
  oids: Record<string, string>
  scales: Record<string, number>
  state_map: Record<string, string>
  state_default: string
  pon_index: string
  pon_label: string
}

export interface GponProfile {
  id: number
  org_id: string | null // null = global (every org's edges receive it)
  name: string
  match_sysobjectid: string
  spec: GponProfileSpec
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface GponProfilesResponse {
  profiles: GponProfile[]
  oid_fields: string[]
  states: string[]
  pon_index_strategies: string[]
}

export type SnmpSubsystem = "health" | "ports" | "optics"
export type SnmpStatusState = "ok" | "empty" | "no_response" | "timeout" | "no_profile" | "error"

/** The edge's per-subsystem SNMP sweep diagnosis — WHY a panel is blank. */
export interface SnmpSubsystemStatus {
  subsystem: SnmpSubsystem
  state: SnmpStatusState
  detail: string | null
  sysobjectid: string | null
  profile: string | null
  item_count: number | null
  updated_at: string
  last_ok_at: string | null
}

/** Operator verdict "this hardware can't do X" — only unsupported rows exist. */
export interface DeviceCapability {
  subsystem: SnmpSubsystem
  supported: boolean
  note: string | null
  updated_by: string | null
  updated_at: string
}

export interface SnmpStatusResponse {
  status: SnmpSubsystemStatus[]
  capability: DeviceCapability[]
}

/** Outcome of the last web-UI optics scrape for one OLT (central's own clock).
    `partial` is a real success: a PON that answered carries real readings. */
export type WebOpticsState =
  | "ok" | "partial" | "skipped" | "no_profile" | "no_credentials"
  | "unreachable" | "login" | "error"

export interface WebOpticsStatus {
  device_id: number
  profile: string
  state: WebOpticsState
  detail: string | null
  rows: number
  updated_at: string
  last_ok_at: string | null
}

/** WHY an OLT shows no per-ONU dBm (GET /api/inventory/rx-status).
    FACTS, not a verdict — the SPA composes the sentence, exactly as
    SnmpDiagnosis does for a blank Ports/Optical panel. */
export interface RxStatusResponse {
  /** the vendor this OLT resolved as, dropdown first then edge detection */
  vendor: string | null
  vendor_source: "declared" | "detected" | null
  /** the web-UI optics recipe covering that vendor, or null if none exists */
  web_profile: string | null
  /** every vendor a profile covers — "which OLTs can be read at all" */
  known_vendors: string[]
  has_credentials: boolean
  web_proxy: boolean
  has_node: boolean
  onus_total: number
  onus_rx: number
  scrape: WebOpticsStatus | null
  /** whether a read can be asked for on demand at all (recipe + login + probe).
      Decided server-side off the sweep's own eligibility query — the button
      must never promise a reading nothing will take. */
  can_refresh: boolean
  /** a read of this OLT is running right now */
  refreshing: boolean
}

/** A web-UI optics vendor recipe (Settings → Monitoring). Closed vocabulary —
    the whole profile is refused rather than partially applied. */
export interface WebOpticsProfileSpec {
  login_page_path: string
  login_path: string
  optics_path: string
  username_field: string
  password_field: string
  login_static: Record<string, string>
  session: string
  session_key_field: string
  optics_method: string
  pon_field: string
  optics_static: Record<string, string>
  charset: string
  onu_id_shape: string
  pon_label: string
  columns: Record<string, string>
  column_order: string[]
  default_pons: number[]
  vendor_markers: string[]
}

export interface WebOpticsProfile {
  id: number
  org_id: string | null // null = global (every org)
  name: string
  spec: WebOpticsProfileSpec
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface WebOpticsProfilesResponse {
  profiles: WebOpticsProfile[]
  /** vendors covered by a profile shipped in code (a same-named row shadows it) */
  builtins: string[]
  fields: string[]
  sessions: string[]
  methods: string[]
  charsets: string[]
  onu_id_shapes: string[]
  example: Partial<WebOpticsProfileSpec>
}

export interface SystemStats {
  hostname: string
  uptime_s: number | null
  cpu: { percent: number | null; cores: number | null; load: number[] | null }
  memory: {
    total_bytes: number; used_bytes: number; available_bytes: number; percent: number
  } | null
  /** Filesystem holding central.db. */
  disk: {
    total_bytes: number; used_bytes: number; free_bytes: number; percent: number
  } | null
  process: { rss_bytes: number | null; db_bytes: number | null }
  /** Release-mirror health: null until the first sync ever runs. */
  release_sync: { ok: boolean; detail: string; at: string } | null
  latest_release: string | null
}

/** One broken-coverage device on the superadmin Overview page. */
export interface OverviewProblem {
  device_id: number
  name: string
  area: "snmp" | "optics" | "ports"
  reason: "never" | "stale"
  detail: string
  last_at: string | null
}

export interface OverviewCounts {
  devices: number
  snmp: { enabled: number; working: number }
  optics: { olts: number; working: number; onus_total: number; onus_online: number }
  ports: { switches: number; discovered: number; monitored: number; working: number; alarms: number }
}

export interface OverviewOrg extends OverviewCounts {
  org_id: string
  name: string | null
  problems: OverviewProblem[]
}

export interface AdminOverview {
  fresh_window_s: number
  generated_at: string
  totals: OverviewCounts
  problems_total: number
  orgs: OverviewOrg[]
}
