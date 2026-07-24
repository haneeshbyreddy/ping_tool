// Bandwidth labels riding the link lines: the port the operator bound to a link
// (device panel → Uplinks / Cross-links) supplies live in/out rates off the SNMP
// port walk.
//
// Bindings are keyed by the UNORDERED device pair, and each bound port is filed
// under the device that OWNS it. That one choice makes every link kind share a
// single lookup: a parent-side port (feeds_device_id), a child-side uplink and
// both ends of an undirected cross-link (uplink_device_id on each) all land on
// the same key, and callers ask for rates by naming which end they're looking
// from. No kind-specific bookkeeping, so a peer link can't fall through a gap.
//
// Icons go through cachedDivIcon (pins.ts discipline): useNow() re-renders every
// tick and an uncached icon would swap every label's DOM node per render.
import type L from "leaflet"
import { isFresh } from "@/lib/format"
import type { LinkPort } from "@/lib/types"
import { pointAlong } from "@/map/cut"
import { polyKm } from "@/map/geometry"
import { isLinkColor, linkColorVar } from "@/map/linkcolor"
import { cachedDivIcon, esc } from "@/map/pins"

/** the ports bound to one link, by the device each port belongs to */
export type LinkBinding = Map<number, LinkPort>

/** order-independent key: one cable is one entry, whichever end declared it */
export const linkKey = (x: number, y: number) =>
  x <= y ? `${x}:${y}` : `${y}:${x}`

/** fold the org-wide /link-ports rows into per-link bindings; on a LAG (several
    ports bound to one link) the lowest if_index carries the label */
export function bindLinkPorts(rows: LinkPort[]): Map<string, LinkBinding> {
  const m = new Map<string, LinkBinding>()
  const file = (own: number, other: number, p: LinkPort) => {
    const k = linkKey(own, other)
    let b = m.get(k)
    if (!b) { b = new Map(); m.set(k, b) }
    if (!b.has(own)) b.set(own, p)
  }
  for (const p of rows) {
    if (p.feeds_device_id != null) file(p.device_id, p.feeds_device_id, p)
    if (p.uplink_device_id != null) file(p.device_id, p.uplink_device_id, p)
  }
  return m
}

export const portLabel = (p: LinkPort) => p.if_name || `if${p.if_index}`

const fmtShort = (bps: number): string => {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)}G`
  if (bps >= 1e6) return `${bps >= 1e7 ? Math.round(bps / 1e6) : (bps / 1e6).toFixed(1)}M`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)}k`
  return `${Math.round(bps)}`
}

const fmtFull = (bps: number | null): string => {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`
  return `${Math.round(bps)} b/s`
}

/** Live rates as seen looking FROM `fromId` TOWARD `toId`: `down` is traffic
    heading to `toId`. The reference end's own counters are preferred (its egress
    IS the link's forward direction); nulls when the walk went stale — a label
    must never show a weeks-old number as if it were now. */
export function linkRates(b: LinkBinding | undefined, fromId: number, toId: number):
  { down: number | null; up: number | null } {
  const from = b?.get(fromId)
  const to = b?.get(toId)
  const src = from ?? to
  if (!src || !isFresh(src.updated_at)) return { down: null, up: null }
  return from
    ? { down: from.out_bps, up: from.in_bps }
    : { down: to!.in_bps, up: to!.out_bps }
}

export function linkTone(b: LinkBinding): "down" | "warn" | null {
  const ports = [...b.values()]
  if (ports.some((p) => p.oper_status === "down" || (p.monitored === 1 && p.alarm === 1)))
    return "down"
  if (ports.some((p) => p.bw_alarm === 1 || p.bw_high_alarm === 1)) return "warn"
  return null
}

/** Where the chip sits on the RENDERED geometry (drawn route or chord), so it
    stays on the line even when the cable path snakes. `frac` is the operator's
    saved 0..1 position along that path; midpoint when they never moved it.

    Deliberately a FRACTION and not a coordinate: the line rubber-bands when
    either pin moves, and a saved lat/lng would drift off it the first time
    anyone corrected a location. */
export const linkLabelPos = (
  pts: Array<[number, number]>, frac?: number | null,
): [number, number] =>
  pointAlong(pts, polyKm(pts) * 1000 * (frac == null ? 0.5 : frac))

export function linkBwIcon(
  b: LinkBinding, from: { id: number; name: string }, to: { id: number; name: string },
  color?: string | null,
): L.DivIcon {
  const { down, up } = linkRates(b, from.id, to.id)
  const hasRates = down != null || up != null
  const ends = [[from, b.get(from.id)] as const, [to, b.get(to.id)] as const]
    .filter(([, p]) => p)
    .map(([d, p]) => `${d.name} ${portLabel(p!)}`)
    .join(" ↔ ")
  const title = esc(hasRates
    ? `${ends} · ↓ ${fmtFull(down)} toward ${to.name} · ↑ ${fmtFull(up)}`
    : `${ends} · no recent rate reading`)
  // arrows in their own span so CSS can quiet them: the rate is the data, the
  // ↓↑ is only which way it flows. fmtShort output is number-derived, so the
  // port name is the one branch that needs escaping.
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const body = hasRates
    ? `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`
    : `<span class="wisp-linkbw__port">${esc(portLabel([...b.values()][0]))}</span>`
  const tone = linkTone(b)
  const cls = ["wisp-linkbw"]
  if (tone) cls.push(`wisp-linkbw--${tone}`)
  // The chip borrows its line's colour so a stack of labels over parallel
  // cables is readable — but only when the line HAS a custom colour and nothing
  // is wrong with it: a port alarm's tone owns the chip outright, same rule as
  // the stroke. Rendered as an inline custom property (the tint reaches a border
  // and a rail through one value) — safe because `color` is validated against
  // the closed palette on both sides before it ever gets here.
  const tint = !tone && isLinkColor(color)
    ? ` style="--wisp-link-tint:${linkColorVar(color)}"` : ""
  if (tint) cls.push("wisp-linkbw--tinted")
  return cachedDivIcon(
    `<div class="${cls.join(" ")}"${tint} title="${title}">${body}</div>`)
}
