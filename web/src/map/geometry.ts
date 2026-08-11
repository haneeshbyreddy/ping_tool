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

export function nearestOnPath(
  px: Array<[number, number]>, x: number, y: number,
): { dist: number; seg: number; t: number } {
  let best = Infinity, bestSeg = 1, bestT = 0
  for (let i = 1; i < px.length; i++) {
    const [ax, ay] = px[i - 1], [bx, by] = px[i]
    const dx = bx - ax, dy = by - ay
    const len2 = dx * dx + dy * dy
    const t = len2 > 0 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / len2)) : 0
    const d = Math.hypot(x - (ax + dx * t), y - (ay + dy * t))
    if (d < best) { best = d; bestSeg = i; bestT = t }
  }
  return { dist: best, seg: bestSeg, t: bestT }
}

export function pointAt(
  pts: Array<[number, number]>, seg: number, t: number,
): [number, number] {
  const a = pts[seg - 1], b = pts[seg]
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
}

export function alongKm(pts: Array<[number, number]>, seg: number, t: number): number {
  let km = 0
  for (let i = 1; i < seg; i++)
    km += distanceKm(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
  const p = pointAt(pts, seg, t)
  return km + distanceKm(pts[seg - 1][0], pts[seg - 1][1], p[0], p[1])
}
