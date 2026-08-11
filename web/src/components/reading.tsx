import type { ReactNode } from "react"
import { BellOff, Pause } from "lucide-react"
import { cn } from "@/lib/utils"
import { ago, isDownState, isFresh } from "@/lib/format"

export type ReadingState = "current" | "stale" | "frozen" | "absent" | "suppressed"

export function readingState(o: {
  value: number | string | null | undefined
  at?: string | null
  deviceState?: string | null
  suppressed?: boolean
  freshWithinS?: number
}): ReadingState {
  if (o.value == null || o.value === "") return "absent"
  if (isDownState(o.deviceState)) return "frozen"
  if (o.suppressed) return "suppressed"
  if (o.at != null && !isFresh(o.at, o.freshWithinS)) return "stale"
  return "current"
}

export function Reading({
  value, unit, state, at, showAge = false, reason, tone, className, mono = true,
}: {
  value: ReactNode
  unit?: string
  state: ReadingState
  at?: string | null
  showAge?: boolean
  reason?: string
  tone?: "warning" | "destructive"
  className?: string
  mono?: boolean
}) {
  const base = cn(
    "inline-flex items-baseline gap-1 whitespace-nowrap",
    mono && "font-mono",
    className,
  )

  if (state === "absent") {
    return (
      <span className={cn(base, "text-faint-foreground")} title={reason}>
        <span className="wisp-deadzone" aria-hidden />
        <span className="sr-only">{reason ?? "not measured"}</span>
      </span>
    )
  }

  const toned = state !== "frozen"
  return (
    <span className={cn(
      base,
      state === "frozen" && "text-faint-foreground",
      state === "stale" && "wisp-reading--stale",
      toned && tone === "warning" && "text-warning",
      toned && tone === "destructive" && "text-destructive",
      !toned && "opacity-90",
    )} title={reason}>
      {state === "frozen" && <Pause className="size-3 shrink-0 self-center" aria-hidden />}
      {state === "suppressed" && (
        <BellOff className="size-3 shrink-0 self-center text-faint-foreground" aria-hidden />
      )}
      <span className={cn(state === "stale" && "wisp-reading__v")}>{value}</span>
      {unit && <span className="text-2xs font-normal opacity-70">{unit}</span>}
      {state === "stale" && showAge && at && (
        <span className="text-2xs font-normal text-faint-foreground">{ago(at)}</span>
      )}
      {state === "frozen" && reason && (
        <span className="text-2xs font-normal text-faint-foreground">{reason}</span>
      )}
    </span>
  )
}
