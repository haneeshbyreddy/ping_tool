import { useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { IndianRupee, Loader2 } from "lucide-react"
import { billingApi, ApiError, type BillingVerify } from "@/lib/api"
import { monthLabel } from "@/lib/billing"
import type { Plan } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"

const POLL_MS = 4000
const POLL_WINDOW_MS = 8 * 60_000

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** The one checkout flow every Pay button shares: create the order on
 * central, open UPIGateway's hosted QR page in a new tab, then poll
 * `/api/billing/verify` — central checks the payment status server-side
 * (there is no signed handshake to trust) and react-query repaints (the lock
 * screen dissolves by itself once /api/billing flips). A payer who closes
 * everything mid-flight still settles via the sweeper within the half hour.
 *
 * Render only when `billing.upi_enabled` — without a key the manual GPay
 * flow stays. */
export function PayOnlineButton({
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
  const [phase, setPhase] = useState<"idle" | "starting" | "waiting">("idle")
  // survives re-renders while polling; also our re-entrancy guard
  const polling = useRef(false)

  const settled = (st: BillingVerify) => {
    toast.success(st.paid_through
      ? `Payment received — paid through ${monthLabel(st.paid_through)}`
      : "Payment received")
    queryClient.invalidateQueries({ queryKey: ["billing"] })
    queryClient.invalidateQueries({ queryKey: ["orgs"] })
    onPaid?.()
  }

  const pollUpi = async (orderId: string) => {
    const deadline = Date.now() + POLL_WINDOW_MS
    let misses = 0
    while (Date.now() < deadline) {
      await sleep(POLL_MS)
      try {
        const st = await billingApi.verify({ org_id: org, order_id: orderId })
        if (st.payment_status === "success") { settled(st); return }
        if (st.payment_status === "failure") {
          toast.error("Payment failed or was cancelled — nothing was charged.")
          return
        }
        misses = 0
      } catch (e) {
        // a gateway/network blip mid-poll is normal; five in a row isn't
        if (++misses >= 5) throw e
      }
    }
    toast.info("Payment still pending — once it clears, your account "
      + "updates automatically (within half an hour at most).")
  }

  const pay = async () => {
    if (polling.current) return
    polling.current = true
    setPhase("starting")
    // Claim the tab synchronously inside the click — after the order await,
    // window.open would be popup-blocked.
    const payTab = window.open("", "_blank")
    try {
      const order = await billingApi.order({
        org_id: org, plan, months, origin: window.location.origin,
      })
      if (!order.payment_url) throw new Error("Gateway returned no payment link")
      if (payTab) payTab.location.href = order.payment_url
      // popup blocked: same-tab — the gateway's return redirect lands back
      // on /app and billing repaints from the server-settled state
      else { window.location.assign(order.payment_url); return }
      setPhase("waiting")
      await pollUpi(order.order_id)
    } catch (e) {
      payTab?.close()
      toast.error(e instanceof ApiError || e instanceof Error
        ? e.message : "Payment failed")
    } finally {
      polling.current = false
      setPhase("idle")
    }
  }

  return (
    <Button variant={variant} size={size} className={className}
      disabled={phase !== "idle"} onClick={pay}>
      {phase !== "idle"
        ? <Loader2 className="size-3.5 animate-spin" />
        : <IndianRupee className="size-3.5" />}
      {phase === "waiting" ? "Waiting for payment…" : label}
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
