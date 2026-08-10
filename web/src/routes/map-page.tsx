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
  ArrowLeft, Check, ChevronDown, ChevronRight, Copy, Crosshair,
  Expand, EyeOff, Layers, ListTree, LocateFixed, MapPin, MapPinOff, Maximize2, Navigation,
  Pencil, Plus, Route, Scissors, Shrink, Slash, Spline, Trash2, Undo2, Users, Waypoints, X,
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
import { CableForm, CableList, CablePanel } from "@/components/cable-record"
import { CouplerTray } from "@/components/coupler-tray"
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
import { durationSince, onuName } from "@/lib/format"
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
import { LinkHoverProbe, hoverIcon, projectLinks, type LinkHover } from "@/map/linkhover"
import { bindLinkPorts, bwRank, linkBwIcon, linkKey, linkLabelPos, type LinkBinding } from "@/map/linklabel"
import {
  isDownState, isPlaced, isTrouble, meIcon, pinIcon, pinTone, vertexIcon, type Placed,
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

/** The dash for a link with NO cable recorded — "these two are joined, nobody
 *  has said by what".
 *
 *  A FIFTH member of a family whose four periods are already argued over, so it
 *  is picked against them: drop "1 7" (8px), peer "1.5 7" (8.5), ref "1 10"
 *  (11), backup "5 8" (13). This sits at a 9px period with a 3px ON — read as
 *  medium DASHES where drop/peer/ref are dots, and clearly shorter than the
 *  backup's long dash. `strokeAt` scales it with the weight, which is not
 *  optional: an unscaled dash closes into a SOLID line as the stroke widens,
 *  and solid on this map means surveyed cable a crew quotes drum against. */
const UNCABLED_DASH = "3 6"

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
/** What the route editor is currently tracing.
 *
 *  The kinds carry the SAME waypoint list and differ only in what addresses
 *  the row it saves into — a device pair for a cable between boxes, a MAC for a
 *  subscriber's drop (`onu_drops` is keyed on the sticker for the same reason
 *  every subscriber-side record here is: `onu_optics` never deletes a vacated
 *  slot, so a slot key rots the moment an ONU is re-registered), a cable id for
 *  the street itself.
 *
 *  ONE editor, two kinds of span. A second would be two sets of gestures to keep
 *  in step, and the day they drifted this map would teach two ways to trace one
 *  network.
 *
 *  `cable` is the odd one and the reason the union earns its keep: it has NO
 *  ANCHORS. A link is drawn between two pins and a drop between a splitter and
 *  an ONU, so both lists are INTERMEDIATE vertices and the ends move with the
 *  boxes. A cable ends wherever the glass ends — routinely mid-street with
 *  nothing recorded there — so its list is the whole route, first vertex to
 *  last. Everything downstream of `anchors` keys off that difference. */
type RouteEdit = { points: Array<[number, number]> } & (
  | { kind: "drop"; mac: string }
  /** `cableId` null = a cable that does not exist yet: the route is drawn FIRST
   *  and the sheath is named on save. That order is the point — a cable is a
   *  thing in the ground, so drawing it is how you say it exists, and the four
   *  hand-made cables on the live fleet with nothing attached to them are what
   *  asking for a name first produced. */
  | { kind: "cable"; cableId: number | null; name: string
      /** WHAT THE TWO ENDS LANDED ON, recorded as they are clicked rather than
       *  worked out afterwards from coordinates. A cable's ends are the whole of
       *  what this model added, so they may not be inferred: two boxes racked at
       *  one point would be indistinguishable, and "near enough" is exactly the
       *  kind of threshold this map has already been wrong about three times.
       *
       *  Null means the click landed on open ground, and a coupler is created
       *  there on save. That is what makes "a cable runs between two couplers"
       *  true BY CONSTRUCTION instead of by rule — the operator draws a line and
       *  the closures appear, which is the order the work happens in. */
      endA?: FibreEnd | null; endB?: FibreEnd | null }
)

/** One end of a cable being drawn: the point it snapped onto. */
type FibreEnd = { device_id?: number; mac?: string; name: string }

/** THE PIN A CLICK LANDED ON, in SCREEN space, or null for open ground.
 *
 *  Screen space and not ground distance, deliberately: this is an affordance
 *  about the cursor — the same 24 px the edit-pins drag already snaps within —
 *  and a metre threshold would mean a click that snaps at one zoom and misses at
 *  another. Nothing about plant is decided here; the operator sees the vertex
 *  jump onto the pin, and the banner names what it caught.
 *
 *  Devices are preferred over subscribers on a tie because a customer pin is the
 *  smaller mark and routinely sits within a fingertip of the splitter feeding it
 *  — the same collision `refhover` had to be given a keep-out for. */
const SNAP_PX = 24

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
  // The cable whose core plan is open, and the core being TRACED on the map.
  //
  // Two pieces of state rather than one, because they answer different
  // questions and only one of them is a claim about the map: a cable can be open
  // with no core picked (reading the plan), and a trace has to survive the panel
  // being scrolled. `traceCore` is cleared whenever the cable changes — core 7
  // of one sheath is a different piece of glass from core 7 of another, so
  // carrying a selection across would highlight a run nobody asked for.
  const [cableOpen, setCableOpenRaw] = useState<number | null>(null)
  const [traceCore, setTraceCore] = useState<number | null>(null)
  const cableDelete = useConfirm()
  /** The cable LIST, the entry point cables had none of. Separate from
   *  `cableOpen` so the back arrow has somewhere to go and closing the panel
   *  from a cable doesn't reopen the list behind it. */
  const [cableList, setCableList] = useState(false)
  /** OPENING A COUPLER MID-SPAN: the map is armed, and the next click on the
   *  cable is where it is cut. A mode rather than a drag, because the point
   *  wanted is a place on the STREET — a thing you point at, not a handle you
   *  move — and because the coupler does not exist yet, so there would be
   *  nothing to take hold of. The server snaps it onto the route, so a click
   *  merely has to be near. */
  const [splitAt, setSplitAt] = useState<
    { cableId: number; cableName: string } | null>(null)
  /** The POINT whose splice tray is open — a box, or a customer. Both, because
   *  a customer point is a coupler too: that is the case the ISPs added, and it
   *  is what lets a lane of houses be daisy-chained down one 4F. */
  const [trayAt, setTrayAt] = useState<
    { device_id?: number | null; mac?: string | null } | null>(null)
  const [trayError, setTrayError] = useState<string | null>(null)
  /** THE FIBRE BEING FOLLOWED, end to end across sheaths and joints.
   *
   *  A strand changes both sheath and core number at the second hop of any real
   *  access network, so this is the only thing that can answer "where does this
   *  fibre actually go" — out of the OLT on the trunk, through the closure it is
   *  cut at, onward on the distribution cable. Held as the fibre it started
   *  from, because that is what the operator clicked. */
  const [traceFrom, setTraceFrom] = useState<
    { cableId: number; coreNo: number } | null>(null)
  const setCableOpen = useCallback((id: number | null) => {
    setCableOpenRaw(id)
    setTraceCore(null)
  }, [])
  /** Naming a cable: an existing one being renamed (`id`), or a brand-new one
   *  whose route has just been TRACED (`path`, no id).
   *
   *  Creation came back on 2026-08-09, reversing the rule that there is no
   *  create form because "a cable that is nowhere is not a thing that exists".
   *  That was right when a cable had no geometry — it was an abstraction, and
   *  the live fleet grew four of them attached to nothing. A cable has a
   *  surveyed route now, so one drawn down a street IS somewhere, and it is in
   *  the ground before anything is spliced to it. Which is why the ROUTE comes
   *  first and this form second: you have already put it somewhere by the time
   *  you are asked what to call it. */
  const [cableForm, setCableForm] = useState<
    { id?: number; name: string; cores: number | null
      path?: Array<[number, number]>
      ends?: [FibreEnd | null, FibreEnd | null] } | null>(null)
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
  // The create sheet's subject: a kind and a coordinate. Non-null means the
  // sheet is open. It carried an inferred FEEDER until 2026-08-09 — placing a
  // box no longer guesses what feeds it, so there is nothing left to infer.
  const [plantDraft, setPlantDraft] = useState<PlantDraft | null>(null)
  const [customerDraft, setCustomerDraft] = useState<CustomerDraft | null>(null)
  // The passive the delete confirm is about. Held apart from `plantMenu`, which
  // is dismissed the moment the item is clicked: the dialog has to keep naming
  // the box after the menu it came from is gone.
  const [plantDelete, setPlantDelete] = useState<OrgDevice | null>(null)
  const confirmPlantDelete = useConfirm()
  // "Click where it goes." Set by a menu item opened ON a pin (that pin already
  // owns its own coordinate, so creating at it would stack two boxes on one
  // point) and by "Save and add another", which is what makes recording a whole
  // feeder run one continuous gesture rather than eight round trips.
  //
  // `passiveId` is for the CUSTOMER kind alone, and that asymmetry is the point:
  // a tech at a drop knows in one instant both where the box is and which
  // splitter its fibre comes off, so that inheritance is a fact rather than a
  // guess. The person placing the SPLITTER usually does not yet know which core
  // will feed it, which is why plant inherits nothing any more.
  const [armed, setArmed] = useState<{ kind: ArmKind; passiveId: number | null } | null>(null)
  // The `+` button's mode: the next click opens the MENU rather than creating
  // anything. A context menu nobody knows to right-click for is a feature that
  // does not exist, and this is the visible twin of it.
  const [addNext, setAddNext] = useState(false)
  // Drawing a cable path: clicks append vertices, drags adjust.
  //
  // TWO KINDS OF SPAN, ONE EDITOR. A link between devices and a subscriber's
  // drop are the same gesture over the same ground and the same waypoint list —
  // only the row they land in differs (`link_routes` keyed on a device pair,
  // `onu_drops` keyed on a MAC). A second editor would mean a second set of
  // undo/straighten/vertex-drag behaviours to keep in step, and the day they
  // drifted the map would have taught two ways to trace one network.
  const [routeEdit, setRouteEdit] = useState<RouteEdit | null>(null)
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
  // LINKS WITH NO FIBRE RECORDED. On by default, because switching them off is
  // the strong claim: on this fleet 59 of 62 placed links have no cable recorded
  // yet, so a default of "off" would blank almost every map and take the
  // branch-fault span with it. Off is the PURE PLANT view — every line you can
  // see is glass somebody wrote down — which is what the operator asked for
  // (2026-08-09) after deleting a cable and finding the line still there.
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
  // The CABLES: the physical sheaths several spans are cut from. Its own query
  // rather than folded into `routes` for the reason routes aren't folded into
  // `/api/inventory` — every page lists devices, only the map (and the cable
  // panel it opens) needs plant. Reference data that will not change this
  // month, so it shares the routes' slow staleTime.
  const cablesQ = useQuery({
    queryKey: ["cables", scopeOrg],
    queryFn: () => inventoryApi.cables(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  /** Everything landing on the point whose tray is open. Its own read, not a
   *  slice of the cable list: the tray is a question about a POINT ("what is
   *  joined to what in here"), and answering it out of the org's cables would
   *  mean the browser re-deriving what the server already resolves — including
   *  the feed side, which comes from a walk no panel holds. */
  const fibreQ = useQuery({
    queryKey: ["point-fibre", scopeOrg, trayAt?.device_id ?? null, trayAt?.mac ?? null],
    queryFn: () => inventoryApi.pointFibre(trayAt!, scopeOrg),
    enabled: trayAt != null,
  })
  /** The whole optical path one fibre makes. Server-walked — see `trace_fibre`
   *  on why an algorithm is not mirrored into the browser the way a vocabulary
   *  is. */
  const traceQ = useQuery({
    queryKey: ["fibre-trace", scopeOrg, traceFrom?.cableId ?? null,
               traceFrom?.coreNo ?? null],
    queryFn: () => inventoryApi.traceFibre(traceFrom!.cableId, traceFrom!.coreNo,
                                           scopeOrg),
    enabled: traceFrom != null,
  })
  // Geometry, and WHOSE it is. A span with no trace of its own, on a cable
  // somebody HAS traced, arrives here already carrying the stretch of that
  // cable between the two points its boxes tap it at — resolved server-side, in
  // `list_link_routes`, so the map and every distance central computes measure
  // one line. `fromCable` rides along because the two are not the same claim:
  // surveyed as this section, or surveyed as the street it is cut from.
  const routeByKey = useMemo(() => {
    const m = new Map<string, { pts: Array<[number, number]>; fromCable: boolean }>()
    for (const r of routesQ.data?.routes ?? [])
      if (r.waypoints.length > 0)
        m.set(`${r.child_id}:${r.parent_id}`,
              { pts: r.waypoints, fromCable: r.from_cable })
    return m
  }, [routesQ.data])
  // A link's styling rides the same rows as its geometry, but is looked up
  // separately: a coloured link commonly has NO drawn route (that's the whole
  // point — you colour the parallel chords you can't otherwise tell apart), so
  // it must not be filtered out by the waypoints test above.
  // Everything on a link_routes row that ISN'T its geometry: the operator's
  // cartography (colour, label position) and the CABLE RECORD (how many fibres
  // the span carries, which strand this run uses). One map because it is one
  // row — and because the render needs them together anyway, the chip carrying
  // the cable facts being the same chip that borrows the colour.
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

  /** Boxes the tray may run a single-fibre tail to, NEAREST FIRST.
   *
   *  Computed here rather than served, because "which boxes are near this one"
   *  is a question about the map — this page already holds every pin, and a
   *  route for it would be a second answer to drift from the first.
   *
   *  DISTANCE IS THE WHOLE SAFETY PROPERTY, and it is a soft one on purpose. A
   *  tail is a real cable appearing on the map, so the box 40 km away must not
   *  sit one careless click under the box 30 m away — but nothing is REFUSED,
   *  because a long tail is unusual rather than impossible, and this record does
   *  not block real plant for looking strange. Ordering and a printed distance
   *  are the guard; the list is capped so the menu stays a menu.
   *
   *  Unplaced boxes are excluded outright. A tail needs two ends to draw between,
   *  and offering one that renders nothing is how a recorded connection becomes
   *  invisible — the exact failure the first surveyed pin hit. */
  const trayBoxes = useMemo(() => {
    const here = fibreQ.data?.point
    if (!here || here.lat == null || here.lng == null) return []
    const { lat, lng } = here
    return placed
      .filter((d) => d.id !== here.device_id)
      .map((d) => ({
        id: d.id, name: d.name, device_type: d.device_type,
        km: distanceKm(lat, lng, d.lat!, d.lng!),
      }))
      .sort((a, b) => a.km - b.km)
      .slice(0, 12)
  }, [fibreQ.data?.point, placed])

  /** SUBSCRIBERS a fibre may be taken out to, nearest first.
   *
   *  The case the tray could not express at all until now: its picker was built
   *  from `placed` DEVICES, so "core 3 is the drop to that customer" — the ISPs'
   *  own sentence, and the reason a customer point is a coupler in this model —
   *  had no way in. The server always accepted it (`to_mac` on the tail route);
   *  only the list was missing.
   *
   *  PLACED customers only, and the menu says so when there are none. A tail is
   *  a cable and a cable needs two ends to draw between; offering the whole
   *  roster would put thousands of unplaceable rows in a picker and end in a
   *  recorded connection nothing can render. */
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
      .slice(0, 12)
  }, [fibreQ.data?.point, places])

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
    || customerDraft != null || plantMenu != null || splitAt != null
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
    // Measured only where the drop was actually WALKED, and only through its own
    // splitter. Both halves matter: an untraced drop is a straight line between
    // two pins and reporting its length would put a drum figure on a span nobody
    // surveyed, and the OLT fallback is a stated guess about which end the cable
    // even runs to. `polyKm` walks it segment-by-segment in metres for the same
    // reason `linkhover` does — Mercator stretches with latitude, and this is a
    // number somebody orders cable against.
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
  /** The spans carrying the core being traced, as `child:parent` keys.
   *
   *  THE TRACE IS EMPHASIS, NEVER COLOUR. A strand's own jacket colour may not
   *  reach a stroke — the TIA-598 sequence contains red, orange, yellow and
   *  green, the hues this map reserves for alarms — so a traced run is drawn the
   *  way a selected path is: heavier and fully opaque, with the colour living in
   *  the panel's swatch. That also means a traced line in TROUBLE still renders
   *  red, which is the right precedence: what is broken outranks what is being
   *  read about.
   *
   *  Built from the cable's own span list rather than by re-scanning the routes,
   *  so the highlighted run and the panel's core plan cannot disagree about
   *  which spans are on core 7.
   *
   *  TWO SOURCES, ONE SET OF KEYS, and they answer different questions. The
   *  CORE PLAN lights the sections of one cable on one strand — a picture of
   *  that sheath. FOLLOWING A FIBRE lights the glass itself, which changes both
   *  sheath and core number at every closure it is cut at, so on a real access
   *  network the second is strictly longer than the first and usually starts
   *  somewhere else entirely. They share a key set because they light lines the
   *  same way, and only one can be active at a time. */
  /** WHICH CABLES ARE LIT, and by which of the two things that can light one.
   *
   *  THE CORE PLAN lights the ONE cable whose core is picked — a picture of that
   *  sheath. FOLLOWING A FIBRE lights the glass itself, which changes both sheath
   *  and core number at every closure it is cut at, so on a real access network
   *  the second is strictly longer than the first and usually starts somewhere
   *  else entirely. They share a set because they light lines the same way, and
   *  only one can be active at a time.
   *
   *  EMPHASIS ONLY — weight and opacity, never hue. A cable in trouble does not
   *  exist (a cable has no state), but the topology drawn over it does, and what
   *  is broken must always outrank what is being read about. */
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

  // The same save for the last hop. A separate mutation rather than a branch
  // inside `setRoute` because it invalidates a DIFFERENT query — the drop's
  // geometry rides `onu-places` (one read of `onu_drops` gives the map both the
  // anchor and the path to it), and invalidating "routes" here would leave the
  // traced drop dotted until something else happened to refetch.
  const setDropRoute = useMutation({
    mutationFn: ({ mac, waypoints }: { mac: string; waypoints: Array<[number, number]> }) =>
      inventoryApi.setDropRoute(mac, waypoints, scopeOrg),
    onSuccess: (_r, v) => {
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      setRouteEdit(null)
      toast.success(v.waypoints.length
        ? "Drop cable traced"
        // Straightening is not a failure and shouldn't read like one — it is
        // the honest state for a span nobody has walked, and the line going
        // back to dotted is the map saying exactly that.
        : "Drop straightened — back to an untraced line")
    },
    onError: (e) => toast.error(
      `Couldn't save the drop route${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // Tracing the street itself. A third mutation rather than a branch, for the
  // same reason the drop is one: it invalidates a DIFFERENT pair of queries.
  // "cables" holds the route being written, and "routes" holds every span that
  // BORROWS it — the server resolves a cabled span's geometry, so a trace saved
  // without invalidating the second would leave the very spans this feature
  // exists for drawing chords until something else happened to refetch.
  const setCablePath = useMutation({
    mutationFn: ({ cableId, path }: { cableId: number; path: Array<[number, number]> }) =>
      inventoryApi.setCablePath(cableId, path),
    onSuccess: (_r, v) => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      setRouteEdit(null)
      toast.success(v.path.length
        ? "Cable traced — untraced spans on it now follow the glass"
        // Same honesty as straightening a drop: this is the correct state for
        // a street nobody has walked, not a failure.
        : "Cable route cleared — its spans go back to straight lines")
    },
    onError: (e) => toast.error(
      `Couldn't save the cable route${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  // Per-link cartography: colour, and where the bandwidth chip rides the line.
  // Sparse by design — each call names only what it changes, so moving a label
  // can't clear a colour.
  const setLinkStyle = useMutation({
    // The style shape is TAKEN FROM THE API CLIENT rather than restated here.
    // It was restated, and the copy went stale the moment the row grew the cable
    // record: a conditional spread widens loosely enough that `cores` slipped
    // past the narrow local type without an error, so the one write that had to
    // be type-checked was the one that wasn't. Deriving it means the client is
    // the single declaration and a new field can't be silently unsendable.
    mutationFn: ({ childId, parentId, style }: {
      childId: number; parentId: number
      style: Parameters<typeof inventoryApi.setLinkStyle>[2]
    }) => inventoryApi.setLinkStyle(childId, parentId, style),
    // Both keys: a span joining a cable changes the cable's own span list and
    // core plan, so refreshing one without the other leaves the panel claiming a
    // strand is free that the map has just drawn.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      queryClient.invalidateQueries({ queryKey: ["cables"] })
    },
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  /** LAY A CABLE — its two ends, its route, and the couplers its ends needed.
   *
   *  THE COUPLERS ARE THE POINT. An end that landed on open ground gets one made
   *  for it right there, so "a cable runs between two couplers" is true BY
   *  CONSTRUCTION rather than by a rule an operator has to satisfy first. That is
   *  the whole difference between this and the flow it replaces, which asked what
   *  a box was fed from before it would let you save it.
   *
   *  Not atomic, and the failure is deliberately the benign one: a coupler with
   *  no cable is a pin somebody can see and delete, where a cable with a missing
   *  end would be a record that cannot be drawn or repaired from the map. The
   *  route is written last for the same reason — a named cable with no path is
   *  something the panel already reports as "not traced". */
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
            name, ip_address: "", device_type: "coupler",
            region: nearestRegion(at[0], at[1], devices), tags: [],
            // NOTHING FEEDS IT YET, and that is a recorded state rather than a
            // gap in one: the fibre says what feeds a box, and this coupler is
            // about to be one end of the cable that answers it.
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
      // A new coupler is a new device and a new plant feed, so the tree and
      // every split total behind it are stale the moment this lands.
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      setCableForm(null)
      if (v.id == null) {
        setRouteEdit(null)
        setCableList(false)
        setCableOpen(id)
        toast.success(`${v.name} laid`, {
          description: "Open a tray at either end to say which cores go where.",
        })
      }
    },
    onError: (e) => toast.error(`Couldn't save${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  /** OPEN A COUPLER MID-SPAN. One call does what the crew does — cut the sheath,
   *  stand a coupler at the cut, splice every core straight through — so nothing
   *  already recorded at either far end is disturbed. It creates an org_devices
   *  row, which is safe for exactly one reason: a coupler is a PASSIVE, so it is
   *  excluded from `org_device_topology` and joins no dependency chain. */
  const splitCable = useMutation({
    mutationFn: (v: { cableId: number; lat: number; lng: number }) =>
      inventoryApi.splitCable(v.cableId, v.lat, v.lng),
    onSuccess: (out) => {
      queryClient.invalidateQueries({ queryKey: ["cables"] })
      queryClient.invalidateQueries({ queryKey: ["routes"] })
      // A coupler is a new device and a new plant feed, so the tree and every
      // split total behind it are stale the moment this lands.
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      setSplitAt(null)
      setCableOpen(out.cable_id)
      toast.success("Coupler opened", {
        description: out.spliced
          ? `${out.spliced} cores spliced straight through — clear any you actually cut.`
          : "Record a fibre count on the cable to splice its cores through.",
      })
    },
    onError: (e) => toast.error(
      `Couldn't open a coupler${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  /** JOIN TWO FIBRES, or take one out to the equipment at this point.
   *
   *  A REFUSAL IS NOT AN ERROR HERE. The server answers 200 with a named reason
   *  ("that fibre is already joined to another one"), because on a splice tray
   *  an unexplained rejection is indistinguishable from a broken button — so it
   *  lands in the tray beside the two fibres it is about, where a toast would
   *  slide away from the only thing that makes it mean anything. */
  const setFibreJoint = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      a: { cableId: number; coreNo: number }
      b: { cableId: number; coreNo: number } | null
    }) => inventoryApi.setFibreJoint({
      ...v.point,
      a_cable_id: v.a.cableId, a_core_no: v.a.coreNo,
      b_cable_id: v.b?.cableId ?? null, b_core_no: v.b?.coreNo ?? null }),
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

  /** TAKE ONE CORE OUT TO A BOX SOMEWHERE ELSE.
   *
   *  Lays a single-fibre tail and lands it at both ends in one write. Same
   *  refusal discipline as a splice — a 200 carrying `ok:false` is the named
   *  reason, not an error — because it goes through the same physics on the way
   *  in. It also creates a CABLE, so the map's own queries have to be told. */
  const takeCoreToBox = useMutation({
    mutationFn: (v: {
      point: { device_id?: number | null; mac?: string | null }
      a: { cableId: number; coreNo: number }
      /** a box OR a customer — the tail route always took both, and offering
       *  only boxes is what made "this core is that customer's drop" unsayable */
      to: { deviceId?: number; mac?: string }
    }) => inventoryApi.takeCoreToBox({
      ...v.point, a_cable_id: v.a.cableId, a_core_no: v.a.coreNo,
      to_device_id: v.to.deviceId ?? null, to_mac: v.to.mac ?? null }),
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

  /** DELETING A PASSIVE, from the map that recorded it.
   *
   *  Passive plant is created here, so it is removed here: a splitter that was
   *  put in the wrong street, or a coupler left behind by a re-trace, is a pin
   *  somebody is looking at, and making them find it again in the Network tree
   *  is how a wrong pin stays on the map.
   *
   *  The server sweeps everything hanging off the row (its drops, the cables
   *  ending on it, the splices made in it), so the only thing this has to get
   *  right is SAYING so first, and invalidating every one of those views after.
   *  A 200 carrying `ok:false` is the refusal path, not an error: the store
   *  answers that way when the box still has children. */
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
      // A panel or a fibre tray still open on a box that no longer exists is the
      // "floating over nothing" failure this map is careful about elsewhere.
      setPlantDelete(null)
      if (selectedId === id) setSelectedId(null)
      setTrayAt((t) => (t?.device_id === id ? null : t))
    },
    onError: (e) => toast.error(
      `Couldn't delete${e instanceof ApiError ? `: ${e.message}` : ""}`),
  })

  /** What the delete takes with it, counted from what the map already holds.
   *
   *  Said BEFORE the click rather than discovered after it: a splitter carries
   *  the drops recorded off it and every cable landing on it, and none of that
   *  is visible from the pin. */
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
    // STANDS DOWN WHILE THE NAMING DIALOG IS OPEN, and this is not defensive
    // tidying: the dialog opens OVER a live route editor (the traced line has to
    // stay visible — it is what is being named), and Radix closes on Escape from
    // its own document listener. Both firing means one Esc backs out of naming
    // AND throws away the survey that was just walked. Cancel returns to the
    // editor with the route intact; nothing here may quietly make Esc destructive.
    if (cableForm != null) return
    if (placingId == null && routeEdit == null && placingOnu == null
      && armed == null && !addNext && splitAt == null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPlacingId(null); setRouteEdit(null); setPlacingOnu(null)
        setArmed(null); setAddNext(false); setSplitAt(null); return
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
  }, [placingId, placingOnu, routeEdit, armed, addNext, cableForm, splitAt])

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

  /** The pin under a click, within `SNAP_PX`, or null for open ground.
   *
   *  Reads the LIVE lists rather than anything cached on a cable, so a box
   *  dragged a second ago snaps where it now is. Both kinds are candidates
   *  because a cable may end on a customer — the case that makes a lane of
   *  daisy-chained houses recordable. */
  const snapToPoint = useCallback((ll: L.LatLng) => {
    const map = mapRef.current
    if (!map) return null
    const at = map.latLngToContainerPoint(ll)
    let best: { end: FibreEnd; pos: [number, number]; d: number } | null = null
    const offer = (pos: [number, number], end: FibreEnd, bias: number) => {
      const p = map.latLngToContainerPoint(pos)
      const d = Math.hypot(p.x - at.x, p.y - at.y) + bias
      if (d <= SNAP_PX && (!best || d < best.d)) best = { end, pos, d }
    }
    for (const d of placed) offer([d.lat, d.lng], { device_id: d.id, name: d.name }, 0)
    for (const pl of places) {
      if (pl.lat == null || pl.lng == null) continue
      offer([pl.lat, pl.lng], { mac: pl.mac, name: onuName(pl) }, 1)
    }
    return best as { end: FibreEnd; pos: [number, number] } | null
  }, [placed, places])

  const onMapClick = useCallback((ll: L.LatLng) => {
    // A menu is open: the click that dismisses it must not also do something.
    // Leaflet fires click after the capture-phase mousedown the menu closes on,
    // so without this the first click outside a menu would place a box.
    if (plantMenu != null) { setPlantMenu(null); return }
    if (routeEdit != null) {
      // LAYING A CABLE snaps its ends onto whatever they land on, and records
      // WHICH — see `FibreEnd`. A drop's route is anchored at both ends already,
      // so it takes the raw click.
      const snap = routeEdit.kind === "cable" ? snapToPoint(ll) : null
      setRouteEdit((re) => {
        if (!re) return re
        const at: [number, number] = snap ? snap.pos : [ll.lat, ll.lng]
        const points = [...re.points, at]
        if (re.kind !== "cable") return { ...re, points }
        return {
          ...re, points,
          // The FIRST click is end A for good. Every click after it is the
          // candidate for end B, so a snap followed by a free click correctly
          // reads as "ends on open ground" rather than keeping a stale catch.
          endA: re.points.length === 0 ? snap?.end ?? null : re.endA,
          endB: re.points.length === 0 ? re.endB : snap?.end ?? null,
        }
      })
    } else if (splitAt != null) {
      // OPENING THE SHEATH HERE. The raw click goes up: the server snaps it onto
      // the cable's own route, so a click near the line is enough and the cut
      // lands exactly on the glass. Deliberately not snapped here first — one
      // owner of that projection, or the coupler and the two halves drawn from
      // it eventually disagree about the same closure.
      splitCable.mutate({ cableId: splitAt.cableId, lat: ll.lat, lng: ll.lng })
    } else if (armed != null) {
      // "click where it goes", from a pin's menu item or from Save-and-add-
      // another. One click, one record: the coordinate is the click and
      // everything else was decided when the mode was armed.
      if (armed.kind === "customer") {
        setCustomerDraft({ lat: ll.lat, lng: ll.lng, passiveId: armed.passiveId })
      } else {
        setPlantDraft({ kind: armed.kind, lat: ll.lat, lng: ll.lng })
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
  }, [placingId, placingOnu, refOnus, routeEdit, setLocation, snapToPoint,
      armed, addNext, plantMenu, openPlantMenu, splitAt, splitCable])

  // A box was recorded. `again` re-arms the map for the NEXT box rather than
  // opening this one's panel, because somebody recording a feeder run is
  // walking it, not reading it. It used to re-arm with this box as the next
  // one's feeder; nothing is inherited now, so it re-arms with the kind alone.
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
      // Opened, because the box is placed and joined to nothing and the next
      // thing to do is say what feeds it — which is the Fibre section of the
      // panel that just opened.
      toast.success(`${created.name} recorded`, {
        description: "Pull a core in to say what feeds it.",
      })
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
      kind: "primary" | "backup" | "peer" | "run"
      // the link_routes key — what a style/geometry write for this line addresses
      childId: number; parentId: number
      route?: { pts: Array<[number, number]>; fromCable: boolean }
      labelPos?: number | null
      /** the sheath this span is cut from, and the strand this run uses */
      cableId: number | null
      cableName: string | null
      cores: number | null
      coreNo: number | null
      binding?: LinkBinding
    }> = []
    const placedById = new Map(drawnDevices.map((d) => [d.id, d]))
    // WHICH CABLE A LINK IS CUT FROM: whichever one runs between the same two
    // points. A cable is not a property of a link any more — it needs no link to
    // exist and draws itself — but a link over a pair that IS joined by fibre
    // should still say so on its chip, and should stop being dashed.
    const cableByPair = new Map<string, Cable>()
    for (const c of cablesQ.data?.cables ?? []) {
      if (c.a.device_id == null || c.b.device_id == null) continue
      for (const k of [`${c.a.device_id}:${c.b.device_id}`,
                       `${c.b.device_id}:${c.a.device_id}`]) {
        const cur = cableByPair.get(k)
        // A measured sheath beats an unmeasured one: `12F` says more than a bare
        // name, and it is the same glass either way.
        if (!cur || (cur.cores == null && c.cores != null)) cableByPair.set(k, c)
      }
    }
    const styled = (childId: number, parentId: number) => {
      const s = styleByKey.get(`${childId}:${parentId}`)
      const c = cableByPair.get(`${childId}:${parentId}`)
      return { childId, parentId, labelPos: s?.label_pos,
               cableId: c?.id ?? null, cableName: c?.name ?? null,
               cores: c?.cores ?? null, coreNo: null }
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
      // from the lower id — otherwise every line renders twice, and the second
      // would carry the far end's tone.
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
  }, [drawnDevices, routeByKey, styleByKey, linkBindings, cablesQ.data])

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
    const wp = l.route?.pts
    const drawn = !!wp?.length && !foldedTogether(from, to)
    const pts: Array<[number, number]> = drawn ? [from, ...wp!, to] : [from, to]
    // Surveyed either way, but not by the same survey — see the hover readout,
    // which is the one surface with room to say which.
    return { ...l, from3: from, to3: to, pts, drawn,
             fromCable: drawn && !!l.route?.fromCable }
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
  /** WHERE A FIBRE POINT IS, whichever kind it is.
   *
   *  ONE resolver for both, because a cable end may be a box or a customer and
   *  every surface that draws one needs the other. The server ships coordinates
   *  on the end itself, but they are read HERE from the live lists so a pin
   *  dragged this second moves its cable with it — a cable reply is reference
   *  data on a slow staleTime, and a line lagging its own pin reads as a
   *  rendering fault. */
  const pinOfPoint = useCallback((pt: FibrePoint): [number, number] | null => {
    if (pt.device_id != null) {
      const d = byId.get(pt.device_id)
      return d && isPlaced(d) ? [d.lat, d.lng] : null
    }
    const place = pt.mac ? places.find((x) => x.mac === pt.mac) : null
    return place && place.lat != null && place.lng != null
      ? [place.lat, place.lng] : null
  }, [byId, places])

  /** EVERY CABLE AS IT WILL BE DRAWN — resolved ONCE.
   *
   *  The render and the chip budget both need this geometry, and on a traced
   *  street the midpoint of the line as drawn is nowhere near the midpoint of
   *  the chord between its ends. Two computations of it is exactly how a budget
   *  reports itself clear over a collision that is plainly visible — the rule
   *  `refChipPos` was extracted for, applied to the family that came after. */
  const cableLines = useMemo(() => {
    if (zoom < detail.passives) return []
    return (cablesQ.data?.cables ?? []).flatMap((cable) => {
      if (routeEdit?.kind === "cable" && routeEdit.cableId === cable.id) return []
      const pts = cablePolyline(cable, pinOfPoint)
      return pts.length < 2 ? [] : [{ cable, pts }]
    })
  }, [cablesQ.data, pinOfPoint, zoom, detail.passives, routeEdit])

  const chipShown = useMemo(() => {
    const links = new Set<string>()
    const cables = new Set<number>()
    const refs = new Set<string>()
    const names = new Set<string>()
    const taken: Array<[number, number, number]> = []
    // A generous fixed box PER FAMILY, not a measurement of each chip's text:
    // measuring would mean laying the icons out to read them back, and an
    // overestimate fails safe — it drops a chip that would just have fitted, and
    // never keeps one that overlaps.
    //
    // ONE GLOBAL NUMBER STOPPED WORKING WHEN THE CABLE CHIPS ARRIVED. It was 78,
    // which is a fair overestimate for `↓3.7M ↑1.2M` and a large UNDER-estimate
    // for `HALIYA TRUNK 24F` — measured in the browser at 134px against a rate
    // chip's ~90 and a subscriber's ~50. Two 134px chips 80px apart pass a
    // 78px test and visibly overlap, which is the exact failure this budget
    // exists to prevent. So a claim carries its own half-width and a pair is
    // judged on the SUM: dense areas keep their narrow chips, and only the wide
    // family spreads. Re-measure these if a chip's content or clamp changes.
    const CHIP_HALF = { link: 48, cable: 68, ref: 28, name: 44 }
    const fits = (x: number, y: number, half: number) =>
      !taken.some(([tx, ty, th]) =>
        Math.abs(tx - x) < th + half && Math.abs(ty - y) < 24)
    const claim = (x: number, y: number, half: number) => {
      taken.push([x, y, half])
    }

    const cands: Array<{ key: string; x: number; y: number; rank: number }> = []
    for (const l of bwLabels ? drawnLinks : []) {
      // A link earns a chip from EITHER half — a bound port's live rate, or a
      // recorded cable. The budget's predicate has to match the render's
      // exactly, or an absence goes on reserving pixels away from something
      // that would have drawn (the rule `refHasChip` is written against, here
      // for the family that came after it).
      if (!l.binding && !l.cores) continue
      // dimming is applied here too, so a chip the render will not draw can't
      // reserve pixels away from one it will
      const emphasized = selectedId != null
        && (l.to.id === selectedId || downstream.has(l.to.id))
      if (troubleOnly && l.tone !== "destructive" && l.tone !== "warning" && !emphasized)
        continue
      // A chip may never outlive the line it rides — and it must not reserve
      // pixels away from one that IS drawn, which is the documented rule for
      // every family in this budget.
      if (!showUncabled && l.cableId == null) continue
      // the ends must be far enough apart on screen that the chip has a line to
      // sit on — zoomed out, the pins (and clusters) own the pixels
      const [ax, ay] = project(l.from3[0], l.from3[1], zoom)
      const [bx, by] = project(l.to3[0], l.to3[1], zoom)
      if (Math.hypot(bx - ax, by - ay) < 90) continue
      const [plat, plng] = linkLabelPos(l.pts, l.labelPos)
      const [x, y] = project(plat, plng, zoom)
      cands.push({ key: l.key, x, y,
        rank: bwRank(l.binding, l.from.id, l.to.id, l.cores) })
    }
    cands.sort((a, b) => b.rank - a.rank)
    for (const c of cands) {
      if (!fits(c.x, c.y, CHIP_HALF.link)) continue
      claim(c.x, c.y, CHIP_HALF.link)
      links.add(c.key)
    }

    // CABLE NAME CHIPS, second. They join this budget rather than starting a
    // third one — the documented rule, and the reason is visible here: a cable
    // chip and a link rate chip ride the SAME line and collide with each other
    // exactly as readably as two of either would, so two budgets would each
    // report themselves clear while the screen showed a smear.
    //
    // Ranked under live rates and over subscribers. A rate is STATE and this is
    // reference, which is the ordering this map keeps everywhere; but one
    // customer's rate never outranks the identity of the sheath their whole
    // branch hangs off. Within themselves, the most glass first: on a street
    // where a 24F trunk and a 4F branch overlap, the trunk is the one worth the
    // pixels.
    const cableCands: Array<{ id: number; x: number; y: number; cores: number }> = []
    for (const c of cableLines) {
      const [plat, plng] = cableLabelPos(c.pts)
      const [x, y] = project(plat, plng, zoom)
      // Same span test the link chips make: zoomed out the pins own the pixels
      // and there is no line left for a label to sit on.
      const [ax, ay] = project(c.pts[0][0], c.pts[0][1], zoom)
      const [bx, by] = project(c.pts[c.pts.length - 1][0],
                               c.pts[c.pts.length - 1][1], zoom)
      if (Math.hypot(bx - ax, by - ay) < 90) continue
      cableCands.push({ id: c.cable.id, x, y, cores: c.cable.cores ?? 0 })
    }
    cableCands.sort((a, b) => b.cores - a.cores)
    for (const c of cableCands) {
      if (!fits(c.x, c.y, CHIP_HALF.cable)) continue
      claim(c.x, c.y, CHIP_HALF.cable)
      cables.add(c.id)
    }

    // Subscriber rate chips, third — and ranked EVIDENCE first among
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
      // Reserved where the chip will actually LAND, which on a traced drop is
      // the midpoint of the walked path and not of the chord — those diverge by
      // exactly as much as the cable bends. A budget measuring one point while
      // the render draws at another reports itself clear over a collision, which
      // is the failure this single shared reservation exists to prevent.
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
      if (!fits(c.x, c.y, CHIP_HALF.name)) continue
      claim(c.x, c.y, CHIP_HALF.name)
      names.add(c.mac)
    }
    return { links, cables, refs, names }
  }, [drawnLinks, bwLabels, showUncabled, zoom, troubleOnly, selectedId, downstream,
      refLinesVisible, refNamesVisible, refVisible, shownPlaces, byId, cableLines])

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
    && splitAt == null
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

  /** EVERY CABLE ENDING ON THE SELECTED BOX. A cable is undirected, so both ends
   *  are asked. This is what replaced "which device is this from?" on the create
   *  sheet: a box's relationship to the network is the fibre landing on it, and
   *  that fibre's geometry belongs to the cable it is in.
   *
   *  ABOVE THE `!scopeOrg` RETURN, and it has to stay there: it landed below it
   *  while this feature was being built, which is a hooks-order violation —
   *  React renders fewer hooks on the scope-less pass and throws. `oxlint`
   *  catches it as `rules-of-hooks`; the two `useResizablePanel` reports beside
   *  it are older and are NOT this. */
  const deviceCables = useMemo(() => {
    if (!selected) return []
    return (cablesQ.data?.cables ?? [])
      .filter((c) => c.a.device_id === selected.id || c.b.device_id === selected.id)
      .map((c) => ({
        cable: c,
        far: c.a.device_id === selected.id ? c.b : c.a,
      }))
  }, [selected, cablesQ.data])

  if (!scopeOrg) return <NeedsOrg />

  const down = troubles.filter((d) => pinTone(d) === "destructive").length
  const degraded = troubles.length - down

  const lineColor = (tone: string) =>
    tone === "destructive" ? "var(--destructive)"
      : tone === "warning" ? "var(--warning)" : "var(--map-link)"

  const parent = selected?.parent_device_id != null ? byId.get(selected.parent_device_id) : null
  const linkKm = selected && isPlaced(selected) && parent && isPlaced(parent)
    ? distanceKm(selected.lat, selected.lng, parent.lat, parent.lng) : null
  // The panel's "cable" figure measures what is DRAWN — borrowed from the cable
  // or traced by hand, both are surveyed geometry and a crew drives the length
  // either way. Which survey it was is the hover readout's to say.
  const selRoute = selected && parent
    ? routeByKey.get(`${selected.id}:${parent.id}`)?.pts : undefined
  const routeKm = selRoute && selected && isPlaced(selected) && parent && isPlaced(parent)
    ? polyKm([[parent.lat, parent.lng], ...selRoute, [selected.lat, selected.lng]]) : null

  // THE PER-SPAN ROUTE EDITOR IS GONE (2026-08-09, operator: "for a device like
  // splitter or OLT there is option to lay out the line. I don't want that
  // too"). It was the last way left to draw a line that is NOT a cable, and a
  // span with geometry of its own is a SECOND record of one piece of glass —
  // which is what made deleting a cable leave its line behind, and what "a
  // span's own trace always wins" then kept on screen.
  //
  // Geometry is authored on the CABLE now (its own Trace/Retrace), and every
  // span cut from it follows. The hand-traced spans that already existed were
  // MOVED onto their cables rather than orphaned — `tools/cable_backfill.py
  // --adopt-traces` — because a record nobody can see or edit is worse than one
  // that is merely wrong.


  /** Trace the last hop: the drop cable from a splitter to one customer.
   *
   *  Only reachable from a drop that HAS a recorded splitter — the server 404s
   *  otherwise, and rightly: the fallback line to the OLT is explicitly a guess
   *  ("we only know the PON"), so tracing it would turn that guess into surveyed
   *  geometry a crew would quote drum off. Record the splitter first, which is
   *  the order the work happens in anyway. */
  const startDropRouteEdit = (p: OnuPlace) => {
    setPlacingId(null)
    setPlaceOpen(false)
    setPlacingOnu(null)
    setRouteEdit({ kind: "drop", mac: p.mac, points: p.drop_waypoints ?? [] })
  }
  // The drop being traced, resolved to its two real ends: the splitter it comes
  // off and the subscriber it feeds. Both must exist and be placed — which they
  // always are at this point, since tracing is only offered from a recorded,
  // located drop, and `dropAnchor` is the same resolver the line itself uses so
  // the editor can never anchor somewhere the render wouldn't.
  const editingDrop = routeEdit?.kind === "drop"
    ? (places.find((p) => p.mac === routeEdit.mac) ?? null) : null
  const editingDropAnchor = editingDrop
    ? dropAnchor(editingDrop.drop_passive_id, editingDrop.device_id, byId) : null

  // `open` must match when a Card actually RENDERS (route editing replaces it),
  // or the chrome makes room for a panel that isn't there. EVERY right-rail card
  // counts: a subscriber opens in the same rail at the same width, and so does a
  // cable — counting only the device panel left the top strip claiming width
  // that was under it, and left the control column (including the Cables button
  // that opened the thing) buried behind the panel with no way to toggle it off.
  const railOpen = (!!selected || !!selectedRef || cableOpen != null || cableList)
    && !routeEdit
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
        {/* TRACED CABLES — the glass itself, drawn before every topology line so
            it sits UNDER them.

            A span that borrows this route draws exactly on top of these pixels,
            so the overdraw is invisible; what this layer is actually for is the
            two cases a span cannot show. One is the street traced PAST the last
            box on it — the trace-first workflow, where the whole point is that
            the next splitter has a route waiting for it. The other is a span
            somebody traced by hand along a different line, where seeing both is
            the information ("the cable runs here, this section was drawn
            there") rather than a rendering fault.

            PLANT'S OWN HUE AT FULL CHROMA, and the step matters: `--map-plant`
            and `--map-plant-quiet` are not loud and quiet versions of one
            thing, they are two SENTENCES — quiet means "nothing recorded here",
            which is the exact opposite of what a traced route is. Subordination
            is carried by WIDTH (2 against a feed's 2.5) and by STACKING (drawn
            before every topology line, so it is always underneath), never by
            draining the colour out of a fact somebody surveyed.

            It takes no status tone at all, because a cable has no state; what
            is broken is the SPAN drawn over it, and that still renders red.
            Gated on the plant zoom floor with the boxes it feeds, or a lone
            violet line would hang in a viewport with nothing at either end. */}
        {cableLines.map(({ cable: c, pts }) => {
          const traced = cableTraced(c)
          // EMPHASIS, NEVER HUE. A traced core lights its whole path across the
          // map; a cable has no state to colour, and what IS broken is the
          // topology drawn over it, which must always stay the loudest thing.
          const lit = tracedCables.has(c.id)
          const w = 2 + fiberBoost(c.cores) + (lit ? 1.5 : 0)
          return (
            <Fragment key={`cable-${c.id}`}>
              {/* Heavier for the glass in it, like every other line that
                  carries a fibre count — this is the sheath itself, so if
                  anything on the map earns that treatment it does. */}
              <Polyline interactive={false} positions={pts}
                pathOptions={{ color: "#000", opacity: CASING_OPACITY,
                  ...casingAt(lineK, w, CASING_OVER_FINE),
                  ...(traced ? {} : { dashArray: CABLE_DASH }) }} />
              <Polyline interactive={false} positions={pts}
                pathOptions={{ color: "var(--map-plant)", opacity: lit ? 1 : 0.9,
                  ...strokeAt(lineK, w),
                  ...(traced ? {} : { dashArray: CABLE_DASH }) }} />
              {/* THE CABLE SAYS ITS OWN NAME. Four violet lines meeting at a
                  closure were previously told apart only by clicking a box and
                  reading a list — on a map, where the thing you are looking at
                  is the LINE. Placement, ranking and suppression are resolved
                  in the shared budget; a suppressed chip is not lost, zooming
                  in spreads the midpoints and it returns.
                  CLICKING IT OPENS THE CABLE, which is the other half: the
                  chip is a marker, so it can be the handle WITHOUT making the
                  polyline interactive — and a live polyline here would swallow
                  the placement and route-drawing clicks the map is also for. */}
              {chipShown.cables.has(c.id) && (
                <Marker position={cableLabelPos(pts)} icon={cableIcon(c)}
                  zIndexOffset={550}
                  eventHandlers={{ click: () => { setCableList(false)
                                                  setCableOpen(c.id) } }} />
              )}
            </Fragment>
          )
        })}
                {drawnLinks.map((l) => {
          // PURE PLANT VIEW: every line on screen is glass somebody recorded.
          // Filtered at the RENDER, not in `drawnLinks`, deliberately — the
          // branch-fault overlay paints on the span feeding a dark branch, and
          // that is an alarm, so it keeps its line at every setting. Same
          // exemption the plant zoom floor makes for a dark splitter.
          if (!showUncabled && l.cableId == null) return null
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
          // Built once and hoisted, because it may legitimately be null (a link
          // with neither a bound port nor a recorded cable) — which is what keeps
          // an ungated future caller a type error rather than an empty pill
          // floating on a line.
          const chipIcon = labeled
            ? linkBwIcon(l.binding, l.from, l.to,
                         { cores: l.cores, coreNo: l.coreNo, name: l.cableName })
            : null
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
          // A run counts as distribution on the same test: how a pair is
          // JOINED (a declared parent, or a recorded core) says nothing about
          // whether the span is a feeder or a distribution leg — the boxes at
          // its ends do.
          const distribution = (l.kind === "primary" || l.kind === "run")
            && isPassiveType(l.from.device_type) && isPassiveType(l.to.device_type)
          // A TRACED CORE reads at selection weight, because that is what it is:
          // the operator has named one strand and this is where it goes. It joins
          // the existing ladder rather than starting a scale of its own, and it
          // adds no colour at all — the strand's jacket hue stays in the panel,
          // where it cannot be mistaken for a status tone.
          const traced = l.cableId != null && tracedCables.has(l.cableId)
          // THE FIBRE BOOST IS ADDED LAST AND IS ADDITIVE, which is the whole
          // condition the "no stroke treatment for core count" note left behind:
          // the tier above is derived from TOPOLOGY and keeps its order inside
          // any one fibre class. A 12F distribution leg still draws under a 12F
          // feed; what the boost separates is how much glass is in the sheath,
          // an axis this ladder never spoke to.
          const weight = (l.kind === "peer" ? 2
            : emphasized || traced ? 3.5
            : l.tone === "destructive" ? 3
            : distribution ? 2.1 : 2.5)
            + (hovered && !emphasized && !traced ? 0.75 : 0)
            + fiberBoost(l.cores)
          // backup = long dash (a standby path), peer = fine dot (cabling).
          // Periods are sized to survive CASING_OVER: a dash pattern has to be
          // longer than the casing's overhang or the cased dots touch.
          //
          // AND A LINK WITH NO CABLE RECORDED IS DASHED, whatever kind it is.
          // This is the same rule the drop line and the ref-ONU line have always
          // kept — dotted means "nobody surveyed this", solid means traced fibre
          // — applied to the one layer that never got it. Until now a topology
          // link drew solid whether or not any glass was recorded, so deleting a
          // cable changed nothing on screen and recording one changed nothing
          // either: the line was never the cable, it was `parent_device_id`.
          const dashArray = l.kind === "backup" ? "5 8"
            : l.kind === "peer" ? "1.5 7"
            : l.cableId == null ? UNCABLED_DASH : undefined
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
                  // The operator tint is GONE (2026-08-08): the only thing it was
                  // ever used to say — "these spans are one physical cable" — is
                  // what `org_cables` says properly, and a strand's OWN colour may
                  // never reach a stroke (the TIA-598 sequence contains the alarm
                  // hues). So a line's colour is its tone and nothing else, and
                  // the one override left is emphasis: "which line did I just
                  // click" outranks a muted default.
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
                      // A chip with a bound port opens the Ports tab of whichever
                      // box owns it. A CABLE-ONLY chip has no port to show, so it
                      // opens the far end of the span instead — the box you would
                      // be looking for when you clicked a cable label. Landing on
                      // an empty Ports tab would read as a broken panel.
                      if (l.binding) {
                        setDetailTab("ports")
                        setSelectedId([...l.binding.keys()][0])
                      } else {
                        setSelectedId(l.to.id)
                      }
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
          // The two ENDS, whichever kind is being traced. Everything after this
          // is identical for a trunk, a drop and a cable — same vertices, same
          // drag, same double-click-to-remove — which is the whole reason the
          // editor was widened rather than duplicated.
          //
          // A CABLE HAS NO ENDS TO ANCHOR TO, and that is not a missing case:
          // it ends wherever the glass ends, so the vertices ARE the route
          // rather than intermediates between two pins. It renders the points
          // alone, which also means the first click of a fresh trace draws
          // nothing until the second — correct, since one point is a place and
          // not a run, and the server refuses it for the same reason.
          //
          // The DROP and the LATERAL are anchored; the cable is not. The `link`
          // kind went with the per-span route editor, since a span's geometry
          // is its cable's.
          const ends: [[number, number], [number, number]] | null =
            editingDrop && editingDropAnchor
              // splitter → subscriber, the direction the waypoints are stored in
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
                      setCustomerDraft({ lat: d.lat, lng: d.lng, passiveId: armed.passiveId })
                    } else {
                      setPlantDraft({ kind: armed.kind, lat: d.lat, lng: d.lng })
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
          // The drop being traced right now is drawn by the EDITOR, not here —
          // two lines along the same span (one stored, one live) read as a
          // rendering fault and make the vertices impossible to aim at.
          if (routeEdit?.kind === "drop" && routeEdit.mac === p.mac) return null
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
          // A TRACED drop follows the ground; an untraced one is the chord it
          // has always been. Waypoints run splitter → subscriber, which is the
          // order they were stored in, so nothing is reversed here.
          //
          // Only a drop routed through its actual SPLITTER may carry geometry:
          // the OLT fallback is explicitly a guess ("we only know the PON"), and
          // waypoints on a guessed anchor would be a surveyed-looking path to
          // the wrong end. The server refuses to store them, so this can only
          // fire on a stale reply — but drawing is where it would be believed.
          const dropPath = viaSplitter ? (p.drop_waypoints ?? []) : []
          const traced = dropPath.length > 0
          const pts: Array<[number, number]> = traced
            ? [[to.lat, to.lng], ...dropPath, [p.lat, p.lng]]
            : [[to.lat, to.lng], [p.lat, p.lng]]
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
          // A TRACED DROP IS SOLID, AND THAT IS THE WHOLE POINT OF TRACING IT.
          // Every dash on this map means "nobody surveyed this" — it is why a
          // drop was the tightest dotted line here, being the least surveyed
          // span there is. Somebody has now walked this one, so it earns the
          // same solid stroke a drawn cable route gets. The two states must
          // never look alike in either direction: an untraced drop may not look
          // surveyed, and a surveyed one may not go on apologising for itself.
          const refDash = traced || hovered ? undefined
            : viaSplitter ? DROP_DASH : REF_DASH
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
                  position={refChipPos(to, p)}
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
          // Either kind of join: since the plant chain can come from a run,
          // the span a branch fault names is routinely a recorded core rather
          // than a declared parent, and dropping that case would take the
          // overlay off exactly the boxes recorded the new way. Ends may arrive
          // in either order — a run is undirected.
          const link = drawnLinks.find(
            (l) => (l.kind === "primary" || l.kind === "run")
              && ((l.to.id === f.passive_id && l.from.id === f.parent_id)
                || (l.kind === "run" && l.from.id === f.passive_id
                    && l.to.id === f.parent_id)))
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
                {armed.passiveId != null && byId.get(armed.passiveId) && (
                  <> · on <span className="font-medium">{byId.get(armed.passiveId)!.name}</span></>
                )}
              </>
            ) : (
              // No "below X" any more: a box is placed, and nothing is drawn to
              // it until a core is pulled in. Naming a feeder here would promise
              // a line that Save is deliberately not going to draw.
              <>Click where the {armed ? PLANT_LABEL[armed.kind as PlantKind] : "box"} goes</>
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

      {/* route-drawing banner ----------------------------------------------------
          ONE banner for both kinds of span. What it names changes (two boxes for
          a cable, a splitter and a customer for a drop) but every control below
          is the same, because the gesture is the same — and a second banner with
          its own undo and its own straighten is how two ways to trace one
          network end up on one map. */}
      {routeEdit && (routeEdit.kind === "cable"
        || !!(editingDrop && editingDropAnchor)) && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(94vw,44rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Spline className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            {routeEdit.kind === "cable" ? (
              // No "A → B": a cable is not between two boxes, which is the
              // whole reason it has a route of its own. It is named instead.
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
            {/* WHY SAVE IS DEAD, said rather than left to be inferred. A new
                cable starts on the point that was right-clicked, so the very
                first thing an operator sees is a Save button that does nothing
                when pressed — which is indistinguishable from a broken one. One
                point is genuinely not a route (nothing can project onto it, and
                every span on the cable would silently fall back to a chord), so
                the button is right to refuse; it just has to say so. */}
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
          {/* Undo pops the LAST point placed (double-click still removes any
              specific one); straighten drops them all, which on save deletes the
              route row outright — the store treats an empty list as "clear". */}
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
            // ONE POINT IS NOT A ROUTE, and the server refuses it — nothing can
            // project onto a single coordinate, so a cable would quietly fall
            // back to a chord. Disabling here says so before the 422 does.
            //
            // A NEW cable needs TWO, and that is a different rule: zero points
            // on a RETRACE is a real statement (it clears the route), but on a
            // cable that does not exist yet there is nowhere to stand its two
            // couplers. Saving with none used to open the naming sheet reading
            // "0 points traced" and then fail on submit — a button that is
            // enabled and cannot work is indistinguishable from a broken one.
            disabled={setRoute.isPending || setDropRoute.isPending
              || setCablePath.isPending
              || (routeEdit.kind === "cable"
                  && routeEdit.points.length < (routeEdit.cableId == null ? 2 : 1)
                  && !(routeEdit.cableId != null && routeEdit.points.length === 0))}
            onClick={() => routeEdit.kind === "cable"
              // A cable that does not exist yet is NAMED NOW, with its route
              // already drawn — which is the order that keeps a cable from
              // being an abstraction. The form carries the path so the two land
              // in one gesture; nothing is written until it is submitted, so
              // cancelling leaves no empty sheath behind.
              ? (routeEdit.cableId == null
                ? setCableForm({ name: "", cores: null, path: routeEdit.points,
                                 ends: [routeEdit.endA ?? null, routeEdit.endB ?? null] })
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
              {/* THE PURE PLANT VIEW. Switching this off leaves only lines with
                  a cable recorded against them — which is what the map claims to
                  be, and what an operator means when they delete a cable and
                  expect the line to go with it.

                  IT USED TO BE CALLED "Links with no cable", AND THAT NAME WENT
                  STALE THE DAY THE SEGMENT MODEL LANDED. It was written when a
                  topology link could carry a cable and most did not ("59 of 62
                  here"); a link now carries NO plant record BY CONSTRUCTION —
                  glass is recorded on the cable itself — so the set it hides is
                  not "the ones nobody got to yet", it is all of them, always.
                  A control whose name describes a state that can no longer occur
                  reads as broken, and this one is the answer to the commonest
                  fibre complaint there is (the dashed dependency lines shouting
                  over the surveyed cable), so it has to be findable.

                  ON by default, which stays the cautious direction: these lines
                  are the monitoring tree, and blanking them by default would hide
                  the dependency an operator navigates by. */}
              <button
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs hover:bg-foreground/5"
                title="The parent → child lines the monitoring tree draws. They are dashed because they are a dependency, not a surveyed cable — fibre is recorded on the cable itself now, so none of them ever carries glass. Switch off for a plant-only map where every line is a sheath somebody wrote down."
                onClick={toggleUncabled}>
                <span>Dependency links</span>
                <span className={cn("text-2xs font-medium", showUncabled ? "text-success" : "text-muted-foreground")}>
                  {showUncabled ? "shown" : "hidden"}
                </span>
              </button>
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
              {/* THE CABLE IS FIRST AND IT IS THE ONLY VIOLET ROW. It was
                  missing entirely, which left the one line family this map is
                  now built around — the sheath, in its own identity hue, at its
                  own weight — as the only thing on screen with no entry in the
                  key. Its two states are the pair an ISP has to be able to tell
                  apart at a glance, because one is a surveyed route a crew
                  quotes drum against and the other is an admitted straight
                  line. */}
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
        {/* CABLES get a control of their own, because they are ORG-LEVEL PLANT
            and reaching one used to mean guessing which device it happened to
            touch — through a panel, a button, a submenu and an item. That is why
            the live fleet ended up with two cables both named "main": the first
            attempt confirmed nothing, so it was made again. A trunk is not a
            property of the box at one end of it. */}
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

      {/* CABLES: the list, and one cable's core plan --------------------------
          IN THE DRILL-IN SLOT, not floating on the left. It shipped at
          `top-14 left-3` on the theory that a right-hand panel would cover the
          end of a traced run — and that was wrong twice over, verified in a
          browser against real data. The AUTHORING is in the device panel on the
          right, so opening a cable put the answer ~1000px from where the
          operator was looking and read as "nothing happened"; and that slot
          already belongs to the unplaced drawer AND the site card, so the site
          card rendered straight over the top of it. Every other drill-in in this
          product opens here. A cable is not a device, but this slot is about
          INTERACTION, not taxonomy: it is where "the thing you just opened"
          goes, and being somewhere else is indistinguishable from being broken.

          It takes the slot FROM the device panel while open, with a back arrow
          when it was opened from one — a drill-in with a way back, rather than
          two panels competing for one strip of screen. */}
      {/* Stands down while the route editor has the map, exactly as the device
          panel does — and it MUST, because `railOpen` already excludes an
          editing session: a card rendering while the chrome believes it is
          closed puts the control column back under the panel. */}
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
              // The LIST — the entry point that did not exist. A cable is
              // org-level plant, so reaching one should never require guessing
              // which device it happens to touch. Not having this is why the
              // live fleet has two cables both called "main": the first attempt
              // gave no visible confirmation, so it was made again.
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
                  // FOLLOWING a fibre is not the same as reading the core plan,
                  // and both are offered from the same cell: the plan lights the
                  // sections of THIS sheath, the trace follows the glass across
                  // every closure it is cut at.
                  onTrace={(coreNo) =>
                    setTraceFrom({ cableId: cable.id, coreNo })}
                  // Tracing hands the map over to the editor, so the panel
                  // stands down — the cable stays "open" underneath, and saving
                  // or cancelling brings it straight back.
                  onRetrace={() => setRouteEdit({
                    kind: "cable", cableId: cable.id, name: cable.name,
                    points: cable.path ?? [],
                  })}
                  // Arms the map: the next click on the route is where the
                  // sheath is opened. A mode rather than a drag, because the
                  // coupler does not exist yet — there is nothing to take hold
                  // of — and the point wanted is a place on the street.
                  onSplit={() => setSplitAt({
                    cableId: cable.id, cableName: cable.name })}
                  onLabel={(coreNo, label) =>
                    setCoreLabel.mutate({ cableId: cable.id, coreNo, label: label ?? "" })} />
              )}
            </div>
            )}
          </Card>
        )
      })()}

      {/* Deleting a cable un-claims its spans, so it says what SURVIVES as well
          as what goes — the same shape the un-place confirm was given after a
          benign-looking icon turned out to delete a device's coordinates with no
          warning. Not `requireText`: the spans keep their geometry and rejoining
          a rebuilt cable is a few clicks, so this is reversible enough that
          making somebody type would only train them to type without reading. */}
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

      {/* OPENING A COUPLER — the same banner shape the route editor uses,
          because it is the same kind of moment: the map has stopped being a
          picture and become an input, and the one thing that must never happen
          is an operator clicking on it without knowing that. */}
      {splitAt && (
        <div className="absolute top-14 left-1/2 z-[1000] flex max-w-[min(94vw,44rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-primary/40 bg-popover/95 dark:bg-popover/95 py-1.5 pr-2 pl-3.5 text-xs shadow-none backdrop-blur">
          <Scissors className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 truncate">
            Click where{" "}
            <span className="font-mono font-semibold">{splitAt.cableName}</span>
            {" "}is opened
            {/* SAID, because a click that lands 30 m off the line and comes back
                sitting exactly on it looks like a bug rather than the feature it
                is. The server owns that projection — one owner, or the coupler
                and the two halves drawn from it drift apart. */}
            <span className="text-muted-foreground"> · it snaps to the cable, and
              every core is spliced straight through</span>
          </span>
          <Button variant="ghost" size="icon" className="size-5" title="Cancel (Esc)"
            onClick={() => setSplitAt(null)}>
            <X className="size-3" />
          </Button>
        </div>
      )}

      {/* FOLLOWING A FIBRE. The lit path is on the map; this says what it IS,
          which the map cannot — every hop is the same neutral emphasis, so
          without the words there is no way to tell a two-cable run from a
          six-cable one, or to know where the certainty ran out. */}
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
            {/* A FAULT STOPS THE WALK AND SAYS SO. What is drawn is the part
                that is unambiguous; carrying on past a fork would draw a
                confident line down whichever branch sorted first, and somebody
                would drive to it. */}
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

      {/* THE TRAY. A dialog rather than the right rail, and that is a judgement
          about the WORK, not about the taxonomy: joining fibres is a focused job
          done once at a box, over a two-column layout that cannot fit in a 380px
          panel, while everything the rail holds is a thing you read WITH the
          map. It also opens over the point it is about, which is the check the
          cable panel failed once by opening ~1000px from where the operator was
          looking. */}
      <Dialog open={trayAt != null}
        onOpenChange={(o) => { if (!o) { setTrayAt(null); setTrayError(null) } }}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">
              Inside {fibreQ.data?.point.name ?? "this point"}
            </DialogTitle>
            <DialogDescription className="text-2xs">
              One row per fibre, and what each one does here — spliced onward
              into another sheath, or taken out to a box or a customer. Runs
              that all do the same thing fold into a line.
            </DialogDescription>
          </DialogHeader>
          {fibreQ.data && trayAt != null && (
            <div className="max-h-[65vh] overflow-y-auto pr-1">
              <CouplerTray
                fibre={fibreQ.data}
                canWrite={canWrite}
                busy={setFibreJoint.isPending || spliceThrough.isPending
                      || clearFibreJoint.isPending || takeCoreToBox.isPending}
                error={trayError}
                boxes={trayBoxes}
                people={trayPeople}
                onClearError={() => setTrayError(null)}
                onJoin={(a, b) => setFibreJoint.mutate({ point: trayAt, a, b })}
                onTail={(a, to) =>
                  takeCoreToBox.mutate({ point: trayAt, a, to })}
                onThrough={(a, b) => spliceThrough.mutate({ point: trayAt, a, b })}
                onClear={(f) => clearFibreJoint.mutate({
                  point: trayAt, cableId: f.cableId, coreNo: f.coreNo })}
                onTrace={(f) => {
                  // Leaving the tray for the map is the point of the button, so
                  // it closes: a lit path under a modal is a path nobody can see.
                  setTrayAt(null)
                  setTraceCore(null)
                  setTraceFrom({ cableId: f.cableId, coreNo: f.coreNo })
                }} />
            </div>
          )}
          {fibreQ.isLoading && (
            <p className="py-6 text-center text-2xs text-faint-foreground">Reading…</p>
          )}
        </DialogContent>
      </Dialog>

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
              // Tracing takes over the map, so the card that launched it stands
              // down — same reason the device panel hides during a route edit:
              // it occupies the right rail, which is map you need to click
              // waypoints onto, and a drop often runs straight under it.
              onTraceDrop: (m) => {
                const place = places.find((x) => x.mac === m)
                if (place) startDropRouteEdit(place)
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
      {selected && !routeEdit && cableOpen == null && !cableList && (
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
                  {/* DELETE, for PLANT only, and the second way in to it: the
                      right-click menu is where this feature lives, and a context
                      menu is undiscoverable on its own. Gear is not offered it
                      here for the same reason it isn't offered there. Un-pin
                      stays beside it because they are genuinely different acts
                      on a box that was put in the wrong place: one says the
                      coordinate is wrong, the other that the box isn't there. */}
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
          {/* WHAT FIBRE LANDS ON THIS BOX, and the one place a box gets joined
              to the network now.
              
              It sits directly under the coordinates because those are the two
              facts a freshly placed box has and lacks: it is somewhere, and it
              is spliced to nothing. Putting the fix for the second right under
              the first is what stops "no line appeared" reading as a failed
              save. Below the map's own read-outs would bury it; above the
              coordinates would put a network fact ahead of a ground one. */}
          {(canWrite || deviceCables.length > 0) && (
            <div className="space-y-1.5 border-b px-4 py-2.5">
              <p className="wisp-eyebrow">Fibre</p>
              {deviceCables.length === 0 ? (
                <p className="text-2xs text-faint-foreground">
                  No cable is recorded as ending here. Lay one on the map — that
                  is what joins a box to the network now.
                </p>
              ) : (
                <ul className="space-y-0.5">
                  {deviceCables.map(({ cable, far }) => (
                    <li key={cable.id}>
                      <button type="button"
                        onClick={() => { setCableList(false); setCableOpen(cable.id) }}
                        className="flex w-full items-baseline gap-2 rounded px-1 py-1 text-left hover:bg-foreground/5">
                        <span className="truncate text-xs">{cable.name}</span>
                        {cable.cores != null && (
                          <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                            {cable.cores}F
                          </span>
                        )}
                        <span className="ml-auto shrink-0 truncate text-2xs text-faint-foreground">
                          {/* The far end is NAMED, never hidden. A cable with an
                              unplaced end is real and recorded; the reason no
                              line is drawn is that nobody has put the other
                              point on the map — a different problem. */}
                          {far.name ?? "far end unplaced"}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {/* THE TRAY is the one place a box is joined to the network, so
                  the way in sits right under the coordinates: those are the two
                  facts a freshly placed box has and lacks — it is somewhere, and
                  it is spliced to nothing. */}
              {deviceCables.length > 0 && (
                <Button size="sm" variant="outline" className="w-full"
                  onClick={() => { setTrayError(null)
                                   setTrayAt({ device_id: selected.id }) }}>
                  <Waypoints className="size-3" />
                  {canWrite ? "Open the tray" : "What is joined here"}
                </Button>
              )}
            </div>
          )}
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
          near={menuFeeder}
          dropOn={menuDropOn}
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
            setSelectedId(null)
          }}
          onCable={(lat, lng, on) => {
            // A NEW cable: the route is drawn first and named on save. `cableId`
            // null is what says so, and it is the whole reason this editor kind
            // takes a nullable id.
            //
            // Starting ON a pin records that box as end A immediately — the
            // commonest thing a trunk does is leave a box, and inferring it back
            // from the coordinate afterwards is exactly the guess a cable's ends
            // exist to stop.
            setPlantMenu(null)
            setSelectedId(null)
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
            setSelectedId(d.id)
            setPlantMenu(null)
          }}
          onDelete={(d) => {
            setPlantMenu(null)
            setPlantDelete(d)
            confirmPlantDelete.ask()
          }}
        />
      )}
      {/* Named, and it says what goes rather than only that something will.
          A splitter's drops and the cables ending on it are swept with it, and
          neither is visible from the pin being right-clicked. The children case
          is a REFUSAL, so the dialog states it and the button says Close: the
          server would answer ok:false anyway, and a confirm that can only fail
          is worse than one that explains itself. No `requireText`: plant is
          re-recorded in a few clicks, and typing on a routine delete trains
          people to type without reading. */}
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
      {/* NAMING A CABLE YOU HAVE JUST TRACED.
      
          A DIALOG, and it has to be one. The route editor owns the whole map
          while it is open — the cable panel is closed and the drill-in rail is
          gone — so the panel-embedded form this shared with a RENAME rendered
          nowhere at all, and pressing Save looked like pressing nothing. That
          is the third time this feature has produced a "nothing is happening"
          from a surface opening somewhere the operator was not: check where a
          thing DRAWS, never just that the handler fired.
          
          It is also the right shape on its own terms — this is the terminal
          step of a gesture, it must not be missable, and the traced line stays
          visible behind it because that is what is being named. Cancel returns
          to the editor with the route intact rather than discarding a survey. */}
      {canWrite && cableForm && cableForm.id == null && (
        <Dialog open onOpenChange={(v) => { if (!v) setCableForm(null) }}>
          <DialogContent className="sm:max-w-sm">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Spline className="size-4 text-muted-foreground" />
                Name this cable
              </DialogTitle>
              <DialogDescription>
                {/* WHAT THE ENDS CAUGHT, said before it is saved. An end that
                    landed on open ground becomes a coupler, and that is a device
                    appearing on the map — so it is announced here rather than
                    discovered afterwards as two pins nobody asked for. */}
                {cableForm.path?.length ?? 0} points traced.
                {cableForm.ends && (cableForm.ends[0] == null || cableForm.ends[1] == null)
                  ? " A coupler is created at each end that lands on open ground."
                  : " It runs between the two points you clicked."}
              </DialogDescription>
            </DialogHeader>
            <CableForm initial={cableForm} busy={saveCable.isPending}
              ends={[cableForm.ends?.[0]?.name ?? "new coupler",
                     cableForm.ends?.[1]?.name ?? "new coupler"]}
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
