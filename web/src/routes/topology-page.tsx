import { Fragment, useEffect, useRef, useState } from "react"
import { useLocation } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowUpFromLine, ChevronRight, CornerDownRight, CornerLeftUp, Gauge, MoreVertical, Palette, Pencil, Plus, Radio, ScanSearch, Search, Tags, Trash2, Waypoints, Wrench, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDebounced } from "@/hooks/use-debounced"
import { useNow } from "@/hooks/use-now"
import { billingApi, gponApi, inventoryApi, nodesApi, ApiError } from "@/lib/api"
import { DEVICE_TYPES, PASSIVE_DEVICE_TYPES, isPassiveType, type OnuSearchMatch, type OrgDevice } from "@/lib/types"
import { DOT as ONU_DOT, onuSev } from "@/components/optical-panel"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import {
  DeviceDetail, DeviceMetrics, RowTag, deviceTabs, isOpticalOlt,
  VITAL_CPU_CRIT, VITAL_TEMP_CRIT, type DeviceTab,
} from "@/components/device-detail"
import { NeedsOrg } from "@/components/needs-org"
import { RegionSelect } from "@/components/region-select"
import { runSnmpTest } from "@/components/snmp-test"
import { TagsInput } from "@/components/tags-input"
import { ViewToggle, loadView, saveView, type ViewMode } from "@/components/view-toggle"
import { SnmpWalkDialog } from "@/components/snmp-walk-dialog"
import { UpgradeNotice } from "@/components/upgrade-notice"
import { WebUiLiveIcon } from "@/components/web-proxy"
import { StatusDot } from "@/components/status-badge"
import { ColorSwatches } from "@/components/color-swatches"
import { ago, deviceTone, durationSince, isDownState, isFresh, isStale } from "@/lib/format"
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

function treeOrder(
  devices: OrgDevice[], collapsed: Set<number>,
  cmp?: (a: OrgDevice, b: OrgDevice) => number,
): Array<OrgDevice & { depth: number; descendantCount: number }> {
  const ids = new Set(devices.map((d) => d.id))
  // A row sits at the TOP LEVEL when it has no parent, when its parent isn't in
  // the rendered set (filtered out by search), or when the operator lifted it
  // out with `tree_detached` — a presentation flag only: the parent link is
  // untouched, so the map, suppression and paging all still see it. That's the
  // point of it — a device an operator reads often shouldn't be buried inside a
  // big aggregation switch's subtree just because that's where the cable goes.
  const isRoot = (d: OrgDevice) =>
    d.parent_device_id == null || !ids.has(d.parent_device_id) || d.tree_detached === 1
  const children = new Map<number, OrgDevice[]>()
  for (const d of devices) {
    if (isRoot(d)) continue
    const key = d.parent_device_id as number
    if (!children.has(key)) children.set(key, [])
    children.get(key)!.push(d)
  }
  // sibling sort only — the parent-before-child structure never changes
  const sorted = (arr: OrgDevice[]) => (cmp ? [...arr].sort(cmp) : arr)
  const kids = (id: number) => sorted(children.get(id) ?? [])
  const descendantCount = (id: number): number =>
    (children.get(id) ?? []).reduce((sum, k) => sum + 1 + descendantCount(k.id), 0)
  const out: Array<OrgDevice & { depth: number; descendantCount: number }> = []
  const emit = (d: OrgDevice, depth: number) => {
    out.push({ ...d, depth, descendantCount: descendantCount(d.id) })

    if (!collapsed.has(d.id)) for (const k of kids(d.id)) emit(k, depth + 1)
  }
  for (const d of sorted(devices.filter(isRoot))) emit(d, 0)
  return out
}

// Operator colour-coding, resolved per device. Two sources, most specific first:
// a TAG colour states something about this box, a PROBE colour only says which
// probe happens to watch it — so the tag wins and the probe fills in the rest.
// With several coloured tags the FIRST in the device's own tag order wins: the
// order is the operator's, and any other rule (alphabetical, most-used) would
// silently reshuffle colours as tags were added elsewhere.
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

// The rail: a colour bar at the row's left edge, OUTSIDE the tree indent guides,
// so the colours line up in one scannable column whatever the depth. Rendered as
// a border so an uncoloured row reserves the same 3px and nothing shifts.
const railStyle = (color: string | null) => ({ borderLeftColor: color ?? "transparent" })
const RAIL = "border-l-[3px]"

/** The tag palette editor. Reached from the Tags filter menu rather than
 *  Settings: tags are created and read on this page, so their colours belong
 *  where the operator is already looking at them. */
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
            its probe's colour. Status always renders on top — a colour never
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

// Numeric IPv4 key — a string sort puts 10.0.0.9 after 10.0.0.10. Passives and
// anything unparseable sink to the bottom.
function ipKey(d: OrgDevice): number {
  const m = d.ip_address.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/)
  if (!m) return Number.MAX_SAFE_INTEGER
  return ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4]
}

// Type sorts by NETWORK HIERARCHY, not the alphabet: aggregation gear first,
// access gear after (switch above OLT), passive plant at the bottom.
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

// Substring match over the fields an operator actually types; matches keep
// their ancestor chain so the tree renders rooted, not floating. `extraIds` are
// devices matched by something the row itself doesn't carry — today, OLTs whose
// ONU roster holds the searched serial/MAC (resolved server-side) — so the tree
// keeps them visible alongside the ONU result block.
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
  pon_port: string
}

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", tags: [],
  parent_device_id: "",
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
    region: editing.region ?? "", tags: editing.tags ?? [],
    parent_device_id: editing.parent_device_id ? String(editing.parent_device_id) : "",
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
        tags: form.tags,
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
          // Changed SNMP settings get verified immediately: a tiny system walk
          // through the probe answers "does this community/port actually work?"
          // right here instead of a stale panel an hour later. Fire-and-forget —
          // the verdict lives in a toast that outlives the form.
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

// The drill-in open-state (which device is expanded + which tab) lives on the
// page, not per row/card — so opening one device auto-closes any other, and the
// grid can place the panel at the end of the open card's visual row instead of
// shoving its right-hand neighbours onto a new line. A row/card scrolls itself
// into view when it becomes the deep-link focus (Home row / command palette).
interface DrillIn {
  open: boolean
  tab: DeviceTab
  /** ONU to scroll to and highlight inside the Optical tab — set when the panel
      was opened from an ONU serial/MAC search hit, null otherwise. */
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

// Live column count of the device grid — mirrors the `grid-cols-1 sm:grid-cols-2
// xl:grid-cols-3` classes so the parent can find the last card in the open card's
// row (Tailwind sm=640px, xl=1280px). Grid only; the list is one device per line.
function useGridCols(): number {
  const [cols, setCols] = useState(1)
  useEffect(() => {
    const sm = window.matchMedia("(min-width: 640px)")
    const xl = window.matchMedia("(min-width: 1280px)")
    const update = () => setCols(xl.matches ? 3 : sm.matches ? 2 : 1)
    update()
    sm.addEventListener("change", update)
    xl.addEventListener("change", update)
    return () => {
      sm.removeEventListener("change", update)
      xl.removeEventListener("change", update)
    }
  }, [])
  return cols
}

// Server-side floor, mirrored so the client doesn't fire a request it knows
// will come back empty (central/api/devices.py:ONU_SEARCH_MIN). The key must
// match `onuroster.search_key` — punctuation is stripped before the length is
// judged, so "hc_" is 2 characters here and does NOT reach the server.
const ONU_SEARCH_MIN = 3
const onuSearchKey = (s: string) => s.replace(/[^a-z0-9]/gi, "")

// ONU hits for the current search, by serial/MAC or provisioned name. The
// Network tree can only render devices, and an ONU isn't one — but its MAC and
// its name are the identifiers a tech actually holds (off the sticker, off a
// subscriber call), so the hits get their own result block above the list. Each
// row jumps into that OLT's Optical tab focused on the ONU; the OLT itself also
// stays in the tree below.
function OnuMatchList({ matches, truncated, loading, onOpen }: {
  matches: OnuSearchMatch[]
  truncated: boolean
  loading: boolean
  onOpen: (deviceId: number, onuId: number) => void
}) {
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
          <RowTag tone="muted" title="More ONUs match than are shown — type more of the MAC to narrow it">
            capped
          </RowTag>
        )}
      </div>
      {matches.map((m) => (
        <Fragment key={m.device_id}>
          {m.onus.map((o) => (
            <button key={o.id} type="button" onClick={() => onOpen(m.device_id, o.id)}
              title="Open this ONU in its OLT's Optical tab"
              className="flex h-11 w-full items-center gap-2.5 border-b px-4 text-left last:border-b-0 hover:bg-foreground/5">
              <span className={cn("size-2 shrink-0 rounded-full", ONU_DOT[onuSev(o)])} />
              <span className="shrink-0 font-mono text-xs font-medium">
                {o.serial || o.onu_key}
              </span>
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {o.name || <span className="text-faint-foreground">unnamed</span>}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-3 text-2xs text-muted-foreground">
                {/* where it is, in the terms the tech will act on: which OLT,
                    which PON, which slot */}
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
    </Card>
  )
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
  // Suppress EVERY SNMP-derived chip whenever the row itself isn't live — the box
  // is down (its ICMP outage owns the row), or its probe has gone silent (the row
  // is already graying to muted). Either way ports, optics and vitals are a frozen
  // snapshot from before it dropped, and a chip is a claim about NOW: "port down"
  // on an unreachable switch is the outage being reported twice, and "low bw" /
  // "82°C" are alarms about a box that isn't there to be slow or hot. The expanded
  // panel still shows the readings, grayed and stamped (.wisp-frozen) — the row
  // just stops shouting them. Same rule the map pin ring uses.
  const isDown = isDownState(device.state)
  const liveSnmp = !isDown && !isStale(device.state_updated_at)
  const opticsChips = hasOptics && liveSnmp
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
      {/* Owner-assigned tags are deliberately NOT chipped here (operator ask,
          2026-07-22): every chip on this row is a claim about the device's
          state, and a row of organisational labels alongside them is noise to
          scan past. Tags still drive the colour RAIL (deviceColor), the filter
          menu and search — they're just not spelled out per row. */}
      {liveSnmp && device.ports_down > 0 && (
        <RowTag tone="destructive" title="A watched port is down. Click for ports"
          onClick={(e) => { e.stopPropagation(); openTab("ports") }}>
          {device.ports_down === 1 ? "port down" : `${device.ports_down} ports down`}
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
      {collapsed && (device.descendantCount ?? 0) > 0 && <RowTag tone="muted">+{device.descendantCount}</RowTag>}
    </>
  )
}

// Capability indicators (optical / dBm / SNMP ports): they just say what this
// device supports, tinted by the same freshness rule as the Overview — red on
// alarm, amber on warn, green when a fresh reading is landing, dim when
// configured but silent (no data yet / gone stale). Trouble beats working.
function DeviceCapabilityIcons({ device, hasOptics, hasPorts }: {
  device: OrgDevice; hasOptics: boolean; hasPorts: boolean
}) {
  if (!hasOptics && !hasPorts) return null
  const opticsFresh = isFresh(device.optics_updated_at)
  const portsFresh = isFresh(device.ports_updated_at)
  // Whether per-ONU OPTICAL POWER is landing, which the optics icon beside it
  // cannot answer: a C-Data/DBC OLT walks a complete roster with every rx_dbm
  // NULL, so that icon goes green on a box measuring no light at all. Both
  // halves are needed — a count with no fresh walk behind it is a memory.
  const rxCount = device.onus_rx ?? 0
  const hasRx = rxCount > 0 && opticsFresh
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
      {/* dBm — deliberately NOT tinted by severity. This icon answers "is
          optical power being measured here", and a missing measurement is a
          coverage gap, not an alarm (the same reason stale chips render
          neutral). Severity already has the icon to its left. */}
      {hasOptics && (
        <span title={hasRx
          ? `Per-ONU dBm: ${rxCount} ONU${rxCount === 1 ? "" : "s"} reporting optical power`
          : rxCount > 0
            ? "Per-ONU dBm: readings have gone stale — the optical walk stopped"
            : "Per-ONU dBm: none. This OLT reports no optical power — open the Optical tab for why"}>
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

// The per-device actions menu (edit / SNMP walk / maintenance / delete) plus its
// dialogs — shared by row and card so the mutations live in one place.
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
  // Tree placement only — see treeOrder. Worth saying out loud in the toast:
  // an operator reaching for this is one keystroke away from thinking they
  // just re-cabled the plant.
  const toggleDetached = useMutation({
    mutationFn: () => inventoryApi.setTreeDetached(device.id, !device.tree_detached),
    onSuccess: () => {
      invalidate()
      toast.success(device.tree_detached
        ? `${device.name} nests under ${parentName ?? "its parent"} again`
        : `${device.name} moved to the top level of the tree`,
        { description: "View only — the parent link, alerting and map are unchanged." })
    },
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
          {/* w-auto overrides the primitive's default width-of-the-trigger: the
              trigger is a size-6 icon, so every item wrapped at min-w-32 */}
          <DropdownMenuContent align="end" className="w-auto min-w-52">
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
            {/* max-w + truncate: real parent names run long (HALIYA-LOCAL-CH-SW)
                and wrapped the item over four lines */}
            {device.parent_device_id != null && (
              <DropdownMenuItem onClick={() => toggleDetached.mutate()}
                className="max-w-72"
                title={(device.tree_detached ? `Nest under ${parentName ?? "parent"}. ` : "")
                  + "Network tree only — the parent link, alerting and map are unchanged"}>
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
  const { open: detailOpen, tab: detailTab, focusOnu, onToggle, onTab: setDetailTab, openTab } = drill
  const ref = useFocusScroll(focus)
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
          RAIL, detailOpen && "border-b")}
        style={railStyle(deviceColor(device, colors))}
        onClick={onToggle}
        title={detailOpen ? undefined : "Click for details"}
      >
        {Array.from({ length: device.depth }).map((_, i) => (
          <span key={i} aria-hidden className="w-3 shrink-0 self-stretch border-l sm:w-4" />
        ))}
        {/* children AS RENDERED, not child_count: a parent whose only children
            were lifted to the top level has nothing left to expand */}
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
          <span className="hidden shrink-0 text-xs text-faint-foreground lg:inline">{device.device_type}</span>
        )}
        {/* A lifted row is the one place the tree stops showing where a device
            actually hangs — so it says so, right on the row. */}
        {device.tree_detached === 1 && parentName && (
          <span className="hidden min-w-0 shrink items-center gap-1 text-xs text-faint-foreground sm:inline-flex"
            title={`Hangs off ${parentName} — shown at the top level for readability`}>
            <CornerLeftUp className="size-3 shrink-0" />
            <span className="truncate">{parentName}</span>
          </span>
        )}
        <DeviceChips device={device} hasOptics={hasOptics} collapsed={collapsed} openTab={openTab} />
        <div className="ml-auto flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <DeviceMetrics device={device} />
          <span className="hidden font-mono text-xs text-muted-foreground md:inline">{device.ip_address}</span>
          <WebUiLiveIcon device={device} />
          <DeviceCapabilityIcons device={device} hasOptics={hasOptics} hasPorts={hasPorts} />
          <DeviceActions device={device} canWrite={canWrite} onEdit={onEdit} parentName={parentName} />
        </div>
      </div>
      {detailOpen && (
        <div className="px-3 pt-1 pb-3">
          <DeviceDetail device={device} tab={detailTab} onTab={setDetailTab} focusOnuId={focusOnu} />
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

  // The expanded detail is rendered by the grid at the end of this card's visual
  // row (see TopologyPage) — inserting it right here would push the cards to this
  // card's right onto the next line. The card itself only reflects open-ness.
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
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t pt-2">
          <DeviceMetrics device={device} />
          <DeviceChips device={device} hasOptics={hasOptics} openTab={openTab} />
          <div className="ml-auto flex items-center gap-1.5">
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
    /* private mode / quota — keep the in-memory state, just don't persist */
  }
}

// Sort preference, persisted like the view toggle (a UI taste).
const SORT_KEY = "wisp:network:sort"

function loadSort(): SortMode {
  try {
    const v = localStorage.getItem(SORT_KEY)
    // old stored values ("name"/"status", removed 2026-07-20) degrade to default
    return v === "ip" || v === "type" ? v : "default"
  } catch {
    return "default"
  }
}

export function TopologyPage() {
  const { scopeOrg, canWrite } = useAuth()
  const location = useLocation()
  const navState = location.state as
    { deviceId?: number; probeId?: string; tab?: DeviceTab; onuId?: number
      /** Home KPI tile deep-link: pre-filters the device list to exactly the
       *  devices that tile counts, labeled for the clearable chip below. */
      statusFilter?: { label: string; ids: number[] } } | null
  const focusId = navState?.deviceId
  // A deep-link may target a specific tab/ONU (the command palette's ONU hits
  // open the Optical tab focused on the ONU); read as primitives so the effect
  // re-fires when the target changes even if the OLT id repeats.
  const focusTab = navState?.tab
  const focusOnuId = navState?.onuId
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<OrgDevice | null>(null)
  // set when a capped org chooses "Add passive plant" — bypasses the upgrade
  // notice into the real form (passives never count against the device cap).
  const [forceForm, setForceForm] = useState(false)
  const [collapsed, setCollapsed] = useState<Set<number>>(() => loadCollapsed(scopeOrg))
  const [probeFilter, setProbeFilter] = useState<string | null>(navState?.probeId ?? null)
  const [statusFilter, setStatusFilter] = useState<{ label: string; ids: number[] } | null>(
    navState?.statusFilter ?? null)
  const [view, setView] = useState<ViewMode>(loadView)
  const [search, setSearch] = useState("")
  const [sortMode, setSortMode] = useState<SortMode>(loadSort)
  // active tag filter — a device must carry EVERY selected tag (narrowing)
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set())
  const [tagColorsOpen, setTagColorsOpen] = useState(false)
  // Which device is drilled in, page-wide — one at a time, so opening another
  // auto-closes it. A row/card gets a controlled `DrillIn` derived from this.
  const [open, setOpen] = useState<{ id: number; tab: DeviceTab; onu?: number | null } | null>(null)
  // Device an ONU search hit asked us to scroll to — kept apart from the
  // router's deep-link focusId so a search jump can't fight a navigation.
  const [jumpId, setJumpId] = useState<number | null>(null)
  const cols = useGridCols()
  // Fresh-open lands on the device's own first tab (optical, for an OLT) —
  // deviceTabs() is the one place that order is decided.
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
  // An ONU hit lands on its OLT's Optical tab with that ONU highlighted — the
  // one place its Rx, ranging distance and dark-time already live.
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
  const toggleTag = (t: string) => setTagFilter((prev) => {
    const next = new Set(prev)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    return next
  })

  useEffect(() => { setCollapsed(loadCollapsed(scopeOrg)) }, [scopeOrg])
  useEffect(() => { setTagFilter(new Set()) }, [scopeOrg])
  // arriving from a stale-probe card while already mounted
  useEffect(() => { if (navState?.probeId) setProbeFilter(navState.probeId) }, [navState?.probeId])
  // arriving from a Home KPI tile while already mounted
  useEffect(() => {
    if (navState?.statusFilter) setStatusFilter(navState.statusFilter)
  }, [navState?.statusFilter])
  // deep-link (Home row / command palette) opens the target's panel — on the
  // Optical tab focused on an ONU when the palette handed us one, else the
  // device's own first tab (optical, for an OLT).
  useEffect(() => {
    if (focusId == null) return
    // Reads the CURRENT device list without depending on it — this must fire
    // only on navigation (deps below), not on the 30s inventory poll, or it'd
    // repeatedly re-open/re-focus the panel out from under the user.
    const focusDevice = data?.devices.find((d) => d.id === focusId)
    const fallbackTab = focusDevice ? deviceTabs(focusDevice)[0] : "health"
    setOpen({ id: focusId, tab: focusTab ?? fallbackTab, onu: focusOnuId ?? null })
    // a navigation outranks a leftover search jump, or `jumpId ?? focusId` would
    // pin the scroll to whatever ONU hit was opened last
    setJumpId(null)
  }, [focusId, focusTab, focusOnuId])
  // retyping invalidates the previous jump target (openOnu re-sets it after)
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
  // ONU search, by serial/MAC or provisioned name. Debounced and floored at 3
  // characters because it scans the org's whole onu_optics table — a
  // keystroke-per-query would walk it on every letter of a device-name search.
  // Matching is punctuation-blind server-side, so the tech can type the tail of
  // a sticker with or without colons, and "hc_kiran" as "hc kiran".
  const onuNeedle = useDebounced(search.trim(), 300)
  // Two gates, deliberately on different clocks: SHOWING hits keys off the live
  // text so backspacing below the floor drops them at once, while FETCHING keys
  // off the settled text so we don't fire mid-word.
  const onuSearchOn = onuSearchKey(search).length >= ONU_SEARCH_MIN
  const onuFetchOn = onuSearchKey(onuNeedle).length >= ONU_SEARCH_MIN
  const onuHits = useQuery({
    queryKey: ["onu-search", scopeOrg, onuNeedle],
    queryFn: () => inventoryApi.onuSearch(scopeOrg, onuNeedle),
    enabled: !!scopeOrg && onuFetchOn,
    // Rosters move on the SNMP sweep (300s), not per second — no need to refetch
    // an open result list, and it keeps backspacing through a MAC off the wire.
    staleTime: 60_000,
    // Hold the previous needle's hits while the next one settles: without this
    // every keystroke past the 3-char floor blanks the block to a skeleton and
    // drops the matched OLT out of the tree for a frame.
    placeholderData: (prev) => prev,
  })

  // A deep-linked device may sit under collapsed ancestors — open the path to it
  // (in memory only; a landing shouldn't rewrite the user's saved collapse prefs).
  const devicesData = data?.devices
  useEffect(() => {
    if (focusId == null || !devicesData) return
    const byId = new Map(devicesData.map((d) => [d.id, d]))
    const ancestors: number[] = []
    // walk the RENDERED chain: a detached device is already at the top level,
    // so expanding the subtree it was lifted out of would reach nothing
    const up = (id: number) => {
      const d = byId.get(id)
      return d && d.tree_detached !== 1 ? d.parent_device_id : null
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
  // The tag/probe filters still apply on top of ONU hits — an OLT the operator
  // has filtered out stays out, ONU match or not.
  const onuData = onuSearchOn ? onuHits.data : undefined
  const onuMatches: OnuSearchMatch[] = onuData?.matches ?? []
  const onuDeviceIds = new Set(onuMatches.map((m) => m.device_id))
  const devices = filterWithAncestors(tagFiltered, search, onuDeviceIds)
  // every tag in use org-wide, with device counts, for the filter menu
  const tagCounts = new Map<string, number>()
  for (const d of allDevices) for (const t of d.tags) {
    tagCounts.set(t, (tagCounts.get(t) ?? 0) + 1)
  }
  const allTags = [...tagCounts.keys()].sort((a, b) => a.localeCompare(b))
  const colors: ColorMaps = {
    tags: data?.tag_colors ?? {},
    nodes: nodes.data?.node_colors ?? {},
  }
  // The cap counts monitored (non-passive) devices, matching the server; compute
  // the live count off the list so an add/delete reflects without refetching.
  const monitoredCount = allDevices.filter((d) => !isPassiveType(d.device_type)).length
  const deviceCap = billing.data?.device_cap ?? null
  const atCap = deviceCap != null && monitoredCount >= deviceCap
  const gridView = view === "grid"
  const cmp = comparatorFor(sortMode)
  // grid flattens the tree (a card grid can't carry indent/collapse); parent-
  // before-child order still groups sensibly and each card names its parent.
  // While searching, collapse is ignored too — a match under a collapsed parent
  // must not be invisible.
  const effectiveCollapsed = gridView || searching ? new Set<number>() : collapsed
  // List view sorts SIBLINGS (parent-before-child structure is the point of the
  // tree); grid view has no hierarchy semantics, so an active sort there orders
  // the whole flat list — sort-by-IP reads as one ascending scan.
  const treeOrdered = treeOrder(devices, effectiveCollapsed, cmp)
  const ordered = gridView && cmp ? [...treeOrdered].sort(cmp) : treeOrdered
  const nameById = new Map(allDevices.map((d) => [d.id, d.name]))
  const activeNodes = (nodes.data?.nodes ?? []).filter((n) => !n.revoked_at)
  const nodeIds = activeNodes.map((n) => n.node_id)

  const fresh = devices.filter((d) => d.assigned_node_id && d.state && !isStale(d.state_updated_at))
  const down = fresh.filter((d) => d.state === "DOWN" || d.state === "UNREACHABLE").length
  const degraded = fresh.filter((d) => d.state === "DEGRADED").length

  const openEdit = (d: OrgDevice) => { setEditing(d); setFormOpen(true) }
  const closeForm = () => { setFormOpen(false); setEditing(null); setForceForm(false) }

  type Ordered = OrgDevice & { depth: number; descendantCount: number }
  const renderList = (list: Ordered[]) => (
    <Card className="gap-0 overflow-hidden py-0">
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
  // Grid drill-in: place the open card's detail after the LAST card in its visual
  // row, so its right-hand neighbours keep their row instead of jumping a line.
  const renderGrid = (list: Ordered[]) => {
    const openIndex = open ? list.findIndex((d) => d.id === open.id) : -1
    const openRowEnd = openIndex < 0 ? -1
      : Math.min(Math.floor(openIndex / cols) * cols + cols - 1, list.length - 1)
    const openDevice = openIndex < 0 ? null : list[openIndex]
    return (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {list.map((d, i) => (
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
            {i === openRowEnd && openDevice && (
              <div className="col-span-full">
                <div className="wisp-drillin px-3 pt-1 pb-3">
                  <DeviceDetail device={openDevice} tab={open!.tab} focusOnuId={open!.onu ?? null}
                    onTab={(t) => setOpen((o) => (o ? { ...o, tab: t } : o))} />
                </div>
              </div>
            )}
          </Fragment>
        ))}
      </div>
    )
  }

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold">Network</h1>
        <ViewToggle view={view} onChange={changeView} />
      </div>

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
              <Input value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder="Find device or ONU…" aria-label="Find device or ONU"
                title="Device name, IP, type, region or tag — plus any ONU MAC or name, punctuation optional"
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
                      // keep the menu open — picking several tags is the point
                      onSelect={(e) => e.preventDefault()}>
                      {/* the tag's colour, so the mapping is legible from the
                          same menu you filter in */}
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

        {/* Add uses the top form (no row to attach to); edit renders inline at its row. */}
        {formOpen && !editing && (
          atCap && !forceForm
            ? <UpgradeNotice billing={billing.data!} resource="device"
                note="Passive plant (splitters, FDBs, closures) doesn't count toward the limit."
                secondary={{ label: "Add passive plant", onClick: () => setForceForm(true) }}
                onClose={closeForm} />
            : <DeviceForm org={scopeOrg} editing={null} devices={allDevices} nodeIds={nodeIds}
                onDone={closeForm} initialType={forceForm ? "splitter" : undefined} />
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
              : "No devices yet. Add one above."}
          </p>
        )}
        {devices.length > 0 && (gridView ? renderGrid(ordered) : renderList(ordered))}
      </section>
    </div>
  )
}
