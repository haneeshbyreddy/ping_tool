// Mirrors src/wisp/central/store.py's row shapes exactly — field names match the JSON
// wire format, not JS convention, since these are passed straight through from the API.

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
  node_count: number
}

export const DEVICE_TYPES = [
  "core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul",
] as const
export type DeviceType = (typeof DEVICE_TYPES)[number]

export type DeviceState = "UP" | "DOWN" | "DEGRADED" | "UNREACHABLE"

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
  child_count: number
  backup_parents: number[]
  // Added by the list_org_devices() LEFT JOIN onto device_states (see CLAUDE.md) — null
  // until the device's first report lands.
  state: DeviceState | null
  latency_ms: number | null
  packet_loss: number | null
  jitter_ms: number | null
  state_updated_at: string | null
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
  bw_direction: "in" | "out" | "either" | "total" | null
  in_bps: number | null
  out_bps: number | null
  bw_low_streak: number
  bw_alarm: 0 | 1
  bw_alarm_since: string | null
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
  created_at: string
  revoked_at: string | null
  version: string | null
  last_seen: string | null
  fleet_size: number | null
  open_outages: number | null
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
}

export interface AccountUser {
  id: number
  org_id: string | null
  username: string
  role: Role
  is_active: 0 | 1
  created_at: string
}
