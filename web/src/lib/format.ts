import type { DeviceState } from "./types"

export function toUtcDate(ts: string): Date {
  let s = ts.trim().replace(" ", "T")
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += "Z"
  return new Date(s)
}

export function ago(ts: string | null | undefined): string {
  if (!ts) return "—"
  const s = Math.max(0, (Date.now() - toUtcDate(ts).getTime()) / 1000)
  if (s < 90) return `${s | 0}s ago`
  if (s < 5400) return `${(s / 60) | 0}m ago`
  if (s < 172800) return `${(s / 3600) | 0}h ago`
  return `${(s / 86400) | 0}d ago`
}

export function fmtDateTime(ts: string | null | undefined): string {
  if (!ts) return "—"
  return toUtcDate(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  })
}

export function fmtDur(seconds: number): string {
  seconds = Math.max(0, Math.floor(seconds))
  const dd = Math.floor(seconds / 86400)
  if (dd) {
    const hhRem = Math.floor((seconds % 86400) / 3600)
    return hhRem ? `${dd}d ${hhRem}h` : `${dd}d`
  }
  const hh = Math.floor(seconds / 3600)
  const mm = Math.floor((seconds % 3600) / 60)
  const ss = seconds % 60
  if (hh) return mm ? `${hh}h ${mm}m` : `${hh}h`
  if (mm) return ss ? `${mm}m ${ss}s` : `${mm}m`
  return `${ss}s`
}

export function durationSince(ts: string | null | undefined): string {
  if (!ts) return "—"
  return fmtDur((Date.now() - toUtcDate(ts).getTime()) / 1000)
}

export function fmtMbps(n: number | null | undefined): string {
  return n == null ? "—" : `${n} Mbps`
}

export function fmtPct(n: number | null | undefined): string {
  return n == null ? "—" : `${Number(n).toFixed(1)}%`
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

export function stateTone(state: DeviceState | string | null | undefined):
  "success" | "warning" | "destructive" | "muted" {
  switch (state) {
    case "UP": return "success"
    case "DEGRADED": return "warning"
    case "DOWN":
    case "UNREACHABLE": return "destructive"
    default: return "muted"
  }
}

// "The box is not answering" — DOWN is the FSM's verdict, UNREACHABLE the
// topology override for a device orphaned behind a down parent. Mirrors
// DOWN_FAMILY in core/state_machine.py; keep the two in step.
// Load-bearing beyond tone: a device in this family cannot be walked, so every
// SNMP reading still on screen for it is a frozen snapshot (see .wisp-frozen).
export function isDownState(state: DeviceState | string | null | undefined): boolean {
  return state === "DOWN" || state === "UNREACHABLE"
}

export const STALE_AFTER_S = 180

export function isStale(ts: string | null | undefined): boolean {
  if (!ts) return true
  return (Date.now() - toUtcDate(ts).getTime()) / 1000 > STALE_AFTER_S
}

// SNMP optics/port sweeps run far slower than the ICMP cadence, so "working"
// mirrors the superadmin Overview: a reading fresher than 900s counts as live.
export const SNMP_FRESH_AFTER_S = 900

export function isFresh(ts: string | null | undefined, withinS = SNMP_FRESH_AFTER_S): boolean {
  if (!ts) return false
  return (Date.now() - toUtcDate(ts).getTime()) / 1000 <= withinS
}

/** What to CALL one ONU. Mirrors `central/onuroster.py:display_name` — keep the
 *  two in step, since a WhatsApp lookup and the Optical tab naming the same
 *  subscriber differently is a support call.
 *
 *  `label` is the operator's own name, typed in the field survey or the
 *  reference-ONU dialog and stored in `onu_places` (uppercase). It WINS over
 *  `name`, which is whatever the OLT reports and which every SNMP walk
 *  overwrites — on the C-Data fleet that column is blank, so the operator's name
 *  is usually the only one there is. Rendering `o.name` alone is what made a
 *  freshly-surveyed drop read "unnamed" on the very OLT that carries it. */
export function onuName(o: {
  label?: string | null; name?: string | null
  serial?: string | null; onu_key?: string | null
}): string {
  return o.label || o.name || o.serial || o.onu_key || ""
}

/** How to COMPARE one ONU string against a typed needle. Mirrors
 *  `central/onuroster.py:search_key` — alphanumerics only, upper — so a client
 *  filter and the server's `onu-search` agree about what matches: "a4:f2",
 *  "A4-F2" and "a4f2" are one sticker, and "hc_kiran" is found by "hc kiran".
 *
 *  SEARCH ONLY. Identity stays separator-exact (`onuroster._norm_mac`): two
 *  differently-punctuated strings collapsing into one here is a convenience,
 *  collapsing them on the write path fabricates duplicate-MAC pages. */
export const onuSearchKey = (s: string | null | undefined): string =>
  (s ?? "").replace(/[^a-z0-9]/gi, "").toUpperCase()

export function deviceTone(
  state: DeviceState | string | null | undefined,
  stateUpdatedAt: string | null | undefined,
): "success" | "warning" | "destructive" | "muted" {
  if (state && isStale(stateUpdatedAt)) return "muted"
  return stateTone(state)
}
