// The device's outage record over 90 days — the availability strip plus the
// spans behind it (notes/viz-plan.md Wave 1, chart A's device half).
// Question: "does this box flap, and is it getting better or worse?"
// Action: aim the truck / UPS / re-parent at the site that earns it.
//
// A fold, like Uplinks: history is reference material, not status — nothing
// alarm-shaped hides in it (a live outage is already the panel's headline).
// Downtime counts final_state DOWN only, the device_reliability rule, so this
// strip can never disagree with the analytics table; UNREACHABLE spans are
// listed and labeled ("parent was down") but never counted.
import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import { DayStrip, DayStripLegend } from "@/chart/day-strip"
import { fmtDurS } from "@/chart/scale"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"
import type { OrgDevice, OutageSpan } from "@/lib/types"
import { cn } from "@/lib/utils"

const DAYS = 90
const SPAN_ROWS = 8

function spanLine(s: OutageSpan): string {
  return toUtcDate(s.started_at).toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  })
}

export function DeviceHistoryPanel({ device }: { device: OrgDevice }) {
  // Open by default (operator's call 2026-08-14) — the strip is why the
  // Health tab now leads; the fold survives only as a way to put it away.
  const [open, setOpen] = useState(true)
  const q = useQuery({
    queryKey: ["device-history", device.id],
    queryFn: () => historyApi.device(device.id, DAYS),
    enabled: open,
    staleTime: 300_000,
  })

  const data = q.data
  const spans = (data?.spans ?? []).slice().reverse()
  const shown = spans.slice(0, SPAN_ROWS)

  return (
    <div className="flex flex-col rounded-lg border bg-muted/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className={cn("flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-foreground/5",
          open ? "rounded-t-lg" : "rounded-lg")}
        title="Daily availability and the outages behind it, over the last 90 days">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <span className="text-2xs font-medium text-muted-foreground">History</span>
        {!open && (
          <span className="font-mono text-2xs text-faint-foreground">last {DAYS} days</span>
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-2 px-3 pb-3">
          {q.isLoading ? (
            <p className="text-2xs text-muted-foreground">loading…</p>
          ) : q.error || !data ? (
            <p className="text-2xs text-destructive">Couldn't load the history.</p>
          ) : (
            <>
              <DayStrip
                sinceMs={toUtcDate(data.since).getTime()}
                untilMs={toUtcDate(data.until).getTime()}
                days={new Map(data.days.map((d) => [d.day * 1000,
                  { downS: d.down_s, outages: d.outages }]))}
                coverage={new Map(data.coverage.map((c) => [c.day * 1000, c.samples]))}
              />
              <div className="flex justify-between text-2xs text-muted-foreground">
                <span>{DAYS} days ago</span><span>now</span>
              </div>
              <DayStripLegend />
              {spans.length === 0 ? (
                <p className="text-2xs text-muted-foreground">
                  No outages in this window.
                </p>
              ) : (
                <div className="flex flex-col gap-0.5 border-t pt-2">
                  {shown.map((s) => (
                    <div key={s.id} className="flex items-baseline gap-2 text-2xs">
                      <span className="shrink-0 tabular-nums text-muted-foreground">
                        {spanLine(s)}
                      </span>
                      <span className={cn("shrink-0 font-mono font-semibold",
                        s.final_state === "DOWN" ? "text-destructive"
                          : "text-muted-foreground")}>
                        {s.resolved_at ? fmtDurS(s.duration_s) : "ongoing"}
                      </span>
                      {s.final_state !== "DOWN" && (
                        <span className="shrink-0 text-faint-foreground">parent was down</span>
                      )}
                      {s.root_cause && (
                        <span className="min-w-0 truncate text-muted-foreground"
                          title={s.root_cause}>{s.root_cause}</span>
                      )}
                    </div>
                  ))}
                  {spans.length > shown.length && (
                    <p className="text-2xs text-faint-foreground">
                      +{spans.length - shown.length} more in the window
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
