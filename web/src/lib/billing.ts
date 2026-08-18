import type { Accrual, BillingStage } from "./types"



const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"]

export function monthLabel(month: string): string {
  return `${MONTHS[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`
}


// ------------------------------------------------------------------- money
// Everything on the wire is INTEGER PAISE. These three are the ONLY places
// paise become rupees; no surface may divide by 100 on its own or the
// rounding drifts between the hero, the chart and the invoice table.

/** Whole rupees, Indian digit grouping. The headline form. */
export function inr(paise: number): string {
  return `₹${Math.round(paise / 100).toLocaleString("en-IN")}`
}

/** Rupees and paise, for a line item where the remainder is the point (a
 *  daily accrual is a monthly figure divided by ~30 and is rarely whole). */
export function inrExact(paise: number): string {
  const sign = paise < 0 ? "-" : ""
  const abs = Math.abs(Math.round(paise))
  const rupees = Math.floor(abs / 100).toLocaleString("en-IN")
  return `${sign}₹${rupees}.${String(abs % 100).padStart(2, "0")}`
}

/** Whole rupees when the amount IS whole, else rupees and paise. Use where a
 *  column mixes both and a trailing ".00" would be noise. */
export function inrAuto(paise: number): string {
  return Math.abs(paise) % 100 === 0 ? inr(paise) : inrExact(paise)
}

/** Whole rupees with the sign OUTSIDE the symbol: -₹1,200, never ₹-1,200.
 *  Credit is a negative balance and reads as one at a glance, which is the
 *  whole point of showing it signed rather than as the word "credit" plus a
 *  positive figure. Rounds through inr() rather than dividing again, so a
 *  credit and a debt of the same size can never round to different rupees. */
export function inrSigned(paise: number): string {
  return paise < 0 ? `-${inr(Math.abs(paise))}` : inr(paise)
}

// ------------------------------------------------------------------- source

/** How an ONU count is labelled and whether it must LOOK estimated.
 *  `reading` maps onto the <Reading> grammar: a held count is stale by
 *  definition, and no count at all gets the dead zone rather than a zero.
 *
 *  Takes a plain string, not just ConnSource, so a row the retired RADIUS
 *  ladder wrote still says what it was charged on. "Billed on a basis we no
 *  longer use" and "we could not count you" are opposite findings and must
 *  never collapse into the same cell. */
export function connSourceMeta(source: string | null | undefined): {
  label: string
  detail: string
  reading: "current" | "stale" | "absent"
} {
  switch (source) {
    case "onu":
      return {
        label: "ONU roster", reading: "current",
        detail: "Subscriber ONUs seen online in the last 7 days.",
      }
    case "held":
      return {
        label: "Held", reading: "stale",
        detail: "The last good count. Today's walk did not arrive.",
      }
    case "none":
    case null:
    case undefined:
    case "":
      return {
        label: "No source", reading: "absent",
        detail: "No OLT reported a roster we could count.",
      }
    default: {
      // The pre-2026-08-17 basis. Kept readable so an old row renders as what
      // it was measured on, never as "no source" — those are opposite claims.
      const retired: Record<string, string> = {
        radius: "RADIUS (retired)",
        declared: "Declared (retired)",
      }
      return {
        label: retired[source] ?? "Unknown source", reading: "stale",
        detail: "Counted on a basis this install no longer bills on.",
      }
    }
  }
}

/** The one thing that happened to a day's count, or null. Ranked by how far
 *  each one moves a bill: a re-price outranks a downgrade outranks a hold
 *  outranks a backfill. */
export function accrualFlagNote(a: Accrual | null | undefined): string | null {
  const f = a?.flags
  if (!f) return null
  if (f.repriced) {
    const from = connSourceMeta(f.repriced.from).label
    return `Re-priced on ${f.repriced.on} onto the ONU count. Was counted from ${from}.`
  }
  if (f.downgraded) {
    // With one measuring rung the only downgrade is off it, and what caught
    // the day is the device floor. Say that rather than naming a rung the
    // reader has never seen.
    return f.downgraded.to === "none"
      ? "No roster answered. The device floor set this day."
      : `Counted from ${connSourceMeta(f.downgraded.to).label} instead of `
        + `${connSourceMeta(f.downgraded.from).label}.`
  }
  if (f.source_changed) {
    return `Count moved from ${connSourceMeta(f.source_changed.from).label} `
      + `to ${connSourceMeta(f.source_changed.to).label}.`
  }
  if (f.held) return "Held from the last good roster walk."
  if (f.backfilled) return "Filled in after a gap. Counts carried forward."
  return null
}

// -------------------------------------------------------------- the ladder

/** The stage chip. Status tones only: billing IS an alarm axis when it locks,
 *  and a neutral tone on a locked account would understate it. */
export function stageMeta(stage: BillingStage, daysOverdue: number): {
  label: string
  className: string
} {
  switch (stage) {
    case "deactivated":
      return { label: "Deactivated", className: "bg-destructive/10 text-destructive" }
    case "locked":
      return {
        label: `Overdue ${daysOverdue}d`,
        className: "bg-destructive/10 text-destructive",
      }
    case "banner":
      return { label: "Payment due", className: "bg-warning-soft text-warning" }
    case "exempt":
      return { label: "Not billed", className: "bg-muted text-muted-foreground" }
    default:
      return { label: "Up to date", className: "bg-success-soft text-success" }
  }
}

/** A date the operator reads, in their own words. Display only. */
export function dayLabel(day: string): string {
  const [y, m, d] = day.split("-").map(Number)
  if (!y || !m || !d) return day
  return `${d} ${MONTHS[m - 1].slice(0, 3)}`
}
