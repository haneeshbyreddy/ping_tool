import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { ChevronDown, ChevronUp, TriangleAlert } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { summaryApi, inventoryApi, outagesApi, nodesApi, logsApi, analyticsApi } from "@/lib/api"
import type { OrgDevice } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { OutageCard } from "@/components/outage-card"
import { ClearPostmortems } from "@/components/clear-postmortems"
import { StaleNodeCard } from "@/components/stale-node-card"
import { StatusDot } from "@/components/status-badge"
import { describeEvent, eventTone } from "@/lib/events"
import { ago, deviceTone, isStale } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

function severityRank(d: OrgDevice): number {
  if (d.maintenance) return 5
  if (!d.assigned_node_id) return 6
  if (!d.state) return 6
  if (d.state === "DOWN") return 0
  if (d.state === "UNREACHABLE") return 1
  if (d.state === "DEGRADED") return 2
  if (isStale(d.state_updated_at)) return 3
  return 4
}

const DEVICE_ROW_CAP = 30

function fmtUptime(pct: number): string {
  return pct >= 99.995 ? "100%" : `${pct.toFixed(2)}%`
}

function Panel({ title, action, children }: {
  title: string
  action?: { label: string; to: string }
  children: React.ReactNode
}) {
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-5 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action && (
          <Link to={action.to} className="text-xs text-muted-foreground transition-colors hover:text-foreground">
            {action.label} →
          </Link>
        )}
      </div>
      {children}
    </section>
  )
}

function PanelEmpty({ children }: { children: React.ReactNode }) {
  return <p className="px-5 py-8 text-center text-xs text-muted-foreground">{children}</p>
}

export function HomePage() {
  const { scopeOrg } = useAuth()
  const [showPostmortems, setShowPostmortems] = useState(false)
  useNow()

  const summary = useQuery({
    queryKey: ["summary", scopeOrg],
    queryFn: () => summaryApi.get(scopeOrg),
    enabled: !!scopeOrg,
  })
  const devices = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const outages = useQuery({
    queryKey: ["outages", scopeOrg],
    queryFn: () => outagesApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: !!scopeOrg,

    refetchInterval: 30_000,
  })
  const reliability = useQuery({
    queryKey: ["analytics", scopeOrg, 7],
    queryFn: () => analyticsApi.reliability(scopeOrg, 7),
    enabled: !!scopeOrg,
  })
  const recentEvents = useQuery({
    queryKey: ["logs", scopeOrg, "recent"],
    queryFn: () => logsApi.list(scopeOrg, 8),
    enabled: !!scopeOrg,
  })

  if (!scopeOrg) return <NeedsOrg />

  const deviceList = devices.data?.devices ?? []

  const registeredNodeIds = new Set(
    (nodes.data?.nodes ?? []).filter((n) => n.registered && !n.revoked_at).map((n) => n.node_id),
  )

  const monitored = deviceList.filter(
    (d) => d.assigned_node_id && registeredNodeIds.has(d.assigned_node_id),
  )

  const online = monitored.filter(
    (d) => d.state === "UP" && !isStale(d.state_updated_at),
  ).length
  const outageList = outages.data?.outages ?? []
  // urgent cards (open outages) always render; resolved-awaiting-post-mortem is
  // paperwork and folds behind a toggle so a backlog can't bury the emergencies
  const activeOutages = outageList.filter((o) => o.status !== "pending_postmortem")
  const postmortemList = outageList.filter((o) => o.status === "pending_postmortem")
  const pendingPostmortems = postmortemList.length
  const portsDown = deviceList.reduce((sum, d) => sum + (d.ports_down ?? 0), 0)
  const lowBw = summary.data?.low_bandwidth.length ?? 0
  const highBw = summary.data?.high_bandwidth.length ?? 0
  const bwAlarms = lowBw + highBw

  const activeNodes = (nodes.data?.nodes ?? []).filter((n) => n.registered && !n.revoked_at)
  const staleNodes = activeNodes.filter((n) => n.last_seen && isStale(n.last_seen))
  const triageCount = outageList.length + staleNodes.length
  const triageLoading = outages.isLoading || nodes.isLoading

  // when nothing is on fire, preview a couple of post-mortems instead of an
  // empty queue with a bare button; the rest stay behind the toggle
  const urgentCount = staleNodes.length + activeOutages.length
  const postmortemPreview = urgentCount === 0 ? Math.min(2, pendingPostmortems) : 0
  const visiblePostmortems = showPostmortems
    ? postmortemList
    : postmortemList.slice(0, postmortemPreview)
  const hiddenPostmortems = pendingPostmortems - postmortemPreview

  const uptimeByDevice = new Map(
    (reliability.data?.devices ?? []).map((r) => [r.device_id, r.uptime_pct]),
  )
  // Within a severity band (e.g. all UP), surface the least-reliable device first
  // so the weakest link gets attention; fall back to name for a stable order.
  const rankedDevices = [...deviceList].sort(
    (a, b) =>
      severityRank(a) - severityRank(b) ||
      (uptimeByDevice.get(a.id) ?? 100) - (uptimeByDevice.get(b.id) ?? 100) ||
      a.name.localeCompare(b.name),
  )
  const visibleDevices = rankedDevices.slice(0, DEVICE_ROW_CAP)

  const events = [...(recentEvents.data?.events ?? [])].sort((a, b) =>
    (b.occurred_at ?? b.received_at).localeCompare(a.occurred_at ?? a.received_at),
  )

  const stats: Array<{
    key: string
    label: string
    loading: boolean
    value: string | number
    detail: string
    tone?: "destructive" | "warning"
    to?: string
  }> = [
    {
      key: "devices",
      label: "Devices online",
      loading: devices.isLoading,
      value: monitored.length ? `${online}/${monitored.length}` : "—",
      detail: online < monitored.length ? `${monitored.length - online} not up` : "all up",
      tone: online < monitored.length ? "destructive" : undefined,
      to: "/topology",
    },
    {
      key: "ports",
      label: "Ports down",
      loading: devices.isLoading,
      value: portsDown,
      detail: portsDown > 0 ? "check switches" : "all up",
      tone: portsDown > 0 ? "destructive" : undefined,
      to: "/topology",
    },
    {
      key: "probes",
      label: "Stale probes",
      loading: nodes.isLoading,
      value: staleNodes.length,
      detail: staleNodes.length > 0 ? "not reporting" : "all reporting",
      tone: staleNodes.length > 0 ? "destructive" : undefined,
      to: "/topology",
    },
    {
      key: "bw",
      label: "Bandwidth alarms",
      loading: summary.isLoading,
      value: bwAlarms,
      detail: bwAlarms > 0 ? [lowBw && `${lowBw} low`, highBw && `${highBw} high`].filter(Boolean).join(" · ") : "within limits",
      tone: bwAlarms > 0 ? "warning" : undefined,
      to: "/topology",
    },
  ]

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 p-4 md:p-6">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border md:grid-cols-4">
        {stats.map((s) => {
          const body = (
            <>
              <p className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">{s.label}</p>
              {s.loading ? <Skeleton className="mt-1.5 h-8 w-16" /> : (
                <p className="mt-1 flex items-baseline gap-2">
                  <span className={cn("text-3xl font-semibold tracking-tight", s.tone === "destructive" && "text-destructive", s.tone === "warning" && "text-warning")}>
                    {s.value}
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{s.detail}</span>
                </p>
              )}
            </>
          )
          return s.to ? (
            <Link key={s.key} to={s.to} className="bg-card px-6 py-5 transition-colors hover:bg-foreground/5">{body}</Link>
          ) : (
            <div key={s.key} className="bg-card px-6 py-5">{body}</div>
          )
        })}
      </div>

      {/* Triage only claims screen space when something actually needs triage — a
          healthy network gets one quiet all-clear line, not a large empty box. */}
      {triageLoading && <Skeleton className="h-12 w-full" />}
      {!triageLoading && triageCount === 0 && (
        <div className="flex items-center gap-3 rounded-lg border bg-card px-5 py-4 text-sm text-muted-foreground">
          <StatusDot tone="success" />
          All clear. No open outages, every probe reporting.
        </div>
      )}
      {!triageLoading && triageCount > 0 && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <TriangleAlert className="size-4 text-muted-foreground" />
              Triage queue
            </h2>
            <div className="flex items-center gap-3">
              <ClearPostmortems org={scopeOrg} count={pendingPostmortems} />
              <span className="rounded-full border bg-card px-2 py-0.5 text-xs font-semibold">
                {triageCount} open
              </span>
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-2 md:items-start xl:grid-cols-3">
            {staleNodes.map((n) => <StaleNodeCard key={n.node_id} node={n} />)}
            {activeOutages.map((o) => <OutageCard key={o.id} outage={o} />)}
            {visiblePostmortems.map((o) => <OutageCard key={o.id} outage={o} />)}
          </div>
          {hiddenPostmortems > 0 && (
            <Button variant="outline" size="sm" className="gap-1.5 self-start"
              onClick={() => setShowPostmortems((v) => !v)}>
              {showPostmortems
                ? <><ChevronUp className="size-3.5" /> Hide post-mortems</>
                : <><ChevronDown className="size-3.5" /> Show {hiddenPostmortems}{postmortemPreview > 0 ? " more" : ""} pending post-mortem{hiddenPostmortems === 1 ? "" : "s"}</>}
            </Button>
          )}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Panel title="Network" action={{ label: "Topology", to: "/topology" }}>
            {devices.isLoading && <Skeleton className="m-4 h-40" />}
            {!devices.isLoading && deviceList.length === 0 && (
              <PanelEmpty>No devices yet. Add them on the Network page.</PanelEmpty>
            )}
            {visibleDevices.map((d) => {
              const uptime = uptimeByDevice.get(d.id)
              const unassigned = !d.assigned_node_id
              const stale = !unassigned && !!d.state && isStale(d.state_updated_at)
              return (
                <Link key={d.id} to="/topology" state={{ deviceId: d.id }}
                  className="flex items-center gap-3 border-t px-5 py-2.5 transition-colors first:border-t-0 hover:bg-foreground/5">
                  <StatusDot tone={unassigned ? "muted" : deviceTone(d.state, d.state_updated_at)} />
                  <span className={cn("min-w-0 truncate font-mono text-xs font-medium",
                    unassigned && "text-muted-foreground")}>{d.name}</span>
                  {d.device_type && (
                    <span className="hidden shrink-0 text-xs text-muted-foreground md:inline">{d.device_type}</span>
                  )}
                  {d.region && (
                    <span className="hidden min-w-0 truncate text-xs text-muted-foreground lg:inline">· {d.region}</span>
                  )}
                  <span className="ml-auto flex shrink-0 items-baseline gap-3 text-right">
                    {unassigned && (
                      <span className="text-xs text-faint-foreground">not monitored</span>
                    )}
                    {!unassigned && d.maintenance === 1 && (
                      <span className="text-xs text-muted-foreground">maintenance</span>
                    )}
                    {stale && <span className="text-xs text-muted-foreground">stale · {ago(d.state_updated_at)}</span>}
                    {!unassigned && !stale && d.state && d.state !== "UP" && (
                      <span className={cn("font-mono text-xs font-semibold",
                        d.state === "DEGRADED" ? "text-warning" : "text-destructive")}>
                        {d.state}
                      </span>
                    )}
                    {!unassigned && !stale && d.state === "UP" && d.latency_ms != null && (
                      <span className="font-mono text-xs text-muted-foreground">{Math.round(d.latency_ms)} ms</span>
                    )}
                    {!unassigned && !stale && d.state === "UP" && d.packet_loss != null && d.packet_loss > 0 && (
                      <span className="font-mono text-xs text-warning">{Math.round(d.packet_loss)}% loss</span>
                    )}
                    {uptime != null && (
                      <span className={cn("hidden font-mono text-xs sm:inline",
                        uptime < 99 ? "text-warning" : "text-muted-foreground")}>
                        {fmtUptime(uptime)}
                      </span>
                    )}
                  </span>
                </Link>
              )
            })}
            {rankedDevices.length > DEVICE_ROW_CAP && (
              <Link to="/topology"
                className="block border-t px-5 py-2.5 text-center text-xs text-muted-foreground transition-colors hover:text-foreground">
                All {rankedDevices.length} devices →
              </Link>
            )}
          </Panel>
        </div>

        <div className="flex flex-col gap-6">
          <Panel title="Probes" action={{ label: "Manage", to: "/topology" }}>
            {nodes.isLoading && <Skeleton className="m-4 h-16" />}
            {!nodes.isLoading && activeNodes.length === 0 && (
              <PanelEmpty>No probes registered.</PanelEmpty>
            )}
            {activeNodes.map((n) => {
              const stale = !n.last_seen || isStale(n.last_seen)
              return (
                <div key={n.node_id} className="flex items-center gap-3 border-t px-5 py-2.5 first:border-t-0">
                  <StatusDot tone={stale ? "destructive" : "success"} />
                  <span className="min-w-0 truncate font-mono text-xs font-medium">{n.node_id}</span>
                  {n.version && <span className="shrink-0 font-mono text-xs text-muted-foreground">{n.version}</span>}
                  <span className={cn("ml-auto shrink-0 text-xs", stale ? "text-destructive" : "text-muted-foreground")}>
                    {n.last_seen ? ago(n.last_seen) : "never seen"}
                  </span>
                </div>
              )
            })}
          </Panel>

          <Panel title="Recent activity" action={{ label: "Logs", to: "/logs" }}>
            {recentEvents.isLoading && <Skeleton className="m-4 h-24" />}
            {!recentEvents.isLoading && events.length === 0 && (
              <PanelEmpty>No events yet.</PanelEmpty>
            )}
            {events.map((ev) => (
              <div key={ev.id} className="flex items-center gap-3 border-t px-5 py-2.5 first:border-t-0">
                <StatusDot tone={eventTone(ev)} />
                <span className="min-w-0 shrink-0 truncate font-mono text-xs font-medium">
                  {ev.device_name || "—"}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={describeEvent(ev)}>
                  {describeEvent(ev)}
                </span>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {ago(ev.occurred_at ?? ev.received_at)}
                </span>
              </div>
            ))}
          </Panel>
        </div>
      </div>
    </div>
  )
}
