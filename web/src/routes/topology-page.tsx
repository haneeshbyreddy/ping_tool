import { Fragment, useEffect, useMemo, useRef, useState } from "react"
import { Link, useLocation } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowUpFromLine, ChevronRight, CornerDownRight, CornerLeftUp, Gauge, MoreVertical, Palette, Pencil, Plus, Radio, ScanSearch, Scissors, Search, Tags, Trash2, Waypoints, Wrench, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDebounced } from "@/hooks/use-debounced"
import { useNow } from "@/hooks/use-now"
import { usePonOptions } from "@/hooks/use-pon-options"
import { PanelResizeGrip, useResizablePanel } from "@/hooks/use-resizable-panel"
import { billingApi, gponApi, inventoryApi, nodesApi, ApiError } from "@/lib/api"
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
  ago, deviceTone, durationSince, isDownState, isFresh, isStale, onuName, onuSearchKey,
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

/** The list in two blocks: the monitored GEAR tree, then the PASSIVE PLANT.
 *  Plant is reference material — it has no state, no probe and nothing that can
 *  alarm — so mixing splitters in among the boxes that DO makes the operator
 *  scan past them on the one screen that exists to show trouble. It sorts to the
 *  bottom under its own divider instead. A splitter CASCADE keeps nesting down
 *  there (that hierarchy is real and the drop panel reads it); what's dropped is
 *  only the passive-under-gear step, so a plant row at depth 0 names the box
 *  that feeds it the same way a `tree_detached` row does. */
function treeOrder(
  devices: OrgDevice[], collapsed: Set<number>,
  cmp?: (a: OrgDevice, b: OrgDevice) => number,
): { gear: TreeRow[]; plant: TreeRow[] } {
  const byId = new Map(devices.map((d) => [d.id, d]))
  const passive = (d: OrgDevice) => isPassiveType(d.device_type)
  // A row sits at the TOP LEVEL of its block when it has no parent, when its
  // parent isn't in the rendered set (filtered out by search), when the operator
  // lifted it out with `tree_detached` — a presentation flag only: the parent
  // link is untouched, so the map, suppression and paging all still see it, and
  // a device an operator reads often shouldn't be buried inside a big
  // aggregation switch's subtree just because that's where the cable goes — or
  // when it doesn't belong to the same block as its parent (plant under gear).
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
  // sibling sort only — the parent-before-child structure never changes
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
  /** passive plant only, as a string ("" = not recorded) */
  split_ratio: string
  /** passive plant only: fibres feeding it, as a string ("" = one) */
  split_inputs: string
  /** OLT only, as a string for the Select ("" = not set → the global cap) */
  onu_pon_limit: string
}

// …and "not recorded" on the PON-port Select, for the same reason
const NO_PON = "__nopon__"
// …and so does "not set" on the PON-type Select, for the same reason
const NO_PON_TYPE = "__default__"
// The two standards, as ONU-per-PON caps: EPON tops out at a 1:64 split, GPON at
// 1:128. Named rather than typed in as a number because the operator knows the
// box by its standard, and a cap they can't name is one they can't check. Any
// other stored value still round-trips (see PON_TYPES' "custom" fallback) —
// org_devices.onu_pon_limit stays a plain integer, so a 1:16 or 1:32 build set
// through the API is preserved rather than silently rounded to one of these.
const PON_TYPES = [
  { cap: 64, label: "EPON · 1:64" },
  { cap: 128, label: "GPON · 1:128" },
]

const EMPTY_FORM: DeviceFormState = {
  name: "", ip_address: "", device_type: "", region: "", tags: [],
  parent_device_id: "",
  assigned_node_id: "", snmp_enabled: false, snmp_community: "", snmp_port: "161",
  gpon_vendor: "", pon_port: "", split_ratio: "", split_inputs: "", onu_pon_limit: "",
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
    pon_port: editing.pon_port ?? "",
    split_ratio: editing.split_ratio ? String(editing.split_ratio) : "",
    split_inputs: editing.split_inputs ? String(editing.split_inputs) : "",
    onu_pon_limit: editing.onu_pon_limit ? String(editing.onu_pon_limit) : "",
  } : { ...EMPTY_FORM })
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
    // …and whatever this OLT already carries, even if that profile has since
    // been disabled or deleted: a Select with no item for its own value renders
    // blank, and saving that blank would silently unstamp the vendor.
    ...(form.gpon_vendor ? [form.gpon_vendor] : []),
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
  // Keyed on the ROW being edited, not on the live Select value: gating the
  // passive options on `passive` would make them vanish the moment somebody
  // picked a gear type, leaving no way back to "splitter" without cancelling.
  const editingPassive = editing != null && isPassiveType(editing.device_type)
  const plantTypes: string[] = !editingPassive ? []
    : editing!.device_type && editing!.device_type !== "splitter"
      ? ["splitter", editing!.device_type]
      : ["splitter"]
  // Which OLT's PON labels this box may be bound to: the one at the head of its
  // parent chain, resolved live off the form's own parent field so changing the
  // parent changes the list. Passives only — nothing else has a PON.
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const ponParent = form.parent_device_id ? byId.get(Number(form.parent_device_id)) ?? null : null
  const ponOlt = passive ? oltHead(ponParent, byId) : null
  const { pons, loading: ponsLoading } = usePonOptions(ponOlt?.id, passive)
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
        // Rides the payload for the same reason split_ratio does: the server
        // reads an absent key as "not set", so leaving it out would drop a
        // GPON box back to the EPON cap every time somebody renamed it.
        onu_pon_limit: form.device_type === "OLT" && form.onu_pon_limit
          ? Number(form.onu_pon_limit) : null,
        pon_port: passive ? (form.pon_port.trim() || null) : null,
        // MUST ride the payload: clean_device_payload reads an absent key as
        // "not recorded", so leaving it out would clear a ratio set from the
        // splitter's own panel every time somebody renamed the box here.
        split_ratio: passive && form.split_ratio ? Number(form.split_ratio) : null,
        // Same rule, same reason: an absent key reads as "one input", so a
        // rename here would quietly downgrade a 2:16 recorded on the map.
        split_inputs: passive && form.split_inputs ? Number(form.split_inputs) : null,
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
              // A DROPDOWN off the OLT at the head of this box's chain, not free
              // text. What gets typed here has to match what the SNMP walk
              // stores, exactly — `EPON0/4` written as `0/4` binds the splitter
              // to a port no roster reports, and the only symptom is an empty
              // customer picker on a different screen days later. Only the walk
              // knows how that OLT spells its ports, so only the walk may
              // supply the vocabulary.
              //
              // Re-parenting the box re-keys the query, so the list follows the
              // chain without anything to press.
              ponOlt && pons.length > 0 ? (
                <>
                  <Select value={form.pon_port || NO_PON}
                    onValueChange={(v) => setForm({ ...form, pon_port: v === NO_PON ? "" : v })}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Not recorded" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_PON}>Not recorded</SelectItem>
                      {/* the stored value is ALWAYS an option, listed or not —
                          a Select with no item for its own value renders blank,
                          and saving that blank unstamps the PON */}
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
                // No OLT up the chain, or that OLT has no roster yet. A text box
                // is the honest fallback rather than an empty dropdown, which
                // would read as "this box has no PONs" when the truth is that
                // nothing has walked it.
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
                {/* Plant is not CREATED here any more (operator's call,
                    2026-08-05): a splitter's defining facts are where it is and
                    what feeds it, and this form can state neither — it asked for
                    a PON as free text and a parent out of a flat list, which is
                    how a fleet ended up with one splitter and no drops. It is
                    recorded on the map or in the survey, where the coordinate
                    comes free and the feeder is inferred from it.

                    A type stays offered while EDITING AN EXISTING PASSIVE, and
                    that is load-bearing rather than a leftover: a Select with no
                    item for its own value renders BLANK, and saving that blank
                    would silently unstamp the device_type of a splitter somebody
                    opened to rename. Same trap the GPON vendor dropdown already
                    had to be fixed for.

                    `plantTypes` is `splitter` PLUS the row's own type when that
                    is something else — an `fdb` or `closure` recorded before the
                    creatable set was narrowed is still a real row, and it must
                    stay editable without being silently retyped. */}
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
                {/* Monitored gear may not hang below plant — a passive has no
                    FSM, so suppression through it is undefined and the server
                    422s. Offering it anyway is how a save fails on a rule the
                    form knew all along, so the list narrows to what can
                    actually be saved. Passive-under-passive stays offered: a
                    cascade is the point of a distribution network. */}
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
          {/* PON type is the ONU-per-PON cap — what "PON at capacity" is judged
              against (onuroster.capacity_faults). Nothing detects it: the split
              standard isn't in any MIB we walk, so this is the operator's claim,
              and leaving it unset keeps the server's global cap rather than
              guessing EPON for a 1:128 box. */}
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
                  {/* a cap set outside this menu stays selectable, or saving an
                      unrelated edit would quietly overwrite it */}
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

// The drill-in open-state (which device is selected + which tab) lives on the
// page, not per row/card — one device at a time, and the page renders its
// DeviceDetail once in the side panel rather than each row rendering its own.
// A row/card scrolls itself into view when it becomes the deep-link focus
// (Home row, map, WhatsApp deep-link).
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

// Server-side floor, mirrored so the client doesn't fire a request it knows
// will come back empty (central/api/devices.py:ONU_SEARCH_MIN). The key must
// match `onuroster.search_key` — punctuation is stripped before the length is
// judged, so "hc_" is 2 characters here and does NOT reach the server.
// (the key itself lives in lib/format beside onuName — the map's search box
// judges the same needle, and two spellings of "what counts as a character"
// would disagree about which searches are worth sending)
const ONU_SEARCH_MIN = 3

// ONU hits for the current search, by serial/MAC or provisioned name. The
// Network tree can only render devices, and an ONU isn't one — but its MAC and
// its name are the identifiers a tech actually holds (off the sticker, off a
// subscriber call), so the hits get their own result block above the list. The
// OLT itself also stays in the tree below.
//
// A row opens the SUBSCRIBER, not the OLT's Optical tab. Searching a customer's
// name or the MAC off their sticker is a question about that customer, and the
// old landing spot answered a different one — here is a 64-row optical list,
// find them in it. The OLT is one click on from the panel, which is the right
// way round: the subscriber is what was asked for, its OLT is context.
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
              // No serial means no identity to look a subscriber up by (the
              // record is keyed on the MAC), so such a row keeps the old
              // behaviour rather than opening a panel that could show nothing.
              onClick={() => (o.serial
                ? setOpenSub({ mac: o.serial, deviceId: m.device_id, onuRowId: o.id })
                : onOpen(m.device_id, o.id))}
              title={o.serial ? "Open this subscriber" : "Open this ONU in its OLT's Optical tab"}
              className="flex h-11 w-full items-center gap-2.5 border-b px-4 text-left last:border-b-0 hover:bg-foreground/5">
              <span className={cn("size-2 shrink-0 rounded-full", ONU_DOT[onuSev(o)])} />
              <span className="shrink-0 font-mono text-xs font-medium">
                {o.serial || o.onu_key}
              </span>
              {/* the operator's own name first — after a field survey it is
                  usually the only name this subscriber has (`onuName`) */}
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {onuName(o) || <span className="text-faint-foreground">unnamed</span>}
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
      {openSub && (
        <SubscriberDialog mac={openSub.mac} onClose={() => setOpenSub(null)}
          actions={{
            // …and the OLT is one click on, landing exactly where this list used
            // to land: that OLT's Optical tab, this ONU's row focused.
            onOpenOlt: () => {
              onOpen(openSub.deviceId, openSub.onuRowId)
              setOpenSub(null)
            },
          }} />
      )}
    </Card>
  )
}

/* ── The chips are TWO GROUPS, and separating them is what makes a list of
 *    OLTs comparable ──────────────────────────────────────────────────────────
 *
 * They used to be one run, emitted immediately after the device name. Names are
 * different lengths and tree rows are indented by depth, so EVERY ROW STARTED
 * ITS ALARMS AT A DIFFERENT X — and the ONU bar, the one thing on this screen
 * you read ACROSS rows ("which of these OLTs is worst"), landed somewhere new
 * each time. Comparing two of them meant finding them first. That is the whole
 * cost of a ragged column, and it is paid on every glance, forever.
 *
 * So they split by AXIS, which is also where they each belong:
 *
 *   IDENTITY (`DeviceIdentityChips`) — passive / maint / unassigned / backup.
 *     These say what the row IS, they modify the NAME, and they are read once.
 *     They stay beside the name, where ragged is correct: they belong to it.
 *
 *   STATUS (`DeviceAlarmChips` + the ONU instrument) — what is wrong right now.
 *     Read across rows, so they get COLUMNS on the right, beside the latency
 *     and IP columns they are scanned with. */

/** What the row IS. Sits with the name; deliberately not aligned. */
function DeviceIdentityChips({ device, collapsed }: {
  device: OrgDevice & { descendantCount?: number }
  collapsed?: boolean
}) {
  const passive = isPassiveType(device.device_type)
  // a splitter with no probe is by design, not a config gap
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
      {/* Owner-assigned tags are deliberately NOT chipped here (operator ask,
          2026-07-22): every chip on this row is a claim about the device's
          state, and a row of organisational labels alongside them is noise to
          scan past. Tags still drive the colour RAIL (deviceColor), the filter
          menu and search — they're just not spelled out per row. */}
    </>
  )
}

/** What is WRONG. Each chip deep-links to the panel tab that tells its story
 *  (optics / ports / health), so the operator never hunts for it. Gated on
 *  hasOptics so a stale badge from before SNMP was turned off can't chip a link
 *  that goes nowhere. The ONU instrument is NOT here — it has a column of its
 *  own (`DeviceOnuHealth`), because it is the one thing read across rows. */
function DeviceAlarmChips({ device, hasOptics, openTab, dupMac = true }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
  /** The grid card renders the dup-MAC chip AFTER the instrument instead (see
   *  `DupMacChip`), so it opts out here. */
  dupMac?: boolean
}) {
  const { liveSnmp, opticsChips } = alarmGates(device, hasOptics)
  return (
    <>
      {/* ── THE ALARM RUN IS RANKED, and the rank is what was missing ─────────
          These used to render in schema order — ports, bandwidth, fiber cut,
          dup MAC, bar, crit, warn, vitals — at identical weight, so a
          suspected fibre cut (a van rolls, somebody splices) and a bandwidth
          floor sat side by side with nothing saying which to walk toward. The
          order below is "what makes a person get up". */}

      {/* The only chip here that rolls a van, and the only one carrying a MARK:
          scissors is legible before the words are, which is the whole job of an
          icon on a row scanned at arm's length. Nothing else gets one — an icon
          on every chip is the uppercase problem in a different channel. */}
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

/** LAST, and MUTED, not destructive. A duplicate MAC is hygiene: CLAUDE.md
 *  records it as state-only and NEVER paged, and on this fleet most of them are
 *  zombie registrations the OLT never dropped (178 "duplicates", 2 live
 *  clones). Drawn in the same red at the same weight as an optical crit — which
 *  does page — it made a non-paging note compete with a real fault in every
 *  tree row. Still present, still clickable; it now also sits BEHIND everything
 *  that can page, so the run reads in the order a person would act on it.
 *
 *  IT IS ITS OWN COMPONENT BECAUSE THE TWO VIEWS PLACE IT DIFFERENTLY, and the
 *  reason is alignment in both cases:
 *
 *    LIST — last in the alarm run, which is RIGHT-aligned against the
 *      instrument's fixed column. The run grows leftward off a fixed edge, so
 *      a variable-width chip at its end costs nothing.
 *
 *    CARD — after the instrument. A card has no columns to right-align to, so
 *      chips simply run left to right after the latency; putting a chip that is
 *      present on some boxes and absent on others BEFORE the bar pushed the bar
 *      to a different x on every card, which is the same raggedness the list
 *      was just cured of. Latency is near-constant width, so with dup MAC moved
 *      past it the bar starts at effectively one x down the whole grid. */
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

/** Suppress EVERY SNMP-derived chip whenever the row itself isn't live — the box
 *  is down (its ICMP outage owns the row), or its probe has gone silent (the row
 *  is already graying to muted). Either way ports, optics and vitals are a frozen
 *  snapshot from before it dropped, and a chip is a claim about NOW: "port down"
 *  on an unreachable switch is the outage being reported twice, and "low bw" /
 *  "82°C" are alarms about a box that isn't there to be slow or hot. The expanded
 *  panel still shows the readings, grayed and stamped (.wisp-frozen) — the row
 *  just stops shouting them. Same rule the map pin ring uses.
 *
 *  Shared by the chips and the ONU instrument now that they render in separate
 *  columns: two copies of this gate is how a frozen bar outlives its own chips. */
function alarmGates(device: OrgDevice, hasOptics: boolean) {
  const liveSnmp = !isDownState(device.state) && !isStale(device.state_updated_at)
  return { liveSnmp, opticsChips: hasOptics && liveSnmp }
}

/** ONE INSTRUMENT WHERE THERE WERE THREE OBJECTS, IN A COLUMN OF ITS OWN.
 *
 *  The bar, "17 ONUS CRIT" and (on a bad box) the fibre-cut chip were three
 *  peers at equal weight restating one fact in the same red; the bar itself was
 *  unlabelled, so 52px of gradient between two shouting blocks read as
 *  decoration. A meter carries its readout — see `OnuHealth`.
 *
 *  It is fixed-width and LEFT-aligned inside that width, which is the half that
 *  makes a list of OLTs comparable: every bar starts at the same x, so their
 *  red shares line up as a column and the worst box is found by looking, not by
 *  reading. Right-aligning would have moved the bar whenever the readout
 *  changed width — "4 crit" vs "17 crit" — which is the same bug in miniature.
 *
 *  Renders an EMPTY box on a switch or a gateway rather than nothing, so the
 *  latency and IP columns beside it stay aligned down the whole list and not
 *  just down the OLTs. */
function DeviceOnuHealth({ device, hasOptics, openTab }: {
  device: OrgDevice
  hasOptics: boolean
  openTab: (t: DeviceTab) => void
}) {
  // The COLUMN is what needs a viewport wide enough to hold it; the INSTRUMENT
  // is needed at every width. So the width is the only thing gated on `lg` —
  // narrower than that it renders auto-width and simply stops being aligned,
  // rather than disappearing off a tablet.
  return (
    <div className="flex shrink-0 lg:w-[7rem]">
      <CardOnuHealth device={device} hasOptics={hasOptics} openTab={openTab}
        className="lg:w-full lg:justify-between" />
    </div>
  )
}

/** The same instrument with no column around it — for the grid card, where
 *  there is nothing to align to. */
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
        { description: "View only. The parent link, alerting and map are unchanged." })
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
            {/* Maintenance silences a device's alerting. Passive plant has no
                FSM and no outage of its own, so the toggle would flip a flag
                nothing reads — and leave a "maint" chip claiming something. */}
            {!isPassiveType(device.device_type) && (
              <DropdownMenuItem onClick={() => toggleMaintenance.mutate()}>
                <Wrench /> {device.maintenance ? "End maintenance" : "Start maintenance"}
              </DropdownMenuItem>
            )}
            {/* max-w + truncate: real parent names run long (HALIYA-LOCAL-CH-SW)
                and wrapped the item over four lines */}
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
  // rendered somewhere other than under its parent — see the parent-name chip
  const lifted = device.tree_detached === 1 || (passive && device.depth === 0)

  return (
    // Open = SELECTED, not expanded: the details live in the page's side panel
    // now, so .wisp-drillin (popover bg + border-strong outline, index.css) marks
    // which row the panel is showing rather than fusing a row to a panel below it.
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
          <span className="hidden shrink-0 text-xs text-faint-foreground @2xl:inline">{device.device_type}</span>
        )}
        {/* A row that renders away from its parent is the one place the tree
            stops showing where a device actually hangs — so it says so, right on
            the row. Two ways to get here: the operator lifted it (tree_detached),
            or it's plant, which lists below the gear it hangs off. */}
        {lifted && parentName && (
          <span className="hidden min-w-0 shrink items-center gap-1 text-xs text-faint-foreground @sm:inline-flex"
            title={device.tree_detached === 1
              ? `Hangs off ${parentName}, shown at the top level for readability`
              : `Fed from ${parentName}. Passive plant lists below the gear.`}>
            <CornerLeftUp className="size-3 shrink-0" />
            <span className="truncate">{parentName}</span>
          </span>
        )}
        {/* Identity rides WITH the name — it modifies it, and ragged is correct
            for something read once, in place. */}
        <DeviceIdentityChips device={device} collapsed={collapsed} />
        {/* ── EVERYTHING PAST HERE IS A COLUMN ────────────────────────────────
            The right side used to be a plain gap-3 run, so alarms began after
            a variable-length name and latency/IP were pushed around by
            whatever preceded them: nothing on this screen lined up except the
            names. Alarms right-align (their run grows leftward off a fixed
            edge), the ONU instrument gets a fixed box, and latency and IP get
            widths — so scanning down the list is reading a table instead of
            re-finding each field on every row. */}
        <div className="ml-auto flex shrink-0 items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-end gap-1.5">
            <DeviceAlarmChips device={device} hasOptics={hasOptics} openTab={openTab} />
          </div>
          <span className="hidden @3xl:inline-flex">
            <DeviceOnuHealth device={device} hasOptics={hasOptics} openTab={openTab} />
          </span>
          {/* min-w, not w: the RIGHT edge is what aligns, and a DEGRADED row
              carries "DEGRADED · 12 ms · 4% loss" which may not be truncated. */}
          <div className="flex min-w-[4.5rem] shrink-0 justify-end">
            <DeviceMetrics device={device} />
          </div>
          <span className="hidden w-[8.5rem] shrink-0 text-right font-mono text-xs text-muted-foreground @xl:inline-block">
            {device.ip_address}
          </span>
          {/* The capability cluster is 0–4 icons wide depending on what a box
              SUPPORTS, and it sat between the IP and the menu — so it shoved
              the IP and latency columns left by up to 70px on exactly the rows
              that had the most to say. Measured: IP right edges landed at three
              different x (1448 / 1491 / 1518) purely from icon count. Its own
              fixed, right-justified box, so what a device supports can never
              move what every device reports. */}
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

// Grid presentation of a device — the flattened, glanceable counterpart to the
// tree row. Same drill-in: clicking the card selects it and the page's side
// panel shows its DeviceDetail, so the tabbed panel is literally one instance
// across both views. Tree depth/collapse are list affordances and don't apply
// here; the parent name carries the context an indent would.
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

  // The card only reflects open-ness; the details are in the page's side panel.
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
        {/* The chips wrap; the ICONS DO NOT MOVE. They used to share one
            flex-wrap container with the metrics and the chips, so a card's
            capability cluster landed wherever the last chip left off — which
            meant the busiest boxes, the ones being compared, were the ones
            whose icons were somewhere else. Its own shrink-0 column, aligned
            to the FIRST line, so the right edge of every card in the grid
            reads down as a column whatever the box is doing. */}
        <div className="flex items-start gap-2 border-t pt-2">
          {/* A card is its own object, so there is no column to right-align to
              and the chips simply run left to right after the latency. That
              makes ORDER the only alignment lever there is: the instrument
              goes as early as possible so its bar starts at effectively one x
              down the whole grid, and dup MAC — the one chip that is present
              on some boxes and absent on others — goes AFTER it, where it can
              no longer shift the bar from card to card. */}
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
    /* private mode / quota — keep the in-memory state, just don't persist */
  }
}

// Sort preference, persisted like the view toggle (a UI taste).
// Whether the plant block is expanded. Remembered per browser and CLOSED by
// default: an ISP has tens of boxes with a state and hundreds of splitters
// without one, so the tree's default shape should be the gear it exists to show.
// Somebody who works plant daily opens it once and it stays open.
const PLANT_KEY = "wisp:network:plant-open"

function loadPlantOpen(): boolean {
  try { return localStorage.getItem(PLANT_KEY) === "1" } catch { return false }
}

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
      /** Top-bar search hand-off: carries a nonce, not a flag, so clicking it
       *  again while already here re-focuses the box instead of no-op'ing. */
      focusSearch?: number
      /** Home KPI tile deep-link: pre-filters the device list to exactly the
       *  devices that tile counts, labeled for the clearable chip below. */
      statusFilter?: { label: string; ids: number[] } } | null
  const focusId = navState?.deviceId
  // A deep-link may target a specific tab/ONU (an ONU hit opens the Optical tab
  // focused on that ONU); read as primitives so the effect
  // re-fires when the target changes even if the OLT id repeats.
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
  // active tag filter — a device must carry EVERY selected tag (narrowing)
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set())
  const [tagColorsOpen, setTagColorsOpen] = useState(false)
  // Which device is drilled in, page-wide — one at a time, so opening another
  // auto-closes it. A row/card gets a controlled `DrillIn` derived from this.
  const [open, setOpen] = useState<{ id: number; tab: DeviceTab; onu?: number | null } | null>(null)
  // Device an ONU search hit asked us to scroll to — kept apart from the
  // router's deep-link focusId so a search jump can't fight a navigation.
  const [jumpId, setJumpId] = useState<number | null>(null)
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
  // arriving from a stale-probe card while already mounted
  useEffect(() => { if (navState?.probeId) setProbeFilter(navState.probeId) }, [navState?.probeId])
  // arriving from a Home KPI tile while already mounted
  useEffect(() => {
    if (navState?.statusFilter) setStatusFilter(navState.statusFilter)
  }, [navState?.statusFilter])
  // arriving from the top bar's search button (or ⌘K): put the cursor in the
  // box. Selecting any existing text means a second search starts by typing
  // rather than by clearing — landing here is a new question, not an edit.
  useEffect(() => {
    if (!navState?.focusSearch) return
    searchRef.current?.focus()
    searchRef.current?.select()
  }, [navState?.focusSearch])
  // deep-link (Home row, map, WhatsApp) opens the target's panel — on the
  // Optical tab focused on an ONU when the caller named one, else the device's
  // own first tab (optical, for an OLT).
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
  // The distribution record's size, for the plant fold's summary. One row per
  // passive, so it stays small on a fleet with thousands of ONUs, and it shares
  // the map's cache key — this is a progress figure, not live status, so it
  // deliberately carries no refetch interval of its own.
  const dropsQ = useQuery({
    queryKey: ["drops", scopeOrg],
    queryFn: () => inventoryApi.drops(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
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
    // walk the RENDERED chain, the same one treeOrder builds: a detached device
    // — or a passive, which lists in its own block below the gear — is already at
    // the top level, so expanding the subtree it renders outside of would reach
    // nothing (and would silently unfold a branch the operator had closed)
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
  const flatten = (rows: Ordered[]) => (gridView && cmp ? [...rows].sort(cmp) : rows)
  const orderedGear = flatten(treeOrdered.gear)
  const orderedPlant = flatten(treeOrdered.plant)
  // Resolved against the FILTERED list, not allDevices: a search or tag filter
  // that hides the selected row takes its panel with it, the way an inline
  // expansion used to vanish with the row that rendered it. Panel-open state
  // survives, so clearing the filter brings it back where it was.
  const openDevice = open ? devices.find((d) => d.id === open.id) ?? null : null
  // The plant fold has to yield to anything that would otherwise HIDE a row the
  // operator is looking at: a search hit, or the box whose panel is open. Same
  // rule as `effectiveCollapsed` above — a match nobody can see reads as no
  // match at all.
  const forcePlantOpen = (searching && orderedPlant.length > 0)
    || (openDevice != null && isPassiveType(openDevice.device_type))
  const showPlant = plantOpen || forcePlantOpen
  // How much of the distribution record exists, stated where the plant is. A
  // splitter count alone says nothing about whether anyone has recorded what
  // hangs off them, which is the half that makes the map's branch verdicts and
  // load bars mean anything. Shares the map's cache key.
  const plantDrops = dropsQ.data?.recorded ?? null
  // Its own stored width, separate from the Map's: a full page and a panel
  // floating over tiles have different room to spend.
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
  // No drill-in placement to solve here any more: the open card's details render
  // in the page's side panel, so cards never reflow around an expanded block.
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
    // `panel.vars` carries the LIVE dragged width down to the panel and to
    // `.wisp-panel-clear`. The panel floats, so the LIST is allowed to run under
    // it — that was the choice. The CONTROLS are not: view toggle, search, sort,
    // tags and Add all sit at the page's right edge, and a panel parked on top
    // of them means you can't find the next device without closing the one
    // you're reading. Reading the live width is what keeps that gutter honest —
    // a hardcoded one would gap or overlap the moment the panel is resized.
    // `wisp-tree-page` exists for one CSS rule (index.css, `[data-pane]`): in a
    // split pane the floating device panel has to anchor to the PANE instead of
    // the viewport. This element must stay UNPOSITIONED for that to work — the
    // panel climbs past it to the pane, which is the box that doesn't scroll.
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
          atCap
            // The "Add passive plant" escape hatch went with plant creation
            // itself. It still holds that passives don't count against the cap,
            // so a capped org can keep recording its distribution network — the
            // way through is the map now, which is what the note says.
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
              : "No devices yet. Add one above."}
          </p>
        )}
        {orderedGear.length > 0 && (gridView ? renderGrid(orderedGear) : renderList(orderedGear))}
        {/* Plant below the gear, and FOLDED.
            A splitter has no state, no ports and nothing that can alarm, so on
            the screen that exists to show trouble it is a row to scan past — and
            once a fleet actually records its distribution network there are
            hundreds of them against a few dozen boxes with a state. What the
            tree owes an operator here is a count and a way in, not a wall.

            Three things the fold must not do. It may not hide a SEARCH hit
            (`forcePlantOpen`), it may not hide the row whose panel is open, and
            it may not imply the record is complete — the label stays "recorded",
            never "all", because a splitter nobody has entered simply isn't
            here. */}
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
              {/* Where plant is authored now. Stated on the screen somebody
                  would otherwise go hunting for it on, and a real link rather
                  than a sentence: this replaced a button that used to be here,
                  so it owes the operator a way through rather than a note. */}
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

      {/* Drill-in side panel. Floats over the list rather than docking beside it,
          so the tree keeps its full width and reads the same open or closed —
          the same shape the Map's pin panel uses, so the two surfaces are one
          habit. Fixed, not sticky: <main> is the scroll container, so the panel
          holds its place while the list scrolls under it. Below md there is no
          room beside anything, so it becomes a bottom sheet clear of the mobile
          tab bar. z-40 sits above the sticky header (z-30) and under Radix
          portals (z-50), so a dialog or dropdown opened from inside still wins. */}
      {/* Scrim: the list recedes rather than going away. It stays READABLE (you
          have to know which row you're clicking) and CLICKABLE (pointer-events
          -none — switching devices is one click, never close-then-reopen), it
          just stops competing with the panel for attention.
          Two things here are load-bearing, both learned by shipping the wrong
          one first. (1) BLACK, not `bg-background/…`: a canvas tone darkens in
          dark mode but LIGHTENS an already-near-white light mode, washing the
          page out instead of pushing it back — so the alpha is mode-split
          instead, the same total effect from opposite starting points.
          (2) NO backdrop-blur. The app's dialogs pair a light scrim with
          `backdrop-blur-xs` and that reads fine under a small centred modal, but
          over a full device tree it renders far heavier than the name suggests
          and takes the row names with it — which kills the click-through this
          panel depends on. Dimming alone is enough. */}
      {openDevice && (
        <div aria-hidden
          className="wisp-tree-scrim pointer-events-none fixed inset-0 z-[35] bg-black/25 dark:bg-black/45" />
      )}
      {openDevice && (
        // Opaque, unlike the Map's panel: /95 + backdrop-blur is right over
        // raster tiles, where sensing what's underneath is the point, and wrong
        // over a data table, where the row text ghosting through the panel is
        // just noise on top of the numbers you opened it to read.
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
          {/* overscroll-contain stops scroll CHAINING: without it, a wheel over
              the panel falls through to <main> the moment the panel has nothing
              left to scroll — or nothing to scroll at all — and the tree slides
              away underneath the thing you're reading. It belongs on the Card
              too (see .wisp-device-panel), because the header and tab strip sit
              OUTSIDE this scroller and a wheel over them would chain regardless. */}
          <div className="overflow-y-auto overscroll-contain p-3">
            <DeviceDetail device={openDevice} tab={open!.tab} focusOnuId={open!.onu ?? null}
              onTab={(t) => setOpen((o) => (o ? { ...o, tab: t } : o))} />
          </div>
        </Card>
      )}
    </div>
  )
}
