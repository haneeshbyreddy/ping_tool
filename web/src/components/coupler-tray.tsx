// THE TRAY: what every fibre in one cable does at one point.
//
// IT IS A SPLICE SCHEDULE — a table, one row per fibre — because that is the
// document this replaces, and because of what the two-column form could not do.
//
// THE TWO-COLUMN TRAY WAS THE WRONG SHAPE AND IT FAILED ON A REAL ASK
// (2026-08-10, the operator's own case: *core 1 to OLT1, core 2 to OLT2, core 3
// to a customer*). Facing pages are right for cable↔cable — a real splice tray
// has two sides — but they make the destination a property of the PANEL, and a
// closure's terminations fan out to many different boxes. So:
//
//   * that arrangement could be ENTERED but never DISPLAYED. With one box on the
//     right, the cores that went elsewhere drew as EMPTY cells — and empty reads
//     as "nothing here" when it means "spoken for, elsewhere". The panel hid the
//     work you had just done, which is the absent-vs-unknown rule this codebase
//     keeps everywhere else, broken on its own newest screen;
//   * three destinations meant three trips through a dropdown, setting a MODE
//     before each click. The promise was "click a fibre, click what it goes to";
//     the shape could not keep it;
//   * undo was mode-dependent — a core tailed to OLT2 could only be cleared
//     while OLT2 happened to be the side on show.
//
// So the destination moved ONTO THE ROW, where it always belonged. Mixed
// destinations now coexist because nothing is modal, and the resting panel shows
// the truth for every core at once. Two things keep that from becoming a wall of
// text on a 24F:
//
// A STRAIGHT-THROUGH RUN COLLAPSES to one line. Nine closures in ten are 1:1 all
// the way across, and twenty-four rows saying the same thing is not information.
// A CROSSING NEVER COLLAPSES — core 3 to core 7 is the one thing at a closure
// worth reading twice, so it always gets its own row.
//
// A RUN NEVER CROSSES A BUFFER TUBE. Past twelve fibres the sequence restarts
// inside a tube and a crew works tube by tube, so "1–24 straight through" would
// describe a job nobody does in one go. Two runs of twelve is the truth.
//
// THE ARCS ARE GONE, and nothing was lost. Their one real job was showing 1:1
// across two cables; a collapsed run says that in words AND states the core
// numbers, which an arc cannot. What they cost was the second column.
import { useEffect, useMemo, useRef, useState } from "react"
import {
  Check, ChevronDown, ChevronRight, Plug, Search, User, Waypoints, X,
} from "lucide-react"
import { StrandSwatch } from "@/components/cable-record"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuPortal, DropdownMenuSeparator, DropdownMenuSub,
  DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { strandLabel, strandName, TUBE_SIZE } from "@/lib/fiber"
import type { FibreJoint, PointFibre, TrayCable } from "@/lib/types"
import { cn } from "@/lib/utils"

export type Fibre = { cableId: number; coreNo: number }

/** A box a fibre may be taken out to. Supplied by the caller: which boxes are
 *  NEAR this one is a question about the map, not about fibre. */
export interface TrayBox {
  id: number
  name: string
  device_type?: string | null
  /** straight-line km from this point; null when unknown */
  km: number | null
}

/** A subscriber a fibre may be taken out to. The case the two-column tray could
 *  not express AT ALL — the picker was built from devices, so "this core is the
 *  drop to that customer" was not merely awkward, it was unsayable. */
export interface TrayPerson {
  mac: string
  name: string
  km: number | null
}

/** Where a fibre goes to on the far side of the tail cable it was joined to. */
type Reach = { name: string; tail: true } | null

const MIN_RUN = 3

/** Which fibre each fibre is joined to at this point, and by which joint.
 *
 *  Keyed both ways round because a splice is undirected and either side has to
 *  be able to see it and undo it. A TERMINATION maps to null with a joint, which
 *  is a different thing from having no entry at all: one is "taken out to the box
 *  here", the other is "nothing recorded". */
function joinIndex(joints: FibreJoint[]) {
  const out = new Map<string, { to: Fibre | null; joint: FibreJoint }>()
  const key = (c: number, n: number) => `${c}:${n}`
  for (const j of joints) {
    const a = { cableId: j.a_cable_id, coreNo: j.a_core_no }
    if (j.b_cable_id == null) {
      out.set(key(a.cableId, a.coreNo), { to: null, joint: j })
      continue
    }
    const b = { cableId: j.b_cable_id, coreNo: j.b_core_no as number }
    out.set(key(a.cableId, a.coreNo), { to: b, joint: j })
    out.set(key(b.cableId, b.coreNo), { to: a, joint: j })
  }
  return out
}

/** Which cable's schedule to open on.
 *
 *  THE BIGGEST, ties broken by the server's feed hint. It was the feed outright
 *  until tails existed: light at a closure now arrives up a 1F tail, so
 *  feed-first opened the tray on a single strand with the 24F trunk — the thing
 *  you came to work on — hidden behind a dropdown. */
function defaultCable(cables: TrayCable[]): number | null {
  const sorted = [...cables].sort((a, b) =>
    (b.cores ?? 0) - (a.cores ?? 0)
    || (a.side === "feed" ? 0 : 1) - (b.side === "feed" ? 0 : 1))
  return sorted[0]?.cable_id ?? null
}

const kmLabel = (km: number | null) =>
  km == null ? "" : km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(1)} km`

/** One line of the schedule: a core, a straight-through run, or a free run. */
type Row =
  | { kind: "core"; coreNo: number }
  | { kind: "run"; from: number; to: number; cableId: number }
  | { kind: "free"; from: number; to: number }

/** THE SCHEDULE, as it reads.
 *
 *  TWO KINDS OF RUN COLLAPSE, and leaving either one out buries the rows that
 *  matter. A straight-through run, because nine closures in ten are 1:1 and
 *  twenty-four identical lines are not information. And an UNRECORDED run,
 *  because a 96F trunk with four cores in use would otherwise put ninety-two
 *  rows of "+ join" around the four an operator came to read — the same failure
 *  as the first, from the opposite direction.
 *
 *  A CROSSING NEVER COLLAPSES. Core 3 to core 7 is the one thing at a closure
 *  worth reading twice, and a termination is a decision somebody made; both keep
 *  their own row however many neighbours look like them.
 *
 *  RUNS STOP AT EVERY TUBE BOUNDARY. A crew opens one tube at a time, so a line
 *  claiming "1–24" describes a job nobody does in one go.
 *
 *  "Free" here means UNRECORDED, and the row says exactly that — never "spare".
 *  Nobody wrote those down, which is not the same as nothing being in them. */
function schedule(
  cores: number,
  destOf: (coreNo: number) => { to: Fibre | null } | undefined,
  expanded: Set<string>,
): Row[] {
  const rows: Row[] = []
  const emit = (from: number, to: number, row: Row) => {
    if (to - from + 1 < MIN_RUN || expanded.has(`${from}-${to}`)) {
      for (let c = from; c <= to; c++) rows.push({ kind: "core", coreNo: c })
    } else {
      rows.push(row)
    }
  }
  let i = 1
  while (i <= cores) {
    const tubeEnd = Math.min(cores, Math.ceil(i / TUBE_SIZE) * TUBE_SIZE)
    const j = destOf(i)
    const straight = j?.to && j.to.coreNo === i ? j.to.cableId : null
    if (straight != null) {
      let end = i
      while (end + 1 <= tubeEnd) {
        const n = destOf(end + 1)
        if (!n?.to || n.to.coreNo !== end + 1 || n.to.cableId !== straight) break
        end++
      }
      emit(i, end, { kind: "run", from: i, to: end, cableId: straight })
      i = end + 1
      continue
    }
    if (!j) {
      let end = i
      while (end + 1 <= tubeEnd && !destOf(end + 1)) end++
      emit(i, end, { kind: "free", from: i, to: end })
      i = end + 1
      continue
    }
    rows.push({ kind: "core", coreNo: i })
    i++
  }
  return rows
}

/** The searchable "what does this fibre do" menu — ONE menu for every answer.
 *
 *  Grouped by the three kinds of thing a fibre can end on and searchable across
 *  all of them, because an operator knows the NAME of where they are sending it
 *  and should not have to know which category we filed it under. Splicing takes a
 *  second click for the CORE, and that is deliberate: auto-picking one would be a
 *  capacity claim, and "recorded is never occupied" is the rule this whole
 *  subsystem is built on. */
function DestMenu({
  here, cables, boxes, people, joinsOf, onSplice, onHere, onBox, onPerson, children,
}: {
  here: string
  cables: TrayCable[]
  boxes: TrayBox[]
  people: TrayPerson[]
  joinsOf: (cableId: number, coreNo: number) => { to: Fibre | null } | undefined
  onSplice: (cableId: number, coreNo: number) => void
  onHere: () => void
  onBox: (deviceId: number) => void
  onPerson: (mac: string) => void
  children: React.ReactNode
}) {
  const [q, setQ] = useState("")
  const hit = (s: string) => s.toLowerCase().includes(q.trim().toLowerCase())
  /** Has this cable any core still free AT THIS POINT?
   *
   *  Not a capacity claim — the "recorded is never occupied" rule bans those —
   *  but the exactly checkable one: every core of it already carries a joint
   *  HERE, so there is nothing left to splice onto. It matters because taking a
   *  core out to a box lays a 1F TAIL that lands at this point too, so a closure
   *  feeding an 8-PON OLT grows eight one-fibre cables that show up as splice
   *  targets with nothing to offer. Shown DISABLED rather than hidden, the same
   *  way a used core is: "full" and "not a cable here" are different answers. */
  const freeIn = (c: TrayCable) =>
    !c.cores || Array.from({ length: c.cores }, (_, i) => i + 1)
      .some((n) => !joinsOf(c.cable_id, n))
  const cs = cables.filter((c) => hit(c.name))
  const bs = boxes.filter((b) => hit(b.name))
  const ps = people.filter((p) => hit(p.name))
  const nothing = !cs.length && !bs.length && !ps.length && !hit(here)
  return (
    <DropdownMenu onOpenChange={(o) => { if (!o) setQ("") }}>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ width: "auto" }}
        className="max-h-96 min-w-64 max-w-80 overflow-y-auto">
        {/* The filter STOPS KEY EVENTS. A Radix menu runs its own typeahead on
            printable keys, so without this every character both filtered the
            list and jumped the highlight to some item starting with that
            letter. */}
        <div className="sticky top-0 z-10 -mx-1 -mt-1 mb-1 flex items-center gap-1.5 border-b bg-popover px-2 py-1.5">
          <Search className="size-3 shrink-0 text-faint-foreground" />
          <input
            autoFocus value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key !== "Escape") e.stopPropagation() }}
            placeholder="cable, box or customer…"
            className="w-full bg-transparent text-xs outline-none placeholder:text-faint-foreground" />
        </div>

        {cs.length > 0 && (
          <>
            <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
              Splice into
            </DropdownMenuLabel>
            {cs.map((c) => !freeIn(c) ? (
              <DropdownMenuItem key={c.cable_id} disabled>
                <Waypoints className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{c.name}</span>
                <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
                  all joined here
                </span>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuSub key={c.cable_id}>
                <DropdownMenuSubTrigger>
                  <Waypoints className="size-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">{c.name}</span>
                  <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                    {c.cores ? `${c.cores}F` : "no count"}
                  </span>
                </DropdownMenuSubTrigger>
                <DropdownMenuPortal>
                  <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
                    {!c.cores ? (
                      <DropdownMenuItem disabled>
                        Record its fibre count first
                      </DropdownMenuItem>
                    ) : Array.from({ length: c.cores }, (_, k) => k + 1).map((n) => {
                      // A core already spoken for HERE is shown and disabled
                      // rather than hidden: "taken" and "not a core of this
                      // cable" are different answers, and only one of them is
                      // the operator's mistake.
                      const held = joinsOf(c.cable_id, n)
                      return (
                        <DropdownMenuItem key={n} disabled={!!held}
                          onSelect={() => onSplice(c.cable_id, n)}>
                          <StrandSwatch coreNo={n} className="shrink-0" />
                          <span>core {n}</span>
                          <span className="ml-auto pl-3 text-2xs text-faint-foreground">
                            {held ? "in use" : strandName(((n - 1) % TUBE_SIZE) + 1)}
                          </span>
                        </DropdownMenuItem>
                      )
                    })}
                  </DropdownMenuSubContent>
                </DropdownMenuPortal>
              </DropdownMenuSub>
            ))}
          </>
        )}

        {(hit(here) || bs.length > 0) && (
          <>
            {cs.length > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
              Take into
            </DropdownMenuLabel>
            {hit(here) && (
              <DropdownMenuItem onSelect={onHere}>
                <Plug className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{here}</span>
                <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                  this box
                </span>
              </DropdownMenuItem>
            )}
            {bs.map((b) => (
              <DropdownMenuItem key={b.id} onSelect={() => onBox(b.id)}>
                <Plug className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{b.name}</span>
                <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
                  {kmLabel(b.km) || b.device_type}
                </span>
              </DropdownMenuItem>
            ))}
          </>
        )}

        {ps.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
              Customer
            </DropdownMenuLabel>
            {ps.map((p) => (
              <DropdownMenuItem key={p.mac} onSelect={() => onPerson(p.mac)}>
                <User className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{p.name}</span>
                <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
                  {kmLabel(p.km)}
                </span>
              </DropdownMenuItem>
            ))}
          </>
        )}

        {nothing && (
          <p className="px-2 py-3 text-center text-2xs text-faint-foreground">
            Nothing here by that name.
            {people.length === 0 && " Customers have to be placed on the map first."}
          </p>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function CouplerTray({
  fibre, canWrite, busy, error, boxes = [], people = [], onJoin, onTail, onThrough,
  onClear, onClearError, onTrace,
}: {
  fibre: PointFibre
  canWrite: boolean
  busy?: boolean
  error?: string | null
  boxes?: TrayBox[]
  people?: TrayPerson[]
  onJoin: (a: Fibre, b: Fibre | null) => void
  onTail?: (a: Fibre, to: { deviceId?: number; mac?: string }) => void
  onThrough: (aCableId: number, bCableId: number) => void
  onClear: (f: Fibre) => void
  onClearError?: () => void
  onTrace?: (f: Fibre) => void
}) {
  const cables = fibre.cables
  const here = fibre.point.name ?? "this box"
  const pointKey = `${fibre.point.kind}:${fibre.point.device_id ?? fibre.point.mac}`
  const [openCable, setOpenCable] = useState<number | null>(() => defaultCable(cables))
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())

  /** Re-pick when the tray changes under us, and start over at a DIFFERENT
   *  point. Membership alone cannot detect a move: one cable's two ends are two
   *  points, so walking to the far end of the same sheath leaves the pick
   *  perfectly valid while the schedule on screen is about the wrong end. */
  const wasAt = useRef(pointKey)
  useEffect(() => {
    const moved = wasAt.current !== pointKey
    wasAt.current = pointKey
    setExpanded(new Set())
    setOpenCable((cur) => {
      const ids = new Set(cables.map((c) => c.cable_id))
      return !moved && cur != null && ids.has(cur) ? cur : defaultCable(cables)
    })
  }, [cables, pointKey])

  const joins = useMemo(() => joinIndex(fibre.joints), [fibre.joints])
  const byId = useMemo(() => new Map(cables.map((c) => [c.cable_id, c])), [cables])
  const cable = openCable != null ? byId.get(openCable) ?? null : null
  const joinOf = (cableId: number, coreNo: number) =>
    joins.get(`${cableId}:${coreNo}`)
  const destOf = (coreNo: number) =>
    cable ? joinOf(cable.cable_id, coreNo) : undefined

  const rows = useMemo(
    () => (cable?.cores ? schedule(cable.cores, destOf, expanded) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cable, joins, expanded])

  /** WHAT A ROW SAYS ITS FIBRE DOES.
   *
   *  A TAIL IS REPORTED AS THE BOX IT REACHES, not as the 1F cable in between.
   *  Storage-wise a tail is a splice onto a one-fibre sheath whose far end is
   *  terminated — three rows — but what the operator did was send this core to
   *  that OLT, and a schedule that answered "→ a1 core 4 → HLY-OLT-2, core 1"
   *  would be describing its own bookkeeping back at them. */
  const reachOf = (to: Fibre): Reach => {
    const t = byId.get(to.cableId)
    if (t && t.cores === 1 && t.far?.name) return { name: t.far.name, tail: true }
    return null
  }

  const clearRow = (coreNo: number) =>
    cable && onClear({ cableId: cable.cable_id, coreNo })

  const join = (coreNo: number, b: Fibre | null) =>
    cable && onJoin({ cableId: cable.cable_id, coreNo }, b)

  if (!cables.length) {
    return (
      <p className="px-1 py-2 text-xs text-muted-foreground">
        No cable is recorded as ending here yet. Lay one on the map and this
        becomes its splice schedule.
      </p>
    )
  }

  const others = cables.filter((c) => c.cable_id !== cable?.cable_id)

  return (
    <div className="space-y-2">
      {error && (
        <p className="flex items-start gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-2xs text-destructive">
          <span className="flex-1">{error}</span>
          {onClearError && (
            <button type="button" onClick={onClearError} aria-label="Dismiss">
              <X className="size-3" />
            </button>
          )}
        </p>
      )}

      <div className="flex items-center gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button"
              className="flex min-w-0 items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium hover:bg-foreground/5">
              <span className="truncate">
                {cable ? `${cable.cores ? `${cable.cores}F ` : ""}${cable.name}`
                       : "Pick a cable"}
              </span>
              <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" style={{ width: "auto" }}
            className="min-w-60 max-w-80">
            {cables.map((c) => (
              <DropdownMenuItem key={c.cable_id}
                onSelect={() => setOpenCable(c.cable_id)}>
                <span className="truncate">
                  {c.cores ? `${c.cores}F · ` : ""}{c.name}
                </span>
                <span className="ml-auto shrink-0 max-w-[45%] truncate pl-2 text-2xs text-faint-foreground">
                  {c.far.name ?? "far end unplaced"}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        {/* SPLICE ALL THROUGH stays one click. Nine closures in ten are 1:1 all
            the way across, and making that twenty-four gestures is the
            difference between a record that gets written and one that does not.
            It SKIPS what is already joined, so pressing it after hand-work
            leaves the hand-work. */}
        {canWrite && cable && others.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="outline" className="ml-auto" disabled={busy}>
                <Check className="size-3" />
                Splice all through…
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" style={{ width: "auto" }}
              className="min-w-56 max-w-80">
              <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
                every free core, 1:1, into
              </DropdownMenuLabel>
              {others.map((c) => (
                <DropdownMenuItem key={c.cable_id}
                  onSelect={() => onThrough(cable.cable_id, c.cable_id)}>
                  <span className="truncate">{c.name}</span>
                  <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                    {c.cores ? `${c.cores}F` : "no count"}
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {!cable?.cores ? (
        <p className="px-1.5 py-3 text-2xs text-faint-foreground">
          This cable has no fibre count recorded, so it has no cores to schedule.
          Set it on the cable panel and every strand appears here.
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border border-border-subtle">
          {rows.map((row, i) => {
            const tube = row.kind === "core"
              ? Math.ceil(row.coreNo / TUBE_SIZE) : Math.ceil(row.from / TUBE_SIZE)
            const prev = rows[i - 1]
            const prevTube = !prev ? 0 : prev.kind === "core"
              ? Math.ceil(prev.coreNo / TUBE_SIZE) : Math.ceil(prev.to / TUBE_SIZE)
            const newTube = (cable.cores ?? 0) > TUBE_SIZE && tube !== prevTube
            return (
              <div key={row.kind === "core" ? `c${row.coreNo}` : `r${row.from}`}>
                {/* Past twelve fibres the sequence restarts inside a tube, and
                    the tube is what a crew opens first — so it is a heading,
                    not something to infer from the arithmetic. */}
                {newTube && (
                  <div className="flex items-center gap-1.5 border-b bg-muted/60 px-2 py-1">
                    <StrandSwatch coreNo={tube} className="shrink-0" />
                    <span className="text-2xs text-muted-foreground">
                      {strandName(((tube - 1) % TUBE_SIZE) + 1)} tube
                    </span>
                  </div>
                )}
                {row.kind === "run" || row.kind === "free" ? (
                  <div className="flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
                    <button type="button"
                      onClick={() => setExpanded((s) =>
                        new Set(s).add(`${row.from}-${row.to}`))}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left">
                      <ChevronRight className="size-3 shrink-0 text-faint-foreground" />
                      <span className="font-mono text-2xs text-muted-foreground">
                        {row.from}–{row.to}
                      </span>
                      <span className={cn("min-w-0 flex-1 truncate text-2xs",
                        row.kind === "run" ? "text-muted-foreground" : "text-faint-foreground")}>
                        {row.kind === "run"
                          ? `straight through to ${byId.get(row.cableId)?.name ?? "another cable"}`
                          /* NEVER "spare". Nobody wrote these down, which is a
                             different sentence from nothing being in them — the
                             rule the splitter legs and the core count already
                             keep, and the one an operator plans capacity on. */
                          : `${row.to - row.from + 1} cores · nothing recorded`}
                      </span>
                    </button>
                    {/* A COLLAPSED FREE RUN MUST STAY ACTIONABLE, and getting
                        this wrong made a fresh cable a dead end: every core of a
                        new 12F is unrecorded, so the whole schedule folded into
                        one grey line with no visible way to join anything. The
                        run NAMES the core it will act on, so one click does the
                        overwhelmingly common thing — join the next free strand —
                        while the chevron is still there to open the run and pick
                        a specific one. */}
                    {row.kind === "free" && canWrite && (
                      <DestMenu
                        here={here} cables={others} boxes={boxes} people={people}
                        joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.from, { cableId: cid, coreNo: n })}
                        onHere={() => join(row.from, null)}
                        onBox={(id) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.from }, { deviceId: id })}
                        onPerson={(mac) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.from }, { mac })}>
                        <button type="button" disabled={busy}
                          className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5">
                          <StrandSwatch coreNo={row.from} className="shrink-0" />
                          <span>join core {row.from}</span>
                          <ChevronDown className="size-3 shrink-0" />
                        </button>
                      </DestMenu>
                    )}
                  </div>
                ) : (
                  <CoreRow
                    coreNo={row.coreNo} cores={cable.cores}
                    join={destOf(row.coreNo)}
                    here={here}
                    reach={destOf(row.coreNo)?.to
                      ? reachOf(destOf(row.coreNo)!.to!) : null}
                    farName={destOf(row.coreNo)?.to
                      ? byId.get(destOf(row.coreNo)!.to!.cableId)?.name ?? null : null}
                    label={cable.labels[String(row.coreNo)] ?? null}
                    canWrite={canWrite} busy={busy}
                    onClear={() => clearRow(row.coreNo)}
                    onTrace={() => onTrace?.(
                      { cableId: cable.cable_id, coreNo: row.coreNo })}
                    menu={(child) => (
                      <DestMenu
                        here={here} cables={others} boxes={boxes} people={people}
                        joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.coreNo, { cableId: cid, coreNo: n })}
                        onHere={() => join(row.coreNo, null)}
                        onBox={(id) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.coreNo },
                          { deviceId: id })}
                        onPerson={(mac) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.coreNo }, { mac })}>
                        {child}
                      </DestMenu>
                    )} />
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** ONE FIBRE, AND WHAT IT DOES. The whole row is the story: strand on the left,
 *  destination on the right, and the destination is a CONTROL rather than a
 *  readout — which is the entire correction over the two-column form. */
function CoreRow({
  coreNo, cores, join, here, reach, farName, label, canWrite, busy,
  onClear, onTrace, menu,
}: {
  coreNo: number
  cores: number | null
  join: { to: Fibre | null } | undefined
  here: string
  reach: Reach
  farName: string | null
  label: string | null
  canWrite: boolean
  busy?: boolean
  onClear: () => void
  onTrace: () => void
  menu: (child: React.ReactElement) => React.ReactNode
}) {
  const done = !!join
  const body = !join ? null
    : join.to == null
      ? <><Plug className="size-3 shrink-0 text-muted-foreground" />
          <span className="truncate">into {here}</span></>
      : reach
        ? <><Plug className="size-3 shrink-0 text-muted-foreground" />
            <span className="truncate">{reach.name}</span>
            <span className="shrink-0 text-2xs text-faint-foreground">on a tail</span></>
        : <><span className="truncate">{farName ?? "another cable"}</span>
            <StrandSwatch coreNo={join.to.coreNo} className="shrink-0" />
            <span className="shrink-0 font-mono text-2xs text-muted-foreground">
              core {join.to.coreNo}
            </span></>

  return (
    <div className="group flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
      <button type="button" onClick={onTrace} title={strandLabel(coreNo, cores)}
        className="flex shrink-0 items-center gap-2">
        <StrandSwatch coreNo={coreNo} size="lg" className="shrink-0" />
        <span className="w-5 text-left font-mono text-2xs text-muted-foreground">
          {coreNo}
        </span>
      </button>

      {/* A core with nothing joined still gets to say what it CARRIES — an
          operator's note, kept visually apart from anything the record derived. */}
      {!done && label && (
        <span className="min-w-0 flex-1 truncate text-2xs text-faint-foreground">
          {label}
        </span>
      )}

      <div className={cn("flex min-w-0 items-center justify-end gap-1.5",
        done || !label ? "flex-1" : "")}>
        {!canWrite ? (
          <span className="flex min-w-0 items-center gap-1.5 text-xs">
            {body ?? <span className="text-faint-foreground">not recorded</span>}
          </span>
        ) : menu(
          <button type="button" disabled={busy}
            className={cn(
              "flex min-w-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs",
              "hover:bg-foreground/5",
              done ? "" : "border border-dashed border-border-subtle text-faint-foreground hover:border-border")}>
            {body ?? <><span>+ join</span></>}
            <ChevronDown className="size-3 shrink-0 text-faint-foreground" />
          </button>,
        )}
        {/* Clearing is on the ROW, always, whatever the fibre is joined to —
            the two-column form could only undo what happened to be the side on
            show, so a core tailed to another box was un-clearable until you
            found your way back to it. */}
        {canWrite && done && (
          <button type="button" onClick={onClear} disabled={busy}
            title="Undo this join"
            className="shrink-0 rounded p-0.5 text-faint-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100">
            <X className="size-3" />
          </button>
        )}
      </div>
    </div>
  )
}
