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

export const paletteVarOf = (c: string | null | undefined) =>
  isPaletteColor(c) ? paletteVar(c) : null

export const paletteSoft = (c: PaletteColor) =>
  `color-mix(in oklab, ${paletteVar(c)} 15%, transparent)`
