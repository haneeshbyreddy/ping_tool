// Replay's chrome: the way in, the transport, and the banner that must never
// come off. Everything user-visible here is written without em-dashes, per the
// house copy rule.
import { useEffect, useRef } from "react"
import {
  ChevronLeft, ChevronRight, History, Pause, Play, X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { fmtDateTime } from "@/lib/format"
import { REPLAY_WINDOWS } from "@/lib/replay-api"
import { ACCUM_LABELS, accumColor } from "@/map/replay"
import { cn } from "@/lib/utils"

export function ReplayButton({ on, onClick }: { on: boolean; onClick: () => void }) {
  // ITS OWN VISIBLE CONTROL, not an entry inside Layers. Layers governs what
  // is DRAWN on a live map; this changes what the map IS ABOUT, and a mode
  // that big may not hide inside a popover. It joins the existing control
  // column so its position follows whatever else that column is rendering —
  // a hardcoded top offset is the documented way to land on another button.
  return (
    <Button variant={on ? "default" : "outline"} size="icon"
      className={cn("size-8 backdrop-blur", !on && "bg-popover/95 dark:bg-popover/95")}
      title={on ? "Leave replay and go back to live" : "Replay: see the map as it was"}
      onClick={onClick}>
      <History className="size-3.5" />
    </Button>
  )
}

export function ReplayDock({
  at, since, until, days, playing, recordingSince, canStepBack, canStepOn,
  loading, onScrub, onDays, onPlay, onStep, onExit, toggle, children,
}: {
  at: number
  since: number
  until: number
  days: number
  playing: boolean
  recordingSince: number | null
  canStepBack: boolean
  canStepOn: boolean
  loading?: boolean
  onScrub: (t: number) => void
  onDays: (d: number) => void
  onPlay: () => void
  onStep: (dir: -1 | 1) => void
  onExit: () => void
  toggle?: React.ReactNode
  children?: React.ReactNode
}) {
  // Escape leaves replay, like every other map mode; the arrows step and space
  // plays. Held in a ref so the listener binds ONCE: the callers are inline
  // arrows and this page re-renders on a 15-second clock, which would
  // otherwise rebind a window listener on every tick.
  const keys = useRef({ onExit, onStep, onPlay })
  keys.current = { onExit, onStep, onPlay }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return
      if (e.key === "Escape") keys.current.onExit()
      else if (e.key === "ArrowLeft") { e.preventDefault(); keys.current.onStep(-1) }
      else if (e.key === "ArrowRight") { e.preventDefault(); keys.current.onStep(1) }
      // Space activates a focused button; taking it away from one would break
      // the transport's own controls.
      else if (e.key === " " && !(el instanceof HTMLButtonElement)) {
        e.preventDefault()
        keys.current.onPlay()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const stamp = fmtDateTime(new Date(at * 1000).toISOString())
  const truncated = recordingSince != null && recordingSince > since

  return (
    <div className="absolute inset-x-2 bottom-2 z-[1002] flex max-h-[52%] flex-col overflow-hidden rounded-xl border border-primary/40 bg-popover/95 backdrop-blur [--marey-gutter:6.5rem] md:inset-x-3 md:[--marey-gutter:11rem] dark:bg-popover/95">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b px-3 py-2">
        {/* THE BANNER. It is the whole safety of this feature: the worst thing
            this product could show is a past map read as now, so the sentence
            says the time AND says it is not live, in the primary tone, and it
            cannot be dismissed while replay is on. */}
        <span className="flex min-w-0 items-center gap-2 text-xs">
          <History className="size-3.5 shrink-0 text-primary" />
          <span className="truncate">
            <span className="text-muted-foreground">Viewing </span>
            <span className="font-medium tabular-nums text-foreground">{stamp}</span>
            <span className="text-primary"> · not live</span>
          </span>
        </span>

        <div className="ml-auto flex items-center gap-1.5">
          {toggle}
          <div className="flex overflow-hidden rounded-md border">
            {REPLAY_WINDOWS.map((d) => (
              <button key={d} type="button" onClick={() => onDays(d)}
                className={cn("px-2 py-1 text-2xs",
                  days === d ? "bg-selected font-medium text-foreground"
                             : "text-muted-foreground hover:bg-foreground/5")}>
                {d === 1 ? "24h" : `${d}d`}
              </button>
            ))}
          </div>
          {/* STEP LANDS ON THE NEXT TRANSITION, never on a uniform tick.
              A fleet is unchanged for most of any window, so a fixed step is
              mostly an operator watching dead air; the reconstruction knows
              exactly when something moved. */}
          <Button variant="outline" size="icon" className="size-7"
            title="Back to the previous change" disabled={loading || !canStepBack}
            onClick={() => onStep(-1)}>
            <ChevronLeft className="size-3.5" />
          </Button>
          <Button variant="outline" size="icon" className="size-7"
            title={playing ? "Pause" : "Play"} disabled={loading} onClick={onPlay}>
            {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
          </Button>
          <Button variant="outline" size="icon" className="size-7"
            title="On to the next change" disabled={loading || !canStepOn}
            onClick={() => onStep(1)}>
            <ChevronRight className="size-3.5" />
          </Button>
          <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-2xs"
            title="Back to live" onClick={onExit}>
            <X className="size-3" />
            Live
          </Button>
        </div>

        <input
          type="range" aria-label="Replay position" disabled={loading}
          min={since} max={until} step={60} value={at}
          onChange={(e) => onScrub(Number(e.target.value))}
          className="w-full accent-primary" />

        {loading && (
          // While the record is still being read every pin is `unknown`, and
          // the map says so in its own grammar. This says why.
          <p className="w-full text-2xs text-muted-foreground">
            Reading the record. Until it lands, nothing here can be claimed.
          </p>
        )}

        {!loading && truncated && (
          // A DESIGNED EMPTY STATE, never a blank left edge: the window runs
          // past where this org's record begins, and the Marey draws that
          // stretch as the dead zone. Say why.
          <p className="w-full text-2xs text-muted-foreground">
            Recording since {fmtDateTime(new Date(recordingSince * 1000).toISOString())}.
            Anything earlier is not recorded, and shows as not recorded.
          </p>
        )}
      </div>

      {children}
    </div>
  )
}

export function AccumLegend({ days }: { days: number }) {
  return (
    <div className="px-2 pt-1 pb-1.5">
      <p className="pb-1 text-2xs text-muted-foreground">
        Share of the last {days === 1 ? "24 hours" : `${days} days`} spent down.
      </p>
      {ACCUM_LABELS.map((label, i) => (
        <div key={label} className="flex items-center gap-2 px-0 py-0.5 text-xs">
          <span className="flex w-4 shrink-0 items-center justify-center">
            <span aria-hidden className="size-3 rounded-full"
              style={{ background: accumColor(i + 1) }} />
          </span>
          <span className="text-muted-foreground">{label}</span>
        </div>
      ))}
    </div>
  )
}
