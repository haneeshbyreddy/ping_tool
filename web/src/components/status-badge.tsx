import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { stateTone } from "@/lib/format"

// One formula for every status surface: the tone's own color for text, the same
// hue at 13% for fill and 30% for edge. Because all four tones share it, a chip
// reads as "a status" before you've read the word — shape carries the category,
// color carries the severity.
const TONE_CLASS: Record<string, string> = {
  success: "border-success/30 bg-success-soft text-success",
  warning: "border-warning/30 bg-warning-soft text-warning",
  destructive: "border-destructive/30 bg-destructive-soft text-destructive",
  info: "border-primary/30 bg-primary-soft text-primary",
  muted: "border-border bg-foreground/[0.04] text-muted-foreground",
}

export type Tone = keyof typeof TONE_CLASS

export function StatusDot({ tone }: { tone: Tone }) {
  const dotClass: Record<string, string> = {
    success: "bg-success", warning: "bg-warning", destructive: "bg-destructive",
    info: "bg-primary", muted: "bg-faint-foreground",
  }
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", dotClass[tone])} />
}

/** Squared status chip — the table/inline variant. Rounded-md (not a pill) so it
 *  sits square against the mono columns it labels; pills belong on free-flowing
 *  text where nothing has to line up. */
export function Chip({ tone, children, className }: {
  tone: Tone
  children: ReactNode
  className?: string
}) {
  return (
    <span className={cn(
      "inline-flex h-5 w-fit shrink-0 items-center gap-1.5 rounded-md border px-2 text-2xs font-medium whitespace-nowrap",
      TONE_CLASS[tone],
      className,
    )}>
      {children}
    </span>
  )
}

export function StateBadge({ state, label }: { state: string | null | undefined; label?: string }) {
  const tone = stateTone(state)
  return (
    <span className={cn(
      "inline-flex items-center gap-1.5 rounded-4xl border px-2 py-0.5 text-2xs font-semibold capitalize",
      TONE_CLASS[tone],
    )}>
      <StatusDot tone={tone} />
      {label ?? (state ? state.toLowerCase() : "unknown")}
    </span>
  )
}

export function TonePill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={cn(
      "inline-flex items-center gap-1.5 rounded-4xl border px-2 py-0.5 text-2xs font-semibold whitespace-nowrap",
      TONE_CLASS[tone],
    )}>
      {children}
    </span>
  )
}
