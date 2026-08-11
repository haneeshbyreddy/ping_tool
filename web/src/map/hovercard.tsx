import L from "leaflet"
import { Marker, useMap } from "react-leaflet"
import { esc } from "@/map/pins"

const CARD_REM = 15.5

const cardPx = (): number => {
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize)
  return CARD_REM * (Number.isFinite(root) && root > 0 ? root : 16)
}

const CARD_CLEARANCE = 198

export type CardTone = "success" | "warning" | "destructive" | "muted"

export interface HoverCardModel {
  tone: CardTone
  name: string
  sub?: string | null
  chip?: string | null
  word: string
  hero?: { value: string; unit?: string; quiet?: boolean } | null
  rows: string[]
}

export const cardRow = (k: string, v: string, cls = ""): string =>
  `<div class="wisp-mapcard__row"><span class="wisp-mapcard__k">${esc(k)}</span>`
  + `<span class="wisp-mapcard__v${cls ? ` ${cls}` : ""}">${v}</span></div>`

function cardHtml(m: HoverCardModel, pos: { below: boolean; shift: number }): string {
  const hero = m.hero
    ? `<span class="wisp-mapcard__hero${m.hero.quiet ? " wisp-mapcard__hero--quiet" : ""}">`
      + esc(m.hero.value)
      + (m.hero.unit ? `<span class="wisp-mapcard__unit">${esc(m.hero.unit)}</span>` : "")
      + `</span>`
    : ""
  const chip = m.chip ? `<span class="wisp-mapcard__chip">${esc(m.chip)}</span>` : ""
  const sub = m.sub ? `<div class="wisp-mapcard__mac">${esc(m.sub)}</div>` : ""
  return `<div class="wisp-mapcard wisp-mapcard--${pos.below ? "below" : "above"}"`
    + ` style="--sx:${pos.shift}px">`
    + `<div class="wisp-mapcard__box wisp-mapcard--${m.tone}">`
    + `<div class="wisp-mapcard__head">`
    + `<div class="wisp-mapcard__who"><span class="wisp-mapcard__name">`
    + `${esc(m.name)}</span>${chip}</div>${sub}</div>`
    + `<div class="wisp-mapcard__state">`
    + `<span class="wisp-mapcard__dot"></span>`
    + `<span class="wisp-mapcard__word">${esc(m.word)}</span>${hero}</div>`
    + (m.rows.length ? `<div class="wisp-mapcard__rows">${m.rows.join("")}</div>` : "")
    + `<span class="wisp-mapcard__tail"></span>`
    + `</div></div>`
}

const _icons = new Map<string, L.DivIcon>()

function cardIcon(html: string): L.DivIcon {
  const hit = _icons.get(html)
  if (hit) return hit
  const icon = L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
  if (_icons.size >= 2) _icons.delete(_icons.keys().next().value as string)
  _icons.set(html, icon)
  return icon
}

export function HoverCard({ at, model }: {
  at: [number, number]
  model: HoverCardModel
}) {
  const map = useMap()
  const pt = map.latLngToContainerPoint(at)
  const size = map.getSize()

  const below = pt.y < CARD_CLEARANCE
  const half = cardPx() / 2
  const limit = half - 20
  const raw = Math.max(0, half + 10 - pt.x)
    - Math.max(0, pt.x + half + 10 - size.x)
  const shift = Math.round(Math.max(-limit, Math.min(limit, raw)))

  return (
    <Marker
      position={at}
      icon={cardIcon(cardHtml(model, { below, shift }))}
      interactive={false}
      zIndexOffset={1500}
    />
  )
}
