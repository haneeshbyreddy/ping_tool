import L from "leaflet"
import { deviceTone, durationSince, isFresh } from "@/lib/format"
import type { OrgDevice } from "@/lib/types"

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

export const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;")

const _iconCache = new Map<string, L.DivIcon>()

export function opticRing(d: OrgDevice): "crit" | "warn" | null {
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
  if (o.selected) cls.push("wisp-pin--selected")
  if (o.dim) cls.push("wisp-pin--dim")
  if (o.impact) cls.push("wisp-pin--impact")
  if (d.maintenance) cls.push("wisp-pin--maint")
  if (optic) cls.push(`wisp-pin--optic-${optic}`)
  if (o.dropTone && o.dropTone !== "quiet") cls.push(`wisp-pin--drops-${o.dropTone}`)
  const weak = (d.onus_crit ?? 0) + (d.onus_warn ?? 0)
  const title = esc(o.title ?? (downFor ? `${d.name} · down for ${downFor}`
    : d.maintenance ? `${d.name} · maintenance`
    : optic ? `${d.name} · ${weak} ONU${weak === 1 ? "" : "s"} weak signal` : d.name))
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
