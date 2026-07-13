// Geographic NOC view: every placed device is a live status pin, topology links
// draw between placed parent/child pairs, and clicking a pin opens the same
// Health/Optical/Ports panel the Network tree uses. Placement is dashboard-side
// only (lat/lng on org_devices) — the edge never sees coordinates.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import L from "leaflet"
import { Circle, MapContainer, Marker, Polyline, ZoomControl } from "react-leaflet"
import "leaflet/dist/leaflet.css"
import {
  Check, ChevronRight, Copy, Crosshair, Expand, EyeOff, Layers, ListTree, LocateFixed,
  MapPin, Maximize2, Navigation, Pencil, Shrink, Spline, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { inventoryApi, orgsApi, ApiError } from "@/lib/api"
import { mapRegionOf } from "@/lib/map-regions"
import { type OrgDevice, type PonFault } from "@/lib/types"
import { DeviceDetail, DeviceMetrics, RowTag, type DeviceTab } from "@/components/device-detail"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { durationSince } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

import {
  BASEMAP_KEY, BASEMAP_LABEL, GOOGLE_BASEMAPS, GoogleLayer, StreetsTiles,
  loadBasemap, type Basemap,
} from "@/map/basemaps"
import { buildClusters, clusterIcon, project, toneRank, type SiteCluster } from "@/map/clusters"
import { cutIcon, pointAlong, ponPath, subPath } from "@/map/cut"
import { distanceKm, fmtKm, polyKm } from "@/map/geometry"
import {
  isDownState, isPlaced, isTrouble, meIcon, pinIcon, pinTone, vertexIcon, type Placed,
} from "@/map/pins"
import { MapSearch, type PlaceHit } from "@/map/search"
import { FIT_PADDING, MapEvents, ViewController, loadView } from "@/map/view"

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
