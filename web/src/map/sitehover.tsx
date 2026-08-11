import { durationSince, isFresh, isStale } from "@/lib/format"
import type { OrgDevice } from "@/lib/types"
import type { SiteCluster } from "@/map/clusters"
import { typeWord } from "@/map/devhover"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { esc, isDownState, pinTone } from "@/map/pins"

export interface SiteHoverCtx {
  uplinks: Array<{ name: string; down: boolean }>
}

function siteTone(members: OrgDevice[]): CardTone {
  const tones = new Set(members.map(pinTone))
  if (tones.has("destructive")) return "destructive"
  if (tones.has("warning")) return "warning"
  if (tones.has("success")) return "success"
  return "muted"
}

const quietReason = (d: OrgDevice): string =>
  d.maintenance ? "maintenance"
  : !d.assigned_node_id ? "no probe"
  : !d.state ? "not polled yet"
  : "no recent poll"

const NAME_CAP = 3

const names = (list: OrgDevice[]): string => {
  const shown = list.slice(0, NAME_CAP).map((m) => m.name).join(", ")
  const rest = list.length - NAME_CAP
  return rest > 0 ? `${shown} +${rest}` : shown
}

function memberRows(members: OrgDevice[]): string[] {
  const rows: string[] = []
  const by = (t: CardTone) => members.filter((m) => pinTone(m) === t)

  const down = by("destructive")
  if (down.length) {
    const since = down.length === 1 && down[0].outage_started_at
      ? durationSince(down[0].outage_started_at).split(" ")[0] : null
    rows.push(cardRow("Down", esc(names(down))
      + (since ? ` <span class="wisp-mapcard__v--soft">· ${esc(since)}</span>` : "")))
  }

  const warn = by("warning")
  if (warn.length) rows.push(cardRow("Degraded", esc(names(warn))))

  const quiet = by("muted")
  if (quiet.length) {
    const reasons = new Set(quiet.map(quietReason))
    const why = reasons.size === 1 ? [...reasons][0] : null
    rows.push(cardRow("No state", esc(names(quiet))
      + (why ? ` <span class="wisp-mapcard__v--soft">· ${esc(why)}</span>` : "")))
  }

  const up = by("success")
  if (up.length) rows.push(cardRow("Up", esc(names(up)), "wisp-mapcard__v--soft"))
  return rows
}

function rollupRows(members: OrgDevice[]): string[] {
  const rows: string[] = []
  let total = 0
  let online = 0
  let counted = 0
  let missing = 0
  let portsDown = 0
  let anyPorts = false
  for (const m of members) {
    const live = !isDownState(m) && !isStale(m.state_updated_at)
    if (m.onus_total != null) {
      if (live && isFresh(m.optics_updated_at)) {
        counted += 1
        total += m.onus_total
        online += m.onus_online ?? 0
      } else {
        missing += 1
      }
    }
    if (live && isFresh(m.ports_updated_at) && m.ports_down) {
      anyPorts = true
      portsDown += m.ports_down
    }
  }
  if (counted) {
    rows.push(cardRow("ONUs", `${online} of ${total} online`
      + (missing
        ? ` <span class="wisp-mapcard__v--soft">· ${missing} not reporting</span>` : ""),
      "wisp-mapcard__v--num"))
  } else if (missing) {
    rows.push(cardRow("ONUs", `${missing} OLT${missing === 1 ? "" : "s"} not reporting`,
                      "wisp-mapcard__v--soft"))
  }
  if (anyPorts) rows.push(cardRow("Ports", `${portsDown} down`, "wisp-mapcard__v--num"))
  return rows
}

function typeMix(members: OrgDevice[]): string | null {
  const counts = new Map<string, number>()
  for (const m of members) {
    const w = typeWord(m.device_type)
    if (w) counts.set(w, (counts.get(w) ?? 0) + 1)
  }
  if (!counts.size) return null
  return [...counts].map(([w, n]) => (n > 1 ? `${n} ${plural(w)}` : w)).join(" · ")
}

const plural = (w: string): string =>
  /(s|ch|sh|x|z)$/i.test(w) ? `${w}es` : `${w}s`

function siteModel(c: SiteCluster, ctx: SiteHoverCtx): HoverCardModel {
  const members = c.members
  const n = members.length
  const down = members.filter((m) => pinTone(m) === "destructive").length
  const warn = members.filter((m) => pinTone(m) === "warning").length
  const up = members.filter((m) => pinTone(m) === "success").length

  const word = down ? `${down} of ${n} down`
    : warn ? (warn === n ? `All ${n} degraded` : `${warn} of ${n} degraded`)
    : up === 0 ? "None reporting"
    : up < n ? `${up} of ${n} up`
    : `All ${n} up`

  const rows = memberRows(members)
  if (ctx.uplinks.length === 1) {
    rows.push(cardRow("Feed", esc(ctx.uplinks[0].name)
      + (ctx.uplinks[0].down ? ` <span class="wisp-mapcard__v--soft">· down</span>` : "")))
  } else if (ctx.uplinks.length > 1) {
    const anyDown = ctx.uplinks.some((u) => u.down)
    rows.push(cardRow("Feeds", `${ctx.uplinks.length} boxes`
      + (anyDown ? ` <span class="wisp-mapcard__v--soft">· 1 or more down</span>` : ""),
      "wisp-mapcard__v--soft"))
  }
  rows.push(...rollupRows(members))

  return {
    tone: siteTone(members),
    name: `${n} devices at this site`,
    sub: typeMix(members),
    chip: null,
    word,
    hero: null,
    rows,
  }
}

export function SiteHoverCard({ cluster, ctx }: {
  cluster: SiteCluster; ctx: SiteHoverCtx
}) {
  return <HoverCard at={cluster.center} model={siteModel(cluster, ctx)} />
}
