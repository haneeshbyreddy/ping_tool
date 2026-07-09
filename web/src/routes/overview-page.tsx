import { Fragment, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { ChevronDown, ChevronRight, ArrowRight, Activity, Signal, Cable, TriangleAlert } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { adminApi } from "@/lib/api"
import type { OverviewOrg, OverviewProblem } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { StatusDot } from "@/components/status-badge"
import { ServerHealthCard } from "@/components/server-health-card"

const AREA_LABEL: Record<OverviewProblem["area"], string> = {
  snmp: "SNMP", optics: "Optics", ports: "Ports",
}

/** "9/12" colored by shortfall; em-dash when nothing is configured at all. */
function Ratio({ working, total }: { working: number; total: number }) {
  if (total === 0) return <span className="text-muted-foreground">—</span>
  return (
    <span className={cn("font-semibold tabular-nums",
      working < total ? "text-warning" : "text-success")}>
      {working}/{total}
    </span>
  )
}

function ProblemRow({ p }: { p: OverviewProblem }) {
  return (
    <div className="flex items-center gap-3 px-4 py-2 md:px-5">
      <StatusDot tone={p.reason === "never" ? "destructive" : "warning"} />
      <span className="min-w-0 truncate text-sm font-medium">{p.name}</span>
      <Badge variant="outline" className="text-[0.6875rem]">{AREA_LABEL[p.area]}</Badge>
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{p.detail}</span>
      <span className="shrink-0 text-[0.75rem] text-muted-foreground">
        {p.last_at ? `last data ${ago(p.last_at)}` : "never reported"}
      </span>
    </div>
  )
}

function OrgRow({ org }: { org: OverviewOrg }) {
  const { setScopeOrg } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const broken = org.problems.length
  const expandable = broken > 0
  const Chevron = open ? ChevronDown : ChevronRight

  return (
    <div className="border-b last:border-b-0">
      <button
        type="button"
        onClick={() => expandable && setOpen((o) => !o)}
        className={cn(
          "grid w-full grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 px-4 py-2.5 text-left md:grid-cols-[minmax(10rem,1.4fr)_repeat(4,minmax(0,1fr))_auto] md:px-5",
          expandable && "cursor-pointer transition-colors hover:bg-accent/40",
        )}
      >
        <span className="flex min-w-0 items-center gap-2">
          {expandable
            ? <Chevron className="size-3.5 shrink-0 text-muted-foreground" />
            : <StatusDot tone={org.devices > 0 ? "success" : "muted"} />}
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium">{org.name || org.org_id}</span>
            <span className="block font-mono text-[0.6875rem] text-muted-foreground">{org.org_id}</span>
          </span>
        </span>
        <span className="hidden text-sm md:block">
          <span className="tabular-nums">{org.devices}</span>
          <span className="ml-1 text-xs text-muted-foreground">devices</span>
        </span>
        <span className="hidden text-sm md:block">
          <Ratio working={org.snmp.working} total={org.snmp.enabled} />
          <span className="ml-1.5 text-xs text-muted-foreground">SNMP</span>
        </span>
        <span className="hidden text-sm md:block">
          <Ratio working={org.optics.working} total={org.optics.olts} />
          <span className="ml-1.5 text-xs text-muted-foreground">optics</span>
        </span>
        <span className="hidden text-sm md:block">
          <Ratio working={org.ports.working} total={org.ports.monitored} />
          <span className="ml-1.5 text-xs text-muted-foreground">ports</span>
        </span>
        {/* Mobile: fold the three ratios into one compact line under the name */}
        <span className="col-span-2 flex items-center gap-3 text-xs md:hidden">
          <span>{org.devices} devices</span>
          <span>SNMP <Ratio working={org.snmp.working} total={org.snmp.enabled} /></span>
          <span>optics <Ratio working={org.optics.working} total={org.optics.olts} /></span>
          <span>ports <Ratio working={org.ports.working} total={org.ports.monitored} /></span>
        </span>
        <span className="ml-auto flex items-center gap-2">
          {broken > 0 && (
            <Badge variant="outline" className="text-[0.6875rem] text-warning">
              {broken} not working
            </Badge>
          )}
          <span
            role="link"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); setScopeOrg(org.org_id); navigate("/topology") }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); setScopeOrg(org.org_id); navigate("/topology") } }}
            className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Network <ArrowRight className="size-3" />
          </span>
        </span>
      </button>
      {open && broken > 0 && (
        <div className="divide-y border-t bg-accent/20">
          {org.problems.map((p) => (
            <ProblemRow key={`${p.device_id}-${p.area}`} p={p} />
          ))}
        </div>
      )}
    </div>
  )
}

export function OverviewPage() {
  const { user } = useAuth()
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => adminApi.overview(),
    refetchInterval: 30_000,
    enabled: !!user?.is_superadmin,
  })

  if (!user?.is_superadmin) return null
  const t = data?.totals

  const stats = [
    {
      key: "snmp", label: "SNMP health", icon: Activity,
      value: t ? `${t.snmp.working}/${t.snmp.enabled}` : "—",
      detail: !t || t.snmp.enabled === 0 ? "none enabled"
        : t.snmp.working < t.snmp.enabled
          ? `${t.snmp.enabled - t.snmp.working} not reporting` : "all reporting",
      bad: !!t && t.snmp.working < t.snmp.enabled,
    },
    {
      key: "optics", label: "Optics (dBm)", icon: Signal,
      value: t ? `${t.optics.working}/${t.optics.olts}` : "—",
      detail: !t || t.optics.olts === 0 ? "no OLTs"
        : t.optics.working < t.optics.olts
          ? `${t.optics.olts - t.optics.working} OLT${t.optics.olts - t.optics.working === 1 ? "" : "s"} without optics`
          : `${t.optics.onus_online}/${t.optics.onus_total} ONUs online`,
      bad: !!t && t.optics.working < t.optics.olts,
    },
    {
      key: "ports", label: "Monitored ports", icon: Cable,
      value: t ? `${t.ports.working}/${t.ports.monitored}` : "—",
      detail: !t || t.ports.monitored === 0 ? "none monitored"
        : t.ports.alarms > 0 ? `${t.ports.alarms} alarming`
        : t.ports.working < t.ports.monitored
          ? `${t.ports.monitored - t.ports.working} stale` : "all fresh",
      bad: !!t && (t.ports.working < t.ports.monitored || t.ports.alarms > 0),
    },
    {
      key: "problems", label: "Coverage gaps", icon: TriangleAlert,
      value: data ? data.problems_total : "—",
      detail: !data ? "" : data.problems_total > 0 ? "devices need attention" : "everything configured is reporting",
      bad: !!data && data.problems_total > 0,
    },
  ]

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 md:p-6 xl:p-8">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Overview</h1>
        <p className="text-sm text-muted-foreground">
          Platform health at a glance: the central server, and whether every monitoring
          feature that's configured is actually delivering data.
        </p>
      </div>

      <ServerHealthCard />

      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border md:grid-cols-4">
        {stats.map((s) => (
          <div key={s.key} className="bg-card px-5 py-4">
            <p className="flex items-center gap-1.5 text-[0.75rem] font-medium tracking-wide text-muted-foreground uppercase">
              <s.icon className="size-3.5" /> {s.label}
            </p>
            {isLoading ? <Skeleton className="mt-1.5 h-8 w-16" /> : (
              <p className="mt-1 flex items-baseline gap-2">
                <span className={cn("text-2xl font-semibold tracking-tight tabular-nums",
                  s.bad && "text-warning")}>
                  {s.value}
                </span>
                <span className="truncate text-xs text-muted-foreground">{s.detail}</span>
              </p>
            )}
          </div>
        ))}
      </div>

      <Card className="py-0 gap-0">
        <CardHeader className="border-b px-5 !py-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            Coverage by organization
            {data && (
              <span className="ml-auto font-normal text-[0.75rem] text-muted-foreground">
                working = data within the last {Math.round(data.fresh_window_s / 60)} min
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading && <div className="p-5"><Skeleton className="h-24 w-full" /></div>}
          {isError && (
            <p className="p-5 text-xs text-destructive">Couldn't load the overview.</p>
          )}
          {data && data.orgs.length === 0 && (
            <p className="p-5 text-sm text-muted-foreground">No organizations yet.</p>
          )}
          {data && data.orgs.map((org) => (
            <Fragment key={org.org_id}>
              <OrgRow org={org} />
            </Fragment>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
