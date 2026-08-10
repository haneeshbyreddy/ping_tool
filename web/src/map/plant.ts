// Recording passive plant, and what a click on the map already knows about it.
//
// Plant was authored through the same form gear is: name it, pick a type, pick a
// parent out of a flat list of every device in the org, type a PON label from
// memory, save, then go to the map, find it in the unplaced drawer, arm
// placement and click. Fifteen interactions for a box an ISP has several hundred
// of — and the PON label was free text that silently decided, ten steps later,
// whether the customer picker had anything in it.
//
// The fix is not a shorter form. It is that a splitter's two defining facts are
// WHERE IT IS and WHAT FEEDS IT, and a right-click on the map already carries
// both. Everything in this file exists to turn one coordinate into as much of a
// record as can be honestly derived, so the sheet only has to ask the one thing
// the ground cannot say (how many ways the box splits).
//
// Two rules hold throughout:
//
//   * DERIVED IS NOT GUESSED. Every inferred field is shown with the box it came
//     from named, and every one of them is overridable. A prefilled parent that
//     cannot be seen or changed is how a whole feeder ends up recorded against
//     the wrong splitter, and a wrong feeder is a wrong branch-fault verdict
//     later.
//   * A PON IS PICKED FROM THE OLT'S OWN LABELS, never typed. A splitter
//     hanging off another splitter arrives with its parent's PON already filled
//     in (one fibre goes in), but the field stays changeable everywhere,
//     because inheritance is only as right as the parent's own column.
import { distanceKm } from "@/map/geometry"
import { isPassiveType, type OrgDevice } from "@/lib/types"

/** The plant kinds an operator may CREATE.
 *
 *  Narrowed to `splitter` alone at the operator's request (2026-08-05: "only
 *  splitter is enough for now"). FDBs and closures were offered on every
 *  authoring surface and chosen zero times — three options where the answer is
 *  always the same is three options' worth of hesitation on a flow whose whole
 *  point is speed.
 *
 *  DELIBERATELY NOT the same list as `PASSIVE_DEVICE_TYPES`, which stays at all
 *  three. That one is what `isPassiveType` answers, and it decides whether a row
 *  is excluded from `org_device_topology`, skipped by billing, and allowed to
 *  carry drops. Dropping a type from THERE would turn any existing `fdb` or
 *  `closure` row into monitored gear: it would join an engine, get an FSM and be
 *  able to page. So this is the CREATABLE set and that one is the RECOGNISED
 *  set, and widening this back is a one-line edit. */
export const PLANT_KINDS = ["splitter"] as const
export type PlantKind = (typeof PLANT_KINDS)[number]

export const PLANT_LABEL: Record<PlantKind, string> = {
  splitter: "splitter",
}

/** Name stem per kind. Short and predictable so the suggestion is easy to read
 *  and easier to overwrite — the field is focused and selected on open, because
 *  operators name boxes after places ("Main road"), not after schemes. */
const NAME_STEM: Record<PlantKind, string> = {
  splitter: "SPL",
}

/** How far from the click we will look for the box that feeds a new one.
 *
 *  Generous on purpose. The cost of suggesting a feeder 1.5 km away is one
 *  glance at a named row the operator can change in a click; the cost of
 *  suggesting nothing is that they open a dropdown of every device in the org,
 *  which is the flow this replaces. Past this the prefill genuinely means
 *  nothing and the sheet says so rather than naming a box from another village. */
export const FEEDER_RADIUS_KM = 2

/** What feeds a box, physically. `feed_device_id` when the server derived one
 *  from the fibre, else the declared parent — one expression, in one place,
 *  because a chain walked two ways is a chain that can disagree with itself.
 *
 *  The server already prefers the declared parent when there is one, so this is
 *  only a fallback for a bundle talking to a central that predates the field
 *  (the SPA deploys the instant it is built; central needs a restart). */
const feedOf = (d: OrgDevice): number | null =>
  d.feed_device_id ?? d.parent_device_id ?? null

/** The chain of passives from a box up to the powered gear feeding it, nearest
 *  first. Cycle-guarded: a bad row must not spin a render.
 *
 *  Walks the PLANT feed, not `parent_device_id` (2026-08-09). Placing a box
 *  stopped asking what feeds it — the honest answer arrives later, when a core
 *  is pulled into it — so a splitter recorded entirely from the fibre has no
 *  declared parent and would otherwise have no chain at all: no split total, no
 *  PON, and no branch-fault span. What it does have is a run, and the run says
 *  the same thing better, because it also says which core.
 *
 *  Lived in `splitter-panel.tsx` until the map started authoring plant too. It
 *  is here now because a pure map helper importing a panel component to get one
 *  rule is how a module graph knots — the same move `onuSev` made into
 *  `lib/format`. */
export function feedChain(device: OrgDevice, byId: Map<number, OrgDevice>) {
  const passives: OrgDevice[] = []
  let head: OrgDevice | null = null
  const first = feedOf(device)
  let cur = first != null ? byId.get(first) : undefined
  const seen = new Set<number>([device.id])
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    if (!isPassiveType(cur.device_type)) { head = cur; break }
    passives.push(cur)
    const up = feedOf(cur)
    cur = up != null ? byId.get(up) : undefined
  }
  return { passives, head }
}

/** Total split from the OLT down to this box: 1:4 feeding a 1:8 is 1:32.
 *
 *  The number that decides whether a PON has any budget left, and it is not
 *  something a single box can answer. Null when any box in the chain has no
 *  recorded ratio: a partial product would understate the split, and
 *  understating it is how a PON ends up over-built.
 *
 *  OUTPUTS ONLY — `split_inputs` is deliberately not read here, and its callers
 *  are deliberately the only places left that still print a hard-coded "1:".
 *  A 2:16 has a protection feed and still splits sixteen ways, so a second input
 *  multiplies nothing; and a CASCADE TOTAL always has one fibre entering at the
 *  head, whatever the boxes below it were built with. So `1:{total}` is correct
 *  at those three render sites and must not be "fixed" to use `ratioLabel` —
 *  that helper describes ONE BOX, and this is a property of a whole chain. */
export function cumulativeSplit(device: OrgDevice, byId: Map<number, OrgDevice>): number | null {
  const { passives } = feedChain(device, byId)
  let total = 1
  for (const d of [device, ...passives]) {
    if (!d.split_ratio) return null
    total *= d.split_ratio
  }
  return total
}

/** What a box that does not exist yet would total, hanging off `parent` at
 *  `ratio`. Same refusal as `cumulativeSplit`: one unrecorded ratio anywhere up
 *  the chain and the answer is null, never a smaller number stated confidently. */
export function splitIfAdded(
  parent: OrgDevice | null, ratio: number | null, byId: Map<number, OrgDevice>,
): number | null {
  if (!ratio) return null
  if (!parent) return ratio
  if (!isPassiveType(parent.device_type)) return ratio
  const above = cumulativeSplit(parent, byId)
  return above == null ? null : above * ratio
}

const placed = (d: OrgDevice): d is OrgDevice & { lat: number; lng: number } =>
  d.lat != null && d.lng != null

/** Is this the KIND of box a new splitter is normally hung off?
 *
 *  Only the SUGGESTION is opinionated. Plant cascades below plant and comes off
 *  an OLT's PON, so those two are what nearest-wins is allowed to propose — a
 *  CPE that happens to be the closest pin is never what feeds a splitter, and
 *  proposing it would teach the operator to distrust the prefill.
 *
 *  The picker below stays PERMISSIVE (an FDB on a switch's fibre tray is a real
 *  record), because narrowing what can be chosen would make this flow poorer
 *  than the form it replaces. The server validates either way, and the one rule
 *  it enforces — a monitored device may not sit under a passive — is about the
 *  child, not the parent. */
const isLikelyFeeder = (d: OrgDevice): boolean =>
  isPassiveType(d.device_type) || (d.device_type ?? "").toUpperCase() === "OLT"

export interface Feeder {
  device: OrgDevice
  /** straight-line metres from the click to that box, for the sheet to state */
  meters: number
}

/** The box a new one dropped HERE most likely hangs off: nearest placed
 *  candidate within `FEEDER_RADIUS_KM`, or null.
 *
 *  Nearest-wins is the right rule for exactly the reason an ISP hangs a customer
 *  off the nearest splitter — fibre costs money and nobody runs a drop past a
 *  closer box. It is a SUGGESTION and the sheet renders it as one; the operator
 *  confirms it by reading the name in the menu item before they ever click. */
export function nearestFeeder(
  lat: number, lng: number, devices: OrgDevice[],
): Feeder | null {
  let best: Feeder | null = null
  for (const d of devices) {
    if (!placed(d) || !isLikelyFeeder(d)) continue
    const km = distanceKm(lat, lng, d.lat, d.lng)
    if (km > FEEDER_RADIUS_KM) continue
    if (!best || km * 1000 < best.meters) best = { device: d, meters: km * 1000 }
  }
  return best
}

/** How close a splitter has to be for a customer dropped here to be assumed to
 *  hang off it. A drop is the last few metres into a house, so this is a tight
 *  radius on purpose: past it the guess stops being a guess about THIS drop and
 *  becomes a guess about the plant, and a wrong drop record inflates one
 *  splitter's load while starving another's. */
export const DROP_RADIUS_KM = 0.3

/** The splitter a customer dropped here most likely hangs off, or null.
 *
 *  Passives ONLY. An OLT is never the answer — that was the lie the straight
 *  ONU-to-OLT line used to tell, and recording it as a drop would make it a
 *  stored one. Null is a perfectly good answer here: the pin still lands, and
 *  the drop stays unrecorded rather than recorded wrongly. */
export function nearestPassive(
  lat: number, lng: number, devices: OrgDevice[],
): Feeder | null {
  let best: Feeder | null = null
  for (const d of devices) {
    if (!placed(d) || !isPassiveType(d.device_type)) continue
    const km = distanceKm(lat, lng, d.lat, d.lng)
    if (km > DROP_RADIUS_KM) continue
    if (!best || km * 1000 < best.meters) best = { device: d, meters: km * 1000 }
  }
  return best
}

/** Every box that could feed a new one, nearest first — what the "change the
 *  feeder" picker lists. Unplaced candidates come LAST rather than being
 *  dropped: an OLT nobody has pinned yet is still the box the fibre comes from,
 *  and hiding it would make this picker narrower than the form it replaces. */
export function feederOptions(
  lat: number, lng: number, devices: OrgDevice[], excludeId?: number,
): Array<{ device: OrgDevice; meters: number | null }> {
  return devices
    .filter((d) => d.id !== excludeId)
    .map((d) => ({
      device: d,
      meters: placed(d) ? distanceKm(lat, lng, d.lat, d.lng) * 1000 : null,
    }))
    .sort((a, b) =>
      (a.meters ?? Infinity) - (b.meters ?? Infinity)
      || a.device.name.localeCompare(b.device.name))
}

/** The OLT at the head of a chain, if the chain reaches one.
 *
 *  Which OLT a splitter ultimately hangs off is what says whose roster its
 *  customers come from and which PON labels are real, so it is worth resolving
 *  even though nothing stores it. A chain that heads into a switch returns null
 *  and the sheet stops offering a PON — a splitter on a switch's tray has no PON
 *  to be on, and offering the field there would invite a label that means
 *  nothing. */
export function oltHead(
  parent: OrgDevice | null, byId: Map<number, OrgDevice>,
): OrgDevice | null {
  if (!parent) return null
  const isOlt = (d: OrgDevice) => (d.device_type ?? "").toUpperCase() === "OLT"
  if (isOlt(parent)) return parent
  if (!isPassiveType(parent.device_type)) return null
  const { head } = feedChain(parent, byId)
  return head && isOlt(head) ? head : null
}

/** What PON a new box under `parent` is on, and whether that is settled.
 *
 *  `inherited` is the load-bearing half. Under another passive the answer is its
 *  parent's PON and there is nothing to decide, so the sheet renders it as a
 *  fact rather than a field — one fibre goes into a splitter, and asking again
 *  is how the two disagree. Under an OLT there IS a choice, and it must be made
 *  from that OLT's real roster labels rather than typed: `EPON0/4` typed as
 *  `0/4` is a splitter whose customer picker is silently empty ten steps later.
 *
 *  It walks the whole passive CHAIN rather than reading the parent's own column,
 *  because plant records get filled in out of order: the splitter somebody
 *  entered first often has no PON on it while its own feeder does. Taking the
 *  nearest recorded one up the chain is not a guess — there is one fibre in that
 *  chain — and it is the difference between a cascade inheriting a real label and
 *  a cascade of blanks.
 *
 *  A chain with NO recorded PON anywhere returns `inherited: false`, which is
 *  what re-opens the question: the sheet then offers the OLT's own labels rather
 *  than reporting "not recorded" and leaving no way to fix it. */
export function ponFor(
  parent: OrgDevice | null, byId: Map<number, OrgDevice>,
): { pon: string | null; inherited: boolean } {
  if (!parent || !isPassiveType(parent.device_type)) {
    return { pon: null, inherited: false }
  }
  let cur: OrgDevice | undefined = parent
  const seen = new Set<number>()
  while (cur && !seen.has(cur.id) && isPassiveType(cur.device_type)) {
    seen.add(cur.id)
    const pon = (cur.pon_port ?? "").trim()
    if (pon) return { pon, inherited: true }
    const up = feedOf(cur)
    cur = up != null ? byId.get(up) : undefined
  }
  return { pon: null, inherited: false }
}

/** The passive plant an OLT/PON focus leaves on the map.
 *
 *  The focus entered from an OLT's panel ("Show on map") narrowed SUBSCRIBERS
 *  and left every splitter in the org drawn, so scoping one OLT during a cut
 *  still put the neighbouring villages' plant on screen — the exact clutter the
 *  focus exists to remove (operator, 2026-08-06). Plant is narrowed with the
 *  drops it carries now.
 *
 *  Two ways in, and the first is the safety one:
 *
 *   1. **Any box a DRAWN subscriber hangs off**, whatever the topology says.
 *      That drop line has to have both ends — a dotted span running to a point
 *      where nothing is drawn reads as a rendering fault, and it is the same
 *      rule that keeps `drop_lines` from ever floating below the `passives`
 *      zoom floor. A mis-recorded drop pointing at another OLT's splitter keeps
 *      that splitter drawn for exactly this reason.
 *   2. **Plant whose feed chain HEADS AT the scoped OLT**, on a picked PON. The
 *      chain is the structural fact and it holds for a box nobody has recorded a
 *      drop on yet, which on a fresh survey is most of them.
 *
 *  …plus the chain ABOVE anything kept, for the same reason as (1): hiding a
 *  feeder would leave the cable into a drawn box ending in empty ground.
 *
 *  A box the record cannot place on a PON stays under a PON pick. `pon_port` is
 *  operator-entered and plant records get filled in out of order, so a blank
 *  column says nobody wrote it down — never "on some other PON" — and a filter
 *  may not answer a question the data never answered. Same instinct as the
 *  splitter panel's "recorded is never occupied": unknown is not spare.
 *
 *  GEAR IS NEVER NARROWED, only plant. A switch or an OLT has a state, an outage
 *  and a page of its own, while a focus is a density control: it may hide
 *  reference material and never a fact. */
export function plantInScope(
  scope: { deviceId: number; pons: string[] },
  devices: OrgDevice[],
  byId: Map<number, OrgDevice>,
  shown: ReadonlyArray<{ drop_passive_id: number | null }>,
): Set<number> {
  const keep = new Set<number>()
  for (const p of shown) if (p.drop_passive_id != null) keep.add(p.drop_passive_id)
  const pons = new Set(scope.pons)
  for (const d of devices) {
    if (keep.has(d.id) || !isPassiveType(d.device_type)) continue
    if (feedChain(d, byId).head?.id !== scope.deviceId) continue
    const { pon } = ponFor(d, byId)
    if (pons.size > 0 && pon && !pons.has(pon)) continue
    keep.add(d.id)
  }
  for (const id of [...keep]) {
    const d = byId.get(id)
    if (d) for (const a of feedChain(d, byId).passives) keep.add(a.id)
  }
  return keep
}

/** A free name of the shape `SPL-7`, org-wide.
 *
 *  Deliberately NOT derived from the parent (`SPL-4-2`), which reads fine at one
 *  hop and becomes `SPL-4-2-3-2` down a cascade. Deliberately not left blank
 *  either: a required field with no default is the thing that makes a fast
 *  capture stop and think, and every one of these gets renamed to a landmark by
 *  whoever stands at it anyway. */
export function suggestPlantName(kind: PlantKind, devices: OrgDevice[]): string {
  const stem = NAME_STEM[kind]
  const taken = new Set(devices.map((d) => d.name.trim().toUpperCase()))
  for (let n = 1; n < 10_000; n++) {
    const candidate = `${stem}-${n}`
    if (!taken.has(candidate)) return candidate
  }
  return stem
}

/** Distinct PON labels an OLT's roster actually reports, in a readable order.
 *
 *  The picker's whole vocabulary, and it is the roster's OWN spelling rather
 *  than anything reformatted: what the walk stores is what the drop record has
 *  to match, so a label prettified on the way to the operator is a label that
 *  matches nothing on the way back.
 *
 *  Only BLANKS are dropped. It is tempting to filter the odd bare `60` these
 *  agents emit, but the same rule would take a legitimate bare `1` or `4` with
 *  it on the GPON builds that number their PONs that way — and a picker missing
 *  the real label is far worse than one carrying a junk row nobody clicks. An
 *  OLT whose walk produced nothing returns an empty list and the sheet falls
 *  back to a plain field, saying why. */
/** What a PON dropdown must offer: the roster's labels, plus WHATEVER IS
 *  ALREADY STORED on the row, even when no walk reports it.
 *
 *  Not a nicety. A Select with no item for its own value renders BLANK, and
 *  saving that blank silently unstamps the PON of a splitter somebody opened to
 *  change its name — the same trap the GPON vendor dropdown and the device-type
 *  Select have both had to be fixed for. A label the roster no longer carries is
 *  a fact about the walk (a stale sweep, a renamed port, a PON with no ONUs
 *  registered on it yet), never permission to discard what the operator typed. */
export function ponOptions(
  pons: string[], current: string | null | undefined,
): string[] {
  const cur = (current ?? "").trim()
  if (!cur || pons.includes(cur)) return pons
  return [cur, ...pons]
}

export function ponLabels(ports: Array<string | null | undefined>): string[] {
  const seen = new Set<string>()
  for (const p of ports) {
    const s = (p ?? "").trim()
    if (s) seen.add(s)
  }
  return [...seen].sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }))
}
