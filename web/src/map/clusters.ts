// ---- Site clustering --------------------------------------------------------
// ISP gear piles up: one cabinet/rooftop holds an OLT, a switch, a backhaul
// radio. Pins that would overlap on screen fold into one "site" badge — the
// count is the member total, a conic ring shows the status composition.
// Clicking it zooms in when the members are genuinely spread out (the cluster
// splits on its own), or opens the SITE CARD when they truly share a spot (a
// rack) — members resolve in UI space, never on tiles. The old spider-fan
// scattered pins onto real coordinates and read as geography (the same lie the
// removed ONU spokes told); the map shows only true locations. Screen-space
// and zoom-dependent by design: no schema, no assignment workflow — placing
// devices at the same spot IS the rack assignment.
import type L from "leaflet"
import type { OrgDevice } from "@/lib/types"
import { cachedDivIcon, esc, pinTone, type Placed } from "@/map/pins"

const CLUSTER_PX = 44

export interface SiteCluster {
  // sorted member ids — membership shifts with zoom; the site card anchors on
  // a member DEVICE id, not this key, so it survives the reshuffle
  key: string
  members: Placed[]
  center: [number, number]
}

// Web Mercator pixel position at `zoom` — mirrors what Leaflet renders
export function project(lat: number, lng: number, zoom: number): [number, number] {
  const scale = 256 * 2 ** zoom
  const s = Math.sin((lat * Math.PI) / 180)
  return [
    ((lng + 180) / 360) * scale,
    (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale,
  ]
}

export function buildClusters(placed: Placed[], zoom: number): SiteCluster[] {
  const acc: Array<{ px: [number, number]; members: Placed[] }> = []
  for (const d of placed) {
    const p = project(d.lat, d.lng, zoom)
    const hit = acc.find((c) => Math.hypot(c.px[0] - p[0], c.px[1] - p[1]) < CLUSTER_PX)
    if (hit) hit.members.push(d)
    else acc.push({ px: p, members: [d] })
  }
  return acc.map((c) => ({
    key: c.members.map((m) => m.id).sort((a, b) => a - b).join(","),
    members: c.members,
    center: [
      c.members.reduce((s, m) => s + m.lat, 0) / c.members.length,
      c.members.reduce((s, m) => s + m.lng, 0) / c.members.length,
    ] as [number, number],
  }))
}

const CLUSTER_TONE_ORDER = ["destructive", "warning", "success", "muted"] as const
const CLUSTER_TONE_COLOR: Record<(typeof CLUSTER_TONE_ORDER)[number], string> = {
  destructive: "var(--destructive)", warning: "var(--warning)",
  success: "var(--success)", muted: "var(--muted-foreground)",
}

export const toneRank = (d: OrgDevice) => CLUSTER_TONE_ORDER.indexOf(pinTone(d))

// Count badge with a conic composition ring: arc lengths are member statuses,
// so "5 devices, one down" reads without a click. Worst tones paint first
// (from 12 o'clock), all-healthy degenerates to a plain success ring.
export function clusterIcon(members: Placed[], o: { dim: boolean; selected: boolean }): L.DivIcon {
  const counts = new Map<string, number>()
  for (const m of members) {
    const t = pinTone(m)
    counts.set(t, (counts.get(t) ?? 0) + 1)
  }
  let acc = 0
  const stops: string[] = []
  for (const t of CLUSTER_TONE_ORDER) {
    const n = counts.get(t) ?? 0
    if (n === 0) continue
    const from = (acc / members.length) * 360
    acc += n
    stops.push(`${CLUSTER_TONE_COLOR[t]} ${from}deg ${(acc / members.length) * 360}deg`)
  }
  const down = counts.get("destructive") ?? 0
  const names = members.slice(0, 6).map((m) => m.name).join(", ")
  const title = esc(`${members.length} devices${down ? `, ${down} down` : ""}: ${names}${members.length > 6 ? ", …" : ""}`)
  const cls = ["wisp-cluster"]
  if (down > 0) cls.push("wisp-cluster--down")
  if (o.selected) cls.push("wisp-cluster--selected")
  if (o.dim) cls.push("wisp-cluster--dim")
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}" style="background:conic-gradient(${stops.join(",")})">
      <span class="wisp-cluster__n">${members.length}</span>
    </div>`)
}
