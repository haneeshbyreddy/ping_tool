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
  }
}

export function MapEvents({ org, onMapClick, onMapContext, onZoom, onMoved }: {
  org: string | null
  onMapClick: (ll: L.LatLng) => void
  onMapContext?: (ll: L.LatLng, point: L.Point) => void
  onZoom: (z: number) => void
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
