import { ago, durationSince, fmtMs, fmtPct, isFresh, isStale } from "@/lib/format"
import { isPassiveType, type OrgDevice, type SplitterLoad } from "@/lib/types"
import { dropTone, isOversubscribed, ratioLabel } from "@/map/drops"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { esc, isDownState, pinTone } from "@/map/pins"

export interface DevHoverCtx {
  parentName: string | null
  parentDown: boolean
  load?: SplitterLoad
  totalSplit?: number | null
  frozen?: boolean
  frozenBy?: string | null
}

const TYPE_WORD: Record<string, string> = {
  olt: "OLT", cpe: "CPE", ap: "AP", fdb: "FDB",
}
export const typeWord = (t: string | null): string | null =>
  t ? TYPE_WORD[t.toLowerCase()] ?? t[0].toUpperCase() + t.slice(1) : null

function gearVerdict(d: OrgDevice): {
  tone: CardTone; word: string; hero: HoverCardModel["hero"]
} {
  const tone = pinTone(d)
  const stale = isStale(d.state_updated_at)
  if (d.maintenance) return { tone, word: "Maintenance", hero: null }
  if (!d.assigned_node_id) return { tone, word: "No probe assigned", hero: null }
  if (!d.state) return { tone, word: "Not polled yet", hero: null }
  if (stale) return { tone, word: `No recent poll · ${ago(d.state_updated_at)}`, hero: null }

  if (isDownState(d)) {
    const word = d.state === "UNREACHABLE" ? "Unreachable" : "Down"
    const since = d.outage_started_at
      ? durationSince(d.outage_started_at).split(" ")[0] : null
    return { tone, word: since ? `${word} · ${since}` : word, hero: null }
  }
  const hero = d.latency_ms != null
    ? { value: fmtMs(d.latency_ms), unit: "ms", quiet: tone === "success" }
    : null
  return { tone, word: d.state === "DEGRADED" ? "Degraded" : "Up", hero }
}

function gearRows(d: OrgDevice, c: DevHoverCtx): string[] {
  const rows: string[] = []
  const down = isDownState(d)
  const live = !down && !isStale(d.state_updated_at)

  if (down)
    rows.push(cardRow("Readings", "frozen while it is down", "wisp-mapcard__v--soft"))

  if (c.parentName)
    rows.push(cardRow("Uplink", esc(c.parentName)
      + (c.parentDown ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))

  if (live && d.onus_total != null) {
    if (!isFresh(d.optics_updated_at)) {
      rows.push(cardRow("ONUs", "last optical walk is stale",
                        "wisp-mapcard__v--soft"))
    } else {
      rows.push(cardRow("ONUs",
        `${d.onus_online ?? 0} of ${d.onus_total} online`, "wisp-mapcard__v--num"))
      const crit = d.onus_crit ?? 0
      const warn = d.onus_warn ?? 0
      const rx = d.onus_rx ?? 0
      if (rx === 0) {
        rows.push(cardRow("Signal", "not measured on this OLT",
                          "wisp-mapcard__v--soft"))
      } else if (crit || warn) {
        rows.push(cardRow("Signal", [crit && `${crit} critical`, warn && `${warn} weak`]
          .filter(Boolean).join(" · ")))
      } else if (rx < d.onus_total) {
        rows.push(cardRow("Signal", `${rx} of ${d.onus_total} measured`,
                          "wisp-mapcard__v--soft"))
      }
      const faults = [
        d.fiber_cuts && `${d.fiber_cuts} suspected fibre cut${d.fiber_cuts === 1 ? "" : "s"}`,
        d.dup_macs && `${d.dup_macs} duplicate MAC${d.dup_macs === 1 ? "" : "s"}`,
      ].filter(Boolean).join(" · ")
      if (faults) rows.push(cardRow("Optics", esc(faults)))
    }
  }

  if (live && isFresh(d.ports_updated_at)) {
    const ports = [
      d.ports_down && `${d.ports_down} down`,
      d.ports_bw_low && `${d.ports_bw_low} under floor`,
      d.ports_bw_high && `${d.ports_bw_high} over ceiling`,
    ].filter(Boolean).join(" · ")
    if (ports) rows.push(cardRow("Ports", esc(ports)))
  }

  if (live && d.packet_loss)
    rows.push(cardRow("Loss", esc(fmtPct(d.packet_loss)), "wisp-mapcard__v--num"))

  if (live && isFresh(d.health_updated_at)) {
    const vitals = [
      d.health_cpu_pct != null && `CPU ${Math.round(d.health_cpu_pct)}%`,
      d.health_temp_c != null && `${Math.round(d.health_temp_c)}°C`,
    ].filter(Boolean).join(" · ")
    if (vitals) rows.push(cardRow("Vitals", esc(vitals), "wisp-mapcard__v--num"))
  }
  return rows
}

function plantVerdict(c: DevHoverCtx): { tone: CardTone; word: string } {
  const load = c.load
  if (!load || load.recorded === 0)
    return { tone: "muted", word: "No subscribers recorded" }
  if (c.frozen)
    return { tone: "muted", word: `${load.recorded} recorded · state unknown` }
  const tone = dropTone(load, c.frozen)
  if (tone === "dark")
    return { tone: "destructive", word: `${load.dark} of ${load.recorded} recorded dark` }
  if (tone === "weak") {
    const weak = load.crit + load.warn
    return { tone: "warning", word: weak
      ? `${weak} of ${load.recorded} on weak signal`
      : `${load.outliers} below its own median` }
  }
  return { tone: "success", word: `${load.online} of ${load.recorded} recorded online` }
}

function plantRows(d: OrgDevice, c: DevHoverCtx): string[] {
  const rows: string[] = []
  const load = c.load
  const ratio = ratioLabel(d.split_ratio)

  if (c.frozen)
    rows.push(cardRow("Readings", c.frozenBy
      ? `frozen · ${esc(c.frozenBy)} is down` : "frozen · its OLT is down",
      "wisp-mapcard__v--soft"))

  if (c.parentName)
    rows.push(cardRow("Feed", esc(c.parentName)
      + (c.parentDown ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))

  if (load?.recorded)
    rows.push(cardRow("Drops", d.split_ratio
      ? `${load.recorded} recorded of ${d.split_ratio} legs`
      : `${load.recorded} recorded`, "wisp-mapcard__v--num"))
  if (load?.orphans)
    rows.push(cardRow("Orphans", `${load.orphans} in no roster`,
                      "wisp-mapcard__v--soft"))

  if (!c.frozen && load?.rx_median != null && load.rx_worst != null
      && load.rx_median - load.rx_worst >= 1)
    rows.push(cardRow("Worst", `${load.rx_worst.toFixed(1)} dBm`,
                      "wisp-mapcard__v--num"))

  const total = c.totalSplit
  const cumulative = total && total !== d.split_ratio ? `1:${total} total` : null
  if (ratio || cumulative)
    rows.push(cardRow("Split", esc([ratio, cumulative].filter(Boolean).join(" · ")),
                      "wisp-mapcard__v--num"))
  return rows
}

function plantHero(c: DevHoverCtx): HoverCardModel["hero"] {
  if (c.frozen || c.load?.rx_median == null) return null
  return { value: c.load.rx_median.toFixed(1), unit: "dBm", quiet: true }
}

function devModel(d: OrgDevice, c: DevHoverCtx): HoverCardModel {
  const passive = isPassiveType(d.device_type)
  const type = typeWord(d.device_type)
  if (passive) {
    const { tone, word } = plantVerdict(c)
    return {
      tone, name: d.name, word,
      sub: [type, d.pon_port && `PON ${d.pon_port}`].filter(Boolean).join(" · "),
      chip: isOversubscribed(d, c.load) ? "Over legs" : null,
      hero: plantHero(c),
      rows: plantRows(d, c),
    }
  }
  const { tone, word, hero } = gearVerdict(d)
  return {
    tone, name: d.name, word, hero,
    sub: [type, d.ip_address].filter(Boolean).join(" · "),
    chip: null,
    rows: gearRows(d, c),
  }
}

export function DevHoverCard({ device, ctx }: { device: OrgDevice; ctx: DevHoverCtx }) {
  if (device.lat == null || device.lng == null) return null
  return <HoverCard at={[device.lat, device.lng]} model={devModel(device, ctx)} />
}
