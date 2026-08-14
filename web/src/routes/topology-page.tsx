import { Fragment, useEffect, useMemo, useRef, useState } from "react"
import { Link, useLocation } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowUpFromLine, Cctv, ChevronRight, CornerDownRight, CornerLeftUp, Gauge, MoreVertical, Palette, Pencil, Plus, Radio, ScanSearch, Scissors, Search, Tags, Trash2, Waypoints, Wrench, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDebounced } from "@/hooks/use-debounced"
import { useNow } from "@/hooks/use-now"
import { usePonOptions } from "@/hooks/use-pon-options"
import { PanelResizeGrip, useResizablePanel } from "@/hooks/use-resizable-panel"
import { billingApi, gponApi, inventoryApi, nodesApi, nvrApi, ApiError } from "@/lib/api"
import { DEVICE_TYPES, isPassiveType, type OnuSearchMatch, type OrgDevice } from "@/lib/types"
import { SplitRatioField } from "@/components/split-ratio-field"
import { oltHead, ponOptions } from "@/map/plant"
import { DOT as ONU_DOT, onuSev } from "@/components/optical-panel"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import {
  DeviceDetail, DeviceMetrics, DevicePanelHeader, RowTag, deviceTabs, isOpticalOlt,
  VITAL_CPU_CRIT, VITAL_TEMP_CRIT, type DeviceTab,
} from "@/components/device-detail"
import { SubscriberDialog } from "@/components/subscriber-detail"
import { NeedsOrg } from "@/components/needs-org"
import { RegionSelect } from "@/components/region-select"
import { runSnmpTest } from "@/components/snmp-test"
import { TagsInput } from "@/components/tags-input"
import { ViewToggle, loadView, saveView, type ViewMode } from "@/components/view-toggle"
import { ProbesPanel } from "@/components/probes-panel"
import { SnmpWalkDialog } from "@/components/snmp-walk-dialog"
import { UpgradeNotice } from "@/components/upgrade-notice"
import { WebUiLiveIcon } from "@/components/web-proxy"
import { StatusDot } from "@/components/status-badge"
import { OnuHealth } from "@/components/onu-bar"
import { ColorSwatches } from "@/components/color-swatches"
import {
  ago, deviceTone, durationSince, isDownState, isFresh, isStale,
  NO_ASSIGNED_DEVICES, onuName, onuSearchKey,
} from "@/lib/format"
import { paletteVarOf, type PaletteColor } from "@/lib/palette"
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
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

type TreeRow = OrgDevice & { depth: number; descendantCount: number }

function treeOrder(
  devices: OrgDevice[], collapsed: Set<number>,
  cmp?: (a: OrgDevice, b: OrgDevice) => number,
): { gear: TreeRow[]; plant: TreeRow[] } {
  const byId = new Map(devices.map((d) => [d.id, d]))
  const passive = (d: OrgDevice) => isPassiveType(d.device_type)
  const parentOf = (d: OrgDevice): OrgDevice | undefined => {
    if (d.parent_device_id == null || d.tree_detached === 1) return undefined
    const p = byId.get(d.parent_device_id)
    if (!p || passive(p) !== passive(d)) return undefined
    return p
  }
  const children = new Map<number, OrgDevice[]>()
  for (const d of devices) {
    const p = parentOf(d)
    if (!p) continue
    if (!children.has(p.id)) children.set(p.id, [])
    children.get(p.id)!.push(d)
  }
  const sorted = (arr: OrgDevice[]) => (cmp ? [...arr].sort(cmp) : arr)
  const kids = (id: number) => sorted(children.get(id) ?? [])
  const descendantCount = (id: number): number =>
    (children.get(id) ?? []).reduce((sum, k) => sum + 1 + descendantCount(k.id), 0)
  const gear: TreeRow[] = []
  const plant: TreeRow[] = []
  const emit = (d: OrgDevice, depth: number, out: TreeRow[]) => {
    out.push({ ...d, depth, descendantCount: descendantCount(d.id) })

    if (!collapsed.has(d.id)) for (const k of kids(d.id)) emit(k, depth + 1, out)
  }
  for (const d of sorted(devices.filter((d) => !parentOf(d)))) {
    emit(d, 0, passive(d) ? plant : gear)
  }
  return { gear, plant }
}

export interface ColorMaps {
  tags: Record<string, string>
  nodes: Record<string, string>
}

function deviceColor(d: OrgDevice, colors: ColorMaps): string | null {
  for (const t of d.tags) {
    const c = paletteVarOf(colors.tags[t])
    if (c) return c
  }
  return d.assigned_node_id ? paletteVarOf(colors.nodes[d.assigned_node_id]) : null
}

const railStyle = (color: string | null) => ({ borderLeftColor: color ?? "transparent" })
const RAIL = "border-l-[3px]"

function TagColorsDialog({ org, tags, colors, counts, open, onOpenChange }: {
  org: string
  tags: string[]
  colors: Record<string, string>
  counts: Map<string, number>
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const queryClient = useQueryClient()
  const setColor = useMutation({
    mutationFn: ({ tag, color }: { tag: string; color: PaletteColor | null }) =>
      inventoryApi.setTagColor(org, tag, color),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inventory"] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to save colour"),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Tag colours</DialogTitle>
          <DialogDescription>
            A device takes the colour of its first coloured tag, and falls back to
            its probe's colour. Status always renders on top, so a colour never
            hides an alarm.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-96 overflow-y-auto">
          {tags.map((t) => (
            <div key={t} className="wisp-row flex h-11 items-center gap-3 px-1">
              <span className="min-w-0 truncate font-mono text-xs">{t}</span>
              <span className="ml-auto shrink-0 text-2xs text-faint-foreground"
                title={`${counts.get(t)} device(s)`}>{counts.get(t)}</span>
              <ColorSwatches value={colors[t]}
                onPick={(color) => setColor.mutate({ tag: t, color })} />
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}

type SortMode = "default" | "ip" | "type"

function ipKey(d: OrgDevice): number {
  const m = d.ip_address.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/)
  if (!m) return Number.MAX_SAFE_INTEGER
  return ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4]
}

const TYPE_RANK: Record<string, number> = {
  core: 0, router: 1, gateway: 2, backhaul: 3, switch: 4, OLT: 5, AP: 6, CPE: 7,
  splitter: 8, fdb: 9, closure: 10,
}

function comparatorFor(mode: SortMode): ((a: OrgDevice, b: OrgDevice) => number) | undefined {
  if (mode === "ip") return (a, b) => ipKey(a) - ipKey(b) || a.name.localeCompare(b.name)
  if (mode === "type") {
    return (a, b) =>
      (TYPE_RANK[a.device_type ?? ""] ?? 99) - (TYPE_RANK[b.device_type ?? ""] ?? 99)
      || ipKey(a) - ipKey(b) || a.name.localeCompare(b.name)
  }
  return undefined // insertion order (ORDER BY id), the historical behavior
}

function filterWithAncestors(devices: OrgDevice[], query: string,
                             extraIds?: ReadonlySet<number>): OrgDevice[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return devices
  const byId = new Map(devices.map((d) => [d.id, d]))
  const hit = (d: OrgDevice) =>
    extraIds?.has(d.id)
    || d.name.toLowerCase().includes(needle)
    || d.ip_address.includes(needle)
    || (d.device_type ?? "").toLowerCase().includes(needle)
    || (d.region ?? "").toLowerCase().includes(needle)
    || d.tags.some((t) => t.toLowerCase().includes(needle))
  const keep = new Set<number>()
  for (const d of devices) {
    if (!hit(d)) continue
    keep.add(d.id)
    let cur = d.parent_device_id
    const seen = new Set<number>()
    while (cur != null && byId.has(cur) && !seen.has(cur)) {
      seen.add(cur)
      keep.add(cur)
      cur = byId.get(cur)!.parent_device_id
    }
  }
  return devices.filter((d) => keep.has(d.id))
}

const GPON_VENDORS = ["huawei", "dbc"] as const

interface DeviceFormState {
  name: string
  ip_address: string
  device_type: string
  region: string
  tags: string[]
  parent_device_id: string
  assigned_node_id: string
  snmp_enabled: boolean
  snmp_community: string
  snmp_port: string
  gpon_vendor: string
  nvr_vendor: string
  pon_port: string
  split_ratio: string
  split_inputs: string
  onu_pon_limit: string
}

const NO_PON = "__nopon__"
const NO_PON_TYPE = "__default__"
const PON_TYPES = [
  { cap: 64, label: "EPON · 1:64" },
  { cap: 128, label: "GPON · 1:128" },
]

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", tags: [],
  parent_device_id: "",
  assigned_node_id: "", snmp_enabled: false, snmp_community: "", snmp_port: "161",
  gpon_vendor: "", nvr_vendor: "", pon_port: "", split_ratio: "",
  split_inputs: "", onu_pon_limit: "",
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
    region: editing.region ?? "", tags: editing.tags ?? [],
    parent_device_id: editing.parent_device_id ? String(editing.parent_device_id) : "",
    assigned_node_id: editing.assigned_node_id ?? "",
    snmp_enabled: !!editing.snmp_enabled, snmp_community: editing.snmp_community ?? "",
    snmp_port: String(editing.snmp_port || 161),
    gpon_vendor: editing.gpon_vendor ?? "",
    nvr_vendor: editing.nvr_vendor ?? "",
    pon_port: editing.pon_port ?? "",
    split_ratio: editing.split_ratio ? String(editing.split_ratio) : "",
    split_inputs: editing.split_inputs ? String(editing.split_inputs) : "",
    onu_pon_limit: editing.onu_pon_limit ? String(editing.onu_pon_limit) : "",
  } : { ...EMPTY_FORM })
  const [error, setError] = useState("")

  const gponProfiles = useQuery({
    queryKey: ["gpon-profiles", org],
    queryFn: () => gponApi.profiles(org),
    enabled: form.device_type === "OLT",
  })
  const gponVendors = [...new Set([
    ...GPON_VENDORS,
    ...(gponProfiles.data?.profiles.filter((p) => p.enabled).map((p) => p.name) ?? []),
    ...(form.gpon_vendor ? [form.gpon_vendor] : []),
  ])].sort()

  const nvrProfiles = useQuery({
    queryKey: ["nvr-profiles", org],
    queryFn: () => nvrApi.profiles(org),
    enabled: form.device_type === "nvr",
  })
  const nvrVendors = [...new Set([
    ...(nvrProfiles.data?.names ?? []),
    ...(form.nvr_vendor ? [form.nvr_vendor] : []),
  ])].sort()

  const cardRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" })
  }, [editing])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["inventory"] })
    queryClient.invalidateQueries({ queryKey: ["regions"] })
  }

  const passive = isPassiveType(form.device_type)
  const editingPassive = editing != null && isPassiveType(editing.device_type)
  const plantTypes: string[] = !editingPassive ? []
    : editing!.device_type && editing!.device_type !== "splitter"
      ? ["splitter", editing!.device_type]
      : ["splitter"]
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const ponParent = form.parent_device_id ? byId.get(Number(form.parent_device_id)) ?? null : null
  const ponOlt = passive ? oltHead(ponParent, byId) : null
  const { pons, loading: ponsLoading } = usePonOptions(ponOlt?.id, passive)
  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        org_id: org,
        name: form.name.trim(),
        ip_address: passive ? "" : form.ip_address.trim(),
        device_type: form.device_type || null,
        region: form.region.trim() || null,
        tags: form.tags,
        parent_device_id: form.parent_device_id ? Number(form.parent_device_id) : null,
        assigned_node_id: passive ? null : (form.assigned_node_id || null),

        gpon_vendor: form.device_type === "OLT" ? (form.gpon_vendor || null) : null,
        nvr_vendor: form.device_type === "nvr" ? (form.nvr_vendor || null) : null,
        onu_pon_limit: form.device_type === "OLT" && form.onu_pon_limit
          ? Number(form.onu_pon_limit) : null,
        pon_port: passive ? (form.pon_port.trim() || null) : null,
        split_ratio: passive && form.split_ratio ? Number(form.split_ratio) : null,
        split_inputs: passive && form.split_inputs ? Number(form.split_inputs) : null,
      }
      if (editing) {
        await inventoryApi.update(editing.id, payload)
        if (!passive) {
          await inventoryApi.setSnmp(editing.id, {
            snmp_enabled: form.snmp_enabled, snmp_community: form.snmp_community.trim() || null,
            snmp_port: form.snmp_port,
          })
          const snmpChanged = form.snmp_enabled !== !!editing.snmp_enabled
            || form.snmp_community.trim() !== (editing.snmp_community ?? "")
            || form.snmp_port !== String(editing.snmp_port || 161)
          if (snmpChanged && form.snmp_enabled && form.snmp_community.trim()
              && form.assigned_node_id) {
            void runSnmpTest({
              id: editing.id, name: form.name.trim() || editing.name,
              ip_address: form.ip_address.trim() || editing.ip_address,
              snmp_port: Number(form.snmp_port) || 161,
            }, queryClient)
          }
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
      <CardContent className="@container flex flex-col gap-3 px-4">
        <p className="text-sm font-semibold">{editing ? `Edit: ${editing.name}` : "Add device"}</p>
        <div className="grid gap-3 @lg:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label>Name</Label>
            <Input placeholder="e.g. ap-ridge-09" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>{passive ? "PON port (optional)" : "IP address"}</Label>
            {passive ? (
              ponOlt && pons.length > 0 ? (
                <>
                  <Select value={form.pon_port || NO_PON}
                    onValueChange={(v) => setForm({ ...form, pon_port: v === NO_PON ? "" : v })}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Not recorded" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_PON}>Not recorded</SelectItem>
                      {ponOptions(pons, form.pon_port).map((p) => (
                        <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-2xs text-faint-foreground">
                    Ports {ponOlt.name} reports.
                    {form.pon_port && !pons.includes(form.pon_port)
                      && " The saved value isn't among them — keep it or pick one."}
                  </p>
                </>
              ) : (
                <>
                  <Input placeholder="EPON0/4" className="font-mono" value={form.pon_port}
                    onChange={(e) => setForm({ ...form, pon_port: e.target.value })} />
                  <p className="text-2xs text-faint-foreground">
                    {ponsLoading ? "Reading PON labels…"
                      : ponOlt ? `${ponOlt.name} has no ONU roster yet, so there are no labels to pick from.`
                        : "Set the parent to see the PONs its OLT reports."}
                  </p>
                </>
              )
            ) : (
              <Input placeholder="10.4.1.9" className="font-mono" value={form.ip_address}
                onChange={(e) => setForm({ ...form, ip_address: e.target.value })} />
            )}
          </div>
          {passive && (
            <div className="flex flex-col gap-1.5">
              <Label>Split ratio (optional)</Label>
              <SplitRatioField
                value={{
                  ratio: form.split_ratio ? Number(form.split_ratio) : null,
                  inputs: form.split_inputs ? Number(form.split_inputs) : null,
                }}
                onChange={(next) => setForm({
                  ...form,
                  split_ratio: next.ratio ? String(next.ratio) : "",
                  split_inputs: next.inputs ? String(next.inputs) : "",
                })} />
            </div>
          )}
          <div className="flex flex-col gap-1.5">
            <Label>Type</Label>
            <Select value={form.device_type} onValueChange={(v) => setForm({ ...form, device_type: v })}>
              <SelectTrigger className="w-full"><SelectValue placeholder="(type)" /></SelectTrigger>
              <SelectContent>
                {DEVICE_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
                {plantTypes.map((t) => (
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
          <div className="flex flex-col gap-1.5 sm:col-span-2">
            <Label>Tags</Label>
            <TagsInput value={form.tags}
              suggestions={devices.flatMap((d) => d.tags)}
              onChange={(tags) => setForm({ ...form, tags })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Parent</Label>
            <Select value={form.parent_device_id || "none"}
              onValueChange={(v) => setForm({ ...form, parent_device_id: v === "none" ? "" : v })}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None (root)</SelectItem>
                {devices
                  .filter((d) => d.id !== editing?.id
                    && (passive || !isPassiveType(d.device_type)))
                  .map((d) => (
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
            Passive plant: lives on the map and in the tree, never probed. New
            boxes are recorded on the map or in the survey, where the location
            and the feeder come from where you clicked or stood.
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
          {form.device_type === "nvr" && (
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-muted-foreground">NVR brand</Label>
              <Select value={form.nvr_vendor || "none"}
                onValueChange={(v) => setForm({ ...form, nvr_vendor: v === "none" ? "" : v })}>
                <SelectTrigger className="w-52"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">not set (camera read off)</SelectItem>
                  {nvrVendors.map((v) => <SelectItem key={v} value={v}>{v}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
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
          {form.device_type === "OLT" && (
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-muted-foreground">PON type</Label>
              <Select value={form.onu_pon_limit || NO_PON_TYPE}
                onValueChange={(v) => setForm({
                  ...form, onu_pon_limit: v === NO_PON_TYPE ? "" : v })}>
                <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PON_TYPE}>not set (global cap)</SelectItem>
                  {PON_TYPES.map((p) => (
                    <SelectItem key={p.cap} value={String(p.cap)}>{p.label}</SelectItem>
                  ))}
                  {form.onu_pon_limit
                    && !PON_TYPES.some((p) => String(p.cap) === form.onu_pon_limit) && (
                    <SelectItem value={form.onu_pon_limit}>
                      custom · 1:{form.onu_pon_limit}
                    </SelectItem>
                  )}
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

interface DrillIn {
  open: boolean
  tab: DeviceTab
  focusOnu: number | null
  onToggle: () => void
  onTab: (t: DeviceTab) => void
  openTab: (t: DeviceTab) => void
}

function useFocusScroll(focus?: boolean) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focus) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" })
  }, [focus])
  return ref
}

const ONU_SEARCH_MIN = 3

function OnuMatchList({ matches, truncated, loading, onOpen }: {
  matches: OnuSearchMatch[]
  truncated: boolean
  loading: boolean
  onOpen: (deviceId: number, onuId: number) => void
}) {
  const [openSub, setOpenSub] = useState<
    { mac: string; deviceId: number; onuRowId: number } | null>(null)
  const total = matches.reduce((n, m) => n + m.onus.length, 0)
  if (loading && !total) return <Skeleton className="h-24 w-full" />
  if (!total) return null
  return (
    <Card className="gap-0 overflow-hidden py-0">
      <div className="flex h-11 items-center gap-2 border-b px-4">
        <Waypoints className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="shrink-0 text-xs font-medium">
          {total} ONU{total === 1 ? "" : "s"}
        </span>
        <span className="min-w-0 truncate text-2xs text-faint-foreground">
          matched by MAC or name on {matches.length} OLT{matches.length === 1 ? "" : "s"}
        </span>
        {truncated && (
          <RowTag tone="muted" title="More ONUs match than are shown. Type more of the MAC to narrow it.">
            capped
          </RowTag>
        )}
      </div>
      {matches.map((m) => (
        <Fragment key={m.device_id}>
          {m.onus.map((o) => (
            <button key={o.id} type="button"
              onClick={() => (o.serial
                ? setOpenSub({ mac: o.serial, deviceId: m.device_id, onuRowId: o.id })
                : onOpen(m.device_id, o.id))}
              title={o.serial ? "Open this subscriber" : "Open this ONU in its OLT's Optical tab"}
              className="flex h-11 w-full items-center gap-2.5 border-b px-4 text-left last:border-b-0 hover:bg-foreground/5">
              <span className={cn("size-2 shrink-0 rounded-full", ONU_DOT[onuSev(o)])} />
              <span className="shrink-0 font-mono text-xs font-medium">
                {o.serial || o.onu_key}
              </span>
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {onuName(o) || <span className="text-faint-foreground">unnamed</span>}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-3 text-2xs text-muted-foreground">
                <span className="hidden font-mono sm:inline">
                  {m.device_name}
                  {o.pon_port ? ` · PON ${o.pon_port}` : ""}
                  {o.onu_id != null ? ` · ONU ${o.onu_id}` : ""}
                </span>
                {o.state === "online"
                  ? <RowTag tone="success">online</RowTag>
                  : <RowTag tone="muted" title={o.last_online_at ? `last online ${ago(o.last_online_at)}` : undefined}>
                      {o.last_online_at ? `dark ${durationSince(o.last_online_at)}` : o.state || "offline"}
                    </RowTag>}
              </div>
            </button>
          ))}
        </Fragment>
      ))}
      {openSub && (
        <SubscriberDialog mac={openSub.mac} onClose={() => setOpenSub(null)}
          actions={{
            onOpenOlt: () => {
              onOpen(openSub.deviceId, openSub.onuRowId)
              setOpenSub(null)
            },
          }} />
      )}
    </Card>
  )
}

function DeviceIdentityChips({ device, collapsed }: {
  device: OrgDevice & { descendantCount?: number }
  collapsed?: boolean
}) {
  const passive = isPassiveType(device.device_type)
  const unassigned = !device.assigned_node_id && !passive
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
      {collapsed && (device.descendantCount ?? 0) > 0 && (
        <RowTag tone="muted" title="Children hidden by this collapsed branch">
          +{device.descendantCount}
        </RowTag>
      )}
    </>
  )
}

function DeviceAlarmChips({ device, hasOptics, openTab, dupMac = true }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
  dupMac?: boolean
}) {
  const { liveSnmp, opticsChips } = alarmGates(device, hasOptics)
  return (
    <>

      {opticsChips && device.fiber_cuts > 0 && (
        <RowTag tone="destructive" icon={Scissors}
          title="Suspected fiber cut (PON mass-drop). Click for optics"
          onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
          {device.fiber_cuts === 1 ? "fiber cut" : `${device.fiber_cuts} fiber cuts`}
        </RowTag>
      )}
      {liveSnmp && device.ports_down > 0 && (
        <RowTag tone="destructive" title="A watched port is down. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          {device.ports_down === 1 ? "port down" : `${device.ports_down} ports down`}
        </RowTag>
      )}
      {liveSnmp && isFresh(device.cameras_updated_at)
        && (device.cameras_down ?? 0) > 0 && (
        <RowTag tone="destructive" title="A camera is dark. Click for cameras"
          onClick={(e) => { e.stopPropagation(); openTab("cameras") }}>
          {device.cameras_down === 1 ? "cam dark" : `${device.cameras_down} cams dark`}
        </RowTag>
      )}
      {liveSnmp && (device.health_temp_c ?? 0) >= VITAL_TEMP_CRIT && (
        <RowTag tone="destructive" title="Device temperature critical. Click for health"
          onClick={(e) => { e.stopPropagation(); openTab("health") }}>
          {Math.round(device.health_temp_c!)}°C
        </RowTag>
      )}
      {liveSnmp && (device.health_cpu_pct ?? 0) >= VITAL_CPU_CRIT && (
        <RowTag tone="destructive" title="Device CPU pegged. Click for health"
          onClick={(e) => { e.stopPropagation(); openTab("health") }}>
          cpu {Math.round(device.health_cpu_pct!)}%
        </RowTag>
      )}
      {liveSnmp && device.ports_bw_low > 0 && (
        <RowTag tone="warning" title="A watched port is below its bandwidth floor. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          low bw
        </RowTag>
      )}
      {liveSnmp && device.ports_bw_high > 0 && (
        <RowTag tone="warning" title="A watched port is above its bandwidth ceiling. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          high bw
        </RowTag>
      )}
      {dupMac && <DupMacChip device={device} hasOptics={hasOptics} openTab={openTab} />}
    </>
  )
}

function DupMacChip({ device, hasOptics, openTab }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
}) {
  const { opticsChips } = alarmGates(device, hasOptics)
  if (!opticsChips || device.dup_macs <= 0) return null
  return (
    <RowTag tone="muted" title="Duplicate ONU MAC: cloned CPE or bridging loop. Click for optics"
      onClick={(e) => { e.stopPropagation(); openTab("optical") }}>
      {device.dup_macs === 1 ? "dup MAC" : `${device.dup_macs} dup MACs`}
    </RowTag>
  )
}

function alarmGates(device: OrgDevice, hasOptics: boolean) {
  const liveSnmp = !isDownState(device.state) && !isStale(device.state_updated_at)
  return { liveSnmp, opticsChips: hasOptics && liveSnmp }
}

function DeviceOnuHealth({ device, hasOptics, openTab }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
}) {
  return (
    <div className="flex shrink-0 lg:w-[7rem]">
      <CardOnuHealth device={device} hasOptics={hasOptics} openTab={openTab}
        className="lg:w-full lg:justify-between" />
    </div>
  )
}

function CardOnuHealth({ device, hasOptics, openTab, className }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
  className?: string
}) {
  const { opticsChips } = alarmGates(device, hasOptics)
  if (!opticsChips || (device.onus_total ?? 0) <= 0) return null
  return (
    <OnuHealth total={device.onus_total ?? 0} crit={device.onus_crit ?? 0}
      warn={device.onus_warn ?? 0} online={device.onus_online ?? undefined}
      className={className}
      onClick={(e) => { e.stopPropagation(); openTab("optical") }} />
  )
}

function DeviceCapabilityIcons({ device, hasOptics, hasPorts }: {
  device: OrgDevice; hasOptics: boolean; hasPorts: boolean
}) {
  const hasCameras = (device.device_type ?? "").toLowerCase() === "nvr"
  if (!hasOptics && !hasPorts && !hasCameras) return null
  const opticsFresh = isFresh(device.optics_updated_at)
  const portsFresh = isFresh(device.ports_updated_at)
  const camerasFresh = isFresh(device.cameras_updated_at)
  const camCount = device.cameras_total ?? 0
  const rxCount = device.onus_rx ?? 0
  const hasRx = rxCount > 0 && opticsFresh
  return (
    <div className="flex items-center gap-1.5">
      {hasCameras && (
        <span title={(device.cameras_down ?? 0) > 0
          ? `Cameras: ${device.cameras_down} of ${camCount} dark`
          : camCount > 0 && camerasFresh
            ? `Cameras: ${camCount} channels reporting`
            : camCount > 0
              ? "Cameras: the channel read has gone stale"
              : "Cameras: no channel read yet. Open the Cameras tab for why."}>
          <Cctv className={cn("size-3.5",
            (device.cameras_down ?? 0) > 0 ? "text-destructive"
              : camCount > 0 && camerasFresh ? "text-success"
              : "text-faint-foreground")} />
        </span>
      )}
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
      {hasOptics && (
        <span title={hasRx
          ? `Per-ONU dBm: ${rxCount} ONU${rxCount === 1 ? "" : "s"} reporting optical power`
          : rxCount > 0
            ? "Per-ONU dBm: readings have gone stale. The optical walk stopped."
            : "Per-ONU dBm: none. This OLT reports no optical power. Open the Optical tab for why."}>
          <Gauge className={cn("size-3.5",
            hasRx ? "text-success" : "text-faint-foreground")} />
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

function DeviceActions({ device, canWrite, onEdit, parentName }: {
  device: OrgDevice; canWrite: boolean; onEdit: (d: OrgDevice) => void
  parentName?: string
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
  const toggleDetached = useMutation({
    mutationFn: () => inventoryApi.setTreeDetached(device.id, !device.tree_detached),
    onSuccess: () => {
      invalidate()
      toast.success(device.tree_detached
        ? `${device.name} nests under ${parentName ?? "its parent"} again`
        : `${device.name} moved to the top level of the tree`,
        { description: "View only. The parent link, alerting and map are unchanged." })
    },
    onError: () => toast.error("Failed to update"),
  })
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
          <DropdownMenuContent align="end" className="w-auto min-w-52">
            <DropdownMenuItem onClick={() => onEdit(device)}>
              <Pencil /> Edit
            </DropdownMenuItem>
            {device.snmp_enabled === 1 && (
              <DropdownMenuItem onClick={() => setWalkOpen(true)}>
                <ScanSearch /> SNMP walk
              </DropdownMenuItem>
            )}
            {!isPassiveType(device.device_type) && (
              <DropdownMenuItem onClick={() => toggleMaintenance.mutate()}>
                <Wrench /> {device.maintenance ? "End maintenance" : "Start maintenance"}
              </DropdownMenuItem>
            )}
            {device.parent_device_id != null && (
              <DropdownMenuItem onClick={() => toggleDetached.mutate()}
                className="max-w-72"
                title={(device.tree_detached ? `Nest under ${parentName ?? "parent"}. ` : "")
                  + "Network tree only. The parent link, alerting and map are unchanged."}>
                {device.tree_detached ? (
                  <>
                    <CornerDownRight />
                    <span className="min-w-0 truncate">Nest under {parentName ?? "parent"}</span>
                  </>
                ) : (
                  <><ArrowUpFromLine /> Show at top level</>
                )}
              </DropdownMenuItem>
            )}
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
  device, canWrite, onEdit, collapsed, onToggleCollapse, focus, drill, parentName,
  colors,
}: {
  device: OrgDevice & { depth: number; descendantCount: number }
  canWrite: boolean
  onEdit: (d: OrgDevice) => void
  collapsed: boolean
  onToggleCollapse: () => void
  focus?: boolean
  drill: DrillIn
  parentName?: string
  colors: ColorMaps
}) {
  const { open: detailOpen, onToggle, openTab } = drill
  const ref = useFocusScroll(focus)
  useNow()
  const hasOptics = isOpticalOlt(device)
  const hasPorts = device.snmp_enabled === 1
  const passive = isPassiveType(device.device_type)
  const unassigned = !device.assigned_node_id && !passive
  const lifted = device.tree_detached === 1 || (passive && device.depth === 0)

  return (
    <div ref={ref} className={cn(detailOpen ? "wisp-drillin" : "border-b last:border-b-0")}>
      <div
        className={cn("group flex h-11 cursor-pointer items-center gap-2.5 px-4 hover:bg-foreground/5",
          RAIL)}
        style={railStyle(deviceColor(device, colors))}
        onClick={onToggle}
        title={detailOpen ? undefined : "Click for details"}
      >
        {Array.from({ length: device.depth }).map((_, i) => (
          <span key={i} aria-hidden className="w-3 shrink-0 self-stretch border-l sm:w-4" />
        ))}
        {device.descendantCount > 0 ? (
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
          <span className="hidden shrink-0 text-xs text-faint-foreground @2xl:inline">{device.device_type}</span>
        )}
        {lifted && parentName && (
          <span className="hidden min-w-0 shrink items-center gap-1 text-xs text-faint-foreground @sm:inline-flex"
            title={device.tree_detached === 1
              ? `Hangs off ${parentName}, shown at the top level for readability`
              : `Fed from ${parentName}. Passive plant lists below the gear.`}>
            <CornerLeftUp className="size-3 shrink-0" />
            <span className="truncate">{parentName}</span>
          </span>
        )}
        <DeviceIdentityChips device={device} collapsed={collapsed} />
        <div className="ml-auto flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-end gap-1.5">
            <DeviceAlarmChips device={device} hasOptics={hasOptics} openTab={openTab} />
          </div>
          <span className="hidden @3xl:inline-flex">
            <DeviceOnuHealth device={device} hasOptics={hasOptics} openTab={openTab} />
          </span>
          <div className="flex min-w-[4.5rem] shrink-0 justify-end">
            <DeviceMetrics device={device} />
          </div>
          <span className="hidden w-[8.5rem] shrink-0 text-right font-mono text-xs text-muted-foreground @xl:inline-block">
            {device.ip_address}
          </span>
          <div className="hidden w-[4.75rem] shrink-0 items-center justify-end gap-1.5 @3xl:flex">
            <WebUiLiveIcon device={device} />
            <DeviceCapabilityIcons device={device} hasOptics={hasOptics} hasPorts={hasPorts} />
          </div>
          <DeviceActions device={device} canWrite={canWrite} onEdit={onEdit} parentName={parentName} />
        </div>
      </div>
    </div>
  )
}

function DeviceCard({ device, canWrite, onEdit, focus, parentName, drill, colors }: {
  device: OrgDevice & { depth: number; descendantCount: number }
  canWrite: boolean
  onEdit: (d: OrgDevice) => void
  focus?: boolean
  parentName?: string
  drill: DrillIn
  colors: ColorMaps
}) {
  const { open: detailOpen, onToggle, openTab } = drill
  const ref = useFocusScroll(focus)
  useNow()
  const hasOptics = isOpticalOlt(device)
  const hasPorts = device.snmp_enabled === 1
  const passive = isPassiveType(device.device_type)
  const unassigned = !device.assigned_node_id && !passive

  return (
    <div
      ref={ref}
      className={cn("group flex cursor-pointer flex-col gap-2 rounded-lg border bg-card p-3 transition-colors hover:bg-foreground/5",
        RAIL, detailOpen && "border-border-strong bg-popover")}
      style={railStyle(deviceColor(device, colors))}
      onClick={onToggle}
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
            <DeviceActions device={device} canWrite={canWrite} onEdit={onEdit} parentName={parentName} />
          </div>
        </div>
        <div className="flex items-center gap-2 text-2xs text-muted-foreground">
          {device.device_type && <span className="shrink-0 text-faint-foreground">{device.device_type}</span>}
          {parentName && <span className="min-w-0 truncate" title={`under ${parentName}`}>↳ {parentName}</span>}
          {device.ip_address && <span className="ml-auto shrink-0 font-mono">{device.ip_address}</span>}
        </div>
        <div className="flex items-start gap-2 border-t pt-2">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2.5 gap-y-1.5">
            <DeviceMetrics device={device} />
            <DeviceIdentityChips device={device} />
            <DeviceAlarmChips device={device} hasOptics={hasOptics} openTab={openTab}
              dupMac={false} />
            <CardOnuHealth device={device} hasOptics={hasOptics} openTab={openTab} />
            <DupMacChip device={device} hasOptics={hasOptics} openTab={openTab} />
          </div>
          <div className="flex h-5 shrink-0 items-center gap-1.5">
            <WebUiLiveIcon device={device} />
            <DeviceCapabilityIcons device={device} hasOptics={hasOptics} hasPorts={hasPorts} />
          </div>
        </div>
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
  }
}

const PLANT_KEY = "wisp:network:plant-open"

function loadPlantOpen(): boolean {
  try { return localStorage.getItem(PLANT_KEY) === "1" } catch { return false }
}

const SORT_KEY = "wisp:network:sort"

function loadSort(): SortMode {
  try {
    const v = localStorage.getItem(SORT_KEY)
    return v === "ip" || v === "type" ? v : "default"
  } catch {
    return "default"
  }
}

export function TopologyPage() {
  const { scopeOrg, canWrite, isWorker } = useAuth()
  const location = useLocation()
  const navState = location.state as
    { deviceId?: number; probeId?: string; tab?: DeviceTab; onuId?: number
      focusSearch?: number
      statusFilter?: { label: string; ids: number[] } } | null
  const focusId = navState?.deviceId
  const focusTab = navState?.tab
  const focusOnuId = navState?.onuId
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<OrgDevice | null>(null)
  const [collapsed, setCollapsed] = useState<Set<number>>(() => loadCollapsed(scopeOrg))
  const [plantOpen, setPlantOpen] = useState<boolean>(loadPlantOpen)
  const [probeFilter, setProbeFilter] = useState<string | null>(navState?.probeId ?? null)
  const [statusFilter, setStatusFilter] = useState<{ label: string; ids: number[] } | null>(
    navState?.statusFilter ?? null)
  const [view, setView] = useState<ViewMode>(loadView)
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const [sortMode, setSortMode] = useState<SortMode>(loadSort)
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set())
  const [tagColorsOpen, setTagColorsOpen] = useState(false)
  const [open, setOpen] = useState<{ id: number; tab: DeviceTab; onu?: number | null } | null>(null)
  const [jumpId, setJumpId] = useState<number | null>(null)
  const drillFor = (device: OrgDevice): DrillIn => {
    const id = device.id
    const defaultTab = deviceTabs(device)[0]
    return {
      open: open?.id === id,
      tab: open?.id === id ? open.tab : defaultTab,
      focusOnu: open?.id === id ? open.onu ?? null : null,
      onToggle: () => setOpen((o) => (o?.id === id ? null : { id, tab: defaultTab })),
      onTab: (t) => setOpen((o) => (o?.id === id ? { ...o, tab: t } : o)),
      openTab: (t) => setOpen({ id, tab: t }),
    }
  }
  const openOnu = (deviceId: number, onuId: number) => {
    setOpen({ id: deviceId, tab: "optical", onu: onuId })
    setJumpId(deviceId)
  }

  const changeView = (v: ViewMode) => {
    setView(v)
    saveView(v)
  }
  const changeSort = (v: SortMode) => {
    setSortMode(v)
    try { localStorage.setItem(SORT_KEY, v) } catch { /* private mode / quota */ }
  }
  const changePlantOpen = (v: boolean) => {
    setPlantOpen(v)
    try { localStorage.setItem(PLANT_KEY, v ? "1" : "0") } catch { /* private mode / quota */ }
  }
  const toggleTag = (t: string) => setTagFilter((prev) => {
    const next = new Set(prev)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    return next
  })

  useEffect(() => { setCollapsed(loadCollapsed(scopeOrg)) }, [scopeOrg])
  useEffect(() => { setTagFilter(new Set()) }, [scopeOrg])
  useEffect(() => { if (navState?.probeId) setProbeFilter(navState.probeId) }, [navState?.probeId])
  useEffect(() => {
    if (navState?.statusFilter) setStatusFilter(navState.statusFilter)
  }, [navState?.statusFilter])
  useEffect(() => {
    if (!navState?.focusSearch) return
    searchRef.current?.focus()
    searchRef.current?.select()
  }, [navState?.focusSearch])
  useEffect(() => {
    if (focusId == null) return
    const focusDevice = data?.devices.find((d) => d.id === focusId)
    const fallbackTab = focusDevice ? deviceTabs(focusDevice)[0] : "health"
    setOpen({ id: focusId, tab: focusTab ?? fallbackTab, onu: focusOnuId ?? null })
    setJumpId(null)
  }, [focusId, focusTab, focusOnuId])
  useEffect(() => { setJumpId(null) }, [search])
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
    refetchInterval: 30_000,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const dropsQ = useQuery({
    queryKey: ["drops", scopeOrg],
    queryFn: () => inventoryApi.drops(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const billing = useQuery({
    queryKey: ["billing", scopeOrg],
    queryFn: () => billingApi.get(scopeOrg),
    enabled: !!scopeOrg && canWrite,
  })
  const onuNeedle = useDebounced(search.trim(), 300)
  const onuSearchOn = onuSearchKey(search).length >= ONU_SEARCH_MIN
  const onuFetchOn = onuSearchKey(onuNeedle).length >= ONU_SEARCH_MIN
  const onuHits = useQuery({
    queryKey: ["onu-search", scopeOrg, onuNeedle],
    queryFn: () => inventoryApi.onuSearch(scopeOrg, onuNeedle),
    enabled: !!scopeOrg && onuFetchOn,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const devicesData = data?.devices
  useEffect(() => {
    if (focusId == null || !devicesData) return
    const byId = new Map(devicesData.map((d) => [d.id, d]))
    const ancestors: number[] = []
    const up = (id: number) => {
      const d = byId.get(id)
      if (!d || d.tree_detached === 1 || d.parent_device_id == null) return null
      const p = byId.get(d.parent_device_id)
      if (!p || isPassiveType(p.device_type) !== isPassiveType(d.device_type)) return null
      return d.parent_device_id
    }
    let cur = up(focusId)
    while (cur != null && byId.has(cur) && !ancestors.includes(cur)) {
      ancestors.push(cur)
      cur = up(cur)
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
  const searching = search.trim().length > 0
  const probeFiltered = probeFilter
    ? allDevices.filter((d) => d.assigned_node_id === probeFilter)
    : allDevices
  const statusFilterIds = statusFilter ? new Set(statusFilter.ids) : null
  const statusFiltered = statusFilterIds
    ? probeFiltered.filter((d) => statusFilterIds.has(d.id))
    : probeFiltered
  const tagFiltered = tagFilter.size === 0 ? statusFiltered
    : statusFiltered.filter((d) => [...tagFilter].every(
        (t) => d.tags.some((x) => x.toLowerCase() === t.toLowerCase())))
  const onuData = onuSearchOn ? onuHits.data : undefined
  const onuMatches: OnuSearchMatch[] = onuData?.matches ?? []
  const onuDeviceIds = new Set(onuMatches.map((m) => m.device_id))
  const devices = filterWithAncestors(tagFiltered, search, onuDeviceIds)
  const tagCounts = new Map<string, number>()
  for (const d of allDevices) for (const t of d.tags) {
    tagCounts.set(t, (tagCounts.get(t) ?? 0) + 1)
  }
  const allTags = [...tagCounts.keys()].sort((a, b) => a.localeCompare(b))
  const colors: ColorMaps = {
    tags: data?.tag_colors ?? {},
    nodes: nodes.data?.node_colors ?? {},
  }
  const monitoredCount = allDevices.filter((d) => !isPassiveType(d.device_type)).length
  const deviceCap = billing.data?.device_cap ?? null
  const atCap = deviceCap != null && monitoredCount >= deviceCap
  const gridView = view === "grid"
  const cmp = comparatorFor(sortMode)
  const effectiveCollapsed = gridView || searching ? new Set<number>() : collapsed
  const treeOrdered = treeOrder(devices, effectiveCollapsed, cmp)
  const flatten = (rows: Ordered[]) => (gridView && cmp ? [...rows].sort(cmp) : rows)
  const orderedGear = flatten(treeOrdered.gear)
  const orderedPlant = flatten(treeOrdered.plant)
  const openDevice = open ? devices.find((d) => d.id === open.id) ?? null : null
  const forcePlantOpen = (searching && orderedPlant.length > 0)
    || (openDevice != null && isPassiveType(openDevice.device_type))
  const showPlant = plantOpen || forcePlantOpen
  const plantDrops = dropsQ.data?.recorded ?? null
  const panel = useResizablePanel({
    storageKey: "wisp:network:panelw", defaultWidth: 420, min: 340, max: 760,
    open: !!openDevice,
  })
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
  const closeForm = () => { setFormOpen(false); setEditing(null) }

  type Ordered = OrgDevice & { depth: number; descendantCount: number }
  const renderList = (list: Ordered[]) => (
    <Card className="@container gap-0 overflow-hidden py-0">
      {list.map((d) => (
        <Fragment key={d.id}>
          <DeviceRow device={d} canWrite={canWrite} onEdit={openEdit}
            collapsed={collapsed.has(d.id)} onToggleCollapse={() => toggleCollapse(d.id)}
            focus={d.id === (jumpId ?? focusId)} drill={drillFor(d)}
            parentName={d.parent_device_id != null ? nameById.get(d.parent_device_id) : undefined}
            colors={colors} />
          {formOpen && editing?.id === d.id && (
            <div className="border-t bg-muted/30 p-3">
              <DeviceForm org={scopeOrg} editing={editing} devices={allDevices} nodeIds={nodeIds} onDone={closeForm} />
            </div>
          )}
        </Fragment>
      ))}
    </Card>
  )
  const renderGrid = (list: Ordered[]) => (
    <div className="@container">
      <div className="grid grid-cols-1 gap-2 @lg:grid-cols-2 @4xl:grid-cols-3">
      {list.map((d) => (
        <Fragment key={d.id}>
          <DeviceCard device={d} canWrite={canWrite} onEdit={openEdit}
            focus={d.id === (jumpId ?? focusId)}
            drill={drillFor(d)}
            parentName={d.parent_device_id != null ? nameById.get(d.parent_device_id) : undefined}
            colors={colors} />
          {formOpen && editing?.id === d.id && (
            <div className="col-span-full rounded-lg border bg-muted/30 p-3">
              <DeviceForm org={scopeOrg} editing={editing} devices={allDevices} nodeIds={nodeIds} onDone={closeForm} />
            </div>
          )}
        </Fragment>
      ))}
      </div>
    </div>
  )

  return (
    <div className="wisp-tree-page mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6" style={panel.vars}>
      <div className="wisp-panel-clear flex items-center justify-between">
        <h1 className="text-base font-semibold">Network</h1>
        <ViewToggle view={view} onChange={changeView} />
      </div>

      <ProbesPanel org={scopeOrg} canWrite={canWrite} view={view} deviceCounts={deviceCounts}
        probeFilter={probeFilter} onProbeFilter={setProbeFilter} />

      <section className="flex flex-col gap-2">
        <div className="wisp-panel-clear flex items-center justify-between">
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
            {statusFilter && (
              <button
                className="flex items-center gap-1.5 self-center rounded-full border bg-card px-2.5 py-0.5 text-2xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                title="Filtered from the Overview page. Click to clear"
                onClick={() => setStatusFilter(null)}>
                {statusFilter.label}
                <X className="size-3" />
              </button>
            )}
            {[...tagFilter].map((t) => (
              <button key={t}
                className="flex items-center gap-1.5 self-center rounded-full border bg-card px-2.5 py-0.5 text-2xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                title="Filtering by this tag. Click to clear"
                onClick={() => toggleTag(t)}>
                <Tags className="size-3" />
                {t}
                <X className="size-3" />
              </button>
            ))}
            {(down > 0 || degraded > 0) && (
              <p className="text-xs">
                {down > 0 && <span className="font-semibold text-destructive">{down} down</span>}
                {down > 0 && degraded > 0 && <span className="text-muted-foreground"> · </span>}
                {degraded > 0 && <span className="font-semibold text-warning">{degraded} degraded</span>}
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input ref={searchRef} value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder="Find device or ONU…" aria-label="Find device or ONU"
                title="Device name, IP, type, region or tag, plus any ONU MAC or name. Punctuation optional."
                className="h-8 w-40 pl-7 text-xs md:w-64" />
              {search && (
                <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  aria-label="Clear search" onClick={() => setSearch("")}>
                  <X className="size-3.5" />
                </button>
              )}
            </div>
            <Select value={sortMode} onValueChange={(v) => changeSort(v as SortMode)}>
              <SelectTrigger className="h-8 w-32 text-xs" aria-label="Sort devices">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Sort: Recent</SelectItem>
                <SelectItem value="ip">Sort: IP</SelectItem>
                <SelectItem value="type">Sort: Type</SelectItem>
              </SelectContent>
            </Select>
            {allTags.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant={tagFilter.size > 0 ? "secondary" : "ghost"} size="sm"
                    className={cn(tagFilter.size === 0 && "text-muted-foreground")}
                    title="Filter by tags (a device must carry every selected tag)">
                    <Tags className="size-3.5" /> Tags{tagFilter.size > 0 && ` · ${tagFilter.size}`}
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end"
                  className="max-h-80 w-auto min-w-52 overflow-y-auto">
                  {allTags.map((t) => (
                    <DropdownMenuCheckboxItem key={t} checked={tagFilter.has(t)}
                      onCheckedChange={() => toggleTag(t)}
                      onSelect={(e) => e.preventDefault()}>
                      {colors.tags[t] && (
                        <span aria-hidden className="size-2 shrink-0 rounded-full"
                          style={{ background: paletteVarOf(colors.tags[t]) ?? undefined }} />
                      )}
                      {t}
                      <span className="ml-auto pl-3 text-2xs text-muted-foreground">{tagCounts.get(t)}</span>
                    </DropdownMenuCheckboxItem>
                  ))}
                  {(tagFilter.size > 0 || canWrite) && <DropdownMenuSeparator />}
                  {canWrite && (
                    <DropdownMenuItem onClick={() => setTagColorsOpen(true)}>
                      <Palette /> Tag colours…
                    </DropdownMenuItem>
                  )}
                  {tagFilter.size > 0 && (
                    <DropdownMenuItem onClick={() => setTagFilter(new Set())}>
                      <X /> Clear tag filter
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {canWrite && !formOpen && (
              <Button variant="ghost" size="sm" className="text-muted-foreground"
                onClick={() => { setEditing(null); setFormOpen(true) }}>
                <Plus className="size-3.5" /> Add device
              </Button>
            )}
            <TagColorsDialog org={scopeOrg} tags={allTags} colors={colors.tags}
              counts={tagCounts} open={tagColorsOpen} onOpenChange={setTagColorsOpen} />
          </div>
        </div>

        {formOpen && !editing && (
          atCap
            ? <UpgradeNotice billing={billing.data!} resource="device"
                note="Splitters don't count toward the limit. Record them from the map."
                onClose={closeForm} />
            : <DeviceForm org={scopeOrg} editing={null} devices={allDevices} nodeIds={nodeIds}
                onDone={closeForm} />
        )}

        {isLoading && <Skeleton className="h-40 w-full" />}
        {searching && onuSearchOn && (
          <OnuMatchList matches={onuMatches} truncated={onuData?.truncated ?? false}
            loading={onuHits.isLoading} onOpen={openOnu} />
        )}
        {!isLoading && devices.length === 0 && (
          <p className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
            {searching ? `No devices or ONUs match “${search.trim()}”.`
              : tagFilter.size > 0 ? "No devices carry all the selected tags."
              : probeFilter ? `No devices assigned to ${probeFilter}.`
              : statusFilter ? `No devices match “${statusFilter.label}”.`
              : isWorker ? NO_ASSIGNED_DEVICES
              : "No devices yet. Add one above."}
          </p>
        )}
        {orderedGear.length > 0 && (gridView ? renderGrid(orderedGear) : renderList(orderedGear))}
        {orderedPlant.length > 0 && (
          <>
            <div className="mt-3 flex items-center gap-2">
              <button type="button"
                onClick={() => { if (!forcePlantOpen) changePlantOpen(!plantOpen) }}
                aria-expanded={showPlant}
                className={cn("flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 py-1 text-left",
                  !forcePlantOpen && "hover:bg-foreground/5")}>
                <ChevronRight aria-hidden className={cn(
                  "size-3.5 shrink-0 text-muted-foreground transition-transform",
                  showPlant && "rotate-90", forcePlantOpen && "opacity-40")} />
                <span className="wisp-eyebrow shrink-0">Passive plant</span>
                <span className="shrink-0 text-2xs text-faint-foreground">
                  {orderedPlant.length} recorded
                  {plantDrops != null && <> · {plantDrops} subscriber drops</>}
                  {" · never probed"}
                </span>
                <span aria-hidden className="h-px flex-1 bg-border" />
              </button>
              {canWrite && (
                <Link to="/map"
                  className="shrink-0 rounded-md px-1.5 py-1 text-2xs text-muted-foreground hover:bg-foreground/5 hover:text-foreground">
                  Add on the map
                </Link>
              )}
            </div>
            {showPlant && (gridView ? renderGrid(orderedPlant) : renderList(orderedPlant))}
          </>
        )}
      </section>

      {openDevice && (
        <div aria-hidden
          className="wisp-tree-scrim pointer-events-none fixed inset-0 z-[35] bg-black/25 dark:bg-black/45" />
      )}
      {openDevice && (
        <Card className="wisp-device-panel fixed inset-x-2 bottom-[4.5rem] z-40 flex max-h-[62%] flex-col gap-0 overflow-hidden border-border-strong bg-popover py-0 md:inset-x-auto md:top-[4.25rem] md:right-3 md:bottom-auto md:max-h-[calc(100vh-5.5rem)]">
          <PanelResizeGrip grip={panel.grip} />
          <DevicePanelHeader device={openDevice}
            tone={!openDevice.assigned_node_id && !isPassiveType(openDevice.device_type)
              ? "muted" : deviceTone(openDevice.state, openDevice.state_updated_at)}>
            <Button variant="ghost" size="icon" className="size-6 text-muted-foreground"
              title="Close (Esc)" onClick={() => setOpen(null)}>
              <X className="size-3.5" />
            </Button>
          </DevicePanelHeader>
          <div className="overflow-y-auto overscroll-contain p-3">
            <DeviceDetail device={openDevice} tab={open!.tab} focusOnuId={open!.onu ?? null}
              onTab={(t) => setOpen((o) => (o ? { ...o, tab: t } : o))} />
          </div>
        </Card>
      )}
    </div>
  )
}
