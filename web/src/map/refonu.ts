// Reference ONUs on the map: the handful of subscribers an operator has vouched
// for as reliably powered (`onu_places`). They are a SUBORDINATE layer, off by
// default — 90% of what hangs off a fleet's ports is an ONU, and a map that
// renders them all stops showing the plant it exists to show.
//
// Two rules this file exists to hold:
//
//   1. A reference ONU is drawn SMALLER and QUIETER than any device pin. It must
//      never be mistaken for infrastructure — but SHAPE stopped carrying that
//      (operator's ask, 2026-08-05: subscribers and splitters are location pins
//      now, because a teardrop is the one silhouette nobody has to learn). So
//      the other three channels carry it alone: TONE (a live drop is the
//      quietest fill on the map), STACKING (`refZIndex`, below every device pin)
//      and the CLUSTERING pass, which gear and plant join and subscribers don't.
//      A splitter's pin is bigger and has a HOLE; a device is a round dot.
//   2. Its state still carries the loudest thing this feature produces: a dark
//      reference ONU is evidence of a fiber cut, because power cannot explain
//      it. So a dark one goes destructive-toned while an online one stays
//      near-silent. It is still ranked BELOW a down device — a dark subscriber
//      is a clue about an outage, not the outage.
import type L from "leaflet"
import { cachedDivIcon, esc } from "@/map/pins"
import { bwIsIdle, fmtFull, fmtShort } from "@/map/linklabel"
import { isFresh, onuName, onuSev } from "@/lib/format"
import type { OnuPlace } from "@/lib/types"

/** What to CALL this subscriber on the map. `onuName` is the shared rule
 *  (`label || name || …`, mirroring `onuroster.display_name`) — the operator's
 *  own typed name beats whatever the OLT reports, which on the C-Data fleet is
 *  blank. The MAC is the last resort rather than part of that helper's ladder
 *  because it is the only field here guaranteed non-empty: an unnamed pin still
 *  has to be callable something. */
export const refName = (p: OnuPlace): string => onuName(p) || p.mac

/** Dark = the ONU left `online`, however the vendor said so. `dying_gasp` is
 *  dark on the map but is NOT witness evidence (it announced a power loss) —
 *  ponfault owns that distinction; here it just isn't drawn as healthy. */
export const isRefDark = (p: OnuPlace): boolean =>
  p.matched && p.state != null && p.state !== "online" && p.state !== "unknown"

/** A dark pin the map is allowed to SHOUT about — i.e. a WITNESS that has gone
 *  dark. This is the one predicate every piece of dark emphasis gates on, and
 *  the distinction it draws is the whole point of the two claims sharing a
 *  table: power cannot darken a subscriber the operator vouched for, so that pin
 *  is a fibre cut with a coordinate. **An ordinary located subscriber going
 *  offline is Tuesday** — thousands of them do it every evening, and drawing
 *  each one as an alarm is how a surveyed fleet turns its map into a wall of red
 *  that nobody can act on (operator's call, 2026-08-02: "for offline customers i
 *  don't want anything special treatment, only dot should be red thats it").
 *
 *  So an offline subscriber gets EXACTLY ONE signal — the destructive fill on
 *  its own mark — and nothing else: its drop line keeps the ordinary weight and
 *  colour, its name stays muted and stays behind the zoom floor, and it claims
 *  no priority in the chip budget. `isRefDark` still answers "is this ONU down"
 *  for counts, verdicts and the subscriber card; this answers the narrower
 *  question "may the map raise its voice", and only display code should ask it. */
export const isRefEvidence = (p: OnuPlace): boolean => p.witness && isRefDark(p)

export type RefTone = "dark" | "live" | "unknown"

export function refTone(p: OnuPlace): RefTone {
  if (!p.matched || p.state == null) return "unknown"
  if (isRefDark(p)) return "dark"
  return p.state === "online" ? "live" : "unknown"
}

/** What this pin CLAIMS. A reference ONU is evidence in a PON verdict; a located
 *  one is a coordinate a tech recorded while standing at a drop. They share a
 *  table and must not share a voice — calling a survey pin a "reference ONU"
 *  would tell an operator the fleet has witnesses it does not have. */
export const refKind = (p: OnuPlace): string =>
  p.witness ? "reference ONU" : "subscriber"

export function refTitle(p: OnuPlace): string {
  const who = refName(p)
  const kind = refKind(p)
  if (!p.matched) return `${who} · ${kind} · no longer in any roster`
  if (p.ambiguous) return `${who} · ${kind} · on ${p.slots} live slots`
  const where = p.device_name ? ` · ${p.device_name} PON ${p.pon_port ?? "?"}` : ""
  return `${who} · ${kind}${where} · ${p.state ?? "unknown"}`
}

export function refOnuIcon(p: OnuPlace, o: { selected: boolean; dim: boolean }) {
  const cls = ["wisp-refonu", `wisp-refonu--${refTone(p)}`]
  if (o.selected) cls.push("wisp-refonu--selected")
  if (o.dim) cls.push("wisp-refonu--dim")
  if (!p.matched) cls.push("wisp-refonu--orphan")
  // A located subscriber is plant record, not evidence: same mark, quieter, so
  // a fleet that geo-tags every drop can't drown the handful of witnesses in
  // pins that look exactly like them.
  if (!p.witness) cls.push("wisp-refonu--plain")
  // NO `title` — deliberately, and don't put one back. The hover CARD
  // (map/refhover.tsx) now answers everything the native tooltip did and more,
  // instantly and in the product's own type. Keeping both meant that holding
  // still over a customer eventually dropped an OS-styled black bar across the
  // map UNDER the pin, repeating the card's own header a second time in a
  // second visual language. `refTitle` survives for the search panel and the
  // name plate, which have no card of their own.
  return cachedDivIcon(
    `<div class="${cls.join(" ")}">`
    + `<span class="wisp-refonu__mark"></span></div>`)
}

/** Devices sit at 0–1000. A dark reference ONU is worth surfacing above a quiet
 *  one, but never above the gear whose outage it is a clue about.
 *
 *  The lift is for WITNESSES only. A dark witness is a fibre cut with a
 *  coordinate; a dark located subscriber is a subscriber who is offline, which
 *  is ordinary — promoting those would make every evening's churn look like
 *  evidence.
 *
 *  The HOVERED one comes to the front of the layer, above even the selected
 *  pin. Not decoration: subscriber marks are deliberately out of the clustering
 *  pass, so on a surveyed street they genuinely overlap — a real pair measured
 *  10.8px apart at z17 with 13px marks. Without the lift, a pixel of pointer
 *  jitter inside one mark can cross into a neighbour that happens to stack
 *  higher, and the card flickers between two customers. Lifting whatever the
 *  pointer is on makes the first one you land on the one you keep.
 *
 *  It rides `zIndexOffset`, a Marker prop Leaflet applies with a single style
 *  write — NOT the icon html, which is cached by string and would remount the
 *  diamond (replaying its fade-in) on every pointer crossing.
 *
 *  Still deep in negative territory: the whole layer stays below every device
 *  pin, hover included. A subscriber may never cover the gear. */
export function refZIndex(p: OnuPlace, selected: boolean, hovered = false): number {
  if (hovered) return -25
  if (selected) return -50
  return isRefEvidence(p) ? -100 : -200
}

// ---------------------------------------------------------------------------
// The customer name beside the mark.
//
// Until this existed the name a worker typed while standing at the drop lived
// only in the hover title and the subscriber card — so a surveyed street read
// as a field of anonymous diamonds, and answering "which of these is the
// complaint" meant clicking them one at a time. That is the survey's whole
// output being invisible on the one screen it was captured for.
//
// It is a SEPARATE marker rather than a span inside `refOnuIcon`, and that is
// load-bearing: icons are cached by their html string, so folding a name that
// the collision budget can turn on and off into the mark's html would swap the
// diamond's DOM node every time panning changed the budget — remounting it,
// replaying `wisp-mark-in`, and making the layer flicker while it is being
// read. The rate chip is a separate marker for the same class of reason. The
// device pin's label can afford to live inside its icon because it is gated by
// zoom ALONE, through a CSS class on the wrapper, and never per pin.
// ---------------------------------------------------------------------------

/** How far below the COORDINATE the name's centre sits, in screen px — the 3px
 *  translate in `.wisp-refonu-name` plus half its ~18px line box.
 *
 *  Exported because the collision budget has to reserve pixels where the TEXT
 *  lands, not where the mark does: two marks a mark's-height apart have names
 *  that overlap, and a budget measuring the wrong row would report that viewport
 *  clear. Keep it in step with the CSS.
 *
 *  It dropped from 21 to 12 when the mark became a location pin: the old diamond
 *  was centred on the coordinate and the name had to clear its lower half, while
 *  a pin stands on the coordinate with nothing below it. */
export const REF_NAME_DY = 12

/** Is this subscriber's Rx worth PRINTING right now?
 *
 *  Three refusals, and each is a documented way this product has rendered a lie
 *  before:
 *
 *  · **No reading at all.** Most of the C-Data/DBC fleet walks a complete roster
 *    with every `rx_dbm` NULL — the firmware publishes none, and the web scrape
 *    fills it in only where a recipe and a credential exist. "Nothing is wrong"
 *    and "nothing is measured" must never render alike, so a missing reading
 *    prints NOTHING here rather than a dash: on a map chip there is nowhere to
 *    explain a dash, and the card and the Optical tab both do explain it.
 *  · **A stale walk.** A dBm on screen carries no date — the whole reason
 *    `RxFreshness` exists on the panel. There is nowhere to put a date on a map
 *    label, so past the staleness gate the number is simply not printed.
 *  · **A dark ONU.** Its stored Rx is whatever the last successful walk saw,
 *    which is by definition not now. The mark already went destructive-toned and
 *    its name went to full weight; a last-gasp light level beside that reads as
 *    a live measurement of a subscriber who is off. */
export function refHasRx(p: OnuPlace): boolean {
  return p.rx_dbm != null && p.state === "online" && isFresh(p.optics_updated_at)
}

export function refNameIcon(p: OnuPlace, o: { frozen: boolean }): L.DivIcon {
  // A dark WITNESS's name is the one worth reading across a viewport, so it
  // takes the weight — same exemption `.wisp-map-lowzoom` makes for a device
  // pin in trouble. Everything else stays reference material, INCLUDING an
  // ordinary offline subscriber: this is the most numerous piece of text on a
  // surveyed map and not one of them is news.
  const cls = ["wisp-refonu-name"]
  if (isRefEvidence(p)) cls.push("wisp-refonu-name--dark")
  if (!p.matched) cls.push("wisp-refonu-name--orphan")
  // `frozen` = its OLT is DOWN, so every SNMP reading behind it stopped being a
  // claim about now up to 15 minutes before staleness would notice. The panel
  // greys the readings and says why; a map label has no room for the "why", so
  // it drops the number and keeps the name. The ICMP outage on the OLT's own
  // pin is the live explanation, and that one is at full strength.
  const showRx = !o.frozen && refHasRx(p)
  const rx = showRx
    ? `<span class="wisp-refonu-name__rx wisp-refonu-name__rx--${onuSev(p)}">`
      + `${(p.rx_dbm as number).toFixed(1)}</span>`
    : ""
  // The unit lives in the TITLE, not the label: "-24.3" beside a customer name
  // is unambiguous to anyone reading a fibre map, and four more characters on
  // the most numerous piece of text here costs budget every other chip wanted.
  //
  // A tooltip HAS room for the reason a number is missing, and the frozen rule
  // says to pair the suppression with a live one — "the readings stopped" reads
  // as a broken panel unless something says why. The other two refusals are
  // already legible from the row itself (a dark ONU, an unmatched pin), so only
  // this one needs saying.
  const title = esc(showRx
    ? `${refTitle(p)} · Rx ${(p.rx_dbm as number).toFixed(2)} dBm`
    : o.frozen ? `${refTitle(p)} · readings frozen · its OLT is down`
    : refTitle(p))
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${title}">`
    + `${esc(refName(p))}${rx}</div>`)
}

// ---------------------------------------------------------------------------
// The line back to the OLT, and the rate riding it.
//
// DOTTED, and that is not decoration: every other line on this map is either a
// drawn cable route or the chord standing in for one, i.e. a claim about plant.
// This one is a LOGICAL association — "this subscriber hangs off that OLT" —
// with no surveyed path behind it. A solid line would read as fibre we have
// traced, and a splicing crew quotes drum off lines that look like that.
// ---------------------------------------------------------------------------

/** Dash pattern for the ONU→OLT association line.
 *
 *  SVG dash lengths are absolute px, NOT multiples of the stroke — so widening
 *  the line without opening the gaps turns a dotted span into a solid one, and
 *  "solid" is the one thing this line may never say. At weight 2 with a round
 *  cap a "1" dash paints a 2.5px dot, so the gap has to carry the stroke width
 *  on top of the spacing it wants. This sits at an 11px period: sparse dots,
 *  clearly apart from the cross-link's short dashes ("1.5 7") and the backup's
 *  long ones ("5 8"), and sparser than the drop dash it must stay
 *  distinguishable from (`DROP_DASH`). */
export const REF_DASH = "1 10"

/** Extra stroke weight while the cursor is on this subscriber's mark. The
 *  hovered line also goes SOLID (the caller drops the dash), which is a
 *  deliberate, bounded exception to everything the paragraph above says — so
 *  the reason it is safe has to live right here beside the rule it bends.
 *
 *  What the dash protects against is a RESTING map that looks surveyed: a line
 *  somebody reads across the room, screenshots, or quotes drum off believing it
 *  traces plant. A hover is none of those. It exists only while the pointer is
 *  held on one 15px pin, exactly one line on the map is solid at a time, it
 *  cannot survive into a wall display, and it is NARRATED — the card that comes
 *  with it names the span in words ("Drop · SPL-MARKET-4", or "Drop · not
 *  recorded"). The resting map says "no surveyed path" silently through the
 *  dash; the hovered one says it out loud, which is the stronger claim.
 *
 *  The line is still not a measurement and must never become one: the hover
 *  DISTANCE readout (map/linkhover.tsx) deliberately probes drawn topology only
 *  and is suppressed outright while a subscriber is hovered, so nothing on this
 *  span can be quoted as cable length. Don't extend it here. */
export const REF_HOVER_BOOST = 1.5

// Formatting is IMPORTED, not re-declared. These were two copies of the same
// four lines and they had already started to matter: the kilobit floor and the
// idle collapse have to land on a subscriber's rate and a trunk's rate
// identically, or the map teaches two different readings of the same chip.

/** Is this reference ONU's interface reading usable NOW?
 *
 *  A stale port walk must not print a weeks-old rate as if it were live — the
 *  same rule `linkRates` keeps for link chips. HILL-OLT-1 is the live example
 *  of why: its roster matches 227 interfaces and only 33 carry a counter,
 *  because that box's port walk has been failing. "No reading" is the honest
 *  render there, and it is a DIFFERENT statement from "0 Mb/s". */
export function refHasRate(p: OnuPlace): boolean {
  if (p.in_bps == null && p.out_bps == null) return false
  return isFresh(p.port_updated_at)
}

/** Tone of the line. Follows the OPTICAL ROSTER, the same source the pin uses —
 *  pin and line contradicting each other on a wall map is worse than either
 *  being wrong. `port_state` rides a different clock and is a second opinion
 *  only; it colours nothing.
 *
 *  Gated on `isRefEvidence`, not bare `isRefDark`: an ordinary subscriber going
 *  offline gets the red fill on its mark and NOTHING else, so its drop line
 *  keeps the ordinary weight and colour. Only a dark WITNESS reddens its line,
 *  because only that one is evidence of a cut. */
export function refLineTone(p: OnuPlace): "dark" | "quiet" {
  return isRefEvidence(p) ? "dark" : "quiet"
}

/** Has this drop line anything to put in a chip?
 *
 *  Three things used to reach it and only two of them said something: a rate (or
 *  `idle`), the word `dark`, and — on most of this fleet — the words "no rate".
 *  That third one is gone (operator's call, 2026-08-05), and it was the COMMON
 *  case rather than an edge: `onu_if_token` matches nothing at all on the GPON
 *  builds (Gpon_04/Gpon_08/TMG/SRPL/NLK, measured 2026-07-28), so a surveyed
 *  street drew a chip on every single drop line to announce an absence.
 *
 *  Nothing is lost by dropping it, because the chip could never have said WHICH
 *  absence it was — "this firmware publishes no per-ONU interface", "the port
 *  walk is stale" and "0 Mb/s" are three different sentences and a badge has
 *  room for none of them. The hover card spells out which one it is, in words,
 *  and that is where that distinction already lived.
 *
 *  The BUDGET reads this too, not just the render: a chip nobody draws must not
 *  reserve its box in the shared screen-space reservation, or an absence goes on
 *  suppressing a live reading and a customer name that would have drawn. */
export function refHasChip(p: OnuPlace): boolean {
  return isRefDark(p) || refHasRate(p)
}

export function refBwIcon(p: OnuPlace): L.DivIcon | null {
  // Null, not an empty chip: the MARKER must not render at all, or the plate's
  // own border draws a blank pill on the line — which is the thing being
  // removed, minus the word.
  if (!refHasChip(p)) return null
  const hasRate = refHasRate(p)
  const dark = isRefDark(p)
  const who = refName(p)
  const port = p.if_name ? ` · ${p.if_name.split(" ")[0]}` : ""
  // ↓ is traffic toward the subscriber, which is the OLT interface's EGRESS.
  const down = p.out_bps
  const up = p.in_bps
  // Two branches only, and by construction: past the gate above, an ONU that is
  // not dark has a live reading.
  const title = esc(
    dark ? `${who}${port} · dark · power can't explain this on a reference ONU`
    : `${who}${port} · ↓ ${fmtFull(down)} to subscriber · ↑ ${fmtFull(up)}`)
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const idle = !dark && hasRate && bwIsIdle(down, up)
  const body = dark
    ? "dark"
    : idle
      // A residential drop is idle most of the day, so this is the COMMON
      // case here — which is exactly why it must be the quietest thing on the
      // line rather than two zeroes at full weight on every subscriber.
      ? `<span class="wisp-linkbw__idle">idle</span>`
      : `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`
  // Reuses the link chip's classes on purpose: a rate is a rate, and a second
  // visual language for the same fact is how a dashboard stops looking like one
  // product. `--down` is the link chip's own alarm tone.
  const cls = ["wisp-linkbw", "wisp-linkbw--ref"]
  if (idle) cls.push("wisp-linkbw--idle")
  // The WORD "dark" stays for any offline ONU — it is the honest reading, and
  // printing a stale rate instead would be worse. The ALARM TONE is gated on
  // evidence: a red chip beside an ordinary offline customer is exactly the
  // extra red the "only the dot" rule exists to remove.
  if (isRefEvidence(p)) cls.push("wisp-linkbw--down")
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">${body}</div>`)
}
