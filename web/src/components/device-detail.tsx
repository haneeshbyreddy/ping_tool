// The per-device drill-down panel (Health / Optical / Ports tabs) shared by the
// Network tree rows and the Map pin popover — one implementation, two surfaces.
import { useState, type MouseEvent, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { useAuth } from "@/hooks/use-auth"
import { analyticsApi, inventoryApi } from "@/lib/api"
import type { OrgDevice, SwitchPort } from "@/lib/types"
import { Meter } from "@/components/meter"
import { OpticalPanel } from "@/components/optical-panel"
import { bucketTrouble, HourStrip } from "@/components/sparkline"
import { StatusDot } from "@/components/status-badge"
import { ago, durationSince, fmtBytes, fmtDur, isFresh, isStale } from "@/lib/format"
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
        <Label className="text-[0.75rem] text-muted-foreground">Min Mbps</Label>
        <Input type="number" min="0" placeholder="none" value={min}
          onChange={(e) => setMin(e.target.value)} className="h-7 w-20 text-xs" />
      </div>
      <div className="flex flex-col gap-0.5">
        <Label className="text-[0.75rem] text-muted-foreground">Max Mbps</Label>
        <Input type="number" min="0" placeholder="none" value={max}
          onChange={(e) => setMax(e.target.value)} className="h-7 w-20 text-xs" />
      </div>
      <div className="flex flex-col gap-0.5">
        <Label className="text-[0.75rem] text-muted-foreground">Direction</Label>
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
    return <p className="px-1 py-2 text-xs text-muted-foreground">No SNMP ports discovered yet.</p>
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

  return (
    <div className="overflow-hidden rounded-lg border bg-muted/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-[0.75rem] text-muted-foreground">
        <span className="font-medium">{ports.length} ports · {watched} watched</span>
        {down > 0 && <span className="font-semibold text-destructive">{down} down</span>}
        {bwAlarms > 0 && <span className="font-semibold text-warning">{bwAlarms} bandwidth</span>}
        {portsStale
          ? <span className="font-semibold text-warning" title="The SNMP port walk on this device has stopped refreshing — these rows are the last good snapshot.">stale · {ago(lastWalk)}</span>
          : lastWalk && <span className="text-muted-foreground/70">as of {ago(lastWalk)}</span>}
        <span className="ml-auto hidden sm:inline">watch a port to alarm on it</span>
      </div>
      {sorted.map((p) => {
        const limits = [
          p.bw_threshold_mbps != null && `≥${p.bw_threshold_mbps}`,
          p.bw_max_mbps != null && `≤${p.bw_max_mbps}`,
        ].filter(Boolean).join(" ")
        return (
          <div key={p.id} className="border-b last:border-b-0">
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
              <span className="ml-auto hidden shrink-0 font-mono text-xs text-muted-foreground sm:inline">
                ↓{fmtRate(p.in_bps)}&ensp;↑{fmtRate(p.out_bps)}
              </span>
              {!!p.monitored && (
                <button
                  className={cn("hidden shrink-0 rounded px-1.5 py-0.5 font-mono text-[0.75rem] sm:inline",
                    limits ? "text-muted-foreground hover:bg-accent" : "text-muted-foreground/60 hover:bg-accent")}
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

  if (!device.assigned_node_id) return <span className="text-xs text-muted-foreground/70">not monitored</span>
  if (!device.state) return <span className="text-xs text-muted-foreground/70">no data</span>
  if (isStale(device.state_updated_at)) {
    return <span className="text-xs text-muted-foreground">stale · {ago(device.state_updated_at)}</span>
  }
  if (device.state === "DOWN" || device.state === "UNREACHABLE") {
    return <span className="text-xs font-semibold text-destructive">{device.state}</span>
  }
  const latency = device.latency_ms == null ? "—"
    : `${device.latency_ms < 10 ? device.latency_ms.toFixed(1) : Math.round(device.latency_ms)} ms`
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
  if (!hasVitals(device)) return null
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between text-[0.75rem] text-muted-foreground">
        <span className="font-medium">Device health</span>
        {device.health_updated_at && isStale(device.health_updated_at) && (
          <span className="text-muted-foreground/70">as of {ago(device.health_updated_at)}</span>
        )}
      </div>
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
  const isDown = device.state === "DOWN" || device.state === "UNREACHABLE"

  const fmtMs = (v: number) => v < 10 ? v.toFixed(1) : String(Math.round(v))

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
        <div className="mb-1 flex items-baseline justify-between text-[0.75rem] text-muted-foreground">
          <span className="font-medium">Last 24 h</span>
          <span className={cn(roughHours > 0 && "font-semibold text-warning")}>
            {trend.error ? "hourly history unavailable"
              : buckets.length === 0 ? "no history yet"
              : roughHours > 0 ? `${roughHours} rough hour${roughHours === 1 ? "" : "s"}` : "clean"}
          </span>
        </div>
        <HourStrip buckets={buckets} />
        <div className="mt-0.5 flex justify-between text-[0.6875rem] text-muted-foreground">
          <span>24 h ago</span><span>now</span>
        </div>
      </div>

      {/* can I trust it --------------------------------------------------------- */}
      {rel && (
        <p className="border-t pt-2 text-[0.75rem] text-muted-foreground">
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

export function RowTag({ tone, children, onClick, title }: {
  tone: "warning" | "success" | "muted" | "destructive"
  children: ReactNode
  onClick?: (e: MouseEvent) => void
  title?: string
}) {
  const cls = {
    warning: "bg-warning-soft text-warning",
    success: "bg-success-soft text-success",
    muted: "bg-muted text-muted-foreground",
    destructive: "bg-destructive-soft text-destructive",
  }[tone]
  return (
    <span title={title} onClick={onClick}
      className={cn("shrink-0 rounded px-1.5 py-px text-[0.6875rem] font-semibold tracking-wide uppercase",
        onClick && "cursor-pointer hover:brightness-125", cls)}>
      {children}
    </span>
  )
}

export type DeviceTab = "health" | "optical" | "ports"
export function isOpticalOlt(device: OrgDevice): boolean {
  return (device.device_type ?? "").toUpperCase() === "OLT" && device.snmp_enabled === 1
}
export function deviceTabs(device: OrgDevice): DeviceTab[] {
  const tabs: DeviceTab[] = ["health"]
  if (isOpticalOlt(device)) tabs.push("optical")
  if (device.snmp_enabled === 1) tabs.push("ports")
  return tabs
}
const TAB_LABEL: Record<DeviceTab, string> = { health: "Health", optical: "Optical", ports: "Ports" }

export function DeviceDetail({ device, tab, onTab, focusOnuId }: {
  device: OrgDevice; tab: DeviceTab; onTab: (t: DeviceTab) => void
  /** ONU row to reveal in the Optical tab — set when a map PON spoke is clicked */
  focusOnuId?: number | null
}) {
  const tabs = deviceTabs(device)
  if (tabs.length === 1) return <DevicePerfPanel device={device} />

  const active = tabs.includes(tab) ? tab : "health"
  return (
    <Tabs value={active} onValueChange={(v) => onTab(v as DeviceTab)}>
      <TabsList className="mb-2">
        {tabs.map((t) => <TabsTrigger key={t} value={t}>{TAB_LABEL[t]}</TabsTrigger>)}
      </TabsList>
      <TabsContent value="health"><DevicePerfPanel device={device} /></TabsContent>
      {tabs.includes("optical") && (
        <TabsContent value="optical"><OpticalPanel device={device} focusOnuId={focusOnuId} /></TabsContent>
      )}
      {tabs.includes("ports") && (
        <TabsContent value="ports"><PortsPanel device={device} /></TabsContent>
      )}
    </Tabs>
  )
}
