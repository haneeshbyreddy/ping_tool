// The chart kit's math substrate: d3-scale/d3-shape/d3-array compute, React
// owns every SVG element (notes/viz-plan.md Stage 2). UTC in, the viewer's
// locale out — the same split the rest of the app keeps.
import { scaleLinear, scaleUtc } from "d3-scale"
import type { ScaleLinear, ScaleTime } from "d3-scale"

export const HOUR_MS = 3_600_000
export const DAY_MS = 86_400_000
export const WEEK_MS = 7 * DAY_MS

export type XScale = ScaleTime<number, number>
export type YScale = ScaleLinear<number, number>

export function timeScale(domain: [number, number], range: [number, number]): XScale {
  return scaleUtc().domain([new Date(domain[0]), new Date(domain[1])]).range(range)
}

export function linearScale(domain: [number, number], range: [number, number]): YScale {
  return scaleLinear().domain(domain).range(range).nice()
}

// Tick labels stay short: a chart axis is recessive furniture, not a table.
export function fmtTime(d: Date, spanMs: number): string {
  if (spanMs <= 2 * DAY_MS) {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
  }
  if (spanMs <= 200 * DAY_MS) {
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short" })
  }
  return d.toLocaleDateString(undefined, { month: "short", year: "2-digit" })
}

export function fmtDay(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, {
    day: "numeric", month: "short", year: "numeric",
  })
}

// Duration for tooltips/legends: "3h 20m", "45m", "12s".
export function fmtDurS(seconds: number | null | undefined): string {
  if (seconds == null) return "—"
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  const rm = m % 60
  if (h < 48) return rm ? `${h}h ${rm}m` : `${h}h`
  return `${Math.round(h / 24)}d`
}

export function epochDayMs(ms: number): number {
  return Math.floor(ms / DAY_MS) * DAY_MS
}
