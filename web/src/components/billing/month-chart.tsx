// MONTH TO DATE, over the org's own stored accrual rows.
//
// ONE PLANE, and it is FLEET (--chart-5, 325°). The bill is
// max(ONUs × rate, devices × floor): both operands are counts of the
// operator's own estate, which is what the fleet plane names, and the only
// other org-account-level chart in the product (Reliability) already lives
// there — an owner reading their own numbers should not be crossing hues
// between two panels that answer the same kind of question. Optical was the
// near miss and was rejected: subscriber ONUs feed the count, but a money
// series drawn in the optics hue reads as an optical measurement, and this is
// not one. No status hue appears here at all: nothing on this chart is a
// failure claim.
//
// A DAY WITH NO ROW IS NOT A ZERO. ColumnMark draws nothing for a zero, so a
// missing day and a free day would render identically. The gap gets a shaded
// band instead (the dead-zone argument, one level up): "we have no reading for
// this day" and "you were charged nothing" take opposite actions.
import { useMemo } from "react"
import { LegendChip, TimeChart, useChart } from "@/chart/frame"
import type { TooltipModel } from "@/chart/frame"
import { ColumnMark, LineMark, RuleMark } from "@/chart/marks"
import { DAY_MS, epochDayMs } from "@/chart/scale"
import { connSourceMeta, dayLabel, inr, inrExact } from "@/lib/billing"
import type { Accrual, BillingInfo } from "@/lib/types"
import { Panel } from "./shared"

const FLEET = "var(--chart-5)"

/** A YYYY-MM-DD operator day as the UTC midnight the chart plots it at. The
 *  whole kit is scaleUtc and every other chart keys its buckets the same way;
 *  the display zone is the operator's, so the two agree by construction. */
function dayMs(day: string): number {
  return Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10))
}

/** The shaded band for days the meter never filed. Drawn under the marks so a
 *  neighbouring column keeps its own edge. */
function MissingDays({ days }: { days: number[] }) {
  const { x, h, pad } = useChart()
  return (
    <g>
      {days.map((t) => (
        <rect key={t} x={x(t)} y={pad.t}
          width={Math.max(1, x(t + DAY_MS) - x(t))}
          height={Math.max(0, h - pad.b - pad.t)}
          fill="var(--faint-foreground)" fillOpacity={0.08} />
      ))}
    </g>
  )
}

function LegendSwatch({ label, dashed }: { label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      {dashed
        ? (
          <svg width="16" height="6" aria-hidden>
            <line x1="0" x2="16" y1="3" y2="3" stroke="var(--muted-foreground)"
              strokeWidth="1" strokeDasharray="4 3" />
          </svg>
        )
        : (
          <span aria-hidden className="h-2 w-3 rounded-[2px]"
            style={{ background: "var(--faint-foreground)", opacity: 0.35 }} />
        )}
      {label}
    </span>
  )
}

export function MonthChart({ b }: { b: BillingInfo }) {
  const model = useMemo(() => {
    const rows = b.accruals
    const byDay = new Map<number, Accrual>(rows.map((a) => [dayMs(a.day), a]))
    const first = dayMs(`${b.month}-01`)
    const lastRow = rows.length ? dayMs(rows[rows.length - 1].day) : first
    // The window ends at today, not at the end of the month: a month-to-date
    // chart padded out with a fortnight of blank right-hand air reads as a
    // data outage rather than as a month in progress.
    const last = Math.max(first, lastRow, b.today ? dayMs(b.today.day) : first)

    const days: number[] = []
    for (let t = first; t <= last; t += DAY_MS) days.push(t)

    const columns = days.map((t) => ({
      t, span: DAY_MS,
      segs: [{ v: byDay.get(t)?.paise ?? 0, color: FLEET, opacity: 0.8 }],
    }))
    // A gap breaks the line (marks.tsx defined()); a day that ran and counted
    // zero ONUs is a real zero and stays on it.
    const conns = days.map((t) => ({
      t: t + DAY_MS / 2,
      v: byDay.get(t)?.conn_count ?? null,
    }))
    const missing = days.filter((t) => !byDay.has(t))

    // The count at which the ONU side overtakes the device floor:
    // ONUs × rate ≥ devices × floor. A real decision boundary, so it is drawn
    // as one (the RxScale grammar) rather than left for the reader to compute.
    const devices = b.today?.device_count ?? b.device_count
    const rate = b.today?.conn_rate_paise ?? b.rates.conn_paise
    const floor = b.today?.floor_paise ?? b.rates.floor_paise
    const breakEven = rate > 0 && devices * floor > 0
      ? Math.ceil((devices * floor) / rate)
      : null
    const maxConn = Math.max(1, ...rows.map((a) => a.conn_count))
    // The NUMBER is always worth having, so it always reaches the legend. The
    // MARK is only drawn when it sits near the data: a boundary at 12,000 over
    // a series at 400 squashes the series flat to answer a question the reader
    // can be told in words instead.
    const ruleAt = breakEven != null && breakEven <= maxConn * 3
      ? breakEven
      : null

    const prev = rows.length > 1 ? rows[rows.length - 2] : null
    const latest = rows.length ? rows[rows.length - 1] : null
    const delta = latest && prev
      && dayMs(latest.day) - dayMs(prev.day) === DAY_MS
      ? latest.conn_count - prev.conn_count
      : null

    return {
      byDay, first, last, missing, columns, conns, delta, breakEven, ruleAt,
      maxPaise: Math.max(1, ...rows.map((a) => a.paise)),
      maxConn: ruleAt != null ? Math.max(maxConn, ruleAt) : maxConn,
    }
  }, [b.accruals, b.month, b.today, b.device_count, b.rates])

  const empty = b.accruals.length === 0
    ? `Nothing has been metered in ${b.month_label} yet. The first day appears once the meter runs.`
    : null

  const tipFor = (kind: "charge" | "conns") => (tMs: number): TooltipModel | null => {
    const day = epochDayMs(tMs)
    if (day < model.first || day > model.last) return null
    const at = day + DAY_MS / 2
    const a = model.byDay.get(day)
    if (!a) {
      return {
        at, title: dayLabel(new Date(day).toISOString().slice(0, 10)),
        rows: [{ label: "meter", value: "did not run" }],
      }
    }
    const rows = kind === "charge"
      ? [
        { label: "charged", value: inrExact(a.paise), color: FLEET },
        { label: "ONUs", value: a.conn_count.toLocaleString("en-IN") },
        { label: "devices", value: a.device_count.toLocaleString("en-IN") },
        {
          label: "billed on",
          value: a.winning_side === "conn" ? "ONUs" : "device floor",
        },
      ]
      : [
        {
          label: "ONUs", color: FLEET,
          value: a.conn_count.toLocaleString("en-IN"),
        },
        { label: "source", value: connSourceMeta(a.conn_source).label },
        { label: "charged", value: inrExact(a.paise) },
      ]
    return { at, title: dayLabel(a.day), rows }
  }

  return (
    <Panel title="This month"
      note={<>{b.month_label} · {b.accruals.length} counted</>}
      right={model.delta != null && model.delta !== 0 && (
        // Hidden on a narrow container rather than wrapped: it is a nowrap
        // sentence and it would otherwise squeeze the panel's own title out.
        <span className="hidden shrink-0 text-2xs whitespace-nowrap text-muted-foreground @lg:inline">
          <span className="font-mono text-foreground">
            {Math.abs(model.delta).toLocaleString("en-IN")}
          </span>
          {model.delta < 0 ? " fewer" : " more"} than the day before
        </span>
      )}>
      <div className="grid gap-4 px-4 pt-3 pb-4 @3xl:grid-cols-2">
        <TimeChart domain={[model.first, model.last + DAY_MS]}
          yMax={model.maxPaise} height={150} empty={empty}
          yFmt={(v) => inr(v)} tooltip={tipFor("charge")}
          legend={<>
            <LegendChip color={FLEET} label="charged per day" />
            {model.missing.length > 0 && <LegendSwatch label="meter did not run" />}
          </>}>
          <MissingDays days={model.missing} />
          <ColumnMark buckets={model.columns} />
        </TimeChart>

        <TimeChart domain={[model.first, model.last + DAY_MS]}
          yMax={model.maxConn} height={150} empty={empty}
          yFmt={(v) => Math.round(v).toLocaleString("en-IN")}
          tooltip={tipFor("conns")}
          legend={<>
            <LegendChip color={FLEET} label="ONUs counted" />
            {model.breakEven != null && (model.ruleAt != null
              ? (
                <LegendSwatch dashed
                  label={`device floor applies below ${model.breakEven.toLocaleString("en-IN")}`} />
              )
              : (
                <span className="text-2xs text-muted-foreground">
                  device floor applies below{" "}
                  {model.breakEven.toLocaleString("en-IN")}
                </span>
              ))}
          </>}>
          <MissingDays days={model.missing} />
          {model.ruleAt != null && (
            <RuleMark v={model.ruleAt} color="var(--muted-foreground)" />
          )}
          <LineMark points={model.conns} color={FLEET} />
        </TimeChart>
      </div>

      <p className="border-t border-border-subtle px-4 py-2.5 text-2xs text-muted-foreground">
        Each bar is a stored day. An invoice is the sum of its own days and is
        never recomputed, so this chart and the bill always agree.
      </p>
    </Panel>
  )
}
