import { cn } from "@/lib/utils"
import { SPLIT_INPUTS, SPLIT_RATIOS } from "@/lib/types"

export interface SplitRatio {
  ratio: number | null
  inputs: number | null
}

function Cell({ active, disabled, title, onClick, children, className }: {
  active: boolean
  disabled?: boolean
  title?: string
  onClick: () => void
  children: React.ReactNode
  className?: string
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 min-w-8 items-center justify-center rounded-md px-2",
        "font-mono text-xs tabular-nums transition-colors",
        "disabled:pointer-events-none disabled:opacity-40",
        active ? "bg-accent text-foreground shadow-[inset_0_0_0_1px_var(--border)]"
          : "text-muted-foreground hover:text-foreground",
        className)}>
      {children}
    </button>
  )
}

export function SplitRatioField({ value, onChange, disabled, className }: {
  value: SplitRatio
  onChange: (next: SplitRatio) => void
  disabled?: boolean
  className?: string
}) {
  const { ratio } = value
  const inputs = value.inputs ?? 1

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div className="flex w-fit items-center gap-1 rounded-lg border bg-muted p-[3px]">
        <div role="radiogroup" aria-label="Inputs" className="flex gap-0.5">
          {SPLIT_INPUTS.map((n) => (
            <Cell key={n}
              active={inputs === n}
              disabled={disabled || !ratio}
              title={!ratio
                ? "Pick how many ways it splits first"
                : n === 2
                  ? "Two feeds in — a protection-input splitter (2:N)"
                  : "One feed in — the ordinary splitter"}
              onClick={() => onChange({ ratio, inputs: n === 1 ? null : n })}>
              {n}
            </Cell>
          ))}
        </div>
        <span aria-hidden className="px-0.5 font-mono text-xs text-faint-foreground">:</span>
        <div role="radiogroup" aria-label="Ways it splits" className="flex gap-0.5">
          {SPLIT_RATIOS.map((r) => (
            <Cell key={r}
              active={ratio === r}
              disabled={disabled}
              title={`Splits ${r} ways`}
              onClick={() => onChange(ratio === r
                ? { ratio: null, inputs: null }
                : { ratio: r, inputs: value.inputs })}>
              {r}
            </Cell>
          ))}
        </div>
      </div>
      <p className="text-2xs leading-snug text-faint-foreground">
        {ratio
          ? inputs > 1
            ? <>A <span className="font-mono text-muted-foreground">{inputs}:{ratio}</span> — {ratio} legs,
              fed by two fibres. Click the ratio again to clear it.</>
            : <>A <span className="font-mono text-muted-foreground">1:{ratio}</span> — {ratio} legs.
              Click it again to clear.</>
          : "Not recorded. A box that only splices has no ratio, and an unknown one is better left blank than guessed."}
      </p>
    </div>
  )
}
