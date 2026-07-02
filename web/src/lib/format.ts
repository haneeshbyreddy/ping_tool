// Small formatting helpers, ported from the old static/app.js so relative-time/duration
// behavior matches exactly (same UTC-parsing gotcha: stamps are ISO with +00:00 except
// SQLite's space-separated `datetime('now')` on ack timestamps — see CLAUDE.md).
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
  return `${(s / 3600) | 0}h ago`
}

export function fmtDur(seconds: number): string {
  seconds = Math.max(0, Math.floor(seconds))
  const hh = Math.floor(seconds / 3600)
  const mm = Math.floor((seconds % 3600) / 60)
  const ss = seconds % 60
  if (hh) return `${hh}h ${mm}m`
  if (mm) return `${mm}m ${ss}s`
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

// UP/DOWN/DEGRADED/UNREACHABLE -> semantic tone, matching core/state_machine.py's
// DOWN_FAMILY = {DOWN, UNREACHABLE} and the mockup's healthy/warning/danger tokens.
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
