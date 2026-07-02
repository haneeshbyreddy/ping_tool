import { useQuery } from "@tanstack/react-query"
import { ChevronDown, Building2 } from "lucide-react"
import { orgsApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"

// Superadmin-only — an org user is pinned to one org server-side and never sees
// this (mirrors the old dashboard's renderOrgPicker, restricted the same way).
export function OrgSwitcher() {
  const { user, scopeOrg, setScopeOrg } = useAuth()
  const { data } = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user?.is_superadmin,
  })

  if (!user?.is_superadmin) return null
  const orgs = data?.orgs ?? []
  const current = orgs.find((o) => o.org_id === scopeOrg)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="min-w-0 max-w-32 gap-1.5 sm:max-w-56">
          <Building2 className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate">{current ? (current.name || current.org_id) : "All orgs"}</span>
          <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuItem onClick={() => setScopeOrg(null)}>
          <span className="flex-1">All orgs</span>
        </DropdownMenuItem>
        {orgs.map((o) => (
          <DropdownMenuItem key={o.org_id} onClick={() => setScopeOrg(o.org_id)}>
            <span className="flex-1 truncate">{o.name || o.org_id}</span>
            <span className="font-mono text-xs text-muted-foreground">{o.node_count} nodes</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
