import { Building2 } from "lucide-react"

// Shown to a superadmin who hasn't picked an org from the header switcher yet — org
// users are always pinned server-side so they never see this (mirrors the old
// dashboard's needsOrgCard()).
export function NeedsOrg() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-24 text-center text-muted-foreground">
      <Building2 className="size-8" />
      <p className="text-sm">Pick an org from the switcher above to see its data.</p>
    </div>
  )
}
