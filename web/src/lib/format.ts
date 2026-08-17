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

export function fmtMs(v: number): string {
  return v < 10 ? v.toFixed(1) : String(Math.round(v))
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

export function isDownState(state: DeviceState | string | null | undefined): boolean {
  return state === "DOWN" || state === "UNREACHABLE"
}

export const STALE_AFTER_S = 180

export function isStale(ts: string | null | undefined): boolean {
  if (!ts) return true
  return (Date.now() - toUtcDate(ts).getTime()) / 1000 > STALE_AFTER_S
}

export const SNMP_FRESH_AFTER_S = 900

export function isFresh(ts: string | null | undefined, withinS = SNMP_FRESH_AFTER_S): boolean {
  if (!ts) return false
  return (Date.now() - toUtcDate(ts).getTime()) / 1000 <= withinS
}

// MIRRORS onuroster.display_name — read the reasoning there; in short, the
// RADIUS username is the identity the ISPs use ("everybody recognise the user
// by username only", 2026-08-17) and the customer name is extra info, while the
// operator's own typed label still outranks both.
export interface OnuIdentity {
  label?: string | null; radius_username?: string | null
  radius_name?: string | null; name?: string | null
  serial?: string | null; onu_key?: string | null
}

const NAME_ORDER = ["label", "radius_username", "radius_name", "name",
                    "serial", "onu_key"] as const

// Which field won. Exported so a surface can style the headline without
// re-deriving the ranking — two rules would drift the first time one moves.
export function onuNameSource(o: OnuIdentity): typeof NAME_ORDER[number] | null {
  for (const k of NAME_ORDER) if (o[k]) return k
  return null
}

export function onuName(o: OnuIdentity): string {
  const k = onuNameSource(o)
  return k ? String(o[k]) : ""
}

// True when the headline is a KEY somebody retypes (a username, a serial, a
// slot) rather than prose about a person. Mono is this app's mark for that —
// same reason a port renders GE0/5 in mono and its alias in sans — and it also
// warns not to case-fold the string the way a survey label is folded.
export function onuNameIsKey(o: OnuIdentity): boolean {
  const k = onuNameSource(o)
  return k === "radius_username" || k === "serial" || k === "onu_key"
}

// Every identity this ONU has, each said to be WHOSE it is. For a dense row
// that can print only the headline: the rest belongs somewhere reachable, or a
// subscriber renamed by billing looks like a different customer to whoever
// remembers the OLT's string.
const NAME_WHOSE: Record<string, string> = {
  label: "recorded in the field",
  radius_username: "billing account",
  radius_name: "account holder",
  name: "the OLT calls it",
}

export function onuIdentityTitle(o: OnuIdentity): string {
  const seen = new Set<string>()
  const parts: string[] = []
  for (const k of ["label", "radius_username", "radius_name", "name"] as const) {
    const v = o[k]
    if (!v || seen.has(onuSearchKey(v))) continue
    seen.add(onuSearchKey(v))
    parts.push(`${v} (${NAME_WHOSE[k]})`)
  }
  return parts.join(" · ") || "unnamed"
}

// The SECOND line: what is left worth saying once the headline is chosen.
// A caller that prints `onuName` big and this small can never print one string
// twice — the failure the customers page had when both lines could resolve to
// the username. Returns "" when identity has nothing to add.
export function onuSubName(o: {
  label?: string | null; radius_username?: string | null
  radius_name?: string | null; name?: string | null
}): string {
  const head = onuSearchKey(onuName(o))
  // The USERNAME first among the leftovers: where a worker's label won the
  // headline, the identifier everyone recognises is the thing still worth a
  // line ("AMAZON OFFICE · smamazon"), and the account holder's name is one
  // section further down. Where the label already IS the username, it is
  // search-key equal and skipped, so the name gets the line instead.
  for (const v of [o.radius_username, o.radius_name, o.name]) {
    if (v && onuSearchKey(v) !== head) return v
  }
  return ""
}

export type OnuSev = "ok" | "warn" | "crit" | "offline"

export function onuSev(
  o: { state?: string | null; severity?: string | null },
): OnuSev {
  if (o.state !== "online") return "offline"
  if (o.severity === "crit") return "crit"
  if (o.severity === "warn") return "warn"
  return "ok"
}

export const onuSearchKey = (s: string | null | undefined): string =>
  (s ?? "").replace(/[^a-z0-9]/gi, "").toUpperCase()

export function deviceTone(
  state: DeviceState | string | null | undefined,
  stateUpdatedAt: string | null | undefined,
): "success" | "warning" | "destructive" | "muted" {
  if (state && isStale(stateUpdatedAt)) return "muted"
  return stateTone(state)
}

export const NO_ASSIGNED_DEVICES =
  "No devices are assigned to you yet. Ask your network owner to assign the ones you are responsible for."
