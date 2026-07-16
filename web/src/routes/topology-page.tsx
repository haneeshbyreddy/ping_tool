import { Fragment, useEffect, useRef, useState } from "react"
import { useLocation } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronRight, LayoutGrid, List, MoreVertical, Pencil, Plus, Radio, ScanSearch, Trash2, Waypoints, Wrench, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { billingApi, gponApi, inventoryApi, nodesApi, ApiError } from "@/lib/api"
import { DEVICE_TYPES, PASSIVE_DEVICE_TYPES, isPassiveType, type OrgDevice } from "@/lib/types"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import {
  DeviceDetail, DeviceMetrics, RowTag, isOpticalOlt,
  VITAL_CPU_CRIT, VITAL_TEMP_CRIT, type DeviceTab,
} from "@/components/device-detail"
import { NeedsOrg } from "@/components/needs-org"
import { RegionSelect } from "@/components/region-select"
import { ProbesPanel } from "@/components/probes-panel"
import { SnmpWalkDialog } from "@/components/snmp-walk-dialog"
import { UpgradeNotice } from "@/components/upgrade-notice"
import { WebUiLiveIcon } from "@/components/web-proxy"
import { StatusDot } from "@/components/status-badge"
import { ago, deviceTone, isFresh, isStale } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

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
  pon_port: string
}

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", parent_device_id: "",
  assigned_node_id: "", snmp_enabled: false, snmp_community: "", snmp_port: "161",
  gpon_vendor: "", pon_port: "",
}

function DeviceForm({
  org, editing, devices, nodeIds, onDone, initialType,
}: {
  org: string
  editing: OrgDevice | null
  devices: OrgDevice[]
  nodeIds: string[]
  onDone: () => void
  initialType?: string
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<DeviceFormState>(() => editing ? {
    name: editing.name, ip_address: editing.ip_address, device_type: editing.device_type ?? "",
    region: editing.region ?? "", parent_device_id: editing.parent_device_id ? String(editing.parent_device_id) : "",
    assigned_node_id: editing.assigned_node_id ?? "",
    snmp_enabled: !!editing.snmp_enabled, snmp_community: editing.snmp_community ?? "",
    snmp_port: String(editing.snmp_port || 161),
    gpon_vendor: editing.gpon_vendor ?? "",
    pon_port: editing.pon_port ?? "",
  } : { ...EMPTY_FORM, device_type: initialType ?? "" })
  const [error, setError] = useState("")

  // Central-served GPON profiles join the built-ins in the override dropdown.
  const gponProfiles = useQuery({
    queryKey: ["gpon-profiles", org],
    queryFn: () => gponApi.profiles(org),
    enabled: form.device_type === "OLT",
  })
  const gponVendors = [...new Set([
    ...GPON_VENDORS,
    ...(gponProfiles.data?.profiles.filter((p) => p.enabled).map((p) => p.name) ?? []),
  ])].sort()

  const cardRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" })
  }, [editing])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["inventory"] })
    // a "New region…" typed here reaches the dropdown via the in-use union
    queryClient.invalidateQueries({ queryKey: ["regions"] })
  }

  const passive = isPassiveType(form.device_type)
  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        org_id: org,
        name: form.name.trim(),
        // passive plant has no address; the server rejects one anyway
        ip_address: passive ? "" : form.ip_address.trim(),
        device_type: form.device_type || null,
        region: form.region.trim() || null,
        parent_device_id: form.parent_device_id ? Number(form.parent_device_id) : null,
        assigned_node_id: passive ? null : (form.assigned_node_id || null),

        gpon_vendor: form.device_type === "OLT" ? (form.gpon_vendor || null) : null,
        pon_port: passive ? (form.pon_port.trim() || null) : null,
      }
      if (editing) {
        await inventoryApi.update(editing.id, payload)
        if (!passive) {
          await inventoryApi.setSnmp(editing.id, {
            snmp_enabled: form.snmp_enabled, snmp_community: form.snmp_community.trim() || null,
            snmp_port: form.snmp_port,
          })
        }
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
        <p className="text-sm font-semibold">{editing ? `Edit: ${editing.name}` : "Add device"}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label>Name</Label>
            <Input placeholder="e.g. ap-ridge-09" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>{passive ? "PON port (optional)" : "IP address"}</Label>
            {passive ? (
              <Input placeholder="0/6" className="font-mono" value={form.pon_port}
                onChange={(e) => setForm({ ...form, pon_port: e.target.value })} />
            ) : (
              <Input placeholder="10.4.1.9" className="font-mono" value={form.ip_address}
                onChange={(e) => setForm({ ...form, ip_address: e.target.value })} />
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Type</Label>
            <Select value={form.device_type} onValueChange={(v) => setForm({ ...form, device_type: v })}>
              <SelectTrigger className="w-full"><SelectValue placeholder="(type)" /></SelectTrigger>
              <SelectContent>
                {DEVICE_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
                {PASSIVE_DEVICE_TYPES.map((t) => (
                  <SelectItem key={t} value={t}>{t} (passive)</SelectItem>
                ))}
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
                <SelectItem value="none">None (root)</SelectItem>
                {devices.filter((d) => d.id !== editing?.id).map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>{d.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {!passive && (
            <div className="flex flex-col gap-1.5">
              <Label>Assigned probe</Label>
              <Select value={form.assigned_node_id || "any"}
                onValueChange={(v) => setForm({ ...form, assigned_node_id: v === "any" ? "" : v })}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="any">Unassigned (not monitored)</SelectItem>
                  {nodeIds.map((id) => <SelectItem key={id} value={id}>{id}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>

        {passive && (
          <p className="text-xs text-muted-foreground">
            Passive plant: lives on the map and in the tree, never probed.
            Hang it under the OLT (or another splitter) that feeds it.
          </p>
        )}
        <div className={cn("flex flex-wrap items-center gap-5", passive && "hidden")}>
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
          {/* GPON vendor is per-OLT — which MIB the edge walks for ONU optics. The edge
              auto-detects it from the box's sysObjectID; picking a vendor here is an
              OVERRIDE for a box whose sysObjectID is missing or wrong. */}
          {form.device_type === "OLT" && (
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-muted-foreground">GPON vendor</Label>
              <Select value={form.gpon_vendor || "auto"}
                onValueChange={(v) => setForm({ ...form, gpon_vendor: v === "auto" ? "" : v })}>
                <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">auto-detect (default)</SelectItem>
                  {gponVendors.map((v) => <SelectItem key={v} value={v}>{v} (override)</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
          <Button size="sm" disabled={save.isPending || !form.name || (!passive && !form.ip_address)}
            onClick={() => save.mutate()}>
            {editing ? "Save" : "Add"}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// Drill-in state shared by the tree row and the grid card: which panel tab is
// open, whether it's expanded, and the deep-link focus effect (Home row /
// command palette lands here — open the panel and scroll it into view).
function useDrillIn(focus?: boolean) {
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailTab, setDetailTab] = useState<DeviceTab>("health")
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focus) {
      setDetailOpen(true)
      ref.current?.scrollIntoView({ behavior: "smooth", block: "center" })
    }
  }, [focus])
  const openTab = (t: DeviceTab) => { setDetailTab(t); setDetailOpen(true) }
  return { detailOpen, setDetailOpen, detailTab, setDetailTab, openTab, ref }
}

// The trouble/status chips shared by the tree row and the grid card. Each chip
// deep-links to the panel tab that tells its story (optics / ports / health),
// so the operator never hunts for it. Gated on hasOptics so a stale badge from
// before SNMP was turned off can't chip a link that goes nowhere.
function DeviceChips({ device, hasOptics, collapsed, openTab }: {
  device: OrgDevice & { descendantCount?: number }
  hasOptics: boolean
  collapsed?: boolean
  openTab: (t: DeviceTab) => void
}) {
  const passive = isPassiveType(device.device_type)
  // a splitter with no probe is by design, not a config gap
  const unassigned = !device.assigned_node_id && !passive
  // Suppress the weak/crit/fiber/dup chips whenever the row itself isn't live —
  // the OLT is down (its ICMP outage owns the row), or its probe has gone silent
  // (the row is already graying to muted). Either way the optics are a stale
  // snapshot: the map pin ring does the same, and they'd count ONUs that aren't up.
  const isDown = device.state === "DOWN" || device.state === "UNREACHABLE"
  const opticsChips = hasOptics && !isDown && !isStale(device.state_updated_at)
  return (
    <>
      {unassigned && <RowTag tone="muted" title="Assign a probe to start monitoring">unassigned</RowTag>}
      {passive && (
        <RowTag tone="muted" title="Passive plant: on the map, never probed">
          passive{device.pon_port ? ` · PON ${device.pon_port}` : ""}
        </RowTag>
      )}
      {!!device.maintenance && <RowTag tone="muted">maint</RowTag>}
      {device.backup_parents.length > 0 && <RowTag tone="success">backup</RowTag>}
      {device.ports_down > 0 && (
        <RowTag tone="destructive" title="A watched port is down. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          {device.ports_down === 1 ? "port down" : `${device.ports_down} ports down`}
        </RowTag>
      )}
      {device.ports_bw_low > 0 && (
        <RowTag tone="warning" title="A watched port is below its bandwidth floor. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          low bw
        </RowTag>
      )}
      {device.ports_bw_high > 0 && (
        <RowTag tone="warning" title="A watched port is above its bandwidth ceiling. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          high bw
        </RowTag>
      )}
      {/* Suspected fiber cut / live duplicate MAC — the same verdicts the Optical
          tab and the Home KPI strip carry, surfaced on the OLT's own row so a
          troubled box flags in the list without the tech drilling in. */}
      {opticsChips && device.fiber_cuts > 0 && (
        <RowTag tone="destructive" title="Suspected fiber cut (PON mass-drop). Click for optics"
          onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
          {device.fiber_cuts === 1 ? "fiber cut" : `${device.fiber_cuts} fiber cuts`}
        </RowTag>
      )}
      {opticsChips && device.dup_macs > 0 && (
        <RowTag tone="destructive" title="Duplicate ONU MAC: cloned CPE or bridging loop. Click for optics"
          onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
          {device.dup_macs === 1 ? "dup MAC" : `${device.dup_macs} dup MACs`}
        </RowTag>
      )}
      {opticsChips && !!device.onus_crit && device.onus_crit > 0 && (
        <RowTag tone="destructive" title="ONUs below the critical Rx-power floor. Click for optics"
          onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
          {device.onus_crit} ONU{device.onus_crit === 1 ? "" : "s"} crit
        </RowTag>
      )}
      {opticsChips && !device.onus_crit && !!device.onus_warn && device.onus_warn > 0 && (
        <RowTag tone="warning" title="ONUs with a weak Rx-power warning. Click for optics"
          onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
          {device.onus_warn} ONU{device.onus_warn === 1 ? "" : "s"} weak
        </RowTag>
      )}
      {/* Device vitals only chip when CRITICAL — a hot or pegged box is a fire to
          walk toward; warn-level tints stay inside the expanded Health panel. */}
      {(device.health_temp_c ?? 0) >= VITAL_TEMP_CRIT && (
        <RowTag tone="destructive" title="Device temperature critical. Click for health"
          onClick={(e) => { e.stopPropagation(); openTab("health") }}>
          {Math.round(device.health_temp_c!)}°C
        </RowTag>
      )}
      {(device.health_cpu_pct ?? 0) >= VITAL_CPU_CRIT && (
        <RowTag tone="destructive" title="Device CPU pegged. Click for health"
          onClick={(e) => { e.stopPropagation(); openTab("health") }}>
          cpu {Math.round(device.health_cpu_pct!)}%
        </RowTag>
      )}
      {collapsed && (device.descendantCount ?? 0) > 0 && <RowTag tone="muted">+{device.descendantCount}</RowTag>}
    </>
  )
}

// Capability indicators (optical / SNMP ports): they just say what this device
// supports, tinted by the same freshness rule as the Overview — red on alarm,
// amber on warn, green when a fresh reading is landing, dim when configured but
// silent (no data yet / gone stale). Trouble beats working.
function DeviceCapabilityIcons({ device, hasOptics, hasPorts }: {
  device: OrgDevice; hasOptics: boolean; hasPorts: boolean
}) {
  if (!hasOptics && !hasPorts) return null
  const opticsFresh = isFresh(device.optics_updated_at)
  const portsFresh = isFresh(device.ports_updated_at)
  return (
    <div className="flex items-center gap-1.5">
      {hasOptics && (
        <span title={device.onus_crit ? `Optical: ${device.onus_crit} ONU(s) critical`
          : device.onus_warn ? `Optical: ${device.onus_warn} ONU(s) weak`
          : opticsFresh ? "Optical (GPON): reporting" : "Optical (GPON): no reading yet"}>
          <Waypoints className={cn("size-3.5",
            device.onus_crit ? "text-destructive"
              : device.onus_warn ? "text-warning"
              : opticsFresh ? "text-success" : "text-faint-foreground")} />
        </span>
      )}
      {hasPorts && (
        <span title={device.ports_down ? `SNMP: ${device.ports_down} port(s) down`
          : (device.ports_bw_low || device.ports_bw_high) ? "SNMP: bandwidth alarm"
          : portsFresh ? "SNMP ports: reporting" : "SNMP ports: no reading yet"}>
          <Radio className={cn("size-3.5",
            device.ports_down ? "text-destructive"
              : (device.ports_bw_low || device.ports_bw_high) ? "text-warning"
              : portsFresh ? "text-success" : "text-faint-foreground")} />
        </span>
      )}
    </div>
  )
}

// The per-device actions menu (edit / SNMP walk / maintenance / delete) plus its
// dialogs — shared by row and card so the mutations live in one place.
function DeviceActions({ device, canWrite, onEdit }: {
  device: OrgDevice; canWrite: boolean; onEdit: (d: OrgDevice) => void
}) {
  const queryClient = useQueryClient()
  const [walkOpen, setWalkOpen] = useState(false)
  const confirmDelete = useConfirm()
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["inventory"] })
  const remove = useMutation({
    mutationFn: () => inventoryApi.remove(device.id),
    onSuccess: (res) => {
      if (res.ok) invalidate()
      else toast.error(res.reason || "Device has children. Remove them first")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })
  const toggleMaintenance = useMutation({
    mutationFn: () => inventoryApi.setMaintenance(device.id, !device.maintenance),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to update"),
  })
  // the web-UI tunnel entry moved into the device panel (WebUiButton beside
  // the Health/Optical/Ports tabs) — this menu is write-actions only again
  return (
    <>
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
            <DropdownMenuSeparator />
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
    </>
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
  const { detailOpen, setDetailOpen, detailTab, setDetailTab, openTab, ref } = useDrillIn(focus)
  useNow()
  const hasOptics = isOpticalOlt(device)
  const hasPorts = device.snmp_enabled === 1
  const passive = isPassiveType(device.device_type)
  const unassigned = !device.assigned_node_id && !passive

  return (
    // Open = the drill-in block: row + panel fuse into one raised surface
    // (.wisp-drillin in index.css); the row itself goes transparent so the
    // block carries the elevation, with a hairline between row and panel.
    <div ref={ref} className={cn(detailOpen ? "wisp-drillin" : "border-b last:border-b-0")}>
      <div
        className={cn("group flex h-11 cursor-pointer items-center gap-2.5 px-4 hover:bg-foreground/5",
          detailOpen && "border-b")}
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
        <span className="inline-flex shrink-0" title={unassigned ? "no probe assigned, not monitored"
          : device.state && isStale(device.state_updated_at)
          ? `stale, no report since ${ago(device.state_updated_at)}` : undefined}>
          <StatusDot tone={unassigned ? "muted" : deviceTone(device.state, device.state_updated_at)} />
        </span>
        <span className={cn("min-w-0 truncate font-mono text-xs font-medium",
          unassigned && "text-muted-foreground")}>{device.name}</span>
        {device.device_type && (
          <span className="hidden shrink-0 text-xs text-faint-foreground lg:inline">{device.device_type}</span>
        )}
        <DeviceChips device={device} hasOptics={hasOptics} collapsed={collapsed} openTab={openTab} />
        <div className="ml-auto flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <DeviceMetrics device={device} />
          <span className="hidden font-mono text-xs text-muted-foreground md:inline">{device.ip_address}</span>
          <WebUiLiveIcon device={device} />
          <DeviceCapabilityIcons device={device} hasOptics={hasOptics} hasPorts={hasPorts} />
          <DeviceActions device={device} canWrite={canWrite} onEdit={onEdit} />
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

// Grid presentation of a device — the flattened, glanceable counterpart to the
// tree row. Same drill-in panel: clicking the card expands its DeviceDetail
// full-width beneath the grid row (col-span-full), so the tabbed panel stays
// identical across both views. Tree depth/collapse are list affordances and
// don't apply here; the parent name carries the context an indent would.
function DeviceCard({ device, canWrite, onEdit, focus, parentName }: {
  device: OrgDevice & { depth: number; descendantCount: number }
  canWrite: boolean
  onEdit: (d: OrgDevice) => void
  focus?: boolean
  parentName?: string
}) {
  const { detailOpen, setDetailOpen, detailTab, setDetailTab, openTab, ref } = useDrillIn(focus)
  useNow()
  const hasOptics = isOpticalOlt(device)
  const hasPorts = device.snmp_enabled === 1
  const passive = isPassiveType(device.device_type)
  const unassigned = !device.assigned_node_id && !passive

  return (
    <>
      <div
        ref={ref}
        className={cn("group flex cursor-pointer flex-col gap-2 rounded-lg border bg-card p-3 transition-colors hover:bg-foreground/5",
          detailOpen && "border-border-strong bg-popover")}
        onClick={() => setDetailOpen(!detailOpen)}
        title={detailOpen ? undefined : "Click for details"}
      >
        <div className="flex items-center gap-2">
          <span className="inline-flex shrink-0" title={unassigned ? "no probe assigned, not monitored"
            : device.state && isStale(device.state_updated_at)
            ? `stale, no report since ${ago(device.state_updated_at)}` : undefined}>
            <StatusDot tone={unassigned ? "muted" : deviceTone(device.state, device.state_updated_at)} />
          </span>
          <span className={cn("min-w-0 flex-1 truncate font-mono text-xs font-medium",
            unassigned && "text-muted-foreground")}>{device.name}</span>
          <div onClick={(e) => e.stopPropagation()}>
            <DeviceActions device={device} canWrite={canWrite} onEdit={onEdit} />
          </div>
        </div>
        <div className="flex items-center gap-2 text-2xs text-muted-foreground">
          {device.device_type && <span className="shrink-0 text-faint-foreground">{device.device_type}</span>}
          {parentName && <span className="min-w-0 truncate" title={`under ${parentName}`}>↳ {parentName}</span>}
          {device.ip_address && <span className="ml-auto shrink-0 font-mono">{device.ip_address}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t pt-2">
          <DeviceMetrics device={device} />
          <DeviceChips device={device} hasOptics={hasOptics} openTab={openTab} />
          <div className="ml-auto flex items-center gap-1.5">
            <WebUiLiveIcon device={device} />
            <DeviceCapabilityIcons device={device} hasOptics={hasOptics} hasPorts={hasPorts} />
          </div>
        </div>
      </div>
      {detailOpen && (
        <div className="col-span-full">
          <div className="wisp-drillin px-3 pt-1 pb-3">
            <DeviceDetail device={device} tab={detailTab} onTab={setDetailTab} />
          </div>
        </div>
      )}
    </>
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

// One shared list/grid preference for the whole Network page (probes + devices),
// persisted org-independently — it's a UI taste, not per-network state.
type ViewMode = "list" | "grid"
const VIEW_KEY = "wisp:network:view"

function loadView(): ViewMode {
  try {
    return localStorage.getItem(VIEW_KEY) === "grid" ? "grid" : "list"
  } catch {
    return "list"
  }
}

function ViewToggle({ view, onChange }: { view: ViewMode; onChange: (v: ViewMode) => void }) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border p-0.5">
      {([["list", List], ["grid", LayoutGrid]] as const).map(([mode, Icon]) => (
        <button key={mode} type="button" onClick={() => onChange(mode)}
          aria-pressed={view === mode} title={`${mode.charAt(0).toUpperCase()}${mode.slice(1)} view`}
          className={cn("flex size-6 items-center justify-center rounded-sm transition-colors",
            view === mode ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
          <Icon className="size-3.5" />
        </button>
      ))}
    </div>
  )
}

export function TopologyPage() {
  const { scopeOrg, canWrite } = useAuth()
  const location = useLocation()
  const navState = location.state as { deviceId?: number; probeId?: string } | null
  const focusId = navState?.deviceId
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<OrgDevice | null>(null)
  // set when a capped org chooses "Add passive plant" — bypasses the upgrade
  // notice into the real form (passives never count against the device cap).
  const [forceForm, setForceForm] = useState(false)
  const [collapsed, setCollapsed] = useState<Set<number>>(() => loadCollapsed(scopeOrg))
  const [probeFilter, setProbeFilter] = useState<string | null>(navState?.probeId ?? null)
  const [view, setView] = useState<ViewMode>(loadView)

  const changeView = (v: ViewMode) => {
    setView(v)
    try { localStorage.setItem(VIEW_KEY, v) } catch { /* private mode / quota */ }
  }

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
    // Polling fallback: SSE + focus/visibility events are the primary refresh path,
    // but none of them fire when the tab stays foreground while the machine sleeps
    // or the SSE stream dies silently — the list then freezes and every row crosses
    // the client-side 180s isStale() line into a false "stale · 11h ago". A plain
    // interval guarantees the view self-heals within ~30s regardless (react-query
    // pauses it while hidden, resumes on visibility).
    refetchInterval: 30_000,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  // Plan + device cap, so "Add device" can surface the paywall up front rather
  // than after the form round-trips to a 422 (shared cache key with Settings).
  const billing = useQuery({
    queryKey: ["billing", scopeOrg],
    queryFn: () => billingApi.get(scopeOrg),
    enabled: !!scopeOrg && canWrite,
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
  // The cap counts monitored (non-passive) devices, matching the server; compute
  // the live count off the list so an add/delete reflects without refetching.
  const monitoredCount = allDevices.filter((d) => !isPassiveType(d.device_type)).length
  const deviceCap = billing.data?.device_cap ?? null
  const atCap = deviceCap != null && monitoredCount >= deviceCap
  const gridView = view === "grid"
  // grid flattens the tree (a card grid can't carry indent/collapse); parent-
  // before-child order still groups sensibly and each card names its parent.
  const ordered = treeOrder(devices, gridView ? new Set<number>() : collapsed)
  const nameById = new Map(allDevices.map((d) => [d.id, d.name]))
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
  const closeForm = () => { setFormOpen(false); setEditing(null); setForceForm(false) }

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold">Network</h1>
        <ViewToggle view={view} onChange={changeView} />
      </div>

      <ProbesPanel org={scopeOrg} canWrite={canWrite} view={view} deviceCounts={deviceCounts}
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
                className="flex items-center gap-1.5 self-center rounded-full border bg-card px-2.5 py-0.5 text-2xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                title="Showing only this probe's devices. Click to clear"
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
          atCap && !forceForm
            ? <UpgradeNotice billing={billing.data!} resource="device"
                note="Passive plant (splitters, FDBs, closures) doesn't count toward the limit."
                secondary={{ label: "Add passive plant", onClick: () => setForceForm(true) }}
                onClose={closeForm} />
            : <DeviceForm org={scopeOrg} editing={null} devices={devices} nodeIds={nodeIds}
                onDone={closeForm} initialType={forceForm ? "splitter" : undefined} />
        )}

        {isLoading && <Skeleton className="h-40 w-full" />}
        {!isLoading && devices.length === 0 && (
          <p className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
            {probeFilter ? `No devices assigned to ${probeFilter}.` : "No devices yet. Add one above."}
          </p>
        )}
        {devices.length > 0 && (gridView ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {ordered.map((d) => (
              <Fragment key={d.id}>
                <DeviceCard device={d} canWrite={canWrite} onEdit={openEdit} focus={d.id === focusId}
                  parentName={d.parent_device_id != null ? nameById.get(d.parent_device_id) : undefined} />
                {formOpen && editing?.id === d.id && (
                  <div className="col-span-full rounded-lg border bg-muted/30 p-3">
                    <DeviceForm org={scopeOrg} editing={editing} devices={devices} nodeIds={nodeIds} onDone={closeForm} />
                  </div>
                )}
              </Fragment>
            ))}
          </div>
        ) : (
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
        ))}
      </section>
    </div>
  )
}
