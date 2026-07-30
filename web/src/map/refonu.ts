// Reference ONUs on the map: the handful of subscribers an operator has vouched
// for as reliably powered (`onu_places`). They are a SUBORDINATE layer, off by
// default — 90% of what hangs off a fleet's ports is an ONU, and a map that
// renders them all stops showing the plant it exists to show.
//
// Two rules this file exists to hold:
//
//   1. A reference ONU is drawn SMALLER and QUIETER than any device pin, with a
//      shape of its own (a diamond — devices are round, passives are small and
//      round). It must never be mistaken for infrastructure.
//   2. Its state still carries the loudest thing this feature produces: a dark
//      reference ONU is evidence of a fiber cut, because power cannot explain
//      it. So a dark one goes destructive-toned while an online one stays
//      near-silent. It is still ranked BELOW a down device — a dark subscriber
//      is a clue about an outage, not the outage.
import type L from "leaflet"
import { cachedDivIcon, esc } from "@/map/pins"
import { isFresh } from "@/lib/format"
import type { OnuPlace } from "@/lib/types"

/** Dark = the ONU left `online`, however the vendor said so. `dying_gasp` is
 *  dark on the map but is NOT witness evidence (it announced a power loss) —
 *  ponfault owns that distinction; here it just isn't drawn as healthy. */
export const isRefDark = (p: OnuPlace): boolean =>
  p.matched && p.state != null && p.state !== "online" && p.state !== "unknown"

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
  const who = p.label || p.name || p.mac
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
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${esc(refTitle(p))}">`
    + `<span class="wisp-refonu__mark"></span></div>`)
}

/** Devices sit at 0–1000. A dark reference ONU is worth surfacing above a quiet
 *  one, but never above the gear whose outage it is a clue about.
 *
 *  The lift is for WITNESSES only. A dark witness is a fibre cut with a
 *  coordinate; a dark located subscriber is a subscriber who is offline, which
 *  is ordinary — promoting those would make every evening's churn look like
 *  evidence. */
export function refZIndex(p: OnuPlace, selected: boolean): number {
  if (selected) return -50
  return p.witness && isRefDark(p) ? -100 : -200
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

const fmtShort = (bps: number): string => {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)}G`
  if (bps >= 1e6) return `${bps >= 1e7 ? Math.round(bps / 1e6) : (bps / 1e6).toFixed(1)}M`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)}k`
  return `${Math.round(bps)}`
}
const fmtFull = (bps: number | null): string => {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`
  return `${Math.round(bps)} b/s`
}

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

/** Tone of the line. Follows the OPTICAL ROSTER (`isRefDark`), the same source
 *  the pin uses — pin and line contradicting each other on a wall map is worse
 *  than either being wrong. `port_state` rides a different clock and is a second
 *  opinion only; it colours nothing. */
export function refLineTone(p: OnuPlace): "dark" | "quiet" {
  return isRefDark(p) ? "dark" : "quiet"
}

export function refBwIcon(p: OnuPlace): L.DivIcon {
  const hasRate = refHasRate(p)
  const dark = isRefDark(p)
  const who = p.label || p.name || p.mac
  const port = p.if_name ? ` · ${p.if_name.split(" ")[0]}` : ""
  // ↓ is traffic toward the subscriber, which is the OLT interface's EGRESS.
  const down = p.out_bps
  const up = p.in_bps
  const title = esc(
    dark ? `${who}${port} · dark — power can't explain this on a reference ONU`
    : hasRate ? `${who}${port} · ↓ ${fmtFull(down)} to subscriber · ↑ ${fmtFull(up)}`
    : p.if_name ? `${who}${port} · no recent rate reading (port walk stale)`
    : `${who} · this OLT's firmware doesn't publish a per-ONU interface`)
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const body = dark
    ? "dark"
    : hasRate
      ? `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`
      : `<span class="wisp-linkbw__port">no rate</span>`
  // Reuses the link chip's classes on purpose: a rate is a rate, and a second
  // visual language for the same fact is how a dashboard stops looking like one
  // product. `--down` is the link chip's own alarm tone.
  const cls = ["wisp-linkbw", "wisp-linkbw--ref"]
  if (dark) cls.push("wisp-linkbw--down")
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">${body}</div>`)
}
