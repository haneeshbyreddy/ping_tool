// The circular state instrument (Home's cockpit). A part-to-whole ring in the
// OnuBar grammar bent into a circle: the same tones, the same worst-first
// order, and a 2px surface gap between segments — the gaps show the card
// through, never a stroke drawn around a mark. The centre is the hero readout;
// hovering a segment swaps the centre to that segment's exact count, so every
// number is reachable without a floating tooltip while the legend chips beside
// the ring stay the dependable channel (colour is never the only encoding).
//
// Stays inert under useNow()/SSE re-renders: the arcs memoise on their own
// data and the only animation is an opacity transition on hover — no mount
// animation, so a refetch can never replay a draw-in.
import { useMemo, useState, type ReactNode } from "react"

export interface RingSeg {
  key: string
  label: string
  value: number
  color: string
  // Centre text colour while this segment is hovered — a quiet 40%-alpha fill
  // is honest as an arc and illegible as text, so the ink is stated apart.
  ink?: string
}

const TAU = Math.PI * 2
const GAP_PX = 2.5
// A single critical ONU in a 2,000-ONU fleet is the whole reason to look at
// this ring, so a non-zero segment never renders below ~3.4°. The excess is
// taken from the largest segment, where the distortion is imperceptible; the
// legend chips and the centre readout carry the exact numbers.
const MIN_ANGLE = 0.06

function pt(c: number, r: number, a: number): string {
  return `${(c + r * Math.sin(a)).toFixed(2)} ${(c - r * Math.cos(a)).toFixed(2)}`
}

function arcPath(c: number, r: number, a0: number, a1: number): string {
  return `M ${pt(c, r, a0)} A ${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${pt(c, r, a1)}`
}

interface Arc {
  seg: RingSeg
  d: string
}

export function StateRing({ segs, size = 172, stroke = 12, hero, sub, ariaLabel }: {
  segs: RingSeg[]
  size?: number
  stroke?: number
  hero: ReactNode
  sub?: ReactNode
  ariaLabel: string
}) {
  const c = size / 2
  const r = (size - stroke) / 2 - 1
  const hit = Math.max(24, stroke + 12)

  const model = useMemo<{ full: RingSeg | null; arcs: Arc[] } | null>(() => {
    const visible = segs.filter((s) => s.value > 0)
    const total = visible.reduce((n, s) => n + s.value, 0)
    if (!total) return null
    if (visible.length === 1) return { full: visible[0], arcs: [] }
    const gap = GAP_PX / r
    const avail = TAU - visible.length * gap
    const angles = visible.map((s) => (s.value / total) * avail)
    let debt = 0
    for (let i = 0; i < angles.length; i++) {
      if (angles[i] < MIN_ANGLE) { debt += MIN_ANGLE - angles[i]; angles[i] = MIN_ANGLE }
    }
    angles[angles.indexOf(Math.max(...angles))] -= debt
    let a = 0
    const arcs = visible.map((s, i) => {
      const arc = { seg: s, d: arcPath(c, r, a, a + angles[i]) }
      a += angles[i] + gap
      return arc
    })
    return { full: null, arcs }
  }, [segs, c, r])

  const [hovered, setHovered] = useState<string | null>(null)
  const hov = hovered ? segs.find((s) => s.key === hovered) : undefined
  const dim = (key: string) => (hovered && hovered !== key ? 0.35 : 1)

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}
      role="img" aria-label={ariaLabel}>
      <svg width={size} height={size} className="absolute inset-0"
        onPointerLeave={() => setHovered(null)}>
        {!model && (
          <circle cx={c} cy={c} r={r} fill="none" stroke="var(--muted)"
            strokeWidth={stroke} />
        )}
        {model?.full && (
          <circle cx={c} cy={c} r={r} fill="none" stroke={model.full.color}
            strokeWidth={stroke} className="transition-opacity duration-150"
            opacity={dim(model.full.key)} pointerEvents="none" />
        )}
        {model?.arcs.map(({ seg, d }) => (
          <path key={seg.key} d={d} fill="none" stroke={seg.color}
            strokeWidth={stroke} className="transition-opacity duration-150"
            opacity={dim(seg.key)} pointerEvents="none" />
        ))}
        {/* Invisible wide hit ring per segment — a 12px arc is far under the
            ~24px hover-target floor, and the centre swap is the tooltip. */}
        {model?.full && (
          <circle cx={c} cy={c} r={r} fill="none" stroke="transparent"
            strokeWidth={hit} style={{ pointerEvents: "stroke" }}
            onPointerEnter={() => setHovered(model.full!.key)} />
        )}
        {model?.arcs.map(({ seg, d }) => (
          <path key={`hit-${seg.key}`} d={d} fill="none" stroke="transparent"
            strokeWidth={hit} style={{ pointerEvents: "stroke" }}
            onPointerEnter={() => setHovered(seg.key)} />
        ))}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-7 text-center">
        {hov ? (
          <>
            <span className="text-3xl leading-none font-semibold"
              style={{ color: hov.ink ?? hov.color }}>
              {hov.value.toLocaleString()}
            </span>
            <span className="mt-1.5 text-2xs leading-tight text-muted-foreground">
              {hov.label}
            </span>
          </>
        ) : (
          <>
            <span className="text-3xl leading-none font-semibold text-foreground">
              {hero}
            </span>
            {sub != null && (
              <span className="mt-1.5 text-2xs leading-tight text-faint-foreground">
                {sub}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  )
}
