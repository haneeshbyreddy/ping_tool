// Subscriber drops on the map: what hangs off each splitter, and which span
// broke.
//
// The map used to draw a subscriber straight to its OLT. That line was a lie of
// omission — an ISP hangs a customer off the NEAREST splitter, and between that
// splitter and the OLT there may be another one, so the drawn line skipped the
// entire distribution network the field crew actually works on. `onu_drops`
// records the last hop; the splitter chain above it was already on the map as
// passive plant with drawn cable routes.
//
// Three rules this file holds:
//
//   1. **A passive stays quiet until its subscribers aren't.** Plant is
//      reference material and must not compete with gear for attention — the
//      same reason passives render small and muted today. The one exception is
//      the one worth making: a splitter whose recorded customers are dark is
//      the most useful object on the map during a cut, so it is allowed to get
//      louder. Size never changes; only tone does.
//   2. **"Recorded" is never "occupied".** A 1:8 with six recorded drops has six
//      recorded drops. It does NOT have two free legs — nobody wrote those down,
//      and unknown is not spare. Over-subscription is the one capacity claim
//      that survives an incomplete record, so it is the one this file makes.
//   3. **A branch fault names a SPAN, not a distance.** Ranging brackets a cut
//      in metres that run ~39% short on the C-Data fleet; two pins and the cable
//      between them are where a van actually drives.
import type L from "leaflet"
import { cachedDivIcon, esc } from "@/map/pins"
import type { BranchFault, OrgDevice, SplitterLoad } from "@/lib/types"

export type DropTone = "dark" | "weak" | "ok" | "quiet"

/** Tone for a passive pin, from what its RECORDED subscribers are doing.
 *
 *  Deliberately not the device's own state: a splitter has no state — it has no
 *  power, no FSM and nothing to ping. What it can report is the health of what
 *  hangs below it, which is the only thing a splitter on a map can honestly say.
 *
 *  `ok` and `quiet` are different sentences and were one for a while (operator's
 *  call, 2026-08-05: plant takes a full status colour now). "Every recorded drop
 *  on this box is online" is a MEASUREMENT; "nobody has recorded a drop here" is
 *  an empty record, and a map that paints them alike is the "nothing is wrong"
 *  vs "nothing is measured" confusion on a splitter. The hover card had already
 *  drawn the line — `plantVerdict` grades a healthy box `success` — so the pin
 *  was the half that disagreed with the card it opens. */
export function dropTone(load: SplitterLoad | undefined, frozen = false): DropTone {
  // A DOWN OLT darkens every ONU behind it, so "all six recorded subscribers
  // are dark" stops being a fact about this branch and becomes that outage
  // reported a second time — on a box that has no outage of its own. Same rule
  // the tree row keeps for its SNMP chips and the bandwidth summary for its
  // alarms: a mark is a claim about NOW, and behind an unreachable box there is
  // nothing current to claim. The hover card says so in words; the pin simply
  // stands down. (Its DETECTION is untouched — `drops.branch_faults` already
  // skips a down OLT server-side, so nothing that pages changes here.)
  if (frozen) return "quiet"
  if (!load || load.recorded === 0) return "quiet"
  if (load.dark > 0) return "dark"
  if (load.crit > 0 || load.warn > 0 || load.outliers > 0) return "weak"
  return "ok"
}

export const ratioLabel = (r: number | null | undefined): string | null =>
  r ? `1:${r}` : null

/** Is this box carrying more drops than it has legs?
 *
 *  The ONE capacity statement an incomplete record still supports: however many
 *  drops were never written down, the ones that were already exceed the ratio.
 *  A splitter can't grow a ninth leg, so this is either a mis-recorded drop or a
 *  cascade nobody drew — both worth surfacing, neither guessable from a count. */
export const isOversubscribed = (d: OrgDevice, load?: SplitterLoad): boolean =>
  !!d.split_ratio && !!load && load.recorded > d.split_ratio

/** What a passive writes on its plate: its SPLIT RATIO, and nothing else.
 *
 *  It REPLACES the name rather than sitting beside it (operator's call,
 *  2026-08-05). Plant names on a real fleet run long and arbitrary — `medha`,
 *  `manjulapur`, `collectorate`, `IK REDDY` — and a splitter is not read for its
 *  name: the ratio is what says whether a PON has budget left, and it is three
 *  characters against fifteen. The recorded count and the dark count went with
 *  the name: both are still carried by the tone the MARK takes (`dropTone`),
 *  spelled out in the hover card, counted in the panel, and written in full in
 *  `passiveTitle` below — so the map loses ink here, not facts.
 *
 *  A box with NO recorded ratio keeps its name. That is not a hedge against the
 *  instruction: it is the only other thing the plate can say, and the plate is
 *  load-bearing — since plant left the clustering pass it is one of the two
 *  channels (with the hole in the mark) telling a splitter from a customer, both
 *  of which are teardrop pins at nearly the same ink. A blank plate would spend
 *  one of the two. FDB and closure have no ratio by nature and so always name
 *  themselves; every splitter on the live fleet has one. */
export function passivePinLabel(d: OrgDevice): string {
  return ratioLabel(d.split_ratio) ?? d.name
}

export function passiveTitle(d: OrgDevice, load?: SplitterLoad,
                             frozen = false): string {
  const bits: string[] = [d.name]
  const ratio = ratioLabel(d.split_ratio)
  if (ratio) bits.push(`${ratio} splitter`)
  if (d.pon_port) bits.push(`PON ${d.pon_port}`)
  if (!load || load.recorded === 0) {
    bits.push("no subscribers recorded")
  } else if (frozen) {
    // Third place the same claim would otherwise be made: tone, sub-label and
    // this. All three stand down together behind a down OLT, or the tooltip
    // contradicts the pin it is attached to.
    bits.push(`${load.recorded} recorded subscriber${load.recorded === 1 ? "" : "s"}`)
    bits.push("readings frozen while its OLT is down")
  } else {
    // "recorded", every time. The map may not imply it knows the rest.
    bits.push(`${load.recorded} recorded subscriber${load.recorded === 1 ? "" : "s"}`)
    if (load.dark) bits.push(`${load.dark} dark`)
    if (load.crit) bits.push(`${load.crit} critical Rx`)
    else if (load.warn) bits.push(`${load.warn} weak Rx`)
    if (load.outliers) bits.push(`${load.outliers} below this splitter's own median`)
    if (isOversubscribed(d, load)) bits.push("MORE DROPS THAN LEGS")
  }
  return bits.join(" · ")
}

// ---------------------------------------------------------------------------
// The drop line: subscriber → the box it actually hangs off.
// ---------------------------------------------------------------------------

/** Dash for the drop. TIGHTER than the ONU→OLT association dash it replaces
 *  (`REF_DASH`, a 9px period), because a drop is the SHORTEST and least
 *  surveyed span on the map — the last few metres into a house, which nobody
 *  traces. Same principle as every other dashed line here: dashes mean "we did
 *  not survey this".
 *
 *  The period is 8px, not the old 4px: dash lengths are absolute px while the
 *  stroke kept getting wider, and at weight 3.5 a round-capped "1" dash is
 *  already a 3.5px dot — the original 3px gap would leave NEGATIVE daylight and
 *  the span would read SOLID, i.e. as traced fibre. Widening a dotted line
 *  means opening its gaps by at least as much. */
export const DROP_DASH = "1 7"

/** Where a reference ONU's line should end.
 *
 *  The recorded splitter when there is one and it is placed; otherwise the OLT,
 *  as before. The fallback is kept deliberately — a subscriber whose splitter
 *  nobody has recorded still belongs to a PON, and drawing nothing would hide a
 *  reference point. It renders WEAKER and says why, so "we routed this through
 *  its plant" and "we guessed at the OLT" never look alike. */
export function dropAnchor(
  splitterId: number | null | undefined, oltId: number | null | undefined,
  byId: Map<number, OrgDevice>,
): { device: OrgDevice; kind: "splitter" | "olt" } | null {
  const sp = splitterId != null ? byId.get(splitterId) : undefined
  if (sp && sp.lat != null && sp.lng != null) return { device: sp, kind: "splitter" }
  const olt = oltId != null ? byId.get(oltId) : undefined
  if (olt && olt.lat != null && olt.lng != null) return { device: olt, kind: "olt" }
  return null
}

// ---------------------------------------------------------------------------
// Branch faults
// ---------------------------------------------------------------------------

/** The suspect span for a branch fault, as the link_routes key of the cable
 *  between the dark box and its parent — which is exactly how every other line
 *  on this map is addressed, so the overlay paints real drawn geometry where the
 *  operator traced it and the chord where they didn't. */
export const branchLinkKey = (f: BranchFault): string =>
  `${f.passive_id}:${f.parent_id}`

export function branchTitle(f: BranchFault, name: string, parentName: string): string {
  const what = f.cause === "power"
    ? "Power loss on this branch"
    : f.suspected ? "Suspected fibre break" : "Fibre break"
  const pon = f.pon_ports.length ? ` · PON ${f.pon_ports.join(", ")}` : ""
  return `${what}${pon} · all ${f.dark} recorded subscriber`
    + `${f.dark === 1 ? "" : "s"} below ${name} are dark`
    + ` while ${f.lit_siblings} on sibling branches stay lit`
    + ` · suspect the span ${parentName} → ${name}`
    + (f.witness_dark
      ? ` · ${f.witness_dark} power-backed reference ONU dark, so power can't explain it`
      : "")
}

export function branchIcon(f: BranchFault, name: string, parentName: string): L.DivIcon {
  const cls = ["wisp-branchfault", `wisp-branchfault--${f.cause}`]
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${esc(branchTitle(f, name, parentName))}">`
    + `${f.cause === "power" ? "⚡" : "✂"}</div>`)
}

/** Index the rollup by passive id — the shape every render site wants. */
export function loadsById(loads: SplitterLoad[] | undefined): Map<number, SplitterLoad> {
  return new Map((loads ?? []).map((l) => [l.passive_id, l]))
}
