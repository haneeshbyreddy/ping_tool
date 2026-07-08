import { Building2 } from "lucide-react"

export function NeedsOrg() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-24 text-center text-muted-foreground">
      <Building2 className="size-8" />
      <p className="text-sm">Pick an org from the switcher above to see its data.</p>
    </div>
  )
}
