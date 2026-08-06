import type { MapDetail } from "@/map/detail"

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
}

export interface Org {
  org_id: string
  name: string | null
  map_region: string | null
  // the superadmin's server-wide Map Tiles key, injected into every org row
  google_maps_key: string | null
  // the superadmin's server-wide map zoom floors, injected the same way — NOT
  // org data; every row carries the identical value (central/mapdetail.py)
  map_detail: MapDetail | null
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
/** Every device type that IS passive plant — what `isPassiveType` answers, and
 *  therefore what decides whether a row is kept out of `org_device_topology`,
 *  skipped by billing and allowed to carry subscriber drops. Mirrors
 *  `inventory.PASSIVE_TYPES` on the server and must keep mirroring it.
 *
 *  NOT the list the operator picks from when CREATING one — that is
 *  `map/plant.ts:PLANT_KINDS`, narrowed to `splitter`. Removing a type from
 *  HERE would promote any existing row of that type to monitored gear, complete
 *  with an FSM and the ability to page, which is why the two lists are separate. */
export const PASSIVE_DEVICE_TYPES = ["splitter", "fdb", "closure"] as const
/** How many ways a passive splits the fibre. CLOSED, and matching
 *  `inventory.SPLIT_RATIOS` on the server — only what an ISP actually stocks,
 *  because the ratio feeds the load bar and the cumulative split down a
 *  cascade, and "1:7" would produce arithmetic nobody can act on. */
export const SPLIT_RATIOS = [2, 4, 8] as const
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
  /** How `kind` was decided. `silence` is the honest name for a verdict reached
   *  with nothing to go on — the C-Data/DBC fleet reports neither dying_gasp
   *  nor LOS, so a "fiber" call there is an assumption until a placed reference
   *  ONU (see OnuPlace) makes it a finding. Don't render the three alike. */
  evidence: "witness" | "dying_gasp" | "silence"
  /** reference ONUs that went dark SILENTLY — power cannot explain these */
  witness_dark: number
  /** reference ONUs on this PON still online */
  witness_alive: number
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
  /** passive plant only: how many ways this box splits (2 | 4 | 8; see
   *  SPLIT_RATIOS). null = not recorded — which is also the honest value for a
   *  closure that only splices. Drives the recorded-load bar and the cumulative
   *  split down a cascade; never an occupancy claim on its own, because a leg
   *  nobody wrote down is unknown, not free. */
  split_ratio: number | null
  /** OLT only: how many ONUs fit on one PON before it reads as full. EPON tops
   *  out at 1:64 and GPON at 1:128, so one global default false-pages half a
   *  mixed fleet. null = not set, i.e. the server's global cap applies. */
  onu_pon_limit: number | null
  /** web-UI proxy address override: where the admin page actually lives when
      it isn't at ip_address:80/443 (port-forwarding / a separate mgmt IP).
      Any set = "Open web UI" targets (web_ip||ip_address):(web_port||default)
      over web_scheme; all null = classic behavior. */
  web_ip: string | null
  web_port: number | null
  web_scheme: string | null
  lat: number | null
  lng: number | null
  /** Where the pin came from, and how well it is known. A phone's first fix is a
   *  cell/wifi estimate at 30–80 m that converges over ~10 s, so a field capture
   *  and a surveyed desktop placement are different claims about the same two
   *  numbers — and the map may not render them alike. All null = placed before
   *  the survey shipped, or dragged on the desktop (which WIPES these on
   *  purpose: the newer hand-placed claim carries no accuracy). "Unknown", never
   *  "surveyed". */
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  child_count: number
  backup_parents: number[]
  /** switch-to-switch cross-links (undirected, no dependency). Stored once per
      cable and expanded symmetrically server-side, so BOTH ends list each other. */
  peer_ids: number[]
  /** Accounts EXPLICITLY on the hook for paging about this device. A PAGING rule
      and nothing else — it never decides what a session may see. Responsibility
      is inherited DOWN the tree, so a device with an empty list here can still be
      covered from an ancestor; `inheritedAssignees` in lib/assignment.ts is what
      resolves that, off the parent chain the tree already has. */
  assignee_ids: number[]

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
  /** what the OLT's walk reports. Rewritten on every SNMP sweep, so nothing an
   *  operator types may ever be stored here — see `label`. */
  name: string | null
  /** The OPERATOR's own name for this subscriber (`onu_places.label`), joined
   *  onto the roster row by the store. Stored UPPERCASE and it WINS over `name`
   *  everywhere an ONU is titled — use `onuName()`, never `o.name` alone, or a
   *  name typed in the field renders as "unnamed" on the OLT that carries it. */
  label: string | null
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
  /** set when this ONU has a PIN — see OnuPlace. A field-located subscriber has
   *  one too, and `witness` is what separates the two claims: it rides here
   *  because the tab's reference-point toggle used to key on `place != null`
   *  and so rendered an ordinary surveyed drop as a reference point, whose
   *  Save then re-asserted a power claim nobody had made. */
  place: {
    lat: number; lng: number; label: string | null; phone: string | null
    witness: boolean
  } | null
  /** the passive box this subscriber's drop comes off (`onu_drops`), or null
   *  when nobody has recorded one. The id only — the device list already holds
   *  the name, and a second copy could disagree with it. */
  drop_passive_id: number | null
}

/** What one passive box is carrying, from its RECORDED drops (`central/drops.py`).
 *
 *  Read every field as "of what the operator has written down". `recorded` is
 *  never an occupancy claim: a splitter leg nobody recorded is UNKNOWN, not
 *  spare, so the UI may say "6 recorded of 8" and may not say "2 free". The one
 *  capacity statement that survives an incomplete record is OVER-subscription —
 *  more recorded drops than legs is provable either way. */
export interface SplitterLoad {
  passive_id: number
  recorded: number
  online: number
  dark: number
  /** recorded MACs no current roster knows — an RMA'd box or a mistyped sticker */
  orphans: number
  crit: number
  warn: number
  rx_seen: number
  rx_median: number | null
  rx_worst: number | null
  /** subscribers sitting `outlier_db` or more below THIS box's own median —
   *  same feeder, same split loss, so the gap is that one drop's own problem */
  outliers: number
  olt_id: number | null
  pon_ports: string[]
}

/** Every recorded subscriber below one passive is dark while a sibling branch
 *  is still lit — so the break is in the single span feeding that box.
 *
 *  Topology, not distance: PON ranging brackets a cut in metres that run ~39%
 *  short on the C-Data fleet (its `distance_m` is EPON time quanta), whereas
 *  two pins and the cable between them are exactly where a crew drives. Derived
 *  read-side; it never pages and never touches a ponfault verdict. */
export interface BranchFault {
  passive_id: number
  parent_id: number | null
  olt_id: number | null
  pon_ports: string[]
  dark: number
  lit_siblings: number
  /** `power` only when the ONUs announced their own loss and no power-backed
   *  reference ONU in the branch contradicts them — rolling a splicing crew for
   *  a DISCOM outage is the mistake this cross exists to avoid. */
  cause: "fiber" | "power"
  witness_dark: number
  /** false only when a reference ONU proves it; everything else is a hypothesis
   *  and is labelled as one */
  suspected: boolean
  passives: number[]
}

/** One recorded subscriber on a passive (GET /api/inventory/drops/subscribers). */
export interface SubscriberDrop {
  mac: string
  olt_id: number | null
  pon_port: string | null
  onu_id: number | null
  name: string | null
  state: OnuOptic["state"]
  rx_dbm: number | null
  severity: OnuOptic["severity"]
  /** false = recorded here but in no current roster. Reported, never hidden —
   *  a drop that quietly stopped counting is what this list must not conceal. */
  matched: boolean
  /** also a placed reference ONU: its darkness is evidence, not a symptom */
  witness: boolean
}

/** An operator-placed REFERENCE ONU (`onu_places`).
 *
 *  Placing one IS the operator's claim that this subscriber's power is
 *  reliable — there is no power field and nothing detects it. A placed ONU
 *  that goes dark is evidence of a fiber cut; ones that stay up while their
 *  neighbours drop are evidence of an area power cut. That is the whole
 *  feature, so every string the UI puts near the action has to say it.
 *
 *  Keyed on the MAC, so the point follows the box if the drop is re-homed.
 *  `matched` false = the MAC is no longer in any current roster (an RMA'd
 *  box), `ambiguous` = it sits on more than one live slot and the server
 *  refuses to guess which OLT it belongs to. */
export interface OnuPlace {
  mac: string
  lat: number
  lng: number
  label: string | null
  /** the subscriber's contact number. REQUIRED by the field survey (name, number
   *  and location are captured together or not at all), optional on the desktop
   *  reference-ONU dialog — whose meaning is the power-supply claim, not the
   *  customer record. Null on every pin placed before 2026-07-31. */
  phone: string | null
  notes: string | null
  /** TRUE = a REFERENCE ONU: the operator's claim that this subscriber's power is
   *  reliable, which nothing detects and which flips a PON mass-drop verdict from
   *  "fibre cut" to "area power cut". FALSE = a plain location recorded in the
   *  field. Both live in `onu_places`, and the two must never render alike — a
   *  witness is evidence, a location is just a coordinate. Only witnesses reach
   *  `ponfault` (`store.onu_place_macs` filters on it). */
  witness: boolean
  /** Provenance of the pin, like OrgDevice's. Null = placed from the desktop
   *  reference-ONU dialog, which is a click on a map and carries no measurement. */
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  created_at: string
  updated_at: string
  matched: boolean
  ambiguous: boolean
  slots: number
  /** the passive box this drop comes off (`onu_drops`). The map draws the line
   *  to THAT, not to the OLT — a straight line to the OLT skips every splitter
   *  in between, which is the plant a crew actually works on. Null = unrecorded,
   *  and the map falls back to the OLT while rendering the difference. */
  drop_passive_id: number | null
  device_id: number | null
  device_name: string | null
  onu_id: number | null
  pon_port: string | null
  name: string | null
  state: OnuOptic["state"]
  rx_dbm: number | null
  /** graded against the OLT's own thresholds by the optics monitor — pass it to
   *  `onuSev` with `state` rather than re-deriving a verdict from `rx_dbm`, or
   *  the map and the Optical tab can grade one subscriber differently. */
  severity: OnuOptic["severity"]
  /** the OPTICS walk's stamp — the clock `rx_dbm` above rides. NOT
   *  `port_updated_at` (a different sweep): a fresh port table says nothing
   *  about how old the light reading beside it is. Null when the placement
   *  matched no roster row. */
  optics_updated_at: string | null
  /** The ONU's OWN ifTable interface on the OLT (C-Data EPON gives each ONU a
   *  row), so a reference point can carry a real per-subscriber rate. Null on
   *  vendors whose builds don't name interfaces that way — render "no reading",
   *  never the PON aggregate, which is shared by up to 64 subscribers. */
  if_name: string | null
  /** the interface's oper_status — a SECOND opinion, on the port walk's clock.
   *  `state` above (the optical roster) still owns whether the ONU is up. */
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
  /** the OPERATOR's own name (`onu_places.label`) — see OnuOptic.label */
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
/** Survey coverage: how many subscribers have a pin, per OLT. The denominator is
 *  the FRESHEST-walk roster (zombie slots excluded), so it is the number of
 *  drops a tech can actually go and find. */
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
/** A subscriber on this OLT that already HAS a pin — the done half of the same
 *  queue. It carries the placement itself (name, number, coordinates) because
 *  the only reason to tap a done row is to correct one of them, and a
 *  correction that arrived without the pin would re-place the subscriber and
 *  restamp a real GPS fix as a hand-placed point. */
export interface OnuCoverageLocatedRow extends OnuCoverageRow {
  /** the operator's own name (`onu_places.label`), which outranks the walked one */
  label: string | null
  phone: string | null
  lat: number
  lng: number
  /** a REFERENCE ONU — a claim about a power supply, not just a location. Must
   *  be visible before a tech re-pins it (see OnuPlace). */
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
  /** only populated when ?device_id= names one OLT — the fleet's whole unplaced
   *  set is thousands of rows and has no business crossing a handset's link. */
  unplaced: OnuCoverageRow[]
  /** the other half of that one OLT's list, off the SAME roster pass — so its
   *  length IS the row's `placed` counter rather than a second derivation. */
  located: OnuCoverageLocatedRow[]
}

export interface OnuSearchResponse {
  matches: OnuSearchMatch[]
  /** hit the server's result cap — the needle is too broad, type more */
  truncated: boolean
}

/** ONE SUBSCRIBER, WHOLE (`GET /api/inventory/subscriber`).
 *
 *  A subscriber was the only first-class object in this product with no home. A
 *  device has one panel that the tree, the map and an issue row all open; a
 *  subscriber had six partial projections — `OnuOptic`, `OnuPlace`,
 *  `SubscriberDrop`, `OnuSearchHit` and the survey's two coverage rows — each
 *  carrying a different subset, none complete, none addressable. This is the
 *  object `subscriber-detail.tsx` renders, and the six above stay as they are:
 *  LISTS should be slim, and every one of them now opens this.
 *
 *  It is a JOIN of readers that already exist, never a second source of truth,
 *  so nothing here may be re-derived — grade Rx with `onuSev` off
 *  `roster.severity`, name the subscriber with `onuName`, and read freshness off
 *  `olt.optics_updated_at`. */
export interface Subscriber {
  /** normalized identity (`onuroster._norm_mac`) — the ONU's serial as the OLT
   *  reports it, which is what the sticker on the customer's box says. NOT the
   *  customer's own device MAC, and the UI must not label it as one. */
  mac: string
  /** what the OPERATOR has written down: name, number, pin, the power claim.
   *  Null when nobody has recorded anything — which is the common case on a
   *  fleet that has just started surveying, and reads as "no record yet", never
   *  as an error. */
  record: SubscriberRecord | null
  /** false = this MAC is in no current roster. An RMA'd box, reported rather
   *  than hidden: a record that quietly stopped describing anything is the one
   *  failure this panel must not conceal. */
  matched: boolean
  /** on more than one live slot, so the server refuses to say which OLT it
   *  belongs to — picking a winner sends a tech to the wrong house. */
  ambiguous: boolean
  slots: number
  /** the freshest-walk roster row, byte-for-byte what the Optical tab shows for
   *  the same slot. Null when `matched` is false or `ambiguous` is true. */
  roster: OnuOptic | null
  olt: {
    id: number
    name: string | null
    /** the OLT's ICMP state, for the FROZEN rule — an unreachable box proves its
     *  readings are stale up to 15 min before staleness would notice, so every
     *  reading below it must look frozen rather than green. */
    state: string | null
    /** the OPTICS walk's own stamp, never `port_updated_at` */
    optics_updated_at: string | null
  } | null
  /** the plant this drop hangs off: the subscriber's own splitter first, then
   *  any cascade, ending at the OLT. Null = nobody recorded a serving splitter,
   *  which the panel says outright rather than implying direct fibre. */
  drop: { passive_id: number; chain: SubscriberPlantHop[] } | null
  /** the ONU's OWN ifTable row, never the PON aggregate (shared by up to 64
   *  subscribers). Null on vendors whose firmware names no per-ONU interface —
   *  a different sentence from "the walk is stale", and the panel says which. */
  rate: {
    if_name: string | null
    port_state: string | null
    in_bps: number | null
    out_bps: number | null
    updated_at: string | null
  } | null
  /** the OLT's own thresholds. Present so nothing re-grades a reading against a
   *  global default the box doesn't use. */
  thresholds: { warn_dbm: number; crit_dbm: number } | null
}

export interface SubscriberRecord {
  label: string | null
  phone: string | null
  notes: string | null
  /** a REFERENCE ONU: the operator's claim that this subscriber's power is
   *  reliable. Retracted when the pin is cleared — placing is what makes the
   *  claim, so unplacing is what takes it back. */
  witness: boolean
  /** null = recorded from the desk (or the pin was cleared). The record itself
   *  no longer needs a coordinate. */
  lat: number | null
  lng: number | null
  accuracy_m: number | null
  place_source: "gps" | "manual" | null
  placed_by: string | null
  placed_at: string | null
  created_at: string
  updated_at: string
}

/** One box between a subscriber and its OLT. `split_ratio` null on the OLT
 *  itself, and on a passive nobody has told us the ratio for — which is why
 *  `cumulativeSplit` refuses to multiply a partial chain. */
export interface SubscriberPlantHop {
  id: number
  name: string | null
  device_type: string | null
  split_ratio: number | null
  pon_port: string | null
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

/** `assigned` is NAMED BUT UNANSWERED: the owner has sent it to somebody and
 *  nobody has said yes yet. It renders as down, because it is — an outage only
 *  reaches `in_progress` when a human accepts (or acknowledges) it. */
export type OutageStatus =
  "unassigned" | "assigned" | "in_progress" | "pending_postmortem"

/** The issue vocabulary — mirrors `central/issues.py:KINDS` one for one. Each
 *  kind is exactly one Home KPI tile's worth of trouble, so a tile can hand its
 *  own kind over as a filter and the list can't describe a different set. */
export type IssueKind =
  | "device_down" | "port_down" | "probe_stale" | "bandwidth"
  | "onu_crit" | "onu_warn" | "dup_mac" | "pon_fiber" | "pon_power"
  | "pon_capacity" | "onu_offline"

export type IssueSeverity = "critical" | "warning" | "info"

/** One PROBLEM, not one device: a dark port, a weak ONU, a silent probe. The
 *  server composes these from the same reads the tiles use — see
 *  `central/issues.py`. */
export interface Issue {
  kind: IssueKind
  kind_label: string
  severity: IssueSeverity
  /** null for a probe: a probe isn't a row in the device tree. */
  device_id: number | null
  device_name: string
  region: string | null
  subject: string
  detail: string
  since: string | null
}

export interface IssuesResponse {
  issues: Issue[]
  /** Per-kind totals over the UNFILTERED list, so a chip can say how many rows
   *  it would show before it is clicked. */
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
  /** Usernames the owner sent to this outage. Always a real array (the server
   *  decodes the stored JSON), so `.length === 0` is the whole test for
   *  "nobody has been named yet". */
  assigned_to: string[]
  assigned_at: string | null
  assigned_by: string | null
  /** Which of `assigned_to` have said yes — always a subset, always a real
   *  array. Empty while an assignment is still an unanswered ask. */
  accepted_by: string[]
  /** When the FIRST acceptance landed. */
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
  // superadmin ops recipient (org 'I've paid' / churn / release-sync failing)
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

// ----- paging responsibility (device → field accounts) -----------------------
// Assignment narrows WHO gets paged about a device; it never changes what any
// session can see (operator choice 2026-07-26). Server side: central/assignment.py.

/** One row: this account is explicitly responsible for this device. */
export interface DeviceAssignment {
  device_id: number
  user_id: number
  username: string
  role: Role
  /** a deactivated account keeps its row so an operator can see and clear it,
      but it pages nobody and does NOT count as "somebody is responsible" */
  is_active: boolean
  /** whether a page could actually reach them — the number itself never ships */
  has_whatsapp: boolean
  assigned_by: string | null
  assigned_at: string | null
}

/** One assignable account, with both counts the screen needs: `assigned` is rows
    ticked, `devices` is the inherited reach. One click on a region head makes
    those numbers very different, and showing only the first makes it look like
    nothing happened. */
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
  /** devices no row covers, directly or by inheritance — these still page every
      worker, which is the safe default, but an operator who thinks assignment is
      finished needs to see the number rather than infer it */
  unassigned: number
}

// ----- worker location tracking ----------------------------------------------
// Workers run the off-the-shelf Traccar Client, which POSTs OsmAnd fixes to the
// public /field/track ingest. Central stores a live position plus a short trail
// on a 7-day clock. Server side: central/field.py, central/store_field.py.

/** One fix as stored. `speed_mps` is SI — the wire carries knots (the OsmAnd
 *  protocol's unit) and central converts once, at ingest. */
export interface WorkerFix {
  ts: string
  lat: number
  lng: number
  accuracy_m: number | null
  speed_mps: number | null
  heading: number | null
  battery_pct: number | null
}

/** One account's tracking state, as FACTS. The four map states are derived from
 *  these in `map/workers.ts`, never shipped — freshness ticks with the clock, so
 *  a state stamped at response time would go on claiming "here now" for as long
 *  as the tab stayed open. */
export interface FieldWorker {
  user_id: number
  username: string
  role: Role
  /** a live tracker credential exists. Without one, "on shift" is a declaration
   *  nothing can corroborate. */
  has_token: boolean
  last_fix: WorkerFix | null
  /** today's route, oldest → newest, in the operator's own timezone's day */
  trail: Array<[number, number]>
  shift_started_at: string | null
  shift_ended_at: string | null
  on_shift: boolean
}

export interface FieldWorkersResponse {
  workers: FieldWorker[]
  trail_since: string
  /** how old a fix may be and still count as "here now". Server-owned so the
   *  threshold has ONE source even though the classification is client-side. */
  fresh_s: number
  retention_days: number
}

/** An org account and its tracker-credential state. The token itself is shown
 *  once at issue and is not recoverable — only "issued <date>" survives. */
export interface FieldAccount {
  user_id: number
  username: string
  role: Role
  issued_at: string | null
  revoked_at: string | null
}

export interface FieldTokensResponse {
  accounts: FieldAccount[]
  /** identical for every worker — the token rides Traccar's `id` field, which is
   *  the whole reason there is one string to put on screen and in a QR */
  server_url: string
  retention_days: number
}

export interface ShiftState {
  on_shift: boolean
  started_at: string | null
  ended_at: string | null
  has_token: boolean
}
