// THE RECONSTRUCTION — the one place this product decides what the record can
// say about a moment in the past. A pure sorted-interval walk: no React, no
// fetch, no clock. BOTH projections (the map replay and the outage Marey) read
// this and nothing else, so they can never disagree about when something
// dropped — the count-agreement rule applied to a timeline.
//
// THREE STATES, and `unknown` is the load-bearing one. The frozen doctrine
// says a reading nobody can stand behind must not render like a live one; the
// same rule pointed at the past says a MOMENT nobody was watching must not
// render like a moment that was fine. Painting a green pin is the worst lie
// this map can tell, so `unknown` covers every case where the record cannot
// answer:
//
//   1. PRE-HISTORY — before the org's own floor (`org_since`: the earlier of
//      when the org row appeared and its first outage). Nothing before that
//      was being recorded, so nothing before it can be claimed.
//   2. THE ENTITY DID NOT EXIST — before its own floor (`since`, a device's
//      created_at). A box entered on Tuesday was not "up" on Monday; it was
//      not there. Without this, a replay of last month renders today's fleet.
//   3. THE PROBE WAS SILENT — a `blind` window. The edge dials central, so a
//      dead probe produces no samples and the FSM freezes: the absence of an
//      outage row across those hours is not evidence of health.
//
// PRECEDENCE: down > unknown > up.
//   - `down` outranks `blind` because an outage span is a POSITIVE record
//     about that entity — the FSM opened it and only closed it when recovery
//     was observed — so a probe blackout inside an outage does not make the
//     outage unknown; it makes the recovery time uncertain, which the span's
//     own end already reports.
//   - `blind` outranks `up` because `up` is the only state here that is
//     inferred from ABSENCE, and absence is exactly what a blackout produces.
//   - a floor outranks everything: an entity that did not exist cannot have a
//     span covering it anyway, so this only ever guards nonsense.
//
// INTERVALS ARE HALF-OPEN, [start, end): an outage 10:00 to 11:00 is down at
// 10:00 and up at 11:00 — the same convention `day_availability` counts
// seconds under, so a Marey bar and the availability strip measure one thing.
//
// GENERIC BY ENTITY ID on purpose. Wave 3 feeds ONU transitions through the
// same walk with string ids (a MAC) and gets step-to-next-event, the Marey and
// the map projection for free; nothing here knows what a device is.

export type ReplayState = "up" | "down" | "unknown"
export type EntityId = number | string

export interface Span {
  start: number             // epoch seconds
  end: number | null        // null = still open at the end of the window
  // `false` marks a span that is a RESTATEMENT of somebody else's outage
  // (UNREACHABLE: my parent was down). It still renders as down-family — the
  // live map does — but it must never be COUNTED as this entity's downtime,
  // or one OLT's outage lands on every box behind it. Exactly the rule
  // `analytics.device_reliability` keeps by counting final_state == DOWN only.
  own?: boolean
}

export interface EntityInput {
  id: EntityId
  since?: number | null     // this entity's own recording floor
  down: Span[]
  blind?: Span[]
}

export interface ReplayWindow {
  since: number
  until: number
  now: number
  orgSince?: number | null
}

interface Norm {
  id: EntityId
  floor: number             // -Infinity = no floor known
  down: Array<[number, number]>
  owned: Array<[number, number]>
  blind: Array<[number, number]>
}

export interface Reconstruction {
  since: number
  until: number
  now: number
  ids: EntityId[]
  /** Every transition inside the window, sorted and de-duplicated. */
  events: number[]
  stateAt(id: EntityId, t: number): ReplayState
  statesAt(t: number): Map<EntityId, ReplayState>
  nextEventAfter(t: number): number | null
  prevEventBefore(t: number): number | null
  /**
   * The last transition at or before `t`, falling back to the window start.
   * BETWEEN TWO EVENTS NOTHING CHANGES — that is what an event is — so this
   * is the cheapest possible key for anything that renders a whole fleet at
   * T: it moves only when the picture does. Playback runs at ten frames a
   * second, and without it every frame would rebuild the device rows, the
   * clusters and the link set to draw an identical map.
   */
  eventFloorAt(t: number): number
  /** Merged down intervals, clipped to the window (for drawing bars). */
  downBars(id: EntityId): Array<[number, number]>
  /** Merged unknown intervals, clipped to the window (floor included). */
  unknownBars(id: EntityId): Array<[number, number]>
  /** Seconds this entity was down BY ITS OWN FAULT inside [from, to). */
  downSecondsIn(id: EntityId, from: number, to: number): number
  outageCountIn(id: EntityId, from: number, to: number): number
}

const OPEN = Number.POSITIVE_INFINITY

function merge(spans: Span[], keep: (s: Span) => boolean): Array<[number, number]> {
  const raw: Array<[number, number]> = []
  for (const s of spans) {
    if (!keep(s)) continue
    const end = s.end == null ? OPEN : s.end
    if (end > s.start) raw.push([s.start, end])
  }
  raw.sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const out: Array<[number, number]> = []
  for (const iv of raw) {
    const last = out[out.length - 1]
    // Adjacent spans (one ends exactly where the next begins) merge: a flap
    // that re-opened in the same second is one bar, not two hairlines.
    if (last && iv[0] <= last[1]) last[1] = Math.max(last[1], iv[1])
    else out.push([iv[0], iv[1]])
  }
  return out
}

function covers(ivs: Array<[number, number]>, t: number): boolean {
  // Binary search: the map calls this once per device per scrub tick, and a
  // fleet-scale linear scan through a 90-day span list is the one place this
  // walk could get slow.
  let lo = 0
  let hi = ivs.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (t < ivs[mid][0]) hi = mid - 1
    else if (t >= ivs[mid][1]) lo = mid + 1
    else return true
  }
  return false
}

function clip(ivs: Array<[number, number]>, since: number, until: number
): Array<[number, number]> {
  const out: Array<[number, number]> = []
  for (const [a, b] of ivs) {
    const lo = Math.max(a, since)
    const hi = Math.min(b, until)
    if (hi > lo) out.push([lo, hi])
  }
  return out
}

function overlapSeconds(ivs: Array<[number, number]>, from: number, to: number): number {
  let total = 0
  for (const [a, b] of ivs) {
    const lo = Math.max(a, from)
    const hi = Math.min(b, to)
    if (hi > lo) total += hi - lo
  }
  return total
}

export function buildReconstruction(
  entities: EntityInput[], win: ReplayWindow,
): Reconstruction {
  const { since, until, now } = win
  const orgFloor = win.orgSince ?? Number.NEGATIVE_INFINITY
  const byId = new Map<EntityId, Norm>()
  const eventSet = new Set<number>()

  const mark = (t: number) => {
    if (t > since && t < until) eventSet.add(t)
  }

  for (const e of entities) {
    const floor = Math.max(orgFloor, e.since ?? Number.NEGATIVE_INFINITY)
    const down = merge(e.down, () => true)
    const owned = merge(e.down, (s) => s.own !== false)
    const blind = merge(e.blind ?? [], () => true)
    byId.set(e.id, { id: e.id, floor, down, owned, blind })
    if (Number.isFinite(floor)) mark(floor)
    for (const ivs of [down, blind]) {
      for (const [a, b] of ivs) { mark(a); if (b !== OPEN) mark(b) }
    }
  }

  const events = [...eventSet].sort((a, b) => a - b)
  const ids = entities.map((e) => e.id)

  const stateAt = (id: EntityId, t: number): ReplayState => {
    const n = byId.get(id)
    // An entity the reply never mentioned is unanswerable, not healthy — a
    // device created after this window was fetched lands here.
    if (!n) return "unknown"
    if (t < n.floor) return "unknown"
    if (covers(n.down, t)) return "down"
    if (covers(n.blind, t)) return "unknown"
    return "up"
  }

  const unknownBars = (id: EntityId): Array<[number, number]> => {
    const n = byId.get(id)
    if (!n) return [[since, until]]
    const bars: Array<[number, number]> = []
    if (n.floor > since) bars.push([since, Math.min(n.floor, until)])
    for (const b of clip(n.blind, since, until)) bars.push(b)
    return merge(bars.map(([a, b]) => ({ start: a, end: b })), () => true)
  }

  return {
    since, until, now, ids, events, stateAt,
    statesAt: (t) => {
      const out = new Map<EntityId, ReplayState>()
      for (const id of ids) out.set(id, stateAt(id, t))
      return out
    },
    nextEventAfter: (t) => {
      for (const e of events) if (e > t) return e
      return null
    },
    prevEventBefore: (t) => {
      for (let i = events.length - 1; i >= 0; i--) if (events[i] < t) return events[i]
      return null
    },
    eventFloorAt: (t) => {
      let lo = 0
      let hi = events.length - 1
      let best = since
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (events[mid] <= t) { best = events[mid]; lo = mid + 1 } else hi = mid - 1
      }
      return best
    },
    downBars: (id) => clip(byId.get(id)?.down ?? [], since, until),
    unknownBars,
    downSecondsIn: (id, from, to) =>
      overlapSeconds(byId.get(id)?.owned ?? [], from, to),
    outageCountIn: (id, from, to) =>
      (byId.get(id)?.owned ?? []).filter(([a, b]) => b > from && a < to).length,
  }
}
