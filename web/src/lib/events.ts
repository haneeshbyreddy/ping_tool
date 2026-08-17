import type { LogEvent } from "./types"
import { stateTone } from "./format"

// A chip is one column wide, so a label here is a SHORT WORD, never the event
// name. Assignment shipped without entries, so the Logs row fell through to the
// raw `OUTAGE_ACCEPTED` and overflowed its column onto the Event text beside it.
export const TYPE_LABEL: Record<string, string> = {
  OUTAGE_OPENED: "Outage",
  OUTAGE_ASSIGNED: "Assigned",
  OUTAGE_ACCEPTED: "Accepted",
  OUTAGE_ACKNOWLEDGED: "Acknowledged",
  OUTAGE_RESOLVED: "Resolved",
  OUTAGE_POSTMORTEM: "Post-mortem",
}

export function eventTone(ev: LogEvent): "success" | "warning" | "destructive" | "info" | "muted" {
  switch (ev.type) {
    case "OUTAGE_OPENED": return stateTone(ev.state) === "warning" ? "warning" : "destructive"
    case "OUTAGE_RESOLVED": return "success"
    // `info` means a human OWNS a still-live incident. Accepting is that
    // answer (it stamps the ack); ASSIGNING is only the owner asking, and
    // toning it the same would read as "somebody is on it" before anyone
    // replied — the one claim the outage card is careful not to make either.
    case "OUTAGE_ACCEPTED":
    case "OUTAGE_ACKNOWLEDGED": return "info"
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
    case "OUTAGE_ASSIGNED": {
      const to = (p.to as string[] | undefined)?.filter(Boolean) ?? []
      const who = to.length ? to.join(", ") : "somebody"
      return `Assigned to ${who}${p.by ? ` by ${p.by as string}` : ""}`
    }
    case "OUTAGE_ACCEPTED":
      return `Accepted by ${(p.by as string) || "an assignee"}`
    case "OUTAGE_RESOLVED":
      return `Recovered from ${ev.state ?? "outage"}`
    case "OUTAGE_POSTMORTEM": {
      const cause = (p.root_cause as string) || "no cause given"
      const notes = p.resolution_notes as string | undefined
      return notes ? `${cause}: ${notes}` : cause
    }
    default:
      return ev.state ? `${ev.type} · ${ev.state}` : ev.type
  }
}
