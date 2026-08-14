import L from "leaflet"
import { deviceTone, durationSince, isFresh } from "@/lib/format"
import { isPassiveType, type OrgDevice } from "@/lib/types"

export type Placed = OrgDevice & { lat: number; lng: number }

export const isPlaced = (d: OrgDevice): d is Placed => d.lat != null && d.lng != null

export function pinTone(d: OrgDevice): "success" | "warning" | "destructive" | "muted" {
  if (d.maintenance) return "muted"
  if (!d.assigned_node_id || !d.state) return "muted"
  return deviceTone(d.state, d.state_updated_at)
}

export const isTrouble = (d: OrgDevice): boolean => {
  const t = pinTone(d)
  return t === "destructive" || t === "warning"
}

export const isDownState = (d: OrgDevice): boolean =>
  d.state === "DOWN" || d.state === "UNREACHABLE"

// Leaflet stacks a marker at (screen y + zIndexOffset), so with equal offsets the
// SOUTHERNMOST mark wins — which is why a splitter used to bury the OLT beside it.
// Gear therefore takes a band of its own, and a lone pin shares the badge's floor.
export const MARK_Z_PLANT = 0
export const MARK_Z_GEAR = 200
export const MARK_Z_IMPACT = 300
export const MARK_Z_DOWN = 500
export const MARK_Z_SELECTED = 1000

// Plant takes ONE rung whatever its drops are doing — a splitter in trouble says so
// in TONE and nothing else. Lifting a dark one over the OLT beside it is the very
// burial this ladder exists to stop.
export function markZIndex(d: OrgDevice, o: {
  selected: boolean; impact: boolean; plant: boolean
}): number {
  if (o.selected) return MARK_Z_SELECTED
  if (o.plant) return MARK_Z_PLANT
  if (pinTone(d) === "destructive") return MARK_Z_DOWN
  if (o.impact) return MARK_Z_IMPACT
  return MARK_Z_GEAR
}

export const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;")

const _iconCache = new Map<string, L.DivIcon>()

export function opticRing(d: OrgDevice): "crit" | "warn" | null {
  if (d.device_type?.toLowerCase() === "nvr") {
    if (d.maintenance || isDownState(d) || !isFresh(d.cameras_updated_at)) return null
    return (d.cameras_down ?? 0) > 0 ? "crit" : null
  }
  if (d.maintenance || isDownState(d) || !isFresh(d.optics_updated_at)) return null
  if ((d.onus_crit ?? 0) > 0) return "crit"
  if ((d.onus_warn ?? 0) > 0) return "warn"
  return null
}

export function pinIcon(d: OrgDevice, o: {
  selected: boolean; dim: boolean; impact: boolean
  label?: string
  dropTone?: "dark" | "weak" | "ok" | "quiet"
  title?: string
}): L.DivIcon {
  const tone = pinTone(d)
  const downFor = isDownState(d) && d.outage_started_at
    ? durationSince(d.outage_started_at).split(" ")[0] : null
  const optic = opticRing(d)
  const cls = ["wisp-pin", `wisp-pin--${tone}`]
  if (d.device_type) cls.push(`wisp-pin--t-${d.device_type.toLowerCase()}`)
  // ONE class off `isPassiveType`, never a list of the four type classes in CSS —
  // that second list would drift from PASSIVE_TYPES, and `coupler` is in that tuple
  // forever precisely so a straggler row stays silent plant. It is a pure function
  // of the device, so the icon's cached html string stays stable per pin.
  if (isPassiveType(d.device_type)) cls.push("wisp-pin--plant")
  if (o.selected) cls.push("wisp-pin--selected")
  if (o.dim) cls.push("wisp-pin--dim")
  if (o.impact) cls.push("wisp-pin--impact")
  if (d.maintenance) cls.push("wisp-pin--maint")
  if (optic) cls.push(`wisp-pin--optic-${optic}`)
  if (o.dropTone && o.dropTone !== "quiet") cls.push(`wisp-pin--drops-${o.dropTone}`)
  const weak = d.device_type?.toLowerCase() === "nvr"
    ? (d.cameras_down ?? 0) : (d.onus_crit ?? 0) + (d.onus_warn ?? 0)
  const weakWord = d.device_type?.toLowerCase() === "nvr"
    ? `camera${weak === 1 ? "" : "s"} dark`
    : `ONU${weak === 1 ? "" : "s"} weak signal`
  const title = esc(o.title ?? (downFor ? `${d.name} · down for ${downFor}`
    : d.maintenance ? `${d.name} · maintenance`
    : optic ? `${d.name} · ${weak} ${weakWord}` : d.name))
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">
      <span class="wisp-pin__dot"></span><span class="wisp-pin__label">${esc(o.label ?? d.name)}</span>
    </div>`)
}

export function cachedDivIcon(html: string): L.DivIcon {
  let icon = _iconCache.get(html)
  if (!icon) {
    if (_iconCache.size > 600) _iconCache.clear()
    icon = L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
    _iconCache.set(html, icon)
  }
  return icon
}

export function meIcon(): L.DivIcon {
  return cachedDivIcon(`<div class="wisp-me" title="You are here"></div>`)
}

export function vertexIcon(): L.DivIcon {
  return cachedDivIcon(`<div class="wisp-vertex" title="Drag to adjust. Double-click to remove"></div>`)
}
