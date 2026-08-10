// Hover a cable, anywhere along it, and read how far that point is from each end.
//
// The question a splicing crew actually asks standing at a fault is "how much
// drum from here to which end?" — the device panel only ever answered it for the
// whole span. Distances are GROUND kilometres walked along the rendered
// geometry: along the drawn route where one exists, along the chord where it
// doesn't, and the readout says which so nobody quotes a chord as cable.
//
// Detection is a MAP-level mousemove, deliberately not a mouseover on the
// Polylines: topology lines must stay interactive={false} or a line crossing the
// viewport swallows the placement clicks the map is there to receive. Probing
// from the map keeps that invariant intact and costs one pass over pre-projected
// geometry per mouse event.
import { useRef } from "react"
import L from "leaflet"
import { useMapEvents } from "react-leaflet"
import { project } from "@/map/clusters"
import { alongKm, fmtKm, nearestOnPath, pointAt, polyKm } from "@/map/geometry"
import { esc } from "@/map/pins"

/** How close the cursor must come to a line, in screen px. Roughly a fingertip
    either side of the stroke — wide enough to catch without hunting, tight
    enough that two parallel cables stay separately hoverable (which is the
    whole point of colouring them). */
const HOVER_SLACK_PX = 12

/** How close to a PIN the readout stands down, in screen px.
 *
 *  Every cable ends at a box, so within a pin's own reach several of them run
 *  inside `HOVER_SLACK_PX` of each other and the readout has nothing left to
 *  say: one end reads ~0 and the other reads the whole span, which is the
 *  number the device panel already quotes. What it does instead is pop a
 *  measurement of some arbitrary one of them over the box you were reaching
 *  for, and fight the card that box opens on hover.
 *
 *  32px, and the size is set by WHERE THE RING IS CENTRED, which is not where
 *  the dot is drawn: `.wisp-pin` is a dot-over-label column translated -50%, so
 *  the coordinate the lines converge on sits ~12px BELOW the visible dot, about
 *  halfway between it and the name plate. A ring that only cleared the dot's own
 *  radius would therefore leave the dot's top edge exposed — measured in the
 *  browser, the readout survived to within 18px of the dot. 32 clears the dot
 *  (19px up) with a fingertip to spare and covers the label below it. Past ~40
 *  it would start blanking genuinely readable cable between two close sites. */
const PIN_KEEPOUT_PX = 32

export interface HoverLink {
  key: string
  pts: Array<[number, number]>
  from: { name: string }
  to: { name: string }
  drawn: boolean
  /** The geometry is the CABLE's route, not this section's own trace. */
  fromCable: boolean
}

export interface LinkHover {
  key: string
  at: [number, number]
  fromName: string
  toName: string
  fromKm: number
  toKm: number
  drawn: boolean
  fromCable: boolean
}

/** Pre-project every line once per zoom; a mousemove then only walks numbers. */
export function projectLinks(links: HoverLink[], zoom: number) {
  return links.map((l) => ({
    link: l,
    px: l.pts.map(([lat, lng]) => project(lat, lng, zoom)) as Array<[number, number]>,
  }))
}

/** Identity of what's on screen, so an unchanged readout doesn't re-render the
    page. Mousemove fires per pixel and MapPage is a big tree: without this, the
    common case (cursor nowhere near a cable) would setState on every event. The
    distances are rounded to what the readout actually PRINTS — a change the
    operator can't see isn't a change. */
const hoverSig = (h: LinkHover | null) =>
  h ? `${h.key}|${h.fromKm.toFixed(3)}|${h.toKm.toFixed(3)}` : ""

export function LinkHoverProbe({ projected, enabled, onHover, zoom, keepOut }: {
  projected: ReturnType<typeof projectLinks>
  enabled: boolean
  zoom: number
  /** Projected pin positions the readout keeps clear of — the same points the
      marks are drawn at, one per site (a folded cluster counts as one). */
  keepOut: Array<[number, number]>
  onHover: (h: LinkHover | null) => void
}) {
  const last = useRef("")
  const emit = (h: LinkHover | null) => {
    const sig = hoverSig(h)
    if (sig === last.current) return
    last.current = sig
    onHover(h)
  }
  useMapEvents({
    mousemove: (e: L.LeafletMouseEvent) => {
      if (!enabled) return
      const [x, y] = project(e.latlng.lat, e.latlng.lng, zoom)
      // Near a box, say nothing. Checked before the walk rather than after, so
      // approaching a pin goes quiet on the way IN — the mark's own mouseover
      // only fires once the cursor is on the 14px dot, and the readout was
      // still measuring for the last fingertip of the approach.
      if (keepOut.some(([kx, ky]) => Math.hypot(kx - x, ky - y) < PIN_KEEPOUT_PX))
        return emit(null)
      let best: LinkHover | null = null
      let bestDist = HOVER_SLACK_PX
      for (const { link, px } of projected) {
        if (px.length < 2) continue
        const hit = nearestOnPath(px, x, y)
        if (hit.dist >= bestDist) continue
        bestDist = hit.dist
        const from = alongKm(link.pts, hit.seg, hit.t)
        best = {
          key: link.key,
          at: pointAt(link.pts, hit.seg, hit.t),
          fromName: link.from.name,
          toName: link.to.name,
          fromKm: from,
          // subtract rather than re-walk: the two numbers must add up to the
          // span the device panel quotes, or the readout looks broken
          toKm: Math.max(polyKm(link.pts) - from, 0),
          drawn: link.drawn,
          fromCable: link.fromCable,
        }
      }
      emit(best)
    },
    // leaving the map (or starting a drag) must clear it — a readout frozen
    // over the tiles reads as a real measurement of wherever it's left sitting
    mouseout: () => emit(null),
    dragstart: () => emit(null),
  })
  return null
}

/** The readout itself, anchored at the point on the line being measured. The
    caller renders it as a non-interactive Marker — it must never eat a click.
    Deliberately NOT cachedDivIcon: the text changes with every mouse position,
    so caching it would churn the shared icon cache and, on overflow, clear() the
    pin icons with it — which restarts every down-pulse on the map. There is only
    ever one of these alive, so a fresh icon per frame is the cheap side. */
export function hoverIcon(h: LinkHover): L.DivIcon {
  // The operator tint this readout used to borrow went with the tint itself
  // (2026-08-08). A measurement is not a place for identity anyway — which
  // cable a span belongs to is a fact the cable panel names in words.
  const html =
    `<div class="wisp-linkhover">`
    + `<span class="wisp-linkhover__dot"></span>`
    + `<div class="wisp-linkhover__box">`
    + `<span class="wisp-linkhover__end">`
    + `<span class="wisp-linkhover__km">${esc(fmtKm(h.fromKm))}</span>`
    + `<span class="wisp-linkhover__name">${esc(h.fromName)}</span></span>`
    + `<span class="wisp-linkhover__end">`
    + `<span class="wisp-linkhover__km">${esc(fmtKm(h.toKm))}</span>`
    + `<span class="wisp-linkhover__name">${esc(h.toName)}</span></span>`
    // Honesty label, on the ONE case that needs it: a chord is not cable length,
    // and this readout is exactly where someone would otherwise assume it was.
    // The drawn case captioned itself "along cable" and was dropped as obvious —
    // the line on screen visibly follows the traced path the number is measured
    // along, so the words only restated the picture. The chord looks identical
    // to a cable and must keep saying that it isn't one.
    //
    // THREE STATES, never two. A span that borrows its CABLE's route is
    // measured along surveyed geometry — so it is not a chord and must not
    // apologise like one — but nobody walked THIS section: what was walked is
    // the street it is cut from, and the number is the stretch between the two
    // points these boxes tap it at. Saying so is the difference between "we
    // measured this run" and "we measured the cable this run is in", which is
    // exactly the distinction a crew ordering drum needs.
    + (h.drawn
      ? (h.fromCable ? `<span class="wisp-linkhover__note">along the cable</span>` : "")
      : `<span class="wisp-linkhover__note">straight-line</span>`)
    + `</div></div>`
  return L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
}
