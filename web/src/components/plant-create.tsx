import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { MapPin, Search, Split, Waypoints } from "lucide-react"
import { ApiError, inventoryApi } from "@/lib/api"
import { type OrgDevice } from "@/lib/types"
import { SplitRatioField, type SplitRatio } from "@/components/split-ratio-field"
import { useDebounced } from "@/hooks/use-debounced"
import { onuName, onuSearchKey } from "@/lib/format"
import { PLANT_LABEL, suggestPlantName, type PlantKind } from "@/map/plant"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

export interface PlantDraft {
  kind: PlantKind
  lat: number
  lng: number
}

const NO_REGION = "__noregion__"

export function nearestRegion(lat: number, lng: number, devices: OrgDevice[]): string | null {
  let best: { d: number; region: string } | null = null
  for (const d of devices) {
    if (d.lat == null || d.lng == null || !d.region) continue
    const dist = (d.lat - lat) ** 2 + (d.lng - lng) ** 2
    if (!best || dist < best.d) best = { d: dist, region: d.region }
  }
  return best?.region ?? null
}

export function PlantCreateDialog({ draft, devices, org, onClose, onCreated }: {
  draft: PlantDraft | null
  devices: OrgDevice[]
  org: string | null
  onClose: () => void
  onCreated: (created: { id: number; name: string }, again: boolean) => void
}) {
  const queryClient = useQueryClient()

  const [name, setName] = useState("")
  const [split, setSplit] = useState<SplitRatio>({ ratio: 8, inputs: null })
  const [region, setRegion] = useState<string | null>(null)

  const regions = useMemo(() => {
    const seen = new Set<string>()
    for (const d of devices) if (d.region) seen.add(d.region)
    return [...seen].sort((a, b) => a.localeCompare(b))
  }, [devices])

  const inheritedRegion = useMemo(
    () => (draft ? nearestRegion(draft.lat, draft.lng, devices) : null),
    [draft, devices])

  useEffect(() => {
    if (!draft) return
    setName(suggestPlantName(draft.kind, devices))
    setSplit({ ratio: 8, inputs: null })
    setRegion(nearestRegion(draft.lat, draft.lng, devices))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  const create = useMutation({
    mutationFn: async ({ again }: { again: boolean }) => {
      if (!draft) throw new Error("no draft")
      const { id } = await inventoryApi.create({
        org_id: org ?? undefined,
        name: name.trim(),
        ip_address: "",
        device_type: draft.kind,
        region,
        tags: [],
        parent_device_id: null,
        pon_port: null,
        split_ratio: split.ratio,
        split_inputs: split.inputs,
      })
      try {
        await inventoryApi.setLocation(id, draft.lat, draft.lng)
      } catch {
        toast.warning(`${name.trim()} was created, but its pin didn't save`, {
          description: "Find it in Network → Passive plant and place it from there.",
        })
      }
      return { id, name: name.trim(), again }
    },
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      queryClient.invalidateQueries({ queryKey: ["drops"] })
      onCreated({ id: r.id, name: r.name }, r.again)
    },
    onError: (e) => toast.error(
      e instanceof ApiError ? e.message : "Couldn't record this box"),
  })

  if (!draft) return null
  const kindLabel = PLANT_LABEL[draft.kind]
  const busy = create.isPending
  const canSave = name.trim().length > 0 && !busy

  return (
    <Dialog open onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Split className="size-4 text-muted-foreground" />
            New {kindLabel}
          </DialogTitle>
          <DialogDescription className="font-mono text-2xs">
            {draft.lat.toFixed(6)}, {draft.lng.toFixed(6)}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label className="text-2xs text-muted-foreground">Split ratio</Label>
            <SplitRatioField value={split} onChange={setSplit} />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label className="text-2xs text-muted-foreground">Name</Label>
            <Input
              autoFocus
              value={name}
              className="font-mono"
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canSave) create.mutate({ again: false })
              }}
            />
          </div>

          {regions.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <Label className="text-2xs text-muted-foreground">Region</Label>
              <Select value={region ?? NO_REGION}
                onValueChange={(v) => setRegion(v === NO_REGION ? null : v)}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_REGION}>None</SelectItem>
                  {regions.map((r) => (
                    <SelectItem key={r} value={r}>{r}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {region && region === inheritedRegion && (
                <p className="text-2xs text-faint-foreground">
                  From the nearest box on the map.
                </p>
              )}
            </div>
          )}

          <div className="flex items-start gap-2 rounded-lg border bg-muted/40 px-3 py-2.5">
            <Waypoints className="mt-px size-3.5 shrink-0 text-muted-foreground" />
            <p className="text-2xs leading-snug text-muted-foreground">
              Nothing is drawn to it yet. Open the box and pull a core in when
              you know which cable feeds it — the line then follows that cable
              instead of a straight guess.
            </p>
          </div>
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={!canSave}
              onClick={() => create.mutate({ again: true })}>
              <MapPin className="size-3.5" /> Save and add another
            </Button>
            <Button size="sm" disabled={!canSave}
              onClick={() => create.mutate({ again: false })}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export interface CustomerDraft {
  lat: number
  lng: number
  passiveId: number | null
}

const ONU_MIN = 3

export function AttachCustomerDialog({ draft, devices, org, onClose, onAttached }: {
  draft: CustomerDraft | null
  devices: OrgDevice[]
  org: string | null
  onClose: () => void
  onAttached: (mac: string) => void
}) {
  const queryClient = useQueryClient()
  const [q, setQ] = useState("")
  const [hangOff, setHangOff] = useState(true)

  useEffect(() => { if (draft) { setQ(""); setHangOff(true) } }, [draft])

  const passive = draft?.passiveId != null
    ? devices.find((d) => d.id === draft.passiveId) ?? null : null

  const debounced = useDebounced(q.trim(), 450)
  const enough = onuSearchKey(debounced).length >= ONU_MIN
  const searchQ = useQuery({
    queryKey: ["onu-search", org, debounced],
    queryFn: () => inventoryApi.onuSearch(org, debounced),
    enabled: !!draft && !!org && enough,
    staleTime: 60_000,
    retry: 0,
  })
  const hits = useMemo(() => {
    const byMac = new Map<string, { mac: string; who: string; where: string }>()
    for (const m of searchQ.data?.matches ?? []) {
      for (const o of m.onus) {
        const mac = (o.serial ?? "").trim().toUpperCase()
        if (!mac || byMac.has(mac)) continue
        byMac.set(mac, {
          mac,
          who: onuName(o),
          where: `${m.device_name}${o.pon_port ? ` · ${o.pon_port}` : ""}`,
        })
      }
    }
    return [...byMac.values()].slice(0, 40)
  }, [searchQ.data])

  const attach = useMutation({
    mutationFn: async (mac: string) => {
      if (!draft) throw new Error("no draft")
      await inventoryApi.setOnuPlace({
        mac, lat: draft.lat, lng: draft.lng, org_id: org,
      })
      if (hangOff && draft.passiveId != null) {
        await inventoryApi.setDrops({
          macs: [mac], passive_id: draft.passiveId, org_id: org,
        })
      }
      return mac
    },
    onSuccess: (mac) => {
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      queryClient.invalidateQueries({ queryKey: ["drops"] })
      queryClient.invalidateQueries({ queryKey: ["optics"] })
      onAttached(mac)
    },
    onError: (e) => toast.error(
      e instanceof ApiError ? e.message : "Couldn't record this customer"),
  })

  if (!draft) return null

  return (
    <Dialog open onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Customer here</DialogTitle>
          <DialogDescription>
            Find the sticker MAC or the name on the subscriber's box. This records
            where it stands, nothing else.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="relative">
            <Search className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input autoFocus value={q} className="pl-8" placeholder="MAC or customer name"
              onChange={(e) => setQ(e.target.value)} />
          </div>

          {passive && (
            <label className={cn(
              "flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-xs",
              hangOff ? "border-primary/40 bg-primary/5" : "bg-muted/40")}>
              <input type="checkbox" className="mt-0.5" checked={hangOff}
                onChange={(e) => setHangOff(e.target.checked)} />
              <span className="min-w-0">
                <span className="block">Hangs off <span className="font-medium">{passive.name}</span></span>
                <span className="block text-2xs text-faint-foreground">
                  Records the drop as well as the pin. Untick if this one comes
                  off a different box.
                </span>
              </span>
            </label>
          )}

          <div className="max-h-64 overflow-y-auto rounded-lg border">
            {!enough ? (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">
                Type at least {ONU_MIN} characters.
              </p>
            ) : searchQ.isLoading ? (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">Searching…</p>
            ) : hits.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">
                No subscriber in the roster matches that.
              </p>
            ) : hits.map((h) => (
              <button key={h.mac} type="button" disabled={attach.isPending}
                onClick={() => attach.mutate(h.mac)}
                className="flex w-full flex-col items-start gap-0.5 border-b px-3 py-2 text-left last:border-b-0 hover:bg-foreground/5 disabled:opacity-50">
                <span className="w-full truncate text-xs font-medium">{h.who}</span>
                <span className="w-full truncate text-2xs text-muted-foreground">{h.where}</span>
              </button>
            ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
