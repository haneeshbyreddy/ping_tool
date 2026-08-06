// Field workers on the map: where the crew is, from the Traccar Client on each
// worker's own phone (`central/field.py`, `/field/track`).
//
// This layer follows the Subscribers layer's discipline exactly, and for the same
// reason — the map exists to show PLANT, and anything else on it is subordinate:
// opt-in and remembered per browser, its own mark (never a device pin shape),
// stacked BELOW every device pin, and out of the clustering pass (a site badge
// mixing staff with gear would count two different things).
//
// It also breaks one rule the plant layers keep, deliberately: **a worker never
// takes a status tone.** `--success`/`--warning`/`--destructive` on this map mean
// something about the NETWORK, and a person is not a network state. The one
// exception is the state below that IS an alarm — see `quiet`.
import { cachedDivIcon, esc } from "@/map/pins"
import { ago } from "@/lib/format"
import type { FieldWorker } from "@/lib/types"

/** The four states, which must never render alike.
 *
 *  Collapsing any two of these makes the map lie, and the specific lie is always
 *  the same one: "last known 40 minutes ago" drawn as "here now".
 *
 *   - `live`  — on shift, fix fresh: here, now.
 *   - `quiet` — on shift, gone quiet: phone dead, no signal, or (much the most
 *               likely) the handset's OEM battery manager killed the background
 *               service. This is the ALARM the two-tap shift declaration exists
 *               to produce — a worker marked on-shift with no fixes arriving is
 *               a discrepancy no server-side code could otherwise detect.
 *   - `off`   — shift ended: went home. NOT a fault, and must not look like one.
 *   - `never` — set up but never reported. Has no coordinates at all, so it is
 *               not a mark; it is a COUNT, and the layer has to state it or an
 *               un-provisioned crew and an off-shift one look identical (both
 *               draw nothing).
 */
export type WorkerState = "live" | "quiet" | "off" | "never"

/** Classified in the SPA, not shipped by the server: freshness ticks with the
 *  clock, so a state stamped at response time would go on claiming "here now"
 *  for as long as the tab stayed open. `freshS` still comes FROM the server
 *  (`fresh_s`), so the threshold itself has one source. */
export function workerState(w: FieldWorker, freshS: number, now: number): WorkerState {
  if (!w.last_fix) return w.on_shift ? "quiet" : "never"
  if (!w.on_shift) return "off"
  const age = (now - Date.parse(w.last_fix.ts)) / 1000
  return Number.isFinite(age) && age <= freshS ? "live" : "quiet"
}

/** Does this worker have a position to draw at all? `never` has none by
 *  definition — and a worker who is on shift but has never reported (`quiet`
 *  with no fix) has none either, which is exactly the case the layer's count
 *  has to speak for instead. */
export const workerPlaced = (w: FieldWorker): boolean => w.last_fix != null

/** Devices sit at 0–1000 and the subscriber layer bottoms out at -200. Workers
 *  go below both: a worker dot must never outshout a device that is down, and it
 *  must not bury a subscriber pin either — the drop is the thing a crew was sent
 *  to. `quiet` is lifted within the band because it is the one state here that
 *  is asking for attention. */
export function workerZIndex(state: WorkerState): number {
  return state === "quiet" ? -250 : -300
}

/** One or two letters for the badge.
 *
 *  Text inside the mark, which nothing else on this map has — that is most of
 *  what makes a worker unmistakably not a device. It also answers the question
 *  an operator actually has with three vans out: not "is somebody there" but
 *  "which one". */
export function workerInitials(username: string): string {
  const parts = username.split(/[\s._-]+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return (parts[0] ?? username).slice(0, 2).toUpperCase()
}

/** What the mark says on hover. Every state names its own meaning rather than
 *  leaving the colour to carry it — "gone quiet" in particular is a sentence, not
 *  a shade, and it is the one an operator has to act on. */
export function workerTitle(w: FieldWorker, state: WorkerState): string {
  const seen = w.last_fix ? ago(w.last_fix.ts) : "never"
  const batt = w.last_fix?.battery_pct != null ? ` · battery ${w.last_fix.battery_pct}%` : ""
  const acc = w.last_fix?.accuracy_m != null ? ` · ±${Math.round(w.last_fix.accuracy_m)} m` : ""
  if (state === "live") return `${w.username} · here now (${seen})${acc}${batt}`
  if (state === "quiet")
    return `${w.username} · on shift but GONE QUIET · last fix ${seen}. `
      + `Phone off, no signal, or the handset's battery manager killed the tracker${batt}`
  if (state === "off") return `${w.username} · shift ended · last seen ${seen}${acc}`
  return `${w.username} · never reported`
}

/** The mark. Rendered `interactive={false}` at the callsite, like every other
 *  subordinate thing on this map: it must never swallow a placement click, and
 *  there is no per-worker card to open — the hover title carries the detail on
 *  the desktop this map is read on, and Settings → Location tracking carries the
 *  same facts as text for anyone who needs them in a list. */
export function workerIcon(w: FieldWorker, state: WorkerState) {
  return cachedDivIcon(
    `<div class="wisp-worker wisp-worker--${state}" title="${esc(workerTitle(w, state))}">`
    + `<span class="wisp-worker__mark">${esc(workerInitials(w.username))}</span></div>`)
}

/** Today's route, as a polyline weight/opacity.
 *
 *  A trail is HISTORY — where the van has been, not a claim about now — so it
 *  stays lighter than every plant line at every state, and an ended shift's
 *  trail fades further still. It is never dashed: a dash on this map means "not
 *  a surveyed path", and a GPS trail is the one line here that IS measured.
 */
export function trailStyle(state: WorkerState): { weight: number; opacity: number } {
  if (state === "live") return { weight: 3, opacity: 0.7 }
  if (state === "quiet") return { weight: 3, opacity: 0.6 }
  // An ended shift's trail is the quietest thing here — but not so quiet it
  // stops existing. The first cut sat at 0.3 uncased and was gone over
  // satellite; the casing at the callsite is what actually earns the low alpha.
  return { weight: 2.5, opacity: 0.45 }
}

/** Layer-level census, for the toggle's own line.
 *
 *  The layer must be able to say "3 of 6 reporting" rather than drawing three
 *  marks and letting the operator infer the rest — same rule the splitter layer
 *  keeps with "N of M subscribers mapped to a splitter". Without it, a crew whose
 *  phones were never set up renders exactly like a crew that has all gone home.
 */
export function workerCensus(workers: FieldWorker[], freshS: number, now: number) {
  let live = 0, quiet = 0, off = 0, never = 0, placed = 0
  for (const w of workers) {
    const s = workerState(w, freshS, now)
    if (s === "live") live++
    else if (s === "quiet") quiet++
    else if (s === "off") off++
    else never++
    // Counted off the FIX, not off the state: a worker on shift who has never
    // reported is `quiet` — the alarm — and still has nothing to draw. Deriving
    // "how many are on the map" from the states would over-count by exactly the
    // people the operator most needs to know are missing.
    if (workerPlaced(w)) placed++
  }
  return { live, quiet, off, never, total: workers.length, placed }
}
