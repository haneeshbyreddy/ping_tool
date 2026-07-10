// Geographic NOC view: every placed device is a live status pin, topology links
// draw between placed parent/child pairs, and clicking a pin opens the same
// Health/Optical/Ports panel the Network tree uses. Placement is dashboard-side
// only (lat/lng on org_devices) — the edge never sees coordinates.
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import L from "leaflet"
import { MapContainer, Marker, Polyline, TileLayer, ZoomControl, useMap, useMapEvents } from "react-leaflet"
import "leaflet/dist/leaflet.css"
import {
  Check, ChevronRight, Copy, Crosshair, Expand, EyeOff, ListTree, LocateFixed,
  MapPin, Maximize2, Navigation, Pencil, Search, Shrink, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { inventoryApi, orgsApi, ApiError } from "@/lib/api"
import { mapRegionOf } from "@/lib/map-regions"
import type { OnuOptic, OrgDevice } from "@/lib/types"
import { DeviceDetail, DeviceMetrics, isOpticalOlt, RowTag, type DeviceTab } from "@/components/device-detail"
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

// ---- PON overlay (Phase 2) --------------------------------------------------
// EPON/GPON ranging gives distance only, never bearing, so the "wire" to each
// ONU is honest about what the OLT actually knows: spoke length is the ranged
// fiber distance, the angle is an even spread stable-sorted by pon_port/onu_id
// (a given ONU keeps its bearing between walks).

type OnuTone = "ok" | "warn" | "crit" | "offline" | "los" | "gasp"

function onuTone(o: OnuOptic): OnuTone {
  if (o.state === "dying_gasp") return "gasp"
  if (o.state === "los") return "los"
  if (o.state !== "online") return "offline"
  if (o.severity === "crit") return "crit"
  if (o.severity === "warn") return "warn"
  return "ok"
}

const SPOKE_STYLE: Record<OnuTone, { color: string; dash?: string; cls?: string; opacity: number }> = {
  ok: { color: "var(--success)", opacity: 0.3 },
  warn: { color: "var(--warning)", opacity: 0.75 },
  crit: { color: "var(--destructive)", opacity: 0.9 },
  offline: { color: "var(--muted-foreground)", dash: "3 5", opacity: 0.45 },
  los: { color: "var(--destructive)", dash: "3 5", opacity: 0.8 },
  gasp: { color: "var(--destructive)", cls: "wisp-spoke--gasp", opacity: 0.9 },
}
// when the fan is over the cap, trouble outranks health — the 65th quiet ONU
// can hide behind "+N more"; a LOS can't
const TONE_RANK: Record<OnuTone, number> = { gasp: 0, los: 1, crit: 2, warn: 3, offline: 4, ok: 5 }

const SPOKE_CAP = 64
const MIN_SPOKE_M = 60 // shorter than this and the spoke end hides under the OLT pin

interface Spoke { onu: OnuOptic; tone: OnuTone; pos: [number, number] }

function buildSpokes(onus: OnuOptic[], center: [number, number]): { spokes: Spoke[]; hidden: number } {
  const byPon = [...onus].sort((a, b) =>
    (a.pon_port ?? "").localeCompare(b.pon_port ?? "", undefined, { numeric: true })
    || (a.onu_id ?? 0) - (b.onu_id ?? 0)
    || a.onu_key.localeCompare(b.onu_key))
  let shown = byPon
  if (byPon.length > SPOKE_CAP) {
    const keep = new Set([...byPon]
      .sort((a, b) => TONE_RANK[onuTone(a)] - TONE_RANK[onuTone(b)])
      .slice(0, SPOKE_CAP))
    shown = byPon.filter((o) => keep.has(o)) // cap by severity, angles stay in PON order
  }
  const known = shown.map((o) => o.distance_m).filter((m): m is number => m != null)
  const fallback = known.length ? [...known].sort((a, b) => a - b)[known.length >> 1] : 500
  const latM = 111_320
  const lngM = latM * Math.cos((center[0] * Math.PI) / 180)
  return {
    spokes: shown.map((o, i) => {
      const ang = -Math.PI / 2 + (2 * Math.PI * i) / shown.length
      const r = Math.max(o.distance_m ?? fallback, MIN_SPOKE_M)
      return {
        onu: o,
        tone: onuTone(o),
        pos: [center[0] + (r * Math.cos(ang)) / latM, center[1] + (r * Math.sin(ang)) / lngM],
      }
    }),
    hidden: byPon.length - shown.length,
  }
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

function onuIcon(o: OnuOptic, tone: OnuTone): L.DivIcon {
  const title = [
    o.name || o.serial || o.onu_key,
    o.rx_dbm != null ? `${o.rx_dbm.toFixed(1)} dBm` : "no Rx",
    o.distance_m != null ? fmtKm(o.distance_m / 1000) : null,
    o.state !== "online" ? o.state : null,
  ].filter(Boolean).join(" · ")
  return cachedDivIcon(`<div class="wisp-onu wisp-onu--${tone}" title="${esc(title)}"></div>`)
}

function moreOnusIcon(hidden: number): L.DivIcon {
  return cachedDivIcon(
    `<div class="wisp-onu-more" title="Open the Optical tab for the full list">+${hidden} more</div>`)
}

function distanceKm(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const R = 6371, toR = Math.PI / 180
  const dLat = (bLat - aLat) * toR, dLng = (bLng - aLng) * toR
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(aLat * toR) * Math.cos(bLat * toR) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}
const fmtKm = (km: number) => km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(km < 10 ? 1 : 0)} km`

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
        className="h-8 bg-card/95 pl-8 text-xs backdrop-blur"
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
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left hover:bg-accent/40"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onDevice(d))}>
              <StatusDot tone={pinTone(d)} />
              <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
              {!isPlaced(d) && <RowTag tone="muted">not placed</RowTag>}
              <span className="ml-auto shrink-0 font-mono text-[0.6875rem] text-muted-foreground">{d.ip_address}</span>
            </button>
          ))}
          {placeHits.map((p, i) => (
            <button key={`${p.lat},${p.lng},${i}`}
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-accent/40"
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
  const [editPins, setEditPins] = useState(false)
  const [troubleOnly, setTroubleOnly] = useState(false)
  const [lowZoom, setLowZoom] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [coordsEdit, setCoordsEdit] = useState(false)
  const [coordsText, setCoordsText] = useState("")
  const wrapRef = useRef<HTMLDivElement>(null)
  const troubleIdx = useRef(0)
  // dark tiles for the dark theme; light for light. Read once — a theme flip
  // remounts routes rarely enough that chasing it live isn't worth a listener.
  const [dark] = useState(() => document.documentElement.classList.contains("dark"))
  useNow()

  const { data, isLoading } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    // same self-heal fallback as the Network page: SSE can die silently
    refetchInterval: 30_000,
  })
  // Settings → Map area (orgs.map_region): the viewport lock for this org
  const orgsQ = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const region = mapRegionOf(orgsQ.data?.orgs.find((o) => o.org_id === scopeOrg)?.map_region)

  const devices = useMemo(() => data?.devices ?? [], [data])
  const placed = useMemo(() => devices.filter(isPlaced), [devices])
  const unplaced = useMemo(() => devices.filter((d) => !isPlaced(d)), [devices])
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const selected = selectedId != null ? byId.get(selectedId) ?? null : null
  const placing = placingId != null ? byId.get(placingId) ?? null : null

  // PON overlay: selecting a placed OLT fans its ONUs out as distance-true
  // spokes. Same query key as the Optical tab, so the two share one fetch.
  const [focusOnuId, setFocusOnuId] = useState<number | null>(null)
  const ponOlt = selected && isOpticalOlt(selected) && isPlaced(selected) ? selected : null
  const opticsQ = useQuery({
    queryKey: ["optics", ponOlt?.id],
    queryFn: () => inventoryApi.optics(ponOlt!.id),
    enabled: ponOlt != null,
    refetchInterval: 30_000,
  })

  // Two boxes in one cabinet share coordinates; fan them out ~15 m so both stay
  // visible and clickable. Display-only — the stored location is untouched.
  const pinPos = useMemo(() => {
    const groups = new Map<string, Placed[]>()
    for (const d of placed) {
      const k = `${d.lat.toFixed(5)},${d.lng.toFixed(5)}`
      const g = groups.get(k)
      if (g) g.push(d)
      else groups.set(k, [d])
    }
    const pos = new Map<number, [number, number]>()
    for (const g of groups.values()) {
      if (g.length === 1) {
        pos.set(g[0].id, [g[0].lat, g[0].lng])
        continue
      }
      g.sort((a, b) => a.id - b.id).forEach((d, i) => {
        const ang = (2 * Math.PI * i) / g.length
        pos.set(d.id, [d.lat + 0.00014 * Math.cos(ang), d.lng + 0.00018 * Math.sin(ang)])
      })
    }
    return pos
  }, [placed])

  const pon = useMemo(() => {
    if (!ponOlt || !opticsQ.data?.onus.length) return null
    const center = pinPos.get(ponOlt.id) ?? [ponOlt.lat, ponOlt.lng] as [number, number]
    return { center, ...buildSpokes(opticsQ.data.onus, center) }
  }, [ponOlt, opticsQ.data, pinPos])

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

  // Search: a placed device flies to its pin; an unplaced one goes straight into
  // placement mode (search Gachibowli → pick the device → click the map).
  const searchDevice = (d: OrgDevice) => {
    const map = mapRef.current
    if (isPlaced(d)) {
      map?.flyTo([d.lat, d.lng], Math.max(map.getZoom(), 15))
      setDetailTab("health")
      setSelectedId(d.id)
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
    if (placingId == null) return
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPlacingId(null) }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [placingId])

  const onMapClick = useCallback((ll: L.LatLng) => {
    if (placingId != null) {
      setLocation.mutate({ id: placingId, lat: ll.lat, lng: ll.lng })
      setSelectedId(placingId)
      setPlacingId(null)
    } else {
      setSelectedId(null)
    }
  }, [placingId, setLocation])

  const fitAll = () => {
    if (placed.length === 0) return
    mapRef.current?.fitBounds(L.latLngBounds(placed.map((d) => [d.lat, d.lng])), FIT_PADDING)
  }

  const locateMe = () => {
    if (!navigator.geolocation) { toast.error("Geolocation is not available in this browser"); return }
    navigator.geolocation.getCurrentPosition(
      (pos) => mapRef.current?.flyTo([pos.coords.latitude, pos.coords.longitude], 14),
      () => toast.error("Couldn't get your location"),
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

  const onZoom = useCallback((z: number) => setLowZoom(z < 12), [])

  // field flow: tech reads GPS off the phone, pastes "17.4401, 78.3489"
  useEffect(() => { setCoordsEdit(false); setCoordsText(""); setFocusOnuId(null) }, [selectedId])
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
    const out: Array<{ key: string; from: Placed; to: Placed; tone: string; backup: boolean }> = []
    const placedById = new Map(placed.map((d) => [d.id, d]))
    for (const d of placed) {
      const tone = pinTone(d)
      if (d.parent_device_id != null) {
        const p = placedById.get(d.parent_device_id)
        if (p) out.push({ key: `p${d.id}`, from: p, to: d, tone, backup: false })
      }
      for (const bp of d.backup_parents) {
        const p = placedById.get(bp)
        if (p) out.push({ key: `b${d.id}-${bp}`, from: p, to: d, tone, backup: true })
      }
    }
    return out
  }, [placed])

  if (!scopeOrg) return <NeedsOrg />

  const down = troubles.filter((d) => pinTone(d) === "destructive").length
  const degraded = troubles.length - down

  const lineColor = (tone: string) =>
    tone === "destructive" ? "var(--destructive)"
      : tone === "warning" ? "var(--warning)" : "var(--muted-foreground)"

  const parent = selected?.parent_device_id != null ? byId.get(selected.parent_device_id) : null
  const linkKm = selected && isPlaced(selected) && parent && isPlaced(parent)
    ? distanceKm(selected.lat, selected.lng, parent.lat, parent.lng) : null

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
        <TileLayer
          key={dark ? "dark" : "light"}
          url={`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`}
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
          subdomains="abcd"
          maxZoom={20}
        />
        <ZoomControl position="bottomright" />
        <MapEvents org={scopeOrg} onMapClick={onMapClick} onZoom={onZoom} />
        <ViewController placed={placed} ready={!isLoading && orgsQ.isSuccess}
          hasSavedView={!!initialView} bounds={region.bounds} />
        {links.map((l) => {
          // a selected device lights up its whole downstream path
          const emphasized = selectedId != null
            && (l.to.id === selectedId || downstream.has(l.to.id))
          const dimmed = troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized
          return (
            <Polyline
              key={l.key}
              // never a click target — a line crossing the viewport would otherwise
              // swallow map clicks during placement
              interactive={false}
              positions={[
                pinPos.get(l.from.id) ?? [l.from.lat, l.from.lng],
                pinPos.get(l.to.id) ?? [l.to.lat, l.to.lng],
              ]}
              pathOptions={{
                color: emphasized && l.tone === "muted" ? "var(--primary)" : lineColor(l.tone),
                weight: emphasized ? 2.5 : l.tone === "destructive" ? 2 : 1.5,
                opacity: dimmed ? 0.12 : emphasized ? 0.9 : l.tone === "muted" ? 0.35 : 0.65,
                dashArray: l.backup ? "4 6" : undefined,
              }}
            />
          )
        })}
        {/* PON fan: one spoke per ONU off the selected OLT, under the pins */}
        {pon && pon.spokes.map((s) => {
          const st = SPOKE_STYLE[s.tone]
          return (
            <Polyline
              key={`onu-l-${s.onu.id}-${s.tone}`}
              interactive={false}
              // className is construction-only in Leaflet (setStyle ignores it),
              // so it rides as a top-level prop and the tone-keyed remount above
              // keeps it honest when an ONU's state changes
              className={st.cls}
              positions={[pon.center, s.pos]}
              pathOptions={{
                color: st.color,
                weight: s.tone === "ok" ? 1 : 1.75,
                opacity: st.opacity,
                dashArray: st.dash,
              }}
            />
          )
        })}
        {pon && pon.spokes.map((s) => (
          <Marker
            key={`onu-m-${s.onu.id}`}
            position={s.pos}
            icon={onuIcon(s.onu, s.tone)}
            zIndexOffset={-200}
            eventHandlers={{
              click: () => {
                if (placingId != null) return
                setFocusOnuId(s.onu.id)
                setDetailTab("optical")
              },
            }}
          />
        ))}
        {pon && pon.hidden > 0 && (
          <Marker
            position={pon.center}
            icon={moreOnusIcon(pon.hidden)}
            zIndexOffset={1200}
            eventHandlers={{ click: () => { if (placingId == null) setDetailTab("optical") } }}
          />
        )}
        {placed.map((d) => {
          const dim = troubleOnly && !isTrouble(d) && d.id !== selectedId
          const impact = downstream.has(d.id)
          return (
            <Marker
              key={d.id}
              position={pinPos.get(d.id) ?? [d.lat, d.lng]}
              icon={pinIcon(d, { selected: d.id === selectedId, dim, impact })}
              draggable={editPins && canWrite}
              // markers swallow map clicks, so placement mode ignores pin taps
              eventHandlers={{
                click: () => {
                  if (placingId != null) return
                  setDetailTab("health")
                  setSelectedId(d.id === selectedId ? null : d.id)
                },
                dragend: (e) => {
                  const ll = (e.target as L.Marker).getLatLng()
                  setLocation.mutate({ id: d.id, lat: ll.lat, lng: ll.lng })
                },
              }}
              zIndexOffset={d.id === selectedId ? 1000
                : pinTone(d) === "destructive" ? 500 : impact ? 300 : 0}
            />
          )
        })}
      </MapContainer>

      {/* search + status strip -------------------------------------------------- */}
      <div className="pointer-events-none absolute top-3 left-3 z-[1000] flex max-w-[calc(100%-6rem)] flex-wrap items-center gap-2">
        <MapSearch devices={devices} bounds={region.bounds}
          onDevice={searchDevice} onPlace={searchPlace} />
        <div className="pointer-events-auto flex h-8 items-center gap-2.5 rounded-lg border bg-card/95 px-3 text-xs backdrop-blur">
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
            className={cn("pointer-events-auto h-8 backdrop-blur", !troubleOnly && "bg-card/95")}
            title="Dim everything that's healthy"
            onClick={() => setTroubleOnly(!troubleOnly)}>
            <EyeOff className="size-3.5" /> Trouble only
          </Button>
        )}
        {canWrite && unplaced.length > 0 && (
          <Button variant="outline" size="sm"
            className="pointer-events-auto h-8 bg-card/95 backdrop-blur"
            onClick={() => { setPlaceOpen(!placeOpen); setPlacingId(null) }}>
            <MapPin className="size-3.5" /> Place devices
            <span className="rounded bg-muted px-1.5 py-px font-mono text-[0.6875rem]">{unplaced.length}</span>
          </Button>
        )}
      </div>

      {/* placement banner ------------------------------------------------------ */}
      {placing && (
        <div className="absolute top-14 left-1/2 z-[1000] flex -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-card/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Crosshair className="size-3.5 text-primary" />
          <span>Click the map to place <span className="font-mono font-semibold">{placing.name}</span></span>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setPlacingId(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {/* controls — slide left of the device panel so they stay clickable ------- */}
      <div className={cn("absolute top-3 right-3 z-[1000] flex flex-col gap-1.5",
        selected && "md:right-[calc(380px+1.5rem)]")}>
        <Button variant="outline" size="icon" className="size-8 bg-card/95 backdrop-blur"
          title="Fit all pins" onClick={fitAll} disabled={placed.length === 0}>
          <Maximize2 className="size-3.5" />
        </Button>
        <Button variant="outline" size="icon" className="size-8 bg-card/95 backdrop-blur"
          title="Go to my location" onClick={locateMe}>
          <LocateFixed className="size-3.5" />
        </Button>
        <Button variant="outline" size="icon" className="size-8 bg-card/95 backdrop-blur"
          title={fullscreen ? "Exit fullscreen" : "Fullscreen (NOC wall)"} onClick={toggleFullscreen}>
          {fullscreen ? <Shrink className="size-3.5" /> : <Expand className="size-3.5" />}
        </Button>
        {canWrite && (
          <Button variant={editPins ? "default" : "outline"} size="icon"
            className={cn("size-8 backdrop-blur", !editPins && "bg-card/95")}
            title={editPins ? "Done moving pins" : "Move pins (drag)"}
            onClick={() => setEditPins(!editPins)}>
            <Pencil className="size-3.5" />
          </Button>
        )}
      </div>
      {editPins && canWrite && (
        <div className={cn("absolute right-3 top-[10rem] z-[1000] rounded-lg border border-warning/40 bg-card/95 px-2.5 py-1.5 text-[0.75rem] text-warning backdrop-blur",
          selected && "md:right-[calc(380px+1.5rem)]")}>
          drag pins to move them
        </div>
      )}

      {/* unplaced drawer ------------------------------------------------------- */}
      {placeOpen && canWrite && (
        <Card className="absolute top-14 left-3 z-[1000] flex max-h-[60%] w-72 flex-col gap-0 overflow-hidden bg-card/95 py-0 backdrop-blur">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <p className="text-xs font-semibold">Not on the map yet</p>
            <Button variant="ghost" size="icon" className="size-6" onClick={() => setPlaceOpen(false)}>
              <X className="size-3.5" />
            </Button>
          </div>
          <div className="overflow-y-auto">
            {unplaced.map((d) => (
              <button key={d.id}
                className="flex h-9 w-full items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-accent/40"
                onClick={() => { setPlacingId(d.id); setPlaceOpen(false); setSelectedId(null) }}>
                <StatusDot tone={pinTone(d)} />
                <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
                {d.device_type && <span className="text-[0.6875rem] text-muted-foreground">{d.device_type}</span>}
                <span className="ml-auto shrink-0 font-mono text-[0.6875rem] text-muted-foreground">{d.ip_address}</span>
              </button>
            ))}
            {unplaced.length === 0 && (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">Every device is placed.</p>
            )}
          </div>
        </Card>
      )}

      {/* device panel ---------------------------------------------------------- */}
      {selected && (
        <Card className="absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden bg-card/95 py-0 backdrop-blur md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)] md:w-[380px]">
          <div className="flex items-start gap-2.5 border-b px-4 py-3">
            <span className="mt-1"><StatusDot tone={pinTone(selected)} /></span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="min-w-0 truncate font-mono text-sm font-semibold">{selected.name}</p>
                {!!selected.maintenance && <RowTag tone="warning">maint</RowTag>}
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
                {linkKm != null && parent && (
                  <span className="text-muted-foreground" title={`Link distance to ${parent.name}`}>
                    {fmtKm(linkKm)} to <span className="font-mono">{parent.name}</span>
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
            <DeviceDetail device={selected} tab={detailTab} onTab={setDetailTab}
              focusOnuId={focusOnuId} />
          </div>
        </Card>
      )}

      {/* first-run nudge ------------------------------------------------------- */}
      {!isLoading && placed.length === 0 && !placing && (
        <div className="pointer-events-none absolute inset-0 z-[999] flex items-center justify-center">
          <div className="pointer-events-auto flex flex-col items-center gap-2 rounded-xl border bg-card/95 px-6 py-5 text-center backdrop-blur">
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
