import { useRef } from "react"
import L from "leaflet"
import { useMapEvents } from "react-leaflet"
import { project } from "@/map/clusters"
import { alongKm, fmtKm, nearestOnPath, pointAt, polyKm } from "@/map/geometry"
import { esc } from "@/map/pins"

const HOVER_SLACK_PX = 12

const PIN_KEEPOUT_PX = 32

export interface HoverLink {
  key: string
  pts: Array<[number, number]>
  from: { name: string }
  to: { name: string }
  drawn: boolean
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

export function projectLinks(links: HoverLink[], zoom: number) {
  return links.map((l) => ({
    link: l,
    px: l.pts.map(([lat, lng]) => project(lat, lng, zoom)) as Array<[number, number]>,
  }))
}

const hoverSig = (h: LinkHover | null) =>
  h ? `${h.key}|${h.fromKm.toFixed(3)}|${h.toKm.toFixed(3)}` : ""

export function LinkHoverProbe({ projected, enabled, onHover, zoom, keepOut }: {
  projected: ReturnType<typeof projectLinks>
  enabled: boolean
  zoom: number
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
          toKm: Math.max(polyKm(link.pts) - from, 0),
          drawn: link.drawn,
          fromCable: link.fromCable,
        }
      }
      emit(best)
    },
    mouseout: () => emit(null),
    dragstart: () => emit(null),
  })
  return null
}

export function hoverIcon(h: LinkHover): L.DivIcon {
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
    + (h.drawn
      ? (h.fromCable ? `<span class="wisp-linkhover__note">along the cable</span>` : "")
      : `<span class="wisp-linkhover__note">straight-line</span>`)
    + `</div></div>`
  return L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
}
