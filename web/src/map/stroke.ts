export const LINE_SCALE_FROM = 13
export const LINE_SCALE_PER_ZOOM = 0.135
export const LINE_SCALE_MAX = 1.85

export function lineScale(zoom: number): number {
  return Math.min(
    LINE_SCALE_MAX,
    Math.max(1, 1 + (zoom - LINE_SCALE_FROM) * LINE_SCALE_PER_ZOOM))
}

const round = (n: number) => Math.round(n * 100) / 100

export function scaleDash(dash: string | undefined, k: number): string | undefined {
  if (!dash) return undefined
  return dash.trim().split(/\s+/).map((n) => round(Number(n) * k)).join(" ")
}

export function casingDash(dash: string | undefined, over: number): string | undefined {
  if (!dash) return undefined
  const [on, off] = dash.split(" ").map(Number)
  return `${on + over} ${Math.max(off - over, 1)}`
}

export const CASING_OPACITY = 0.55
export const CASING_OPACITY_HOVER = 0.68

export const FIBER_BOOST_PER_DOUBLING = 0.3
export const FIBER_BOOST_MAX = 2

export function fiberBoost(cores: number | null | undefined): number {
  if (!cores || cores < 2) return 0
  return round(Math.min(FIBER_BOOST_MAX,
                        FIBER_BOOST_PER_DOUBLING * Math.log2(cores)))
}

export interface Stroke {
  weight: number
  dashArray?: string
}

export const strokeAt = (k: number, weight: number, dash?: string): Stroke =>
  ({ weight: round(weight * k), dashArray: scaleDash(dash, k) })

export const casingAt = (
  k: number, weight: number, over: number, dash?: string,
): Stroke => ({
  weight: round((weight + over) * k),
  dashArray: scaleDash(casingDash(dash, over), k),
})
