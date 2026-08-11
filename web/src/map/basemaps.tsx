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

export function AttributionPrefix() {
  const map = useMap()
  useEffect(() => {
    map.attributionControl?.setPrefix(false)
  }, [map])
  return null
}

const CARTO_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'

export function loadBasemap(): Basemap {
  try {
    const v = localStorage.getItem(BASEMAP_KEY)
    return v === "gsat" || v === "sat" ? "gsat" : "google"
  } catch {
    return "google"
  }
}

export function StreetsTiles({ dark = false }: { dark?: boolean }) {
  const style = dark ? "dark_all" : "voyager"
  return (
    <TileLayer
      key={`streets-${style}`}
      url={`https://{s}.basemaps.cartocdn.com/rastertiles/${style}/{z}/{x}/{y}{r}.png`}
      attribution={CARTO_ATTR}
      subdomains="abcd"
      maxZoom={20}
    />
  )
}

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

export function GoogleLayer({ apiKey, mapType, dark = false, labels = true, onFail }: {
  apiKey: string
  mapType: GoogleMapType
  dark?: boolean
  labels?: boolean
  onFail: (why: string) => void
}) {
  const [session, setSession] = useState<string | null>(
    () => loadGoogleSession(mapType, dark, labels))
  const [gen, setGen] = useState(0) // bump = force a fresh createSession
  const recreated = useRef(false)
  const errTimes = useRef<number[]>([])
  const handledSession = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    setSession(loadGoogleSession(mapType, dark, labels))
    createGoogleSession(apiKey, mapType, dark, labels).then(
      (s) => { if (alive) setSession(s) },
      (e) => { if (alive) onFail(e instanceof Error ? e.message : "session request failed") },
    )
    return () => { alive = false }
  }, [apiKey, mapType, dark, labels, gen, onFail])

  const onTileError = useCallback(() => {
    const now = Date.now()
    errTimes.current = [...errTimes.current.filter((ts) => now - ts < 5000), now]
    if (errTimes.current.length < 3) return
    errTimes.current = []
    if (!session || handledSession.current === session) return
    handledSession.current = session
    if (!recreated.current) {
      recreated.current = true
      clearGoogleSession(mapType, dark, labels)
      setSession(null)
      setGen((g) => g + 1)
    } else {
      onFail("tiles failed to load")
    }
  }, [session, mapType, dark, labels, onFail])

  if (!session) return <StreetsTiles dark={dark} />
  return (
    <>
      <TileLayer
        key={`g-${mapType}-${session}`}
        url={googleTileUrl(session, apiKey)}
        maxNativeZoom={mapType === "satellite" ? 20 : 22}
        maxZoom={22}
        eventHandlers={{ tileerror: onTileError }}
      />
      <GoogleAttribution session={session} apiKey={apiKey} />
    </>
  )
}
