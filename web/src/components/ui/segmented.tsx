import { cn } from "@/lib/utils"

/** Segmented control: a small, CLOSED set of mutually exclusive choices, all
 *  visible at once.
 *
 *  Use it over a dropdown when there are ≤5 options and the current one is worth
 *  seeing without a click (a log filter, a list/grid switch) — the whole point
 *  is that the choice and its alternatives cost one glance instead of one
 *  interaction. Past ~5 options it stops fitting and a Select is the honest
 *  control.
 *
 *  The track is a WELL (muted recesses below its surface) and the active thumb
 *  is the raised accent step, so selection reads as elevation rather than as
 *  color — which keeps the accent hue free for things that are actionable.
 */
export function Segmented<T extends string | null>({ value, options, onChange, className }: {
  value: T
  options: ReadonlyArray<{ value: T; label: React.ReactNode; title?: string }>
  onChange: (value: T) => void
  className?: string
}) {
  return (
    <div role="tablist"
      className={cn("inline-flex w-fit gap-0.5 rounded-lg border bg-muted p-[3px]", className)}>
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={String(o.value)}
            role="tab"
            type="button"
            aria-selected={active}
            title={o.title}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex h-7 items-center justify-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors",
              active
                ? "bg-accent text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
