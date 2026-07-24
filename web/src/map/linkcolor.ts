// Per-link colour: the operator's cartography for telling PARALLEL cables apart.
//
// Two switches with both a primary feed and a cross-link between them draw two
// lines along nearly the same chord, and their bandwidth chips end up stacked —
// which label belongs to which line was unanswerable. A colour separates them,
// and the chip inherits it (see linklabel.ts), so the pairing is visible without
// clicking anything.
//
// The palette is THE product-wide operator vocabulary, now shared with tag and
// probe colours — it lives in lib/palette.ts and is mirrored in
// central/inventory.py (the server rejects anything else). Links keep these
// aliases because "link colour" is what the map's callers mean; there is only
// ever one set of names.
import { PALETTE, isPaletteColor, paletteName, paletteVar, type PaletteColor } from "@/lib/palette"

export const LINK_COLORS = PALETTE
export type LinkColor = PaletteColor

export const isLinkColor = isPaletteColor
export const linkColorVar = paletteVar
export const linkColorName = paletteName

/** The colour a link's stroke actually renders in.
    Trouble ALWAYS wins: a down link is red whatever the operator painted it, or
    the one screen that exists to show alarms could be made to hide one. The
    custom colour is what a HEALTHY line looks like, nothing more. */
export function paintedLineColor(
  tone: string, color: string | null | undefined, fallback: string,
): string {
  if (tone === "destructive" || tone === "warning") return fallback
  return isLinkColor(color) ? linkColorVar(color) : fallback
}
