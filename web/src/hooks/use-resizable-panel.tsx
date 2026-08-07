// Drag-to-resize for the device side panel, shared by the Network page and the
// Map's pin panel. It lives here rather than in either page for the same reason
// `device-detail.tsx` does: two copies of this drift, and the parts that matter
// are the ones that look incidental (the ref shadow, the double clamp).
import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react"

/** Gap between the panel's outer edge and the viewport — matches `right-3`. */
export const PANEL_GUTTER = 12

export interface PanelGripProps {
  role: "separator"
  "aria-orientation": "vertical"
  "aria-label": string
  title: string
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => void
  onPointerMove: (e: ReactPointerEvent<HTMLDivElement>) => void
  onPointerUp: (e: ReactPointerEvent<HTMLDivElement>) => void
  onPointerCancel: (e: ReactPointerEvent<HTMLDivElement>) => void
  onDoubleClick: () => void
}

export interface ResizablePanel {
  width: number
  /** Set on an ancestor: `--wisp-panel-w` sizes the panel, `--wisp-panel-clear`
   *  is the gutter chrome beside it must keep (0 while the panel is closed). */
  vars: CSSProperties
  grip: PanelGripProps
}

export function useResizablePanel(opts: {
  /** localStorage key — each surface keeps its OWN width. A map panel and a
   *  full-page panel have different room to spend, and one shared number would
   *  mean sizing the map's panel resized the Network page's behind your back. */
  storageKey: string
  defaultWidth: number
  min: number
  max: number
  /** false while nothing is open — zeroes the gutter so chrome moves back. */
  open: boolean
}): ResizablePanel {
  const { storageKey, defaultWidth, min, max, open } = opts

  // Clamped against the VIEWPORT as well as the caller's ceiling: a width
  // dragged on a 27" monitor otherwise comes back on a laptop wider than the
  // window it has to share.
  const clamp = (w: number) =>
    Math.max(min, Math.min(Math.min(max, Math.round(window.innerWidth * 0.6)), w))

  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem(storageKey))
      return Number.isFinite(v) && v > 0 ? clamp(v) : defaultWidth
    } catch {
      return defaultWidth
    }
  })
  // Shadows the state because the pointerup that persists reads a value the
  // pointermove just set — through the render closure that would be one drag
  // step stale, saving a width the user never let go at.
  const widthRef = useRef(width)
  const resizing = useRef(false)

  const save = (w: number) => {
    try { localStorage.setItem(storageKey, String(w)) } catch { /* private mode / quota */ }
  }
  const apply = (w: number) => {
    const next = clamp(w)
    widthRef.current = next
    setWidth(next)
  }

  // A saved width outlives the window it was dragged in.
  useEffect(() => {
    const onResize = () => setWidth((w) => {
      const next = clamp(w)
      widthRef.current = next
      return next
    })
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [min, max])

  return {
    width,
    vars: {
      "--wisp-panel-w": `${width}px`,
      "--wisp-panel-clear": open ? `${width + PANEL_GUTTER * 2}px` : "0px",
    } as CSSProperties,
    grip: {
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize panel",
      title: "Drag to resize · double-click to reset",
      // Pointer events (not mouse) so touchpad, pen and touchscreen all work
      // from one handler, and pointer CAPTURE so a fast drag that outruns the
      // grip keeps resizing instead of dropping the gesture on leaving it.
      onPointerDown: (e) => {
        e.preventDefault() // stops the drag selecting the text behind it
        e.currentTarget.setPointerCapture(e.pointerId)
        resizing.current = true
      },
      onPointerMove: (e) => {
        if (!resizing.current) return
        // Measured from the panel's OWN right edge, which is the one thing that
        // holds still during the drag whatever the panel is pinned to. It used
        // to read `window.innerWidth`, which is the same number only while the
        // panel spans the whole window — in a SPLIT PANE the panel is pinned to
        // the pane's right edge instead, and measuring from the window's put the
        // grip a whole neighbouring pane away from the cursor.
        //
        // `offsetParent` is the panel Card itself (the grip is absolutely
        // positioned inside it, and the Card is positioned in every layout).
        // With no offsetParent at all — display:none, or a fixed ancestor chain
        // — the old viewport measurement is the right fallback.
        const host = e.currentTarget.offsetParent as HTMLElement | null
        const rightEdge = host ? host.getBoundingClientRect().right + PANEL_GUTTER : window.innerWidth
        apply(rightEdge - e.clientX - PANEL_GUTTER)
      },
      onPointerUp: (e) => {
        if (!resizing.current) return
        resizing.current = false
        e.currentTarget.releasePointerCapture(e.pointerId)
        save(widthRef.current)
      },
      onPointerCancel: (e) => {
        if (!resizing.current) return
        resizing.current = false
        e.currentTarget.releasePointerCapture(e.pointerId)
        save(widthRef.current)
      },
      onDoubleClick: () => { apply(defaultWidth); save(defaultWidth) },
    },
  }
}

/** The visible drag bar. It has to be DRAWN at rest, not merely hoverable — an
 *  invisible edge is a thing you have to already know about. 14px of hit strip
 *  around a short centred pill; a pill rather than a full-height rule, because a
 *  full-height bar reads as another border on a panel that already has one. */
export function PanelResizeGrip({ grip }: { grip: PanelGripProps }) {
  return (
    <div {...grip}
      className="group absolute inset-y-0 left-0 z-10 hidden w-3.5 cursor-col-resize touch-none md:flex md:items-center md:justify-center">
      <span aria-hidden
        className="h-10 w-1 rounded-full bg-border-strong transition-colors group-hover:bg-foreground/45 group-active:bg-foreground/70" />
    </div>
  )
}
