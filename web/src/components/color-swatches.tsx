import { X } from "lucide-react"

import { PALETTE, paletteName, paletteVar, type PaletteColor } from "@/lib/palette"
import { cn } from "@/lib/utils"

/** The closed-palette picker: six swatches plus "no colour".
 *
 *  Deliberately a fixed row rather than a colour input — see lib/palette.ts for
 *  why the set is closed. Small enough to live inside a dropdown or a table row,
 *  which is the point: colouring a tag shouldn't be a trip to Settings. */
export function ColorSwatches({ value, onPick, className }: {
  value?: string | null
  onPick: (color: PaletteColor | null) => void
  className?: string
}) {
  const ring = "ring-2 ring-foreground/70 ring-offset-2 ring-offset-popover"
  return (
    <div className={cn("flex shrink-0 items-center gap-1.5", className)}>
      {PALETTE.map((c) => (
        <button key={c} type="button" title={paletteName(c)} aria-label={paletteName(c)}
          aria-pressed={value === c}
          onClick={(e) => { e.stopPropagation(); onPick(c) }}
          style={{ background: paletteVar(c) }}
          className={cn("size-4 rounded-full transition", value === c ? ring : "hover:brightness-125")} />
      ))}
      <button type="button" title="No colour" aria-label="No colour" aria-pressed={!value}
        onClick={(e) => { e.stopPropagation(); onPick(null) }}
        className={cn(
          "flex size-4 items-center justify-center rounded-full border border-dashed border-border-strong text-faint-foreground transition",
          !value ? ring : "hover:text-foreground")}>
        <X className="size-2.5" />
      </button>
    </div>
  )
}
