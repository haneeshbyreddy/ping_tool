// THE operator colour vocabulary — one closed set for the whole product.
//
// It started life as the map's per-link palette and now also colours tags and
// probes, which is deliberate: a colour should mean the same thing wherever an
// operator meets it, and a second palette would drift out of step with this one
// the first time either was extended.
//
// Closed, never a free hex field. A picker that can produce red lets an operator
// paint a healthy thing the same colour as a broken one, faking an alarm on the
// screens that exist to show alarms — so every name here is clear of the status
// tones (no red, amber or green), and status always renders on top of a colour
// anyway (the row's dot, the map's stroke).
//
// Mirrored in central/inventory.py:PALETTE, which rejects anything else. The
// CSS token prefix is --map-line-* for historical reasons (the map got here
// first); the values live in index.css so they stay theme data.
export const PALETTE = ["violet", "magenta", "teal", "lime", "indigo", "chalk"] as const
export type PaletteColor = (typeof PALETTE)[number]

const NAMES: Record<PaletteColor, string> = {
  violet: "Violet", magenta: "Magenta", teal: "Teal",
  lime: "Lime", indigo: "Indigo", chalk: "Chalk",
}

export const isPaletteColor = (v: unknown): v is PaletteColor =>
  typeof v === "string" && (PALETTE as readonly string[]).includes(v)

export const paletteVar = (c: PaletteColor) => `var(--map-line-${c})`
export const paletteName = (c: PaletteColor) => NAMES[c]

/** The colour a value maps to, or null when it isn't a palette name (an
 *  uncoloured tag, or a row hand-edited into the DB). */
export const paletteVarOf = (c: string | null | undefined) =>
  isPaletteColor(c) ? paletteVar(c) : null

/** Soft fill for a chip in this colour — the same relationship status tones use
 *  (`status-badge.tsx`: full-strength text, ~13% fill), so a coloured tag chip
 *  sits at the same visual weight as the muted one it replaces. */
export const paletteSoft = (c: PaletteColor) =>
  `color-mix(in oklab, ${paletteVar(c)} 15%, transparent)`
