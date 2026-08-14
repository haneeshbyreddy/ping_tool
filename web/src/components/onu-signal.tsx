// The org's crit/warn ONU trend on Home (the operator's ask, 2026-08-14).
// Question: "did crit ONUs spike — after the storm, after the splice, or is
// the fleet slowly getting worse?" Action: open Issues / the OLT before the
// complaints arrive.
//
// The two series ARE failure claims, so they wear the status hues (the one
// place a chart may) — everything else here stays quiet so the red line is
// the loudest thing in the panel. Values are each OLT's HOURLY WORST summed
// across the fleet, and the panel says so. The domain clamps server-side to
// recording_since, so a young historian fills the width with what it has
// instead of drawing months of fake zero.
import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { LegendChip, TimeChart } from "@/chart/frame"
import type { TooltipModel } from "@/chart/frame"
import { LineMark } from "@/chart/marks"
import { HOUR_MS } from "@/chart/scale"
import { useAuth } from "@/hooks/use-auth"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"

const CRIT = "var(--destructive)"
const WARN = "var(--warning)"
const DAYS = 14

export function OnuSignalPanel({ hasOptics }: { hasOptics: boolean }) {
  const { scopeOrg, canWrite } = useAuth()
  const q = useQuery({
    queryKey: ["onu-trend", scopeOrg],
    queryFn: () => historyApi.onus(scopeOrg, DAYS),
    enabled: !!scopeOrg && canWrite && hasOptics,
    staleTime: 300_000,
    refetchInterval: 600_000,
  })

  const model = useMemo(() => {
    if (!q.data) return null
    const since = toUtcDate(q.data.since).getTime()
    const until = toUtcDate(q.data.until).getTime()
    const byBucket = new Map(q.data.buckets.map((b) => [b.bucket * 1000, b]))
    const crit = []
    const warn = []
    for (let t = since; t < until; t += HOUR_MS) {
      const b = byBucket.get(t)
      crit.push({ t: t + HOUR_MS / 2, v: b ? b.crit : null })
      warn.push({ t: t + HOUR_MS / 2, v: b ? b.warn : null })
    }
    const yMax = Math.max(4, ...q.data.buckets.map((b) => Math.max(b.crit, b.warn)))
    return { since, until, byBucket, crit, warn, yMax }
  }, [q.data])

  if (!scopeOrg || !canWrite || !hasOptics) return null

  const tooltip = (tMs: number): TooltipModel | null => {
    if (!model) return null
    const t = Math.floor(tMs / HOUR_MS) * HOUR_MS
    const b = model.byBucket.get(t)
    const title = new Date(t).toLocaleString(undefined,
      { day: "numeric", month: "short", hour: "numeric" })
    if (!b) return { at: t + HOUR_MS / 2, title,
                     rows: [{ label: "no walks this hour", value: "—" }] }
    return {
      at: t + HOUR_MS / 2, title,
      rows: [
        { label: "critical · worst of hour", value: String(b.crit), color: CRIT },
        { label: "warning", value: String(b.warn), color: WARN },
        { label: "online", value: `${b.online} of ${b.onus}` },
        { label: "OLTs reporting", value: String(b.olts) },
      ],
    }
  }

  const young = q.data?.recording_since
    && toUtcDate(q.data.recording_since).getTime() > Date.now() - 2 * 86_400_000

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">ONU signal</span>
          <span className="text-xs text-faint-foreground">
            hourly worst across the fleet
            {young && q.data?.recording_since
              ? ` · recording since ${toUtcDate(q.data.recording_since)
                  .toLocaleDateString(undefined, { day: "numeric", month: "short" })}`
              : ` · last ${DAYS} days`}
          </span>
        </h2>
        <Link to="/issues?kind=onu_crit"
          className="shrink-0 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
          Issues →
        </Link>
      </div>
      <div className="px-4 pt-3 pb-4">
        {q.isLoading || !model ? (
          <div className="h-[150px]" />
        ) : (
          <TimeChart domain={[model.since, model.until]} yMax={model.yMax}
            height={150} tooltip={tooltip}
            empty={q.data && q.data.buckets.length === 0
              ? `No ONU readings recorded yet — recording began ${
                  q.data.recording_since
                    ? toUtcDate(q.data.recording_since).toLocaleDateString()
                    : "today"}.`
              : null}
            legend={<>
              <LegendChip color={CRIT} label="critical" />
              <LegendChip color={WARN} label="warning" />
            </>}>
            <LineMark points={model.warn} color={WARN} />
            <LineMark points={model.crit} color={CRIT} />
          </TimeChart>
        )}
      </div>
    </section>
  )
}
