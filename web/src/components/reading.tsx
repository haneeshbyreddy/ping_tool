import type { ReactNode } from "react"
import { BellOff, Pause } from "lucide-react"
import { cn } from "@/lib/utils"
import { ago, isDownState, isFresh } from "@/lib/format"

/* ── <Reading> — one form for every number that can be UNCERTAIN ─────────────
 *
 * This product's most distinctive property is already written down in prose:
 * "nothing is wrong" and "nothing is measured" must never render alike; a
 * frozen reading must look frozen and say why; a stale walk prints nothing; a
 * dash is not a zero. Those rules were paid for in the field — a C-Data OLT
 * walks a COMPLETE roster with every rx_dbm NULL, so the optics badge went
 * green and crit/warn sat at 0 on a box measuring no light at all, which is
 * byte-identical to a healthy fleet.
 *
 * The rules existed. They had no consistent visual FORM — every screen
 * re-implemented them, which is how "nothing is measured" kept escaping as a
 * green zero. This is that form, in one place.
 *
 * THE SHAPE COMES FROM THE DOMAIN, NOT FROM TASTE. An OTDR has had a
 * first-class visual concept for "the instrument cannot measure here" for forty
 * years: the DEAD ZONE. It is not an absence of trace, it is a marked REGION on
 * the scale, drawn so a technician can see that the question was asked and
 * could not be answered. That is exactly what `absent` needs, and it is why
 * `absent` renders as a dash sitting ON A HAIRLINE TRACK occupying the same box
 * a real reading would, rather than as an em dash floating in a gap. A blank
 * cell says "nobody looked". A dead zone says "we looked and this instrument
 * cannot answer".
 *
 * FIVE STATES, each with its own form, and the whole point is that no two of
 * them can be mistaken for each other or for a healthy value:
 *
 *   current      full ink, tabular. The only one that claims to be true NOW.
 *   stale        the value, dotted underline. Still the last known truth —
 *                which is exactly what a tech reads while driving to the site —
 *                but marked, because a dBm on screen otherwise carries no date.
 *                It KEEPS its status tone: staleness is a fact about the walk,
 *                not about the fibre, and dropping the tone would hide which
 *                PON was worst on every OLT whose sweep is behind.
 *   frozen       desaturated + a pause glyph. The box it came from is
 *                unreachable, so this persisted rather than updated. ALWAYS
 *                paired with a reason: grey with no explanation reads as a
 *                broken panel.
 *   absent       the dead zone. Never a zero, never a green, never a blank.
 *   suppressed   the value is true and current; only the PAGE is switched off
 *                by the notification governor. A struck bell, because a
 *                suppressed alert must never look like a suppressed FACT.
 *
 * EVERY STATE CARRIES A NON-COLOUR CHANNEL — a glyph, an underline, a track —
 * so the grammar survives greyscale, a colour-blind reader, and a screenshot
 * pasted into WhatsApp, which is how these actually travel.
 */

export type ReadingState = "current" | "stale" | "frozen" | "absent" | "suppressed"

/** Work out which of the five a reading is in, from the facts the row already
 *  carries. Deliberately ordered: DOWN beats STALE, because an unreachable box
 *  is proof the data is dead up to 15 minutes before staleness would notice —
 *  the same reason `isDownState` and not `isFresh` is the trigger everywhere
 *  else in this codebase. */
export function readingState(o: {
  value: number | string | null | undefined
  /** the stamp of the sweep that produced THIS value — never a sibling's */
  at?: string | null
  /** state of the device the reading came from */
  deviceState?: string | null
  /** the governor is not paging on this kind; the fact is still true */
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
  /** The stamp behind the value. DERIVES the state; it does not print.
   *
   *  A whole list of readings usually shares ONE walk, so dating each figure
   *  repeats the same four characters down the column — the first wiring of
   *  this put "7h ago" beside all fourteen PON readings in a panel whose header
   *  already said "stale · 7h ago" once. That is the same redundancy the Issues
   *  list gets criticised for, one level down. The DOTTED UNDERLINE is the
   *  per-reading mark; the AGE is said once, by whoever owns the walk. */
  at?: string | null
  /** Print the age beside the value. For a SOLITARY reading with nothing else
   *  to date it — a map card, a single KPI. Never in a column. */
  showAge?: boolean
  /** why it is frozen or absent. REQUIRED reading for those two states: the
   *  panel-level rule is that a greyed number always ships a live reason. */
  reason?: string
  /** a status tone, applied ONLY when the value is claimable. A `crit` grading
   *  on a number we cannot stand behind would paint the row red with nothing to
   *  explain it. */
  tone?: "warning" | "destructive"
  className?: string
  mono?: boolean
}) {
  const base = cn(
    "inline-flex items-baseline gap-1 whitespace-nowrap",
    mono && "font-mono",
    className,
  )

  // THE DEAD ZONE. A dash ON a track, sized to the box a real reading occupies,
  // so an unmeasurable field holds its place in the column instead of leaving a
  // hole that reads as "not loaded yet".
  if (state === "absent") {
    return (
      <span className={cn(base, "text-faint-foreground")} title={reason}>
        <span className="wisp-deadzone" aria-hidden />
        <span className="sr-only">{reason ?? "not measured"}</span>
      </span>
    )
  }

  // FROZEN is the only state that surrenders its tone, and that mirrors the
  // rule the rest of the codebase already follows: `.wisp-frozen` greys a
  // subtree when the device is DOWN and does NOT when a walk is merely stale.
  // An unreachable box's stored "crit" is not a claim anyone can stand behind;
  // a stale one's still is. Alarm STATE is untouched either way — this governs
  // the rendering only, never what pages.
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
