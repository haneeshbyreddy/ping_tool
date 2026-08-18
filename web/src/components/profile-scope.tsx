import { Label } from "@/components/ui/label"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { scopeLabel, scopeSentence, scopeTitle, type ProfileScope } from "@/lib/profiles"

// One control and one badge for every recipe table (SNMP health, GPON, web-UI
// optics, user-MAC, RADIUS). Global vs one-org is now the most consequential
// field on a profile, so it gets the same words and the same place everywhere
// rather than a phrasing per card.

/** Who a saved recipe reaches. Sits beside the name on a list row. */
export function ScopeBadge({ orgId }: { orgId: string | null }) {
  return (
    <span
      className="shrink-0 rounded bg-muted px-1.5 py-px text-2xs font-semibold text-muted-foreground"
      title={scopeTitle(orgId)}>
      {scopeLabel(orgId)}
    </span>
  )
}

/**
 * Create mode. Global is the default and the override is visible next to it,
 * never buried: an org-scoped row is the thing that turns one recipe into one
 * copy per customer.
 */
export function ScopeField({ scope, onScope, org, hint }: {
  scope: ProfileScope
  onScope: (s: ProfileScope) => void
  org: string | null
  hint: string
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>Who gets it</Label>
      <Select value={scope} onValueChange={(v) => onScope(v as ProfileScope)}>
        <SelectTrigger className="h-9 text-xs"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="global">Global · every organization</SelectItem>
          <SelectItem value="org" disabled={!org}>
            {org ? `${org} only` : "One organization only (pick an org first)"}
          </SelectItem>
        </SelectContent>
      </Select>
      <p className="text-2xs text-muted-foreground">{hint}</p>
    </div>
  )
}

/** Edit mode. Scope is set at create time, so it is stated, not offered. */
export function ScopeNote({ orgId }: { orgId: string | null }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>Who gets it</Label>
      <p className="text-xs text-muted-foreground">
        {scopeSentence(orgId)} Set when the profile was created, and not editable here.
      </p>
    </div>
  )
}
