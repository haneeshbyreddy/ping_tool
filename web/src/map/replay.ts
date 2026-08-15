// THE MAP'S TIME SHIFT. Replay is not a second map: it is a PROJECTION of the
// device rows the live map already draws, applied before they enter any memo.
// Everything downstream — pinTone, pinIcon's silhouettes, buildClusters, the
// detail floors, the hover cards, the tree — then reads a past fleet through
// the one grammar this product has, and the two can never drift apart.
//
// It also isolates replay from the live stream WITHOUT touching the event
// stream: a `/api/inventory` refetch fired by SSE re-runs this projection at
// the SAME T and yields the same states, so a live invalidation cannot
// repaint a replay tint. What a refetch may still change is which devices
// exist and where their pins are, which is correct.
import { cachedDivIcon } from "@/map/pins"
import type { OrgDevice } from "@/lib/types"
import type { Reconstruction, ReplayState } from "@/lib/replay"

// Live readings that a past map may NOT claim. An SNMP figure is a statement
// about NOW: a walk that landed four minutes ago says nothing about last
// Tuesday, so every reading and its freshness stamp is cleared and the panels
// fall through to the same "not measured" wording a device with no walk gets.
// Identity, geometry and topology survive untouched — where a box IS, what it
// is called and what it hangs off are not readings.
type Blanked = Pick<OrgDevice,
  | "latency_ms" | "packet_loss" | "jitter_ms"
  | "onus_total" | "onus_online" | "onus_warn" | "onus_crit" | "onus_rx"
  | "fiber_cuts" | "dup_macs" | "optics_updated_at"
  | "ports_down" | "ports_bw_low" | "ports_bw_high" | "ports_updated_at"
  | "cameras_total" | "cameras_down" | "cameras_updated_at"
  | "health_cpu_pct" | "health_mem_pct" | "health_mem_used_bytes"
  | "health_mem_total_bytes" | "health_temp_c" | "health_updated_at"
  | "outage_started_at">

const BLANK: Blanked = {
  latency_ms: null, packet_loss: null, jitter_ms: null,
  onus_total: null, onus_online: null, onus_warn: null, onus_crit: null,
  onus_rx: null, fiber_cuts: 0, dup_macs: 0, optics_updated_at: null,
  ports_down: 0, ports_bw_low: 0, ports_bw_high: 0, ports_updated_at: null,
  cameras_total: null, cameras_down: null, cameras_updated_at: null,
  health_cpu_pct: null, health_mem_pct: null, health_mem_used_bytes: null,
  health_mem_total_bytes: null, health_temp_c: null, health_updated_at: null,
  // Its only two consumers render it as a DURATION against the wall clock
  // ("down for 3h"), which in replay would count from the outage's real start
  // to right now instead of to T. A wrong duration beats no duration only if
  // you are not the person deciding whether to roll a van, so it goes.
  outage_started_at: null,
}

// `state_updated_at` is stamped with the CURRENT wall clock, not T, and that
// is not a fib about when the poll happened — nothing renders this field as a
// date in replay (the hover card's stale branch is unreachable because the
// stamp is fresh, and the panel is closed). It exists to satisfy `isStale`,
// which every tone helper gates on against Date.now(); a stamp of T would put
// a 7-day-old replay permanently in the muted "no recent poll" branch and the
// whole map would render grey. The caller refreshes it on a coarse tick so a
// paused replay cannot age past STALE_AFTER_S while somebody is reading it.
//
// A NULL reconstruction projects EVERYTHING to `unknown`, and that is not a
// loading placeholder — it is the honest state while the record is still being
// read. The alternative is worse than a grey map: replay would be announced by
// its banner while the pins were still showing live tones, which is precisely
// the past-read-as-now lie in reverse.
export function projectDevices(
  devices: OrgDevice[], recon: Reconstruction | null, at: number, stamp: string,
): OrgDevice[] {
  return devices.map((d) => {
    const st: ReplayState = recon ? recon.stateAt(d.id, at) : "unknown"
    if (st === "unknown") {
      // `state: null` is what pinTone reads as muted and what the hover card
      // words as "Not polled yet" — which is the true sentence here: at this
      // moment the record holds no poll for this box.
      return { ...d, ...BLANK, state: null, state_updated_at: null }
    }
    return {
      ...d, ...BLANK,
      state: st === "down" ? "DOWN" : "UP",
      state_updated_at: stamp,
    } as OrgDevice
  })
}

// ── THE ACCUMULATION LAYER ────────────────────────────────────────────────
// Outage minutes over the window, tinting marks on the LIVE map: the
// recurrence finder. "This branch blinks every evening" is invisible on a map
// of NOW and invisible in a replay of one moment; it is only visible as time
// summed onto a place.
//
// IT DESCRIBES THE PAST ON A LIVE MAP, so it may not touch Axis A. A branch
// that has been flapping for a week but is up right now is not an alarm, and
// painting it red would fabricate one on the screen that exists to show real
// ones. It therefore rides ONE identity hue — `--plane-fleet`, the probes'
// plane, 325deg — in opacity steps of that single hue. Never a multi-hue ramp
// (a rainbow scale reintroduces the red), never a mix toward the grey (that
// drags the hue as chroma falls — the documented `--map-live-quiet` lesson).
//
// A device that is DOWN NOW keeps its live status tone by construction: this
// draws a mark of its own UNDER the pin and never alters the pin, so an alarm
// outranks identity without needing a rule.

// Steps are a FRACTION of the window, not absolute minutes, so 24h and 7d are
// read the same way and one ladder serves both. The floor is deliberately low
// (0.1% is ~10 min in a week, ~1.5 min in a day): a short flap repeated every
// evening is exactly what this layer exists to surface, and it would vanish
// under a ladder tuned to long outages.
export const ACCUM_STEPS = [0.001, 0.01, 0.05, 0.15] as const
export const ACCUM_LABELS = ["under 1%", "1 to 5%", "5 to 15%", "over 15%"]

export function accumLevel(downS: number, windowS: number): number {
  if (windowS <= 0) return 0
  const f = downS / windowS
  let level = 0
  for (const step of ACCUM_STEPS) if (f >= step) level++
  return level                       // 0 = nothing worth saying
}

// Alpha steps of the one hue. Judged on real tiles in both themes: the layer
// has to be findable across a viewport without competing with a status dot,
// and `--plane-fleet` is already chroma-capped at the identity ceiling, so
// only the alpha moves.
const ACCUM_ALPHA = ["", "28%", "45%", "66%", "88%"]

export const accumColor = (level: number): string =>
  `color-mix(in srgb, var(--plane-fleet) ${ACCUM_ALPHA[level]}, transparent)`

// A COIN BEHIND THE MARK, not a halo. Hard edge, constant size, no blur, no
// pulse, no animation — the operator threw the coloured glow off the splitter
// pins for exactly those reasons ("i don't want those special effect"). 24px
// sits 2.2px outside the pin dot's own 19.6px border, so it reads as the
// mark's ground, and it nests well inside the optical ring at -7px, so an OLT
// with weak ONUs still shows that ring uncrowded.
//
// IT BORROWS THE PIN'S OWN LAYOUT rather than guessing an offset, and that is
// load-bearing. `.wisp-pin` is a dot-over-label column translated -50%, so the
// visible dot sits ABOUT 12px above the coordinate — but only while the label
// is drawn, and the label has a zoom floor of its own. A hardcoded nudge would
// therefore be right at one zoom and wrong at the next. Reusing the column
// puts the coin's centre at -(gap + label height) / 2, which is where the dot's
// centre is too: the coin's own height cancels, so they are concentric at
// every zoom and at any size. The placeholder label is `visibility:hidden`
// (holds its box) and inherits `.wisp-map-lowzoom`'s `display:none` with the
// real one, so the pair move together across the floor.
const ACCUM_PX = 24

export function accumIcon(level: number) {
  return cachedDivIcon(
    `<div class="wisp-pin" aria-hidden="true" style="pointer-events:none">`
    + `<span style="width:${ACCUM_PX}px;height:${ACCUM_PX}px;border-radius:9999px;`
    + `background:${accumColor(level)}"></span>`
    + `<span class="wisp-pin__label" style="visibility:hidden">&nbsp;</span></div>`)
}

// Its own stacking band: this is ground, so it goes under every mark it
// grounds — below gear (0+), below plant, below the subscriber layer.
export const ACCUM_Z = -400
