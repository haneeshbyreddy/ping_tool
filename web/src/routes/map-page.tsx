import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  useLocation as useNavLocation, useNavigate, useSearchParams,
} from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import L from "leaflet"
import { Circle, MapContainer, Marker, Polyline, ZoomControl } from "react-leaflet"
import "leaflet/dist/leaflet.css"
import {
  ArrowLeft, Check, ChevronDown, ChevronRight, Copy, Crosshair,
  Expand, EyeOff, Layers, ListTree, LocateFixed, MapPin, MapPinOff, Maximize2, Navigation,
  PanelRight, Pencil, Plus, Route, Scissors, Shrink, Slash, Spline, Trash2, Undo2, Users, X,
  Cable as CableIcon,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDarkMode } from "@/hooks/use-dark-mode"
import { useNow } from "@/hooks/use-now"
import { PanelResizeGrip, useResizablePanel } from "@/hooks/use-resizable-panel"
import { fieldApi, inventoryApi, orgsApi, ApiError } from "@/lib/api"
import { mapRegionOf } from "@/lib/map-regions"
import { isPassiveType, type Cable, type FibrePoint, type OnuPlace, type OrgDevice,
         type PonFault } from "@/lib/types"
import {
  DeviceDetail, DevicePanelHeader, RowTag, deviceTabs, type DeviceTab,
} from "@/components/device-detail"
import { CableForm, CableList, CablePanel,
         type MoveTarget } from "@/components/cable-record"
import { FibrePanel, type FarLanding } from "@/components/coupler-tray"
import {
  CABLE_DASH, cableIcon, cableLabelPos, cablePolyline, cableTraced,
} from "@/map/cables"
import { SubscriberDetail } from "@/components/subscriber-detail"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { isPlumbing, portKindsFor } from "@/lib/fiber"
import { durationSince, NO_ASSIGNED_DEVICES, onuName } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

import {
  AttributionPrefix, BASEMAP_KEY, BASEMAP_LABEL, GOOGLE_BASEMAPS, GoogleLayer,
  StreetsTiles, loadBasemap, type Basemap,
} from "@/map/basemaps"
import { buildClusters, clusterIcon, project, toneRank, type SiteCluster } from "@/map/clusters"
import { cutIcon, pointAlong, ponPath, subPath } from "@/map/cut"
import { DevHoverCard } from "@/map/devhover"
import { alongKm, distanceKm, fmtKm, nearestOnPath, pointAt, polyKm } from "@/map/geometry"
import { LinkHoverProbe, hoverIcon, projectLinks, type HoverLink, type LinkHover } from "@/map/linkhover"
import { bindLinkPorts, bwRank, linkBwIcon, linkKey, linkLabelPos, linkRateBody, linkTone, type LinkBinding } from "@/map/linklabel"
import {
  MARK_Z_DOWN, MARK_Z_GEAR, MARK_Z_SELECTED, isDownState, isPlaced, isTrouble,
  markZIndex, meIcon, pinIcon, pinTone, vertexIcon, type Placed,
} from "@/map/pins"
import {
  REF_DASH, REF_HOVER_BOOST, REF_NAME_DY, isRefDark, isRefEvidence, refBwIcon,
  refChipPos, refHasChip, refLineTone, refNameIcon, refOnuIcon, refZIndex,
} from "@/map/refonu"
import { RefHoverCard } from "@/map/refhover"
import { SiteHoverCard, type SiteHoverCtx } from "@/map/sitehover"
import {
  DROP_DASH, branchIcon, dropAnchor, dropTone, loadsById, passivePinLabel,
  passiveTitle,
} from "@/map/drops"
import {
  PLANT_LABEL, cumulativeSplit, nearestFeeder, nearestPassive, plantInScope,
  type PlantKind,
} from "@/map/plant"
import { PlantMenu, type ArmKind, type PlantMenuAnchor } from "@/map/plantmenu"
import {
  AttachCustomerDialog, nearestRegion, PlantCreateDialog,
  type CustomerDraft, type PlantDraft,
} from "@/components/plant-create"
import { detailFrom } from "@/map/detail"
import { MapSearch, type OnuHit, type PlaceHit } from "@/map/search"
import {
  CASING_OPACITY, CASING_OPACITY_HOVER, casingAt, fiberBoost, lineScale, strokeAt,
} from "@/map/stroke"
import { FIT_PADDING, InvalidateOnResize, MapEvents, ViewController, loadView } from "@/map/view"
import {
  trailStyle, workerCensus, workerIcon, workerPlaced, workerState, workerZIndex,
} from "@/map/workers"

const BW_LABELS_KEY = "wisp:map:bw-labels"
const REF_ONUS_KEY = "wisp:map:ref-onus"
const WORKERS_KEY = "wisp:map:workers"
const GOOGLE_LABELS_KEY = "wisp:map:google-labels"
const UNCABLED_KEY = "wisp:map:uncabled"

const UNCABLED_DASH = "3 6"

const ponKey = (p: OnuPlace): string => p.pon_port ?? "—"

interface PonRow { pon: string; total: number; dark: number }

const HULL_MIN_M = 30

const CASING_OVER = 3
const CASING_OVER_FINE = 1.5

type RouteEdit = { points: Array<[number, number]> } & (
  | { kind: "drop"; mac: string }
  | { kind: "cable"; cableId: number | null; name: string
      endA?: FibreEnd | null; endB?: FibreEnd | null }
)

type FibreEnd = { device_id?: number; mac?: string; name: string }

const SNAP_PX = 24
const SNAP_M = 8

const MARK_DY_PX = 12

const CUT_SLACK_PX = 14

const NEAR_MISS_M = 25

const foldedTogether = (a: [number, number], b: [number, number]): boolean =>
  a[0] === b[0] && a[1] === b[1]

export function MapPage() {
  const { scopeOrg, canWrite, isWorker } = useAuth()
  const dark = useDarkMode()
  const navigate = useNavigate()
  const navLocation = useNavLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const mapRef = useRef<L.Map | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  // Selecting a device and OPENING ITS PANEL are two different things. A pin click, a
  // placement or a link chip still means both (`selectDevice`); the list-driven paths — a
  // search hit, a site-card row — select the pin WITHOUT the panel and put up the PON focus
  // bar instead, because the answer they were reaching for is usually on the map, not in a
  // panel covering it. The panel is one click away either way: its row carries the button,
  // and clicking the pin still opens it.
  const [panelFor, setPanelFor] = useState<number | null>(null)
  const selectDevice = useCallback((id: number | null) => {
    setSelectedId(id); setPanelFor(id)
  }, [])
  const [cableOpen, setCableOpenRaw] = useState<number | null>(null)
  const [traceCore, setTraceCore] = useState<number | null>(null)
  const cableDelete = useConfirm()
  const [cableList, setCableList] = useState(false)
  const [splitAt, setSplitAt] = useState<
    { cableId: number; cableName: string } | null>(null)
  const [fibreOpen, setFibreOpen] = useState(false)
  const [trayError, setTrayError] = useState<string | null>(null)
  const [traceFrom, setTraceFrom] = useState<
    { cableId: number; coreNo: number } | null>(null)
  const setCableOpen = useCallback((id: number | null) => {
    setCableOpenRaw(id)
    setTraceCore(null)
  }, [])
  const [cableForm, setCableForm] = useState<
    { id?: number; name: string; cores: number | null
      path?: Array<[number, number]>
      ends?: [FibreEnd | null, FibreEnd | null]
      near?: [{ end: FibreEnd; m: number } | null,
              { end: FibreEnd; m: number } | null] } | null>(null)
  const [hoverId, setHoverId] = useState<number | null>(null)
  const [hoverSiteId, setHoverSiteId] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<DeviceTab>("health")
  const [detailOnu, setDetailOnu] = useState<{ deviceId: number; mac: string } | null>(null)
  const [placingId, setPlacingId] = useState<number | null>(null)
  const [placingOnu, setPlacingOnu] =
    useState<{ mac: string; label: string } | null>(null)
  const [selectedOnuMac, setSelectedOnuMac] = useState<string | null>(null)
  const [hoverOnuMac, setHoverOnuMac] = useState<string | null>(null)
  const [focusFlying, setFocusFlying] = useState(false)
  const [placeOpen, setPlaceOpen] = useState(false)
  const [plantMenu, setPlantMenu] = useState<PlantMenuAnchor | null>(null)
  const [plantDraft, setPlantDraft] = useState<PlantDraft | null>(null)
  const [customerDraft, setCustomerDraft] = useState<CustomerDraft | null>(null)
  const [plantDelete, setPlantDelete] = useState<OrgDevice | null>(null)
  const confirmPlantDelete = useConfirm()
  const [armed, setArmed] = useState<{ kind: ArmKind; passiveId: number | null } | null>(null)
  const [addNext, setAddNext] = useState(false)
  const [routeEdit, setRouteEdit] = useState<RouteEdit | null>(null)
  const [editPins, setEditPins] = useState(false)
  const [troubleOnly, setTroubleOnly] = useState(false)
  const [zoom, setZoom] = useState(4)
  const [siteAnchor, setSiteAnchor] = useState<number | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [hover, setHover] = useState<LinkHover | null>(null)
  const [coordsEdit, setCoordsEdit] = useState(false)
  const [coordsText, setCoordsText] = useState("")
  const confirmUnpin = useConfirm()
  const [basemap, setBasemap] = useState<Basemap>(loadBasemap)
  const [layersOpen, setLayersOpen] = useState(false)
  const [refOnus, setRefOnus] = useState(() => {
    try { return localStorage.getItem(REF_ONUS_KEY) === "on" } catch { return false }
  })
  const toggleRefOnus = () => {
    setRefOnus((v) => {
      try { localStorage.setItem(REF_ONUS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  const [showWorkers, setShowWorkers] = useState(() => {
    try { return localStorage.getItem(WORKERS_KEY) === "on" } catch { return false }
  })
  const toggleWorkers = () => {
    setShowWorkers((v) => {
      try { localStorage.setItem(WORKERS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  const [showUncabled, setShowUncabled] = useState(() => {
    try { return localStorage.getItem(UNCABLED_KEY) !== "off" } catch { return true }
  })
  const toggleUncabled = () => {
    setShowUncabled((v) => {
      try { localStorage.setItem(UNCABLED_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  const [bwLabels, setBwLabels] = useState(() => {
    try { return localStorage.getItem(BW_LABELS_KEY) !== "off" } catch { return true }
  })
  const toggleBwLabels = () => {
    setBwLabels((v) => {
      try { localStorage.setItem(BW_LABELS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  const [googleLabels, setGoogleLabels] = useState(() => {
    try { return localStorage.getItem(GOOGLE_LABELS_KEY) !== "off" } catch { return true }
  })
  const toggleGoogleLabels = () => {
    setGoogleLabels((v) => {
      try { localStorage.setItem(GOOGLE_LABELS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
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
    toast.error(`Google basemap unavailable (${why}). Showing the fallback map`)
    setGoogleDown(true)
  }, [])
  const [myLoc, setMyLoc] = useState<{ lat: number; lng: number; acc: number } | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const troubleIdx = useRef(0)
  const now = useNow()

  const { data, isLoading } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const routesQ = useQuery({
    queryKey: ["routes", scopeOrg],
    queryFn: () => inventoryApi.routes(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const cablesQ = useQuery({
    queryKey: ["cables", scopeOrg],
    queryFn: () => inventoryApi.cables(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const portsQ = useQuery({
    queryKey: ["device-ports", scopeOrg],
    queryFn: () => inventoryApi.devicePorts(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  // A FIBRE POINT IS A DEVICE OR A CUSTOMER. The ISPs settled on 2026-08-09 that a
  // customer point is a coupler too — core 1 out to this house, cores 2-4 carrying on
  // to the next three — which is how a lane of daisy-chained houses is recorded. The
  // types, the joints table and `store.point_fibre` all carried a mac already; only
  // the callers that OPEN the panel were device-only.
  const trayPoint = useMemo(
    () => (selectedOnuMac ? { mac: selectedOnuMac }
           : selectedId != null ? { device_id: selectedId } : null),
    [selectedId, selectedOnuMac])
  const fibreQ = useQuery({
    queryKey: ["point-fibre", scopeOrg, selectedId, selectedOnuMac],
    queryFn: () => inventoryApi.pointFibre(trayPoint!, scopeOrg),
    enabled: trayPoint != null && fibreOpen,
  })
  const traceQ = useQuery({
    queryKey: ["fibre-trace", scopeOrg, traceFrom?.cableId ?? null,
               traceFrom?.coreNo ?? null],
    queryFn: () => inventoryApi.traceFibre(traceFrom!.cableId, traceFrom!.coreNo,
                                           scopeOrg),
    enabled: traceFrom != null,
  })
  const routeByKey = useMemo(() => {
    const m = new Map<string, { pts: Array<[number, number]> }>()
    for (const r of routesQ.data?.routes ?? [])
      if (r.waypoints.length > 0)
        m.set(`${r.child_id}:${r.parent_id}`, { pts: r.waypoints })
    return m
  }, [routesQ.data])
  const styleByKey = useMemo(() => {
    const m = new Map<string, {
      label_pos: number | null; cable_id: number | null
      cable_name: string | null; cores: number | null; core_no: number | null
    }>()
    for (const r of routesQ.data?.routes ?? [])
      if (r.label_pos != null || r.cable_id != null || r.core_no != null)
        m.set(`${r.child_id}:${r.parent_id}`, {
          label_pos: r.label_pos, cable_id: r.cable_id,
          cable_name: r.cable_name, cores: r.cores, core_no: r.core_no,
        })
    return m
  }, [routesQ.data])

  const [focusOnuMac, setFocusOnuMac] = useState<string | null>(null)
  const [onuScope, setOnuScope] = useState<{ deviceId: number; pons: string[] } | null>(null)
  const placesQ = useQuery({
    queryKey: ["onu-places", scopeOrg],
    queryFn: () => inventoryApi.onuPlaces(scopeOrg),
    enabled: !!scopeOrg && (refOnus || onuScope != null || placingOnu != null
      || focusOnuMac != null || selectedId != null),
    staleTime: 60_000,
  })
  const places = placesQ.data?.places ?? []

  const setOnuPlace = useMutation({
    mutationFn: ({ mac, lat, lng, label }: {
      mac: string; lat: number | null; lng: number | null; label?: string | null
    }) => inventoryApi.setOnuPlace({ mac, lat, lng, label, org_id: scopeOrg }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      queryClient.invalidateQueries({ queryKey: ["pon-faults"] })
      queryClient.invalidateQueries({ queryKey: ["pon-summary"] })
      queryClient.invalidateQueries({ queryKey: ["optics"] })
    },
    onError: (e) => toast.error(
      `Couldn't save the reference point${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const linkPortsQ = useQuery({
    queryKey: ["link-ports", scopeOrg],
    queryFn: () => inventoryApi.linkPorts(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const linkBindings = useMemo(
    () => bindLinkPorts(linkPortsQ.data?.ports ?? []), [linkPortsQ.data])

  const faultsQ = useQuery({
    queryKey: ["pon-faults-org", scopeOrg],
    queryFn: () => inventoryApi.orgPonFaults(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const dropsQ = useQuery({
    queryKey: ["drops", scopeOrg],
    queryFn: () => inventoryApi.drops(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const loadByPassive = useMemo(
    () => loadsById(dropsQ.data?.splitters), [dropsQ.data])
  const branchFaults = useMemo(
    () => dropsQ.data?.faults ?? [], [dropsQ.data])

  const workersQ = useQuery({
    queryKey: ["field-workers", scopeOrg],
    queryFn: () => fieldApi.workers(scopeOrg),
    enabled: !!scopeOrg && canWrite && showWorkers,
    refetchInterval: 60_000,
  })
  const fieldWorkers = workersQ.data?.workers ?? []
  const workerFreshS = workersQ.data?.fresh_s ?? 300
  const census = useMemo(
    () => workerCensus(fieldWorkers, workerFreshS, now),
    [fieldWorkers, workerFreshS, now])

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

  const orgsQ = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const myOrg = orgsQ.data?.orgs.find((o) => o.org_id === scopeOrg)
  const region = mapRegionOf(myOrg?.map_region)
  const googleKey = myOrg?.google_maps_key?.trim() || null
  const googleActive = !!googleKey && !googleDown
  const detail = useMemo(() => detailFrom(myOrg?.map_detail), [myOrg?.map_detail])

  const devices = useMemo(() => data?.devices ?? [], [data])
  const placed = useMemo(() => devices.filter(isPlaced), [devices])
  const unplaced = useMemo(() => devices.filter((d) => !isPlaced(d)), [devices])
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const selected = selectedId != null ? byId.get(selectedId) ?? null : null
  const placing = placingId != null ? byId.get(placingId) ?? null : null

  useEffect(() => { setFibreOpen(false) }, [selectedId, selectedOnuMac])

  // The cables opened at a box, with the cores still FREE at that end. An enclosure
  // has no ports, so this is what a connect must ask for there. `cable.plan` already
  // carries which cores are used at which END, so no extra read is needed — and a
  // core we get wrong is refused by name at the server anyway.
  const cablesAt = useCallback((deviceId: number) =>
    (cablesQ.data?.cables ?? [])
      .filter((c) => !isPlumbing(c)
        && (c.a.device_id === deviceId || c.b.device_id === deviceId))
      .map((c) => {
        const side = c.a.device_id === deviceId ? "a" : "b"
        return {
          cable_id: c.id, name: c.name, cores: c.cores,
          freeCores: Array.from({ length: c.cores ?? 0 }, (_, i) => i + 1)
            .filter((n) => !c.plan[String(n)]?.[side]),
        }
      }), [cablesQ.data])

  const trayBoxes = useMemo(() => {
    const here = fibreQ.data?.point
    if (!here || here.lat == null || here.lng == null) return []
    const { lat, lng } = here
    const declared = new Set<number>()
    for (const d of devices) {
      if (d.parent_device_id === here.device_id) declared.add(d.id)
      if (d.id === here.device_id && d.parent_device_id) declared.add(d.parent_device_id)
    }
    return placed
      .filter((d) => d.id !== here.device_id)
      .map((d) => ({
        id: d.id, name: d.name, device_type: d.device_type,
        declared: declared.has(d.id),
        port_kinds: portKindsFor(d.device_type),
        ports: portsQ.data?.ports?.[String(d.id)] ?? [],
        cables: cablesAt(d.id),
        km: distanceKm(lat, lng, d.lat!, d.lng!),
      }))
      .sort((a, b) => Number(b.declared) - Number(a.declared) || a.km - b.km)
  }, [fibreQ.data?.point, placed, devices, portsQ.data, cablesAt])

  const boxOf = useCallback((id: number) => {
    const d = byId.get(id)
    if (!d) return undefined
    return {
      id: d.id, name: d.name, device_type: d.device_type, km: null,
      port_kinds: portKindsFor(d.device_type),
      ports: portsQ.data?.ports?.[String(id)] ?? [],
      cables: cablesAt(id),
    }
  }, [byId, portsQ.data, cablesAt])

  const cableMoveTargets = useMemo(() => {
    const c = cablesQ.data?.cables.find((x) => x.id === cableOpen)
    if (!c) return []
    const at = [c.a, c.b].find((e) => e.lat != null && e.lng != null)
    return placed
      .map((d) => ({
        device_id: d.id, name: d.name,
        km: at?.lat != null ? distanceKm(at.lat, at.lng!, d.lat!, d.lng!) : null,
      }))
      .sort((x, y) => (x.km ?? 0) - (y.km ?? 0))
  }, [cablesQ.data, cableOpen, placed])

  const trayPeople = useMemo(() => {
    const here = fibreQ.data?.point
    if (!here || here.lat == null || here.lng == null) return []
    const { lat, lng } = here
    return places
      .filter((p) => p.lat != null && p.lng != null && p.mac !== here.mac)
      .map((p) => ({
        mac: p.mac, name: onuName(p),
        km: distanceKm(lat, lng, p.lat!, p.lng!),
      }))
      .sort((a, b) => a.km - b.km)
  }, [fibreQ.data?.point, places])

  const menuFeeder = useMemo(
    () => (plantMenu && !plantMenu.device
      ? nearestFeeder(plantMenu.lat, plantMenu.lng, devices) : null),
    [plantMenu, devices])
  const menuDropOn = useMemo(
    () => (plantMenu && !plantMenu.device
      ? nearestPassive(plantMenu.lat, plantMenu.lng, devices) : null),
    [plantMenu, devices])

  const shownPlaces = useMemo(() => {
    if (!onuScope) return places
    const { deviceId, pons } = onuScope
    return places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
  }, [places, onuScope])

  const plantPinned = editPins || routeEdit != null || armed != null || addNext
    || placingId != null || placingOnu != null || plantDraft != null
    || customerDraft != null || plantMenu != null || splitAt != null
  const scopePlant = useMemo(
    () => (onuScope ? plantInScope(onuScope, devices, byId, shownPlaces) : null),
    [onuScope, devices, byId, shownPlaces])
  const hiddenPlant = useMemo(() => {
    const out = new Set<number>()
    if (plantPinned) return out
    if (scopePlant) {
      for (const d of placed)
        if (isPassiveType(d.device_type) && !scopePlant.has(d.id) && d.id !== selectedId)
          out.add(d.id)
      return out
    }
    if (zoom >= detail.passives) return out
    // ONE floor for every passive type — a splitter, a closure and an FDB stand down
    // together. Trouble on a splitter's drops says so in TONE and buys it no extra
    // zoom, or plant appears and disappears by a rule nobody can predict.
    for (const d of placed)
      if (isPassiveType(d.device_type) && d.id !== selectedId) out.add(d.id)
    return out
  }, [placed, zoom, detail.passives, plantPinned, scopePlant, selectedId])
  const drawnDevices = useMemo(
    () => (hiddenPlant.size === 0
      ? placed : placed.filter((d) => !hiddenPlant.has(d.id))),
    [placed, hiddenPlant])

  const clusters = useMemo(() => buildClusters(drawnDevices, zoom),
                           [drawnDevices, zoom])
  const pinPos = useMemo(() => {
    const pos = new Map<number, [number, number]>()
    for (const c of clusters)
      for (const m of c.members)
        pos.set(m.id, c.members.length === 1 ? [m.lat, m.lng] : c.center)
    return pos
  }, [clusters])
  const selectedRef = useMemo(
    () => (selectedOnuMac == null ? null
      : places.find((p) => p.mac === selectedOnuMac) ?? null),
    [places, selectedOnuMac])

  const refVisible = (refOnus && zoom >= detail.subscribers)
    || onuScope != null || placingOnu != null
  const refLinesVisible = refVisible
    && (zoom >= detail.drop_lines || onuScope != null || placingOnu != null)
  const refNamesVisible = refVisible
    && (zoom >= detail.subscriber_names || onuScope != null || placingOnu != null)
  // Hovering a passive reveals the customers recorded on it, layer and zoom floors
  // alike — the same override an OLT focus takes, but pointer-bound.
  const hoverDropMacs = useMemo(() => {
    const out = new Set<string>()
    if (hoverId == null) return out
    for (const p of shownPlaces)
      if (p.drop_passive_id === hoverId) out.add(p.mac)
    return out
  }, [hoverId, shownPlaces])
  const refShown = refVisible || hoverDropMacs.size > 0
  const placedByOlt = useMemo(() => {
    const m = new Map<number, number>()
    for (const p of places)
      if (p.device_id != null) m.set(p.device_id, (m.get(p.device_id) ?? 0) + 1)
    return m
  }, [places])
  const ponsByOlt = useMemo(() => {
    const m = new Map<number, Map<string, PonRow>>()
    for (const p of places) {
      if (p.device_id == null) continue
      let pons = m.get(p.device_id)
      if (!pons) { pons = new Map(); m.set(p.device_id, pons) }
      const pon = ponKey(p)
      const row = pons.get(pon) ?? { pon, total: 0, dark: 0 }
      row.total += 1
      if (isRefDark(p)) row.dark += 1
      pons.set(pon, row)
    }
    return new Map([...m].map(([id, pons]) => [id, [...pons.values()]
      .sort((a, b) => a.pon.localeCompare(b.pon, undefined, { numeric: true }))]))
  }, [places])
  const scopePons = onuScope ? ponsByOlt.get(onuScope.deviceId) ?? [] : []
  useEffect(() => {
    if (selectedOnuMac == null) return
    const drawn = shownPlaces.some((p) => p.mac === selectedOnuMac)
    if (!drawn || (!refVisible && !focusFlying)) setSelectedOnuMac(null)
  }, [refVisible, shownPlaces, selectedOnuMac, focusFlying])

  useEffect(() => {
    if (selectedId != null && selectedOnuMac != null) setSelectedOnuMac(null)
  }, [selectedId, selectedOnuMac])

  const hoverPlace = useMemo(() => {
    if (hoverOnuMac == null || !refVisible || hoverOnuMac === selectedOnuMac) return null
    if (placingId != null || placingOnu != null || routeEdit != null) return null
    return shownPlaces.find((p) => p.mac === hoverOnuMac) ?? null
  }, [hoverOnuMac, refVisible, selectedOnuMac, shownPlaces,
      placingId, placingOnu, routeEdit])

  useEffect(() => {
    if (hoverOnuMac == null) return
    if (!refVisible || !shownPlaces.some((p) => p.mac === hoverOnuMac))
      setHoverOnuMac(null)
  }, [hoverOnuMac, refVisible, shownPlaces])

  const hoverCtx = (p: OnuPlace) => {
    const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
    const olt = p.device_id != null ? byId.get(p.device_id) : undefined
    const traced = anchor?.kind === "splitter" && (p.drop_waypoints?.length ?? 0) > 0
      && anchor.device.lat != null && anchor.device.lng != null
    return {
      anchorName: anchor?.device.name ?? null,
      viaSplitter: anchor?.kind === "splitter",
      dropKm: traced
        ? polyKm([[anchor!.device.lat!, anchor!.device.lng!],
                  ...p.drop_waypoints, [p.lat, p.lng]])
        : null,
      frozen: !!olt && isDownState(olt),
    }
  }

  const hoverDevice = useMemo(() => {
    if (hoverId == null || hoverId === selectedId || hoverOnuMac != null) return null
    if (placingId != null || placingOnu != null || routeEdit != null || editPins) return null
    if (armed != null || addNext || plantMenu != null) return null
    if (plantDraft != null || customerDraft != null) return null
    const solo = clusters.find((c) => c.members.length === 1 && c.members[0].id === hoverId)
    return solo?.members[0] ?? null
  }, [hoverId, selectedId, hoverOnuMac, placingId, placingOnu, routeEdit, editPins,
      armed, addNext, plantMenu, plantDraft, customerDraft, clusters])

  useEffect(() => {
    if (hoverId == null) return
    if (!clusters.some((c) => c.members.length === 1 && c.members[0].id === hoverId))
      setHoverId(null)
  }, [hoverId, clusters])

  const devHoverCtx = (d: OrgDevice) => {
    const parent = d.parent_device_id != null ? byId.get(d.parent_device_id) : undefined
    const passive = isPassiveType(d.device_type)
    const load = passive ? loadByPassive.get(d.id) : undefined
    const olt = load?.olt_id != null ? byId.get(load.olt_id) : undefined
    return {
      parentName: parent?.name ?? null,
      parentDown: !!parent && isDownState(parent),
      load,
      totalSplit: passive ? cumulativeSplit(d, byId) : null,
      frozen: !!olt && isDownState(olt),
      frozenBy: olt?.name ?? null,
    }
  }

  const siteCluster = useMemo(() => {
    if (siteAnchor == null) return null
    const c = clusters.find((x) => x.members.some((m) => m.id === siteAnchor))
    return c && c.members.length > 1 ? c : null
  }, [clusters, siteAnchor])

  const hoverSite = useMemo(() => {
    if (hoverSiteId == null || hoverOnuMac != null) return null
    if (placingId != null || placingOnu != null || routeEdit != null || editPins) return null
    if (armed != null || addNext || plantMenu != null) return null
    if (plantDraft != null || customerDraft != null) return null
    const c = clusters.find((x) => x.members.length > 1
      && x.members.some((m) => m.id === hoverSiteId))
    return c && c !== siteCluster ? c : null
  }, [hoverSiteId, hoverOnuMac, placingId, placingOnu, routeEdit, editPins,
      armed, addNext, plantMenu, plantDraft, customerDraft, clusters, siteCluster])

  useEffect(() => {
    if (hoverSiteId == null) return
    if (!clusters.some((c) => c.members.length > 1
        && c.members.some((m) => m.id === hoverSiteId)))
      setHoverSiteId(null)
  }, [hoverSiteId, clusters])

  const siteHoverCtx = (c: SiteCluster): SiteHoverCtx => {
    const inside = new Set(c.members.map((m) => m.id))
    const seen = new Set<number>()
    const uplinks: Array<{ name: string; down: boolean }> = []
    for (const m of c.members) {
      const pid = m.parent_device_id
      if (pid == null || inside.has(pid) || seen.has(pid)) continue
      seen.add(pid)
      const p = byId.get(pid)
      if (p) uplinks.push({ name: p.name, down: isDownState(p) })
    }
    return { uplinks }
  }

  const tracedCables = useMemo(() => {
    const ids = new Set<number>()
    for (const hop of traceQ.data?.hops ?? []) ids.add(hop.cable_id)
    if (cableOpen != null && traceCore != null) ids.add(cableOpen)
    return ids
  }, [cableOpen, traceCore, traceQ.data])

  const hoverLinkIds = useMemo(() => {
    const ids = new Set<number>()
    if (hoverId != null) ids.add(hoverId)
    if (hoverSite) for (const m of hoverSite.members) ids.add(m.id)
    return ids
  }, [hoverId, hoverSite])

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

  const setDropRoute = useMutation({
    mutationFn: ({ mac, waypoints }: { mac: string; waypoints: Array<[number, number]> }) =>
      inventoryApi.setDropRoute(mac, waypoints, scopeOrg),
    onSuccess: (_r, v) => {
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      setRouteEdit(null)
      toast.success(v.waypoints.length
        ? "Drop cable traced"
        : "Drop straightened — back to an untraced line")
    },
    onError: (e) => toast.error(
      `Couldn't save the drop route${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const setCablePath = useMutation({
    mutationFn: ({ cableId, path }: { cableId: number; path: Array<[number, number]> }) =>
      inventoryApi.setCablePath(cableId, path),
    onSuccess: (_r, v) => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      setRouteEdit(null)
      toast.success(v.path.length
        ? "Cable traced — untraced spans on it now follow the glass"
        : "Cable route cleared — its spans go back to straight lines")
    },
    onError: (e) => toast.error(
      `Couldn't save the cable route${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const setLinkStyle = useMutation({
    mutationFn: ({ childId, parentId, style }: {
      childId: number; parentId: number
      style: Parameters<typeof inventoryApi.setLinkStyle>[2]
    }) => inventoryApi.setLinkStyle(childId, parentId, style),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      queryClient.invalidateQueries({ queryKey: ["cables"] })
    },
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const saveCable = useMutation({
    mutationFn: async (v: {
      id?: number; name: string; cores: number | null
      path?: Array<[number, number]>
      ends?: [FibreEnd | null, FibreEnd | null]
    }) => {
      let a = v.ends?.[0] ?? null
      let b = v.ends?.[1] ?? null
      const path = v.path ?? []
      if (v.id == null) {
        const coupler = async (at: [number, number] | undefined, n: number) => {
          if (!at) throw new Error("a cable needs a point at both ends")
          const name = `${v.name} JC${n}`
          const { id } = await inventoryApi.create({
            org_id: scopeOrg ?? undefined,
            name, ip_address: "", device_type: "closure",
            region: nearestRegion(at[0], at[1], devices), tags: [],
            parent_device_id: null, pon_port: null,
            split_ratio: null, split_inputs: null,
          })
          await inventoryApi.setLocation(id, at[0], at[1])
          return { device_id: id, name }
        }
        if (!a) a = await coupler(path[0], 1)
        if (!b) b = await coupler(path[path.length - 1], 2)
      }
      const { id } = await inventoryApi.saveCable({
        id: v.id, name: v.name, cores: v.cores,
        ...(a && b ? {
          a_device_id: a.device_id ?? null, a_mac: a.mac ?? null,
          b_device_id: b.device_id ?? null, b_mac: b.mac ?? null,
        } : {}),
      }, scopeOrg)
      if (v.id == null && path.length) await inventoryApi.setCablePath(id, path)
      return id
    },
    onSuccess: (id, v) => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      setCableForm(null)
      if (v.id == null) {
        setRouteEdit(null)
        setCableList(false)
        toast.success(`${v.name} laid`, {
          action: { label: "Open", onClick: () => setCableOpen(id) },
        })
      }
    },
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // Moving a cable end is PREVIEWED first: a splice is a fact about a particular
  // closure, and the operator has already lost some to a blanket discard once.
  const [moveAsk, setMoveAsk] = useState<
    { cableId: number; end: "a" | "b"; from: FibrePoint & { name?: string | null }
      to: MoveTarget
      discards: number; carries: number; unported: number
      collapses: number } | null>(null)

  const moveEnd = useMutation({
    mutationFn: (v: { cableId: number; from: FibrePoint; to: number
                      preview?: boolean }) =>
      inventoryApi.moveCableEnd({
        org_id: scopeOrg,
        from_device_id: v.from.device_id ?? null, from_mac: v.from.mac ?? null,
        to_device_id: v.to, cable_ids: [v.cableId], preview: v.preview }),
    onError: (e) => toast.error(
      `Couldn't move that end${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const askMoveEnd = useCallback((cableId: number, end: "a" | "b",
                                  from: FibrePoint & { name?: string | null },
                                  to: MoveTarget) => {
    moveEnd.mutate({ cableId, from, to: to.device_id, preview: true }, {
      onSuccess: (out) => {
        if (!out.ok) { toast.error(out.reason ?? "That move was refused"); return }
        setMoveAsk({ cableId, end, from, to, discards: out.discards ?? 0,
                     carries: out.carries ?? 0, unported: out.unported ?? 0,
                     collapses: out.collapses ?? 0 })
      },
    })
  }, [moveEnd])

  const doMoveEnd = useCallback(() => {
    if (!moveAsk) return
    moveEnd.mutate({ cableId: moveAsk.cableId, from: moveAsk.from,
                     to: moveAsk.to.device_id }, {
      onSuccess: (out) => {
        setMoveAsk(null)
        if (!out.ok) { toast.error(out.reason ?? "That move was refused"); return }
        for (const key of ["cables", "routes", "point-fibre", "fibre-trace",
                           "inventory"]) {
          queryClient.invalidateQueries({ queryKey: [key] })
        }
        toast.success(`End moved to ${moveAsk.to.name}`, {
          description: out.discarded
            ? `${out.discarded} splice${out.discarded === 1 ? "" : "s"} discarded — `
              + "their fibres no longer meet."
            : "The traced route is unchanged.",
        })
      },
    })
  }, [moveAsk, moveEnd, queryClient])

  const splitCable = useMutation({
    mutationFn: (v: { cableId: number; lat: number; lng: number }) =>
      inventoryApi.splitCable(v.cableId, v.lat, v.lng),
    onSuccess: (out) => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      setSplitAt(null)
      toast.success("Closure opened", {
        description: out.spliced
          ? `${out.spliced} cores spliced straight through — clear any you actually cut.`
          : "Record a fibre count on the cable to splice its cores through.",
        action: { label: "Open", onClick: () => setCableOpen(out.cable_id) },
      })
    },
    onError: (e) => toast.error(
      `Couldn't open a closure${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const setFibreJoint = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      a: { cableId: number; coreNo: number }
      b: { cableId: number; coreNo: number } | null
      port?: { kind: string; ref: string | null }
    }) => inventoryApi.setFibreJoint({
      ...v.point,
      a_cable_id: v.a.cableId, a_core_no: v.a.coreNo,
      b_cable_id: v.b?.cableId ?? null, b_core_no: v.b?.coreNo ?? null,
      port_kind: v.port?.kind ?? null, port_ref: v.port?.ref ?? null }),
    onSuccess: (out) => {
      if (!out.ok) { setTrayError(out.reason ?? "That join was refused"); return }
      setTrayError(null)
      queryClient.invalidateQueries({ queryKey: ["point-fibre"] })
      queryClient.invalidateQueries({ queryKey: ["fibre-trace"] })
      queryClient.invalidateQueries({ queryKey: ["cables"] })
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't record that join"),
  })

  const connectPort = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      deviceId: number
      port: { kind: string; ref: string | null }
      far?: FarLanding
    }) => inventoryApi.connectPort({
      ...v.point, to_device_id: v.deviceId, org_id: scopeOrg,
      port_kind: v.port.kind || null, port_ref: v.port.ref,
      // The far end is a PORT, or — at a closure, which has none — a CORE.
      ...(v.far && "cableId" in v.far
          ? { to_cable_id: v.far.cableId, to_core_no: v.far.coreNo }
          : { to_port_kind: v.far?.port.kind || null,
              to_port_ref: v.far?.port.ref ?? null }) }),
    onSuccess: (out) => {
      if (!out.ok) { setTrayError(out.reason ?? "That connection was refused"); return }
      setTrayError(null)
      for (const key of ["point-fibre", "fibre-trace", "cables", "routes", "inventory"]) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
      toast.success(out.far_port
        ? `Connected · ${out.far_port} at the far end`
        : "Connected")
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't record that connection"),
  })

  const dropOnLeg = useMutation({
    mutationFn: (v: { mac: string; passiveId: number; legNo: string | null }) =>
      inventoryApi.setDrops({ macs: [v.mac], passive_id: v.passiveId,
                              leg_no: v.legNo, org_id: scopeOrg }),
    onSuccess: () => {
      setTrayError(null)
      for (const key of ["point-fibre", "drops", "splitter-drops", "inventory"]) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't record that drop"),
  })

  const takeCoreToBox = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      a: { cableId: number; coreNo: number }
      to: { deviceId?: number; mac?: string }
      far?: FarLanding
    }) => inventoryApi.takeCoreToBox({
      ...v.point, a_cable_id: v.a.cableId, a_core_no: v.a.coreNo,
      to_device_id: v.to.deviceId ?? null, to_mac: v.to.mac ?? null,
      // The far end is a PORT, or — at a closure, which has none — a CORE.
      ...(v.far && "cableId" in v.far
          ? { to_cable_id: v.far.cableId, to_core_no: v.far.coreNo }
          : { port_kind: v.far?.port.kind ?? null,
              port_ref: v.far?.port.ref ?? null }) }),
    onSuccess: (out) => {
      if (!out.ok) { setTrayError(out.reason ?? "That tail was refused"); return }
      setTrayError(null)
      for (const key of ["point-fibre", "fibre-trace", "cables", "routes"]) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't run that tail"),
  })

  const spliceThrough = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      a: number; b: number
    }) => inventoryApi.spliceThrough({
      ...v.point, a_cable_id: v.a, b_cable_id: v.b }),
    onSuccess: (out) => {
      queryClient.invalidateQueries({ queryKey: ["point-fibre"] })
      queryClient.invalidateQueries({ queryKey: ["fibre-trace"] })
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      setTrayError(out.reason ?? null)
      if (!out.reason) {
        toast.success(`${out.spliced} core${out.spliced === 1 ? "" : "s"} spliced`,
          out.skipped ? {
            description: `${out.skipped} already joined — left alone.`,
          } : undefined)
      }
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't splice those through"),
  })

  const clearFibreJoint = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      cableId: number; coreNo: number
    }) => inventoryApi.clearFibreJoint({
      ...v.point, cable_id: v.cableId, core_no: v.coreNo }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["point-fibre"] })
      queryClient.invalidateQueries({ queryKey: ["fibre-trace"] })
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      setTrayError(null)
    },
    onError: (e) => setTrayError(
      e instanceof ApiError ? e.message : "Couldn't undo that join"),
  })

  const setCoreLabel = useMutation({
    mutationFn: ({ cableId, coreNo, label }: {
      cableId: number; coreNo: number; label: string
    }) => inventoryApi.setCableCore(cableId, coreNo, label || null),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cables"] }),
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const deleteCable = useMutation({
    mutationFn: (id: number) => inventoryApi.deleteCable(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      setCableOpen(null)
    },
    onError: (e) => toast.error(`Couldn't delete${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const deletePassive = useMutation({
    mutationFn: (id: number) => inventoryApi.remove(id),
    onSuccess: (res, id) => {
      if (!res.ok) {
        toast.error(res.reason || "Couldn't delete that box")
        return
      }
      for (const key of ["inventory", "drops", "cables", "routes", "point-fibre",
                         "fibre-trace", "onu-places"]) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
      setPlantDelete(null)
      if (selectedId === id) selectDevice(null)
      setTrayError(null)
    },
    onError: (e) => toast.error(
      `Couldn't delete${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  const plantDeleteToll = useMemo(() => {
    if (!plantDelete) return null
    const children = devices.filter(
      (d) => d.parent_device_id === plantDelete.id).length
    return {
      children,
      drops: loadByPassive.get(plantDelete.id)?.recorded ?? 0,
      cables: (cablesQ.data?.cables ?? []).filter(
        (c) => c.a.device_id === plantDelete.id
            || c.b.device_id === plantDelete.id).length,
    }
  }, [plantDelete, devices, loadByPassive, cablesQ.data])

  const searchDevice = (d: OrgDevice, openPanel = false) => {
    const map = mapRef.current
    if (isPlaced(d)) {
      setDetailTab(deviceTabs(d)[0])
      setSiteAnchor(d.id) // a pin folded into a cluster must not land hidden
      if (openPanel) {
        map?.flyTo([d.lat, d.lng], Math.max(map.getZoom(), 15))
        selectDevice(d.id)
      } else {
        focusDevice(d, { fly: true })
      }
    } else if (canWrite) {
      selectDevice(null)
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
  const searchOnu = (hit: OnuHit) => {
    if (!hit.place) {
      toast.info(`${hit.who} has no location recorded yet`,
                 { description: `In the roster on ${hit.where}. Record where it stands from Survey.` })
      return
    }
    if (!refOnus) toggleRefOnus()
    flyToOnu(hit.place)
  }

  const initialView = useMemo(() => loadView(scopeOrg), [scopeOrg])

  useEffect(() => {
    const armed = (navLocation.state as {
      placeOnu?: { mac: string; label: string } } | null)?.placeOnu
    if (!armed?.mac) return
    setPlacingOnu({ mac: armed.mac, label: armed.label ?? "" })
    selectDevice(null)
    setPlaceOpen(false)
    navigate(navLocation.pathname, { replace: true, state: null })
  }, [navLocation.state, navLocation.pathname, navigate, selectDevice])

  const onuParam = searchParams.get("onu")
  useEffect(() => {
    if (!onuParam) return
    setFocusOnuMac(onuParam.trim().toUpperCase())
    if (!refOnus) toggleRefOnus()
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("onu")
      return next
    }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onuParam, setSearchParams])

  // `pan: false` is for a caller that is already flying somewhere: the flight is animated, so
  // getBounds() still reports the viewport being LEFT, and the reveal test would pan against
  // the flight.
  const scopeOnus = useCallback((deviceId: number, pons: string[],
                                 opts?: { pan?: boolean }) => {
    setOnuScope({ deviceId, pons })
    setSelectedOnuMac(null)
    const pts = places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
    const shown: Array<[number, number]> = pts.map((p) => [p.lat, p.lng])
    for (const id of plantInScope({ deviceId, pons }, devices, byId, pts)) {
      const box = byId.get(id)
      if (box && isPlaced(box)) shown.push([box.lat, box.lng])
    }
    const map = mapRef.current
    if (!map || shown.length === 0 || opts?.pan === false) return
    const view = map.getBounds()
    if (shown.some(([lat, lng]) => view.contains(L.latLng(lat, lng)))) return
    map.panTo(L.latLngBounds(shown).getCenter())
  }, [places, devices, byId])

  const toggleScopePon = useCallback((deviceId: number, pon: string) => {
    const cur = onuScope?.deviceId === deviceId ? onuScope.pons : []
    scopeOnus(deviceId, cur.includes(pon) ? cur.filter((x) => x !== pon) : [...cur, pon])
  }, [onuScope, scopeOnus])

  // Go to a device without opening its panel: select the pin, then focus the map on its
  // located subscribers and the plant feeding them. A device nobody has surveyed has no PON
  // list and no customers, and scoping to it would hide the org's plant to reveal nothing —
  // so there we just go there and select the pin, which is what the operator asked for.
  const focusDevice = useCallback((d: OrgDevice, opts?: { fly?: boolean }) => {
    if (!isPlaced(d)) return
    if (opts?.fly) {
      const map = mapRef.current
      map?.flyTo([d.lat, d.lng], Math.max(map.getZoom(), 15))
    }
    setSelectedId(d.id)
    setPanelFor(null)
    setSelectedOnuMac(null)
    if ((placedByOlt.get(d.id) ?? 0) > 0) scopeOnus(d.id, [], { pan: !opts?.fly })
    else setOnuScope(null)
  }, [placedByOlt, scopeOnus])

  const flyToOnu = useCallback((p: OnuPlace) => {
    setOnuScope(null)
    const map = mapRef.current
    map?.flyTo([p.lat, p.lng], Math.max(map.getZoom(), detail.drop_lines + 1))
    setFocusFlying(true)
    selectDevice(null)
    setSelectedOnuMac(p.mac)
  }, [detail.drop_lines, selectDevice])

  useEffect(() => {
    if (!focusOnuMac || places.length === 0) return
    const hit = places.find((p) => p.mac === focusOnuMac)
    setFocusOnuMac(null)
    if (!hit) {
      toast.info("That subscriber doesn't have a location yet")
      return
    }
    flyToOnu(hit)
  }, [focusOnuMac, places, flyToOnu])

  useEffect(() => {
    if (cableForm != null) return
    if (placingId == null && routeEdit == null && placingOnu == null
      && armed == null && !addNext && splitAt == null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPlacingId(null); setRouteEdit(null); setPlacingOnu(null)
        setArmed(null); setAddNext(false); setSplitAt(null); return
      }
      if (routeEdit != null && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        const el = document.activeElement
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return
        e.preventDefault()
        setRouteEdit((re) => re && re.points.length ? { ...re, points: re.points.slice(0, -1) } : re)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [placingId, placingOnu, routeEdit, armed, addNext, cableForm, splitAt])

  const openPlantMenu = useCallback((
    lat: number, lng: number, x: number, y: number, device: OrgDevice | null,
  ) => {
    if (!canWrite) return
    if (placingId != null || placingOnu != null || routeEdit != null || editPins) return
    setPlantMenu({ lat, lng, x, y, device })
  }, [canWrite, placingId, placingOnu, routeEdit, editPins])

  const onMapContext = useCallback((ll: L.LatLng, point: L.Point) => {
    openPlantMenu(ll.lat, ll.lng, point.x, point.y, null)
  }, [openPlantMenu])

  const fibreEnds = useMemo(() => {
    const out: Array<{ pos: [number, number]; end: FibreEnd; bias: number }> = []
    for (const d of placed) {
      out.push({ pos: [d.lat, d.lng], end: { device_id: d.id, name: d.name }, bias: 0 })
    }
    for (const pl of places) {
      if (pl.lat == null || pl.lng == null) continue
      out.push({ pos: [pl.lat, pl.lng], end: { mac: pl.mac, name: onuName(pl) }, bias: 1 })
    }
    return out
  }, [placed, places])

  const snapToPoint = useCallback((ll: L.LatLng) => {
    const map = mapRef.current
    if (!map) return null
    const at = map.latLngToContainerPoint(ll)
    let best: { end: FibreEnd; pos: [number, number]; d: number } | null = null
    for (const { pos, end, bias } of fibreEnds) {
      const p = map.latLngToContainerPoint(pos)
      const d = Math.min(
        Math.hypot(p.x - at.x, p.y - at.y),
        Math.hypot(p.x - at.x, p.y - MARK_DY_PX - at.y),
      ) + bias
      if (d > SNAP_PX && map.distance(ll, pos) > SNAP_M) continue
      if (!best || d < best.d) best = { end, pos, d }
    }
    return best as { end: FibreEnd; pos: [number, number] } | null
  }, [fibreEnds])

  const nearMiss = useCallback((pos: [number, number] | undefined) => {
    const map = mapRef.current
    if (!map || !pos) return null
    let best: { end: FibreEnd; m: number } | null = null
    for (const { pos: p, end } of fibreEnds) {
      const m = map.distance(pos, p)
      if (m <= NEAR_MISS_M && (!best || m < best.m)) best = { end, m }
    }
    return best
  }, [fibreEnds])

  const onMapClick = useCallback((ll: L.LatLng) => {
    if (plantMenu != null) { setPlantMenu(null); return }
    if (routeEdit != null) {
      const snap = routeEdit.kind === "cable" ? snapToPoint(ll) : null
      setRouteEdit((re) => {
        if (!re) return re
        const at: [number, number] = snap ? snap.pos : [ll.lat, ll.lng]
        const points = [...re.points, at]
        if (re.kind !== "cable") return { ...re, points }
        return {
          ...re, points,
          endA: re.points.length === 0 ? snap?.end ?? null : re.endA,
          endB: re.points.length === 0 ? re.endB : snap?.end ?? null,
        }
      })
    } else if (splitAt != null) {
      splitCable.mutate({ cableId: splitAt.cableId, lat: ll.lat, lng: ll.lng })
    } else if (armed != null) {
      if (armed.kind === "customer") {
        setCustomerDraft({ lat: ll.lat, lng: ll.lng, passiveId: armed.passiveId })
      } else {
        setPlantDraft({ kind: armed.kind, lat: ll.lat, lng: ll.lng })
      }
      setArmed(null)
    } else if (addNext) {
      setAddNext(false)
      const pt = mapRef.current?.latLngToContainerPoint(ll)
      openPlantMenu(ll.lat, ll.lng, pt?.x ?? 0, pt?.y ?? 0, null)
    } else if (placingOnu != null) {
      setOnuPlace.mutate({ mac: placingOnu.mac, lat: ll.lat, lng: ll.lng,
                           label: placingOnu.label || null })
      if (!refOnus) toggleRefOnus()
      setSelectedOnuMac(placingOnu.mac)
      setPlacingOnu(null)
    } else if (placingId != null) {
      setLocation.mutate({ id: placingId, lat: ll.lat, lng: ll.lng })
      selectDevice(placingId)
      setPlacingId(null)
    } else {
      selectDevice(null)
      setSelectedOnuMac(null)
      setSiteAnchor(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placingId, placingOnu, refOnus, routeEdit, setLocation, snapToPoint,
      armed, addNext, plantMenu, openPlantMenu, splitAt, splitCable])

  const onPlantCreated = useCallback((
    created: { id: number; name: string }, again: boolean,
  ) => {
    const kind = plantDraft?.kind
    setPlantDraft(null)
    if (again && kind) {
      setArmed({ kind, passiveId: null })
      toast.success(`${created.name} recorded`, {
        description: "Click where the next box goes.",
      })
    } else {
      toast.success(`${created.name} recorded`, {
        description: "Pull a core in to say what feeds it.",
      })
      selectDevice(created.id)
    }
  }, [plantDraft, selectDevice])

  const onCustomerAttached = useCallback((mac: string) => {
    setCustomerDraft(null)
    if (!refOnus) toggleRefOnus()
    selectDevice(null)
    setSelectedOnuMac(mac)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refOnus])

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
            ? "Location blocked. Allow location for this site in the browser's address bar, then retry"
            : "Location needs HTTPS. Open the dashboard over https to use it")
        } else if (err.code === err.TIMEOUT) {
          toast.error("Timed out getting your location. Try again")
        } else {
          toast.error("Your device couldn't determine a location")
        }
      },
      { enableHighAccuracy: true, timeout: 10_000 },
    )
  }

  const cycleTrouble = () => {
    if (troubles.length === 0) return
    const d = troubles[troubleIdx.current % troubles.length]
    troubleIdx.current += 1
    mapRef.current?.flyTo([d.lat, d.lng], Math.max(mapRef.current.getZoom(), 14))
    setDetailTab(deviceTabs(d)[0])
    selectDevice(d.id)
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

  const onZoom = useCallback((z: number) => {
    setZoom(z); setFocusFlying(false)
  }, [])
  const lowZoom = zoom < detail.labels
  // A WRAPPER CLASS, never a per-pin decision — icons are cached by their html
  // string, so folding this into `pinIcon` would swap every plant pin's DOM node
  // on the threshold crossing and replay its mount. Two classes because the two
  // floors are independent; the CSS pairs them.
  const lowPlant = zoom < detail.passive_names
  const lineK = lineScale(zoom)

  const onClusterClick = (c: SiteCluster) => {
    if (routeEdit != null) return
    if (placingId != null) {
      const t = c.members.reduce((best, m) =>
        distanceKm(m.lat, m.lng, c.center[0], c.center[1])
          < distanceKm(best.lat, best.lng, c.center[0], c.center[1]) ? m : best)
      setLocation.mutate({ id: placingId, lat: t.lat, lng: t.lng })
      toast.success(`Placed at ${t.name} (same site)`)
      selectDevice(placingId)
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

  useEffect(() => { setCoordsEdit(false); setCoordsText("") }, [selectedId])
  const saveCoords = () => {
    if (!selected) return
    const m = coordsText.trim().match(/^(-?\d+(?:\.\d+)?)[,;\s]+(-?\d+(?:\.\d+)?)$/)
    if (!m) { toast.error('Use "lat, lng", e.g. 17.4401, 78.3489'); return }
    setLocation.mutate({ id: selected.id, lat: Number(m[1]), lng: Number(m[2]) })
    setCoordsEdit(false)
  }

  const unpinSelected = () => {
    if (!selected) return
    const name = selected.name
    setLocation.mutate({ id: selected.id, lat: null, lng: null }, {
      onSuccess: () => toast.success(`${name} taken off the map. The device is unchanged.`),
    })
    selectDevice(null)
  }

  const links = useMemo(() => {
    const out: Array<{
      key: string; from: Placed; to: Placed; tone: string
      kind: "primary" | "backup" | "peer" | "run"
      childId: number; parentId: number
      route?: { pts: Array<[number, number]> }
      labelPos?: number | null
      cableId: number | null
      cableName: string | null
      cores: number | null
      coreNo: number | null
      cabled: boolean
      plantCableId: number | null
      plantRun: number[]
      binding?: LinkBinding
    }> = []
    const placedById = new Map(drawnDevices.map((d) => [d.id, d]))
    const cableByPair = new Map<string, Cable>()
    for (const c of cablesQ.data?.cables ?? []) {
      if (c.a.device_id == null || c.b.device_id == null) continue
      for (const k of [`${c.a.device_id}:${c.b.device_id}`,
                       `${c.b.device_id}:${c.a.device_id}`]) {
        const cur = cableByPair.get(k)
        if (!cur || (cur.cores == null && c.cores != null)) cableByPair.set(k, c)
      }
    }
    // WHICH SHEATH CARRIES THIS PAIR, if the fibre record joins them at all — direct,
    // or on through a closure. Server-derived (`fiber.connected_spans`), the same walk
    // the `undrawn` draft reads, so the map and the draft cannot disagree about what
    // counts as recorded. `null` means joined but by nothing worth labelling.
    const cabled = new Map<string, { label: number | null; path: number[] }>()
    for (const p of cablesQ.data?.cabled_pairs ?? [])
      cabled.set(linkKey(p.a, p.b), { label: p.cable_id, path: p.cable_ids ?? [] })
    const styled = (childId: number, parentId: number) => {
      const s = styleByKey.get(`${childId}:${parentId}`)
      const c = cableByPair.get(`${childId}:${parentId}`)
      const key = linkKey(childId, parentId)
      return { childId, parentId, labelPos: s?.label_pos,
               cableId: c?.id ?? null, cableName: c?.name ?? null,
               cores: c?.cores ?? null, coreNo: null,
               // RECORDED GLASS WINS: this chord stands down for good, `plantCableId`
               // says which sheath inherits its rate chip, and `plantRun` is every
               // sheath that must be lit in its place.
               cabled: cabled.has(key), plantCableId: cabled.get(key)?.label ?? null,
               plantRun: cabled.get(key)?.path ?? [] }
    }
    for (const d of drawnDevices) {
      const tone = pinTone(d)
      if (d.parent_device_id != null) {
        const p = placedById.get(d.parent_device_id)
        if (p) out.push({ key: `p${d.id}`, from: p, to: d, tone, kind: "primary",
          ...styled(d.id, p.id),
          route: routeByKey.get(`${d.id}:${p.id}`),
          binding: linkBindings.get(linkKey(d.id, p.id)) })
      }
      for (const bp of d.backup_parents) {
        const p = placedById.get(bp)
        if (p) out.push({ key: `b${d.id}-${bp}`, from: p, to: d, tone, kind: "backup",
          ...styled(d.id, bp),
          route: routeByKey.get(`${d.id}:${bp}`),
          binding: linkBindings.get(linkKey(d.id, bp)) })
      }
      for (const pid of d.peer_ids ?? []) {
        if (pid < d.id) continue
        const p = placedById.get(pid)
        if (p) out.push({ key: `x${d.id}-${pid}`, from: d, to: p, kind: "peer",
          tone: toneRank(p) < toneRank(d) ? pinTone(p) : tone,
          ...styled(pid, d.id),
          route: routeByKey.get(`${pid}:${d.id}`) ?? routeByKey.get(`${d.id}:${pid}`),
          binding: linkBindings.get(linkKey(d.id, pid)) })
      }
    }
    return out
  }, [drawnDevices, routeByKey, styleByKey, linkBindings, cablesQ.data])

  const drawnLinks = useMemo(() => links.map((l) => {
    const from = pinPos.get(l.from.id) ?? [l.from.lat, l.from.lng] as [number, number]
    const to = pinPos.get(l.to.id) ?? [l.to.lat, l.to.lng] as [number, number]
    const wp = l.route?.pts
    const drawn = !!wp?.length && !foldedTogether(from, to)
    const pts: Array<[number, number]> = drawn ? [from, ...wp!, to] : [from, to]
    return { ...l, from3: from, to3: to, pts, drawn }
  }), [links, pinPos])

  // THE RATE A SHEATH INHERITED from the chord that stood down for it. Keyed on the
  // cable `connected_spans` picked — the biggest on the run — so an 8-PON OLT fed off
  // one trunk cannot stack eight readings on one line: the last one in wins and the
  // rest keep no chip, which is the honest outcome of asking one label to answer for
  // several links. Rank picks the loudest so the one that survives is worth reading.
  const cableRate = useMemo(() => {
    const m = new Map<number, ReturnType<typeof linkRateBody> & { rank: number }>()
    if (!bwLabels) return m
    for (const l of links) {
      if (!l.cabled || l.plantCableId == null || !l.binding) continue
      const body = linkRateBody(l.binding, l.from, l.to)
      // ONLY A REAL READING MOVES. With no fresh counter the chord's fallback is the
      // PORT NAME, and `main 6F · GE0/1` on a sheath reads as though the cable were
      // called GE0/1 — the label belongs to a line between two boxes, not to the glass
      // in the ground. The plain cable chip is the honest mark then; where the reading
      // went is the device panel's Ports tab, which is where a date exists to explain
      // it. Same rule the map keeps for a dBm it cannot stand behind: print nothing.
      if (!body.hasRates) continue
      const rank = bwRank(l.binding, l.from.id, l.to.id, l.cores)
      const cur = m.get(l.plantCableId)
      if (cur && cur.rank >= rank) continue
      m.set(l.plantCableId, { ...body, rank })
    }
    return m
  }, [links, bwLabels])

  // A SHEATH THAT REPLACED A CHORD INHERITS THE CHORD'S VISIBILITY, NOT PLANT'S — so
  // it draws at EVERY zoom (operator, 2026-08-12: "I don't want dotted line at any
  // zoom level"). The passives floor exists to stop dense plant smearing at low zoom;
  // these lines cost nothing extra, because each one stands in for a dependency line
  // that was already drawn there. It is the WHOLE RUN, not just the labelled sheath —
  // lighting `main` alone would leave the 1F tails dark and the line would stop at a
  // closure. Same shape as the floor's existing dark-splitter exemption.
  const standsDown = useCallback(
    (l: { cabled: boolean; plantCableId: number | null }) =>
      l.cabled && l.plantCableId != null, [])
  const exemptCables = useMemo(() => {
    const s = new Set<number>()
    for (const l of links) if (standsDown(l)) for (const id of l.plantRun) s.add(id)
    return s
  }, [links, standsDown])

  const pinOfPoint = useCallback((pt: FibrePoint): [number, number] | null => {
    if (pt.device_id != null) {
      const d = byId.get(pt.device_id)
      return d && isPlaced(d) ? [d.lat, d.lng] : null
    }
    const place = pt.mac ? places.find((x) => x.mac === pt.mac) : null
    return place && place.lat != null && place.lng != null
      ? [place.lat, place.lng] : null
  }, [byId, places])

  const cableLines = useMemo(() => {
    const below = zoom < detail.passives
    return (cablesQ.data?.cables ?? []).flatMap((cable) => {
      // Below the plant floor, only the runs standing in for a dependency chord draw.
      if (below && !exemptCables.has(cable.id)) return []
      if (routeEdit?.kind === "cable" && routeEdit.cableId === cable.id) return []
      const pts = cablePolyline(cable, pinOfPoint)
      return pts.length < 2 ? [] : [{ cable, pts }]
    })
  }, [cablesQ.data, pinOfPoint, zoom, detail.passives, routeEdit, exemptCables])

  const cableUnder = useCallback((ll: L.LatLng) => {
    const map = mapRef.current
    if (!map || !cableLines.length) return null
    const at = map.latLngToContainerPoint(ll)
    let best: { cableId: number; name: string; meters: number; d: number } | null = null
    for (const { cable, pts } of cableLines) {
      const px = pts.map((p) =>
        [map.latLngToContainerPoint(p).x, map.latLngToContainerPoint(p).y] as [number, number])
      if (px.length < 2) continue
      const hit = nearestOnPath(px, at.x, at.y)
      if (hit.dist > CUT_SLACK_PX || (best && hit.dist >= best.d)) continue
      const on = pointAt(pts, hit.seg, hit.t)
      best = { cableId: cable.id, name: cable.name,
               meters: map.distance(ll, on), d: hit.dist }
    }
    return best
  }, [cableLines])

  const menuCut = useMemo(
    () => (plantMenu && !plantMenu.device
      ? cableUnder(L.latLng(plantMenu.lat, plantMenu.lng)) : null),
    [plantMenu, cableUnder])

  const chipShown = useMemo(() => {
    const links = new Set<string>()
    const cables = new Set<number>()
    const refs = new Set<string>()
    const names = new Set<string>()
    const taken: Array<[number, number, number]> = []
    // MEASURED IN A BROWSER, not estimated — a claim carries its own half-width, and
    // two wide chips reserved at a narrow one pass the collision test and visibly
    // overlap. `cableRate` is the cable chip once it inherits a stood-down chord's
    // reading: a bare `main 6F` is 65px and the same chip with `↓70M ↑5.3M` is 175,
    // so a 14ch name at the clamp plus a four-figure rate lands near 244. Re-measure
    // on ANY content change to either chip.
    const CHIP_HALF = { link: 48, cable: 68, cableRate: 122, cableBare: 56,
                        ref: 28, name: 44 }
    const fits = (x: number, y: number, half: number) =>
      !taken.some(([tx, ty, th]) =>
        Math.abs(tx - x) < th + half && Math.abs(ty - y) < 24)
    const claim = (x: number, y: number, half: number) => {
      taken.push([x, y, half])
    }

    // ONE PASS FOR EVERY RATE, wherever it is drawn. A reading that moved from a
    // stood-down chord onto its sheath is the SAME claim relocated, so it keeps the
    // rank it had (`bwRank`) instead of inheriting the cable pass's fibre-count order.
    // Left in the later pass it competed at 244px against every link chip claimed
    // first and lost outright — the busiest link on the map going quiet the moment its
    // plant was recorded, which is the opposite of what recording plant should do.
    //
    // THE ZOOM FLOOR IS TAKEN HERE, NOT AT THE RENDER (Map detail → Cable & rate
    // labels). The budget must read the same predicate the render does, or a chip
    // nobody draws goes on reserving pixels away from a subscriber name that would
    // have — the rule the drop-line chips already keep. Every family below asks
    // `chipFloor || <worth shouting about>`: a port that is DOWN or over its bandwidth
    // is an ALARM on this map and no density setting may hide one, and a selected path
    // or a traced core is the operator pointing at that line. The same "down or
    // selected keeps its name at every zoom" exemption the device-name floor makes.
    const chipFloor = zoom >= detail.line_labels
    const cands: Array<{ key: string; cable?: number; x: number; y: number
                         rank: number; half: number }> = []
    for (const l of bwLabels ? drawnLinks : []) {
      if (!l.binding && !l.cores) continue
      const emphasized = selectedId != null
        && (l.to.id === selectedId || downstream.has(l.to.id))
      if (troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized)
        continue
      // A chord that stands down reserves no pixels — the budget must read the same
      // predicate as the render, or an absent line goes on crowding out a chip that
      // really is drawn. Its rate is claimed by the sheath below instead.
      if (standsDown(l) || !showUncabled) continue
      if (!chipFloor && !emphasized && !(l.binding && linkTone(l.binding))) continue
      const [ax, ay] = project(l.from3[0], l.from3[1], zoom)
      const [bx, by] = project(l.to3[0], l.to3[1], zoom)
      if (Math.hypot(bx - ax, by - ay) < 90) continue
      const [plat, plng] = linkLabelPos(l.pts, l.labelPos)
      const [x, y] = project(plat, plng, zoom)
      cands.push({ key: l.key, x, y, half: CHIP_HALF.link,
        rank: bwRank(l.binding, l.from.id, l.to.id, l.cores) })
    }
    for (const { cable, pts } of bwLabels ? cableLines : []) {
      const rate = cableRate.get(cable.id)
      if (!rate) continue
      // The reading relocated onto this sheath keeps the tone it had on the chord,
      // so an alarming port stays readable below the floor wherever its chip ended up.
      if (!chipFloor && !rate.tone && !tracedCables.has(cable.id)) continue
      const [plat, plng] = cableLabelPos(pts)
      const [x, y] = project(plat, plng, zoom)
      // A plumbing sheath draws the rate ALONE (no name, no `1F`), so it claims close
      // to a link chip's pixels rather than a cable chip's — measured at 103px against
      // a link chip's 88, the difference being this chip's own chrome.
      cands.push({ key: `c${cable.id}`, cable: cable.id, x, y, rank: rate.rank,
                   half: isPlumbing(cable) ? CHIP_HALF.cableBare : CHIP_HALF.cableRate })
    }
    cands.sort((a, b) => b.rank - a.rank)
    for (const c of cands) {
      if (!fits(c.x, c.y, c.half)) continue
      claim(c.x, c.y, c.half)
      if (c.cable != null) cables.add(c.cable)
      else links.add(c.key)
    }

    const cableCands: Array<{ id: number; x: number; y: number; cores: number
                              half: number }> = []
    for (const c of cableLines) {
      // A rate-carrying sheath was already offered pixels above, at its own rank —
      // and if it lost there it may not try again at a narrower claim, or the budget
      // would reserve one box and draw a wider one.
      if (cableRate.has(c.cable.id)) continue
      // Plumbing is never LISTED as a cable and never labelled for its own sake. It
      // reaches the map only by inheriting a rate, which the line above has handled.
      if (isPlumbing(c.cable)) continue
      // A plain name chip carries no alarm — a cable has no state — so the only
      // thing that outranks the floor here is a core somebody is tracing.
      if (!chipFloor && !tracedCables.has(c.cable.id)) continue
      const [plat, plng] = cableLabelPos(c.pts)
      const [x, y] = project(plat, plng, zoom)
      const [ax, ay] = project(c.pts[0][0], c.pts[0][1], zoom)
      const [bx, by] = project(c.pts[c.pts.length - 1][0],
                               c.pts[c.pts.length - 1][1], zoom)
      if (Math.hypot(bx - ax, by - ay) < 90) continue
      cableCands.push({ id: c.cable.id, x, y, cores: c.cable.cores ?? 0,
                        half: CHIP_HALF.cable })
    }
    cableCands.sort((a, b) => b.cores - a.cores)
    for (const c of cableCands) {
      if (!fits(c.x, c.y, c.half)) continue
      claim(c.x, c.y, c.half)
      cables.add(c.id)
    }

    const refCands: Array<{ mac: string; x: number; y: number; dark: boolean }> = []
    for (const p of refLinesVisible ? shownPlaces : []) {
      if (!refHasChip(p)) continue
      const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
      if (!anchor) continue
      const to = anchor.device as Placed
      const a = project(to.lat, to.lng, zoom)
      const b = project(p.lat, p.lng, zoom)
      if (Math.hypot(a[0] - b[0], a[1] - b[1]) < 56) continue
      const [clat, clng] = refChipPos(to, p)
      const [cx, cy] = project(clat, clng, zoom)
      refCands.push({ mac: p.mac, dark: isRefEvidence(p), x: cx, y: cy })
    }
    refCands.sort((x, y) => Number(y.dark) - Number(x.dark))
    for (const c of refCands) {
      if (!fits(c.x, c.y, CHIP_HALF.ref)) continue
      claim(c.x, c.y, CHIP_HALF.ref)
      refs.add(c.mac)
    }

    const nameCands: Array<{ mac: string; x: number; y: number; dark: boolean }> = []
    for (const p of refVisible ? shownPlaces : []) {
      const loud = isRefEvidence(p)
      if (!refNamesVisible && !loud) continue
      if (troubleOnly && !isRefDark(p)) continue
      const [x, y] = project(p.lat, p.lng, zoom)
      nameCands.push({ mac: p.mac, dark: loud, x, y: y + REF_NAME_DY })
    }
    nameCands.sort((x, y) => Number(y.dark) - Number(x.dark))
    for (const c of nameCands) {
      if (!fits(c.x, c.y, CHIP_HALF.name)) continue
      claim(c.x, c.y, CHIP_HALF.name)
      names.add(c.mac)
    }
    return { links, cables, refs, names }
  }, [drawnLinks, bwLabels, showUncabled, zoom, troubleOnly, selectedId, downstream,
      refLinesVisible, refNamesVisible, refVisible, shownPlaces, byId, cableLines,
      cableRate, standsDown, detail.line_labels, tracedCables])

  const hoverEnabled = placingId == null && routeEdit == null && !editPins
    && splitAt == null
    && selectedId == null && siteCluster == null && selectedOnuMac == null
    && hoverPlace == null && hoverDevice == null && hoverSite == null
  // THE PROBE READS WHAT THE MAP DRAWS — the rule the chip budget already keeps, and
  // the same failure when it doesn't. A chord that stood down for its fibre is not on
  // screen, so measuring it put the readout (and its `straight-line` note) over empty
  // ground beside the sheath that replaced it; so did every chord hidden by switching
  // Dependency links off. Reported 2026-08-13.
  //
  // THE QUESTION DID NOT GO AWAY WITH THE LINE, so the sheath inherits the measure
  // exactly as it inherited the rate chip: the operator laid that cable to BE the
  // connection, and a TRACED one finally answers in drum metres instead of a chord.
  // The readout names the cable's OWN ends — the closures a crew drives to and orders
  // drum between — never the run's, which is a sum this line cannot show. It names no
  // cable, so plumbing is measurable here without being labelled.
  const hoverLines = useMemo<HoverLink[]>(() => {
    if (!hoverEnabled) return []
    const out: HoverLink[] = []
    for (const l of drawnLinks) {
      if (standsDown(l) || !showUncabled) continue
      out.push({ key: l.key, pts: l.pts, from: l.from, to: l.to, straight: !l.drawn })
    }
    for (const { cable, pts } of cableLines)
      out.push({ key: `cable:${cable.id}`, pts,
                 from: { name: cable.a.name ?? "?" }, to: { name: cable.b.name ?? "?" },
                 straight: !cableTraced(cable) })
    return out
  }, [drawnLinks, cableLines, standsDown, showUncabled, hoverEnabled])
  const hoverable = useMemo(
    () => projectLinks(hoverLines, zoom), [hoverLines, zoom])
  const hoverKeepOut = useMemo(() => {
    if (!hoverEnabled) return []
    return clusters.map((c) => {
      const [lat, lng] = c.members.length === 1
        ? [c.members[0].lat, c.members[0].lng] : c.center
      return project(lat, lng, zoom)
    })
  }, [clusters, zoom, hoverEnabled])
  useEffect(() => { if (!hoverEnabled) setHover(null) }, [hoverEnabled])

  const deviceCables = useMemo(() => {
    if (!selected) return []
    return (cablesQ.data?.cables ?? [])
      .filter((c) => c.a.device_id === selected.id || c.b.device_id === selected.id)
      .map((c) => ({
        cable: c,
        far: c.a.device_id === selected.id ? c.b : c.a,
      }))
  }, [selected, cablesQ.data])

  const refCables = useMemo(() => {
    if (!selectedOnuMac) return []
    return (cablesQ.data?.cables ?? [])
      .filter((c) => c.a.mac === selectedOnuMac || c.b.mac === selectedOnuMac)
      .map((c) => ({
        cable: c,
        far: c.a.mac === selectedOnuMac ? c.b : c.a,
      }))
  }, [selectedOnuMac, cablesQ.data])

  const fibreTodo = useMemo(() => {
    if (!selected) return 0
    const joined = new Set<number>()
    for (const { far } of deviceCables) {
      if (far.device_id != null) joined.add(far.device_id)
    }
    const declared = new Set<number>()
    for (const d of devices) {
      if (d.parent_device_id === selected.id) declared.add(d.id)
    }
    if (selected.parent_device_id) declared.add(selected.parent_device_id)
    return [...declared].filter((id) => !joined.has(id)).length
  }, [selected, devices, deviceCables])

  // EVERY hook has to run before the early return below. A render that bails out
  // here calls fewer hooks than the one after scopeOrg arrives, and React counts
  // hooks by position — that mismatch is error #310, which takes the whole map
  // down with "Something went wrong". It broke intermittently and only on the
  // largest org, because it needs scopeOrg to flip between two renders.
  const railOpen = (!!selected || !!selectedRef || cableOpen != null || cableList)
    && !routeEdit
  const panel = useResizablePanel({
    storageKey: "wisp:map:panelw", defaultWidth: 380, min: 320, max: 620,
    open: railOpen,
  })

  if (!scopeOrg) return <NeedsOrg />

  const down = troubles.filter((d) => pinTone(d) === "destructive").length
  const degraded = troubles.length - down

  const lineColor = (tone: string) =>
    tone === "destructive" ? "var(--destructive)"
      : tone === "warning" ? "var(--warning)" : "var(--map-link)"

  const parent = selected?.parent_device_id != null ? byId.get(selected.parent_device_id) : null
  const linkKm = selected && isPlaced(selected) && parent && isPlaced(parent)
    ? distanceKm(selected.lat, selected.lng, parent.lat, parent.lng) : null
  const selRoute = selected && parent
    ? routeByKey.get(`${selected.id}:${parent.id}`)?.pts : undefined
  const routeKm = selRoute && selected && isPlaced(selected) && parent && isPlaced(parent)
    ? polyKm([[parent.lat, parent.lng], ...selRoute, [selected.lat, selected.lng]]) : null

  const startDropRouteEdit = (p: OnuPlace) => {
    setPlacingId(null)
    setPlaceOpen(false)
    setPlacingOnu(null)
    setRouteEdit({ kind: "drop", mac: p.mac, points: p.drop_waypoints ?? [] })
  }
  const editingDrop = routeEdit?.kind === "drop"
    ? (places.find((p) => p.mac === routeEdit.mac) ?? null) : null
  const editingDropAnchor = editingDrop
    ? dropAnchor(editingDrop.drop_passive_id, editingDrop.device_id, byId) : null

  return (
    <div ref={wrapRef} style={panel.vars} className={cn(
      "wisp-map-wrap relative h-[var(--wisp-pane-h,calc(100svh-3.5rem-4rem))] md:h-[var(--wisp-pane-h,calc(100svh-3.5rem))]",
      (placingId != null || placingOnu != null) && "wisp-map-placing",
      lowZoom && "wisp-map-lowzoom",
      lowPlant && "wisp-map-lowplant",
    )}>
      <MapContainer
        ref={mapRef}
        center={initialView ? [initialView.lat, initialView.lng] : [22.5, 79]}
        zoom={initialView?.zoom ?? 4}
        zoomControl={false}
        attributionControl={true}
        className="wisp-map h-full w-full"
        zoomSnap={0.25}
        wheelPxPerZoomLevel={120}
        worldCopyJump
      >
        <AttributionPrefix />
        <InvalidateOnResize />
        {googleActive ? (
          <GoogleLayer
            key={`google-${GOOGLE_BASEMAPS[basemap]}-${dark ? "night" : "day"}`
              + `-${googleLabels ? "lbl" : "nolbl"}`}
            apiKey={googleKey!}
            mapType={GOOGLE_BASEMAPS[basemap]}
            dark={dark}
            labels={googleLabels}
            onFail={onGoogleFail}
          />
        ) : (
          <StreetsTiles dark={dark} />
        )}
        <ZoomControl position="bottomright" />
        <MapEvents org={scopeOrg} onMapClick={onMapClick} onZoom={onZoom}
          onMapContext={onMapContext}
          onMoved={() => setPlantMenu(null)} />
        <ViewController placed={placed} ready={!isLoading && orgsQ.isSuccess}
          hasSavedView={!!initialView} bounds={region.bounds} />
        <LinkHoverProbe projected={hoverable} enabled={hoverEnabled}
          zoom={zoom} keepOut={hoverKeepOut} onHover={setHover} />
        {hover && (
          <Marker position={hover.at} icon={hoverIcon(hover)}
            interactive={false} zIndexOffset={1100} />
        )}
        {cableLines.map(({ cable: c, pts }) => {
          const traced = cableTraced(c)
          const lit = tracedCables.has(c.id)
          const w = 2 + fiberBoost(c.cores) + (lit ? 1.5 : 0)
          return (
            <Fragment key={`cable-${c.id}`}>
              <Polyline interactive={false} positions={pts}
                pathOptions={{ color: "#000", opacity: CASING_OPACITY,
                  ...casingAt(lineK, w, CASING_OVER_FINE),
                  ...(traced ? {} : { dashArray: CABLE_DASH }) }} />
              <Polyline interactive={false} positions={pts}
                pathOptions={{ color: "var(--map-plant)", opacity: lit ? 1 : 0.9,
                  ...strokeAt(lineK, w),
                  ...(traced ? {} : { dashArray: CABLE_DASH }) }} />
              {chipShown.cables.has(c.id) && (
                <Marker position={cableLabelPos(pts)}
                  icon={cableIcon(c, cableRate.get(c.id))}
                  zIndexOffset={550}
                  eventHandlers={{ click: () => { setCableList(false)
                                                  setCableOpen(c.id) } }} />
              )}
            </Fragment>
          )
        })}
                {drawnLinks.map((l) => {
          // RECORDED GLASS WINS, and it wins automatically. A dependency chord is a
          // claim about what depends on what, drawn straight because nobody surveyed
          // it; once the fibre between the pair IS written down, the sheath says the
          // same thing along the route a van drives. Two lines for one connection, one
          // of them a straight line through ground nothing runs under, is the thing
          // this map is most careful about. What stays dashed is the to-do list, and
          // that is what "Dependency links" governs now.
          if (standsDown(l)) return null
          if (!showUncabled) return null
          const emphasized = selectedId != null
            && (l.to.id === selectedId || downstream.has(l.to.id))
          const dimmed = troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized
          const { pts } = l
          const labeled = chipShown.links.has(l.key)
          const chipIcon = labeled
            ? linkBwIcon(l.binding, l.from, l.to,
                         { cores: l.cores, coreNo: l.coreNo, name: l.cableName })
            : null
          const hovered = !dimmed
            && (hoverLinkIds.has(l.to.id) || hoverLinkIds.has(l.from.id))
          const distribution = (l.kind === "primary" || l.kind === "run")
            && isPassiveType(l.from.device_type) && isPassiveType(l.to.device_type)
          const traced = l.cableId != null && tracedCables.has(l.cableId)
          const weight = (l.kind === "peer" ? 2
            : emphasized || traced ? 3.5
            : l.tone === "destructive" ? 3
            : distribution ? 2.1 : 2.5)
            + (hovered && !emphasized && !traced ? 0.75 : 0)
            + fiberBoost(l.cores)
          // A CHORD REACHING THE SCREEN IS ALWAYS UNSURVEYED NOW. It used to go solid
          // when a cable joined the pair directly — but such a pair stands down the
          // moment its sheath is drawn, so the only way this line survives is that no
          // cable is on screen to be the surveyed one. Solid there is a straight line
          // through ground nothing runs under, claiming somebody walked it.
          const dashArray = l.kind === "backup" ? "5 8"
            : l.kind === "peer" ? "1.5 7"
            : !hovered ? UNCABLED_DASH : undefined
          const casingOver = l.kind === "peer" ? CASING_OVER_FINE : CASING_OVER
          return (
            <Fragment key={l.key}>
              {!dimmed && (
                <Polyline
                  interactive={false}
                  positions={pts}
                  pathOptions={{
                    color: "#000",
                    opacity: hovered ? CASING_OPACITY_HOVER : CASING_OPACITY,
                    ...casingAt(lineK, weight, casingOver, dashArray),
                  }}
                />
              )}
              <Polyline
                interactive={false}
                positions={pts}
                pathOptions={{
                  color: (emphasized || traced) && l.tone === "muted" ? "var(--primary)"
                    : lineColor(l.tone),
                  opacity: dimmed && !traced ? 0.12
                    : hovered || emphasized || traced ? 1
                    : l.kind === "peer" ? 0.85 : l.tone === "muted" ? 0.85 : 0.9,
                  ...strokeAt(lineK, weight, dashArray),
                }}
              />
              {labeled && chipIcon && (
                <Marker
                  position={linkLabelPos(pts, l.labelPos)}
                  icon={chipIcon}
                  zIndexOffset={600}
                  draggable={editPins && canWrite}
                  eventHandlers={{
                    drag: (e) => {
                      const m = e.target as L.Marker
                      const ll = m.getLatLng()
                      const [x, y] = project(ll.lat, ll.lng, zoom)
                      const hit = nearestOnPath(
                        pts.map(([la, ln]) => project(la, ln, zoom)), x, y)
                      m.setLatLng(pointAt(pts, hit.seg, hit.t))
                    },
                    dragend: (e) => {
                      const ll = (e.target as L.Marker).getLatLng()
                      const [x, y] = project(ll.lat, ll.lng, zoom)
                      const hit = nearestOnPath(
                        pts.map(([la, ln]) => project(la, ln, zoom)), x, y)
                      const total = polyKm(pts)
                      setLinkStyle.mutate({
                        childId: l.childId, parentId: l.parentId,
                        style: { label_pos: total > 0 ? alongKm(pts, hit.seg, hit.t) / total : 0.5 },
                      })
                    },
                    click: () => {
                      if (placingId != null || routeEdit != null || editPins) return
                      if (l.binding) {
                        setDetailTab("ports")
                        selectDevice([...l.binding.keys()][0])
                      } else {
                        selectDevice(l.to.id)
                      }
                    },
                  }}
                />
              )}
            </Fragment>
          )
        })}
        {powerIncidents.map((inc, i) => {
          const r = (inc.radius_km ?? 0) * 1000 * 1.15
          if (r < HULL_MIN_M) return null
          return (
            <Circle
              key={`pw-${i}-${inc.since ?? ""}`}
              center={inc.center as [number, number]}
              radius={r}
              interactive={false}
              pathOptions={{
                color: "var(--warning)", opacity: 0.6,
                fillColor: "var(--warning)", fillOpacity: 0.07,
                ...strokeAt(lineK, 1.5, "6 6"),
              }}
            />
          )
        })}
        {cutSegments.map((s) => (
          <Fragment key={s.key}>
            <Polyline
              interactive={false}
              positions={s.pts}
              pathOptions={{ color: "var(--destructive)", opacity: 0.85,
                ...strokeAt(lineK, 5, "6 5") }}
            />
            <Marker
              position={s.mid}
              icon={cutIcon(s.fault, s.oltName)}
              zIndexOffset={900}
              eventHandlers={{
                click: () => {
                  if (placingId != null || routeEdit != null) return
                  setDetailTab("optical")
                  selectDevice(s.fault.device_id)
                },
              }}
            />
          </Fragment>
        ))}
        {routeEdit && (() => {
          const ends: [[number, number], [number, number]] | null =
            editingDrop && editingDropAnchor
              ? [[(editingDropAnchor.device as Placed).lat,
                  (editingDropAnchor.device as Placed).lng],
                 [editingDrop.lat, editingDrop.lng]]
              : null
          const anchored = routeEdit.kind !== "cable"
          if (anchored && !ends) return null
          const line: Array<[number, number]> = ends
            ? [ends[0], ...routeEdit.points, ends[1]]
            : routeEdit.points
          return (
            <>
              {line.length > 1 && <Polyline
                interactive={false}
                positions={line}
                pathOptions={{ color: "var(--primary)", opacity: 0.9,
                  ...strokeAt(lineK, 2.5, "6 6") }}
              />}
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
            const sel = c.members.some((m) => m.id === selectedId)
            return (
              <Marker
                key={c.key}
                position={c.center}
                icon={clusterIcon(c.members, {
                  dim: troubleOnly && !c.members.some(isTrouble), selected: sel,
                })}
                eventHandlers={{
                  click: () => onClusterClick(c),
                  mouseover: () => setHoverSiteId(c.members[0].id),
                  mouseout: () => setHoverSiteId((h) =>
                    (c.members.some((m) => m.id === h) ? null : h)),
                }}
                zIndexOffset={sel ? MARK_Z_SELECTED
                  : anyDown ? MARK_Z_DOWN : MARK_Z_GEAR}
              />
            )
          }
          const d = c.members[0]
          const dim = troubleOnly && !isTrouble(d) && d.id !== selectedId
          const impact = downstream.has(d.id)
          const passive = isPassiveType(d.device_type)
          const load = passive ? loadByPassive.get(d.id) : undefined
          const loadOlt = load?.olt_id != null ? byId.get(load.olt_id) : undefined
          const frozen = !!loadOlt && isDownState(loadOlt)
          return (
            <Marker
              key={d.id}
              position={[d.lat, d.lng]}
              icon={pinIcon(d, {
                selected: d.id === selectedId, dim, impact,
                label: passive ? passivePinLabel(d) : undefined,
                dropTone: passive ? dropTone(load, frozen) : undefined,
                title: passive ? passiveTitle(d, load, frozen) : undefined,
              })}
              draggable={editPins && canWrite}
              eventHandlers={{
                mouseover: () => setHoverId(d.id),
                mouseout: () => setHoverId((h) => (h === d.id ? null : h)),
                contextmenu: (e) => {
                  const p = (e as L.LeafletMouseEvent).containerPoint
                  openPlantMenu(d.lat, d.lng, p.x, p.y, d)
                },
                click: (e) => {
                  if (plantMenu != null) { setPlantMenu(null); return }
                  if (routeEdit != null) return
                  if (addNext) {
                    setAddNext(false)
                    const p = (e as L.LeafletMouseEvent).containerPoint
                    openPlantMenu(d.lat, d.lng, p.x, p.y, d)
                    return
                  }
                  if (armed != null) {
                    if (armed.kind === "customer") {
                      setCustomerDraft({ lat: d.lat, lng: d.lng, passiveId: armed.passiveId })
                    } else {
                      setPlantDraft({ kind: armed.kind, lat: d.lat, lng: d.lng })
                    }
                    setArmed(null)
                    return
                  }
                  if (placingOnu != null) {
                    setOnuPlace.mutate({ mac: placingOnu.mac, lat: d.lat, lng: d.lng,
                                         label: placingOnu.label || null })
                    if (!refOnus) toggleRefOnus()
                    toast.success(`Placed at ${d.name}`)
                    setSelectedOnuMac(placingOnu.mac)
                    setPlacingOnu(null)
                    return
                  }
                  if (placingId != null) {
                    if (placingId !== d.id) {
                      setLocation.mutate({ id: placingId, lat: d.lat, lng: d.lng })
                      toast.success(`Placed at ${d.name} (same site)`)
                      selectDevice(placingId)
                    }
                    setPlacingId(null)
                    return
                  }
                  setDetailTab(deviceTabs(d)[0])
                  // A pin click still opens the panel — including on a device already
                  // pin-selected from a search hit or a site-card row, where closing the
                  // selection instead would leave the panel unreachable from the map.
                  if (d.id === selectedId && panelFor === d.id) selectDevice(null)
                  else selectDevice(d.id)
                },
                dragend: (e) => {
                  const ll = (e.target as L.Marker).getLatLng()
                  const near = nearestOther(d.id, ll.lat, ll.lng)
                  if (near) toast.success(`Snapped to ${near.name} (same site)`)
                  setLocation.mutate({
                    id: d.id,
                    lat: near ? near.lat : ll.lat,
                    lng: near ? near.lng : ll.lng,
                  })
                },
              }}
              zIndexOffset={markZIndex(d, {
                selected: d.id === selectedId, impact, plant: passive,
              })}
            />
          )
        })}
        {refShown && shownPlaces.map((p) => {
          // Its OWN hover goes solid (one line, narrated by its card); revealing a
          // whole splitter's worth stays DOTTED — none of those spans was surveyed.
          const own = p.mac === hoverOnuMac
          const hovered = own || hoverDropMacs.has(p.mac)
          if (!refLinesVisible && !hovered) return null
          if (routeEdit?.kind === "drop" && routeEdit.mac === p.mac) return null
          const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
          if (!anchor) return null
          const to = anchor.device as Placed
          const viaSplitter = anchor.kind === "splitter"
          const dropPath = viaSplitter ? (p.drop_waypoints ?? []) : []
          const traced = dropPath.length > 0
          const pts: Array<[number, number]> = traced
            ? [[to.lat, to.lng], ...dropPath, [p.lat, p.lng]]
            : [[to.lat, to.lng], [p.lat, p.lng]]
          const tone = refLineTone(p)
          const refWeight = (tone === "dark" ? 4.5 : viaSplitter ? 3.5 : 2.5)
            + (hovered ? REF_HOVER_BOOST : 0)
          const refDash = traced || own ? undefined
            : viaSplitter ? DROP_DASH : REF_DASH
          const bwIcon = chipShown.refs.has(p.mac) ? refBwIcon(p) : null
          return (
            <Fragment key={`refline:${p.mac}`}>
              <Polyline
                positions={pts}
                interactive={false}
                pathOptions={{
                  color: "#000",
                  opacity: hovered ? CASING_OPACITY_HOVER : CASING_OPACITY,
                  lineCap: "round",
                  ...casingAt(lineK, refWeight, CASING_OVER_FINE, refDash),
                }}
              />
              <Polyline
                positions={pts}
                interactive={false}
                className={`wisp-refline wisp-refline--${tone}`}
                pathOptions={{
                  color: tone === "dark" ? "var(--destructive)" : "var(--map-link)",
                  opacity: hovered ? 1
                    : tone === "dark" ? 0.95 : viaSplitter ? 0.9 : 0.75,
                  lineCap: "round",
                  ...strokeAt(lineK, refWeight, refDash),
                }}
              />
              {bwIcon && (
                <Marker
                  position={refChipPos(to, p)}
                  icon={bwIcon}
                  interactive={false}
                  zIndexOffset={-150}
                />
              )}
            </Fragment>
          )
        })}
        {branchFaults.map((f) => {
          const link = drawnLinks.find(
            (l) => (l.kind === "primary" || l.kind === "run")
              && ((l.to.id === f.passive_id && l.from.id === f.parent_id)
                || (l.kind === "run" && l.from.id === f.passive_id
                    && l.to.id === f.parent_id)))
          if (!link) return null
          const box = byId.get(f.passive_id)
          const parentBox = f.parent_id != null ? byId.get(f.parent_id) : undefined
          if (!box || !parentBox) return null
          const mid = pointAlong(link.pts, (polyKm(link.pts) * 1000) / 2)
          return (
            <Fragment key={`branch:${f.passive_id}`}>
              <Polyline
                positions={link.pts}
                interactive={false}
                className={`wisp-branchspan wisp-branchspan--${f.cause}`}
                pathOptions={{
                  color: f.cause === "power" ? "var(--warning)" : "var(--destructive)",
                  opacity: 0.3, lineCap: "round",
                  ...strokeAt(lineK, 7, f.cause === "power" ? "10 8" : undefined),
                }}
              />
              <Marker
                position={mid}
                icon={branchIcon(f, box.name, parentBox.name)}
                interactive={false}
                zIndexOffset={620}
              />
            </Fragment>
          )
        })}
        {refShown && shownPlaces.map((p) => {
          const onHovered = hoverDropMacs.has(p.mac)
          if (!refVisible && !onHovered) return null
          return (
          <Marker
            key={`ref:${p.mac}`}
            position={[p.lat, p.lng]}
            icon={refOnuIcon(p, {
              selected: p.mac === selectedOnuMac,
              dim: troubleOnly && !isRefDark(p) && !onHovered,
            })}
            zIndexOffset={refZIndex(p, p.mac === selectedOnuMac,
                                    p.mac === hoverOnuMac || onHovered)}
            eventHandlers={{
              click: () => {
                if (routeEdit != null || placingId != null || placingOnu != null) return
                setSelectedOnuMac(p.mac === selectedOnuMac ? null : p.mac)
                selectDevice(null)
              },
              mouseover: () => {
                if (routeEdit != null || placingId != null || placingOnu != null) return
                setHoverOnuMac(p.mac)
              },
              mouseout: () => setHoverOnuMac((m) => (m === p.mac ? null : m)),
            }}
          />
          )
        })}
        {hoverPlace && (
          <RefHoverCard place={hoverPlace} ctx={hoverCtx(hoverPlace)} />
        )}
        {hoverDevice && (
          <DevHoverCard device={hoverDevice} ctx={devHoverCtx(hoverDevice)} />
        )}
        {hoverSite && (
          <SiteHoverCard cluster={hoverSite} ctx={siteHoverCtx(hoverSite)} />
        )}
        {refVisible && shownPlaces.map((p) => {
          if (!chipShown.names.has(p.mac)) return false
          const olt = p.device_id != null ? byId.get(p.device_id) : undefined
          return (
            <Marker
              key={`refname:${p.mac}`}
              position={[p.lat, p.lng]}
              icon={refNameIcon(p, { frozen: !!olt && isDownState(olt) })}
              interactive={false}
              zIndexOffset={-220}
            />
          )
        })}
        {showWorkers && fieldWorkers.map((w) => {
          if (!workerPlaced(w)) return null
          const state = workerState(w, workerFreshS, now)
          const fix = w.last_fix!
          const trail = w.trail.length >= 2 ? w.trail : null
          const style = trailStyle(state)
          return (
            <Fragment key={`worker:${w.user_id}`}>
              {trail && (
                <>
                  <Polyline
                    positions={trail}
                    interactive={false}
                    pathOptions={{
                      color: "#000", opacity: CASING_OPACITY,
                      lineCap: "round", lineJoin: "round",
                      ...casingAt(lineK, style.weight, CASING_OVER_FINE),
                    }}
                  />
                  <Polyline
                    positions={trail}
                    interactive={false}
                    pathOptions={{
                      color: state === "off" ? "var(--muted-foreground)" : "var(--primary)",
                      opacity: style.opacity, lineCap: "round", lineJoin: "round",
                      ...strokeAt(lineK, style.weight),
                    }}
                  />
                </>
              )}
              <Marker
                position={[fix.lat, fix.lng]}
                icon={workerIcon(w, state)}
                interactive={false}
                zIndexOffset={workerZIndex(state)}
              />
            </Fragment>
          )
        })}
        {myLoc && (
          <>
            {myLoc.acc <= 2000 && (
              <Circle
                center={[myLoc.lat, myLoc.lng]}
                radius={myLoc.acc}
                interactive={false}
                pathOptions={{ color: "var(--primary)", opacity: 0.35,
                  fillOpacity: 0.08, ...strokeAt(lineK, 1) }}
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

      {googleActive && (
        <span aria-hidden className="pointer-events-none absolute bottom-1 left-2 z-[1000] select-none font-medium"
          style={{
            fontFamily: "'Product Sans', Roboto, Arial, sans-serif", fontSize: "18px",
            color: "#fff", textShadow: "0 0 4px rgba(0,0,0,.55), 0 1px 2px rgba(0,0,0,.55)",
          }}>
          Google
        </span>
      )}

      <div className="wisp-panel-strip pointer-events-none absolute top-3 left-3 z-[1002] flex max-w-[calc(100%-6rem)] flex-wrap items-center gap-2">
        <MapSearch devices={devices} org={scopeOrg} bounds={region.bounds}
          onDevice={searchDevice} onOnu={searchOnu} onPlace={searchPlace} />
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
        {powerIncidents.length > 0 && (
          <button
            className="pointer-events-auto flex h-8 max-w-full min-w-0 items-center gap-2 rounded-lg border border-warning/50 bg-popover/95 px-3 text-xs backdrop-blur hover:brightness-110 dark:bg-popover/95"
            title="Zoom to the affected area"
            onClick={() => {
              const inc = powerIncidents[0]
              if (!inc.center) return
              mapRef.current?.flyToBounds(
                L.latLng(inc.center[0], inc.center[1])
                  .toBounds(Math.max((inc.radius_km ?? 0) * 2600, 1200)),
                { padding: [48, 48] })
            }}>
            <span className="shrink-0 font-semibold text-warning">⚡ Power-outage pattern</span>
            <span className="min-w-0 truncate text-muted-foreground">
              {powerIncidents[0].count} devices · {powerIncidents[0].branches} independent feeds
              {" · "}{(powerIncidents[0].radius_km ?? 0) * 1000 >= HULL_MIN_M
                ? `${(powerIncidents[0].radius_km ?? 0).toFixed(1)} km area`
                : "one site"}
              {powerIncidents[0].since && <> · {durationSince(powerIncidents[0].since)}</>}
            </span>
          </button>
        )}
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
        {onuScope && (() => {
          const olt = byId.get(onuScope.deviceId)
          const dark = shownPlaces.filter(isRefDark).length
          return (
            <div className="pointer-events-auto flex min-h-8 max-w-full flex-wrap items-center gap-1.5 rounded-lg border border-primary/40 bg-popover/95 dark:bg-popover/95 px-2 py-1 text-xs backdrop-blur">
              <Users className="size-3.5 shrink-0 text-primary" />
              <span className="max-w-40 truncate font-mono font-semibold">
                {olt?.name ?? `OLT ${onuScope.deviceId}`}
              </span>
              <span className="shrink-0 text-muted-foreground">
                {shownPlaces.length} located
                {dark > 0 && <span className="font-semibold text-destructive"> · {dark} dark</span>}
                {scopePlant && scopePlant.size > 0 && (
                  <span title="Passive plant on this OLT. The rest of the org's plant is hidden while a focus is on.">
                    {" · "}{scopePlant.size} plant
                  </span>
                )}
              </span>
              {scopePons.length > 1 && (
                <span className="mx-0.5 h-4 w-px shrink-0 bg-border" aria-hidden />
              )}
              {scopePons.length > 1 && (
                <span className="flex min-w-0 flex-wrap items-center gap-1">
                  <button
                    className={cn("rounded px-1.5 py-0.5 text-2xs hover:bg-foreground/5",
                      onuScope.pons.length === 0 ? "bg-accent font-semibold" : "text-muted-foreground")}
                    title="Every PON on this OLT"
                    onClick={() => scopeOnus(onuScope.deviceId, [])}>
                    All PONs
                  </button>
                  {scopePons.map((p) => (
                    <button key={p.pon}
                      title={`${p.total} located on ${p.pon}${p.dark > 0 ? ` · ${p.dark} dark` : ""}`
                        + " · click to add or drop it from the view"}
                      className={cn("rounded px-1.5 py-0.5 font-mono text-2xs hover:bg-foreground/5",
                        onuScope.pons.includes(p.pon) ? "bg-accent font-semibold" : "text-muted-foreground")}
                      onClick={() => toggleScopePon(onuScope.deviceId, p.pon)}>
                      {p.pon}
                      <span className="ml-1 text-faint-foreground">
                        {p.total}
                      </span>
                    </button>
                  ))}
                </span>
              )}
              <Button variant="ghost" size="icon" className="size-5 shrink-0"
                title="Stop focusing on this OLT" onClick={() => setOnuScope(null)}>
                <X className="size-3" />
              </Button>
            </div>
          )
        })()}
      </div>

      {(armed || addNext) && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(92vw,34rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur dark:bg-popover/95">
          <Crosshair className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            {addNext ? "Click the map to add something here" : armed?.kind === "customer" ? (
              <>Click where the customer stands
                {armed.passiveId != null && byId.get(armed.passiveId) && (
                  <> · on <span className="font-medium">{byId.get(armed.passiveId)!.name}</span></>
                )}
              </>
            ) : (
              <>Click where the {armed ? PLANT_LABEL[armed.kind as PlantKind] : "box"} goes</>
            )}
          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0" title="Cancel (Esc)"
            onClick={() => { setArmed(null); setAddNext(false) }}>
            <X className="size-3" />
          </Button>
        </div>
      )}

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

      {placingOnu && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(92vw,34rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Crosshair className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            Click where{" "}
            <span className="font-mono font-semibold">{placingOnu.label || placingOnu.mac}</span>
            {" "}stands

          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0" title="Cancel (Esc)"
            onClick={() => setPlacingOnu(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {routeEdit && (routeEdit.kind === "cable"
        || !!(editingDrop && editingDropAnchor)) && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(94vw,44rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Spline className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            {routeEdit.kind === "cable" ? (
              <>Click along <span className="font-mono font-semibold">{routeEdit.name}</span></>
            ) : (<>
              Click along the drop cable{" "}
              <span className="font-mono font-semibold">
                {editingDropAnchor!.device.name}
              </span>
              {" → "}
              <span className="font-mono font-semibold">{onuName(editingDrop!)}</span>
            </>)}
            <span className="text-muted-foreground"> · drag to adjust, double-click removes
              · {routeEdit.points.length} pt{routeEdit.points.length === 1 ? "" : "s"}</span>
            {routeEdit.kind === "cable" && routeEdit.cableId == null
              && routeEdit.points.length > 0 && (
              <span className="text-muted-foreground"> ·{" "}
                <span className={cn(routeEdit.endA && "text-foreground")}>
                  {routeEdit.endA?.name ?? "open ground"}
                </span>
                {routeEdit.points.length > 1 && <>
                  {" → "}
                  <span className={cn(routeEdit.endB && "text-foreground")}>
                    {routeEdit.endB?.name ?? "open ground"}
                  </span>
                </>}
              </span>
            )}
            {routeEdit.kind === "cable" && routeEdit.cableId == null
              && routeEdit.points.length < 2 && (
              <span className="text-warning"> · click at least
                {routeEdit.points.length === 1 ? " one more point" : " two points"}</span>
            )}
            {routeEdit.kind === "cable" && routeEdit.cableId != null
              && routeEdit.points.length === 1 && (
              <span className="text-warning"> · click at least one more point</span>
            )}
          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0" title="Undo last point (Ctrl+Z)"
            disabled={!routeEdit.points.length}
            onClick={() => setRouteEdit((re) => re && { ...re, points: re.points.slice(0, -1) })}>
            <Undo2 className="size-3" />
          </Button>
          <Button variant="ghost" size="icon" className="size-5 shrink-0"
            title="Straighten · drop every point, back to a straight line"
            disabled={!routeEdit.points.length}
            onClick={() => setRouteEdit((re) => re && { ...re, points: [] })}>
            <Slash className="size-3" />
          </Button>
          <Button size="sm" className="h-6 shrink-0 px-2 text-2xs"
            disabled={setRoute.isPending || setDropRoute.isPending
              || setCablePath.isPending
              || (routeEdit.kind === "cable"
                  && routeEdit.points.length < (routeEdit.cableId == null ? 2 : 1)
                  && !(routeEdit.cableId != null && routeEdit.points.length === 0))}
            onClick={() => routeEdit.kind === "cable"
              ? (routeEdit.cableId == null
                ? setCableForm({ name: "", cores: null, path: routeEdit.points,
                                 ends: [routeEdit.endA ?? null, routeEdit.endB ?? null],
                                 near: [
                                   routeEdit.endA ? null : nearMiss(routeEdit.points[0]),
                                   routeEdit.endB ? null
                                     : nearMiss(routeEdit.points[routeEdit.points.length - 1]),
                                 ] })
                : setCablePath.mutate({ cableId: routeEdit.cableId, path: routeEdit.points }))
              : setDropRoute.mutate({ mac: routeEdit.mac, waypoints: routeEdit.points })}>
            <Check className="size-3" /> Save
          </Button>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setRouteEdit(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      <div className={cn("absolute top-3 right-3 z-[1001] flex flex-col items-end gap-1.5",
        railOpen && "md:flex-row md:items-center")}>
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
                  {basemap === "google" && (
                    <button
                      className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                      title="Google's own place names, road names and POI markers. Off leaves the roads, water and parks; only the writing goes."
                      onClick={toggleGoogleLabels}>
                      <span>Google labels</span>
                      <span className={cn("text-2xs font-medium",
                        googleLabels ? "text-success" : "text-muted-foreground")}>
                        {googleLabels ? "on" : "off"}
                      </span>
                    </button>
                  )}
                  <div className="my-1 border-t" />
                </>
              )}
              <button
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                title="The parent → child lines the monitoring tree draws, dashed because they are a dependency and nobody surveyed them. A pair whose fibre IS recorded drops its line on its own — the sheath says the same thing along the route a van drives, and takes the link's ↓/↑ rate with it. So what stays dashed here is the plant nobody has written down yet. Switch off to hide those too."
                onClick={toggleUncabled}>
                <span>Dependency links</span>
                <span className={cn("text-2xs font-medium", showUncabled ? "text-success" : "text-muted-foreground")}>
                  {showUncabled ? "shown" : "hidden"}
                </span>
              </button>
              <button
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                title="Live ↓/↑ rate chips on links with a bound port (device panel → Uplinks). The zoom these and the cable name chips start drawing at is Settings → Platform → Map detail."
                onClick={toggleBwLabels}>
                <span>Bandwidth labels</span>
                {/* SAY WHEN THE FLOOR IS WHAT IS HIDING THEM, exactly as the
                    Subscribers row does. A toggle reading "on" over a map drawing
                    no chips reads as a broken feature rather than as a setting. */}
                <span className={cn("text-2xs font-medium",
                  bwLabels && zoom >= detail.line_labels
                    ? "text-success" : "text-muted-foreground")}>
                  {!bwLabels ? "off"
                    : zoom >= detail.line_labels ? "on" : "on · zoom in"}
                </span>
              </button>
              <button
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                title="Subscriber ONUs with a location: field-survey pins plus the power-backed reference points you've placed. Click an OLT to focus on just its drops and the plant feeding them."
                onClick={() => { setOnuScope(null); toggleRefOnus() }}>
                <span>Subscribers{places.length > 0 ? ` · ${places.length}` : ""}</span>
                <span className={cn("text-2xs font-medium",
                  onuScope != null || (refOnus && zoom >= detail.subscribers)
                    ? "text-success" : "text-muted-foreground")}>
                  {onuScope != null ? "focused"
                    : !refOnus ? "off"
                      : zoom >= detail.subscribers ? "on" : "on · zoom in"}
                </span>
              </button>
              {canWrite && (
                <button
                  className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                  title="Field workers running the tracker app on their phones. Live position plus today's route. Set up under Settings → Users → Location tracking."
                  onClick={toggleWorkers}>
                  <span>Workers{census.total > 0 ? ` · ${census.placed}/${census.total}` : ""}</span>
                  <span className={cn("text-2xs font-medium",
                    showWorkers ? "text-success" : "text-muted-foreground")}>
                    {showWorkers ? "on" : "off"}
                  </span>
                </button>
              )}
              {canWrite && showWorkers && census.total > 0 && (
                <p className="px-2 pb-1 text-2xs text-muted-foreground">
                  {census.live > 0 && <span className="text-foreground">{census.live} here now</span>}
                  {census.live > 0 && (census.quiet > 0 || census.off > 0 || census.never > 0) && " · "}
                  {census.quiet > 0 && <span className="text-warning">{census.quiet} gone quiet</span>}
                  {census.quiet > 0 && (census.off > 0 || census.never > 0) && " · "}
                  {census.off > 0 && `${census.off} off shift`}
                  {census.off > 0 && census.never > 0 && " · "}
                  {census.never > 0 && `${census.never} never reported`}
                </p>
              )}
              <div className="my-1 border-t" />
              <p className="px-2 pt-1 pb-0.5 text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                Links
              </p>
              {([
                ["", "var(--map-plant)", "Cable — route traced"],
                [CABLE_DASH, "var(--map-plant)", "Cable — not traced yet"],
                ["", "var(--map-link)", "Feed (parent → child)"],
                ["4 6", "var(--map-link)", "Backup uplink (ring)"],
                ["1 4", "var(--map-link)", "Cross-link (same level)"],
                [DROP_DASH, "var(--map-link)", "Subscriber drop (splitter → ONU)"],
              ] as Array<[string, string, string]>).map(([dash, color, label]) => (
                <div key={label} className="flex items-center gap-2 px-2 py-1 text-xs">
                  <span className="flex w-4 shrink-0 items-center justify-center">
                    <svg width="16" height="3" aria-hidden>
                      <line x1="0" y1="1.5" x2="16" y2="1.5" stroke={color}
                        strokeWidth={color === "var(--map-plant)" ? 3 : 2}
                        strokeDasharray={dash || undefined} />
                    </svg>
                  </span>
                  <span className="text-muted-foreground">{label}</span>
                </div>
              ))}
              <div className="my-1 border-t" />
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
                [<span key="s" className="size-2 rotate-45 rounded-[1px] bg-muted-foreground/60" />, "Splitter (passive)"],
                [<span key="s" className="flex size-3.5 items-center justify-center rounded-full border border-warning">
                  <span className="size-2 rounded-full bg-muted-foreground" />
                </span>, "Weak ONUs (ring)"],
                ...(canWrite
                  ? [[<span key="s" className="size-3 rounded-[3px] bg-primary" />,
                      "Field worker (initials)"]] as Array<[ReactNode, string]>
                  : []),
              ] as Array<[ReactNode, string]>).map(([swatch, label]) => (
                <div key={label} className="flex items-center gap-2 px-2 py-1 text-xs">
                  <span className="flex w-4 shrink-0 items-center justify-center">{swatch}</span>
                  <span className="text-muted-foreground">{label}</span>
                </div>
              ))}
              {dropsQ.data && (dropsQ.data.recorded + dropsQ.data.unrecorded) > 0 && (
                <>
                  <div className="my-1 border-t" />
                  <p className="px-2 py-1 text-2xs text-muted-foreground">
                    <span className="font-medium text-foreground">
                      {dropsQ.data.recorded}
                    </span>
                    {" of "}
                    {dropsQ.data.recorded + dropsQ.data.unrecorded}
                    {" subscribers mapped to a splitter"}
                  </p>
                </>
              )}
            </div>
          )}
        </div>
        <Button variant="outline" size="icon" className="size-8 bg-popover/95 dark:bg-popover/95 backdrop-blur"
          title="Fit all pins" onClick={fitAll} disabled={placed.length === 0}>
          <Maximize2 className="size-3.5" />
        </Button>
        <Button variant={cableOpen != null || cableList ? "default" : "outline"} size="icon"
          className={cn("size-8 backdrop-blur",
            cableOpen == null && !cableList && "bg-popover/95 dark:bg-popover/95")}
          title="Cables"
          onClick={() => {
            if (cableOpen != null || cableList) { setCableOpen(null); setCableList(false) }
            else setCableList(true)
          }}>
          <CableIcon className="size-3.5" />
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
        {canWrite && (
          <Button variant={addNext ? "default" : "outline"} size="icon"
            className={cn("size-8 backdrop-blur", !addNext && "bg-popover/95 dark:bg-popover/95")}
            title="Add a splitter or a customer (or right-click the map)"
            onClick={() => { setAddNext((v) => !v); setEditPins(false) }}>
            <Plus className="size-3.5" />
          </Button>
        )}
        {editPins && canWrite && (
          <div className={cn("pointer-events-none rounded-lg border border-warning/40 bg-popover/95 px-2.5 py-1.5 text-2xs text-warning backdrop-blur dark:bg-popover/95",
            railOpen && "md:order-first")}>
            drag pins to move them
          </div>
        )}
      </div>

      {(cableOpen != null || cableList) && !routeEdit && (() => {
        const cable = cableOpen == null ? null
          : cablesQ.data?.cables.find((c) => c.id === cableOpen)
        const cables = cablesQ.data?.cables ?? []
        return (
          <Card className="wisp-device-panel absolute inset-x-2 bottom-2 z-[1001] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:w-95 md:max-h-[calc(100%-4.5rem)]">
            <div className="flex items-center gap-1.5 border-b px-3 py-2">
              {cable && (
                <Button variant="ghost" size="icon" className="size-6"
                  title="All cables" onClick={() => { setCableOpen(null); setCableList(true) }}>
                  <ArrowLeft className="size-3.5" />
                </Button>
              )}
              <p className="min-w-0 flex-1 truncate text-xs font-semibold">
                {cable ? cable.name : `Cables · ${cables.length}`}
              </p>
              <Button variant="ghost" size="icon" className="size-6"
                onClick={() => { setCableOpen(null); setCableList(false) }}>
                <X className="size-3.5" />
              </Button>
            </div>
            {!cable && (
              <div className="overflow-y-auto px-2 py-2">
                <CableList cables={cables} canWrite={canWrite}
                  onOpen={(id) => { setCableList(false); setCableOpen(id) }}
                  onLay={canWrite ? () => {
                    setCableList(false)
                    setRouteEdit({ kind: "cable", cableId: null, name: "New cable",
                                   points: [] })
                  } : undefined} />
              </div>
            )}
            {cable && (
            <div className="overflow-y-auto px-3 py-2.5">
              {cableForm?.id === cable.id ? (
                <CableForm initial={cableForm}
                  ends={[cable.a.name ?? "unplaced", cable.b.name ?? "unplaced"]}
                  busy={saveCable.isPending}
                  onCancel={() => setCableForm(null)}
                  onSave={(v) => saveCable.mutate({ ...v, id: cable.id })} />
              ) : (
                <CablePanel cable={cable} canWrite={canWrite}
                  busy={saveCable.isPending || splitCable.isPending}
                  selectedCore={traceCore} onCore={setTraceCore}
                  onEdit={() => setCableForm({
                    id: cable.id, name: cable.name, cores: cable.cores })}
                  onDelete={cableDelete.ask}
                  onTrace={(coreNo) =>
                    setTraceFrom({ cableId: cable.id, coreNo })}
                  onRetrace={() => setRouteEdit({
                    kind: "cable", cableId: cable.id, name: cable.name,
                    points: cable.path ?? [],
                  })}
                  onSplit={() => setSplitAt({
                    cableId: cable.id, cableName: cable.name })}
                  onLabel={(coreNo, label) =>
                    setCoreLabel.mutate({ cableId: cable.id, coreNo, label: label ?? "" })}
                  moveTargets={cableMoveTargets.filter(
                    (t) => t.device_id !== cable.a.device_id
                        && t.device_id !== cable.b.device_id)}
                  onMoveEnd={(end, to) =>
                    askMoveEnd(cable.id, end, cable[end], to)} />
              )}
            </div>
            )}
          </Card>
        )
      })()}

      {moveAsk && (
        <ConfirmDialog open onOpenChange={(v) => { if (!v) setMoveAsk(null) }}
          title={`Move this end to ${moveAsk.to.name}?`}
          description={[
            moveAsk.discards
              ? `${moveAsk.discards} splice${moveAsk.discards === 1 ? "" : "s"} made at `
                + `${moveAsk.from.name ?? "the old point"} ${moveAsk.discards === 1 ? "is" : "are"} `
                + "discarded — those fibres stop meeting anywhere."
              : "No splice is lost.",
            moveAsk.carries
              ? `${moveAsk.carries} travel${moveAsk.carries === 1 ? "s" : ""} with the cable.`
              : "",
            moveAsk.unported
              ? `${moveAsk.unported} land${moveAsk.unported === 1 ? "s" : ""} on a port `
                + "already taken there, so the port is left unrecorded."
              : "",
            moveAsk.collapses
              ? `${moveAsk.collapses} single fibre${moveAsk.collapses === 1 ? "" : "s"} `
                + `connecting the two boxes ${moveAsk.collapses === 1 ? "is" : "are"} `
                + "removed — they would run from that box back to itself."
              : "",
            "The traced route is kept.",
          ].filter(Boolean).join(" ")}
          confirmLabel="Move end"
          onConfirm={doMoveEnd} />
      )}

      {cableOpen != null && (() => {
        const cable = cablesQ.data?.cables.find((c) => c.id === cableOpen)
        if (!cable) return null
        return (
          <ConfirmDialog {...cableDelete.props}
            title={`Delete "${cable.name}"?`}
            description={cable.cores_recorded
              ? `${cable.cores_recorded} recorded core${cable.cores_recorded === 1 ? "" : "s"} go with it — a splice names two fibres, and one of them is about to stop existing.`
              : "Nothing is recorded on it yet."}
            confirmLabel="Delete cable"
            onConfirm={() => deleteCable.mutate(cable.id)} />
        )
      })()}

      {splitAt && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(94vw,44rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Scissors className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            Click where{" "}
            <span className="font-mono font-semibold">{splitAt.cableName}</span>
            {" "}is opened
            <span className="text-muted-foreground"> · it snaps to the cable, and
              every core is spliced straight through</span>
          </span>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setSplitAt(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {traceFrom != null && traceQ.data && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(94vw,52rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-border-strong bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Route className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate">
            {traceQ.data.hops.map((h, i) => (
              <Fragment key={`${h.cable_id}:${h.core_no}:${i}`}>
                {i === 0 && (
                  <span className="font-mono font-semibold">{h.from.name}</span>
                )}
                <span className="text-muted-foreground">
                  {" "}·{" "}{h.cable_name} core {h.core_no}{" "}·{" "}
                </span>
                <span className="font-mono font-semibold">{h.to.name}</span>
              </Fragment>
            ))}
            {traceQ.data.fault === "fork" && (
              <span className="text-warning">
                {" "}· stops at {traceQ.data.fault_at?.name} — two fibres claim to
                continue it
              </span>
            )}
            {traceQ.data.fault === "loop" && (
              <span className="text-warning">
                {" "}· the record loops back on itself at {traceQ.data.fault_at?.name}
              </span>
            )}
          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0"
            title="Stop following" onClick={() => setTraceFrom(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

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
                onClick={() => { setPlacingId(d.id); setPlaceOpen(false); selectDevice(null) }}>
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
                  onClick={() => { setDetailTab(deviceTabs(m)[0]); focusDevice(m) }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { setDetailTab(deviceTabs(m)[0]); focusDevice(m) }
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
                    <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                      title={`Open ${m.name}'s panel`}
                      onClick={(e) => {
                        e.stopPropagation()
                        setDetailTab(deviceTabs(m)[0])
                        selectDevice(m.id)
                      }}>
                      <PanelRight className="size-3.5" />
                    </Button>
                    {canWrite && editPins && (
                      <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
                        title={`Move ${m.name}: click its new spot on the map`}
                        onClick={(e) => {
                          e.stopPropagation()
                          setSiteAnchor(null)
                          selectDevice(null)
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

      {selectedRef && !routeEdit && (
        <Card className="wisp-device-panel absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)]">
          <PanelResizeGrip grip={panel.grip} />
          <SubscriberDetail
            mac={selectedRef.mac}
            actions={{
              onClose: () => setSelectedOnuMac(null),
              onPlace: (mac, label) => {
                setPlacingOnu({ mac, label })
                setSelectedOnuMac(null)
              },
              onOpenOlt: (deviceId, mac) => {
                setDetailTab("optical")
                setDetailOnu({ deviceId, mac })
                selectDevice(deviceId)
                setSelectedOnuMac(null)
              },
              onOpenPassive: (deviceId) => {
                selectDevice(deviceId)
                setSelectedOnuMac(null)
              },
              onTraceDrop: (m) => {
                const place = places.find((x) => x.mac === m)
                if (place) startDropRouteEdit(place)
                setSelectedOnuMac(null)
              },
            }}
            fibre={canWrite || (fibreQ.data?.cables?.length ?? 0) > 0 ? (
              <FibrePanel
                open={fibreOpen}
                onOpen={(v) => { setFibreOpen(v); if (!v) setTrayError(null) }}
                cables={refCables}
                todo={0}
                fibre={fibreQ.data}
                loading={fibreQ.isLoading}
                canWrite={canWrite}
                busy={setFibreJoint.isPending || spliceThrough.isPending
                      || clearFibreJoint.isPending || takeCoreToBox.isPending}
                error={trayError}
                boxes={trayBoxes}
                boxOf={boxOf}
                people={trayPeople}
                onClearError={() => setTrayError(null)}
                onOpenCable={(id) => { setCableList(false); setCableOpen(id) }}
                onJoin={(a, b, port) => trayPoint
                  && setFibreJoint.mutate({ point: trayPoint, a, b, port })}
                onTail={(a, to, far) => trayPoint
                  && takeCoreToBox.mutate({ point: trayPoint, a, to, far })}
                onConnect={(deviceId, port, far) => trayPoint
                  && connectPort.mutate({ point: trayPoint, deviceId, port, far })}
                onThrough={(a, b) => trayPoint
                  && spliceThrough.mutate({ point: trayPoint, a, b })}
                onClear={(f) => trayPoint && clearFibreJoint.mutate({
                  point: trayPoint, cableId: f.cableId, coreNo: f.coreNo })}
                onTrace={(f) => {
                  setTraceCore(null)
                  setTraceFrom({ cableId: f.cableId, coreNo: f.coreNo })
                }} />
            ) : null} />
        </Card>
      )}

      {selected && panelFor === selected.id && !routeEdit && cableOpen == null && !cableList && (
        <Card className="wisp-device-panel absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)]">
          <PanelResizeGrip grip={panel.grip} />
          <DevicePanelHeader device={selected} tone={pinTone(selected)}
            downstream={downstream.size} downstreamDown={downstreamDown}>
            <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
              title="Show in the Network tree"
              onClick={() => navigate("/topology", { state: { deviceId: selected.id } })}>
              <ListTree className="size-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
              onClick={() => selectDevice(null)}>
              <X className="size-3.5" />
            </Button>
          </DevicePanelHeader>
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
                  {canWrite && isPlaced(selected) && (
                    <span className="mx-1 h-4 w-px shrink-0 bg-border" aria-hidden />
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
                  {canWrite && isPlaced(selected) && (
                    <Button variant="ghost" size="icon"
                      className="size-7 text-muted-foreground hover:text-destructive"
                      title="Remove this pin from the map"
                      onClick={confirmUnpin.ask}>
                      <MapPinOff className="size-3.5" />
                    </Button>
                  )}
                  {canWrite && isPassiveType(selected.device_type) && (
                    <Button variant="ghost" size="icon"
                      className="size-7 text-muted-foreground hover:text-destructive"
                      title="Delete this box and its plant record"
                      onClick={() => {
                        setPlantDelete(selected)
                        confirmPlantDelete.ask()
                      }}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  )}
                </span>
              </>
            )}
          </div>
          {linkKm != null && parent && (
            <div className="flex min-w-0 items-center gap-x-1.5 border-b px-4 py-1.5 text-xs text-muted-foreground">
              {routeKm != null && (
                <span className="shrink-0" title={`Along the drawn cable route to ${parent.name}`}>
                  <span className="font-semibold text-foreground">{fmtKm(routeKm)}</span> cable
                </span>
              )}
              <span className="shrink-0"
                title={`Straight-line distance to ${parent.name}, not cable length`}>
                {routeKm != null && <span className="text-faint-foreground">· </span>}
                {fmtKm(linkKm)} straight-line
              </span>
              <span className="min-w-0 truncate">
                to <span className="font-mono">{parent.name}</span>
              </span>
            </div>
          )}
          {(placedByOlt.get(selected.id) ?? 0) > 0 && (() => {
            const pons = ponsByOlt.get(selected.id) ?? []
            const focused = onuScope?.deviceId === selected.id
            const picked = focused ? onuScope.pons : []
            const label = !focused ? "Show on map"
              : picked.length === 0 ? "All PONs"
              : picked.length === 1 ? picked[0]
              : `${picked.length} PONs`
            return (
              <div className="flex min-h-9 items-center gap-2 border-b px-4 py-1.5 text-xs">
                <Users className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 truncate text-muted-foreground">
                  <span className="font-semibold text-foreground">
                    {placedByOlt.get(selected.id)}
                  </span>
                  {" located"}
                  {selected.onus_total ? ` of ${selected.onus_total}` : ""}
                </span>
                {pons.length < 2 ? (
                  <Button variant={focused ? "default" : "outline"}
                    size="sm" className="ml-auto h-7 px-2 text-xs"
                    title="Draw only this OLT's located subscribers and the plant feeding them, and frame it"
                    onClick={() => focused ? setOnuScope(null) : scopeOnus(selected.id, [])}>
                    {focused ? "Focused" : "Show on map"}
                  </Button>
                ) : (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant={focused ? "default" : "outline"}
                        size="sm" className="ml-auto h-7 max-w-36 px-2 text-xs"
                        title="Draw this OLT's located subscribers and the plant feeding them: all PONs, or the ones you pick">
                        <span className="min-w-0 truncate font-mono">{label}</span>
                        <ChevronDown className="size-3 shrink-0 opacity-60" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-56">
                      <DropdownMenuLabel className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                        Show subscribers and plant on
                      </DropdownMenuLabel>
                      <DropdownMenuCheckboxItem
                        checked={focused && picked.length === 0}
                        onSelect={(e) => e.preventDefault()}
                        onCheckedChange={() => scopeOnus(selected.id, [])}>
                        All PONs
                        <span className="ml-auto font-mono text-2xs text-faint-foreground">
                          {placedByOlt.get(selected.id)}
                        </span>
                      </DropdownMenuCheckboxItem>
                      <DropdownMenuSeparator />
                      {pons.map((p) => (
                        <DropdownMenuCheckboxItem key={p.pon}
                          checked={focused && picked.includes(p.pon)}
                          title={`${p.total} located on ${p.pon}`
                            + (p.dark > 0 ? ` · ${p.dark} dark` : "")}
                          onSelect={(e) => e.preventDefault()}
                          onCheckedChange={() => toggleScopePon(selected.id, p.pon)}>
                          <span className="min-w-0 truncate font-mono">{p.pon}</span>
                          <span className="ml-auto font-mono text-2xs text-faint-foreground">
                            {p.total}
                          </span>
                        </DropdownMenuCheckboxItem>
                      ))}
                      {focused && (
                        <>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem onSelect={() => setOnuScope(null)}>
                            <X className="size-3.5" /> Stop focusing
                          </DropdownMenuItem>
                        </>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </div>
            )
          })()}
          <div className="flex flex-col gap-2.5 overflow-y-auto overscroll-contain p-3">
            {(canWrite || deviceCables.length > 0) && (
              <FibrePanel
                open={fibreOpen}
                onOpen={(v) => { setFibreOpen(v); if (!v) setTrayError(null) }}
                cables={deviceCables}
                todo={fibreTodo}
                fibre={fibreQ.data}
                loading={fibreQ.isLoading}
                canWrite={canWrite}
                busy={setFibreJoint.isPending || spliceThrough.isPending
                      || clearFibreJoint.isPending || takeCoreToBox.isPending}
                error={trayError}
                boxes={trayBoxes}
                boxOf={boxOf}
                people={trayPeople}
                onClearError={() => setTrayError(null)}
                onOpenCable={(id) => { setCableList(false); setCableOpen(id) }}
                onJoin={(a, b, port) => trayPoint
                  && setFibreJoint.mutate({ point: trayPoint, a, b, port })}
                onTail={(a, to, far) => trayPoint
                  && takeCoreToBox.mutate({ point: trayPoint, a, to, far })}
                onConnect={(deviceId, port, far) => trayPoint
                  && connectPort.mutate({ point: trayPoint, deviceId, port, far })}
                onDrop={(mac, port) => trayPoint?.device_id != null
                  && dropOnLeg.mutate({ mac, passiveId: trayPoint.device_id,
                                        legNo: port.ref })}
                onThrough={(a, b) => trayPoint
                  && spliceThrough.mutate({ point: trayPoint, a, b })}
                onClear={(f) => trayPoint && clearFibreJoint.mutate({
                  point: trayPoint, cableId: f.cableId, coreNo: f.coreNo })}
                onTrace={(f) => {
                  setTraceCore(null)
                  setTraceFrom({ cableId: f.cableId, coreNo: f.coreNo })
                }} />
            )}
            <DeviceDetail device={selected} tab={detailTab}
              onTab={(t) => { setDetailTab(t); setDetailOnu(null) }}
              onOpenFibre={() => setFibreOpen(true)}
              focusOnuMac={detailOnu?.deviceId === selected.id ? detailOnu.mac : null} />
          </div>
          <ConfirmDialog {...confirmUnpin.props}
            title={`Take ${selected.name} off the map?`}
            description="The coordinates are deleted and nothing keeps a copy, so a pin placed in the field loses its GPS accuracy too. The device, its topology and its history are untouched."
            confirmLabel="Take off the map"
            onConfirm={unpinSelected} />
        </Card>
      )}

      {!isLoading && placed.length === 0 && !placing && (
        <div className="pointer-events-none absolute inset-0 z-[999] flex items-center justify-center">
          <div className="pointer-events-auto flex flex-col items-center gap-2 rounded-xl border border-border-strong bg-popover/95 dark:bg-popover/95 px-6 py-5 text-center backdrop-blur">
            <MapPin className="size-5 text-muted-foreground" />
            <p className="text-sm font-medium">
              {isWorker && devices.length === 0 ? "Nothing assigned to you yet"
                : "No devices on the map yet"}
            </p>
            {isWorker && devices.length === 0 && (
              <p className="max-w-64 text-xs text-muted-foreground">{NO_ASSIGNED_DEVICES}</p>
            )}
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

      {plantMenu && canWrite && (
        <PlantMenu
          anchor={plantMenu}
          near={menuFeeder}
          dropOn={menuDropOn}
          cut={menuCut}
          onCut={(cableId, cableName) => {
            setSplitAt({ cableId, cableName })
            setPlantMenu(null)
          }}
          width={wrapRef.current?.clientWidth ?? 0}
          height={wrapRef.current?.clientHeight ?? 0}
          onClose={() => setPlantMenu(null)}
          onPlant={(kind) => {
            setPlantDraft({ kind, lat: plantMenu.lat, lng: plantMenu.lng })
            setPlantMenu(null)
          }}
          onArm={(kind, passiveId) => {
            setArmed({ kind, passiveId })
            setPlantMenu(null)
            selectDevice(null)
          }}
          onCable={(lat, lng, on) => {
            setPlantMenu(null)
            selectDevice(null)
            setPlacingId(null)
            setRouteEdit({
              kind: "cable", cableId: null, name: "New cable",
              points: [[lat, lng]],
              endA: on ? { device_id: on.id, name: on.name } : null, endB: null })
          }}
          onCustomer={(passiveId) => {
            setCustomerDraft({ lat: plantMenu.lat, lng: plantMenu.lng, passiveId })
            setPlantMenu(null)
          }}
          onOpenDevice={(d) => {
            setDetailTab(deviceTabs(d)[0])
            selectDevice(d.id)
            setPlantMenu(null)
          }}
          onDelete={(d) => {
            setPlantMenu(null)
            setPlantDelete(d)
            confirmPlantDelete.ask()
          }}
        />
      )}
      {plantDelete && plantDeleteToll && (
        plantDeleteToll.children > 0 ? (
          <ConfirmDialog {...confirmPlantDelete.props}
            title={`${plantDelete.name} has ${plantDeleteToll.children} box${plantDeleteToll.children === 1 ? "" : "es"} below it`}
            description="Move or delete those first. A box with plant hanging off it can't be removed."
            confirmLabel="Close"
            onConfirm={() => setPlantDelete(null)} />
        ) : (
          <ConfirmDialog {...confirmPlantDelete.props}
            title={`Delete ${plantDelete.name}?`}
            description={[
              plantDeleteToll.drops
                ? `${plantDeleteToll.drops} recorded drop${plantDeleteToll.drops === 1 ? "" : "s"} go with it. Those subscribers stay in the roster and go back to reading "splitter not recorded".`
                : "",
              plantDeleteToll.cables
                ? `${plantDeleteToll.cables} cable${plantDeleteToll.cables === 1 ? "" : "s"} ending here ${plantDeleteToll.cables === 1 ? "is" : "are"} deleted too, with the splices made in them.`
                : "",
              !plantDeleteToll.drops && !plantDeleteToll.cables
                ? "Nothing is recorded against it."
                : "",
              "Nothing keeps a copy.",
            ].filter(Boolean).join(" ")}
            confirmLabel="Delete"
            onConfirm={() => deletePassive.mutate(plantDelete.id)} />
        )
      )}
      {canWrite && cableForm && cableForm.id == null && (
        <Dialog open onOpenChange={(v) => { if (!v) setCableForm(null) }}>
          <DialogContent className="sm:max-w-sm">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Spline className="size-4 text-muted-foreground" />
                Name this cable
              </DialogTitle>
              <DialogDescription>
                {cableForm.path?.length ?? 0} points traced.
                {cableForm.ends && (cableForm.ends[0] == null || cableForm.ends[1] == null)
                  ? " A closure is created at each end that lands on open ground."
                  : " It runs between the two points you clicked."}
              </DialogDescription>
            </DialogHeader>
            <CableForm initial={cableForm} busy={saveCable.isPending}
              ends={[cableForm.ends?.[0]?.name ?? "new closure",
                     cableForm.ends?.[1]?.name ?? "new closure"]}
              near={[
                cableForm.near?.[0]
                  ? { name: cableForm.near[0].end.name, m: cableForm.near[0].m } : null,
                cableForm.near?.[1]
                  ? { name: cableForm.near[1].end.name, m: cableForm.near[1].m } : null,
              ]}
              onLand={(i) => setCableForm((f) => {
                const n = f?.near?.[i]
                if (!f || !n) return f
                const ends: [FibreEnd | null, FibreEnd | null] =
                  [f.ends?.[0] ?? null, f.ends?.[1] ?? null]
                ends[i] = n.end
                const near: typeof f.near = [f.near?.[0] ?? null, f.near?.[1] ?? null]
                near[i] = null
                return { ...f, ends, near }
              })}
              onCancel={() => setCableForm(null)}
              onSave={(v) => saveCable.mutate({
                ...v, path: cableForm.path, ends: cableForm.ends })} />
          </DialogContent>
        </Dialog>
      )}
      {canWrite && (
        <PlantCreateDialog draft={plantDraft} devices={devices} org={scopeOrg}
          onClose={() => setPlantDraft(null)} onCreated={onPlantCreated} />
      )}
      {canWrite && (
        <AttachCustomerDialog draft={customerDraft} devices={devices} org={scopeOrg}
          onClose={() => setCustomerDraft(null)} onAttached={onCustomerAttached} />
      )}
    </div>
  )
}
