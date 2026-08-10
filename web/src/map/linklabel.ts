// Bandwidth labels riding the link lines: the port the operator bound to a link
// (device panel → Uplinks / Cross-links) supplies live in/out rates off the SNMP
// port walk.
//
// Bindings are keyed by the UNORDERED device pair, and each bound port is filed
// under the device that OWNS it. That one choice makes every link kind share a
// single lookup: a parent-side port (feeds_device_id), a child-side uplink and
// both ends of an undirected cross-link (uplink_device_id on each) all land on
// the same key, and callers ask for rates by naming which end they're looking
// from. No kind-specific bookkeeping, so a peer link can't fall through a gap.
//
// Icons go through cachedDivIcon (pins.ts discipline): useNow() re-renders every
// tick and an uncached icon would swap every label's DOM node per render.
import type L from "leaflet"
import { isFresh } from "@/lib/format"
import type { LinkPort } from "@/lib/types"
import { pointAlong } from "@/map/cut"
import { polyKm } from "@/map/geometry"
import { cableChipText, strandHex, strandLabel } from "@/lib/fiber"
import { cachedDivIcon, esc } from "@/map/pins"

/** the ports bound to one link, by the device each port belongs to */
export type LinkBinding = Map<number, LinkPort>

/** order-independent key: one cable is one entry, whichever end declared it */
export const linkKey = (x: number, y: number) =>
  x <= y ? `${x}:${y}` : `${y}:${x}`

/** fold the org-wide /link-ports rows into per-link bindings; on a LAG (several
    ports bound to one link) the lowest if_index carries the label */
export function bindLinkPorts(rows: LinkPort[]): Map<string, LinkBinding> {
  const m = new Map<string, LinkBinding>()
  const file = (own: number, other: number, p: LinkPort) => {
    const k = linkKey(own, other)
    let b = m.get(k)
    if (!b) { b = new Map(); m.set(k, b) }
    if (!b.has(own)) b.set(own, p)
  }
  for (const p of rows) {
    if (p.feeds_device_id != null) file(p.device_id, p.feeds_device_id, p)
    if (p.uplink_device_id != null) file(p.device_id, p.uplink_device_id, p)
  }
  return m
}

export const portLabel = (p: LinkPort) => p.if_name || `if${p.if_index}`

/** Below this a link is carrying nothing an operator would act on.
 *
 *  This threshold exists because of what an idle link used to print: "↓29 ↑171"
 *  — twenty-nine BITS per second — rendered at the same weight, in the same
 *  chip, as "↓3.7M ↑82k". Two chips a centimetre apart, one of them a busy
 *  gigabit-class trunk and one of them doing nothing, and the only way to tell
 *  which was which was to read every digit of both. A viewport of those is why
 *  the map stopped being scannable: nothing could be dismissed at a glance, so
 *  everything had to be read. */
export const IDLE_BPS = 1000

/** Is this link doing nothing in BOTH directions? Collapsing that case to one
 *  word is the whole point — an idle link should cost the eye a shape, not a
 *  number. */
export const bwIsIdle = (down: number | null, up: number | null) =>
  (down ?? 0) < IDLE_BPS && (up ?? 0) < IDLE_BPS

/** Chip text, KILOBIT-FLOORED on purpose.
 *
 *  k/M/G is the resolution an operator actually works at, so a sub-kilobit
 *  reading renders "0" rather than a three-digit number that LOOKS large beside
 *  "8k" — magnitude has to survive being skimmed, and raw b/s inverts it. The
 *  precise figure is not lost: the tooltip still carries full b/s, and the
 *  both-idle case never reaches here at all. */
export const fmtShort = (bps: number): string => {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)}G`
  if (bps >= 1e6) return `${bps >= 1e7 ? Math.round(bps / 1e6) : (bps / 1e6).toFixed(1)}M`
  if (bps >= IDLE_BPS) return `${Math.round(bps / 1e3)}k`
  return "0"
}

export const fmtFull = (bps: number | null): string => {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`
  return `${Math.round(bps)} b/s`
}

/** Live rates as seen looking FROM `fromId` TOWARD `toId`: `down` is traffic
    heading to `toId`. The reference end's own counters are preferred (its egress
    IS the link's forward direction); nulls when the walk went stale — a label
    must never show a weeks-old number as if it were now. */
export function linkRates(b: LinkBinding | undefined, fromId: number, toId: number):
  { down: number | null; up: number | null } {
  const from = b?.get(fromId)
  const to = b?.get(toId)
  const src = from ?? to
  if (!src || !isFresh(src.updated_at)) return { down: null, up: null }
  return from
    ? { down: from.out_bps, up: from.in_bps }
    : { down: to!.in_bps, up: to!.out_bps }
}

/** How hard this chip fights for its pixels when two of them collide.
 *
 *  TROUBLE OUTRANKS EVERYTHING and by a margin no rate can close — a toned chip
 *  is the only one making a claim about state rather than reporting reference
 *  data, so it must never be the one suppressed. Below that the busiest link
 *  wins, and an idle one loses to everything: if exactly one of two overlapping
 *  chips can be read, it should be the one with something to say. */
export function bwRank(
  b: LinkBinding | undefined, fromId: number, toId: number,
  cores?: number | null,
): number {
  const tone = b ? linkTone(b) : null
  if (tone === "down") return Number.MAX_SAFE_INTEGER
  if (tone === "warn") return Number.MAX_SAFE_INTEGER - 1
  const { down, up } = b ? linkRates(b, fromId, toId) : { down: null, up: null }
  const rate = Math.max(down ?? 0, up ?? 0)
  // A CABLE-ONLY CHIP RANKS BELOW EVERY LIVE RATE, and by fibre count within
  // itself. Two reasons it can't just share the rate scale: a cable record is
  // reference data that will not change this month, so when exactly one of two
  // colliding chips can be drawn it should be the one reporting NOW; and the
  // fleet's plant links (splitters) carry no bound port at all, so without a
  // floor of their own they would all rank 0 and be suppressed in arrival order
  // rather than by which cable matters. Fibre count is the right tiebreak among
  // them — a 48F backbone is the span you look for first.
  //
  // Negative so the whole family sits under an idle bound port: "this link is
  // doing nothing" is still a statement about the present.
  if (rate === 0 && !b) return -1_000_000 + (cores ?? 0)
  return rate
}

export function linkTone(b: LinkBinding): "down" | "warn" | null {
  const ports = [...b.values()]
  if (ports.some((p) => p.oper_status === "down" || (p.monitored === 1 && p.alarm === 1)))
    return "down"
  if (ports.some((p) => p.bw_alarm === 1 || p.bw_high_alarm === 1)) return "warn"
  return null
}

/** Where the chip sits on the RENDERED geometry (drawn route or chord), so it
    stays on the line even when the cable path snakes. `frac` is the operator's
    saved 0..1 position along that path; midpoint when they never moved it.

    Deliberately a FRACTION and not a coordinate: the line rubber-bands when
    either pin moves, and a saved lat/lng would drift off it the first time
    anyone corrected a location. */
export const linkLabelPos = (
  pts: Array<[number, number]>, frac?: number | null,
): [number, number] =>
  pointAlong(pts, polyKm(pts) * 1000 * (frac == null ? 0.5 : frac))

export function linkBwIcon(
  b: LinkBinding | undefined,
  from: { id: number; name: string }, to: { id: number; name: string },
  cable?: { cores: number | null; coreNo: number | null; name?: string | null },
): L.DivIcon | null {
  const cableText = cableChipText(cable?.cores, cable?.coreNo)
  // Nothing to say, no chip. A badge that exists only to announce an absence
  // spends the pixels a live reading would have used — the lesson `refHasChip`
  // already paid for on the subscriber layer, and the reason a link with
  // neither a bound port nor a recorded cable draws nothing at all.
  if (!b && !cableText) return null

  const { down, up } = b ? linkRates(b, from.id, to.id) : { down: null, up: null }
  const hasRates = down != null || up != null
  const ends = b
    ? [[from, b.get(from.id)] as const, [to, b.get(to.id)] as const]
      .filter(([, p]) => p)
      .map(([d, p]) => `${d.name} ${portLabel(p!)}`)
      .join(" ↔ ")
    : `${from.name} ↔ ${to.name}`
  // The cable half of the tooltip is where the strand gets NAMED. On the chip
  // it is a dot and a number, which is all that fits; a splicer needs the
  // colour in words, and past twelve fibres the tube as well — "core 25" alone
  // sends somebody to the wrong bundle.
  const cableTitle = cable?.cores || cable?.name
    ? [cable.name, cable.cores ? `${cable.cores}F` : null,
       cable.coreNo ? strandLabel(cable.coreNo, cable.cores)
         : cable.cores ? "strand not recorded" : null]
      .filter(Boolean).join(" · ")
    : null
  const rateTitle = !b ? null : hasRates
    ? `↓ ${fmtFull(down)} toward ${to.name} · ↑ ${fmtFull(up)}`
    : "no recent rate reading"
  const title = esc([ends, cableTitle, rateTitle].filter(Boolean).join(" · "))

  // arrows in their own span so CSS can quiet them: the rate is the data, the
  // ↓↑ is only which way it flows. fmtShort output is number-derived, so the
  // port name is the one branch that needs escaping.
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const idle = hasRates && bwIsIdle(down, up)
  const rateBody = !b ? ""
    : !hasRates
      ? `<span class="wisp-linkbw__port">${esc(portLabel([...b.values()][0]))}</span>`
      : idle
        // One word, not two zeroes: "↓0 ↑0" is still four glyphs of arithmetic
        // to dismiss, and the chip's job here is to be recognised and skipped.
        ? `<span class="wisp-linkbw__idle">idle</span>`
        : `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`

  // THE STRAND IS A DOT, NEVER THE TEXT AND NEVER THE LINE. The TIA-598
  // sequence contains red, orange, yellow and green — the hues this product
  // reserves for alarms — so a cable rendered in its strand colour is a
  // fabricated outage on the one screen that exists to show real ones. Neutral
  // text beside a coloured mark is the identity-chip grammar the two-colour-axes
  // pass settled, and it is exactly why an identity fact can carry a "red"
  // without ever being read as one.
  const strand = cable?.coreNo
    ? `<span class="wisp-strand" style="--strand:${strandHex(cable.coreNo)}"></span>`
    : ""
  const cableBody = cableText
    ? `${strand}<span class="wisp-linkbw__cable">${esc(cableText)}</span>`
    : ""
  // Cable first, rate second: what the span IS outranks what is flowing through
  // it right now for the purpose of finding it, and the rate is the half that
  // changes. Keeping the stable half leftmost means a chip doesn't reshuffle
  // under the eye every time the walk lands.
  const body = cableBody && rateBody
    ? `${cableBody}<span class="wisp-linkbw__sep"></span>${rateBody}`
    : cableBody || rateBody

  const tone = b ? linkTone(b) : null
  const cls = ["wisp-linkbw"]
  if (idle && !tone) cls.push("wisp-linkbw--idle")
  if (tone) cls.push(`wisp-linkbw--${tone}`)
  // The chip used to borrow the line's operator tint, which went with the tint
  // itself (2026-08-08). Telling near-parallel cables apart is now the CABLE's
  // job and it does it with words: the chip prints the count and the strand, and
  // the tooltip names the sheath. A stack of labels over two trunks is readable
  // because they say `24F·7` and `12F·3`, which is a fact about each rather than
  // a colour somebody had to remember the meaning of.
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${title}">${body}</div>`)
}
