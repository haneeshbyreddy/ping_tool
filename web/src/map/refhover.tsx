import { onuSev, onuSubName } from "@/lib/format"
import type { OnuPlace } from "@/lib/types"
import { fmtKm } from "@/map/geometry"
import { HoverCard, cardRow, type CardTone, type HoverCardModel } from "@/map/hovercard"
import { bwIsIdle, fmtShort } from "@/map/linklabel"
import { esc } from "@/map/pins"
import { isRefDark, refHasRate, refHasRx, refName } from "@/map/refonu"

export interface RefHoverCtx {
  anchorName: string | null
  viaSplitter: boolean
  dropKm: number | null
  frozen: boolean
}

function verdict(p: OnuPlace, showRx: boolean): { tone: CardTone; word: string } {
  if (!p.matched) return { tone: "warning", word: "Not in any roster" }
  if (p.ambiguous) return { tone: "warning", word: `On ${p.slots} live slots` }
  if (isRefDark(p)) return { tone: "destructive", word: `Dark · ${p.state ?? "offline"}` }
  if (p.state !== "online") return { tone: "muted", word: "State unknown" }
  const sev = onuSev(p)
  if (showRx && sev === "crit")
    return { tone: "destructive", word: "Online · critical signal" }
  if (showRx && sev === "warn")
    return { tone: "warning", word: "Online · weak signal" }
  return { tone: "success", word: "Online" }
}

function rxNote(p: OnuPlace, c: RefHoverCtx, showRx: boolean): string | null {
  if (showRx || !p.matched || isRefDark(p)) return null
  if (c.frozen) return "frozen · its OLT is down"
  if (p.rx_dbm == null) return "not measured on this OLT"
  return "last reading is stale"
}

function refModel(p: OnuPlace, c: RefHoverCtx): HoverCardModel {
  const showRx = !c.frozen && refHasRx(p)
  const { tone, word } = verdict(p, showRx)
  const sev = onuSev(p)
  const dark = isRefDark(p)

  const rows: string[] = []
  const note = rxNote(p, c, showRx)
  if (note) rows.push(cardRow(c.frozen ? "Readings" : "Signal", esc(note),
                              "wisp-mapcard__v--soft"))

  if (c.viaSplitter && c.anchorName) {
    rows.push(cardRow("Drop", esc(c.anchorName)
      + (c.dropKm != null
        ? ` <span class="wisp-mapcard__v--soft">· ${esc(fmtKm(c.dropKm))}</span>`
        : "")))
  } else if (p.matched) {
    rows.push(cardRow("Drop", "not recorded", "wisp-mapcard__v--soft"))
  }
  if (p.matched) {
    const where = [p.device_name, p.pon_port && `PON ${p.pon_port}`]
      .filter(Boolean).join(" · ")
    if (where) rows.push(cardRow("On", esc(where)))
  }

  if (p.matched && !dark && !c.frozen) {
    if (refHasRate(p)) {
      const down = p.out_bps   // ↓ toward the subscriber = the OLT port's egress
      const up = p.in_bps
      rows.push(cardRow("Traffic", bwIsIdle(down, up)
        ? `<span class="wisp-mapcard__v--soft">idle</span>`
        : `<span class="wisp-mapcard__ar">↓</span>${esc(fmtShort(down ?? 0))}`
          + `<span class="wisp-mapcard__ar">↑</span>${esc(fmtShort(up ?? 0))}`,
        "wisp-mapcard__v--num"))
    } else {
      rows.push(cardRow("Traffic", p.if_name
        ? "no recent reading · port walk stale"
        : "no per-ONU interface on this OLT", "wisp-mapcard__v--soft"))
    }
  }

  // The card is the one subscriber surface with room for the second identity.
  // The mark and its plate carry the username (the identifier everyone here
  // recognises); the account holder's name is what you read out on the phone,
  // and it fits in a labelled row without competing with the reading above it.
  const who = onuSubName(p)
  if (who) rows.push(cardRow("Customer", esc(who)))

  if (p.phone) rows.push(cardRow("Phone", esc(p.phone), "wisp-mapcard__v--num"))

  return {
    tone,
    name: refName(p),
    sub: p.mac,
    chip: p.witness ? "Reference" : null,
    word,
    hero: showRx
      ? { value: (p.rx_dbm as number).toFixed(2), unit: "dBm", quiet: sev === "ok" }
      : null,
    rows,
  }
}

export function RefHoverCard({ place, ctx }: { place: OnuPlace; ctx: RefHoverCtx }) {
  return <HoverCard at={[place.lat, place.lng]} model={refModel(place, ctx)} />
}
