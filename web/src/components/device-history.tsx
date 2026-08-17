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
import { Fragment, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import { DayStrip, DayStripLegend } from "@/chart/day-strip"
import { fmtDurS } from "@/chart/scale"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"
import type { OrgDevice, OutageSpan } from "@/lib/types"
import { cn } from "@/lib/utils"

const DAYS = 90
// Three, not eight (operator's call 2026-08-15): the strip above already says
// whether this box flaps — the list is there to name the last few, and a
// nine-line ledger under it made the fold as tall as the panel it sits in.
// The rest is one click away rather than gone: "+N more" expands, so nothing
// this window holds is unreachable from here.
const SPAN_ROWS = 3

function spanLine(s: OutageSpan): string {
  return toUtcDate(s.started_at).toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  })
}

export function DeviceHistoryPanel({ device }: { device: OrgDevice }) {
  // Open by default (operator's call 2026-08-14) — the strip is why the
  // Health tab now leads; the fold survives only as a way to put it away.
  const [open, setOpen] = useState(true)
  const [allSpans, setAllSpans] = useState(false)
  const q = useQuery({
    queryKey: ["device-history", device.id],
    queryFn: () => historyApi.device(device.id, DAYS),
    enabled: open,
    staleTime: 300_000,
  })

  const data = q.data
  const spans = (data?.spans ?? []).slice().reverse()
  const shown = allSpans ? spans : spans.slice(0, SPAN_ROWS)

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
                <div className="flex flex-col gap-1 border-t pt-2">
                  {/* ONE grid, rows flattened — so the four columns are sized
                      across every row and line up. Widths are `auto` rather
                      than hardcoded: a date string's width is the locale's,
                      not ours, and a fixed column would truncate somebody's. */}
                  <div className={cn(
                    "grid grid-cols-[auto_auto_auto_minmax(0,1fr)] items-baseline",
                    "gap-x-2 gap-y-0.5 text-2xs",
                    allSpans && "max-h-40 overflow-y-auto")}>
                    {shown.map((s) => (
                      <Fragment key={s.id}>
                        <span className="tabular-nums whitespace-nowrap text-muted-foreground">
                          {spanLine(s)}
                        </span>
                        <span className={cn(
                          "text-right font-mono font-semibold tabular-nums whitespace-nowrap",
                          s.final_state === "DOWN" ? "text-destructive"
                            : "text-muted-foreground")}>
                          {s.resolved_at ? fmtDurS(s.duration_s) : "ongoing"}
                        </span>
                        <span className="whitespace-nowrap text-faint-foreground">
                          {s.final_state !== "DOWN" ? "parent was down" : ""}
                        </span>
                        <span className="min-w-0 truncate text-muted-foreground"
                          title={s.root_cause ?? undefined}>
                          {s.root_cause ?? ""}
                        </span>
                      </Fragment>
                    ))}
                  </div>
                  {spans.length > SPAN_ROWS && (
                    <button type="button" onClick={() => setAllSpans((v) => !v)}
                      className="self-start text-2xs text-faint-foreground hover:text-muted-foreground">
                      {allSpans
                        ? "Show fewer"
                        : `+${spans.length - SPAN_ROWS} more in the window`}
                    </button>
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
