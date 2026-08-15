// Sanity harness for the REAL web/src/lib/replay.ts, transpiled — never a
// copy of the logic, which would pass just as happily against a wrong walk.
// There is no frontend test suite in this repo (`tsc -b` is the check), and
// adding a node dependency to the Python suite is a worse trade than a
// documented command. `replay.ts` is import-free ON PURPOSE, partly so this
// one line always works:
//
//   cd web && npx tsc src/lib/replay.ts --ignoreConfig --outDir /tmp/replaychk \
//     --module commonjs --target es2020 \
//     && RECON=/tmp/replaychk/replay.js node checks/replay.check.cjs
//
// Every case below is a rule the reconstruction must not lose: half-open
// intervals, window edges, open ends, merging, the two floors, the
// down-outranks-blind precedence, and the never-count-a-parent's-outage rule.
const assert = require("assert")
const { buildReconstruction } = require(process.env.RECON || "../../.replay-build/replay.js")

const S = 1000, U = 2000            // window [1000, 2000)
const W = { since: S, until: U, now: U }
let n = 0
const t = (name, fn) => { fn(); n++; console.log("ok   " + name) }

t("a plain window with no spans is up throughout", () => {
  const r = buildReconstruction([{ id: 1, down: [] }], W)
  assert.equal(r.stateAt(1, S), "up")
  assert.equal(r.stateAt(1, 1500), "up")
  assert.equal(r.events.length, 0)
  assert.equal(r.nextEventAfter(S), null)
})

t("intervals are half-open: down at start, up at end", () => {
  const r = buildReconstruction([{ id: 1, down: [{ start: 1200, end: 1300 }] }], W)
  assert.equal(r.stateAt(1, 1199), "up")
  assert.equal(r.stateAt(1, 1200), "down")
  assert.equal(r.stateAt(1, 1299), "down")
  assert.equal(r.stateAt(1, 1300), "up")
})

t("an open-ended span runs to the window edge", () => {
  const r = buildReconstruction([{ id: 1, down: [{ start: 1800, end: null }] }], W)
  assert.equal(r.stateAt(1, 1999), "down")
  assert.equal(r.stateAt(1, 5000), "down")   // still open past the window
  assert.deepEqual(r.downBars(1), [[1800, U]])
  assert.deepEqual(r.events, [1800])         // no phantom close event
})

t("a span crossing the left edge keeps covering the edge", () => {
  const r = buildReconstruction([{ id: 1, down: [{ start: 500, end: 1200 }] }], W)
  assert.equal(r.stateAt(1, S), "down")
  assert.deepEqual(r.downBars(1), [[S, 1200]])
  assert.deepEqual(r.events, [1200])         // the start is outside the window
})

t("adjacent and overlapping spans merge into one bar", () => {
  const r = buildReconstruction([{ id: 1, down: [
    { start: 1100, end: 1200 }, { start: 1200, end: 1250 },
    { start: 1240, end: 1300 }] }], W)
  assert.deepEqual(r.downBars(1), [[1100, 1300]])
})

t("spans arriving out of order still merge", () => {
  const r = buildReconstruction([{ id: 1, down: [
    { start: 1400, end: 1500 }, { start: 1100, end: 1200 }] }], W)
  assert.deepEqual(r.downBars(1), [[1100, 1200], [1400, 1500]])
})

t("before an entity's own floor is unknown, never up", () => {
  const r = buildReconstruction([{ id: 1, since: 1400, down: [] }], W)
  assert.equal(r.stateAt(1, 1399), "unknown")
  assert.equal(r.stateAt(1, 1400), "up")
  assert.deepEqual(r.unknownBars(1), [[S, 1400]])
})

t("the org floor applies to every entity", () => {
  const r = buildReconstruction([{ id: 1, down: [] }, { id: 2, down: [] }],
                                { ...W, orgSince: 1300 })
  assert.equal(r.stateAt(1, 1299), "unknown")
  assert.equal(r.stateAt(2, 1299), "unknown")
  assert.equal(r.stateAt(1, 1301), "up")
})

t("the LATER of the org floor and the entity floor wins", () => {
  const r = buildReconstruction([{ id: 1, since: 1600, down: [] }],
                                { ...W, orgSince: 1300 })
  assert.equal(r.stateAt(1, 1599), "unknown")
  assert.equal(r.stateAt(1, 1600), "up")
})

t("a probe blackout is unknown, never up", () => {
  const r = buildReconstruction(
    [{ id: 1, down: [], blind: [{ start: 1200, end: 1400 }] }], W)
  assert.equal(r.stateAt(1, 1199), "up")
  assert.equal(r.stateAt(1, 1300), "unknown")
  assert.equal(r.stateAt(1, 1400), "up")
})

t("DOWN OUTRANKS BLIND: a recorded outage is a positive fact", () => {
  const r = buildReconstruction([{
    id: 1, down: [{ start: 1100, end: 1500 }],
    blind: [{ start: 1200, end: 1400 }],
  }], W)
  assert.equal(r.stateAt(1, 1300), "down")
})

t("step lands on transitions, not on ticks", () => {
  const r = buildReconstruction([
    { id: 1, down: [{ start: 1100, end: 1150 }] },
    { id: 2, down: [{ start: 1700, end: null }], since: 1050 },
  ], W)
  assert.deepEqual(r.events, [1050, 1100, 1150, 1700])
  assert.equal(r.nextEventAfter(S), 1050)
  assert.equal(r.nextEventAfter(1150), 1700)
  assert.equal(r.nextEventAfter(1700), null)
  assert.equal(r.prevEventBefore(1700), 1150)
  assert.equal(r.prevEventBefore(1050), null)
})

t("an UNREACHABLE span renders down but is never COUNTED", () => {
  const r = buildReconstruction([{ id: 1, down: [
    { start: 1100, end: 1200, own: false },   // my parent was down
    { start: 1400, end: 1450 },
  ] }], W)
  assert.equal(r.stateAt(1, 1150), "down")            // renders down
  assert.equal(r.downSecondsIn(1, S, U), 50)          // counts only its own
  assert.equal(r.outageCountIn(1, S, U), 1)
  assert.deepEqual(r.downBars(1), [[1100, 1200], [1400, 1450]])
})

t("an entity the reply never mentioned is unknown, not healthy", () => {
  const r = buildReconstruction([{ id: 1, down: [] }], W)
  assert.equal(r.stateAt(99, 1500), "unknown")
  assert.deepEqual(r.unknownBars(99), [[S, U]])
})

t("statesAt answers for every id at once", () => {
  const r = buildReconstruction([
    { id: 1, down: [{ start: 1100, end: 1300 }] },
    { id: "MAC-A", down: [], since: 1900 },     // string ids: the Wave 3 shape
  ], W)
  const at = r.statesAt(1200)
  assert.equal(at.get(1), "down")
  assert.equal(at.get("MAC-A"), "unknown")
})

t("downSecondsIn clips to the asked-for range", () => {
  const r = buildReconstruction([{ id: 1, down: [{ start: 1100, end: 1900 }] }], W)
  assert.equal(r.downSecondsIn(1, S, U), 800)
  assert.equal(r.downSecondsIn(1, 1500, 1600), 100)
  assert.equal(r.downSecondsIn(1, 100, 200), 0)
})

t("a zero-length span is dropped rather than drawn as a hairline", () => {
  const r = buildReconstruction([{ id: 1, down: [{ start: 1200, end: 1200 }] }], W)
  assert.deepEqual(r.downBars(1), [])
  assert.equal(r.stateAt(1, 1200), "up")
})

t("unknown bars merge the floor with a blackout that touches it", () => {
  const r = buildReconstruction([{
    id: 1, since: 1300, down: [], blind: [{ start: 1300, end: 1400 }],
  }], W)
  assert.deepEqual(r.unknownBars(1), [[S, 1400]])
})

t("eventFloorAt is the cheapest key a projection can memoize on", () => {
  const r = buildReconstruction([
    { id: 1, down: [{ start: 1100, end: 1150 }] },
    { id: 2, down: [{ start: 1700, end: null }] },
  ], W)
  assert.equal(r.eventFloorAt(1000), S)      // before the first event
  assert.equal(r.eventFloorAt(1099), S)
  assert.equal(r.eventFloorAt(1100), 1100)   // the event itself
  assert.equal(r.eventFloorAt(1149), 1100)
  assert.equal(r.eventFloorAt(1150), 1150)
  assert.equal(r.eventFloorAt(1699), 1150)
  assert.equal(r.eventFloorAt(1999), 1700)
  // and the whole point: the states at t and at its floor are identical
  for (let t = S; t < U; t += 7) {
    for (const id of [1, 2]) {
      assert.equal(r.stateAt(id, t), r.stateAt(id, r.eventFloorAt(t)),
                   `state drifted from its floor at t=${t} for ${id}`)
    }
  }
})


console.log(`\n${n} checks OK`)
