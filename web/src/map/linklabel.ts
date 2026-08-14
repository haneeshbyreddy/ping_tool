import type L from "leaflet"
import { isFresh } from "@/lib/format"
import type { LinkPort } from "@/lib/types"
import { pointAlong } from "@/map/cut"
import { polyKm } from "@/map/geometry"
import { cableChipText, strandHex, strandLabel } from "@/lib/fiber"
import { cachedDivIcon, esc } from "@/map/pins"

export type LinkBinding = Map<number, LinkPort>

export const linkKey = (x: number, y: number) =>
  x <= y ? `${x}:${y}` : `${y}:${x}`

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

export const IDLE_BPS = 1000

export const bwIsIdle = (down: number | null, up: number | null) =>
  (down ?? 0) < IDLE_BPS && (up ?? 0) < IDLE_BPS

export const fmtShort = (bps: number): string => {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)}G`
  if (bps >= 1e6) return `${bps >= 1e7 ? Math.round(bps / 1e6) : (bps / 1e6).toFixed(1)}M`
  if (bps >= IDLE_BPS) return `${Math.round(bps / 1e3)}k`
  return "0"
}

export const fmtFull = (bps: number | null): string => {
  if (bps == null) return "—"
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`
  return `${Math.round(bps)} b/s`
}

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

export function bwRank(
  b: LinkBinding | undefined, fromId: number, toId: number,
  cores?: number | null,
): number {
  const tone = b ? linkTone(b) : null
  if (tone === "down") return Number.MAX_SAFE_INTEGER
  if (tone === "warn") return Number.MAX_SAFE_INTEGER - 1
  const { down, up } = b ? linkRates(b, fromId, toId) : { down: null, up: null }
  const rate = Math.max(down ?? 0, up ?? 0)
  if (rate === 0 && !b) return -1_000_000 + (cores ?? 0)
  return rate
}

export function linkTone(b: LinkBinding): "down" | "warn" | null {
  const ports = [...b.values()]
  if (ports.some((p) => p.oper_status === "down" || (p.monitored === 1 && p.alarm === 1)))
    return "down"
  if (ports.some((p) => p.bw_alarm === 1 || p.bw_high_alarm === 1)) return "warn"
  return null
}

export const linkLabelPos = (
  pts: Array<[number, number]>, frac?: number | null,
): [number, number] =>
  pointAlong(pts, polyKm(pts) * 1000 * (frac == null ? 0.5 : frac))

// THE RATE, AS A BODY AND A SENTENCE — one grammar, because the same reading now
// rides two marks. A link's own chord carries it while the fibre under it is
// unrecorded; the moment the glass is written down the chord stands down and the
// SHEATH carries it instead, and a rate that changed shape as the plant record filled
// in would read as two different measurements of one port.
export function linkRateBody(
  b: LinkBinding, from: { id: number; name: string }, to: { id: number; name: string },
): { html: string; ends: string; rateTitle: string; title: string
     hasRates: boolean; idle: boolean; tone: string | null } {
  const { down, up } = linkRates(b, from.id, to.id)
  const hasRates = down != null || up != null
  const idle = hasRates && bwIsIdle(down, up)
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const html = !hasRates
    ? `<span class="wisp-linkbw__port">${esc(portLabel([...b.values()][0]))}</span>`
    : idle
      ? `<span class="wisp-linkbw__idle">idle</span>`
      : `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`
  const ends = [[from, b.get(from.id)] as const, [to, b.get(to.id)] as const]
    .filter(([, p]) => p)
    .map(([d, p]) => `${d.name} ${portLabel(p!)}`)
    .join(" ↔ ")
  const rateTitle = hasRates
    ? `↓ ${fmtFull(down)} toward ${to.name} · ↑ ${fmtFull(up)}`
    : "no recent rate reading"
  return { html, ends, rateTitle,
           title: [ends, rateTitle].filter(Boolean).join(" · "),
           hasRates, idle, tone: linkTone(b) }
}

export function linkBwIcon(
  b: LinkBinding | undefined,
  from: { id: number; name: string }, to: { id: number; name: string },
  cable?: { cores: number | null; coreNo: number | null; name?: string | null },
): L.DivIcon | null {
  const cableText = cableChipText(cable?.cores, cable?.coreNo)
  if (!b && !cableText) return null

  const rate = b ? linkRateBody(b, from, to) : null
  const ends = rate ? rate.ends : `${from.name} ↔ ${to.name}`
  const cableTitle = cable?.cores || cable?.name
    ? [cable.name, cable.cores ? `${cable.cores}F` : null,
       cable.coreNo ? strandLabel(cable.coreNo, cable.cores)
         : cable.cores ? "strand not recorded" : null]
      .filter(Boolean).join(" · ")
    : null
  const title = esc([ends, cableTitle, rate?.rateTitle].filter(Boolean).join(" · "))

  const idle = !!rate?.idle
  const rateBody = rate?.html ?? ""

  const strand = cable?.coreNo
    ? `<span class="wisp-strand" style="--strand:${strandHex(cable.coreNo)}"></span>`
    : ""
  const cableBody = cableText
    ? `${strand}<span class="wisp-linkbw__cable">${esc(cableText)}</span>`
    : ""
  const body = cableBody && rateBody
    ? `${cableBody}<span class="wisp-linkbw__sep"></span>${rateBody}`
    : cableBody || rateBody

  const tone = rate?.tone ?? null
  const cls = ["wisp-linkbw"]
  if (idle && !tone) cls.push("wisp-linkbw--idle")
  if (tone) cls.push(`wisp-linkbw--${tone}`)
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${title}">${body}</div>`)
}
