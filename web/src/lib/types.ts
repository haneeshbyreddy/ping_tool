import type { PortKind } from "@/lib/fiber"
import type { MapDetail } from "@/map/detail"

export type Role = "owner" | "worker"

export interface User {
  id: number
  username: string
  org_id: string | null
  org_name: string | null
  role: Role
  whatsapp_number: string | null
  totp_enabled: boolean
  is_superadmin: boolean
}

export interface MeResponse {
  user: User
}

export interface Org {
  org_id: string
  name: string | null
  map_region: string | null
  google_maps_key: string | null
  map_detail: MapDetail | null
  poll_interval_s: number | null
  plan: Plan
  web_proxy: number
  node_count: number
  device_count: number
  user_count: number
}

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

export interface WebUiCredentials {
  username: string
  has_password: boolean
  auth_mode: "basic" | "form"
  updated_by: string | null
  updated_at: string | null
}

export type Plan = "free" | "pro" | "vip"
export type BillingStatus = "free" | "active" | "due_soon" | "locked"

export interface PlanSpec {
  label: string
  price_inr: number
  device_cap: number | null // null = unlimited
  node_cap: number | null // edge probes; null = unlimited
  features: string[]
}

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
  node_count: number
  node_cap: number | null
  gpay_number: string
  qr_image: string | null
  plans: Record<Plan, PlanSpec>
}

export const DEVICE_TYPES = [
  "core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul", "nvr",
] as const
export const PASSIVE_DEVICE_TYPES = ["splitter", "coupler", "fdb", "closure"] as const
export const SPLIT_RATIOS = [2, 4, 8, 16] as const
export const SPLIT_INPUTS = [1, 2] as const
export type DeviceType =
  (typeof DEVICE_TYPES)[number] | (typeof PASSIVE_DEVICE_TYPES)[number]
export const isPassiveType = (t: string | null | undefined): boolean =>
  !!t && (PASSIVE_DEVICE_TYPES as readonly string[]).includes(t)

export type DeviceState = "UP" | "DOWN" | "DEGRADED" | "UNREACHABLE"

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
  suspect: string | null
  evidence: "witness" | "dying_gasp" | "silence"
  witness_dark: number
  witness_alive: number
}

export interface PonSummary {
  olts: number
  onus_total: number
  onus_online: number
  onus_offline: number
  fiber_cuts: number
  pons_over_cap: number
  over_cap_device_ids: number[]
  pon_cap: number
  pon_cap_worst: number
  dup_macs_live: number
  dup_macs_total: number
  onus_crit: number
  onus_warn: number
  onus_rx: number
  olts_rx: number
}

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

export interface LinkRoute {
  child_id: number
  parent_id: number
  waypoints: Array<[number, number]>
  label_pos: number | null
  cable_id: number | null
  cable_name: string | null
  cores: number | null
  core_no: number | null
  from_cable: boolean
  updated_at: string
  updated_by: string | null
}

export interface FibrePoint {
  kind: "device" | "onu"
  device_id: number | null
  mac: string | null
  name?: string | null
  device_type?: string | null
  lat?: number | null
  lng?: number | null
}

export interface CoreEnd {
  cable_id?: number
  cable_name?: string | null
  core_no?: number
  terminates?: boolean
  point?: string | null
}

export interface Cable {
  id: number
  name: string
  cores: number | null
  path: Array<[number, number]>
  length_m: number | null
  a: FibrePoint
  b: FibrePoint
  notes: string | null
  plan: Record<string, { a?: CoreEnd; b?: CoreEnd }>
  labels: Record<string, string>
  cores_recorded: number
  updated_at: string
  updated_by: string | null
}

// A device pair the FIBRE RECORD already joins — directly, or on through a closure,
// which is where a sheath is opened rather than a place the light stops. The map
// stands that pair's dependency chord down (the glass is the better line, drawn where
// somebody walked it) and hangs the link's rate chip on `cable_id`, the biggest sheath
// on the run. Derived server-side by the same walk `undrawn` reads, so the map and the
// draft can never disagree about what counts as recorded.
export interface CabledPair {
  a: number
  b: number
  // The sheath worth labelling — the biggest on the run.
  cable_id: number | null
  // EVERY cable the light crosses between the pair. The map draws all of them wherever
  // it stands the chord down, including below the plant zoom floor: the sheath has
  // taken over the chord's job, so it inherits the chord's visibility, not plant's.
  cable_ids: number[]
}

export interface TrayCable {
  cable_id: number
  name: string
  cores: number | null
  end: "a" | "b"
  far: FibrePoint
  plumbing?: boolean
  labels: Record<string, string>
  side: "feed" | "onward"
}

export interface FibreJoint {
  id: number
  a_cable_id: number
  a_core_no: number
  b_cable_id: number | null
  b_core_no: number | null
  port_kind: PortKind | null
  port_ref: string | null
}

export interface TrayPort {
  kind: PortKind
  ref: string
  label: string
  // true up, false down, null NOT MEASURED — a passive leg, a stale walk, or a box
  // that is down. The third never renders as a colour.
  live?: boolean | null
  drops: Array<{ mac: string; name: string | null }>
}

export interface UndrawnLink {
  far: FibrePoint & { name?: string | null; device_type?: string | null }
  relation: "feeds" | "fed by"
}

export interface CarriedEnd {
  name: string | null
  port: string | null
  here: boolean
}

export interface PointFibre {
  point: FibrePoint
  cables: TrayCable[]
  // cable_id -> core_no -> the terminations that fibre reaches. Only cores that
  // REACH something appear: a core joined to nothing carries nothing.
  carries: Record<string, Record<string, CarriedEnd[]>>
  ports: TrayPort[]
  port_add: PortKind[]
  undrawn: UndrawnLink[]
  unplaced_drops: Array<{ mac: string; name: string | null }>
  joints: FibreJoint[]
}

export interface TraceHop {
  cable_id: number
  cable_name: string | null
  cores: number | null
  core_no: number
  from: FibrePoint
  to: FibrePoint
}

export interface FibreTrace {
  ok: boolean
  fault: "fork" | "loop" | "missing" | null
  fault_at: FibrePoint | null
  hops: TraceHop[]
  points: FibrePoint[]
  ends: Array<{ point: FibrePoint } | null>
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
  tags: string[]
  parent_device_id: number | null
  feed_device_id: number | null
  tree_detached: 0 | 1
  assigned_node_id: string | null
  maintenance: 0 | 1
  snmp_enabled: 0 | 1
  snmp_version: string
  snmp_community: string | null
  snmp_port: number

  gpon_vendor: string | null
  nvr_vendor: string | null
  pon_port: string | null
  fibre_pon: {
    olt_id: number | null
    pon_no: number | null
    source: "fibre" | "inherited"
    ambiguous: boolean
    via_device_id?: number
  } | null
  split_ratio: number | null
  split_inputs: number | null
  onu_pon_limit: number | null
  web_ip: string | null
  web_port: number | null
  web_scheme: string | null
  lat: number | null
  lng: number | null
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  child_count: number
  backup_parents: number[]
  peer_ids: number[]
  assignee_ids: number[]

  ports_down: number
  ports_bw_low: number
  ports_bw_high: number

  onus_total: number | null
  onus_online: number | null
  onus_warn: number | null
  onus_crit: number | null
  onus_rx: number | null
  fiber_cuts: number
  dup_macs: number
  optics_updated_at: string | null
  ports_updated_at: string | null
  cameras_total: number | null
  cameras_down: number | null
  cameras_updated_at: string | null
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
  uplink_device_id: number | null
}

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
  label: string | null
  radius_name?: string | null
  radius_username?: string | null
  radius_mobile?: string | null
  radius_status?: string | null
  radius_match?: "mac" | "name" | null
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
  last_online_at: string | null
  place: {
    lat: number; lng: number; label: string | null; phone: string | null
    witness: boolean
  } | null
  drop_passive_id: number | null
}

export interface SplitterLoad {
  passive_id: number
  recorded: number
  online: number
  dark: number
  orphans: number
  crit: number
  warn: number
  rx_seen: number
  rx_median: number | null
  rx_worst: number | null
  outliers: number
  olt_id: number | null
  pon_ports: string[]
}

export interface BranchFault {
  passive_id: number
  parent_id: number | null
  olt_id: number | null
  pon_ports: string[]
  dark: number
  lit_siblings: number
  cause: "fiber" | "power"
  witness_dark: number
  suspected: boolean
  passives: number[]
}

export interface SubscriberDrop {
  mac: string
  olt_id: number | null
  pon_port: string | null
  onu_id: number | null
  name: string | null
  state: OnuOptic["state"]
  rx_dbm: number | null
  severity: OnuOptic["severity"]
  matched: boolean
  witness: boolean
}

export interface OnuPlace {
  mac: string
  lat: number
  lng: number
  label: string | null
  // The customer's name from billing, NULL when the MAC is on more than one live
  // slot — the mark and its card must never name one subscriber two ways.
  radius_name?: string | null
  phone: string | null
  notes: string | null
  witness: boolean
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  created_at: string
  updated_at: string
  matched: boolean
  ambiguous: boolean
  slots: number
  drop_passive_id: number | null
  drop_waypoints: Array<[number, number]>
  device_id: number | null
  device_name: string | null
  onu_id: number | null
  pon_port: string | null
  name: string | null
  state: OnuOptic["state"]
  rx_dbm: number | null
  severity: OnuOptic["severity"]
  optics_updated_at: string | null
  if_name: string | null
  port_state: string | null
  in_bps: number | null
  out_bps: number | null
  port_updated_at: string | null
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
export interface OnuSearchHit {
  id: number
  onu_key: string
  pon_port: string | null
  onu_id: number | null
  name: string | null
  label: string | null
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
export interface OnuCoverageOlt {
  device_id: number
  device_name: string | null
  total: number
  placed: number
}
export interface OnuCoverageRow {
  mac: string
  name: string | null
  pon_port: string | null
  onu_id: number | null
  state: string | null
  device_id: number
  device_name: string | null
}
export interface OnuCoverageLocatedRow extends OnuCoverageRow {
  label: string | null
  phone: string | null
  lat: number
  lng: number
  witness: boolean
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
}
export interface OnuCoverageResponse {
  total: number
  placed: number
  olts: OnuCoverageOlt[]
  unplaced: OnuCoverageRow[]
  located: OnuCoverageLocatedRow[]
}

export interface OnuSearchResponse {
  matches: OnuSearchMatch[]
  truncated: boolean
}

export interface Subscriber {
  mac: string
  record: SubscriberRecord | null
  matched: boolean
  ambiguous: boolean
  slots: number
  roster: OnuOptic | null
  olt: {
    id: number
    name: string | null
    state: string | null
    optics_updated_at: string | null
  } | null
  drop: { passive_id: number; chain: SubscriberPlantHop[] } | null
  rate: {
    if_name: string | null
    port_state: string | null
    in_bps: number | null
    out_bps: number | null
    updated_at: string | null
  } | null
  thresholds: { warn_dbm: number; crit_dbm: number } | null
  user_macs: UserMac[]
  user_mac_status: WebMacStatus | null
  radius: RadiusCustomer | null
  radius_panels: RadiusStatus[]
}

// The customer as the BILLING panel holds them, tied to this ONU by the MAC we
// scrape off the OLT (or, failing that, by the ONU's provisioned name matching
// the RADIUS username). `match_by` says WHICH evidence tied them, because an
// ambiguous match links nothing at all rather than guessing.
// `expiry` and `balance` are the panel's own strings, deliberately unparsed.
export type RadiusCustomer = {
  username: string
  match_by: "mac" | "name"
  updated_at: string
  account_id: number
  account_label: string | null
  account_url: string | null
  name: string | null
  mobile: string | null
  alt_mobile: string | null
  status: string | null
  expiry: string | null
  package: string | null
  branch: string | null
  area: string | null
  address: string | null
  balance: string | null
  acno: string | null
  mac: string | null
}

// The panel's address, the recipe to read it with, and who we sign in as. The
// password is WRITE-ONLY: `password_set` is a boolean and the secret itself never
// leaves the server, so a blank field on the form means "leave the stored one".
// MANY panels per org, not one: an ISP can run several billing systems at once
// and Hansa asked for a second the week the first went live. `id` identifies the
// panel everywhere, and account ORDER is priority — the panel connected first
// wins a cross-panel tie.
export type RadiusAccount = {
  id: number
  label: string
  profile: string
  base_url: string
  username: string | null
  password_set: boolean
  enabled: boolean
  updated_at: string
  status: RadiusStatus | null
  customers: number
}

export type RadiusSettings = {
  accounts: RadiusAccount[]
  customers: number
  profiles: string[]
}

export type RadiusAccountPayload = {
  id?: number
  label?: string
  profile: string
  base_url: string
  username: string
  password?: string
  enabled?: boolean
}

// The billing book joined to the network, one row per billing customer.
// `net` is a claim about NOW and keeps the frozen rule: a customer behind a
// DOWN OLT reads `frozen`, never `dark` — the OLT's outage owns that page.
export type CustomerNet = "online" | "dark" | "frozen" | "stale" | "unlinked"

export interface CustomerRow {
  username: string
  name: string | null
  mobile: string | null
  status: string
  package: string | null
  branch: string | null
  area: string | null
  address: string | null
  acno: string | null
  expiry: string | null
  expiry_at: string | null
  days_left: number | null
  account_id: number
  account_label: string | null
  in_last_read: boolean
  last_seen_at: string | null
  net: CustomerNet
  reason: string | null
  match_by: string | null
  device_id: number | null
  device_name: string | null
  onu_mac: string | null
  onu_label: string | null
  onu_name: string | null
  pon_port: string | null
  onu_id: number | null
  located: boolean
  dark_since: string | null
}

export interface CustomersReply {
  customers: CustomerRow[]
  counts: {
    customers: number
    active: number
    linked: number
    paying_dark: number
    paying_frozen: number
  }
  reasons: Record<string, string>
  panels: RadiusStatus[]
}

// `forbidden` is its own state and is NOT a password problem: the sign-in worked
// and the panel refused the export to this login. Reported as `login` it sends an
// ISP to change a password that was never wrong.
export type RadiusStatus = {
  account_id: number
  org_id: string
  profile: string
  state: "ok" | "partial" | "skipped" | "no_profile" | "no_credentials"
    | "unreachable" | "login" | "forbidden" | "error"
  detail: string | null
  customers: number
  linked: number
  updated_at: string
  last_ok_at: string | null
}

// The address the SUBSCRIBER'S OWN equipment presents behind the ONU, read off
// the OLT's address table. Not the ONU's MAC — on the GPON fleet the ONU has no
// MAC at all, so this is the only address that customer has, and it is what the
// ISPs' RADIUS app is keyed on.
export interface UserMac {
  mac: string
  vlan: string | null
  kind: string | null
  port_label: string | null
  first_seen_at: string
  last_seen_at: string
}

export interface WebMacStatus {
  profile: string
  state: "ok" | "partial" | "skipped" | "no_profile" | "no_credentials"
    | "unreachable" | "login" | "error"
  detail: string | null
  rows: number
  declared: number | null
  updated_at: string
  last_ok_at: string | null
}

export interface SubscriberRecord {
  label: string | null
  phone: string | null
  notes: string | null
  witness: boolean
  lat: number | null
  lng: number | null
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  created_at: string
  updated_at: string
}

export interface SubscriberPlantHop {
  id: number
  name: string | null
  device_type: string | null
  split_ratio: number | null
  split_inputs: number | null
  pon_port: string | null
}

export interface OpticsResponse {
  onus: OnuOptic[]
  olt: OltOptics | null
  warn_dbm: number
  crit_dbm: number
  onu_pon_limit: number
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

// /api/history/reliability?device_id= — the availability strip's rows.
// day/coverage keys are epoch SECONDS (UTC day floors), the historian's
// number-only convention; the SPA converts to ms at the edge.
export interface ReliabilityDay {
  day: number
  down_s: number
  outages: number
}

export interface OutageSpan {
  id: number
  started_at: string
  resolved_at: string | null
  final_state: string
  root_cause: string | null
  duration_s: number
}

export interface CoverageDay {
  day: number
  samples: number
}

export interface DeviceReliability {
  since: string
  until: string
  days: ReliabilityDay[]
  spans: OutageSpan[]
  coverage: CoverageDay[]
  recording_since: string | null
}

// /api/history/reliability (org view) — Monday-anchored ISO weeks, epoch s.
export interface WeekStat {
  week: number
  outages: number
  resolved: number
  ttr_p50_s: number | null
  ttr_p90_s: number | null
  tta_p50_s: number | null
  tta_p90_s: number | null
}

export interface OrgReliability {
  since: string
  until: string
  weeks: WeekStat[]
}

// /api/history/onus — the org's hourly ONU story, summed over OLTs.
// crit/warn are sums of each OLT's hourly WORST, labeled that way on screen.
export interface OnuTrendBucket {
  bucket: number
  olts: number
  samples: number
  onus: number
  online: number
  warn: number
  crit: number
}

export interface OnuTrendReply {
  since: string
  until: string
  recording_since: string | null
  buckets: OnuTrendBucket[]
}

// /api/history/paging — kind '' is the pre-kind era, labeled, never dropped.
export interface PagingRow {
  day: string
  kind: string
  status: string
  n: number
}

export interface PagingReply {
  since: string
  until: string
  rows: PagingRow[]
}

export type OutageStatus =
  "unassigned" | "assigned" | "in_progress" | "pending_postmortem"

export type IssueKind =
  | "device_down" | "port_down" | "camera_down" | "probe_stale" | "bandwidth"
  | "onu_crit" | "onu_warn" | "dup_mac" | "pon_fiber" | "pon_power"
  | "pon_capacity" | "onu_offline"

export type IssueSeverity = "critical" | "warning" | "info"

export interface Issue {
  kind: IssueKind
  kind_label: string
  severity: IssueSeverity
  device_id: number | null
  device_name: string
  region: string | null
  subject: string
  detail: string
  since: string | null
}

export interface IssuesResponse {
  issues: Issue[]
  counts: Record<string, number>
  total: number
  generated_at: string
  kinds: IssueKind[]
  kind_labels: Record<string, string>
}

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
  assigned_to: string[]
  assigned_at: string | null
  assigned_by: string | null
  accepted_by: string[]
  accepted_at: string | null
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
  whatsapp_number: string | null
  created_at: string
}

export interface WhatsappSettings {
  enabled: boolean
  phone_id: string
  template: string
  lang: string
  api_version: string
  admin_number: string
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
export type SnmpStatusState =
  | "ok" | "partial" | "empty" | "no_response" | "timeout" | "no_profile" | "error"

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

export interface RxStatusResponse {
  vendor: string | null
  vendor_source: "declared" | "detected" | null
  web_profile: string | null
  known_vendors: string[]
  has_credentials: boolean
  web_proxy: boolean
  has_node: boolean
  onus_total: number
  onus_rx: number
  scrape: WebOpticsStatus | null
  can_refresh: boolean
  refreshing: boolean
}

export interface NvrChannel {
  channel_no: number
  name: string | null
  ip_address: string | null
  port: number | null
  camera_kind: string | null
  enabled: boolean
  monitored: boolean
  state: "online" | "offline" | "unknown"
  last_online_at: string | null
  first_seen_at: string
  updated_at: string
}

export interface NvrScrapeStatus {
  state: string
  detail: string | null
  profile: string
  channels: number
  updated_at: string
  last_ok_at: string | null
}

export interface NvrChannelsResponse {
  channels: NvrChannel[]
  scrape: NvrScrapeStatus | null
  vendor: string | null
  profile: string | null
  known_vendors: string[]
  has_credentials: boolean
  web_proxy: boolean
  has_node: boolean
  can_refresh: boolean
  refreshing: boolean
}

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
  disk: {
    total_bytes: number; used_bytes: number; free_bytes: number; percent: number
  } | null
  process: { rss_bytes: number | null; db_bytes: number | null }
  release_sync: { ok: boolean; detail: string; at: string } | null
  latest_release: string | null
}

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

export interface DeviceAssignment {
  device_id: number
  user_id: number
  username: string
  role: Role
  is_active: boolean
  has_whatsapp: boolean
  assigned_by: string | null
  assigned_at: string | null
}

export interface AssignableAccount {
  user_id: number
  username: string
  role: Role
  has_whatsapp: boolean
  assigned: number
  devices: number
}

export interface AssignmentRoster {
  assignments: DeviceAssignment[]
  accounts: AssignableAccount[]
  unassigned: number
}

export interface WorkerFix {
  ts: string
  lat: number
  lng: number
  accuracy_m: number | null
  speed_mps: number | null
  heading: number | null
  battery_pct: number | null
}

export interface FieldWorker {
  user_id: number
  username: string
  role: Role
  has_token: boolean
  last_fix: WorkerFix | null
  trail: Array<[number, number]>
  shift_started_at: string | null
  shift_ended_at: string | null
  on_shift: boolean
}

export interface FieldWorkersResponse {
  workers: FieldWorker[]
  trail_since: string
  fresh_s: number
  retention_days: number
}

export interface FieldAccount {
  user_id: number
  username: string
  role: Role
  issued_at: string | null
  revoked_at: string | null
}

export interface FieldTokensResponse {
  accounts: FieldAccount[]
  server_url: string
  retention_days: number
}

export interface ShiftState {
  on_shift: boolean
  started_at: string | null
  ended_at: string | null
  has_token: boolean
}
