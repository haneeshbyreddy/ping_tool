import { NavLink } from "react-router-dom"
import { cn } from "@/lib/utils"
import { planTone } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"

/** Plan-tier chip for the top bar, next to the org scope. Click-through to
 * Settings → Plan & billing. Tones come from planTone (lib/billing.ts). */
export function PlanChip({ billing }: { billing: BillingInfo }) {
  const label = billing.plans[billing.plan]?.label ?? billing.plan
  return (
    <NavLink
      to="/settings"
      title="Plan & billing"
      className={cn(
        "hidden h-6 shrink-0 items-center rounded-4xl border px-2.5 text-xs font-semibold tracking-wide transition-opacity hover:opacity-80 sm:inline-flex",
        planTone(billing.plan),
      )}
    >
      {label}
    </NavLink>
  )
}
