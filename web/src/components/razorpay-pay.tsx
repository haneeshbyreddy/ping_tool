import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { IndianRupee, Loader2 } from "lucide-react"
import { billingApi, ApiError } from "@/lib/api"
import { openCheckout } from "@/lib/razorpay"
import { monthLabel } from "@/lib/billing"
import type { Plan } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"

/** The one checkout flow every Pay button shares: create the order on
 * central, open Razorpay Checkout, post the signed result back for
 * verification, then let react-query repaint (the lock screen dissolves by
 * itself once /api/billing flips). Render only when
 * `billing.razorpay_key_id` is set — without keys the GPay flow stays. */
export function RazorpayPayButton({
  org, plan, months = 1, label, variant = "default", size = "sm",
  className, onPaid,
}: {
  /** omitted = the caller's own org (server scopes writes to it anyway) */
  org?: string | null
  /** target plan — pass to upgrade a free org at checkout; omit to renew */
  plan?: Plan
  months?: number
  label: string
  variant?: "default" | "outline" | "secondary"
  size?: "sm" | "default" | "lg"
  className?: string
  onPaid?: () => void
}) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)

  const pay = async () => {
    setBusy(true)
    try {
      const order = await billingApi.order({ org_id: org, plan, months })
      const result = await openCheckout(order)
      if (!result) return // modal dismissed, nothing charged
      const st = await billingApi.verify({ org_id: org, ...result })
      toast.success(st.paid_through
        ? `Payment received — paid through ${monthLabel(st.paid_through)}`
        : "Payment received")
      queryClient.invalidateQueries({ queryKey: ["billing"] })
      queryClient.invalidateQueries({ queryKey: ["orgs"] })
      onPaid?.()
    } catch (e) {
      toast.error(e instanceof ApiError || e instanceof Error
        ? e.message : "Payment failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button variant={variant} size={size} className={className}
      disabled={busy} onClick={pay}>
      {busy ? <Loader2 className="size-3.5 animate-spin" /> : <IndianRupee className="size-3.5" />}
      {label}
    </Button>
  )
}

/** Self-serve drop to Free — the only plan change that needs no payment, and
 * the lock screen's escape hatch for an org that won't pay this month.
 * Confirmed first: it's a real downgrade, not a checkout. */
export function FreePlanButton({
  org, label = "Switch to Free", variant = "outline", size = "sm",
  className, onDone,
}: {
  org?: string | null
  label?: string
  variant?: "ghost" | "outline" | "secondary"
  size?: "sm" | "default"
  className?: string
  onDone?: () => void
}) {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [busy, setBusy] = useState(false)

  const downgrade = async () => {
    setBusy(true)
    try {
      await billingApi.setPlan({ org_id: org, plan: "free" })
      toast.success("You're on the Free plan now")
      queryClient.invalidateQueries({ queryKey: ["billing"] })
      queryClient.invalidateQueries({ queryKey: ["orgs"] })
      onDone?.()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Plan change failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button variant={variant} size={size} className={className}
        disabled={busy} onClick={confirm.ask}>
        {busy && <Loader2 className="size-3.5 animate-spin" />}
        {label}
      </Button>
      <ConfirmDialog {...confirm.props} title="Switch to the Free plan?"
        confirmLabel="Switch to Free"
        description={"Existing devices keep working and alerts keep flowing — nothing is deleted. On Free, adding devices caps at 5 and edge probes at 1. You can upgrade again anytime by paying online."}
        onConfirm={downgrade} />
    </>
  )
}
