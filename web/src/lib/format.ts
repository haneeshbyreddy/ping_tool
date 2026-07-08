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

export const STALE_AFTER_S = 180

export function isStale(ts: string | null | undefined): boolean {
  if (!ts) return true
  return (Date.now() - toUtcDate(ts).getTime()) / 1000 > STALE_AFTER_S
}

export function deviceTone(
  state: DeviceState | string | null | undefined,
  stateUpdatedAt: string | null | undefined,
): "success" | "warning" | "destructive" | "muted" {
  if (state && isStale(stateUpdatedAt)) return "muted"
  return stateTone(state)
}
