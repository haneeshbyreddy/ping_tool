import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Check, Copy, CreditCard } from "lucide-react"
import { cn } from "@/lib/utils"
import { billingApi } from "@/lib/api"
import {
  PLAN_ORDER, addMonths, billingStatusMeta, currentMonthKey, inr, monthLabel, monthShort,
} from "@/lib/billing"
import type { BillingInfo, Plan } from "@/lib/types"
import { FreePlanButton, IvePaidButton } from "@/components/pay-online"
import { QrImage } from "@/components/qr-image"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

function PlanTier({ plan, billing, org }: { plan: Plan; billing: BillingInfo; org: string }) {
  const spec = billing.plans[plan]
  const current = billing.plan === plan
  const action = current || plan !== "free" ? null
    : <FreePlanButton org={org} className="mt-auto w-full" />
  return (
    <div className={cn(
      "flex flex-col gap-2 rounded-lg border p-4",
      current ? "border-primary/50 bg-primary-soft" : "bg-card",
    )}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">{spec.label}</span>
        {current && (
          <span className="rounded-4xl bg-primary/15 px-2 py-0.5 text-2xs font-medium text-primary">
            Current
          </span>
        )}
      </div>
      <p className="text-lg font-semibold tabular-nums">
        {spec.price_inr === 0 ? "₹0" : inr(spec.price_inr)}
        <span className="ml-1 text-xs font-normal text-muted-foreground">/month</span>
      </p>
      <ul className="flex flex-col gap-1.5">
        {spec.features.map((f) => (
          <li key={f} className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <Check className="mt-0.5 size-3 shrink-0 text-success" />
            {f}
          </li>
        ))}
      </ul>
      {action}
    </div>
  )
}

function MonthStrip({ billing }: { billing: BillingInfo }) {
  const now = currentMonthKey()
  const months = Array.from({ length: 12 }, (_, i) => addMonths(now, i - 2))
  const paid = new Set(billing.paid_months)
  return (
    <div className="flex flex-wrap gap-1.5">
      {months.map((m) => (
        <span key={m}
          title={`${monthLabel(m)}${paid.has(m) ? " (paid)" : ""}`}
          className={cn(
            "rounded-md border px-2 py-1 text-2xs font-medium tabular-nums",
            paid.has(m)
              ? "border-success/30 bg-success-soft text-success"
              : m < now ? "text-faint-foreground" : "text-muted-foreground",
            m === now && "ring-1 ring-ring",
          )}>
          {monthShort(m)} {m.slice(2, 4)}
        </span>
      ))}
    </div>
  )
}

function PayWell({ billing, org, note, showPaid }: {
  billing: BillingInfo; org: string; note: string; showPaid: boolean
}) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(billing.gpay_number)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex flex-col gap-2.5 rounded-lg border bg-muted px-4 py-3">
      <div className="flex items-center gap-4">
        {billing.qr_image && (
          <QrImage src={billing.qr_image} className="shrink-0"
            imgClassName="size-28 rounded-md border object-contain p-1.5" />
        )}
        <div className="flex flex-1 items-center justify-between gap-3">
          <div>
            <p className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
              {billing.qr_image ? "Scan or pay via GPay" : "Pay via GPay"}
            </p>
            <p className="font-mono text-base font-semibold tracking-wider tabular-nums">
              {billing.gpay_number}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={copy}>
            {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{note}</p>
      {showPaid && <IvePaidButton org={org} className="w-fit" />}
    </div>
  )
}

export function BillingCard({ org }: { org: string }) {
  const { data: billing, isLoading } = useQuery({
    queryKey: ["billing", org],
    queryFn: () => billingApi.get(org),
    enabled: !!org,
  })

  if (isLoading) return <Skeleton className="h-64 w-full" />
  if (!billing) return null

  const meta = billingStatusMeta(billing.status)
  const spec = billing.plans[billing.plan]
  const capPct = billing.device_cap
    ? Math.min(100, (billing.device_count / billing.device_cap) * 100)
    : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <CreditCard className="size-4 text-muted-foreground" /> Plan &amp; billing
          <span className={cn("ml-auto rounded-4xl px-2 py-0.5 text-2xs font-medium", meta.className)}>
            {meta.label}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          {PLAN_ORDER.map((p) => <PlanTier key={p} plan={p} billing={billing} org={org} />)}
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-baseline justify-between text-xs">
            <span className="text-muted-foreground">Monitored devices</span>
            <span className="font-medium tabular-nums">
              {billing.device_count}{billing.device_cap != null ? ` of ${billing.device_cap}` : " · unlimited"}
            </span>
          </div>
          {capPct != null && (
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className={cn("h-full rounded-full",
                capPct >= 100 ? "bg-destructive" : capPct >= 85 ? "bg-warning" : "bg-primary/60")}
                style={{ width: `${Math.max(capPct, 2)}%` }} />
            </div>
          )}
          {capPct != null && capPct >= 100 && (
            <p className="text-xs text-muted-foreground">
              Device limit reached. Adding more needs {billing.plan === "free" ? "Pro or VIP" : "VIP"}.
              Passive plant never counts.
            </p>
          )}
          <div className="flex items-baseline justify-between text-xs">
            <span className="text-muted-foreground">Edge probes</span>
            <span className="font-medium tabular-nums">
              {billing.node_count}{billing.node_cap != null ? ` of ${billing.node_cap}` : " · unlimited"}
            </span>
          </div>
        </div>

        {billing.status !== "free" && (
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline justify-between text-xs">
              <span className="text-muted-foreground">Payments</span>
              <span className="font-medium">
                {billing.locked
                  ? `${monthLabel(billing.due_month ?? billing.current_month)} unpaid`
                  : billing.paid_through
                    ? `Paid through ${monthLabel(billing.paid_through)}`
                    : ""}
              </span>
            </div>
            <MonthStrip billing={billing} />
          </div>
        )}

        {billing.status === "free" ? (
          <PayWell billing={billing} org={org} showPaid={false} note={
            `Upgrades are manual: pay the first month (${inr(billing.plans.pro.price_inr)} Pro, ${inr(billing.plans.vip.price_inr)} VIP) to this number and the admin switches your plan in a moment.`
          } />
        ) : (
          <PayWell billing={billing} org={org} showPaid note={
            `${inr(spec.price_inr)} per month for the ${spec.label} plan. Tap "I've paid" after you pay and the admin marks your account. Reminders go to the owner alert channel from 3 days before a month runs out.`
          } />
        )}
      </CardContent>
    </Card>
  )
}
