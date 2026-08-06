// ---- Map detail: the zoom at which each layer starts drawing ----------------
//
// These used to be three constants in map-page.tsx, moved and tuned by hand
// whenever an operator said the map was too busy or too empty. That is the same
// shape of ask as "make the palette warmer", and the answer is the same one the
// theme panel already gives: it is a dashboard control, not a code edit.
//
// SERVER-WIDE, SUPERADMIN-SET, ONE CONFIGURATION FOR EVERYONE — Settings →
// Platform, stored in `app_settings.map_detail`, riding the `/api/orgs` reply
// beside `google_maps_key`. It shipped as a per-browser localStorage preference
// in the Layers popover first and was pulled back the same day (operator's call,
// 2026-08-02). Worth keeping the reasoning: map density is a judgement about how
// this product should read, and the person making it is the one who looks at the
// fleet all day. Handing it to every account buys a support surface ("my map
// looks different from yours") in exchange for a choice nobody else asked to
// make. So this module no longer touches localStorage at all — if a per-user
// override is ever wanted again, it goes back on top of these values, never
// instead of them.
//
// NOTHING HERE IS A CLAIM ABOUT THE NETWORK. Every value only decides when a
// layer is DRAWN; no alarm, no count, no verdict and no page reads any of it,
// which is what makes it safe to put in a form. Same discipline as the
// notification governor writing state rows regardless of its allowlist: a
// display knob may never be able to hide a fact.

/** The zoom floor for each layer that has one.
 *
 *  snake_case because these values arrive from the API verbatim (`/api/orgs` →
 *  `map_detail`) and go back the same way. A camelCase mirror would mean a
 *  translation layer on both ends, which is one more place for a field to get
 *  dropped silently. */
export interface MapDetail {
  /** Device NAME labels on pins. The dots, their status tone and the down-pulse
   *  ALWAYS draw — this hides the writing only, and trouble and the selection
   *  keep their labels at any zoom (`.wisp-map-lowzoom` in index.css). So even
   *  the highest setting can never hide a device that is down. */
  labels: number
  /** Passive plant — splitter/FDB/closure pins AND the cable drawn into them.
   *  Both, or the map keeps a line running to a point where nothing is drawn.
   *  A splitter whose recorded subscribers are dark is exempt (with the plant
   *  above it), so this can hide reference material but never an alarm. */
  passives: number
  /** Located-subscriber marks (the survey's output, plus reference ONUs). */
  subscribers: number
  /** The customer NAME beside a located subscriber's mark. Its own floor rather
   *  than the marks': on a surveyed fleet these outnumber device pins a hundred
   *  to one, so the zoom at which a diamond is useful texture and the zoom at
   *  which its name is readable are not the same number. */
  subscriber_names: number
  /** The dotted line from a subscriber to the splitter feeding it, and the rate
   *  chip that rides that line. */
  drop_lines: number
}

/** Shipped defaults — the values these were hardcoded to, and the reasoning
 *  that produced them. Reset returns here. MIRRORED in
 *  `central/mapdetail.py:DEFAULTS`, deliberately: the SPA needs a value to draw
 *  with before the orgs query resolves, and central needs one to validate
 *  against without asking a browser.
 *
 *  · labels 12 — past tower-map altitude the names are soup and the dots carry
 *    the state anyway.
 *  · passives 13 — one level below the subscriber marks, so plant outlives the
 *    drops hanging off it by a zoom step, which is the ranking every other
 *    channel on this map already gives the two. Plant left the clustering pass
 *    on 2026-08-05 ("a site badge mixing plant with gear counts nonsense"), and
 *    the accepted cost was that dense plant OVERLAPS at low zoom instead of
 *    folding — exactly what subscribers do, and this is the same answer they
 *    got. Below 13 a fleet's splitters are a smear over the gear they are
 *    subordinate to; at 13 you are looking at one town.
 *  · subscribers 14 — a live located drop is the quietest fill on the map
 *    (`--success` 32%), so a town's worth reads as texture while a dark one
 *    still shouts. It was 16 until an operator pointed out that pulling back far
 *    enough to SEE an area was exactly when they vanished.
 *  · subscriber_names 17 — street zoom, one above the drop lines. A name is the
 *    widest thing this layer draws and there is one per customer, so it is the
 *    first thing to turn a surveyed town into a wall of text. At 17 a name is
 *    something you read off a street you are already looking at; the mark, the
 *    tone and the card carry the rest at every zoom below it.
 *  · drop_lines 16 — below street zoom the whole span is a handful of pixels: it
 *    can't be traced, and a few dozen with their black casings smear into a
 *    smudge around every splitter, burying the plant the layer is subordinate
 *    to. This is what forced the single old floor to 16; the marks never needed
 *    it. */
export const DETAIL_DEFAULTS: MapDetail = {
  labels: 12,
  passives: 13,
  subscribers: 14,
  subscriber_names: 17,
  drop_lines: 16,
}

/** Google's tiles stop at 20 and the region lock sets its own floor, so the
 *  offered span is deliberately narrower than what Leaflet would accept: past
 *  either end the control would be pretending to do something. A floor at or
 *  below the map's own minimum simply reads as "always on", which is a legible
 *  outcome rather than a broken one. */
export const DETAIL_MIN = 4
export const DETAIL_MAX = 19

/** The rows the Platform settings card offers, in the order it offers them.
 *
 *  Kept HERE rather than in the JSX so the label, the help text and the
 *  invariant that governs the value all live beside each other — three copies of
 *  "what does this row mean" is how a control ends up describing behaviour it no
 *  longer has. */
export const DETAIL_ROWS: ReadonlyArray<{
  key: keyof MapDetail
  label: string
  hint: string
}> = [
  {
    key: "labels",
    label: "Device names",
    hint: "Names on device pins. The dots and their status colour always draw, "
      + "and anything down or selected keeps its name at every zoom.",
  },
  {
    key: "passives",
    label: "Splitters",
    hint: "Splitter, FDB and closure pins, and the cable drawn into them. A "
      + "splitter whose recorded customers are dark stays on the map at every "
      + "zoom, along with the plant feeding it.",
  },
  {
    key: "subscribers",
    label: "Subscribers",
    hint: "Surveyed subscriber pins. Needs the Subscribers layer on in the map's "
      + "Layers menu. Lower this to see drops while zoomed out.",
  },
  {
    key: "subscriber_names",
    label: "Subscriber names",
    // "A dark subscriber keeps its name at every zoom" was true for one day and
    // then wasn't: round 6b narrowed the exemption to a dark REFERENCE ONU
    // (`isRefEvidence`), because thousands of customers go offline every evening
    // and naming each one is the wall of text this row exists to control. The
    // comment above this table warns about exactly this drift, so the fix is
    // here rather than a second sentence somewhere else.
    hint: "The customer name beside each subscriber pin. Raise it if a surveyed "
      + "area reads as a wall of text. Only a dark reference ONU keeps its name "
      + "below this zoom.",
  },
  {
    key: "drop_lines",
    label: "Drop lines",
    hint: "The dotted line from a subscriber to its splitter, plus its rate "
      + "chip. Never lower than Subscribers or Splitters — a line has two ends, "
      + "and both need a pin to point at.",
  },
]

const clamp = (n: number) =>
  Math.min(DETAIL_MAX, Math.max(DETAIL_MIN, Math.round(n)))

/** The lowest value one row may take given the others — the ordering invariant
 *  in this module: nothing is drawn at a zoom where the MARK it belongs to
 *  isn't. `subscriber_names` sits at or above `subscribers`; `drop_lines` sits
 *  at or above BOTH `subscribers` and `passives`.
 *
 *  It is not cosmetic. Each of those rides a mark — `refLinesVisible` and
 *  `refNamesVisible` are both `refVisible && …`, and a drop line is suppressed
 *  with the splitter it runs to — so a floor set BELOW its mark's doesn't draw
 *  it earlier, it does nothing at all, and a control that silently no-ops is
 *  worse than one that refuses. A drop line is the one row with TWO marks under
 *  it: the subscriber's diamond at one end and the splitter at the other, and a
 *  dotted line running to a point where nothing is drawn reads as a rendering
 *  fault rather than as a setting.
 *
 *  Exported because the STEPPER needs it too: a `−` that stays enabled and then
 *  gets silently undone by `normalizeDetail` is the same no-op wearing a
 *  different hat. `normalizeDetail` is written in terms of this function rather
 *  than repeating the comparison, so the button's disabled state and the value
 *  actually stored can never disagree about where the floor is. Central repairs
 *  the same invariant server-side (`mapdetail.clean`), so a hand-edited DB row
 *  can't reach the map in the broken state either. */
export function detailMin(d: MapDetail, k: keyof MapDetail): number {
  if (k === "drop_lines")
    return Math.max(DETAIL_MIN, clamp(d.subscribers), clamp(d.passives))
  if (k === "subscriber_names") return Math.max(DETAIL_MIN, clamp(d.subscribers))
  return DETAIL_MIN
}

/** Clamp every row to the offered span, then to the invariant above.
 *
 *  ONE repair covers every way of breaking it, which is why this needs no notion
 *  of which knob was touched: raising Subscribers or Splitters past a dependent
 *  row pushes that row up with it, and lowering a dependent row below its floor
 *  stops there. Both land on the floor itself, so every press still moves
 *  something visible and no pair can rest in the state that would no-op. */
export function normalizeDetail(d: MapDetail): MapDetail {
  const base = { ...d, subscribers: clamp(d.subscribers), passives: clamp(d.passives) }
  const floor = (k: "drop_lines" | "subscriber_names") =>
    Math.max(detailMin(base, k), clamp(d[k]))
  return {
    labels: clamp(d.labels),
    passives: base.passives,
    subscribers: base.subscribers,
    subscriber_names: floor("subscriber_names"),
    drop_lines: floor("drop_lines"),
  }
}

/** Coerce whatever `/api/orgs` handed back into something drawable.
 *
 *  Per FIELD, not per object: a row written before a field existed must not
 *  discard the fields it does carry, and a missing or corrupt value must degrade
 *  to the shipped number rather than to NaN — a NaN threshold makes every
 *  `zoom >= n` false and reads as "the layer is broken". Central validates on
 *  both the write and the read, so this is the third net rather than the first;
 *  it exists because the map must render before, and regardless of, that reply. */
export function detailFrom(raw: unknown): MapDetail {
  const v = (raw ?? {}) as Partial<Record<keyof MapDetail, unknown>>
  const pick = (k: keyof MapDetail) =>
    typeof v[k] === "number" && Number.isFinite(v[k])
      ? (v[k] as number) : DETAIL_DEFAULTS[k]
  return normalizeDetail({
    labels: pick("labels"),
    passives: pick("passives"),
    subscribers: pick("subscribers"),
    subscriber_names: pick("subscriber_names"),
    drop_lines: pick("drop_lines"),
  })
}

/** Whether these are the shipped values. Drives the Reset affordance (an
 *  always-visible reset on an untouched control is noise) — and central uses the
 *  same test to decide not to store a row at all, so an install nobody has
 *  touched keeps following the defaults and a future change to them still
 *  reaches everyone who never expressed an opinion. Same rule as the theme
 *  overrides being a sparse diff rather than a snapshot. */
export const isDetailDefault = (d: MapDetail): boolean =>
  (Object.keys(DETAIL_DEFAULTS) as Array<keyof MapDetail>)
    .every((k) => d[k] === DETAIL_DEFAULTS[k])
