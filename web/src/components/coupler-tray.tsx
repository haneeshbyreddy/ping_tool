import { useEffect, useMemo, useRef, useState } from "react"
import {
  Check, ChevronDown, ChevronRight, FileText, Plug, Plus, Search, User,
  Waypoints, X,
} from "lucide-react"
import { StrandSwatch } from "@/components/cable-record"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuPortal, DropdownMenuSeparator, DropdownMenuSub,
  DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  cutPairs, isNumberedKind, isPlumbing, portKey, portKindWord, PORT_REF_MAX,
  portName, strandLabel, strandName, TUBE_SIZE,
} from "@/lib/fiber"
import type {
  FibreJoint, PointFibre, TrayCable, TrayPort, UndrawnLink,
} from "@/lib/types"
import { cn } from "@/lib/utils"

export type Fibre = { cableId: number; coreNo: number }

export interface TrayBox {
  id: number
  name: string
  device_type?: string | null
  declared?: boolean
  port_kinds?: string[]
  ports?: TrayPort[]
  // An ENCLOSURE has no ports — every fibre in one is a splice — so what a connect
  // must ask for there is which CORE of which cable, not which port.
  cables?: Array<{ cable_id: number; name: string; cores: number | null
                   freeCores: number[] }>
  km: number | null
}

export interface TrayPerson {
  mac: string
  name: string
  km: number | null
}

type Reach = { name: string; tail: true } | null

const MIN_RUN = 3

const MIN_PORT_RUN = 8

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

function defaultCable(cables: TrayCable[]): number | null {
  const sorted = cables.filter((c) => !c.plumbing).sort((a, b) =>
    (b.cores ?? 0) - (a.cores ?? 0)
    || (a.side === "feed" ? 0 : 1) - (b.side === "feed" ? 0 : 1))
  return sorted[0]?.cable_id ?? null
}

const MENU_CAP = 12

// D: THE CAP IS ON THE RENDERED LIST, NEVER ON THE SEARCHED SET. Nearest-first with
// the distance printed is what makes the common case one click, so the resting list
// stays short — but typing a real name used to answer "Nothing here by that name"
// for two thirds of the org, because the search filtered the already-capped slice.
// While a search is ACTIVE, rank by match quality before distance, or an exact name
// still lands under whoever happens to be nearby.
function narrow<T extends { name: string }>(rows: T[], q: string): T[] {
  const needle = q.trim().toLowerCase()
  if (!needle) return rows.slice(0, MENU_CAP)
  const rank = (n: string) => {
    const low = n.toLowerCase()
    return low === needle ? 0 : low.startsWith(needle) ? 1 : 2
  }
  return rows
    .filter((r) => r.name.toLowerCase().includes(needle))
    .map((r, i) => ({ r, i }))
    .sort((a, b) => rank(a.r.name) - rank(b.r.name) || a.i - b.i)
    .slice(0, MENU_CAP)
    .map(({ r }) => r)
}

const kmLabel = (km: number | null) =>
  km == null ? "" : km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(1)} km`

function askFarPort(resolve: ((id: number) => TrayBox | undefined) | undefined,
                    deviceId?: number | null): TrayBox | undefined {
  if (deviceId == null || !resolve) return undefined
  const box = resolve(deviceId)
  if (!box || !(box.ports?.length)) return undefined
  const inputs = box.ports.filter((p) => p.kind === "in")
  if (box.device_type === "splitter" && inputs.length <= 1) return undefined
  return box
}

// IS THERE LIGHT ON IT. Green up, muted-red down, and NOTHING AT ALL when the server
// says `null` — a splitter leg nothing measures, a stale walk, or a box that is down.
// Drawing a grey dot for "not measured" would put a mark in the same slot as a
// measurement, which is the one thing this product refuses to do with a reading.
function PortDot({ live }: { live?: boolean | null }) {
  if (live == null) return null
  return (
    <span aria-hidden
      title={live ? "up" : "down"}
      className={cn("size-1.5 shrink-0 rounded-full",
        live ? "bg-success" : "bg-destructive")} />
  )
}

// Naming a new port. The kind PICKER is shown ONLY where the box genuinely has two
// kinds (an OLT: a PON or the uplink), so a splitter and a switch keep their current
// click count — nothing here may add a step to a path that already works.
function AddPortRow({ kinds, disabled, onPick }: {
  kinds: string[]
  disabled?: boolean
  onPick: (port: TrayPortRef) => void
}) {
  const [kind, setKind] = useState(kinds[0] ?? "")
  const [typed, setTyped] = useState("")
  if (!kinds.length) return null
  const active = kinds.includes(kind) ? kind : kinds[0]
  const numbered = isNumberedKind(active)
  return (
    <div className="flex items-center gap-1.5 px-2 py-1.5"
      onKeyDown={(e) => e.stopPropagation()}>
      {kinds.length > 1 ? (
        <div className="flex shrink-0 rounded border border-border">
          {kinds.map((k) => (
            <button key={k} type="button" onClick={() => setKind(k)}
              className={cn("px-1.5 py-0.5 font-mono text-2xs first:rounded-l last:rounded-r",
                k === active ? "bg-selected text-foreground"
                             : "text-faint-foreground hover:bg-foreground/5")}>
              {portKindWord(k)}
            </button>
          ))}
        </div>
      ) : (
        <span className="shrink-0 font-mono text-2xs text-faint-foreground">
          {portKindWord(active)}
        </span>
      )}
      <input value={typed} onChange={(e) => setTyped(e.target.value)}
        inputMode={numbered ? "numeric" : "text"}
        placeholder={numbered ? "number" : "name on the box"}
        maxLength={PORT_REF_MAX}
        className={cn("h-6 min-w-0 rounded border border-border bg-background px-1.5 text-2xs",
          numbered ? "w-16" : "w-32")} />
      <button type="button" disabled={disabled || cleanRef(active, typed) == null}
        onClick={() => onPick({ kind: active, ref: cleanRef(active, typed)! })}
        className="shrink-0 rounded px-1.5 py-0.5 text-2xs text-muted-foreground hover:bg-foreground/10 disabled:opacity-40">
        Connect
      </button>
    </div>
  )
}

function BoxItem({ box, onPick }: {
  box: TrayBox
  onPick: (far?: FarLanding) => void
}) {
  const kinds = box.port_kinds ?? []
  // An ENCLOSURE has no ports, so what a fibre lands on there is a CORE. Without
  // this the fibre arrives at the far closure and stops — the same incomplete
  // record the switch panel used to leave.
  const cables = (box.cables ?? []).filter((c) => c.freeCores.length > 0)
  const note = box.declared ? "on the map" : kmLabel(box.km) || box.device_type
  if (!kinds.length && !cables.length) {
    return (
      <DropdownMenuItem onSelect={() => onPick()}>
        <Plug className="size-3 shrink-0 text-muted-foreground" />
        <span className="truncate">{box.name}</span>
        <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
          {note}
        </span>
      </DropdownMenuItem>
    )
  }
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger>
        <Plug className="size-3 shrink-0 text-muted-foreground" />
        <span className="truncate">{box.name}</span>
        <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
          {note}
        </span>
      </DropdownMenuSubTrigger>
      <DropdownMenuPortal>
        <DropdownMenuSubContent className="max-h-80 min-w-44 overflow-y-auto">
          <DropdownMenuItem onSelect={() => onPick()}>
            <span className="text-2xs text-faint-foreground">
              {cables.length ? "Not joined to a core yet" : "Port not known yet"}
            </span>
          </DropdownMenuItem>
          {(box.ports?.length ?? 0) > 0 && <DropdownMenuSeparator />}
          {(box.ports ?? []).map((p) => (
            // TITLED, because a port is named by the BOX and some boxes append the
            // operator's own description to the socket — `GPON0/2 BANDARICOLLECTORAT`
            // is the most useful string on this menu and the one certain to clip.
            <DropdownMenuItem key={`${p.kind}:${p.ref}`} title={p.label}
              onSelect={() => onPick({ port: { kind: p.kind, ref: p.ref } })}>
              <PortDot live={p.live} />
              <span className="truncate font-mono text-2xs">{p.label}</span>
            </DropdownMenuItem>
          ))}
          {kinds.length > 0 && (
            <>
              <DropdownMenuSeparator />
              <AddPortRow kinds={kinds} onPick={(port) => onPick({ port })} />
            </>
          )}
          {cables.map((fc) => (
            <DropdownMenuSub key={fc.cable_id}>
              <DropdownMenuSubTrigger>
                <Waypoints className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{fc.name || "cable"}</span>
                <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                  {fc.cores ? `${fc.cores}F` : "no count"}
                </span>
              </DropdownMenuSubTrigger>
              <DropdownMenuPortal>
                <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
                  <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
                    …into which core
                  </DropdownMenuLabel>
                  {fc.freeCores.map((n) => (
                    <DropdownMenuItem key={n}
                      onSelect={() => onPick({ cableId: fc.cable_id, coreNo: n })}>
                      <StrandSwatch coreNo={n} className="shrink-0" />
                      <span>core {n}</span>
                      <span className="ml-auto pl-3 text-2xs text-faint-foreground">
                        {strandName(((n - 1) % TUBE_SIZE) + 1)}
                      </span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuPortal>
            </DropdownMenuSub>
          ))}
        </DropdownMenuSubContent>
      </DropdownMenuPortal>
    </DropdownMenuSub>
  )
}

type Row =
  | { kind: "core"; coreNo: number }
  | { kind: "run"; from: number; to: number; cableId: number }
  | { kind: "free"; from: number; to: number }

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

type DrumRow =
  | { kind: "core"; coreNo: number }
  | { kind: "through"; from: number; to: number }
  | { kind: "free"; from: number; to: number }

// The merged schedule of a cut drum: one row per core of the DRUM. A core spliced
// straight through collapses into a run (the nine-closures-in-ten case); a core
// free on BOTH sides collapses as unrecorded; anything else — taken out, crossed,
// or used on one side with spare glass on the other — stands alone, because that
// asymmetry is the one thing at a cut worth reading twice. Runs still never cross
// a buffer tube: a crew opens one tube at a time.
function drumSchedule(
  cores: number,
  nearOf: (n: number) => { to: Fibre | null } | undefined,
  farCableId: number,
  farOf: (n: number) => { to: Fibre | null } | undefined,
  expanded: Set<string>,
): DrumRow[] {
  const rows: DrumRow[] = []
  const emit = (from: number, to: number, row: DrumRow) => {
    if (to - from + 1 < MIN_RUN || expanded.has(`${from}-${to}`)) {
      for (let c = from; c <= to; c++) rows.push({ kind: "core", coreNo: c })
    } else {
      rows.push(row)
    }
  }
  const through = (n: number) => {
    const j = nearOf(n)
    return !!(j?.to && j.to.cableId === farCableId && j.to.coreNo === n)
  }
  const open = (n: number) => !nearOf(n) && !farOf(n)
  let i = 1
  while (i <= cores) {
    const tubeEnd = Math.min(cores, Math.ceil(i / TUBE_SIZE) * TUBE_SIZE)
    if (through(i)) {
      let end = i
      while (end + 1 <= tubeEnd && through(end + 1)) end++
      emit(i, end, { kind: "through", from: i, to: end })
      i = end + 1
    } else if (open(i)) {
      let end = i
      while (end + 1 <= tubeEnd && open(end + 1)) end++
      emit(i, end, { kind: "free", from: i, to: end })
      i = end + 1
    } else {
      rows.push({ kind: "core", coreNo: i })
      i++
    }
  }
  return rows
}

function DestMenu({
  here, cables, boxes, people, herePorts = [], joinsOf,
  onSplice, onHere, onBox, onPerson, children,
}: {
  here: string
  cables: TrayCable[]
  boxes: TrayBox[]
  people: TrayPerson[]
  herePorts?: TrayPort[]
  joinsOf: (cableId: number, coreNo: number) => { to: Fibre | null } | undefined
  onSplice: (cableId: number, coreNo: number) => void
  onHere: (port?: TrayPortRef) => void
  onBox: (deviceId: number, far?: FarLanding) => void
  onPerson: (mac: string) => void
  children: React.ReactNode
}) {
  const [q, setQ] = useState("")
  const hit = (s: string) => s.toLowerCase().includes(q.trim().toLowerCase())
  const freeIn = (c: TrayCable) =>
    !c.cores || Array.from({ length: c.cores }, (_, i) => i + 1)
      .some((n) => !joinsOf(c.cable_id, n))
  // Plumbing is never offered in a picker — a 1F tail's core is always taken, so it
  // could only ever render as a nameless, dead "all joined here" row.
  const cs = cables.filter((c) => !c.plumbing && hit(c.name))
  const bs = narrow(boxes, q)
  const ps = narrow(people, q)
  const nothing = !cs.length && !bs.length && !ps.length && !hit(here)
  return (
    <DropdownMenu onOpenChange={(o) => { if (!o) setQ("") }}>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ width: "auto" }}
        className="max-h-96 min-w-64 max-w-80 overflow-y-auto">
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
            {hit(here) && (herePorts.length === 0 ? (
              <DropdownMenuItem onSelect={() => onHere()}>
                <Plug className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{here}</span>
                <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                  this box
                </span>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <Plug className="size-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">{here}</span>
                  <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                    this box
                  </span>
                </DropdownMenuSubTrigger>
                <DropdownMenuPortal>
                  <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
                    <DropdownMenuItem onSelect={() => onHere()}>
                      <span className="text-muted-foreground">port not recorded</span>
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    {herePorts.map((p) => (
                      <DropdownMenuItem key={`${p.kind}:${p.ref}`} title={p.label}
                        onSelect={() => onHere({ kind: p.kind, ref: p.ref })}>
                        <PortDot live={p.live} />
                        <span className="truncate font-mono text-2xs">{p.label}</span>
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuPortal>
              </DropdownMenuSub>
            ))}
            {bs.map((b) => (
              <BoxItem key={b.id} box={b} onPick={(far) => onBox(b.id, far)} />
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

function SourceMenu({
  cables, boxes = [], people = [], joinsOf, onPick, onConnect, onDrop, children,
}: {
  cables: TrayCable[]
  boxes?: TrayBox[]
  people?: TrayPerson[]
  joinsOf: (cableId: number, coreNo: number) => { to: Fibre | null } | undefined
  onPick: (f: Fibre) => void
  onConnect?: (deviceId: number, far?: FarLanding) => void
  onDrop?: (mac: string) => void
  children: React.ReactNode
}) {
  const [q, setQ] = useState("")
  const hit = (s: string) => s.toLowerCase().includes(q.trim().toLowerCase())
  const bs = onConnect ? narrow(boxes, q) : []
  const ps = onDrop ? narrow(people, q) : []
  // Same plumbing rule as DestMenu — the box panels pass the point's FULL cable
  // list (the port rows need the tails for their far-end names), so the picker
  // filters here or a switch fed by four tails offers four nameless dead rows.
  const cs = cables.filter((c) => !c.plumbing && hit(c.name))
  return (
    <DropdownMenu onOpenChange={(o) => { if (!o) setQ("") }}>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ width: "auto" }}
        className="max-h-96 min-w-64 max-w-80 overflow-y-auto">
        {(boxes.length + people.length + cables.length > 6) && (
          <div className="sticky top-0 z-10 -mx-1 -mt-1 mb-1 flex items-center gap-1.5 border-b bg-popover px-2 py-1.5">
            <Search className="size-3 shrink-0 text-faint-foreground" />
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key !== "Escape") e.stopPropagation() }}
              placeholder="cable, box or customer…"
              className="w-full bg-transparent text-xs outline-none placeholder:text-faint-foreground" />
          </div>
        )}

        {ps.length > 0 && onDrop && (
          <>
            <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
              Customer on this leg
            </DropdownMenuLabel>
            {ps.map((p) => (
              <DropdownMenuItem key={p.mac} onSelect={() => onDrop(p.mac)}>
                <User className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{p.name}</span>
                <span className="ml-auto shrink-0 whitespace-nowrap pl-2 text-2xs text-faint-foreground">
                  {kmLabel(p.km) || "recorded here"}
                </span>
              </DropdownMenuItem>
            ))}
          </>
        )}

        {bs.length > 0 && onConnect && (
          <>
            {ps.length > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
              Connect to
            </DropdownMenuLabel>
            {bs.map((b) => (
              <BoxItem key={b.id} box={b} onPick={(far) => onConnect(b.id, far)} />
            ))}
          </>
        )}

        {cs.length > 0 && (bs.length > 0 || ps.length > 0) && <DropdownMenuSeparator />}
        {cs.length > 0 && (
          <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
            A core already landing here
          </DropdownMenuLabel>
        )}
        {cables.length === 0 && bs.length === 0 && ps.length === 0 ? (
          <p className="px-2 py-3 text-center text-2xs text-faint-foreground">
            Nothing placed nearby to connect to yet.
          </p>
        ) : cs.map((c) => (
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
                  const held = joinsOf(c.cable_id, n)
                  return (
                    <DropdownMenuItem key={n} disabled={!!held}
                      onSelect={() => onPick({ cableId: c.cable_id, coreNo: n })}>
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
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

interface TrayActions {
  busy?: boolean
  error?: string | null
  boxes?: TrayBox[]
  people?: TrayPerson[]
  boxOf?: (id: number) => TrayBox | undefined
  onJoin: (a: Fibre, b: Fibre | null, port?: TrayPortRef) => void
  onTail?: (a: Fibre, to: { deviceId?: number; mac?: string },
            far?: FarLanding) => void
  onConnect?: (deviceId: number, port: TrayPortRef, far?: FarLanding) => void
  onDrop?: (mac: string, port: TrayPortRef) => void
  onThrough: (aCableId: number, bCableId: number) => void
  onClear: (f: Fibre) => void
  onClearError?: () => void
  onTrace?: (f: Fibre) => void
}

export type TrayPortRef = { kind: string; ref: string | null }
// What the FAR end of a connect lands on: a port, or a core at a closure.
export type FarLanding =
  | { port: TrayPortRef }
  | { cableId: number; coreNo: number }

// What a typed port ref has to be before it is worth sending: a numbered kind wants a
// number, a `port` wants whatever the box has written on it. Server-side `clean_port`
// is the authority; this only keeps the button from firing a request that would 422.
function cleanRef(kind: string, typed: string): string | null {
  const ref = typed.trim()
  if (!ref || ref.length > PORT_REF_MAX) return null
  if (!isNumberedKind(kind)) return ref
  return /^\d+$/.test(ref) && parseInt(ref, 10) >= 1 ? String(parseInt(ref, 10)) : null
}

export function FibrePanel({
  open, onOpen, cables, fibre, loading, canWrite, onOpenCable, todo = 0, ...tray
}: {
  open: boolean
  onOpen: (v: boolean) => void
  todo?: number
  cables: Array<{ cable: { id: number; name: string; cores: number | null
                           path?: Array<[number, number]> } }>
  fibre?: PointFibre
  loading?: boolean
  canWrite: boolean
  onOpenCable?: (cableId: number) => void
} & TrayActions) {
  const ports = fibre?.ports ?? []
  const wired = new Set((fibre?.joints ?? [])
    .filter((j) => j.b_cable_id == null && j.port_kind)
    .map((j) => `${j.port_kind}:${portKey(j.port_ref)}`))
  const left = fibre?.undrawn?.length ?? todo
  const done = ports.filter((p) => wired.has(`${p.kind}:${portKey(p.ref)}`)).length
  const summary = left > 0
    ? `${left} to connect`
    : done > 0 ? `${done} connected`
    : ports.length > 0 ? `${ports.length} port${ports.length === 1 ? "" : "s"}`
    : cables.length === 0 ? "nothing recorded yet"
    : (() => {
        const real = cables.filter(({ cable }) => !isPlumbing(cable))
        if (!real.length) return `${cables.length} connected`
        // A cut drum is one object: its two halves say "6F main" once, not twice.
        const pairs = cutPairs(real.map(({ cable }) => ({
          id: cable.id, name: cable.name, cores: cable.cores })))
        const skip = new Set<number>()
        for (const [a, b] of pairs) skip.add(Math.max(a, b))
        return [...real]
          .filter(({ cable }) => !skip.has(cable.id))
          .sort((a, b) => (b.cable.cores ?? 0) - (a.cable.cores ?? 0))
          .map(({ cable }) => cable.cores ? `${cable.cores}F ${cable.name}` : cable.name)
          .join(" · ")
      })()

  return (
    <div className="flex flex-col rounded-lg border bg-muted/40">
      <button type="button" onClick={() => onOpen(!open)}
        className={cn("flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-foreground/5",
          open ? "rounded-t-lg" : "rounded-lg")}
        title="Every fibre landing here, and what each one is joined to">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <Waypoints className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="shrink-0 text-2xs font-medium text-muted-foreground">Fibre</span>
        {!open && (
          <span className="ml-auto min-w-0 truncate text-right text-2xs text-faint-foreground">
            {summary}
          </span>
        )}
      </button>
      {open && (
        <div className="px-3 pb-3">
          {cables.length === 0 && !(fibre?.ports?.length || fibre?.port_add?.length) ? (
            <p className="text-2xs text-faint-foreground">
              No cable is recorded as ending here. Lay one on the map — that is
              what joins a box to the network now.
            </p>
          ) : loading && !fibre ? (
            <p className="py-3 text-center text-2xs text-faint-foreground">Reading…</p>
          ) : fibre ? (
            <CouplerTray fibre={fibre} canWrite={canWrite}
              onOpenCable={onOpenCable} {...tray} />
          ) : null}
        </div>
      )}
    </div>
  )
}

export function CouplerTray({
  fibre, canWrite, busy, error, boxes = [], people = [], boxOf,
  onJoin, onTail, onThrough,
  onConnect, onDrop, onClear, onClearError, onTrace, onOpenCable,
}: {
  fibre: PointFibre
  canWrite: boolean
  onOpenCable?: (cableId: number) => void
} & TrayActions) {
  const cables = fibre.cables
  const here = fibre.point.name ?? "this box"
  // WHAT A CORE CARRIES, in one line: the far terminations the fibre reaches across
  // every splice. A closure used to show core 1 with nothing on it while the fibre
  // in it ran to a switch two closures away — the trace already knew. The point we
  // are STANDING at is dropped: repeating it back says nothing.
  const carriesOf = (cableId: number, coreNo: number): string | null => {
    const ends = (fibre.carries?.[String(cableId)]?.[String(coreNo)] ?? [])
      .filter((e) => !e.here && e.name)
    if (!ends.length) return null
    return ends.map((e) => (e.port ? `${e.name} · ${e.port}` : e.name)).join("  ↔  ")
  }
  const pointKey = `${fibre.point.kind}:${fibre.point.device_id ?? fibre.point.mac}`
  const [openCable, setOpenCable] = useState<number | null>(() => defaultCable(cables))
  const [schedOpen, setSchedOpen] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())

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

  const laid = useMemo(() => cables.filter((c) => !c.plumbing), [cables])
  const pairs = useMemo(() => cutPairs(cables.map((c) => ({
    id: c.cable_id, name: c.name, cores: c.cores, plumbing: c.plumbing }))),
    [cables])
  // A CUT DRUM RENDERS AS ONE SCHEDULE. `near` is the side the light arrives on
  // (the feed side), so its numbering leads; picking either half in the dropdown
  // lands on the same merged view.
  const partner = cable ? byId.get(pairs.get(cable.cable_id) ?? -1) ?? null : null
  const drum = useMemo(() => (cable && partner
    ? cable.side === "feed" ? { near: cable, far: partner }
      : partner.side === "feed" ? { near: partner, far: cable }
      : cable.cable_id < partner.cable_id ? { near: cable, far: partner }
      : { near: partner, far: cable }
    : null), [cable, partner])

  // One picker entry per OBJECT: a cut pair folds into its drum, everything else
  // stands alone. Order follows `laid` (feed side first, the server's sort).
  const entries = useMemo(() => {
    const seen = new Set<number>()
    const out: Array<{ main: TrayCable; mate: TrayCable | null }> = []
    for (const c of laid) {
      if (seen.has(c.cable_id)) continue
      const mateId = pairs.get(c.cable_id)
      const mate = mateId != null
        ? laid.find((x) => x.cable_id === mateId) ?? null : null
      if (mate) seen.add(mate.cable_id)
      out.push({ main: c, mate })
    }
    return out
  }, [laid, pairs])

  const rows = useMemo(
    () => (!drum && cable?.cores ? schedule(cable.cores, destOf, expanded) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cable, drum, joins, expanded])

  const drumRows = useMemo(
    () => (drum?.near.cores
      ? drumSchedule(drum.near.cores,
                     (n) => joins.get(`${drum.near.cable_id}:${n}`),
                     drum.far.cable_id,
                     (n) => joins.get(`${drum.far.cable_id}:${n}`),
                     expanded)
      : []),
    [drum, joins, expanded])

  const reachOf = (to: Fibre): Reach => {
    const t = byId.get(to.cableId)
    if (t && t.cores === 1 && t.far?.name) return { name: t.far.name, tail: true }
    return null
  }

  const clearRow = (coreNo: number) =>
    cable && onClear({ cableId: cable.cable_id, coreNo })

  const join = (coreNo: number, b: Fibre | null, port?: TrayPortRef) =>
    cable && onJoin({ cableId: cable.cable_id, coreNo }, b, port)

  const herePorts = fibre.ports ?? []

  const undrawn = fibre.undrawn ?? []
  if (!cables.length && herePorts.length === 0 && !fibre.port_add?.length
      && undrawn.length === 0) {
    return (
      <p className="px-1 py-2 text-xs text-muted-foreground">
        No cable is recorded as ending here yet. Lay one on the map and this
        becomes its splice schedule.
      </p>
    )
  }

  const inView = new Set(drum ? [drum.near.cable_id, drum.far.cable_id]
                              : cable ? [cable.cable_id] : [])
  const others = laid.filter((c) => !inView.has(c.cable_id))

  // What a row's destination menu offers. The drum's other side goes FIRST — the
  // commonest join at a cut is straight through — and any pair member anywhere in
  // the list carries its far end in its name, because two entries both reading
  // "main · 6F" is the picker defect this view exists to kill.
  const menuCables = (source: TrayCable): TrayCable[] => {
    const partnerId = pairs.get(source.cable_id)
    const named = laid
      .filter((c) => c.cable_id !== source.cable_id)
      .map((c) => pairs.has(c.cable_id)
        ? { ...c, name: c.cable_id === partnerId
            ? `toward ${c.far.name ?? "the far end"}`
            : `${c.name} · toward ${c.far.name ?? "the far end"}` }
        : c)
    if (partnerId == null) return named
    return [...named.filter((c) => c.cable_id === partnerId),
            ...named.filter((c) => c.cable_id !== partnerId)]
  }

  const menuFor = (source: TrayCable, coreNo: number,
                   child: React.ReactElement) => (
    <DestMenu
      here={here} cables={menuCables(source)} boxes={boxes} people={people}
      herePorts={herePorts} joinsOf={joinOf}
      onSplice={(cid, n) => onJoin({ cableId: source.cable_id, coreNo },
                                   { cableId: cid, coreNo: n })}
      onHere={(port) => onJoin({ cableId: source.cable_id, coreNo }, null, port)}
      onBox={(id, far) => onTail?.(
        { cableId: source.cable_id, coreNo }, { deviceId: id }, far)}
      onPerson={(mac) => onTail?.(
        { cableId: source.cable_id, coreNo }, { mac })}>
      {child}
    </DestMenu>
  )

  // Both facts about a drum core's side, computed once for the row: the joint (if
  // any), the far end its glass runs toward, and the joint rendered the way the
  // per-cable rows render theirs.
  const sideFacts = (side: TrayCable, other: TrayCable, coreNo: number) => {
    const j = joinOf(side.cable_id, coreNo)
    const toward = side.far.name ?? "the far end"
    if (!j) return { j: undefined, toward, body: null as React.ReactNode }
    let body: React.ReactNode
    if (j.to == null) {
      const port = portName(herePorts, j.joint?.port_kind, j.joint?.port_ref)
      body = (
        <>
          <Plug className="size-3 shrink-0 text-muted-foreground" />
          <span className="truncate">into {here}</span>
          {port && (
            <span className="shrink-0 font-mono text-2xs text-muted-foreground">
              {port}
            </span>
          )}
        </>
      )
    } else if (j.to.cableId === other.cable_id) {
      body = (
        <>
          <span className="truncate">
            core {j.to.coreNo} · toward {other.far.name ?? "the far end"}
          </span>
          <StrandSwatch coreNo={j.to.coreNo} className="shrink-0" />
        </>
      )
    } else {
      const reach = reachOf(j.to)
      body = reach ? (
        <>
          <Plug className="size-3 shrink-0 text-muted-foreground" />
          <span className="truncate">{reach.name}</span>
        </>
      ) : (
        <>
          <span className="truncate">
            {byId.get(j.to.cableId)?.name ?? "another cable"}
          </span>
          <StrandSwatch coreNo={j.to.coreNo} className="shrink-0" />
          <span className="shrink-0 font-mono text-2xs text-muted-foreground">
            core {j.to.coreNo}
          </span>
        </>
      )
    }
    return { j, toward, body }
  }

  return (
    <div className="space-y-2">
      {(herePorts.length > 0 || fibre.port_add?.length || undrawn.length > 0) && (
        <PortList
          ports={herePorts} cables={cables} unplaced={fibre.unplaced_drops ?? []}
          joins={fibre.joints} joinsOf={joinOf} canWrite={canWrite} busy={busy}
          addKinds={fibre.port_add ?? []} boxes={boxes} people={people}
          undrawn={undrawn}
          boxOf={boxOf}
          onLand={(f, port) => onJoin(f, null, port)}
          onConnect={(deviceId, port, toPort) => onConnect?.(deviceId, port, toPort)}
          onDrop={(mac, port) => onDrop?.(mac, port)}
          onClear={(f) => onClear(f)} />
      )}

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

      {herePorts.length > 0 && !schedOpen ? (laid.length === 0 ? null : (
        <button type="button" onClick={() => setSchedOpen(true)}
          className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-2xs text-faint-foreground hover:bg-foreground/5">
          <ChevronRight className="size-3 shrink-0" />
          <span className="truncate">
            {entries.length === 1
              ? `Splices in ${entries[0].main.cores ? `the ${entries[0].main.cores}F ` : ""}${entries[0].main.name}`
              : `Splices · ${entries.length} cables here`}
          </span>
        </button>
      )) : (<>
      <div className="flex items-center gap-2">
        {herePorts.length > 0 && (
          <button type="button" onClick={() => setSchedOpen(false)}
            className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-foreground/5"
            title="Fold the core plan away">
            <ChevronDown className="size-3" />
          </button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button"
              className="flex min-w-0 items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium hover:bg-foreground/5"
              title={drum
                ? `The ${cable?.cores}F ${cable?.name} is cut at this closure — one schedule covers both sides`
                : undefined}>
              <span className="truncate">
                {cable ? `${cable.cores ? `${cable.cores}F ` : ""}${cable.name}`
                       : "Pick a cable"}
              </span>
              <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" style={{ width: "auto" }}
            className="min-w-60 max-w-80">
            {entries.map(({ main, mate }) => (
              <DropdownMenuItem key={main.cable_id}
                onSelect={() => setOpenCable(main.cable_id)}>
                <span className="truncate">
                  {main.cores ? `${main.cores}F · ` : ""}{main.name}
                </span>
                <span className="ml-auto shrink-0 max-w-[45%] truncate pl-2 text-2xs text-faint-foreground">
                  {mate
                    ? `${main.far.name ?? "?"} ↔ ${mate.far.name ?? "?"}`
                    : main.far.name ?? "far end unplaced"}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        {cable && onOpenCable && (drum ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon" variant="ghost"
                className="size-6 shrink-0 text-muted-foreground"
                title={`Open the record for ${cable.name} — two segments meet here`}>
                <FileText className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" style={{ width: "auto" }}
              className="min-w-44 max-w-72">
              {[drum.near, drum.far].map((c) => (
                <DropdownMenuItem key={c.cable_id}
                  onSelect={() => onOpenCable(c.cable_id)}>
                  <span className="truncate">
                    toward {c.far.name ?? "the far end"}
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <Button size="icon" variant="ghost"
            className="size-6 shrink-0 text-muted-foreground"
            title={`Open the record for ${cable.name}`}
            onClick={() => onOpenCable(cable.cable_id)}>
            <FileText className="size-3.5" />
          </Button>
        ))}

        {canWrite && cable && (drum || others.length > 0) && (
          drum && others.length === 0 ? (
            <Button size="sm" variant="outline" className="ml-auto" disabled={busy}
              onClick={() => onThrough(drum.near.cable_id, drum.far.cable_id)}>
              <Check className="size-3" />
              Splice all through
            </Button>
          ) : (
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
              {drum && (
                <DropdownMenuItem
                  onSelect={() => onThrough(drum.near.cable_id, drum.far.cable_id)}>
                  <span className="truncate">straight through the cut</span>
                  <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                    {drum.near.cores}F
                  </span>
                </DropdownMenuItem>
              )}
              {others.map((c) => (
                <DropdownMenuItem key={c.cable_id}
                  onSelect={() => onThrough((drum ? drum.near : cable).cable_id,
                                            c.cable_id)}>
                  <span className="truncate">{c.name}</span>
                  <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                    {c.cores ? `${c.cores}F` : "no count"}
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          )
        )}
      </div>

      {drum ? (
        <div className="overflow-hidden rounded-md border border-border-subtle">
          {drumRows.map((row, i) => {
            const cores = drum.near.cores ?? 0
            const tube = row.kind === "core"
              ? Math.ceil(row.coreNo / TUBE_SIZE) : Math.ceil(row.from / TUBE_SIZE)
            const prev = drumRows[i - 1]
            const prevTube = !prev ? 0 : prev.kind === "core"
              ? Math.ceil(prev.coreNo / TUBE_SIZE) : Math.ceil(prev.to / TUBE_SIZE)
            const newTube = cores > TUBE_SIZE && tube !== prevTube
            return (
              <div key={row.kind === "core" ? `c${row.coreNo}` : `r${row.from}`}>
                {newTube && (
                  <div className="flex items-center gap-1.5 border-b bg-muted/60 px-2 py-1">
                    <StrandSwatch coreNo={tube} className="shrink-0" />
                    <span className="text-2xs text-muted-foreground">
                      {strandName(((tube - 1) % TUBE_SIZE) + 1)} tube
                    </span>
                  </div>
                )}
                {row.kind === "through" || row.kind === "free" ? (
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
                        row.kind === "through" ? "text-muted-foreground" : "text-faint-foreground")}>
                        {row.kind === "through" ? "straight through" : "nothing recorded"}
                      </span>
                    </button>
                    {row.kind === "free" && canWrite && menuFor(drum.near, row.from,
                      <button type="button" disabled={busy}
                        className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5">
                        <StrandSwatch coreNo={row.from} className="shrink-0" />
                        <span>join core {row.from}</span>
                        <ChevronDown className="size-3 shrink-0" />
                      </button>)}
                  </div>
                ) : (() => {
                  const nearF = sideFacts(drum.near, drum.far, row.coreNo)
                  const farF = sideFacts(drum.far, drum.near, row.coreNo)
                  const isThrough = !!(nearF.j?.to
                    && nearF.j.to.cableId === drum.far.cable_id
                    && nearF.j.to.coreNo === row.coreNo)
                  if (isThrough) {
                    const carries = carriesOf(drum.near.cable_id, row.coreNo)
                    const label = drum.near.labels[String(row.coreNo)]
                      ?? drum.far.labels[String(row.coreNo)] ?? null
                    return (
                      <div className="group flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
                        <button type="button"
                          onClick={() => onTrace?.(
                            { cableId: drum.near.cable_id, coreNo: row.coreNo })}
                          title={strandLabel(row.coreNo, cores)}
                          className="flex shrink-0 items-center gap-2">
                          <StrandSwatch coreNo={row.coreNo} size="lg" className="shrink-0" />
                          <span className="w-5 text-left font-mono text-2xs text-muted-foreground">
                            {row.coreNo}
                          </span>
                        </button>
                        {(carries || label) && (
                          <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-2xs">
                            {carries && (
                              <span className="min-w-0 truncate text-muted-foreground">
                                {carries}
                              </span>
                            )}
                            {label && (
                              <span className="min-w-0 truncate text-faint-foreground">
                                {label}
                              </span>
                            )}
                          </span>
                        )}
                        <div className={cn("flex min-w-0 items-center justify-end gap-1.5",
                          !(carries || label) ? "flex-1" : "")}>
                          <span className="shrink-0 text-xs text-muted-foreground">
                            straight through
                          </span>
                          {canWrite && (
                            <button type="button" disabled={busy}
                              onClick={() => onClear(
                                { cableId: drum.near.cable_id, coreNo: row.coreNo })}
                              title="Undo this join"
                              className="shrink-0 rounded p-0.5 text-faint-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100">
                              <X className="size-3" />
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  }
                  return (
                    <div className="border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
                      <div className="flex items-start gap-2">
                        <button type="button"
                          onClick={() => onTrace?.(
                            { cableId: drum.near.cable_id, coreNo: row.coreNo })}
                          title={strandLabel(row.coreNo, cores)}
                          className="flex shrink-0 items-center gap-2 pt-px">
                          <StrandSwatch coreNo={row.coreNo} size="lg" className="shrink-0" />
                          <span className="w-5 text-left font-mono text-2xs text-muted-foreground">
                            {row.coreNo}
                          </span>
                        </button>
                        <div className="min-w-0 flex-1 space-y-1">
                          {[
                            { side: drum.near, f: nearF,
                              c: carriesOf(drum.near.cable_id, row.coreNo),
                              l: drum.near.labels[String(row.coreNo)] ?? null },
                            { side: drum.far, f: farF,
                              c: carriesOf(drum.far.cable_id, row.coreNo),
                              l: drum.far.labels[String(row.coreNo)] ?? null },
                          ].map(({ side, f, c, l }) => (
                            <div key={side.cable_id}
                              className="group/side flex min-h-6 items-center gap-2">
                              {f.j ? (<>
                                <span className={cn("min-w-0 flex-1 truncate text-2xs",
                                  c ? "text-muted-foreground" : "text-faint-foreground")}>
                                  {c ?? `${f.toward} side`}{l ? ` · ${l}` : ""}
                                </span>
                                <span className="flex min-w-0 items-center gap-1.5 text-xs">
                                  {f.body}
                                </span>
                                {canWrite && (
                                  <button type="button" disabled={busy}
                                    onClick={() => onClear(
                                      { cableId: side.cable_id, coreNo: row.coreNo })}
                                    title="Undo this join"
                                    className="shrink-0 rounded p-0.5 text-faint-foreground opacity-0 transition-opacity hover:text-foreground group-hover/side:opacity-100">
                                    <X className="size-3" />
                                  </button>
                                )}
                              </>) : (<>
                                <span className="min-w-0 flex-1 truncate text-2xs text-faint-foreground">
                                  spare · toward {f.toward}{l ? ` · ${l}` : ""}
                                </span>
                                {canWrite ? menuFor(side, row.coreNo,
                                  <button type="button" disabled={busy}
                                    className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5">
                                    <span>+ join</span>
                                    <ChevronDown className="size-3 shrink-0" />
                                  </button>)
                                : (
                                  <span className="text-xs text-faint-foreground">
                                    not recorded
                                  </span>
                                )}
                              </>)}
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      ) : !cable?.cores ? (
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
                          : "nothing recorded"}
                      </span>
                    </button>
                    {row.kind === "free" && canWrite && (
                      <DestMenu
                        here={here} cables={menuCables(cable)} boxes={boxes} people={people}
                        herePorts={herePorts} joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.from, { cableId: cid, coreNo: n })}
                        onHere={(port) => join(row.from, null, port)}
                        onBox={(id, far) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.from },
                          { deviceId: id }, far)}
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
                    here={here} ports={herePorts}
                    reach={destOf(row.coreNo)?.to
                      ? reachOf(destOf(row.coreNo)!.to!) : null}
                    farName={destOf(row.coreNo)?.to
                      ? byId.get(destOf(row.coreNo)!.to!.cableId)?.name ?? null : null}
                    label={cable.labels[String(row.coreNo)] ?? null}
                    carries={carriesOf(cable.cable_id, row.coreNo)}
                    canWrite={canWrite} busy={busy}
                    onClear={() => clearRow(row.coreNo)}
                    onTrace={() => onTrace?.(
                      { cableId: cable.cable_id, coreNo: row.coreNo })}
                    menu={(child) => (
                      <DestMenu
                        here={here} cables={menuCables(cable)} boxes={boxes} people={people}
                        herePorts={herePorts} joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.coreNo, { cableId: cid, coreNo: n })}
                        onHere={(port) => join(row.coreNo, null, port)}
                        onBox={(id, far) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.coreNo },
                          { deviceId: id }, far)}
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
      </>)}
    </div>
  )
}

function PortList({
  ports, cables, unplaced, joins, joinsOf, canWrite, busy, addKinds, undrawn,
  boxes, people, boxOf, onLand, onConnect, onDrop, onClear,
}: {
  ports: TrayPort[]
  cables: TrayCable[]
  boxes: TrayBox[]
  people: TrayPerson[]
  unplaced: Array<{ mac: string; name: string | null }>
  joins: FibreJoint[]
  joinsOf: (cableId: number, coreNo: number) => { to: Fibre | null } | undefined
  canWrite: boolean
  busy?: boolean
  addKinds: string[]
  undrawn: UndrawnLink[]
  boxOf?: (id: number) => TrayBox | undefined
  onLand: (f: Fibre, port: TrayPortRef) => void
  onConnect: (deviceId: number, port: TrayPortRef, far?: FarLanding) => void
  onDrop: (mac: string, port: TrayPortRef) => void
  onClear: (f: Fibre) => void
}) {
  const [adding, setAdding] = useState<string | null>(null)
  const [addKind, setAddKind] = useState<string>("")
  const addingKind = addKinds.includes(addKind) ? addKind : (addKinds[0] ?? "")
  const already = new Set(unplaced.map((d) => d.mac))
  const legPeople: TrayPerson[] = [
    ...unplaced.map((d) => ({ mac: d.mac, name: d.name || d.mac, km: null })),
    ...people.filter((p) => !already.has(p.mac)),
  ]
  const onPort = new Map<string, FibreJoint>()
  for (const j of joins) {
    if (j.b_cable_id == null && j.port_kind) {
      onPort.set(`${j.port_kind}:${portKey(j.port_ref)}`, j)
    }
  }
  const far = new Map(cables.map((c) => [c.cable_id, c]))
  const freePorts = ports.filter((p) => !onPort.has(`${p.kind}:${portKey(p.ref)}`))
  const [openRuns, setOpenRuns] = useState<Set<string>>(new Set())
  const portRows = useMemo(() => {
    const busyPort = (p: TrayPort) =>
      onPort.has(`${p.kind}:${portKey(p.ref)}`) || p.drops.length > 0
    const out: Array<
      | { kind: "port"; port: TrayPort }
      | { kind: "run"; id: string; from: string; to: string; first: TrayPort }> = []
    let run: TrayPort[] = []
    const flush = () => {
      if (!run.length) return
      const id = `${run[0].kind}:${portKey(run[0].ref)}`
      if (run.length >= MIN_PORT_RUN && !openRuns.has(id)) {
        out.push({ kind: "run", id, from: run[0].label,
                   to: run[run.length - 1].label, first: run[0] })
      } else {
        for (const p of run) out.push({ kind: "port", port: p })
      }
      run = []
    }
    for (const p of ports) {
      if (busyPort(p) || !p.ref
          || (run.length && run[0].kind !== p.kind)) {
        flush()
        if (busyPort(p) || !p.ref) out.push({ kind: "port", port: p })
        else run.push(p)
        continue
      }
      run.push(p)
    }
    flush()
    return out
  }, [ports, onPort, openRuns])
  return (
    <>
    {canWrite && undrawn.length > 0 && (
      <div className="mb-2 overflow-hidden rounded-md border border-dashed border-border-subtle">
        <div className="flex items-center gap-1.5 border-b border-border-subtle bg-muted/40 px-2 py-1">
          <span className="text-2xs text-muted-foreground">
            On the network map, not yet in the fibre
          </span>
          <span className="ml-auto shrink-0 text-2xs text-faint-foreground">
            {undrawn.length}
          </span>
        </div>
        {undrawn.map((u) => (
          <div key={`${u.far.device_id ?? u.far.mac}`}
            className="flex items-center gap-2 border-b border-border-subtle px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
            <span className="w-14 shrink-0 truncate font-mono text-2xs text-faint-foreground">
              {u.relation}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs">{u.far.name}</span>
            <PortPick ports={freePorts} addKinds={addKinds} disabled={busy}
              far={askFarPort(boxOf, u.far.device_id)}
              onPick={(port, far) => u.far.device_id != null
                && onConnect(u.far.device_id, port, far)} />
          </div>
        ))}
      </div>
    )}
    <div className="overflow-hidden rounded-md border border-border-subtle">
      {portRows.map((r) => {
        if (r.kind === "run") {
          return (
            <div key={`run${r.id}`}
              className="flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
              <button type="button"
                onClick={() => setOpenRuns((s) => new Set(s).add(r.id))}
                className="flex min-w-0 flex-1 items-center gap-2 text-left">
                <ChevronRight className="size-3 shrink-0 text-faint-foreground" />
                <span className="min-w-0 truncate font-mono text-2xs text-muted-foreground">
                  {r.from === r.to ? r.from : `${r.from}–${r.to}`}
                </span>
                {!canWrite && (
                  <span className="shrink-0 text-2xs text-faint-foreground">
                    nothing on them
                  </span>
                )}
              </button>
              {canWrite && (
                <SourceMenu cables={cables} joinsOf={joinsOf} boxes={boxes}
                  people={r.first.kind === "leg" ? legPeople : []}
                  onPick={(f) => onLand(f, { kind: r.first.kind, ref: r.first.ref })}
                  onConnect={(id, far) =>
                    onConnect(id, { kind: r.first.kind, ref: r.first.ref }, far)}
                  onDrop={r.first.kind === "leg"
                    ? (mac) => onDrop(mac, { kind: r.first.kind, ref: r.first.ref })
                    : undefined}>
                  <button type="button" disabled={busy}
                    className="flex min-w-0 shrink items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5 hover:text-foreground">
                    <span className="truncate">Connect {r.first.label}…</span>
                    <ChevronDown className="size-3 shrink-0" />
                  </button>
                </SourceMenu>
              )}
            </div>
          )
        }
        const p = r.port
        const j = onPort.get(`${p.kind}:${portKey(p.ref)}`)
        const fibre: Fibre | null = j ? { cableId: j.a_cable_id, coreNo: j.a_core_no } : null
        return (
          <div key={`${p.kind}:${p.ref}`}
            className="group flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
            <span className="flex w-16 shrink-0 items-center gap-1.5 font-mono text-2xs text-muted-foreground"
              title={p.live == null ? p.label
                     : `${p.label} — ${p.live ? "up" : "down"}`}>
              <PortDot live={p.live} />
              <span className="min-w-0 truncate">{p.label}</span>
            </span>
            <div className="flex min-w-0 flex-1 items-center justify-end gap-1.5">
              {fibre ? (
                <span className="flex min-w-0 items-center gap-1.5 text-xs"
                  title={far.get(fibre.cableId)?.name || undefined}>
                  <span className="truncate">
                    {far.get(fibre.cableId)?.far?.name ?? "the far end"}
                  </span>
                  {!far.get(fibre.cableId)?.plumbing && (<>
                    <StrandSwatch coreNo={fibre.coreNo} className="shrink-0" />
                    <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                      core {fibre.coreNo}
                    </span>
                  </>)}
                </span>
              ) : p.drops.length ? (
                <span className="flex min-w-0 items-center gap-1.5 text-xs">
                  <User className="size-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">
                    {p.drops.map((d) => d.name || d.mac).join(", ")}
                  </span>
                </span>
              ) : !canWrite ? (
                <span className="text-xs text-faint-foreground">nothing on it</span>
              ) : (
                <SourceMenu cables={cables} joinsOf={joinsOf}
                  boxes={boxes} people={p.kind === "leg" ? legPeople : []}
                  onPick={(f) => onLand(f, { kind: p.kind, ref: p.ref })}
                  onConnect={(id, far) =>
                    onConnect(id, { kind: p.kind, ref: p.ref }, far)}
                  onDrop={p.kind === "leg"
                    ? (mac) => onDrop(mac, { kind: p.kind, ref: p.ref }) : undefined}>
                  <button type="button" disabled={busy}
                    className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5 hover:text-foreground">
                    <span>Connect…</span>
                    <ChevronDown className="size-3 shrink-0" />
                  </button>
                </SourceMenu>
              )}
              {canWrite && fibre && (
                <button type="button" onClick={() => onClear(fibre)} disabled={busy}
                  title="Take this fibre off the port"
                  className="shrink-0 rounded p-0.5 text-faint-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100">
                  <X className="size-3" />
                </button>
              )}
            </div>
          </div>
        )
      })}
      {canWrite && addKinds.length > 0 && (adding === null ? (
        <button type="button" onClick={() => { setAddKind(addKinds[0]); setAdding("") }}
          className="flex w-full items-center gap-1.5 border-t bg-muted/40 px-2 py-1.5 text-2xs text-faint-foreground hover:bg-foreground/5">
          <Plus className="size-3 shrink-0" />
          <span>
            Name another {addKinds.map(portKindWord).join(" or ")}
          </span>
        </button>
      ) : (
        <div className="flex items-center gap-2 border-t bg-muted/40 px-2 py-1.5">
          {addKinds.length > 1 ? (
            <div className="flex shrink-0 rounded border border-border">
              {addKinds.map((k) => (
                <button key={k} type="button" onClick={() => setAddKind(k)}
                  className={cn("px-1.5 py-0.5 font-mono text-2xs first:rounded-l last:rounded-r",
                    k === addingKind ? "bg-selected text-foreground"
                                     : "text-faint-foreground hover:bg-foreground/5")}>
                  {portKindWord(k)}
                </button>
              ))}
            </div>
          ) : (
            <span className="w-14 shrink-0 font-mono text-2xs text-faint-foreground">
              {portKindWord(addingKind)}
            </span>
          )}
          <input autoFocus value={adding} onChange={(e) => setAdding(e.target.value)}
            inputMode={isNumberedKind(addingKind) ? "numeric" : "text"}
            placeholder={isNumberedKind(addingKind) ? "number" : "name on the box"}
            maxLength={PORT_REF_MAX}
            onKeyDown={(e) => { if (e.key === "Escape") setAdding(null) }}
            className={cn("h-6 min-w-0 rounded border border-border bg-background px-1.5 text-2xs",
              isNumberedKind(addingKind) ? "w-20" : "w-36")} />
          <SourceMenu cables={cables} joinsOf={joinsOf} boxes={boxes}
            onConnect={(id, far) => {
              const ref = cleanRef(addingKind, adding)
              if (ref == null) return
              onConnect(id, { kind: addingKind, ref }, far)
              setAdding(null)
            }}
            onPick={(f) => {
              const ref = cleanRef(addingKind, adding)
              if (ref == null) return
              onLand(f, { kind: addingKind, ref })
              setAdding(null)
            }}>
            <button type="button" disabled={busy || cleanRef(addingKind, adding) == null}
              className="flex shrink-0 items-center gap-1 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-2xs text-faint-foreground hover:border-border hover:bg-foreground/5 disabled:opacity-50">
              <span>land a fibre on it</span>
              <ChevronDown className="size-3 shrink-0" />
            </button>
          </SourceMenu>
          <button type="button" onClick={() => setAdding(null)}
            className="shrink-0 rounded p-0.5 text-faint-foreground hover:text-foreground"
            aria-label="Cancel">
            <X className="size-3" />
          </button>
        </div>
      ))}
      {unplaced.length > 0 && (
        <div className="border-t bg-muted/40 px-2 py-1.5 text-2xs text-faint-foreground">
          {unplaced.length} recorded {unplaced.length === 1 ? "drop" : "drops"} with
          no leg noted — {unplaced.map((d) => d.name || d.mac).join(", ")}
        </div>
      )}
    </div>
    </>
  )
}

function PortPick({ ports, addKinds, disabled, far, onPick }: {
  ports: TrayPort[]
  addKinds: string[]
  disabled?: boolean
  far?: TrayBox
  onPick: (port: TrayPortRef, far?: FarLanding) => void
}) {
  const farPorts = far?.ports ?? []
  // SITE 7: the far end gets a submenu whenever the far box HAS kinds — not only when
  // we happen to have walked its ports. Four OLTs on this fleet walk nothing, and a
  // box you can see but have not walked is the one that most needs naming by hand.
  const farKinds = far?.port_kinds ?? []
  // ...and where the far box is an ENCLOSURE it has no ports at all, so the question
  // is which CORE. Without it the fibre arrives at the closure and stops, which is
  // the record the switch panel used to leave.
  const farCables = (far?.cables ?? []).filter((c) => c.freeCores.length > 0)
  const askFar = farPorts.length > 0 || farKinds.length > 0 || farCables.length > 0
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" disabled={disabled}
          className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5 hover:text-foreground">
          <span>Connect…</span>
          <ChevronDown className="size-3 shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48 max-w-72">
        <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
          on which port here
        </DropdownMenuLabel>
        {ports.map((p) => {
          const here = (
            <>
              <PortDot live={p.live} />
              <span className="min-w-0 truncate font-mono text-2xs" title={p.label}>
                {p.label}
              </span>
            </>
          )
          if (!askFar) {
            return (
              <DropdownMenuItem key={`${p.kind}:${p.ref}`}
                onSelect={() => onPick({ kind: p.kind, ref: p.ref })}>
                {here}
              </DropdownMenuItem>
            )
          }
          return (
            <DropdownMenuSub key={`${p.kind}:${p.ref}`}>
              <DropdownMenuSubTrigger>{here}</DropdownMenuSubTrigger>
              <DropdownMenuPortal>
                <DropdownMenuSubContent className="max-h-80 min-w-44 overflow-y-auto">
                  <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
                    …into which port on {far?.name}
                  </DropdownMenuLabel>
                  <DropdownMenuItem
                    onSelect={() => onPick({ kind: p.kind, ref: p.ref })}>
                    <span className="text-2xs text-faint-foreground">
                      Not known yet
                    </span>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  {farPorts.map((fp) => (
                    <DropdownMenuItem key={`${fp.kind}:${fp.ref}`} title={fp.label}
                      onSelect={() => onPick({ kind: p.kind, ref: p.ref },
                                             { port: { kind: fp.kind, ref: fp.ref } })}>
                      <PortDot live={fp.live} />
                      <span className="min-w-0 truncate font-mono text-2xs">{fp.label}</span>
                    </DropdownMenuItem>
                  ))}
                  {farKinds.length > 0 && (
                    <>
                      {farPorts.length > 0 && <DropdownMenuSeparator />}
                      <AddPortRow kinds={farKinds} disabled={disabled}
                        onPick={(fp) => onPick({ kind: p.kind, ref: p.ref },
                                               { port: fp })} />
                    </>
                  )}
                  {farCables.map((fc) => (
                    <DropdownMenuSub key={fc.cable_id}>
                      <DropdownMenuSubTrigger>
                        <Waypoints className="size-3 shrink-0 text-muted-foreground" />
                        <span className="truncate">{fc.name || "cable"}</span>
                        <span className="ml-auto shrink-0 pl-2 text-2xs text-faint-foreground">
                          {fc.cores ? `${fc.cores}F` : "no count"}
                        </span>
                      </DropdownMenuSubTrigger>
                      <DropdownMenuPortal>
                        <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
                          <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
                            …into which core
                          </DropdownMenuLabel>
                          {fc.freeCores.map((n) => (
                            <DropdownMenuItem key={n}
                              onSelect={() => onPick({ kind: p.kind, ref: p.ref },
                                                     { cableId: fc.cable_id, coreNo: n })}>
                              <StrandSwatch coreNo={n} className="shrink-0" />
                              <span>core {n}</span>
                              <span className="ml-auto pl-3 text-2xs text-faint-foreground">
                                {strandName(((n - 1) % TUBE_SIZE) + 1)}
                              </span>
                            </DropdownMenuItem>
                          ))}
                        </DropdownMenuSubContent>
                      </DropdownMenuPortal>
                    </DropdownMenuSub>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuPortal>
            </DropdownMenuSub>
          )
        })}
        <AddPortRow kinds={addKinds} disabled={disabled}
          onPick={(port) => onPick(port)} />
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onPick({ kind: "", ref: null })}>
          <span className="text-2xs text-faint-foreground">
            Record it — port not known yet
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function CoreRow({
  coreNo, cores, join, here, ports, reach, farName, label, carries, canWrite, busy,
  onClear, onTrace, menu,
}: {
  coreNo: number
  cores: number | null
  carries?: string | null
  join: { to: Fibre | null; joint?: FibreJoint } | undefined
  here: string
  // This box's own ports, so a termination is named the way the box names it.
  ports: TrayPort[]
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
  const port = portName(ports, join?.joint?.port_kind, join?.joint?.port_ref)
  const body = !join ? null
    : join.to == null
      ? <><Plug className="size-3 shrink-0 text-muted-foreground" />
          <span className="truncate">into {here}</span>
          {port && (
            <span className="shrink-0 font-mono text-2xs text-muted-foreground">
              {port}
            </span>
          )}</>
      : reach
        ? <><Plug className="size-3 shrink-0 text-muted-foreground" />
            <span className="truncate">{reach.name}</span></>
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

      {/* WHAT THIS CORE CARRIES — the far end the fibre actually reaches, across
          every splice. A closure used to show core 1 with nothing on it while the
          fibre in it ran to a switch two closures away. `here` is dropped: the row
          is already standing at this point and repeating it says nothing. */}
      {(carries || label) && (
        <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-2xs">
          {carries && (
            <span className="min-w-0 truncate text-muted-foreground">
              {carries}
            </span>
          )}
          {label && (
            <span className="min-w-0 truncate text-faint-foreground">{label}</span>
          )}
        </span>
      )}

      <div className={cn("flex min-w-0 items-center justify-end gap-1.5",
        done || !(carries || label) ? "flex-1" : "")}>
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
