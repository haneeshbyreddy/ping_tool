// HOME IS AN INSTRUMENT PANEL (2026-08-15, the operator's ask: "make home a
// visual area — intuitive analysis, not numbered blocks"). The grid of stat
// tiles became the cockpit band (home-pulse.tsx): two state rings and a watch
// column, so a healthy fleet reads as a quiet shape and trouble as a red arc,
// not as one number among eleven. The triage queue moved to /triage with a
// nav badge; the verdict band here is what keeps Home from ever claiming
// all-clear while the queue is hot — it names the queue's depth and links it.
//
// Every figure still derives exactly as the tiles did (count agreement), and
// every drill-through keeps the tiles' destinations: topology statusFilter
// state and /issues?kind=.
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { ArrowRight } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { useTriage } from "@/hooks/use-triage"
import { summaryApi, inventoryApi } from "@/lib/api"
import { PulseBand, type WatchItem } from "@/components/home-pulse"
import { CapacityPanel } from "@/components/capacity-panel"
import { DownMostPanel } from "@/components/down-most"
import { NeedsOrg } from "@/components/needs-org"
import { OnuSignalPanel } from "@/components/onu-signal"
import { OrgReliabilityPanel } from "@/components/org-reliability"
import { StatusDot } from "@/components/status-badge"
import { ago, isStale } from "@/lib/format"

export function HomePage() {
  const { scopeOrg } = useAuth()
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
  const ponSummary = useQuery({
    queryKey: ["pon-summary", scopeOrg],
    queryFn: () => inventoryApi.ponSummary(scopeOrg),
    enabled: !!scopeOrg,
  })
  const triage = useTriage()

  if (!scopeOrg) return <NeedsOrg />

  const deviceList = devices.data?.devices ?? []
  const registeredNodeIds = new Set(triage.activeNodes.map((n) => n.node_id))
  const monitored = deviceList.filter(
    (d) => d.assigned_node_id && registeredNodeIds.has(d.assigned_node_id),
  )

  const staleNodeIds = new Set(triage.staleNodes.map((n) => n.node_id))
  const staleProbeDeviceIds = deviceList
    .filter((d) => d.assigned_node_id && staleNodeIds.has(d.assigned_node_id))
    .map((d) => d.id)

  const lastSeen = triage.activeNodes
    .map((n) => n.last_seen)
    .filter((t): t is string => !!t)
    .sort()
    .at(-1)
  const feedStale = !lastSeen || isStale(lastSeen)

  const portsDown = deviceList.reduce((sum, d) => sum + (d.ports_down ?? 0), 0)
  const portsDownIds = deviceList.filter((d) => (d.ports_down ?? 0) > 0).map((d) => d.id)
  const hasNvrs = deviceList.some((d) => (d.device_type ?? "").toLowerCase() === "nvr")
  const camerasDark = deviceList.reduce((sum, d) => sum + (d.cameras_down ?? 0), 0)
  const camerasDarkIds = deviceList.filter((d) => (d.cameras_down ?? 0) > 0).map((d) => d.id)
  const lowBw = summary.data?.low_bandwidth.length ?? 0
  const highBw = summary.data?.high_bandwidth.length ?? 0
  const bwAlarmIds = [...new Set(
    [...(summary.data?.low_bandwidth ?? []), ...(summary.data?.high_bandwidth ?? [])]
      .map((a) => a.device_id),
  )]

  const pon = ponSummary.data
  const hasOptics = (pon?.olts ?? 0) > 0
  const dupStale = pon ? pon.dup_macs_total - pon.dup_macs_live : 0
  const dupMacIds = deviceList.filter((d) => (d.dup_macs ?? 0) > 0).map((d) => d.id)
  const fiberCutIds = deviceList.filter((d) => (d.fiber_cuts ?? 0) > 0).map((d) => d.id)

  const watch: WatchItem[] = [
    {
      key: "ports", label: "Ports down", one: "port down", value: portsDown,
      tone: "destructive", plane: "traffic",
      filter: { label: "Ports down", ids: portsDownIds }, issueKind: "port_down",
    },
    {
      key: "bw", label: "Bandwidth alarms", one: "bandwidth alarm", value: lowBw + highBw,
      detail: [lowBw && `${lowBw} low`, highBw && `${highBw} high`].filter(Boolean).join(" · ") || undefined,
      tone: "warning", plane: "traffic",
      filter: { label: "Bandwidth alarm", ids: bwAlarmIds }, issueKind: "bandwidth",
    },
    ...(hasNvrs ? [{
      key: "cameras", label: "Cameras dark", one: "camera dark", value: camerasDark,
      tone: "destructive", plane: "plant",
      filter: { label: "Cameras dark", ids: camerasDarkIds }, issueKind: "camera_down",
    } satisfies WatchItem] : []),
    {
      key: "probes", label: "Stale probes", one: "stale probe", value: triage.staleNodes.length,
      tone: "destructive", plane: "fleet",
      filter: { label: "Behind a stale probe", ids: staleProbeDeviceIds },
      issueKind: "probe_stale",
    },
    ...(hasOptics ? [
      {
        key: "dup-macs", label: "Duplicate MACs", one: "duplicate MAC", value: pon?.dup_macs_live ?? 0,
        detail: dupStale > 0 ? `${dupStale} stale-only` : undefined,
        tone: "destructive", plane: "optical",
        filter: { label: "Duplicate MACs", ids: dupMacIds }, issueKind: "dup_mac",
      } satisfies WatchItem,
      {
        key: "fiber", label: "Fiber cuts", one: "fiber cut", value: pon?.fiber_cuts ?? 0,
        tone: "destructive", plane: "optical",
        filter: { label: "Fiber cuts", ids: fiberCutIds }, issueKind: "pon_fiber",
      } satisfies WatchItem,
      {
        key: "pon-cap", label: "PONs at capacity", one: "PON at capacity", value: pon?.pons_over_cap ?? 0,
        detail: (pon?.pons_over_cap ?? 0) > 0 ? `busiest has ${pon!.pon_cap_worst}` : undefined,
        tone: "warning", plane: "optical",
        filter: { label: "PON at capacity", ids: pon?.over_cap_device_ids ?? [] },
        issueKind: "pon_capacity",
      } satisfies WatchItem,
    ] : []),
  ]

  const dataLoading = devices.isLoading || summary.isLoading || ponSummary.isLoading

  return (
    <div className="wisp-page @container flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-lg font-semibold tracking-tight">Home</h1>
        <div className="flex items-center gap-3">
          {/* The queue's depth stays visible without a panel of its own: the
              cockpit shows the trouble, this chip names the queue. */}
          {!triage.loading && triage.urgent > 0 && (
            <Link to="/triage"
              className="flex items-center gap-1.5 rounded-md border border-destructive/30 bg-destructive-soft px-2.5 py-1 text-xs font-medium text-destructive transition-[filter] hover:brightness-125">
              <span className="size-1.5 rounded-full bg-destructive" aria-hidden />
              {[
                triage.activeOutages.length > 0
                  && `${triage.activeOutages.length} outage${triage.activeOutages.length === 1 ? "" : "s"}`,
                triage.staleNodes.length > 0
                  && `${triage.staleNodes.length} probe${triage.staleNodes.length === 1 ? "" : "s"} dark`,
              ].filter(Boolean).join(" · ")}
              <ArrowRight className="size-3" />
            </Link>
          )}
          <div className="flex items-center gap-2 text-xs text-faint-foreground">
            <StatusDot tone={feedStale ? "destructive" : "success"} />
            {lastSeen
              ? <>{feedStale ? "Feed stale" : "Live"} · updated {ago(lastSeen)}</>
              : "No probe has reported yet"}
          </div>
        </div>
      </div>

      <PulseBand
        monitored={monitored}
        devicesLoading={devices.isLoading || triage.loading}
        pon={pon}
        ponLoading={ponSummary.isLoading}
        hasOptics={hasOptics}
        watch={watch}
        watchLoading={dataLoading || triage.loading}
      />

      <div className="grid items-stretch gap-4 @2xl:grid-cols-[1.5fr_1fr]">
        <OnuSignalPanel hasOptics={hasOptics} />
        <DownMostPanel devices={deviceList} />
      </div>

      <OrgReliabilityPanel />

      {/* Capacity closes the band: the cockpit says what is wrong now, the
          reliability panels say how the fleet has been behaving, and this says
          what to buy before either becomes a complaint. Owner-only, and it
          mounts nothing for an org whose ports the historian doesn't sample. */}
      <CapacityPanel />
    </div>
  )
}
