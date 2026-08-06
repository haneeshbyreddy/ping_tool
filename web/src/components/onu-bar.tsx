import { cn } from "@/lib/utils"

/* ── OnuBar — the heat strip, at aggregate resolution ────────────────────────
 *
 * The PON heat strip in the device panel is this product's signature mark: the
 * one place a per-object state is drawn as a FIELD rather than as a list of
 * numbers, and the only thing on screen that reads like an instrument instead
 * of a dashboard. It should recur wherever ONU health is summarised.
 *
 * IT CANNOT RECUR LITERALLY, and that limit is worth stating rather than
 * papering over. The real strip draws one cell per ONU from `pon.onus[]`; a
 * Network tree row and an Issues group header carry only COUNTS
 * (`onus_crit` / `onus_warn` / `onus_online` / `onus_total`). So this is the
 * same grammar at the resolution the caller actually has: proportional segments
 * in the same order and the same tones, which answers "how much of this box is
 * in trouble" — a question the three bare numbers beside it genuinely do not.
 *
 * A PROPORTION, NOT A COUNT, is the point. "26 ONUs crit" reads the same on an
 * OLT with 30 subscribers and one with 600, and those are opposite situations.
 *
 * OFFLINE IS NOT AN ALARM COLOUR here. Hundreds of ONUs are dark every evening
 * on a real fleet; drawing that as destructive would make every OLT look
 * critical at 8pm. It takes the muted step, the same call the map makes for a
 * dark subscriber that is not a witness.
 *
 * It renders NOTHING without a total — an empty bar on a box with no roster
 * would be "nothing is measured" drawn as "nothing is wrong", which is the one
 * mistake this codebase keeps a whole section of rules about. */
export function OnuBar({ total, crit, warn, online, className, title }: {
  total: number
  crit: number
  warn: number
  /** online count; the ok share is whatever is online and not crit/warn */
  online?: number
  className?: string
  /** `null` suppresses the built-in tooltip — for a caller that wraps the bar
   *  and owns the hover text itself (OnuHealth). Nested titles resolve to the
   *  innermost, so without this the composed instrument would lose its own. */
  title?: string | null
}) {
  if (!total || total <= 0) return null
  const c = Math.max(0, crit)
  const w = Math.max(0, warn)
  const on = online == null ? total : Math.max(0, Math.min(online, total))
  const ok = Math.max(0, on - c - w)
  const off = Math.max(0, total - on)
  const pct = (n: number) => (n / total) * 100

  return (
    <span className={cn("wisp-onubar", className)}
      title={title === null ? undefined
        : title ?? `${total} ONUs · ${c} critical · ${w} warning · ${ok} ok · ${off} offline`}
      aria-hidden>
      {c > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--crit" style={{ width: `${pct(c)}%` }} />}
      {w > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--warn" style={{ width: `${pct(w)}%` }} />}
      {ok > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--ok" style={{ width: `${pct(ok)}%` }} />}
      {off > 0 && <span className="wisp-onubar__seg wisp-onubar__seg--off" style={{ width: `${pct(off)}%` }} />}
    </span>
  )
}

/* ── OnuHealth — the bar and its readout, as ONE object ──────────────────────
 *
 * A Network tree row used to spend THREE separate objects on one fact: a bare
 * `OnuBar`, and beside it a chip reading "17 ONUS CRIT", and (when the box also
 * had a mass-drop) "7 FIBER CUTS" — three peers at equal weight, two of them in
 * the same red, saying "this OLT's optics are in trouble" over and over. The
 * bar in particular had no anchor: 52px of unlabelled gradient floating between
 * two shouting blocks reads as decoration, not as a measurement.
 *
 * A METER HAS A READOUT ATTACHED TO IT. That is the whole idea — the same shape
 * `/issues`' GroupHead already uses (bar, then toned counts in mono) and the
 * same instrument-over-badge grammar as `<Reading>` and `RxScale`. Composed
 * here rather than at the call site so the row and the grid card cannot drift
 * into two readings of one measurement.
 *
 * THE READOUT NAMES THE WORST THING AND STOPS. Crit wins outright over warn —
 * not because warn stops mattering, but because the bar is ALREADY showing the
 * warn share as an amber segment, so a second number would restate what the
 * picture beside it says. The row is scanned, not read. */
export function OnuHealth({ total, crit, warn, online, onClick, className }: {
  total: number
  crit: number
  warn: number
  online?: number
  onClick?: (e: React.MouseEvent) => void
  /** The tree row passes `w-full justify-between` so the bar pins left and the
   *  readout pins right inside its fixed column — which gives the NUMBERS a
   *  column of their own too, instead of letting "4 crit" and "17 crit" start
   *  at different x. The grid card passes nothing and stays compact. */
  className?: string
}) {
  if (!total || total <= 0) return null
  const readout = crit > 0
    ? { n: crit, word: "crit", cls: "text-destructive" }
    : warn > 0 ? { n: warn, word: "weak", cls: "text-warning" }
    : null
  const off = online == null ? 0 : Math.max(0, total - online)
  return (
    <span
      onClick={onClick}
      title={`${total} ONUs · ${crit} critical · ${warn} weak · ${off} offline`
        + (onClick ? ". Click for optics" : "")}
      className={cn("inline-flex shrink-0 items-center gap-1.5",
        onClick && "cursor-pointer hover:brightness-125", className)}
    >
      <OnuBar total={total} crit={crit} warn={warn} online={online} title={null} />
      {readout && (
        <span className={cn("font-mono text-2xs font-semibold tabular-nums", readout.cls)}>
          {readout.n} {readout.word}
        </span>
      )}
    </span>
  )
}
