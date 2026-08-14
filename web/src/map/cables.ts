import type L from "leaflet"
import { coresRecordedLabel, isPlumbing } from "@/lib/fiber"
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

// `rate` is the reading off a DEPENDENCY LINK whose glass this sheath carries. Once
// the fibre record joins a pair the chord stands down — the cable is the better line,
// drawn where somebody walked it — so its ↓/↑ moves here rather than being lost. It
// rides the BIGGEST sheath on the run (`fiber.connected_spans`), because the tails
// either side of a trunk are our own 26 m plumbing and unreadable at trunk zoom.
export function cableIcon(
  cable: Cable,
  rate?: { html: string; title: string; idle: boolean; tone: string | null } | null,
): L.DivIcon {
  const traced = cableTraced(cable)
  // PLUMBING CARRIES THE RATE AND NOTHING ELSE — see below. Its title sheds the cable
  // furniture with the chip: `rate.title` already names both ends AND their ports, so
  // the cable's own ends line would say the same thing again, less precisely.
  const bare = !!rate && isPlumbing(cable)
  const bits = (bare ? [rate!.title] : [
    `${cable.a.name ?? "?"} ↔ ${cable.b.name ?? "?"}`,
    traced && cable.length_m != null
      ? `${Math.round(cable.length_m)} m along the route`
      : "route not traced — drawn straight",
    coresRecordedLabel(cable.cores_recorded, cable.cores) || null,
    rate?.title || null,
  ]).filter(Boolean)
  const cls = ["wisp-linkbw", "wisp-cablechip"]
  // The TONE is the link's, not the cable's — a sheath has no state of its own, and
  // the tone here says the same thing it said on the chord it replaced.
  if (rate?.tone) cls.push(`wisp-linkbw--${rate.tone}`)
  else if (rate?.idle) cls.push("wisp-linkbw--idle")
  // PLUMBING CARRIES THE RATE AND NOTHING ELSE. A cable nobody laid is never labelled
  // for its own sake, and `· 1F` beside a reading is this codebase's row numbering read
  // aloud to somebody asking about their network — the very thing `is_plumbing` exists
  // to stop. It reaches the map only because it is the one line left under a chord that
  // stood down, so it draws exactly what that chord was drawing.
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${esc(bits.join(" · "))}">`
    + (bare ? "" : `<span class="wisp-cablechip__n">${esc(cable.name)}</span>`)
    + (!bare && cable.cores
        ? `<span class="wisp-cablechip__f">${cable.cores}F</span>` : "")
    + (rate ? `${bare ? "" : `<span class="wisp-linkbw__sep"></span>`}${rate.html}` : "")
    + `</div>`)
}
