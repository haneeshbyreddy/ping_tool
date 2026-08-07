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
  Expand, EyeOff, Layers, ListTree, LocateFixed, MapPin, MapPinOff, Maximize2, Navigation,
  Pencil, Plus, Shrink, Slash, Spline, Undo2, Users, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDarkMode } from "@/hooks/use-dark-mode"
import { useNow } from "@/hooks/use-now"
import { PanelResizeGrip, useResizablePanel } from "@/hooks/use-resizable-panel"
import { fieldApi, inventoryApi, orgsApi, ApiError } from "@/lib/api"
import { mapRegionOf } from "@/lib/map-regions"
import { isPassiveType, type OnuPlace, type OrgDevice, type PonFault } from "@/lib/types"
import {
  DeviceDetail, DevicePanelHeader, RowTag, deviceTabs, type DeviceTab,
} from "@/components/device-detail"
import { SubscriberDetail } from "@/components/subscriber-detail"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
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
import { DevHoverCard } from "@/map/devhover"
import { alongKm, distanceKm, fmtKm, nearestOnPath, pointAt, polyKm } from "@/map/geometry"
import { LINK_COLORS, isLinkColor, linkColorName, linkColorVar, paintedLineColor } from "@/map/linkcolor"
import { LinkHoverProbe, hoverIcon, projectLinks, type LinkHover } from "@/map/linkhover"
import { bindLinkPorts, bwRank, linkBwIcon, linkKey, linkLabelPos, type LinkBinding } from "@/map/linklabel"
import {
  isDownState, isPlaced, isTrouble, meIcon, pinIcon, pinTone, vertexIcon, type Placed,
} from "@/map/pins"
import {
  REF_DASH, REF_HOVER_BOOST, REF_NAME_DY, isRefDark, isRefEvidence, refBwIcon,
  refHasChip,
  refLineTone, refNameIcon, refOnuIcon, refZIndex,
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
  AttachCustomerDialog, PlantCreateDialog,
  type CustomerDraft, type PlantDraft,
} from "@/components/plant-create"
import { detailFrom } from "@/map/detail"
import { MapSearch, type OnuHit, type PlaceHit } from "@/map/search"
import {
  CASING_OPACITY, CASING_OPACITY_HOVER, casingAt, lineScale, strokeAt,
} from "@/map/stroke"
import { FIT_PADDING, InvalidateOnResize, MapEvents, ViewController, loadView } from "@/map/view"
import {
  trailStyle, workerCensus, workerIcon, workerPlaced, workerState, workerZIndex,
} from "@/map/workers"

const BW_LABELS_KEY = "wisp:map:bw-labels"
const REF_ONUS_KEY = "wisp:map:ref-onus"
const WORKERS_KEY = "wisp:map:workers"
const GOOGLE_LABELS_KEY = "wisp:map:google-labels"

/** Every layer that has a zoom floor now reads it from `map/detail.ts`, which
 *  holds the shipped defaults, their reasoning and the one ordering invariant
 *  between them, and persists the operator's own numbers per browser. The
 *  Layers popover edits them live. Nothing outside display reads any of it. */

/** Which PON bucket a located subscriber falls in, for the focus filter.
 *
 *  ONE definition, used by the filter AND by the picker that drives it: a
 *  subscriber whose `pon_port` the walk never carried is still somebody's drop,
 *  and if the two sides spelled its bucket differently, ticking a PON would hide
 *  pins the operator was asking to see. */
const ponKey = (p: OnuPlace): string => p.pon_port ?? "—"

/** one PON of one OLT, as the focus picker and the status strip count it */
interface PonRow { pon: string; total: number; dark: number }

/** Below this much spread, a power-pattern wave is ONE SITE, not an area: no
 *  hull is drawn for it and the verdict says so in words. `incidents.evaluate`
 *  rounds its radius to 10 m, so this only ever rejects a rack's worth of GPS
 *  noise — anything a crew could drive between still gets its circle. */
const HULL_MIN_M = 30

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

/** A drawn route needs somewhere to go: TRUE when a link's two ends resolve to
 *  the same point on screen, which is the only case where traced geometry cannot
 *  be rendered as itself.
 *
 *  THIS REPLACED A DISPLACEMENT THRESHOLD, AND THE THRESHOLD WAS THE WRONG IDEA
 *  TWICE OVER. History, because it is the whole argument: a cluster fold moves a
 *  pin to its site's centroid, so a route anchored on that pin starts a little
 *  off where it was surveyed. The first rule dropped the route on ANY
 *  displacement (exact equality), which meant racked gear never showed a route
 *  at all. The second allowed 10 screen px — but a fixed px budget was being
 *  compared against a GROUND distance, which doubles per zoom level, so it
 *  flipped as you zoomed in and a traced route reverted to a chord permanently
 *  past z20 (operator: "when i zoom in enough on an OLT the laid out line becomes
 *  straight"). The third added a relative clause and STILL failed, on
 *  badri_fiber's Gpon_08→Gpon_04 at exactly z17: Gpon_04 folds in with SPL-1/5,
 *  the centroid sits 11.9px off, and the segment it anchors is only 24px, so
 *  neither clause could pass. One zoom level either side was fine (operator:
 *  "at specific zoom levels the line is still becoming straight").
 *
 *  Three attempts at "how far is too far" is the tell that the QUESTION is
 *  wrong. There is no such distance, because the two outcomes are not
 *  commensurable:
 *
 *  · a folded endpoint nudges the FIRST OR LAST SEGMENT by at most a cluster
 *    radius. Every waypoint between is untouched, and it self-heals the moment
 *    the cluster splits. Cosmetic, bounded, temporary.
 *  · a chord replaces the ENTIRE surveyed path with a straight line that is
 *    indistinguishable from a real one. Unbounded, and a lie of exactly the kind
 *    this map may not tell — crews order drum off these lines, which is why
 *    `linkhover` labels the chord case and CLAUDE.md keeps the dashes apart.
 *
 *  So the rule stops asking how far and asks whether the route can be drawn at
 *  all. It can, unless both ends landed on one point — every device in one
 *  badge, where the "route" would be a scribble looping from a dot back to
 *  itself and the chord is correctly a zero-length line. */
const foldedTogether = (a: [number, number], b: [number, number]): boolean =>
  a[0] === b[0] && a[1] === b[1]

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
  // The device under the cursor. Hovering a box lights the CABLES INTO IT —
  // the question a network map is asked more than any other ("what does this
  // connect to") and, until now, one that cost a click and a panel to answer.
  //
  // Deliberately a LIGHTER emphasis than selection: selection lights the whole
  // downstream PATH and is a statement about what you are working on; hover
  // lights only the DIRECT links and evaporates. If the two looked alike,
  // sweeping the cursor across a dense site would read as the selection jumping
  // around. Nothing else keys off it — no panel opens, no query fires, no state
  // is written — so it stays free to be wrong.
  const [hoverId, setHoverId] = useState<number | null>(null)
  // The SITE under the cursor, held as one of its member device ids rather than
  // a cluster key — membership shifts with zoom, and a key-anchored hover would
  // be dropped mid-zoom by a badge that is still under the pointer. Same reason
  // `siteAnchor` (the click-opened card) is a device id.
  //
  // Separate state from `hoverId` because a badge is not a pin: it stands for
  // several boxes, so what lights up is every cable into ANY member, and what
  // opens is a card about the site rather than about one box.
  const [hoverSiteId, setHoverSiteId] = useState<number | null>(null)
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
  // Armed subscriber placement. It carries NO claim and cannot: putting a
  // customer on the map is a location, and the power claim is its own toggle.
  // This button used to assert it silently, which is how a morning of survey
  // work became a fleet of witnesses.
  const [placingOnu, setPlacingOnu] =
    useState<{ mac: string; label: string } | null>(null)
  const [selectedOnuMac, setSelectedOnuMac] = useState<string | null>(null)
  // The subscriber under the cursor. Same shape as `hoverId` above and for the
  // same reason — nothing is written, no query fires, it evaporates — but it
  // carries more: the drop line goes SOLID and a card opens beside the pin.
  //
  // It is state rather than CSS because two things OUTSIDE the mark have to
  // change with it, which :hover cannot reach. The mark's own scale stays pure
  // CSS, and this must never enter `refOnuIcon`'s html: icons are cached by
  // that string, so a hover class there would swap the diamond's DOM node and
  // replay its fade-in every time the pointer crossed one.
  const [hoverOnuMac, setHoverOnuMac] = useState<string | null>(null)
  // A subscriber focus whose flight is still in the air. `zoom` state only
  // lands at zoomend, so for the length of a flyTo the visibility guard below
  // would judge the pin we are flying TO against the zoom we are flying FROM —
  // and close its card before it ever drew. Cleared by the first zoom report
  // after arrival, which is also the first moment the guard can judge fairly.
  const [focusFlying, setFocusFlying] = useState(false)
  const [placeOpen, setPlaceOpen] = useState(false)
  // ---- recording plant and customers from the map --------------------------
  // The right-click menu, anchored in CONTAINER px. It closes on any view move
  // because a menu pinned to a screen position stops pointing at the ground it
  // was opened over the moment the map pans under it.
  const [plantMenu, setPlantMenu] = useState<PlantMenuAnchor | null>(null)
  // The create sheet's subject: a kind, a coordinate and the feeder the click
  // inferred. Non-null means the sheet is open.
  const [plantDraft, setPlantDraft] = useState<PlantDraft | null>(null)
  const [customerDraft, setCustomerDraft] = useState<CustomerDraft | null>(null)
  // "Click where it goes." Set by a menu item opened ON a pin (that pin already
  // owns its own coordinate, so creating at it would stack two boxes on one
  // point) and by "Save and add another", which is what makes recording a whole
  // feeder run one continuous gesture rather than eight round trips.
  const [armed, setArmed] = useState<{ kind: ArmKind; parentId: number | null } | null>(null)
  // The `+` button's mode: the next click opens the MENU rather than creating
  // anything. A context menu nobody knows to right-click for is a feature that
  // does not exist, and this is the visible twin of it.
  const [addNext, setAddNext] = useState(false)
  // drawing a cable path for one link: clicks append vertices, drags adjust
  const [routeEdit, setRouteEdit] = useState<{
    childId: number; parentId: number; points: Array<[number, number]>
  } | null>(null)
  const [editPins, setEditPins] = useState(false)
  const [troubleOnly, setTroubleOnly] = useState(false)
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
  const confirmUnpin = useConfirm()
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
  // Field workers, off by default and remembered per browser — same discipline
  // as the subscriber layer, for the same reason: the map is a plant view, and
  // everything else on it has to be asked for. Owner-only (the API is), so a
  // worker session never fetches it.
  const [showWorkers, setShowWorkers] = useState(() => {
    try { return localStorage.getItem(WORKERS_KEY) === "on" } catch { return false }
  })
  const toggleWorkers = () => {
    setShowWorkers((v) => {
      try { localStorage.setItem(WORKERS_KEY, v ? "off" : "on") } catch { /* private mode */ }
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
  // The wall clock, ticking every 15s. Held rather than discarded because the
  // worker layer's four states are AGE-derived: "here now" has to become "gone
  // quiet" on its own, without a refetch, or the map keeps a stale claim alive
  // for as long as the tab is open.
  const now = useNow()

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

  // Where the crew is (central/field.py). Fetched only while the layer is on:
  // the reply carries a trail per worker, so it is the one query here worth
  // gating on the toggle. Owner-only server-side, hence the canWrite gate — a
  // worker session would 403 on every poll.
  const workersQ = useQuery({
    queryKey: ["field-workers", scopeOrg],
    queryFn: () => fieldApi.workers(scopeOrg),
    enabled: !!scopeOrg && canWrite && showWorkers,
    // A position ages into "gone quiet" on its own; this is how the map finds
    // out a phone came back. Slower than the plant polls — a van does not move
    // faster than the 90 s the tracker itself reports on.
    refetchInterval: 60_000,
  })
  const fieldWorkers = workersQ.data?.workers ?? []
  const workerFreshS = workersQ.data?.fresh_s ?? 300
  const census = useMemo(
    () => workerCensus(fieldWorkers, workerFreshS, now),
    [fieldWorkers, workerFreshS, now])

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
  // Per-layer zoom floors: ONE server-wide setting (Settings → Platform), riding
  // this same org row like the Maps key. `detailFrom` fills in the shipped
  // defaults, so the map draws correctly on the first paint — before this query
  // resolves and for as long as it never does.
  const detail = useMemo(() => detailFrom(myOrg?.map_detail), [myOrg?.map_detail])

  const devices = useMemo(() => data?.devices ?? [], [data])
  const placed = useMemo(() => devices.filter(isPlaced), [devices])
  const unplaced = useMemo(() => devices.filter((d) => !isPlaced(d)), [devices])
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const selected = selectedId != null ? byId.get(selectedId) ?? null : null
  const placing = placingId != null ? byId.get(placingId) ?? null : null

  // What a right-click on bare ground can infer. Both are SUGGESTIONS and the
  // menu names them in the item you are about to press, so a wrong guess costs a
  // glance rather than a wrong branch-fault verdict later. Over a PIN neither is
  // computed: that click already named its box.
  const menuFeeder = useMemo(
    () => (plantMenu && !plantMenu.device
      ? nearestFeeder(plantMenu.lat, plantMenu.lng, devices) : null),
    [plantMenu, devices])
  const menuDropOn = useMemo(
    () => (plantMenu && !plantMenu.device
      ? nearestPassive(plantMenu.lat, plantMenu.lng, devices) : null),
    [plantMenu, devices])

  // What the subscriber layer actually draws. A scope NARROWS; it never adds.
  //
  // Declared HERE, above the plant block, rather than beside the rest of the
  // layer's rules below: the plant a focus leaves on the map is derived partly
  // from the drops that survive this filter, so the two can't be read in the
  // other order.
  const shownPlaces = useMemo(() => {
    if (!onuScope) return places
    const { deviceId, pons } = onuScope
    return places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
  }, [places, onuScope])

  // ---- Passive plant's own zoom floor, and an OLT focus's narrowing --------
  //
  // Plant left the clustering pass on 2026-08-05 — a site badge is a claim about
  // GEAR, and folding a splitter into one made the count answer a question
  // nobody asked. The cost accepted with it was that dense plant now OVERLAPS at
  // low zoom instead of folding, which is exactly what subscribers do; this is
  // the same answer subscribers already got for it (operator, 2026-08-05).
  //
  // The PIN and the CABLE INTO IT stand down together. Hiding one without the
  // other leaves a line running to a point where nothing is drawn, which reads
  // as a rendering fault rather than as a setting — the same reason `drop_lines`
  // may never sit below this floor (`detailMin`).
  //
  // Two exemptions, in the grammar the device labels already use ("anything down
  // or selected keeps its name at every zoom"), because a density knob may hide
  // reference material and never a fact:
  //
  //  · A passive whose recorded subscribers are DARK, AND the plant above it. A
  //    branch fault names the SPAN between two pins — that span is the whole
  //    output of the feature and it is where a van drives — so dropping either
  //    end would take an alarm off the map. The ancestor walk is also what keeps
  //    that overlay's own link in `drawnLinks`: a dark splitter fed by a healthy
  //    one still needs the cable between them drawn.
  //  · Anywhere the operator has already said what they want to see: the
  //    selection (its panel is open, and a panel floating over nothing is the
  //    failure this map is careful about), and every input surface where plant
  //    is what the cursor is aiming at or dragging.
  //
  // An OLT FOCUS is deliberately NOT on that list any more (it was, until
  // 2026-08-06). It is not "show me everything" — it is the operator naming one
  // OLT, so it NARROWS plant the way it already narrowed subscribers, and the
  // narrowed set is what then bypasses the zoom floor. See `scopePlant`.
  //
  // A focus does NOT carry the dark-splitter exemption across, and that is the
  // one judgement call here. It is not a density knob quietly hiding a fact: it
  // is announced on the map (the focus bar), one click to leave, and it ALREADY
  // hides the dark SUBSCRIBERS under another OLT's splitters — so keeping their
  // branch-fault span drawn over customers that aren't would be the louder lie.
  const plantPinned = editPins || routeEdit != null || armed != null || addNext
    || placingId != null || placingOnu != null || plantDraft != null
    || customerDraft != null || plantMenu != null
  // The plant an OLT focus leaves drawn — null when nothing is focused. The
  // rules (and the two reasons a box out of scope still stays) live in
  // `plant.ts:plantInScope`; it is fed `shownPlaces` rather than `places`, so a
  // PON pick narrows the drop-line safety net with the drops themselves.
  const scopePlant = useMemo(
    () => (onuScope ? plantInScope(onuScope, devices, byId, shownPlaces) : null),
    [onuScope, devices, byId, shownPlaces])
  const hiddenPlant = useMemo(() => {
    const out = new Set<number>()
    // An input surface wins outright: picking a parent for a new splitter means
    // reaching boxes outside the focus, and a dropdown of plant you cannot see
    // is the flow the map authoring replaced.
    if (plantPinned) return out
    // A FOCUS outranks the zoom floor in both directions — the set it leaves is
    // bounded and was asked for by name, so it draws however far out you are,
    // and everything else stands down however far in.
    if (scopePlant) {
      for (const d of placed)
        if (isPassiveType(d.device_type) && !scopePlant.has(d.id) && d.id !== selectedId)
          out.add(d.id)
      return out
    }
    if (zoom >= detail.passives) return out
    const passives = placed.filter((d) => isPassiveType(d.device_type))
    const keep = new Set<number>(branchFaults.map((f) => f.passive_id))
    for (const d of passives) {
      const load = loadByPassive.get(d.id)
      // frozen exactly as the PIN computes it, so the exemption and the tone it
      // is drawn from can never disagree: behind a DOWN OLT there is nothing
      // current to claim, and `drops.branch_faults` skips one server-side too.
      const olt = load?.olt_id != null ? byId.get(load.olt_id) : undefined
      if (dropTone(load, !!olt && isDownState(olt)) === "dark") keep.add(d.id)
    }
    for (const id of [...keep]) {
      let cur = byId.get(id)?.parent_device_id ?? null
      // cycle-guarded like every other parent walk here — a bad row may not spin
      // a render, and gear ends the chain anyway (it is never hidden).
      for (let hop = 0; cur != null && hop < 32; hop++) {
        const p = byId.get(cur)
        if (!p || !isPassiveType(p.device_type) || keep.has(p.id)) break
        keep.add(p.id)
        cur = p.parent_device_id ?? null
      }
    }
    for (const d of passives)
      if (!keep.has(d.id) && d.id !== selectedId) out.add(d.id)
    return out
  }, [placed, zoom, detail.passives, plantPinned, scopePlant, branchFaults,
      loadByPassive, byId, selectedId])
  // What the map actually DRAWS — pins and the links between them. Deliberately
  // not `placed` itself: the census ("N / M on map"), Fit-all, search and the
  // drag-snap all still count and reach every placed box, because hiding a
  // reference layer must not make the fleet look smaller than it is.
  const drawnDevices = useMemo(
    () => (hiddenPlant.size === 0
      ? placed : placed.filter((d) => !hiddenPlant.has(d.id))),
    [placed, hiddenPlant])

  // Overlapping pins fold into site clusters. pinPos is each device's DISPLAY
  // position — raw when alone, the cluster centroid while folded. Nothing ever
  // renders at a fabricated coordinate: folded members are listed in the site
  // card (UI space), not scattered over the tiles. Links read pinPos, so
  // lines follow the fold.
  const clusters = useMemo(() => buildClusters(drawnDevices, zoom),
                           [drawnDevices, zoom])
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
  const refVisible = (refOnus && zoom >= detail.subscribers)
    || onuScope != null || placingOnu != null
  // The lines and their rate chips need LENGTH to mean anything, so they carry
  // their own floor, which `normalizeDetail` keeps at or above the marks' one.
  // Both exceptions carry over unchanged — a named scope and an in-progress
  // placement are cases where the operator has said what they want to see.
  const refLinesVisible = refVisible
    && (zoom >= detail.drop_lines || onuScope != null || placingOnu != null)
  // Names ride the MARK, so they can never outlive it — and they carry their own
  // floor above it because a name is the widest thing this layer draws and there
  // is one per customer. Same two exceptions again. A DARK subscriber is exempt
  // (see the budget below): the name somebody is about to phone.
  // A dark WITNESS is exempt from the floor (see the budget below); an ordinary
  // offline customer is not.
  const refNamesVisible = refVisible
    && (zoom >= detail.subscriber_names || onuScope != null || placingOnu != null)
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

  // A DEVICE selection supersedes a SUBSCRIBER one. Both panels render into the
  // same right rail — deliberately, since a subscriber is an object of the same
  // weight as a device — so exactly one may be open. Enforced HERE, in one
  // place, rather than at the ~15 call sites that set `selectedId`: a rule
  // spelled out once cannot be forgotten by the next thing that opens a device
  // panel. The subscriber marker's own click clears `selectedId` for the same
  // reason in the other direction, where there is only one caller.
  useEffect(() => {
    if (selectedId != null && selectedOnuMac != null) setSelectedOnuMac(null)
  }, [selectedId, selectedOnuMac])

  // The hovered subscriber, resolved to the row the card is built from.
  //
  // Derived rather than stored so it can never outlive its pin: a mark that
  // stops being drawn (zoomed past the floor, filtered out by a scope, gone
  // from the roster) takes its card with it in the same render. `mouseout`
  // handles the ordinary case; this handles every case where the mark leaves
  // WITHOUT the pointer moving, which is exactly when a stale card would hang
  // over the tiles claiming to describe something that isn't there.
  //
  // Suppressed for the SELECTED subscriber, whose full card is already open
  // with the same facts and the actions besides.
  const hoverPlace = useMemo(() => {
    if (hoverOnuMac == null || !refVisible || hoverOnuMac === selectedOnuMac) return null
    // Arming a placement or a route mid-hover has to close it too — the mode
    // can change without the pointer ever moving off the diamond.
    if (placingId != null || placingOnu != null || routeEdit != null) return null
    return shownPlaces.find((p) => p.mac === hoverOnuMac) ?? null
  }, [hoverOnuMac, refVisible, selectedOnuMac, shownPlaces,
      placingId, placingOnu, routeEdit])

  // …and forget it once the mark is gone. Deriving the CARD is enough to stop a
  // stale one being drawn, but the MAC would sit in state unclaimed: zoom past
  // the floor while hovering, zoom back, and a card would reappear over a pin
  // the cursor left minutes ago. `mouseout` can't cover it — the mark leaves
  // without the pointer ever moving.
  useEffect(() => {
    if (hoverOnuMac == null) return
    if (!refVisible || !shownPlaces.some((p) => p.mac === hoverOnuMac))
      setHoverOnuMac(null)
  }, [hoverOnuMac, refVisible, shownPlaces])

  // What the card needs that the placement row doesn't carry: the NAME of the
  // box its highlighted line runs to, and whether the readings behind it are
  // frozen. Both are facts about the device list, and resolving them here keeps
  // `refhover.tsx` knowing nothing about devices — the same split that keeps
  // `pins.ts` and `map/drops.ts` from importing each other.
  const hoverCtx = (p: OnuPlace) => {
    const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
    const olt = p.device_id != null ? byId.get(p.device_id) : undefined
    return {
      anchorName: anchor?.device.name ?? null,
      viaSplitter: anchor?.kind === "splitter",
      frozen: !!olt && isDownState(olt),
    }
  }

  // The hovered BOX, resolved to the row its card is built from — the same
  // shape as `hoverPlace` above and derived for the same reason: a card must not
  // outlive the mark it points at. Read off `clusters`, not `byId`, because that
  // is what decides whether this pin is actually drawn: zoom out and a pin folds
  // into a site badge WITHOUT the pointer ever moving, and Leaflet fires no
  // mouseout when the marker it was over simply unmounts.
  //
  // Suppressed for the SELECTED device, whose full panel is already open with
  // these facts and the actions besides, and while the map is an INPUT surface
  // (placement, route drawing, dragging pins) — there the cursor means "put a
  // thing here", and a card chasing it is the same noise the distance readout
  // stands down for. A hovered SUBSCRIBER wins outright: its diamond sits above
  // the pins and the two cards would land on the same pixels.
  const hoverDevice = useMemo(() => {
    if (hoverId == null || hoverId === selectedId || hoverOnuMac != null) return null
    if (placingId != null || placingOnu != null || routeEdit != null || editPins) return null
    if (armed != null || addNext || plantMenu != null) return null
    if (plantDraft != null || customerDraft != null) return null
    const solo = clusters.find((c) => c.members.length === 1 && c.members[0].id === hoverId)
    return solo?.members[0] ?? null
  }, [hoverId, selectedId, hoverOnuMac, placingId, placingOnu, routeEdit, editPins,
      armed, addNext, plantMenu, plantDraft, customerDraft, clusters])

  // …and forget the id once its pin stops being drawn, for the same reason the
  // subscriber layer does: deriving the card is enough to stop a stale one being
  // drawn, but the id would sit in state unclaimed and the card would reappear
  // over a pin the cursor left minutes ago.
  useEffect(() => {
    if (hoverId == null) return
    if (!clusters.some((c) => c.members.length === 1 && c.members[0].id === hoverId))
      setHoverId(null)
  }, [hoverId, clusters])

  // What the device row doesn't carry: the name (and state) of the box above,
  // and — for passive plant — what its recorded drops are doing, the total split
  // down the chain, and whether the OLT holding those readings is down. Resolved
  // here so `devhover.tsx` stays a renderer and knows nothing about queries.
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

  // The hovered SITE, resolved to the cluster its card is built from. Derived
  // rather than stored for the reason `hoverDevice` and `hoverPlace` are: a
  // badge stops being drawn the moment a zoom splits it, and Leaflet fires no
  // mouseout when the marker under the cursor simply unmounts — so a stored card
  // would hang over the tiles describing a site that no longer folds.
  //
  // Suppressed while the map is an INPUT surface (placement, route drawing,
  // dragging pins, the plant menu and its sheets), where the cursor means "put a
  // thing here"; and for the site whose LIST card is already open, which carries
  // the same members with their actions besides.
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

  // …and forget the anchor once no badge holds it, so zooming out and back in
  // can't reopen a card over a site the cursor left minutes ago.
  useEffect(() => {
    if (hoverSiteId == null) return
    if (!clusters.some((c) => c.members.length > 1
        && c.members.some((m) => m.id === hoverSiteId)))
      setHoverSiteId(null)
  }, [hoverSiteId, clusters])

  // What feeds the site FROM OUTSIDE it. Members feeding each other are dropped
  // deliberately: the switch in the same cabinet as the OLT it feeds is already
  // listed as a member, and naming it as the site's uplink would answer "what
  // does this hang off" with something two rows above.
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

  // Every member of the hovered site, so a badge lights the cables into ALL of
  // them. A fold is a presentational accident — the same three boxes unfolded
  // would each light their own feed on hover — so the emphasis has to survive it.
  const hoverLinkIds = useMemo(() => {
    const ids = new Set<number>()
    if (hoverId != null) ids.add(hoverId)
    if (hoverSite) for (const m of hoverSite.members) ids.add(m.id)
    return ids
  }, [hoverId, hoverSite])

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
                 { description: `In the roster on ${hit.where}. Record where it stands from Survey.` })
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
    const armed = (navLocation.state as {
      placeOnu?: { mac: string; label: string } } | null)?.placeOnu
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

  // Focus the layer on one OLT (and optionally a chosen set of its PONs).
  //
  // **THE ZOOM IS NEVER TOUCHED** (operator, 2026-08-06: "if I were to add a
  // filter on OLT, zoom is being adjusted — I don't want that"). This used to
  // `flyToBounds` the whole scoped set, which REVERSES the working order: you
  // pick the zoom that suits the street you are looking at, and a filter is
  // supposed to thin what is drawn there, not re-frame the map under you. It
  // got worse the moment plant joined the filter, since one splitter across
  // town could pull the fit out to the whole district.
  //
  // The one thing the old fit was right about survives: an unframed filter that
  // leaves the screen EMPTY reads as "nothing here" rather than as a filter. So
  // the map PANS — at the current zoom, never a fly, which would arc through
  // other zooms on the way — and only when nothing the focus reveals is on
  // screen at all. Ticking PONs while looking at them therefore never moves
  // anything, which is the case that made this worth changing.
  //
  // The test deliberately excludes the OLT: the focus is usually entered from
  // its own panel, so the OLT is on screen by definition and counting it would
  // make "reveals nothing" unreachable.
  //
  // An EMPTY `pons` is every PON, never none — un-ticking the last one has to
  // land on "the whole OLT", not on a focus that draws nothing and reads as a
  // dark fleet.
  const scopeOnus = useCallback((deviceId: number, pons: string[]) => {
    setOnuScope({ deviceId, pons })
    setSelectedOnuMac(null)
    const pts = places.filter((p) => p.device_id === deviceId
      && (pons.length === 0 || pons.includes(ponKey(p))))
    // What the focus REVEALS: its drops and the plant carrying them. Not the
    // OLT — see above.
    const shown: Array<[number, number]> = pts.map((p) => [p.lat, p.lng])
    for (const id of plantInScope({ deviceId, pons }, devices, byId, pts)) {
      const box = byId.get(id)
      if (box && isPlaced(box)) shown.push([box.lat, box.lng])
    }
    const map = mapRef.current
    if (!map || shown.length === 0) return
    const view = map.getBounds()
    if (shown.some(([lat, lng]) => view.contains(L.latLng(lat, lng)))) return
    map.panTo(L.latLngBounds(shown).getCenter())
  }, [places, devices, byId])

  // Tick one PON on or off. The pins change under the open menu and the
  // VIEWPORT DOES NOT — comparing two PONs means watching one patch of ground
  // gain and lose drops, which a re-frame on every tick actively destroys.
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
    // Past the LINE floor, not the mark floor. A deep link names ONE subscriber,
    // so it should land where everything about that subscriber draws — the drop
    // line to its splitter and the rate chip included — rather than at the zoom
    // where the pin merely starts existing. (Clearing the mark floor was the
    // original point of this; since the two split, +1 on the mark floor would
    // land an operator who asked for one customer at town zoom.)
    const map = mapRef.current
    map?.flyTo([p.lat, p.lng], Math.max(map.getZoom(), detail.drop_lines + 1))
    setFocusFlying(true)
    setSelectedId(null)
    setSelectedOnuMac(p.mac)
  }, [detail.drop_lines])

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
    if (placingId == null && routeEdit == null && placingOnu == null
      && armed == null && !addNext) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPlacingId(null); setRouteEdit(null); setPlacingOnu(null)
        setArmed(null); setAddNext(false); return
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
  }, [placingId, placingOnu, routeEdit, armed, addNext])

  // Open the plant menu over a point. `device` is the pin it was opened ON, and
  // the two cases differ: over a pin the menu offers actions ABOUT that box (and
  // arms creation for the next click), over bare ground it creates HERE.
  //
  // Refused outright while the map is already an input surface. A right-click
  // mid-placement or mid-route would put two modes on one map, and the click
  // that dismissed the menu would land in the other one.
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

  const onMapClick = useCallback((ll: L.LatLng) => {
    // A menu is open: the click that dismisses it must not also do something.
    // Leaflet fires click after the capture-phase mousedown the menu closes on,
    // so without this the first click outside a menu would place a box.
    if (plantMenu != null) { setPlantMenu(null); return }
    if (routeEdit != null) {
      setRouteEdit((re) => re && { ...re, points: [...re.points, [ll.lat, ll.lng]] })
    } else if (armed != null) {
      // "click where it goes", from a pin's menu item or from Save-and-add-
      // another. One click, one record: the coordinate is the click and
      // everything else was decided when the mode was armed.
      if (armed.kind === "customer") {
        setCustomerDraft({ lat: ll.lat, lng: ll.lng, passiveId: armed.parentId })
      } else {
        setPlantDraft({ kind: armed.kind, lat: ll.lat, lng: ll.lng, parentId: armed.parentId })
      }
      setArmed(null)
    } else if (addNext) {
      // The `+` button's click: it opens the MENU rather than deciding for the
      // operator, so the button and the right-click reach exactly the same place.
      setAddNext(false)
      const pt = mapRef.current?.latLngToContainerPoint(ll)
      openPlantMenu(ll.lat, ll.lng, pt?.x ?? 0, pt?.y ?? 0, null)
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
  }, [placingId, placingOnu, refOnus, routeEdit, setLocation,
      armed, addNext, plantMenu, openPlantMenu])

  // A box was recorded. `again` re-arms the map with THIS box as the next
  // feeder — the chain flow — rather than opening its panel, because somebody
  // recording a feeder run is walking it, not reading it.
  const onPlantCreated = useCallback((
    created: { id: number; name: string }, again: boolean,
  ) => {
    const kind = plantDraft?.kind
    setPlantDraft(null)
    if (again && kind) {
      setArmed({ kind, parentId: created.id })
      toast.success(`${created.name} recorded`, {
        description: "Click where the next box goes.",
      })
    } else {
      toast.success(`${created.name} recorded`)
      setSelectedId(created.id)
    }
  }, [plantDraft])

  const onCustomerAttached = useCallback((mac: string) => {
    setCustomerDraft(null)
    // Switch the layer on, or the pin somebody just recorded isn't drawn — the
    // same reason every other placement path does it.
    if (!refOnus) toggleRefOnus()
    setSelectedId(null)
    setSelectedOnuMac(mac)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refOnus])

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
    setZoom(z); setFocusFlying(false)
  }, [])
  // DERIVED, not a second piece of state. It was `setLowZoom(z < 12)` alongside
  // `setZoom(z)`, which is fine while the threshold is a constant and a stale
  // closure the moment it becomes a setting this callback would have to close
  // over. One source for "how zoomed out are we" removes that whole class of bug.
  const lowZoom = zoom < detail.labels
  // Every geographic stroke on this map is scaled by ONE factor (see map/stroke.ts):
  // a fixed-px line has to span more screen the further you zoom in, so it reads
  // as a hairline at street level. Uniform, so the tuned weight RANKING between
  // line kinds survives at every zoom.
  const lineK = lineScale(zoom)

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

  // Un-place: the one write on this panel that DELETES something. Its own
  // success toast rather than one on `setLocation` — that mutation also carries
  // every pin drag and every typed coordinate, and a toast per drag while
  // arranging a site is noise. Says what SURVIVED, like the subscriber panel's
  // "Customer details kept": the row, its topology and its history are all
  // untouched, and it is only the two numbers that are gone.
  const unpinSelected = () => {
    if (!selected) return
    const name = selected.name
    setLocation.mutate({ id: selected.id, lat: null, lng: null }, {
      onSuccess: () => toast.success(`${name} taken off the map. The device is unchanged.`),
    })
    setSelectedId(null)
  }

  // Only links where both ends are DRAWN; a line inherits the child's trouble
  // so a red pin drags a red path back toward its feed. Reading `drawnDevices`
  // rather than `placed` is what makes plant's zoom floor honest — a cable to a
  // splitter that isn't on the map would end in empty ground — and it is one
  // choke point, so the hover probe, the rate chips and the branch-fault overlay
  // all inherit it instead of each re-deriving what is visible.
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
    const placedById = new Map(drawnDevices.map((d) => [d.id, d]))
    const styled = (childId: number, parentId: number) => {
      const s = styleByKey.get(`${childId}:${parentId}`)
      return { childId, parentId, color: s?.color, labelPos: s?.label_pos }
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
  }, [drawnDevices, routeByKey, styleByKey, linkBindings])

  // The geometry each line is actually DRAWN along, resolved once so the render,
  // the hover probe and the label all measure the same path. A drawn route is
  // dropped when a cluster fold would make it snake into a centroid — but the
  // test is whether the fold VISIBLY MOVED the endpoint, not whether the
  // position is bit-identical. Gear racked at one site sits ~1 m apart (three
  // switches at HALIYA are within 1.5 m), so it clusters at every usable zoom
  // while its centroid stays inside its own pin: an exact-equality test
  // suppressed those routes forever and the map drew a chord no zoom could fix.
  // A traced route is DRAWN unless it has nowhere to go — see `foldedTogether`
  // for why the three displacement thresholds that came before it were all
  // answering the wrong question. Endpoints still ride `pinPos`, so a line always
  // meets the pin it belongs to; what a fold costs is a nudge on the first or
  // last segment, never the surveyed path between.
  //
  // An empty waypoint list is NOT a drawn route (a link_routes row survives on a
  // colour or a label position alone). It used to set `drawn: true` for what is
  // geometrically a chord, which suppressed linkhover's "straight-line" note on
  // the one case that most needs it.
  //
  // No longer depends on `zoom` — which is the point. The old rule recomputed
  // per zoom and could therefore CHANGE ITS MIND per zoom, and a map that draws
  // surveyed cable at z16, a straight line at z17 and cable again at z18 teaches
  // an operator not to trust any of it.
  const drawnLinks = useMemo(() => links.map((l) => {
    const from = pinPos.get(l.from.id) ?? [l.from.lat, l.from.lng] as [number, number]
    const to = pinPos.get(l.to.id) ?? [l.to.lat, l.to.lng] as [number, number]
    const wp = l.route
    const drawn = !!wp?.length && !foldedTogether(from, to)
    const pts: Array<[number, number]> = drawn ? [from, ...wp!, to] : [from, to]
    return { ...l, from3: from, to3: to, pts, drawn }
  }), [links, pinPos])

  // WHICH ↓/↑ chips actually render.
  //
  // A chip sits at the operator's saved fraction along its line, or the
  // midpoint — and links CONVERGE on devices, so two cables into one switch put
  // their midpoints within a few pixels of each other and the chips land on top
  // of one another. An overlapping pair is strictly WORSE than a single chip:
  // neither number can be read, and the collision itself reads as a rendering
  // fault rather than as data. Sliding one clear by hand is a per-link,
  // per-org, forever chore, so it was never going to be the answer.
  //
  // Greedy screen-space suppression, ranked (see bwRank): trouble first, then
  // the busiest link. A suppressed chip is not lost — zooming in spreads the
  // midpoints and it returns, which is how every map label engine behaves and
  // is what makes this read as a map rather than a bug. Deliberately measured
  // in PROJECTED PIXELS, not degrees: whether two labels collide is a fact
  // about the screen, and a degree box would collide differently by latitude.
  //
  // ONE reservation covers BOTH chip families — link rates and subscriber rates
  // — because they collide with each other, not just among themselves. A trunk's
  // chip and a drop's chip landing on the same pixels is the same unreadable
  // pair whichever layers they came from, and two independent budgets would each
  // report themselves clear while the screen showed a collision.
  //
  // Order is the priority: every link chip is offered pixels before any
  // subscriber chip, and subscriber NAMES are offered what is left after both.
  // A cable between two boxes carries the whole branch below it; one customer's
  // rate never outranks that, and a name — which the mark's tone, the hover
  // title and the card all still carry — never outranks a reading.
  const chipShown = useMemo(() => {
    const links = new Set<string>()
    const refs = new Set<string>()
    const names = new Set<string>()
    const taken: Array<[number, number]> = []
    // A generous fixed box, not a measurement of each chip's text: measuring
    // would mean laying the icons out to read them back, and an overestimate
    // fails safe — it drops a chip that would just have fitted, and never keeps
    // one that overlaps.
    const fits = (x: number, y: number) =>
      !taken.some(([tx, ty]) => Math.abs(tx - x) < 78 && Math.abs(ty - y) < 20)
    const claim = (x: number, y: number) => { taken.push([x, y]) }

    const cands: Array<{ key: string; x: number; y: number; rank: number }> = []
    for (const l of bwLabels ? drawnLinks : []) {
      if (!l.binding) continue
      // dimming is applied here too, so a chip the render will not draw can't
      // reserve pixels away from one it will
      const emphasized = selectedId != null
        && (l.to.id === selectedId || downstream.has(l.to.id))
      if (troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized)
        continue
      // the ends must be far enough apart on screen that the chip has a line to
      // sit on — zoomed out, the pins (and clusters) own the pixels
      const [ax, ay] = project(l.from3[0], l.from3[1], zoom)
      const [bx, by] = project(l.to3[0], l.to3[1], zoom)
      if (Math.hypot(bx - ax, by - ay) < 90) continue
      const [plat, plng] = linkLabelPos(l.pts, l.labelPos)
      const [x, y] = project(plat, plng, zoom)
      cands.push({ key: l.key, x, y, rank: bwRank(l.binding, l.from.id, l.to.id) })
    }
    cands.sort((a, b) => b.rank - a.rank)
    for (const c of cands) {
      if (!fits(c.x, c.y)) continue
      claim(c.x, c.y)
      links.add(c.key)
    }

    // Subscriber rate chips, second — and ranked EVIDENCE first among
    // themselves. On a surveyed fleet these outnumber link chips a hundred to
    // one and nearly all of them read "idle", so if exactly one can be drawn it
    // must be the one saying a power-backed witness has gone dark. An ordinary
    // offline customer claims no priority: thousands go offline every evening,
    // and letting each one outrank a live reading is the same "everything is an
    // alarm" failure in the budget rather than in the paint.
    const refCands: Array<{ mac: string; x: number; y: number; dark: boolean }> = []
    // gated on the LINE's visibility, not the mark's — a chip rides the line, so
    // below the line floor it must not reserve pixels from a link chip that will
    // actually be drawn
    for (const p of refLinesVisible ? shownPlaces : []) {
      // A drop with no reading draws no chip, so it may not reserve one either
      // — on the GPON builds that is nearly every subscriber, and each of them
      // would go on suppressing a live reading or a name that will draw.
      if (!refHasChip(p)) continue
      const anchor = dropAnchor(p.drop_passive_id, p.device_id, byId)
      if (!anchor) continue
      const to = anchor.device as Placed
      const a = project(to.lat, to.lng, zoom)
      const b = project(p.lat, p.lng, zoom)
      // the span must be long enough for the chip to sit ON the line rather
      // than on top of the pin whose state matters more than its rate
      if (Math.hypot(a[0] - b[0], a[1] - b[1]) < 56) continue
      refCands.push({
        mac: p.mac, dark: isRefEvidence(p),
        x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2,
      })
    }
    refCands.sort((x, y) => Number(y.dark) - Number(x.dark))
    for (const c of refCands) {
      if (!fits(c.x, c.y)) continue
      claim(c.x, c.y)
      refs.add(c.mac)
    }

    // Subscriber NAMES, last. They join this budget rather than starting a
    // third one for the reason the rate chips did: a name and a rate chip
    // collide with each other just as readably as two names do, and two
    // independent budgets would each report themselves clear while the screen
    // showed a smear. Going last is what makes the layer safe to leave on — in
    // a dense area the names thin out on their own instead of burying the
    // readings and the plant underneath them.
    const nameCands: Array<{ mac: string; x: number; y: number; dark: boolean }> = []
    for (const p of refVisible ? shownPlaces : []) {
      // EVIDENCE, not merely dark. Below the name floor only a dark WITNESS is
      // named, the same way `.wisp-map-lowzoom` keeps a device label for a pin
      // in trouble — that is the name somebody is about to phone. An ordinary
      // offline customer waits for the floor like every other subscriber, or a
      // town's evening churn would name itself over the plant. Bounded by
      // refVisible, because a name whose mark isn't drawn floats over nothing.
      const loud = isRefEvidence(p)
      if (!refNamesVisible && !loud) continue
      // A dimmed mark's name must not reserve pixels from one that will be
      // drawn at full strength — the same rule the link pass keeps. This one
      // reads bare darkness on purpose: `troubleOnly` is the operator asking to
      // see problems, and an offline customer IS one. It decides what is DRAWN,
      // not how loud it is.
      if (troubleOnly && !isRefDark(p)) continue
      const [x, y] = project(p.lat, p.lng, zoom)
      nameCands.push({ mac: p.mac, dark: loud, x, y: y + REF_NAME_DY })
    }
    nameCands.sort((x, y) => Number(y.dark) - Number(x.dark))
    for (const c of nameCands) {
      if (!fits(c.x, c.y)) continue
      claim(c.x, c.y)
      names.add(c.mac)
    }
    return { links, refs, names }
  }, [drawnLinks, bwLabels, zoom, troubleOnly, selectedId, downstream,
      refLinesVisible, refNamesVisible, refVisible, shownPlaces, byId])

  // Measuring is a READ, so it stays available to everyone — but not while the
  // map is being used as an input surface: during placement or route drawing the
  // cursor means "put a thing here", and a readout chasing it is noise.
  //
  // Nor while something is SELECTED. A device panel, a site card or a subscriber
  // card means the operator is reading ONE object, and every cable the cursor
  // crosses on the way to that card popped a measurement of a span nobody asked
  // about. Closing the card arms measuring again, so nothing is lost — the two
  // modes just stop competing for the same pointer.
  //
  // Nor while a SUBSCRIBER, a BOX or a SITE is hovered. That card is the operator
  // reading one object too, and the subscriber case is where the two would
  // genuinely collide: a drop line often runs within a fingertip of the cable
  // feeding its splitter, so aiming at a diamond would pop a span measurement
  // underneath the card about the customer. It also keeps the promise the
  // hovered drop line makes by going solid — nothing on this map may quote a
  // length off a span nobody surveyed (see `refonu.ts:REF_HOVER_BOOST`).
  //
  // The device case is the one operators actually complained about, and the
  // `keepOut` ring below is the other half of that fix: this stops the readout
  // once the pointer is ON a pin, and the ring stops it on the way in.
  const hoverEnabled = placingId == null && routeEdit == null && !editPins
    && selectedId == null && siteCluster == null && selectedOnuMac == null
    && hoverPlace == null && hoverDevice == null && hoverSite == null
  const hoverable = useMemo(
    () => (hoverEnabled ? projectLinks(drawnLinks, zoom) : []),
    [drawnLinks, zoom, hoverEnabled])
  // Where the marks are, in the same projected pixels the probe works in — one
  // point per SITE, so a folded badge is one keep-out rather than none (its
  // members are not drawn) or five stacked on a pixel.
  const hoverKeepOut = useMemo(() => {
    if (!hoverEnabled) return []
    return clusters.map((c) => {
      const [lat, lng] = c.members.length === 1
        ? [c.members[0].lat, c.members[0].lng] : c.center
      return project(lat, lng, zoom)
    })
  }, [clusters, zoom, hoverEnabled])
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
  // `open` must match when a Card actually RENDERS (route editing replaces it),
  // or the chrome makes room for a panel that isn't there. BOTH right-rail cards
  // count: a subscriber opens in the same rail at the same width, and counting
  // only the device panel left the top strip claiming width that was under it.
  const railOpen = (!!selected || !!selectedRef) && !routeEdit
  // Its own stored width and a tighter ceiling than the Network page's: this
  // panel floats over the map it's read against, and past ~halfway it stops
  // being a panel on a map and becomes a map behind a panel.
  const panel = useResizablePanel({
    storageKey: "wisp:map:panelw", defaultWidth: 380, min: 320, max: 620,
    open: railOpen,
  })

  return (
    // header is h-14 (3.5rem); the mobile tab bar overlays the bottom ~4rem.
    //
    // `--wisp-pane-h` is the SPLIT-VIEW override and is unset everywhere else,
    // so the fallback below is the height this page has always had. A map is the
    // one page here that must fill its box exactly rather than scroll, so it is
    // also the one page that cannot be handed a viewport measurement when it is
    // only getting half the viewport.
    <div ref={wrapRef} style={panel.vars} className={cn(
      "wisp-map-wrap relative h-[var(--wisp-pane-h,calc(100svh-3.5rem-4rem))] md:h-[var(--wisp-pane-h,calc(100svh-3.5rem))]",
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
        // Zooming is how you ask this map a question — "is that one site or
        // three", "which PON is that" — so it has to land WHERE YOU MEANT, and
        // Leaflet's defaults make that hard: one wheel notch is a whole zoom
        // level, i.e. a 2x jump in scale, and the level you want is routinely
        // between two of them. Quarter steps (and a wheel that needs twice the
        // travel per level) turn a coarse ratchet into something you can aim.
        //
        // Everything downstream already tolerates a fractional zoom: `project`
        // is 2**zoom, the cluster fold and both chip budgets are computed in
        // projected pixels, and every threshold here is a `>=` comparison.
        zoomSnap={0.25}
        wheelPxPerZoomLevel={120}
        worldCopyJump
      >
        <AttributionPrefix />
        <InvalidateOnResize />
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
        <MapEvents org={scopeOrg} onMapClick={onMapClick} onZoom={onZoom}
          onMapContext={onMapContext}
          onMoved={() => setPlantMenu(null)} />
        <ViewController placed={placed} ready={!isLoading && orgsQ.isSuccess}
          hasSavedView={!!initialView} bounds={region.bounds} />
        <LinkHoverProbe projected={hoverable} enabled={hoverEnabled}
          zoom={zoom} keepOut={hoverKeepOut} onHover={setHover} />
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
          const { pts } = l
          // ↓/↑ chip riding the line. Every condition — a bound port, enough
          // screen span to sit on, dimming, and not being crowded out by a
          // higher-ranked chip — is resolved once in `bwShown`, because whether
          // THIS chip renders depends on the others.
          const labeled = chipShown.links.has(l.key)
          // a cable INTO the box under the cursor. Direct links only — hover is
          // a peek, not a selection, and lighting a whole subtree on mouseover
          // would make the map twitch as the pointer crosses a dense site.
          const hovered = !dimmed
            && (hoverLinkIds.has(l.to.id) || hoverLinkIds.has(l.from.id))
          // a cross-link carries no dependency, so it stays visually quieter
          // than any feed — thinner, and it never thickens on emphasis (a
          // selected switch lights its PATH, not its siblings). A HOVER adds
          // half a pixel where emphasis adds a full one: enough to pick the
          // cable out of a bundle, not enough to be mistaken for a selection.
          // FEEDER vs DISTRIBUTION. Every primary link used to draw at one
          // weight, so the cable leaving an OLT — which carries the entire PON —
          // and a cable between two splitters four hops down the cascade were
          // the same object on screen. On a fleet whose plant is a cascade that
          // is the one hierarchy the map was not expressing: you could see WHERE
          // the cables ran but not which of them mattered.
          //
          // The tier is derived from the topology, never stored: a primary link
          // with a PASSIVE at both ends is distribution; anything with gear at
          // the parent end is a feeder or the backbone. So it needs no schema, no
          // operator input and no migration, and re-parenting a splitter re-ranks
          // its cable automatically — the same rule `assignment.py` follows for
          // responsibility and for the same reason.
          //
          // It thins the SUBORDINATE line rather than thickening the feeder,
          // which keeps the whole existing ladder (peer 2 < feed 2.5 < destructive
          // 3 < emphasized 3.5) exactly where it was and where it was judged. And
          // the tier sits BELOW both status rungs deliberately: a distribution
          // cable in trouble still draws at 3, because on this map status outranks
          // structure everywhere else and a thin red line would be the one place
          // it didn't.
          const distribution = l.kind === "primary"
            && isPassiveType(l.from.device_type) && isPassiveType(l.to.device_type)
          const weight = (l.kind === "peer" ? 2
            : emphasized ? 3.5
            : l.tone === "destructive" ? 3
            : distribution ? 2.1 : 2.5)
            + (hovered && !emphasized ? 0.75 : 0)
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
                    color: "#000", opacity: CASING_OPACITY,
                    ...casingAt(lineK, weight, casingOver, dashArray),
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
                  opacity: dimmed ? 0.12 : hovered || emphasized ? 1
                    : l.kind === "peer" ? 0.85 : l.tone === "muted" ? 0.85 : 0.9,
                  ...strokeAt(lineK, weight, dashArray),
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
            circle — shade the area so the eye reads "feeder", not "fiber".

            THE RADIUS IS THE MEASURED EXTENT AND NOTHING ELSE. It used to carry
            a 400 m floor so a zero-extent wave still drew something, and that is
            exactly backwards: a rack whose four feeds all went dark sits on ONE
            pin, extent 0.0 km, and the floor shaded 800 m of a village nobody
            had measured — a claim about the ground invented to satisfy a
            legibility want. Same error as the ONU spokes that were removed for
            fabricating a bearing EPON ranging cannot give. On this map
            everything reads as geography.

            So a point incident draws NO hull, and loses nothing: its site badge
            is already ringed red, and the strip's verdict says "one site" in
            words, which is the honest form of the thing this circle was
            attempting. A wave with real spread still gets its area, padded 15%
            so the pins sit inside the line rather than on it. */}
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
        {/* suspected-cut stretch: louder than any link (thick, dashed), and the
            ✕ is clickable — it opens the OLT's Optical tab with the verdict */}
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
                pathOptions={{ color: "var(--primary)", opacity: 0.9,
                  ...strokeAt(lineK, 2.5, "6 6") }}
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
                eventHandlers={{
                  click: () => onClusterClick(c),
                  // A badge is the one mark here that HIDES what it stands for,
                  // so hovering it has more to answer than a pin does: the card
                  // names the members and their states, and every cable into any
                  // of them lights up. Anchored on a member id, not the cluster
                  // key — a zoom that reshuffles membership must not drop a card
                  // still under the pointer.
                  mouseover: () => setHoverSiteId(c.members[0].id),
                  // Guarded rather than a bare clear, like the subscriber marks:
                  // with badges close together the pointer can enter the next one
                  // before this one's mouseout lands.
                  mouseout: () => setHoverSiteId((h) =>
                    (c.members.some((m) => m.id === h) ? null : h)),
                }}
                zIndexOffset={sel ? 1000 : anyDown ? 500 : 100}
              />
            )
          }
          const d = c.members[0]
          const dim = troubleOnly && !isTrouble(d) && d.id !== selectedId
          const impact = downstream.has(d.id)
          // Passive plant writes its SPLIT RATIO on the plate in place of its
          // name, and takes its tone from what its recorded subscribers are
          // doing. Gear is unchanged — a switch has a state of its own and a
          // name worth reading.
          const passive = isPassiveType(d.device_type)
          const load = passive ? loadByPassive.get(d.id) : undefined
          // …and it stands down behind a DOWN OLT, which darkens every ONU
          // under it: the tone would be that outage restated on a box with no
          // outage of its own. The hover card says the same thing in words.
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
                // Right-clicking a BOX asks about that box: hang a splitter off
                // it, record a customer on it, open it. Leaflet stamps the
                // container point from the marker's own position, so the menu
                // opens on the pin rather than wherever the pointer happened to
                // be inside it.
                contextmenu: (e) => {
                  const p = (e as L.LeafletMouseEvent).containerPoint
                  openPlantMenu(d.lat, d.lng, p.x, p.y, d)
                },
                click: (e) => {
                  if (plantMenu != null) { setPlantMenu(null); return }
                  if (routeEdit != null) return
                  // `+` armed: a pin click asks the same question a right-click
                  // on it would, or the button and the right-click would reach
                  // different places on the same mark.
                  if (addNext) {
                    setAddNext(false)
                    const p = (e as L.LeafletMouseEvent).containerPoint
                    openPlantMenu(d.lat, d.lng, p.x, p.y, d)
                    return
                  }
                  // A pin click while a record is armed means "at that site" —
                  // the splitter on the same pole as the OLT is a real case.
                  if (armed != null) {
                    if (armed.kind === "customer") {
                      setCustomerDraft({ lat: d.lat, lng: d.lng, passiveId: armed.parentId })
                    } else {
                      setPlantDraft({ kind: armed.kind, lat: d.lat, lng: d.lng,
                                      parentId: armed.parentId })
                    }
                    setArmed(null)
                    return
                  }
                  // Placing a reference ONU onto a device pin means "at that
                  // site" — the common real case, since the subscriber whose
                  // power is reliable is often the tower the gear is on.
                  if (placingOnu != null) {
                    setOnuPlace.mutate({ mac: placingOnu.mac, lat: d.lat, lng: d.lng,
                                         label: placingOnu.label || null })
                    if (!refOnus) toggleRefOnus()
                    toast.success(`Placed at ${d.name}`)
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
          // HOVER BYPASSES THE LINE FLOOR. `refLinesVisible` exists to stop a
          // few dozen dotted spans with their black casings smearing into a
          // smudge around every splitter at low zoom — that is an argument
          // about MASS, not about one. A single line, drawn because the cursor
          // is on its diamond, is the answer to "what does this hang off"
          // rather than the noise the floor was raised against, and at zooms
          // 14–15 (marks drawn, lines not) it is the only way to get that
          // answer without clicking.
          const hovered = p.mac === hoverOnuMac
          if (!refLinesVisible && !hovered) return null
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
          //
          // …and the HOVERED one goes solid, heavier and to full strength. That
          // is a deliberate exception to the dotted rule and the argument for it
          // lives with the rule, at `refonu.ts:REF_HOVER_BOOST`: it is
          // pointer-bound, one line at a time, and the card that comes with it
          // states in words what the span is. It keeps its TONE either way — a
          // hover may make a line findable, never make it look healthy.
          const refWeight = (tone === "dark" ? 4.5 : viaSplitter ? 3.5 : 2.5)
            + (hovered ? REF_HOVER_BOOST : 0)
          const refDash = hovered ? undefined : viaSplitter ? DROP_DASH : REF_DASH
          // Two gates and they answer different questions: the budget says
          // whether there are pixels for a chip, `refBwIcon` whether there is
          // anything to write in one. Hoisted because it may legitimately be
          // null, which is what keeps an ungated future caller a type error
          // rather than a blank pill on a line.
          const bwIcon = chipShown.refs.has(p.mac) ? refBwIcon(p) : null
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
                  // Darker under a hovered line: it is the one span meant to be
                  // findable across a viewport, and a solid stroke over bright
                  // fields needs more backing than a dot does.
                  color: "#000",
                  opacity: hovered ? CASING_OPACITY_HOVER : CASING_OPACITY,
                  lineCap: "round",
                  ...casingAt(lineK, refWeight, CASING_OVER_FINE, refDash),
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
                  // The hover emphasis is WEIGHT + SOLIDITY + full opacity, and
                  // pointedly not a colour of its own: tone on this map is a
                  // claim about the network, and a hover is a claim about the
                  // pointer. Applied through pathOptions rather than a class,
                  // because `className` is a mount-time prop on a Leaflet path
                  // (setStyle drops it) while these go through setStyle cleanly.
                  opacity: hovered ? 1
                    : tone === "dark" ? 0.95 : viaSplitter ? 0.9 : 0.75,
                  // Round caps so the dashes render as DOTS rather than stubby
                  // bars — the line has to keep reading as "logical
                  // association", never as traced fibre a crew could quote drum
                  // off. `strokeAt` scales the dash period with the weight for
                  // exactly that reason: a wider stroke on the unscaled gaps
                  // would close them into a solid line. A real drop gets the
                  // tighter dash and the OLT fallback the sparser one, and
                  // scaling both by the same factor keeps them tellable apart.
                  lineCap: "round",
                  ...strokeAt(lineK, refWeight, refDash),
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
              {bwIcon && (
                <Marker
                  position={[(to.lat + p.lat) / 2, (to.lng + p.lng) / 2]}
                  icon={bwIcon}
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
                  opacity: 0.3, lineCap: "round",
                  // a power branch is a hypothesis about the grid, not a cut —
                  // dashed so it never reads as "send the splicing crew here"
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
        {refVisible && shownPlaces.map((p) => (
          <Marker
            key={`ref:${p.mac}`}
            position={[p.lat, p.lng]}
            icon={refOnuIcon(p, {
              selected: p.mac === selectedOnuMac,
              dim: troubleOnly && !isRefDark(p),
            })}
            zIndexOffset={refZIndex(p, p.mac === selectedOnuMac,
                                    p.mac === hoverOnuMac)}
            eventHandlers={{
              click: () => {
                if (routeEdit != null || placingId != null || placingOnu != null) return
                setSelectedOnuMac(p.mac === selectedOnuMac ? null : p.mac)
                setSelectedId(null)
              },
              // Not armed while the map is an INPUT surface: during placement
              // or route drawing the cursor means "put a thing here", and a
              // card chasing it is the same noise the distance readout stands
              // down for.
              mouseover: () => {
                if (routeEdit != null || placingId != null || placingOnu != null) return
                setHoverOnuMac(p.mac)
              },
              // Guarded rather than a bare clear: with marks a few pixels apart
              // the pointer can enter the next diamond before this one's
              // mouseout lands, and an unguarded clear would wipe the card that
              // just opened.
              mouseout: () => setHoverOnuMac((m) => (m === p.mac ? null : m)),
            }}
          />
        ))}
        {/* …and the card it opens. One at a time, anchored on its own pin.
            Suppressed for the SELECTED subscriber: its full card is already
            open at the top-left with the same facts and the actions besides,
            and two cards about one customer is the drill-down disagreeing with
            itself. */}
        {hoverPlace && (
          <RefHoverCard place={hoverPlace} ctx={hoverCtx(hoverPlace)} />
        )}
        {/* The same card for a BOX. It renders here, after the subscriber
            layer, only so both cards live in one place — they are mutually
            exclusive by construction (see `hoverDevice`), and the frame stacks
            either of them above every mark. */}
        {hoverDevice && (
          <DevHoverCard device={hoverDevice} ctx={devHoverCtx(hoverDevice)} />
        )}
        {/* …and for a folded SITE. Mutually exclusive with the other two by
            construction: a device is either its own pin or a member of a badge,
            never both, and a hovered subscriber suppresses this one outright. */}
        {hoverSite && (
          <SiteHoverCard cluster={hoverSite} ctx={siteHoverCtx(hoverSite)} />
        )}
        {/* The customer name, in its own marker so the diamond's html string
            stays stable — folding it into the mark would remount every pin
            (and replay its fade-in) each time panning changed the budget.
            Non-interactive: the click target is the mark, and a name plate
            that swallowed it would make a subscriber harder to open than
            before it was labelled. */}
        {refVisible && shownPlaces.map((p) => {
          if (!chipShown.names.has(p.mac)) return false
          // A DOWN OLT freezes every SNMP reading behind it — the rows persist,
          // so the last walked dBm would go on printing as if it were now, up
          // to 15 minutes before the staleness gate would catch it. `isDownState`
          // is the trigger, not `isFresh`, for exactly that reason. The name
          // still draws; only the reading drops.
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
        {/* Field workers — where the crew is, from the tracker on each phone.
            Subordinate exactly like the subscriber layer: opt-in, its own mark
            (a badge of initials — nothing else here carries text), stacked
            BELOW every device pin, out of the clustering pass (a site badge
            mixing staff with plant would count nonsense), and every element
            non-interactive so none of it can swallow a placement click.

            Only workers with a fix are drawn. "Set up but never reported" has
            no coordinates by definition — it is a COUNT, stated on the layer
            toggle, because a crew whose phones were never provisioned must not
            render identically to a crew that has all gone home. */}
        {showWorkers && fieldWorkers.map((w) => {
          if (!workerPlaced(w)) return null
          const state = workerState(w, workerFreshS, now)
          const fix = w.last_fix!
          const trail = w.trail.length >= 2 ? w.trail : null
          const style = trailStyle(state)
          return (
            <Fragment key={`worker:${w.user_id}`}>
              {/* Today's route. SOLID, unlike every other subordinate line
                  here: a dash on this map means "not a surveyed path", and a
                  GPS trail is the one line on the screen that IS measured.
                  Lighter than any plant line at every state, because it is
                  history rather than a claim about now.

                  CASED, like every other line on this map, and for the reason
                  the ref-ONU lines had to learn twice: satellite runs from
                  near-white over fields to near-black over water inside one
                  viewport, and a casing-less stroke at this weight simply
                  vanishes over half of it. Verified on real imagery — the
                  uncased first cut was invisible. */}
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
      {/* z-[1002], above every floating card on this map (all z-1000) AND above
          the control row (1001): the unplaced drawer, the site card and the
          subscriber card all open at `top-14 left-3`, which is exactly where
          this strip WRAPS to once the focus bar joins it — and a z-index tie is
          broken by DOM order, so those cards were burying the search results and
          the PON chips. A transient list covering a status bar is the wrong way
          round. The ladder on this map, bottom-up: cards 1000 · controls 1001 ·
          this strip 1002 · the plant menu 1003.

          EVERYTHING that states what is on the map goes IN here, in the wrap
          flow — never as a second absolutely-positioned bar in the same band
          (operator, 2026-08-07: "something seems to be behind the elements and
          i can't read it"). The power-outage verdict was its own `top-3 left-1/2`
          pill at z-1000: dead centre of the band this strip already occupies,
          one rung BELOW it, so the moment the strip grew past halfway the
          verdict slid underneath the buttons and read as torn fragments of a
          sentence. Centring is not a layout — it is a bet that the left side
          stays short. It also escaped `.wisp-panel-strip`, so it could run under
          an open device panel as well. In the flow it wraps instead, which is
          the one behaviour that cannot collide at any width.

          MODE banners (placement, route drawing) stay centred at `top-14`: they
          are what the next CLICK will do, not what the map is showing, and only
          one is ever up. */}
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
        {/* power-pattern verdict: the read a veteran gets off the wall — many
            feeds, one small circle. It sits right after the down count because
            it EXPLAINS that count; it explains the red, never silences it. */}
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
              {/* A wave whose members all sit on ONE pin has no area, and
                  "0.0 km area" printed a measurement of nothing. Say which of
                  the two it is: a rack that went dark is a site, not a
                  neighbourhood, and it is also why no hull is drawn for it. */}
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
                  {/* Plant is filtered with the drops now, so the bar says how
                    much of it survived. Same reason the count beside it exists:
                    a map hiding the rest of the org's splitters has to say what
                    it is showing, or a village with no pin reads as a village
                    with no plant recorded. Silent at zero, like the dark count. */}
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
                        {/* ALWAYS the located count (operator's ask, 2026-08-06).
                          The dark count used to take this cell whenever one drop
                          was down, so the one thing the chip exists to say — how
                          many customers sit on this PON — vanished the moment
                          anything went wrong, and a bare "1" read as a PON with
                          one subscriber. Dark is on the map itself (the pins and
                          their lines) and in the tooltip; it does not get to
                          overwrite a different number. */}
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

      {/* plant capture banners -------------------------------------------------
          Same pill as every other map mode, for the same reason: a map that has
          quietly become an input surface must SAY so, or the next click means
          something the operator didn't intend. */}
      {(armed || addNext) && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(92vw,34rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur dark:bg-popover/95">
          <Crosshair className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            {addNext ? "Click the map to add something here" : armed?.kind === "customer" ? (
              <>Click where the customer stands
                {armed.parentId != null && byId.get(armed.parentId) && (
                  <> · on <span className="font-medium">{byId.get(armed.parentId)!.name}</span></>
                )}
              </>
            ) : (
              <>Click where the {armed ? PLANT_LABEL[armed.kind as PlantKind] : "box"} goes
                {armed?.parentId != null && byId.get(armed.parentId) && (
                  <> · below <span className="font-medium">{byId.get(armed.parentId)!.name}</span></>
                )}
              </>
            )}
          </span>
          <Button variant="ghost" size="icon" className="size-5 shrink-0" title="Cancel (Esc)"
            onClick={() => { setArmed(null); setAddNext(false) }}>
            <X className="size-3" />
          </Button>
        </div>
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

      {/* subscriber placement banner -------------------------------------------
          It no longer restates the power contract, because there is no longer a
          claim to warn about: placement is a location and the claim is its own
          toggle. The warning rode EVERY placement before, including plain moves
          that asserted nothing — a warning shown where it doesn't apply is how
          the one that matters stops being read. */}
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
            title="Straighten · drop every point, back to a straight line"
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

      {/* controls — they DOCK ABOVE THE PANEL rather than sliding out of its way
          (operator, 2026-08-06: "the options bar is on the left to olt panel").
          They used to slide left by the panel's width, which works only while
          there is map left over: at 605px of panel on a 1044px map the column
          landed in the 12px gutter between the two, floating over the tiles,
          attached to nothing and 44px higher than the panel it belonged to.
          A right-rail card already starts at `top-14`, so the same `right-3`
          anchor laid out as a ROW fills the band above it exactly — one column
          of chrome, one right edge, and every pixel of map left of the panel
          stays map. Vertical stays the layout with nothing open (there is no
          panel to align to, and a column spends less of the top band), and
          below `md` the panel is a bottom sheet, so the column is correct
          there whatever is open.

          z-1001, ONE rung over the panel, because the Layers legend opens
          leftward out of this stack and the row now sits above the panel rather
          than beside it — at z-1000 it tied with the panel and lost on DOM
          order, so all but its first row rendered BEHIND the panel. The buttons
          themselves never overlap the panel (they stop 12px above it), so the
          rung buys the popover and changes nothing else; the top strip stays
          above both at 1002. ---------------------------------------------- */}
      <div className={cn("absolute top-3 right-3 z-[1001] flex flex-col items-end gap-1.5",
        railOpen && "md:flex-row md:items-center")}>
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
                title="Subscriber ONUs with a location: field-survey pins plus the power-backed reference points you've placed. Click an OLT to focus on just its drops and the plant feeding them."
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
                  onuScope != null || (refOnus && zoom >= detail.subscribers)
                    ? "text-success" : "text-muted-foreground")}>
                  {onuScope != null ? "focused"
                    : !refOnus ? "off"
                      : zoom >= detail.subscribers ? "on" : "on · zoom in"}
                </span>
              </button>
              {/* Where the crew is. Owner-only, so a worker session never sees
                  the entry at all. */}
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
              {/* The census, spelled out rather than left to be inferred from
                  how many badges happen to be on screen. Four states and only
                  three of them draw anything, so an empty layer has to be able
                  to say WHICH kind of empty it is: nobody on shift, or nobody
                  set up. Same rule as the splitter layer's "N of M mapped". */}
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
                [<span key="s" className="size-2 rotate-45 rounded-[1px] bg-muted-foreground/60" />, "Splitter (passive)"],
                [<span key="s" className="flex size-3.5 items-center justify-center rounded-full border border-warning">
                  <span className="size-2 rounded-full bg-muted-foreground" />
                </span>, "Weak ONUs (ring)"],
                // A rounded square carrying initials — the one mark here with
                // text in it, which is what keeps a person from reading as
                // plant. Listed only for owners: the layer is owner-only, and a
                // legend entry for a mark you can never turn on is noise.
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
        {/* The visible twin of the right-click. A context menu is the fast path
            and an undiscoverable one — nobody finds it without being told — so
            the same menu has a button, and pressing it arms the next click to
            open it. Deliberately NOT a dropdown of its own: a menu here would
            have to decide the coordinate for you, and the coordinate is most of
            what is being recorded. */}
        {canWrite && (
          <Button variant={addNext ? "default" : "outline"} size="icon"
            className={cn("size-8 backdrop-blur", !addNext && "bg-popover/95 dark:bg-popover/95")}
            title="Add a splitter or a customer (or right-click the map)"
            onClick={() => { setAddNext((v) => !v); setEditPins(false) }}>
            <Plus className="size-3.5" />
          </Button>
        )}
        {/* the hint FLOWS under the buttons instead of an absolute top-[10rem]:
            the stack's height depends on which controls render, and the hard
            offset landed on top of the very Pencil button you click to leave
            edit mode. Inside the stack it can never overlap one. In row mode it
            goes FIRST, i.e. out to the left over the map: last would put a
            transient hint between the buttons and the panel edge they are
            aligned to, sliding the whole set sideways when edit mode opens. */}
        {editPins && canWrite && (
          <div className={cn("pointer-events-none rounded-lg border border-warning/40 bg-popover/95 px-2.5 py-1.5 text-2xs text-warning backdrop-blur dark:bg-popover/95",
            railOpen && "md:order-first")}>
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
      {/* SUBSCRIBER PANEL — in the RIGHT RAIL, where the device panel opens.
          Not a floating card any more, and that is the whole point of this pass.
          A subscriber is an object of the same weight as a device — it has an
          identity, a place in the topology, live readings and a history — so it
          gets the surface a device gets, rendered by the SAME component every
          other screen opens (`subscriber-detail.tsx`). The 288px card that used
          to sit at top-14 left-3 was the richest of six partial views of a
          customer, and being the richest is what made "where do I look?" a real
          question. The transient hover card stays: that is a glance, this is
          the act. */}
      {selectedRef && !routeEdit && (
        <Card className="wisp-device-panel absolute inset-x-2 bottom-2 z-[1000] flex max-h-[55%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-14 md:right-3 md:bottom-auto md:max-h-[calc(100%-4.5rem)]">
          <PanelResizeGrip grip={panel.grip} />
          <SubscriberDetail
            mac={selectedRef.mac}
            actions={{
              onClose: () => setSelectedOnuMac(null),
              // Placement stays a MAP action, so it is only offered here.
              onPlace: (mac, label) => {
                setPlacingOnu({ mac, label })
                setSelectedOnuMac(null)
              },
              // Stays on the map: the same device panel every pin opens, on the
              // Optical tab with this ONU's row focused. Leaving for /topology
              // threw away the map the operator was reading it against.
              onOpenOlt: (deviceId, mac) => {
                setDetailTab("optical")
                setDetailOnu({ deviceId, mac })
                setSelectedId(deviceId)
                setSelectedOnuMac(null)
              },
              onOpenPassive: (deviceId) => {
                setSelectedId(deviceId)
                setSelectedOnuMac(null)
              },
            }} />
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
            {/* NOTHING DESTRUCTIVE LIVES BESIDE CLOSE (2026-08-06, operator:
                "what is that location mark left of close button"). Un-placing a
                device used to sit here — a bare `MapPin` in the same muted ghost
                as its neighbours, 2px from the X, no confirm and no toast, and
                the coordinates are not recoverable (nothing keeps a history, and
                a field-surveyed box loses its GPS provenance with them). The
                glyph also said "location", not "delete location". It moved down
                to the coords row's EDIT group, where every other write to this
                pin already is. Keep this header read-only: navigate and close. */}
            <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
              title="Show in the Network tree"
              onClick={() => navigate("/topology", { state: { deviceId: selected.id } })}>
              <ListTree className="size-3.5" />
            </Button>
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
                  {/* Un-place. It ends the row rather than the header for the
                      reason given up there, and it is the only DESTRUCTIVE thing
                      in this group — so it says so three ways the old one didn't:
                      `MapPinOff` (the glyph names the action instead of naming a
                      pin), a confirm naming the device, and a toast on the way
                      out. Same discipline the subscriber panel's Unpin already
                      had; the device panel was the odd one out. */}
                  {canWrite && isPlaced(selected) && (
                    <Button variant="ghost" size="icon"
                      className="size-7 text-muted-foreground hover:text-destructive"
                      title="Remove this pin from the map"
                      onClick={confirmUnpin.ask}>
                      <MapPinOff className="size-3.5" />
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
                          The map holds still underneath as you go — see
                          `scopeOnus` for why the fit that used to happen here
                          was the wrong half of a filter. */}
                      {pons.map((p) => (
                        <DropdownMenuCheckboxItem key={p.pon}
                          checked={focused && picked.includes(p.pon)}
                          title={`${p.total} located on ${p.pon}`
                            + (p.dark > 0 ? ` · ${p.dark} dark` : "")}
                          onSelect={(e) => e.preventDefault()}
                          onCheckedChange={() => toggleScopePon(selected.id, p.pon)}>
                          <span className="min-w-0 truncate font-mono">{p.pon}</span>
                          {/* ALWAYS the located count (operator's ask,
                              2026-08-06) — see the status-strip chip for why the
                              dark count may not take a cell that means something
                              else. It stays on the hover title. */}
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
          {/* overscroll-contain stops scroll CHAINING out of the panel (the Card
              carries it too, via .wisp-device-panel, since the header and the
              coords/cable rows sit outside this scroller). */}
          <div className="overflow-y-auto overscroll-contain p-3">
            <DeviceDetail device={selected} tab={detailTab}
              onTab={(t) => { setDetailTab(t); setDetailOnu(null) }}
              focusOnuMac={detailOnu?.deviceId === selected.id ? detailOnu.mac : null} />
          </div>
          {/* Named, so the dialog can't be answered without reading WHICH box it
              is about — this panel is opened by clicking pins, and the last one
              clicked is not always the one in mind. No `requireText`: that bar is
              for a delete with no backup (an org), and typing on a routine action
              trains people to type without reading. The description says what
              goes and what stays, since "remove from the map" could be read as
              deleting the device. */}
          <ConfirmDialog {...confirmUnpin.props}
            title={`Take ${selected.name} off the map?`}
            description="The coordinates are deleted and nothing keeps a copy, so a pin placed in the field loses its GPS accuracy too. The device, its topology and its history are untouched."
            confirmLabel="Take off the map"
            onConfirm={unpinSelected} />
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

      {/* Recording plant and customers. The menu lives INSIDE the map wrapper
          (its coordinates are container px), the two sheets are Radix dialogs
          and portal to the body — which is exactly why `.wisp-map-wrap` isolates
          its stacking context: Leaflet's own panes reach z-1000 and would
          otherwise beat every portal in the app. */}
      {plantMenu && canWrite && (
        <PlantMenu
          anchor={plantMenu}
          feeder={menuFeeder}
          dropOn={menuDropOn}
          width={wrapRef.current?.clientWidth ?? 0}
          height={wrapRef.current?.clientHeight ?? 0}
          onClose={() => setPlantMenu(null)}
          onPlant={(kind, parentId) => {
            setPlantDraft({ kind, lat: plantMenu.lat, lng: plantMenu.lng, parentId })
            setPlantMenu(null)
          }}
          onArm={(kind, parentId) => {
            setArmed({ kind, parentId })
            setPlantMenu(null)
            setSelectedId(null)
          }}
          onCustomer={(passiveId) => {
            setCustomerDraft({ lat: plantMenu.lat, lng: plantMenu.lng, passiveId })
            setPlantMenu(null)
          }}
          onOpenDevice={(d) => {
            setDetailTab(deviceTabs(d)[0])
            setSelectedId(d.id)
            setPlantMenu(null)
          }}
        />
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
