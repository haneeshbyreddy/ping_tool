import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react"
import { useLocation, useNavigate } from "react-router-dom"

export type SplitAxis = "side" | "stacked"

export const MIN_PANE_W = 380
export const MIN_PANE_H = 260
export const SPLIT_GRIP = 8

const STORE_KEY = "wisp:split"

interface StoredSplit {
  view: string
  axis: SplitAxis
  fraction: number
}

interface PaneState {
  entry: string
  live: string
  epoch: number
}

export interface SplitBox {
  w: number
  h: number
}

export interface SplitContextValue {
  view: string | null
  entry: string | null
  epoch: number
  axis: SplitAxis
  fraction: number

  hostRef: (el: HTMLElement | null) => void
  box: SplitBox

  roomFor: (axis: SplitAxis) => boolean
  active: boolean

  open: (view: string, axis?: SplitAxis) => void
  close: () => void
  setAxis: (axis: SplitAxis) => void
  setFraction: (f: number) => void
  swap: () => void

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
      fraction: Number.isFinite(v.fraction) ? clamp01(v.fraction as number) : 0.5,
    }
  } catch {
    return null
  }
}

const clamp01 = (f: number) => Math.max(0.05, Math.min(0.95, f))

export function SplitProvider({ children }: { children: ReactNode }) {
  const [stored] = useState(readStored)
  const [pane, setPane] = useState<PaneState | null>(() =>
    stored ? { entry: stored.view, live: stored.view, epoch: 1 } : null,
  )
  const [axis, setAxisState] = useState<SplitAxis>(() => stored?.axis ?? "side")
  const [fraction, setFractionState] = useState(() => stored?.fraction ?? 0.5)
  const [box, setBox] = useState<SplitBox>({ w: 0, h: 0 })

  const navigate = useNavigate()
  const location = useLocation()

  const observer = useRef<ResizeObserver | null>(null)
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

  useEffect(() => {
    try {
      if (!pane) localStorage.removeItem(STORE_KEY)
      else localStorage.setItem(STORE_KEY, JSON.stringify({ view: pane.live, axis, fraction }))
    } catch { /* private mode / quota */ }
  }, [pane, axis, fraction])

  const roomFor = useCallback((a: SplitAxis) => {
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
    const here = location.pathname + location.search
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
