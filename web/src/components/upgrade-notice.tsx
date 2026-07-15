import { Link } from "react-router-dom"
import { Lock } from "lucide-react"
import type { BillingInfo } from "@/lib/types"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

// Surfaced the instant an "add" action is clicked while the org is at a plan
// cap, so the paywall lands up front instead of after a form round-trips to a
// 422 (central/billing.py enforces the same caps server-side). `resource` picks
// which cap to read; `secondary` is an optional way through (e.g. passive plant,
// which never counts against the device cap).
export function UpgradeNotice({ billing, resource, note, secondary, onClose }: {
  billing: BillingInfo
  resource: "device" | "probe"
  note?: string
  secondary?: { label: string; onClick: () => void }
  onClose: () => void
}) {
  const planLabel = billing.plans[billing.plan].label
  const upgrade = billing.plan === "free" ? billing.plans.pro : billing.plans.vip
  const field = resource === "probe" ? "node_cap" : "device_cap"
  const cap = billing.plans[billing.plan][field] ?? 0
  const upgradeCap = upgrade[field]
  const upgradeText = upgradeCap == null ? "unlimited" : `up to ${upgradeCap}`
  return (
    <Card className="border-primary/30">
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex items-start gap-2.5">
          <Lock className="mt-0.5 size-4 shrink-0 text-primary" />
          <div className="flex flex-col gap-1">
            <p className="text-sm font-semibold">{planLabel} plan {resource} limit reached</p>
            <p className="text-xs text-muted-foreground">
              The {planLabel} plan includes {cap} {resource}{cap === 1 ? "" : "s"} and you're using{" "}
              {cap === 1 ? "it" : "all of them"}. Upgrade to {upgrade.label} for {upgradeText}{" "}
              {resource}s. Manage your plan in Settings → Billing.
            </p>
          </div>
        </div>
        {note && <p className="text-xs text-muted-foreground">{note}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
          {secondary && (
            <Button variant="outline" size="sm" onClick={secondary.onClick}>{secondary.label}</Button>
          )}
          <Button size="sm" asChild>
            <Link to="/settings">Go to Billing</Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
