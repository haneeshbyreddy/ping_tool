// THE HEADLINE. One state, chosen once, from facts the server already sent —
// the SPA composes the sentence and guesses nothing (the rx-diagnosis rule,
// applied to money). The amount is the biggest number on the page because it
// is the only thing most owners open this page to read.
//
// THREE CHANNELS, EACH SAYING SOMETHING DIFFERENT: the chip carries the
// account's state, the eyebrow names what the figure is, and the sub says what
// to do about it. An earlier cut had "Deactivated" in all three at once, which
// is how a hero ends up loud and uninformative.
//
// The big slot is ALWAYS a rupee figure, never a word. That keeps one type
// size in one box across all seven states, so the number cannot move as data
// arrives, and it means "not billed" still answers the question the operator
// actually asked, which is how much.
//
// TONE: plain foreground ink everywhere except overdue. Money is not an alarm
// axis, and a green balance would spend a status colour on "nothing is wrong",
// which is exactly what Axis A is reserved against. Overdue IS an alarm: past
// the banner window the dashboard locks, so it takes destructive outright.
import type { ReactNode } from "react"
import { useAuth } from "@/hooks/use-auth"
import { dayLabel, inr, monthLabel } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { StageChip } from "./shared"

type Action = "due" | "advance" | "none"

interface Hero {
  /** What the figure below it IS. Never the same word as the stage chip. */
  eyebrow: string
  /** Integer paise. Always present: one type size, one box, no shift. */
  amount: number
  alarm: boolean
  sub: ReactNode
  action: Action
}

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`

/** The one honest headline, in priority order. Every branch reads only what
 *  /api/billing shipped; nothing here re-derives a number the server owns. */
function heroState(b: BillingInfo): Hero {
  const owed = Math.max(0, b.outstanding_paise)
  const mtd = b.month_to_date_paise
  const counted = b.accruals.length
  const contact = b.payment.admin_contact

  if (b.deactivated) {
    return {
      eyebrow: owed > 0 ? "Outstanding" : "Nothing due",
      amount: owed,
      // A red ₹0 would be an alarm about nothing. The chip carries the state.
      alarm: owed > 0,
      action: owed > 0 ? "due" : "none",
      sub: (
        <>
          This account has been switched off.
          {contact
            ? <> Contact {contact} to switch it back on.</>
            : <> Contact your administrator to switch it back on.</>}
        </>
      ),
    }
  }

  if (b.exempt) {
    return {
      eyebrow: owed > 0 ? "Outstanding" : "Nothing to pay",
      amount: owed,
      alarm: false,
      action: "none",
      // Exempt orgs can still carry a balance from before they were exempted.
      // Saying so costs a clause; a number somebody finds later costs more.
      sub: owed > 0
        ? <>Nothing accrues and no invoice is raised. The balance above
          predates the exemption.</>
        : <>Nothing accrues on this account and no invoice is raised.</>,
    }
  }

  // Negative outstanding IS credit; credit_paise carries it positive.
  if (b.outstanding_paise < 0) {
    return {
      eyebrow: "In credit",
      amount: b.credit_paise,
      alarm: false,
      action: "advance",
      sub: b.credit_lasts_until
        ? <>Covers the daily charge until about {dayLabel(b.credit_lasts_until)}.</>
        // Null when nothing is accruing against it. A projected date there
        // would be invented, so say what is true instead.
        : <>Nothing is drawing it down, so it has no end date yet.</>,
    }
  }

  if (b.open_invoice) {
    const inv = b.open_invoice
    const madeOf = <>
      {monthLabel(inv.month)} invoice {inr(inv.paise)}
      {mtd > 0 && <> · {inr(mtd)} accrued since</>}
    </>
    if (b.stage === "locked") {
      return {
        eyebrow: "Outstanding",
        amount: owed,
        alarm: true,
        action: "due",
        sub: <>{madeOf}. {plural(b.days_overdue, "day")} past due, and the
          dashboard stays locked until it is paid.</>,
      }
    }
    return {
      eyebrow: "Outstanding",
      amount: owed,
      alarm: false,
      action: "due",
      sub: <>{madeOf}.</>,
    }
  }

  if (mtd > 0) {
    return {
      eyebrow: "This month so far",
      amount: mtd,
      alarm: false,
      action: "advance",
      sub: <>
        {b.month_label} · {plural(counted, "day")} counted. Invoiced when the
        month closes.
        {/* Normally owed equals mtd: no open invoice means nothing older is
            outstanding. When they differ the balance is the number that
            matters, and burying it would be the dishonest choice. */}
        {owed !== mtd && (owed > 0
          ? <> Balance {inr(owed)}.</>
          : <> The balance is settled.</>)}
      </>,
    }
  }

  return {
    eyebrow: "Nothing due",
    amount: 0,
    alarm: false,
    action: "advance",
    sub: counted > 0
      ? <>{b.month_label} · {plural(counted, "day")} counted, and the balance
        is settled.</>
      : <>Nothing has accrued this month and nothing is outstanding.</>,
  }
}

function PayAction({ b, action, onPay }: {
  b: BillingInfo
  action: Action
  onPay: () => void
}) {
  const { canWrite } = useAuth()
  if (action === "none" || !canWrite) return null

  // Dormant gateway: an honest sentence, never a button that 503s. The same
  // words the route answers with, so the two cannot tell different stories.
  if (!b.payment.enabled) {
    return (
      <p className="max-w-64 text-xs text-muted-foreground">
        Online payment is not yet enabled.{" "}
        {b.payment.admin_contact
          ? <>Contact <span className="font-medium text-foreground">
            {b.payment.admin_contact}</span>.</>
          : <>Contact your administrator.</>}
      </p>
    )
  }

  return action === "due"
    ? (
      <Button size="lg" onClick={onPay} className="w-full @md:w-auto">
        Pay {inr(Math.max(0, b.outstanding_paise))}
      </Button>
    )
    : (
      <Button size="lg" variant="outline" onClick={onPay}
        className="w-full @md:w-auto"
        title="Paying ahead leaves the balance in credit. The daily charge draws it down.">
        Pay in advance
      </Button>
    )
}

export function BillingHero({ b, onPay }: { b: BillingInfo; onPay: () => void }) {
  const h = heroState(b)
  return (
    <section className="wisp-panel">
      <div className="flex flex-col gap-4 p-4 @md:flex-row @md:items-end @md:justify-between md:p-5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="wisp-eyebrow">{h.eyebrow}</span>
            <StageChip stage={b.stage} daysOverdue={b.days_overdue} />
          </div>
          {/* Fixed box: the skeleton and all seven states land on the same
              baseline, so arriving data never nudges the page. */}
          <div className="mt-1 flex h-11 items-end">
            <span className={cn(
              "text-4xl leading-none font-semibold tracking-tight tabular-nums",
              h.alarm ? "text-destructive" : "text-foreground")}>
              {inr(h.amount)}
            </span>
          </div>
          <p className="mt-2 max-w-prose text-xs text-muted-foreground">{h.sub}</p>
        </div>
        <div className="shrink-0 @md:text-right">
          <PayAction b={b} action={h.action} onPay={onPay} />
        </div>
      </div>
    </section>
  )
}
