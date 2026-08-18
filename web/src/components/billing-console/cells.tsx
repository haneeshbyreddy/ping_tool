// The console's shared readouts. The fleet table and the drawer's daily meter
// print the same facts about the same accrual row, so they print them through
// the same components: a count that looks estimated in one place and solid in
// the other would be the bug this console exists to catch.

import { AlertTriangle } from "lucide-react"
import { cn } from "@/lib/utils"
import { accrualFlagNote, connSourceMeta, inrAuto } from "@/lib/billing"
import type { Accrual, AccrualFlags, StoredConnSource } from "@/lib/types"
import { Reading } from "@/components/reading"
import { FLAG_WORD, flagKind } from "./rows"

/** accrualFlagNote reads ONE field off its argument. The console's `today` is
 *  a thinner row than a full Accrual (every column nullable), so it is handed
 *  the field the helper actually reads rather than a fabricated row. One cast,
 *  one place, and the wording stays in lib/billing where the ranking lives. */
export function flagNote(flags: AccrualFlags | undefined): string | null {
  return flags ? accrualFlagNote({ flags } as unknown as Accrual) : null
}

/** Money owed, credit-aware. A negative balance is NOT printed as a minus:
 *  advance payment is the credit mechanism here, so the sign is a fact about
 *  whose side the money is on and gets a word, not a symbol a reader has to
 *  decode. Zero prints ₹0, because square is a measured state. */
export function Outstanding({ paise, className, big = false }: {
  paise: number
  className?: string
  big?: boolean
}) {
  const credit = paise < 0
  return (
    <span className={cn("flex flex-col items-end leading-tight", className)}
      title={credit
        ? `${inrAuto(-paise)} paid ahead, applied against future accrual.`
        : paise === 0 ? "Square. Nothing owed." : `${inrAuto(paise)} outstanding.`}>
      <span className={cn("font-mono whitespace-nowrap tabular-nums",
        big ? "text-xl font-semibold" : "text-xs",
        paise === 0 && "text-faint-foreground")}>
        {inrAuto(credit ? -paise : paise)}
      </span>
      {credit && (
        <span className="text-2xs font-medium text-muted-foreground">credit</span>
      )}
    </span>
  )
}

/** A balance as a sentence fragment, for prose and previews. Same credit rule
 *  as <Outstanding>: the sign becomes a word, never a symbol. */
export function balanceText(paise: number): string {
  return paise < 0 ? `${inrAuto(-paise)} in credit` : inrAuto(paise)
}

/** One movement in the ledger. The DIRECTION leads, because an adjustment may
 *  go either way and a bare minus buried in a money column is the one thing an
 *  operator must not misread here. Positive lowers what the org owes. */
export function SignedAmount({ paise, className }: {
  paise: number
  className?: string
}) {
  const charge = paise < 0
  return (
    <span className={cn("font-mono text-xs tabular-nums", className)}
      title={charge
        ? "Adds to what this org owes."
        : "Reduces what this org owes."}>
      {charge ? "−" : "+"}{inrAuto(Math.abs(paise))}
    </span>
  )
}

/** Today's ONU count, in the Reading grammar. A held count must LOOK
 *  estimated (dotted underline) and a count nothing could answer gets the dead
 *  zone, never a zero: "no ONUs online" and "no roster answered" are opposite
 *  findings and a bill rides on which one this is. */
export function ConnCell({ count, source, absentReason }: {
  count: number | null | undefined
  source: StoredConnSource | null | undefined
  absentReason: string
}) {
  const meta = connSourceMeta(source)
  const missing = count == null
  return (
    <span className="flex flex-col items-end leading-tight">
      <Reading
        value={missing ? null : count.toLocaleString("en-IN")}
        state={missing ? "absent" : meta.reading}
        reason={missing ? absentReason : meta.detail}
        className="text-xs"
      />
      {!missing && (
        <span className="text-2xs text-faint-foreground" title={meta.detail}>
          {meta.label}
        </span>
      )}
    </span>
  )
}

/** What went wrong with a day's count, or nothing at all. Only a DOWNGRADE
 *  takes the alarm tone: it is the one flag that moved the bill on its own
 *  (a rotated credential dropping an org to a cheaper source). A hold or a
 *  lateral move is news the operator should see, not an alarm to chase. */
export function FlagCell({ flags, className }: {
  flags: AccrualFlags | undefined
  className?: string
}) {
  const kind = flagKind(flags)
  const note = flagNote(flags)
  if (!kind || !note) {
    return <span className={cn("text-2xs text-faint-foreground", className)}>—</span>
  }
  const hot = kind === "downgraded"
  return (
    <span className={cn("flex min-w-0 items-center gap-1.5", className)} title={note}>
      {hot && <AlertTriangle className="size-3 shrink-0 text-warning" aria-hidden />}
      <span className={cn("shrink-0 text-2xs font-medium",
        hot ? "text-warning" : "text-muted-foreground")}>
        {FLAG_WORD[kind]}
      </span>
      <span className="truncate text-2xs text-faint-foreground">{note}</span>
    </span>
  )
}

/** Which side of max(ONUs, device floor) set today's charge. Printed
 *  under the amount because it is the whole arithmetic of this product in two
 *  words, and an operator reading a bill they did not expect asks it first. */
export function WinningSide({ side }: { side: "conn" | "floor" | null | undefined }) {
  if (!side) return null
  return (
    <span className="text-2xs text-faint-foreground"
      title={side === "conn"
        ? "ONUs outran the device floor today. Billed per ONU."
        : "The device floor outran the ONU count today. Billed on the floor."}>
      {side === "conn" ? "ONUs" : "device floor"}
    </span>
  )
}
