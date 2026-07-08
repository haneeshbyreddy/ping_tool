import type { LogEvent } from "./types"
import { stateTone } from "./format"

export const TYPE_LABEL: Record<string, string> = {
  OUTAGE_OPENED: "Outage",
  OUTAGE_ACKNOWLEDGED: "Acknowledged",
  OUTAGE_RESOLVED: "Resolved",
  OUTAGE_POSTMORTEM: "Post-mortem",
}

export function eventTone(ev: LogEvent): "success" | "warning" | "destructive" | "muted" {
  switch (ev.type) {
    case "OUTAGE_OPENED": return stateTone(ev.state) === "warning" ? "warning" : "destructive"
    case "OUTAGE_RESOLVED": return "success"
    default: return "muted"
  }
}

export function describeEvent(ev: LogEvent): string {
  const p = ev.payload ?? {}
  switch (ev.type) {
    case "OUTAGE_OPENED":
      return `Went ${ev.state ?? "DOWN"}`
    case "OUTAGE_ACKNOWLEDGED":
      return `Acknowledged by ${(p.by as string) || "an operator"}`
    case "OUTAGE_RESOLVED":
      return `Recovered from ${ev.state ?? "outage"}`
    case "OUTAGE_POSTMORTEM": {
      const cause = (p.root_cause as string) || "no cause given"
      const notes = p.resolution_notes as string | undefined
      return notes ? `${cause} — ${notes}` : cause
    }
    default:
      return ev.state ? `${ev.type} · ${ev.state}` : ev.type
  }
}
