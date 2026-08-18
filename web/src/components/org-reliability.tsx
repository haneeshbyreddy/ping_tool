// The org's reliability story (Wave 1, chart A-org). Two questions, side by
// side, because on real fleets they answer each other:
//   "how often did things break?"  -> outages opened per week
//   "how much did it actually cost us?" -> hours down per week, split at 30 min
// Actions: aim the effort, buy the UPS, send the truck — the owner's own KPI.
//
// It used to plot three duration percentiles as three lines in ONE hue,
// separated by opacity and a dash. That failed three ways and all three were
// measured, not guessed (2026-08-17): opacity reads as "faded", not as
// "different series"; one outlier week owned the axis and flattened the rest;
// and the time-to-acknowledge line drew off 17 acknowledgements in 3,776
// outages, which is a line pretending to be evidence. The median was the
// deepest problem — 92% of one org's outages clear inside five minutes, so
// "median 160 s" describes the flaps and buries the handful of faults that
// needed a van. Downtime weights those correctly by construction.
//
// Owner-only: org-wide numbers leak past a worker's assignment scope, and the
// endpoint refuses workers anyway. ONE measurement plane (fleet, --chart-5);
// the two duration bands separate by a SEQUENTIAL step of that one hue, which
// is legal grammar here where three categorical lines were not — duration is
// an ordered quantity, so an ordered encoding is the honest one.
import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { LegendChip, TimeChart } from "@/chart/frame"
import type { TooltipModel } from "@/chart/frame"
import { ColumnMark } from "@/chart/marks"
import { fmtDurS, WEEK_MS } from "@/chart/scale"
import { useAuth } from "@/hooks/use-auth"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"
import type { WeekStat } from "@/lib/types"

const FLEET = "var(--chart-5)"
// Monday 1970-01-05 00:00 UTC — the server's own week anchor.
const MONDAY_MS = 4 * 86_400_000
const DAYS = 182
// The split itself is the SERVER's (analytics.LONG_OUTAGE_S) and is never
// re-derived here — the SPA only names it. One label, read by the legend, the
// tooltip and the summary line, so the three can't drift apart on screen.
const LONG_LABEL = "30 min or longer"
const SHORT_LABEL = "under 30 min"
// The long band is the one you act on, so it carries the ink; the short band
// recedes without leaving (a flap is still downtime). Legend dots mix toward
// transparent to land where the column's fill-opacity lands.
//
// The gap between them is deliberately MODEST. Alpha over the ground moves in
// OPPOSITE directions per mode — the basemap-ladder trap — so the 0.3 that
// read as a pale block on white read as a HOLE on near-black, i.e. as no data
// at all, which is the one thing this product may not render. These two are
// peers in a stack (both are downtime) and only need to be told apart, not
// ranked by weight; verified in a real browser in both modes.
const LONG_OP = 0.95
const SHORT_OP = 0.55
const tone = (op: number) =>
  `color-mix(in oklch, ${FLEET} ${Math.round(op * 100)}%, transparent)`

// Downtime here is a SUM ACROSS DEVICES, so it stays in hours and never rolls
// over into fmtDurS's days: "10d" under a seven-week fleet total reads as ten
// calendar days of blackout, which is not the claim being made. One formatter
// for the axis, the tooltip and the summary — a panel that prints "60h" on
// the axis and "3d" in the tooltip makes the reader do conversion to check
// whether they agree.
function fmtDown(s: number): string {
  if (s <= 0) return "0"
  if (s < 3600) return fmtDurS(s)
  return `${Math.round(s / 3600).toLocaleString()}h`
}

function weekFloor(ms: number): number {
  return Math.floor((ms - MONDAY_MS) / WEEK_MS) * WEEK_MS + MONDAY_MS
}

function weekLabel(ms: number): string {
  return "Week of " + new Date(ms).toLocaleDateString(undefined,
    { day: "numeric", month: "short" })
}

export function OrgReliabilityPanel() {
  const { scopeOrg, canWrite } = useAuth()
  const q = useQuery({
    queryKey: ["org-history", scopeOrg],
    queryFn: () => historyApi.org(scopeOrg, DAYS),
    enabled: !!scopeOrg && canWrite,
    staleTime: 300_000,
  })
  if (!scopeOrg || !canWrite) return null

  return <Body weeks={q.data?.weeks ?? null}
    since={q.data ? toUtcDate(q.data.since).getTime() : null}
    until={q.data ? toUtcDate(q.data.until).getTime() : null} />
}

function Body({ weeks, since, until }: {
  weeks: WeekStat[] | null
  since: number | null
  until: number | null
}) {
  const model = useMemo(() => {
    if (!weeks || since == null || until == null) return null
    const byWeek = new Map(weeks.map((w) => [w.week * 1000, w]))
    const all: number[] = []
    for (let t = weekFloor(since); t < until; t += WEEK_MS) all.push(t)
    const columns = all.map((t) => ({
      t, span: WEEK_MS,
      segs: [{ v: byWeek.get(t)?.outages ?? 0, color: FLEET, opacity: 0.75 }],
    }))
    // Long sits at the BASELINE so the segment you compare week to week is
    // the one anchored to a fixed edge; the flap band floats on top of it.
    const downColumns = all.map((t) => {
      const w = byWeek.get(t)
      return {
        t, span: WEEK_MS,
        segs: [
          { v: w?.down_long_s ?? 0, color: FLEET, opacity: LONG_OP },
          { v: w?.down_short_s ?? 0, color: FLEET, opacity: SHORT_OP },
        ],
      }
    })
    // Every figure in the summary is a fold of the SAME rows the columns
    // draw, so the sentence can never claim a number the charts don't show
    // (the count-agreement rule the issue plane keeps).
    const totals = weeks.reduce((a, w) => ({
      outages: a.outages + w.outages,
      long: a.long + w.long_outages,
      downLong: a.downLong + w.down_long_s,
      down: a.down + w.down_long_s + w.down_short_s,
    }), { outages: 0, long: 0, downLong: 0, down: 0 })
    return {
      all, byWeek, columns, downColumns, totals,
      maxOutages: Math.max(1, ...weeks.map((w) => w.outages)),
      maxDown: Math.max(60, ...weeks.map((w) => w.down_long_s + w.down_short_s)),
    }
  }, [weeks, since, until])

  if (!model || since == null || until == null) return null
  const empty = weeks && weeks.length === 0
    ? "No outages in this window."
    : null

  const tipFor = (kind: "outages" | "downtime") => (tMs: number): TooltipModel | null => {
    const t = weekFloor(tMs)
    const w = model.byWeek.get(t)
    const at = t + WEEK_MS / 2
    if (kind === "outages") {
      const rows = w
        ? [{ label: "opened", value: String(w.outages) },
           { label: LONG_LABEL, value: String(w.long_outages) }]
        : [{ label: "opened", value: "0" }]
      return { at, title: weekLabel(t), rows }
    }
    if (!w) return { at, title: weekLabel(t), rows: [{ label: "down", value: "0" }] }
    // A week can carry downtime from an outage that opened earlier, so the
    // long-band row counts what OPENED here and says so rather than implying
    // the hours and the count came from the same outages.
    return {
      at, title: weekLabel(t),
      rows: [
        { label: "down", value: fmtDown(w.down_long_s + w.down_short_s) },
        { label: LONG_LABEL, value: fmtDown(w.down_long_s), color: tone(LONG_OP) },
        { label: SHORT_LABEL, value: fmtDown(w.down_short_s), color: tone(SHORT_OP) },
        { label: "opened this week", value: `${w.outages} · ${w.long_outages} long` },
      ],
    }
  }

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">Reliability</span>
          <span className="text-xs text-faint-foreground">
            {model.all.length === 1 ? "last week" : `last ${model.all.length} weeks`}
            {" · device outages"}
          </span>
        </h2>
      </div>
      {!empty && <Summary {...model.totals} />}
      <div className="grid gap-4 px-4 pb-4 pt-3 @2xl:grid-cols-2">
        <TimeChart domain={[since, until]} yMax={model.maxOutages} height={150}
          empty={empty} tooltip={tipFor("outages")}
          legend={<LegendChip color={tone(0.75)} label="outages opened / week" />}>
          <ColumnMark buckets={model.columns} />
        </TimeChart>
        <TimeChart domain={[since, until]} yMax={model.maxDown} height={150}
          empty={empty} yFmt={fmtDown} tooltip={tipFor("downtime")}
          legend={<>
            <LegendChip color={tone(LONG_OP)} label={`down · ${LONG_LABEL}`} />
            <LegendChip color={tone(SHORT_OP)} label={SHORT_LABEL} />
          </>}>
          <ColumnMark buckets={model.downColumns} />
        </TimeChart>
      </div>
    </section>
  )
}

// The conclusion, in words, above the evidence. It exists because the two
// charts disagree on purpose and the disagreement is the finding: a fleet can
// open a thousand outages and lose its afternoon to four of them. Reading
// that off two bar charts takes a moment; reading it here takes none.
function Summary({ outages, long, downLong, down }: {
  outages: number
  long: number
  downLong: number
  down: number
}) {
  const n = (v: number) => v.toLocaleString()
  const share = down > 0 ? Math.round(100 * downLong / down) : 0
  return (
    <p className="px-4 pt-3 text-xs text-muted-foreground">
      <span className="tabular-nums text-foreground">{n(outages)}</span>
      {outages === 1 ? " outage opened" : " outages opened"}
      {" · "}
      <span className="tabular-nums text-foreground">{fmtDown(down)}</span>
      {" of device downtime"}
      {long === 0
        ? " · none ran 30 minutes or longer"
        : <>
            {" · "}
            <span className="tabular-nums text-foreground">{n(long)}</span>
            {" ran 30 minutes or longer and "}
            {long === 1 ? "accounts" : "account"} for{" "}
            <span className="tabular-nums text-foreground">{share}%</span>
            {" of it"}
          </>}
    </p>
  )
}
