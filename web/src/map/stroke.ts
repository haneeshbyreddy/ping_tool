// ---- Stroke weights, scaled to zoom ----------------------------------------
//
// Every line on this map is a fixed PIXEL width, which means it is not fixed at
// all in the only sense that matters: as you zoom in, the same 2.5px stroke has
// to span more and more screen, so a cable that read as a solid link across a
// town becomes a hairline crossing the viewport. Zoomed OUT the lines are short
// and read as substantial; zoomed IN they thin out. Operator's report,
// 2026-08-02: "when i zoom out lines look thick enough but while zooming in they
// become too much thin."
//
// The cure is a multiplier that grows with zoom, and the curve matters. Each
// zoom level DOUBLES the ground scale, so holding stroke-to-span ratio constant
// would mean doubling the weight per level — 2.5px would reach 160px by z19,
// which is absurd. What is wanted is a constant ADDITION per doubling, i.e.
// linear in the zoom level and logarithmic in scale. Hence `1 + k·(z − z0)`.
//
// FLOOR AT 1.0, deliberately: the zoomed-out end was reported as already right,
// so this may only ever thicken, never thin. It changes nothing at or below
// `LINE_SCALE_FROM`, which keeps the whole fleet-altitude view — the one every
// weight in this app was originally judged at — byte-identical.
//
// ONE MULTIPLIER FOR EVERY LINE, and that is the load-bearing part. The relative
// weights here carry meaning that was tuned by eye and argued over: a feed (2.5)
// outranks a peer (2), a selected path (3.5) outranks both, a dark drop (4.5) is
// the heaviest thing in its layer, a hover adds half of what emphasis adds. A
// uniform multiplier preserves every one of those ratios by construction; a
// per-kind curve would silently re-rank them at some zoom nobody tested.
export const LINE_SCALE_FROM = 13
/** Added per zoom level above the floor. 0.135 puts an ordinary feed at ~3.5px
 *  by z17 and ~4.5px by z19 — visibly a cable rather than a hairline, without
 *  the map turning into a diagram of ribbons. */
export const LINE_SCALE_PER_ZOOM = 0.135
/** Google's tiles stop at 20; past ~z19 the extra span comes from a viewport
 *  showing one street, where more weight buys nothing. */
export const LINE_SCALE_MAX = 1.85

/** The stroke multiplier at this zoom. 1.0 at fleet altitude, rising to
 *  LINE_SCALE_MAX at street level. */
export function lineScale(zoom: number): number {
  return Math.min(
    LINE_SCALE_MAX,
    Math.max(1, 1 + (zoom - LINE_SCALE_FROM) * LINE_SCALE_PER_ZOOM))
}

const round = (n: number) => Math.round(n * 100) / 100

/** Scale a dashArray by the same factor as the stroke it belongs to.
 *
 *  THIS IS NOT OPTIONAL — it is the trap this map has already fallen into once.
 *  SVG dash lengths are absolute px while the stroke width is independent, so
 *  widening a dotted line without opening its gaps closes the dots into a SOLID
 *  line. On this map that is not a cosmetic regression: a dashed line means "not
 *  a surveyed path" and a solid one means traced fibre a splicing crew can quote
 *  drum off.
 *
 *  Scaling both by the SAME factor makes the rendered pattern exactly
 *  zoom-invariant, and that is provable rather than approximate. With
 *  `lineCap: "round"` a dash of length `on` at weight `w` paints a capsule of
 *  length `on + w` (half a round cap at each end) separated by `off − w`. Scale
 *  everything by k and the visible ratio is
 *
 *      (on·k + w·k) / (off·k − w·k)  =  (on + w) / (off − w)
 *
 *  — k cancels. So the ref-ONU line's dots read 1:1 dot-to-gap at z13 and at
 *  z19 alike; only their size changes. Scale the weight alone and that ratio
 *  runs away to solid. */
export function scaleDash(dash: string | undefined, k: number): string | undefined {
  if (!dash) return undefined
  return dash.trim().split(/\s+/).map((n) => round(Number(n) * k)).join(" ")
}

/** A casing can't reuse the stroke's own dashArray. SVG dashes are measured
 *  along the path but the cap is square to it, so the wider casing overhangs
 *  each dash by over/2 at BOTH ends — on a fine "1.5 7" dot a CASING_OVER of 3
 *  turns a 1.5px dash into 4.5px and closes the gap to 4, and the dots visibly
 *  touch. Grow each dash by the overhang and take it back out of the gap,
 *  keeping the PERIOD identical so casing and stroke stay in phase. */
export function casingDash(dash: string | undefined, over: number): string | undefined {
  if (!dash) return undefined
  const [on, off] = dash.split(" ").map(Number)
  return `${on + over} ${Math.max(off - over, 1)}`
}

// ---- Casing opacity ---------------------------------------------------------
//
// The casing is a dark stroke painted UNDER a line so it survives whatever tile
// it happens to cross. It is the documented mechanism for surviving a variable
// backdrop, and it matters far more than the line's own colour, because it is
// the half that adapts: a black outline gains contrast exactly where a mid-tone
// fill loses it.
//
// IT WAS TOO WEAK, and the numbers say so. Measured against representative
// satellite tones inside ONE viewport, taking the BEST of (fill vs backdrop) and
// (casing vs backdrop) — i.e. the best chance the line had of being seen at all:
//
//                     cable fill   casing@0.32   BEST      casing@0.55   BEST
//   bare earth           1.13         1.90       1.90         3.15       3.15
//   grass/field          1.24         1.76       1.76         2.67       2.67
//   bright rooftop       2.20         2.11       2.20         4.10       4.10
//   roadmap light        3.12         2.20       3.12         4.55       4.55
//   roadmap dark         4.75         1.07       4.75         1.11       4.75
//
// Over mid-tone ground NEITHER half cleared 2:1 — which is the operator's report
// (2026-08-05, "things are little hard to differentiate immediately from
// background") in one number. 0.55 lifts the worst case from 1.76 to 2.67 and
// the common bright case from 2.20 to 4.10.
//
// WHY THE CASING AND NOT THE LINE COLOUR. Brightening `--map-link` is the
// obvious move and it is a trap: #5e8a9e → #7fb3cc takes the dark roadmap from
// 4.75 to 7.81 and simultaneously takes bright satellite from 2.20 DOWN to 1.34,
// because a lighter line converges on a lighter backdrop. The casing has no such
// trade — it only ever helps, and it costs nothing on the dark roadmap (1.07 →
// 1.11) where the fill already carries the line at 4.75.
//
// It also stays well clear of reading as ink in its own right: this is an
// outline a couple of px wide under an existing stroke, not a fill.
export const CASING_OPACITY = 0.55
/** A hovered line is the one span meant to be findable across a whole viewport,
 *  so its casing goes further — a solid stroke over bright fields needs more
 *  backing than a resting dot does. Kept as a ratio to the resting value rather
 *  than a second free number, so raising one raises both. */
export const CASING_OPACITY_HOVER = 0.68

/** What a Polyline needs: a weight and, when it is dashed, a matching period. */
export interface Stroke {
  weight: number
  dashArray?: string
}

/** The visible stroke at scale `k`.
 *
 *  Weight and dash are scaled TOGETHER in one call precisely so a caller cannot
 *  do one and forget the other — see `scaleDash` for why that particular
 *  omission turns a dotted line into a claim about plant. */
export const strokeAt = (k: number, weight: number, dash?: string): Stroke =>
  ({ weight: round(weight * k), dashArray: scaleDash(dash, k) })

/** The dark casing under that stroke: `over` px wider before scaling, growing
 *  with it so the outline stays proportional rather than swallowing the line at
 *  low zoom or vanishing under it at high zoom. Takes the overhang and the dash
 *  together, so a dashed casing can't be built without the phase correction. */
export const casingAt = (
  k: number, weight: number, over: number, dash?: string,
): Stroke => ({
  weight: round((weight + over) * k),
  dashArray: scaleDash(casingDash(dash, over), k),
})
