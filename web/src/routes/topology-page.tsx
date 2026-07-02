import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronRight, Pencil, Plus, Radio, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { inventoryApi, nodesApi, ApiError } from "@/lib/api"
import { DEVICE_TYPES, type OrgDevice, type SwitchPort } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { stateTone } from "@/lib/format"
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

// parent-before-child, so indentation reads as a tree — mirrors the old dashboard's
// treeOrder() in static/app.js.
function treeOrder(devices: OrgDevice[]): Array<OrgDevice & { depth: number }> {
  const children = new Map<number | null, OrgDevice[]>()
  for (const d of devices) {
    const key = d.parent_device_id
    if (!children.has(key)) children.set(key, [])
    children.get(key)!.push(d)
  }
  const out: Array<OrgDevice & { depth: number }> = []
  const seen = new Set<number>()
  const walk = (parentId: number | null, depth: number) => {
    for (const d of children.get(parentId) ?? []) {
      out.push({ ...d, depth })
      seen.add(d.id)
      walk(d.id, depth + 1)
    }
  }
  walk(null, 0)
  for (const d of devices) if (!seen.has(d.id)) out.push({ ...d, depth: 0 })
  return out
}

interface DeviceFormState {
  name: string
  ip_address: string
  device_type: string
  region: string
  parent_device_id: string
  assigned_node_id: string
  maintenance: boolean
  snmp_enabled: boolean
  snmp_community: string
  snmp_port: string
}

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", parent_device_id: "",
  assigned_node_id: "", maintenance: false, snmp_enabled: false, snmp_community: "", snmp_port: "161",
}

function DeviceForm({
  tenant, editing, devices, nodeIds, onDone,
}: {
  tenant: string
  editing: OrgDevice | null
  devices: OrgDevice[]
  nodeIds: string[]
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<DeviceFormState>(() => editing ? {
    name: editing.name, ip_address: editing.ip_address, device_type: editing.device_type ?? "",
    region: editing.region ?? "", parent_device_id: editing.parent_device_id ? String(editing.parent_device_id) : "",
    assigned_node_id: editing.assigned_node_id ?? "", maintenance: !!editing.maintenance,
    snmp_enabled: !!editing.snmp_enabled, snmp_community: editing.snmp_community ?? "",
    snmp_port: String(editing.snmp_port || 161),
  } : EMPTY_FORM)
  const [error, setError] = useState("")

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["inventory"] })

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        tenant_id: tenant,
        name: form.name.trim(),
        ip_address: form.ip_address.trim(),
        device_type: form.device_type || null,
        region: form.region.trim() || null,
        parent_device_id: form.parent_device_id ? Number(form.parent_device_id) : null,
        assigned_node_id: form.assigned_node_id || null,
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
    onError: (e) => setError(e instanceof ApiError ? e.message : "save failed"),
  })

  return (
    <Card className="border-primary/30">
      <CardContent className="flex flex-col gap-3 px-4">
        <p className="text-sm font-bold">{editing ? `Edit — ${editing.name}` : "Add node"}</p>
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
            <Input placeholder="north-dc" value={form.region}
              onChange={(e) => setForm({ ...form, region: e.target.value })} />
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
                <SelectItem value="any">— any probe —</SelectItem>
                {nodeIds.map((id) => <SelectItem key={id} value={id}>{id}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-5">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={form.maintenance}
              onCheckedChange={(v) => setForm({ ...form, maintenance: !!v })} />
            Maintenance
          </label>
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

function PortsPanel({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["inventory-ports", device.id],
    queryFn: () => inventoryApi.ports(device.id),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["inventory-ports", device.id] })
  const toggleMonitored = useMutation({
    mutationFn: (p: SwitchPort) => inventoryApi.setPortMonitored(p.id, !p.monitored),
    onSuccess: invalidate,
    onError: () => toast.error("failed to update port"),
  })

  if (isLoading) return <Skeleton className="h-16 w-full" />
  const ports = data?.ports ?? []
  if (ports.length === 0) {
    return <p className="px-1 py-2 text-xs text-muted-foreground">No SNMP ports discovered yet.</p>
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/40 p-3">
      {ports.map((p) => {
        const mbps = (bps: number | null) => bps == null ? "—" : `${(bps / 1e6).toFixed(1)} Mbps`
        return (
          <div key={p.id} className="flex flex-col gap-1.5 border-b pb-2 last:border-b-0 last:pb-0">
            <div className="flex items-center gap-2">
              <StatusDot tone={p.oper_status === "up" ? "success" : "destructive"} />
              <span className="font-mono text-[11.5px] font-semibold">
                {p.if_name || `if${p.if_index}`}{p.if_alias ? ` (${p.if_alias})` : ""}
              </span>
              {p.bw_alarm === 1 && (
                <span className="rounded-full bg-warning-soft px-1.5 py-0.5 text-[10px] font-bold text-warning">low bw</span>
              )}
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                ↓{mbps(p.in_bps)} ↑{mbps(p.out_bps)}
              </span>
            </div>
            <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
              <Switch checked={!!p.monitored} onCheckedChange={() => toggleMonitored.mutate(p)}
                className="scale-75" />
              Watch this port
            </label>
          </div>
        )
      })}
    </div>
  )
}

function DeviceRow({
  device, canWrite, onEdit,
}: {
  device: OrgDevice & { depth: number }
  canWrite: boolean
  onEdit: (d: OrgDevice) => void
}) {
  const queryClient = useQueryClient()
  const [portsOpen, setPortsOpen] = useState(false)
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["inventory"] })

  const remove = useMutation({
    mutationFn: () => inventoryApi.remove(device.id),
    onSuccess: (res) => {
      if (res.ok) invalidate()
      else toast.error(res.reason || "device has children — remove them first")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "delete failed"),
  })
  const toggleMaintenance = useMutation({
    mutationFn: () => inventoryApi.setMaintenance(device.id, !device.maintenance),
    onSuccess: invalidate,
    onError: () => toast.error("failed to update"),
  })

  return (
    <div className="border-b px-4 py-2.5 last:border-b-0">
      <div className="flex items-center gap-2" style={{ paddingLeft: device.depth * 18 }}>
        {device.depth > 0 && <span className="shrink-0 font-mono text-xs text-muted-foreground">└</span>}
        <StatusDot tone={stateTone(device.state)} />
        <span className="min-w-0 flex-1 truncate font-mono text-[13.5px] font-semibold">{device.name}</span>
        <span className="shrink-0 font-mono text-[10.5px] text-muted-foreground">{device.ip_address}</span>
        {device.snmp_enabled === 1 && (
          <Button variant="ghost" size="icon" className="size-6" onClick={() => setPortsOpen(!portsOpen)}>
            <Radio className={cn("size-3.5", portsOpen && "text-primary")} />
          </Button>
        )}
        {canWrite && (
          <>
            <Button variant="ghost" size="icon" className="size-6" onClick={() => onEdit(device)}>
              <Pencil className="size-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="size-6" onClick={() => remove.mutate()}>
              <Trash2 className="size-3.5" />
            </Button>
          </>
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1.5" style={{ paddingLeft: device.depth * 18 + 16 }}>
        {device.device_type && (
          <span className="rounded px-1.5 py-0.5 text-[9.5px] font-bold text-muted-foreground bg-muted capitalize">
            {device.device_type}
          </span>
        )}
        {!!device.maintenance && (
          <button
            onClick={() => canWrite && toggleMaintenance.mutate()}
            className="rounded px-1.5 py-0.5 text-[9.5px] font-bold text-warning bg-warning-soft"
          >
            Maintenance
          </button>
        )}
        {device.snmp_enabled === 1 && (
          <span className="rounded px-1.5 py-0.5 text-[9.5px] font-bold text-muted-foreground bg-muted">SNMP</span>
        )}
        {device.backup_parents.length > 0 && (
          <span className="rounded px-1.5 py-0.5 text-[9.5px] font-bold text-success bg-success-soft">Backup</span>
        )}
        {device.child_count > 0 && (
          <span className="rounded px-1.5 py-0.5 text-[9.5px] font-bold text-muted-foreground bg-muted">
            {device.child_count} child
          </span>
        )}
      </div>
      {portsOpen && (
        <div className="mt-2" style={{ paddingLeft: device.depth * 18 + 16 }}>
          <PortsPanel device={device} />
        </div>
      )}
    </div>
  )
}

export function TopologyPage() {
  const { scopeTenant, canWrite } = useAuth()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<OrgDevice | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["inventory", scopeTenant],
    queryFn: () => inventoryApi.list(scopeTenant),
    enabled: !!scopeTenant,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeTenant],
    queryFn: () => nodesApi.list(scopeTenant),
    enabled: !!scopeTenant,
  })

  if (!scopeTenant) return <NeedsOrg />

  const devices = data?.devices ?? []
  const ordered = treeOrder(devices)
  const nodeIds = (nodes.data?.nodes ?? []).filter((n) => !n.revoked_at).map((n) => n.node_id)

  const openEdit = (d: OrgDevice) => { setEditing(d); setFormOpen(true) }
  const closeForm = () => { setFormOpen(false); setEditing(null) }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-3 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Topology</h1>
        {canWrite && !formOpen && (
          <Button size="sm" onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus className="size-4" /> Add node
          </Button>
        )}
      </div>

      {formOpen && (
        <DeviceForm tenant={scopeTenant} editing={editing} devices={devices} nodeIds={nodeIds} onDone={closeForm} />
      )}

      {isLoading && <Skeleton className="h-40 w-full" />}
      {!isLoading && devices.length === 0 && (
        <p className="py-16 text-center text-sm text-muted-foreground">No devices yet — add one above.</p>
      )}
      {devices.length > 0 && (
        <Card className="gap-0 overflow-hidden py-0">
          {ordered.map((d) => (
            <DeviceRow key={d.id} device={d} canWrite={canWrite} onEdit={openEdit} />
          ))}
        </Card>
      )}
      <p className="flex items-center gap-1 text-xs text-muted-foreground">
        <ChevronRight className="size-3" /> Map view isn't built yet — this platform doesn't store device
        coordinates.
      </p>
    </div>
  )
}
