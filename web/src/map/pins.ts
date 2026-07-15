// Pin tones and divIcons for the map: fill = health, ::after ring = optics,
// silhouette = device role (wisp-pin--t-<type> in index.css).
import L from "leaflet"
import { deviceTone, durationSince, isFresh } from "@/lib/format"
import type { OrgDevice } from "@/lib/types"

export type Placed = OrgDevice & { lat: number; lng: number }

export const isPlaced = (d: OrgDevice): d is Placed => d.lat != null && d.lng != null

export function pinTone(d: OrgDevice): "success" | "warning" | "destructive" | "muted" {
  // planned downtime must never read as an outage on the wall map
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

// The label rides inside the divIcon so pin + name pan as one DOM node (no
// per-frame tooltip sync). iconSize 0 + CSS translate keeps the dot on the latlng.
// Icons are cached by their html: useNow() re-renders every 15s, and a fresh
// divIcon each tick would make react-leaflet swap the DOM node — restarting the
// down-pulse animation and churning every pin's DOM for nothing.
const _iconCache = new Map<string, L.DivIcon>()

// A PON going soft, visible from the wall map: ring the OLT's pin when its ONUs
// are weak. Quiet when the OLT itself is down (the red pulse owns the pin), in
// maintenance, or the optics walk went stale (no zombie alarms off old readings).
export function opticRing(d: OrgDevice): "crit" | "warn" | null {
  if (d.maintenance || isDownState(d) || !isFresh(d.optics_updated_at)) return null
  if ((d.onus_crit ?? 0) > 0) return "crit"
  if ((d.onus_warn ?? 0) > 0) return "warn"
  return null
}

export function pinIcon(d: OrgDevice, o: { selected: boolean; dim: boolean; impact: boolean }): L.DivIcon {
  const tone = pinTone(d)
  // first token only ("43m", not "43m 12s") so the hover title churns per minute
  const downFor = isDownState(d) && d.outage_started_at
    ? durationSince(d.outage_started_at).split(" ")[0] : null
  const optic = opticRing(d)
  const cls = ["wisp-pin", `wisp-pin--${tone}`]
  // role is the third visual channel: fill = health, ring = optics, SHAPE = type
  if (d.device_type) cls.push(`wisp-pin--t-${d.device_type.toLowerCase()}`)
  if (o.selected) cls.push("wisp-pin--selected")
  if (o.dim) cls.push("wisp-pin--dim")
  if (o.impact) cls.push("wisp-pin--impact")
  if (d.maintenance) cls.push("wisp-pin--maint")
  if (optic) cls.push(`wisp-pin--optic-${optic}`)
  const weak = (d.onus_crit ?? 0) + (d.onus_warn ?? 0)
  const title = esc(downFor ? `${d.name} · down for ${downFor}`
    : d.maintenance ? `${d.name} · maintenance`
    : optic ? `${d.name} · ${weak} ONU${weak === 1 ? "" : "s"} weak signal` : d.name)
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">
      <span class="wisp-pin__dot"></span><span class="wisp-pin__label">${esc(d.name)}</span>
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
