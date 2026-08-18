// THE PAYMENT FLOW, and the three results it is allowed to claim.
//
// The rule the whole dialog is built around: A VERIFIED RETURN IS NOT "PAID".
// The return signature covers the order and payment ids but not the AMOUNT, so
// the server refuses to post a ledger row from it (api/billing.py:
// payment_return) and the WEBHOOK is what records the money. This screen
// therefore says "received, the ledger is catching up" and then WATCHES the
// refetched document for a payments row carrying its own payment id. Only then
// does it say the payment is recorded, and it shows the balance the server
// computed rather than one it worked out itself.
//
// A failure to VERIFY is not a failure to PAY, and the two are separate states
// for that reason. The unverified state keeps watching too, so an honest "we
// cannot tell yet" resolves itself into certainty the moment the webhook lands.
import { useEffect, useState } from "react"
import type { ReactNode } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { Check, Clock, CircleAlert, IndianRupee, Loader2, TriangleAlert } from "lucide-react"
import { ApiError, billingApi } from "@/lib/api"
import { useNow } from "@/hooks/use-now"
import { inr } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import { brandColor, loadRazorpay } from "./razorpay"
import { MAX_PAY_PAISE, rupeeInput, toPaise } from "./shared"

type Phase =
  | { k: "form" }
  | { k: "opening" }
  /** The gateway's own modal owns the screen; ours steps out of its way. */
  | { k: "checkout" }
  | { k: "verifying" }
  | { k: "processing"; paymentId: string; since: number }
  | { k: "unverified"; paymentId: string; detail: string; since: number }
  | { k: "settled"; paymentId: string }
  | { k: "failed"; title: string; detail: string }
  | { k: "dormant"; detail: string }

const UNVERIFIED_FALLBACK =
  "We could not verify that payment from here. If money left your account it "
  + "is still recorded when the gateway confirms it."

/** Slow is 60 s: past that the copy stops implying "any second now" without
 *  ever implying something went wrong. Nothing here can time out into failure. */
const SLOW_MS = 60_000

function dormantSentence(b: BillingInfo): string {
  return "Online payment is not yet enabled. "
    + (b.payment.admin_contact
      ? `Contact ${b.payment.admin_contact}.`
      : "Contact your administrator.")
}

function Result({ tone, icon, title, children, mono }: {
  tone: "neutral" | "good" | "warn" | "bad"
  icon: ReactNode
  title: string
  children: ReactNode
  mono?: string
}) {
  const ring = {
    neutral: "bg-foreground/[0.06] text-muted-foreground",
    good: "bg-success-soft text-success",
    warn: "bg-warning-soft text-warning",
    bad: "bg-destructive-soft text-destructive",
  }[tone]
  return (
    <div className="flex flex-col items-center gap-2 px-2 py-3 text-center">
      <span className={cn(
        "flex size-9 items-center justify-center rounded-full", ring)}>
        {icon}
      </span>
      <p className="text-sm font-semibold text-foreground">{title}</p>
      <div className="max-w-72 text-xs text-balance text-muted-foreground">
        {children}
      </div>
      {mono && (
        <p className="font-mono text-2xs break-all text-faint-foreground">{mono}</p>
      )}
    </div>
  )
}

export function PayDialog({ b, org, onClose }: {
  b: BillingInfo
  org: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const now = useNow(5000)
  const owed = Math.max(0, b.outstanding_paise)
  // Seeded once, deliberately: a background refetch that moved the outstanding
  // balance must never rewrite an amount somebody is halfway through typing.
  const [amount, setAmount] = useState(() =>
    owed > 0 ? rupeeInput(owed)
      : b.month_to_date_paise > 0 ? rupeeInput(b.month_to_date_paise) : "")
  const [phase, setPhase] = useState<Phase>(() => b.payment.enabled
    ? { k: "form" }
    : { k: "dormant", detail: dormantSentence(b) })

  // Both open-ended states resolve the same way: the ledger row is the proof.
  // The poll below is what fetches it — "billing" is NOT in the event stream's
  // LIVE_QUERY_KEYS (hooks/use-event-stream.ts), so the only other thing that
  // would move this document is the page's own 60 s refetch, which is a long
  // time to sit in front of a payment you just made.
  const waiting = phase.k === "processing" || phase.k === "unverified"
    ? phase : null

  useEffect(() => {
    if (!waiting) return
    if (b.payments.some((p) => p.provider_payment_id === waiting.paymentId)) {
      setPhase({ k: "settled", paymentId: waiting.paymentId })
    }
  }, [waiting, b.payments])

  useEffect(() => {
    if (!waiting) return
    const tick = setInterval(
      () => qc.invalidateQueries({ queryKey: ["billing"] }), 5000)
    // Bounded: a webhook that has not landed in three minutes is not landing
    // in the next second either, and this must not become a permanent poll on
    // a tab somebody leaves open.
    const stop = setTimeout(() => clearInterval(tick), 180_000)
    return () => { clearInterval(tick); clearTimeout(stop) }
  }, [waiting, qc])

  const paise = toPaise(amount)
  // Mirrors what the route refuses (api/billing.py: pay). A 422 the field
  // could have caught reads as a broken button on the one screen where a
  // broken button costs the most trust.
  const invalid = paise == null
    ? "That is not an amount we can read."
    : paise <= 0
      ? "Enter an amount above zero."
      : paise > MAX_PAY_PAISE
        ? "That is more than the gateway takes in one payment. Pay it in parts."
        : null
  // Nothing is wrong with an empty field somebody has not filled in yet.
  const problem = amount.trim() === "" ? null : invalid

  const start = async () => {
    if (invalid != null || paise == null) return
    setPhase({ k: "opening" })

    let order
    try {
      order = await billingApi.pay({ org_id: org, paise })
    } catch (e) {
      // The route answers 503 while the gateway is unconfigured. That is not a
      // failure, it is a state, and it gets the same sentence the hero uses.
      if (e instanceof ApiError && e.status === 503) {
        setPhase({ k: "dormant", detail: e.message || dormantSentence(b) })
        return
      }
      setPhase({
        k: "failed",
        title: "Could not start the payment",
        detail: (e instanceof ApiError ? e.message : "The server did not answer.")
          + " Nothing was collected.",
      })
      return
    }

    let Ctor
    try {
      Ctor = await loadRazorpay()
    } catch (e) {
      setPhase({
        k: "failed",
        title: "Could not open the payment window",
        detail: `${e instanceof Error ? e.message : "It could not be loaded"}.`
          + " Nothing was collected.",
      })
      return
    }

    const rz = new Ctor({
      key: order.key_id,
      order_id: order.order_id,
      amount: order.amount,
      currency: order.currency,
      // `name` is left to the gateway so the merchant's own registered name
      // appears. Anything we typed here could disagree with the bank entry.
      description: b.org_name,
      theme: { color: brandColor() },
      // The gateway's built-in retry keeps its modal open after a decline,
      // which would fire payment.failed while the payer is still trying. One
      // retry path, and it is ours.
      retry: { enabled: false },
      handler: (r) => {
        setPhase({ k: "verifying" })
        billingApi.verifyReturn({
          org_id: org,
          razorpay_order_id: r.razorpay_order_id,
          razorpay_payment_id: r.razorpay_payment_id,
          razorpay_signature: r.razorpay_signature,
        })
          .then((res) => {
            qc.invalidateQueries({ queryKey: ["billing"] })
            setPhase(res.verified
              ? {
                k: "processing", paymentId: r.razorpay_payment_id,
                since: Date.now(),
              }
              : {
                k: "unverified", paymentId: r.razorpay_payment_id,
                detail: res.error || UNVERIFIED_FALLBACK, since: Date.now(),
              })
          })
          .catch(() => setPhase({
            k: "unverified", paymentId: r.razorpay_payment_id,
            detail: UNVERIFIED_FALLBACK, since: Date.now(),
          }))
      },
      modal: {
        ondismiss: () => setPhase((p) =>
          p.k === "checkout" ? { k: "form" } : p),
      },
    })
    // Optional-called: a build without `on` must not throw here and strand the
    // payer on an "opening" screen. Losing the decline detail is survivable —
    // the gateway closes its modal and ondismiss puts the form back.
    rz.on?.("payment.failed", (e) => setPhase((p) => p.k === "checkout"
      ? {
        k: "failed",
        title: "Payment failed",
        detail: `${e.error?.description || e.error?.reason
          || "The bank did not complete it."} Nothing was collected.`,
      }
      : p))

    setPhase({ k: "checkout" })
    rz.open()
  }

  const slow = waiting != null && now - waiting.since > SLOW_MS

  const body = () => {
    switch (phase.k) {
      case "dormant":
        return (
          <Result tone="neutral" icon={<CircleAlert className="size-4" />}
            title="Not available yet">
            {phase.detail}
          </Result>
        )
      case "opening":
      case "verifying":
        return (
          <Result tone="neutral"
            icon={<Loader2 className="size-4 animate-spin" />}
            title={phase.k === "opening"
              ? "Opening the payment window"
              : "Checking the payment"}>
            {phase.k === "opening"
              ? "One moment."
              : "Confirming the return with the server."}
          </Result>
        )
      case "checkout":
        return null
      case "processing":
      case "unverified":
        return (
          <Result
            tone={phase.k === "processing" ? "neutral" : "warn"}
            icon={phase.k === "processing"
              ? <Clock className="size-4" />
              : <TriangleAlert className="size-4" />}
            title={phase.k === "processing"
              ? "Payment received"
              : "Could not confirm it from here"}
            mono={phase.paymentId}>
            {phase.k === "processing"
              ? <>
                The ledger is catching up. This lands in Payments as soon as
                the gateway confirms it, and the balance moves with it.
                {slow && <> Still catching up. You can close this, nothing is
                  lost and the row appears on its own.</>}
              </>
              : <>
                {phase.detail} This page is watching for it.
                {slow && <> Nothing yet. Check Payments in a few minutes.</>}
              </>}
          </Result>
        )
      case "settled":
        return (
          <Result tone="good" icon={<Check className="size-4" />}
            title="Payment recorded" mono={phase.paymentId}>
            {b.outstanding_paise > 0
              ? <>{inr(b.outstanding_paise)} is still outstanding.</>
              : b.credit_paise > 0
                ? <>Nothing is due. {inr(b.credit_paise)} sits in credit and
                  draws down against the daily charge.</>
                : <>Nothing is due.</>}
          </Result>
        )
      case "failed":
        return (
          <Result tone="bad" icon={<CircleAlert className="size-4" />}
            title={phase.title}>
            {phase.detail}
          </Result>
        )
      default:
        return (
          <div className="flex flex-col gap-2.5">
            <label htmlFor="wisp-pay-amount" className="wisp-eyebrow block">
              Amount
            </label>
            <div className="relative">
              <IndianRupee className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint-foreground" />
              <Input id="wisp-pay-amount" autoFocus inputMode="decimal"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !invalid) start() }}
                aria-invalid={problem != null}
                className={cn("h-10 pl-7 font-mono text-base tabular-nums",
                  problem && "border-destructive")} />
            </div>
            <p className={cn("text-2xs",
              problem ? "text-destructive" : "text-muted-foreground")}>
              {problem ?? note(owed, paise)}
            </p>
          </div>
        )
    }
  }

  const footer = () => {
    switch (phase.k) {
      case "form":
        return (
          <>
            <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button size="sm" disabled={invalid != null} onClick={start}>
              {paise != null && paise > 0 ? `Pay ${inr(paise)}` : "Pay"}
            </Button>
          </>
        )
      case "failed":
        return (
          <>
            <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
            <Button size="sm" onClick={() => setPhase({ k: "form" })}>
              Try again
            </Button>
          </>
        )
      case "opening":
      case "verifying":
      case "checkout":
        return null
      default:
        return <Button size="sm" onClick={onClose}>Close</Button>
    }
  }

  const f = footer()
  // Escape and the overlay are refused mid-transaction: dismissing while an
  // order is being created or a return verified would leave the gateway's
  // window on screen with nothing behind it to report the result to.
  const dismissible = phase.k !== "opening" && phase.k !== "verifying"
    && phase.k !== "checkout"
  return (
    // While the gateway's modal is up ours steps out: a Radix modal marks the
    // rest of the document inert, and the checkout iframe lives in that rest.
    // Leaving it mounted makes the payment window unclickable.
    <Dialog open={phase.k !== "checkout"}
      onOpenChange={(v) => { if (!v && dismissible) onClose() }}>
      <DialogContent className="sm:max-w-md" showCloseButton={dismissible}>
        <DialogHeader>
          <DialogTitle>Pay online</DialogTitle>
          <DialogDescription>
            {phase.k === "form"
              ? <>
                {owed > 0
                  ? <>{inr(owed)} outstanding.</>
                  : <>Nothing is outstanding.</>}
                {" "}Pay less, or pay ahead. The amount is yours.
              </>
              : <>{b.org_name}</>}
          </DialogDescription>
        </DialogHeader>
        {body()}
        {f && <DialogFooter className="gap-2">{f}</DialogFooter>}
      </DialogContent>
    </Dialog>
  )
}

/** What this amount does to the balance, said plainly before it is charged. */
function note(owed: number, paise: number | null) {
  if (paise == null || paise <= 0) {
    return owed > 0
      ? "Anything less leaves the rest outstanding."
      : "Paying ahead leaves the balance in credit."
  }
  if (paise < owed) return `${inr(owed - paise)} stays outstanding.`
  if (paise === owed && owed > 0) return "Settles the balance in full."
  return `${inr(paise - owed)} goes to credit and draws down`
    + " against the daily charge."
}
