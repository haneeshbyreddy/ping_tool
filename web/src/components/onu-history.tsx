// The subscriber's own history (Wave 2), first thing in the panel's History.
//
// Question: "did this drop just get better, and was it always like this?"
// Action: close the ticket at the pole, or come back with a splice kit — the
// post-repair read has to land in ONE glance, or it costs a second site visit.
// So the live reading the panel already shows is repeated as a labelled dot at
// the chart's right edge: history behind it, now on the end of it.
//
// ONE PLANE (optical, --chart-1) for every series, per notes/viz-grammar.md —
// the five plane hues measure ΔE 2.8–4.3 apart and fail as a categorical set,
// so this drop and its PON's median separate STRUCTURALLY (solid vs dashed,
// full vs half opacity, direct labels). The only status hues on the plot are
// the warn/crit REGIONS, which are genuine failure claims and take the RxScale
// precedent: bands where the thresholds are, and `ok` gets no band at all.
//
// The second line is the WHOLE PON's median, this drop included, so it is
// labelled "PON median" and never "sibling median" (see onu-history-api.ts).
//
// THE THREE BLANKS THIS CHART MUST NEVER RENDER ALIKE — the honesty rule that
// shapes the whole component:
//   1. no bucket on the grid      the OLT's walk never arrived      (gap)
//   2. bucket, online 0           the ONU was dark, no light to read (dark)
//   3. bucket, online > 0, rx_n 0 it was walked and up, no dBm came  (no reading)
// A line with a hole in it says only "something is missing", so the presence
// rail under the plot answers all three, cell by cell, and the OLT's own outage
// windows are shaded on top with the reason said in words outside the plot.
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import { LegendChip, PAD, TimeChart, useChart } from "@/chart/frame"
import type { TooltipModel, TooltipRow } from "@/chart/frame"
import { BandMark, LineMark } from "@/chart/marks"
import { DAY_MS, HOUR_MS, fmtDay } from "@/chart/scale"
import type { TimePoint } from "@/chart/marks"
import { Skeleton } from "@/components/ui/skeleton"
import type { OnuSev } from "@/lib/format"
import { toUtcDate } from "@/lib/format"
import { onuHistoryApi } from "@/lib/onu-history-api"
import type {
  OnuHistoryBucket, OnuHistoryReply, OnuStateEvent,
} from "@/lib/onu-history-api"
import { cn } from "@/lib/utils"

const OPTICAL = "var(--plane-optical)"
const HEIGHT = 150

// The frame's own plot inset: the presence rail sits UNDER the plot and must
// start exactly where the plot starts.
const PLOT_PAD = PAD

const WINDOWS = [
  { label: "48h", days: 2 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const

// The RxScale bands are 42% / 38% of the tone across a 52x8px track. These
// regions cover ~30x that area, so they are the same claim at a whisper —
// judged by INK, never by copying the number across (the same reasoning the
// located-half green wash needed). One alpha for both themes, exactly as
// .wisp-rxscale__band does.
const CRIT_FILL = 0.13
const WARN_FILL = 0.10
// The OLT's own outage: neutral, because it is not this drop's failure.
const OUTAGE_FILL = 0.12

const fmtDbm = (v: number): string => v.toFixed(1)
// The OLT's own word for it, underscores opened up and nothing else changed:
// `unknown` is a REAL state this fleet reports on purpose (the STGP08X profile
// maps 0 to unknown so 68 never-registered slots cannot read as a mass drop),
// so it may not double as our word for "we have no value here".
const stateWord = (s: string): string => s.replace(/_/g, " ")
// `old: null` is FIRST SEEN, which is a different sentence from "it was
// unknown" and must never be printed as one.
const transition = (e: OnuStateEvent): string =>
  e.old == null
    ? `first seen ${stateWord(e.new)}`
    : `${stateWord(e.old)} to ${stateWord(e.new)}`

function bucketLabel(t: number, tier: "hour" | "day"): string {
  return tier === "hour"
    ? new Date(t).toLocaleString(undefined,
        { day: "numeric", month: "short", hour: "numeric" })
    : fmtDay(t)
}

/* ── the presence rail ─────────────────────────────────────────────────────
   HourStrip/DayStrip's grammar, in this subsystem's vocabulary. Offline takes
   the MUTED step and never destructive: hundreds of drops go dark every
   evening and a wall of red is a wall nobody can act on. What IS worth seeing
   is the difference between dark and unmeasured, so those two get opposite
   channels — a fill for dark, a bordered empty for nothing-arrived. */

type CellKind = "online" | "partial" | "dark" | "gap" | "outage" | "nodbm"

interface Cell {
  t: number
  kind: CellKind
  offPct: number
  title: string
}

function PresenceStrip({ cells, height, className }: {
  cells: Cell[]
  height: string
  className?: string
}) {
  return (
    <div className={cn("flex min-w-0 gap-px", className)} role="img"
      aria-label="when this drop was online and when it was measured">
      {cells.map((c) => (
        <span key={c.t} title={c.title}
          className={cn("relative min-w-0 flex-1 overflow-hidden rounded-[1px]", height,
            c.kind === "gap" && "border border-border/60",
            c.kind === "outage" && "bg-muted-foreground/15",
            c.kind === "dark" && "bg-muted-foreground/45",
            // Two steps of the ONE tone, deliberately far apart (2.5x): at an
            // 8px rail a subtle step is no step, and "up, no dBm" vs "up and
            // measured" is precisely the distinction a hole in the line above
            // cannot make on its own.
            c.kind === "nodbm" && "bg-success/[0.18]",
            (c.kind === "online" || c.kind === "partial") && "bg-success/45")}>
          {c.kind === "partial" && (
            <span aria-hidden className="absolute inset-x-0 top-0 bg-muted-foreground/45"
              style={{ height: `${c.offPct}%` }} />
          )}
        </span>
      ))}
    </div>
  )
}

function StripLegend({ kinds, className }: { kinds: CellKind[]; className?: string }) {
  const chip = (cls: string, label: string) => (
    <span key={label} className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <span aria-hidden className={cn("h-2 w-3 rounded-[1px]", cls)} />
      {label}
    </span>
  )
  const all: Array<[CellKind, string, string]> = [
    ["online", "bg-success/45", "online"],
    ["partial", "bg-success/45", "online"],
    ["nodbm", "bg-success/[0.18]", "up, no dBm"],
    ["dark", "bg-muted-foreground/45", "dark"],
    ["gap", "border border-border/60", "no walk"],
    ["outage", "bg-muted-foreground/15", "OLT down"],
  ]
  const seen = new Set<string>()
  const shown = all.filter(([k, , label]) => {
    if (!kinds.includes(k) || seen.has(label)) return false
    seen.add(label)
    return true
  })
  // A legend with one entry explains nothing (the grammar's own rule: legends
  // at two series or more, none for one). A rail that is solid green needs no
  // key beside it.
  if (shown.length < 2) return null
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1", className)}>
      {shown.map(([, cls, label]) => chip(cls, label))}
    </div>
  )
}

/* ── marks local to this chart ─────────────────────────────────────────────
   Each reads the frame's scales through useChart(), the kit's own extension
   point. Zone and Span are the two obvious candidates for promotion into
   marks.tsx once a second chart wants them (the bandwidth ceiling, the OLT
   crit trend) — noted rather than done, so the kit grows on evidence. */

// A horizontal decision REGION (the RxScale band on a time axis). Clamped to
// the plot: the frame's linearScale .nice()s the domain, so a threshold can sit
// outside the drawn area and a rect that ignored that would bleed into the axis.
function Zone({ from, to, fill, opacity }: {
  from: number
  to: number
  fill: string
  opacity: number
}) {
  const { y, w, h, pad } = useChart()
  const a = y(from)
  const b = y(to)
  const top = Math.max(pad.t, Math.min(a, b))
  const bottom = Math.min(h - pad.b, Math.max(a, b))
  const width = w - pad.r - pad.l
  if (bottom <= top || width <= 0) return null
  return (
    <rect x={pad.l} y={top} width={width} height={bottom - top}
      fill={fill} fillOpacity={opacity} />
  )
}

// The OLT's down windows. Behind everything, neutral, and explained in words
// under the plot — a shaded span with no reason reads as a broken chart.
function OutageSpans({ spans }: { spans: Array<[number, number]> }) {
  const { x, w, h, pad } = useChart()
  return (
    <g>
      {spans.map(([a, b], i) => {
        const x0 = Math.max(pad.l, x(a))
        const x1 = Math.min(w - pad.r, x(b))
        if (x1 <= x0) return null
        return (
          <rect key={i} x={x0} y={pad.t} width={Math.max(1, x1 - x0)}
            height={h - pad.b - pad.t}
            fill="var(--muted-foreground)" fillOpacity={OUTAGE_FILL} />
        )
      })}
    </g>
  )
}

// Transitions, as ticks off the baseline rather than the kit's full-height
// EventRule: an evening flapper puts dozens of these on one plot and the
// annotation must stay subordinate to the line it annotates. Going DARK is the
// heavier tick, so a dense patch still reads as "kept dropping" and not merely
// as "something happened here".
function EventTicks({ events }: { events: OnuStateEvent[] }) {
  const { x, h, pad } = useChart()
  const base = h - pad.b
  return (
    <g stroke="var(--muted-foreground)">
      {events.map((e, i) => {
        const px = x(e.ts * 1000)
        if (px < pad.l || px > x.range()[1]) return null
        const dark = e.new !== "online"
        return (
          <line key={`${e.ts}-${i}`} x1={px} x2={px} y1={base} y2={base - 7}
            strokeWidth={dark ? 1.5 : 1} strokeOpacity={dark ? 0.85 : 0.4} />
        )
      })}
    </g>
  )
}

// The live reading the panel is already showing, placed where "now" is — so
// "how it has been" and "how it is since I spliced it" are one glance apart.
// Deliberately NOT joined to the last bucket: we know those two points and not
// the path between them, and a connecting segment would draw the path.
//
// Toned by the server's own verdict, never a local one: a crit reading is a
// failure claim and takes the status hue, anything else stays on the optical
// plane. No opaque casing behind it — the plot is transparent over whichever
// surface the panel is mounted on (popover on the map, the dialog elsewhere),
// so a disc painted in any one surface token would show as a patch on the other.
function NowDot({ v, at, tone }: { v: number; at: number; tone: string }) {
  const { x, y, w, pad } = useChart()
  const cx = Math.min(w - pad.r, x(at))
  const cy = y(v)
  return (
    <g>
      <circle cx={cx} cy={cy} r={3.5} fill={tone} />
      <text x={cx - 8} y={cy} dy="0.32em" textAnchor="end"
        className="fill-muted-foreground text-2xs">now</text>
    </g>
  )
}

function LegendLine({ label, dash, opacity = 1 }: {
  label: string
  dash?: string
  opacity?: number
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <svg width="16" height="6" aria-hidden>
        <line x1="0" x2="16" y1="3" y2="3" stroke={OPTICAL} strokeWidth="1.5"
          strokeDasharray={dash} strokeOpacity={opacity} />
      </svg>
      {label}
    </span>
  )
}

/* ── the model ─────────────────────────────────────────────────────────────
   Built once per reply. The grid is constructed from tier + since/until and
   the buckets are looked up into it, because absence is the record: a slot with
   no bucket is a walk that never landed, and nothing may interpolate over it. */

interface Slot {
  t: number
  b: OnuHistoryBucket | null
  outage: boolean
}

interface Model {
  slots: Slot[]
  step: number
  domain: [number, number]
  cells: Cell[]
  kinds: CellKind[]
  hasRx: boolean
  ponHasRx: boolean
  darkThroughout: boolean
  // THE OFFSET. TimeChart's y domain is [0, yMax] and Rx is negative, so every
  // value below is plotted as its HEIGHT ABOVE this floor and the axis relabels
  // through yFmt. One subtraction, contained here, beats a second chart frame.
  base: number
  yMax: number
  own: TimePoint[]
  ponLine: TimePoint[]
  band: Array<{ t: number; lo: number | null; hi: number | null }> | null
  critTop: number | null
  warnTop: number | null
  spans: Array<[number, number]>
  eventsBySlot: Map<number, OnuStateEvent[]>
  median: number | null
}

function buildModel(d: OnuHistoryReply, nowRx: number | null): Model | null {
  const sinceMs = toUtcDate(d.since).getTime()
  const untilMs = toUtcDate(d.until).getTime()
  if (!(untilMs > sinceMs)) return null
  const step = d.tier === "hour" ? HOUR_MS : DAY_MS

  const start = Math.floor(sinceMs / step) * step
  const times: number[] = []
  for (let t = start; t < untilMs && times.length < 4000; t += step) times.push(t)
  if (!times.length) return null
  const domain: [number, number] = [start, times[times.length - 1] + step]

  const byT = new Map(d.buckets.map((b) => [b.t * 1000, b]))
  const sibByT = new Map(d.sibling.map((s) => [s.t * 1000, s]))

  // Only the windows that actually reach the drawn grid. A span outside it
  // shades nothing, and the note under the plot must never explain a shading
  // that is not on screen — the same rule the chip budget keeps.
  const spans: Array<[number, number]> = d.outages
    .map((o): [number, number] => [
      toUtcDate(o.start).getTime(),
      o.end ? toUtcDate(o.end).getTime() : untilMs,
    ])
    .filter(([a, b]) => b > domain[0] && a < domain[1])
  const inOutage = (t: number) => spans.some(([a, b]) => t >= a && t < b)

  const slots: Slot[] = times.map((t) => {
    const b = byT.get(t) ?? null
    return { t, b, outage: !b && inOutage(t + step / 2) }
  })

  const cells: Cell[] = slots.map((s) => {
    const label = bucketLabel(s.t, d.tier)
    if (!s.b || s.b.samples <= 0) {
      return s.outage
        ? { t: s.t, kind: "outage" as const, offPct: 0,
            title: `${label}: its OLT was not reachable, so nothing was measured` }
        : { t: s.t, kind: "gap" as const, offPct: 0,
            title: `${label}: no walk arrived` }
    }
    const { samples, online, rx_n } = s.b
    const off = samples - online
    if (online <= 0) {
      return { t: s.t, kind: "dark" as const, offPct: 100,
               title: `${label}: dark, walked ${samples} time${samples === 1 ? "" : "s"}` }
    }
    if (off > 0) {
      return { t: s.t, kind: "partial" as const, offPct: (off / samples) * 100,
               title: `${label}: dark for ${off} of ${samples} walks`
                 + (rx_n > 0 ? "" : ", no reading") }
    }
    return rx_n > 0
      ? { t: s.t, kind: "online" as const, offPct: 0,
          title: `${label}: online, ${rx_n} reading${rx_n === 1 ? "" : "s"}` }
      : { t: s.t, kind: "nodbm" as const, offPct: 0,
          title: `${label}: online, but no receive power came back` }
  })

  const hasRx = slots.some((s) => s.b != null && s.b.rx_n > 0)
  const ponHasRx = d.sibling.some((s) => s.rx_med != null)
  const walked = slots.filter((s) => s.b != null && s.b.samples > 0)
  const darkThroughout = walked.length > 0 && walked.every((s) => s.b!.online <= 0)

  // The Y domain, on the RxScale decision-boundary doctrine: [crit-3, warn+3]
  // is where the reading is DECIDED, so the scale never zooms out past it and a
  // healthy drop pegs near the top instead of stretching the axis flat. It is
  // then extended to cover whatever the data actually did — clamping a bad
  // reading to the floor would draw a flat line where the fault is. Integers,
  // so every axis label is an exact dBm rather than a rounded one. No upper
  // threshold is invented: this product models none, and a scale that disagrees
  // with what pages is worse than one that is merely incomplete.
  let seen = false
  let vMin = 0
  let vMax = 0
  const see = (v: number) => {
    if (!seen) { seen = true; vMin = v; vMax = v; return }
    if (v < vMin) vMin = v
    if (v > vMax) vMax = v
  }
  for (const s of slots) {
    if (!s.b) continue
    if (s.b.rx_avg != null) see(s.b.rx_avg)
    if (s.b.rx_min != null) see(s.b.rx_min)
    if (s.b.rx_max != null) see(s.b.rx_max)
  }
  for (const s of d.sibling) if (s.rx_med != null) see(s.rx_med)
  if (nowRx != null) see(nowRx)

  const warn = d.thresholds?.warn
  const crit = d.thresholds?.crit
  const graded = typeof warn === "number" && typeof crit === "number" && crit < warn
  if (graded) { see(crit - 3); see(warn + 3) }
  if (!seen) { see(-30); see(-20) }

  // One dB of floor under the worst point, so a bottomed-out reading is still
  // drawn as a reading rather than flattened onto the axis.
  const base = Math.floor(vMin) - 1
  const yMax = Math.max(1, Math.ceil(vMax) - base)

  const mid = (t: number) => t + step / 2
  const own: TimePoint[] = slots.map((s) => ({
    t: mid(s.t),
    v: s.b?.rx_avg != null ? s.b.rx_avg - base : null,
  }))
  const ponLine: TimePoint[] = times.map((t) => {
    const v = sibByT.get(t)?.rx_med
    return { t: mid(t), v: v != null ? v - base : null }
  })

  // The spread only earns its ink where there IS a spread: on a bucket holding
  // one sample min == max and the band is a hairline nobody can read.
  const spread = slots.some((s) =>
    s.b?.rx_min != null && s.b.rx_max != null && s.b.rx_max - s.b.rx_min >= 0.2)
  const band = spread
    ? slots.map((s) => ({
        t: mid(s.t),
        lo: s.b?.rx_min != null ? s.b.rx_min - base : null,
        hi: s.b?.rx_max != null ? s.b.rx_max - base : null,
      }))
    : null

  const eventsBySlot = new Map<number, OnuStateEvent[]>()
  for (const e of d.events) {
    const t = Math.floor((e.ts * 1000) / step) * step
    const list = eventsBySlot.get(t)
    if (list) list.push(e)
    else eventsBySlot.set(t, [e])
  }

  // The window's MEDIAN bucket average, which is what "how it has been" means
  // here. Deliberately not the last bucket: the newest one is still open, and
  // after a splice it already holds the new value, so a last-bucket comparison
  // would report the repair as no change at all — the one reading this panel
  // exists to make. A median is also robust to the splice's own hour sitting
  // inside the window.
  const avgs: number[] = []
  for (const s of slots) if (s.b?.rx_avg != null) avgs.push(s.b.rx_avg)
  avgs.sort((a, b) => a - b)
  const median = avgs.length
    ? (avgs.length % 2
        ? avgs[(avgs.length - 1) / 2]
        : (avgs[avgs.length / 2 - 1] + avgs[avgs.length / 2]) / 2)
    : null

  const kinds = Array.from(new Set(cells.map((c) => c.kind)))

  return {
    slots, step, domain, cells, kinds, hasRx, ponHasRx, darkThroughout,
    base, yMax, own, ponLine, band,
    critTop: graded ? crit - base : null,
    warnTop: graded ? warn - base : null,
    spans, eventsBySlot, median,
  }
}

/* ── the section ───────────────────────────────────────────────────────────── */

export function OnuHistorySection({ deviceId, onuKey, nowRx, nowSev }: {
  deviceId: number
  onuKey: string
  // The live reading EXACTLY as the panel decided to print it: null whenever
  // the panel would not stand behind a current number (frozen behind a down
  // OLT, dark, a stale optics walk, or no per-ONU Rx at all). One place owns
  // that grammar, and a stale value must never be redrawn here as "now".
  nowRx: number | null
  nowSev: OnuSev
}) {
  const [open, setOpen] = useState(true)
  // 48h by default, for three reasons that agree: it is the window the named
  // job asks for (did my splice hold), it is the only tier with real resolution
  // (hourly buckets, against 7 points for a week at day grain), and on a young
  // historian it is the one window that is fully covered rather than mostly
  // gap. Everything wider is one click away.
  const [days, setDays] = useState(2)
  const q = useQuery({
    queryKey: ["onu-history", deviceId, onuKey, days],
    queryFn: () => onuHistoryApi.get(deviceId, onuKey, days),
    enabled: open,
    staleTime: 300_000,
    // Switching window keeps the old plot on screen, dimmed, rather than
    // blanking to a skeleton on every click — a chart that vanishes when you
    // touch its own control reads as broken.
    placeholderData: (prev) => prev,
  })
  const data = q.data ?? null
  const model = useMemo(() => (data ? buildModel(data, nowRx) : null), [data, nowRx])

  const nowTone = nowSev === "crit" ? "var(--destructive)"
    : nowSev === "warn" ? "var(--warning)" : OPTICAL

  const delta = nowRx != null && model?.median != null ? nowRx - model.median : null

  const recMs = data?.recording_since ? toUtcDate(data.recording_since).getTime() : null
  const untilMs = data ? toUtcDate(data.until).getTime() : null

  // Every label describing the window is derived from the reply that is DRAWN,
  // never from the chip that is selected: while a wider window loads the two
  // disagree, and the words under a plot have to describe that plot. The same
  // reason they are said in HOURS when the answer is short: the server clamps
  // `since` to recording_since, so "48 hours ago" over six hours of strip is
  // the young-historian case mislabelled at exactly the moment it matters.
  const spanMs = model != null && untilMs != null
    ? untilMs - model.domain[0] : days * DAY_MS
  const spanHours = Math.max(1, Math.round(spanMs / HOUR_MS))
  const spanDays = Math.max(1, Math.round(spanMs / DAY_MS))
  const windowLabel = spanHours <= 48 ? `${spanHours}h` : `${spanDays}d`
  const agoLabel = spanHours <= 48
    ? `${spanHours} hours ago` : `${spanDays} days ago`
  // The server CLAMPS `since` to recording_since, so a young historian answers
  // with a short honest window rather than months of empty axis. `>=` therefore,
  // not `>`: the clamp routinely lands the two on the same instant, and losing
  // the note in exactly the case it exists for would be the whole bug.
  const young = recMs != null && model != null && recMs >= model.domain[0]
  const collected = recMs != null && untilMs != null
    ? Math.max(0, Math.floor((untilMs - recMs) / DAY_MS)) : null

  const tip = useMemo(() => {
    if (!model || !data) return undefined
    return (tMs: number): TooltipModel | null => {
      const i = Math.floor((tMs - model.domain[0]) / model.step)
      const slot = model.slots[i]
      if (!slot) return null
      const at = slot.t + model.step / 2
      const title = bucketLabel(slot.t, data.tier)
      const rows: TooltipRow[] = []
      if (!slot.b || slot.b.samples <= 0) {
        rows.push({ label: "walks",
                    value: slot.outage ? "none, OLT not reachable" : "none arrived" })
      } else {
        const b = slot.b
        if (b.rx_avg != null) {
          rows.push({ label: "Rx", value: `${fmtDbm(b.rx_avg)} dBm`, color: OPTICAL })
          if (b.rx_min != null && b.rx_max != null && b.rx_max - b.rx_min >= 0.2) {
            rows.push({ label: "min to max",
                        value: `${fmtDbm(b.rx_min)} to ${fmtDbm(b.rx_max)}` })
          }
        } else {
          rows.push({ label: "Rx", value: b.online > 0 ? "not measured" : "dark" })
        }
        const sib = data.sibling.find((s) => s.t * 1000 === slot.t)
        if (sib?.rx_med != null) {
          rows.push({ label: "PON median", value: `${fmtDbm(sib.rx_med)} dBm` })
        }
        rows.push({ label: "online", value: `${b.online} of ${b.samples} walks` })
      }
      const evs = model.eventsBySlot.get(slot.t) ?? []
      for (const e of evs.slice(0, 3)) {
        rows.push({ label: new Date(e.ts * 1000).toLocaleTimeString(undefined,
                      { hour: "numeric", minute: "2-digit" }),
                    value: transition(e) })
      }
      if (evs.length > 3) {
        rows.push({ label: "and", value: `${evs.length - 3} more changes` })
      }
      return { at, title, rows }
    }
  }, [model, data])

  const header = (
    <div className="flex items-center gap-2">
      <button type="button" onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex min-w-0 items-center gap-1 text-left"
        title="This drop's receive power and when it was online">
        <ChevronRight className={cn("size-3 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <span className="wisp-eyebrow">History</span>
      </button>
      {open ? (
        <div className="ml-auto flex shrink-0 items-center gap-0.5 rounded-md border p-0.5">
          {WINDOWS.map((w) => (
            <button key={w.days} type="button" onClick={() => setDays(w.days)}
              className={cn("rounded px-1.5 py-0.5 text-2xs tabular-nums transition-colors",
                w.days === days
                  ? "bg-accent font-medium text-foreground"
                  : "text-muted-foreground hover:text-foreground")}>
              {w.label}
            </button>
          ))}
        </div>
      ) : (
        <span className="ml-auto shrink-0 text-2xs text-faint-foreground">
          last {windowLabel}
        </span>
      )}
    </div>
  )

  return (
    <div className="border-b px-4 py-3 last:border-b-0">
      {header}
      {open && (
        <div className={cn("mt-2 flex flex-col gap-2 transition-opacity",
          q.isFetching && !!data && "opacity-60")}>
          {q.isLoading ? (
            <>
              <Skeleton className="h-4 w-28" />
              <Skeleton className="w-full" style={{ height: HEIGHT }} />
            </>
          ) : q.error || !data ? (
            <p className="text-2xs text-muted-foreground">
              Couldn't load this drop's history.
            </p>
          ) : !model || (!model.slots.length) ? (
            <YoungState recMs={recMs} collected={collected}
              onNarrow={days > 2 ? () => setDays(2) : undefined} />
          ) : !model.hasRx && !data.events.length
              && model.cells.every((c) => c.kind === "gap") ? (
            <YoungState recMs={recMs} collected={collected}
              onNarrow={days > 2 ? () => setDays(2) : undefined} />
          ) : model.hasRx ? (
            <>
              {nowRx != null && (
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-2xs text-faint-foreground">now</span>
                  <span className={cn("font-mono text-sm font-semibold tabular-nums",
                    nowSev === "crit" ? "text-destructive"
                      : nowSev === "warn" ? "text-warning" : "")}>
                    {/* Two decimals, matching the panel's own Signal row to the
                        digit: this repeats that number, and one place printing
                        -21.43 beside another printing -21.4 reads as two
                        readings. Inside the plot 1 dp is right (those are
                        averages, and an axis is furniture). */}
                    {nowRx.toFixed(2)} dBm
                  </span>
                  {delta != null && Math.abs(delta) >= 0.3 && (
                    <span className="text-2xs text-muted-foreground">
                      {Math.abs(delta).toFixed(1)} dB{" "}
                      {delta > 0 ? "stronger" : "weaker"} than its {windowLabel} median
                    </span>
                  )}
                </div>
              )}
              <TimeChart domain={model.domain} yMax={model.yMax} height={HEIGHT}
                yFmt={(v) => String(Math.round(v + model.base))} tooltip={tip}
                legend={<>
                  <LegendLine label="this drop" />
                  {model.ponHasRx && (
                    <LegendLine
                      label={data.onu.pon_port ? `PON ${data.onu.pon_port} median` : "PON median"}
                      dash="4 3" opacity={0.5} />
                  )}
                  {model.band && (
                    <LegendChip color={`color-mix(in srgb, ${OPTICAL} 30%, transparent)`}
                      label="min to max" />
                  )}
                </>}>
                <OutageSpans spans={model.spans} />
                {model.critTop != null && (
                  <Zone from={0} to={model.critTop} fill="var(--destructive)"
                    opacity={CRIT_FILL} />
                )}
                {model.critTop != null && model.warnTop != null && (
                  <Zone from={model.critTop} to={model.warnTop} fill="var(--warning)"
                    opacity={WARN_FILL} />
                )}
                {model.band && <BandMark points={model.band} color={OPTICAL} opacity={0.16} />}
                {model.ponHasRx && (
                  <LineMark points={model.ponLine} color={OPTICAL} width={1.25}
                    dash="4 3" opacity={0.5} />
                )}
                <LineMark points={model.own} color={OPTICAL} width={1.75} />
                <EventTicks events={data.events} />
                {nowRx != null && untilMs != null && (
                  <NowDot v={nowRx - model.base} at={untilMs} tone={nowTone} />
                )}
              </TimeChart>
              <PresenceRail cells={model.cells} />
              <StripLegend kinds={model.kinds} />
              <Notes data={data} model={model} recMs={recMs} collected={collected}
                young={young} />
            </>
          ) : (
            <>
              {/* No dBm anywhere in the window. Presence is the whole record for
                  most of this fleet, so it is the designed hero here rather than
                  an apology for a missing chart: no fabricated dBm axis, and the
                  y in each cell is how much of that bucket the drop was up for. */}
              <PresenceStrip cells={model.cells} height="h-6" />
              <div className="flex justify-between text-2xs text-faint-foreground">
                <span>{agoLabel}</span><span>now</span>
              </div>
              <StripLegend kinds={model.kinds} />
              <RecentEvents events={data.events} />
              <Notes data={data} model={model} recMs={recMs} collected={collected}
                young={young} />
            </>
          )}
        </div>
      )}
    </div>
  )
}

// The rail under the plot. Its left gutter is the frame's y-label column, so
// the label sits where an axis label sits and the cells line up with the plot
// they explain, bucket for bucket.
function PresenceRail({ cells }: { cells: Cell[] }) {
  return (
    <div className="flex items-center">
      <span className="shrink-0 pr-1.5 text-right text-2xs text-faint-foreground"
        style={{ width: PLOT_PAD.l }}>online</span>
      <PresenceStrip cells={cells} height="h-2" className="flex-1" />
      <span aria-hidden className="shrink-0" style={{ width: PLOT_PAD.r }} />
    </div>
  )
}

// The last few transitions in words. A strip says THAT it dropped; a tech on
// the phone to the customer is asked WHEN, and the OLT's own word for it
// (dying gasp, LOS) is the difference between a power cut and a cut fibre.
function RecentEvents({ events }: { events: OnuStateEvent[] }) {
  if (!events.length) return null
  const recent = events.slice(-4).reverse()
  return (
    <div className="flex flex-col gap-0.5 border-t pt-1.5">
      {recent.map((e, i) => (
        <div key={`${e.ts}-${i}`} className="flex items-baseline gap-2 text-2xs">
          <span className="shrink-0 tabular-nums text-muted-foreground">
            {new Date(e.ts * 1000).toLocaleString(undefined,
              { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}
          </span>
          <span className={cn("shrink-0",
            e.new === "online" ? "text-muted-foreground" : "text-foreground")}>
            {transition(e)}
          </span>
        </div>
      ))}
      {events.length > recent.length && (
        <p className="text-2xs text-faint-foreground">
          {events.length} state changes in this window
        </p>
      )}
    </div>
  )
}

// Everything the plot cannot say inside itself. The frozen rule's other half:
// a shaded span always carries a live reason, said OUTSIDE the plot.
function Notes({ data, model, recMs, collected, young }: {
  data: OnuHistoryReply
  model: Model
  recMs: number | null
  collected: number | null
  young: boolean
}) {
  const graded = model.critTop != null && model.warnTop != null
  const lines: string[] = []

  if (!model.hasRx) {
    if (model.darkThroughout) {
      lines.push("It was dark for the whole window, so there was no light to measure.")
    } else if (model.ponHasRx) {
      lines.push("Other drops on its PON were measured in this window. This one was not.")
    } else {
      lines.push("No receive power was recorded for this drop or its PON in this "
        + "window. Its OLT's Optical tab says what that box can read.")
    }
  }
  // "not reachable", not "down": these windows include UNREACHABLE spans, where
  // the OLT's own parent was down and the box itself was never proven dead.
  // Either way no walk landed and either way it is not the subscriber's fault,
  // which is the whole point of saying it. The legend chip keeps the house's
  // short word ("OLT down", the union isDownState already covers) because a
  // chip has no room for the distinction and this line does.
  if (model.spans.length) {
    lines.push((model.hasRx
      ? "Shaded: its OLT was not reachable, so nothing was measured then. "
      : "Its OLT was not reachable for part of this window, so nothing was "
        + "measured then. ")
      + "That is the OLT's outage, not this drop's.")
  }
  if (young && recMs != null) {
    lines.push(`Recording since ${fmtDay(recMs)}`
      + (collected != null ? ` · ${collected} day${collected === 1 ? "" : "s"} collected` : "")
      + ". Anything before that was never recorded.")
  }

  // The thresholds are named only where they are DRAWN. On a window with no
  // dBm in it they grade nothing, and printing them there would imply a
  // measurement was taken against them.
  const showThresholds = graded && model.hasRx
  if (!lines.length && !showThresholds) return null
  return (
    <div className="flex flex-col gap-0.5">
      {showThresholds && (
        <p className="font-mono text-2xs text-faint-foreground">
          warn {data.thresholds.warn} · crit {data.thresholds.crit} dBm
        </p>
      )}
      {lines.map((l) => (
        <p key={l} className="text-2xs text-muted-foreground">{l}</p>
      ))}
    </div>
  )
}

// Day one is a designed state, not a blank. A historian that started on Tuesday
// has nothing to say about Monday and should say exactly that.
//
// The way out matters as much as the sentence: the day tier only exists after
// the nightly fold, so on a fleet that started recording this morning every
// window WIDER than 48h legitimately answers with nothing while the 48h view is
// full. An empty panel with no way forward would read as a broken feature, so
// it offers the window that works.
function YoungState({ recMs, collected, onNarrow }: {
  recMs: number | null
  collected: number | null
  onNarrow?: () => void
}) {
  return (
    <div className="rounded-md border border-dashed border-border px-3 py-3 text-2xs text-muted-foreground">
      {recMs != null ? (
        <>
          <p>
            Recording since {fmtDay(recMs)}
            {collected != null && ` · ${collected} day${collected === 1 ? "" : "s"} collected`}
          </p>
          <p className="mt-0.5 text-faint-foreground">
            Nothing has been recorded for this drop yet. It fills in as its OLT
            is walked.
          </p>
        </>
      ) : (
        <p>History isn't being recorded yet.</p>
      )}
      {onNarrow && (
        <button type="button" onClick={onNarrow}
          className="mt-1.5 underline-offset-2 hover:text-foreground hover:underline">
          Try the last 48 hours
        </button>
      )}
    </div>
  )
}
