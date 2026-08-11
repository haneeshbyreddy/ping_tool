import type L from "leaflet"
import { cachedDivIcon, esc } from "@/map/pins"
import type { BranchFault, OrgDevice, SplitterLoad } from "@/lib/types"

export type DropTone = "dark" | "weak" | "ok" | "quiet"

export function dropTone(load: SplitterLoad | undefined, frozen = false): DropTone {
  if (frozen) return "quiet"
  if (!load || load.recorded === 0) return "quiet"
  if (load.dark > 0) return "dark"
  if (load.crit > 0 || load.warn > 0 || load.outliers > 0) return "weak"
  return "ok"
}

export function ratioLabel(
  ratio: number | null | undefined, inputs?: number | null,
): string | null {
  return ratio ? `${inputs && inputs > 1 ? inputs : 1}:${ratio}` : null
}

export const deviceRatioLabel = (
  d: Pick<OrgDevice, "split_ratio" | "split_inputs">,
): string | null => ratioLabel(d.split_ratio, d.split_inputs)

export const hasProtectionInput = (
  d: Pick<OrgDevice, "split_inputs">,
): boolean => (d.split_inputs ?? 1) > 1

export const isOversubscribed = (d: OrgDevice, load?: SplitterLoad): boolean =>
  !!d.split_ratio && !!load && load.recorded > d.split_ratio

export function passivePinLabel(d: OrgDevice): string {
  return deviceRatioLabel(d) ?? d.name
}

export function passiveTitle(d: OrgDevice, load?: SplitterLoad,
                             frozen = false): string {
  const bits: string[] = [d.name]
  const ratio = deviceRatioLabel(d)
  if (ratio) bits.push(`${ratio} splitter`)
  if (d.pon_port) bits.push(`PON ${d.pon_port}`)
  if (!load || load.recorded === 0) {
    bits.push("no subscribers recorded")
  } else if (frozen) {
    bits.push(`${load.recorded} recorded subscriber${load.recorded === 1 ? "" : "s"}`)
    bits.push("readings frozen while its OLT is down")
  } else {
    bits.push(`${load.recorded} recorded subscriber${load.recorded === 1 ? "" : "s"}`)
    if (load.dark) bits.push(`${load.dark} dark`)
    if (load.crit) bits.push(`${load.crit} critical Rx`)
    else if (load.warn) bits.push(`${load.warn} weak Rx`)
    if (load.outliers) bits.push(`${load.outliers} below this splitter's own median`)
    if (isOversubscribed(d, load)) bits.push("MORE DROPS THAN LEGS")
  }
  return bits.join(" · ")
}

export const DROP_DASH = "1 7"

export function dropAnchor(
  splitterId: number | null | undefined, oltId: number | null | undefined,
  byId: Map<number, OrgDevice>,
): { device: OrgDevice; kind: "splitter" | "olt" } | null {
  const sp = splitterId != null ? byId.get(splitterId) : undefined
  if (sp && sp.lat != null && sp.lng != null) return { device: sp, kind: "splitter" }
  const olt = oltId != null ? byId.get(oltId) : undefined
  if (olt && olt.lat != null && olt.lng != null) return { device: olt, kind: "olt" }
  return null
}

export const branchLinkKey = (f: BranchFault): string =>
  `${f.passive_id}:${f.parent_id}`

export function branchTitle(f: BranchFault, name: string, parentName: string): string {
  const what = f.cause === "power"
    ? "Power loss on this branch"
    : f.suspected ? "Suspected fibre break" : "Fibre break"
  const pon = f.pon_ports.length ? ` · PON ${f.pon_ports.join(", ")}` : ""
  return `${what}${pon} · all ${f.dark} recorded subscriber`
    + `${f.dark === 1 ? "" : "s"} below ${name} are dark`
    + ` while ${f.lit_siblings} on sibling branches stay lit`
    + ` · suspect the span ${parentName} → ${name}`
    + (f.witness_dark
      ? ` · ${f.witness_dark} power-backed reference ONU dark, so power can't explain it`
      : "")
}

export function branchIcon(f: BranchFault, name: string, parentName: string): L.DivIcon {
  const cls = ["wisp-branchfault", `wisp-branchfault--${f.cause}`]
  return cachedDivIcon(
    `<div class="${cls.join(" ")}" title="${esc(branchTitle(f, name, parentName))}">`
    + `${f.cause === "power" ? "⚡" : "✂"}</div>`)
}

export function loadsById(loads: SplitterLoad[] | undefined): Map<number, SplitterLoad> {
  return new Map((loads ?? []).map((l) => [l.passive_id, l]))
}
