// Viewport persistence + the single view decision-maker. All pan/zoom/lock
// logic lives in ViewController INSIDE MapContainer — a ref on the container
// isn't populated yet when a query resolves in the same commit.
import { useEffect, useRef } from "react"
import L from "leaflet"
import { useMap, useMapEvents } from "react-leaflet"
import type { Placed } from "@/map/pins"

const VIEW_KEY = "wisp:map:view"

interface MapView { lat: number; lng: number; zoom: number }

export function loadView(org: string | null): MapView | null {
  if (!org) return null
  try {
    const raw = localStorage.getItem(`${VIEW_KEY}:${org}`)
    const v = raw ? (JSON.parse(raw) as MapView) : null
    return v && Number.isFinite(v.lat) && Number.isFinite(v.lng) && Number.isFinite(v.zoom) ? v : null
  } catch {
    return null
  }
}

function saveView(org: string | null, map: L.Map): void {
  if (!org) return
  try {
    const c = map.getCenter()
    localStorage.setItem(`${VIEW_KEY}:${org}`,
      JSON.stringify({ lat: c.lat, lng: c.lng, zoom: map.getZoom() }))
  } catch {
    /* private mode / quota — the view just won't persist */
  }
}

export function MapEvents({ org, onMapClick, onMapContext, onZoom, onMoved }: {
  org: string | null
  onMapClick: (ll: L.LatLng) => void
  /** Right-click (and long-press, which Leaflet simulates as the same event).
   *  Registering it at all is what suppresses the BROWSER menu: Leaflet only
   *  calls preventDefault on contextmenu when something is listening for it. */
  onMapContext?: (ll: L.LatLng, point: L.Point) => void
  onZoom: (z: number) => void
  /** the view moved — anything anchored to a screen position has to go */
  onMoved?: () => void
}) {
  const map = useMapEvents({
    click: (e) => onMapClick(e.latlng),
    contextmenu: (e) => onMapContext?.(e.latlng, e.containerPoint),
    movestart: () => onMoved?.(),
    moveend: () => saveView(org, map),
    zoomend: () => onZoom(map.getZoom()),
  })
  useEffect(() => { onZoom(map.getZoom()) }, [map, onZoom])
  return null
}

/** Tells Leaflet its container changed size.
 *
 *  Leaflet measures its container on init and thereafter only on a WINDOW
 *  resize, so anything that changes the map's box without resizing the window
 *  leaves it painting tiles for its old size — the canvas keeps the old width
 *  and the rest of the box renders empty. Three things here do exactly that:
 *  collapsing the sidebar, opening or closing a SPLIT PANE, and dragging the
 *  split divider. Only the first predates split view, which is why this was
 *  never noticed: it takes a big step change to be obvious, and switching a
 *  split from side-by-side to stacked is the biggest one there is.
 *
 *  Safe inside a ResizeObserver because `invalidateSize` re-reads the box and
 *  redraws; it does not change the box, so it cannot feed itself. rAF-batched so
 *  a divider DRAG costs one recalculation per frame rather than one per
 *  observation, and `animate: false` because the map is not moving — its frame
 *  is.
 */
export function InvalidateOnResize() {
  const map = useMap()
  useEffect(() => {
    const el = map.getContainer()
    let frame = 0
    const ro = new ResizeObserver(() => {
      if (frame) return
      frame = requestAnimationFrame(() => { frame = 0; map.invalidateSize({ animate: false }) })
    })
    ro.observe(el)
    return () => { if (frame) cancelAnimationFrame(frame); ro.disconnect() }
  }, [map])
  return null
}

export const FIT_PADDING: L.FitBoundsOptions = { padding: [56, 56], maxZoom: 15 }

// One decision-maker for the viewport, INSIDE MapContainer (useMap — a ref on
// the container isn't populated yet when a query resolves in the same commit).
// Two jobs, strictly ordered so there's no fit race:
//   1. lock pan/zoom to the org's Settings map area ("show only my state")
//   2. frame the initial view exactly once, after BOTH queries land:
//      saved view > placed pins > map area. animate:false — an animated fit can
//      be cancelled by the next call, which is how the race looked in testing.
export function ViewController({ placed, ready, hasSavedView, bounds }: {
  placed: Placed[]; ready: boolean; hasSavedView: boolean
  bounds: [number, number, number, number] | null
}) {
  const map = useMap()
  const fitted = useRef(false)
  useEffect(() => {
    if (!ready) return
    const locked = bounds
      ? L.latLngBounds([bounds[0], bounds[1]], [bounds[2], bounds[3]]).pad(0.12)
      : null
    // Frame FIRST, lock SECOND: setMinZoom on a still-zoomed-out map fires an
    // ANIMATED setZoom that lands after — and silently overrides — an
    // animate:false fitBounds issued in the same tick.
    if (!fitted.current) {
      fitted.current = true
      if (!hasSavedView) {
        if (placed.length > 0) {
          map.fitBounds(L.latLngBounds(placed.map((d) => [d.lat, d.lng])),
            { ...FIT_PADDING, animate: false })
        } else if (locked) {
          map.fitBounds(locked, { animate: false })
        }
      }
    }
    if (locked) {
      // area changed under a view that's now outside it (or a stale saved view)
      if (!locked.contains(map.getCenter())) map.fitBounds(locked, { animate: false })
      map.options.maxBoundsViscosity = 1.0 // hard wall, no rubber-banding out
      map.setMaxBounds(locked)
      map.setMinZoom(Math.max(2, map.getBoundsZoom(locked)))
    } else {
      map.setMaxBounds(undefined as unknown as L.LatLngBoundsExpression)
      map.setMinZoom(2)
    }
  }, [map, ready, bounds, placed, hasSavedView])
  return null
}
