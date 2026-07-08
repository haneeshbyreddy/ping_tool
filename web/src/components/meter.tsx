import { cn } from "@/lib/utils"

/** Horizontal utilization meter — slim bar + right-aligned reading, tinted by
 * threshold (default 75/90, the central-server card's convention). `pct` drives
 * the fill; pass `value` when the reading isn't a percentage (e.g. "71°C"). */
export function Meter({ label, pct, value, detail, warn = 75, crit = 90 }: {
  label: string
  pct: number | null
  value?: string
  detail?: string
  warn?: number
  crit?: number
}) {
  const barTone = pct == null ? "bg-muted-foreground/40"
    : pct >= crit ? "bg-destructive"
    : pct >= warn ? "bg-warning"
    : "bg-primary/60"
  const textTone = pct != null && pct >= crit ? "text-destructive"
    : pct != null && pct >= warn ? "text-warning" : ""
  return (
    <div className="flex items-center gap-3">
      <span className="w-14 shrink-0 text-[0.75rem] font-medium text-muted-foreground">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full transition-[width] duration-500", barTone)}
          style={{ width: `${Math.min(100, Math.max(pct ?? 0, pct == null ? 0 : 2))}%` }} />
      </div>
      <span className={cn("w-12 shrink-0 text-right text-[0.75rem] font-semibold tabular-nums", textTone)}>
        {value ?? (pct == null ? "—" : `${pct.toFixed(0)}%`)}
      </span>
      <span className="hidden w-40 shrink-0 text-right text-[0.75rem] tabular-nums text-muted-foreground sm:block">
        {detail}
      </span>
    </div>
  )
}
