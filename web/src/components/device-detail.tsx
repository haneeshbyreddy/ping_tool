// The per-device drill-down panel (Health / Optical / Ports tabs) shared by the
// Network tree rows and the Map pin popover — one implementation, two surfaces.
import { useEffect, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronRight, Plus, X, type LucideIcon } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { analyticsApi, inventoryApi } from "@/lib/api"
import { isPassiveType, type OrgDevice, type SwitchPort } from "@/lib/types"
import { AssignmentPanel } from "@/components/device-assignees"
import { Meter } from "@/components/meter"
import { OpticalPanel } from "@/components/optical-panel"
import { SnmpDiagnosis } from "@/components/snmp-diagnosis"
import { DistributionPanel } from "@/components/splitter-panel"
import {
  WebUiButton, WebUiCredentialsButton, canOpenWebUi, useCanManageCreds, useWebProxy,
} from "@/components/web-proxy"
import { bucketTrouble, HourStrip, TrendSpark } from "@/components/sparkline"
import { CHIP_BOX, PlaneDot, StatusDot, TONE_CLASS } from "@/components/status-badge"
import type { Plane } from "@/lib/planes"
import { ago, durationSince, fmtBytes, fmtDur, fmtMs, isDownState, isFresh, isStale } from "@/lib/format"
import { paletteVarOf } from "@/lib/palette"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

const BW_DIRECTIONS = ["in", "out", "either", "total"] as const

function PortBandwidthForm({ port, onSaved }: { port: SwitchPort; onSaved: () => void }) {
  const [min, setMin] = useState(port.bw_threshold_mbps == null ? "" : String(port.bw_threshold_mbps))
  const [max, setMax] = useState(port.bw_max_mbps == null ? "" : String(port.bw_max_mbps))
  const [direction, setDirection] = useState<string>(port.bw_direction ?? "either")

  const save = useMutation({
    mutationFn: () => {
      const minVal = min.trim() === "" ? null : Number(min)
      const maxVal = max.trim() === "" ? null : Number(max)
      if (minVal != null && maxVal != null && maxVal <= minVal) {
        throw new Error("max must be greater than min")
      }
      return inventoryApi.setPortBandwidth(port.id, minVal, direction, maxVal)
    },
    onSuccess: () => { toast.success("Bandwidth limits saved"); onSaved() },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to save limits"),
  })

  return (
    <div className="flex flex-wrap items-end gap-2 text-xs">
      <div className="flex flex-col gap-0.5">
        <Label className="text-2xs text-muted-foreground">Min Mbps</Label>
        <Input type="number" min="0" placeholder="none" value={min}
          onChange={(e) => setMin(e.target.value)} className="h-7 w-20 text-xs" />
      </div>
      <div className="flex flex-col gap-0.5">
        <Label className="text-2xs text-muted-foreground">Max Mbps</Label>
        <Input type="number" min="0" placeholder="none" value={max}
          onChange={(e) => setMax(e.target.value)} className="h-7 w-20 text-xs" />
      </div>
      <div className="flex flex-col gap-0.5">
        <Label className="text-2xs text-muted-foreground">Direction</Label>
        <Select value={direction} onValueChange={setDirection}>
          <SelectTrigger className="h-7 w-24 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            {BW_DIRECTIONS.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <Button size="sm" className="h-7" disabled={save.isPending} onClick={() => save.mutate()}>
        Save
      </Button>
    </div>
  )
}

function fmtRate(bps: number | null): string {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(0)} kb/s`
  return `${Math.round(bps)} b/s`
}

function portTone(p: SwitchPort): "success" | "destructive" | "muted" {
  if (p.admin_status !== "up") return "muted"
  return p.oper_status === "up" ? "success" : "destructive"
}

function portAlarmed(p: SwitchPort): boolean {
  return !!p.monitored && (p.alarm === 1 || p.bw_alarm === 1 || p.bw_high_alarm === 1)
}

export function PortsPanel({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const [configOpen, setConfigOpen] = useState<number | null>(null)
  const { data, isLoading } = useQuery({
    queryKey: ["inventory-ports", device.id],
    queryFn: () => inventoryApi.ports(device.id),
    refetchInterval: 30_000, // rates/alarms move on the SNMP cadence; SSE doesn't cover this key
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["inventory-ports", device.id] })

    queryClient.invalidateQueries({ queryKey: ["inventory"] })
  }
  const toggleMonitored = useMutation({
    mutationFn: (p: SwitchPort) => inventoryApi.setPortMonitored(p.id, !p.monitored),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to update port"),
  })

  if (isLoading) return <Skeleton className="h-16 w-full" />
  const ports = data?.ports ?? []
  if (ports.length === 0) {
    // Not a dead end: the edge diagnoses WHY each SNMP sweep came back empty.
    return <SnmpDiagnosis device={device} subsystem="ports" />
  }

  // alarmed first (a down port that's alarming is the urgent one), then open/up
  // ports, then quiet monitored ports, then everything else; if_index as tie-break.
  const rank = (p: SwitchPort) =>
    portAlarmed(p) ? 0 : p.oper_status === "up" ? 1 : p.monitored ? 2 : 3
  const sorted = [...ports].sort((a, b) => rank(a) - rank(b) || a.if_index - b.if_index)
  const watched = ports.filter((p) => p.monitored).length
  const down = ports.filter((p) => p.monitored && p.alarm === 1).length
  const bwAlarms = ports.filter((p) => p.monitored && (p.bw_alarm === 1 || p.bw_high_alarm === 1)).length
  // Newest port row = last successful SNMP port walk. These rows persist, so without
  // this stamp a walk that quietly stopped weeks ago still looks live. Matches the
  // dim/green capability icon on the row (same 900s freshness rule).
  const lastWalk = ports.reduce<string | null>(
    (a, p) => (p.updated_at && (!a || p.updated_at > a) ? p.updated_at : a), null)
  const portsStale = !isFresh(lastWalk)
  // A device that isn't answering ICMP isn't answering SNMP either, so every row
  // below is the last walk before it dropped — frozen the INSTANT it went down,
  // which is up to 15 minutes before the 900s staleness rule would notice. The
  // per-port alarm counts go with it: "3 down" off a frozen table is a claim
  // about now, and "0 down" would be just as false, so the header states the
  // one thing that IS true (the box is unreachable) instead of counting.
  const isDown = isDownState(device.state)

  return (
    // @container, not viewport breakpoints: this table renders inside a ~380–420px
    // side panel on a wide screen, where every `sm:` guard passes and the row
    // overflows its own panel (the documented trap — see CLAUDE.md "Viewport
    // breakpoints are wrong inside the device panel"). The rate column is what
    // gives way when it's tight: the toggle is the row's action and the limits
    // button is how you reach the form, while the same throughput is already on
    // the tree row's bandwidth chip and in this panel's own header counts.
    <div className="@container overflow-hidden rounded-lg border bg-muted/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-2xs text-muted-foreground">
        <span className="font-medium">{ports.length} ports · {watched} watched</span>
        {!isDown && down > 0 && <span className="font-semibold text-destructive">{down} down</span>}
        {!isDown && bwAlarms > 0 && <span className="font-semibold text-warning">{bwAlarms} bandwidth</span>}
        {/* stale is a data-freshness note, not an alarm — neutral, never amber */}
        {isDown
          ? <span className="font-semibold text-foreground" title="This device is unreachable, so its port table can't refresh. These rows are the last walk before it went down.">device offline · last walk {ago(lastWalk)}</span>
          : portsStale
          ? <span className="font-semibold" title="The SNMP port walk on this device has stopped refreshing. These rows are the last good snapshot.">stale · {ago(lastWalk)}</span>
          : lastWalk && <span className="text-faint-foreground">as of {ago(lastWalk)}</span>}
        {!isDown && <span className="ml-auto hidden @[30rem]:inline">watch a port to alarm on it</span>}
      </div>
      {sorted.map((p) => {
        const limits = [
          p.bw_threshold_mbps != null && `≥${p.bw_threshold_mbps}`,
          p.bw_max_mbps != null && `≤${p.bw_max_mbps}`,
        ].filter(Boolean).join(" ")
        return (
          <div key={p.id} className={cn("border-b last:border-b-0", isDown && "wisp-frozen")}>
            <div className={cn("flex h-10 items-center gap-2 px-4", portAlarmed(p) && "bg-destructive-soft/30")}>
              <StatusDot tone={portTone(p)} />
              <span className={cn("min-w-0 shrink truncate font-mono text-xs font-medium",
                !p.monitored && "text-muted-foreground")}>
                {p.if_name || `if${p.if_index}`}
                {p.if_alias && <span className="font-normal text-muted-foreground"> · {p.if_alias}</span>}
              </span>
              {p.admin_status !== "up" && <RowTag tone="muted">admin down</RowTag>}
              {!!p.monitored && p.alarm === 1 && <RowTag tone="destructive">down</RowTag>}
              {p.bw_alarm === 1 && <RowTag tone="warning">low bw</RowTag>}
              {p.bw_high_alarm === 1 && <RowTag tone="warning">high bw</RowTag>}
              <span className="ml-auto hidden shrink-0 font-mono text-xs text-muted-foreground @[30rem]:inline">
                ↓{fmtRate(p.in_bps)}&ensp;↑{fmtRate(p.out_bps)}
              </span>
              {!!p.monitored && (
                <button
                  className={cn("shrink-0 rounded px-1.5 py-0.5 font-mono text-2xs",
                    limits ? "text-muted-foreground hover:bg-accent" : "text-faint-foreground hover:bg-accent")}
                  title="Bandwidth limits (Mbps)"
                  onClick={() => setConfigOpen(configOpen === p.id ? null : p.id)}>
                  {limits ? `${limits} ${p.bw_direction ?? "either"}` : "set limits"}
                </button>
              )}
              <Switch checked={!!p.monitored} onCheckedChange={() => toggleMonitored.mutate(p)}
                title={p.monitored ? "Stop watching this port" : "Watch this port"}
                className="shrink-0 scale-75" />
            </div>
            {configOpen === p.id && !!p.monitored && (
              <div className="border-t bg-card/50 px-4 py-2.5">
                <PortBandwidthForm port={p} onSaved={() => { invalidate(); setConfigOpen(null) }} />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function DeviceMetrics({ device }: { device: OrgDevice }) {
  // Passive plant is never probed BY DESIGN, so "not monitored" reads as a
  // config gap that isn't one. The row already carries a `passive` chip and the
  // panel names the type — there is no reading to stand in for.
  if (isPassiveType(device.device_type)) return null
  if (!device.assigned_node_id) return <span className="text-xs text-faint-foreground">not monitored</span>
  if (!device.state) return <span className="text-xs text-faint-foreground">no data</span>
  if (isStale(device.state_updated_at)) {
    return <span className="text-xs text-muted-foreground">stale · {ago(device.state_updated_at)}</span>
  }
  if (device.state === "DOWN" || device.state === "UNREACHABLE") {
    return <span className="text-xs font-semibold text-destructive">{device.state}</span>
  }
  const latency = device.latency_ms == null ? "—" : `${fmtMs(device.latency_ms)} ms`
  const loss = device.packet_loss ? ` · ${Math.round(device.packet_loss)}% loss` : ""
  if (device.state === "DEGRADED") {
    return (
      <span className="text-xs font-semibold text-warning">
        {/* detail hides on narrow screens so a long readout never truncates the name */}
        DEGRADED<span className="hidden font-mono font-normal sm:inline"> · {latency}{loss}</span>
      </span>
    )
  }
  return <span className="font-mono text-xs text-muted-foreground">{latency}{loss}</span>
}

const median = (xs: number[]): number | null =>
  xs.length ? [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] : null

// SNMP device vitals (CPU / RAM / temperature) — display-only, never alarms.
// Warn/crit tints only; the thresholds are conventional NOC eyeball values.
export const VITAL_CPU_WARN = 80, VITAL_CPU_CRIT = 95
export const VITAL_MEM_WARN = 80, VITAL_MEM_CRIT = 95
export const VITAL_TEMP_WARN = 70, VITAL_TEMP_CRIT = 85

function hasVitals(device: OrgDevice): boolean {
  return device.health_cpu_pct != null || device.health_mem_pct != null
    || device.health_temp_c != null
}

function DeviceVitals({ device }: { device: OrgDevice }) {
  const { health_cpu_pct: cpu, health_mem_pct: mem, health_temp_c: temp } = device
  if (!hasVitals(device)) {
    // SNMP is on but no CPU/RAM/temp ever landed — say why instead of hiding the
    // section (an SNMP-less device stays quiet; there's nothing to diagnose).
    if (device.snmp_enabled === 1) {
      return (
        <div className="flex flex-col gap-2">
          <span className="text-2xs font-medium text-muted-foreground">Device health</span>
          <SnmpDiagnosis device={device} subsystem="health" />
        </div>
      )
    }
    return null
  }
  // Unreachable box ⇒ these vitals are the last successful health walk, not the
  // machine's condition now. Stamp the reading unconditionally (not just past the
  // staleness threshold) and gray the meters: a warn/crit tint here is otherwise
  // an alarm about a device that isn't there to be hot.
  const isDown = isDownState(device.state)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between text-2xs text-muted-foreground">
        <span className="font-medium">Device health</span>
        {device.health_updated_at && (isDown || isStale(device.health_updated_at)) && (
          <span className="text-faint-foreground">as of {ago(device.health_updated_at)}</span>
        )}
      </div>
      <div className={cn("flex flex-col gap-2", isDown && "wisp-frozen")}>
        {cpu != null && (
          <Meter label="CPU" pct={cpu} warn={VITAL_CPU_WARN} crit={VITAL_CPU_CRIT} />
        )}
        {mem != null && (
          <Meter label="RAM" pct={mem} warn={VITAL_MEM_WARN} crit={VITAL_MEM_CRIT}
            detail={device.health_mem_used_bytes != null && device.health_mem_total_bytes != null
              ? `${fmtBytes(device.health_mem_used_bytes)} / ${fmtBytes(device.health_mem_total_bytes)}`
              : undefined} />
        )}
        {temp != null && (
          <Meter label="Temp" pct={Math.min(100, Math.max(0, temp))} value={`${Math.round(temp)}°C`}
            warn={VITAL_TEMP_WARN} crit={VITAL_TEMP_CRIT} />
        )}
      </div>
    </div>
  )
}

export function DevicePerfPanel({ device }: { device: OrgDevice }) {
  const { scopeOrg } = useAuth()
  const live = useQuery({
    queryKey: ["perf-samples", device.id],
    queryFn: () => inventoryApi.perfSamples(device.id),
    refetchInterval: 15_000,
  })
  const trend = useQuery({
    queryKey: ["perf-trend", device.id],
    queryFn: () => analyticsApi.trend(device.id, 1),
    refetchInterval: 60_000,
  })
  const perf = useQuery({
    queryKey: ["perf-state", device.id],
    queryFn: () => inventoryApi.perf(device.id),
    refetchInterval: 60_000,
  })

  const reliability = useQuery({
    queryKey: ["reliability", scopeOrg],
    queryFn: () => analyticsApi.reliability(scopeOrg, 7),
    staleTime: 60_000,
    enabled: !!scopeOrg,
  })
  if (live.isLoading) return <Skeleton className="h-20 w-full" />
  if (live.error) {
    return (
      <p className="rounded-lg border border-destructive/30 bg-destructive-soft/40 px-3 py-2 text-xs text-destructive">
        Couldn't load the latency history ({live.error instanceof Error ? live.error.message : "request failed"}).
      </p>
    )
  }

  const samples = live.data?.samples ?? []
  const buckets = trend.data?.buckets ?? []
  const perfRow = perf.data?.perf
  const rel = reliability.data?.devices.find((d) => d.device_id === device.id)
  const latest = samples.at(-1)

  const typical = perfRow?.baseline_ms
    ?? median(buckets.filter((b) => !bucketTrouble(b) && b.avg_latency_ms != null)
      .map((b) => b.avg_latency_ms!))
  const roughHours = buckets.filter((b) => bucketTrouble(b)).length
  const isDown = isDownState(device.state)

  return (
    <div className="flex flex-col gap-2.5 rounded-lg border bg-muted/40 p-3">
      {/* now + verdict --------------------------------------------------------- */}
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {isDown ? (
          <span className="text-sm font-semibold text-destructive">{device.state}</span>
        ) : latest?.latency_ms != null ? (
          <>
            <span className="font-mono text-sm font-semibold">{fmtMs(latest.latency_ms)} ms</span>
            {(latest.packet_loss ?? 0) > 0 && (
              <span className="text-xs font-semibold text-destructive">{Math.round(latest.packet_loss!)}% loss</span>
            )}
            {latest.jitter_ms != null && (
              <span className="font-mono text-xs text-muted-foreground">±{fmtMs(latest.jitter_ms)} ms jitter</span>
            )}
          </>
        ) : (
          <span className="text-xs text-muted-foreground">no reading yet</span>
        )}
        <span className="ml-auto text-right text-xs">
          {perfRow?.degraded === 1 && perfRow.current_ms != null && perfRow.baseline_ms != null ? (
            <span className="font-semibold text-warning">
              {perfRow.metric ?? "latency"} {(perfRow.current_ms / Math.max(perfRow.baseline_ms, 0.1)).toFixed(1)}×
              its normal {fmtMs(perfRow.baseline_ms)} ms
              {/* first token only — "1h 5m" → "1h": a verdict wants a magnitude, not a stopwatch */}
              {perfRow.since && <span className="font-normal"> · for {durationSince(perfRow.since).split(" ")[0]}</span>}
            </span>
          ) : !isDown && typical != null ? (
            <span className="text-muted-foreground">normal for this link · ~{fmtMs(typical)} ms</span>
          ) : null}
        </span>
      </div>

      {/* device internals, same freshness rules as the port/optics sweeps -------- */}
      <DeviceVitals device={device} />

      {/* when was it bad, last 24 clock hours ----------------------------------- */}
      <div>
        <div className="mb-1 flex items-baseline justify-between text-2xs text-muted-foreground">
          <span className="font-medium">Last 24 h</span>
          <span className={cn(roughHours > 0 && "font-semibold text-warning")}>
            {trend.error ? "hourly history unavailable"
              : buckets.length === 0 ? "no history yet"
              : roughHours > 0 ? `${roughHours} rough hour${roughHours === 1 ? "" : "s"}` : "clean"}
          </span>
        </div>
        {/* the SHAPE, then the VERDICT — same grid, same 24 slots, so a rising
            line and the hour it finally went rough line up vertically */}
        <TrendSpark buckets={buckets} />
        <HourStrip buckets={buckets} />
        <div className="mt-0.5 flex justify-between text-2xs text-muted-foreground">
          <span>24 h ago</span><span>now</span>
        </div>
      </div>

      {/* can I trust it --------------------------------------------------------- */}
      {rel && (
        <p className="border-t pt-2 text-2xs text-muted-foreground">
          Last 7 days ·{" "}
          <span className={cn("font-mono font-semibold",
            rel.uptime_pct >= 99.9 ? "text-success" : rel.uptime_pct >= 99 ? "text-foreground" : "text-warning")}>
            {rel.uptime_pct.toFixed(rel.uptime_pct >= 100 ? 0 : 2)}%
          </span>{" "}
          uptime · {rel.outage_count === 0 ? "no outages"
            : `${rel.outage_count} outage${rel.outage_count === 1 ? "" : "s"} · ${fmtDur(rel.downtime_seconds)} down`}
        </p>
      )}
    </div>
  )
}

// ----- physical connection (which port carries each uplink) ------------------
// The closest-to-reality model of the plant: a link isn't just parent→child, it
// leaves a specific port on each box. The child side writes
// switch_ports.uplink_device_id, the parent side writes feeds_device_id — the
// SAME column ports.py folds a port-down into the child's outage through, so
// declaring the cabling here also arms that (once the port is watched). The map
// reads both to hang a live bandwidth label on the link line.

function UplinkPortSelect({ owner, bound, onPick, busy }: {
  owner: OrgDevice
  bound: SwitchPort | undefined
  onPick: (portId: number | null) => void
  busy: boolean
}) {
  const { canWrite } = useAuth()
  const snmp = owner.snmp_enabled === 1
  const { data, isLoading } = useQuery({
    queryKey: ["inventory-ports", owner.id],
    queryFn: () => inventoryApi.ports(owner.id),
    enabled: snmp,
    staleTime: 30_000,
  })
  if (!snmp) {
    return <span className="flex h-7 items-center text-2xs text-faint-foreground">no SNMP ports</span>
  }
  const ports = data?.ports ?? []
  if (!isLoading && ports.length === 0) {
    return <span className="flex h-7 items-center text-2xs text-faint-foreground">no ports walked yet</span>
  }
  if (!canWrite) {
    return (
      <span className="flex h-7 items-center font-mono text-xs">
        {bound ? (bound.if_name || `if${bound.if_index}`) : <span className="text-faint-foreground">not set</span>}
      </span>
    )
  }
  return (
    <Select value={bound ? String(bound.id) : "none"} disabled={isLoading || busy}
      onValueChange={(v) => onPick(v === "none" ? null : Number(v))}>
      <SelectTrigger className="h-7 w-full text-xs"><SelectValue placeholder="port…" /></SelectTrigger>
      <SelectContent>
        <SelectItem value="none"><span className="text-muted-foreground">no port</span></SelectItem>
        {ports.map((p) => (
          <SelectItem key={p.id} value={String(p.id)}>
            <span className="font-mono">{p.if_name || `if${p.if_index}`}</span>
            {p.if_alias && <span className="text-muted-foreground"> · {p.if_alias}</span>}
            {p.oper_status !== "up" && <span className="text-faint-foreground"> (down)</span>}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// One link, from the point of view of the panel we're in: `near` is this device,
// `far` is the other end. The only thing the kind changes is how the FAR end's
// port is bound — a dependency link uses feeds_device_id (which ports.py also
// folds a port-down through), while an undirected cross-link uses uplink_device_id
// on BOTH ends, deliberately: feeds_device_id would tell ports.py this port feeds
// the peer, and a cross-link cable dropping is not the peer's outage cause.
function LinkRow({ near, far, kind }: {
  near: OrgDevice; far: OrgDevice; kind: "primary" | "backup" | "peer"
}) {
  const { canWrite } = useAuth()
  const queryClient = useQueryClient()
  const nearQ = useQuery({
    queryKey: ["inventory-ports", near.id],
    queryFn: () => inventoryApi.ports(near.id),
    enabled: near.snmp_enabled === 1,
    staleTime: 30_000,
  })
  const farQ = useQuery({
    queryKey: ["inventory-ports", far.id],
    queryFn: () => inventoryApi.ports(far.id),
    enabled: far.snmp_enabled === 1,
    staleTime: 30_000,
  })
  const nearBound = nearQ.data?.ports.find((p) => p.uplink_device_id === far.id)
  const farBound = farQ.data?.ports.find((p) => kind === "peer"
    ? p.uplink_device_id === near.id : p.feeds_device_id === near.id)

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["inventory-ports", near.id] })
    queryClient.invalidateQueries({ queryKey: ["inventory-ports", far.id] })
    queryClient.invalidateQueries({ queryKey: ["link-ports"] })
  }
  const setNearPort = useMutation({
    // re-picking moves the binding: clear the old port, then bind the new one
    mutationFn: async (portId: number | null) => {
      if (nearBound && nearBound.id !== portId) await inventoryApi.setPortUplink(nearBound.id, null)
      if (portId != null) await inventoryApi.setPortUplink(portId, far.id)
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to set the port"),
  })
  const setFarPort = useMutation({
    mutationFn: async (portId: number | null) => {
      const bind = kind === "peer" ? inventoryApi.setPortUplink : inventoryApi.setPortFeeds
      if (farBound && farBound.id !== portId) await bind(farBound.id, null)
      if (portId != null) await bind(portId, near.id)
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to set the port"),
  })
  const removeLink = useMutation({
    mutationFn: () => kind === "peer"
      ? inventoryApi.removePeerLink(near.id, far.id)
      : inventoryApi.removeBackupLink(near.id, far.id),
    onSuccess: () => {
      toast.success(kind === "peer" ? "Cross-link removed" : "Backup link removed")
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      invalidate()
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to remove the link"),
  })

  // live rates read off the FAR end's port when bound (its egress is this link's
  // forward direction), normalized to this device's point of view
  const rateSrc = farBound ?? nearBound
  const down = farBound ? farBound.out_bps : nearBound?.in_bps ?? null
  const up = farBound ? farBound.in_bps : nearBound?.out_bps ?? null
  // Whichever END we read the counters off has to be reachable. A frozen
  // "↓450 Mb/s" on a dead link is worse than no figure at all — it's the number
  // an operator uses to rule the link OUT. Note this is the rate SOURCE, not
  // both ends: if the far end is up, its port genuinely still counts this link
  // (and reports the drop to zero), which is a real reading worth showing.
  const rateOwnerDown = isDownState(farBound ? far.state : near.state)
  const hasRates = !!rateSrc && !rateOwnerDown
    && isFresh(rateSrc.updated_at) && (down != null || up != null)
  const portDown = [nearBound, farBound].some(
    (p) => p && (p.oper_status === "down" || (p.monitored === 1 && p.alarm === 1)))
  const removable = kind !== "primary" && canWrite

  return (
    <div className="flex flex-col gap-1.5 border-b py-2 first:pt-1 last:border-b-0 last:pb-0">
      <div className="flex items-center gap-2">
        <span className="min-w-0 truncate font-mono text-xs font-medium">{far.name}</span>
        {kind !== "peer" && (
          <RowTag tone={kind === "backup" ? "success" : "muted"}>{kind}</RowTag>
        )}
        {portDown && <RowTag tone="destructive">port down</RowTag>}
        {hasRates && (
          <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground"
            title="Live rate on this link, toward / from this device">
            ↓{fmtRate(down)}&ensp;↑{fmtRate(up)}
          </span>
        )}
        {removable && (
          <Button variant="ghost" size="icon"
            className={cn("size-6 shrink-0 text-muted-foreground", !hasRates && "ml-auto")}
            title={kind === "peer" ? `Remove the cross-link to ${far.name}`
              : `Remove the backup link to ${far.name}`}
            disabled={removeLink.isPending} onClick={() => removeLink.mutate()}>
            <X className="size-3.5" />
          </Button>
        )}
      </div>
      <div className="grid grid-cols-2 items-end gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <Label className="truncate text-2xs text-muted-foreground">port on {near.name}</Label>
          <UplinkPortSelect owner={near} bound={nearBound}
            onPick={(id) => setNearPort.mutate(id)} busy={setNearPort.isPending} />
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <Label className="truncate text-2xs text-muted-foreground">port on {far.name}</Label>
          <UplinkPortSelect owner={far} bound={farBound}
            onPick={(id) => setFarPort.mutate(id)} busy={setFarPort.isPending} />
        </div>
      </div>
    </div>
  )
}

export function ConnectionPanel({ device }: { device: OrgDevice }) {
  const { scopeOrg, canWrite } = useAuth()
  const queryClient = useQueryClient()
  // revealed by the "+" next to Uplinks; a device with no cross-links shows no
  // Cross-links section at all. Resets when the panel moves to another device,
  // so an abandoned picker never follows you around the tree.
  const [addingPeer, setAddingPeer] = useState(false)
  // Cabling is reference material, not status: an operator opens this to wire up
  // ports or read a link's rate, and otherwise wants the tabs below it. So the
  // whole block folds, closed by default, with a summary on the header so the
  // fold still answers "what is this hanging off". Nothing ALARM-shaped hides in
  // here — a down uplink port is already a chip on the tree row and a row in the
  // Ports tab; this only ever hid the port pickers.
  const [open, setOpen] = useState(false)
  useEffect(() => { setAddingPeer(false); setOpen(false) }, [device.id])
  const { data } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 30_000,
  })
  const addBackup = useMutation({
    mutationFn: (parentId: number) => inventoryApi.addBackupLink(device.id, parentId),
    onSuccess: () => {
      toast.success("Backup uplink added. The ring closes here.")
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to add the backup link"),
  })
  const addPeer = useMutation({
    mutationFn: (peerId: number) => inventoryApi.addPeerLink(device.id, peerId),
    onSuccess: () => {
      toast.success("Cross-link added")
      // the section now stands on its own rows; drop the "asked for it" flag so
      // removing the last cross-link collapses it again
      setAddingPeer(false)
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to add the cross-link"),
  })

  // passive plant (splitters/FDBs) has no ports and no uplink of its own
  if (isPassiveType(device.device_type)) return null

  const devices = data?.devices ?? []
  const byId = new Map(devices.map((d) => [d.id, d]))
  // the fresh inventory row — `device` may predate a just-added link
  const self = byId.get(device.id) ?? device
  const parent = self.parent_device_id != null ? byId.get(self.parent_device_id) : undefined
  const backups = (self.backup_parents ?? [])
    .map((id) => byId.get(id)).filter((d): d is OrgDevice => !!d)
  const peers = (self.peer_ids ?? [])
    .map((id) => byId.get(id)).filter((d): d is OrgDevice => !!d)
  // anything already joined to this device — by any kind of edge — is off both
  // menus: the server refuses a second edge between one pair, so offering it
  // would just be an error waiting to happen
  const taken = new Set<number>([
    self.id, ...(self.parent_device_id != null ? [self.parent_device_id] : []),
    ...self.backup_parents, ...(self.peer_ids ?? []),
  ])
  const candidates = devices.filter(
    (d) => !taken.has(d.id) && !isPassiveType(d.device_type))
  // a device shouldn't back up its own descendant — that's the loop the server
  // rejects anyway; peers have no such rule (a ring of cross-links IS the point)
  const backupCandidates = candidates.filter((d) => d.parent_device_id !== self.id)
  // most plant is a plain tree: the section earns its space only once a
  // cross-link exists (or the operator asked for the picker)
  const showPeers = peers.length > 0 || addingPeer

  if (!parent && backups.length === 0 && peers.length === 0 && !canWrite) return null

  // What the closed header says instead of nothing: where this box hangs, and
  // whether there's more than the one plain uplink under the fold.
  const summary = [
    parent ? parent.name : backups.length === 0 ? "root device" : null,
    backups.length > 0 ? `+${backups.length} backup` : null,
    peers.length > 0 ? `${peers.length} cross-link${peers.length === 1 ? "" : "s"}` : null,
  ].filter(Boolean).join(" · ")

  return (
    <div className="flex flex-col rounded-lg border bg-muted/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className={cn("flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-foreground/5",
          open ? "rounded-t-lg" : "rounded-lg")}
        title="Which port carries each uplink, and any cross-links to devices at the same level">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <span className="text-2xs font-medium text-muted-foreground">Uplinks</span>
        {!open && summary && (
          <span className="min-w-0 truncate font-mono text-2xs text-faint-foreground">{summary}</span>
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          <div className="flex flex-col gap-1">
            {/* the ONLY cross-link affordance until one exists — most plant is a
                plain tree, so an operator who never cross-links never sees the
                section, just this one quiet button */}
            {!showPeers && canWrite && candidates.length > 0 && (
              <Button variant="ghost" size="sm"
                className="-mt-1 h-6 self-start px-1.5 text-2xs text-faint-foreground hover:text-foreground"
                title="Add a cross-link: a cable to a device at the same level. Documents the plant; does not affect alerting."
                onClick={() => setAddingPeer(true)}>
                <Plus className="size-3" /> cross-link
              </Button>
            )}
            {!parent && backups.length === 0 && (
              <p className="text-xs text-faint-foreground">No uplink · root device.</p>
            )}
            {parent && <LinkRow near={self} far={parent} kind="primary" />}
            {backups.map((b) => <LinkRow key={b.id} near={self} far={b} kind="backup" />)}
            {canWrite && backupCandidates.length > 0 && (
              <Select value="" disabled={addBackup.isPending}
                onValueChange={(v) => addBackup.mutate(Number(v))}>
                <SelectTrigger className="mt-1 h-7 w-full text-xs text-muted-foreground">
                  <SelectValue placeholder="Add backup uplink (ring)…" />
                </SelectTrigger>
                <SelectContent>
                  {backupCandidates.map((d) => (
                    <SelectItem key={d.id} value={String(d.id)}>
                      <span className="font-mono">{d.name}</span>
                      {d.device_type && <span className="text-muted-foreground"> · {d.device_type}</span>}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          {/* Cross-links: cabling between boxes at the same level. Deliberately NOT
              part of the dependency graph — whether traffic actually reroutes over
              one depends on STP/routing state we can't see, so these describe the
              plant (and carry bandwidth) without ever changing what pages. */}
          {showPeers && (
            <div className="flex flex-col gap-1 border-t pt-2.5">
              <div className="flex items-center gap-2">
                <span className="text-2xs font-medium text-muted-foreground"
                  title="Switch-to-switch cabling at the same level. Records the plant and shows live bandwidth. It does not affect alerting: declare a backup uplink if a path should actually fail over.">
                  Cross-links
                </span>
                {peers.length === 0 && (
                  <Button variant="ghost" size="icon"
                    className="ml-auto size-5 text-faint-foreground hover:text-foreground"
                    title="Cancel" onClick={() => setAddingPeer(false)}>
                    <X className="size-3" />
                  </Button>
                )}
              </div>
              {peers.map((p) => <LinkRow key={p.id} near={self} far={p} kind="peer" />)}
              {canWrite && candidates.length > 0 && (
                <Select value="" disabled={addPeer.isPending}
                  onValueChange={(v) => addPeer.mutate(Number(v))}>
                  <SelectTrigger className="mt-1 h-7 w-full text-xs text-muted-foreground">
                    <SelectValue placeholder="Add cross-link…" />
                  </SelectTrigger>
                  <SelectContent>
                    {candidates.map((d) => (
                      <SelectItem key={d.id} value={String(d.id)}>
                        <span className="font-mono">{d.name}</span>
                        {d.device_type && <span className="text-muted-foreground"> · {d.device_type}</span>}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** The inline chip carried by a Network tree row, an ONU row and a map search
 *  hit. It IS `Chip` (status-badge.tsx) — same box, same tone formula — with
 *  two additions that only a row needs: a click target that deep-links into the
 *  panel tab telling its story, and the operator-palette paint path.
 *
 *  IT USED TO BE ITS OWN GRAMMAR and that was the whole reason the Network page
 *  read as noise: UPPERCASE + `tracking-wide` + `font-semibold` with a fill and
 *  NO edge. That is the loudest type in this system, and it was spent equally
 *  on "7 FIBER CUTS" and on "MAINT" — so the loud style stopped meaning
 *  anything, and a busy OLT row was four shouting blocks with no rank between
 *  them. Sentence case with a 30% edge is the documented formula and the one
 *  Home and /issues already use; the Network page was the screen that never
 *  got it. Severity is carried by TONE, which is the only channel that ranks. */
export function RowTag({ tone, children, onClick, title, color, icon: Icon }: {
  tone: "warning" | "success" | "muted" | "destructive"
  children: ReactNode
  onClick?: (e: MouseEvent) => void
  title?: string
  /** An operator palette name (lib/palette.ts) — only ever reaches a chip that
   *  carries no status meaning (today: tags). It renders at the SAME weight the
   *  tone classes do, so a coloured tag can't outshout a real alarm chip. */
  color?: string | null
  /** A mark for the one chip worth identifying BEFORE it is read. Deliberately
   *  rare: an icon on every chip is the uppercase problem in another channel. */
  icon?: LucideIcon
}) {
  const painted = paletteVarOf(color)
  return (
    <span title={title} onClick={onClick}
      // the tone is DATA, so it rides a custom property into .wisp-tag, which
      // owns the light/dark readability correction (index.css)
      style={painted ? ({ "--tag": painted } as CSSProperties) : undefined}
      className={cn(CHIP_BOX, "gap-1 px-1.5",
        onClick && "cursor-pointer hover:brightness-125",
        painted ? "wisp-tag" : TONE_CLASS[tone])}>
      {Icon && <Icon className="size-3 shrink-0" aria-hidden />}
      {children}
    </span>
  )
}

// Identity block for a device side panel — dot, name, address line, live
// metrics. Shared by the Map's pin panel and the Network page's drill-in panel
// for the same reason DeviceDetail itself is: two panels naming the same device
// two different ways is how the surfaces drift. `tone` is a prop because the map
// mutes a pin under maintenance (pinTone) while the tree shows the real state;
// `children` are the surface's own header buttons (close, unpin, show-in-tree).
export function DevicePanelHeader({
  device, tone, downstream = 0, downstreamDown = 0, children,
}: {
  device: OrgDevice
  tone: "success" | "warning" | "destructive" | "muted"
  /** devices fed by this one — the map counts them, the tree already shows them */
  downstream?: number
  downstreamDown?: number
  children?: ReactNode
}) {
  return (
    <div className="flex items-start gap-2.5 border-b px-4 py-3">
      <span className="mt-1"><StatusDot tone={tone} /></span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="min-w-0 truncate font-mono text-sm font-semibold">{device.name}</p>
          {!!device.maintenance && <RowTag tone="muted">maint</RowTag>}
        </div>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          {device.ip_address && <span className="font-mono">{device.ip_address}</span>}
          {device.device_type && <span>{device.device_type}</span>}
          {device.region && <span>{device.region}</span>}
        </p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2">
          <DeviceMetrics device={device} />
          {isDownState(device.state) && device.outage_started_at && (
            <span className="text-xs font-semibold text-destructive">
              for {durationSince(device.outage_started_at)}
            </span>
          )}
        </div>
        {downstream > 0 && (
          <p className="mt-1 text-xs text-muted-foreground">
            Feeds <span className="font-semibold text-foreground">{downstream}</span> downstream
            {downstreamDown > 0 && (
              <span className="font-semibold text-destructive"> · {downstreamDown} down</span>
            )}
          </p>
        )}
      </div>
      {children && <div className="flex shrink-0 items-center gap-0.5">{children}</div>}
    </div>
  )
}

export type DeviceTab = "health" | "optical" | "ports"
export function isOpticalOlt(device: OrgDevice): boolean {
  return (device.device_type ?? "").toUpperCase() === "OLT" && device.snmp_enabled === 1
}
export function deviceTabs(device: OrgDevice): DeviceTab[] {
  // Optical leads for an OLT — it's the tab an operator actually wants first,
  // both as the leftmost tab and (see the drill-in callers) the one that opens
  // by default.
  const tabs: DeviceTab[] = []
  if (isOpticalOlt(device)) tabs.push("optical")
  tabs.push("health")
  if (device.snmp_enabled === 1) tabs.push("ports")
  return tabs
}
const TAB_LABEL: Record<DeviceTab, string> = { health: "Health", optical: "Optical", ports: "Ports" }
/** Which measurement plane each tab reads from. "Health" is the vitals plane —
 *  the tab is named for what an operator calls it, the plane for what produces
 *  it, and those do not have to be the same word. */
const TAB_PLANE: Record<DeviceTab, Plane> = { health: "vitals", optical: "optical", ports: "traffic" }

export function DeviceDetail({ device, tab, onTab, focusOnuId, focusOnuMac }: {
  device: OrgDevice; tab: DeviceTab; onTab: (t: DeviceTab) => void
  /** ONU row to reveal in the Optical tab — set when a map PON spoke is clicked */
  focusOnuId?: number | null
  /** the same, by MAC — how a placed reference ONU is keyed (see OpticalPanel) */
  focusOnuMac?: string | null
}) {
  const tabs = deviceTabs(device)
  const webUi = useWebProxy() && canOpenWebUi(device)
  const manageCreds = useCanManageCreds() && canOpenWebUi(device)
  // Passive plant gets a panel of its own rather than the monitoring one with
  // every reading blank. A splitter is never probed, has no ports, no vitals, no
  // uptime and no outage, and it can't page anybody — so latency, the 24 h strip,
  // the reliability line and the paging roster aren't "no data yet", they're
  // questions this box will never have an answer to, and rendering them empty is
  // the same lie as a green badge on an OLT that measures nothing. What it
  // carries and what feeds it IS the panel.
  if (isPassiveType(device.device_type)) {
    return <DistributionPanel device={device} />
  }
  if (tabs.length === 1) {
    return (
      <>
        {/* no tab row to anchor to — the buttons get their own row */}
        {(webUi || manageCreds) && (
          <div className="mb-2 flex justify-start gap-1.5">
            {webUi && <WebUiButton device={device} />}
            {manageCreds && <WebUiCredentialsButton device={device} />}
          </div>
        )}
        <div className="flex flex-col gap-2.5">
          <DevicePerfPanel device={device} />
          <ConnectionPanel device={device} />
          <AssignmentPanel device={device} />
        </div>
      </>
    )
  }

  const active = tabs.includes(tab) ? tab : tabs[0]
  return (
    <Tabs value={active} onValueChange={(v) => onTab(v as DeviceTab)}>
      {/* the line TabsList is w-full (its hairline spans the panel), so the
          button rides INSIDE it, right after the last tab — a flex sibling
          outside would always end up at the far edge */}
      <TabsList variant="line" className="mb-2">
        {/* The tab strip IS the identity axis, and has been since before there
            was one: Optical / Health / Ports are three MEASUREMENT PLANES, on
            three separate SNMP clocks, with three separate freshness stamps on
            the same device row — told apart by nothing but a word. The dot is
            the plane's own hue (lib/planes.ts), so the same three colours mean
            the same three things wherever they appear next. */}
        {tabs.map((t) => (
          <TabsTrigger key={t} value={t}>
            <PlaneDot plane={TAB_PLANE[t]} />
            {TAB_LABEL[t]}
          </TabsTrigger>
        ))}
        {(webUi || manageCreds) && (
          <span className="ml-1 flex items-center gap-1.5 pb-1">
            {webUi && <WebUiButton device={device} />}
            {manageCreds && <WebUiCredentialsButton device={device} />}
          </span>
        )}
      </TabsList>
      <TabsContent value="health">
        <div className="flex flex-col gap-2.5">
          <DevicePerfPanel device={device} />
          <ConnectionPanel device={device} />
          <AssignmentPanel device={device} />
        </div>
      </TabsContent>
      {tabs.includes("optical") && (
        <TabsContent value="optical">
          <OpticalPanel device={device} focusOnuId={focusOnuId} focusOnuMac={focusOnuMac} />
        </TabsContent>
      )}
      {tabs.includes("ports") && (
        <TabsContent value="ports"><PortsPanel device={device} /></TabsContent>
      )}
    </Tabs>
  )
}
