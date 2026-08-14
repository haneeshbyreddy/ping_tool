// The one chart frame (notes/viz-grammar.md). Owns the scales, the recessive
// axes/grid, the crosshair + tooltip layer, the legend row and the empty
// state; marks are children reading the scales via useChart(). Charts must
// stay inert under useNow()/SSE re-renders: the frame's hover state is local,
// scales are memoized on [width, domain], and every mark memoizes its path on
// its own data — nothing here animates or replays on refetch.
import { createContext, useCallback, useContext, useEffect, useMemo,
         useRef, useState } from "react"
import { cn } from "@/lib/utils"
import { fmtTime, linearScale, timeScale } from "./scale"
import type { XScale, YScale } from "./scale"

export interface ChartScales {
  x: XScale
  y: YScale
  w: number
  h: number
  pad: { l: number; r: number; t: number; b: number }
}

const Ctx = createContext<ChartScales | null>(null)

export function useChart(): ChartScales {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error("chart mark outside <TimeChart>")
  return ctx
}

export interface TooltipRow {
  label: string
  value: string
  color?: string
}

export interface TooltipModel {
  at?: number          // snapped time for the crosshair (defaults to cursor)
  title: string
  rows: TooltipRow[]
}

export function LegendChip({ color, label, struck }: {
  color: string
  label: string
  struck?: boolean
}) {
  // Identity grammar: neutral text beside a coloured dot — a legend never
  // wears the series colour as text. `struck` is the suppressed-alert channel.
  return (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <span aria-hidden className="size-2 shrink-0 rounded-full"
        style={{ background: color }} />
      <span className={struck ? "line-through decoration-muted-foreground/70" : undefined}>
        {label}
      </span>
    </span>
  )
}

const PAD = { l: 42, r: 8, t: 10, b: 20 }

export function TimeChart({
  domain, yMax, height = 160, yFmt, yTicks = 3, tooltip, legend, empty,
  className, children,
}: {
  domain: [number, number]
  yMax: number
  height?: number
  yFmt?: (v: number) => string
  yTicks?: number
  tooltip?: (tMs: number) => TooltipModel | null
  legend?: React.ReactNode
  empty?: string | null
  className?: string
  children?: React.ReactNode
}) {
  const [w, setW] = useState(0)
  const observer = useRef<ResizeObserver | null>(null)
  const hostRef = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      const width = Math.round(entry.contentRect.width)
      setW((prev) => (prev === width ? prev : width))
    })
    ro.observe(el)
    observer.current = ro
    setW(Math.round(el.getBoundingClientRect().width))
  }, [])
  useEffect(() => () => observer.current?.disconnect(), [])

  const scales = useMemo<ChartScales>(() => ({
    x: timeScale(domain, [PAD.l, Math.max(PAD.l + 1, w - PAD.r)]),
    y: linearScale([0, Math.max(1e-9, yMax)], [height - PAD.b, PAD.t]),
    w, h: height, pad: PAD,
  }), [domain[0], domain[1], yMax, w, height])

  const [hover, setHover] = useState<{ px: number; tip: TooltipModel } | null>(null)
  const onMove = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    if (!tooltip) return
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    if (px < PAD.l || px > w - PAD.r) { setHover(null); return }
    const tip = tooltip(scales.x.invert(px).getTime())
    setHover(tip ? { px: tip.at != null ? scales.x(tip.at) : px, tip } : null)
  }, [tooltip, scales, w])

  const span = domain[1] - domain[0]
  const xTicks = useMemo(
    () => (w > 0 ? scales.x.ticks(Math.max(2, Math.floor(w / 110))) : []),
    [scales, w])
  const yTickVals = useMemo(() => scales.y.ticks(yTicks), [scales, yTicks])

  return (
    <div className={cn("min-w-0", className)}>
      {legend != null && (
        <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">{legend}</div>
      )}
      <div ref={hostRef} className="relative min-w-0" style={{ height }}>
        {empty ? (
          <div className="flex h-full items-center justify-center rounded-md border border-dashed border-border px-3 text-center text-2xs text-muted-foreground">
            {empty}
          </div>
        ) : w > 0 && (
          <svg width={w} height={height} className="block"
            role="img"
            onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
            {yTickVals.map((v) => (
              <g key={`y${v}`}>
                <line x1={PAD.l} x2={w - PAD.r} y1={scales.y(v)} y2={scales.y(v)}
                  className="stroke-border" strokeOpacity={0.5} />
                <text x={PAD.l - 6} y={scales.y(v)} dy="0.32em" textAnchor="end"
                  className="fill-faint-foreground text-2xs tabular-nums">
                  {yFmt ? yFmt(v) : v}
                </text>
              </g>
            ))}
            {xTicks.map((d) => (
              <text key={`x${+d}`} x={scales.x(d)} y={height - 6} textAnchor="middle"
                className="fill-faint-foreground text-2xs tabular-nums">
                {fmtTime(d, span)}
              </text>
            ))}
            <Ctx.Provider value={scales}>{children}</Ctx.Provider>
            {hover && (
              <line x1={hover.px} x2={hover.px} y1={PAD.t} y2={height - PAD.b}
                className="stroke-muted-foreground" strokeOpacity={0.45}
                strokeDasharray="2 3" />
            )}
          </svg>
        )}
        {hover && !empty && (
          <div
            className="pointer-events-none absolute top-1 z-10 min-w-36 rounded-md border border-border bg-popover px-2.5 py-1.5"
            style={hover.px > w * 0.55
              ? { right: Math.max(0, w - hover.px + 8) }
              : { left: hover.px + 8 }}>
            <div className="text-2xs font-medium text-foreground">{hover.tip.title}</div>
            {hover.tip.rows.map((r, i) => (
              <div key={i} className="flex items-center justify-between gap-3 text-2xs">
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  {r.color && <span aria-hidden className="size-1.5 rounded-full"
                    style={{ background: r.color }} />}
                  {r.label}
                </span>
                <span className="tabular-nums text-foreground">{r.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
