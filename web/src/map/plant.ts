import { distanceKm } from "@/map/geometry"
import { isPassiveType, type OrgDevice } from "@/lib/types"

export const PLANT_KINDS = ["splitter"] as const
export type PlantKind = (typeof PLANT_KINDS)[number]

export const PLANT_LABEL: Record<PlantKind, string> = {
  splitter: "splitter",
}

const NAME_STEM: Record<PlantKind, string> = {
  splitter: "SPL",
}

export const FEEDER_RADIUS_KM = 2

const feedOf = (d: OrgDevice): number | null =>
  d.feed_device_id ?? d.parent_device_id ?? null

export function feedChain(device: OrgDevice, byId: Map<number, OrgDevice>) {
  const passives: OrgDevice[] = []
  let head: OrgDevice | null = null
  const first = feedOf(device)
  let cur = first != null ? byId.get(first) : undefined
  const seen = new Set<number>([device.id])
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    if (!isPassiveType(cur.device_type)) { head = cur; break }
    passives.push(cur)
    const up = feedOf(cur)
    cur = up != null ? byId.get(up) : undefined
  }
  return { passives, head }
}

export function cumulativeSplit(device: OrgDevice, byId: Map<number, OrgDevice>): number | null {
  const { passives } = feedChain(device, byId)
  let total = 1
  for (const d of [device, ...passives]) {
    if (!d.split_ratio) return null
    total *= d.split_ratio
  }
  return total
}

export function splitIfAdded(
  parent: OrgDevice | null, ratio: number | null, byId: Map<number, OrgDevice>,
): number | null {
  if (!ratio) return null
  if (!parent) return ratio
  if (!isPassiveType(parent.device_type)) return ratio
  const above = cumulativeSplit(parent, byId)
  return above == null ? null : above * ratio
}

const placed = (d: OrgDevice): d is OrgDevice & { lat: number; lng: number } =>
  d.lat != null && d.lng != null

const isLikelyFeeder = (d: OrgDevice): boolean =>
  isPassiveType(d.device_type) || (d.device_type ?? "").toUpperCase() === "OLT"

export interface Feeder {
  device: OrgDevice
  meters: number
}

export function nearestFeeder(
  lat: number, lng: number, devices: OrgDevice[],
): Feeder | null {
  let best: Feeder | null = null
  for (const d of devices) {
    if (!placed(d) || !isLikelyFeeder(d)) continue
    const km = distanceKm(lat, lng, d.lat, d.lng)
    if (km > FEEDER_RADIUS_KM) continue
    if (!best || km * 1000 < best.meters) best = { device: d, meters: km * 1000 }
  }
  return best
}

export const DROP_RADIUS_KM = 0.3

export function nearestPassive(
  lat: number, lng: number, devices: OrgDevice[],
): Feeder | null {
  let best: Feeder | null = null
  for (const d of devices) {
    if (!placed(d) || !isPassiveType(d.device_type)) continue
    const km = distanceKm(lat, lng, d.lat, d.lng)
    if (km > DROP_RADIUS_KM) continue
    if (!best || km * 1000 < best.meters) best = { device: d, meters: km * 1000 }
  }
  return best
}

export function feederOptions(
  lat: number, lng: number, devices: OrgDevice[], excludeId?: number,
): Array<{ device: OrgDevice; meters: number | null }> {
  return devices
    .filter((d) => d.id !== excludeId)
    .map((d) => ({
      device: d,
      meters: placed(d) ? distanceKm(lat, lng, d.lat, d.lng) * 1000 : null,
    }))
    .sort((a, b) =>
      (a.meters ?? Infinity) - (b.meters ?? Infinity)
      || a.device.name.localeCompare(b.device.name))
}

export function oltHead(
  parent: OrgDevice | null, byId: Map<number, OrgDevice>,
): OrgDevice | null {
  if (!parent) return null
  const isOlt = (d: OrgDevice) => (d.device_type ?? "").toUpperCase() === "OLT"
  if (isOlt(parent)) return parent
  if (!isPassiveType(parent.device_type)) return null
  const { head } = feedChain(parent, byId)
  return head && isOlt(head) ? head : null
}

export function ponFor(
  parent: OrgDevice | null, byId: Map<number, OrgDevice>,
): { pon: string | null; inherited: boolean } {
  if (!parent || !isPassiveType(parent.device_type)) {
    return { pon: null, inherited: false }
  }
  let cur: OrgDevice | undefined = parent
  const seen = new Set<number>()
  while (cur && !seen.has(cur.id) && isPassiveType(cur.device_type)) {
    seen.add(cur.id)
    const pon = (cur.pon_port ?? "").trim()
    if (pon) return { pon, inherited: true }
    const up = feedOf(cur)
    cur = up != null ? byId.get(up) : undefined
  }
  return { pon: null, inherited: false }
}

export function plantInScope(
  scope: { deviceId: number; pons: string[] },
  devices: OrgDevice[],
  byId: Map<number, OrgDevice>,
  shown: ReadonlyArray<{ drop_passive_id: number | null }>,
): Set<number> {
  const keep = new Set<number>()
  for (const p of shown) if (p.drop_passive_id != null) keep.add(p.drop_passive_id)
  const pons = new Set(scope.pons)
  for (const d of devices) {
    if (keep.has(d.id) || !isPassiveType(d.device_type)) continue
    if (feedChain(d, byId).head?.id !== scope.deviceId) continue
    const { pon } = ponFor(d, byId)
    if (pons.size > 0 && pon && !pons.has(pon)) continue
    keep.add(d.id)
  }
  for (const id of [...keep]) {
    const d = byId.get(id)
    if (d) for (const a of feedChain(d, byId).passives) keep.add(a.id)
  }
  return keep
}

export function suggestPlantName(kind: PlantKind, devices: OrgDevice[]): string {
  const stem = NAME_STEM[kind]
  const taken = new Set(devices.map((d) => d.name.trim().toUpperCase()))
  for (let n = 1; n < 10_000; n++) {
    const candidate = `${stem}-${n}`
    if (!taken.has(candidate)) return candidate
  }
  return stem
}

export function ponOptions(
  pons: string[], current: string | null | undefined,
): string[] {
  const cur = (current ?? "").trim()
  if (!cur || pons.includes(cur)) return pons
  return [cur, ...pons]
}

export function ponLabels(ports: Array<string | null | undefined>): string[] {
  const seen = new Set<string>()
  for (const p of ports) {
    const s = (p ?? "").trim()
    if (s) seen.add(s)
  }
  return [...seen].sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }))
}
