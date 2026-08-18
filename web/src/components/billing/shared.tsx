// Shared furniture for the billing surface: the pieces more than one panel
// needs. Anything used once stays in the panel that uses it.
import type { ReactNode } from "react"
import { CHIP_BOX } from "@/components/status-badge"
import { stageMeta } from "@/lib/billing"
import type { BillingStage } from "@/lib/types"
import { cn } from "@/lib/utils"

// The server's own ceiling (central/api/billing.py: _MAX_PAY_PAISE).
// Mirrored so a field refuses what the route would refuse anyway: a 422 the
// input could have caught reads as a broken button, and this page's whole job
// is that money never surprises anybody. If the route's number moves, move
// this with it.
export const MAX_PAY_PAISE = 100_000_000   // ₹10,00,000 in one payment

// stageMeta ships the label plus the fill and the ink; the border is the one
// thing it leaves to the caller, and it cannot be skipped: CHIP_BOX declares a
// border WIDTH, so an unstated colour falls back to currentColor and the chip
// ends up outlined in its own text tone, louder than every other chip in the
// product.
const STAGE_BORDER: Record<BillingStage, string> = {
  clear: "border-success/30",
  banner: "border-warning/30",
  locked: "border-destructive/30",
  deactivated: "border-destructive/30",
  exempt: "border-border",
}

export function StageChip({ stage, daysOverdue }: {
  stage: BillingStage
  daysOverdue: number
}) {
  const m = stageMeta(stage, daysOverdue)
  return (
    <span className={cn(CHIP_BOX, STAGE_BORDER[stage], m.className)}>
      {m.label}
    </span>
  )
}

/** The standard panel object. Every section on this page is one, so the
 *  header rhythm stays a single decision rather than a Tailwind string copied
 *  seven times. */
export function Panel({ title, note, right, children, className }: {
  title: string
  note?: ReactNode
  right?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cn("wisp-panel flex flex-col", className)}>
      <div className="wisp-panel-head">
        <h2 className="flex min-w-0 items-baseline gap-2">
          <span className="text-sm font-semibold whitespace-nowrap text-foreground">
            {title}
          </span>
          {note != null && (
            <span className="truncate text-xs text-faint-foreground">{note}</span>
          )}
        </h2>
        {right}
      </div>
      {children}
    </section>
  )
}

/** The designed empty state. Not a blank panel: an empty table on a money page
 *  is indistinguishable from one that failed to load. Centres in whatever
 *  height a stretched grid gives it, so a pair of panels beside each other
 *  don't read as one full and one broken. */
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center px-4 py-8">
      <p className="max-w-72 text-center text-xs text-balance text-faint-foreground">
        {children}
      </p>
    </div>
  )
}

/** A row label above a number. The uppercase micro-label, at one size. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="wisp-eyebrow block">{children}</span>
}

/** A rupee string the operator typed, as integer paise. Null when it is not an
 *  amount we can bill.
 *
 *  The INPUT direction. lib/billing.ts owns paise-to-rupees for every display
 *  on the page and nothing here duplicates it; there is simply no helper for
 *  the way back, because this form is the only place a human types money.
 *
 *  Deliberately NOT Number(x) * 100: 39.87 * 100 is 3986.9999… in binary
 *  floating point, so the naive form charges a paise less than the figure on
 *  the screen. Splitting on the point and adding integers cannot drift. */
export function toPaise(raw: string): number | null {
  const s = raw.replace(/[,\s₹]/g, "")
  if (s === "" || s === "." || !/^\d*(\.\d{0,2})?$/.test(s)) return null
  const [rupees, paise = ""] = s.split(".")
  return Number(rupees || "0") * 100 + Number(paise.padEnd(2, "0"))
}

/** Paise back into the rupee string a text field starts with. Whole amounts
 *  lose the ".00" so the common case is one tap to edit. */
export function rupeeInput(paise: number): string {
  const abs = Math.max(0, Math.round(paise))
  return abs % 100 === 0 ? String(abs / 100) : (abs / 100).toFixed(2)
}
