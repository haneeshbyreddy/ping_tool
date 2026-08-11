import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { stateTone } from "@/lib/format"
import { PLANE_LABEL, PLANE_VAR, type Plane } from "@/lib/planes"

export const TONE_CLASS: Record<string, string> = {
  success: "border-success/30 bg-success-soft text-success",
  warning: "border-warning/30 bg-warning-soft text-warning",
  destructive: "border-destructive/30 bg-destructive-soft text-destructive",
  info: "border-primary/30 bg-primary-soft text-primary",
  muted: "border-border bg-foreground/[0.04] text-muted-foreground",
}

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

export function PlaneChip({ plane, label, className }: {
  plane: Plane
  label?: ReactNode
  className?: string
}) {
  return (
    <span className={cn(
      CHIP_BOX, "border-border bg-foreground/[0.04] text-muted-foreground",
      className,
    )}>
      <PlaneDot plane={plane} />
      {label ?? PLANE_LABEL[plane]}
    </span>
  )
}

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
