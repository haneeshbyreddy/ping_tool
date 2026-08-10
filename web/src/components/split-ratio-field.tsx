// The split-ratio control: ONE picker, four surfaces.
//
// A splitter used to be one number, so four screens each rendered their own
// dropdown of `1:2 / 1:4 / 1:8` and that was fine. It stopped being fine the day
// the ratio grew a second axis (operator, 2026-08-08: "splitter upto 1/16 and
// 2/16"), because a 2:16 is not another item in that list — it is a different
// question about the same box, and pasting eight combinations into four separate
// dropdowns is how the plant-create dialog and the device form end up offering
// different vocabularies of the same fact.
//
// So: one component, used by the splitter panel, the map's plant-create dialog,
// the Network device form and the field survey sheet.
//
// THE CONTROL SPELLS THE ANSWER. Inputs on the left, a literal ":" between, ways
// on the right — the thing on screen has the same shape as the thing written on
// the box's casing, which is where the operator is reading it from. That is the
// whole design: no toggle to discover, no jargon, nothing to translate. It is
// also why the two groups are NOT one nine-item list — nine items hides the fact
// that there are two questions, and "1:8" and "2:8" would sit next to each other
// looking like neighbouring sizes rather than the same size wired two ways.
//
// TWO REFUSALS, both mirroring the server exactly so this form cannot produce a
// 422:
//   * a second input needs a ratio first (a "2:?" names no product), so the
//     inputs group is disabled until one is picked, and says why;
//   * clearing the ratio clears the inputs with it, in the same write.
import { cn } from "@/lib/utils"
import { SPLIT_INPUTS, SPLIT_RATIOS } from "@/lib/types"

export interface SplitRatio {
  /** ways it splits; null = not recorded */
  ratio: number | null
  /** fibres feeding it; null reads as one (see SPLIT_INPUTS) */
  inputs: number | null
}

/** The shared cell. Deliberately not the `Segmented` primitive: this needs two
 *  groups reading as one expression, and two Segmenteds side by side would draw
 *  two separate wells with two separate borders — two controls, when the point
 *  is that it is one. */
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
  // null reads as one, everywhere — the column's whole contract. Reading it as
  // "unknown" here would draw every splitter recorded before this existed with
  // neither input selected, i.e. as an incomplete record of something nobody
  // got wrong.
  const inputs = value.inputs ?? 1

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div className="flex w-fit items-center gap-1 rounded-lg border bg-muted p-[3px]">
        <div role="radiogroup" aria-label="Inputs" className="flex gap-0.5">
          {SPLIT_INPUTS.map((n) => (
            <Cell key={n}
              active={inputs === n}
              // The server refuses "2 inputs, no ratio" and so does this: a
              // 2:? names no product anybody stocks. Disabled rather than
              // silently accepted-then-rejected, because a form that lets you
              // choose and then 422s is worse than one that says no up front.
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
        {/* The colon is the point of the whole layout: it makes the control
            read as the ratio being written, rather than as two settings that
            happen to be adjacent. Decorative, so it is hidden from the reader
            that would otherwise announce it between two radio groups. */}
        <span aria-hidden className="px-0.5 font-mono text-xs text-faint-foreground">:</span>
        <div role="radiogroup" aria-label="Ways it splits" className="flex gap-0.5">
          {SPLIT_RATIOS.map((r) => (
            <Cell key={r}
              active={ratio === r}
              disabled={disabled}
              title={`Splits ${r} ways`}
              // Re-clicking the selected ratio clears it, and clearing takes the
              // second input with it — the same single write the server would
              // enforce anyway, so the two can never disagree about what is
              // recorded. It also means "not recorded" needs no button of its
              // own competing with four real answers.
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
          // A closure that only splices genuinely has no ratio, so "nothing
          // picked" is a real answer and not a prompt to be nagged about.
          : "Not recorded. A box that only splices has no ratio, and an unknown one is better left blank than guessed."}
      </p>
    </div>
  )
}
