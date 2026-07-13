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

export function MapEvents({ org, onMapClick, onZoom }: {
  org: string | null
  onMapClick: (ll: L.LatLng) => void
  onZoom: (z: number) => void
}) {
  const map = useMapEvents({
    click: (e) => onMapClick(e.latlng),
    moveend: () => saveView(org, map),
    zoomend: () => onZoom(map.getZoom()),
  })
  useEffect(() => { onZoom(map.getZoom()) }, [map, onZoom])
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
