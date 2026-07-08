import { Fragment, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react"
import { useLocation } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronRight, MoreVertical, Pencil, Plus, Radio, ScanSearch, Trash2, Waypoints, Wrench, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { analyticsApi, inventoryApi, nodesApi, ApiError } from "@/lib/api"
import { DEVICE_TYPES, type OrgDevice, type SwitchPort } from "@/lib/types"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import { Meter } from "@/components/meter"
import { NeedsOrg } from "@/components/needs-org"
import { OpticalPanel } from "@/components/optical-panel"
import { RegionSelect } from "@/components/region-select"
import { ProbesPanel } from "@/components/probes-panel"
import { SnmpWalkDialog } from "@/components/snmp-walk-dialog"
import { bucketTrouble, HourStrip } from "@/components/sparkline"
import { StatusDot } from "@/components/status-badge"
import { ago, deviceTone, durationSince, fmtBytes, fmtDur, isStale } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import { Switch } from "@/components/ui/switch"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

function treeOrder(
  devices: OrgDevice[], collapsed: Set<number>,
): Array<OrgDevice & { depth: number; descendantCount: number }> {
  const children = new Map<number | null, OrgDevice[]>()
  for (const d of devices) {
    const key = d.parent_device_id
    if (!children.has(key)) children.set(key, [])
    children.get(key)!.push(d)
  }
  const descendantCount = (id: number): number =>
    (children.get(id) ?? []).reduce((sum, k) => sum + 1 + descendantCount(k.id), 0)
  const ids = new Set(devices.map((d) => d.id))
  const out: Array<OrgDevice & { depth: number; descendantCount: number }> = []
  const emit = (d: OrgDevice, depth: number) => {
    out.push({ ...d, depth, descendantCount: descendantCount(d.id) })

    if (!collapsed.has(d.id)) for (const k of children.get(d.id) ?? []) emit(k, depth + 1)
  }
  for (const d of children.get(null) ?? []) emit(d, 0)

  for (const d of devices) {
    if (d.parent_device_id != null && !ids.has(d.parent_device_id)) emit(d, 0)
  }
  return out
}

const GPON_VENDORS = ["huawei", "dbc"] as const

interface DeviceFormState {
  name: string
  ip_address: string
  device_type: string
  region: string
  parent_device_id: string
  assigned_node_id: string
  snmp_enabled: boolean
  snmp_community: string
  snmp_port: string
  gpon_vendor: string
}

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", parent_device_id: "",
  assigned_node_id: "", snmp_enabled: false, snmp_community: "", snmp_port: "161",
  gpon_vendor: "",
}

function DeviceForm({
  org, editing, devices, nodeIds, onDone,
}: {
  org: string
  editing: OrgDevice | null
  devices: OrgDevice[]
  nodeIds: string[]
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<DeviceFormState>(() => editing ? {
    name: editing.name, ip_address: editing.ip_address, device_type: editing.device_type ?? "",
    region: editing.region ?? "", parent_device_id: editing.parent_device_id ? String(editing.parent_device_id) : "",
    assigned_node_id: editing.assigned_node_id ?? "",
    snmp_enabled: !!editing.snmp_enabled, snmp_community: editing.snmp_community ?? "",
    snmp_port: String(editing.snmp_port || 161),
    gpon_vendor: editing.gpon_vendor ?? "",
  } : EMPTY_FORM)
  const [error, setError] = useState("")

  const cardRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" })
  }, [editing])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["inventory"] })
    // a "New region…" typed here reaches the dropdown via the in-use union
    queryClient.invalidateQueries({ queryKey: ["regions"] })
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        org_id: org,
        name: form.name.trim(),
        ip_address: form.ip_address.trim(),
        device_type: form.device_type || null,
        region: form.region.trim() || null,
        parent_device_id: form.parent_device_id ? Number(form.parent_device_id) : null,
        assigned_node_id: form.assigned_node_id || null,

        gpon_vendor: form.device_type === "OLT" ? (form.gpon_vendor || null) : null,
      }
      if (editing) {
        await inventoryApi.update(editing.id, payload)
        await inventoryApi.setSnmp(editing.id, {
          snmp_enabled: form.snmp_enabled, snmp_community: form.snmp_community.trim() || null,
          snmp_port: form.snmp_port,
        })
      } else {
        await inventoryApi.create(payload)
      }
    },
    onSuccess: () => { invalidate(); onDone() },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Save failed"),
  })

  return (
    <Card ref={cardRef} className="border-primary/30">
      <CardContent className="flex flex-col gap-3 px-4">
        <p className="text-sm font-semibold">{editing ? `Edit — ${editing.name}` : "Add device"}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label>Name</Label>
            <Input placeholder="e.g. ap-ridge-09" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>IP address</Label>
            <Input placeholder="10.4.1.9" className="font-mono" value={form.ip_address}
              onChange={(e) => setForm({ ...form, ip_address: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Type</Label>
            <Select value={form.device_type} onValueChange={(v) => setForm({ ...form, device_type: v })}>
              <SelectTrigger className="w-full"><SelectValue placeholder="(type)" /></SelectTrigger>
              <SelectContent>
                {DEVICE_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Region</Label>
            <RegionSelect org={org} value={form.region} className="w-full"
              onChange={(v) => setForm({ ...form, region: v })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Parent</Label>
            <Select value={form.parent_device_id || "none"}
              onValueChange={(v) => setForm({ ...form, parent_device_id: v === "none" ? "" : v })}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">— none (root) —</SelectItem>
                {devices.filter((d) => d.id !== editing?.id).map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>{d.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Assigned probe</Label>
            <Select value={form.assigned_node_id || "any"}
              onValueChange={(v) => setForm({ ...form, assigned_node_id: v === "any" ? "" : v })}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="any">— unassigned (not monitored) —</SelectItem>
                {nodeIds.map((id) => <SelectItem key={id} value={id}>{id}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-5">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={form.snmp_enabled}
              onCheckedChange={(v) => setForm({ ...form, snmp_enabled: !!v })} />
            SNMP enabled
          </label>
          {form.snmp_enabled && (
            <>
              <Input placeholder="community" className="w-32" value={form.snmp_community}
                onChange={(e) => setForm({ ...form, snmp_community: e.target.value })} />
              <Input placeholder="port" className="w-20" value={form.snmp_port}
                onChange={(e) => setForm({ ...form, snmp_port: e.target.value })} />
            </>
          )}
          {/* GPON vendor is per-OLT — which MIB the edge walks for ONU optics. Only an
              OLT has ONUs, so surface it only for that type (auto = the fleet default). */}
          {form.device_type === "OLT" && (
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-muted-foreground">GPON vendor</Label>
              <Select value={form.gpon_vendor || "auto"}
                onValueChange={(v) => setForm({ ...form, gpon_vendor: v === "auto" ? "" : v })}>
                <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">auto (default)</SelectItem>
                  {GPON_VENDORS.map((v) => <SelectItem key={v} value={v}>{v}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
          <Button size="sm" disabled={save.isPending || !form.name || !form.ip_address}
            onClick={() => save.mutate()}>
            {editing ? "Save" : "Add"}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

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

function PortsPanel({ device }: { device: OrgDevice }) {
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

  const rank = (p: SwitchPort) => (portAlarmed(p) ? 0 : p.monitored ? 1 : 2)
  const sorted = [...ports].sort((a, b) => rank(a) - rank(b) || a.if_index - b.if_index)
  const watched = ports.filter((p) => p.monitored).length
  const down = ports.filter((p) => p.monitored && p.alarm === 1).length
  const bwAlarms = ports.filter((p) => p.monitored && (p.bw_alarm === 1 || p.bw_high_alarm === 1)).length

  return (
    <div className="overflow-hidden rounded-lg border bg-muted/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-[0.75rem] text-muted-foreground">
        <span className="font-medium">{ports.length} ports · {watched} watched</span>
        {down > 0 && <span className="font-semibold text-destructive">{down} down</span>}
        {bwAlarms > 0 && <span className="font-semibold text-warning">{bwAlarms} bandwidth</span>}
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

function DeviceMetrics({ device }: { device: OrgDevice }) {

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
const VITAL_CPU_WARN = 80, VITAL_CPU_CRIT = 95
const VITAL_MEM_WARN = 80, VITAL_MEM_CRIT = 95
const VITAL_TEMP_WARN = 70, VITAL_TEMP_CRIT = 85

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

function DevicePerfPanel({ device }: { device: OrgDevice }) {
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

function RowTag({ tone, children, onClick, title }: {
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

type DeviceTab = "health" | "optical" | "ports"
function isOpticalOlt(device: OrgDevice): boolean {
  return (device.device_type ?? "").toUpperCase() === "OLT" && device.snmp_enabled === 1
}
function deviceTabs(device: OrgDevice): DeviceTab[] {
  const tabs: DeviceTab[] = ["health"]
  if (isOpticalOlt(device)) tabs.push("optical")
  if (device.snmp_enabled === 1) tabs.push("ports")
  return tabs
}
const TAB_LABEL: Record<DeviceTab, string> = { health: "Health", optical: "Optical", ports: "Ports" }

function DeviceDetail({ device, tab, onTab }: {
  device: OrgDevice; tab: DeviceTab; onTab: (t: DeviceTab) => void
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
        <TabsContent value="optical"><OpticalPanel device={device} /></TabsContent>
      )}
      {tabs.includes("ports") && (
        <TabsContent value="ports"><PortsPanel device={device} /></TabsContent>
      )}
    </Tabs>
  )
}

function DeviceRow({
  device, canWrite, onEdit, collapsed, onToggleCollapse, focus,
}: {
  device: OrgDevice & { depth: number; descendantCount: number }
  canWrite: boolean
  onEdit: (d: OrgDevice) => void
  collapsed: boolean
  onToggleCollapse: () => void
  focus?: boolean
}) {
  const queryClient = useQueryClient()
  const [detailOpen, setDetailOpen] = useState(false)
  const [walkOpen, setWalkOpen] = useState(false)
  const confirmDelete = useConfirm()
  const rowRef = useRef<HTMLDivElement>(null)

  // Deep-link landing (Home row / command palette): open the panel and scroll here.
  useEffect(() => {
    if (focus) {
      setDetailOpen(true)
      rowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
    }
  }, [focus])

  const hasOptics = isOpticalOlt(device)
  const hasPorts = device.snmp_enabled === 1
  const [detailTab, setDetailTab] = useState<DeviceTab>("health")
  const openTab = (t: DeviceTab) => { setDetailTab(t); setDetailOpen(true) }
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["inventory"] })
  useNow()

  const remove = useMutation({
    mutationFn: () => inventoryApi.remove(device.id),
    onSuccess: (res) => {
      if (res.ok) invalidate()
      else toast.error(res.reason || "Device has children — remove them first")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })
  const toggleMaintenance = useMutation({
    mutationFn: () => inventoryApi.setMaintenance(device.id, !device.maintenance),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to update"),
  })

  const unassigned = !device.assigned_node_id

  return (
    <div ref={rowRef} className="border-b last:border-b-0">
      <div
        className={cn("group flex h-11 cursor-pointer items-center gap-2.5 px-4 hover:bg-accent/40",
          detailOpen && "bg-accent/40")}
        onClick={() => setDetailOpen(!detailOpen)}
        title={detailOpen ? undefined : "Click for details"}
      >
        {Array.from({ length: device.depth }).map((_, i) => (
          <span key={i} aria-hidden className="w-3 shrink-0 self-stretch border-l sm:w-4" />
        ))}
        {device.child_count > 0 ? (
          <Button variant="ghost" size="icon" className="size-5 shrink-0"
            onClick={(e) => { e.stopPropagation(); onToggleCollapse() }}>
            <ChevronRight className={cn("size-3.5 transition-transform", !collapsed && "rotate-90")} />
          </Button>
        ) : (
          <span className="size-5 shrink-0" />
        )}
        <span className="inline-flex shrink-0" title={unassigned ? "no probe assigned — not monitored"
          : device.state && isStale(device.state_updated_at)
          ? `stale — no report since ${ago(device.state_updated_at)}` : undefined}>
          <StatusDot tone={unassigned ? "muted" : deviceTone(device.state, device.state_updated_at)} />
        </span>
        <span className={cn("min-w-0 truncate font-mono text-xs font-medium",
          unassigned && "text-muted-foreground")}>{device.name}</span>
        {device.device_type && (
          <span className="hidden shrink-0 text-xs text-muted-foreground/70 lg:inline">{device.device_type}</span>
        )}
        {unassigned && <RowTag tone="muted" title="Assign a probe to start monitoring">unassigned</RowTag>}
        {!!device.maintenance && <RowTag tone="warning">maint</RowTag>}
        {device.backup_parents.length > 0 && <RowTag tone="success">backup</RowTag>}
        {/* Monitored-port trouble surfaces on the switch's own row — clicking a chip
            opens the ports panel straight to the story instead of making the operator
            hunt for it behind the radio icon. */}
        {device.ports_down > 0 && (
          <RowTag tone="destructive" title="A watched port is down — click for ports"
            onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
            {device.ports_down === 1 ? "port down" : `${device.ports_down} ports down`}
          </RowTag>
        )}
        {device.ports_bw_low > 0 && (
          <RowTag tone="warning" title="A watched port is below its bandwidth floor — click for ports"
            onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
            low bw
          </RowTag>
        )}
        {device.ports_bw_high > 0 && (
          <RowTag tone="warning" title="A watched port is above its bandwidth ceiling — click for ports"
            onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
            high bw
          </RowTag>
        )}
        {/* OLT optical trouble surfaces on the OLT's own row — a click deep-links to the
            Optical tab, same pattern as the port chips. Gated on hasOptics so a stale
            badge from before SNMP was turned off can't show a chip that links nowhere. */}
        {hasOptics && !!device.onus_crit && device.onus_crit > 0 && (
          <RowTag tone="destructive" title="ONUs below the critical Rx-power floor — click for optics"
            onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
            {device.onus_crit} ONU{device.onus_crit === 1 ? "" : "s"} crit
          </RowTag>
        )}
        {hasOptics && !device.onus_crit && !!device.onus_warn && device.onus_warn > 0 && (
          <RowTag tone="warning" title="ONUs with a weak Rx-power warning — click for optics"
            onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
            {device.onus_warn} ONU{device.onus_warn === 1 ? "" : "s"} weak
          </RowTag>
        )}
        {/* Device vitals only chip when CRITICAL — a hot or pegged box is a fire to
            walk toward; warn-level tints stay inside the expanded Health panel. */}
        {(device.health_temp_c ?? 0) >= VITAL_TEMP_CRIT && (
          <RowTag tone="destructive" title="Device temperature critical — click for health"
            onClick={(e) => { e.stopPropagation(); openTab("health") }}>
            {Math.round(device.health_temp_c!)}°C
          </RowTag>
        )}
        {(device.health_cpu_pct ?? 0) >= VITAL_CPU_CRIT && (
          <RowTag tone="destructive" title="Device CPU pegged — click for health"
            onClick={(e) => { e.stopPropagation(); openTab("health") }}>
            cpu {Math.round(device.health_cpu_pct!)}%
          </RowTag>
        )}
        {collapsed && device.descendantCount > 0 && <RowTag tone="muted">+{device.descendantCount}</RowTag>}
        <div className="ml-auto flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <DeviceMetrics device={device} />
          <span className="hidden font-mono text-xs text-muted-foreground md:inline">{device.ip_address}</span>
          {/* Passive capability indicators — no longer a button. They just say what this
              device supports (optical / SNMP ports); the trouble tone matches whatever the
              row's chips already say. Click the row to open the tabbed panel. */}
          {(hasOptics || hasPorts) && (
            <div className="flex items-center gap-1.5 text-muted-foreground/60">
              {hasOptics && (
                <span title={device.onus_crit ? `Optical — ${device.onus_crit} ONU(s) critical`
                  : device.onus_warn ? `Optical — ${device.onus_warn} ONU(s) weak` : "Optical (GPON) monitored"}>
                  <Waypoints className={cn("size-3.5",
                    device.onus_crit ? "text-destructive" : device.onus_warn ? "text-warning" : "")} />
                </span>
              )}
              {hasPorts && (
                <span title={device.ports_down ? `SNMP — ${device.ports_down} port(s) down` : "SNMP ports monitored"}>
                  <Radio className={cn("size-3.5",
                    device.ports_down ? "text-destructive"
                      : (device.ports_bw_low || device.ports_bw_high) ? "text-warning" : "")} />
                </span>
              )}
            </div>
          )}
          {canWrite && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon"
                  className="size-6 text-muted-foreground opacity-60 group-hover:opacity-100 data-[state=open]:opacity-100">
                  <MoreVertical className="size-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => onEdit(device)}>
                  <Pencil /> Edit
                </DropdownMenuItem>
                {device.snmp_enabled === 1 && (
                  <DropdownMenuItem onClick={() => setWalkOpen(true)}>
                    <ScanSearch /> SNMP walk
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onClick={() => toggleMaintenance.mutate()}>
                  <Wrench /> {device.maintenance ? "End maintenance" : "Start maintenance"}
                </DropdownMenuItem>
                <DropdownMenuItem variant="destructive" onClick={() => confirmDelete.ask()}>
                  <Trash2 /> Delete
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <ConfirmDialog {...confirmDelete.props}
            title={`Delete ${device.name}?`}
            description="The device, its state, and its outage history are removed. This cannot be undone."
            onConfirm={() => remove.mutate()} />
          {walkOpen && (
            <SnmpWalkDialog device={device} open={walkOpen} onOpenChange={setWalkOpen} />
          )}
        </div>
      </div>
      {detailOpen && (
        <div className="px-3 pt-1 pb-3">
          <DeviceDetail device={device} tab={detailTab} onTab={setDetailTab} />
        </div>
      )}
    </div>
  )
}

const COLLAPSE_KEY = "wisp:topology:collapsed"

function loadCollapsed(org: string | null): Set<number> {
  if (!org) return new Set()
  try {
    const raw = localStorage.getItem(`${COLLAPSE_KEY}:${org}`)
    const ids = raw ? (JSON.parse(raw) as unknown) : []
    return new Set(Array.isArray(ids) ? (ids as number[]) : [])
  } catch {
    return new Set()
  }
}

function saveCollapsed(org: string | null, set: Set<number>): void {
  if (!org) return
  try {
    localStorage.setItem(`${COLLAPSE_KEY}:${org}`, JSON.stringify([...set]))
  } catch {
    /* private mode / quota — keep the in-memory state, just don't persist */
  }
}

export function TopologyPage() {
  const { scopeOrg, canWrite } = useAuth()
  const location = useLocation()
  const navState = location.state as { deviceId?: number; probeId?: string } | null
  const focusId = navState?.deviceId
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<OrgDevice | null>(null)
  const [collapsed, setCollapsed] = useState<Set<number>>(() => loadCollapsed(scopeOrg))
  const [probeFilter, setProbeFilter] = useState<string | null>(navState?.probeId ?? null)

  useEffect(() => { setCollapsed(loadCollapsed(scopeOrg)) }, [scopeOrg])
  // arriving from a stale-probe card while already mounted
  useEffect(() => { if (navState?.probeId) setProbeFilter(navState.probeId) }, [navState?.probeId])
  const toggleCollapse = (id: number) => setCollapsed((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    saveCollapsed(scopeOrg, next)
    return next
  })

  const { data, isLoading } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })

  // A deep-linked device may sit under collapsed ancestors — open the path to it
  // (in memory only; a landing shouldn't rewrite the user's saved collapse prefs).
  const devicesData = data?.devices
  useEffect(() => {
    if (focusId == null || !devicesData) return
    const byId = new Map(devicesData.map((d) => [d.id, d]))
    const ancestors: number[] = []
    let cur = byId.get(focusId)?.parent_device_id
    while (cur != null && byId.has(cur) && !ancestors.includes(cur)) {
      ancestors.push(cur)
      cur = byId.get(cur)?.parent_device_id
    }
    if (ancestors.length) {
      setCollapsed((prev) => {
        const next = new Set(prev)
        for (const id of ancestors) next.delete(id)
        return next
      })
    }
  }, [focusId, devicesData])

  if (!scopeOrg) return <NeedsOrg />

  const allDevices = data?.devices ?? []
  const devices = probeFilter
    ? allDevices.filter((d) => d.assigned_node_id === probeFilter)
    : allDevices
  const ordered = treeOrder(devices, collapsed)
  const activeNodes = (nodes.data?.nodes ?? []).filter((n) => !n.revoked_at)
  const nodeIds = activeNodes.map((n) => n.node_id)
  const deviceCounts = new Map<string, number>()
  for (const d of allDevices) {
    if (d.assigned_node_id) {
      deviceCounts.set(d.assigned_node_id, (deviceCounts.get(d.assigned_node_id) ?? 0) + 1)
    }
  }

  const fresh = devices.filter((d) => d.assigned_node_id && d.state && !isStale(d.state_updated_at))
  const down = fresh.filter((d) => d.state === "DOWN" || d.state === "UNREACHABLE").length
  const degraded = fresh.filter((d) => d.state === "DEGRADED").length

  const openEdit = (d: OrgDevice) => { setEditing(d); setFormOpen(true) }
  const closeForm = () => { setFormOpen(false); setEditing(null) }

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6">
      <ProbesPanel org={scopeOrg} canWrite={canWrite} deviceCounts={deviceCounts}
        probeFilter={probeFilter} onProbeFilter={setProbeFilter} />

      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-3">
            <h2 className="text-sm font-semibold">
              Devices
              {devices.length > 0 && <span className="ml-2 font-normal text-muted-foreground">{devices.length}</span>}
            </h2>
            {probeFilter && (
              <button
                className="flex items-center gap-1.5 self-center rounded-full border bg-card px-2.5 py-0.5 text-[0.75rem] font-medium text-muted-foreground transition-colors hover:text-foreground"
                title="Showing only this probe's devices — click to clear"
                onClick={() => setProbeFilter(null)}>
                {probeFilter}
                <X className="size-3" />
              </button>
            )}
            {(down > 0 || degraded > 0) && (
              <p className="text-xs">
                {down > 0 && <span className="font-semibold text-destructive">{down} down</span>}
                {down > 0 && degraded > 0 && <span className="text-muted-foreground"> · </span>}
                {degraded > 0 && <span className="font-semibold text-warning">{degraded} degraded</span>}
              </p>
            )}
          </div>
          {canWrite && !formOpen && (
            <Button variant="ghost" size="sm" className="text-muted-foreground"
              onClick={() => { setEditing(null); setFormOpen(true) }}>
              <Plus className="size-3.5" /> Add device
            </Button>
          )}
        </div>

        {/* Add uses the top form (no row to attach to); edit renders inline at its row. */}
        {formOpen && !editing && (
          <DeviceForm org={scopeOrg} editing={null} devices={devices} nodeIds={nodeIds} onDone={closeForm} />
        )}

        {isLoading && <Skeleton className="h-40 w-full" />}
        {!isLoading && devices.length === 0 && (
          <p className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
            {probeFilter ? `No devices assigned to ${probeFilter}.` : "No devices yet — add one above."}
          </p>
        )}
        {devices.length > 0 && (
          <Card className="gap-0 overflow-hidden py-0">
            {ordered.map((d) => (
              <Fragment key={d.id}>
                <DeviceRow device={d} canWrite={canWrite} onEdit={openEdit}
                  collapsed={collapsed.has(d.id)} onToggleCollapse={() => toggleCollapse(d.id)}
                  focus={d.id === focusId} />
                {formOpen && editing?.id === d.id && (
                  <div className="border-t bg-muted/30 p-3">
                    <DeviceForm org={scopeOrg} editing={editing} devices={devices} nodeIds={nodeIds} onDone={closeForm} />
                  </div>
                )}
              </Fragment>
            ))}
          </Card>
        )}
      </section>
    </div>
  )
}
