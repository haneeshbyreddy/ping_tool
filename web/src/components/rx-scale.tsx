import { cn } from "@/lib/utils"

/* ── RxScale — optical power the way the instrument shows it ─────────────────
 *
 * An ISP tech reads received power on a SCALE with the pass/fail marks printed
 * on it, because the question is never "what is the number" — it is "how much
 * headroom is left before this drop needs a visit". A bare "-26.8 dBm" makes
 * that a subtraction the reader has to do against thresholds printed somewhere
 * else on the page, once, in small type.
 *
 * THE DOMAIN IS THE DECISION BOUNDARY, NOT THE OPTICAL RANGE, and that choice
 * is the whole design. A linear track over the real span of readings on this
 * fleet (about -7 to -30 dBm) would spend two thirds of its width on the region
 * where nothing is ever decided, and squeeze warn and crit — three dB apart —
 * into a few pixels at one end. So the track runs [crit - 3, warn + 3] and
 * anything comfortably healthy PEGS at the top.
 *
 * Pegging is not a limitation, it is what a power meter in pass/fail mode does:
 * once you are clear of the threshold, HOW clear stops being interesting. It
 * also makes the two cases a tech actually cares about instantly different — a
 * pegged mark means "fine", a mark sitting in the band means "this one is
 * close", and those used to be two similar-looking negative numbers.
 *
 * PER-OLT, NEVER GLOBAL. Thresholds are a property of the box (`optical_warn_
 * dbm` / `optical_crit_dbm`, falling back to the org default), so two OLTs can
 * legitimately grade the same dBm differently. The scale is therefore drawn
 * from the same pair `optics.py:_severity` grades against, and the ends are
 * LABELLED in the tooltip rather than implied — a spectrum analyser states its
 * reference level for the same reason: two readings may only be compared when
 * you can see they are on one scale.
 *
 * NO UPPER BOUND, deliberately. Too much light IS a fault — an over-driven ONU
 * near 0 dBm is a real failure mode, and PYLON has one at -2.87 — but this
 * product models no upper threshold: `_severity` only tests `rx <= crit` and
 * `rx <= warn`. Drawing a ceiling here would invent a rule the alarm path does
 * not have, and a scale that disagrees with what pages is worse than one that
 * is merely incomplete. The gap is real and belongs to Axis A, not to a
 * rendering component.
 *
 * It draws NOTHING without a reading (see `<Reading>`'s dead zone for that
 * case) and nothing without thresholds — a scale with no marks on it is a
 * decoration.
 */
export function RxScale({ rx, warn, crit, className }: {
  rx: number | null | undefined
  warn: number | null | undefined
  crit: number | null | undefined
  className?: string
}) {
  if (rx == null || warn == null || crit == null) return null
  // crit must sit below warn; a bad pair would invert the whole track, and
  // inventory.py already refuses to store one.
  if (!(crit < warn)) return null

  const lo = crit - 3
  const hi = warn + 3
  const span = hi - lo
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - lo) / span) * 100))

  const pegged = rx > hi
  const pos = pct(rx)
  const tone = rx <= crit ? "crit" : rx <= warn ? "warn" : "ok"

  return (
    <span
      className={cn("wisp-rxscale", `wisp-rxscale--${tone}`, className)}
      title={`${rx.toFixed(2)} dBm · warn ${warn} · crit ${crit} · scale ${lo} to ${hi} dBm`}
      aria-hidden
    >
      {/* the two bands that mean something. Everything right of warn is the
          track's own background — "ok" needs no fill, for the same reason a
          healthy thing on this map takes no colour. */}
      <span className="wisp-rxscale__band wisp-rxscale__band--crit"
        style={{ width: `${pct(crit)}%` }} />
      <span className="wisp-rxscale__band wisp-rxscale__band--warn"
        style={{ left: `${pct(crit)}%`, width: `${pct(warn) - pct(crit)}%` }} />
      <span className={cn("wisp-rxscale__mark", pegged && "wisp-rxscale__mark--peg")}
        style={{ left: `${pos}%` }} />
    </span>
  )
}
