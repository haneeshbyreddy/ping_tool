import type L from "leaflet"
import { coresRecordedLabel } from "@/lib/fiber"
import type { Cable, FibrePoint } from "@/lib/types"
import { cachedDivIcon, esc } from "@/map/pins"

export type LatLng = [number, number]

const flat = (p: LatLng, lat0: number): LatLng =>
  [p[1] * Math.cos((lat0 * Math.PI) / 180), p[0]]

const d2 = (a: LatLng | null, b: LatLng, lat0: number) => {
  if (!a) return 0
  const [ax, ay] = flat(a, lat0)
  const [bx, by] = flat(b, lat0)
  return (ax - bx) ** 2 + (ay - by) ** 2
}

export function orient(path: LatLng[], a: LatLng | null, b: LatLng | null): boolean {
  if (path.length < 2) return true
  const lat0 = path[0][0]
  const head = path[0], tail = path[path.length - 1]
  return d2(a, head, lat0) + d2(b, tail, lat0)
      <= d2(a, tail, lat0) + d2(b, head, lat0)
}

export type PinOf = (p: FibrePoint) => LatLng | null

export function cablePolyline(cable: Cable, pinOf: PinOf): LatLng[] {
  const a = pinOf(cable.a)
  const b = pinOf(cable.b)
  const path = (cable.path ?? []) as LatLng[]
  if (path.length < 2) return a && b ? [a, b] : []
  const forward = orient(path, a, b)
  const route = forward ? path : [...path].reverse()
  return [...(a ? [a] : []), ...route, ...(b ? [b] : [])]
}

export const cableTraced = (cable: Cable) => (cable.path?.length ?? 0) >= 2

export const CABLE_DASH = "2 9"

export function cableLabelPos(pts: LatLng[]): LatLng {
  if (pts.length < 2) return pts[0] ?? [0, 0]
  const lat0 = pts[0][0]
  const seg: number[] = []
  let total = 0
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = flat(pts[i - 1], lat0)
    const [bx, by] = flat(pts[i], lat0)
    const d = Math.hypot(bx - ax, by - ay)
    seg.push(d)
    total += d
  }
  let want = total / 2
  for (let i = 0; i < seg.length; i++) {
    if (want <= seg[i] || i === seg.length - 1) {
      const t = seg[i] ? want / seg[i] : 0
      return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
              pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t]
    }
    want -= seg[i]
  }
  return pts[0]
}

export function cableIcon(cable: Cable): L.DivIcon {
  const traced = cableTraced(cable)
  const bits = [
    `${cable.a.name ?? "?"} ↔ ${cable.b.name ?? "?"}`,
    traced && cable.length_m != null
      ? `${Math.round(cable.length_m)} m along the route`
      : "route not traced — drawn straight",
    coresRecordedLabel(cable.cores_recorded, cable.cores) || null,
  ].filter(Boolean)
  return cachedDivIcon(
    `<div class="wisp-linkbw wisp-cablechip" title="${esc(bits.join(" · "))}">`
    + `<span class="wisp-cablechip__n">${esc(cable.name)}</span>`
    + (cable.cores
        ? `<span class="wisp-cablechip__f">${cable.cores}F</span>` : "")
    + `</div>`)
}
