import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react"

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
  vars: CSSProperties
  grip: PanelGripProps
}

export function useResizablePanel(opts: {
  storageKey: string
  defaultWidth: number
  min: number
  max: number
  open: boolean
}): ResizablePanel {
  const { storageKey, defaultWidth, min, max, open } = opts

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
      onPointerDown: (e) => {
        e.preventDefault() // stops the drag selecting the text behind it
        e.currentTarget.setPointerCapture(e.pointerId)
        resizing.current = true
      },
      onPointerMove: (e) => {
        if (!resizing.current) return
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

export function PanelResizeGrip({ grip }: { grip: PanelGripProps }) {
  return (
    <div {...grip}
      className="group absolute inset-y-0 left-0 z-10 hidden w-3.5 cursor-col-resize touch-none md:flex md:items-center md:justify-center">
      <span aria-hidden
        className="h-10 w-1 rounded-full bg-border-strong transition-colors group-hover:bg-foreground/45 group-active:bg-foreground/70" />
    </div>
  )
}
