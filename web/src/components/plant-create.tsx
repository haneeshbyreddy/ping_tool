// Recording one passive box, from the map, in as few decisions as the truth
// allows.
//
// The old way was the device form: seven fields on a page with no map, followed
// by a second trip to the map to place the pin. This sheet exists because the
// click already answered most of them — where the box is, what feeds it, which
// PON that feeder is on, which region it sits in — so the only thing left to ask
// is the one fact the ground cannot state: how many ways the box splits.
//
// Three rules it must keep:
//
//   * EVERY DERIVED FIELD NAMES ITS SOURCE, and every one is changeable. A
//     prefill you cannot see is a prefill you cannot correct, and a wrong feeder
//     is a wrong branch-fault verdict months later, when nobody remembers this
//     click.
//   * THE PON IS NOT A TEXT FIELD. It is picked from the OLT's own roster
//     labels, because `EPON0/4` typed as `0/4` produces a splitter whose
//     customer picker is silently empty — a failure that only shows up ten
//     steps later, on a different screen. Under a splitter it arrives already
//     filled in from the feeder (one fibre goes in) but stays changeable, since
//     inheritance is only as right as the parent's own column.
//   * A RATIO IS NEVER INVENTED. "Not recorded" is a real answer and stays one
//     press away. The load bar and the cumulative split both refuse to compute
//     from a guess, and this sheet must not hand them one.
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowRight, Check, MapPin, Search, Split } from "lucide-react"
import { ApiError, inventoryApi } from "@/lib/api"
import { SPLIT_RATIOS, isPassiveType, type OrgDevice } from "@/lib/types"
import { useDebounced } from "@/hooks/use-debounced"
import { usePonOptions } from "@/hooks/use-pon-options"
import { onuName, onuSearchKey } from "@/lib/format"
import { fmtKm } from "@/map/geometry"
import {
  PLANT_LABEL, feederOptions, nearestFeeder, oltHead, ponFor, ponOptions,
  splitIfAdded, suggestPlantName, type PlantKind,
} from "@/map/plant"
import { Button } from "@/components/ui/button"
import {
  Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Segmented } from "@/components/ui/segmented"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

/** What a right-click handed us: a kind, a coordinate, and the box the map
 *  thinks feeds it. `parentId` null means nothing placed was near enough to
 *  suggest, which the sheet states rather than hides. */
export interface PlantDraft {
  kind: PlantKind
  lat: number
  lng: number
  parentId: number | null
}

const NO_RATIO = "__none__"
const NO_PON = "__nopon__"

/** One derived fact, with the box it came from named beside it. */
function Derived({ label, value, note }: {
  label: string; value: React.ReactNode; note?: string
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-right">
        {value}
        {note && <span className="ml-1.5 text-2xs text-faint-foreground">{note}</span>}
      </span>
    </div>
  )
}

export function PlantCreateDialog({ draft, devices, org, onClose, onCreated }: {
  draft: PlantDraft | null
  devices: OrgDevice[]
  org: string | null
  onClose: () => void
  /** `again` = the operator pressed "Save and add another", so the caller should
   *  re-arm the map for the next box with this one as its feeder. */
  onCreated: (created: { id: number; name: string }, again: boolean) => void
}) {
  const queryClient = useQueryClient()
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])

  const [name, setName] = useState("")
  const [ratio, setRatio] = useState<string>(NO_RATIO)
  const [parentId, setParentId] = useState<number | null>(null)
  const [pon, setPon] = useState<string>("")
  const [ponTouched, setPonTouched] = useState(false)
  const [pickFeeder, setPickFeeder] = useState(false)

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
    setRatio("8")
    setParentId(draft.parentId)
    setPickFeeder(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  const parent = parentId != null ? byId.get(parentId) ?? null : null
  const inherited = ponFor(parent, byId)
  const olt = oltHead(parent, byId)
  // A PON is offered whenever an OLT heads this chain — under a splitter as
  // well as directly under an OLT. Under a splitter it arrives already filled
  // in (one fibre goes into a splitter, so it is on its feeder's PON), but it
  // stays CHANGEABLE: inheritance is only as right as the parent's own column,
  // and a value the operator can see but not correct is the worse half of both
  // options. This deliberately matches the survey sheet — one field behaving two
  // ways on two screens is how the same splitter gets recorded differently.
  const askPon = olt != null

  // The picker's vocabulary: what this OLT's roster actually reports. Changing
  // the feeder re-keys the query, so the list refreshes on its own.
  const { pons, loading: ponsLoading } = usePonOptions(olt?.id, askPon)
  // Untouched, the choice TRACKS the feeder; once made by hand it stands. A
  // label belongs to one OLT's roster, so a feeder change has to hand it back
  // rather than carry it onto a box that never reported that port.
  const effectivePon = ponTouched ? pon : (inherited.inherited ? inherited.pon ?? "" : "")

  useEffect(() => { setPon(""); setPonTouched(false) }, [parentId])

  const ratioNum = ratio === NO_RATIO ? null : Number(ratio)
  const total = splitIfAdded(parent, ratioNum, byId)
  const feeder = draft && parent && parent.lat != null && parent.lng != null
    ? nearestFeeder(draft.lat, draft.lng, [parent])
    : null

  const options = useMemo(
    () => (draft ? feederOptions(draft.lat, draft.lng, devices) : []),
    [draft, devices])

  const create = useMutation({
    mutationFn: async ({ again }: { again: boolean }) => {
      if (!draft) throw new Error("no draft")
      const { id } = await inventoryApi.create({
        org_id: org ?? undefined,
        name: name.trim(),
        ip_address: "",
        device_type: draft.kind,
        // Inherited silently and deliberately: a region is an operator's own
        // grouping, the box is physically inside its feeder's, and asking again
        // would be a field with one obvious answer.
        region: parent?.region ?? null,
        tags: [],
        parent_device_id: parentId,
        pon_port: effectivePon || null,
        split_ratio: ratioNum,
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
            <Segmented
              className="w-full"
              value={ratio}
              onChange={setRatio}
              options={[
                ...SPLIT_RATIOS.map((r) => ({ value: String(r), label: `1:${r}` })),
                { value: NO_RATIO, label: "Not recorded",
                  title: "A box that only splices has no ratio, and an unknown one must not be guessed" },
              ]}
            />
            {total != null && (
              <p className="text-2xs text-faint-foreground">
                Total split to this box becomes{" "}
                <span className="font-mono text-foreground">1:{total}</span>
              </p>
            )}
            {ratioNum != null && total == null && parent != null && (
              <p className="text-2xs text-faint-foreground">
                Total split unknown · a box above this one has no ratio recorded
              </p>
            )}
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

          {/* ---- what the click already knew ------------------------------- */}
          <div className="flex flex-col gap-2 rounded-lg border bg-muted/40 px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="wisp-eyebrow">Fed from</span>
              <button type="button"
                className="text-2xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                onClick={() => setPickFeeder((v) => !v)}>
                {pickFeeder ? "Close" : "Change"}
              </button>
            </div>

            {pickFeeder ? (
              <Command className="rounded-md border bg-popover">
                <CommandInput placeholder="Search devices…" className="h-8 text-xs" />
                <CommandList className="max-h-52">
                  <CommandEmpty className="py-4 text-xs">No device matches.</CommandEmpty>
                  <CommandGroup>
                    <CommandItem value="__root__"
                      onSelect={() => { setParentId(null); setPickFeeder(false) }}>
                      <span className="text-muted-foreground">No feeder recorded</span>
                    </CommandItem>
                    {options.map((o) => (
                      <CommandItem key={o.device.id}
                        value={`${o.device.name} ${o.device.device_type ?? ""}`}
                        onSelect={() => { setParentId(o.device.id); setPickFeeder(false) }}>
                        {o.device.id === parentId
                          ? <Check className="size-3.5 shrink-0" />
                          : <span className="size-3.5 shrink-0" />}
                        <span className="min-w-0 truncate">{o.device.name}</span>
                        <span className="ml-auto shrink-0 font-mono text-2xs text-faint-foreground">
                          {o.meters == null ? "not placed"
                            : o.meters < 1000 ? `${Math.round(o.meters)} m`
                              : fmtKm(o.meters / 1000)}
                        </span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </CommandList>
              </Command>
            ) : parent ? (
              <div className="flex flex-col gap-1">
                <div className="flex items-center gap-1.5 text-xs">
                  <span className="min-w-0 truncate font-medium">{parent.name}</span>
                  {isPassiveType(parent.device_type) && (
                    <span className="shrink-0 rounded bg-muted px-1 py-px text-2xs text-muted-foreground">
                      passive
                    </span>
                  )}
                  <ArrowRight className="size-3 shrink-0 text-faint-foreground" />
                  <span className="min-w-0 truncate font-mono text-2xs text-muted-foreground">
                    {name.trim() || `new ${kindLabel}`}
                  </span>
                </div>
                <Derived label="PON"
                  value={askPon
                    ? <span className="font-mono">{effectivePon || "not recorded"}</span>
                    : <span className="text-muted-foreground">not on a PON</span>}
                  note={!ponTouched && inherited.inherited ? "inherited"
                    : olt ? `on ${olt.name}` : undefined} />
                {parent.region && <Derived label="Region" value={parent.region} note="inherited" />}
                {feeder && (
                  <Derived label="Straight-line"
                    value={feeder.meters < 1000
                      ? `${Math.round(feeder.meters)} m` : fmtKm(feeder.meters / 1000)}
                    note="no route drawn yet" />
                )}
              </div>
            ) : (
              // Nothing placed was near enough to propose. Said plainly, because
              // a silent blank here reads as "this box feeds nothing", which is
              // never true of a splitter.
              <p className="text-2xs text-muted-foreground">
                Nothing placed within 2 km of this point. Pick the box that feeds
                it, or leave it and set the feeder later.
              </p>
            )}
          </div>

          {/* The PON, offered wherever an OLT heads the chain. Prefilled from
              the feeder under a splitter, empty under an OLT, changeable in
              both. */}
          {askPon && (
            <div className="flex flex-col gap-1.5">
              <Label className="text-2xs text-muted-foreground">
                PON on {olt?.name}
              </Label>
              {pons.length > 0 ? (
                <>
                  <Select value={effectivePon || NO_PON}
                    onValueChange={(v) => {
                      setPonTouched(true)
                      setPon(v === NO_PON ? "" : v)
                    }}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Not recorded" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_PON}>Not recorded</SelectItem>
                      {/* the current value is always listed, even when this
                          OLT's roster no longer carries it — a blank Select
                          saves as a cleared PON */}
                      {ponOptions(pons, effectivePon).map((p) => (
                        <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {!ponTouched && inherited.inherited && inherited.pon && (
                    <p className="text-2xs text-faint-foreground">
                      Inherited from {parent?.name}. One fibre goes into a
                      splitter, so it is on its feeder's PON.
                    </p>
                  )}
                </>
              ) : (
                <>
                  <Input value={effectivePon} className="font-mono" placeholder="EPON0/4"
                    onChange={(e) => { setPonTouched(true); setPon(e.target.value) }} />
                  <p className="text-2xs text-faint-foreground">
                    {ponsLoading
                      ? "Reading this OLT's PON labels…"
                      : "This OLT has no ONU roster yet, so there are no labels to"
                        + " pick from. Type it exactly as the OLT reports it."}
                  </p>
                </>
              )}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <div className="flex gap-2">
            {/* The chain flow. Plant is walked, not filled in from a desk, so
                the common posture is "and the next one is 60 m up the road" —
                this saves and re-arms the map with THIS box as the next
                feeder, which is what makes recording a whole feeder run one
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
