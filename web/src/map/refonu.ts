import type L from "leaflet"
import { cachedDivIcon, esc } from "@/map/pins"
import { bwIsIdle, fmtFull, fmtShort } from "@/map/linklabel"
import { isFresh, onuName, onuSev } from "@/lib/format"
import type { OnuPlace } from "@/lib/types"

export const refName = (p: OnuPlace): string => onuName(p) || p.mac

export const isRefDark = (p: OnuPlace): boolean =>
  p.matched && p.state != null && p.state !== "online" && p.state !== "unknown"

export const isRefEvidence = (p: OnuPlace): boolean => p.witness && isRefDark(p)

export type RefTone = "dark" | "live" | "unknown"

export function refTone(p: OnuPlace): RefTone {
  if (!p.matched || p.state == null) return "unknown"
  if (isRefDark(p)) return "dark"
  return p.state === "online" ? "live" : "unknown"
}

export const refKind = (p: OnuPlace): string =>
  p.witness ? "reference ONU" : "subscriber"

export function refTitle(p: OnuPlace): string {
  const who = refName(p)
  const kind = refKind(p)
  if (!p.matched) return `${who} · ${kind} · no longer in any roster`
  if (p.ambiguous) return `${who} · ${kind} · on ${p.slots} live slots`
  const where = p.device_name ? ` · ${p.device_name} PON ${p.pon_port ?? "?"}` : ""
  return `${who} · ${kind}${where} · ${p.state ?? "unknown"}`
}

export function refOnuIcon(p: OnuPlace, o: { selected: boolean; dim: boolean }) {
  const cls = ["wisp-refonu", `wisp-refonu--${refTone(p)}`]
  if (o.selected) cls.push("wisp-refonu--selected")
  if (o.dim) cls.push("wisp-refonu--dim")
  if (!p.matched) cls.push("wisp-refonu--orphan")
  if (!p.witness) cls.push("wisp-refonu--plain")
  return cachedDivIcon(
    `<div class="${cls.join(" ")}">`
    + `<span class="wisp-refonu__mark"></span></div>`)
}

export function refZIndex(p: OnuPlace, selected: boolean, hovered = false): number {
  if (hovered) return -25
  if (selected) return -50
  return isRefEvidence(p) ? -100 : -200
}

export const REF_NAME_DY = 12

export function refHasRx(p: OnuPlace): boolean {
  return p.rx_dbm != null && p.state === "online" && isFresh(p.optics_updated_at)
}

export function refNameIcon(p: OnuPlace, o: { frozen: boolean }): L.DivIcon {
  const cls = ["wisp-refonu-name"]
  if (isRefEvidence(p)) cls.push("wisp-refonu-name--dark")
  if (!p.matched) cls.push("wisp-refonu-name--orphan")
  const showRx = !o.frozen && refHasRx(p)
  const rx = showRx
    ? `<span class="wisp-refonu-name__rx wisp-refonu-name__rx--${onuSev(p)}">`
      + `${(p.rx_dbm as number).toFixed(1)}</span>`
    : ""
  const title = esc(showRx
    ? `${refTitle(p)} · Rx ${(p.rx_dbm as number).toFixed(2)} dBm`
    : o.frozen ? `${refTitle(p)} · readings frozen · its OLT is down`
    : refTitle(p))
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${title}">`
    + `${esc(refName(p))}${rx}</div>`)
}

export const REF_DASH = "1 10"

export const REF_HOVER_BOOST = 1.5

export function refHasRate(p: OnuPlace): boolean {
  if (p.in_bps == null && p.out_bps == null) return false
  return isFresh(p.port_updated_at)
}

export function refLineTone(p: OnuPlace): "dark" | "quiet" {
  return isRefEvidence(p) ? "dark" : "quiet"
}

export function refHasChip(p: OnuPlace): boolean {
  return isRefDark(p) || refHasRate(p)
}

export function refChipPos(
  from: { lat: number; lng: number }, p: OnuPlace,
): [number, number] {
  const pts: Array<[number, number]> = [
    [from.lat, from.lng], ...(p.drop_waypoints ?? []), [p.lat, p.lng],
  ]
  const mid = (pts.length - 1) / 2
  const i = Math.floor(mid)
  const t = mid - i
  const [alat, alng] = pts[i]
  const [blat, blng] = pts[Math.min(i + 1, pts.length - 1)]
  return [alat + (blat - alat) * t, alng + (blng - alng) * t]
}

export function refBwIcon(p: OnuPlace): L.DivIcon | null {
  if (!refHasChip(p)) return null
  const hasRate = refHasRate(p)
  const dark = isRefDark(p)
  const who = refName(p)
  const port = p.if_name ? ` · ${p.if_name.split(" ")[0]}` : ""
  const down = p.out_bps
  const up = p.in_bps
  const title = esc(
    dark ? `${who}${port} · dark · power can't explain this on a reference ONU`
    : `${who}${port} · ↓ ${fmtFull(down)} to subscriber · ↑ ${fmtFull(up)}`)
  const ar = (g: string) => `<span class="wisp-linkbw__ar">${g}</span>`
  const idle = !dark && hasRate && bwIsIdle(down, up)
  const body = dark
    ? "dark"
    : idle
      ? `<span class="wisp-linkbw__idle">idle</span>`
      : `${ar("↓")}${fmtShort(down ?? 0)}${ar("↑")}${fmtShort(up ?? 0)}`
  const cls = ["wisp-linkbw", "wisp-linkbw--ref"]
  if (idle) cls.push("wisp-linkbw--idle")
  if (isRefEvidence(p)) cls.push("wisp-linkbw--down")
  return cachedDivIcon(`<div class="${cls.join(" ")}" title="${title}">${body}</div>`)
}
