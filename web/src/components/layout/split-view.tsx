import { useCallback, useEffect, useMemo, useRef, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react"
import { ArrowLeftRight, Check, ChevronDown, Columns2, Rows2, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import {
  MIN_PANE_H, MIN_PANE_W, SPLIT_GRIP, useSplit, type SplitAxis,
} from "@/hooks/use-split-view"
import { PaneRouter, paneViewFor, paneViewsFor } from "./pane-views"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

const KEY_STEP = 0.02

export function SplitView({ children }: { children: React.ReactNode }) {
  const split = useSplit()
  const hostEl = useRef<HTMLDivElement | null>(null)
  const dragging = useRef(false)

  const side = split.axis === "side"

  usePaneViewGuard(split)

  const span = Math.max(0, (side ? split.box.w : split.box.h) - SPLIT_GRIP)
  const floor = side ? MIN_PANE_W : MIN_PANE_H
  const minF = span ? Math.min(0.5, floor / span) : 0.2
  const maxF = span ? Math.max(0.5, 1 - floor / span) : 0.8
  const fraction = Math.max(minF, Math.min(maxF, split.fraction))
  const basis = (f: number) => `calc((100% - ${SPLIT_GRIP}px) * ${f})`

  const setFrom = useCallback((clientX: number, clientY: number) => {
    const el = hostEl.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const usable = (side ? r.width : r.height) - SPLIT_GRIP
    const raw = usable > 0 ? ((side ? clientX - r.left : clientY - r.top) - SPLIT_GRIP / 2) / usable : 0.5
    split.setFraction(Math.max(minF, Math.min(maxF, raw)))
  }, [side, split, minF, maxF])

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault() // or the drag selects the text in whichever pane it crosses
    e.currentTarget.setPointerCapture(e.pointerId)
    dragging.current = true
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    setFrom(e.clientX, e.clientY)
  }
  const endDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    dragging.current = false
    e.currentTarget.releasePointerCapture(e.pointerId)
  }

  const hasRoom = split.roomFor(split.axis)

  const attachHost = useCallback((el: HTMLDivElement | null) => {
    hostEl.current = el
    split.hostRef(el)
  }, [split.hostRef])

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {!hasRoom && <SplitTooSmallNote />}
      <div
        ref={attachHost}
        className={cn("flex min-h-0 flex-1", side ? "flex-row" : "flex-col")}
      >
        <Pane style={hasRoom ? { flexBasis: basis(fraction), flexGrow: 0, flexShrink: 0 } : { flex: "1 1 100%" }}>
          {children}
        </Pane>

        {hasRoom && (
          <div
            role="separator"
            aria-orientation={side ? "vertical" : "horizontal"}
            aria-label="Resize panes"
            aria-valuenow={Math.round(fraction * 100)}
            aria-valuemin={Math.round(minF * 100)}
            aria-valuemax={Math.round(maxF * 100)}
            tabIndex={0}
            title="Drag to resize · double-click to even up"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onDoubleClick={() => split.setFraction(0.5)}
            onKeyDown={(e) => {
              const back = side ? "ArrowLeft" : "ArrowUp"
              const fwd = side ? "ArrowRight" : "ArrowDown"
              if (e.key !== back && e.key !== fwd && e.key !== "Home") return
              e.preventDefault()
              if (e.key === "Home") return split.setFraction(0.5)
              const next = fraction + (e.key === fwd ? KEY_STEP : -KEY_STEP)
              split.setFraction(Math.max(minF, Math.min(maxF, next)))
            }}
            style={{ flex: `0 0 ${SPLIT_GRIP}px` }}
            className={cn(
              "group relative z-20 flex shrink-0 touch-none items-center justify-center bg-border/60",
              "outline-none focus-visible:bg-primary/60",
              side ? "cursor-col-resize" : "cursor-row-resize",
            )}
          >
            <span aria-hidden className={cn(
              "rounded-full bg-border-strong transition-colors group-hover:bg-foreground/45 group-active:bg-foreground/70 group-focus-visible:bg-primary",
              side ? "h-10 w-1" : "h-1 w-10",
            )} />
          </div>
        )}

        {hasRoom && split.entry && (
          <Pane style={{ flexBasis: basis(1 - fraction), flexGrow: 0, flexShrink: 0 }} bar={<PaneBar />}>
            <PaneRouter key={split.epoch} entry={split.entry} />
          </Pane>
        )}
      </div>
    </div>
  )
}

function usePaneViews() {
  const { user } = useAuth()
  const isWorker = !!user && !user.is_superadmin && user.role === "worker"
  const isSuperadmin = !!user?.is_superadmin
  return useMemo(
    () => paneViewsFor({ isSuperadmin, isWorker }),
    [isSuperadmin, isWorker],
  )
}

function usePaneViewGuard(split: ReturnType<typeof useSplit>) {
  const views = usePaneViews()
  const { view, open } = split
  useEffect(() => {
    if (!view) return
    const match = paneViewFor(view)
    if (match && views.some((v) => v.to === match.to)) return
    open("/")
  }, [view, views, open])
}

function Pane({ children, style, bar }: {
  children: React.ReactNode
  style?: CSSProperties
  bar?: React.ReactNode
}) {
  return (
    <div
      data-pane
      style={{ ...style, "--wisp-pane-h": "100%" } as CSSProperties}
      className="relative flex min-h-0 min-w-0 flex-col overflow-hidden"
    >
      {bar}
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  )
}

function PaneBar() {
  const split = useSplit()
  const views = usePaneViews()
  const current = split.view ? paneViewFor(split.view) : null
  const Icon = current?.icon

  return (
    <div className="flex h-9 shrink-0 items-center gap-1 border-b bg-sidebar px-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button className="flex min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground">
            {Icon && <Icon className="size-3.5 shrink-0" />}
            <span className="truncate">{current?.label ?? "View"}</span>
            <ChevronDown className="size-3 shrink-0 opacity-60" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-48">
          <DropdownMenuLabel className="text-2xs">Show in this pane</DropdownMenuLabel>
          {views.map((v) => (
            <DropdownMenuItem key={v.to} onSelect={() => split.open(v.to)}>
              <v.icon />
              {v.label}
              {current?.to === v.to && <Check className="ml-auto size-3.5" />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => split.setAxis(split.axis === "side" ? "stacked" : "side")}>
            {split.axis === "side" ? <Rows2 /> : <Columns2 />}
            {split.axis === "side" ? "Stack them" : "Put side by side"}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <div className="flex-1" />

      <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
        title="Swap panes" aria-label="Swap panes" onClick={split.swap}>
        <ArrowLeftRight className="size-3.5" />
      </Button>
      <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
        title="Close this pane" aria-label="Close this pane" onClick={split.close}>
        <X className="size-3.5" />
      </Button>
    </div>
  )
}

export function SplitControl() {
  const split = useSplit()
  const views = usePaneViews()

  const canSide = split.roomFor("side")
  const canStacked = split.roomFor("stacked")
  const measured = split.box.w > 0
  const roughSide = measured ? canSide : window.innerWidth >= MIN_PANE_W * 2 + SPLIT_GRIP + 220
  const roughStacked = measured ? canStacked : window.innerHeight >= MIN_PANE_H * 2 + SPLIT_GRIP + 56

  if (!roughSide && !roughStacked) return null

  const openIn = (to: string, axis: SplitAxis) => split.open(to, axis)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon"
          className={cn("hidden size-8 text-muted-foreground md:inline-flex",
            split.active && "bg-foreground/[0.07] text-foreground")}
          title="Split view" aria-label="Split view">
          {split.axis === "side" ? <Columns2 className="size-4" /> : <Rows2 className="size-4" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        {split.view ? (
          <>
            <DropdownMenuLabel className="text-2xs">Layout</DropdownMenuLabel>
            <DropdownMenuItem disabled={!roughSide} onSelect={() => split.setAxis("side")}>
              <Columns2 />
              Side by side
              {split.axis === "side" && <Check className="ml-auto size-3.5" />}
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!roughStacked} onSelect={() => split.setAxis("stacked")}>
              <Rows2 />
              Stacked
              {split.axis === "stacked" && <Check className="ml-auto size-3.5" />}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={split.swap}>
              <ArrowLeftRight />
              Swap panes
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={split.close}>
              <X />
              Close split
            </DropdownMenuItem>
          </>
        ) : (
          <>
            <DropdownMenuLabel className="text-2xs">
              Open a second pane {roughSide ? "beside" : "below"} this one
            </DropdownMenuLabel>
            {views.map((v) => (
              <DropdownMenuItem key={v.to}
                onSelect={() => openIn(v.to, roughSide ? "side" : "stacked")}>
                <v.icon />
                {v.label}
              </DropdownMenuItem>
            ))}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function SplitTooSmallNote() {
  const split = useSplit()
  const other: SplitAxis = split.axis === "side" ? "stacked" : "side"
  const canOther = split.roomFor(other)
  return (
    <div className="flex items-center gap-2 border-b bg-muted/40 px-4 py-1.5 text-2xs text-muted-foreground">
      <span>
        Not enough room for a {split.axis === "side" ? "side-by-side" : "stacked"} split. Your
        layout is kept and returns at a larger size.
      </span>
      {canOther && (
        <button className="font-medium text-foreground underline-offset-2 hover:underline"
          onClick={() => split.setAxis(other)}>
          {other === "side" ? "Put side by side" : "Stack instead"}
        </button>
      )}
      <button className="ml-auto shrink-0 font-medium text-foreground underline-offset-2 hover:underline"
        onClick={split.close}>
        Close split
      </button>
    </div>
  )
}
