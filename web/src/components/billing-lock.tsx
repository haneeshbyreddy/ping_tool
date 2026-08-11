import { useState } from "react"
import { Check, Copy, Lock, LogOut, TriangleAlert, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { inr, monthLabel } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"
import { FreePlanButton, IvePaidButton } from "@/components/pay-online"
import { QrImage } from "@/components/qr-image"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"

export function GpayWell({ billing }: { billing: BillingInfo }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(billing.gpay_number)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex flex-col gap-3 rounded-lg border bg-muted px-4 py-3">
      {billing.qr_image && (
        <div className="flex flex-col items-center gap-1.5">
          <QrImage src={billing.qr_image} className="block w-full"
            imgClassName="w-full rounded-lg border p-2" />
          <p className="text-2xs text-muted-foreground">Tap the code to enlarge · scan with any UPI app</p>
        </div>
      )}
      <div>
        <p className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
          {billing.qr_image ? "Or pay via GPay" : "Pay via GPay"}
        </p>
        <div className="mt-1 flex items-center justify-between gap-3">
          <span className="font-mono text-xl font-semibold tracking-wider tabular-nums">
            {billing.gpay_number}
          </span>
          <Button variant="outline" size="sm" onClick={copy}>
            {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      </div>
    </div>
  )
}

export function BillingLock({ billing, org }: { billing: BillingInfo; org?: string | null }) {
  const { user, logout } = useAuth()
  const spec = billing.plans[billing.plan]
  const dueMonth = billing.due_month ?? billing.current_month

  return (
    <div className="relative flex min-h-svh flex-col items-center justify-center overflow-hidden bg-background px-4 py-10">
      <div aria-hidden className="pointer-events-none absolute top-1/2 left-1/2 size-[36rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-destructive/5 blur-3xl" />
      <Card className="relative w-full max-w-md">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10">
              <Lock className="size-5 text-destructive" />
            </div>
            <div>
              <h1 className="text-lg font-semibold tracking-tight">Renew to continue</h1>
              <p className="text-sm text-muted-foreground">
                {monthLabel(dueMonth)} renewal
              </p>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center justify-between rounded-lg border px-4 py-3">
            <div>
              <p className="text-xs text-muted-foreground">{spec.label} plan</p>
              <p className="text-sm font-medium">{monthLabel(dueMonth)}</p>
            </div>
            <p className="text-xl font-semibold tabular-nums">
              {inr(spec.price_inr)}
              <span className="ml-1 text-xs font-normal text-muted-foreground">/month</span>
            </p>
          </div>
          <GpayWell billing={billing} />
          <IvePaidButton org={org} size="lg" className="w-full"
            label={`I've paid ${inr(spec.price_inr)}`} />
          <p className="text-sm text-muted-foreground">
            Paid already? Tap the button and the admin extends your account in a
            moment. This page unlocks itself, nothing to refresh.
          </p>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">
              Not now? Free keeps up to 5 devices monitored.
            </p>
            <FreePlanButton label="Go Free" className="shrink-0" />
          </div>
          <p className="rounded-lg border bg-muted px-3 py-2 text-xs text-muted-foreground">
            Your network is still being watched: probes, monitoring and outage alerts
            keep running while the dashboard is locked.
          </p>
          <div className="flex items-center justify-between border-t pt-3">
            <span className="text-xs text-faint-foreground">
              Signed in as {user?.username}
            </span>
            <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={logout}>
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

export function BillingBanner({ billing, org }: { billing: BillingInfo; org: string }) {
  const dueMonth = billing.due_month ?? ""
  const dismissKey = `wisp-billing-dismiss-${org}-${dueMonth}`
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(dismissKey) === "1")
  if (billing.status !== "due_soon" || dismissed) return null
  const spec = billing.plans[billing.plan]
  const days = billing.days_left ?? 0
  return (
    <div className="flex items-center gap-2.5 border-b bg-warning-soft px-3 py-2 md:px-5">
      <TriangleAlert className="size-4 shrink-0 text-warning" />
      <p className="min-w-0 flex-1 truncate text-xs text-foreground">
        <span className="font-medium">
          {spec.label} renews {days === 0 ? "today" : `in ${days} day${days === 1 ? "" : "s"}`}
        </span>
        <span className="text-muted-foreground">
          : send {inr(spec.price_inr)} for {monthLabel(dueMonth)} via GPay to{" "}
        </span>
        <span className="font-mono font-medium tabular-nums">{billing.gpay_number}</span>
        <span className="text-muted-foreground">{" "}to stay covered.</span>
      </p>
      <IvePaidButton org={org} label="I've paid" variant="outline" size="sm"
        className="h-7 shrink-0 px-2.5 text-xs" />
      <button
        aria-label="Dismiss payment reminder"
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
        onClick={() => { localStorage.setItem(dismissKey, "1"); setDismissed(true) }}
      >
        <X className="size-3.5" />
      </button>
    </div>
  )
}

export function BillingLockedNote({ billing }: { billing: BillingInfo }) {
  if (!billing.locked) return null
  return (
    <div className="flex items-center gap-2.5 border-b bg-destructive/10 px-3 py-2 md:px-5">
      <Lock className="size-4 shrink-0 text-destructive" />
      <p className="min-w-0 flex-1 truncate text-xs">
        <span className="font-medium">This org is locked</span>
        <span className="text-muted-foreground">
          : {monthLabel(billing.due_month ?? billing.current_month)} unpaid. Its users see
          the paywall; mark the month paid from Organizations → Billing to unlock.
        </span>
      </p>
    </div>
  )
}
