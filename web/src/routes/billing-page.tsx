// BILLING — the owner's account page for the metered postpaid ledger.
//
// Reading order is the operator's question order: what do I owe and how do I
// pay it (hero) · what am I being charged right now (meter) · how has that
// moved (chart) · why is it that number (formula) · the record (invoices,
// payments). There is no input panel: nothing an owner can type moves a
// metered bill.
//
// Two house rules run through the whole surface. Every figure is INTEGER PAISE
// on the wire and becomes rupees only at display, through lib/billing.ts —
// nothing here divides by 100. And no number is re-derived: the daily rows,
// the invoice totals and the outstanding balance are the server's, printed
// verbatim, because an invoice is the SUM OF ITS STORED DAYS and never
// recomputed. A client-side total that disagreed by one paise would cost more
// trust than the page buys.
//
// It shares the ["billing", org] query key with the app shell, which polls the
// same document for the lock gate — one query, two readers, so the banner and
// this page cannot disagree about what is owed. Note that "billing" is NOT in
// the event stream's LIVE_QUERY_KEYS, so this poll and the pay dialog's own
// are the only things that move it; a webhook landing does not push.
import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "@/hooks/use-auth"
import { ApiError, billingApi } from "@/lib/api"
import { NeedsOrg } from "@/components/needs-org"
import { Skeleton } from "@/components/ui/skeleton"
import { BillingHero } from "@/components/billing/hero"
import { FormulaCard } from "@/components/billing/formula"
import { InvoicesPanel, PaymentsPanel } from "@/components/billing/ledger"
import { TodayMeter } from "@/components/billing/meter"
import { MonthChart } from "@/components/billing/month-chart"
import { PayDialog } from "@/components/billing/pay-dialog"
import { Panel } from "@/components/billing/shared"

const PAIR = "grid items-stretch gap-4 @3xl:grid-cols-2"

/** The loaded page's own frame with skeleton contents, rather than a spinner
 *  or a shorter placeholder: the panels, their headers and every row height
 *  are already correct, so arriving data fills the page instead of moving it. */
function Loading() {
  return (
    <>
      <section className="wisp-panel">
        <div className="flex flex-col gap-4 p-4 @md:flex-row @md:items-end @md:justify-between md:p-5">
          <div className="min-w-0 flex-1">
            <Skeleton className="h-3 w-24" />
            <div className="mt-1 flex h-11 items-end">
              <Skeleton className="h-9 w-40" />
            </div>
            <Skeleton className="mt-2 h-3 w-64" />
          </div>
          <Skeleton className="h-9 w-32 shrink-0" />
        </div>
      </section>

      <Panel title="Today">
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-3 @2xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <div key={i}>
              <Skeleton className="h-3 w-16" />
              <div className="mt-1 flex h-6 items-center">
                <Skeleton className="h-4 w-20" />
              </div>
              <div className="mt-0.5 h-4"><Skeleton className="h-3 w-28" /></div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="This month">
        <div className="grid gap-4 px-4 pt-3 pb-4 @3xl:grid-cols-2">
          <Skeleton className="h-[150px] w-full" />
          <Skeleton className="h-[150px] w-full" />
        </div>
      </Panel>

      <Panel title="How this is computed">
        <div className="flex-1 px-4 py-3"><Skeleton className="h-32 w-full" /></div>
      </Panel>

      <div className={PAIR}>
        <Panel title="Invoices">
          <div className="px-4 py-3"><Skeleton className="h-24 w-full" /></div>
        </Panel>
        <Panel title="Payments">
          <div className="px-4 py-3"><Skeleton className="h-24 w-full" /></div>
        </Panel>
      </div>
    </>
  )
}

export default function BillingPage() {
  const { scopeOrg } = useAuth()
  const [paying, setPaying] = useState(false)
  // Deliberately no useNow(): every stamp on this page is ABSOLUTE ("3 Aug,
  // 14:20"), because a ledger entry is a record rather than a reading. Nothing
  // here decays with the clock, so nothing needs a tick to stay true.

  const query = useQuery({
    queryKey: ["billing", scopeOrg],
    queryFn: () => billingApi.get(scopeOrg),
    enabled: !!scopeOrg,
    // Matches the app shell's poll on the same key. Anything faster would be
    // two observers arguing over one query.
    refetchInterval: 60_000,
  })

  if (!scopeOrg) return <NeedsOrg />
  const b = query.data

  return (
    <div className="wisp-page wisp-page--narrow @container flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Billing</h1>
        {/* Named because a superadmin reads this page scoped into somebody
            else's account, and an unlabelled balance is the wrong one to act
            on. The h1 sets the row height, so its absence while loading moves
            nothing. */}
        <span className="text-xs text-faint-foreground">{b?.org_name}</span>
      </div>

      {query.isLoading && <Loading />}

      {query.isError && (
        <div className="wisp-panel">
          <p className="px-4 py-8 text-center text-xs text-destructive">
            {query.error instanceof ApiError
              ? query.error.message
              : "Could not load billing for this account."}
          </p>
        </div>
      )}

      {b && (
        <>
          <BillingHero b={b} onPay={() => setPaying(true)} />
          <TodayMeter b={b} />
          <MonthChart b={b} />
          <FormulaCard b={b} />
          <div className={PAIR}>
            <InvoicesPanel b={b} org={scopeOrg} />
            <PaymentsPanel b={b} />
          </div>
          {/* Mounted only while it is open, so the flow starts from a clean
              state machine every time and a stale "processing" can never be
              re-shown for a payment that already settled. */}
          {paying && (
            <PayDialog b={b} org={scopeOrg} onClose={() => setPaying(false)} />
          )}
        </>
      )}
    </div>
  )
}
