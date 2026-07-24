// Great-circle + polyline math shared by the map page, cut overlay and the
// route editor. All distances are honest geometry — never display positions.

export function distanceKm(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const R = 6371, toR = Math.PI / 180
  const dLat = (bLat - aLat) * toR, dLng = (bLng - aLng) * toR
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(aLat * toR) * Math.cos(bLat * toR) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

export const fmtKm = (km: number) => km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(km < 10 ? 1 : 0)} km`

export const polyKm = (pts: Array<[number, number]>): number => {
  let km = 0
  for (let i = 1; i < pts.length; i++)
    km += distanceKm(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
  return km
}

/** Where a point falls on a polyline: how far off the path it is, and which
    segment (`seg` = the index of the segment's FAR vertex) it lands on at what
    fraction `t` along that segment.

    Proximity is judged in projected pixels (`project` the path first) because
    that is the unit the operator is pointing in — a degree of longitude is a
    different number of metres at every latitude, so a fixed metre threshold
    would grab a line from three screens away when zoomed out. The (seg, t) that
    comes back is geometry-independent, which is what lets `alongKm` turn it
    into honest ground distance rather than a Mercator-stretched one. */
export function nearestOnPath(
  px: Array<[number, number]>, x: number, y: number,
): { dist: number; seg: number; t: number } {
  let best = Infinity, bestSeg = 1, bestT = 0
  for (let i = 1; i < px.length; i++) {
    const [ax, ay] = px[i - 1], [bx, by] = px[i]
    const dx = bx - ax, dy = by - ay
    const len2 = dx * dx + dy * dy
    // a zero-length segment can't be divided into — clamp to its start
    const t = len2 > 0 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / len2)) : 0
    const d = Math.hypot(x - (ax + dx * t), y - (ay + dy * t))
    if (d < best) { best = d; bestSeg = i; bestT = t }
  }
  return { dist: best, seg: bestSeg, t: bestT }
}

/** The lat/lng at (seg, t) on a path — linear in coordinates, which is what
    Leaflet draws between two vertices, so the marker lands on the pixel the
    operator pointed at. */
export function pointAt(
  pts: Array<[number, number]>, seg: number, t: number,
): [number, number] {
  const a = pts[seg - 1], b = pts[seg]
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
}

/** Ground kilometres from the path's start to (seg, t) — real-world distance,
    walked segment by segment, NOT the projected length scaled by a fraction
    (Mercator stretches with latitude and a splicing crew quotes metres). */
export function alongKm(pts: Array<[number, number]>, seg: number, t: number): number {
  let km = 0
  for (let i = 1; i < seg; i++)
    km += distanceKm(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
  const p = pointAt(pts, seg, t)
  return km + distanceKm(pts[seg - 1][0], pts[seg - 1][1], p[0], p[1])
}
