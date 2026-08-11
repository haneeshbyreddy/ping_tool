import { cn } from "@/lib/utils"

export function OnuBar({ total, crit, warn, online, className, title }: {
  total: number
  crit: number
  warn: number
  online?: number
  className?: string
  title?: string | null
}) {
  if (!total || total <= 0) return null
  const c = Math.max(0, crit)
  const w = Math.max(0, warn)
  const on = online == null ? total : Math.max(0, Math.min(online, total))
  const ok = Math.max(0, on - c - w)
  const off = Math.max(0, total - on)
  const pct = (n: number) => (n / total) * 100

  return (
    <span className={cn("wisp-onubar", className)}
      title={title === null ? undefined
        : title ?? `${total} ONUs · ${c} critical · ${w} warning · ${ok} ok · ${off} offline`}
      aria-hidden>
      {c > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--crit" style={{ width: `${pct(c)}%` }} />}
      {w > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--warn" style={{ width: `${pct(w)}%` }} />}
      {ok > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--ok" style={{ width: `${pct(ok)}%` }} />}
      {off > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--off" style={{ width: `${pct(off)}%` }} />}
    </span>
  )
}

export function OnuHealth({ total, crit, warn, online, onClick, className }: {
  total: number
  crit: number
  warn: number
  online?: number
  onClick?: (e: React.MouseEvent) => void
  className?: string
}) {
  if (!total || total <= 0) return null
  const readout = crit > 0
    ? { n: crit, word: "crit", cls: "text-destructive" }
    : warn > 0 ? { n: warn, word: "weak", cls: "text-warning" }
    : null
  const off = online == null ? 0 : Math.max(0, total - online)
  return (
    <span
      onClick={onClick}
      title={`${total} ONUs · ${crit} critical · ${warn} weak · ${off} offline`
        + (onClick ? ". Click for optics" : "")}
      className={cn("inline-flex shrink-0 items-center gap-1.5",
        onClick && "cursor-pointer hover:brightness-125", className)}
    >
      <OnuBar total={total} crit={crit} warn={warn} online={online} title={null} />
      {readout && (
        <span className={cn("font-mono text-2xs font-semibold tabular-nums", readout.cls)}>
          {readout.n} {readout.word}
        </span>
      )}
    </span>
  )
}
