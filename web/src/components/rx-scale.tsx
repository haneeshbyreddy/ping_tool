import { cn } from "@/lib/utils"

export function RxScale({ rx, warn, crit, className }: {
  rx: number | null | undefined
  warn: number | null | undefined
  crit: number | null | undefined
  className?: string
}) {
  if (rx == null || warn == null || crit == null) return null
  if (!(crit < warn)) return null

  const lo = crit - 3
  const hi = warn + 3
  const span = hi - lo
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - lo) / span) * 100))

  const pegged = rx > hi
  const pos = pct(rx)
  const tone = rx <= crit ? "crit" : rx <= warn ? "warn" : "ok"

  return (
    <span
      className={cn("wisp-rxscale", `wisp-rxscale--${tone}`, className)}
      title={`${rx.toFixed(2)} dBm · warn ${warn} · crit ${crit} · scale ${lo} to ${hi} dBm`}
      aria-hidden
    >
      <span className="wisp-rxscale__band wisp-rxscale__band--crit"
        style={{ width: `${pct(crit)}%` }} />
      <span className="wisp-rxscale__band wisp-rxscale__band--warn"
        style={{ left: `${pct(crit)}%`, width: `${pct(warn) - pct(crit)}%` }} />
      <span className={cn("wisp-rxscale__mark", pegged && "wisp-rxscale__mark--peg")}
        style={{ left: `${pos}%` }} />
    </span>
  )
}
