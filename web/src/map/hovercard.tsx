// The frame every map hover card opens in, and the arithmetic that keeps it
// pointing at its own mark.
//
// Two things on this map answer "what is this" without a click — a subscriber
// diamond (map/refhover.tsx) and a device pin (map/devhover.tsx) — and a second
// card grammar for the second one is how a dashboard stops reading as one
// product. So the FRAME is here: the surface, the tail, the edge clamp, the
// tinted verdict row, the label/value rows, the one-at-a-time icon cache. Each
// caller supplies only a model of what it may claim, which is the part that is
// genuinely different between a customer and a box.
//
// Everything here is non-interactive by construction. These cards can grow to
// cover a good part of a dense site, and one that swallowed a click would make
// the mark under it HARDER to open than it was before the card existed.
import L from "leaflet"
import { Marker, useMap } from "react-leaflet"
import { esc } from "@/map/pins"

/** Card width, in REM — mirrored by `.wisp-mapcard__box`.
 *
 *  It has to be rem, and finding that out cost a browser pass: a fixed 236px
 *  card looked right at 1440px and clipped its longest row ("NDN-OLT · PON
 *  EPON0/5") on a 1600px screen, because the root font-size scales up at ≥1600px
 *  and the type inside the card grew while the card did not. The truncation lands
 *  on the OLT and PON — the two things a crew needs off this card — which is the
 *  same failure the PDF's column solver was written to stop: a fixed share of the
 *  width starving the identifier column.
 *
 *  But the edge clamp below is arithmetic in SCREEN pixels, so the two have to be
 *  reconciled at runtime rather than by picking one unit and hoping. `cardPx`
 *  reads the ramp the CSS is actually on. */
const CARD_REM = 15.5

/** The card's width in px right now, at whatever step of the type ramp the root
 *  is on. One read of the root font-size per card — there is only ever one. */
const cardPx = (): number => {
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize)
  return CARD_REM * (Number.isFinite(root) && root > 0 ? root : 16)
}

/** Vertical room the tallest card needs, plus its tail and a little air. Under
 *  this much space above the mark the card flips below rather than being clipped
 *  by the top of the map. Tracks the 32px above-gap in `.wisp-mapcard--above`,
 *  which grew by 8px when the subscriber mark became a location pin standing on
 *  its coordinate rather than a diamond straddling it. */
const CARD_CLEARANCE = 198

export type CardTone = "success" | "warning" | "destructive" | "muted"

export interface HoverCardModel {
  /** Seeds the dot, the verdict word and the wash together, so those three can
   *  never disagree about what state the thing is in. */
  tone: CardTone
  /** What to call it: the customer's name, the box's name. */
  name: string
  /** The mono identifier beneath it — a MAC, an IP, a PON. Never a repeat of
   *  the name, and empty is fine. */
  sub?: string | null
  /** A CLAIM worth chipping beside the name. Not the type, not the state — see
   *  `.wisp-mapcard__chip`. */
  chip?: string | null
  /** The one-line verdict. It carries what the TONE means in words, or a red
   *  band reading "Online" gets read as "down". */
  word: string
  /** The one number riding the verdict row. Present only when the card can
   *  stand behind it; `quiet` opts a healthy value out of the tone. */
  hero?: { value: string; unit?: string; quiet?: boolean } | null
  /** Label/value rows, pre-built with `cardRow` — values may carry markup. */
  rows: string[]
}

/** One label/value row. `v` is markup (callers wrap numbers and soft text in
 *  their own spans), so anything coming from the DB must be `esc`'d by the
 *  caller; `k` is escaped here because it is always a literal. */
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

// A TWO-ENTRY ICON CACHE, and it is not an optimization — it is what stops the
// card blinking. MapPage re-renders every second off `useNow()`, and a fresh
// L.divIcon each tick makes react-leaflet swap the DOM node, which REPLAYS the
// mount animation: a card that flashes once a second while you read it.
//
// Two, not one: only one card is meant to be open at a time, but a single slot
// would thrash if that ever stopped being true — each card evicting the other's
// html every render, which is the blink this exists to prevent, arriving by a
// route nobody would think to look for.
//
// Deliberately NOT the shared `cachedDivIcon`. Hovering a few hundred marks over
// a shift would otherwise fill that cache and trip its wholesale clear(), which
// restarts the down-pulse on every pin on the map.
const _icons = new Map<string, L.DivIcon>()

function cardIcon(html: string): L.DivIcon {
  const hit = _icons.get(html)
  if (hit) return hit
  const icon = L.divIcon({ className: "wisp-pin-anchor", iconSize: [0, 0], html })
  if (_icons.size >= 2) _icons.delete(_icons.keys().next().value as string)
  _icons.set(html, icon)
  return icon
}

/** The card, anchored on the mark it describes.
 *
 *  Rendered as a Marker rather than a floating panel so it stays glued to the
 *  coordinate: a card that drifts off its pin during a pan is a card pointing at
 *  the wrong house. */
export function HoverCard({ at, model }: {
  at: [number, number]
  model: HoverCardModel
}) {
  const map = useMap()
  const pt = map.latLngToContainerPoint(at)
  const size = map.getSize()

  // ABOVE by default, and that is not a coin toss: every mark here wears its
  // name plate BELOW itself, so a card below would land on the one piece of text
  // already answering "which one is this". It flips only when there isn't room
  // above, where being clipped by the top of the map is the worse failure.
  const below = pt.y < CARD_CLEARANCE
  // …and slide it back inside the viewport near the left and right edges, with
  // the TAIL staying over the mark. A card clipped by the map frame is the same
  // failure in the other axis, and clamping the slide keeps the tail from
  // sliding off its own corner and pointing at nothing.
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
      // Above every pin: it is transient, it is pointed AT one of them, and it
      // cannot take a click from the one it covers.
      zIndexOffset={1500}
    />
  )
}
