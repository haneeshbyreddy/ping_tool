// The org's reliability story (Wave 1, charts A-org + C).
// Questions: "are outages getting better or worse month over month?" and
// "how long do we take to fix and to acknowledge?" Actions: aim the effort,
// set staffing/escalation — the owner's own KPI.
//
// Owner-only: org-wide numbers leak past a worker's assignment scope, and the
// endpoint refuses workers anyway. One measurement plane (fleet, --chart-5);
// the three triage series separate by LINE STYLE, never by pairing plane hues
// (the validator-measured rule in notes/viz-grammar.md).
import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { LegendChip, TimeChart } from "@/chart/frame"
import type { TooltipModel } from "@/chart/frame"
import { ColumnMark, LineMark } from "@/chart/marks"
import { fmtDurS, WEEK_MS } from "@/chart/scale"
import { useAuth } from "@/hooks/use-auth"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"
import type { WeekStat } from "@/lib/types"

const FLEET = "var(--chart-5)"
// Monday 1970-01-05 00:00 UTC — the server's own week anchor.
const MONDAY_MS = 4 * 86_400_000
const DAYS = 182

function weekFloor(ms: number): number {
  return Math.floor((ms - MONDAY_MS) / WEEK_MS) * WEEK_MS + MONDAY_MS
}

function weekLabel(ms: number): string {
  return "Week of " + new Date(ms).toLocaleDateString(undefined,
    { day: "numeric", month: "short" })
}

function LegendLine({ label, dash, opacity = 1 }: {
  label: string
  dash?: string
  opacity?: number
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <svg width="16" height="6" aria-hidden>
        <line x1="0" x2="16" y1="3" y2="3" stroke={FLEET} strokeWidth="1.5"
          strokeDasharray={dash} strokeOpacity={opacity} />
      </svg>
      {label}
    </span>
  )
}

export function OrgReliabilityPanel() {
  const { scopeOrg, canWrite } = useAuth()
  const q = useQuery({
    queryKey: ["org-history", scopeOrg],
    queryFn: () => historyApi.org(scopeOrg, DAYS),
    enabled: !!scopeOrg && canWrite,
    staleTime: 300_000,
  })
  if (!scopeOrg || !canWrite) return null

  return <Body weeks={q.data?.weeks ?? null}
    since={q.data ? toUtcDate(q.data.since).getTime() : null}
    until={q.data ? toUtcDate(q.data.until).getTime() : null} />
}

function Body({ weeks, since, until }: {
  weeks: WeekStat[] | null
  since: number | null
  until: number | null
}) {
  const model = useMemo(() => {
    if (!weeks || since == null || until == null) return null
    const byWeek = new Map(weeks.map((w) => [w.week * 1000, w]))
    const all: number[] = []
    for (let t = weekFloor(since); t < until; t += WEEK_MS) all.push(t)
    const columns = all.map((t) => ({
      t, span: WEEK_MS,
      segs: [{ v: byWeek.get(t)?.outages ?? 0, color: FLEET, opacity: 0.75 }],
    }))
    const line = (pick: (w: WeekStat) => number | null) =>
      all.map((t) => {
        const w = byWeek.get(t)
        const v = w ? pick(w) : null
        return { t: t + WEEK_MS / 2, v }
      })
    return {
      all, byWeek, columns,
      maxOutages: Math.max(1, ...weeks.map((w) => w.outages)),
      ttr50: line((w) => w.ttr_p50_s),
      ttr90: line((w) => w.ttr_p90_s),
      tta50: line((w) => w.tta_p50_s),
      maxDur: Math.max(60, ...weeks.flatMap((w) =>
        [w.ttr_p90_s ?? 0, w.tta_p50_s ?? 0, w.ttr_p50_s ?? 0])),
    }
  }, [weeks, since, until])

  if (!model || since == null || until == null) return null
  const empty = weeks && weeks.length === 0
    ? "No outages in this window."
    : null

  const tipFor = (kind: "outages" | "triage") => (tMs: number): TooltipModel | null => {
    const t = weekFloor(tMs)
    const w = model.byWeek.get(t)
    if (!w) return { at: t + WEEK_MS / 2, title: weekLabel(t),
                     rows: [{ label: "outages", value: "0" }] }
    const rows = kind === "outages"
      ? [{ label: "outages", value: String(w.outages) },
         { label: "resolved", value: String(w.resolved) }]
      : [{ label: "resolve p50", value: fmtDurS(w.ttr_p50_s) },
         { label: "resolve p90", value: fmtDurS(w.ttr_p90_s) },
         { label: "acknowledge p50", value: fmtDurS(w.tta_p50_s) }]
    return { at: t + WEEK_MS / 2, title: weekLabel(t), rows }
  }

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">Reliability</span>
          <span className="text-xs text-faint-foreground">last 26 weeks · device outages</span>
        </h2>
      </div>
      <div className="grid gap-4 px-4 pt-3 pb-4 @2xl:grid-cols-2">
        <TimeChart domain={[since, until]} yMax={model.maxOutages} height={150}
          empty={empty} tooltip={tipFor("outages")}
          legend={<LegendChip color={FLEET} label="outages opened / week" />}>
          <ColumnMark buckets={model.columns} />
        </TimeChart>
        <TimeChart domain={[since, until]} yMax={model.maxDur} height={150}
          empty={empty} yFmt={(v) => fmtDurS(v)} tooltip={tipFor("triage")}
          legend={<>
            <LegendLine label="time to resolve · median" />
            <LegendLine label="p90" opacity={0.45} />
            <LegendLine label="time to acknowledge · median" dash="4 3" />
          </>}>
          <LineMark points={model.ttr90} color={FLEET} opacity={0.45} />
          <LineMark points={model.ttr50} color={FLEET} />
          <LineMark points={model.tta50} color={FLEET} dash="4 3" />
        </TimeChart>
      </div>
    </section>
  )
}
