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
import { isPlumbing, portLabel, strandLabel, strandName, TUBE_SIZE } from "@/lib/fiber"
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
  port_kind?: string | null
  ports?: TrayPort[]
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

function BoxItem({ box, onPick }: {
  box: TrayBox
  onPick: (port?: TrayPortRef) => void
}) {
  const [typed, setTyped] = useState("")
  const kind = box.port_kind
  const note = box.declared ? "on the map" : kmLabel(box.km) || box.device_type
  if (!kind) {
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
              Port not known yet
            </span>
          </DropdownMenuItem>
          {(box.ports?.length ?? 0) > 0 && <DropdownMenuSeparator />}
          {(box.ports ?? []).map((p) => (
            <DropdownMenuItem key={`${p.kind}:${p.no}`}
              onSelect={() => onPick({ kind: p.kind, no: p.no })}>
              <span className="font-mono text-2xs">{p.label}</span>
              {p.device_label && (
                <span className="ml-auto min-w-0 truncate pl-2 text-2xs text-faint-foreground">
                  {p.device_label}
                </span>
              )}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <div className="flex items-center gap-1.5 px-2 py-1.5"
            onKeyDown={(e) => e.stopPropagation()}>
            <span className="shrink-0 font-mono text-2xs text-faint-foreground">
              {kind === "pon" ? "PON" : kind}
            </span>
            <input value={typed} onChange={(e) => setTyped(e.target.value)}
              inputMode="numeric" placeholder="no."
              className="h-6 w-14 rounded border border-border bg-background px-1.5 text-2xs" />
            <button type="button"
              disabled={!/^\d+$/.test(typed.trim()) || parseInt(typed, 10) < 1}
              onClick={() => onPick({ kind, no: parseInt(typed, 10) })}
              className="rounded px-1.5 py-0.5 text-2xs text-muted-foreground hover:bg-foreground/10 disabled:opacity-40">
              Connect
            </button>
          </div>
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
  onBox: (deviceId: number, port?: TrayPortRef) => void
  onPerson: (mac: string) => void
  children: React.ReactNode
}) {
  const [q, setQ] = useState("")
  const hit = (s: string) => s.toLowerCase().includes(q.trim().toLowerCase())
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
                      <DropdownMenuItem key={`${p.kind}:${p.no}`}
                        onSelect={() => onHere({ kind: p.kind, no: p.no })}>
                        <span className="font-mono text-2xs">{p.label}</span>
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuPortal>
              </DropdownMenuSub>
            ))}
            {bs.map((b) => (
              <BoxItem key={b.id} box={b} onPick={(port) => onBox(b.id, port)} />
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
  onConnect?: (deviceId: number, port?: TrayPortRef) => void
  onDrop?: (mac: string) => void
  children: React.ReactNode
}) {
  const [q, setQ] = useState("")
  const hit = (s: string) => s.toLowerCase().includes(q.trim().toLowerCase())
  const bs = onConnect ? boxes.filter((b) => hit(b.name)) : []
  const ps = onDrop ? people.filter((p) => hit(p.name)) : []
  const cs = cables.filter((c) => hit(c.name))
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
              <BoxItem key={b.id} box={b} onPick={(port) => onConnect(b.id, port)} />
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
            port?: TrayPortRef) => void
  onConnect?: (deviceId: number, port: TrayPortRef, toPort?: TrayPortRef) => void
  onDrop?: (mac: string, port: TrayPortRef) => void
  onThrough: (aCableId: number, bCableId: number) => void
  onClear: (f: Fibre) => void
  onClearError?: () => void
  onTrace?: (f: Fibre) => void
}

export type TrayPortRef = { kind: string; no: number | null }

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
    .map((j) => `${j.port_kind}:${j.port_no ?? ""}`))
  const left = fibre?.undrawn?.length ?? todo
  const done = ports.filter((p) => wired.has(`${p.kind}:${p.no ?? ""}`)).length
  const summary = left > 0
    ? `${left} to connect`
    : done > 0 ? `${done} connected`
    : ports.length > 0 ? `${ports.length} port${ports.length === 1 ? "" : "s"}`
    : cables.length === 0 ? "nothing recorded yet"
    : (() => {
        const real = cables.filter(({ cable }) => !isPlumbing(cable))
        if (!real.length) return `${cables.length} connected`
        return [...real]
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
          {cables.length === 0 && !(fibre?.ports?.length || fibre?.port_add) ? (
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

  const rows = useMemo(
    () => (cable?.cores ? schedule(cable.cores, destOf, expanded) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cable, joins, expanded])

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
  if (!cables.length && herePorts.length === 0 && !fibre.port_add
      && undrawn.length === 0) {
    return (
      <p className="px-1 py-2 text-xs text-muted-foreground">
        No cable is recorded as ending here yet. Lay one on the map and this
        becomes its splice schedule.
      </p>
    )
  }

  const laid = cables.filter((c) => !c.plumbing)

  const others = laid.filter((c) => c.cable_id !== cable?.cable_id)

  return (
    <div className="space-y-2">
      {(herePorts.length > 0 || fibre.port_add || undrawn.length > 0) && (
        <PortList
          ports={herePorts} cables={cables} unplaced={fibre.unplaced_drops ?? []}
          joins={fibre.joints} joinsOf={joinOf} canWrite={canWrite} busy={busy}
          addKind={fibre.port_add} boxes={boxes} people={people} undrawn={undrawn}
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
            {laid.length === 1
              ? `Splices in ${laid[0].cores ? `the ${laid[0].cores}F ` : ""}${laid[0].name}`
              : `Splices · ${laid.length} cables here`}
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
            {laid.map((c) => (
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

        {cable && onOpenCable && (
          <Button size="icon" variant="ghost"
            className="size-6 shrink-0 text-muted-foreground"
            title={`Open the record for ${cable.name}`}
            onClick={() => onOpenCable(cable.cable_id)}>
            <FileText className="size-3.5" />
          </Button>
        )}

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
                        here={here} cables={others} boxes={boxes} people={people}
                        herePorts={herePorts} joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.from, { cableId: cid, coreNo: n })}
                        onHere={(port) => join(row.from, null, port)}
                        onBox={(id, port) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.from },
                          { deviceId: id }, port)}
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
                        herePorts={herePorts} joinsOf={joinOf}
                        onSplice={(cid, n) => join(row.coreNo, { cableId: cid, coreNo: n })}
                        onHere={(port) => join(row.coreNo, null, port)}
                        onBox={(id, port) => cable && onTail?.(
                          { cableId: cable.cable_id, coreNo: row.coreNo },
                          { deviceId: id }, port)}
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
  ports, cables, unplaced, joins, joinsOf, canWrite, busy, addKind, undrawn,
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
  addKind: string | null
  undrawn: UndrawnLink[]
  boxOf?: (id: number) => TrayBox | undefined
  onLand: (f: Fibre, port: TrayPortRef) => void
  onConnect: (deviceId: number, port: TrayPortRef, toPort?: TrayPortRef) => void
  onDrop: (mac: string, port: TrayPortRef) => void
  onClear: (f: Fibre) => void
}) {
  const [adding, setAdding] = useState<string | null>(null)
  const already = new Set(unplaced.map((d) => d.mac))
  const legPeople: TrayPerson[] = [
    ...unplaced.map((d) => ({ mac: d.mac, name: d.name || d.mac, km: null })),
    ...people.filter((p) => !already.has(p.mac)),
  ]
  const onPort = new Map<string, FibreJoint>()
  for (const j of joins) {
    if (j.b_cable_id == null && j.port_kind) {
      onPort.set(`${j.port_kind}:${j.port_no ?? ""}`, j)
    }
  }
  const far = new Map(cables.map((c) => [c.cable_id, c]))
  const freePorts = ports.filter((p) => !onPort.has(`${p.kind}:${p.no ?? ""}`))
  const [openRuns, setOpenRuns] = useState<Set<number>>(new Set())
  const portRows = useMemo(() => {
    const busyPort = (p: TrayPort) =>
      onPort.has(`${p.kind}:${p.no ?? ""}`) || p.drops.length > 0
    const out: Array<
      | { kind: "port"; port: TrayPort }
      | { kind: "run"; from: number; to: number; portKind: string }> = []
    let run: TrayPort[] = []
    const flush = () => {
      if (!run.length) return
      const first = run[0].no
      const last = run[run.length - 1].no
      if (run.length >= MIN_PORT_RUN && first != null && last != null
          && !openRuns.has(first)) {
        out.push({ kind: "run", from: first, to: last, portKind: run[0].kind })
      } else {
        for (const p of run) out.push({ kind: "port", port: p })
      }
      run = []
    }
    for (const p of ports) {
      if (busyPort(p) || p.no == null
          || (run.length && run[0].kind !== p.kind)) {
        flush()
        if (busyPort(p) || p.no == null) out.push({ kind: "port", port: p })
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
            <PortPick ports={freePorts} addKind={addKind} disabled={busy}
              far={askFarPort(boxOf, u.far.device_id)}
              onPick={(port, toPort) => u.far.device_id != null
                && onConnect(u.far.device_id, port, toPort)} />
          </div>
        ))}
      </div>
    )}
    <div className="overflow-hidden rounded-md border border-border-subtle">
      {portRows.map((r) => {
        if (r.kind === "run") {
          return (
            <div key={`run${r.from}`}
              className="flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
              <button type="button"
                onClick={() => setOpenRuns((s) => new Set(s).add(r.from))}
                className="flex min-w-0 flex-1 items-center gap-2 text-left">
                <ChevronRight className="size-3 shrink-0 text-faint-foreground" />
                <span className="font-mono text-2xs text-muted-foreground">
                  {r.from === r.to ? r.from : `${r.from}–${r.to}`}
                </span>
                {!canWrite && (
                  <span className="min-w-0 flex-1 truncate text-2xs text-faint-foreground">
                    nothing on them
                  </span>
                )}
              </button>
              {canWrite && (
                <SourceMenu cables={cables} joinsOf={joinsOf} boxes={boxes}
                  people={r.portKind === "leg" ? legPeople : []}
                  onPick={(f) => onLand(f, { kind: r.portKind, no: r.from })}
                  onConnect={(id, toPort) =>
                    onConnect(id, { kind: r.portKind, no: r.from }, toPort)}
                  onDrop={r.portKind === "leg"
                    ? (mac) => onDrop(mac, { kind: r.portKind, no: r.from })
                    : undefined}>
                  <button type="button" disabled={busy}
                    className="flex shrink-0 items-center gap-1.5 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-xs text-faint-foreground hover:border-border hover:bg-foreground/5 hover:text-foreground">
                    <span>Connect {portLabel(r.portKind, r.from)}…</span>
                    <ChevronDown className="size-3 shrink-0" />
                  </button>
                </SourceMenu>
              )}
            </div>
          )
        }
        const p = r.port
        const j = onPort.get(`${p.kind}:${p.no ?? ""}`)
        const fibre: Fibre | null = j ? { cableId: j.a_cable_id, coreNo: j.a_core_no } : null
        return (
          <div key={`${p.kind}:${p.no}`}
            className="group flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 hover:bg-foreground/5">
            <span className="w-14 shrink-0 truncate font-mono text-2xs text-muted-foreground"
              title={p.device_label ? `${p.label} · ${p.device_label}` : undefined}>
              {p.label}
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
                  onPick={(f) => onLand(f, { kind: p.kind, no: p.no })}
                  onConnect={(id, toPort) =>
                    onConnect(id, { kind: p.kind, no: p.no }, toPort)}
                  onDrop={p.kind === "leg"
                    ? (mac) => onDrop(mac, { kind: p.kind, no: p.no }) : undefined}>
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
      {canWrite && addKind && (adding === null ? (
        <button type="button" onClick={() => setAdding("")}
          className="flex w-full items-center gap-1.5 border-t bg-muted/40 px-2 py-1.5 text-2xs text-faint-foreground hover:bg-foreground/5">
          <Plus className="size-3 shrink-0" />
          <span>Name another {addKind === "pon" ? "PON" : addKind}</span>
        </button>
      ) : (
        <div className="flex items-center gap-2 border-t bg-muted/40 px-2 py-1.5">
          <span className="w-14 shrink-0 font-mono text-2xs text-faint-foreground">
            {addKind === "pon" ? "PON" : addKind}
          </span>
          <input autoFocus value={adding} onChange={(e) => setAdding(e.target.value)}
            inputMode="numeric" placeholder="number"
            onKeyDown={(e) => { if (e.key === "Escape") setAdding(null) }}
            className="h-6 w-20 rounded border border-border bg-background px-1.5 text-2xs" />
          <SourceMenu cables={cables} joinsOf={joinsOf} boxes={boxes}
            onConnect={(id, toPort) => {
              const no = parseInt(adding, 10)
              if (!Number.isFinite(no) || no < 1) return
              onConnect(id, { kind: addKind, no }, toPort)
              setAdding(null)
            }}
            onPick={(f) => {
              const no = parseInt(adding, 10)
              if (!Number.isFinite(no) || no < 1) return
              onLand(f, { kind: addKind, no })
              setAdding(null)
            }}>
            <button type="button" disabled={busy || !adding.trim()}
              className="flex items-center gap-1 rounded-md border border-dashed border-border-subtle px-1.5 py-0.5 text-2xs text-faint-foreground hover:border-border hover:bg-foreground/5 disabled:opacity-50">
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

function PortPick({ ports, addKind, disabled, far, onPick }: {
  ports: TrayPort[]
  addKind: string | null
  disabled?: boolean
  far?: TrayBox
  onPick: (port: TrayPortRef, toPort?: TrayPortRef) => void
}) {
  const [typed, setTyped] = useState("")
  const farPorts = far?.ports ?? []
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
              <span className="shrink-0 font-mono text-2xs">{p.label}</span>
              {p.device_label && (
                <span className="ml-auto min-w-0 truncate pl-2 text-2xs text-faint-foreground">
                  {p.device_label}
                </span>
              )}
            </>
          )
          if (!farPorts.length) {
            return (
              <DropdownMenuItem key={`${p.kind}:${p.no}`}
                onSelect={() => onPick({ kind: p.kind, no: p.no })}>
                {here}
              </DropdownMenuItem>
            )
          }
          return (
            <DropdownMenuSub key={`${p.kind}:${p.no}`}>
              <DropdownMenuSubTrigger>{here}</DropdownMenuSubTrigger>
              <DropdownMenuPortal>
                <DropdownMenuSubContent className="max-h-80 min-w-44 overflow-y-auto">
                  <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">
                    …into which port on {far?.name}
                  </DropdownMenuLabel>
                  <DropdownMenuItem
                    onSelect={() => onPick({ kind: p.kind, no: p.no })}>
                    <span className="text-2xs text-faint-foreground">
                      Not known yet
                    </span>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  {farPorts.map((fp) => (
                    <DropdownMenuItem key={`${fp.kind}:${fp.no}`}
                      onSelect={() => onPick({ kind: p.kind, no: p.no },
                                             { kind: fp.kind, no: fp.no })}>
                      <span className="font-mono text-2xs">{fp.label}</span>
                      {fp.device_label && (
                        <span className="ml-auto min-w-0 truncate pl-2 text-2xs text-faint-foreground">
                          {fp.device_label}
                        </span>
                      )}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuPortal>
            </DropdownMenuSub>
          )
        })}
        {addKind && (
          <div className="flex items-center gap-1.5 px-2 py-1.5"
            onKeyDown={(e) => e.stopPropagation()}>
            <span className="shrink-0 font-mono text-2xs text-faint-foreground">
              {addKind === "pon" ? "PON" : addKind}
            </span>
            <input value={typed} onChange={(e) => setTyped(e.target.value)}
              inputMode="numeric" placeholder="number"
              className="h-6 w-16 rounded border border-border bg-background px-1.5 text-2xs" />
            <button type="button"
              disabled={!/^\d+$/.test(typed.trim()) || parseInt(typed, 10) < 1}
              onClick={() => onPick({ kind: addKind, no: parseInt(typed, 10) })}
              className="rounded px-1.5 py-0.5 text-2xs text-muted-foreground hover:bg-foreground/10 disabled:opacity-40">
              Connect
            </button>
          </div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onPick({ kind: "", no: null })}>
          <span className="text-2xs text-faint-foreground">
            Record it — port not known yet
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function CoreRow({
  coreNo, cores, join, here, reach, farName, label, canWrite, busy,
  onClear, onTrace, menu,
}: {
  coreNo: number
  cores: number | null
  join: { to: Fibre | null; joint?: FibreJoint } | undefined
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
  const port = portLabel(join?.joint?.port_kind, join?.joint?.port_no)
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
