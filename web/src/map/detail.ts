export interface MapDetail {
  labels: number
  passives: number
  subscribers: number
  subscriber_names: number
  drop_lines: number
}

export const DETAIL_DEFAULTS: MapDetail = {
  labels: 12,
  passives: 13,
  subscribers: 14,
  subscriber_names: 17,
  drop_lines: 16,
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

export function detailMin(d: MapDetail, k: keyof MapDetail): number {
  if (k === "drop_lines")
    return Math.max(DETAIL_MIN, clamp(d.subscribers), clamp(d.passives))
  if (k === "subscriber_names") return Math.max(DETAIL_MIN, clamp(d.subscribers))
  return DETAIL_MIN
}

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

export const isDetailDefault = (d: MapDetail): boolean =>
  (Object.keys(DETAIL_DEFAULTS) as Array<keyof MapDetail>)
    .every((k) => d[k] === DETAIL_DEFAULTS[k])
