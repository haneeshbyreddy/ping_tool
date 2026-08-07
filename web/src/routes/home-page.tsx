import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { ChevronDown, ChevronUp, ListTree, TriangleAlert } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { summaryApi, inventoryApi, outagesApi, nodesApi, logsApi, analyticsApi } from "@/lib/api"
import type { IssueKind, OrgDevice } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { OutageCard } from "@/components/outage-card"
import { ClearPostmortems } from "@/components/clear-postmortems"
import { StaleNodeCard } from "@/components/stale-node-card"
import { StatusDot } from "@/components/status-badge"
import { describeEvent, eventTone } from "@/lib/events"
import { ago, deviceTone, isStale } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
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

// Home panels are a glanceable preview: the three most-urgent rows each, with a
// "view all" link into the full page. Ranking floats trouble to the top so the
// three shown are the three worth looking at.
const PANEL_ROW_CAP = 3

function fmtUptime(pct: number): string {
  return pct >= 99.995 ? "100%" : `${pct.toFixed(2)}%`
}

function Panel({ title, count, action, children }: {
  title: string
  count?: string | number
  action?: { label: string; to: string }
  children: React.ReactNode
}) {
  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">{title}</span>
          {count != null && <span className="text-xs text-faint-foreground">{count}</span>}
        </h2>
        {action && (
          <Link to={action.to}
            className="shrink-0 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
            {action.label} →
          </Link>
        )}
      </div>
      {children}
    </section>
  )
}

function PanelEmpty({ children }: { children: React.ReactNode }) {
  return <p className="px-4 py-8 text-center text-xs text-faint-foreground">{children}</p>
}

// A row is a fixed-height rail so three panels stacked beside each other line up
// on the same baselines — that alignment is most of why a dense dashboard reads
// as calm rather than busy.
const ROW = "flex h-11 items-center gap-3 px-4 wisp-row"

function PanelMore({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <Link to={to}
      className="block border-t border-border-subtle px-4 py-2.5 text-center text-xs font-medium text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground">
      {children}
    </Link>
  )
}

type Stat = {
  key: string
  label: string
  loading: boolean
  value: string | number
  detail: string
  tone?: "destructive" | "warning"
  to?: string
  /** Present when this stat names a specific set of devices — clicking filters
   *  the Network page down to just them (a clearable chip there) instead of
   *  landing on the full unfiltered tree. Absent for a healthy metric, where
   *  there's nothing to filter to. */
  filter?: { label: string; ids: number[] }
  /** The issue kind this tile counts. The tile body drills into the DEVICES
   *  behind the number; the corner action drills into the PROBLEMS themselves —
   *  one row per port/ONU/PON/probe on the Issues page. Both are offered because
   *  they answer different questions ("which boxes" vs "what is wrong"), and a
   *  switch with four dark ports is one row in the tree and four in the list. */
  issueKind?: IssueKind
}

// undefined (no filter) when there's nothing to show — a healthy metric's
// tile should still land on the full tree, not a guaranteed-empty list.
function filterFor(label: string, ids: number[]): Stat["filter"] {
  return ids.length > 0 ? { label, ids } : undefined
}

// The whole point of the strip is that a healthy metric costs you NO attention.
// A quiet tile is plain; only a tile with something wrong picks up a tinted
// value and edge, so scanning is a search for color, not a read of eight numbers.
function StatTile({ s }: { s: Stat }) {
  const body = (
    <>
      {/* NOT `.wisp-eyebrow`. An eyebrow is a micro-label above a GROUP, and
          that is still what the other 13 uses of it are. This one names the
          number directly under it — it is the half of the tile you read to know
          what the figure means — and it was set as the quietest thing on the
          card: 12px, uppercase, letterspaced, --faint-foreground at 4.80:1
          against a 30px figure. That is the giant-number-over-tiny-label
          pattern, and it makes a wall of ten tiles unreadable without leaning
          in. Now 13px, sentence case, --muted-foreground at 7.27:1. It stays
          BELOW the figure in rank (the number is the live state, the label is
          reference) — readable, not loud. */}
      <p className="truncate pr-7 text-xs font-medium text-muted-foreground">{s.label}</p>
      {s.loading ? <Skeleton className="mt-3.5 h-7 w-14" /> : (
        <p className={cn(
          "mt-3.5 font-mono text-3xl leading-none font-medium tracking-tight",
          s.tone === "destructive" ? "text-destructive"
            : s.tone === "warning" ? "text-warning" : "text-foreground",
        )}>
          {s.value}
        </p>
      )}
      <p className="mt-2 truncate text-xs text-faint-foreground">{s.detail}</p>
    </>
  )
  const shell = cn(
    "wisp-panel px-5 py-4 transition-colors",
    s.tone === "destructive" && "border-destructive/35",
    s.tone === "warning" && "border-warning/35",
    s.to && "hover:bg-foreground/[0.03]",
  )
  const state = s.filter ? { statusFilter: s.filter } : undefined
  // The list action only appears when there is something to list — `filter` is
  // already the "this tile names actual trouble" signal, so a healthy tile stays
  // a plain number with nothing to click twice.
  const listable = s.issueKind && s.filter
  if (!s.to) return <div className={shell}>{body}</div>
  return (
    // The whole tile stays one click target via a STRETCHED link rather than an
    // <a> wrapping everything: a second interactive control nested inside an
    // anchor is invalid, and the corner action has to sit above it.
    <div className={cn(shell, "relative")}>
      <Link to={s.to} state={state} aria-label={`${s.label} · filter the network`}
        className="absolute inset-0 z-0 rounded-[inherit]" />
      {body}
      {listable && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Link to={`/issues?kind=${s.issueKind}`}
              className="absolute top-3 right-3 z-10 flex size-6 items-center justify-center rounded-md border border-border bg-card text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground">
              <ListTree className="size-3.5" />
            </Link>
          </TooltipTrigger>
          <TooltipContent>List these issues</TooltipContent>
        </Tooltip>
      )}
    </div>
  )
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
  const ponSummary = useQuery({
    queryKey: ["pon-summary", scopeOrg],
    queryFn: () => inventoryApi.ponSummary(scopeOrg),
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

  // Kept as the device list, not just a count: the "Devices online" tile drills
  // into exactly these devices on click.
  const notUpDevices = monitored.filter(
    (d) => !(d.state === "UP" && !isStale(d.state_updated_at)),
  )
  const online = monitored.length - notUpDevices.length
  const outageList = outages.data?.outages ?? []
  // urgent cards (open outages) always render; resolved-awaiting-post-mortem is
  // paperwork and folds behind a toggle so a backlog can't bury the emergencies
  const activeOutages = outageList.filter((o) => o.status !== "pending_postmortem")
  const postmortemList = outageList.filter((o) => o.status === "pending_postmortem")
  const pendingPostmortems = postmortemList.length
  const portsDown = deviceList.reduce((sum, d) => sum + (d.ports_down ?? 0), 0)
  const portsDownIds = deviceList.filter((d) => (d.ports_down ?? 0) > 0).map((d) => d.id)
  const lowBwAlarms = summary.data?.low_bandwidth ?? []
  const highBwAlarms = summary.data?.high_bandwidth ?? []
  const lowBw = lowBwAlarms.length
  const highBw = highBwAlarms.length
  const bwAlarms = lowBw + highBw
  const bwAlarmIds = [...new Set([...lowBwAlarms, ...highBwAlarms].map((a) => a.device_id))]

  const activeNodes = (nodes.data?.nodes ?? []).filter((n) => n.registered && !n.revoked_at)
  const staleNodes = activeNodes.filter((n) => n.last_seen && isStale(n.last_seen))
  // Devices behind a dark probe — what the "Stale probes" tile actually drills
  // into, since a probe id isn't a row on the Network page but its devices are.
  const staleNodeIds = new Set(staleNodes.map((n) => n.node_id))
  const staleProbeDeviceIds = deviceList
    .filter((d) => d.assigned_node_id && staleNodeIds.has(d.assigned_node_id))
    .map((d) => d.id)
  const triageCount = outageList.length + staleNodes.length
  const triageLoading = outages.isLoading || nodes.isLoading

  // "Live" is only honest if it names the freshest thing that actually reported.
  // The newest probe heartbeat is that clock — a dashboard claiming live while
  // every probe is stale is the one lie a NOC tool cannot afford.
  const lastSeen = activeNodes
    .map((n) => n.last_seen)
    .filter((t): t is string => !!t)
    .sort()
    .at(-1)
  const feedStale = !lastSeen || isStale(lastSeen)

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
  const visibleDevices = rankedDevices.slice(0, PANEL_ROW_CAP)

  const events = [...(recentEvents.data?.events ?? [])].sort((a, b) =>
    (b.occurred_at ?? b.received_at).localeCompare(a.occurred_at ?? a.received_at),
  )
  const visibleNodes = activeNodes.slice(0, PANEL_ROW_CAP)
  const visibleEvents = events.slice(0, PANEL_ROW_CAP)

  const stats: Stat[] = [
    {
      key: "devices",
      label: "Devices online",
      loading: devices.isLoading,
      value: monitored.length ? `${online}/${monitored.length}` : "—",
      // "— / all up" reads as a healthy network when it actually means nothing
      // is being watched at all. Name that case instead of implying health.
      detail: !monitored.length ? "no probe assigned"
        : online < monitored.length ? `${notUpDevices.length} not up` : "all up",
      tone: online < monitored.length ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Not up", notUpDevices.map((d) => d.id)),
      issueKind: "device_down",
    },
    {
      key: "ports",
      label: "Ports down",
      loading: devices.isLoading,
      value: portsDown,
      detail: portsDown > 0 ? "check switches" : "all up",
      tone: portsDown > 0 ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Ports down", portsDownIds),
      issueKind: "port_down",
    },
    {
      key: "probes",
      label: "Stale probes",
      loading: nodes.isLoading,
      value: staleNodes.length,
      detail: staleNodes.length > 0 ? "not reporting" : "all reporting",
      tone: staleNodes.length > 0 ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Behind a stale probe", staleProbeDeviceIds),
      issueKind: "probe_stale",
    },
    {
      key: "bw",
      label: "Bandwidth alarms",
      loading: summary.isLoading,
      value: bwAlarms,
      detail: bwAlarms > 0 ? [lowBw && `${lowBw} low`, highBw && `${highBw} high`].filter(Boolean).join(" · ") : "within limits",
      tone: bwAlarms > 0 ? "warning" : undefined,
      to: "/topology",
      filter: filterFor("Bandwidth alarm", bwAlarmIds),
      issueKind: "bandwidth",
    },
  ]

  // Second strip — optical/PON plane. Only rendered once the org actually runs
  // OLTs (a network with no fiber shouldn't stare at four zeros). All counts
  // ride the freshest walk per OLT, so a stale C-Data box never inflates them.
  const pon = ponSummary.data
  const hasOptics = (pon?.olts ?? 0) > 0
  const dupStale = pon ? pon.dup_macs_total - pon.dup_macs_live : 0
  // Is anything actually MEASURING optical power? A C-Data/DBC OLT walks a full
  // roster with every Rx NULL, so "0 critical ONUs" on that fleet means "no ONU
  // is measured", not "every ONU is healthy" — and those two render identically
  // as a green zero. The dBm tiles below say which, because reading the first
  // as the second is the whole failure mode the optical plane has to avoid.
  const rxCount = pon?.onus_rx ?? 0
  const noRx = hasOptics && rxCount === 0
  const partialRx = hasOptics && rxCount > 0 && rxCount < (pon?.onus_total ?? 0)
  // Shown under a dBm tile: never let a count stand alone when only part of the
  // fleet is measured.
  const rxCoverage = partialRx
    ? `${rxCount} of ${pon!.onus_total} ONUs measured`
    : `${rxCount} ONUs measured`
  // Per-OLT drill-down sets for the tiles below — sourced from the same rows
  // list_org_devices already stamps on each device, so they can't disagree with
  // the row chips (DeviceChips) a tech would land on next. PONs at capacity is
  // the one exception: that verdict lives per-PON, not per-device, so it rides
  // pon_summary's own device id list instead.
  const onusCritIds = deviceList.filter((d) => (d.onus_crit ?? 0) > 0).map((d) => d.id)
  const onusWarnIds = deviceList.filter((d) => (d.onus_warn ?? 0) > 0).map((d) => d.id)
  const dupMacIds = deviceList.filter((d) => (d.dup_macs ?? 0) > 0).map((d) => d.id)
  const fiberCutIds = deviceList.filter((d) => (d.fiber_cuts ?? 0) > 0).map((d) => d.id)
  const onusOfflineIds = deviceList
    .filter((d) => (d.onus_total ?? 0) > (d.onus_online ?? 0))
    .map((d) => d.id)
  const opticalStats: Stat[] = [
    {
      key: "onus-crit",
      label: "Critical ONUs",
      loading: ponSummary.isLoading,
      // An em dash, not a 0: nothing was measured, so there is no count to give.
      value: noRx ? "—" : (pon?.onus_crit ?? 0),
      detail: noRx ? "no OLT reports dBm"
        : (pon?.onus_crit ?? 0) > 0 ? "below the Rx floor" : rxCoverage,
      tone: !noRx && (pon?.onus_crit ?? 0) > 0 ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Critical ONUs", onusCritIds),
      issueKind: "onu_crit",
    },
    {
      key: "onus-warn",
      label: "Warning ONUs",
      loading: ponSummary.isLoading,
      value: noRx ? "—" : (pon?.onus_warn ?? 0),
      detail: noRx ? "check the Optical tab"
        : (pon?.onus_warn ?? 0) > 0 ? "weak Rx power" : rxCoverage,
      tone: !noRx && (pon?.onus_warn ?? 0) > 0 ? "warning" : undefined,
      to: "/topology",
      filter: filterFor("Warning ONUs", onusWarnIds),
      issueKind: "onu_warn",
    },
    {
      key: "dup-macs",
      label: "Duplicate MACs",
      loading: ponSummary.isLoading,
      value: pon?.dup_macs_live ?? 0,
      detail: (pon?.dup_macs_live ?? 0) > 0 ? "cloned or looping"
        : dupStale > 0 ? `${dupStale} stale-only` : "none live",
      tone: (pon?.dup_macs_live ?? 0) > 0 ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Duplicate MACs", dupMacIds),
      issueKind: "dup_mac",
    },
    {
      key: "fiber",
      label: "Fiber cuts",
      loading: ponSummary.isLoading,
      value: pon?.fiber_cuts ?? 0,
      detail: (pon?.fiber_cuts ?? 0) > 0 ? "check optical tab" : "none suspected",
      tone: (pon?.fiber_cuts ?? 0) > 0 ? "destructive" : undefined,
      to: "/topology",
      filter: filterFor("Fiber cuts", fiberCutIds),
      issueKind: "pon_fiber",
    },
    {
      key: "pon-cap",
      label: "PONs at capacity",
      loading: ponSummary.isLoading,
      value: pon?.pons_over_cap ?? 0,
      detail: (pon?.pons_over_cap ?? 0) > 0
        ? `busiest has ${pon!.pon_cap_worst}`
        : `all under ${pon?.pon_cap ?? 64}`,
      tone: (pon?.pons_over_cap ?? 0) > 0 ? "warning" : undefined,
      to: "/topology",
      filter: filterFor("PON at capacity", pon?.over_cap_device_ids ?? []),
      issueKind: "pon_capacity",
    },
    {
      key: "onus",
      label: "ONUs online",
      loading: ponSummary.isLoading,
      value: pon?.onus_total ? `${pon.onus_online}/${pon.onus_total}` : "—",
      detail: (pon?.onus_offline ?? 0) > 0 ? `${pon!.onus_offline} offline` : "all up",
      to: "/topology",
      filter: filterFor("ONUs offline", onusOfflineIds),
      issueKind: "onu_offline",
    },
  ]

  const allStats = hasOptics ? [...stats, ...opticalStats] : stats

  // A tile reading ZERO is the absence of news, and ten equal cards is what you
  // get when nobody has decided what matters. Split them: anything with a
  // non-zero count or a status tone keeps a card, the rest collapse into one
  // strip. `24/24` and `1007/1579` are NOT zero — they are the fleet's two
  // denominators and stay loud — so the test is on the leading number, not on
  // the tone.
  const isZero = (s: Stat) =>
    !s.tone && (s.value === 0 || s.value === "0") && !s.loading
  const loudStats = allStats.filter((s) => !isZero(s))
  const quietStats = allStats.filter(isZero)

  // THE VERDICT HAS TO COVER THE WHOLE PAGE, or it must not use the words "all
  // clear". It used to read "All clear. No open outages, every probe reporting."
  // directly BELOW tiles saying 85 critical ONUs and 128 warning — true
  // sentence, false impression, and the one claim a NOC screen may never make
  // wrongly. `triageCount` only ever counted outages and stale probes, i.e. the
  // REACHABILITY plane; the optical plane was never in it.
  // So the verdict now grades on everything the page shows, and the middle case
  // gets its own words: nothing is DOWN, and that is not the same as nothing
  // being WRONG.
  const troubled = allStats.filter((s) => s.tone && !s.loading)
  const verdict = triageCount > 0
    ? null                                  // the triage queue speaks for itself
    : troubled.length === 0
      ? { tone: "success" as const, head: "All clear.",
          rest: "No open outages, every probe reporting." }
      : { tone: "warning" as const,
          head: "Nothing is down.",
          // Names the two worst and COUNTS the rest. Listing all four produced a
          // sentence that ran the width of the page, and lower-casing the labels
          // to make it read as prose turned "Critical ONUs" into "critical onus"
          // — the acronyms in this domain are most of the nouns, so they are left
          // exactly as the tiles spell them.
          rest: `Every device is up and every probe reporting, but ${
            [...troubled]
              .sort((a, b) => (a.tone === "destructive" ? 0 : 1) - (b.tone === "destructive" ? 0 : 1))
              .slice(0, 2)
              .map((t) => `${t.value} ${t.label}`)
              .join(", ")
          }${troubled.length > 2 ? `, and ${troubled.length - 2} more` : ""} need attention.` }

  return (
    <div className="wisp-page @container flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex items-center justify-between gap-4">
        {/* "Home", not "Overview": the superadmin's /overview platform page owns
            that word, and both were in the sidebar at once. */}
        <h1 className="text-lg font-semibold tracking-tight">Home</h1>
        <div className="flex items-center gap-2 text-xs text-faint-foreground">
          <StatusDot tone={feedStale ? "destructive" : "success"} />
          {lastSeen
            ? <>{feedStale ? "Feed stale" : "Live"} · updated {ago(lastSeen)}</>
            : "No probe has reported yet"}
        </div>
      </div>

      {triageLoading && <Skeleton className="h-12 w-full rounded-xl" />}
      {!triageLoading && verdict && (
        <div className="wisp-panel flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3.5">
          <span className={cn("size-2 shrink-0 rounded-full",
            verdict.tone === "success" ? "bg-success ring-4 ring-success/15"
              : "bg-warning ring-4 ring-warning/15")} />
          <span className="text-sm font-medium text-foreground">{verdict.head}</span>
          <span className="text-sm text-muted-foreground">{verdict.rest}</span>
          {lastSeen && (
            <span className="ml-auto text-xs text-faint-foreground">Last check {ago(lastSeen)}</span>
          )}
        </div>
      )}

      {/* BAND 2 — only the tiles that are SAYING something.
          Ten equal tiles is what you produce when you have not decided what
          matters: on a healthy fleet four of them read 0 and still spend a full
          card each, so the two that are shouting have to compete with eight that
          are not. A tile reading zero is not news — it is the ABSENCE of news —
          so it collapses into one quiet strip below, where it remains readable
          and clickable but costs no attention.

          The grid is sized by CONTAINER queries, not viewport ones: it is as
          likely to be living in half a window (split view) as in a whole one,
          and `md:` asks the wrong box. The steps reproduce the old widths
          exactly at every viewport — the container is the page box, so a 768px
          `md:` viewport is a ~448px container — with ONE step added at @md,
          because three tiles across 4 columns of a 600px pane truncate their
          own titles. */}
      {loudStats.length > 0 && (
        <div className={cn("grid grid-cols-2 gap-3 @md:grid-cols-3 @2xl:grid-cols-4",
          loudStats.length >= 5 && "@4xl:grid-cols-5")}>
          {loudStats.map((s) => <StatTile key={s.key} s={s} />)}
        </div>
      )}

      {/* BAND 3 — the all-clear strip. Every one of these reads zero, and a zero
          is worth SEEING (it is the difference between "no fiber cuts" and "we
          are not looking for fiber cuts") but not worth a card. */}
      {quietStats.length > 0 && (
        <div className="wisp-panel flex flex-wrap items-center gap-x-5 gap-y-2 px-5 py-3">
          {quietStats.map((s) => (
            <span key={s.key} className="flex items-baseline gap-1.5 text-xs">
              <span className="font-mono font-medium text-muted-foreground">{s.value}</span>
              <span className="text-faint-foreground">{s.label.toLowerCase()}</span>
            </span>
          ))}
        </div>
      )}

      {/* Triage only claims screen space when something actually needs triage — a
          healthy network gets one quiet all-clear line, not a large empty box. */}
      {!triageLoading && triageCount > 0 && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <TriangleAlert className="size-4 text-destructive" />
              Triage queue
            </h2>
            <div className="flex items-center gap-3">
              <ClearPostmortems org={scopeOrg} count={pendingPostmortems} />
              <span className="rounded-4xl border bg-card px-2.5 py-0.5 text-xs font-semibold">
                {triageCount} open
              </span>
            </div>
          </div>
          <div className="grid gap-3 @md:grid-cols-2 @md:items-start @4xl:grid-cols-3">
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

      <div className="grid items-start gap-4 @2xl:grid-cols-[1.85fr_1fr]">
        <Panel title="Network" count={`${rankedDevices.length} devices`}
          action={{ label: "Topology", to: "/topology" }}>
          {devices.isLoading && <Skeleton className="m-4 h-32" />}
          {!devices.isLoading && deviceList.length === 0 && (
            <PanelEmpty>No devices yet. Add them on the Network page.</PanelEmpty>
          )}
          {visibleDevices.map((d) => {
            const uptime = uptimeByDevice.get(d.id)
            const unassigned = !d.assigned_node_id
            const stale = !unassigned && !!d.state && isStale(d.state_updated_at)
            return (
              <Link key={d.id} to="/topology" state={{ deviceId: d.id }}
                className={cn(ROW, "transition-colors hover:bg-foreground/5")}>
                <StatusDot tone={unassigned ? "muted" : deviceTone(d.state, d.state_updated_at)} />
                <span className={cn("min-w-0 truncate font-mono text-xs font-medium",
                  unassigned && "text-muted-foreground")}>{d.name}</span>
                {d.device_type && (
                  <span className="hidden shrink-0 text-xs text-faint-foreground md:inline">{d.device_type}</span>
                )}
                {d.region && (
                  <span className="hidden min-w-0 truncate text-xs text-faint-foreground lg:inline">· {d.region}</span>
                )}
                <span className="ml-auto flex shrink-0 items-baseline gap-4 text-right">
                  {unassigned && (
                    <span className="text-xs text-ghost-foreground">not monitored</span>
                  )}
                  {!unassigned && d.maintenance === 1 && (
                    <span className="text-xs text-faint-foreground">maintenance</span>
                  )}
                  {stale && <span className="text-xs text-faint-foreground">stale · {ago(d.state_updated_at)}</span>}
                  {!unassigned && !stale && d.state && d.state !== "UP" && (
                    <span className={cn("font-mono text-xs font-semibold",
                      d.state === "DEGRADED" ? "text-warning" : "text-destructive")}>
                      {d.state}
                    </span>
                  )}
                  {!unassigned && !stale && d.state === "UP" && d.latency_ms != null && (
                    <span className="w-14 font-mono text-xs text-muted-foreground">{Math.round(d.latency_ms)} ms</span>
                  )}
                  {!unassigned && !stale && d.state === "UP" && d.packet_loss != null && d.packet_loss > 0 && (
                    <span className="font-mono text-xs text-warning">{Math.round(d.packet_loss)}% loss</span>
                  )}
                  {/* A 30-DAY ROLLUP IS REFERENCE, NOT STATE. This was drawn in
                      --warning below 99%, which made it one of only six
                      chromatic elements on a healthy Home — a historical
                      average wearing an alarm tone while the device's LIVE
                      verdict was a 6px dot beside its name. Reference outranking
                      state is the inversion this pass exists to remove. It stays
                      readable and still sorts the panel; it just stops
                      shouting. A device that is actually in trouble says so
                      through the dot, the state word, and the loss figure. */}
                  {uptime != null && (
                    <span className="hidden w-16 font-mono text-xs text-faint-foreground sm:inline"
                      title={`${fmtUptime(uptime)} uptime over the last 7 days`}>
                      {fmtUptime(uptime)}
                    </span>
                  )}
                </span>
              </Link>
            )
          })}
          {rankedDevices.length > PANEL_ROW_CAP && (
            <PanelMore to="/topology">All {rankedDevices.length} devices →</PanelMore>
          )}
        </Panel>

        <div className="flex flex-col gap-4">
          <Panel title="Probes" count={activeNodes.length || undefined}
            action={{ label: "Manage", to: "/topology" }}>
            {nodes.isLoading && <Skeleton className="m-4 h-16" />}
            {!nodes.isLoading && activeNodes.length === 0 && (
              <PanelEmpty>No probes registered.</PanelEmpty>
            )}
            {visibleNodes.map((n) => {
              const stale = !n.last_seen || isStale(n.last_seen)
              return (
                <div key={n.node_id} className={ROW}>
                  <StatusDot tone={stale ? "destructive" : "success"} />
                  <span className="min-w-0 truncate font-mono text-xs font-medium">{n.node_id}</span>
                  {n.version && <span className="shrink-0 font-mono text-2xs text-faint-foreground">{n.version}</span>}
                  <span className={cn("ml-auto shrink-0 font-mono text-xs",
                    stale ? "text-destructive" : "text-faint-foreground")}>
                    {n.last_seen ? ago(n.last_seen) : "never seen"}
                  </span>
                </div>
              )
            })}
            {activeNodes.length > PANEL_ROW_CAP && (
              <PanelMore to="/topology">All {activeNodes.length} probes →</PanelMore>
            )}
          </Panel>

          <Panel title="Recent activity" action={{ label: "Logs", to: "/logs" }}>
            {recentEvents.isLoading && <Skeleton className="m-4 h-24" />}
            {!recentEvents.isLoading && events.length === 0 && (
              <PanelEmpty>No events yet.</PanelEmpty>
            )}
            {visibleEvents.map((ev) => (
              <div key={ev.id} className={ROW}>
                <StatusDot tone={eventTone(ev)} />
                <span className="min-w-0 shrink-0 truncate font-mono text-xs font-medium">
                  {ev.device_name || "—"}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={describeEvent(ev)}>
                  {describeEvent(ev)}
                </span>
                <span className="ml-auto shrink-0 font-mono text-xs text-faint-foreground">
                  {ago(ev.occurred_at ?? ev.received_at)}
                </span>
              </div>
            ))}
            {events.length > PANEL_ROW_CAP && (
              <PanelMore to="/logs">More in Logs →</PanelMore>
            )}
          </Panel>
        </div>
      </div>
    </div>
  )
}
