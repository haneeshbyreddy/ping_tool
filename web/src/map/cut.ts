import type L from "leaflet"
import { isPassiveType, type OrgDevice, type PonFault } from "@/lib/types"
import { distanceKm } from "@/map/geometry"
import { cachedDivIcon, esc, isPlaced, type Placed } from "@/map/pins"

export function pointAlong(path: Array<[number, number]>, distM: number): [number, number] {
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const seg = distanceKm(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]) * 1000
    if (acc + seg >= distM && seg > 0) {
      const t = (distM - acc) / seg
      return [
        path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t,
        path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t,
      ]
    }
    acc += seg
  }
  return path[path.length - 1]
}

export function subPath(path: Array<[number, number]>, d0: number, d1: number): Array<[number, number]> {
  const out: Array<[number, number]> = [pointAlong(path, d0)]
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const seg = distanceKm(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]) * 1000
    if (acc + seg > d0 && acc + seg < d1) out.push(path[i])
    acc += seg
  }
  out.push(pointAlong(path, d1))
  return out
}

export function ponPath(
  olt: Placed, port: string | null, devices: OrgDevice[],
  routeByKey: Map<string, { pts: Array<[number, number]> }>,
): Array<[number, number]> | null {
  const path: Array<[number, number]> = [[olt.lat, olt.lng]]
  const seen = new Set<number>([olt.id])
  let cur: Placed = olt
  let first = true
  for (let hop = 0; hop < 20; hop++) {
    const kids = devices.filter((d): d is Placed =>
      d.parent_device_id === cur.id && isPassiveType(d.device_type) && isPlaced(d)
      && !seen.has(d.id)
      && (first ? d.pon_port === port : (d.pon_port === port || d.pon_port == null)))
    const next = kids.find((d) => routeByKey.has(`${d.id}:${cur.id}`)) ?? kids[0]
    if (!next) break
    const wps = routeByKey.get(`${next.id}:${cur.id}`)?.pts ?? []
    path.push(...wps, [next.lat, next.lng])
    seen.add(next.id)
    cur = next
    first = false
  }
  return path.length > 1 ? path : null
}

export function cutIcon(f: PonFault, oltName: string): L.DivIcon {
  const hi = f.cut_high_m == null ? "" : `${(f.cut_high_m / 1000).toFixed(2)} km`
  const lo = f.cut_low_m ? `${(f.cut_low_m / 1000).toFixed(2)} km – ` : "within "
  const title = esc(`Suspected fiber cut · ${oltName} PON ${f.pon_port ?? "?"}: `
    + `${f.dark} ONUs dark, ${lo}${hi} from the OLT (by ranging)`)
  return cachedDivIcon(`<div class="wisp-cut" title="${title}">✕</div>`)
}
