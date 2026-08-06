import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Check, Loader2 } from "lucide-react"
import { billingApi, ApiError } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"

/** "I've paid": once the org has sent the GPay/QR payment, this pings the
 * platform admin's payments channel with the org name so they verify and mark
 * the month. There's no gateway to confirm against — the admin does that by
 * hand — so the button just fires the heads-up and reports it was sent. It
 * stays reachable while the dashboard is locked. */
export function IvePaidButton({
  org, label = "I've paid", variant = "default", size = "sm", className,
}: {
  org?: string | null
  label?: string
  variant?: "default" | "outline" | "secondary"
  size?: "sm" | "default" | "lg"
  className?: string
}) {
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<"idle" | "sending" | "sent">("idle")

  const notify = async () => {
    setPhase("sending")
    try {
      await billingApi.markPaid(org)
      setPhase("sent")
      toast.success("Thanks, we've let the team know. Your dashboard unlocks "
        + "the moment your payment is confirmed.")
      // billing itself doesn't change yet (the admin marks the month), but a
      // refetch keeps the card honest if they already did
      queryClient.invalidateQueries({ queryKey: ["billing"] })
    } catch (e) {
      setPhase("idle")
      toast.error(e instanceof ApiError ? e.message : "Couldn't send that. Try again.")
    }
  }

  return (
    <Button variant={variant} size={size} className={className}
      disabled={phase !== "idle"} onClick={notify}>
      {phase === "sending"
        ? <Loader2 className="size-3.5 animate-spin" />
        : <Check className="size-3.5" />}
      {phase === "sent" ? "Admin notified" : label}
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
        description={"Existing devices keep working and alerts keep flowing, nothing is deleted. On Free, adding devices caps at 5 and edge probes at 1. You can move back to a paid plan anytime by paying the admin."}
        onConfirm={downgrade} />
    </>
  )
}
