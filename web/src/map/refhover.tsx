// Hovering one subscriber: what the card says, and the rules about what it may
// claim. The frame it opens in is `map/hovercard.tsx`, shared with the device
// card — this file is only the part that is true of a customer and nothing else.
//
// A surveyed street is a field of pins. The name plate answers "who", the
// mark's fill answers "up or down", and everything else about a customer —
// which splitter feeds them, what light they are getting, what number to ring —
// needed a click, a panel, and a decision about which pin to click first. That
// is the wrong order: you pick which one to open BY knowing those things.
//
// So this is the GLANCE and the click is the ACT. The card carries no buttons,
// and the `tel:` link stays on the selected card where a deliberate click has
// already happened.
//
// Everything it prints is bound by the same honesty rules the panel and the map
// label follow, and the card is the one surface with room to obey them
// PROPERLY: where a map label must silently omit a reading it cannot stand
// behind, this can say which of the three reasons it is.
import { onuSev } from "@/lib/format"
import type { OnuPlace } from "@/lib/types"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { bwIsIdle, fmtShort } from "@/map/linklabel"
import { esc } from "@/map/pins"
import { isRefDark, refHasRate, refHasRx, refName } from "@/map/refonu"

export interface RefHoverCtx {
  /** The box the highlighted line runs to — the splitter whose drop feeds this
   *  subscriber, or the OLT when no drop has been recorded. */
  anchorName: string | null
  viaSplitter: boolean
  /** Its OLT is DOWN. Every SNMP reading behind an unreachable box stopped being
   *  a claim about now up to 15 minutes before the staleness gate would notice,
   *  so the readings are dropped and the card says why — the frozen rule's
   *  "always pair the suppression with a live reason". */
  frozen: boolean
}

/** The one-line verdict at the top of the card, and the tone the whole header
 *  takes from it.
 *
 *  Graded on what is PRINTABLE, not on the stored severity alone. A crit
 *  `severity` whose reading is stale or frozen must not paint the row red with
 *  no number to explain it — that is "nothing is measured" rendering as
 *  "something is wrong", the same confusion the blank-Rx work exists to stop.
 *  With no printable reading the row falls back to the ICMP-grade fact: the ONU
 *  is registered and online, or it is not. */
function verdict(p: OnuPlace, showRx: boolean): { tone: CardTone; word: string } {
  if (!p.matched) return { tone: "warning", word: "Not in any roster" }
  if (p.ambiguous) return { tone: "warning", word: `On ${p.slots} live slots` }
  if (isRefDark(p)) return { tone: "destructive", word: `Dark · ${p.state ?? "offline"}` }
  if (p.state !== "online") return { tone: "muted", word: "State unknown" }
  // The WORD carries what the tone means, or the band is ambiguous: a red strip
  // reading only "Online" was read on screen as "this subscriber is down", when
  // what it says is "up, on critically low light". Same shape as "Dark · offline".
  const sev = onuSev(p)
  if (showRx && sev === "crit")
    return { tone: "destructive", word: "Online · critical signal" }
  if (showRx && sev === "warn")
    return { tone: "warning", word: "Online · weak signal" }
  return { tone: "success", word: "Online" }
}

/** Why there is no dBm to print. Never a dash: a dash beside a customer name is
 *  read as a measurement that came back empty, and all three of these are
 *  different statements with different actions behind them. Returns null when a
 *  reading IS printable, and for a dark ONU — where "no current light reading"
 *  is what the row above already said, in more useful words. */
function rxNote(p: OnuPlace, c: RefHoverCtx, showRx: boolean): string | null {
  if (showRx || !p.matched || isRefDark(p)) return null
  if (c.frozen) return "frozen · its OLT is down"
  if (p.rx_dbm == null) return "not measured on this OLT"
  return "last reading is stale"
}

function refModel(p: OnuPlace, c: RefHoverCtx): HoverCardModel {
  const showRx = !c.frozen && refHasRx(p)
  const { tone, word } = verdict(p, showRx)
  const sev = onuSev(p)
  const dark = isRefDark(p)

  const rows: string[] = []
  const note = rxNote(p, c, showRx)
  // FROZEN is a statement about EVERY reading behind that OLT, not just the
  // light — so it is said once, under a key that covers both, and the Traffic
  // row stands down below. Saying "port walk stale" beside it would be true and
  // useless: the walk is stale because the box is unreachable, and pointing at
  // the symptom is how somebody goes looking for an SNMP fault.
  if (note) rows.push(cardRow(c.frozen ? "Readings" : "Signal", esc(note),
                              "wisp-mapcard__v--soft"))

  // WHERE THE LINE GOES, first — the card exists because a line lit up, and the
  // first thing to answer is what it lit up towards. A recorded drop names the
  // splitter a crew drives to; an unrecorded one says so plainly rather than
  // letting a line into the OLT imply direct fibre to the customer.
  //
  // Keyed on the ANCHOR, not on `matched`: an orphaned placement whose drop was
  // recorded still draws a line to its splitter, and a card that left that row
  // out would be silent about the one span on screen.
  if (c.viaSplitter && c.anchorName) rows.push(cardRow("Drop", esc(c.anchorName)))
  else if (p.matched) rows.push(cardRow("Drop", "not recorded", "wisp-mapcard__v--soft"))
  if (p.matched) {
    const where = [p.device_name, p.pon_port && `PON ${p.pon_port}`]
      .filter(Boolean).join(" · ")
    if (where) rows.push(cardRow("On", esc(where)))
  }

  // A dark ONU's counters are as old as its light reading, so the rate is left
  // off entirely rather than printed as a rate it is currently not passing.
  // Frozen is the same case one level up — the "Readings" row above covers it.
  if (p.matched && !dark && !c.frozen) {
    if (refHasRate(p)) {
      const down = p.out_bps   // ↓ toward the subscriber = the OLT port's egress
      const up = p.in_bps
      rows.push(cardRow("Traffic", bwIsIdle(down, up)
        ? `<span class="wisp-mapcard__v--soft">idle</span>`
        : `<span class="wisp-mapcard__ar">↓</span>${esc(fmtShort(down ?? 0))}`
          + `<span class="wisp-mapcard__ar">↑</span>${esc(fmtShort(up ?? 0))}`,
        "wisp-mapcard__v--num"))
    } else {
      rows.push(cardRow("Traffic", p.if_name
        ? "no recent reading · port walk stale"
        : "no per-ONU interface on this OLT", "wisp-mapcard__v--soft"))
    }
  }

  // The number to ring. On a fault this is the whole reason to identify a
  // customer at all, so it earns a row even though the card can't dial it.
  if (p.phone) rows.push(cardRow("Phone", esc(p.phone), "wisp-mapcard__v--num"))

  return {
    tone,
    name: refName(p),
    sub: p.mac,
    // A REFERENCE point is evidence in a PON verdict; a located drop is a
    // coordinate somebody recorded. Chipping the first is what stops the second
    // being read as a witness the fleet does not have.
    chip: p.witness ? "Reference" : null,
    word,
    // The dBm rides the verdict row when — and only when — it is printable.
    // `ok` goes quiet: a healthy Rx is the overwhelming majority and none of it
    // is news, so a toned number there would be colour spent on the boring case.
    hero: showRx
      ? { value: (p.rx_dbm as number).toFixed(2), unit: "dBm", quiet: sev === "ok" }
      : null,
    rows,
  }
}

/** The card, anchored on the subscriber it describes. */
export function RefHoverCard({ place, ctx }: { place: OnuPlace; ctx: RefHoverCtx }) {
  return <HoverCard at={[place.lat, place.lng]} model={refModel(place, ctx)} />
}
