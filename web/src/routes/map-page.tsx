// Geographic NOC view: every placed device is a live status pin, topology links
// draw between placed parent/child pairs, and clicking a pin opens the same
// Health/Optical/Ports panel the Network tree uses. Placement is dashboard-side
// only (lat/lng on org_devices) — the edge never sees coordinates.
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
  ArrowDown, ArrowLeftRight, ArrowUp, Check, ChevronDown, ChevronRight, Copy, Crosshair,
  Expand, EyeOff, Layers, ListTree, LocateFixed, MapPin, Maximize2, Navigation, Pencil,
  Shrink, Slash, Spline, Undo2, Users, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDarkMode } from "@/hooks/use-dark-mode"
import { useNow } from "@/hooks/use-now"
import { PanelResizeGrip, useResizablePanel } from "@/hooks/use-resizable-panel"
import { inventoryApi, orgsApi, ApiError } from "@/lib/api"
import { mapRegionOf } from "@/lib/map-regions"
import { isPassiveType, type OnuPlace, type OrgDevice, type PonFault } from "@/lib/types"
import {
  DeviceDetail, DevicePanelHeader, RowTag, deviceTabs, type DeviceTab,
} from "@/components/device-detail"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { durationSince } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

import {
  AttributionPrefix, BASEMAP_KEY, BASEMAP_LABEL, GOOGLE_BASEMAPS, GoogleLayer,
  StreetsTiles, loadBasemap, type Basemap,
} from "@/map/basemaps"
import { buildClusters, clusterIcon, project, toneRank, type SiteCluster } from "@/map/clusters"
import { cutIcon, pointAlong, ponPath, subPath } from "@/map/cut"
import { alongKm, distanceKm, fmtKm, nearestOnPath, pointAt, polyKm } from "@/map/geometry"
import { LINK_COLORS, isLinkColor, linkColorName, linkColorVar, paintedLineColor } from "@/map/linkcolor"
import { LinkHoverProbe, hoverIcon, projectLinks, type LinkHover } from "@/map/linkhover"
import { bindLinkPorts, linkBwIcon, linkKey, linkLabelPos, type LinkBinding } from "@/map/linklabel"
import {
  isDownState, isPlaced, isTrouble, meIcon, pinIcon, pinTone, vertexIcon, type Placed,
} from "@/map/pins"
import {
  REF_DASH, isRefDark, refBwIcon, refHasRate, refLineTone, refOnuIcon, refZIndex,
} from "@/map/refonu"
import {
  DROP_DASH, branchIcon, dropAnchor, dropTone, loadsById, passiveSubLabel,
  passiveTitle,
} from "@/map/drops"
import { MapSearch, type OnuHit, type PlaceHit } from "@/map/search"
import { FIT_PADDING, MapEvents, ViewController, loadView } from "@/map/view"

const BW_LABELS_KEY = "wisp:map:bw-labels"
const REF_ONUS_KEY = "wisp:map:ref-onus"
const GOOGLE_LABELS_KEY = "wisp:map:google-labels"

/** Reference ONUs only render from street zoom in.
 *
 *  They are subscriber drops — a dozen of them sit inside one town, so zoomed
 *  out their marks, dotted OLT lines and rate chips pile onto the same few
 *  pixels as the plant they are subordinate to, and the map stops showing the
 *  gear it exists to show. Same argument as the layer being off by default,
 *  applied to distance rather than to preference: at z<15 the line to the OLT
 *  is a few pixels long and tells nobody anything. Deliberately NOT a cluster
 *  fold — these are not plant, and a badge counting subscribers with devices
 *  would be a count of two different things (`clusters.ts` skips them). */
const REF_ONU_MIN_ZOOM = 16

/** Which PON bucket a located subscriber falls in, for the focus filter.
 *
 *  ONE definition, used by the filter AND by the picker that drives it: a
 *  subscriber whose `pon_port` the walk never carried is still somebody's drop,
 *  and if the two sides spelled its bucket differently, ticking a PON would hide
 *  pins the operator was asking to see. */
const ponKey = (p: OnuPlace): string => p.pon_port ?? "—"

/** one PON of one OLT, as the focus picker and the status strip count it */
interface PonRow { pon: string; total: number; dark: number }

/** how much wider the dark casing runs than the link stroke it backs */
const CASING_OVER = 3
/** A fine dot is mostly overhang: at CASING_OVER the casing is 2.5x the dot's
    own width in every direction, so the black wins the pixel and the line reads
    GRAY rather than as its colour. Dotted kinds get a tighter casing — they
    still need one (a bare 1.5px dot vanishes over satellite), just not one that
    swallows the stroke it exists to protect. */
const CASING_OVER_FINE = 1.5

/** One drawable cable span the selected device is an end of. `childId`/`parentId`
    are the link_routes key and fix the waypoint direction (parent→child). */
type Cable = {
  childId: number; parentId: number; other: OrgDevice
  dir: "up" | "down" | "across"
  kind: "primary" | "backup" | "peer"
  route?: Array<[number, number]>
  color?: string | null
}

/** How far a folded endpoint may drift from its true position and still anchor a
    drawn cable route, in screen px at the current zoom. Sized to the pin radius:
    within this the line still meets the pin it belongs to. */
const ROUTE_FOLD_SLACK_PX = 10

/** Is a device's DISPLAY position close enough to its true one that a drawn
    route still lands honestly? See the callsite for why this is px, not equality. */
function nearTrue(disp: [number, number], d: { lat: number; lng: number }, zoom: number): boolean {
  const [ax, ay] = project(disp[0], disp[1], zoom)
  const [bx, by] = project(d.lat, d.lng, zoom)
  return Math.hypot(bx - ax, by - ay) <= ROUTE_FOLD_SLACK_PX
}

/** A casing can't reuse the stroke's own dashArray. SVG dashes are measured
    along the path but the cap is square to it, so the wider casing overhangs
    each dash by over/2 at BOTH ends — on a fine "1.5 7" dot a CASING_OVER of 3
    turns a 1.5px dash into 4.5px and closes the gap to 4, and the dots visibly
    touch. Grow each dash by the overhang and take it back out of the gap,
    keeping the period identical so casing and stroke stay in phase. */
function casingDash(dash: string | undefined, over: number): string | undefined {
  if (!dash) return undefined
  const [on, off] = dash.split(" ").map(Number)
  return `${on + over} ${Math.max(off - over, 1)}`
}

export function MapPage() {
  const { scopeOrg, canWrite } = useAuth()
  // Basemaps follow the app theme (Google's styled night roadmap / CARTO
  // dark_all). Reactive off the <html> class, since the theme has no provider.
  const dark = useDarkMode()
  const navigate = useNavigate()
  const navLocation = useNavLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const mapRef = useRef<L.Map | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<DeviceTab>("health")
  // An ONU to open when the Optical tab does, carried WITH the device it belongs
  // to: the panel is shared by every pin, and a focus left over from another OLT
  // would highlight an unrelated row. Addressed by MAC — the reference-ONU list
  // carries the ONU's SLOT number, not its optics row id, and the MAC is what
  // this whole feature is keyed on anyway.
  const [detailOnu, setDetailOnu] = useState<{ deviceId: number; mac: string } | null>(null)
  const [placingId, setPlacingId] = useState<number | null>(null)
  // Placing a REFERENCE ONU (map/refonu.ts) — a second placement target, armed
  // from the Optical tab's dialog via nav state. Kept separate from placingId
  // rather than made a union: they write different tables, and a stray click
  // must never save an ONU's coordinates onto a device row.
  const [placingOnu, setPlacingOnu] = useState<{ mac: string; label: string } | null>(null)
  const [selectedOnuMac, setSelectedOnuMac] = useState<string | null>(null)
  // A subscriber focus whose flight is still in the air. `zoom` state only
  // lands at zoomend, so for the length of a flyTo the visibility guard below
  // would judge the pin we are flying TO against the zoom we are flying FROM —
  // and close its card before it ever drew. Cleared by the first zoom report
  // after arrival, which is also the first moment the guard can judge fairly.
  const [focusFlying, setFocusFlying] = useState(false)
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
  // the cable under the cursor, with its distance to each end (null = none)
  const [hover, setHover] = useState<LinkHover | null>(null)
  const [coordsEdit, setCoordsEdit] = useState(false)
  const [coordsText, setCoordsText] = useState("")
  const [basemap, setBasemap] = useState<Basemap>(loadBasemap)
  const [layersOpen, setLayersOpen] = useState(false)
  // Reference ONUs are OFF by default and remembered per browser: they are
  // subordinate detail, and an operator who placed forty of them shouldn't have
  // the plant buried under subscriber marks on every visit.
  const [refOnus, setRefOnus] = useState(() => {
    try { return localStorage.getItem(REF_ONUS_KEY) === "on" } catch { return false }
  })
  const toggleRefOnus = () => {
    setRefOnus((v) => {
      try { localStorage.setItem(REF_ONUS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  // per-link ↓/↑ chips off the bound ports; on unless the operator switched
  // them off (Layers popover) — an org with no bindings simply shows none
  const [bwLabels, setBwLabels] = useState(() => {
    try { return localStorage.getItem(BW_LABELS_KEY) !== "off" } catch { return true }
  })
  const toggleBwLabels = () => {
    setBwLabels((v) => {
      try { localStorage.setItem(BW_LABELS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
  // Google's OWN place names and POI markers. On by default (a basemap with no
  // writing on it is disorienting until you ask for it) and remembered per
  // browser, like every other layer choice here. Roadmap only — a satellite
  // session carries no labels in the first place, so the menu doesn't offer a
  // switch that would do nothing.
  const [googleLabels, setGoogleLabels] = useState(() => {
    try { return localStorage.getItem(GOOGLE_LABELS_KEY) !== "off" } catch { return true }
  })
  const toggleGoogleLabels = () => {
    setGoogleLabels((v) => {
      try { localStorage.setItem(GOOGLE_LABELS_KEY, v ? "off" : "on") } catch { /* private mode */ }
      return !v
    })
  }
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
    toast.error(`Google basemap unavailable (${why}). Showing the fallback map`)
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
  // A link's styling rides the same rows as its geometry, but is looked up
  // separately: a coloured link commonly has NO drawn route (that's the whole
  // point — you colour the parallel chords you can't otherwise tell apart), so
  // it must not be filtered out by the waypoints test above.
  const styleByKey = useMemo(() => {
    const m = new Map<string, { color: string | null; label_pos: number | null }>()
    for (const r of routesQ.data?.routes ?? [])
      if (r.color != null || r.label_pos != null)
        m.set(`${r.child_id}:${r.parent_id}`, { color: r.color, label_pos: r.label_pos })
    return m
  }, [routesQ.data])

  // Reference ONUs: operator-vouched, reliably-powered subscribers. Fetched
  // whenever the layer is on OR a placement is armed (the arming navigation can
  // land before the toggle is flipped, and the banner needs the current set to
  // tell "move this point" from "add it").
  // A subscriber the caller asked us to go and show (survey "View on map", ONU
  // search). Held until the places load, then consumed by the effect below —
  // arriving before the data is the normal case, not the edge one.
  const [focusOnuMac, setFocusOnuMac] = useState<string | null>(null)
  // FOCUS: show the subscribers of ONE OLT, optionally of ONE PON — and nothing
  // else. The layer's only control used to be all-or-nothing, which does not
  // match how a fault arrives: "EPON0/4 has five crit ONUs" is a question about
  // one PON, and answering it by drawing every subscriber in the fleet and
  // hunting for the right diamonds is not answering it. Scoping is also what
  // makes the layer usable on a growing fleet at all — thousands of drops is a
  // texture, not a map.
  //
  // Deliberately SEPARATE from the `refOnus` toggle rather than a third value of
  // it: the toggle is the operator's standing preference (remembered), a scope
  // is what they are working on right now. Clearing the scope must put the map
  // back the way they keep it, not decide for them.
  //
  // `pons` is a SET, and an EMPTY one means every PON on that OLT — not "no
  // PONs", which would be a focus that draws nothing. Multi-select was the
  // operator's explicit call over the original one-PON-at-a-time chips: a
  // splitter cascade and a feeder often carry two or three PONs of one village,
  // and answering "is this the whole area or just that PON" meant clicking
  // between them from memory. The map is the one screen where holding two PONs
  // side by side is the question.
  const [onuScope, setOnuScope] = useState<{ deviceId: number; pons: string[] } | null>(null)
  const placesQ = useQuery({
    queryKey: ["onu-places", scopeOrg],
    queryFn: () => inventoryApi.onuPlaces(scopeOrg),
    // …and whenever a device panel is OPEN, which is not a display need but a
    // DISCOVERY one: the panel's "N located · Show on map" row is the way into
    // the focus, and it is drawn from this list. Gating it on the layer already
    // being on made the entry point appear only for operators who had found the
    // layer some other way — an affordance you must already know about is not
    // one. One cached request per map session (staleTime 60s).
    enabled: !!scopeOrg && (refOnus || onuScope != null || placingOnu != null
      || focusOnuMac != null || selectedId != null),
    staleTime: 60_000,
  })
  const places = placesQ.data?.places ?? []

  const setOnuPlace = useMutation({
    // org_id is REQUIRED, not optional: a superadmin's own org_id is NULL, so
    // the scope it is viewing is the only thing that says which org owns the
    // point. Same reason setTagColor takes one.
    mutationFn: ({ mac, lat, lng, label }: {
      mac: string; lat: number | null; lng: number | null; label?: string | null
    }) => inventoryApi.setOnuPlace({ mac, lat, lng, label, org_id: scopeOrg }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      // a reference point is an input to the PON-fault verdict, not decoration
      queryClient.invalidateQueries({ queryKey: ["pon-faults"] })
      queryClient.invalidateQueries({ queryKey: ["pon-summary"] })
      queryClient.invalidateQueries({ queryKey: ["optics"] })
    },
    onError: (e) => toast.error(
      `Couldn't save the reference point${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // which port carries each link (device panel → Uplinks) + its live rates —
  // rates move on the SNMP walk cadence, which SSE doesn't cover
  const linkPortsQ = useQuery({
    queryKey: ["link-ports", scopeOrg],
    queryFn: () => inventoryApi.linkPorts(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const linkBindings = useMemo(
    () => bindLinkPorts(linkPortsQ.data?.ports ?? []), [linkPortsQ.data])

  // PON mass-drop verdicts (fiber cut / power pattern) for the cut overlay
  const faultsQ = useQuery({
    queryKey: ["pon-faults-org", scopeOrg],
    queryFn: () => inventoryApi.orgPonFaults(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  // Subscriber drops: what hangs off each splitter, and which span went dark.
  // Always fetched (not gated on the reference-ONU layer) — this is PLANT, not
  // a subscriber overlay: it decides how loud a splitter pin is and where the
  // branch-fault overlay paints, both of which are map furniture rather than an
  // opt-in extra. The reply is one row per passive, not per subscriber, so it
  // stays small on a fleet with thousands of ONUs.
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
  const selectedRef = useMemo(
    () => (selectedOnuMac == null ? null
      : places.find((p) => p.mac === selectedOnuMac) ?? null),
    [places, selectedOnuMac])

  // The layer is on AND we're close enough for it to mean anything. Two
  // exceptions, both cases where the operator has named what they want to see:
  // while a reference point is being placed the existing ones are what they are
  // aiming between, and a SCOPED set is bounded and was asked for by name — so
  // it ignores the zoom floor, which exists to stop a fleet's worth of pins, not
  // to hide one OLT's dozen.
  const refVisible = (refOnus && zoom >= REF_ONU_MIN_ZOOM)
    || onuScope != null || placingOnu != null
  // What the layer actually draws. A scope NARROWS; it never adds.
  const shownPlaces = useMemo(() => {
    if (!onuScope) return places
    const { deviceId, pons } = onuScope
    return places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
  }, [places, onuScope])
  // Located subscribers per OLT — what makes the panel able to say "12 located"
  // instead of offering a focus that would draw nothing.
  const placedByOlt = useMemo(() => {
    const m = new Map<number, number>()
    for (const p of places)
      if (p.device_id != null) m.set(p.device_id, (m.get(p.device_id) ?? 0) + 1)
    return m
  }, [places])
  // The PON rows, per OLT. Built from the PLACED subscribers, not from the
  // OLT's PON list: a row for a PON nobody has surveyed would filter to an empty
  // map and read as "this PON is dark". The dark count is what makes them worth
  // having during a cut — it is the PON to open first.
  //
  // Computed for EVERY OLT, not just the scoped one, because the device panel's
  // picker has to offer the choice BEFORE a focus exists.
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
  // Zooming out past the threshold takes the pin off the map, so the card it
  // opened has to go with it — a detail card floating over nothing is the
  // "shows a thing that isn't there" failure this layer is careful about. A
  // scope that filters the selected pin away is the same failure by a different
  // route, so it is the DRAWN set that decides, not visibility alone.
  useEffect(() => {
    if (selectedOnuMac == null) return
    // A scope (or a vanished row) closes the card at once — that pin is not
    // being drawn and never will be. The ZOOM half waits out an in-flight
    // focus: the operator named this subscriber, and the map is on its way.
    const drawn = shownPlaces.some((p) => p.mac === selectedOnuMac)
    if (!drawn || (!refVisible && !focusFlying)) setSelectedOnuMac(null)
  }, [refVisible, shownPlaces, selectedOnuMac, focusFlying])

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

  // Per-link cartography: colour, and where the bandwidth chip rides the line.
  // Sparse by design — each call names only what it changes, so moving a label
  // can't clear a colour.
  const setLinkStyle = useMutation({
    mutationFn: ({ childId, parentId, style }: {
      childId: number; parentId: number
      style: { color?: string | null; label_pos?: number | null }
    }) => inventoryApi.setLinkStyle(childId, parentId, style),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["routes"] }),
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // Search: a placed device flies to its pin; an unplaced one goes straight into
  // placement mode (search Gachibowli → pick the device → click the map).
  const searchDevice = (d: OrgDevice) => {
    const map = mapRef.current
    if (isPlaced(d)) {
      map?.flyTo([d.lat, d.lng], Math.max(map.getZoom(), 15))
      setDetailTab(deviceTabs(d)[0])
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
  // Search picked a SUBSCRIBER (sticker MAC or the customer name a tech typed).
  // A located one flies to its pin, with the layer switched on — arriving at a
  // map that isn't drawing the thing you asked for reads as a broken search.
  //
  // An unplaced one says so and stops. It deliberately does NOT arm placement
  // the way an unplaced DEVICE does: dropping a device pin records a
  // coordinate, but dropping one HERE writes a REFERENCE ONU — the operator's
  // claim that this subscriber's power is reliable, which flips a dark PON from
  // "fibre cut, roll a crew" to "area power cut". That claim is made from the
  // Optical tab's dialog or the field survey, where the contract is stated,
  // never as the side effect of typing a MAC into a search box.
  const searchOnu = (hit: OnuHit) => {
    if (!hit.place) {
      toast.info(`${hit.who} has no location recorded yet`,
                 { description: `In the roster on ${hit.where} — record where it stands from Survey.` })
      return
    }
    if (!refOnus) toggleRefOnus()
    flyToOnu(hit.place)
  }

  // Initial view: last saved per org, else fit every placed pin once they load,
  // else a wide world view a first-time org can zoom from.
  const initialView = useMemo(() => loadView(scopeOrg), [scopeOrg])

  // Arrive from the Optical tab's "Pick on map" with a reference ONU armed.
  // Consumed once and cleared off the history entry, or a back-navigation (or a
  // reload) would silently re-arm placement and the next map click would move a
  // point the operator only meant to look at.
  useEffect(() => {
    const armed = (navLocation.state as { placeOnu?: { mac: string; label: string } } | null)
      ?.placeOnu
    if (!armed?.mac) return
    setPlacingOnu({ mac: armed.mac, label: armed.label ?? "" })
    setSelectedId(null)
    setPlaceOpen(false)
    navigate(navLocation.pathname, { replace: true, state: null })
  }, [navLocation.state, navLocation.pathname, navigate])

  // "Take me to this subscriber" — `?onu=<mac>`, a QUERY param rather than nav
  // state because the whole point is that it survives being shared, bookmarked
  // and reloaded (nav state does not). Placing a pin and then not being able to
  // find it is what this exists to fix: the subscriber layer is off by default
  // and only draws from street zoom, so without a way in, a fresh placement is
  // invisible on both counts.
  const onuParam = searchParams.get("onu")
  useEffect(() => {
    if (!onuParam) return
    setFocusOnuMac(onuParam.trim().toUpperCase())
    // Turning the layer ON is part of the ask: arriving at a map that is not
    // drawing the thing you asked to see reads as a broken link.
    if (!refOnus) toggleRefOnus()
    setSearchParams((prev) => {
      // A FRESH instance — mutating the one useSearchParams returns edits
      // react-router's memoized object, so the value a render reads moves
      // without the reference the memo is keyed on changing.
      const next = new URLSearchParams(prev)
      next.delete("onu")
      return next
    }, { replace: true })
    // refOnus is read fresh each render; re-running on it would re-arm the focus
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onuParam, setSearchParams])

  // Focus the layer on one OLT (and optionally a chosen set of its PONs), then
  // FRAME what that leaves on screen. The fit is the half that makes this feel
  // like a focus rather than a filter: scoping to a PON whose subscribers are
  // three villages away, and leaving the viewport where it was, shows an empty
  // map and reads as "nothing here". `maxZoom` keeps a single-subscriber PON
  // from slamming to street level, where there is no context to place it in.
  //
  // An EMPTY `pons` is every PON, never none — un-ticking the last one has to
  // land on "the whole OLT", not on a focus that draws nothing and reads as a
  // dark fleet.
  const scopeOnus = useCallback((deviceId: number, pons: string[]) => {
    setOnuScope({ deviceId, pons })
    setSelectedOnuMac(null)
    const pts = places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
    const olt = byId.get(deviceId)
    const all: Array<[number, number]> = pts.map((p) => [p.lat, p.lng])
    // Include the OLT itself so the picture is "these drops, off that box" —
    // the association line to it is most of what the layer is saying.
    if (olt && isPlaced(olt)) all.push([olt.lat, olt.lng])
    if (all.length > 0 && mapRef.current)
      mapRef.current.flyToBounds(L.latLngBounds(all), { ...FIT_PADDING, maxZoom: 17 })
  }, [places, byId])

  // Tick one PON on or off. Re-fits every time, so the map keeps answering
  // "what does this selection look like" while the menu is still open — that
  // live re-frame is most of why comparing two PONs is worth having.
  const toggleScopePon = useCallback((deviceId: number, pon: string) => {
    const cur = onuScope?.deviceId === deviceId ? onuScope.pons : []
    scopeOnus(deviceId, cur.includes(pon) ? cur.filter((x) => x !== pon) : [...cur, pon])
  }, [onuScope, scopeOnus])

  // Go to ONE subscriber's pin and open its card. Shared by the `?onu=` deep
  // link and the search box, so a subscriber found two different ways lands the
  // same way — a lookup that leaves you somewhere else than the link does is
  // how an operator stops trusting either.
  const flyToOnu = useCallback((p: OnuPlace) => {
    // A focus on some OTHER OLT would filter away the very subscriber that was
    // asked for — the same "arrived at a map not drawing the thing you asked to
    // see" failure the layer toggle avoids.
    setOnuScope(null)
    // Past REF_ONU_MIN_ZOOM deliberately — the layer's zoom floor would
    // otherwise leave us hovering over a pin that refuses to draw.
    const map = mapRef.current
    map?.flyTo([p.lat, p.lng], Math.max(map.getZoom(), REF_ONU_MIN_ZOOM + 1))
    setFocusFlying(true)
    setSelectedId(null)
    setSelectedOnuMac(p.mac)
  }, [])

  // Consume the focus once the places are in hand.
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
    if (placingId == null && routeEdit == null && placingOnu == null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPlacingId(null); setRouteEdit(null); setPlacingOnu(null); return
      }
      // Ctrl/⌘-Z pops the last waypoint. Nothing focusable is on screen while a
      // route is being drawn (the device panel is hidden), but guard anyway so a
      // field that does get focus keeps its native undo.
      if (routeEdit != null && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        const el = document.activeElement
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return
        e.preventDefault()
        setRouteEdit((re) => re && re.points.length ? { ...re, points: re.points.slice(0, -1) } : re)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [placingId, placingOnu, routeEdit])

  const onMapClick = useCallback((ll: L.LatLng) => {
    if (routeEdit != null) {
      setRouteEdit((re) => re && { ...re, points: [...re.points, [ll.lat, ll.lng]] })
    } else if (placingOnu != null) {
      setOnuPlace.mutate({ mac: placingOnu.mac, lat: ll.lat, lng: ll.lng,
                           label: placingOnu.label || null })
      // switch the layer on, or the point the operator just placed isn't drawn
      if (!refOnus) toggleRefOnus()
      setSelectedOnuMac(placingOnu.mac)
      setPlacingOnu(null)
    } else if (placingId != null) {
      setLocation.mutate({ id: placingId, lat: ll.lat, lng: ll.lng })
      setSelectedId(placingId)
      setPlacingId(null)
    } else {
      setSelectedId(null)
      setSelectedOnuMac(null)
      setSiteAnchor(null)
    }
    // toggleRefOnus/setOnuPlace are stable enough for this handler's purpose;
    // refOnus is read fresh each render so the deps below cover the branch
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placingId, placingOnu, refOnus, routeEdit, setLocation])

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

  // "Take me to the problem": each click flies to the next trouble pin, worst first.
  const cycleTrouble = () => {
    if (troubles.length === 0) return
    const d = troubles[troubleIdx.current % troubles.length]
    troubleIdx.current += 1
    mapRef.current?.flyTo([d.lat, d.lng], Math.max(mapRef.current.getZoom(), 14))
    setDetailTab(deviceTabs(d)[0])
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

  const onZoom = useCallback((z: number) => {
    setZoom(z); setLowZoom(z < 12); setFocusFlying(false)
  }, [])

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
      toast.success(`Placed at ${t.name} (same site)`)
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
    if (!m) { toast.error('Use "lat, lng", e.g. 17.4401, 78.3489'); return }
    setLocation.mutate({ id: selected.id, lat: Number(m[1]), lng: Number(m[2]) })
    setCoordsEdit(false)
  }

  // Only links where both ends are pinned; a line inherits the child's trouble
  // so a red pin drags a red path back toward its feed.
  const links = useMemo(() => {
    const out: Array<{
      key: string; from: Placed; to: Placed; tone: string
      kind: "primary" | "backup" | "peer"
      // the link_routes key — what a style/geometry write for this line addresses
      childId: number; parentId: number
      route?: Array<[number, number]>
      color?: string | null
      labelPos?: number | null
      binding?: LinkBinding
    }> = []
    const placedById = new Map(placed.map((d) => [d.id, d]))
    const styled = (childId: number, parentId: number) => {
      const s = styleByKey.get(`${childId}:${parentId}`)
      return { childId, parentId, color: s?.color, labelPos: s?.label_pos }
    }
    for (const d of placed) {
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
      // Cross-links are undirected and BOTH ends list each other, so draw only
      // from the lower id — otherwise every cable renders twice, and the second
      // line would carry the far end's tone.
      for (const pid of d.peer_ids ?? []) {
        if (pid < d.id) continue
        const p = placedById.get(pid)
        // a cross-link carries no dependency, so it can't inherit "the child's
        // trouble" like a feed does — it's neutral unless an END is in trouble
        // lower toneRank = worse; a cross-link shows the worse of its two ends
        // geometry and styling are keyed (child=higher, parent=lower) so the
        // waypoints run parent→child like every other kind; see the store's
        // list_link_routes for why that's the opposite of org_device_links
        if (p) out.push({ key: `x${d.id}-${pid}`, from: d, to: p, kind: "peer",
          tone: toneRank(p) < toneRank(d) ? pinTone(p) : tone,
          ...styled(pid, d.id),
          route: routeByKey.get(`${pid}:${d.id}`) ?? routeByKey.get(`${d.id}:${pid}`),
          binding: linkBindings.get(linkKey(d.id, pid)) })
      }
    }
    return out
  }, [placed, routeByKey, styleByKey, linkBindings])

  // The geometry each line is actually DRAWN along, resolved once so the render,
  // the hover probe and the label all measure the same path. A drawn route is
  // dropped when a cluster fold would make it snake into a centroid — but the
  // test is whether the fold VISIBLY MOVED the endpoint, not whether the
  // position is bit-identical. Gear racked at one site sits ~1 m apart (three
  // switches at HALIYA are within 1.5 m), so it clusters at every usable zoom
  // while its centroid stays inside its own pin: an exact-equality test
  // suppressed those routes forever and the map drew a chord no zoom could fix.
  // Screen space is the right unit — displacement you can't see can't read as an
  // error.
  const drawnLinks = useMemo(() => links.map((l) => {
    const from = pinPos.get(l.from.id) ?? [l.from.lat, l.from.lng] as [number, number]
    const to = pinPos.get(l.to.id) ?? [l.to.lat, l.to.lng] as [number, number]
    const atTrue = nearTrue(from, l.from, zoom) && nearTrue(to, l.to, zoom)
    const drawn = !!(l.route && atTrue)
    const pts: Array<[number, number]> = drawn ? [from, ...l.route!, to] : [from, to]
    return { ...l, from3: from, to3: to, pts, drawn }
  }), [links, pinPos, zoom])

  // Measuring is a READ, so it stays available to everyone — but not while the
  // map is being used as an input surface: during placement or route drawing the
  // cursor means "put a thing here", and a readout chasing it is noise.
  const hoverEnabled = placingId == null && routeEdit == null && !editPins
  const hoverable = useMemo(
    () => (hoverEnabled ? projectLinks(drawnLinks, zoom) : []),
    [drawnLinks, zoom, hoverEnabled])
  useEffect(() => { if (!hoverEnabled) setHover(null) }, [hoverEnabled])

  if (!scopeOrg) return <NeedsOrg />

  const down = troubles.filter((d) => pinTone(d) === "destructive").length
  const degraded = troubles.length - down

  const lineColor = (tone: string) =>
    tone === "destructive" ? "var(--destructive)"
      : tone === "warning" ? "var(--warning)" : "var(--map-link)"

  const parent = selected?.parent_device_id != null ? byId.get(selected.parent_device_id) : null
  const linkKm = selected && isPlaced(selected) && parent && isPlaced(parent)
    ? distanceKm(selected.lat, selected.lng, parent.lat, parent.lng) : null
  const selRoute = selected && parent ? routeByKey.get(`${selected.id}:${parent.id}`) : undefined
  const routeKm = selRoute && selected && isPlaced(selected) && parent && isPlaced(parent)
    ? polyKm([[parent.lat, parent.lng], ...selRoute, [selected.lat, selected.lng]]) : null

  // Every cable this device is an END of, not just the one going up. A device
  // owns the spans down to its children as much as its uplink, but the panel
  // used to expose the parent link alone — so a downstream route could only be
  // drawn by knowing to open the CHILD instead, and nothing anywhere said
  // whether a link already had a path drawn on it.
  const cables: Cable[] = []
  if (selected && isPlaced(selected)) {
    const add = (childId: number, parentId: number, other: OrgDevice | undefined,
      dir: Cable["dir"], kind: Cable["kind"]) => {
      // both ends must be pinned — a route needs two anchors to rubber-band to
      if (!other || !isPlaced(other)) return
      cables.push({ childId, parentId, other, dir, kind,
        route: routeByKey.get(`${childId}:${parentId}`),
        color: styleByKey.get(`${childId}:${parentId}`)?.color })
    }
    if (selected.parent_device_id != null)
      add(selected.id, selected.parent_device_id, byId.get(selected.parent_device_id), "up", "primary")
    for (const bp of selected.backup_parents ?? [])
      add(selected.id, bp, byId.get(bp), "up", "backup")
    for (const d of devices) {
      if (d.id === selected.id) continue
      if (d.parent_device_id === selected.id) add(d.id, selected.id, d, "down", "primary")
      else if (d.backup_parents?.includes(selected.id)) add(d.id, selected.id, d, "down", "backup")
    }
    // A cross-link has no child end, but its ROUTE does. The renderer draws a
    // peer from the LOWER id, so waypoints have to run lower→higher or the
    // saved path renders back-to-front; pin that order here, at the one place
    // peer routes are written.
    for (const pid of selected.peer_ids ?? [])
      add(Math.max(selected.id, pid), Math.min(selected.id, pid), byId.get(pid), "across", "peer")
  }
  const drawnCables = cables.filter((c) => c.route).length

  const startRouteEdit = (c: Cable) => {
    setPlacingId(null)
    setPlaceOpen(false)
    setRouteEdit({ childId: c.childId, parentId: c.parentId, points: c.route ?? [] })
  }
  const editingChild = routeEdit ? byId.get(routeEdit.childId) : null
  const editingParent = routeEdit ? byId.get(routeEdit.parentId) : null
  // `open` must match when the Card actually RENDERS (route editing replaces it),
  // or the control column would slide aside for a panel that isn't there. Its own
  // stored width and a tighter ceiling than the Network page's: this panel floats
  // over the map it's read against, and past ~halfway it stops being a panel on a
  // map and becomes a map behind a panel.
  const panel = useResizablePanel({
    storageKey: "wisp:map:panelw", defaultWidth: 380, min: 320, max: 620,
    open: !!selected && !routeEdit,
  })

  return (
    // header is h-14 (3.5rem); the mobile tab bar overlays the bottom ~4rem
    <div ref={wrapRef} style={panel.vars} className={cn(
      "wisp-map-wrap relative h-[calc(100svh-3.5rem-4rem)] md:h-[calc(100svh-3.5rem)]",
      (placingId != null || placingOnu != null) && "wisp-map-placing",
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
        <AttributionPrefix />
        {googleActive ? (
          <GoogleLayer
            // dark and labels are in the key as well as the props: each styled
            // roadmap is a different SESSION, so flipping one has to remount the
            // layer, not just re-render it
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
        <MapEvents org={scopeOrg} onMapClick={onMapClick} onZoom={onZoom} />
        <ViewController placed={placed} ready={!isLoading && orgsQ.isSuccess}
          hasSavedView={!!initialView} bounds={region.bounds} />
        <LinkHoverProbe projected={hoverable} enabled={hoverEnabled}
          zoom={zoom} onHover={setHover} />
        {/* Where the cursor meets a cable: distance to each end, measured along
            the geometry actually drawn. Non-interactive — it reports on the
            pointer, it must never become something the pointer can hit. */}
        {hover && (
          <Marker position={hover.at} icon={hoverIcon(hover)}
            interactive={false} zIndexOffset={1100} />
        )}
        {drawnLinks.map((l) => {
          // the link being redrawn renders as the edit preview instead
          if (routeEdit && l.to.id === routeEdit.childId && l.from.id === routeEdit.parentId)
            return null
          // a selected device lights up its whole downstream path
          const emphasized = selectedId != null
            && (l.to.id === selectedId || downstream.has(l.to.id))
          const dimmed = troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized
          const { from3: from, to3: to, pts } = l
          // ↓/↑ chip riding the line: only when a port is bound to this link and
          // the ends are far enough apart on screen that the chip has a line to
          // sit on — zoomed out, the pins (and clusters) own the pixels
          const labeled = bwLabels && l.binding && !dimmed && (() => {
            const [ax, ay] = project(from[0], from[1], zoom)
            const [bx, by] = project(to[0], to[1], zoom)
            return Math.hypot(bx - ax, by - ay) >= 90
          })()
          // a cross-link carries no dependency, so it stays visually quieter
          // than any feed — thinner, and it never thickens on emphasis (a
          // selected switch lights its PATH, not its siblings)
          const weight = l.kind === "peer" ? 2 : emphasized ? 3.5 : l.tone === "destructive" ? 3 : 2.5
          // backup = long dash (a standby path), peer = fine dot (cabling).
          // Periods are sized to survive CASING_OVER: a dash pattern has to be
          // longer than the casing's overhang or the cased dots touch.
          const dashArray = l.kind === "backup" ? "5 8" : l.kind === "peer" ? "1.5 7" : undefined
          const casingOver = l.kind === "peer" ? CASING_OVER_FINE : CASING_OVER
          return (
            <Fragment key={l.key}>
              {/* Casing: a dark stroke under the line so it survives the basemap
                  it happens to cross — satellite is bright over fields and near
                  black over water within one viewport, and a single flat colour
                  can't read on both. Standard road-casing treatment. */}
              {!dimmed && (
                <Polyline
                  interactive={false}
                  positions={pts}
                  pathOptions={{
                    color: "#000", weight: weight + casingOver, opacity: 0.32,
                    dashArray: casingDash(dashArray, casingOver),
                  }}
                />
              )}
              <Polyline
                // never a click target — a line crossing the viewport would otherwise
                // swallow map clicks during placement
                interactive={false}
                positions={pts}
                pathOptions={{
                  // Emphasis (a selected path) still overrides a custom colour —
                  // "which line did I just click" has to beat "which cable is
                  // this", and paintedLineColor already refuses to paint a line
                  // that's in trouble.
                  color: emphasized && l.tone === "muted" ? "var(--primary)"
                    : paintedLineColor(l.tone, l.color, lineColor(l.tone)),
                  weight,
                  opacity: dimmed ? 0.12 : l.kind === "peer" ? 0.85
                    : emphasized ? 1 : l.tone === "muted" ? 0.85 : 0.9,
                  dashArray,
                }}
              />
              {labeled && (
                <Marker
                  position={linkLabelPos(pts, l.labelPos)}
                  icon={linkBwIcon(l.binding!, l.from, l.to, l.color)}
                  zIndexOffset={600}
                  // Slid along the line in the same mode that moves pins — one
                  // "rearrange the map" mode, not a second one to discover.
                  // Two cables running the same chord stack their chips; sliding
                  // one clear is what makes the pair readable, and the colour
                  // carries which is which once they're apart.
                  draggable={editPins && canWrite}
                  eventHandlers={{
                    // CONSTRAINED drag: the chip is a label ON a line, so it
                    // snaps back to the nearest point of the rendered geometry
                    // every frame. Free-floating would let it drift off the
                    // cable it names, which is the bug being fixed, not a
                    // feature.
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
                      // in pin-edit mode the chip is a handle, not a link
                      if (placingId != null || routeEdit != null || editPins) return
                      // the chip opens the Ports tab of whichever box owns the port
                      setDetailTab("ports")
                      setSelectedId([...l.binding!.keys()][0])
                    },
                  }}
                />
              )}
            </Fragment>
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
          // Passive plant carries its own second channel: the split ratio and
          // what its recorded subscribers are doing. Gear is unchanged — a
          // switch has a state of its own and doesn't need borrowing one.
          const passive = isPassiveType(d.device_type)
          const load = passive ? loadByPassive.get(d.id) : undefined
          return (
            <Marker
              key={d.id}
              position={[d.lat, d.lng]}
              icon={pinIcon(d, {
                selected: d.id === selectedId, dim, impact,
                sub: passive ? passiveSubLabel(d, load) : null,
                dropTone: passive ? dropTone(load) : undefined,
                title: passive ? passiveTitle(d, load) : undefined,
              })}
              draggable={editPins && canWrite}
              eventHandlers={{
                click: () => {
                  if (routeEdit != null) return
                  // Placing a reference ONU onto a device pin means "at that
                  // site" — the common real case, since the subscriber whose
                  // power is reliable is often the tower the gear is on.
                  if (placingOnu != null) {
                    setOnuPlace.mutate({ mac: placingOnu.mac, lat: d.lat, lng: d.lng,
                                         label: placingOnu.label || null })
                    if (!refOnus) toggleRefOnus()
                    toast.success(`Reference point at ${d.name}`)
                    setSelectedOnuMac(placingOnu.mac)
                    setPlacingOnu(null)
                    return
                  }
                  // placement mode: a tap on an existing pin means "same spot"
                  // — start the rack deliberately instead of eyeballing it
                  if (placingId != null) {
                    if (placingId !== d.id) {
                      setLocation.mutate({ id: placingId, lat: d.lat, lng: d.lng })
                      toast.success(`Placed at ${d.name} (same site)`)
                      setSelectedId(placingId)
                    }
                    setPlacingId(null)
                    return
                  }
                  setDetailTab(deviceTabs(d)[0])
                  setSelectedId(d.id === selectedId ? null : d.id)
                },
                dragend: (e) => {
                  const ll = (e.target as L.Marker).getLatLng()
                  // dropping within a badge radius of a neighbor joins its site
                  const near = nearestOther(d.id, ll.lat, ll.lng)
                  if (near) toast.success(`Snapped to ${near.name} (same site)`)
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
        {/* Reference ONUs — a subordinate layer, off by default. Rendered AFTER
            the device pins so a same-coordinate subscriber can't sit on top of
            the gear, and deliberately outside the clustering pass: they are not
            plant, and folding them into a site badge would make a count that
            mixes infrastructure with subscribers. */}
        {refVisible && shownPlaces.map((p) => {
          // The line from a subscriber to the plant it hangs off.
          //
          // It ends at the SPLITTER whose drop feeds it — an ISP connects a
          // customer to the nearest splitter, and there may be a second one
          // between that and the OLT, so a line drawn straight to the OLT skips
          // the whole distribution network a crew works on. The splitter's own
          // chain upward is already on the map as passive plant with drawn
          // routes, so this hop is the only one missing.
          //
          // When no drop has been recorded it still falls back to the OLT, and
          // renders WEAKER for it: a reference point must not vanish because
          // its plant is undocumented, but "routed through its splitter" and
          // "we only know the PON" may not look alike.
          const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
          if (!anchor) return null
          const to = anchor.device as Placed
          const viaSplitter = anchor.kind === "splitter"
          const pts: Array<[number, number]> = [[to.lat, to.lng], [p.lat, p.lng]]
          const tone = refLineTone(p)
          // Sized to the TOPOLOGY lines (a feed is 2.5, a peer 2, a selected
          // path 3.5) rather than to a rank below them. Twice now this layer has
          // been drawn subordinate and come back unreadable — the first cut was
          // weight 1 at 0.3 opacity, the second 1.5 at 0.5 — and a line nobody
          // can see on satellite imagery ranks below everything by default. The
          // RANKING that carries meaning is the DASH, not the weight: these stay
          // dotted, so they still read as "logical association, not traced
          // fibre" at any width. Ordering within the layer survives too — a dark
          // span is heaviest, a recorded drop next, the OLT guess lightest.
          const refWeight = tone === "dark" ? 4.5 : viaSplitter ? 3.5 : 2.5
          const refDash = viaSplitter ? DROP_DASH : REF_DASH
          return (
            <Fragment key={`refline:${p.mac}`}>
              {/* Casing, the same treatment every topology line gets: satellite
                  imagery runs from near-white over fields to near-black over
                  water inside one viewport, and no single stroke colour reads on
                  both. This layer went without one while it was hairline-thin,
                  which is most of why it disappeared over bright ground. FINE
                  overhang — a round dot is mostly overhang already, and the wide
                  casing would swallow the stroke it exists to protect. */}
              <Polyline
                positions={pts}
                interactive={false}
                pathOptions={{
                  color: "#000", weight: refWeight + CASING_OVER_FINE, opacity: 0.3,
                  dashArray: casingDash(refDash, CASING_OVER_FINE),
                  lineCap: "round",
                }}
              />
              <Polyline
                positions={pts}
                // MUST stay non-interactive: an interactive polyline swallows
                // placement clicks (the map-wide rule for topology lines).
                interactive={false}
                className={`wisp-refline wisp-refline--${tone}`}
                pathOptions={{
                  color: tone === "dark" ? "var(--destructive)" : "var(--map-link)",
                  weight: refWeight,
                  opacity: tone === "dark" ? 0.95 : viaSplitter ? 0.9 : 0.75,
                  // a real drop gets the tighter dash; the OLT fallback keeps the
                  // sparser one so the two spans stay tellable apart
                  dashArray: refDash,
                  // Round caps so the dashes render as DOTS rather than stubby
                  // bars — the line has to keep reading as "logical
                  // association", never as traced fibre a crew could quote drum
                  // off. Both dash periods were opened up alongside the weight
                  // (see REF_DASH / DROP_DASH); a wider stroke on the old gaps
                  // would have closed them into a solid line.
                  lineCap: "round",
                }}
              />
              {/* The rate chip rides the midpoint; no saved position, because
                  this line has no operator-drawn geometry to slide along.
                  Suppressed when the span is only a few pixels: routing the
                  line through the SPLITTER made it a real drop rather than a
                  cross-town line to the OLT, so at most zooms the chip would
                  land on top of the pin it belongs to and hide the state that
                  matters more than the rate. Screen space is the right unit —
                  the same test the drawn-route/chord fallback uses. */}
              {(() => {
                const a = project(to.lat, to.lng, zoom)
                const b = project(p.lat, p.lng, zoom)
                return Math.hypot(a[0] - b[0], a[1] - b[1]) >= 56
              })() && (
                <Marker
                  position={[(to.lat + p.lat) / 2, (to.lng + p.lng) / 2]}
                  icon={refBwIcon(p)}
                  interactive={false}
                  zIndexOffset={-150}
                />
              )}
            </Fragment>
          )
        })}
        {/* Branch faults: every recorded subscriber below one passive is dark
            while a sibling branch stays lit, so the break is in the ONE span
            feeding it. Painted over the cable itself — drawn geometry where the
            operator traced it, the chord where they didn't — because that span
            is exactly what a crew drives to. Read-side only: this overlay is
            derived from plant records and pages nobody. */}
        {branchFaults.map((f) => {
          const link = drawnLinks.find(
            (l) => l.kind === "primary" && l.to.id === f.passive_id
              && l.from.id === f.parent_id)
          if (!link) return null
          const box = byId.get(f.passive_id)
          const parentBox = f.parent_id != null ? byId.get(f.parent_id) : undefined
          if (!box || !parentBox) return null
          // halfway ALONG the cable, not the chord's midpoint — on a traced
          // route those are different places, and the ✂ should sit on the span
          const mid = pointAlong(link.pts, (polyKm(link.pts) * 1000) / 2)
          return (
            <Fragment key={`branch:${f.passive_id}`}>
              <Polyline
                positions={link.pts}
                interactive={false}
                className={`wisp-branchspan wisp-branchspan--${f.cause}`}
                pathOptions={{
                  color: f.cause === "power" ? "var(--warning)" : "var(--destructive)",
                  weight: 7, opacity: 0.3, lineCap: "round",
                  // a power branch is a hypothesis about the grid, not a cut —
                  // dashed so it never reads as "send the splicing crew here"
                  dashArray: f.cause === "power" ? "10 8" : undefined,
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
        {refVisible && shownPlaces.map((p) => (
          <Marker
            key={`ref:${p.mac}`}
            position={[p.lat, p.lng]}
            icon={refOnuIcon(p, {
              selected: p.mac === selectedOnuMac,
              dim: troubleOnly && !isRefDark(p),
            })}
            zIndexOffset={refZIndex(p, p.mac === selectedOnuMac)}
            eventHandlers={{
              click: () => {
                if (routeEdit != null || placingId != null || placingOnu != null) return
                setSelectedOnuMac(p.mac === selectedOnuMac ? null : p.mac)
                setSelectedId(null)
              },
            }}
          />
        ))}
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
      {/* z-[1002], one rung above every floating card on this map (all z-1000):
          the unplaced drawer, the site card and the subscriber card all open at
          `top-14 left-3`, which is exactly where this strip WRAPS to once the
          focus bar joins it — and a z-index tie is broken by DOM order, so those
          cards were burying the search results and the PON chips. A transient
          list covering a status bar is the wrong way round. */}
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
        {/* subscriber focus bar ---------------------------------------------------
            What is being shown, and the one control that narrows it further.

            It lives IN the status strip, beside "Trouble only" and the on-map
            count, rather than in the Layers popover: a map that is deliberately
            hiding most of its content has to say so ON the map, or the next
            person to walk up to the wall reads a scoped view as the whole
            network. It cannot be a floating card either — `top-14 left-3` is
            already taken by the unplaced drawer and the subscriber card, and
            clicking a scoped pin would bury the bar under the card it opened.

            Chips MULTI-select (operator's call, 2026-07-29 — they were a single
            choice first). Two PONs of one village share a feeder and a cascade,
            so "is the whole area out or just that PON" is a question about a
            SET, and answering it by clicking between chips from memory is what
            the map is supposed to spare you. "All PONs" is the empty set, not a
            member: it CLEARS the ticks rather than being one more of them. */}
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
                  {/* the count that matters during a cut, and only when it is not
                    zero — a permanent "0 dark" is noise on a healthy night */}
                {dark > 0 && <span className="font-semibold text-destructive"> · {dark} dark</span>}
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
                        {/* dark count rides the chip, so the PON to open first is
                          readable without clicking through every one of them */}
                      <span className={cn("ml-1",
                        p.dark > 0 ? "font-semibold text-destructive" : "text-faint-foreground")}>
                        {p.dark > 0 ? p.dark : p.total}
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

      {/* reference-ONU placement banner ----------------------------------------
          Restates the contract at the moment of the click. The dialog said it
          too, but a "Pick on map" hop can be minutes and a screen apart from
          reading it, and a reference point placed casually is what turns an
          area power cut back into a crew roll. */}
      {placingOnu && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(92vw,34rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Crosshair className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            Click where{" "}
            <span className="font-mono font-semibold">{placingOnu.label || placingOnu.mac}</span>
            {" "}stands
            <span className="text-muted-foreground"> · only if its power is reliable</span>
          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0" title="Cancel (Esc)"
            onClick={() => setPlacingOnu(null)}>
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
          {/* Undo pops the LAST point placed (double-click still removes any
              specific one); straighten drops them all, which on save deletes the
              route row outright — the store treats an empty list as "clear". */}
          <Button variant="ghost" size="icon" className="size-5" title="Undo last point (Ctrl+Z)"
            disabled={!routeEdit.points.length}
            onClick={() => setRouteEdit((re) => re && { ...re, points: re.points.slice(0, -1) })}>
            <Undo2 className="size-3" />
          </Button>
          <Button variant="ghost" size="icon" className="size-5"
            title="Straighten — drop every point, back to a straight line"
            disabled={!routeEdit.points.length}
            onClick={() => setRouteEdit((re) => re && { ...re, points: [] })}>
            <Slash className="size-3" />
          </Button>
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

      {/* controls — slide left of the device panel so they stay clickable. The
          offset rides `--wisp-panel-beside` rather than a literal, because the
          panel is draggable now and a hardcoded width leaves a gap or an overlap
          the moment it's resized. ------------------------------------------- */}
      <div className="wisp-panel-beside absolute top-3 right-3 z-[1000] flex flex-col items-end gap-1.5">
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
                  {/* Google's own writing, off in one switch. Offered ONLY on
                      the roadmap: a satellite session carries no labels to
                      begin with (they'd be an explicit layerTypes overlay we
                      never request), and a toggle that does nothing where it is
                      shown is worse than one that isn't there. */}
                  {basemap === "google" && (
                    <button
                      className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                      title="Google's own place names, road names and POI markers. Off leaves the roads, water and parks — only the writing goes."
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
                title="Live ↓/↑ rate chips on links with a bound port (device panel → Uplinks)"
                onClick={toggleBwLabels}>
                <span>Bandwidth labels</span>
                <span className={cn("text-2xs font-medium", bwLabels ? "text-success" : "text-muted-foreground")}>
                  {bwLabels ? "on" : "off"}
                </span>
              </button>
              {/* Off by default and remembered: subscribers outnumber plant by
                  an order of magnitude, so this layer has to be asked for. */}
              <button
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                title="Subscriber ONUs with a location — field-survey pins plus the power-backed reference points you've placed. Click an OLT on the map to focus on just its drops."
                // Leaving the focus armed would make this toggle look broken:
                // its two states both draw the same scoped set. Dropping the
                // scope first means one press always changes what is on screen.
                onClick={() => { setOnuScope(null); toggleRefOnus() }}>
                {/* Named for what the layer HOLDS, not what it originally held:
                    since the field survey it carries ordinary located drops as
                    well as reference points, and "Reference ONUs" would hide the
                    survey's whole output behind a toggle nobody would think to
                    look under. The count is what makes it discoverable at all. */}
                <span>Subscribers{places.length > 0 ? ` · ${places.length}` : ""}</span>
                {/* "on" while nothing is drawn would read as a broken toggle —
                    say the layer is waiting on zoom instead. A FOCUS outranks
                    both: with one OLT scoped, this toggle is not what decides
                    what is on screen, and reporting "off" while pins are drawn
                    would be the same lie in reverse. */}
                <span className={cn("text-2xs font-medium",
                  onuScope != null || (refOnus && zoom >= REF_ONU_MIN_ZOOM)
                    ? "text-success" : "text-muted-foreground")}>
                  {onuScope != null ? "focused"
                    : !refOnus ? "off" : zoom >= REF_ONU_MIN_ZOOM ? "on" : "on · zoom in"}
                </span>
              </button>
              <div className="my-1 border-t" />
              <p className="px-2 pt-1 pb-0.5 text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                Links
              </p>
              {([
                ["", "Feed (parent → child)"],
                ["4 6", "Backup uplink (ring)"],
                ["1 4", "Cross-link (same level)"],
                [DROP_DASH, "Subscriber drop (splitter → ONU)"],
              ] as Array<[string, string]>).map(([dash, label]) => (
                <div key={label} className="flex items-center gap-2 px-2 py-1 text-xs">
                  <span className="flex w-4 shrink-0 items-center justify-center">
                    <svg width="16" height="2" aria-hidden>
                      <line x1="0" y1="1" x2="16" y2="1" stroke="var(--map-link)"
                        strokeWidth="2" strokeDasharray={dash || undefined} />
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
              {/* How complete the plant record is, stated where somebody asks
                  "what am I looking at". A splitter's load and every branch
                  verdict count only RECORDED drops, so this number is how much
                  weight either deserves — and leaving it to be inferred from
                  thin-looking splitters is exactly how a partial map gets read
                  as a complete one. */}
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
        {/* the hint FLOWS under the buttons instead of an absolute top-[10rem]:
            the stack's height depends on which controls render, and the hard
            offset landed on top of the very Pencil button you click to leave
            edit mode. Inside the column it can never overlap one. */}
        {editPins && canWrite && (
          <div className="pointer-events-none rounded-lg border border-warning/40 bg-popover/95 px-2.5 py-1.5 text-2xs text-warning backdrop-blur dark:bg-popover/95">
            drag pins to move them
          </div>
        )}
      </div>

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
                  onClick={() => { setDetailTab(deviceTabs(m)[0]); setSelectedId(m.id) }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { setDetailTab(deviceTabs(m)[0]); setSelectedId(m.id) }
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
                        title={`Move ${m.name}: click its new spot on the map`}
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

      {/* reference-ONU card -----------------------------------------------------
          Deliberately a small card and NOT the device panel: an ONU is not a
          device, it has no health tab, no ports and no outage of its own. What
          it has is a claim attached to it, so the card states the claim, where
          the box currently registers, and the two things you can do to it. */}
      {selectedRef && (
        <Card className="absolute top-14 left-3 z-[1000] w-72 gap-0 overflow-hidden border-border-strong bg-popover/95 dark:bg-popover/95 py-0 backdrop-blur">
          <div className="flex items-start justify-between gap-2 border-b px-3 py-2">
            <div className="min-w-0">
              <p className="truncate text-xs font-semibold">
                {/* refKind, not a fixed word: since the field survey this card
                    opens for ordinary located drops too, and titling one
                    "Reference ONU" would tell an operator the fleet has
                    witnesses it does not have. */}
                {selectedRef.label || selectedRef.name
                  || (selectedRef.witness ? "Reference ONU" : "Subscriber")}
              </p>
              <p className="truncate font-mono text-2xs text-muted-foreground">
                {selectedRef.mac}
              </p>
            </div>
            <Button variant="ghost" size="icon" className="size-6 shrink-0"
              onClick={() => setSelectedOnuMac(null)}>
              <X className="size-3.5" />
            </Button>
          </div>
          <div className="space-y-1.5 px-3 py-2 text-2xs">
            {/* The claim this pin makes, and only the one it actually makes.
                A WITNESS is the operator's statement that this subscriber's
                power is reliable — `ponfault` reads it to call a dark PON an
                area power cut instead of rolling a splicing van. A located drop
                claims nothing but a coordinate, and printing the witness
                sentence over one would tell an operator the fleet has evidence
                it does not have (the exact split `onu_places.witness` exists
                for). */}
            <p className="text-muted-foreground">
              {selectedRef.witness
                ? "Power-backed reference point · used to tell a fibre cut from an area power cut."
                : "Subscriber location recorded in the field · not a power-backed reference point."}
            </p>
            {/* Three states, never collapsed: registered somewhere, on more than
                one live slot (so we refuse to say where), or in no roster at all
                — which means it is witnessing nothing and the operator has to
                know rather than trust a pin that looks fine. */}
            {!selectedRef.matched ? (
              <p className="text-warning">
                Not in any current roster — the ONU was probably swapped. Re-place
                it under the new MAC.
              </p>
            ) : selectedRef.ambiguous ? (
              <p className="text-warning">
                On {selectedRef.slots} live slots, so we can't say which OLT it
                belongs to.
              </p>
            ) : (
              <p>
                <span className="font-mono">{selectedRef.device_name}</span>
                {selectedRef.pon_port && <> · PON {selectedRef.pon_port}</>}
                {selectedRef.onu_id != null && <> · ONU {selectedRef.onu_id}</>}
              </p>
            )}
            {/* Where the drop actually comes from. The line on the map already
                shows it, but the card is where a name can be read and followed:
                a subscriber's problem is usually its splitter's problem, and
                that box is the next thing to open. An unrecorded drop says so
                plainly rather than leaving the OLT line to imply direct fibre. */}
            {selectedRef.matched && (
              selectedRef.drop_passive_id != null
                && byId.get(selectedRef.drop_passive_id) ? (
                <p>
                  <span className="text-faint-foreground">Drop from </span>
                  <button className="underline-offset-2 hover:underline"
                    onClick={() => {
                      setSelectedId(selectedRef.drop_passive_id!)
                      setSelectedOnuMac(null)
                    }}>
                    {byId.get(selectedRef.drop_passive_id)!.name}
                  </button>
                </p>
              ) : (
                <p className="text-faint-foreground">
                  Serving splitter not recorded — the line to the OLT stands in
                  for plant nobody has mapped yet.
                </p>
              )
            )}
            {selectedRef.matched && (
              <p className={cn("font-medium",
                isRefDark(selectedRef) ? "text-destructive" : "text-success")}>
                {isRefDark(selectedRef)
                  ? `Dark (${selectedRef.state}) — power can't explain this`
                  : "Online"}
                {selectedRef.rx_dbm != null && (
                  <span className="font-mono font-normal text-muted-foreground">
                    {" "}· {selectedRef.rx_dbm.toFixed(1)} dBm
                  </span>
                )}
              </p>
            )}
            {/* Three different sentences, never collapsed into a blank cell:
                a live rate, a port whose walk went stale, and a firmware that
                publishes no per-ONU interface at all. */}
            {selectedRef.matched && (
              refHasRate(selectedRef) ? (
                <p className="font-mono text-muted-foreground">
                  ↓ {((selectedRef.out_bps ?? 0) / 1e6).toFixed(1)} Mb/s
                  {" · "}↑ {((selectedRef.in_bps ?? 0) / 1e6).toFixed(1)} Mb/s
                  {selectedRef.if_name && (
                    <span className="text-faint-foreground">
                      {" "}· {selectedRef.if_name.split(" ")[0]}
                    </span>
                  )}
                </p>
              ) : (
                <p className="text-faint-foreground">
                  {selectedRef.if_name
                    ? "No recent rate — this OLT's port walk is stale."
                    : "This OLT's firmware doesn't publish a per-ONU interface, so there's no rate to show."}
                </p>
              )
            )}
          </div>
          {canWrite && (
            <div className="flex gap-1 border-t px-2 py-1.5">
              <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
                title="Click the map to move this reference point"
                onClick={() => {
                  setPlacingOnu({ mac: selectedRef.mac,
                                  label: selectedRef.label || selectedRef.name || "" })
                  setSelectedOnuMac(null)
                }}>
                <Crosshair className="size-3" /> Move
              </Button>
              {selectedRef.device_id != null && (
                // Stays on the map: the same device panel every pin opens, on the
                // Optical tab with this ONU's row focused. Leaving for /topology
                // threw away the map the operator was reading it against.
                <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
                  title="Open this ONU's OLT in the device panel"
                  onClick={() => {
                    setDetailTab("optical")
                    setDetailOnu({ deviceId: selectedRef.device_id!, mac: selectedRef.mac })
                    setSelectedId(selectedRef.device_id)
                    setSelectedOnuMac(null)
                  }}>
                  <ListTree className="size-3" /> Its OLT
                </Button>
              )}
              <Button variant="ghost" size="sm"
                className="h-7 flex-1 text-2xs text-muted-foreground hover:text-destructive"
                onClick={() => {
                  setOnuPlace.mutate({ mac: selectedRef.mac, lat: null, lng: null })
                  setSelectedOnuMac(null)
                }}>
                <EyeOff className="size-3" /> Remove
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* device panel ----------------------------------------------------------
          Hidden while a route is being drawn: it occupies the right 380px, which
          is map you need to click waypoints onto, and the route being edited
          often runs straight under it. selectedId is KEPT, not cleared — the
          panel comes back on save/cancel with the same device still open. */}
      {selected && !routeEdit && (
        // Opaque, and no longer /95 + backdrop-blur. Seeing the tiles through it
        // reads as atmosphere on an empty stretch of map and as noise the moment
        // anything is under it — a label or a satellite frame ghosting up through
        // a dBm column is exactly the figure/ground problem this panel exists to
        // avoid. The map stays fully visible AROUND the panel, which is where it
        // was doing its work.
        <Card className="wisp-device-panel absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)]">
          <PanelResizeGrip grip={panel.grip} />
          <DevicePanelHeader device={selected} tone={pinTone(selected)}
            downstream={downstream.size} downstreamDown={downstreamDown}>
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
          </DevicePanelHeader>
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
                  {/* Reading the pin (copy/navigate) and editing it (route/coords)
                      are different jobs — a hairline splits them so four
                      identical ghosts don't read as one undifferentiated strip. */}
                  {canWrite && isPlaced(selected) && (
                    <span className="mx-1 h-4 w-px shrink-0 bg-border" aria-hidden />
                  )}
                  {canWrite && cables.length > 0 && (
                    // The button carries STATE: a device with any drawn cable
                    // reads as active, so "does this link have a path?" is
                    // answerable without opening the menu. Every cable opens a
                    // submenu rather than acting on click — a span has two
                    // editable things now (its path and its colour), so even a
                    // device with ONE cable has a choice to make.
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon"
                          className={cn("size-7", drawnCables ? "text-primary" : "text-muted-foreground")}
                          title="Cables on this device">
                          <Spline className="size-3.5" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-64">
                        <DropdownMenuLabel className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                          Cables · {drawnCables}/{cables.length} drawn
                        </DropdownMenuLabel>
                        {cables.map((c) => (
                          <DropdownMenuSub key={`${c.childId}:${c.parentId}`}>
                            <DropdownMenuSubTrigger>
                              {c.dir === "up" ? <ArrowUp className="size-3.5 shrink-0 text-muted-foreground" />
                                : c.dir === "down" ? <ArrowDown className="size-3.5 shrink-0 text-muted-foreground" />
                                : <ArrowLeftRight className="size-3.5 shrink-0 text-muted-foreground" />}
                              <span className="min-w-0 flex-1 truncate font-mono text-xs">{c.other.name}</span>
                              {c.kind !== "primary" && <RowTag tone="muted">{c.kind}</RowTag>}
                              {/* colour reads as a swatch, not a word — it's the
                                  thing you're matching against the map */}
                              {isLinkColor(c.color) && (
                                <span className="size-2.5 shrink-0 rounded-full"
                                  style={{ background: linkColorVar(c.color) }} aria-hidden />
                              )}
                            </DropdownMenuSubTrigger>
                            <DropdownMenuSubContent className="w-56">
                              <DropdownMenuItem onSelect={() => startRouteEdit(c)}>
                                <Spline className="size-3.5 shrink-0 text-muted-foreground" />
                                <span className="flex-1">{c.route ? "Edit" : "Draw"} cable route</span>
                                <span className={cn("shrink-0 text-2xs",
                                  c.route ? "text-primary" : "text-faint-foreground")}>
                                  {c.route ? `${c.route.length} pt` : "none"}
                                </span>
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuLabel className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                                Line colour
                              </DropdownMenuLabel>
                              {/* Swatches, not a list of names: picking a colour
                                  is a visual match against the map behind the
                                  panel. Grid rather than a row so the hit targets
                                  stay finger-sized. */}
                              <div className="flex flex-wrap gap-1.5 px-2 py-1.5">
                                {LINK_COLORS.map((col) => (
                                  <button key={col} type="button"
                                    title={linkColorName(col)}
                                    aria-label={linkColorName(col)}
                                    aria-pressed={c.color === col}
                                    className={cn(
                                      "size-6 rounded-md border transition-colors",
                                      c.color === col
                                        ? "border-foreground/70" : "border-border hover:border-border-strong")}
                                    style={{ background: linkColorVar(col) }}
                                    onClick={() => setLinkStyle.mutate({
                                      childId: c.childId, parentId: c.parentId,
                                      style: { color: c.color === col ? null : col },
                                    })} />
                                ))}
                              </div>
                              <DropdownMenuItem disabled={!c.color}
                                onSelect={() => setLinkStyle.mutate({
                                  childId: c.childId, parentId: c.parentId,
                                  style: { color: null, label_pos: null },
                                })}>
                                <Undo2 className="size-3.5 shrink-0 text-muted-foreground" />
                                Reset colour &amp; label
                              </DropdownMenuItem>
                            </DropdownMenuSubContent>
                          </DropdownMenuSub>
                        ))}
                        {/* the status tones are never available as a choice, so
                            say what a coloured line still can't hide */}
                        <p className="px-2 pt-1 pb-1.5 text-2xs leading-snug text-faint-foreground">
                          A line in trouble always renders red or amber, whatever
                          colour you give it.
                        </p>
                      </DropdownMenuContent>
                    </DropdownMenu>
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
          {/* Cable length is a sentence, so it gets its own line — sharing the
              coords row with four buttons is what wrapped that row. ONE readout,
              not two competing ones: drawn length leads (splicing crews quote
              drum metres off it), the chord trails as context, and the far end
              is named once at the end rather than by each number. */}
          {linkKm != null && parent && (
            <div className="flex min-w-0 items-center gap-x-1.5 border-b px-4 py-1.5 text-xs text-muted-foreground">
              {routeKm != null && (
                <span className="shrink-0" title={`Along the drawn cable route to ${parent.name}`}>
                  <span className="font-semibold text-foreground">{fmtKm(routeKm)}</span> cable
                </span>
              )}
              {/* labeled honestly: this is the chord, not cable length — a
                  splicing crew quoting drum meters off it comes up short */}
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
          {/* Focus the subscriber layer on THIS box. The row only renders where
              it has something to draw — an OLT with located drops — because an
              action that reveals nothing teaches the operator to stop pressing
              it, and on most of the fleet that is what it would do.

              It says "located", never "subscribers": a survey is always partial,
              and a count that reads as the roster would make an OLT with 3 pins
              and 196 customers look fully mapped (the same rule the splitter
              panel's "recorded" follows). */}
          {(placedByOlt.get(selected.id) ?? 0) > 0 && (() => {
            const pons = ponsByOlt.get(selected.id) ?? []
            const focused = onuScope?.deviceId === selected.id
            const picked = focused ? onuScope.pons : []
            // What the trigger SAYS is the current selection, not the action —
            // this row is the only place the PON filter is visible while the
            // panel covers the status strip's copy of it.
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
                {/* One PON is no choice at all, so it stays the plain toggle it
                    was — a menu whose every path does the same thing is worse
                    than the button it replaced. */}
                {pons.length < 2 ? (
                  <Button variant={focused ? "default" : "outline"}
                    size="sm" className="ml-auto h-7 px-2 text-xs"
                    title="Draw only this OLT's located subscribers, and frame them"
                    onClick={() => focused ? setOnuScope(null) : scopeOnus(selected.id, [])}>
                    {focused ? "Focused" : "Show on map"}
                  </Button>
                ) : (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant={focused ? "default" : "outline"}
                        size="sm" className="ml-auto h-7 max-w-36 px-2 text-xs"
                        title="Draw this OLT's located subscribers — all of them, or the PONs you pick">
                        <span className="min-w-0 truncate font-mono">{label}</span>
                        <ChevronDown className="size-3 shrink-0 opacity-60" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-56">
                      <DropdownMenuLabel className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
                        Show subscribers on
                      </DropdownMenuLabel>
                      {/* "All PONs" CLEARS the ticks rather than being a tick of
                          its own — the empty set is what every-PON means here,
                          and a checkbox that fights the others reads as a state
                          you can be in alongside them. */}
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
                      {/* The menu stays OPEN on each tick (onSelect prevented):
                          picking a set one item at a time through a menu that
                          closes each time is how a multi-select stops being one.
                          The map re-fits underneath as you go. */}
                      {pons.map((p) => (
                        <DropdownMenuCheckboxItem key={p.pon}
                          checked={focused && picked.includes(p.pon)}
                          onSelect={(e) => e.preventDefault()}
                          onCheckedChange={() => toggleScopePon(selected.id, p.pon)}>
                          <span className="min-w-0 truncate font-mono">{p.pon}</span>
                          {/* dark count wins the cell during a cut — it is the
                              PON to tick first; otherwise the located count */}
                          <span className={cn("ml-auto font-mono text-2xs",
                            p.dark > 0 ? "font-semibold text-destructive" : "text-faint-foreground")}>
                            {p.dark > 0 ? `${p.dark} dark` : p.total}
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
          {/* overscroll-contain stops scroll CHAINING out of the panel (the Card
              carries it too, via .wisp-device-panel, since the header and the
              coords/cable rows sit outside this scroller). */}
          <div className="overflow-y-auto overscroll-contain p-3">
            <DeviceDetail device={selected} tab={detailTab}
              onTab={(t) => { setDetailTab(t); setDetailOnu(null) }}
              focusOnuMac={detailOnu?.deviceId === selected.id ? detailOnu.mac : null} />
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
