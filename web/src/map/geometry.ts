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
