import { cachedDivIcon, esc } from "@/map/pins"
import { ago } from "@/lib/format"
import type { FieldWorker } from "@/lib/types"

export type WorkerState = "live" | "quiet" | "off" | "never"

export function workerState(w: FieldWorker, freshS: number, now: number): WorkerState {
  if (!w.last_fix) return w.on_shift ? "quiet" : "never"
  if (!w.on_shift) return "off"
  const age = (now - Date.parse(w.last_fix.ts)) / 1000
  return Number.isFinite(age) && age <= freshS ? "live" : "quiet"
}

export const workerPlaced = (w: FieldWorker): boolean => w.last_fix != null

export function workerZIndex(state: WorkerState): number {
  return state === "quiet" ? -250 : -300
}

export function workerInitials(username: string): string {
  const parts = username.split(/[\s._-]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return (parts[0] ?? username).slice(0, 2).toUpperCase()
}

export function workerTitle(w: FieldWorker, state: WorkerState): string {
  const seen = w.last_fix ? ago(w.last_fix.ts) : "never"
  const batt = w.last_fix?.battery_pct != null ? ` · battery ${w.last_fix.battery_pct}%` : ""
  const acc = w.last_fix?.accuracy_m != null ? ` · ±${Math.round(w.last_fix.accuracy_m)} m` : ""
  if (state === "live") return `${w.username} · here now (${seen})${acc}${batt}`
  if (state === "quiet")
    return `${w.username} · on shift but GONE QUIET · last fix ${seen}. `
      + `Phone off, no signal, or the handset's battery manager killed the tracker${batt}`
  if (state === "off") return `${w.username} · shift ended · last seen ${seen}${acc}`
  return `${w.username} · never reported`
}

export function workerIcon(w: FieldWorker, state: WorkerState) {
  return cachedDivIcon(
    `<div class="wisp-worker wisp-worker--${state}" title="${esc(workerTitle(w, state))}">`
    + `<span class="wisp-worker__mark">${esc(workerInitials(w.username))}</span></div>`)
}

export function trailStyle(state: WorkerState): { weight: number; opacity: number } {
  if (state === "live") return { weight: 3, opacity: 0.7 }
  if (state === "quiet") return { weight: 3, opacity: 0.6 }
  return { weight: 2.5, opacity: 0.45 }
}

export function workerCensus(workers: FieldWorker[], freshS: number, now: number) {
  let live = 0, quiet = 0, off = 0, never = 0, placed = 0
  for (const w of workers) {
    const s = workerState(w, freshS, now)
    if (s === "live") live++
    else if (s === "quiet") quiet++
    else if (s === "off") off++
    else never++
    if (workerPlaced(w)) placed++
  }
  return { live, quiet, off, never, total: workers.length, placed }
}
