import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { stateTone } from "@/lib/format"
import { PLANE_LABEL, PLANE_VAR, type Plane } from "@/lib/planes"

// One formula for every status surface: the tone's own color for text, the same
// hue at 13% for fill and 30% for edge. Because all four tones share it, a chip
// reads as "a status" before you've read the word — shape carries the category,
// color carries the severity.
export const TONE_CLASS: Record<string, string> = {
  success: "border-success/30 bg-success-soft text-success",
  warning: "border-warning/30 bg-warning-soft text-warning",
  destructive: "border-destructive/30 bg-destructive-soft text-destructive",
  info: "border-primary/30 bg-primary-soft text-primary",
  muted: "border-border bg-foreground/[0.04] text-muted-foreground",
}

/** The chip BOX — height, radius, padding, type. Exported so the tree row's
 *  `RowTag` is literally the same object as `Chip` rather than a second chip
 *  grammar that drifts. It was one for months: `RowTag` shipped as an
 *  UPPERCASE tracking-wide semibold fill with no edge, which is the loudest
 *  type this system has, and it was spent equally on "7 FIBER CUTS" and on
 *  "MAINT" — so the loud style carried no information and the densest screen
 *  in the app read as a wall of alarm. */
export const CHIP_BOX =
  "inline-flex h-5 w-fit shrink-0 items-center gap-1.5 rounded-md border px-2 text-2xs font-medium whitespace-nowrap"

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
    <span className={cn(CHIP_BOX, TONE_CLASS[tone], className)}>
      {children}
    </span>
  )
}

/** IDENTITY chip — Axis B. Says what KIND of thing a row is about, never how it
 *  is doing.
 *
 *  STRUCTURALLY IDENTICAL to `Chip` above and deliberately so: same height, same
 *  radius, same padding, same type. Two chips sitting in one row must not look
 *  like two different design systems arguing.
 *
 *  IMPOSSIBLE TO CONFUSE WITH IT, and that is carried by WHERE the colour is,
 *  not by how much there is:
 *
 *      status chip     COLOURED TEXT + toned fill + toned edge
 *      identity chip   neutral text + neutral edge + ONE coloured dot
 *
 *  So the rule a reader learns without being told is: if the WRITING is
 *  coloured, something is wrong; if only the dot is, it is telling you what
 *  this is. One glance, no legend.
 *
 *  Identity NEVER colours text, and that is arithmetic rather than taste. The
 *  budget ceiling for an identity hue in light mode lands at 3.72:1 — below the
 *  4.5:1 AA floor for body text — because the ceiling is derived from the
 *  QUIETEST status tone on the screen and light mode's is only 4.65:1. A hue
 *  that cannot legally be read as text can still be a mark, where the 3:1
 *  data-vis floor applies. The constraint and the distinction turn out to be
 *  the same fact.
 *
 *  It is also QUIETER overall than a status chip, which is the correct ranking:
 *  identity is always present and never news. */
export function PlaneChip({ plane, label, className }: {
  plane: Plane
  /** Overrides the plane's own name — e.g. "PON EPON0/4" is still optical. */
  label?: ReactNode
  className?: string
}) {
  return (
    <span className={cn(
      // identical structure to Chip; only the colour PLACEMENT differs
      CHIP_BOX, "border-border bg-foreground/[0.04] text-muted-foreground",
      className,
    )}>
      <PlaneDot plane={plane} />
      {label ?? PLANE_LABEL[plane]}
    </span>
  )
}

/** The mark on its own, for places that already have a label — a tab, a section
 *  head, a table column. Sized to `StatusDot` so a row carrying both reads as
 *  one grammar rather than two.
 *
 *  An inline style rather than a class per plane: the value is DATA (which plane
 *  this is), exactly like `.wisp-tag` taking its colour from `--tag`. Five
 *  hardcoded classes would have to be kept in step with the token list by hand. */
export function PlaneDot({ plane, className }: { plane: Plane; className?: string }) {
  return (
    <span
      className={cn("inline-block size-2 shrink-0 rounded-full", className)}
      style={{ background: PLANE_VAR[plane] }}
      aria-hidden
    />
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
