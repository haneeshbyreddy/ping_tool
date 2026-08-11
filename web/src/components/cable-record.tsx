import { useState } from "react"
import {
  ArrowRight, Check, MapPin, Pencil, Plus, Route, Scissors, Trash2, Waypoints,
} from "lucide-react"
import {
  FIBER_COUNTS, STRAND_COLORS, TUBE_SIZE,
  coresRecordedLabel, isPlumbing, strandHex, strandLabel, tubeRows,
} from "@/lib/fiber"
import { Button } from "@/components/ui/button"
import type { Cable, CoreEnd, FibrePoint } from "@/lib/types"
import { cn } from "@/lib/utils"

export const cableEnds = (c: Cable) =>
  `${c.a.name ?? "unplaced"} ↔ ${c.b.name ?? "unplaced"}`

export const cableLength = (m: number | null | undefined): string | null =>
  m == null ? null : m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`

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

function endText(end: CoreEnd | undefined): string | null {
  if (!end) return null
  if (end.terminates) return end.point ?? "this box"
  return `${end.cable_name ?? "cable"} #${end.core_no}`
}

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
          <Button size="sm" variant="outline" onClick={onSplit} disabled={busy}
            title="Cut the cable here and splice every core straight through">
            <Scissors className="size-3" />
            Open a closure
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

export function CableList({ cables, onOpen, onLay, canWrite }: {
  cables: Cable[]
  onOpen: (id: number) => void
  onLay?: () => void
  canWrite: boolean
}) {
  const laid = cables.filter((c) => !isPlumbing(c))
  const plumbed = cables.length - laid.length
  return (
    <div className="space-y-2">
      {canWrite && onLay && (
        <Button size="sm" variant="outline" className="w-full" onClick={onLay}>
          <Plus className="size-3" />
          Lay a cable
        </Button>
      )}
      {!laid.length ? (
        <p className="px-1 py-2 text-xs text-muted-foreground">
          No cable recorded yet. Draw one on the map and it appears here.
        </p>
      ) : laid.map((c) => (
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
      {plumbed > 0 && (
        <p className="px-2 pt-1 text-2xs text-faint-foreground">
          {plumbed} single {plumbed === 1 ? "fibre" : "fibres"} connecting boxes
          directly — shown on those boxes, not here.
        </p>
      )}
    </div>
  )
}

export function CableForm({
  initial, ends, near, onLand, onSave, onCancel, busy, error,
}: {
  initial: { id?: number; name: string; cores: number | null }
  ends?: [string, string]
  near?: [{ name: string; m: number } | null, { name: string; m: number } | null]
  onLand?: (i: 0 | 1) => void
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
      {near?.map((n, i) => n && onLand && (
        <button key={i} type="button" onClick={() => onLand(i as 0 | 1)}
          className="flex w-full items-center gap-1.5 rounded-md border border-border-subtle bg-muted/40 px-2 py-1.5 text-left text-2xs hover:bg-foreground/5">
          <MapPin className="size-3 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate text-muted-foreground">
            {i === 0 ? "Starts" : "Ends"} {Math.round(n.m)} m from{" "}
            <span className="font-medium text-foreground">{n.name}</span>
          </span>
          <span className="ml-auto shrink-0 whitespace-nowrap font-medium text-primary">
            land it there
          </span>
        </button>
      ))}
      <label className="block space-y-1">
        <span className="wisp-eyebrow">Name</span>
        <input autoFocus value={name} maxLength={64}
          onChange={(e) => setName(e.target.value)}
          placeholder="Main St trunk"
          className="h-8 w-full rounded border border-border bg-background px-2 text-xs" />
      </label>
      <div className="space-y-1">
        <span className="wisp-eyebrow">Fibres</span>
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
