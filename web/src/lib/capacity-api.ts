// BUSY-HOUR CAPACITY: the wire types and the two helpers that must not be
// re-implemented per screen (notes/viz-plan.md Wave 2, chart E).
//
// Everything here is a CAPACITY fact. Nothing in this file grades, tones or
// alarms: a port pinned near its ceiling is a purchase decision, and painting
// that list red would fabricate alarms the product never raised. The only
// status claims the panels make are the flags ports.py already set
// (`alarm`, `bw_high_alarm`), echoed verbatim off the row.
import { aq, request } from "@/lib/api"

// -- wire ---------------------------------------------------------------------

export interface CapacityWindow {
  since: string
  until: string
  /** the window actually served (bounded by the hour tier's retention) */
  days: number
  days_requested: number
  max_days: number
  clamped: boolean
  recording_since: string | null
}

export interface HeatCell {
  /** UTC hour of day */
  h: number
  in_bps: number | null
  out_bps: number | null
  /** the direction-resolved mean the server shades on (heatmap rows only) */
  bps?: number | null
  peak_in_bps: number | null
  peak_out_bps: number | null
  /** samples that HAD a rate; a cell only exists when this is > 0 */
  n: number
  /** distinct UTC days that contributed to this hour of the clock */
  days: number
  samples: number
}

export interface CapacityRow {
  device_id: number
  if_index: number
  device_name: string | null
  device_type: string | null
  region: string | null
  device_state: string | null
  if_name: string | null
  if_alias: string | null
  label: string
  monitored: 0 | 1
  feeds_device_id: number | null
  uplink_device_id: number | null
  admin_status: string | null
  oper_status: string | null
  alarm: 0 | 1
  bw_alarm: 0 | 1
  bw_high_alarm: 0 | 1
  bw_max_mbps: number | null
  bw_threshold_mbps: number | null
  bw_direction: string
  updated_at: string | null
  /** the busiest hour's mean, on the direction this port is judged on */
  busy_bps: number | null
  busy_hour: number | null
  /** null when no ceiling is recorded — never a faked denominator */
  util_pct: number | null
  busy_in_bps: number | null
  busy_in_hour: number | null
  busy_out_bps: number | null
  busy_out_hour: number | null
  peak_in_bps: number | null
  peak_out_bps: number | null
  /** distinct UTC days this port was sampled on (the coverage channel) */
  days: number
  /** hour buckets behind those days */
  hour_buckets: number
  samples: number
  /** samples that HAD a rate; 0 means walked but nothing computable */
  rate_n: number
  up_samples: number
  first_bucket: number | null
  last_bucket: number | null
}

export interface HeatRow {
  device_id: number
  if_index: number
  cells: HeatCell[]
}

export interface CapacityReply extends CapacityWindow {
  eligible: number
  sampled: number
  no_ceiling: number
  heatmap_ports: number
  ranking: CapacityRow[]
  heatmap: HeatRow[]
}

export interface PortDay {
  day: number
  samples: number
  rate_n: number
  up_samples: number
  busy_in_bps: number | null
  busy_in_hour: number | null
  busy_out_bps: number | null
  busy_out_hour: number | null
}

export interface PortHistoryReply extends CapacityWindow {
  device_id: number
  if_index: number
  if_name: string | null
  if_alias: string | null
  label: string
  device_name: string | null
  bw_max_mbps: number | null
  bw_threshold_mbps: number | null
  bw_direction: string
  /** the busiest hour's mean on the declared direction, resolved server-side */
  busy_bps: number | null
  busy_hour: number | null
  /** null when no ceiling is recorded — never a faked denominator */
  util_pct: number | null
  hours: HeatCell[]
  series: PortDay[]
  busy_in_bps: number | null
  busy_in_hour: number | null
  busy_out_bps: number | null
  busy_out_hour: number | null
  peak_in_bps: number | null
  peak_out_bps: number | null
  days_covered: number
  rate_n: number
  samples: number
  up_samples: number
}

export const capacityApi = {
  org: (org: string | null | undefined, days = 30) =>
    request<CapacityReply>(`/api/history/capacity?days=${days}${aq(org)}`),
  port: (deviceId: number, ifIndex: number, days = 30) =>
    request<PortHistoryReply>(
      `/api/history/port?device_id=${deviceId}&if_index=${ifIndex}&days=${days}`),
}

// -- the rules -----------------------------------------------------------------

// MIRRORS central/history.py:port_eligible — the one rule for which ports the
// historian samples at all. The SPA needs it BEFORE it offers a drill, or the
// Ports tab hands 28 interfaces a button that leads to an empty panel.
// Pinned against the Python by tests/unit/test_capacity.py:SpaAgreementTest.
export function portRecords(p: {
  monitored: 0 | 1
  feeds_device_id: number | null
  uplink_device_id: number | null
  bw_threshold_mbps: number | null
  bw_max_mbps: number | null
}): boolean {
  return !!p.monitored
    || p.feeds_device_id != null
    || p.uplink_device_id != null
    || p.bw_threshold_mbps != null
    || p.bw_max_mbps != null
}

// Which half of the traffic `busy_bps` came from. The ranking is SORTED on
// busy_bps, so the arrow beside it has to name the same direction the sort
// used, or a row is ordered by a number the screen never shows — the same
// count-agreement rule the heatmap's shaded cell keeps.
export function busyArrow(row: {
  busy_bps: number | null
  busy_out_bps: number | null
  bw_direction: string
}): string {
  if (row.bw_direction === "total") return "↕"
  if (row.busy_bps != null && row.busy_out_bps != null
      && row.busy_bps === row.busy_out_bps) return "↑"
  return "↓"
}

// HOW FULL IS FULL (operator's ask, 2026-08-17: "colour indication … red above
// 90%"). ONE ladder, read by the Home ranking AND by the per-port drill, so the
// same percentage can never be graded two ways on two screens — the pin-vs-card
// rule. Deliberately coarse: a capacity figure earns a tone for crossing a
// threshold, not for every point it moves.
//
// It grades the OPERATOR'S OWN CEILING (`bw_max_mbps`), never a modelled one —
// a port with no ceiling recorded gets no bar and no tone, because "we don't
// know what full is here" is a different sentence from "there is room". That
// refusal is what keeps this honest while it borrows the status tones: the
// arithmetic is only as much of a claim as the number the operator typed.
export type UtilStage = "ok" | "watch" | "full"

export const UTIL_WATCH_PCT = 70
export const UTIL_FULL_PCT = 90

export function utilStage(pct: number | null | undefined): UtilStage | null {
  if (pct == null) return null
  if (pct >= UTIL_FULL_PCT) return "full"
  if (pct >= UTIL_WATCH_PCT) return "watch"
  return "ok"
}

// The panels' rate vocabulary. Identical to map/linklabel.ts:fmtFull, which is
// deliberately not imported: that module pulls leaflet in, and Home must not
// carry the map bundle. Keep the two in step if either ever changes.
export function fmtRate(bps: number | null | undefined): string {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`
  return `${Math.round(bps)} b/s`
}

// -- hours of the clock --------------------------------------------------------
//
// Buckets are UTC hours; an operator reads a wall clock, and "the evening
// peak" is a LOCAL sentence. So a cell is labelled with the local time that
// UTC hour BEGINS — in a half-hour zone (IST is +5:30) that reads 19:30, which
// is the truth rather than a rounding — and the axis is rotated to start at
// the local day, so the shape reads as a day instead of wrapping mid-plot.
// This is the epoch-hour trap the HourStrip already documents, one level up:
// the cells stay UTC, only the presentation moves.

export interface HourSlot {
  /** the UTC hour this column holds */
  h: number
  /** minutes past local midnight where that hour begins (the sort key) */
  mins: number
  label: string
}

function localAt(refMs: number, utcHour: number): Date {
  const d = new Date(refMs)
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())
    + utcHour * 3_600_000)
}

export function hourLabel(utcHour: number | null | undefined,
                          refMs: number): string {
  if (utcHour == null) return "—"
  return localAt(refMs, utcHour).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", hour12: false,
  })
}

// The offset is read once, at the window's end: a zone that changed offset
// mid-window would shift the axis by an hour, which is a smaller lie than
// re-basing every cell against a date it did not come from.
export function hourSlots(refMs: number): HourSlot[] {
  const slots: HourSlot[] = []
  for (let h = 0; h < 24; h++) {
    const at = localAt(refMs, h)
    slots.push({ h, mins: at.getHours() * 60 + at.getMinutes(),
                 label: hourLabel(h, refMs) })
  }
  return slots.sort((a, b) => a.mins - b.mins)
}
