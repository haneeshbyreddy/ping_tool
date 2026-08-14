// The availability strip: HourStrip's grammar stretched to day grain over an
// arbitrary range. DOM cells, not SVG — the tooltip is the native title, the
// tones are the status vocabulary, and the gap/unknown states carry non-colour
// channels (border-only, neutral) so the strip survives greyscale.
//
// Cell states, each a different sentence (the five-state table for this chart):
//   red     ≥1h of DOWN outage that day (a failure claim — status tone)
//   amber   any DOWN outage under an hour
//   green   no outage AND the probe is known to have reported (coverage > 0)
//   bordered empty  coverage known to be ZERO — the probe was silent all day
//   neutral no outage recorded, probe coverage UNKNOWN (before the historian /
//           past the rollups' 30 days) — must not render like measured-ok
import { cn } from "@/lib/utils"
import { DAY_MS, epochDayMs, fmtDay, fmtDurS } from "./scale"

export interface DayCell {
  downS: number
  outages: number
}

export function DayStrip({ sinceMs, untilMs, days, coverage, className }: {
  sinceMs: number
  untilMs: number
  days: Map<number, DayCell>
  coverage: Map<number, number> | null
  className?: string
}) {
  const first = epochDayMs(sinceMs)
  const cells: number[] = []
  for (let d = first; d < untilMs; d += DAY_MS) cells.push(d)
  return (
    <div className={cn("flex gap-px", className)} role="img"
      aria-label="daily availability">
      {cells.map((d) => {
        const cell = days.get(d)
        const samples = coverage?.get(d)
        const label = fmtDay(d)
        if (cell && cell.downS > 0) {
          const long = cell.downS >= 3600
          return <span key={d}
            title={`${label}: down ${fmtDurS(cell.downS)} (${cell.outages} outage${cell.outages > 1 ? "s" : ""})`}
            className={cn("h-4 min-w-0 flex-1 rounded-[2px]",
              long ? "bg-destructive" : "bg-warning")} />
        }
        if (samples != null && samples <= 0) {
          return <span key={d} title={`${label}: probe reported nothing all day`}
            className="h-4 min-w-0 flex-1 rounded-[2px] border border-border/70" />
        }
        if (samples != null) {
          return <span key={d} title={`${label}: no outage`}
            className="h-4 min-w-0 flex-1 rounded-[2px] bg-success/40" />
        }
        return <span key={d}
          title={`${label}: no outage recorded · probe coverage unknown`}
          className="h-4 min-w-0 flex-1 rounded-[2px] bg-muted-foreground/15" />
      })}
    </div>
  )
}

export function DayStripLegend({ className }: { className?: string }) {
  const chip = (cls: string, label: string) => (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <span aria-hidden className={cn("h-2 w-3 rounded-[2px]", cls)} />
      {label}
    </span>
  )
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1", className)}>
      {chip("bg-destructive", "down ≥1h")}
      {chip("bg-warning", "down <1h")}
      {chip("bg-success/40", "up")}
      {chip("border border-border/70", "probe silent")}
      {chip("bg-muted-foreground/15", "coverage unknown")}
    </div>
  )
}
