import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { CreditCard } from "lucide-react"
import { cn } from "@/lib/utils"
import { ApiError, billingApi } from "@/lib/api"
import {
  addMonths, billingStatusMeta, currentMonthKey, inr, monthLabel, monthShort,
} from "@/lib/billing"
import type { Plan } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

/** Superadmin-only billing control for one org: pick the plan, click months
 * paid/unpaid. Marking future months ahead IS the "no reminder" switch —
 * central pages the owner only when the paid runway drops under 3 days, and
 * locks the dashboard the moment a month starts unpaid. */
export function BillingAdminDialog({ org, name }: { org: string; name: string | null }) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data: billing, isLoading } = useQuery({
    queryKey: ["billing", org],
    queryFn: () => billingApi.get(org),
    enabled: open,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["billing", org] })
    queryClient.invalidateQueries({ queryKey: ["orgs"] })
  }
  const save = useMutation({
    mutationFn: (body: { plan?: Plan; month?: string; paid?: boolean }) =>
      billingApi.adminSave({ org_id: org, ...body }),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Billing update failed"),
  })

  const now = currentMonthKey()
  const months = Array.from({ length: 12 }, (_, i) => addMonths(now, i - 1))
  const paid = new Set(billing?.paid_months ?? [])
  const meta = billing ? billingStatusMeta(billing.status) : null

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <CreditCard className="size-3.5" /> Billing
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Billing: {name || org}
            {meta && (
              <span className={cn("rounded-4xl px-2 py-0.5 text-2xs font-medium", meta.className)}>
                {meta.label}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>
        {isLoading || !billing ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label>Plan</Label>
              <Select value={billing.plan} disabled={save.isPending}
                onValueChange={(v) => save.mutate({ plan: v as Plan })}>
                <SelectTrigger className="w-full max-w-56"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(Object.keys(billing.plans) as Plan[]).map((p) => (
                    <SelectItem key={p} value={p}>
                      {billing.plans[p].label} ({billing.plans[p].price_inr === 0
                        ? "free" : `${inr(billing.plans[p].price_inr)}/mo`})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {billing.device_count} monitored device{billing.device_count === 1 ? "" : "s"}
                {billing.device_cap != null && ` of ${billing.device_cap} allowed`}
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label>Paid months</Label>
              <div className="grid grid-cols-4 gap-1.5">
                {months.map((m) => {
                  const isPaid = paid.has(m)
                  return (
                    <button key={m}
                      disabled={save.isPending || billing.plan === "free"}
                      title={`${monthLabel(m)}: click to mark ${isPaid ? "unpaid" : "paid"}`}
                      className={cn(
                        "rounded-md border px-2 py-1.5 text-xs font-medium tabular-nums transition-colors",
                        isPaid
                          ? "border-success/40 bg-success-soft text-success"
                          : "text-muted-foreground hover:bg-foreground/5",
                        m === now && "ring-1 ring-ring",
                        billing.plan === "free" && "opacity-50",
                      )}
                      onClick={() => save.mutate({ month: m, paid: !isPaid })}>
                      {monthShort(m)} {m.slice(2, 4)}
                    </button>
                  )
                })}
              </div>
              <p className="text-xs text-muted-foreground">
                {billing.plan === "free"
                  ? "Free plan never locks. Months apply to Pro/VIP only."
                  : "The org locks the moment a month starts unpaid; its owner is reminded from 3 days before. Pre-mark future months to skip the reminders."}
              </p>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
