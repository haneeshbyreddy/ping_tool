// Geographic NOC view: every placed device is a live status pin, topology links
// draw between placed parent/child pairs, and clicking a pin opens the same
// Health/Optical/Ports panel the Network tree uses. Placement is dashboard-side
// only (lat/lng on org_devices) — the edge never sees coordinates.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import L from "leaflet"
import { Circle, MapContainer, Marker, Polyline, TileLayer, ZoomControl, useMap, useMapEvents } from "react-leaflet"
import "leaflet/dist/leaflet.css"
import {
  Check, ChevronRight, Copy, Crosshair, Expand, EyeOff, Layers, ListTree, LocateFixed,
  MapPin, Maximize2, Navigation, Pencil, Search, Shrink, Spline, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { inventoryApi, orgsApi, ApiError } from "@/lib/api"
import {
  clearGoogleSession, createGoogleSession, fetchGoogleAttribution, googleTileUrl,
  loadGoogleSession, type GoogleMapType,
} from "@/lib/google-tiles"
import { mapRegionOf } from "@/lib/map-regions"
import { isPassiveType, type OrgDevice, type PonFault } from "@/lib/types"
import { DeviceDetail, DeviceMetrics, RowTag, type DeviceTab } from "@/components/device-detail"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { deviceTone, durationSince, isFresh } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

type Placed = OrgDevice & { lat: number; lng: number }

const isPlaced = (d: OrgDevice): d is Placed => d.lat != null && d.lng != null

function pinTone(d: OrgDevice): "success" | "warning" | "destructive" | "muted" {
  // planned downtime must never read as an outage on the wall map
  if (d.maintenance) return "muted"
  if (!d.assigned_node_id || !d.state) return "muted"
  return deviceTone(d.state, d.state_updated_at)
}

const isTrouble = (d: OrgDevice): boolean => {
  const t = pinTone(d)
  return t === "destructive" || t === "warning"
}

const isDownState = (d: OrgDevice): boolean =>
  d.state === "DOWN" || d.state === "UNREACHABLE"

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;")

// The label rides inside the divIcon so pin + name pan as one DOM node (no
// per-frame tooltip sync). iconSize 0 + CSS translate keeps the dot on the latlng.
// Icons are cached by their html: useNow() re-renders every 15s, and a fresh
// divIcon each tick would make react-leaflet swap the DOM node — restarting the
// down-pulse animation and churning every pin's DOM for nothing.
const _iconCache = new Map<string, L.DivIcon>()

// A PON going soft, visible from the wall map: ring the OLT's pin when its ONUs
// are weak. Quiet when the OLT itself is down (the red pulse owns the pin), in
// maintenance, or the optics walk went stale (no zombie alarms off old readings).
function opticRing(d: OrgDevice): "crit" | "warn" | null {
  if (d.maintenance || isDownState(d) || !isFresh(d.optics_updated_at)) return null
  if ((d.onus_crit ?? 0) > 0) return "crit"
  if ((d.onus_warn ?? 0) > 0) return "warn"
  return null
}

function pinIcon(d: OrgDevice, o: { selected: boolean; dim: boolean; impact: boolean }): L.DivIcon {
  const tone = pinTone(d)
  // first token only ("43m", not "43m 12s") so the hover title churns per minute
  const downFor = isDownState(d) && d.outage_started_at
    ? durationSince(d.outage_started_at).split(" ")[0] : null
  const optic = opticRing(d)
  const cls = ["wisp-pin", `wisp-pin--${tone}`]
  // role is the third visual channel: fill = health, ring = optics, SHAPE = type
  if (d.device_type) cls.push(`wisp-pin--t-${d.device_type.toLowerCase()}`)
  if (o.selected) cls.push("wisp-pin--selected")
  if (o.dim) cls.push("wisp-pin--dim")
  if (o.impact) cls.push("wisp-pin--impact")
  if (d.maintenance) cls.push("wisp-pin--maint")
  if (optic) cls.push(`wisp-pin--optic-${optic}`)
  const weak = (d.onus_crit ?? 0) + (d.onus_warn ?? 0)
  const title = esc(downFor ? `${d.name} — down for ${downFor}`
    : d.maintenance ? `${d.name} — maintenance`
    : optic ? `${d.name} — ${weak} ONU${weak === 1 ? "" : "s"} weak signal` : d.name)
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">
      <span class="wisp-pin__dot"></span><span class="wisp-pin__label">${esc(d.name)}</span>
    </div>`)
}

function cachedDivIcon(html: string): L.DivIcon {
  let icon = _iconCache.get(html)
  if (!icon) {
    if (_iconCache.size > 600) _iconCache.clear()
    icon = L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
    _iconCache.set(html, icon)
  }
  return icon
}

function meIcon(): L.DivIcon {
  return cachedDivIcon(`<div class="wisp-me" title="You are here"></div>`)
}

function vertexIcon(): L.DivIcon {
  return cachedDivIcon(`<div class="wisp-vertex" title="Drag to adjust — double-click to remove"></div>`)
}

const polyKm = (pts: Array<[number, number]>): number => {
  let km = 0
  for (let i = 1; i < pts.length; i++)
    km += distanceKm(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
  return km
}

// ---- Fiber-cut localization (D2) -------------------------------------------
// ponfault brackets a cut in RANGING meters from the OLT; if the operator drew
// the PON's cable route (OLT → splitter chain), we can walk that geometry to
// the same distance and paint the suspect stretch. Ranging is optical path
// (slack coils included) so the stretch is an estimate — the marker says so.

/** point `distM` meters along a polyline (clamped to its ends) */
function pointAlong(path: Array<[number, number]>, distM: number): [number, number] {
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const seg = distanceKm(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]) * 1000
    if (acc + seg >= distM && seg > 0) {
      const t = (distM - acc) / seg
      return [
        path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t,
        path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t,
      ]
    }
    acc += seg
  }
  return path[path.length - 1]
}

/** sub-polyline between two along-path distances (meters) */
function subPath(path: Array<[number, number]>, d0: number, d1: number): Array<[number, number]> {
  const out: Array<[number, number]> = [pointAlong(path, d0)]
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const seg = distanceKm(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]) * 1000
    if (acc + seg > d0 && acc + seg < d1) out.push(path[i])
    acc += seg
  }
  out.push(pointAlong(path, d1))
  return out
}

/** The PON's drawn path: OLT pin → placed passive chain matching this port.
    Per-link drawn routes are used where they exist, chords where they don't.
    Returns null when no placed splitter serves the port — no fake geometry. */
function ponPath(
  olt: Placed, port: string | null, devices: OrgDevice[],
  routeByKey: Map<string, Array<[number, number]>>,
): Array<[number, number]> | null {
  const path: Array<[number, number]> = [[olt.lat, olt.lng]]
  const seen = new Set<number>([olt.id])
  let cur: Placed = olt
  let first = true
  for (let hop = 0; hop < 20; hop++) {
    const kids = devices.filter((d): d is Placed =>
      d.parent_device_id === cur.id && isPassiveType(d.device_type) && isPlaced(d)
      && !seen.has(d.id)
      // the first hop must name the PON; deeper plant may leave it blank
      && (first ? d.pon_port === port : (d.pon_port === port || d.pon_port == null)))
    const next = kids.find((d) => routeByKey.has(`${d.id}:${cur.id}`)) ?? kids[0]
    if (!next) break
    const wps = routeByKey.get(`${next.id}:${cur.id}`) ?? []
    path.push(...wps, [next.lat, next.lng])
    seen.add(next.id)
    cur = next
    first = false
  }
  return path.length > 1 ? path : null
}

function cutIcon(f: PonFault, oltName: string): L.DivIcon {
  const hi = f.cut_high_m == null ? "" : `${(f.cut_high_m / 1000).toFixed(2)} km`
  const lo = f.cut_low_m ? `${(f.cut_low_m / 1000).toFixed(2)} km – ` : "within "
  const title = esc(`Suspected fiber cut — ${oltName} PON ${f.pon_port ?? "?"}: `
    + `${f.dark} ONUs dark, ${lo}${hi} from the OLT (by ranging)`)
  return cachedDivIcon(`<div class="wisp-cut" title="${title}">✕</div>`)
}

// ---- Site clustering --------------------------------------------------------
// ISP gear piles up: one cabinet/rooftop holds an OLT, a switch, a backhaul
// radio. Pins that would overlap on screen fold into one "site" badge — the
// count is the member total, a conic ring shows the status composition.
// Clicking it zooms in when the members are genuinely spread out (the cluster
// splits on its own), or opens the SITE CARD when they truly share a spot (a
// rack) — members resolve in UI space, never on tiles. The old spider-fan
// scattered pins onto real coordinates and read as geography (the same lie the
// removed ONU spokes told); the map shows only true locations. Screen-space
// and zoom-dependent by design: no schema, no assignment workflow — placing
// devices at the same spot IS the rack assignment.

const CLUSTER_PX = 44

interface SiteCluster {
  // sorted member ids — membership shifts with zoom; the site card anchors on
  // a member DEVICE id, not this key, so it survives the reshuffle
  key: string
  members: Placed[]
  center: [number, number]
}

// Web Mercator pixel position at `zoom` — mirrors what Leaflet renders
function project(lat: number, lng: number, zoom: number): [number, number] {
  const scale = 256 * 2 ** zoom
  const s = Math.sin((lat * Math.PI) / 180)
  return [
    ((lng + 180) / 360) * scale,
    (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale,
  ]
}

function buildClusters(placed: Placed[], zoom: number): SiteCluster[] {
  const acc: Array<{ px: [number, number]; members: Placed[] }> = []
  for (const d of placed) {
    const p = project(d.lat, d.lng, zoom)
    const hit = acc.find((c) => Math.hypot(c.px[0] - p[0], c.px[1] - p[1]) < CLUSTER_PX)
    if (hit) hit.members.push(d)
    else acc.push({ px: p, members: [d] })
  }
  return acc.map((c) => ({
    key: c.members.map((m) => m.id).sort((a, b) => a - b).join(","),
    members: c.members,
    center: [
      c.members.reduce((s, m) => s + m.lat, 0) / c.members.length,
      c.members.reduce((s, m) => s + m.lng, 0) / c.members.length,
    ] as [number, number],
  }))
}

const CLUSTER_TONE_ORDER = ["destructive", "warning", "success", "muted"] as const
const CLUSTER_TONE_COLOR: Record<(typeof CLUSTER_TONE_ORDER)[number], string> = {
  destructive: "var(--destructive)", warning: "var(--warning)",
  success: "var(--success)", muted: "var(--muted-foreground)",
}

const toneRank = (d: OrgDevice) => CLUSTER_TONE_ORDER.indexOf(pinTone(d))

// Count badge with a conic composition ring: arc lengths are member statuses,
// so "5 devices, one down" reads without a click. Worst tones paint first
// (from 12 o'clock), all-healthy degenerates to a plain success ring.
function clusterIcon(members: Placed[], o: { dim: boolean; selected: boolean }): L.DivIcon {
  const counts = new Map<string, number>()
  for (const m of members) {
    const t = pinTone(m)
    counts.set(t, (counts.get(t) ?? 0) + 1)
  }
  let acc = 0
  const stops: string[] = []
  for (const t of CLUSTER_TONE_ORDER) {
    const n = counts.get(t) ?? 0
    if (n === 0) continue
    const from = (acc / members.length) * 360
    acc += n
    stops.push(`${CLUSTER_TONE_COLOR[t]} ${from}deg ${(acc / members.length) * 360}deg`)
  }
  const down = counts.get("destructive") ?? 0
  const names = members.slice(0, 6).map((m) => m.name).join(", ")
  const title = esc(`${members.length} devices${down ? `, ${down} down` : ""} — ${names}${members.length > 6 ? ", …" : ""}`)
  const cls = ["wisp-cluster"]
  if (down > 0) cls.push("wisp-cluster--down")
  if (o.selected) cls.push("wisp-cluster--selected")
  if (o.dim) cls.push("wisp-cluster--dim")
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}" style="background:conic-gradient(${stops.join(",")})">
      <span class="wisp-cluster__n">${members.length}</span>
    </div>`)
}

function distanceKm(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const R = 6371, toR = Math.PI / 180
  const dLat = (bLat - aLat) * toR, dLng = (bLng - aLng) * toR
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(aLat * toR) * Math.cos(bLat * toR) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}
const fmtKm = (km: number) => km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(km < 10 ? 1 : 0)} km`

// Basemaps are Google's Map Tiles API only (2026-07-11: operator dropped the
// CARTO/Esri menu entries) — the sanctioned third-party-renderer API, not the
// SDK-only Maps tiles. The menu shows nothing without an org key in Settings
// (orgs.google_maps_key, referrer-restricted, ships to the browser by design).
// CARTO Voyager survives NOT as a choice but as the keyless safety net: it
// renders for orgs with no key, under a still-creating session, and after a
// Google failure — the map is never blank. Browser-fetched throughout;
// central needs no egress.
type Basemap = "google" | "gsat"

const BASEMAP_KEY = "wisp:map:basemap"
const BASEMAP_LABEL: Record<Basemap, string> = { google: "Google", gsat: "Google Satellite" }
const GOOGLE_BASEMAPS: Record<Basemap, GoogleMapType> = { google: "roadmap", gsat: "satellite" }

const CARTO_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'

function loadBasemap(): Basemap {
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
function StreetsTiles() {
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
function GoogleLayer({ apiKey, mapType, onFail }: {
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

const VIEW_KEY = "wisp:map:view"

interface MapView { lat: number; lng: number; zoom: number }

function loadView(org: string | null): MapView | null {
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

function MapEvents({ org, onMapClick, onZoom }: {
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

const FIT_PADDING: L.FitBoundsOptions = { padding: [56, 56], maxZoom: 15 }

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

interface PlaceHit { label: string; lat: number; lng: number }

// OSM Nominatim, browser-side (CORS-open, keyless — same trust model as the tile
// CDN). Debounced + min 3 chars keeps us a polite interactive client; results are
// boxed to the org's Settings map area so "Kondapur" finds yours, not Kolkata's.
async function geocode(q: string, bounds: [number, number, number, number] | null): Promise<PlaceHit[]> {
  const params = new URLSearchParams({ q, format: "jsonv2", limit: "6" })
  if (bounds) {
    const [s, w, n, e] = bounds
    params.set("viewbox", `${w},${n},${e},${s}`)
    params.set("bounded", "1")
  }
  const res = await fetch(`https://nominatim.openstreetmap.org/search?${params.toString()}`)
  if (!res.ok) throw new Error(`geocoder replied ${res.status}`)
  const rows = (await res.json()) as Array<{ display_name: string; lat: string; lon: string }>
  return rows
    .map((r) => ({ label: r.display_name, lat: Number(r.lat), lng: Number(r.lon) }))
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lng))
}

function MapSearch({ devices, bounds, onDevice, onPlace }: {
  devices: OrgDevice[]
  bounds: [number, number, number, number] | null
  onDevice: (d: OrgDevice) => void
  onPlace: (p: PlaceHit) => void
}) {
  const [q, setQ] = useState("")
  const [open, setOpen] = useState(false)
  const needle = q.trim().toLowerCase()
  const debounced = useDebounced(q.trim(), 450)

  const deviceHits = needle
    ? devices.filter((d) =>
        d.name.toLowerCase().includes(needle) || d.ip_address.includes(needle)).slice(0, 6)
    : []
  const places = useQuery({
    queryKey: ["geocode", debounced, bounds?.join(",") ?? "world"],
    queryFn: () => geocode(debounced, bounds),
    enabled: open && debounced.length >= 3,
    staleTime: 5 * 60_000,
    retry: 0,
  })
  const placeHits = debounced.length >= 3 ? places.data ?? [] : []

  const pick = (fn: () => void) => { fn(); setQ(""); setOpen(false) }
  const first = () => {
    if (deviceHits.length > 0) pick(() => onDevice(deviceHits[0]))
    else if (placeHits.length > 0) pick(() => onPlace(placeHits[0]))
  }

  return (
    <div className="pointer-events-auto relative w-56 md:w-72">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={q}
        placeholder="Find a device or place…"
        className="h-8 bg-popover/95 dark:bg-popover/95 pl-8 text-xs backdrop-blur"
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "Enter") first()
          if (e.key === "Escape") { setQ(""); setOpen(false); (e.target as HTMLInputElement).blur() }
        }}
      />
      {open && needle && (
        <Card className="absolute top-9 right-0 left-0 z-[1001] flex max-h-80 flex-col gap-0 overflow-y-auto bg-popover py-0">
          {deviceHits.map((d) => (
            <button key={d.id}
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left hover:bg-foreground/5"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onDevice(d))}>
              <StatusDot tone={pinTone(d)} />
              <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
              {!isPlaced(d) && <RowTag tone="muted">not placed</RowTag>}
              <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">{d.ip_address}</span>
            </button>
          ))}
          {placeHits.map((p, i) => (
            <button key={`${p.lat},${p.lng},${i}`}
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-foreground/5"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onPlace(p))}>
              <MapPin className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate text-xs">{p.label}</span>
            </button>
          ))}
          {deviceHits.length === 0 && placeHits.length === 0 && (
            <p className="px-3 py-2.5 text-xs text-muted-foreground">
              {debounced.length < 3 ? "No matching devices. Type 3+ letters to search places too."
                : places.isFetching ? "Searching places…"
                : places.isError ? "No matching devices; place search is unreachable."
                : "Nothing found on the map or in your devices."}
            </p>
          )}
        </Card>
      )}
    </div>
  )
}

// One decision-maker for the viewport, INSIDE MapContainer (useMap — a ref on
// the container isn't populated yet when a query resolves in the same commit).
// Two jobs, strictly ordered so there's no fit race:
//   1. lock pan/zoom to the org's Settings map area ("show only my state")
//   2. frame the initial view exactly once, after BOTH queries land:
//      saved view > placed pins > map area. animate:false — an animated fit can
//      be cancelled by the next call, which is how the race looked in testing.
function ViewController({ placed, ready, hasSavedView, bounds }: {
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

export function MapPage() {
  const { scopeOrg, canWrite } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const mapRef = useRef<L.Map | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<DeviceTab>("health")
  const [placingId, setPlacingId] = useState<number | null>(null)
  const [placeOpen, setPlaceOpen] = useState(false)
  // drawing a cable path for one link: clicks append vertices, drags adjust
  const [routeEdit, setRouteEdit] = useState<{
    childId: number; parentId: number; points: Array<[number, number]>
  } | null>(null)
  const [editPins, setEditPins] = useState(false)
  const [troubleOnly, setTroubleOnly] = useState(false)
  const [lowZoom, setLowZoom] = useState(false)
  // live zoom drives clustering; MapEvents reports it on mount and every zoomend
  const [zoom, setZoom] = useState(4)
  // site card anchor: a member DEVICE id, not a cluster key — zoom reshuffles
  // membership and a key-anchored card would slam shut mid-zoom
  const [siteAnchor, setSiteAnchor] = useState<number | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [coordsEdit, setCoordsEdit] = useState(false)
  const [coordsText, setCoordsText] = useState("")
  const [basemap, setBasemap] = useState<Basemap>(loadBasemap)
  const [layersOpen, setLayersOpen] = useState(false)
  // Google failure drops to the fallback tiles WITHOUT forgetting the user's
  // pick; one toast per failure, re-picking from the menu re-arms the retry
  const [googleDown, setGoogleDown] = useState(false)
  const googleFailed = useRef(false)
  const pickBasemap = (b: Basemap) => {
    googleFailed.current = false
    setGoogleDown(false)
    setBasemap(b)
    setLayersOpen(false)
    try { localStorage.setItem(BASEMAP_KEY, b) } catch { /* private mode */ }
  }
  const onGoogleFail = useCallback((why: string) => {
    if (googleFailed.current) return
    googleFailed.current = true
    toast.error(`Google basemap unavailable (${why}) — showing the fallback map`)
    setGoogleDown(true)
  }, [])
  // browser geolocation fix from the locate button; accuracy in meters
  const [myLoc, setMyLoc] = useState<{ lat: number; lng: number; acc: number } | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const troubleIdx = useRef(0)
  useNow()

  const { data, isLoading } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    // same self-heal fallback as the Network page: SSE can die silently
    refetchInterval: 30_000,
  })
  // drawn cable paths, keyed "child:parent" — map-only geometry, own endpoint
  const routesQ = useQuery({
    queryKey: ["routes", scopeOrg],
    queryFn: () => inventoryApi.routes(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const routeByKey = useMemo(() => {
    const m = new Map<string, Array<[number, number]>>()
    for (const r of routesQ.data?.routes ?? [])
      if (r.waypoints.length > 0) m.set(`${r.child_id}:${r.parent_id}`, r.waypoints)
    return m
  }, [routesQ.data])

  // PON mass-drop verdicts (fiber cut / power pattern) for the cut overlay
  const faultsQ = useQuery({
    queryKey: ["pon-faults-org", scopeOrg],
    queryFn: () => inventoryApi.orgPonFaults(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  // outage-wave shape (power vs upstream) — annotation only, never a mute
  const incidentsQ = useQuery({
    queryKey: ["incidents", scopeOrg],
    queryFn: () => inventoryApi.incidentShape(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const powerIncidents = useMemo(
    () => (incidentsQ.data?.incidents ?? []).filter(
      (i) => i.kind === "power" && i.center != null && i.radius_km != null),
    [incidentsQ.data])

  // Settings → Map area (orgs.map_region): the viewport lock for this org
  const orgsQ = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const myOrg = orgsQ.data?.orgs.find((o) => o.org_id === scopeOrg)
  const region = mapRegionOf(myOrg?.map_region)
  const googleKey = myOrg?.google_maps_key?.trim() || null
  // no key (removed in Settings, or orgs still loading) → fallback tiles,
  // quietly — no toast, and the saved pick survives for when a key returns
  const googleActive = !!googleKey && !googleDown

  const devices = useMemo(() => data?.devices ?? [], [data])
  const placed = useMemo(() => devices.filter(isPlaced), [devices])
  const unplaced = useMemo(() => devices.filter((d) => !isPlaced(d)), [devices])
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const selected = selectedId != null ? byId.get(selectedId) ?? null : null
  const placing = placingId != null ? byId.get(placingId) ?? null : null

  // Overlapping pins fold into site clusters. pinPos is each device's DISPLAY
  // position — raw when alone, the cluster centroid while folded. Nothing ever
  // renders at a fabricated coordinate: folded members are listed in the site
  // card (UI space), not scattered over the tiles. Links read pinPos, so
  // lines follow the fold.
  const clusters = useMemo(() => buildClusters(placed, zoom), [placed, zoom])
  const pinPos = useMemo(() => {
    const pos = new Map<number, [number, number]>()
    for (const c of clusters)
      for (const m of c.members)
        pos.set(m.id, c.members.length === 1 ? [m.lat, m.lng] : c.center)
    return pos
  }, [clusters])
  // the cluster the site card is showing; a 1-member resolution means the
  // cluster split honestly at this zoom, so there's nothing to list
  const siteCluster = useMemo(() => {
    if (siteAnchor == null) return null
    const c = clusters.find((x) => x.members.some((m) => m.id === siteAnchor))
    return c && c.members.length > 1 ? c : null
  }, [clusters, siteAnchor])

  // Fiber-cut overlays: for each fiber-kind fault, walk the drawn PON path to
  // the ranging interval and paint the suspect stretch + an ✕. No drawn path /
  // unplaced OLT = no overlay (the Optical tab still carries the distance).
  const cutSegments = useMemo(() => {
    const out: Array<{
      key: string; fault: PonFault; pts: Array<[number, number]>
      mid: [number, number]; oltName: string
    }> = []
    for (const f of faultsQ.data?.faults ?? []) {
      if (f.kind !== "fiber" || f.cut_high_m == null) continue
      const olt = byId.get(f.device_id)
      if (!olt || !isPlaced(olt)) continue
      const path = ponPath(olt, f.pon_port, devices, routeByKey)
      if (!path) continue
      const totalM = polyKm(path) * 1000
      // ranging is optical length ≥ geographic length — clamp into the geometry
      // and keep the stretch visible even when the interval collapses
      let d1 = Math.min(f.cut_high_m, totalM)
      let d0 = Math.min(f.cut_low_m ?? 0, d1)
      if (d1 - d0 < 40) d0 = Math.max(0, d1 - 40)
      if (d1 <= 0) { d0 = Math.max(0, totalM - 60); d1 = totalM }
      out.push({
        key: `cut-${f.device_id}-${f.pon_port ?? "?"}`,
        fault: f, pts: subPath(path, d0, d1),
        mid: pointAlong(path, (d0 + d1) / 2), oltName: olt.name,
      })
    }
    return out
  }, [faultsQ.data, byId, devices, routeByKey])

  // Blast radius: everything downstream of the selected device (full device set,
  // not just placed — the count answers "how many customers am I about to page").
  const downstream = useMemo(() => {
    const out = new Set<number>()
    if (selectedId == null) return out
    const kids = new Map<number, number[]>()
    for (const d of devices) {
      if (d.parent_device_id != null) {
        const g = kids.get(d.parent_device_id)
        if (g) g.push(d.id)
        else kids.set(d.parent_device_id, [d.id])
      }
    }
    const stack = [...(kids.get(selectedId) ?? [])]
    while (stack.length) {
      const id = stack.pop()!
      if (out.has(id)) continue
      out.add(id)
      stack.push(...(kids.get(id) ?? []))
    }
    return out
  }, [devices, selectedId])
  const downstreamDown = useMemo(
    () => devices.filter((d) => downstream.has(d.id) && pinTone(d) === "destructive").length,
    [devices, downstream])

  // down first, then degraded — the 2am order of operations
  const troubles = useMemo(() =>
    placed.filter(isTrouble).sort((a, b) =>
      (pinTone(a) === "destructive" ? 0 : 1) - (pinTone(b) === "destructive" ? 0 : 1)
      || a.name.localeCompare(b.name)),
    [placed])

  const setLocation = useMutation({
    mutationFn: ({ id, lat, lng }: { id: number; lat: number | null; lng: number | null }) =>
      inventoryApi.setLocation(id, lat, lng),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inventory"] }),
    onError: (e) => toast.error(`Couldn't save the pin${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const setRoute = useMutation({
    mutationFn: ({ childId, parentId, waypoints }: {
      childId: number; parentId: number; waypoints: Array<[number, number]>
    }) => inventoryApi.setRoute(childId, parentId, waypoints),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      setRouteEdit(null)
    },
    onError: (e) => toast.error(`Couldn't save the route${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // Search: a placed device flies to its pin; an unplaced one goes straight into
  // placement mode (search Gachibowli → pick the device → click the map).
  const searchDevice = (d: OrgDevice) => {
    const map = mapRef.current
    if (isPlaced(d)) {
      map?.flyTo([d.lat, d.lng], Math.max(map.getZoom(), 15))
      setDetailTab("health")
      setSelectedId(d.id)
      // folded behind a badge? the site card names it — nothing hides on the map
      setSiteAnchor(d.id)
    } else if (canWrite) {
      setSelectedId(null)
      setPlaceOpen(false)
      setPlacingId(d.id)
    } else {
      toast.info(`${d.name} isn't on the map yet`)
    }
  }
  const searchPlace = (p: PlaceHit) => {
    const map = mapRef.current
    map?.flyTo([p.lat, p.lng], Math.max(map.getZoom(), 14))
  }

  // Initial view: last saved per org, else fit every placed pin once they load,
  // else a wide world view a first-time org can zoom from.
  const initialView = useMemo(() => loadView(scopeOrg), [scopeOrg])

  useEffect(() => {
    if (placingId == null && routeEdit == null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setPlacingId(null); setRouteEdit(null) }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [placingId, routeEdit])

  const onMapClick = useCallback((ll: L.LatLng) => {
    if (routeEdit != null) {
      setRouteEdit((re) => re && { ...re, points: [...re.points, [ll.lat, ll.lng]] })
    } else if (placingId != null) {
      setLocation.mutate({ id: placingId, lat: ll.lat, lng: ll.lng })
      setSelectedId(placingId)
      setPlacingId(null)
    } else {
      setSelectedId(null)
      setSiteAnchor(null)
    }
  }, [placingId, routeEdit, setLocation])

  // Drag-snap: existing near-stacks (pins dropped "close enough" by eye) are
  // exactly what made the old fan misleading — dropping a pin within a badge
  // radius of a neighbor now joins its site at the SAME coords.
  const nearestOther = useCallback((id: number, lat: number, lng: number): Placed | null => {
    const p = project(lat, lng, zoom)
    let best: Placed | null = null
    let bestPx = 24
    for (const d of placed) {
      if (d.id === id) continue
      const q = project(d.lat, d.lng, zoom)
      const px = Math.hypot(q[0] - p[0], q[1] - p[1])
      if (px < bestPx) { best = d; bestPx = px }
    }
    return best
  }, [placed, zoom])

  const fitAll = () => {
    if (placed.length === 0) return
    mapRef.current?.fitBounds(L.latLngBounds(placed.map((d) => [d.lat, d.lng])), FIT_PADDING)
  }

  const locateMe = () => {
    if (!navigator.geolocation) { toast.error("Geolocation is not available in this browser"); return }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setMyLoc({ lat: pos.coords.latitude, lng: pos.coords.longitude, acc: pos.coords.accuracy })
        mapRef.current?.flyTo([pos.coords.latitude, pos.coords.longitude], 14)
      },
      (err) => {
        if (err.code === err.PERMISSION_DENIED) {
          toast.error(window.isSecureContext
            ? "Location blocked — allow location for this site in the browser's address bar, then retry"
            : "Location needs HTTPS — open the dashboard over https to use it")
        } else if (err.code === err.TIMEOUT) {
          toast.error("Timed out getting your location — try again")
        } else {
          toast.error("Your device couldn't determine a location")
        }
      },
      { enableHighAccuracy: true, timeout: 10_000 },
    )
  }

  // "Take me to the problem": each click flies to the next trouble pin, worst first.
  const cycleTrouble = () => {
    if (troubles.length === 0) return
    const d = troubles[troubleIdx.current % troubles.length]
    troubleIdx.current += 1
    mapRef.current?.flyTo([d.lat, d.lng], Math.max(mapRef.current.getZoom(), 14))
    setDetailTab("health")
    setSelectedId(d.id)
    setSiteAnchor(d.id) // a folded trouble pin surfaces in the site card
  }

  const toggleFullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void wrapRef.current?.requestFullscreen()
  }
  useEffect(() => {
    const onFs = () => setFullscreen(!!document.fullscreenElement)
    document.addEventListener("fullscreenchange", onFs)
    return () => document.removeEventListener("fullscreenchange", onFs)
  }, [])

  const onZoom = useCallback((z: number) => { setZoom(z); setLowZoom(z < 12) }, [])

  // Click a folded site: members genuinely spread out → zoom to them and let
  // the cluster split on its own; truly co-located (a rack) → the site card.
  // In placement mode a badge click means "this device lives here too": snap
  // to the site's exact coords instead of pixel-hunting next to the badge.
  const onClusterClick = (c: SiteCluster) => {
    if (routeEdit != null) return
    if (placingId != null) {
      const t = c.members.reduce((best, m) =>
        distanceKm(m.lat, m.lng, c.center[0], c.center[1])
          < distanceKm(best.lat, best.lng, c.center[0], c.center[1]) ? m : best)
      setLocation.mutate({ id: placingId, lat: t.lat, lng: t.lng })
      toast.success(`Placed at ${t.name} — same site`)
      setSelectedId(placingId)
      setPlacingId(null)
      return
    }
    const b = L.latLngBounds(c.members.map((m) => [m.lat, m.lng] as [number, number]))
    const spanM = distanceKm(b.getSouth(), b.getWest(), b.getNorth(), b.getEast()) * 1000
    if (spanM > 30 && zoom < 17) {
      mapRef.current?.flyToBounds(b, { padding: [64, 64], maxZoom: 18 })
    } else {
      setPlaceOpen(false) // the site card takes the same corner as the drawer
      setSiteAnchor((a) => (c.members.some((m) => m.id === a) ? null : c.members[0].id))
    }
  }

  // field flow: tech reads GPS off the phone, pastes "17.4401, 78.3489"
  useEffect(() => { setCoordsEdit(false); setCoordsText("") }, [selectedId])
  const saveCoords = () => {
    if (!selected) return
    const m = coordsText.trim().match(/^(-?\d+(?:\.\d+)?)[,;\s]+(-?\d+(?:\.\d+)?)$/)
    if (!m) { toast.error('Use "lat, lng" — e.g. 17.4401, 78.3489'); return }
    setLocation.mutate({ id: selected.id, lat: Number(m[1]), lng: Number(m[2]) })
    setCoordsEdit(false)
  }

  // Only links where both ends are pinned; a line inherits the child's trouble
  // so a red pin drags a red path back toward its feed.
  const links = useMemo(() => {
    const out: Array<{
      key: string; from: Placed; to: Placed; tone: string; backup: boolean
      route?: Array<[number, number]>
    }> = []
    const placedById = new Map(placed.map((d) => [d.id, d]))
    for (const d of placed) {
      const tone = pinTone(d)
      if (d.parent_device_id != null) {
        const p = placedById.get(d.parent_device_id)
        if (p) out.push({ key: `p${d.id}`, from: p, to: d, tone, backup: false,
          route: routeByKey.get(`${d.id}:${p.id}`) })
      }
      for (const bp of d.backup_parents) {
        const p = placedById.get(bp)
        if (p) out.push({ key: `b${d.id}-${bp}`, from: p, to: d, tone, backup: true,
          route: routeByKey.get(`${d.id}:${bp}`) })
      }
    }
    return out
  }, [placed, routeByKey])

  if (!scopeOrg) return <NeedsOrg />

  const down = troubles.filter((d) => pinTone(d) === "destructive").length
  const degraded = troubles.length - down

  const lineColor = (tone: string) =>
    tone === "destructive" ? "var(--destructive)"
      : tone === "warning" ? "var(--warning)" : "var(--muted-foreground)"

  const parent = selected?.parent_device_id != null ? byId.get(selected.parent_device_id) : null
  const linkKm = selected && isPlaced(selected) && parent && isPlaced(parent)
    ? distanceKm(selected.lat, selected.lng, parent.lat, parent.lng) : null
  const selRoute = selected && parent ? routeByKey.get(`${selected.id}:${parent.id}`) : undefined
  const routeKm = selRoute && selected && isPlaced(selected) && parent && isPlaced(parent)
    ? polyKm([[parent.lat, parent.lng], ...selRoute, [selected.lat, selected.lng]]) : null

  const startRouteEdit = () => {
    if (!selected || !isPlaced(selected) || !parent || !isPlaced(parent)) return
    setPlacingId(null)
    setPlaceOpen(false)
    setRouteEdit({ childId: selected.id, parentId: parent.id, points: selRoute ?? [] })
  }
  const editingChild = routeEdit ? byId.get(routeEdit.childId) : null
  const editingParent = routeEdit ? byId.get(routeEdit.parentId) : null

  return (
    // header is h-14 (3.5rem); the mobile tab bar overlays the bottom ~4rem
    <div ref={wrapRef} className={cn(
      "wisp-map-wrap relative h-[calc(100svh-3.5rem-4rem)] md:h-[calc(100svh-3.5rem)]",
      placingId != null && "wisp-map-placing",
      lowZoom && "wisp-map-lowzoom",
    )}>
      <MapContainer
        ref={mapRef}
        center={initialView ? [initialView.lat, initialView.lng] : [22.5, 79]}
        zoom={initialView?.zoom ?? 4}
        zoomControl={false}
        attributionControl={true}
        className="wisp-map h-full w-full"
        worldCopyJump
      >
        {googleActive ? (
          <GoogleLayer
            key={`google-${GOOGLE_BASEMAPS[basemap]}`}
            apiKey={googleKey!}
            mapType={GOOGLE_BASEMAPS[basemap]}
            onFail={onGoogleFail}
          />
        ) : (
          <StreetsTiles />
        )}
        <ZoomControl position="bottomright" />
        <MapEvents org={scopeOrg} onMapClick={onMapClick} onZoom={onZoom} />
        <ViewController placed={placed} ready={!isLoading && orgsQ.isSuccess}
          hasSavedView={!!initialView} bounds={region.bounds} />
        {links.map((l) => {
          // the link being redrawn renders as the edit preview instead
          if (routeEdit && l.to.id === routeEdit.childId && l.from.id === routeEdit.parentId)
            return null
          // a selected device lights up its whole downstream path
          const emphasized = selectedId != null
            && (l.to.id === selectedId || downstream.has(l.to.id))
          const dimmed = troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized
          const from = pinPos.get(l.from.id) ?? [l.from.lat, l.from.lng] as [number, number]
          const to = pinPos.get(l.to.id) ?? [l.to.lat, l.to.lng] as [number, number]
          // drawn route only when both ends display at their TRUE positions — a
          // cable path snaking into a cluster centroid reads as an error
          const atTrue = from[0] === l.from.lat && from[1] === l.from.lng
            && to[0] === l.to.lat && to[1] === l.to.lng
          return (
            <Polyline
              key={l.key}
              // never a click target — a line crossing the viewport would otherwise
              // swallow map clicks during placement
              interactive={false}
              positions={l.route && atTrue ? [from, ...l.route, to] : [from, to]}
              pathOptions={{
                color: emphasized && l.tone === "muted" ? "var(--primary)" : lineColor(l.tone),
                weight: emphasized ? 3 : l.tone === "destructive" ? 2.5 : 2,
                opacity: dimmed ? 0.12 : emphasized ? 0.9 : l.tone === "muted" ? 0.45 : 0.75,
                dashArray: l.backup ? "4 6" : undefined,
              }}
            />
          )
        })}
        {/* power-outage hull: several independent feeds dark inside one small
            circle — shade the area so the eye reads "feeder", not "fiber" */}
        {powerIncidents.map((inc, i) => (
          <Circle
            key={`pw-${i}-${inc.since ?? ""}`}
            center={inc.center as [number, number]}
            radius={Math.max((inc.radius_km ?? 0) * 1000 * 1.15, 400)}
            interactive={false}
            pathOptions={{
              color: "var(--warning)", weight: 1.5, opacity: 0.6,
              fillColor: "var(--warning)", fillOpacity: 0.07, dashArray: "6 6",
            }}
          />
        ))}
        {/* suspected-cut stretch: louder than any link (thick, dashed), and the
            ✕ is clickable — it opens the OLT's Optical tab with the verdict */}
        {cutSegments.map((s) => (
          <Fragment key={s.key}>
            <Polyline
              interactive={false}
              positions={s.pts}
              pathOptions={{ color: "var(--destructive)", weight: 5, opacity: 0.85, dashArray: "6 5" }}
            />
            <Marker
              position={s.mid}
              icon={cutIcon(s.fault, s.oltName)}
              zIndexOffset={900}
              eventHandlers={{
                click: () => {
                  if (placingId != null || routeEdit != null) return
                  setDetailTab("optical")
                  setSelectedId(s.fault.device_id)
                },
              }}
            />
          </Fragment>
        ))}
        {routeEdit && (() => {
          const child = byId.get(routeEdit.childId)
          const par = byId.get(routeEdit.parentId)
          if (!child || !par || !isPlaced(child) || !isPlaced(par)) return null
          return (
            <>
              <Polyline
                interactive={false}
                positions={[[par.lat, par.lng], ...routeEdit.points, [child.lat, child.lng]]}
                pathOptions={{ color: "var(--primary)", weight: 2.5, opacity: 0.9, dashArray: "6 6" }}
              />
              {routeEdit.points.map((pt, i) => (
                <Marker
                  key={`v-${i}`}
                  position={pt}
                  draggable
                  icon={vertexIcon()}
                  zIndexOffset={1200}
                  eventHandlers={{
                    dragend: (e) => {
                      const ll = (e.target as L.Marker).getLatLng()
                      setRouteEdit((re) => re && {
                        ...re,
                        points: re.points.map((p, j) => (j === i ? [ll.lat, ll.lng] as [number, number] : p)),
                      })
                    },
                    dblclick: () => setRouteEdit((re) => re && {
                      ...re, points: re.points.filter((_, j) => j !== i),
                    }),
                  }}
                />
              ))}
            </>
          )
        })()}
        {clusters.map((c) => {
          if (c.members.length > 1) {
            const anyDown = c.members.some((m) => pinTone(m) === "destructive")
            // a folded selection highlights the badge — the pin itself never
            // pops out to a fake coordinate
            const sel = c.members.some((m) => m.id === selectedId)
            return (
              <Marker
                key={c.key}
                position={c.center}
                icon={clusterIcon(c.members, {
                  dim: troubleOnly && !c.members.some(isTrouble), selected: sel,
                })}
                eventHandlers={{ click: () => onClusterClick(c) }}
                zIndexOffset={sel ? 1000 : anyDown ? 500 : 100}
              />
            )
          }
          const d = c.members[0]
          const dim = troubleOnly && !isTrouble(d) && d.id !== selectedId
          const impact = downstream.has(d.id)
          return (
            <Marker
              key={d.id}
              position={[d.lat, d.lng]}
              icon={pinIcon(d, { selected: d.id === selectedId, dim, impact })}
              draggable={editPins && canWrite}
              eventHandlers={{
                click: () => {
                  if (routeEdit != null) return
                  // placement mode: a tap on an existing pin means "same spot"
                  // — start the rack deliberately instead of eyeballing it
                  if (placingId != null) {
                    if (placingId !== d.id) {
                      setLocation.mutate({ id: placingId, lat: d.lat, lng: d.lng })
                      toast.success(`Placed at ${d.name} — same site`)
                      setSelectedId(placingId)
                    }
                    setPlacingId(null)
                    return
                  }
                  setDetailTab("health")
                  setSelectedId(d.id === selectedId ? null : d.id)
                },
                dragend: (e) => {
                  const ll = (e.target as L.Marker).getLatLng()
                  // dropping within a badge radius of a neighbor joins its site
                  const near = nearestOther(d.id, ll.lat, ll.lng)
                  if (near) toast.success(`Snapped to ${near.name} — same site`)
                  setLocation.mutate({
                    id: d.id,
                    lat: near ? near.lat : ll.lat,
                    lng: near ? near.lng : ll.lng,
                  })
                },
              }}
              zIndexOffset={d.id === selectedId ? 1000
                : pinTone(d) === "destructive" ? 500 : impact ? 300 : 0}
            />
          )
        })}
        {/* "you are here" from the locate button — never a click target, so it
            can't swallow placement clicks; accuracy circle only when the fix is
            tight enough to mean something at street zoom */}
        {myLoc && (
          <>
            {myLoc.acc <= 2000 && (
              <Circle
                center={[myLoc.lat, myLoc.lng]}
                radius={myLoc.acc}
                interactive={false}
                pathOptions={{ color: "var(--primary)", weight: 1, opacity: 0.35, fillOpacity: 0.08 }}
              />
            )}
            <Marker
              position={[myLoc.lat, myLoc.lng]}
              icon={meIcon()}
              interactive={false}
              zIndexOffset={800}
            />
          </>
        )}
      </MapContainer>

      {/* Google ToS: their wordmark must be visible whenever Google tiles render.
          Fixed px on purpose — it's a logo, not type-scale text. White-with-shadow
          is how Google Maps itself renders it over both roadmap and satellite. */}
      {googleActive && (
        <span aria-hidden className="pointer-events-none absolute bottom-1 left-2 z-[1000] select-none font-medium"
          style={{
            fontFamily: "'Product Sans', Roboto, Arial, sans-serif", fontSize: "18px",
            color: "#fff", textShadow: "0 0 4px rgba(0,0,0,.55), 0 1px 2px rgba(0,0,0,.55)",
          }}>
          Google
        </span>
      )}

      {/* search + status strip -------------------------------------------------- */}
      <div className="pointer-events-none absolute top-3 left-3 z-[1000] flex max-w-[calc(100%-6rem)] flex-wrap items-center gap-2">
        <MapSearch devices={devices} bounds={region.bounds}
          onDevice={searchDevice} onPlace={searchPlace} />
        <div className="pointer-events-auto flex h-8 items-center gap-2.5 rounded-lg border border-border-strong bg-popover/95 dark:bg-popover/95 px-3 text-xs backdrop-blur">
          <span className="font-semibold">{placed.length}<span className="font-normal text-muted-foreground"> / {devices.length} on map</span></span>
          {troubles.length > 0 && (
            <button className="flex items-center gap-2 font-semibold hover:brightness-125"
              title="Jump to the next problem" onClick={cycleTrouble}>
              {down > 0 && <span className="text-destructive">{down} down</span>}
              {degraded > 0 && <span className="text-warning">{degraded} degraded</span>}
              <ChevronRight className="size-3 text-muted-foreground" />
            </button>
          )}
          {isLoading && <span className="text-muted-foreground">loading…</span>}
        </div>
        {(troubles.length > 0 || troubleOnly) && (
          <Button variant={troubleOnly ? "default" : "outline"} size="sm"
            className={cn("pointer-events-auto h-8 backdrop-blur", !troubleOnly && "bg-popover/95 dark:bg-popover/95")}
            title="Dim everything that's healthy"
            onClick={() => setTroubleOnly(!troubleOnly)}>
            <EyeOff className="size-3.5" /> Trouble only
          </Button>
        )}
        {canWrite && unplaced.length > 0 && (
          <Button variant="outline" size="sm"
            className="pointer-events-auto h-8 bg-popover/95 dark:bg-popover/95 backdrop-blur"
            onClick={() => { setPlaceOpen(!placeOpen); setPlacingId(null); setSiteAnchor(null) }}>
            <MapPin className="size-3.5" /> Place devices
            <span className="rounded bg-muted px-1.5 py-px font-mono text-2xs">{unplaced.length}</span>
          </Button>
        )}
      </div>

      {/* power-pattern banner: the verdict a veteran reads off the wall — many
          feeds, one small circle. Explains the red, never silences it. ------- */}
      {powerIncidents.length > 0 && (
        <button
          className="absolute top-3 left-1/2 z-[1000] flex -translate-x-1/2 items-center gap-2 rounded-full border border-warning/50 bg-popover/95 dark:bg-popover/95 px-3.5 py-1.5 text-xs backdrop-blur hover:brightness-110"
          title="Zoom to the affected area"
          onClick={() => {
            const inc = powerIncidents[0]
            if (!inc.center) return
            mapRef.current?.flyToBounds(
              L.latLng(inc.center[0], inc.center[1])
                .toBounds(Math.max((inc.radius_km ?? 0) * 2600, 1200)),
              { padding: [48, 48] })
          }}>
          <span className="font-semibold text-warning">⚡ Power-outage pattern</span>
          <span className="text-muted-foreground">
            {powerIncidents[0].count} devices · {powerIncidents[0].branches} independent feeds
            · {(powerIncidents[0].radius_km ?? 0).toFixed(1)} km area
            {powerIncidents[0].since && <> · {durationSince(powerIncidents[0].since)}</>}
          </span>
        </button>
      )}

      {/* placement banner ------------------------------------------------------ */}
      {placing && (
        <div className="absolute top-14 left-1/2 z-[1000] flex -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Crosshair className="size-3.5 text-primary" />
          <span>Click the map to place <span className="font-mono font-semibold">{placing.name}</span></span>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setPlacingId(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {/* route-drawing banner ---------------------------------------------------- */}
      {routeEdit && editingChild && editingParent && (
        <div className="absolute top-14 left-1/2 z-[1000] flex -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Spline className="size-3.5 text-primary" />
          <span>
            Click along the cable path <span className="font-mono font-semibold">{editingParent.name}</span>
            {" → "}<span className="font-mono font-semibold">{editingChild.name}</span>
            <span className="text-muted-foreground"> · drag to adjust, double-click removes
              · {routeEdit.points.length} pt{routeEdit.points.length === 1 ? "" : "s"}</span>
          </span>
          <Button size="sm" className="h-6 px-2 text-2xs"
            disabled={setRoute.isPending}
            onClick={() => setRoute.mutate({
              childId: routeEdit.childId, parentId: routeEdit.parentId, waypoints: routeEdit.points,
            })}>
            <Check className="size-3" /> Save
          </Button>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setRouteEdit(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {/* controls — slide left of the device panel so they stay clickable ------- */}
      <div className={cn("absolute top-3 right-3 z-[1000] flex flex-col gap-1.5",
        selected && "md:right-[calc(380px+1.5rem)]")}>
        {/* style choices only with a key (the fallback map is not a style);
            the legend rides here too, so the button now renders for everyone */}
        <div className="relative">
          <Button variant={layersOpen ? "default" : "outline"} size="icon"
            className={cn("size-8 backdrop-blur", !layersOpen && "bg-popover/95 dark:bg-popover/95")}
            title="Map style & legend" onClick={() => setLayersOpen(!layersOpen)}>
            <Layers className="size-3.5" />
          </Button>
          {layersOpen && (
            <div className="absolute top-0 right-9 w-44 rounded-lg border border-border-strong bg-popover/95 dark:bg-popover/95 p-1 backdrop-blur">
              {googleKey != null && (
                <>
                  {(Object.keys(BASEMAP_LABEL) as Basemap[]).map((b) => (
                    <button key={b}
                      className={cn("flex w-full items-center rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5",
                        basemap === b && "bg-accent font-medium")}
                      onClick={() => pickBasemap(b)}>
                      {BASEMAP_LABEL[b]}
                    </button>
                  ))}
                  <div className="my-1 border-t" />
                </>
              )}
              <p className="px-2 pt-1 pb-0.5 text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                Pin shapes
              </p>
              {([
                [<span key="s" className="size-3 rounded-full border-2 border-muted-foreground" />, "Core / Gateway"],
                [<span key="s" className="size-3 rounded-[2px] bg-muted-foreground" />, "OLT"],
                [<span key="s" className="size-3 rounded-[4px] bg-muted-foreground" />, "Switch"],
                [<span key="s" className="size-3 rotate-45 rounded-[2px] bg-muted-foreground" />, "Backhaul"],
                [<span key="s" className="size-3 rounded-full bg-muted-foreground" />, "Router / AP"],
                [<span key="s" className="size-2 rounded-full bg-muted-foreground" />, "CPE"],
                [<span key="s" className="size-2 rotate-45 rounded-[1px] bg-muted-foreground/60" />, "Splitter / FDB (passive)"],
                [<span key="s" className="flex size-3.5 items-center justify-center rounded-full border border-warning">
                  <span className="size-2 rounded-full bg-muted-foreground" />
                </span>, "Weak ONUs (ring)"],
              ] as Array<[ReactNode, string]>).map(([swatch, label]) => (
                <div key={label} className="flex items-center gap-2 px-2 py-1 text-xs">
                  <span className="flex w-4 shrink-0 items-center justify-center">{swatch}</span>
                  <span className="text-muted-foreground">{label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <Button variant="outline" size="icon" className="size-8 bg-popover/95 dark:bg-popover/95 backdrop-blur"
          title="Fit all pins" onClick={fitAll} disabled={placed.length === 0}>
          <Maximize2 className="size-3.5" />
        </Button>
        <Button variant="outline" size="icon" className="size-8 bg-popover/95 dark:bg-popover/95 backdrop-blur"
          title="Go to my location" onClick={locateMe}>
          <LocateFixed className="size-3.5" />
        </Button>
        <Button variant="outline" size="icon" className="size-8 bg-popover/95 dark:bg-popover/95 backdrop-blur"
          title={fullscreen ? "Exit fullscreen" : "Fullscreen (NOC wall)"} onClick={toggleFullscreen}>
          {fullscreen ? <Shrink className="size-3.5" /> : <Expand className="size-3.5" />}
        </Button>
        {canWrite && (
          <Button variant={editPins ? "default" : "outline"} size="icon"
            className={cn("size-8 backdrop-blur", !editPins && "bg-popover/95 dark:bg-popover/95")}
            title={editPins ? "Done moving pins" : "Move pins (drag)"}
            onClick={() => setEditPins(!editPins)}>
            <Pencil className="size-3.5" />
          </Button>
        )}
      </div>
      {editPins && canWrite && (
        <div className={cn("absolute right-3 top-[10rem] z-[1000] rounded-lg border border-warning/40 bg-popover/95 dark:bg-popover/95 px-2.5 py-1.5 text-2xs text-warning backdrop-blur",
          selected && "md:right-[calc(380px+1.5rem)]")}>
          drag pins to move them
        </div>
      )}

      {/* unplaced drawer ------------------------------------------------------- */}
      {placeOpen && canWrite && (
        <Card className="absolute top-14 left-3 z-[1000] flex max-h-[60%] w-72 flex-col gap-0 overflow-hidden border-border-strong bg-popover/95 dark:bg-popover/95 py-0 backdrop-blur">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <p className="text-xs font-semibold">Not on the map yet</p>
            <Button variant="ghost" size="icon" className="size-6" onClick={() => setPlaceOpen(false)}>
              <X className="size-3.5" />
            </Button>
          </div>
          <div className="overflow-y-auto">
            {unplaced.map((d) => (
              <button key={d.id}
                className="flex h-9 w-full items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-foreground/5"
                onClick={() => { setPlacingId(d.id); setPlaceOpen(false); setSelectedId(null) }}>
                <StatusDot tone={pinTone(d)} />
                <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
                {d.device_type && <span className="text-2xs text-muted-foreground">{d.device_type}</span>}
                <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">{d.ip_address}</span>
              </button>
            ))}
            {unplaced.length === 0 && (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">Every device is placed.</p>
            )}
          </div>
        </Card>
      )}

      {/* site card: the members of a folded badge, resolved in UI space — the
          map keeps ONE honest pin, this list answers "what's in that cabinet".
          Row click drives the same device panel a pin click does. ------------ */}
      {siteCluster && (() => {
        const members = [...siteCluster.members].sort((a, b) =>
          toneRank(a) - toneRank(b) || a.name.localeCompare(b.name))
        const siteDown = members.filter((m) => pinTone(m) === "destructive").length
        return (
          <Card className="absolute top-14 left-3 z-[1000] flex max-h-[60%] w-72 flex-col gap-0 overflow-hidden border-border-strong bg-popover/95 dark:bg-popover/95 py-0 backdrop-blur">
            <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
              <div className="min-w-0">
                <p className="text-xs font-semibold">{members.length} devices at this site</p>
                <p className="font-mono text-2xs text-muted-foreground">
                  {siteCluster.center[0].toFixed(5)}, {siteCluster.center[1].toFixed(5)}
                  {siteDown > 0 && (
                    <span className="font-sans font-semibold text-destructive"> · {siteDown} down</span>
                  )}
                </p>
              </div>
              <Button variant="ghost" size="icon" className="size-6 shrink-0"
                onClick={() => setSiteAnchor(null)}>
                <X className="size-3.5" />
              </Button>
            </div>
            <div className="overflow-y-auto">
              {members.map((m) => (
                <div key={m.id} role="button" tabIndex={0}
                  className={cn(
                    "flex h-9 w-full cursor-pointer items-center gap-2 border-b px-3 text-left last:border-b-0",
                    m.id === selectedId ? "bg-accent" : "hover:bg-foreground/5",
                  )}
                  onClick={() => { setDetailTab("health"); setSelectedId(m.id) }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { setDetailTab("health"); setSelectedId(m.id) }
                  }}>
                  <StatusDot tone={pinTone(m)} />
                  <span className="min-w-0 truncate font-mono text-xs font-medium">{m.name}</span>
                  {m.device_type && <span className="shrink-0 text-2xs text-muted-foreground">{m.device_type}</span>}
                  <span className="ml-auto flex shrink-0 items-center gap-1">
                    {isDownState(m) && m.outage_started_at ? (
                      <span className="text-2xs font-semibold text-destructive">
                        down {durationSince(m.outage_started_at).split(" ")[0]}
                      </span>
                    ) : m.maintenance ? (
                      <RowTag tone="muted">maint</RowTag>
                    ) : null}
                    {canWrite && editPins && (
                      <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                        title={`Move ${m.name} — click its new spot on the map`}
                        onClick={(e) => {
                          e.stopPropagation()
                          setSiteAnchor(null)
                          setSelectedId(null)
                          setPlaceOpen(false)
                          setPlacingId(m.id)
                        }}>
                        <Crosshair className="size-3" />
                      </Button>
                    )}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        )
      })()}

      {/* device panel ---------------------------------------------------------- */}
      {selected && (
        <Card className="absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover/95 dark:bg-popover/95 py-0 backdrop-blur md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)] md:w-[380px]">
          <div className="flex items-start gap-2.5 border-b px-4 py-3">
            <span className="mt-1"><StatusDot tone={pinTone(selected)} /></span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="min-w-0 truncate font-mono text-sm font-semibold">{selected.name}</p>
                {!!selected.maintenance && <RowTag tone="muted">maint</RowTag>}
              </div>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                <span className="font-mono">{selected.ip_address}</span>
                {selected.device_type && <span>{selected.device_type}</span>}
                {selected.region && <span>{selected.region}</span>}
              </p>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-2">
                <DeviceMetrics device={selected} />
                {isDownState(selected) && selected.outage_started_at && (
                  <span className="text-xs font-semibold text-destructive">
                    for {durationSince(selected.outage_started_at)}
                  </span>
                )}
              </div>
              {downstream.size > 0 && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Feeds <span className="font-semibold text-foreground">{downstream.size}</span> downstream
                  {downstreamDown > 0 && (
                    <span className="font-semibold text-destructive"> · {downstreamDown} down</span>
                  )}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                title="Show in the Network tree"
                onClick={() => navigate("/topology", { state: { deviceId: selected.id } })}>
                <ListTree className="size-3.5" />
              </Button>
              {canWrite && isPlaced(selected) && (
                <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                  title="Remove this pin from the map"
                  onClick={() => {
                    setLocation.mutate({ id: selected.id, lat: null, lng: null })
                    setSelectedId(null)
                  }}>
                  <MapPin className="size-3.5" />
                </Button>
              )}
              <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                onClick={() => setSelectedId(null)}>
                <X className="size-3.5" />
              </Button>
            </div>
          </div>
          {/* field-dispatch row: coords + copy + drive-there + typed GPS entry */}
          <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1.5 text-xs">
            {coordsEdit ? (
              <>
                <Input autoFocus placeholder="17.4401, 78.3489" value={coordsText}
                  className="h-7 w-48 font-mono text-xs"
                  onChange={(e) => setCoordsText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveCoords()
                    if (e.key === "Escape") setCoordsEdit(false)
                  }} />
                <Button variant="ghost" size="icon" className="size-7" title="Save coordinates"
                  disabled={setLocation.isPending} onClick={saveCoords}>
                  <Check className="size-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="size-7" onClick={() => setCoordsEdit(false)}>
                  <X className="size-3.5" />
                </Button>
              </>
            ) : (
              <>
                <span className="font-mono text-muted-foreground">
                  {isPlaced(selected) ? `${selected.lat.toFixed(5)}, ${selected.lng.toFixed(5)}` : "not on the map"}
                </span>
                {routeKm != null && parent && (
                  <span className="text-muted-foreground"
                    title={`Along the drawn cable route to ${parent.name}`}>
                    <span className="font-semibold text-foreground">{fmtKm(routeKm)}</span> cable
                    to <span className="font-mono">{parent.name}</span>
                  </span>
                )}
                {linkKm != null && parent && (
                  // labeled honestly: this is the chord, not cable length — a
                  // splicing crew quoting drum meters off it comes up short
                  <span className="text-muted-foreground"
                    title={`Straight-line distance to ${parent.name} — not cable length`}>
                    {fmtKm(linkKm)} straight-line{routeKm == null && parent ? <> to <span className="font-mono">{parent.name}</span></> : null}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-0.5">
                  {isPlaced(selected) && (
                    <>
                      <Button variant="ghost" size="icon" className="size-7 text-muted-foreground"
                        title="Copy coordinates"
                        onClick={() => {
                          void navigator.clipboard.writeText(`${selected.lat}, ${selected.lng}`)
                          toast.success("Coordinates copied")
                        }}>
                        <Copy className="size-3.5" />
                      </Button>
                      <Button asChild variant="ghost" size="icon" className="size-7 text-muted-foreground"
                        title="Navigate there (Google Maps)">
                        <a target="_blank" rel="noreferrer"
                          href={`https://www.google.com/maps/dir/?api=1&destination=${selected.lat},${selected.lng}`}>
                          <Navigation className="size-3.5" />
                        </a>
                      </Button>
                    </>
                  )}
                  {canWrite && isPlaced(selected) && parent && isPlaced(parent) && (
                    <Button variant="ghost" size="icon" className="size-7 text-muted-foreground"
                      title={selRoute ? `Edit the cable route to ${parent.name}`
                        : `Draw the cable route to ${parent.name}`}
                      onClick={startRouteEdit}>
                      <Spline className="size-3.5" />
                    </Button>
                  )}
                  {canWrite && (
                    <Button variant="ghost" size="icon" className="size-7 text-muted-foreground"
                      title="Type coordinates (paste from a GPS app)"
                      onClick={() => {
                        setCoordsText(isPlaced(selected) ? `${selected.lat}, ${selected.lng}` : "")
                        setCoordsEdit(true)
                      }}>
                      <Pencil className="size-3.5" />
                    </Button>
                  )}
                </span>
              </>
            )}
          </div>
          <div className="overflow-y-auto p-3">
            <DeviceDetail device={selected} tab={detailTab} onTab={setDetailTab} />
          </div>
        </Card>
      )}

      {/* first-run nudge ------------------------------------------------------- */}
      {!isLoading && placed.length === 0 && !placing && (
        <div className="pointer-events-none absolute inset-0 z-[999] flex items-center justify-center">
          <div className="pointer-events-auto flex flex-col items-center gap-2 rounded-xl border border-border-strong bg-popover/95 dark:bg-popover/95 px-6 py-5 text-center backdrop-blur">
            <MapPin className="size-5 text-muted-foreground" />
            <p className="text-sm font-medium">No devices on the map yet</p>
            {canWrite && devices.length > 0 ? (
              <>
                <p className="max-w-64 text-xs text-muted-foreground">
                  Pick a device, then click its spot on the map.
                </p>
                <Button size="sm" className="mt-1" onClick={() => setPlaceOpen(true)}>
                  <MapPin className="size-3.5" /> Place devices
                </Button>
              </>
            ) : (
              <p className="max-w-64 text-xs text-muted-foreground">
                {devices.length === 0 ? "Add devices on the Network page first."
                  : "An operator can pin devices to the map from here."}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
