// Recording one passive box, from the map, in as few decisions as the truth
// allows.
//
// **IT NO LONGER ASKS WHAT FEEDS THE BOX** (2026-08-09, the operator's ask:
// *"when a new device is added — OLT or splitter or switch — don't ask which
// device it is from, just place it and then we should be able to configure it…
// I don't want small inconveniences like a line being drawn automatically"*).
// The reason is not only ergonomic. Somebody standing at a new splitter knows
// where it is and how many ways it splits, and routinely does NOT yet know
// which core of which cable will feed it — that is decided at the closure, or
// already was and nobody here remembers. This sheet used to GUESS: nearest
// placed box within 2 km, written into `parent_device_id`, and the map drew a
// line from it the instant you pressed Save. A claim about plant, invented from
// proximity, on the one screen a crew reads geometry off.
//
// So the feed is neither asked nor guessed. It arrives with the FIBRE — *core 3
// of that cable runs from here to there* — which is a sentence somebody can
// stand behind, and which draws along glass that has been surveyed instead of a
// chord between two pins.
//
// What is left is the one fact the ground cannot state: how many ways it splits.
//
// Two rules it must keep:
//
//   * EVERY DERIVED FIELD NAMES ITS SOURCE, and every one is changeable. A
//     prefill you cannot see is a prefill you cannot correct.
//   * A RATIO IS NEVER INVENTED. "Not recorded" is a real answer and stays one
//     press away. The load bar and the cumulative split both refuse to compute
//     from a guess, and this sheet must not hand them one.
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

/** What a right-click handed us: a kind and a coordinate. Deliberately nothing
 *  else — there is no `parentId` any more, because there is no longer a question
 *  here whose answer a click could have guessed. */
export interface PlantDraft {
  kind: PlantKind
  lat: number
  lng: number
}

const NO_REGION = "__noregion__"

/** The nearest placed box's region, as the only thing still worth inheriting.
 *
 *  A region is the operator's own grouping of ground, and a new splitter is
 *  physically inside whichever one its neighbours are in — so this is a fact
 *  about the COORDINATE rather than a guess about the network, which is exactly
 *  what the feeder was not. Squared degrees are enough: nothing is measured off
 *  this, it only has to pick the closest of a few dozen pins. */
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
  /** `again` = the operator pressed "Save and add another", so the caller should
   *  re-arm the map for the next box. */
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

  // Reseed on every fresh draft. A sheet that remembered the last box's name
  // would silently write "SPL-7" twice down a cascade being recorded quickly,
  // which is exactly the posture this flow is for.
  useEffect(() => {
    if (!draft) return
    setName(suggestPlantName(draft.kind, devices))
    // A splitter that splits nothing is not a thing anyone stocks, so the
    // commonest real ratio is offered up front. "Not recorded" stays one press
    // away: an unknown ratio is a real answer, and the load bar and the
    // cumulative split both refuse to compute from a guess.
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
        // NOTHING FEEDS IT YET, and that is a recorded state rather than a gap
        // in one. The fibre says what feeds it, and until somebody records a
        // core there is no honest answer to write here — which is also why the
        // map draws no line from this box until there is one.
        parent_device_id: null,
        pon_port: null,
        split_ratio: split.ratio,
        split_inputs: split.inputs,
      })
      // Two calls, and they are not atomic — there is no create-with-coordinates
      // verb an owner can reach (`field-passive` has one, but it deliberately
      // cannot set a parent). If the pin fails we say so and name the box rather
      // than leaving a placed-looking row that isn't: an unplaced passive is
      // findable in the tree, an unreported failure is not.
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
          {/* The one real question. First, because it is the only one. */}
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

          {/* The only thing still inherited, and it comes from the GROUND rather
              than from the network: a box is physically inside whichever region
              its neighbours are in. Named and changeable, like every derived
              field on this sheet. */}
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

          {/* WHAT HAPPENS NEXT, said here because its ABSENCE is what an
              operator notices: they press Save and no line appears. That is the
              feature, but only if it is announced — an unexplained absence reads
              as a failed save, which is how somebody ends up pressing Save twice
              and recording the same box again. */}
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
            {/* Plant is walked, not filled in from a desk, so the common posture
                is "and the next one is 60 m up the road" — this saves and
                re-arms the map, which makes recording a whole feeder run one
                continuous gesture instead of eight round trips. */}
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

// ---------------------------------------------------------------------------
// Attaching a customer
// ---------------------------------------------------------------------------

/** Where a subscriber is being put, and what it will hang off.
 *
 *  `passiveId` rides WITH the coordinate on purpose. A tech standing at a drop
 *  knows both facts in the same instant — where the box is, and which splitter
 *  the fibre comes from — and until now those were two flows on two surfaces (a
 *  map pin here, a bulk dialog inside the splitter panel there). Recording half
 *  of what somebody already knows is most of why `onu_drops` sat empty. */
export interface CustomerDraft {
  lat: number
  lng: number
  passiveId: number | null
}

/** Mirrors `central/api/devices.py:ONU_SEARCH_MIN` — the server refuses less. */
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

  // Debounced and keyed exactly like the map's own search box, so the two share
  // a cache rather than each holding their own copy of the same roster answer.
  // `onuSearchKey` is the punctuation-blind needle — a MAC typed with colons and
  // one typed with dashes are the same lookup, and the server agrees.
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
    // The identity is the SERIAL column, uppercased — the same normalisation the
    // map search does, and the same one `onu_places` is keyed on. A hit with no
    // serial has no identity a drop record could survive on, so it is skipped
    // rather than shown as a row that cannot be clicked.
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
      // The location first: it is the fact this dialog was opened to record, and
      // it must land even if the drop write is refused. Deliberately carries NO
      // witness claim — putting a customer on the map is a coordinate, and the
      // power-supply claim that flips a PON verdict has its own explicit toggle.
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
            {/* A customer is never CREATED: the ONU is already in the OLT's
                roster because the walk found it. What is being recorded is where
                it stands, so the verb has to be "find", not "add" — and a MAC
                the roster has never seen is a typo, not a new subscriber. */}
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
              // "No such subscriber" and "nobody has walked that OLT" are
              // different answers, and this list may only give the first one.
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
