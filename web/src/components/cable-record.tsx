// THE CABLE: one sheath SEGMENT, the two points it runs between, and what each
// of its cores does at each end.
//
// The ISPs corrected the model on 2026-08-09, and this panel is what that
// correction looks like. Their sentence was *fibre runs between two couplers,
// and at a coupler you join cable to cable or take a core out to a device on a
// single fibre* — plus *any core may carry anything, including a customer line*,
// which is why a customer point is a coupler too. So a cable has ENDS, core N of
// it runs between them by definition, and the run object this panel used to list
// is gone along with the tap, the double-booking checker and two geometry
// contracts.
//
// THREE RULES RUN THROUGH EVERY SURFACE HERE.
//
// A STRAND COLOUR IS A MARK, NEVER TEXT AND NEVER A LINE. The TIA-598 sequence
// contains red, orange, yellow and green — the exact hues this product reserves
// for alarms — so a cable drawn red because it happens to be core 7 is a
// fabricated outage. Colour appears as a swatch beside neutral words: the
// identity-chip grammar from the two-colour-axes pass, where a status chip is
// coloured TEXT and an identity chip is neutral text beside a coloured MARK.
//
// RECORDED IS NEVER OCCUPIED. Three cores written down on a 24F does not leave
// twenty-one free — nobody wrote them down, and unknown is not spare. Exactly
// the splitter-legs rule, and the only capacity claim made anywhere here is
// over-subscription, which is provable either way.
//
// WHERE A CORE GOES IS DERIVED; WHAT IT CARRIES IS TYPED. The first is a fact
// the record holds (the joints at both ends) and the second is the operator's
// own note ("BSNL leased line"). They render differently on purpose, so a note
// can never be mistaken for a finding.
import { useState } from "react"
import {
  ArrowRight, Check, Pencil, Plus, Route, Scissors, Trash2, Waypoints,
} from "lucide-react"
import {
  FIBER_COUNTS, STRAND_COLORS, TUBE_SIZE,
  coresRecordedLabel, strandHex, strandLabel, tubeRows,
} from "@/lib/fiber"
import { Button } from "@/components/ui/button"
import type { Cable, CoreEnd, FibrePoint } from "@/lib/types"
import { cn } from "@/lib/utils"

/** A cable's two ends, as one string. `↔`, NEVER `→`: which end feeds the other
 *  is not a fact about a piece of glass — it is derived by walking out from the
 *  gear. An arrow here would state a direction the record does not have, and
 *  would state it differently depending on which end the operator was standing
 *  at when they drew it. */
export const cableEnds = (c: Cable) =>
  `${c.a.name ?? "unplaced"} ↔ ${c.b.name ?? "unplaced"}`

/** Metres, the way a crew orders drum. Null on an untraced cable — nobody walked
 *  it, and a zero would be a measurement. */
export const cableLength = (m: number | null | undefined): string | null =>
  m == null ? null : m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`

/** The strand's colour as a mark. NEVER as text and never as a line — see the
 *  header. Sized `sm` for a chip-like row, `lg` where a panel has the room for
 *  it to read as a swatch. */
export function StrandSwatch({ coreNo, size = "sm", className }: {
  coreNo: number
  size?: "sm" | "lg"
  className?: string
}) {
  return (
    <span
      className={cn("wisp-strand", size === "lg" && "wisp-strand--lg", className)}
      style={{ "--strand": strandHex(coreNo) } as React.CSSProperties}
      aria-hidden />
  )
}

/** One cable and strand, compressed to a line: `12F · blue, green tube`. */
export function CableSummary({ cores, coreNo, name }: {
  cores: number | null; coreNo: number | null; name?: string | null
}) {
  if (!cores && !name) return null
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {name && <span className="truncate">{name}</span>}
      {cores != null && <span className="font-mono text-xs shrink-0">{cores}F</span>}
      {coreNo != null && (
        <>
          <StrandSwatch coreNo={coreNo} />
          <span className="truncate text-2xs text-muted-foreground">
            {strandLabel(coreNo, cores)}
          </span>
        </>
      )}
    </span>
  )
}

/** What one core does at one end, as words. Null when nothing is recorded — an
 *  absence draws NOTHING rather than a dash, because a dash in a grid this dense
 *  reads as a value. */
function endText(end: CoreEnd | undefined): string | null {
  if (!end) return null
  if (end.terminates) return end.point ?? "this box"
  return `${end.cable_name ?? "cable"} #${end.core_no}`
}

/** THE CORE PLAN: the cable laid out as it is BUILT, twelve to a row.
 *
 *  The layout is the explanation, which is the whole design. Twelve is a buffer
 *  tube, so a 48F draws four rows and each row IS a tube, labelled in its own
 *  colour from the same sequence — picking core 25 means clicking the first
 *  swatch of the third row, which is the motion of finding it in the field with
 *  the sheath open. Nothing here explains the arithmetic because the shape
 *  performs it.
 *
 *  A cell is one of two things and they must never look alike: RECORDED (a
 *  filled swatch, what it does at each end named on hover) or NOT RECORDED —
 *  drawn as an empty WELL rather than a pale swatch, because "nobody wrote this
 *  down" and "this is spare" are different sentences and the second is a claim
 *  this panel is not entitled to make. */
function CorePlan({ cable, selected, onSelect }: {
  cable: Cable
  selected: number | null
  onSelect: (coreNo: number | null) => void
}) {
  if (!cable.cores) {
    return (
      <p className="text-2xs text-faint-foreground">
        Record a fibre count and the core plan appears here.
      </p>
    )
  }
  const rows = tubeRows(cable.cores)
  return (
    <div className="space-y-1.5">
      {rows.map((row) => (
        <div key={row.tube} className="flex items-center gap-1.5">
          {rows.length > 1 && (
            <span className="flex w-12 shrink-0 items-center gap-1"
              title={`${STRAND_COLORS[(row.tube - 1) % TUBE_SIZE].name} tube`}>
              <StrandSwatch coreNo={row.tube} />
              <span className="font-mono text-2xs text-faint-foreground">
                {(row.tube - 1) * TUBE_SIZE + 1}
              </span>
            </span>
          )}
          <div className="flex min-w-0 flex-wrap gap-1">
            {row.cores.map((coreNo) => {
              const plan = cable.plan[String(coreNo)]
              const label = cable.labels[String(coreNo)]
              const recorded = !!plan || !!label
              const where = [endText(plan?.a), endText(plan?.b)]
                .filter(Boolean).join("  ↔  ")
              return (
                <button
                  key={coreNo} type="button"
                  onClick={() => onSelect(selected === coreNo ? null : coreNo)}
                  title={[strandLabel(coreNo, cable.cores), where || null, label]
                    .filter(Boolean).join(" — ")}
                  className={cn(
                    "flex size-6 items-center justify-center rounded transition-colors",
                    selected === coreNo ? "bg-selected" : "hover:bg-foreground/5")}>
                  {recorded
                    ? <StrandSwatch coreNo={coreNo} size="lg" />
                    : <span className="wisp-strand-well" aria-hidden />}
                </button>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

/** The selected core, spelled out: where it goes at each end, and what it
 *  carries. THE ONE SURFACE WITH ROOM to say which of the two it is showing. */
function CoreDetail({ cable, coreNo, canWrite, onLabel, onTrace }: {
  cable: Cable
  coreNo: number
  canWrite: boolean
  onLabel: (coreNo: number, label: string | null) => void
  onTrace?: (coreNo: number) => void
}) {
  const plan = cable.plan[String(coreNo)]
  const label = cable.labels[String(coreNo)] ?? ""
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(label)
  const ends: Array<[string, CoreEnd | undefined, FibrePoint]> = [
    ["at " + (cable.a.name ?? "end A"), plan?.a, cable.a],
    ["at " + (cable.b.name ?? "end B"), plan?.b, cable.b],
  ]
  return (
    <div className="space-y-2 rounded-md bg-muted/60 p-2">
      <div className="flex items-center gap-2">
        <StrandSwatch coreNo={coreNo} size="lg" />
        <span className="text-xs font-medium">
          {strandLabel(coreNo, cable.cores)}
        </span>
        {onTrace && (
          <Button size="sm" variant="ghost" className="ml-auto"
            onClick={() => onTrace(coreNo)}>
            <Route className="size-3" />
            Follow
          </Button>
        )}
      </div>
      <dl className="space-y-1">
        {ends.map(([where, end]) => (
          <div key={where} className="flex items-baseline gap-2 text-2xs">
            <dt className="w-24 shrink-0 truncate text-faint-foreground">{where}</dt>
            <dd className={cn("min-w-0 flex-1 truncate",
              end ? "text-foreground" : "text-faint-foreground")}>
              {endText(end) ?? "nothing recorded"}
            </dd>
          </div>
        ))}
      </dl>
      {/* WHAT IT CARRIES is free text and sits apart from where it GOES, so a
          note the operator typed can never be read as a fact the record
          derived. */}
      {editing ? (
        <div className="flex items-center gap-1">
          <input autoFocus value={draft} maxLength={80}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { onLabel(coreNo, draft.trim() || null); setEditing(false) }
              if (e.key === "Escape") { setDraft(label); setEditing(false) }
            }}
            placeholder="BSNL leased line, village A tower, reserved…"
            className="h-7 min-w-0 flex-1 rounded border border-border bg-background px-1.5 text-2xs" />
          <Button size="sm" variant="ghost"
            onClick={() => { onLabel(coreNo, draft.trim() || null); setEditing(false) }}>
            <Check className="size-3" />
          </Button>
        </div>
      ) : (
        <button type="button" disabled={!canWrite}
          onClick={() => { setDraft(label); setEditing(true) }}
          className={cn("flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-2xs",
            canWrite && "hover:bg-foreground/5")}>
          <span className={label ? "text-foreground" : "text-faint-foreground"}>
            {label || (canWrite ? "Add a note — what this fibre carries" : "no note")}
          </span>
          {canWrite && <Pencil className="ml-auto size-3 shrink-0 text-faint-foreground" />}
        </button>
      )}
    </div>
  )
}

/** THE CABLE PANEL. Opens in the DRILL-IN SLOT, not as a floating card.
 *
 *  That is about interaction, not taxonomy: the right rail is where "the thing
 *  you just opened" goes, and the one time this shipped in the left slot it
 *  opened perfectly about a thousand pixels from where the operator was looking
 *  — and under the unplaced drawer and the site card, which already live there. */
export function CablePanel({
  cable, canWrite, busy, onEdit, onTrace, onRetrace, onSplit, onDelete,
  onLabel, onCore, selectedCore,
}: {
  cable: Cable
  canWrite: boolean
  busy?: boolean
  onEdit: () => void
  onTrace?: (coreNo: number) => void
  onRetrace: () => void
  onSplit: () => void
  onDelete: () => void
  onLabel: (coreNo: number, label: string | null) => void
  onCore: (coreNo: number | null) => void
  selectedCore: number | null
}) {
  const length = cableLength(cable.length_m)
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-sm font-medium">{cable.name}</span>
          {cable.cores != null && (
            <span className="shrink-0 font-mono text-xs text-muted-foreground">
              {cable.cores}F
            </span>
          )}
        </div>
        <p className="flex items-center gap-1.5 truncate text-2xs text-muted-foreground">
          <span className="truncate">{cable.a.name ?? "unplaced"}</span>
          <ArrowRight className="size-3 shrink-0 rotate-0 text-faint-foreground" />
          <span className="truncate">{cable.b.name ?? "unplaced"}</span>
        </p>
        <p className="text-2xs text-faint-foreground">
          {/* Two different absences, said differently. An untraced cable has no
              length because nobody walked it; a cable with no count has no core
              plan because nobody measured the sheath. */}
          {length ? `${length} along the route` : "route not traced"}
          {cable.cores ? ` · ${coresRecordedLabel(cable.cores_recorded, cable.cores)}` : ""}
        </p>
      </div>

      <CorePlan cable={cable} selected={selectedCore} onSelect={onCore} />

      {selectedCore != null && (
        <CoreDetail cable={cable} coreNo={selectedCore} canWrite={canWrite}
          onLabel={onLabel} onTrace={onTrace} />
      )}

      {canWrite && (
        <div className="flex flex-wrap gap-1.5 border-t border-border-subtle pt-2">
          <Button size="sm" variant="outline" onClick={onEdit} disabled={busy}>
            <Pencil className="size-3" />
            Edit
          </Button>
          <Button size="sm" variant="outline" onClick={onRetrace} disabled={busy}>
            <Waypoints className="size-3" />
            {cable.path.length ? "Retrace" : "Trace"}
          </Button>
          <Button size="sm" variant="outline" onClick={onSplit}
            disabled={busy || cable.path.length < 2}
            title={cable.path.length < 2
              ? "Trace this cable first — a coupler stands at a point on its route"
              : "Cut the cable here and splice every core straight through"}>
            <Scissors className="size-3" />
            Open a coupler
          </Button>
          <Button size="sm" variant="ghost" onClick={onDelete} disabled={busy}
            className="text-destructive hover:text-destructive">
            <Trash2 className="size-3" />
          </Button>
        </div>
      )}
    </div>
  )
}

/** The cable list — org-level plant, reachable without guessing which box it
 *  happens to touch. A trunk is not a property of the switch at one end of it. */
export function CableList({ cables, onOpen, onLay, canWrite }: {
  cables: Cable[]
  onOpen: (id: number) => void
  onLay?: () => void
  canWrite: boolean
}) {
  return (
    <div className="space-y-2">
      {canWrite && onLay && (
        <Button size="sm" variant="outline" className="w-full" onClick={onLay}>
          <Plus className="size-3" />
          Lay a cable
        </Button>
      )}
      {!cables.length ? (
        <p className="px-1 py-2 text-xs text-muted-foreground">
          No cable recorded yet. Draw one on the map and it appears here.
        </p>
      ) : cables.map((c) => (
        <button key={c.id} type="button" onClick={() => onOpen(c.id)}
          className="flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left hover:bg-foreground/5">
          <span className="flex items-baseline gap-2">
            <span className="truncate text-xs font-medium">{c.name}</span>
            {c.cores != null && (
              <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                {c.cores}F
              </span>
            )}
            <span className="ml-auto shrink-0 text-2xs text-faint-foreground">
              {cableLength(c.length_m) ?? "untraced"}
            </span>
          </span>
          <span className="truncate text-2xs text-faint-foreground">
            {cableEnds(c)}
          </span>
        </button>
      ))}
    </div>
  )
}

/** THE LAY-A-CABLE SHEET. One form, and it is the only creation gesture.
 *
 *  It appears after the route is drawn, because drawing is how you say a cable
 *  exists — the ends come from what the drawing landed on, and an end that
 *  landed on empty ground has already become a coupler by the time this opens. */
export function CableForm({ initial, ends, onSave, onCancel, busy, error }: {
  initial: { id?: number; name: string; cores: number | null }
  /** what the two ends resolved to, for the operator to check before saving */
  ends?: [string, string]
  onSave: (v: { name: string; cores: number | null }) => void
  onCancel: () => void
  busy?: boolean
  error?: string | null
}) {
  const [name, setName] = useState(initial.name)
  const [cores, setCores] = useState<number | null>(initial.cores)
  return (
    <form className="space-y-3"
      onSubmit={(e) => { e.preventDefault(); onSave({ name: name.trim(), cores }) }}>
      {ends && (
        <p className="flex items-center gap-1.5 truncate text-2xs text-muted-foreground">
          <span className="truncate">{ends[0]}</span>
          <ArrowRight className="size-3 shrink-0 text-faint-foreground" />
          <span className="truncate">{ends[1]}</span>
        </p>
      )}
      <label className="block space-y-1">
        <span className="wisp-eyebrow">Name</span>
        <input autoFocus value={name} maxLength={64}
          onChange={(e) => setName(e.target.value)}
          placeholder="Main St trunk"
          className="h-8 w-full rounded border border-border bg-background px-2 text-xs" />
      </label>
      <div className="space-y-1">
        <span className="wisp-eyebrow">Fibres</span>
        {/* A ROW OF CHIPS, not a select. The count is one of eight things an ISP
            stocks and it is read off a drum tag — a dropdown makes an operator
            open a menu to answer a question they already know the answer to. */}
        <div className="flex flex-wrap gap-1">
          {FIBER_COUNTS.map((n) => (
            <button key={n} type="button" onClick={() => setCores(cores === n ? null : n)}
              className={cn(
                "rounded border px-2 py-1 font-mono text-2xs transition-colors",
                cores === n
                  ? "border-primary bg-selected text-foreground"
                  : "border-border text-muted-foreground hover:bg-foreground/5")}>
              {n}F
            </button>
          ))}
        </div>
        <p className="text-2xs text-faint-foreground">
          Leave it unset if nobody has measured the sheath — unrecorded is a real
          answer, and a guessed count is arithmetic nobody can act on.
        </p>
      </div>
      {error && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-2xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-1.5">
        <Button size="sm" type="submit" disabled={busy || !name.trim()}>
          {initial.id ? "Save" : "Lay cable"}
        </Button>
        <Button size="sm" type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
