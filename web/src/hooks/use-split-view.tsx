// Split view: two pages side by side (or stacked) in one window.
//
// THE ASYMMETRY IS THE DESIGN, not an implementation shortcut. The PRIMARY pane
// is the URL — it is `<Outlet/>` under the app's own HashRouter, so every deep
// link, bookmark, back/forward press and `?kind=`/`?onu=` param keeps working
// exactly as it did before this feature existed. The SECONDARY pane is a
// separate MemoryRouter with no address of its own. That buys three things:
//
//   1. Nothing about the existing URL contract changes. A second page in the
//      query string (`?split=/map`) would have to carry that page's OWN params
//      too, and the encoding of a route-inside-a-route is exactly the kind of
//      thing that silently breaks a shared link months later.
//   2. Navigation inside the secondary pane stays inside it — clicking a Home
//      tile there opens /issues in that pane, not under the operator's feet.
//   3. The split is a WORKSPACE preference, so it persists like one
//      (localStorage, beside the map's layer toggles and the panel widths)
//      rather than like a location.
//
// The cost, accepted: a split layout is not shareable by pasting a URL. That is
// the right trade — an operator sets this up once and lives in it.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react"
import { useLocation, useNavigate } from "react-router-dom"

/** Which way the divider runs.
 *
 *  Named for the LAYOUT, never for the divider. "Vertical split" means
 *  side-by-side to half the world and stacked to the other half, and the two
 *  camps each think the other is obviously wrong — so the words that reach the
 *  operator are "Side by side" and "Stacked", and the type spells the same
 *  thing. Nothing here is called vertical or horizontal. */
export type SplitAxis = "side" | "stacked"

/** Floors, in CSS px, below which a pane stops being a view of anything.
 *
 *  Measured against the narrowest thing each page has to show, not picked round:
 *  380 is `.wisp-device-panel`'s own 26.25rem (420px) shrunk to the point where
 *  the tree beside it still shows a name and a status dot, and 260 is a map with
 *  its top strip, its control column and about six rows of ground left over.
 *  Below either, the pane renders but nothing in it can be read — which is worse
 *  than refusing the split, because the operator concludes the page is broken. */
export const MIN_PANE_W = 380
export const MIN_PANE_H = 260
/** The divider's own hit strip — counted in the room arithmetic below, or a
 *  split that "just fits" fits by 8px too little. */
export const SPLIT_GRIP = 8

const STORE_KEY = "wisp:split"

interface StoredSplit {
  view: string
  axis: SplitAxis
  fraction: number
}

interface PaneState {
  /** What the MemoryRouter was MOUNTED with. Changes only when the operator
   *  picks a different view, because it is the remount key — see `epoch`. */
  entry: string
  /** Where that pane's router has since navigated to. This is what gets
   *  persisted, so a reload comes back where the pane actually was and not
   *  where it was opened. */
  live: string
  /** Bumped only by `open`. The MemoryRouter is keyed on it, so picking a view
   *  remounts the pane while the pane navigating itself does NOT — keying on
   *  the path instead would remount the pane every time it moved, i.e. an
   *  infinite loop between `live` and `initialEntries`. */
  epoch: number
}

export interface SplitBox {
  w: number
  h: number
}

export interface SplitContextValue {
  /** Live path of the secondary pane, or null when there is no split. */
  view: string | null
  /** What to mount, and the key to mount it under. */
  entry: string | null
  epoch: number
  axis: SplitAxis
  /** Primary pane's share of the host box, 0..1. */
  fraction: number

  /** Attach to the element the two panes divide up. Everything below measures
   *  THAT box, not the window: the sidebar collapsing changes how much room a
   *  side-by-side split has, and the window does not know that. */
  hostRef: (el: HTMLElement | null) => void
  box: SplitBox

  /** Is there physically room for this layout right now? */
  roomFor: (axis: SplitAxis) => boolean
  /** Room for the layout currently selected. A split with no room COLLAPSES to
   *  the primary pane and keeps its settings — see `active`. */
  active: boolean

  open: (view: string, axis?: SplitAxis) => void
  close: () => void
  setAxis: (axis: SplitAxis) => void
  setFraction: (f: number) => void
  /** Trade places: the URL goes where the secondary pane was and vice versa. */
  swap: () => void

  /** Called from INSIDE the secondary pane's own router when it navigates, so
   *  the persisted view follows it. Nothing else may call this — it deliberately
   *  does not bump `epoch`, so it can never remount the pane it is reporting
   *  from. */
  reportPaneLocation: (path: string) => void
}

const SplitContext = createContext<SplitContextValue | null>(null)

function readStored(): StoredSplit | null {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as Partial<StoredSplit>
    if (typeof v?.view !== "string" || !v.view) return null
    return {
      view: v.view,
      axis: v.axis === "stacked" ? "stacked" : "side",
      // A hand-edited or older value must degrade to the middle rather than to
      // NaN, which would make every clamp below return NaN and render a pane
      // with no size at all.
      fraction: Number.isFinite(v.fraction) ? clamp01(v.fraction as number) : 0.5,
    }
  } catch {
    return null
  }
}

const clamp01 = (f: number) => Math.max(0.05, Math.min(0.95, f))

export function SplitProvider({ children }: { children: ReactNode }) {
  // `useState(readStored)` — the LAZY form, passing the function rather than
  // calling it. This provider wraps the whole app, and `useRef(readStored())`
  // would hit localStorage and parse JSON on every render of every page for a
  // value that is only ever read once.
  const [stored] = useState(readStored)
  const [pane, setPane] = useState<PaneState | null>(() =>
    stored ? { entry: stored.view, live: stored.view, epoch: 1 } : null,
  )
  const [axis, setAxisState] = useState<SplitAxis>(() => stored?.axis ?? "side")
  const [fraction, setFractionState] = useState(() => stored?.fraction ?? 0.5)
  const [box, setBox] = useState<SplitBox>({ w: 0, h: 0 })

  const navigate = useNavigate()
  const location = useLocation()

  // A ResizeObserver on the host, not a window resize listener: the box changes
  // when the sidebar collapses or the billing banner appears, neither of which
  // fires a resize. Callback ref rather than an object ref so mounting the host
  // (which only happens once a split is open) starts the observation without a
  // second effect chasing `.current`.
  const observer = useRef<ResizeObserver | null>(null)
  // Bails when the box hasn't actually moved. A ResizeObserver fires for any
  // reason its target reflows, and a fresh object every time would re-render the
  // whole shell (both panes, the map among them) on frames where nothing
  // changed — and, with a ref callback that isn't stable, would not terminate at
  // all: measure → setState → re-render → re-attach → measure.
  const measure = useCallback((w: number, h: number) => {
    setBox((prev) => (prev.w === w && prev.h === h ? prev : { w, h }))
  }, [])
  const hostRef = useCallback((el: HTMLElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      const r = entry.contentRect
      measure(Math.round(r.width), Math.round(r.height))
    })
    ro.observe(el)
    observer.current = ro
    const r = el.getBoundingClientRect()
    measure(Math.round(r.width), Math.round(r.height))
  }, [measure])
  useEffect(() => () => observer.current?.disconnect(), [])

  // Persistence is a single write of the LIVE state, so a pane that wandered to
  // /issues?kind=port_down comes back there.
  useEffect(() => {
    try {
      if (!pane) localStorage.removeItem(STORE_KEY)
      else localStorage.setItem(STORE_KEY, JSON.stringify({ view: pane.live, axis, fraction }))
    } catch { /* private mode / quota */ }
  }, [pane, axis, fraction])

  const roomFor = useCallback((a: SplitAxis) => {
    // Before the host has been measured there is no split to have room for —
    // report false rather than guessing, so the menu never offers a layout that
    // vanishes on the next frame.
    if (!box.w || !box.h) return false
    return a === "side"
      ? box.w >= MIN_PANE_W * 2 + SPLIT_GRIP
      : box.h >= MIN_PANE_H * 2 + SPLIT_GRIP
  }, [box])

  const open = useCallback((view: string, a?: SplitAxis) => {
    setPane((prev) => ({ entry: view, live: view, epoch: (prev?.epoch ?? 0) + 1 }))
    if (a) setAxisState(a)
  }, [])

  const close = useCallback(() => setPane(null), [])

  const swap = useCallback(() => {
    if (!pane) return
    // Both sides carry their FULL path including the query, or a swap silently
    // drops the `?kind=` an operator had filtered to.
    const here = location.pathname + location.search
    // The primary pane IS the URL, so trading places means navigating the real
    // router. Done OUTSIDE the updater on purpose: a setState updater has to be
    // pure (React may call it twice), and navigating from inside one would fire
    // the navigation twice under StrictMode.
    navigate(pane.live)
    setPane((prev) => (prev ? { entry: here, live: here, epoch: prev.epoch + 1 } : prev))
  }, [pane, navigate, location.pathname, location.search])

  const reportPaneLocation = useCallback((path: string) => {
    setPane((prev) => (!prev || prev.live === path ? prev : { ...prev, live: path }))
  }, [])

  const value = useMemo<SplitContextValue>(() => ({
    view: pane?.live ?? null,
    entry: pane?.entry ?? null,
    epoch: pane?.epoch ?? 0,
    axis,
    fraction,
    hostRef,
    box,
    roomFor,
    active: !!pane && roomFor(axis),
    open,
    close,
    setAxis: setAxisState,
    setFraction: (f) => setFractionState(clamp01(f)),
    swap,
    reportPaneLocation,
  }), [pane, axis, fraction, hostRef, box, roomFor, open, close, swap, reportPaneLocation])

  return <SplitContext.Provider value={value}>{children}</SplitContext.Provider>
}

export function useSplit(): SplitContextValue {
  const ctx = useContext(SplitContext)
  if (!ctx) throw new Error("useSplit must be used inside SplitProvider")
  return ctx
}
