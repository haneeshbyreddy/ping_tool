// Basemaps are Google's Map Tiles API only (2026-07-11: operator dropped the
// CARTO/Esri menu entries) — the sanctioned third-party-renderer API, not the
// SDK-only Maps tiles. The menu shows nothing without an org key in Settings
// (orgs.google_maps_key, referrer-restricted, ships to the browser by design).
// CARTO Voyager survives NOT as a choice but as the keyless safety net: it
// renders for orgs with no key, under a still-creating session, and after a
// Google failure — the map is never blank. Browser-fetched throughout;
// central needs no egress.
import { useCallback, useEffect, useRef, useState } from "react"
import { TileLayer, useMap } from "react-leaflet"
import {
  clearGoogleSession, createGoogleSession, fetchGoogleAttribution, googleTileUrl,
  loadGoogleSession, type GoogleMapType,
} from "@/lib/google-tiles"

export type Basemap = "google" | "gsat"

export const BASEMAP_KEY = "wisp:map:basemap"
export const BASEMAP_LABEL: Record<Basemap, string> = { google: "Google", gsat: "Google Satellite" }
export const GOOGLE_BASEMAPS: Record<Basemap, GoogleMapType> = { google: "roadmap", gsat: "satellite" }

const CARTO_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'

export function loadBasemap(): Basemap {
  try {
    const v = localStorage.getItem(BASEMAP_KEY)
    // legacy "sat" picks stay on imagery; everything else lands on roadmap
    return v === "gsat" || v === "sat" ? "gsat" : "google"
  } catch {
    return "google"
  }
}

// The keyless fallback layer, never in the menu: shown while a Google session
// is being created, when the org has no key, or after a Google failure.
export function StreetsTiles() {
  return (
    <TileLayer
      key="streets"
      url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
      attribution={CARTO_ATTR}
      subdomains="abcd"
      maxZoom={20}
    />
  )
}

// ToS-required dynamic attribution: Google's viewport endpoint names the data
// providers for what's on screen. Debounced off moveend, swapped in and out of
// Leaflet's attribution control (react-leaflet's static `attribution` prop
// can't change per-move). Best-effort — a failed lookup keeps the generic
// "Map data ©Google" line, tiles keep working.
function GoogleAttribution({ session, apiKey }: { session: string; apiKey: string }) {
  const map = useMap()
  const shown = useRef<string | null>(null)
  useEffect(() => {
    let alive = true
    let t: number | undefined
    const swap = (text: string) => {
      if (shown.current === text) return
      if (shown.current) map.attributionControl.removeAttribution(shown.current)
      shown.current = text
      map.attributionControl.addAttribution(text)
    }
    const update = () => {
      const b = map.getBounds()
      fetchGoogleAttribution(session, apiKey, map.getZoom(), {
        north: b.getNorth(), south: b.getSouth(), east: b.getEast(), west: b.getWest(),
      }).then((c) => { if (alive) swap(c) })
        .catch(() => { /* keep whatever line is up */ })
    }
    const onMove = () => { window.clearTimeout(t); t = window.setTimeout(update, 700) }
    swap("Map data ©Google")
    update()
    map.on("moveend", onMove)
    return () => {
      alive = false
      window.clearTimeout(t)
      map.off("moveend", onMove)
      if (shown.current) map.attributionControl.removeAttribution(shown.current)
      shown.current = null
    }
  }, [map, session, apiKey])
  return null
}

// Google Map Tiles API layer: create/reuse a ~2-week session token (cached in
// localStorage), render tiles, keep the viewport attribution fresh. Failure
// ladder: an expired/revoked session shows up as a burst of tile errors →
// recreate the session once; if createSession fails or the fresh session still
// can't load tiles (bad key, quota out), onFail drops the map back to Streets.
export function GoogleLayer({ apiKey, mapType, onFail }: {
  apiKey: string
  mapType: GoogleMapType
  onFail: (why: string) => void
}) {
  const [session, setSession] = useState<string | null>(() => loadGoogleSession(mapType))
  const [gen, setGen] = useState(0) // bump = force a fresh createSession
  const recreated = useRef(false)
  const errTimes = useRef<number[]>([])
  const handledSession = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    createGoogleSession(apiKey, mapType).then(
      (s) => { if (alive) setSession(s) },
      (e) => { if (alive) onFail(e instanceof Error ? e.message : "session request failed") },
    )
    return () => { alive = false }
  }, [apiKey, mapType, gen, onFail])

  // A stray single 404 (deep-zoom satellite gap, flaky link) must not nuke the
  // basemap: act only on a burst — 3 failed tiles inside 5s — and only once per
  // session token (one burst floods the handler with every tile on screen).
  const onTileError = useCallback(() => {
    const now = Date.now()
    errTimes.current = [...errTimes.current.filter((ts) => now - ts < 5000), now]
    if (errTimes.current.length < 3) return
    errTimes.current = []
    if (!session || handledSession.current === session) return
    handledSession.current = session
    if (!recreated.current) {
      recreated.current = true
      clearGoogleSession(mapType)
      setSession(null)
      setGen((g) => g + 1)
    } else {
      onFail("tiles failed to load")
    }
  }, [session, mapType, onFail])

  if (!session) return <StreetsTiles />
  return (
    <>
      <TileLayer
        key={`g-${mapType}-${session}`}
        url={googleTileUrl(session, apiKey)}
        // satellite coverage thins out past z20 in rural areas; upscale instead
        // of requesting tiles that would 404 into the error handler
        maxNativeZoom={mapType === "satellite" ? 20 : 22}
        maxZoom={22}
        eventHandlers={{ tileerror: onTileError }}
      />
      <GoogleAttribution session={session} apiKey={apiKey} />
    </>
  )
}
