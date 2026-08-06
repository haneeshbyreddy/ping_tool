// Hovering a SITE badge: what the card says about a cabinet, a rooftop or a
// pole carrying several boxes. The frame is `map/hovercard.tsx`, shared with the
// device and subscriber cards — a third card grammar for the third thing that
// opens one is how a dashboard stops reading as one product.
//
// A folded badge is the one mark on this map that HIDES what it stands for. A
// pin says its name and its state; a badge says "3" and a composition ring, so
// reading a site meant clicking it, losing the map, and reading a list. That is
// backwards for the case the fold exists to serve — an ISP cabinet holding an
// OLT, an aggregation switch and a backhaul radio, where the question is always
// "which of these is the one that's down".
//
// So the card is the GLANCE and the click is still the ACT: the site card that
// opens on click is scrollable, per-member and clickable, and this one is
// none of those. It names what is inside, groups them by state, and stops.
//
// Every honesty rule the device card follows carries over, for the same reasons:
// a DOWN box freezes its SNMP readings up to 15 minutes before staleness would
// notice, so a box in that state is left OUT of the site's ONU and port totals
// and the card says how many were left out. A total that quietly shrank is the
// "nothing is wrong" / "nothing is measured" confusion wearing a sum.
import { durationSince, isFresh, isStale } from "@/lib/format"
import type { OrgDevice } from "@/lib/types"
import type { SiteCluster } from "@/map/clusters"
import { typeWord } from "@/map/devhover"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { esc, isDownState, pinTone } from "@/map/pins"

export interface SiteHoverCtx {
  /** The boxes feeding this site FROM OUTSIDE it. Members feeding each other are
   *  excluded deliberately: a switch in the same cabinet as the OLT it feeds is
   *  not what the site hangs off, and naming it would answer "what feeds this
   *  site" with something already listed two rows above. */
  uplinks: Array<{ name: string; down: boolean }>
}

/** Worst tone wins — the same ranking the badge's composition ring paints in, so
 *  a red arc and a red card can never disagree about one site. */
function siteTone(members: OrgDevice[]): CardTone {
  const tones = new Set(members.map(pinTone))
  if (tones.has("destructive")) return "destructive"
  if (tones.has("warning")) return "warning"
  if (tones.has("success")) return "success"
  return "muted"
}

/** Why a box is muted. "Muted" means "we are not claiming anything about this
 *  one", and which of the four reasons it is decides whether anybody should act
 *  — the device card spells them out for one box and this does it for a group. */
const quietReason = (d: OrgDevice): string =>
  d.maintenance ? "maintenance"
  : !d.assigned_node_id ? "no probe"
  : !d.state ? "not polled yet"
  : "no recent poll"

/** How many names one row prints before it starts counting instead.
 *
 *  Measured, not guessed: the value column ellipsizes, and a site card opened on
 *  a real 10-box cabinet printed all ten into a row that showed three and a half
 *  of them. A truncated list is worse than a count — it looks like the whole
 *  answer, and which names survive is decided by string length rather than by
 *  anything an operator would choose. Three names plus "+7" keeps identity where
 *  it fits and stays honest where it doesn't. */
const NAME_CAP = 3

const names = (list: OrgDevice[]): string => {
  const shown = list.slice(0, NAME_CAP).map((m) => m.name).join(", ")
  const rest = list.length - NAME_CAP
  return rest > 0 ? `${shown} +${rest}` : shown
}

/** One row per STATE, not per member.
 *
 *  Grouping is what makes a card this size able to hold a cabinet: six devices
 *  are six rows of which five say "up", and the eye has to find the sixth. It
 *  also sidesteps a real constraint — `.wisp-mapcard__k` is 3rem and does not
 *  ellipsize, so a device NAME can never be a key here (`HALIYA-WAN-SW` would
 *  land on its own value). The state is the short half; the names are the long
 *  half, and the value column is the one that ellipsizes. */
function memberRows(members: OrgDevice[]): string[] {
  const rows: string[] = []
  const by = (t: CardTone) => members.filter((m) => pinTone(m) === t)

  const down = by("destructive")
  if (down.length) {
    // How long it has been down is the first thing asked, and there is room for
    // it while exactly one box is down. FIRST TOKEN only ("43m", not "43m 12s"),
    // exactly as the pin's title and the device card do it: the html is this
    // card's icon cache key, so a duration ticking per second would swap the DOM
    // node and replay the fade while somebody is reading it.
    const since = down.length === 1 && down[0].outage_started_at
      ? durationSince(down[0].outage_started_at).split(" ")[0] : null
    rows.push(cardRow("Down", esc(names(down))
      + (since ? ` <span class="wisp-mapcard__v--soft">· ${esc(since)}</span>` : "")))
  }

  const warn = by("warning")
  if (warn.length) rows.push(cardRow("Degraded", esc(names(warn))))

  const quiet = by("muted")
  if (quiet.length) {
    // One shared reason is worth naming; a mixture is not worth four clauses on
    // one line, and the pins themselves still carry it.
    const reasons = new Set(quiet.map(quietReason))
    const why = reasons.size === 1 ? [...reasons][0] : null
    rows.push(cardRow("No state", esc(names(quiet))
      + (why ? ` <span class="wisp-mapcard__v--soft">· ${esc(why)}</span>` : "")))
  }

  const up = by("success")
  if (up.length) rows.push(cardRow("Up", esc(names(up)), "wisp-mapcard__v--soft"))
  return rows
}

/** The site's ONU and port totals — over the boxes that can currently answer for
 *  themselves, with the number that couldn't stated rather than absorbed.
 *
 *  Same gate the tree row and the device card use (`isDownState` plus the
 *  staleness rule, never `isFresh` alone): behind an unreachable box the rows
 *  persist and would go on summing yesterday's roster into today's total. */
function rollupRows(members: OrgDevice[]): string[] {
  const rows: string[] = []
  let total = 0
  let online = 0
  let counted = 0
  let missing = 0
  let portsDown = 0
  let anyPorts = false
  for (const m of members) {
    const live = !isDownState(m) && !isStale(m.state_updated_at)
    if (m.onus_total != null) {
      if (live && isFresh(m.optics_updated_at)) {
        counted += 1
        total += m.onus_total
        online += m.onus_online ?? 0
      } else {
        missing += 1
      }
    }
    if (live && isFresh(m.ports_updated_at) && m.ports_down) {
      anyPorts = true
      portsDown += m.ports_down
    }
  }
  if (counted) {
    rows.push(cardRow("ONUs", `${online} of ${total} online`
      + (missing
        ? ` <span class="wisp-mapcard__v--soft">· ${missing} not reporting</span>` : ""),
      "wisp-mapcard__v--num"))
  } else if (missing) {
    // Every OLT here is down or stale. Saying nothing would read as a site with
    // no ONUs on it, which is the one thing this must not imply about a cabinet
    // during an outage.
    rows.push(cardRow("ONUs", `${missing} OLT${missing === 1 ? "" : "s"} not reporting`,
                      "wisp-mapcard__v--soft"))
  }
  if (anyPorts) rows.push(cardRow("Ports", `${portsDown} down`, "wisp-mapcard__v--num"))
  return rows
}

/** "2 OLTs · Switch · Backhaul" — what KIND of site this is, in the slot the
 *  other cards put an identifier in. A badge already says how many; the type mix
 *  is the part it cannot show, and it is what tells a rack of subscriber-facing
 *  gear from a pole carrying one radio. */
function typeMix(members: OrgDevice[]): string | null {
  const counts = new Map<string, number>()
  for (const m of members) {
    const w = typeWord(m.device_type)
    if (w) counts.set(w, (counts.get(w) ?? 0) + 1)
  }
  if (!counts.size) return null
  return [...counts].map(([w, n]) => (n > 1 ? `${n} ${plural(w)}` : w)).join(" · ")
}

/** A bare "+s" printed "4 Switchs" on the first real site this card opened on.
 *  The sibilant rule covers every word `typeWord` can produce — Switch, Box —
 *  and leaves OLTs, APs and Gateways alone. */
const plural = (w: string): string =>
  /(s|ch|sh|x|z)$/i.test(w) ? `${w}es` : `${w}s`

function siteModel(c: SiteCluster, ctx: SiteHoverCtx): HoverCardModel {
  const members = c.members
  const n = members.length
  const down = members.filter((m) => pinTone(m) === "destructive").length
  const warn = members.filter((m) => pinTone(m) === "warning").length
  const up = members.filter((m) => pinTone(m) === "success").length

  // The WORD carries what the tone means. A red band over a bare "3 devices"
  // says a site is in trouble without saying how much of it, which on a cabinet
  // is the difference between one radio and the whole rack.
  const word = down ? `${down} of ${n} down`
    : warn ? (warn === n ? `All ${n} degraded` : `${warn} of ${n} degraded`)
    : up === 0 ? "None reporting"
    : up < n ? `${up} of ${n} up`
    : `All ${n} up`

  const rows = memberRows(members)
  if (ctx.uplinks.length === 1) {
    rows.push(cardRow("Feed", esc(ctx.uplinks[0].name)
      + (ctx.uplinks[0].down ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))
  } else if (ctx.uplinks.length > 1) {
    // Two feeds into one site is a real shape (a ring, or two OLTs on separate
    // backhauls), and naming them all would push the member list off the card.
    const anyDown = ctx.uplinks.some((u) => u.down)
    rows.push(cardRow("Feeds", `${ctx.uplinks.length} boxes`
      + (anyDown ? ` <span class="wisp-mapcard__v--soft">· 1 or more down</span>` : ""),
      "wisp-mapcard__v--soft"))
  }
  rows.push(...rollupRows(members))

  return {
    tone: siteTone(members),
    name: `${n} devices at this site`,
    sub: typeMix(members),
    chip: null,
    word,
    // No hero: a site has no single number of its own. A round trip belongs to
    // ONE box, and picking the worst of them and printing it bare would read as
    // the site's own latency.
    hero: null,
    rows,
  }
}

/** The card, anchored on the badge it describes. */
export function SiteHoverCard({ cluster, ctx }: {
  cluster: SiteCluster; ctx: SiteHoverCtx
}) {
  return <HoverCard at={cluster.center} model={siteModel(cluster, ctx)} />
}
