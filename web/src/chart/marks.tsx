// Marks consume the frame's scales and render plain SVG. Every path is
// memoized on its own data so a useNow()/SSE re-render recomputes nothing.
// Gap grammar: a NULL value BREAKS the line (d3's defined()); a lone point
// between gaps renders as a dot rather than vanishing (the Sparkline rule).
import { useMemo } from "react"
import { area, line } from "d3-shape"
import { useChart } from "./frame"

export interface TimePoint {
  t: number
  v: number | null
}

export function LineMark({ points, color, width = 1.5, dash, opacity = 1 }: {
  points: TimePoint[]
  color: string
  width?: number
  dash?: string
  opacity?: number
}) {
  const { x, y } = useChart()
  const { d, lone } = useMemo(() => {
    const gen = line<TimePoint>()
      .defined((p) => p.v != null)
      .x((p) => x(p.t))
      .y((p) => y(p.v as number))
    const singles: TimePoint[] = points.filter((p, i) =>
      p.v != null
      && (i === 0 || points[i - 1].v == null)
      && (i === points.length - 1 || points[i + 1].v == null))
    return { d: gen(points) ?? "", lone: singles }
  }, [points, x, y])
  return (
    <g stroke={color} fill="none" opacity={opacity}>
      <path d={d} strokeWidth={width} strokeDasharray={dash}
        strokeLinejoin="round" strokeLinecap="round" />
      {lone.map((p) => (
        <circle key={p.t} cx={x(p.t)} cy={y(p.v as number)} r={2}
          fill={color} stroke="none" />
      ))}
    </g>
  )
}

export interface BandPoint {
  t: number
  lo: number | null
  hi: number | null
}

export function BandMark({ points, color, opacity = 0.16 }: {
  points: BandPoint[]
  color: string
  opacity?: number
}) {
  const { x, y } = useChart()
  const d = useMemo(() => {
    const gen = area<BandPoint>()
      .defined((p) => p.lo != null && p.hi != null)
      .x((p) => x(p.t))
      .y0((p) => y(p.lo as number))
      .y1((p) => y(p.hi as number))
    return gen(points) ?? ""
  }, [points, x, y])
  return <path d={d} fill={color} fillOpacity={opacity} stroke="none" />
}

export interface ColumnSeg {
  v: number
  color: string
  opacity?: number
}

export interface ColumnBucket {
  t: number        // bucket start (ms)
  span: number     // bucket width (ms)
  segs: ColumnSeg[]
}

export function ColumnMark({ buckets, gap = 2 }: {
  buckets: ColumnBucket[]
  gap?: number
}) {
  // Stacked columns, baseline-anchored, a 2px surface gap between segments
  // and between neighbours (the dataviz mark spec). Zero-height segments
  // draw nothing — a zero is an honest zero, not a dead zone, because these
  // buckets are counts of events.
  const { x, y, h, pad } = useChart()
  const cols = useMemo(() => buckets.map((b) => {
    const x0 = x(b.t) + gap / 2
    const bw = Math.max(1, x(b.t + b.span) - x(b.t) - gap)
    let base = h - pad.b
    const rects = []
    for (const s of b.segs) {
      if (s.v <= 0) continue
      const top = y(s.v)
      const hh = Math.max(1, (h - pad.b) - top)
      base -= hh
      rects.push({ x: x0, y: base, w: bw, h: hh, color: s.color,
                   opacity: s.opacity ?? 1 })
      base -= 2
    }
    return rects
  }), [buckets, x, y, h, pad, gap])
  return (
    <g>
      {cols.flat().map((r, i) => (
        <rect key={i} x={r.x} y={r.y} width={r.w} height={r.h} rx={1.5}
          fill={r.color} fillOpacity={r.opacity} />
      ))}
    </g>
  )
}

export function EventRule({ t, color, opacity = 0.65 }: {
  t: number
  color: string
  opacity?: number
}) {
  const { x, h, pad } = useChart()
  const px = x(t)
  return (
    <line x1={px} x2={px} y1={pad.t} y2={h - pad.b} stroke={color}
      strokeOpacity={opacity} strokeDasharray="3 3" />
  )
}

export function RuleMark({ v, color, label }: {
  v: number
  color: string
  label?: string
}) {
  // A decision boundary (threshold/ceiling) — the RxScale grammar on a time
  // chart: a fixed mark the series is on one side of.
  const { y, w, pad } = useChart()
  const py = y(v)
  return (
    <g>
      <line x1={pad.l} x2={w - pad.r} y1={py} y2={py} stroke={color}
        strokeOpacity={0.7} strokeDasharray="4 3" />
      {label && (
        <text x={w - pad.r} y={py - 4} textAnchor="end"
          className="text-2xs" fill={color}>{label}</text>
      )}
    </g>
  )
}
