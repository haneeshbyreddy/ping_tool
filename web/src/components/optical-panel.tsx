import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { inventoryApi, ApiError } from "@/lib/api"
import type { DupMac, OnuOptic, OpticsResponse, OrgDevice, PonFault } from "@/lib/types"
import {
  ago, durationSince, isDownState, isFresh, onuName, onuSev, type OnuSev,
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

// `onuSev` MOVED to lib/format.ts — the map's subscriber labels grade with it
// too now, and a pure map helper importing this panel to get one rule is how a
// module graph turns into a knot. Re-exported so the two files that already
// imported it from here keep working; the definition is single.
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
// ONU ranging: the DBC OLT reports 0 m for an unranged/dark ONU — that's
// "unknown", not a zero-length drop. Cut-bracket math keeps plain fmtKm.
function fmtOnuKm(m: number | null): string {
  return m ? fmtKm(m) : "—"
}
function ackActive(o: OnuOptic): boolean {
  return !!o.ack_until && new Date(o.ack_until).getTime() > Date.now()
}

// The ONU's own index on its PON, as the OLT reports it (`onu_id` — the `.2` of
// slot key "1.2"). It rides as an S.No column because this list is SORTED BY
// dBm, which throws away the order the OLT lists its ONUs in — and that order is
// the one a tech reads off the box's own web UI, the one the heat-strip above is
// drawn in, and the one the "All N ONUs" roster falls back to. Without it there
// is no way to carry a row from this list back to the OLT.
//
// It also REPLACES the bare "1.2" slot chip that used to sit mid-row: the PON
// half of that key is already printed in the header two lines above, so the chip
// spent four characters to say one new digit and read as a version number.
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

/* The row's cells are components rather than inline spans because the row is
   TWO LAYOUTS, not one: below the panel's `@2xl` the identity and the secondary
   readings drop to a second line, above it they sit inline. Rendering the same
   component in both places is what stops the two layouts drifting into two
   different sets of facts — which is the failure the PON header row already had
   to be rebuilt for (see PonRow's note). Only the className differs. */

// The sticker. A worst-first list is a work order and this is what identifies
// the box a tech drives to, so it must render WHOLE — no truncate, and it is
// never dropped at a narrow width, it moves to line two.
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

// Acknowledging an ONU is not cosmetic: `optics.py` counts only UNACKED crits
// into the OLT's optical alarm, so this is what clears the OLT's red optical
// badge (and, when the notification governor has optical kinds switched on,
// what stops it paging) for a drop somebody has already been told about. It is
// a quiet text button rather than an outline one because it is the rarest thing
// in the row — it only renders for a crit/warn ONU that is actually online.
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
  /** THIS OLT's thresholds, threaded from the optics reply rather than a global
   *  default: two boxes may legitimately grade the same dBm differently, and a
   *  scale drawn against the wrong pair would disagree with the dot beside it. */
  warnDbm?: number | null; critDbm?: number | null
  // whole PON has no per-ONU Rx (DBC/C-Data EPON): the Rx-derived columns are
  // structurally dead here, not merely empty for this row
  noRx?: boolean
  // passive id → name, for the "fed from" column. Threaded from the panel
  // rather than queried per row: a PON is up to 64 rows.
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
  // clicked on the map — bring the row into view so the spoke and the numbers meet
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "nearest" })
  }, [focused])
  return (
    // Sized up from text-xs/py-1.5: this is the row a tech reads a dBm figure off
    // at a pole, on a phone, and the identifier columns were sitting at the 12px
    // floor. The focused row also gets an OUTLINE, not just a wash — arriving
    // here from a map click, "which row did it open?" has to be answerable at a
    // glance rather than by spotting a slightly lighter background.
    <div ref={ref} className={cn("py-2 text-sm",
      focused && "-mx-1.5 rounded-md bg-accent px-1.5 ring-1 ring-border-strong")}>
      {/* EVERY COLUMN WAS FIXED-WIDTH AND ONLY THE NAME COULD GIVE. Six fixed
          cells plus a 10.5rem MAC came to ~600px of a panel that opens at 420
          and is usually dragged to ~630 — so the name column, the one thing on
          `flex-1`, was squeezed to a single character while a 17-char MAC sat
          beside it at full width. The budget is now checked at the breakpoints:
          nothing here is allowed to need more room than the width it renders at.
          Widths key off the PANEL (@container on the panel root), not the
          viewport — this panel is 420px on a 2560px screen, so sm:/md:/lg:
          guards all pass and overflow it. */}
      <div className="flex items-center gap-2.5">
        <span className={cn("size-2.5 shrink-0 rounded-full", DOT[onuSev(o)])} />
        {/* S.No — the OLT's own ONU index, see onuIndex(). It leads the row
            because that is where a number you're matching against another list
            has to be, and it is the one column that stays at every width. */}
        <span className="w-8 shrink-0 text-right font-mono text-2xs tabular-nums text-faint-foreground"
          title={`ONU ${o.onu_id ?? "?"}${o.pon_port ? ` on PON ${o.pon_port}` : ""}`
            + (o.onu_key ? ` · slot ${o.onu_key}` : "")}>
          {onuIndex(o)}
        </span>
        {/* The OPERATOR's name wins over the walked one (`onuName`). A tech
            standing at the drop typed it, and on this fleet the OLT's own name
            column is usually blank — so naming the row off `o.name` alone
            printed "unnamed" for a subscriber somebody had just been to and
            named. The walked name is kept in the tooltip rather than dropped:
            where a box reports one, "what does the OLT call it" is still a real
            question. */}
        {/* …and the name is the way IN to the subscriber. A row in this list is
            a reading; the person behind it — their number, which splitter feeds
            them, whether the drop has ever been located — lived on four other
            screens, and the tech reading a bad dBm here is exactly who needs
            them. `subscriber-detail.tsx` is that one place; this is one of the
            five doors into it. Gated on a serial because identity is the MAC:
            with no sticker there is no key to look anything up by. */}
        {o.serial ? (
          <button type="button"
            className="min-w-0 flex-1 truncate text-left underline-offset-2 hover:underline"
            title={o.label && o.name && o.label !== o.name
              ? `${o.label} · the OLT calls it ${o.name}` : "Open this subscriber"}
            onClick={() => setOpenSub(true)}>
            {onuName(o) || <span className="text-muted-foreground">unnamed</span>}
          </button>
        ) : (
          <span className="min-w-0 flex-1 truncate">
            {onuName(o) || <span className="text-muted-foreground">unnamed</span>}
          </span>
        )}
        {/* Reference point toggle. It sits next to the NAME rather than out in
            the action column because it is a fact about the site, not a fact
            about this reading — and a placed one has to stay legible when the Rx
            and ack columns are empty, which on a no-Rx vendor is always. */}
        <ReferenceOnuButton o={o} deviceId={deviceId} />
        <MacCell o={o} className="hidden w-[8.75rem] text-xs @2xl:block" />
        {/* Which splitter feeds this drop. Genuinely diagnostic beside an Rx
            column: several weak ONUs sharing one box is a feeder problem, and
            the same readings scattered across boxes are separate drops. Wide
            widths only — this is context rather than a reading. An unrecorded
            drop renders as a dash, not as blank: "nobody wrote it down" is a
            fact worth seeing. */}
        <span className="hidden w-24 shrink-0 truncate text-xs text-muted-foreground @3xl:inline"
          title={o.drop_passive_id != null
            ? `Drop from ${splitters?.get(o.drop_passive_id) ?? "a splitter that no longer exists"}`
            : "No serving splitter recorded. Add it on the splitter's panel on the map."}>
          {o.drop_passive_id != null
            ? splitters?.get(o.drop_passive_id) ?? "—"
            : <span className="text-faint-foreground">—</span>}
        </span>
        {/* ONE COLUMN PER FACT — no column stands in for another. The Rx cell
            used to fall back to distance/time-dark so the narrow panel kept a
            useful number, but the dedicated distance column then printed the
            same km twice on a no-Rx ONU. Rx stays on line one at every width:
            it is the value this list is sorted on. */}
        {!noRx && (
          <span className="flex w-36 shrink-0 items-center justify-end gap-2">
            {/* The scale sits BEFORE the figure, so the eye meets the verdict
                and then the number — which is the order a tech reads a power
                meter in. It draws only for an ONLINE ONU: a dark one's stored
                Rx is whatever the last good walk saw, and placing that on a
                live scale would claim it is the reading now. */}
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
      {/* Narrow layout's second line — the same cells, indented under the name.
          Padding matches dot + gap + index + gap so the MAC starts where the
          name does. */}
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

// Two lines, not columns: the header facts on one compact row and the ONU
// heat-strip on its own FULL-WIDTH line beneath. The strip used to sit in a
// flex-1 slot between ~275px of fixed columns; inside the 380px device panel
// that slot collapsed to zero and the strip wrapped one 11px cell per line —
// a PON row as tall as its ONU count with nothing visible in it.
function PonRow({ pon, open, onToggle, limit, opticsAt }: {
  pon: Pon; open: boolean; onToggle: () => void; limit: number
  /** the OPTICS walk's own stamp — never the port walk's, which rides a
   *  different clock and says nothing about the age of a light reading */
  opticsAt?: string | null
}) {

  const hasRx = pon.bestRx != null || pon.worstRx != null
  // A reading's state is a fact about the WALK that produced it. The card as a
  // whole already greys when the OLT is down (.wisp-frozen), so what is left
  // for the figure itself to say is whether the walk behind it is current.
  const rxState = readingState({ value: hasRx ? 1 : null, at: opticsAt })
  // EPON tops out at a 1:64 split — a PON that reached its cap can take no more
  // subscribers (central/onuroster.py pages this too)
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
        {/* best + worst Rx — the PON's span, so a wide gap reads as one bad
            drop and a low pair as the shared plant; a vendor with no Rx
            readings (EPON without an optics profile) says so once instead of
            two dashes */}
        {hasRx ? (
          <span className="ml-auto flex shrink-0 items-baseline gap-3 text-2xs">
            <Reading value={fmtDbm(pon.bestRx)} state={rxState} at={opticsAt}
              className="text-muted-foreground" />
            <Reading value={fmtDbm(pon.worstRx)} state={rxState} at={opticsAt}
              tone={pon.crit > 0 ? "destructive" : pon.warn > 0 ? "warning" : undefined}
              className={cn("font-semibold", pon.crit === 0 && pon.warn === 0 && "text-muted-foreground")} />
          </span>
        ) : (
          /* THE DEAD ZONE. This used to read "no Rx data" — true, but a
             SENTENCE where every sibling row shows a NUMBER, so it neither
             held the column nor looked like the same kind of thing. Most of
             the C-Data/DBC fleet is in exactly this state, so it is the common
             case, not an edge one. */
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
  // A vendor with no per-ONU Rx (the DBC/C-Data EPON fleet) leaves EVERY reading
  // NULL — the worst-Rx filter would render an empty card over a PON full of
  // live ONUs. Fall back to a roster ordered by ONU id (a stable slot order the
  // tech reads down, not shuffled by which ONUs are up).
  const rosterOnly = pon.onus.every((o) => o.rx_dbm == null)
  const worst = useMemo(() => {
    const rows = rosterOnly
      ? [...pon.onus].sort((a, b) => (a.onu_id ?? 0) - (b.onu_id ?? 0))
      : [...pon.onus]
          .filter((o) => o.state === "online" && o.rx_dbm != null)
          .sort((a, b) => a.rx_dbm! - b.rx_dbm!)
    // a focused offline/LOS ONU has no Rx and would vanish — surface it on top
    const focus = focusOnuId != null ? pon.onus.find((o) => o.id === focusOnuId) : undefined
    if (focus && !rows.includes(focus)) rows.unshift(focus)
    return rows
  }, [pon, focusOnuId, rosterOnly])
  // the focused ONU may sit past the worst-N cut; expand rather than hide it
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
      {/* "This OLT doesn't report per-ONU Rx" used to be stated flatly here,
          which was a GUESS presented as a hardware fact: the same blank column
          is produced by a vendor with no recipe, an OLT nobody has stored a
          password for, and a scrape that has been failing for a day. The
          diagnosis says which — and on the fleet that started this, the honest
          answer was "we never asked", not "this vendor has none". */}
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

// PON mass-drop card: dying-gasp majority = the neighborhood lost power (don't
// roll a splicing crew); LOS majority = fiber, with the cut bracketed by EPON
// ranging. The interval wording stays honest — ranging is optical path length,
// slack coils included, so it's a stretch of route, never a point.
function FaultCard({ f }: { f: PonFault }) {
  const fiber = f.kind === "fiber"
  // a reference ONU that went dark is testimony, not inference — "suspected" is
  // the wrong word once power has been ruled out by something that stayed up
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
      {/* Say what the verdict RESTS ON. On the C-Data/DBC fleet no ONU reports a
          dying gasp or LOS, so "fiber" there is this system's assumption until a
          reference ONU turns it into a finding — and the reader is deciding
          whether to wake a crew at 2am. Never render the two alike. */}
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

// A single mass-drop shows its full card; more than one collapse behind a count
// banner — an area power cut darkens every PON on the OLT at once, so a big box
// stacks a dozen fault cards that bury the ONU strips. The banner leads with the
// severity (any fiber verdict makes it red) and the tech expands for the detail.
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

// A single dup-MAC shows its full card; more than one collapse behind a count
// banner (a big C-Data fleet can carry dozens of live clones — they'd bury the
// PON strips otherwise) that the tech expands when they want the slot list.
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

// Redundant-MAC card: one ONU MAC on 2+ slots means a cloned CPE, a bridging
// loop, or a stale double-registration. Detection is org-wide; the panel shows
// the groups that touch this OLT.
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
  /** map spoke click-through: open this ONU's PON group and highlight its row */
  focusOnuId?: number | null
  /** Same, addressed by MAC — what a REFERENCE ONU is keyed on (`onu_places`).
   *  The places API carries the ONU's slot number, not its `onu_optics` row id,
   *  so focusing by id from there would highlight whatever row happened to
   *  share the number. Identity is the MAC everywhere else in this feature; it
   *  is the only key that survives a re-homed drop. */
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
  // Names for the "fed from" column. Reads the device list every page already
  // holds (react-query dedupes it) rather than shipping a second copy of the
  // name in the optics reply, where it could disagree with the tree.
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

  // PONs start collapsed on open — the tech expands the one they want, rather
  // than the worst PON springing open every time. A map spoke click-through
  // still auto-opens its ONU's PON (the focusPort effect below).
  const [openPort, setOpenPort] = useState<string | null>(null)
  // One focus id, resolved from whichever key the caller had. `_norm_mac` is
  // trim + upper-case and deliberately NOT separator-stripping (central/
  // onuroster.py) — two differently-punctuated strings really are two values.
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
    // Not a dead end: the edge diagnoses WHY the ONU walk came back empty
    // (vendor unmatched vs agent silent vs genuinely no ONUs).
    return <SnmpDiagnosis device={device} subsystem="optics" />
  }

  // A down/unreachable OLT has no reachable subscribers: the last SNMP walk still
  // says these ONUs were "online", but none are right now. Read online as 0 and
  // mute the stale Rx alarms — the readings below stay as a labelled last snapshot.
  const isDown = isDownState(device.state)
  const online = isDown ? 0 : onus.filter((o) => o.state === "online").length
  const crit = isDown ? 0 : onus.filter((o) => onuSev(o) === "crit").length
  const warn = isDown ? 0 : onus.filter((o) => onuSev(o) === "warn").length
  const limit = q.data?.onu_pon_limit ?? Infinity
  const dupMacs = q.data?.dup_macs ?? []
  // Freshness of the optics walk — same field/rule the row capability icon and
  // map pin use (olt_optics.updated_at, 900s). Without this the panel gives no
  // way to tell a live OLT from one whose walk quietly stopped, especially on a
  // no-Rx vendor (DBC) where there are no dBm numbers to look stale.
  const opticsStale = !isFresh(device.optics_updated_at)

  // The banner lives OUTSIDE the frozen card on purpose: it's the reason the card
  // is gray, so it has to stay at full strength (and a filter can't be undone on
  // a descendant). Graying the card covers everything the banner is claiming —
  // the ONU state dots, the dBm figures, the per-PON strips and the fault
  // verdicts derived from them — in one place, so a reading added later is
  // frozen by default rather than by remembering to check.
  // Nothing on this OLT carries an Rx figure at all. That is a claim about
  // COVERAGE, not about the plant, so it goes outside the frozen card next to
  // the offline banner rather than being whispered inside a PON drill-down the
  // operator has to open first. It never renders when readings exist.
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
        {/* right side: worst-PON + dBm thresholds (only when at least one ONU
            has an Rx reading — on a no-Rx vendor they'd point at an arbitrary
            PON and quote thresholds nothing is judged against) followed by a
            freshness stamp that ALWAYS shows, so a no-Rx OLT still says whether
            its walk is landing. Stale is a data-freshness note, not an alarm —
            neutral, never amber (mirrors the ports panel + CLAUDE.md rule). */}
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
          {/* The stamp above dates the SNMP roster. On a vendor whose dBm comes
              from its web page instead, that walk can be seconds old while the
              optical figures beside it are from yesterday — so the web read
              gets its own stamp rather than hiding behind the roster's. */}
          {!isDown && <RxFreshness device={device} canWrite={canWrite} />}
        </div>
      </div>

      {/* per-PON strips, each expanding INLINE to its worst-first drill-down --- */}
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
