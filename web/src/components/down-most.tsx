// The repeat offenders (the operator's ask, 2026-08-14): which devices go
// down the most, ranked over the last 30 days. Question: "where does the
// truck/UPS/re-parent effort actually belong?" — the fleet-wide answer the
// per-device History fold gives one box at a time.
//
// Reads the SAME /api/analytics rows the reliability table serves (DOWN-only,
// UNREACHABLE excluded), so this list can never disagree with it. Rank is by
// outage COUNT — "tending to go down" is flappiness — with downtime as the
// tiebreak and the second figure. Bars carry the fleet plane (a historical
// fact, not a live alarm); the dot in front is the device's LIVE state, so a
// box that is down right now still claims its status tone.
import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { fmtDurS } from "@/chart/scale"
import { useAuth } from "@/hooks/use-auth"
import { analyticsApi } from "@/lib/api"
import { deviceTone, isStale } from "@/lib/format"
import type { OrgDevice } from "@/lib/types"
import { StatusDot } from "@/components/status-badge"
import { cn } from "@/lib/utils"

const FLEET = "var(--chart-5)"
const DAYS = 30
// Five rows at h-10 lands this panel at the ONU signal chart's height, so the
// Home row the two share reads as one band instead of a step (2026-08-15).
const ROWS = 5

export function DownMostPanel({ devices }: { devices: OrgDevice[] }) {
  const { scopeOrg, canWrite } = useAuth()
  const q = useQuery({
    queryKey: ["analytics", scopeOrg, DAYS],
    queryFn: () => analyticsApi.reliability(scopeOrg, DAYS),
    enabled: !!scopeOrg && canWrite,
    staleTime: 300_000,
  })

  const byId = useMemo(
    () => new Map(devices.map((d) => [d.id, d])), [devices])
  const ranked = useMemo(() => {
    const rows = (q.data?.devices ?? []).filter((r) => r.outage_count > 0)
    rows.sort((a, b) => b.outage_count - a.outage_count
      || b.downtime_seconds - a.downtime_seconds)
    return rows
  }, [q.data])

  if (!scopeOrg || !canWrite) return null

  const top = ranked.slice(0, ROWS)
  const maxCount = Math.max(1, ...top.map((r) => r.outage_count))

  return (
    <section className="wisp-panel flex h-full flex-col">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">Down the most</span>
          <span className="text-xs text-faint-foreground">
            last {DAYS} days
            {ranked.length > ROWS && ` · top ${ROWS} of ${ranked.length}`}
          </span>
        </h2>
        <Link to="/issues?kind=device_down"
          className="shrink-0 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
          Issues →
        </Link>
      </div>
      {q.isLoading ? (
        <div className="h-24" />
      ) : top.length === 0 ? (
        <p className="px-4 py-8 text-center text-xs text-faint-foreground">
          No outages in the last {DAYS} days.
        </p>
      ) : (
        <div className="flex flex-col py-1.5">
          {top.map((r) => {
            const d = byId.get(r.device_id)
            const live = d && d.state && !isStale(d.state_updated_at)
            return (
              <Link key={r.device_id} to="/topology"
                state={{ deviceId: r.device_id }}
                title={`${r.uptime_pct.toFixed(2)}% uptime · ${fmtDurS(r.downtime_seconds)} down over ${DAYS} days`}
                className="flex h-10 items-center gap-3 px-4 transition-colors hover:bg-foreground/5">
                <StatusDot tone={live ? deviceTone(d!.state, d!.state_updated_at) : "muted"} />
                <span className="w-0 flex-1">
                  <span className="flex items-baseline gap-2">
                    <span className="min-w-0 truncate font-mono text-xs font-medium">
                      {r.name}
                    </span>
                    {r.region && (
                      <span className="hidden min-w-0 truncate text-2xs text-faint-foreground sm:inline">
                        {r.region}
                      </span>
                    )}
                  </span>
                  <span className="mt-1 block h-1 overflow-hidden rounded-full bg-muted">
                    <span className="block h-full rounded-full"
                      style={{ width: `${(r.outage_count / maxCount) * 100}%`,
                               background: FLEET, opacity: 0.75 }} />
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block font-mono text-xs font-semibold tabular-nums">
                    {r.outage_count}×
                  </span>
                  <span className={cn("block font-mono text-2xs tabular-nums",
                    r.downtime_seconds >= 3600 ? "text-warning" : "text-faint-foreground")}>
                    {fmtDurS(r.downtime_seconds)} down
                  </span>
                </span>
              </Link>
            )
          })}
        </div>
      )}
    </section>
  )
}
