export interface MapDetail {
  labels: number
  passives: number
  passive_names: number
  subscribers: number
  subscriber_names: number
  drop_lines: number
  line_labels: number
}

export const DETAIL_DEFAULTS: MapDetail = {
  labels: 12,
  passives: 13,
  passive_names: 13,
  subscribers: 14,
  subscriber_names: 17,
  drop_lines: 16,
  line_labels: 4,
}

export const DETAIL_MIN = 4
export const DETAIL_MAX = 19

export const DETAIL_ROWS: ReadonlyArray<{
  key: keyof MapDetail
  label: string
  hint: string
}> = [
  {
    key: "labels",
    label: "Device names",
    hint: "Names on gear pins — OLTs, switches, routers. The dots and their "
      + "status colour always draw, and anything down or selected keeps its name "
      + "at every zoom. Splitters and closures have their own row below.",
  },
  {
    key: "passives",
    label: "Splitters",
    hint: "Splitter, FDB and closure pins, and the cable drawn into them. A "
      + "splitter whose recorded customers are dark stays on the map at every "
      + "zoom, along with the plant feeding it.",
  },
  {
    key: "passive_names",
    label: "Splitter labels",
    hint: "The plate beside a splitter, closure or FDB — its split ratio, or its "
      + "name where no ratio is recorded. Never lower than Splitters. A box whose "
      + "recorded customers are dark or weak keeps its plate at every zoom, as "
      + "does the selected one.",
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
  {
    key: "line_labels",
    label: "Cable & rate labels",
    hint: "The chip on a cable naming it and its fibre count, and the ↓/↑ rate "
      + "chip on a link. 4 draws them at every zoom. A link whose port is down "
      + "or over its bandwidth keeps its chip whatever this says, as does a "
      + "selected path and a traced core.",
  },
]

const clamp = (n: number) =>
  Math.min(DETAIL_MAX, Math.max(DETAIL_MIN, Math.round(n)))

// `line_labels` deliberately takes NO floor from another row, unlike the two
// subscriber dependents. The lines it labels do not share one floor: a chord
// between two boxes draws at every zoom, and so does a sheath standing in for one,
// while a plain cable is bound by Splitters. So a value below Splitters is not the
// no-op the ordering invariant refuses — it still governs every chip on a line
// that draws down there. A cable chip can never outlive its own cable regardless,
// because the budget only ever offers pixels to a line that was drawn.
export function detailMin(d: MapDetail, k: keyof MapDetail): number {
  if (k === "drop_lines")
    return Math.max(DETAIL_MIN, clamp(d.subscribers), clamp(d.passives))
  if (k === "subscriber_names") return Math.max(DETAIL_MIN, clamp(d.subscribers))
  if (k === "passive_names") return Math.max(DETAIL_MIN, clamp(d.passives))
  return DETAIL_MIN
}

export function normalizeDetail(d: MapDetail): MapDetail {
  const base = { ...d, subscribers: clamp(d.subscribers), passives: clamp(d.passives) }
  const floor = (k: "drop_lines" | "subscriber_names" | "passive_names") =>
    Math.max(detailMin(base, k), clamp(d[k]))
  return {
    labels: clamp(d.labels),
    passives: base.passives,
    passive_names: floor("passive_names"),
    subscribers: base.subscribers,
    subscriber_names: floor("subscriber_names"),
    drop_lines: floor("drop_lines"),
    line_labels: clamp(d.line_labels),
  }
}

export function detailFrom(raw: unknown): MapDetail {
  const v = (raw ?? {}) as Partial<Record<keyof MapDetail, unknown>>
  const pick = (k: keyof MapDetail) =>
    typeof v[k] === "number" && Number.isFinite(v[k])
      ? (v[k] as number) : DETAIL_DEFAULTS[k]
  return normalizeDetail({
    labels: pick("labels"),
    passives: pick("passives"),
    passive_names: pick("passive_names"),
    subscribers: pick("subscribers"),
    subscriber_names: pick("subscriber_names"),
    drop_lines: pick("drop_lines"),
    line_labels: pick("line_labels"),
  })
}

export const isDetailDefault = (d: MapDetail): boolean =>
  (Object.keys(DETAIL_DEFAULTS) as Array<keyof MapDetail>)
    .every((k) => d[k] === DETAIL_DEFAULTS[k])
