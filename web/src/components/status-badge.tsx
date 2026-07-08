import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { stateTone } from "@/lib/format"

const TONE_CLASS: Record<string, string> = {
  success: "border-success/30 bg-success-soft text-success",
  warning: "border-warning/30 bg-warning-soft text-warning",
  destructive: "border-destructive/30 bg-destructive-soft text-destructive",
  muted: "border-border bg-muted text-muted-foreground",
}

export function StatusDot({ tone }: { tone: keyof typeof TONE_CLASS }) {
  const dotClass: Record<string, string> = {
    success: "bg-success", warning: "bg-warning", destructive: "bg-destructive", muted: "bg-muted-foreground",
  }
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", dotClass[tone])} />
}

export function StateBadge({ state, label }: { state: string | null | undefined; label?: string }) {
  const tone = stateTone(state)
  return (
    <span className={cn(
      "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[0.75rem] font-semibold capitalize",
      TONE_CLASS[tone],
    )}>
      <StatusDot tone={tone} />
      {label ?? (state ? state.toLowerCase() : "unknown")}
    </span>
  )
}

export function TonePill({ tone, children }: { tone: keyof typeof TONE_CLASS; children: ReactNode }) {
  return (
    <span className={cn(
      "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[0.75rem] font-semibold whitespace-nowrap",
      TONE_CLASS[tone],
    )}>
      {children}
    </span>
  )
}
