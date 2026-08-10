import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Check, ChevronRight, Crosshair, MapPin, Navigation, Plus, Search, Signal,
  TriangleAlert, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useIsMobile } from "@/hooks/use-mobile"
import { useGpsFix, GOOD_FIX_M, type GpsFix } from "@/hooks/use-gps-fix"
import { usePonOptions } from "@/hooks/use-pon-options"
import { inventoryApi, ApiError } from "@/lib/api"
import {
  isPassiveType,
  type OnuCoverageLocatedRow, type OnuCoverageRow, type OnuPlace, type OrgDevice,
} from "@/lib/types"
// The map's plant rules, shared rather than re-derived: the two surfaces must
// agree about what feeds what and what a box should be called, or the same
// splitter recorded from a phone and from a desk comes out differently.
import {
  feederOptions, nearestFeeder, oltHead, ponFor, ponOptions, suggestPlantName,
} from "@/map/plant"
import { NeedsOrg } from "@/components/needs-org"
import { ShiftButton } from "@/components/field-tracking-card"
import { PinAdjustMap } from "@/components/pin-adjust-map"
import { SplitRatioField, type SplitRatio } from "@/components/split-ratio-field"
import { Chip, StatusDot, type Tone } from "@/components/status-badge"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"

/** Radix refuses an empty Select value, and "not recorded" is a real answer. */
const NO_FEEDER = "__nofeeder__"
const NO_PON = "__nopon__"
/** The only plant kind an operator may create — see `plant.ts:PLANT_KINDS`. */
const PLANT_KIND = "splitter" as const

// The field-capture screen: a phone, one hand, standing at a pole.
//
// Deliberately NOT part of the map. Pinch-zooming to drop a pin in the sun while
// holding a ladder is how a splitter ends up 200 m into a field — so the map is
// the VERIFICATION view and this is the capture one. The interaction it is built
// around is: pick the thing in front of you, press one big button, confirm. The
// list is the primary surface; the coordinates are never typed and never
// dragged here.
//
// What it may write is narrow by construction (server: _WORKER_POST): a
// coordinate on an existing device, and passive plant with no parent. Everything
// consequential about a device — its parent, its IP, its probe — stays the
// owner's on the desktop, which is exactly the division the operator asked for.

/** How a fix reads once you have it. Three states, never two: a tight fix and a
 *  cell-tower estimate are both "a location", and rendering them alike is the
 *  thing this whole screen exists to avoid. */
function fixTone(accuracy: number | null): Tone {
  if (accuracy == null) return "muted"
  if (accuracy <= GOOD_FIX_M) return "success"
  if (accuracy <= 60) return "warning"
  return "destructive"
}

function fixLabel(accuracy: number | null): string {
  if (accuracy == null) return "accuracy unknown"
  return `±${Math.round(accuracy)} m`
}

/** The provenance line under an already-placed device. "Unknown" is a real
 *  answer here — a pin dragged on the desktop carries no accuracy, and claiming
 *  one would be worse than admitting the gap. */
function placedNote(d: OrgDevice): string {
  if (d.lat == null) return "not placed"
  const who = d.placed_by ? ` by ${d.placed_by}` : ""
  const when = d.placed_at ? ` ${ago(d.placed_at)}` : ""
  if (d.place_source === "gps") return `GPS ${fixLabel(d.accuracy_m)}${who}${when}`
  if (d.place_source === "manual") return `set by hand${who}${when}`
  return "placed on the desktop"
}

type Target =
  | { kind: "device"; device: OrgDevice }
  | { kind: "passive" }
  | {
      kind: "onu"; mac: string; who: string; where: string; located: boolean
      /** the operator's own name for this subscriber (`onu_places.label`), if one
       *  has been recorded — what the name field starts from. */
      label: string | null
      /** their contact number (`onu_places.phone`). Null on a pin placed before
       *  the number was captured, which is why reopening one asks for it. */
      phone: string | null
      /** what the OLT calls it (`onu_optics.name`). Reference only: it is
       *  rewritten by every SNMP walk, so it can never be the field's target. */
      walked: string | null
      /** where the pin already sits, so a RENAME doesn't move it. */
      at: { lat: number; lng: number } | null
    }

/** The server's own floor for an ONU lookup (`api/devices.onu_search`) — below
 *  three characters the needle matches most of a fleet. */
const ONU_MIN_CHARS = 3

/** Mirrors `onuroster._norm_mac`: separator-EXACT, case-insensitive. Used here
 *  only to ask "does this search hit already have a pin?", against MACs the
 *  server normalized on the way in. Identity on the WRITE path stays the
 *  server's job — the SPA sends the serial as the roster spells it. Deliberately
 *  not the punctuation-blind search form, which would collapse genuinely
 *  different serials and mark the wrong subscriber as already located. */
const normMac = (raw: string | null | undefined): string => (raw ?? "").trim().toUpperCase()

/** Mirrors `inventory._onu_phone`. Separators are the writer's habit rather than
 *  data, so they're stripped instead of refused — a tech writes down the ten
 *  digits the customer gave them, and a form that rejects "98765 43210" is one
 *  that stops being filled in. Deliberately looser than the WhatsApp recipient
 *  rule (`api/users._WA_RE`), which demands international format because Meta's
 *  API does; this number is one a human dials. Kept in step with the server so
 *  the save button never promises what a 422 takes back. */
const phoneOk = (raw: string): boolean =>
  /^\+?\d{7,15}$/.test(raw.replace(/[\s\-().]/g, ""))

export function SurveyPage() {
  const { scopeOrg, user } = useAuth()
  const isMobile = useIsMobile()
  const isWorker = !!user && !user.is_superadmin && user.role === "worker"
  const qc = useQueryClient()
  const [search, setSearch] = useState("")
  const [target, setTarget] = useState<Target | null>(null)

  const devices = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })

  // ONUs are NOT org_devices rows — they live in the SNMP roster, keyed by the
  // MAC on the sticker — so they can't come out of the inventory list and need
  // their own lookup. Debounced because this one is a server round trip on a
  // handset's connection, unlike the device filter above.
  const [onuNeedle, setOnuNeedle] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setOnuNeedle(search.trim()), 350)
    return () => clearTimeout(t)
  }, [search])

  const onus = useQuery({
    queryKey: ["onu-search", scopeOrg, onuNeedle],
    queryFn: () => inventoryApi.onuSearch(scopeOrg, onuNeedle),
    enabled: !!scopeOrg && onuNeedle.length >= ONU_MIN_CHARS,
  })

  // Which subscribers already carry a pin, so a tech isn't asked to re-record
  // one — and so a REFERENCE ONU is never presented as an ordinary drop.
  const places = useQuery({
    queryKey: ["onu-places", scopeOrg],
    queryFn: () => inventoryApi.onuPlaces(scopeOrg),
    enabled: !!scopeOrg,
  })
  // Keyed by MAC, carrying the whole row: the survey needs the operator's own
  // name and the existing pin as well as "is it placed", and three parallel maps
  // would drift.
  const placedMacs = useMemo(() => {
    const m = new Map<string, OnuPlace>()
    for (const p of places.data?.places ?? []) m.set(p.mac, p)
    return m
  }, [places.data])

  const all = useMemo(() => devices.data?.devices ?? [], [devices.data])

  const unplaced = useMemo(
    () => all.filter((d) => d.lat == null || d.lng == null),
    [all])

  // "Placed today, by me" — field staff need to watch their own work accumulate
  // or they stop trusting the tool, and it is the only affordance for spotting a
  // mis-tap while still standing near the thing that was mis-tapped.
  const mine = useMemo(() => {
    const today = new Date(); today.setHours(0, 0, 0, 0)
    return all
      .filter((d) => d.placed_by === user?.username && d.placed_at &&
                     new Date(d.placed_at.replace(" ", "T") + "Z") >= today)
      .sort((a, b) => (b.placed_at ?? "").localeCompare(a.placed_at ?? ""))
  }, [all, user])

  const needle = search.trim().toLowerCase()
  const results = useMemo(() => {
    if (!needle) return unplaced
    return all.filter((d) => d.name.toLowerCase().includes(needle) ||
                             (d.ip_address ?? "").includes(needle))
  }, [all, unplaced, needle])

  // Flattened for the list: the tech is looking for one sticker, not for which
  // OLT it turned out to be on — that's the ANSWER, so it belongs in the row.
  const onuHits = useMemo(() => {
    const out: {
      mac: string; who: string; where: string; located: boolean; witness: boolean
      label: string | null; phone: string | null; walked: string | null
      at: { lat: number; lng: number } | null
    }[] = []
    for (const m of onus.data?.matches ?? []) {
      for (const o of m.onus) {
        const mac = normMac(o.serial)
        if (!mac) continue
        const p = placedMacs.get(mac)
        out.push({
          mac,
          // The OPERATOR's name wins over the walked one — it is the newer,
          // deliberate answer, and the same precedence `refTitle` uses on the
          // map so a subscriber isn't called two different things in two places.
          who: p?.label || o.name || o.serial || mac,
          where: `${m.device_name}${o.pon_port ? ` · PON ${o.pon_port}` : ""}`,
          label: p?.label ?? null,
          phone: p?.phone ?? null,
          walked: o.name ?? null,
          at: p ? { lat: p.lat, lng: p.lng } : null,
          located: !!p,
          witness: p?.witness === true,
        })
      }
    }
    return out
  }, [onus.data, placedMacs])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["inventory", scopeOrg] })
    void qc.invalidateQueries({ queryKey: ["onu-places", scopeOrg] })
    void qc.invalidateQueries({ queryKey: ["onu-coverage", scopeOrg] })
  }

  if (!scopeOrg) return <NeedsOrg />

  // Equipment and subscribers are DIFFERENT SIZES of job — tens of boxes against
  // thousands of drops — so one merged "N left" counter is useless for both. The
  // header states the equipment figure (a survey you can finish this week) and
  // the subscribers panel carries its own coverage bar.
  const gearLeft = unplaced.length

  // A worker on a phone has no map to go to — app-shell's FieldShell redirects
  // every other path back here — so an affordance that navigates there would
  // bounce. Same condition as the shell's, deliberately: two places deciding
  // "is this the field handset" by different rules is how one of them ends up
  // offering a dead link.
  const fieldOnly = isWorker && isMobile

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 px-4 py-4">
      <header className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Survey</h1>
          <p className="text-2xs text-faint-foreground">
            Record where equipment physically stands. Connections stay with the owner.
          </p>
        </div>
        {devices.isSuccess && (
          <span className={cn(
            "shrink-0 rounded-full border px-2.5 py-1 text-xs font-semibold tabular-nums",
            gearLeft === 0 ? "bg-card text-faint-foreground" : "bg-card")}>
            {gearLeft === 0 ? "gear done" : `${gearLeft} gear left`}
          </span>
        )}
      </header>

      {/* Start/End shift. It lives HERE because /survey is the only screen a
          worker on a phone gets (FieldShell), and marking on-shift is the first
          thing they do when the van leaves. It is a declaration and nothing
          else: the tracker app's own switch is what actually transmits, and the
          gap between the two is the alarm — see components/field-tracking-card. */}
      <ShiftButton className="items-start" />

      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search equipment or a subscriber MAC…"
          className="h-11 pl-9"
          autoCapitalize="none"
          autoCorrect="off"
        />
      </div>

      {devices.isLoading && <Skeleton className="h-40 w-full rounded-xl" />}
      {devices.isError && (
        <p className="text-sm text-destructive">
          {devices.error instanceof ApiError ? devices.error.message : "Failed to load devices"}
        </p>
      )}

      {devices.isSuccess && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">
              {needle ? "Matches" : "Not yet placed"}
            </span>
            <span className="text-2xs text-faint-foreground tabular-nums">{results.length}</span>
          </div>
          {results.length === 0 && (
            <div className="flex items-center gap-3 px-4 py-6 text-sm text-muted-foreground">
              <StatusDot tone="success" />
              {needle ? "Nothing matches that." : "Everything has a location."}
            </div>
          )}
          {results.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={() => setTarget({ kind: "device", device: d })}
              className="wisp-row flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-foreground/5"
            >
              <MapPin className={cn("size-4 shrink-0",
                d.lat == null ? "text-faint-foreground" : "text-success")} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{d.name}</span>
                <span className="block truncate text-2xs text-faint-foreground">
                  {d.device_type ?? "device"}
                  {d.ip_address ? ` · ${d.ip_address}` : ""}
                  {d.lat != null ? ` · ${placedNote(d)}` : ""}
                </span>
              </span>
            </button>
          ))}
        </section>
      )}

      {/* Subscribers by OLT. A fleet has thousands of drops, so the fleet-wide
          unplaced list is not a thing anybody can work down — but the PER-OLT
          one is exactly how a field walk is organised, and the coverage bar is
          the only place the real size of the job is visible. (The first cut
          offered search alone and reported "0 left" once the gear was done,
          while 2,155 of 2,156 subscribers had no pin.) */}
      {!needle && <SubscriberCoverage org={scopeOrg} onPick={setTarget}
                                      expandable={!fieldOnly} />}

      {/* Search hits — the other entry point: a tech holding one sticker. */}
      {needle.length >= ONU_MIN_CHARS && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">Subscribers</span>
            <span className="text-2xs text-faint-foreground tabular-nums">
              {onus.isFetching ? "…" : onuHits.length}
            </span>
          </div>
          {onus.isFetching && onuHits.length === 0 && (
            <div className="px-4 py-6"><Skeleton className="h-4 w-40" /></div>
          )}
          {!onus.isFetching && onuHits.length === 0 && (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              No ONU matches that MAC or name.
            </div>
          )}
          {onuHits.map((o) => (
            /* Located hits carry the same green wash the per-OLT list uses.
               One page had two ways of saying "done" — a green pin here, a
               green row there — and the weaker one reads as a different claim. */
            <div key={o.mac} className={cn("wisp-row flex items-center gap-1 pr-2",
              o.located && "bg-success/[0.18] dark:bg-success/[0.11]")}>
              <button
                type="button"
                onClick={() => setTarget({
                  kind: "onu", mac: o.mac, who: o.who, where: o.where,
                  located: o.located, label: o.label, phone: o.phone,
                  walked: o.walked, at: o.at,
                })}
                className={cn("flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left",
                  o.located ? "hover:bg-success/15" : "hover:bg-foreground/5")}
              >
                {o.located
                  ? <Check className="size-4 shrink-0 text-success" />
                  : <MapPin className="size-4 shrink-0 text-faint-foreground" />}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-sm font-medium">{o.who}</span>
                  <span className="block truncate text-2xs text-faint-foreground">{o.where}</span>
                </span>
                {/* A reference ONU is somebody's claim about a power supply, and
                    the handset must say so before a tech re-pins it — the survey
                    preserves the flag, but silence here would read as "this is an
                    ordinary drop". */}
                {o.witness && <Chip tone="info">reference</Chip>}
              </button>
              {/* Finding a subscriber and going to where they are were two
                  different journeys until now — the search could name them but
                  had no way to put them on the map. Only offered once there IS
                  a location; on an unplaced one the row's own tap records it. */}
              {o.located && !fieldOnly && (
                <Button asChild size="icon" variant="ghost" className="size-9 shrink-0"
                        title="Show on map">
                  <Link to={`/map?onu=${encodeURIComponent(o.mac)}`}>
                    <Navigation className="size-4" />
                  </Link>
                </Button>
              )}
            </div>
          ))}
          {onus.data?.truncated && (
            <p className="px-4 py-2.5 text-2xs text-faint-foreground">
              Showing the first matches only. Type more of the MAC.
            </p>
          )}
        </section>
      )}

      {mine.length > 0 && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">Placed today</span>
            <span className="text-2xs text-faint-foreground tabular-nums">{mine.length}</span>
          </div>
          {mine.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={() => setTarget({ kind: "device", device: d })}
              className="wisp-row flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-foreground/5"
            >
              <Check className="size-4 shrink-0 text-success" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{d.name}</span>
                <span className="block truncate text-2xs text-faint-foreground">{placedNote(d)}</span>
              </span>
              <Chip tone={fixTone(d.accuracy_m)}>{fixLabel(d.accuracy_m)}</Chip>
            </button>
          ))}
        </section>
      )}

      {/* Thumb zone. Plant discovered on a walk is the whole reason the passive
          map has never been filled in — most splitters have no row until
          somebody stands at one.

          NOT offered on the field handset (operator's call, 2026-07-31): the
          worker's survey is subscribers, and creating plant is the one thing on
          this screen that adds a row to `org_devices` rather than a coordinate
          to one. The ROUTE stays open — `field-passive` is still worker-reachable
          and unchanged — so this is a narrowing of the screen, not of the
          permission; an owner surveying on a desktop still gets the button. */}
      {!fieldOnly && (
        <div className="sticky bottom-0 -mx-4 mt-auto border-t bg-background/95 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] backdrop-blur">
          <Button
            className="h-12 w-full text-sm"
            variant="outline"
            onClick={() => setTarget({ kind: "passive" })}
          >
            <Plus className="size-4" /> New splitter here
          </Button>
        </div>
      )}

      <CaptureSheet
        target={target}
        onClose={() => setTarget(null)}
        onDone={invalidate}
        placed={all.filter((d) => d.lat != null && d.lng != null)}
        devices={all}
        fieldOnly={fieldOnly}
        org={scopeOrg}
      />
    </div>
  )
}

/** Subscriber coverage, and the per-OLT queue behind it.
 *
 *  The denominator is the freshest-walk roster, so it counts drops a tech can
 *  actually go and find rather than every slot an ONU has ever occupied
 *  (`onu_optics` never deletes a vacated one). Collapsed by default and one OLT
 *  deep: the fleet's whole unplaced set is thousands of rows, and nobody works
 *  a list like that — they work an area.
 *
 *  An opened OLT lists BOTH halves, the located ones first under a green wash
 *  and the queue behind them, each in slot order. Showing only what was left
 *  made the expanded list disagree with the `placed/total` it was opened from,
 *  and a tech revisiting a street had no way to see which drops on the pole
 *  were already recorded. Both halves come off one server pass, so `located` IS
 *  that counter (`api/devices.onu_coverage`).
 *
 *  `expandable` false is the FIELD HANDSET (operator's call, 2026-07-31): the
 *  OLT rows stay, because progress per OLT is the only thing that says how much
 *  of the job is left, but they no longer open a browsable list of somebody
 *  else's customers. The remaining way in is the search box at the top — a tech
 *  standing at a drop is holding one sticker, not choosing from a roster. The
 *  drill-down query is skipped entirely rather than merely hidden, so a handset
 *  never pays for a list it cannot open. */
function SubscriberCoverage({ org, onPick, expandable = true }: {
  org: string | null | undefined
  onPick: (t: Target) => void
  expandable?: boolean
}) {
  const [openOlt, setOpenOlt] = useState<number | null>(null)

  const coverage = useQuery({
    queryKey: ["onu-coverage", org],
    queryFn: () => inventoryApi.onuCoverage(org),
    enabled: !!org,
  })
  const drill = useQuery({
    queryKey: ["onu-coverage", org, openOlt],
    queryFn: () => inventoryApi.onuCoverage(org, openOlt!),
    enabled: !!org && expandable && openOlt != null,
  })

  // Done FIRST, as its own block, rather than interleaved into slot order.
  // Interleaving was tried and is wrong at real scale: HLY-OLT-2 carries 219
  // subscribers of which 5 are located, so in walk order the green rows sit
  // three screens down among EPON0/3 and 0/8 — a "this is done" mark nobody can
  // see. The done set is the small, finite thing an operator opens an OLT to
  // check; the queue behind it is hundreds of rows nobody reads to the end.
  // Slot order still holds WITHIN each block (the server's), so a tech keeps
  // their place between visits either way.
  //
  // `located` is defaulted because an SPA build deploys without a central
  // restart: until the backend restarts, this reply has the unplaced half only,
  // and the list must degrade to what it used to be rather than crash.
  const located: OnuCoverageLocatedRow[] = drill.data?.located ?? []
  const unplaced: OnuCoverageRow[] = drill.data?.unplaced ?? []

  const c = coverage.data
  if (coverage.isLoading) return <Skeleton className="h-32 w-full rounded-xl" />
  if (!c || c.total === 0) return null

  const pct = Math.round((c.placed / c.total) * 100)

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <span className="wisp-eyebrow">Subscribers</span>
        <span className="text-2xs text-faint-foreground tabular-nums">
          {c.placed} of {c.total} located
        </span>
      </div>

      <div className="px-4 py-3">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-success transition-[width]"
               style={{ width: `${Math.max(pct, c.placed > 0 ? 1 : 0)}%` }} />
        </div>
        <p className="mt-2 text-2xs text-faint-foreground">
          {/* "Recorded", never "occupied" — the same honesty the splitter load
              bar keeps. A subscriber with no pin is one nobody has walked to,
              not one that doesn't exist. */}
          {c.placed === 0
            ? expandable
              ? "No subscriber locations recorded yet. Pick an OLT to start."
              : "No subscriber locations recorded yet. Search a MAC or a customer name to start."
            : expandable
              ? `${pct}% recorded · pick an OLT to continue`
              : `${pct}% recorded · search a MAC or a customer name to add one`}
        </p>
      </div>

      {c.olts.map((o) => {
        const left = o.total - o.placed
        const open = expandable && openOlt === o.device_id
        // Read-only on the handset: a row that reports progress and opens
        // nothing is a <div>, not a <button> — an inert control that still
        // depresses under the thumb reads as a broken screen, not a narrowed one.
        const Row = expandable ? "button" : "div"
        return (
          <div key={o.device_id} className="wisp-row">
            <Row
              {...(expandable
                ? { type: "button" as const,
                    onClick: () => setOpenOlt(open ? null : o.device_id) }
                : {})}
              className={cn("flex w-full items-center gap-3 px-4 py-3 text-left",
                expandable && "hover:bg-foreground/5")}
            >
              {expandable && (
                <ChevronRight className={cn("size-4 shrink-0 text-faint-foreground transition-transform",
                  open && "rotate-90")} />
              )}
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                {o.device_name ?? `OLT ${o.device_id}`}
              </span>
              <span className="shrink-0 text-2xs text-faint-foreground tabular-nums">
                {left === 0 ? "all located" : `${o.placed}/${o.total}`}
              </span>
            </Row>

            {open && (
              <div className="border-t bg-muted/30">
                {drill.isLoading && <div className="px-4 py-3"><Skeleton className="h-4 w-32" /></div>}

                {located.length > 0 && (
                  <div className="flex items-center justify-between gap-2 border-t border-border-subtle bg-success/10 px-4 py-1.5 dark:bg-success/[0.07]">
                    <span className="wisp-eyebrow">Located</span>
                    <span className="text-2xs text-faint-foreground tabular-nums">
                      {located.length}
                    </span>
                  </div>
                )}
                {/* Done, and it says so with a wash of the same success tone
                    the coverage bar uses. Still a button: the reason to tap a
                    done row is to fix a name, add the number a pin placed
                    before 2026-07-31 has no room for, or move a pin that went
                    in from the wrong side of the road. */}
                {located.map((l) => (
                  <button
                    key={l.mac}
                    type="button"
                    onClick={() => onPick({
                      kind: "onu", mac: l.mac,
                      who: l.label || l.name || l.mac,
                      where: `${l.device_name ?? ""}${l.pon_port ? ` · PON ${l.pon_port}` : ""}`,
                      located: true, label: l.label, phone: l.phone,
                      walked: l.name ?? null,
                      // Carried in so reopening this is a RENAME by default —
                      // re-placing would restamp the provenance, turning a real
                      // GPS fix into a hand-placed point over a typo.
                      at: l.lat != null && l.lng != null
                        ? { lat: l.lat, lng: l.lng } : null,
                    })}
                    /* The fill is measured PER MODE, not one alpha for both.
                       The two success tones sit at opposite luminances by design
                       (#2f7d4f darkened to clear AA on white, #5fbe83 on
                       near-black), so the 10% that reads as a clear tint in dark
                       lands at #E7EEEB on white — a grey nobody would call
                       green, which is the whole signal this row carries. Judge
                       these two numbers on screen in both themes, never by
                       matching them to each other. */
                    className="flex w-full items-center gap-3 border-t border-border-subtle bg-success/[0.18] px-4 py-2.5 text-left hover:bg-success/25 dark:bg-success/[0.11] dark:hover:bg-success/[0.17]"
                  >
                    <Check className="size-3.5 shrink-0 text-success" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-xs">
                        {l.label || l.name || l.mac}
                      </span>
                      <span className="block truncate text-2xs text-faint-foreground">
                        <span className="font-mono">{l.mac}</span>
                        {l.placed_at ? ` · ${ago(l.placed_at)}` : ""}
                        {l.placed_by ? ` by ${l.placed_by}` : ""}
                      </span>
                    </span>
                    {/* A reference ONU carries somebody's claim about a power
                        supply. The survey preserves that flag, but a tech about
                        to re-pin one has to know it is not an ordinary drop. */}
                    {l.witness && <Chip tone="info">reference</Chip>}
                    <span className="shrink-0 text-2xs text-faint-foreground">
                      {l.pon_port ?? ""}
                    </span>
                  </button>
                ))}

                {drill.isSuccess && unplaced.length === 0 && (
                  <p className="border-t border-border-subtle px-4 py-3 text-2xs text-muted-foreground">
                    Every subscriber on this OLT has a location.
                  </p>
                )}
                {/* The queue only needs a heading once there is a block above it
                    to be told apart from — on an OLT nobody has started, the
                    panel is the queue and a label would be noise. */}
                {located.length > 0 && unplaced.length > 0 && (
                  <div className="flex items-center justify-between gap-2 border-t border-border-subtle px-4 py-1.5">
                    <span className="wisp-eyebrow">Still to visit</span>
                    <span className="text-2xs text-faint-foreground tabular-nums">
                      {unplaced.length}
                    </span>
                  </div>
                )}
                {unplaced.map((u) => (
                  <button
                    key={u.mac}
                    type="button"
                    onClick={() => onPick({
                      kind: "onu", mac: u.mac,
                      who: u.name || u.mac,
                      where: `${u.device_name ?? ""}${u.pon_port ? ` · PON ${u.pon_port}` : ""}`,
                      // By definition unplaced, so there is no stored label,
                      // number or pin to carry in.
                      located: false, label: null, phone: null,
                      walked: u.name ?? null, at: null,
                    })}
                    className="flex w-full items-center gap-3 border-t border-border-subtle px-4 py-2.5 text-left hover:bg-foreground/5"
                  >
                    <MapPin className="size-3.5 shrink-0 text-faint-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-xs">
                        {u.name || u.mac}
                      </span>
                      {u.name && (
                        <span className="block truncate font-mono text-2xs text-faint-foreground">
                          {u.mac}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 text-2xs text-faint-foreground">
                      {u.pon_port ?? ""}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </section>
  )
}

/** The capture itself. A sheet rather than a route so closing it never loses the
 *  list position a worker walked down. */
function CaptureSheet({ target, onClose, onDone, placed, devices, fieldOnly, org }: {
  target: Target | null
  onClose: () => void
  onDone: () => void
  placed: OrgDevice[]
  /** every device in the org — the passive capture needs the whole set to
   *  suggest a free name and to find the box that feeds where you're standing */
  devices: OrgDevice[]
  fieldOnly: boolean
  org: string | null | undefined
}) {
  const navigate = useNavigate()
  const { canWrite } = useAuth()
  const { fix, phase, error, start, reset } = useGpsFix()
  const [name, setName] = useState("")
  const [phone, setPhone] = useState("")
  const [split, setSplit] = useState<SplitRatio>({ ratio: null, inputs: null })
  // The box this one hangs off. OWNER-ONLY, and that is a capability limit
  // rather than a screen one: `field-passive` deliberately cannot set a parent
  // (it is the one worker-reachable route that adds an `org_devices` row, and
  // the parent link is the field that would give it consequences). An owner has
  // `update`, so for them the feeder is captured with everything else instead of
  // being left as desk work nobody comes back to.
  //
  // `null` is a real value: "no feeder recorded" has to stay sayable, or the
  // nearest box within 2 km silently becomes a claim.
  const [feederId, setFeederId] = useState<number | null>(null)
  const [feederTouched, setFeederTouched] = useState(false)
  // The PON, and whether it has been chosen by hand. Untouched, it tracks
  // whatever the feeder implies — so changing the feeder moves it, which is the
  // whole point: a PON label belongs to one OLT's roster and carrying a stale
  // one across would bind this box to a port the new OLT never reported.
  const [pon, setPon] = useState("")
  const [ponTouched, setPonTouched] = useState(false)
  const [sameAs, setSameAs] = useState<OrgDevice | null>(null)
  // Where the pin currently sits when it isn't simply the live GPS fix: either
  // seeded from an existing placement (reopened to rename) or dragged by hand.
  const [pin, setPin] = useState<{ lat: number; lng: number } | null>(null)
  // …and whether the operator MOVED it this time. The two are separate because
  // a seeded pin and a dragged one mean opposite things: one is "leave this
  // alone", the other is "I know better than the chip".
  const [moved, setMoved] = useState(false)
  // The adjust-pin map's fold. Opens on its own for a REOPENED placement, where
  // seeing where the pin already sits is the point of coming back.
  const [mapOpen, setMapOpen] = useState(false)

  const open = target != null
  const isPassive = target?.kind === "passive"

  // Arming on OPEN rather than on a button press: the worker is already standing
  // where the answer is, and a fix takes ~10 s to converge — asking them to
  // press "start" first spends that time on a tap.
  const openChange = (next: boolean) => {
    if (!next) { reset(); onClose(); return }
  }
  const onOpenAutoFocus = () => {
    reset(); setSameAs(null)
    // Same default as the map sheet, for the same reason: a splitter that
    // splits nothing is not a thing anyone stocks. "None" stays reachable —
    // an unrecorded ratio is a real answer and must never be guessed.
    setSplit({ ratio: target?.kind === "passive" ? 8 : null, inputs: null })
    setFeederId(null); setFeederTouched(false)
    setPon(""); setPonTouched(false)
    // The subscriber's details start from whatever was recorded before — these
    // fields are as much for CORRECTING an entry as filling a missing one. A pin
    // placed before the number was captured opens with an empty phone box, which
    // is how the backfill happens: the save won't go through without it.
    // A plant capture opens with a free name already in the box. The field is
    // required, and a required field with no default is what makes a fast
    // capture stop and think — every one of these gets renamed to a landmark by
    // whoever is standing at it anyway.
    setName(target?.kind === "onu" ? (target.label ?? "")
      : target?.kind === "passive" ? suggestPlantName(PLANT_KIND, devices) : "")
    setPhone(target?.kind === "onu" ? (target.phone ?? "") : "")
    // An ONU that already has a pin opens ON that pin, not on a fresh fix: the
    // common reason to reopen a located subscriber is to fix its NAME, and
    // silently re-pinning them to wherever the tech happens to be standing would
    // corrupt the plant record as a side effect of a typo fix. "Use my GPS"
    // takes over when they really have moved.
    const seeded = target?.kind === "onu" && target.at ? target.at : null
    setPin(seeded)
    setMoved(false)
    setMapOpen(seeded != null)
    start()
  }

  const placeDevice = useMutation({
    mutationFn: (body: { id: number; lat: number; lng: number; accuracy_m: number | null; source: "gps" | "manual" }) =>
      inventoryApi.placeInField(body),
    onSuccess: () => { toast.success("Location saved"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the location"),
  })

  const createPassive = useMutation({
    // TWO calls for an owner, and the order matters.
    //
    // `field-passive` goes first because it is the only route that stamps
    // PROVENANCE — `accuracy_m`, `place_source`, `placed_by` — which is the
    // whole reason a field capture and a desk placement are different claims
    // about the same two numbers. Creating through the owner's plain `create` +
    // `setLocation` instead would land the box with no accuracy at all, and
    // `set_org_device_location` deliberately WIPES provenance.
    //
    // The follow-up `update` then adds the one field the field route cannot
    // carry (the parent, and the PON that comes with it). It touches no location
    // column, so the GPS stamp survives it.
    mutationFn: async ({ body, parentId, pon, region }: {
      body: Parameters<typeof inventoryApi.createFieldPassive>[0]
      parentId: number | null
      pon: string | null
      region: string | null
    }) => {
      const { id } = await inventoryApi.createFieldPassive(body)
      if (parentId == null) return { id, wired: false }
      try {
        await inventoryApi.update(id, {
          name: body.name, ip_address: "", device_type: body.device_type,
          region, tags: [], parent_device_id: parentId,
          pon_port: pon, split_ratio: body.split_ratio ? Number(body.split_ratio) : null,
          split_inputs: body.split_inputs ?? null,
        })
        return { id, wired: true }
      } catch {
        // The box and its pin are already saved, so this is a partial success,
        // not a failure — say which half is missing rather than an error that
        // implies nothing was recorded and invites a duplicate capture.
        toast.warning(`${body.name} was recorded, but its feeder wasn't saved`, {
          description: "Set it from the map or the Network page.",
        })
        return { id, wired: false }
      }
    },
    onSuccess: () => { toast.success("Plant recorded"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't record this"),
  })

  const locateOnu = useMutation({
    mutationFn: (body: Parameters<typeof inventoryApi.locateOnuInField>[0]) =>
      inventoryApi.locateOnuInField(body),
    onSuccess: (_r, body) => {
      // The confirmation carries a way to SEE the result. A subscriber pin lands
      // on a layer that is off by default and only draws from street zoom, so
      // "saved" with no route to it is how the first placement looked like it
      // had done nothing at all. Not offered on the field handset, which has no
      // map to reach.
      toast.success("Subscriber located", fieldOnly ? undefined : {
        action: {
          label: "View on map",
          onClick: () => navigate(`/map?onu=${encodeURIComponent(body.mac)}`),
        },
      })
      onDone(); reset(); onClose()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the location"),
  })

  const nameOnu = useMutation({
    mutationFn: (body: { mac: string; label: string; phone: string }) =>
      inventoryApi.nameOnuInField(body),
    onSuccess: () => { toast.success("Details saved"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the details"),
  })

  const busy = placeDevice.isPending || createPassive.isPending ||
    locateOnu.isPending || nameOnu.isPending

  // Three sources, most-specific first.
  //
  // A NUDGED pin wins outright: somebody standing there who can see the rooftop
  // on imagery knows better than the chip. It records as `manual` with NO
  // accuracy — not because a dragged point is worse (it is usually far better)
  // but because `accuracy_m` means "the radius this measurement is good to", and
  // a hand-placed point has no such radius. Carrying the old GPS figure over
  // would attach a measurement to a point that was never measured. Same rule
  // `set_org_device_location` follows when the owner drags on the desktop.
  //
  // A co-located capture BORROWS the neighbour's coordinates rather than taking
  // its own — two boxes in one rack are at one point, and two fixes taken a
  // minute apart would scatter them by the accuracy radius.
  const coords: { lat: number; lng: number; accuracy: number | null; source: "gps" | "manual" } | null =
    pin
      ? { lat: pin.lat, lng: pin.lng, accuracy: null, source: "manual" }
      : sameAs && sameAs.lat != null && sameAs.lng != null
        ? { lat: sameAs.lat, lng: sameAs.lng, accuracy: null, source: "manual" }
        : fix
          ? { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy, source: "gps" }
          : null

  // The box that feeds where you're standing. DERIVED rather than stored, so it
  // keeps improving as the fix converges — a first cell-tower estimate 60 m out
  // can name the wrong splitter, and a suggestion frozen at that moment would be
  // the one that got saved. Once the operator picks for themselves their choice
  // wins outright and nothing recomputes under them.
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const suggestedFeeder = useMemo(
    () => (isPassive && canWrite && coords
      ? nearestFeeder(coords.lat, coords.lng, devices) : null),
    [isPassive, canWrite, coords, devices])
  const effectiveFeederId = feederTouched ? feederId : suggestedFeeder?.device.id ?? null
  const feeder = effectiveFeederId != null ? byId.get(effectiveFeederId) ?? null : null
  const feederPon = ponFor(feeder, byId)
  // The PON, as an OPTIONAL dropdown off whichever OLT heads this chain. It
  // starts on the inherited label when the feeder is a splitter (one fibre goes
  // in, so that is the answer) and empty under an OLT, where there is a real
  // choice to make. Either way it is the OLT's OWN labels: what gets stored has
  // to match what the walk stores, and only the walk knows the spelling.
  const ponOlt = oltHead(feeder, byId)
  const { pons, loading: ponsLoading } = usePonOptions(ponOlt?.id, isPassive && canWrite)
  const effectivePon = ponTouched ? pon : (feederPon.inherited ? feederPon.pon ?? "" : "")
  const feederChoices = useMemo(
    () => (isPassive && canWrite && coords
      ? feederOptions(coords.lat, coords.lng, devices).slice(0, 25) : []),
    [isPassive, canWrite, coords, devices])

  // Reopened a located subscriber and didn't touch the pin ⇒ this is a RENAME,
  // and it must not go through the placement route. Re-placing would restamp
  // accuracy/source/placed_by, so fixing a spelling would downgrade a real 6 m
  // GPS fix to a hand-placed point and reattribute the visit. Dragging the pin,
  // or pressing "Back to GPS", turns it back into a real placement.
  const renameOnly = target?.kind === "onu" && target.located && pin != null && !moved

  const good = renameOnly ||
    (coords != null && (coords.accuracy == null || coords.accuracy <= GOOD_FIX_M))

  // A subscriber row is worth having only if a crew can act on it, so NAME,
  // NUMBER and LOCATION are captured together or not at all (operator's call,
  // 2026-07-31). The server enforces the same three in
  // `inventory.clean_field_onu_payload` — this half only keeps the button from
  // promising a save that would come back 422.
  const onuDetailsOk = name.trim().length > 0 && phoneOk(phone)
  // Reopened to correct something: don't offer a save that writes back exactly
  // what is already stored.
  const detailsChanged = target?.kind === "onu" &&
    (name.trim() !== (target.label ?? "") || phone.trim() !== (target.phone ?? ""))

  const canSave = !busy && (
    renameOnly ? onuDetailsOk && detailsChanged
      : target?.kind === "onu" ? onuDetailsOk && coords != null
        : coords != null && (!isPassive || name.trim().length > 0))

  const save = () => {
    if (renameOnly && target?.kind === "onu") {
      nameOnu.mutate({ mac: target.mac, label: name.trim(), phone: phone.trim() })
      return
    }
    if (!coords) return
    if (isPassive) {
      createPassive.mutate({
        body: {
          name: name.trim(), device_type: PLANT_KIND,
          lat: coords.lat, lng: coords.lng,
          accuracy_m: coords.accuracy, source: coords.source,
          split_ratio: split.ratio ? String(split.ratio) : null,
          split_inputs: split.inputs,
          // Region and PON ride the FIRST call as well as the follow-up, even
          // though the update would write them anyway: if the parent write is
          // the half that fails, everything it could have carried is already
          // stored, and the gap is exactly the one the warning names.
          region: feeder?.region ?? null,
          pon_port: effectivePon || null,
        },
        parentId: effectiveFeederId,
        pon: effectivePon || null,
        region: feeder?.region ?? null,
      })
    } else if (target?.kind === "device") {
      placeDevice.mutate({
        id: target.device.id, lat: coords.lat, lng: coords.lng,
        accuracy_m: coords.accuracy, source: coords.source,
      })
    } else if (target?.kind === "onu") {
      locateOnu.mutate({
        mac: target.mac, lat: coords.lat, lng: coords.lng,
        accuracy_m: coords.accuracy, source: coords.source,
        // Name and number ride the placement, so a first visit records all three
        // in one press rather than making the tech save twice.
        label: name.trim(), phone: phone.trim(),
      })
    }
  }

  const title = isPassive ? "New splitter here"
    : target?.kind === "device" ? target.device.name
      : target?.kind === "onu" ? target.who : ""
  const replacing =
    (target?.kind === "device" && target.device.lat != null) ||
    (target?.kind === "onu" && target.located)

  return (
    <Sheet open={open} onOpenChange={openChange}>
      <SheetContent
        side="bottom"
        onOpenAutoFocus={onOpenAutoFocus}
        className="max-h-[92svh] gap-0 overflow-y-auto rounded-t-2xl pb-[calc(1rem+env(safe-area-inset-bottom))]"
      >
        <SheetHeader className="px-4 pt-4 pb-2">
          <SheetTitle className="truncate text-base">{title}</SheetTitle>
          {target?.kind === "device" && (
            <p className="truncate text-2xs text-faint-foreground">
              {target.device.device_type ?? "device"}
              {target.device.ip_address ? ` · ${target.device.ip_address}` : ""}
            </p>
          )}
          {target?.kind === "onu" && (
            <p className="truncate text-2xs text-faint-foreground">{target.where}</p>
          )}
        </SheetHeader>

        <div className="flex flex-col gap-4 px-4">
          <FixReadout phase={phase} fix={fix} error={error} borrowed={sameAs}
                      pinned={pin != null} moved={moved} onRetry={start} />

          {/* Aim the pin. A GPS fix is a circle — 25 m of it is a whole compound
              — and the person standing there can see which rooftop the box is
              on, which is the ONLY way a handset beats its own chip.

              FOLDED, like the device panel's Uplinks and "Paged for this device"
              sections: the common capture is "stand there, press save", and 208px
              of map ahead of the name field and the button made the routine case
              pay for the exception. The trigger states the current fix so the
              fold never hides a decision — and it springs open once the pin has
              actually been moved, because then the map IS the answer. */}
          {coords && (
            <div className="flex flex-col rounded-xl border bg-muted/40">
              <button
                type="button"
                onClick={() => setMapOpen((v) => !v)}
                className={cn("flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-foreground/5",
                  mapOpen ? "rounded-t-xl" : "rounded-xl")}
              >
                <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
                  mapOpen && "rotate-90")} />
                <span className="text-2xs font-medium text-muted-foreground">
                  {/* Three states, not two. A REOPENED placement has a pin
                      nobody touched this visit, and calling that "set by hand"
                      describes a drag that didn't happen — the same slip the
                      map's own caption made. */}
                  {moved ? "Pin set by hand"
                    : pin ? "Where this was recorded"
                      : "Adjust pin on map"}
                </span>
                <span className="ml-auto shrink-0 font-mono text-2xs text-faint-foreground">
                  {coords.lat.toFixed(5)}, {coords.lng.toFixed(5)}
                </span>
              </button>
              {mapOpen && (
                <div className="p-2 pt-0">
                  <PinAdjustMap
                    org={org}
                    lat={coords.lat}
                    lng={coords.lng}
                    adjusted={pin != null}
                    moved={moved}
                    onAdjust={(lat, lng) => { setPin({ lat, lng }); setMoved(true) }}
                    // "Back to GPS" drops the seeded pin too, which is how a tech
                    // says "this subscriber really has moved" and turns a rename
                    // back into a placement.
                    onReset={() => { setPin(null); setMoved(false) }}
                  />
                </div>
              )}
            </div>
          )}

          {/* The subscriber's name. It writes `onu_places.label`, NOT the
              roster's name — the OLT's name is rewritten by every SNMP walk, so
              anything typed into that would vanish within ~5 minutes. */}
          {target?.kind === "onu" && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="survey-onu-name">Customer name</Label>
              {/* UPPERCASED as it is typed. The server does it anyway
                  (`inventory._onu_label`), but a phone keyboard that has just
                  autocapitalized one word and left the rest lower-case would
                  then show something different from what gets saved — and the
                  one thing a capture screen must not do is disagree with its own
                  result. `autoCapitalize` alone wouldn't do it: it capitalizes
                  first letters, not the word. */}
              <Input
                id="survey-onu-name"
                value={name}
                onChange={(e) => setName(e.target.value.toUpperCase())}
                placeholder={target.walked || "Not recorded"}
                className="h-11"
                autoCapitalize="characters"
                autoCorrect="off"
              />
              <p className="text-2xs text-faint-foreground">
                {target.walked
                  ? <>Saved here for your records. The OLT keeps calling it{" "}
                      <span className="font-mono">{target.walked}</span>.</>
                  : "Saved here for your records. This ONU has no name on the OLT."}
              </p>
            </div>
          )}

          {/* The number to call. Required alongside the name and the pin: a dark
              drop with a coordinate and nobody to ring is a van sent to knock on
              a door. `inputMode="tel"` gives the phone its keypad rather than a
              full keyboard — this is thumb-typed standing at a pole. */}
          {target?.kind === "onu" && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="survey-onu-phone">Phone number</Label>
              <Input
                id="survey-onu-phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="e.g. 9876543210"
                className="h-11"
                type="tel"
                inputMode="tel"
                autoComplete="off"
                autoCorrect="off"
              />
              {/* Only complains once there is something to complain ABOUT — an
                  error shown against an empty field the tech has not reached yet
                  is noise, and the disabled save button already says "not yet". */}
              {phone.trim().length > 0 && !phoneOk(phone) && (
                <p className="text-2xs text-destructive">
                  That doesn't look like a phone number. 7 to 15 digits.
                </p>
              )}
            </div>
          )}

          {isPassive && (
            <div className="flex flex-col gap-3">
              {/* No type picker: `PLANT_KINDS` is splitter alone, and a control
                  with one option is a control that only costs a glance.

                  Segmented rather than a Select for the ratio, though — four
                  options all worth seeing without a tap, and a thumb standing at
                  a pole hits a 28px-tall segment far more reliably than it lands
                  a dropdown item. Same control and the same default as the map
                  sheet, so one splitter recorded two ways comes out the same. */}
              <div className="flex flex-col gap-1.5">
                <Label>Split ratio</Label>
                <SplitRatioField value={split} onChange={setSplit} />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="survey-name">Name</Label>
                <Input
                  id="survey-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. SPL-MAIN-04"
                  className="h-11 font-mono"
                  autoCapitalize="characters"
                  autoCorrect="off"
                />
              </div>

              {/* The feeder. OWNER-ONLY — see `feederId` — and the single
                  biggest thing this capture was missing: a splitter with no
                  recorded parent is a box on a map that no branch verdict, load
                  bar or cumulative split can say anything about. Standing at it
                  with a GPS fix is a BETTER moment to answer that than a desk
                  is, so the question belongs here. */}
              {canWrite && (
                <div className="flex flex-col gap-1.5">
                  <Label>Fed from</Label>
                  <Select
                    value={effectiveFeederId != null ? String(effectiveFeederId) : NO_FEEDER}
                    onValueChange={(v) => {
                      setFeederTouched(true)
                      setFeederId(v === NO_FEEDER ? null : Number(v))
                      // Hand the PON back to whatever the new feeder implies. A
                      // label chosen against the old OLT's roster is worse than
                      // no label at all under the new one.
                      setPon(""); setPonTouched(false)
                    }}>
                    <SelectTrigger className="h-11"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_FEEDER}>No feeder recorded</SelectItem>
                      {feederChoices.map((o) => (
                        <SelectItem key={o.device.id} value={String(o.device.id)}>
                          {o.device.name}
                          <span className="ml-2 font-mono text-2xs text-faint-foreground">
                            {o.meters == null ? "not placed"
                              : o.meters < 1000 ? `${Math.round(o.meters)} m`
                                : `${(o.meters / 1000).toFixed(1)} km`}
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {/* The suggestion says it is one. Naming the box AND the
                      distance is what lets somebody standing there catch a
                      wrong guess for free, instead of it surfacing months later
                      as a branch-fault verdict pointing at the wrong span. */}
                  {feeder ? (
                    <p className="text-2xs text-faint-foreground">
                      {!feederTouched && suggestedFeeder
                        ? <>Nearest box, {Math.round(suggestedFeeder.meters)} m away. Change it if that isn't the feeder.</>
                        : <>Recorded as fed from {feeder.name}.</>}
                    </p>
                  ) : (
                    <p className="text-2xs text-faint-foreground">
                      Nothing placed within 2 km. You can set the feeder later
                      from the map.
                    </p>
                  )}
                </div>
              )}

              {/* The PON. OPTIONAL, and a dropdown of the OLT's own labels —
                  never typed, because what is stored has to match the walk's
                  spelling exactly or the customer picker on this box comes up
                  empty later, on a different screen, with no clue why.
                  Re-keyed on the OLT, so changing the feeder refreshes it. */}
              {canWrite && ponOlt && (
                <div className="flex flex-col gap-1.5">
                  <Label>PON on {ponOlt.name} (optional)</Label>
                  <Select
                    value={effectivePon || NO_PON}
                    onValueChange={(v) => {
                      setPonTouched(true)
                      setPon(v === NO_PON ? "" : v)
                    }}>
                    <SelectTrigger className="h-11">
                      <SelectValue placeholder="Not recorded" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_PON}>Not recorded</SelectItem>
                      {ponOptions(pons, effectivePon).map((p) => (
                        <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-2xs text-faint-foreground">
                    {ponsLoading ? "Reading PON labels…"
                      : pons.length === 0
                        ? `${ponOlt.name} has no ONU roster yet, so there is nothing to pick from.`
                        : !ponTouched && feederPon.inherited && feederPon.pon
                          ? `Inherited from ${feeder?.name}. One fibre goes into a splitter, so it is on its feeder's PON.`
                          : "Which PON this box hangs off."}
                  </p>
                </div>
              )}

              {/* A WORKER cannot set a parent — `field-passive` has no such
                  field, by design. So the sheet says what will and won't be
                  recorded rather than showing a control that would 403, and the
                  desk work is named rather than implied. */}
              {!canWrite && (
                <p className="text-2xs text-faint-foreground">
                  It joins no parent and is not monitored. The owner wires it into
                  the network on the dashboard.
                </p>
              )}
            </div>
          )}

          {/* Only warn when the pin will actually MOVE. A rename leaves the
              location alone, so warning about replacing it there would be a
              false alarm — and a warning that cries wolf is one nobody reads
              on the visit where it mattered. */}
          {replacing && !renameOnly && (
            <div className="flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2.5 text-2xs">
              <TriangleAlert className="mt-px size-3.5 shrink-0 text-warning" />
              <span>
                {target?.kind === "device"
                  ? <>This already has a location, {placedNote(target.device)}. Saving replaces it.</>
                  : "This subscriber already has a location. Saving replaces it."}
              </span>
            </div>
          )}

          {/* The reference-ONU disclaimer that used to sit here is GONE at the
              operator's request. Only the EXPLANATION went — the guarantee is
              structural and unchanged: `clean_field_onu_payload` has no witness
              key, so a survey pin cannot create a reference point, and
              `field_onu` preserves an existing flag rather than clearing it. A
              subscriber that IS a reference point still says so, on its search
              row. */}

          <SameSpot placed={placed} value={sameAs} onChange={setSameAs} />
        </div>

        {/* STICKY. The sheet grew a 208px map, and on a short handset that
            pushed the primary action below the fold of a scrolling container —
            a capture flow whose save button has to be hunted for is one that
            gets abandoned halfway up a pole. */}
        <div className="sticky bottom-0 z-10 mt-4 flex flex-col gap-2 border-t bg-background/95 px-4 pt-3 pb-1 backdrop-blur">
          <Button
            className="h-12 w-full text-sm"
            disabled={!canSave || !good}
            onClick={save}
          >
            <Check className="size-4" />
            {isPassive ? "Record this plant"
              : renameOnly ? "Save details"
                : target?.kind === "onu" ? "Save subscriber location"
                  : "Save this location"}
          </Button>
          {/* Never a hard block. A worker under canopy still needs to record
              something, and refusing the save is how coordinates end up in a
              WhatsApp message instead of the database — the accuracy rides
              along, so a loose pin is visibly loose rather than silently wrong. */}
          {canSave && !good && (
            <Button variant="ghost" className="h-10 w-full text-xs" onClick={save}>
              Save anyway at {fixLabel(coords?.accuracy ?? null)}
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** The live fix. It shows accuracy the whole time it is converging, because the
 *  number is what tells a worker whether to step out from under the canopy. */
function FixReadout({ phase, fix, error, borrowed, pinned, moved, onRetry }: {
  phase: string
  fix: GpsFix | null
  error: string | null
  borrowed: OrgDevice | null
  pinned: boolean
  moved: boolean
  onRetry: () => void
}) {
  // Once the pin is being carried rather than measured, the fix is no longer
  // what gets saved — so its accuracy chip has to stop being the headline, or
  // the sheet shows "±8 m" above a point that number no longer describes.
  if (pinned) {
    return (
      <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
        <MapPin className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 text-sm">
          {moved ? "Pin placed by hand" : "Existing location"}
          <span className="block text-2xs text-faint-foreground">
            {moved
              ? "Saved as an exact spot rather than a measurement"
              : "Left where it was. Edit the details, or drag the pin to move it."}
          </span>
        </span>
      </div>
    )
  }
  if (borrowed) {
    return (
      <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
        <MapPin className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 text-sm">
          Using <span className="font-medium">{borrowed.name}</span>'s exact spot
          <span className="block text-2xs text-faint-foreground tabular-nums">
            {borrowed.lat?.toFixed(6)}, {borrowed.lng?.toFixed(6)}
          </span>
        </span>
      </div>
    )
  }
  if (phase === "error") {
    return (
      <div className="flex flex-col gap-2.5 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
        <span className="text-sm text-destructive">{error}</span>
        <Button size="sm" variant="outline" className="h-9 self-start" onClick={onRetry}>
          <Crosshair className="size-3.5" /> Retry
        </Button>
      </div>
    )
  }
  const acquiring = phase === "acquiring"
  return (
    <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
      {/* Literal classes, never a template — Tailwind extracts by scanning the
          source text, so a constructed `text-${tone}` compiles to nothing and
          silently renders unstyled. */}
      <Signal className={cn("size-4 shrink-0",
        acquiring && "animate-pulse text-muted-foreground",
        !acquiring && fix && (fix.accuracy <= GOOD_FIX_M ? "text-success" : "text-warning"),
        !acquiring && !fix && "text-faint-foreground")} />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="text-sm font-medium">
            {acquiring ? "Getting a fix…" : fix ? "Located" : "No fix"}
          </span>
          {fix && (
            <Chip tone={fixTone(fix.accuracy)}>{fixLabel(fix.accuracy)}</Chip>
          )}
        </span>
        {fix && (
          <span className="block text-2xs text-faint-foreground tabular-nums">
            {fix.lat.toFixed(6)}, {fix.lng.toFixed(6)}
          </span>
        )}
        {!acquiring && fix && fix.accuracy > GOOD_FIX_M && (
          <span className="block text-2xs text-warning">
            Loose fix. Step into the open and retry for a tighter one.
          </span>
        )}
      </span>
      {!acquiring && (
        <Button size="icon" variant="ghost" className="size-9 shrink-0" onClick={onRetry}>
          <Crosshair className="size-4" />
        </Button>
      )}
    </div>
  )
}

/** Co-location. Six boxes in one rack are at ONE point, and six independent
 *  fixes would scatter them across the accuracy radius and read as six sites on
 *  the map. Picking a neighbour copies its exact coordinates. */
function SameSpot({ placed, value, onChange }: {
  placed: OrgDevice[]
  value: OrgDevice | null
  onChange: (d: OrgDevice | null) => void
}) {
  const [q, setQ] = useState("")
  const [open, setOpen] = useState(false)
  const needle = q.trim().toLowerCase()
  const hits = useMemo(
    () => (needle ? placed.filter((d) => d.name.toLowerCase().includes(needle)).slice(0, 6) : []),
    [placed, needle])

  if (value) {
    return (
      <Button variant="ghost" className="h-9 justify-start px-2 text-xs"
              onClick={() => { onChange(null); setQ(""); setOpen(false) }}>
        <X className="size-3.5" /> Use my own fix instead
      </Button>
    )
  }
  if (!open) {
    return (
      <Button variant="ghost" className="h-9 justify-start px-2 text-xs"
              onClick={() => setOpen(true)}>
        <MapPin className="size-3.5" /> Same spot as another device…
      </Button>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <Input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Which device is it beside?"
        className="h-11"
        autoCapitalize="none"
        autoCorrect="off"
        autoFocus
      />
      {hits.map((d) => (
        <button
          key={d.id}
          type="button"
          onClick={() => { onChange(d); setOpen(false) }}
          className="flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left text-sm hover:bg-foreground/5"
        >
          <MapPin className="size-3.5 shrink-0 text-faint-foreground" />
          <span className="min-w-0 flex-1 truncate">{d.name}</span>
          {isPassiveType(d.device_type) && (
            <span className="shrink-0 text-2xs text-faint-foreground">{d.device_type}</span>
          )}
        </button>
      ))}
    </div>
  )
}
