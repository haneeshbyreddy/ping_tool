import type { BillingStatus, Plan } from "./types"

/** Month keys are 'YYYY-MM' in UTC — mirror central/billing.py's math. */

export function currentMonthKey(): string {
  const d = new Date()
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`
}

export function addMonths(month: string, n: number): string {
  const y = Number(month.slice(0, 4))
  const m = Number(month.slice(5, 7)) - 1 + n
  const yy = y + Math.floor(m / 12)
  const mm = ((m % 12) + 12) % 12
  return `${yy}-${String(mm + 1).padStart(2, "0")}`
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"]

export function monthLabel(month: string): string {
  return `${MONTHS[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`
}

export function monthShort(month: string): string {
  return MONTHS[Number(month.slice(5, 7)) - 1].slice(0, 3)
}

export function inr(n: number): string {
  return `₹${n.toLocaleString("en-IN")}`
}

export const PLAN_ORDER: Plan[] = ["free", "pro", "vip"]

/** Plan-tier tone, shared by the top-bar chip and the orgs-page badge:
 * Free stays quiet, Pro rides the accent, VIP gets its own premium violet —
 * a product-tier color, not status (status colors stay the loudest). */
export function planTone(plan: Plan): string {
  switch (plan) {
    case "vip":
      return "border-violet-500/40 bg-violet-500/10 text-violet-600 dark:text-violet-400"
    case "pro":
      return "border-primary/40 bg-primary-soft text-primary"
    default:
      return "border-border bg-muted text-muted-foreground"
  }
}

/** Status chip tone — theme tokens only, warning stays the loudest thing. */
export function billingStatusMeta(status: BillingStatus): { label: string; className: string } {
  switch (status) {
    case "locked":
      return { label: "Locked, payment due", className: "bg-destructive/10 text-destructive" }
    case "due_soon":
      return { label: "Payment due soon", className: "bg-warning-soft text-warning" }
    case "active":
      return { label: "Active", className: "bg-success-soft text-success" }
    default:
      return { label: "Free plan", className: "bg-muted text-muted-foreground" }
  }
}
