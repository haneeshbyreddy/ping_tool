import type { PerfSample, TrendBucket } from "@/lib/types"
import { toUtcDate } from "@/lib/format"

export function Sparkline({ samples, height = 56, baseline }: {
  samples: PerfSample[]
  height?: number
  baseline?: number | null
}) {
  const width = 600
  const pad = 4
  const n = samples.length
  if (n === 0) return null

  const xs = (i: number) => n === 1 ? width / 2 : pad + (i * (width - pad * 2)) / (n - 1)
  const values = samples.map((s) => s.latency_ms)
  const max = Math.max(1, baseline ?? 0, ...values.filter((v): v is number => v != null))
  const ys = (v: number) => height - pad - (v / (max * 1.15)) * (height - pad * 2)

  const segments: string[] = []
  const lonePoints: Array<[number, number]> = []
  let run: Array<[number, number]> = []
  const flush = () => {
    if (run.length === 1) lonePoints.push(run[0])
    if (run.length > 1) segments.push(run.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "))
    run = []
  }
  values.forEach((v, i) => {
    if (v == null) flush()
    else run.push([xs(i), ys(v)])
  })
  flush()

  const bad = samples
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => (s.packet_loss ?? 0) > 0 || (s.state && s.state !== "UP"))

  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
      className="w-full text-primary" style={{ height }} aria-hidden>
      {baseline != null && (
        <line x1={pad} x2={width - pad} y1={ys(baseline)} y2={ys(baseline)}
          className="stroke-muted-foreground/50" strokeWidth="1"
          strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
      )}
      {segments.map((pts, i) => (
        <polyline key={i} points={pts} fill="none" stroke="currentColor" strokeWidth="1.5"
          vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
      ))}
      {lonePoints.map(([x, y], i) => (
        <circle key={`p${i}`} cx={x} cy={y} r="2" fill="currentColor" />
      ))}
      {bad.map(({ s, i }) => (
        <circle key={`b${i}`} cx={xs(i)}
          cy={s.latency_ms != null ? ys(s.latency_ms) : height - pad}
          r="2.5" className="fill-destructive" />
      ))}
    </svg>
  )
}

export function bucketTrouble(b: TrendBucket): "down" | "loss" | null {
  if ((b.down_pct ?? 0) > 0 || b.avg_latency_ms == null) return "down"
  if ((b.avg_loss_pct ?? 0) > 1) return "loss"
  return null
}

export function HourStrip({ buckets, hours = 24 }: { buckets: TrendBucket[]; hours?: number }) {
  const HOUR = 3_600_000
  const byTime = new Map(buckets.map((b) => [toUtcDate(b.bucket).getTime(), b]))
  const top = Math.floor(Date.now() / HOUR) * HOUR
  const hourLabel = (t: number) =>
    new Date(t).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })

  return (
    <div className="flex gap-px" role="img" aria-label={`last ${hours} hours of health`}>
      {Array.from({ length: hours }, (_, i) => {
        const t = top - (hours - 1 - i) * HOUR
        const b = byTime.get(t)
        if (!b) {
          return <span key={t} title={`${hourLabel(t)}: no data`}
            className="h-4 min-w-0 flex-1 rounded-[2px] border border-border/60" />
        }
        const trouble = bucketTrouble(b)
        // healthy hours whisper (40%) so a red/amber cell is the loudest thing here
        const cls = trouble === "down" ? "bg-destructive"
          : trouble === "loss" ? "bg-warning"
          : "bg-success/40"
        const detail = [
          hourLabel(t),
          b.avg_latency_ms != null ? `${b.avg_latency_ms.toFixed(1)} ms avg` : "no reply all hour",
          (b.avg_loss_pct ?? 0) > 0 ? `${b.avg_loss_pct!.toFixed(1)}% loss` : null,
          (b.down_pct ?? 0) > 0 ? `down ${b.down_pct!.toFixed(0)}% of the hour` : null,
        ].filter(Boolean).join(" · ")
        return <span key={t} title={detail} className={`h-4 min-w-0 flex-1 rounded-[2px] ${cls}`} />
      })}
    </div>
  )
}

/** The SHAPE of the last 24 hours' latency, drawn on the same epoch-hour grid
 *  `HourStrip` uses so the two stack into one reading.
 *
 *  WHY BOTH, rather than one or the other: an OTDR shows a TRACE and an EVENT
 *  TABLE together, because the graph finds the anomaly and the table says what
 *  it was. `HourStrip` is the event table — which hours were rough — and it
 *  cannot show that a link has been climbing all afternoon while never once
 *  crossing a threshold. This is the trace. A number with no trend behind it is
 *  half a number.
 *
 *  THE GRID IS SHARED AND THAT IS LOAD-BEARING. Both floor on EPOCH hours, not
 *  local ones — CLAUDE.md's documented trap, because a half-hour zone like IST
 *  shifts every cell — so column N here is the same hour as cell N below it.
 *  Drawn across all 24 slots even where a bucket is missing, so a gap in the
 *  history is a gap in the LINE rather than a line that quietly closes over it.
 *
 *  It takes NO status tone. A latency trend is reference material: the strip
 *  beneath already carries red and amber for the hours that were actually bad,
 *  and a second red channel repeating that louder is exactly what the rank
 *  audit found wrong everywhere else on this product. */
export function TrendSpark({ buckets, hours = 24, height = 22 }: {
  buckets: TrendBucket[]; hours?: number; height?: number
}) {
  const HOUR = 3_600_000
  const byTime = new Map(buckets.map((b) => [toUtcDate(b.bucket).getTime(), b]))
  const top = Math.floor(Date.now() / HOUR) * HOUR
  const slots = Array.from({ length: hours }, (_, i) =>
    byTime.get(top - (hours - 1 - i) * HOUR)?.avg_latency_ms ?? null)

  const seen = slots.filter((v): v is number => v != null)
  if (seen.length < 2) return null

  const W = 240, pad = 2
  const max = Math.max(...seen), min = Math.min(...seen)
  // a flat line is a REAL answer — pin it mid-height rather than letting a
  // zero-range divide blow a steady link up into noise
  const range = max - min < 0.05 ? 1 : max - min
  const x = (i: number) => pad + (i * (W - pad * 2)) / (hours - 1)
  const y = (v: number) => height - pad - ((v - min) / range) * (height - pad * 2)

  const runs: string[] = []
  let run: string[] = []
  slots.forEach((v, i) => {
    if (v == null) { if (run.length > 1) runs.push(run.join(" ")); run = [] }
    else run.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (run.length > 1) runs.push(run.join(" "))

  return (
    <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" style={{ height }}
      className="w-full text-muted-foreground" role="img"
      aria-label={`latency over the last ${hours} hours, ${min.toFixed(1)} to ${max.toFixed(1)} ms`}>
      {runs.map((pts, i) => (
        <polyline key={i} points={pts} fill="none" stroke="currentColor" strokeWidth="1.25"
          vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
      ))}
    </svg>
  )
}
