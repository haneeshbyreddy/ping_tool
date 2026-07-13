export type Role = "owner" | "operator" | "tech"

export interface User {
  id: number
  username: string
  org_id: string | null
  org_name: string | null
  role: Role
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
  ntfy_topic_operator: string | null
  ntfy_topic_tech: string | null
  map_region: string | null
  // the superadmin's server-wide Map Tiles key, injected into every org row
  google_maps_key: string | null
  node_count: number
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

/** Drawn cable path for one link — intermediate vertices only, parent→child order. */
export interface LinkRoute {
  child_id: number
  parent_id: number
  waypoints: Array<[number, number]>
  updated_at: string
  updated_by: string | null
}

export interface OrgRegion {
  name: string
  declared: boolean
  device_count: number
  worker_count: number
}

export interface OrgDevice {
  id: number
  org_id: string
  name: string
  ip_address: string
  device_type: DeviceType | null
  region: string | null
  parent_device_id: number | null
  assigned_node_id: string | null
  maintenance: 0 | 1
  snmp_enabled: 0 | 1
  snmp_version: string
  snmp_community: string | null
  snmp_port: number

  gpon_vendor: string | null
  /** passive plant only: which PON this splitter/FDB serves (e.g. "0/6") */
  pon_port: string | null
  lat: number | null
  lng: number | null
  child_count: number
  backup_parents: number[]

  ports_down: number
  ports_bw_low: number
  ports_bw_high: number

  onus_total: number | null
  onus_online: number | null
  onus_warn: number | null
  onus_crit: number | null
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
}

export interface Worker {
  id: number
  org_id: string
  name: string
  role: Role
  region: string | null
  is_active: 0 | 1
  notes: string | null
}

export interface AttendanceOperator {
  id: number
  name: string
  role: Role
  region: string | null
  present_today: boolean
  days: Record<string, boolean>
}

export interface AttendanceOverview {
  today: string
  days: string[]
  operators: AttendanceOperator[]
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
  created_at: string
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
