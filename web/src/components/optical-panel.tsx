import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { inventoryApi, ApiError } from "@/lib/api"
import type { DupMac, OnuOptic, OpticsResponse, OrgDevice, PonFault } from "@/lib/types"
import {
  ago, durationSince, isDownState, isFresh, onuIdentityTitle, onuName, onuSev,
  type OnuSev,
} from "@/lib/format"
import { useAuth } from "@/hooks/use-auth"
import { SnmpDiagnosis } from "@/components/snmp-diagnosis"
import { RxDiagnosis, RxFreshness } from "@/components/rx-diagnosis"
import { ReferenceOnuButton } from "@/components/reference-onu"
import { SubscriberDialog } from "@/components/subscriber-detail"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import { Reading, readingState } from "@/components/reading"
import { RxScale } from "@/components/rx-scale"

export { onuSev } from "@/lib/format"
type Sev = OnuSev

const CELL: Record<Sev, string> = {
  ok: "bg-success/70",
  warn: "bg-warning",
  crit: "bg-destructive",
  offline: "bg-muted-foreground/40",
}
export const DOT: Record<Sev, string> = {
  ok: "bg-success", warn: "bg-warning", crit: "bg-destructive", offline: "bg-muted-foreground/40",
}

function fmtDbm(v: number | null): string {
  return v == null ? "—" : v.toFixed(1)
}
function fmtKm(m: number | null): string {
  return m == null ? "—" : `${(m / 1000).toFixed(2)} km`
}
function fmtOnuKm(m: number | null): string {
  return m ? fmtKm(m) : "—"
}
function ackActive(o: OnuOptic): boolean {
  return !!o.ack_until && new Date(o.ack_until).getTime() > Date.now()
}

function onuIndex(o: OnuOptic): string {
  if (o.onu_id != null) return `#${o.onu_id}`
  const tail = (o.onu_key ?? "").split(/[.:/]/).pop()
  return tail ? `#${tail}` : "—"
}

interface Pon {
  port: string
  onus: OnuOptic[]
  online: number
  worstRx: number | null
  bestRx: number | null
  crit: number
  warn: number
}

function groupByPon(onus: OnuOptic[]): Pon[] {
  const map = new Map<string, OnuOptic[]>()
  for (const o of onus) {
    const key = o.pon_port ?? "—"
    ;(map.get(key) ?? map.set(key, []).get(key)!).push(o)
  }
  const pons: Pon[] = []
  for (const [port, list] of map) {
    const rx = list.filter((o) => o.state === "online" && o.rx_dbm != null).map((o) => o.rx_dbm!)
    pons.push({
      port,
      onus: list,
      online: list.filter((o) => o.state === "online").length,
      worstRx: rx.length ? Math.min(...rx) : null,
      bestRx: rx.length ? Math.max(...rx) : null,
      crit: list.filter((o) => onuSev(o) === "crit").length,
      warn: list.filter((o) => onuSev(o) === "warn").length,
    })
  }

  pons.sort((a, b) => a.port.localeCompare(b.port, undefined, { numeric: true }))
  return pons
}

function CellStrip({ onus }: { onus: OnuOptic[] }) {

  const ordered = [...onus].sort((a, b) => (a.onu_id ?? 0) - (b.onu_id ?? 0))
  return (
    <div className="flex flex-wrap gap-[3px]">
      {ordered.map((o) => (
        <span
          key={o.id}
          title={`${onuName(o) || `ONU ${o.onu_id ?? ""}`} · ${fmtDbm(o.rx_dbm)} dBm · ${o.state ?? "?"}`}
          className={cn("size-[11px] rounded-[2px]", CELL[onuSev(o)])}
        />
      ))}
    </div>
  )
}

function MacCell({ o, className }: { o: OnuOptic; className?: string }) {
  return (
    <span className={cn("shrink-0 font-mono text-muted-foreground", className)}
      title={o.serial && o.onu_key && o.onu_key !== o.serial
        ? `Slot ${o.onu_key} on this OLT` : undefined}>
      {o.serial || o.onu_key}
    </span>
  )
}

function DistCell({ o, className }: { o: OnuOptic; className?: string }) {
  return (
    <span className={cn("shrink-0 font-mono text-xs tabular-nums text-muted-foreground", className)}
      title="Ranging distance from the OLT. Optical path with slack coils, not road metres.">
      {fmtOnuKm(o.distance_m)}
    </span>
  )
}

function DarkCell({ o, className }: { o: OnuOptic; className?: string }) {
  const text = o.state === "online" ? null
    : o.last_online_at ? `dark ${durationSince(o.last_online_at)}` : "offline"
  return (
    <span className={cn("shrink-0 truncate text-xs text-muted-foreground", className)}>
      {text}
    </span>
  )
}

function AckCell({ o, onAck, pending, className }: {
  o: OnuOptic; onAck: () => void; pending: boolean; className?: string
}) {
  const sev = onuSev(o)
  if (sev === "ok" || o.state !== "online") {
    return <span className={cn("shrink-0", className)} aria-hidden />
  }
  const acked = ackActive(o)
  return (
    <button type="button" onClick={onAck} disabled={pending}
      title={acked
        ? "Excluded from the OLT's optical alarm. Click to un-acknowledge."
        : "Acknowledge for 24h. Keeps this drop out of the OLT's optical alarm badge."}
      className={cn("shrink-0 rounded px-1.5 py-0.5 text-2xs font-medium transition-colors disabled:opacity-50",
        acked
          ? "text-faint-foreground hover:text-foreground"
          : "border border-border text-muted-foreground hover:bg-accent hover:text-foreground",
        className)}>
      {acked ? "acked" : "Ack"}
    </button>
  )
}

function OnuRow({ o, deviceId, focused, noRx, splitters, warnDbm, critDbm }: {
  o: OnuOptic; deviceId: number; focused?: boolean
  warnDbm?: number | null; critDbm?: number | null
  noRx?: boolean
  splitters?: Map<number, string>
}) {
  const qc = useQueryClient()
  const acked = ackActive(o)
  const ack = useMutation({
    mutationFn: () => inventoryApi.ackOnu(o.id, acked ? null : 24),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["optics", deviceId] })
      qc.invalidateQueries({ queryKey: ["inventory"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Acknowledge failed"),
  })
  const onAck = () => ack.mutate()
  const [openSub, setOpenSub] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest" })
  }, [focused])
  return (
    <div ref={ref} className={cn("py-2 text-sm",
      focused && "-mx-1.5 rounded-md bg-accent px-1.5 ring-1 ring-border-strong")}>
      <div className="flex items-center gap-2.5">
        <span className={cn("size-2.5 shrink-0 rounded-full", DOT[onuSev(o)])} />
        <span className="w-8 shrink-0 text-right font-mono text-2xs tabular-nums text-faint-foreground"
          title={`ONU ${o.onu_id ?? "?"}${o.pon_port ? ` on PON ${o.pon_port}` : ""}`
            + (o.onu_key ? ` · slot ${o.onu_key}` : "")}>
          {onuIndex(o)}
        </span>
        {/* One line, so the row prints the headline and the title carries the
            rest. `onuIdentityTitle` names every identity this ONU has and says
            whose each one is — the row is 15px and a customer's full name would
            push the MAC and the Rx off a 380px panel. */}
        {o.serial ? (
          <button type="button"
            className="min-w-0 flex-1 truncate text-left underline-offset-2 hover:underline"
            title={`${onuIdentityTitle(o)} · open this subscriber`}
            onClick={() => setOpenSub(true)}>
            {onuName(o) || <span className="text-muted-foreground">unnamed</span>}
          </button>
        ) : (
          <span className="min-w-0 flex-1 truncate" title={onuIdentityTitle(o)}>
            {onuName(o) || <span className="text-muted-foreground">unnamed</span>}
          </span>
        )}
        <ReferenceOnuButton o={o} deviceId={deviceId} />
        <MacCell o={o} className="hidden w-[8.75rem] text-xs @2xl:block" />
        <span className="hidden w-24 shrink-0 truncate text-xs text-muted-foreground @3xl:inline"
          title={o.drop_passive_id != null
            ? `Drop from ${splitters?.get(o.drop_passive_id) ?? "a splitter that no longer exists"}`
            : "No serving splitter recorded. Add it on the splitter's panel on the map."}>
          {o.drop_passive_id != null
            ? splitters?.get(o.drop_passive_id) ?? "—"
            : <span className="text-faint-foreground">—</span>}
        </span>
        {!noRx && (
          <span className="flex w-36 shrink-0 items-center justify-end gap-2">
            {o.state === "online" && (
              <RxScale rx={o.rx_dbm} warn={warnDbm} crit={critDbm} />
            )}
            <span className={cn("text-right font-mono font-semibold tabular-nums",
              onuSev(o) === "crit" ? "text-destructive" : onuSev(o) === "warn" ? "text-warning" : "")}>
              {o.rx_dbm != null
                ? `${fmtDbm(o.rx_dbm)} dBm`
                : <span className="font-normal text-faint-foreground">—</span>}
            </span>
          </span>
        )}
        <DistCell o={o} className="hidden w-14 text-right @2xl:block" />
        <DarkCell o={o} className="hidden w-16 text-right @2xl:block" />
        <AckCell o={o} onAck={onAck} pending={ack.isPending}
          className="hidden w-11 text-center @2xl:block" />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 pl-[3.875rem] @2xl:hidden">
        <MacCell o={o} className="text-2xs" />
        <span className="text-faint-foreground">·</span>
        <DistCell o={o} />
        {o.state !== "online" && (
          <>
            <span className="text-faint-foreground">·</span>
            <DarkCell o={o} />
          </>
        )}
        <AckCell o={o} onAck={onAck} pending={ack.isPending} className="ml-auto" />
      </div>
      {openSub && o.serial && (
        <SubscriberDialog mac={o.serial} onClose={() => setOpenSub(false)} />
      )}
    </div>
  )
}

function PonRow({ pon, open, onToggle, limit, opticsAt }: {
  pon: Pon; open: boolean; onToggle: () => void; limit: number
  opticsAt?: string | null
}) {

  const hasRx = pon.bestRx != null || pon.worstRx != null
  const rxState = readingState({ value: hasRx ? 1 : null, at: opticsAt })
  const atCap = pon.onus.length >= limit
  return (
    <button onClick={onToggle} aria-expanded={open}
      className={cn("flex w-full flex-col gap-1.5 rounded-md px-2 py-2 text-left hover:bg-foreground/5",
        open && "bg-accent/50")}>
      <span className="flex w-full items-center gap-3">
        <span className="shrink-0 font-mono text-xs font-semibold">PON {pon.port}</span>
        <span className="shrink-0 font-mono text-2xs text-muted-foreground">
          {pon.online}/{pon.onus.length}
        </span>
        {atCap && (
          <span className="shrink-0 rounded bg-destructive-soft px-1.5 py-0.5 text-2xs font-semibold text-destructive">
            at capacity {pon.onus.length}/{limit}
          </span>
        )}
        {hasRx ? (
          <span className="ml-auto flex shrink-0 items-baseline gap-3 text-2xs">
            <Reading value={fmtDbm(pon.bestRx)} state={rxState} at={opticsAt}
              className="text-muted-foreground" />
            <Reading value={fmtDbm(pon.worstRx)} state={rxState} at={opticsAt}
              tone={pon.crit > 0 ? "destructive" : pon.warn > 0 ? "warning" : undefined}
              className={cn("font-semibold", pon.crit === 0 && pon.warn === 0 && "text-muted-foreground")} />
          </span>
        ) : (
          <span className="ml-auto shrink-0">
            <Reading value={null} state="absent"
              reason="This OLT reports no per-ONU Rx. Its firmware has no optical
                      column to read, so nothing was measured here — this is not a
                      reading of zero." />
          </span>
        )}
        {(pon.crit > 0 || pon.warn > 0) && (
          <span className="shrink-0 text-right text-2xs font-semibold">
            {pon.crit > 0 && <span className="text-destructive">{pon.crit}</span>}
            {pon.crit > 0 && pon.warn > 0 && <span className="text-muted-foreground"> · </span>}
            {pon.warn > 0 && <span className="text-warning">{pon.warn}</span>}
          </span>
        )}
        <span className={cn("shrink-0 text-[0.625rem] text-muted-foreground transition-transform", open && "rotate-90")}>
          ▶
        </span>
      </span>
      <CellStrip onus={pon.onus} />
    </button>
  )
}

const WORST_N = 6

function PonDetail({ pon, device, focusOnuId, splitters, warnDbm, critDbm }: {
  pon: Pon; device: OrgDevice; focusOnuId?: number | null
  splitters?: Map<number, string>
  warnDbm?: number | null; critDbm?: number | null
}) {
  const deviceId = device.id
  const [showAll, setShowAll] = useState(false)
  const rosterOnly = pon.onus.every((o) => o.rx_dbm == null)
  const worst = useMemo(() => {
    const rows = rosterOnly
      ? [...pon.onus].sort((a, b) => (a.onu_id ?? 0) - (b.onu_id ?? 0))
      : [...pon.onus]
          .filter((o) => o.state === "online" && o.rx_dbm != null)
          .sort((a, b) => a.rx_dbm! - b.rx_dbm!)
    const focus = focusOnuId != null ? pon.onus.find((o) => o.id === focusOnuId) : undefined
    if (focus && !rows.includes(focus)) rows.unshift(focus)
    return rows
  }, [pon, focusOnuId, rosterOnly])
  useEffect(() => {
    if (focusOnuId != null && worst.findIndex((o) => o.id === focusOnuId) >= WORST_N) {
      setShowAll(true)
    }
  }, [focusOnuId, worst])
  if (!worst.length) {
    return (
      <div className="mb-1 ml-2 rounded-md border bg-card/50 px-3 py-2 text-2xs text-muted-foreground">
        No online ONUs with an Rx reading on PON {pon.port}.
      </div>
    )
  }
  return (
    <div className="mb-1 ml-2 rounded-md border bg-card/50 px-3 py-2">
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {rosterOnly ? "By ONU ID" : "Worst first"} · PON {pon.port} · {pon.onus.length} ONUs
      </div>
      {rosterOnly && (
        <div className="mb-1">
          <p className="text-2xs text-faint-foreground">
            No Rx readings on this PON. Showing state, distance and time dark.
          </p>
          <RxDiagnosis device={device} compact />
        </div>
      )}
      <div className="divide-y divide-border/60">
        {(showAll ? worst : worst.slice(0, WORST_N)).map((o) => (
          <OnuRow key={o.id} o={o} deviceId={deviceId} focused={o.id === focusOnuId}
            noRx={rosterOnly} splitters={splitters} warnDbm={warnDbm} critDbm={critDbm} />
        ))}
      </div>
      {worst.length > WORST_N && (
        <button className="mt-1.5 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show fewer" : `All ${pon.onus.length} ONUs on ${pon.port} →`}
        </button>
      )}
    </div>
  )
}

function FaultCard({ f }: { f: PonFault }) {
  const fiber = f.kind === "fiber"
  const witnessed = f.evidence === "witness" && f.witness_dark > 0
  const range = fiber && f.cut_high_m != null
    ? (f.cut_low_m ? `${fmtKm(f.cut_low_m)} – ${fmtKm(f.cut_high_m)}` : `within ${fmtKm(f.cut_high_m)}`)
    : null
  return (
    <div className={cn(
      "rounded-lg border px-3 py-2 text-xs",
      fiber ? "border-destructive/40 bg-destructive-soft/40" : "border-warning/40 bg-warning-soft/40",
    )}>
      <p className={cn("font-semibold", fiber ? "text-destructive" : "text-warning")}>
        {fiber
          ? (witnessed ? "Fibre cut confirmed" : "Suspected fiber cut")
          : "Power-outage pattern"} · PON {f.pon_port ?? "?"}
      </p>
      <p className="mt-0.5 text-muted-foreground">
        {f.dark} of {f.onus_total} ONUs dark
        {f.dying_gasp > 0 && <> · {f.dying_gasp} sent dying-gasp</>}
        {f.since && <> · since {durationSince(f.since)} ago</>}
      </p>
      {fiber ? (
        <p className="mt-0.5">
          {range
            ? <>Cut likely <span className="font-semibold">{range}</span> from the OLT (by ranging: optical path, not road meters).</>
            : <>No ranging distances on this PON, so we can't bracket the cut.</>}
          {f.suspect && <> Suspect: <span className="font-mono font-semibold">{f.suspect}</span>.</>}
        </p>
      ) : (
        <p className="mt-0.5">
          {f.evidence === "witness"
            ? <>A power-backed reference ONU here is still online, so light is
                reaching the area. The ONUs that dropped almost certainly lost
                mains power. Don't send a splicing crew.</>
            : <>Mostly dying-gasp: customers likely lost mains power. Check the
                area's supply before sending a splicing crew.</>}
        </p>
      )}
      <p className="mt-1 text-2xs text-faint-foreground">
        {f.evidence === "witness" ? (
          f.witness_dark > 0
            ? <>Confirmed by {f.witness_dark} power-backed reference ONU
                {f.witness_dark > 1 ? "s" : ""} going dark. Power can't explain that.</>
            : <>Based on {f.witness_alive} power-backed reference ONU
                {f.witness_alive > 1 ? "s" : ""} still online past the dark ONUs.</>
        ) : f.evidence === "dying_gasp" ? (
          <>Based on the ONUs' own dying-gasp reports.</>
        ) : (
          <>No dying-gasp or LOS reported on this hardware, so this is an
            assumption, not a measurement. Placing a power-backed reference ONU on
            this PON would settle it.</>
        )}
      </p>
    </div>
  )
}

function FaultSection({ faults }: { faults: PonFault[] }) {
  const [open, setOpen] = useState(false)
  if (!faults.length) return null
  if (faults.length === 1) return <FaultCard f={faults[0]} />
  const fiber = faults.filter((f) => f.kind === "fiber").length
  const power = faults.length - fiber
  const parts = [
    fiber > 0 && `${fiber} suspected fiber cut${fiber > 1 ? "s" : ""}`,
    power > 0 && `${power} power pattern${power > 1 ? "s" : ""}`,
  ].filter(Boolean)
  return (
    <div className="flex flex-col gap-2">
      <button onClick={() => setOpen(!open)} aria-expanded={open}
        className={cn("flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left text-xs",
          fiber > 0
            ? "border-destructive/40 bg-destructive-soft/40 hover:bg-destructive-soft/60"
            : "border-warning/40 bg-warning-soft/40 hover:bg-warning-soft/60")}>
        <span className={cn("font-semibold", fiber > 0 ? "text-destructive" : "text-warning")}>
          {faults.length} PON mass-drops
        </span>
        <span className="hidden text-muted-foreground @md:inline">· {parts.join(" · ")}</span>
        <span className={cn("ml-auto shrink-0 text-[0.625rem] text-muted-foreground transition-transform", open && "rotate-90")}>
          ▶
        </span>
      </button>
      {open && faults.map((f) => (
        <FaultCard key={`${f.device_id}:${f.pon_port ?? "?"}`} f={f} />
      ))}
    </div>
  )
}

function DupMacSection({ dupMacs }: { dupMacs: DupMac[] }) {
  const [open, setOpen] = useState(false)
  if (!dupMacs.length) return null
  if (dupMacs.length === 1) return <DupMacCard d={dupMacs[0]} />
  return (
    <div className="flex flex-col gap-2">
      <button onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-lg border border-destructive/40 bg-destructive-soft/40 px-3 py-2 text-left text-xs hover:bg-destructive-soft/60">
        <span className="font-semibold text-destructive">{dupMacs.length} duplicate ONU MACs</span>
        <span className="hidden text-muted-foreground @md:inline">
          · cloned CPE, bridging loop, or stale double-registration
        </span>
        <span className={cn("ml-auto shrink-0 text-[0.625rem] text-muted-foreground transition-transform", open && "rotate-90")}>
          ▶
        </span>
      </button>
      {open && dupMacs.map((d) => <DupMacCard key={d.mac} d={d} />)}
    </div>
  )
}

function DupMacCard({ d }: { d: DupMac }) {
  return (
    <div className="rounded-lg border border-destructive/40 bg-destructive-soft/40 px-3 py-2 text-xs">
      <p className="font-semibold text-destructive">
        Duplicate ONU MAC · <span className="font-mono">{d.mac}</span>
      </p>
      <p className="mt-0.5 text-muted-foreground">
        Registered on {d.members.length} ONU slots, likely a cloned CPE, a
        bridging loop, or a stale double-registration.
      </p>
      <ul className="mt-1 space-y-0.5 font-mono text-2xs">
        {d.members.map((m) => (
          <li key={`${m.device_id}:${m.onu_key}`} className="text-foreground">
            {m.device_name} · PON {m.pon_port ?? "?"} · ONU {m.onu_id ?? "?"}
            {m.state && m.state !== "online" && (
              <span className="text-muted-foreground"> ({m.state})</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function OpticalPanel({ device, focusOnuId, focusOnuMac }: {
  device: OrgDevice
  focusOnuId?: number | null
  focusOnuMac?: string | null
}) {
  const { canWrite, scopeOrg } = useAuth()
  const q = useQuery<OpticsResponse>({
    queryKey: ["optics", device.id],
    queryFn: () => inventoryApi.optics(device.id),
    refetchInterval: 30_000,
  })
  const faultsQ = useQuery({
    queryKey: ["pon-faults", device.id],
    queryFn: () => inventoryApi.ponFaults(device.id),
    refetchInterval: 30_000,
  })
  const pons = useMemo(() => groupByPon(q.data?.onus ?? []), [q.data])
  const invQ = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 30_000,
  })
  const splitterNames = useMemo(
    () => new Map((invQ.data?.devices ?? []).map((d) => [d.id, d.name])),
    [invQ.data])

  const worstPon = useMemo(() => {
    if (!pons.length) return null
    return [...pons].sort((a, b) =>
      b.crit - a.crit || (a.worstRx ?? 0) - (b.worstRx ?? 0))[0].port
  }, [pons])

  const [openPort, setOpenPort] = useState<string | null>(null)
  const focusId = useMemo(() => {
    if (focusOnuId != null) return focusOnuId
    const mac = (focusOnuMac ?? "").trim().toUpperCase()
    if (!mac) return null
    const o = (q.data?.onus ?? []).find(
      (x) => (x.serial ?? "").trim().toUpperCase() === mac)
    return o?.id ?? null
  }, [focusOnuId, focusOnuMac, q.data])
  const focusPort = useMemo(() => {
    if (focusId == null) return null
    const o = (q.data?.onus ?? []).find((x) => x.id === focusId)
    return o ? o.pon_port ?? "—" : null // "—" is groupByPon's null-port bucket
  }, [focusId, q.data])
  useEffect(() => {
    if (focusPort != null) setOpenPort(focusPort)
  }, [focusPort, focusId])
  const activePort = openPort
  const toggle = (port: string) =>
    setOpenPort((prev) => (prev === port ? null : port))

  if (q.isLoading) return <Skeleton className="h-40 w-full" />
  if (q.error) {
    return (
      <p className="rounded-lg border border-destructive/30 bg-destructive-soft/40 px-3 py-2 text-xs text-destructive">
        Couldn't load the optical readings ({q.error instanceof Error ? q.error.message : "request failed"}).
      </p>
    )
  }
  const onus = q.data?.onus ?? []
  if (!onus.length) {
    return <SnmpDiagnosis device={device} subsystem="optics" />
  }

  const isDown = isDownState(device.state)
  const online = isDown ? 0 : onus.filter((o) => o.state === "online").length
  const crit = isDown ? 0 : onus.filter((o) => onuSev(o) === "crit").length
  const warn = isDown ? 0 : onus.filter((o) => onuSev(o) === "warn").length
  const limit = q.data?.onu_pon_limit ?? Infinity
  const dupMacs = q.data?.dup_macs ?? []
  const opticsStale = !isFresh(device.optics_updated_at)

  const noRxAtAll = onus.every((o) => o.rx_dbm == null)

  return (
    <div className="flex flex-col gap-2.5">
      {isDown && (
        <div className="rounded-lg border border-border bg-popover px-3 py-2 text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">OLT offline.</span>{" "}
          All {onus.length} ONUs unreachable. Readings below are the last snapshot
          before it went down.
        </div>
      )}
      {noRxAtAll && !isDown && <RxDiagnosis device={device} />}
      <div className={cn("@container flex flex-col gap-3 rounded-lg border bg-muted/40 p-3",
        isDown && "wisp-frozen")}>
      <FaultSection faults={faultsQ.data?.faults ?? []} />
      <DupMacSection dupMacs={dupMacs} />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="text-sm">
          <span className="font-semibold">{onus.length}</span>
          <span className="text-muted-foreground"> ONUs · {online} online</span>
        </span>
        {crit > 0 && (
          <span className="rounded bg-destructive-soft px-1.5 py-0.5 text-2xs font-semibold text-destructive">
            {crit} below {q.data!.crit_dbm} dBm
          </span>
        )}
        {warn > 0 && (
          <span className="rounded bg-warning-soft px-1.5 py-0.5 text-2xs font-semibold text-warning">
            {warn} warning
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-1 text-2xs text-muted-foreground">
          {onus.some((o) => o.rx_dbm != null) && (
            <span className="flex items-center gap-3 font-mono">
              {worstPon && <span>worst: PON {worstPon}</span>}
              <span>warn {q.data!.warn_dbm} · crit {q.data!.crit_dbm} dBm</span>
            </span>
          )}
          {device.optics_updated_at && (opticsStale
            ? <span className="font-semibold" title="The SNMP optical walk on this OLT has stopped refreshing. These readings are the last good snapshot.">stale · {ago(device.optics_updated_at)}</span>
            : <span className="text-faint-foreground">as of {ago(device.optics_updated_at)}</span>)}
          {!isDown && <RxFreshness device={device} canWrite={canWrite} />}
        </div>
      </div>

      <div className="flex flex-col">
        {pons.map((pon) => (
          <div key={pon.port}>
            <PonRow pon={pon} open={pon.port === activePort} onToggle={() => toggle(pon.port)}
              limit={limit} opticsAt={device.optics_updated_at} />
            {pon.port === activePort && (
              <PonDetail pon={pon} device={device} focusOnuId={focusId}
                splitters={splitterNames}
                warnDbm={q.data?.warn_dbm} critDbm={q.data?.crit_dbm} />
            )}
          </div>
        ))}
      </div>
      </div>
    </div>
  )
}
