import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { inventoryApi, ApiError } from "@/lib/api"
import type { OnuOptic, OpticsResponse, OrgDevice } from "@/lib/types"
import { SnmpDiagnosis } from "@/components/snmp-diagnosis"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type Sev = "ok" | "warn" | "crit" | "offline"

function onuSev(o: OnuOptic): Sev {
  if (o.state !== "online") return "offline"
  if (o.severity === "crit") return "crit"
  if (o.severity === "warn") return "warn"
  return "ok"
}

const CELL: Record<Sev, string> = {
  ok: "bg-success/70",
  warn: "bg-warning",
  crit: "bg-destructive",
  offline: "bg-muted-foreground/40",
}
const DOT: Record<Sev, string> = {
  ok: "bg-success", warn: "bg-warning", crit: "bg-destructive", offline: "bg-muted-foreground/40",
}

function fmtDbm(v: number | null): string {
  return v == null ? "—" : v.toFixed(1)
}
function fmtKm(m: number | null): string {
  return m == null ? "—" : `${(m / 1000).toFixed(2)} km`
}
function ackActive(o: OnuOptic): boolean {
  return !!o.ack_until && new Date(o.ack_until).getTime() > Date.now()
}

function Drift({ o }: { o: OnuOptic }) {
  if (o.rx_dbm == null || o.rx_ref_dbm == null) return <span className="text-faint-foreground">—</span>
  const delta = o.rx_dbm - o.rx_ref_dbm
  if (Math.abs(delta) < 0.2) return <span className="text-muted-foreground">± 0 dB</span>
  const worse = delta < 0
  return (
    <span className={cn("tabular-nums", worse ? "text-warning" : "text-success")}>
      {worse ? "▼" : "▲"} {Math.abs(delta).toFixed(1)} dB
    </span>
  )
}

interface Pon {
  port: string
  onus: OnuOptic[]
  online: number
  worstRx: number | null
  typicalRx: number | null
  crit: number
  warn: number
}

function median(xs: number[]): number | null {
  if (!xs.length) return null
  const s = [...xs].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
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
      typicalRx: median(rx),
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
          title={`${o.name || o.serial || `ONU ${o.onu_id ?? ""}`} · ${fmtDbm(o.rx_dbm)} dBm · ${o.state ?? "?"}`}
          className={cn("size-[11px] rounded-[2px]", CELL[onuSev(o)])}
        />
      ))}
    </div>
  )
}

function OnuRow({ o, deviceId, focused }: { o: OnuOptic; deviceId: number; focused?: boolean }) {
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
  // clicked on the map — bring the row into view so the spoke and the numbers meet
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest" })
  }, [focused])
  return (
    <div ref={ref} className={cn("flex items-center gap-3 py-1.5 text-xs",
      focused && "-mx-1.5 rounded-md bg-accent/60 px-1.5")}>
      <span className={cn("size-2 shrink-0 rounded-full", DOT[onuSev(o)])} />
      <span className="min-w-0 flex-1 truncate">
        {o.name || <span className="text-muted-foreground">unnamed</span>}
      </span>
      {/* extra columns key off the PANEL's width (@container on the panel
          root), not the viewport — the 380px map panel renders on a wide
          desktop screen, so sm:/md:/lg: guards all pass and overflow it */}
      <span className="hidden w-32 shrink-0 truncate font-mono text-2xs text-muted-foreground @xl:inline">
        {o.serial || o.onu_key}
      </span>
      <span className={cn("w-20 shrink-0 text-right font-mono font-semibold tabular-nums",
        onuSev(o) === "crit" ? "text-destructive" : onuSev(o) === "warn" ? "text-warning" : "")}>
        {fmtDbm(o.rx_dbm)} dBm
      </span>
      <span className="hidden w-20 shrink-0 text-right text-2xs @md:inline"><Drift o={o} /></span>
      <span className="hidden w-16 shrink-0 text-right font-mono text-2xs text-muted-foreground @2xl:inline">
        {fmtKm(o.distance_m)}
      </span>
      <span className="w-14 shrink-0 text-right">
        {onuSev(o) === "ok" || o.state !== "online" ? null : acked ? (
          <button className="text-2xs text-muted-foreground hover:text-foreground"
            onClick={() => ack.mutate()} disabled={ack.isPending}>acked</button>
        ) : (
          <Button variant="outline" size="sm" className="h-6 px-2 text-2xs"
            onClick={() => ack.mutate()} disabled={ack.isPending}>Ack</Button>
        )}
      </span>
    </div>
  )
}

// Two lines, not columns: the header facts on one compact row and the ONU
// heat-strip on its own FULL-WIDTH line beneath. The strip used to sit in a
// flex-1 slot between ~275px of fixed columns; inside the 380px device panel
// that slot collapsed to zero and the strip wrapped one 11px cell per line —
// a PON row as tall as its ONU count with nothing visible in it.
function PonRow({ pon, open, onToggle }: {
  pon: Pon; open: boolean; onToggle: () => void
}) {

  const worstTone = pon.crit > 0 ? "text-destructive" : pon.warn > 0 ? "text-warning" : "text-muted-foreground"
  const hasRx = pon.typicalRx != null || pon.worstRx != null
  return (
    <button onClick={onToggle} aria-expanded={open}
      className={cn("flex w-full flex-col gap-1.5 rounded-md px-2 py-2 text-left hover:bg-foreground/5",
        open && "bg-accent/50")}>
      <span className="flex w-full items-center gap-3">
        <span className="shrink-0 font-mono text-xs font-semibold">PON {pon.port}</span>
        <span className="shrink-0 font-mono text-2xs text-muted-foreground">
          {pon.online}/{pon.onus.length}
        </span>
        {/* typical (median) + worst Rx; a vendor with no Rx readings (EPON
            without an optics profile) says so once instead of two dashes */}
        {hasRx ? (
          <span className="ml-auto flex shrink-0 items-baseline gap-3 font-mono text-2xs tabular-nums">
            <span className="text-muted-foreground">{fmtDbm(pon.typicalRx)}</span>
            <span className={cn("font-semibold", worstTone)}>{fmtDbm(pon.worstRx)}</span>
          </span>
        ) : (
          <span className="ml-auto shrink-0 text-2xs text-faint-foreground">no Rx data</span>
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

function PonDetail({ pon, deviceId, focusOnuId }: {
  pon: Pon; deviceId: number; focusOnuId?: number | null
}) {
  const [showAll, setShowAll] = useState(false)
  const worst = useMemo(() => {
    const rows = [...pon.onus]
      .filter((o) => o.state === "online" && o.rx_dbm != null)
      .sort((a, b) => a.rx_dbm! - b.rx_dbm!)
    // a focused offline/LOS ONU has no Rx and would vanish — surface it on top
    const focus = focusOnuId != null ? pon.onus.find((o) => o.id === focusOnuId) : undefined
    if (focus && !rows.includes(focus)) rows.unshift(focus)
    return rows
  }, [pon, focusOnuId])
  // the focused ONU may sit past the worst-N cut; expand rather than hide it
  useEffect(() => {
    if (focusOnuId != null && worst.findIndex((o) => o.id === focusOnuId) >= WORST_N) {
      setShowAll(true)
    }
  }, [focusOnuId, worst])
  if (!worst.length) {
    return (
      <div className="mb-1 ml-2 rounded-md border bg-card/50 px-3 py-2 text-2xs text-muted-foreground">
        No online ONUs with an Rx reading on PON {pon.port} yet.
      </div>
    )
  }
  return (
    <div className="mb-1 ml-2 rounded-md border bg-card/50 px-3 py-2">
      <div className="mb-1 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
        Worst first · PON {pon.port} · {pon.onus.length} ONUs
      </div>
      <div className="divide-y divide-border/60">
        {(showAll ? worst : worst.slice(0, WORST_N)).map((o) => (
          <OnuRow key={o.id} o={o} deviceId={deviceId} focused={o.id === focusOnuId} />
        ))}
      </div>
      {worst.length > WORST_N && (
        <button className="mt-1 text-2xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show fewer" : `All ${pon.onus.length} ONUs on ${pon.port} →`}
        </button>
      )}
    </div>
  )
}

export function OpticalPanel({ device, focusOnuId }: {
  device: OrgDevice
  /** map spoke click-through: open this ONU's PON group and highlight its row */
  focusOnuId?: number | null
}) {
  const q = useQuery<OpticsResponse>({
    queryKey: ["optics", device.id],
    queryFn: () => inventoryApi.optics(device.id),
    refetchInterval: 30_000,
  })
  const pons = useMemo(() => groupByPon(q.data?.onus ?? []), [q.data])

  const worstPon = useMemo(() => {
    if (!pons.length) return null
    return [...pons].sort((a, b) =>
      b.crit - a.crit || (a.worstRx ?? 0) - (b.worstRx ?? 0))[0].port
  }, [pons])

  const [openPort, setOpenPort] = useState<string | null | undefined>(undefined)
  const focusPort = useMemo(() => {
    if (focusOnuId == null) return null
    const o = (q.data?.onus ?? []).find((x) => x.id === focusOnuId)
    return o ? o.pon_port ?? "—" : null // "—" is groupByPon's null-port bucket
  }, [focusOnuId, q.data])
  useEffect(() => {
    if (focusPort != null) setOpenPort(focusPort)
  }, [focusPort, focusOnuId])
  const activePort = openPort === undefined ? worstPon : openPort
  const toggle = (port: string) =>
    setOpenPort((prev) => ((prev === undefined ? worstPon : prev) === port ? null : port))

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
    // Not a dead end: the edge diagnoses WHY the ONU walk came back empty
    // (vendor unmatched vs agent silent vs genuinely no ONUs).
    return <SnmpDiagnosis device={device} subsystem="optics" />
  }

  const online = onus.filter((o) => o.state === "online").length
  const crit = onus.filter((o) => onuSev(o) === "crit").length
  const warn = onus.filter((o) => onuSev(o) === "warn").length

  return (
    <div className="@container flex flex-col gap-3 rounded-lg border bg-muted/40 p-3">
      {/* header readout ------------------------------------------------------- */}
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
        <span className="ml-auto flex items-center gap-3 font-mono text-2xs text-muted-foreground">
          {worstPon && <span>worst: PON {worstPon}</span>}
          <span>warn {q.data!.warn_dbm} · crit {q.data!.crit_dbm} dBm</span>
        </span>
      </div>

      {/* per-PON strips, each expanding INLINE to its worst-first drill-down --- */}
      <div className="flex flex-col">
        {pons.map((pon) => (
          <div key={pon.port}>
            <PonRow pon={pon} open={pon.port === activePort} onToggle={() => toggle(pon.port)} />
            {pon.port === activePort && (
              <PonDetail pon={pon} deviceId={device.id} focusOnuId={focusOnuId} />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
