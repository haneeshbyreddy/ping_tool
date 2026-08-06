// Hovering one BOX: what the card says about a device or a piece of plant.
// The frame is `map/hovercard.tsx`, shared with the subscriber card — a second
// card grammar for the second thing that opens one is how a dashboard stops
// reading as one product.
//
// Subscribers got a card first and the boxes carrying them did not, so reading
// a fault ran backwards: the customer under the cursor said which splitter fed
// it, what light it had and who to ring, while the OLT beside it said only its
// name. Answering "is this box up, what is it carrying, what feeds it" meant a
// click, a panel, and losing the map you were reading.
//
// Two kinds of card, because the two boxes can honestly say different things:
//
//   GEAR has a state of its own — an FSM, an outage, SNMP readings — so its card
//   is graded by `pinTone`, the same rule that fills the pin. The pin and the
//   card it opens must never disagree.
//
//   PLANT has none. Nothing pings a splitter: it has no power, no FSM and no
//   outage, and the only honest thing it can report is what its RECORDED
//   subscribers are doing — which is exactly what `dropTone` already paints its
//   pin with. Every count it prints says "recorded", because a leg nobody wrote
//   down is unknown, not free.
//
// Honesty rules carry over unchanged. A DOWN box freezes every SNMP reading
// behind it up to 15 minutes before staleness would notice, so those rows are
// dropped and the card says WHY rather than printing yesterday's numbers beside
// a red band; the same goes for a splitter whose OLT is down, where "6 of 6
// dark" would be that outage reported a second time.
import { ago, durationSince, fmtMs, fmtPct, isFresh, isStale } from "@/lib/format"
import { isPassiveType, type OrgDevice, type SplitterLoad } from "@/lib/types"
import { dropTone, isOversubscribed, ratioLabel } from "@/map/drops"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { esc, isDownState, pinTone } from "@/map/pins"

export interface DevHoverCtx {
  /** The box above: the uplink for gear, the feeder for plant. Named because
   *  hovering lights the cables INTO this pin, and the first thing to answer is
   *  what just lit up — the same reason the subscriber card leads with its drop. */
  parentName: string | null
  /** …and whether that box is itself down, which is usually the whole
   *  explanation for an UNREACHABLE child. */
  parentDown: boolean
  /** Passive plant only: what its recorded drops are doing, and the total split
   *  from the OLT down (null when a box in the chain has no ratio recorded — a
   *  partial product understates the split, and understating it is how a PON
   *  ends up over-built). */
  load?: SplitterLoad
  totalSplit?: number | null
  /** Passive plant only: the OLT its recorded subscribers sit on is DOWN, so
   *  every state and dBm below it is frozen. */
  frozen?: boolean
  frozenBy?: string | null
}

/** Type in words, under the name. The shape of a mark distinguishes an OLT from
 *  a switch from a splitter for someone who knows the shapes; the card is for
 *  everyone else, and it costs one faint line.
 *
 *  Exported for the SITE card, which composes the same words into a type mix —
 *  one spelling of "OLT", or two cards opened a pixel apart would name the same
 *  box two ways. */
const TYPE_WORD: Record<string, string> = {
  olt: "OLT", cpe: "CPE", ap: "AP", fdb: "FDB",
}
export const typeWord = (t: string | null): string | null =>
  t ? TYPE_WORD[t.toLowerCase()] ?? t[0].toUpperCase() + t.slice(1) : null

// ---------------------------------------------------------------------------
// Gear
// ---------------------------------------------------------------------------

/** The verdict row for a monitored box, and the one number that rides it.
 *
 *  Toned by `pinTone` — the pin's own fill — so the card can never grade a box
 *  differently from the mark that opened it. The WORD carries what the tone
 *  means: a red band reading only "OLT" tells nobody anything, and a muted one
 *  covers three different situations (planned downtime, no probe, no recent
 *  poll) that call for three different actions. */
function gearVerdict(d: OrgDevice): {
  tone: CardTone; word: string; hero: HoverCardModel["hero"]
} {
  const tone = pinTone(d)
  const stale = isStale(d.state_updated_at)
  // The three muted cases, spelled out. "Muted" on this map means "we are not
  // claiming anything about this box", and which reason it is decides whether
  // somebody should act.
  if (d.maintenance) return { tone, word: "Maintenance", hero: null }
  if (!d.assigned_node_id) return { tone, word: "No probe assigned", hero: null }
  if (!d.state) return { tone, word: "Not polled yet", hero: null }
  if (stale) return { tone, word: `No recent poll · ${ago(d.state_updated_at)}`, hero: null }

  if (isDownState(d)) {
    // How long it has been down is the first thing asked, and the pin can only
    // say it in a tooltip this card now covers. UNREACHABLE is kept distinct
    // from DOWN: it means the FSM suppressed this box behind a parent that
    // dropped, so the Uplink row below is the answer rather than this one.
    //
    // FIRST TOKEN only ("43m", not "43m 12s"), exactly as the pin's own title
    // does it: the html is the card's icon cache key, so a duration ticking
    // per second would swap the DOM node and replay the card's fade while
    // somebody is reading it. At minute resolution that costs one 120ms fade a
    // minute, which is worth paying for the one number a card like this exists
    // to carry.
    const word = d.state === "UNREACHABLE" ? "Unreachable" : "Down"
    const since = d.outage_started_at
      ? durationSince(d.outage_started_at).split(" ")[0] : null
    return { tone, word: since ? `${word} · ${since}` : word, hero: null }
  }
  // A round trip is the one live number gear has that is worth a glance, and it
  // goes quiet when healthy: a green 3 ms would be colour spent on the case
  // that is true nearly everywhere.
  const hero = d.latency_ms != null
    ? { value: fmtMs(d.latency_ms), unit: "ms", quiet: tone === "success" }
    : null
  return { tone, word: d.state === "DEGRADED" ? "Degraded" : "Up", hero }
}

function gearRows(d: OrgDevice, c: DevHoverCtx): string[] {
  const rows: string[] = []
  const down = isDownState(d)
  // The SAME gate the tree row uses for its SNMP chips (topology-page's
  // `liveSnmp`), so a card and a row can never disagree about whether a reading
  // is a claim about now.
  const live = !down && !isStale(d.state_updated_at)

  // FROZEN, said once and only when it is true. Without it the missing ONU and
  // port rows read as "this box has none", which is the "nothing is wrong" /
  // "nothing is measured" confusion in its most expensive form: a dark OLT
  // looking like a box with nothing on it.
  if (down)
    rows.push(cardRow("Readings", "frozen while it is down", "wisp-mapcard__v--soft"))

  // What feeds it, first — the card opens because a pin lit its cables up, and
  // for an UNREACHABLE box the parent's own state IS the diagnosis.
  if (c.parentName)
    rows.push(cardRow("Uplink", esc(c.parentName)
      + (c.parentDown ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))

  if (live && d.onus_total != null) {
    if (!isFresh(d.optics_updated_at)) {
      rows.push(cardRow("ONUs", "last optical walk is stale",
                        "wisp-mapcard__v--soft"))
    } else {
      rows.push(cardRow("ONUs",
        `${d.onus_online ?? 0} of ${d.onus_total} online`, "wisp-mapcard__v--num"))
      // A roster with no dBm in it is the C-Data/DBC fleet's normal state, and
      // a card silent about signal there would read exactly like a card silent
      // because everything is fine. Say which it is.
      const crit = d.onus_crit ?? 0
      const warn = d.onus_warn ?? 0
      const rx = d.onus_rx ?? 0
      if (rx === 0) {
        rows.push(cardRow("Signal", "not measured on this OLT",
                          "wisp-mapcard__v--soft"))
      } else if (crit || warn) {
        rows.push(cardRow("Signal", [crit && `${crit} critical`, warn && `${warn} weak`]
          .filter(Boolean).join(" · ")))
      } else if (rx < d.onus_total) {
        // Partial coverage, stated for the same reason the Home tile states it:
        // "3 measured of 194" and "194 measured" are different assurances.
        rows.push(cardRow("Signal", `${rx} of ${d.onus_total} measured`,
                          "wisp-mapcard__v--soft"))
      }
      // The optical verdicts the tree row and the Optical tab already carry.
      const faults = [
        d.fiber_cuts && `${d.fiber_cuts} suspected fibre cut${d.fiber_cuts === 1 ? "" : "s"}`,
        d.dup_macs && `${d.dup_macs} duplicate MAC${d.dup_macs === 1 ? "" : "s"}`,
      ].filter(Boolean).join(" · ")
      if (faults) rows.push(cardRow("Optics", esc(faults)))
    }
  }

  if (live && isFresh(d.ports_updated_at)) {
    const ports = [
      d.ports_down && `${d.ports_down} down`,
      d.ports_bw_low && `${d.ports_bw_low} under floor`,
      d.ports_bw_high && `${d.ports_bw_high} over ceiling`,
    ].filter(Boolean).join(" · ")
    if (ports) rows.push(cardRow("Ports", esc(ports)))
  }

  // Loss earns a row only when there is some: on a healthy box it is the same
  // "0.0%" on every card, which teaches the eye to skip the column that matters
  // on the one box where it isn't zero.
  if (live && d.packet_loss)
    rows.push(cardRow("Loss", esc(fmtPct(d.packet_loss)), "wisp-mapcard__v--num"))

  // CPU and temperature only. RAM was there and pushed the row past the value
  // column in a browser — measured, not guessed — and of the three it is the one
  // nobody acts on: a switch sitting at 80% memory is how these boxes ship. The
  // panel still carries all three with their meters.
  if (live && isFresh(d.health_updated_at)) {
    const vitals = [
      d.health_cpu_pct != null && `CPU ${Math.round(d.health_cpu_pct)}%`,
      d.health_temp_c != null && `${Math.round(d.health_temp_c)}°C`,
    ].filter(Boolean).join(" · ")
    if (vitals) rows.push(cardRow("Vitals", esc(vitals), "wisp-mapcard__v--num"))
  }
  return rows
}

// ---------------------------------------------------------------------------
// Passive plant
// ---------------------------------------------------------------------------

/** The verdict row for a splitter, FDB or closure.
 *
 *  A passive has no state to report, so this reports its RECORDED subscribers.
 *  The TONE is `dropTone` itself — the very function that paints the pin — so
 *  the card and the mark that opened it cannot grade one box two ways; only the
 *  WORDS are chosen here. Every phrasing says "recorded": six drops on a 1:8 is
 *  six recorded drops and says nothing about the other two legs, which nobody
 *  wrote down. */
function plantVerdict(c: DevHoverCtx): { tone: CardTone; word: string } {
  const load = c.load
  if (!load || load.recorded === 0)
    return { tone: "muted", word: "No subscribers recorded" }
  // A down OLT darkens everything behind it, so the dark tally stops being a
  // fact about this branch and becomes that outage restated. Grade the card on
  // what survives: how many drops are recorded here. The row below names the
  // box that went down — a suppression must always come with a live reason.
  if (c.frozen)
    return { tone: "muted", word: `${load.recorded} recorded · state unknown` }
  const tone = dropTone(load, c.frozen)
  if (tone === "dark")
    return { tone: "destructive", word: `${load.dark} of ${load.recorded} recorded dark` }
  if (tone === "weak") {
    const weak = load.crit + load.warn
    // Outliers are judged against this box's OWN median — same feeder, same
    // split loss — so a gap that size is one drop's own bend, splice or dirty
    // connector. Worded short because the hero beside it IS that median and the
    // row below is the worst of them: at "below this box's own median" the word
    // ellipsised in a browser, and the half that went was which median.
    return { tone: "warning", word: weak
      ? `${weak} of ${load.recorded} on weak signal`
      : `${load.outliers} below its own median` }
  }
  return { tone: "success", word: `${load.online} of ${load.recorded} recorded online` }
}

function plantRows(d: OrgDevice, c: DevHoverCtx): string[] {
  const rows: string[] = []
  const load = c.load
  const ratio = ratioLabel(d.split_ratio)

  if (c.frozen)
    rows.push(cardRow("Readings", c.frozenBy
      ? `frozen · ${esc(c.frozenBy)} is down` : "frozen · its OLT is down",
      "wisp-mapcard__v--soft"))

  // What feeds it. On a branch fault this is half the answer: the span between
  // this box and the one named here is where the van goes.
  if (c.parentName)
    rows.push(cardRow("Feed", esc(c.parentName)
      + (c.parentDown ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))

  // "of N legs", never "N free". However many drops were never written down,
  // the record can only speak for the ones that were.
  if (load?.recorded)
    rows.push(cardRow("Drops", d.split_ratio
      ? `${load.recorded} recorded of ${d.split_ratio} legs`
      : `${load.recorded} recorded`, "wisp-mapcard__v--num"))
  if (load?.orphans)
    rows.push(cardRow("Orphans", `${load.orphans} in no roster`,
                      "wisp-mapcard__v--soft"))

  // The MEDIAN rides the verdict row (see `plantHero`); the row below is for
  // the worst of them, and only when it is meaningfully worse. Both together in
  // one row was measured overflowing the value column in a browser — and the
  // half that got truncated was the number.
  if (!c.frozen && load?.rx_median != null && load.rx_worst != null
      && load.rx_median - load.rx_worst >= 1)
    rows.push(cardRow("Worst", `${load.rx_worst.toFixed(1)} dBm`,
                      "wisp-mapcard__v--num"))

  // The cumulative split is the number that says whether the PON has budget
  // left, and it is not something one box can answer. Printed only when it says
  // something the ratio beside it doesn't: a splitter hanging straight off an
  // OLT totals its own ratio, and "1:8 · 1:8 total" is noise that teaches the
  // eye to skip the line where it reads "1:32".
  const total = c.totalSplit
  const cumulative = total && total !== d.split_ratio ? `1:${total} total` : null
  if (ratio || cumulative)
    rows.push(cardRow("Split", esc([ratio, cumulative].filter(Boolean).join(" · ")),
                      "wisp-mapcard__v--num"))
  return rows
}

/** A splitter's own median Rx, in the slot the subscriber card puts a dBm in.
 *
 *  It is the number that makes a passive worth hovering during a soft fault:
 *  every drop on one box shares the feeder and the split loss, so they differ
 *  only by drop length — a whole box reading low is the FEEDER, and that shows
 *  up as this median sitting below its siblings' rather than as a box full of
 *  outliers. ALWAYS quiet: it is reference, not a verdict, and a median painted
 *  destructive because two subscribers are dark would claim the light was bad
 *  when it was measured on the ones still up. */
function plantHero(c: DevHoverCtx): HoverCardModel["hero"] {
  if (c.frozen || c.load?.rx_median == null) return null
  return { value: c.load.rx_median.toFixed(1), unit: "dBm", quiet: true }
}

// ---------------------------------------------------------------------------

function devModel(d: OrgDevice, c: DevHoverCtx): HoverCardModel {
  const passive = isPassiveType(d.device_type)
  const type = typeWord(d.device_type)
  if (passive) {
    const { tone, word } = plantVerdict(c)
    return {
      tone, name: d.name, word,
      sub: [type, d.pon_port && `PON ${d.pon_port}`].filter(Boolean).join(" · "),
      // The one claim worth chipping on a passive: more recorded drops than the
      // box has legs is provable either way, and it is either a mis-recorded
      // drop or a cascade nobody drew.
      chip: isOversubscribed(d, c.load) ? "Over legs" : null,
      hero: plantHero(c),
      rows: plantRows(d, c),
    }
  }
  const { tone, word, hero } = gearVerdict(d)
  return {
    tone, name: d.name, word, hero,
    sub: [type, d.ip_address].filter(Boolean).join(" · "),
    chip: null,
    rows: gearRows(d, c),
  }
}

/** The card, anchored on the pin it describes. */
export function DevHoverCard({ device, ctx }: { device: OrgDevice; ctx: DevHoverCtx }) {
  if (device.lat == null || device.lng == null) return null
  return <HoverCard at={[device.lat, device.lng]} model={devModel(device, ctx)} />
}
