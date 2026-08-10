// THE CABLE ON THE MAP: one sheath segment, drawn from pin to pin through
// whatever route somebody walked.
//
// A cable knows its own two ends now, so this is the one place that turns those
// three facts — end A, the traced street, end B — into a line. Doing it once is
// the same discipline `list_link_routes` keeps server-side: the line the browser
// draws and the length central reports have to be the same line.
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

/** TRUE when end `a` belongs to `path[0]` — mirrored from `cablepath.orient`.
 *
 *  Measured rather than stored, on the TOTAL of the two stubs and never on
 *  either alone. A pin can easily be nearer the wrong end of a street that
 *  doubles back, and deciding each end independently is how both stubs get drawn
 *  to one vertex with the cable crossing itself. An unplaced end abstains, so a
 *  cable with one pin still draws the right way round from the other. */
export function orient(path: LatLng[], a: LatLng | null, b: LatLng | null): boolean {
  if (path.length < 2) return true
  const lat0 = path[0][0]
  const head = path[0], tail = path[path.length - 1]
  return d2(a, head, lat0) + d2(b, tail, lat0)
      <= d2(a, tail, lat0) + d2(b, head, lat0)
}

/** Where a fibre point sits, or null when nobody has placed it. */
export type PinOf = (p: FibrePoint) => LatLng | null

/** THE WHOLE DRAWN LINE for one cable: pin → route → pin.
 *
 *  The stubs are not decoration. A traced street stops where the glass stops,
 *  which is routinely a closure on a pole with the box it feeds a few metres
 *  off it — so a route drawn alone leaves both ends hanging near, but not on,
 *  the pins the record says it reaches.
 *
 *  An UNTRACED cable falls back to the chord between its two pins, and that is
 *  honest rather than a guess dressed up: the record says these two points are
 *  joined by this sheath, and nobody has said where it runs. The renderer draws
 *  it dashed for exactly that reason.
 *
 *  Returns [] when there is nothing drawable at all — an untraced cable with an
 *  unplaced end. Nothing is better than a line to a coordinate we do not have. */
export function cablePolyline(cable: Cable, pinOf: PinOf): LatLng[] {
  const a = pinOf(cable.a)
  const b = pinOf(cable.b)
  const path = (cable.path ?? []) as LatLng[]
  if (path.length < 2) return a && b ? [a, b] : []
  const forward = orient(path, a, b)
  const route = forward ? path : [...path].reverse()
  return [...(a ? [a] : []), ...route, ...(b ? [b] : [])]
}

/** Whether this cable is being drawn as SURVEYED geometry or as an admitted
 *  chord. The dash on this map means "nobody walked this", and a cable and its
 *  drop lines must not teach two readings of the same signal. */
export const cableTraced = (cable: Cable) => (cable.path?.length ?? 0) >= 2

/** An untraced cable's dash. Wider than a drop's (`DROP_DASH`, an 8px period)
 *  and than a reference ONU's, because a sheath is a heavier line and a dash
 *  array is absolute px: reusing a finer period on a wider stroke closes the
 *  gaps into a solid line, i.e. into a claim that somebody surveyed it. */
export const CABLE_DASH = "2 9"

/** THE POINT ON A CABLE ITS LABEL RIDES: the middle of the line AS DRAWN.
 *
 *  Measured along the polyline rather than taken as the chord's midpoint, for
 *  the reason `refChipPos` exists — on a traced street the two are nowhere near
 *  each other, and a chip placed at the chord would sit off the cable it names,
 *  in a field. Walking the real geometry is also what lets the collision budget
 *  and the render agree about where the chip is; computing it twice, differently,
 *  is how a budget reports itself clear over a visible collision. */
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

/** THE CABLE'S CHIP: its NAME, and how much glass is in it.
 *
 *  The complaint this answers, from an ISP looking at their own street: four
 *  violet lines meet at a closure and NOTHING on any of them says which is the
 *  24F trunk and which is the 4F branch. Every other line family here already
 *  earns a chip — the link's rate, the drop's — and the cable, the object this
 *  whole view exists for, had none. Identifying one meant clicking a box and
 *  reading a list, which is backwards: on a map you are looking at the LINE.
 *
 *  THE NAME LEADS, and the count is the second fact. A bare `24F` separates a
 *  trunk from a branch but not one trunk from another, and on a segment-per-span
 *  model a drum is several cables sharing a name — so the name is what makes
 *  four lines read as one route. Truncation is CSS, because a chip that grows to
 *  fit its text overruns the collision box the budget reserved for it.
 *
 *  It shares `.wisp-linkbw` with the rate chip deliberately rather than growing
 *  a second badge style: the two ride the same lines and land in the same
 *  budget, so they have to read as one family. It carries NO status tone — a
 *  cable has no state, and what IS broken is the span drawn over it.
 *
 *  THE TITLE IS WHERE THE HONEST DETAIL GOES, because a chip has room for a name
 *  and nothing else: the length only when somebody WALKED the route (an untraced
 *  cable has no length rather than zero — zero would be a measurement), and
 *  coverage said the way `coresRecordedLabel` says it everywhere else, i.e.
 *  RECORDED and never spare. */
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
