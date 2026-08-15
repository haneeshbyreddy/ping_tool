// The hour-of-day heatmap (notes/viz-grammar.md; Wave 2, chart E).
// DOM cells, not SVG — the DayStrip precedent: the tooltip is the native
// title, the grid is one CSS grid, and nothing here animates or re-measures
// under a useNow()/SSE re-render.
//
// COLOUR LAW, and it is the whole reason this is a separate mark: a heatmap
// wants a ramp, and a ramp is exactly where a chart drifts into the alarm
// vocabulary. So the ramp is SEQUENTIAL ON ONE IDENTITY HUE — steps of the
// traffic plane and nothing else. No second hue, no green-to-red, no
// interpolation through amber. A busy hour is not a fault; the whole point of
// this plot is that it stays legible on a screen where a real alarm has to be
// the loudest thing.
//
// Steps are QUANTIZED to five, on purpose: a continuous ramp cannot be
// legended, and a cell nobody can name a value for is decoration. The lowest
// step is reserved for "measured, and quiet" so a measured-idle hour can never
// render like an hour nobody walked — that one is the dead zone, a hairline
// track, the same distinction <Reading> makes.
import { Fragment } from "react"
import { cn } from "@/lib/utils"

export const HEAT_PLANE = "var(--plane-traffic)"

// Alpha of the plane, per step. The top step is the plane at full strength,
// which is chroma-capped at ~55% of the quietest status tone by construction —
// so even a saturated row cannot outshout an alarm on the same screen.
export const HEAT_STEPS = [0.14, 0.3, 0.48, 0.68, 0.92] as const

export function heatLevel(v: number | null | undefined,
                          max: number | null | undefined): number {
  // 0 = absent (no reading). 1..5 = measured, quietest first. A real zero is a
  // real reading and takes step 1 — "idle" and "not measured" are different
  // sentences and this plot may not draw them alike.
  if (v == null) return 0
  if (!max || max <= 0) return 1
  const k = Math.min(1, Math.max(0, v / max))
  return Math.min(HEAT_STEPS.length, Math.max(1, Math.ceil(k * HEAT_STEPS.length)))
}

export function heatFill(level: number): string | undefined {
  if (level <= 0) return undefined
  const pct = Math.round(HEAT_STEPS[level - 1] * 100)
  return `color-mix(in srgb, ${HEAT_PLANE} ${pct}%, transparent)`
}

export interface HeatmapRow {
  key: string
  label?: React.ReactNode
  /** value per column, in the caller's column order; null/absent = not measured */
  values: (number | null)[]
  /** the row's own normaliser; falls back to the row's max */
  max?: number | null
  title?: (col: number, v: number | null) => string
  to?: string
  onSelect?: () => void
}

export interface HeatmapColumn {
  key: string | number
  label: string
}

export function Heatmap({
  rows, columns, labelWidth = "9rem", axisEvery = 4, className, cellClass,
}: {
  rows: HeatmapRow[]
  columns: HeatmapColumn[]
  /** null drops the label column entirely (a single-row strip) */
  labelWidth?: string | null
  axisEvery?: number
  className?: string
  cellClass?: string
}) {
  const cols = columns.length
  const template = labelWidth
    ? `${labelWidth} repeat(${cols}, minmax(0, 1fr))`
    : `repeat(${cols}, minmax(0, 1fr))`
  return (
    <div className={cn("min-w-0", className)}>
      <div className="grid items-center gap-x-px gap-y-1"
        style={{ gridTemplateColumns: template }}>
        {rows.map((row) => {
          const max = row.max ?? Math.max(
            0, ...row.values.filter((v): v is number => v != null))
          return (
            <Fragment key={row.key}>
              {labelWidth && (
                <div className="min-w-0 truncate pr-2 text-2xs text-muted-foreground">
                  {row.label}
                </div>
              )}
              {row.values.map((v, i) => {
                const level = heatLevel(v, max)
                const title = row.title?.(i, v)
                return level === 0 ? (
                  <span key={i} title={title}
                    className={cn("h-3.5 rounded-[2px] border border-dashed border-border/70",
                      cellClass)} />
                ) : (
                  <span key={i} title={title}
                    className={cn("h-3.5 rounded-[2px]", cellClass)}
                    style={{ background: heatFill(level) }} />
                )
              })}
            </Fragment>
          )
        })}
        {labelWidth && <div />}
        {columns.map((c, i) => (
          i % axisEvery === 0 ? (
            <div key={c.key} style={{ gridColumn: `span ${Math.min(axisEvery, cols - i)}` }}
              className="pt-0.5 text-2xs tabular-nums text-faint-foreground">
              {c.label}
            </div>
          ) : null
        ))}
      </div>
    </div>
  )
}

export function HeatmapLegend({ quiet = "quiet", busy = "busy", className }: {
  quiet?: string
  busy?: string
  className?: string
}) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1", className)}>
      <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
        {quiet}
        <span aria-hidden className="inline-flex gap-px">
          {HEAT_STEPS.map((_, i) => (
            <span key={i} className="h-2 w-3 rounded-[2px]"
              style={{ background: heatFill(i + 1) }} />
          ))}
        </span>
        {busy}
      </span>
      <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
        <span aria-hidden
          className="h-2 w-3 rounded-[2px] border border-dashed border-border/70" />
        not measured
      </span>
    </div>
  )
}
