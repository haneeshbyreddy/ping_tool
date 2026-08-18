/* Billing v2's three chrome surfaces: the 402 screen, the pre-lock banner and
   the worker's note. All three are mounted by layout/app-shell.tsx and none of
   them is a page — they are what an operator meets on the way to work.

   The one rule they share: a bill is not an alarm. Monitoring, edge ingest and
   paging are never gated by billing (server.py `_billing_blocked` guards
   `/api/*` and nothing else), so every screen here says so in words rather
   than implying an outage with red chrome. The lock is a PAYMENT SCREEN, not a
   wall: a locked owner should be able to land, read one number and pay.

   Money is INTEGER PAISE on the wire and becomes rupees only in lib/billing.ts
   (inr / inrExact / inrAuto). Nothing here divides by 100. */

import { useEffect, useMemo, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import {
  Activity, Check, Copy, CreditCard, FileText, Loader2, Lock, LogOut, Receipt,
  TriangleAlert, X,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { ApiError, billingApi } from "@/lib/api"
import { inr, monthLabel, stageMeta } from "@/lib/billing"
import type { BillingInfo, Invoice } from "@/lib/types"
// ONE checkout loader for the whole SPA. The lock screen and the billing
// page's pay dialog both open the same bundle, and the bundle does not
// survive being evaluated twice: two module-level promises guarding one
// <script> tag is a race waiting for the one session that opens both.
import { loadRazorpay, type RazorpayReturn as CheckoutReturn }
  from "@/components/billing/razorpay"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"

/** metering.BANNER_DAYS. Days 1..3 of an overdue invoice show the banner; the
 *  first 402 is day 4. Mirrored here (not imported) because the SPA has no
 *  channel for server constants; if the ladder ever moves, both sides move. */
const BANNER_DAYS = 3

// ----------------------------------------------------------------- the money

/** What the ladder is actually holding against this org: everything BILLED and
 *  not yet paid.
 *
 *  Deliberately not `outstanding_paise`, which is the whole account
 *  (Σ accruals − Σ payments) and so also carries the current month running up
 *  day by day. That money is not invoiced, is not due, and never locked
 *  anybody: the 402 is anchored to the oldest OPEN INVOICE. Billing a locked
 *  owner for it would be us inventing a demand the ladder never made.
 *
 *  Computed from the server's two totals rather than by summing
 *  `billing.payments`, which the store caps at the newest 200 rows: a sum over
 *  a truncated list is a wrong number that looks exact. The one case it can
 *  overstate is a VOIDED invoice (forgiven by the superadmin, still counted in
 *  the accruals) — such an org is not normally locked, and the invoice links
 *  under the figure name the months that are genuinely open. */
function dueNowPaise(billing: BillingInfo): number {
  return Math.max(0, billing.outstanding_paise - billing.month_to_date_paise)
}

/** Unpaid invoices, oldest first: the oldest is the one the ladder anchors to,
 *  so it is the one a payer should see first. */
function openInvoices(billing: BillingInfo): Invoice[] {
  return billing.invoices
    .filter((i) => i.status === "open")
    .sort((a, b) => (a.month < b.month ? -1 : a.month > b.month ? 1 : 0))
}

/** The gateway's own words when it sent any, else ours. */
function failureText(payload: unknown): string {
  const desc = (payload as { error?: { description?: string } })?.error?.description
  return typeof desc === "string" && desc
    ? `The payment did not go through. ${desc}`
    : "The payment did not go through. Nothing has been charged."
}

// ------------------------------------------------------------- small pieces

/** A number a stressed person has to get into another app. Tappable on the
 *  phone they are probably holding, copyable on the desk they might not be. */
function ContactWell({ contact, children }: {
  contact: string
  children: ReactNode
}) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(contact).then(
      () => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      },
      () => { /* clipboard denied: the number is still on screen and dialable */ },
    )
  }
  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted px-3 py-2.5">
      <p className="text-xs leading-relaxed">{children}</p>
      {!!contact && (
        <Button variant="outline" size="sm" className="w-full" onClick={copy}>
          {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy number"}
        </Button>
      )}
    </div>
  )
}

function ContactNumber({ contact }: { contact: string }) {
  return (
    <a href={`tel:${contact.replace(/[^\d+]/g, "")}`}
      className="font-mono font-medium tracking-wide tabular-nums hover:underline">
      {contact}
    </a>
  )
}

/** The load-bearing sentence. It is literally true (billing gates `/api/*` and
 *  nothing else) and it is the first thing an owner locked out at 2am needs to
 *  know, so it sits directly under the action rather than in the fine print. */
function StillWatching() {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border bg-muted px-3 py-2.5">
      <Activity className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <p className="text-xs leading-relaxed">
        <span className="font-medium">Your network is still being monitored.
          Alerts are still being sent.</span>{" "}
        <span className="text-muted-foreground">Only this dashboard is locked.</span>
      </p>
    </div>
  )
}

// ------------------------------------------------------------- the 402 screen

type PayPhase =
  | "idle"        // nothing started
  | "opening"     // creating the order and fetching the gateway's script
  | "checkout"    // the gateway's window is open over this page
  | "verifying"   // the browser came back; asking central to check the signature
  | "processing"  // verified. The webhook posts the money; the lock lifts itself
  | "unverified"  // we could not confirm it. Money may still have moved

export function BillingLock({ billing, org }: {
  billing: BillingInfo
  org: string | null
}) {
  const { user, logout } = useAuth()
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<PayPhase>("idle")
  const [error, setError] = useState("")
  // Payments can be dormant at first paint (the config says so) or turn out to
  // be dormant on the first click (a 503 from /api/billing/pay). Both land in
  // the same honest sentence; neither leaves a button that cannot work.
  const [dormant, setDormant] = useState(!billing.payment.enabled)
  // The unlock did not arrive on its own. A payment screen that only ever says
  // "catching up" is a dead end for the one person who has already paid.
  const [stalled, setStalled] = useState(false)

  const deactivated = billing.stage === "deactivated"
  const due = dueNowPaise(billing)
  const unpaid = openInvoices(billing)
  const contact = billing.payment.admin_contact.trim()
  const stage = stageMeta(billing.stage, billing.days_overdue)

  // A payment lands in two steps: the browser returns instantly, the webhook
  // posts the money a moment later, and the unlock rides `billing.locked`
  // flipping. The shell already refetches every 60 s and on SSE, but somebody
  // who has just paid is watching the screen — ask again on a short clock
  // instead. Bounded to two minutes, and this component dies with the lock.
  useEffect(() => {
    setStalled(false)
    if (phase !== "processing" && phase !== "unverified") return
    let ticks = 0
    const id = window.setInterval(() => {
      ticks += 1
      if (ticks > 24) {
        window.clearInterval(id)
        setStalled(true)
        return
      }
      void queryClient.invalidateQueries({ queryKey: ["billing"] })
    }, 5_000)
    return () => window.clearInterval(id)
  }, [phase, queryClient])

  const confirm = async (res: CheckoutReturn) => {
    setPhase("verifying")
    try {
      const out = await billingApi.verifyReturn({ ...res, org_id: org })
      if (out.verified) {
        setError("")
        setPhase("processing")
        return
      }
      setError(out.error || "We could not confirm that payment from here.")
      setPhase("unverified")
    } catch (e) {
      // Never tell somebody who has just paid that nothing happened: the
      // browser return is only the fast path and the webhook records the money
      // either way.
      setError(e instanceof ApiError ? e.message
        : "We could not confirm that payment from here. If money left your "
          + "account it will still be recorded.")
      setPhase("unverified")
    }
  }

  const pay = async () => {
    setError("")
    setPhase("opening")
    try {
      const order = await billingApi.pay({ org_id: org, paise: due })
      // Razorpay is the only adapter the SPA can open. A second gateway is a
      // deliberate piece of work on this screen, not something to fail at by
      // handing its order to the wrong checkout script.
      if (order.provider !== "razorpay") {
        throw new Error(`This account pays through ${order.provider}. `
          + "This screen cannot open that gateway yet.")
      }
      const Checkout = await loadRazorpay()
      const rzp = new Checkout({
        key: order.key_id,
        order_id: order.order_id,
        amount: order.amount,
        currency: order.currency,
        description: unpaid.length
          ? `${monthLabel(unpaid[0].month)} invoice`
          : "Account balance",
        prefill: user?.whatsapp_number
          ? { contact: user.whatsapp_number }
          : undefined,
        handler: (res) => { void confirm(res as CheckoutReturn) },
        // Dismissing is not a failure. Any message a failed attempt left
        // stands; the button comes back exactly as it was.
        modal: { ondismiss: () => setPhase("idle") },
      })
      // The gateway keeps its window open after a declined card so the payer
      // can try another one. Record what it said and let ondismiss decide when
      // this screen is in charge again.
      rzp.on?.("payment.failed", (payload) => setError(failureText(payload)))
      setPhase("checkout")
      rzp.open()
    } catch (e) {
      setPhase("idle")
      if (e instanceof ApiError && e.status === 503) {
        setDormant(true)
        return
      }
      setError(e instanceof Error ? e.message
        : "Something went wrong starting the payment. Try again.")
    }
  }

  const busy = phase === "opening" || phase === "checkout" || phase === "verifying"
  const settled = due <= 0

  return (
    <div className="relative flex min-h-svh flex-col items-center justify-center overflow-hidden bg-background px-4 py-10">
      {/* The same soft wash the sign-in screen carries: these are the two
          full-page surfaces in the product and they should read as one place.
          Deliberately NOT a red glow — this is a payment screen, and the
          person reading it has already had the bad news. */}
      <div aria-hidden className="pointer-events-none absolute top-1/2 left-1/2 size-[36rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/5 blur-3xl" />

      <Card className="relative w-full max-w-md">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10">
              <Lock className="size-5 text-destructive" />
            </div>
            <div className="min-w-0">
              <h1 className="text-lg font-semibold tracking-tight">
                {deactivated ? "Account deactivated" : "Dashboard locked"}
              </h1>
              <p className="truncate text-sm text-muted-foreground">
                {billing.org_name}
              </p>
            </div>
          </div>
        </CardHeader>

        <CardContent className="flex flex-col gap-4">
          {!settled && (
            <div className="rounded-lg border bg-muted px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <p className="wisp-eyebrow">Amount due</p>
                <span className={cn(
                  "inline-flex h-5 shrink-0 items-center rounded-md px-2 text-2xs font-medium",
                  stage.className,
                )}>
                  {stage.label}
                </span>
              </div>
              <p className="mt-1 text-4xl leading-none font-semibold tracking-tight tabular-nums">
                {inr(due)}
              </p>
              {unpaid.length > 0 && (
                <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="text-xs text-muted-foreground">
                    {unpaid.length > 1 ? "Unpaid invoices" : "Invoice"}
                  </span>
                  {unpaid.slice(0, 3).map((inv) => (
                    <a
                      key={inv.month}
                      href={billingApi.invoiceUrl(inv.month, org)}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
                    >
                      <FileText className="size-3.5" />
                      {monthLabel(inv.month)}
                    </a>
                  ))}
                  {unpaid.length > 3 && (
                    <span className="text-xs text-muted-foreground">
                      +{unpaid.length - 3} earlier
                    </span>
                  )}
                </div>
              )}
              {/* Only when the two numbers differ, and named precisely: the
                  ledger's total includes this month accruing, which is real
                  money owed but is not what the lock is about. */}
              {billing.outstanding_paise > due && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Account total {inr(billing.outstanding_paise)}, including
                  {" "}{billing.month_label} so far.
                </p>
              )}
            </div>
          )}

          {/* ---- the one action, or the honest sentence in its place ---- */}

          {deactivated ? (
            <ContactWell contact={contact}>
              This account has been deactivated, so paying online will not
              reopen it.{" "}
              {contact
                ? <>Contact <ContactNumber contact={contact} /> to have it restored.</>
                : "Contact your administrator to have it restored."}
            </ContactWell>
          ) : settled ? (
            <div className="flex items-start gap-2.5 rounded-lg border bg-muted px-3 py-2.5">
              <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" />
              <p className="text-xs leading-relaxed">
                <span className="font-medium">Nothing is due on this account.</span>{" "}
                <span className="text-muted-foreground">
                  This screen clears itself in a moment.
                </span>
              </p>
            </div>
          ) : dormant ? (
            <ContactWell contact={contact}>
              Online payment is not yet enabled.{" "}
              {contact
                ? <>Contact <ContactNumber contact={contact} /> to pay.</>
                : "Contact your administrator to pay."}
            </ContactWell>
          ) : phase === "processing" || phase === "unverified" ? (
            <div className={cn(
              "flex items-start gap-2.5 rounded-lg border px-3 py-2.5",
              // Pending is NEUTRAL, never green: the money is not in the
              // ledger until the webhook lands, and a success tone here would
              // be a claim we cannot make yet.
              phase === "processing" ? "bg-muted" : "border-warning/30 bg-warning-soft",
            )}>
              {phase === "processing"
                ? <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" />
                : <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />}
              <div className="flex flex-col gap-1">
                <p className="text-xs leading-relaxed">
                  {phase === "processing" ? (
                    <>
                      <span className="font-medium">Payment received. The ledger
                        is catching up.</span>{" "}
                      <span className="text-muted-foreground">
                        This screen unlocks itself. Nothing to reload.
                      </span>
                    </>
                  ) : (
                    <span className="text-muted-foreground">{error}</span>
                  )}
                </p>
                {phase === "unverified" && !!contact && (
                  <p className="text-2xs text-muted-foreground">
                    Check with <ContactNumber contact={contact} /> before paying
                    again.
                  </p>
                )}
                {stalled && (
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <p className="text-2xs text-muted-foreground">
                      Still locked after a few minutes?
                    </p>
                    <Button variant="outline" size="xs"
                      onClick={() => { setPhase("idle"); setError("") }}>
                      Back to payment
                    </Button>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <Button size="lg" className="h-11 w-full text-sm" disabled={busy}
                onClick={() => { void pay() }}>
                {busy
                  ? <Loader2 className="size-4 animate-spin" />
                  : <CreditCard className="size-4" />}
                {phase === "opening" ? "Opening the payment window…"
                  : phase === "checkout" ? "Finish in the payment window"
                    : phase === "verifying" ? "Confirming your payment…"
                      : `Pay ${inr(due)}`}
              </Button>
              {!!error && (
                <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive-soft px-3 py-2 text-xs text-destructive">
                  {error}
                </p>
              )}
              {!!contact && (
                <p className="text-center text-2xs text-faint-foreground">
                  Trouble paying? Contact <ContactNumber contact={contact} />.
                </p>
              )}
            </div>
          )}

          <StillWatching />

          {/* A locked person may simply be in the wrong account. Never trap
              them here with no way out. */}
          <div className="flex items-center justify-between gap-3 border-t pt-3">
            <span className="min-w-0 truncate text-2xs text-faint-foreground">
              Signed in as {user?.username}
            </span>
            <Button variant="ghost" size="sm" className="shrink-0 text-muted-foreground"
              onClick={() => { void logout() }}>
              <LogOut className="size-3.5" /> Log out
            </Button>
          </div>
        </CardContent>
      </Card>

      <p className="relative mt-6 text-xs text-faint-foreground">
        WISP Central: uptime monitoring for ISPs
      </p>
    </div>
  )
}

// ------------------------------------------------------------- the banner

/** Days 1..3 of an overdue invoice. One line, one click, dismissable for the
 *  day. The tone ramps from info to warning and stops there: destructive is
 *  what the lock is for, and a red band on a NOC screen means the NETWORK. */
export function BillingBanner({ billing, org }: {
  billing: BillingInfo
  org: string | null
}) {
  const invoice = billing.open_invoice
  const days = billing.days_overdue
  const due = dueNowPaise(billing)

  // The operator's day, from the accrual row the server stamps in the display
  // zone (WISP_DISPLAY_TZ) — the browser's own midnight is a different moment
  // and would bring the banner back mid-shift. Falls back to the local date
  // when there is no accrual row (an org whose first day has not been metered
  // yet), which is close enough for a reminder.
  const day = billing.today?.day ?? new Date().toLocaleDateString("en-CA")

  // Keyed on ORG so dismissing one workspace's reminder cannot silence
  // another's (a superadmin switches scope; an owner can hold two accounts),
  // and on the DAY so a dismissal means "not now", never "never". Tomorrow's
  // key does not exist yet, so tomorrow the banner is back.
  const key = `wisp.billing.due.${org ?? "-"}.${day}`
  const [dismissedKey, setDismissedKey] = useState<string | null>(null)
  const dismissed = useMemo(
    // Read synchronously during render, never in an effect: an effect would
    // paint the banner and then remove it, which is the layout shift this is
    // trying to avoid. Recomputes when the key moves (org switch, new day).
    () => dismissedKey === key || readFlag(key),
    [dismissedKey, key],
  )

  // `stage === "banner"` already implies days 1..3, not exempt, not
  // deactivated and an open invoice. The rest is stated anyway: this component
  // renders in the shell of every page, and the one thing it must never do is
  // dun an org that is square with us.
  const show = !!invoice && billing.stage === "banner"
    && !billing.exempt && !billing.deactivated
    && days >= 1 && days <= BANNER_DAYS
    && billing.credit_paise <= 0 && due > 0
  if (!show || dismissed) return null

  const age = `${days} day${days === 1 ? "" : "s"} overdue`
  // The consequence, counted down rather than restated: day 1 has three days
  // of room, day 3 has one. This is the ramp, and it is the sentence a person
  // acts on, so it is never shortened away.
  const untilLock = BANNER_DAYS + 1 - days
  const when = untilLock <= 1 ? "tomorrow" : `in ${untilLock} days`
  const loud = days >= BANNER_DAYS

  const dismiss = () => {
    writeFlag(key, day)
    setDismissedKey(key)
  }

  return (
    <div
      role="status"
      className={cn(
        // A FIXED height and a single line: the chrome must be the same size
        // whatever the org is called and however long the amount is.
        "flex h-9 shrink-0 items-center gap-2.5 overflow-hidden border-b px-3 md:px-5",
        loud ? "bg-warning-soft" : "bg-primary-soft",
      )}
    >
      {loud
        ? <TriangleAlert className="size-4 shrink-0 text-warning" />
        : <Receipt className="size-4 shrink-0 text-primary" />}
      {/* Facts first, explanation second: the line truncates from the right on
          a narrow window, so the amount and the age survive and the sentence
          that can be inferred is the part that goes. */}
      <p className="min-w-0 flex-1 truncate text-xs">
        <span className="font-medium">
          {inr(due)} due · {monthLabel(invoice.month)} · {age}
        </span>
        <span className="text-muted-foreground">
          {" "}The dashboard locks {when} if it is not paid.
        </span>
      </p>
      <Button asChild variant="outline" size="xs" className="shrink-0">
        <Link to="/billing">Pay</Link>
      </Button>
      <button
        aria-label="Dismiss until tomorrow"
        title="Dismiss until tomorrow"
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
        onClick={dismiss}
      >
        <X className="size-3.5" />
      </button>
    </div>
  )
}

// ------------------------------------------------------------ the worker's note

/** What somebody who is not paying the bill is told: a worker whose dashboard
 *  is limited, or a superadmin scoped into a locked org. No amount, no month,
 *  no pay action, and nothing addressed to a particular reader, because both
 *  see this one line. Workers never see billing at all (`/api/billing` is off
 *  the server's worker allow-list) and the book is the largest PII surface in
 *  the product.
 *
 *  Calm on purpose. The band is neutral because a red bar on a NOC screen is a
 *  claim about the NETWORK, and neither reader is being asked to do anything
 *  this second. The lock icon carries the weight. */
export function LockedBand({ lead, rest, action }: {
  lead: string
  rest: string
  action?: ReactNode
}) {
  return (
    <div role="status"
      className="flex h-9 shrink-0 items-center gap-2.5 overflow-hidden border-b bg-muted px-3 md:px-5">
      <Lock className="size-4 shrink-0 text-muted-foreground" />
      <p className="min-w-0 flex-1 truncate text-xs">
        <span className="font-medium">{lead}</span>
        <span className="text-muted-foreground"> {rest}</span>
      </p>
      {action}
    </div>
  )
}

export function BillingLockedNote({ billing }: { billing: BillingInfo }) {
  if (!billing.locked) return null
  // DEACTIVATED and merely LOCKED are not the same claim about the network.
  // An overdue org is still fully monitored, which is the invariant worth
  // stating out loud; a deactivated one has had its probes stood down, and
  // repeating the reassurance there would be a lie on a NOC screen.
  return billing.stage === "deactivated"
    ? <LockedBand
        lead="This account is deactivated."
        rest="Its probes are stood down and nothing is being monitored. It has to be restored by the operator." />
    : <LockedBand
        lead="This account is locked."
        rest="The bill needs settling before the dashboard comes back. Monitoring and alerts keep running." />
}

// --------------------------------------------------------------- the storage

/** localStorage is unavailable in a locked-down browser and throws rather than
 *  returning null. A reminder nobody can dismiss beats a dashboard that will
 *  not render, in both directions. */
function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1"
  } catch {
    return false
  }
}

function writeFlag(key: string, day: string): void {
  try {
    localStorage.setItem(key, "1")
    // Sweep the days that have passed as we write today's. One key per org per
    // day would otherwise accumulate forever, including the v1 keys this file
    // replaces. Only runs on a dismissal, at most once a day.
    for (let i = localStorage.length - 1; i >= 0; i -= 1) {
      const k = localStorage.key(i)
      if (!k) continue
      const ours = k.startsWith("wisp.billing.due.")
      const legacy = k.startsWith("wisp-billing-dismiss-")
      if ((ours && !k.endsWith(`.${day}`)) || legacy) localStorage.removeItem(k)
    }
  } catch {
    /* nothing to clean up, and nothing worth breaking a render over */
  }
}
