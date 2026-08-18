import { useQuery } from "@tanstack/react-query"
import { Check, ChevronDown, ChevronsUpDown, Layers } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi } from "@/lib/api"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

function OrgTile({ letter, all, className }: { letter: string; all: boolean; className?: string }) {
  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center rounded-md",
        all ? "border bg-accent text-muted-foreground" : "bg-primary font-bold text-primary-foreground",
        className,
      )}
    >
      {all ? <Layers className="size-3.5" /> : letter.slice(0, 1).toUpperCase()}
    </span>
  )
}

export function WorkspaceRow({ variant = "sidebar" }: { variant?: "sidebar" | "topbar" }) {
  const { user, scopeOrg, setScopeOrg } = useAuth()
  const isSuperadmin = !!user?.is_superadmin
  const { data } = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user,
  })
  const orgs = data?.orgs ?? []

  const isAllOrgs = isSuperadmin && !scopeOrg
  const current = isSuperadmin ? orgs.find((o) => o.org_id === scopeOrg) : orgs[0]

  const name = isAllOrgs
    ? "All orgs"
    : current?.name || current?.org_id || scopeOrg || user?.org_id || "Workspace"
  // The plan label lived here until plans died with billing v1. The org id is
  // the only other thing this row genuinely knows without a new read — and it
  // is what an operator types into a probe enrollment or quotes to support.
  // This row renders for workers too, so nothing billing-shaped may go here.
  // Skipped when the headline IS the id (no display name set) so the row never
  // says the same thing twice.
  const orgId = current?.org_id ?? scopeOrg ?? user?.org_id ?? null
  const sidebarSub = isAllOrgs
    ? orgs.length
      ? `${orgs.length} organization${orgs.length === 1 ? "" : "s"}`
      : "Platform"
    : orgId && orgId !== name ? orgId : ""

  const sidebar = variant === "sidebar"
  const tile = sidebar ? "size-7 text-xs" : "size-6 text-2xs"

  const inner = (
    <>
      <OrgTile letter={name} all={isAllOrgs} className={tile} />
      <span className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
        <span className="block truncate text-sm font-semibold tracking-tight text-foreground">
          {name}
        </span>
        {sidebar && sidebarSub && (
          // An org id is a key somebody retypes, so it renders verbatim and
          // mono. The all-orgs count is prose and stays in the UI face.
          <span className={cn("block truncate text-2xs text-faint-foreground",
            isAllOrgs ? "" : "font-mono")}>
            {sidebarSub}
          </span>
        )}
      </span>
      {isSuperadmin &&
        (sidebar ? (
          <ChevronsUpDown className="size-3.5 shrink-0 text-faint-foreground group-data-[collapsible=icon]:hidden" />
        ) : (
          <ChevronDown className="size-3.5 shrink-0 text-faint-foreground" />
        ))}
    </>
  )

  const base = sidebar
    ? "flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left group-data-[collapsible=icon]:px-0"
    : "flex items-center gap-2 rounded-lg px-1.5 py-1 text-left"
  const topbarWidth = sidebar ? "" : "max-w-[9rem] sm:max-w-[13rem]"

  if (!isSuperadmin) {
    return <div className={cn(base, topbarWidth)}>{inner}</div>
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className={cn(base, topbarWidth, "transition-colors hover:bg-foreground/5 aria-expanded:bg-foreground/5")}
          aria-label="Switch organization"
        >
          {inner}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuLabel className="text-2xs font-normal text-faint-foreground">Workspace</DropdownMenuLabel>
        <DropdownMenuItem onClick={() => setScopeOrg(null)}>
          <Layers className="size-4 opacity-70" />
          <span className="flex-1">All orgs</span>
          {isAllOrgs && <Check className="size-4" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {orgs.map((o) => (
          <DropdownMenuItem key={o.org_id} onClick={() => setScopeOrg(o.org_id)}>
            <span className="flex-1 truncate">{o.name || o.org_id}</span>
            <span className="ml-auto flex items-center gap-2">
              <span className="font-mono text-2xs text-faint-foreground">{o.node_count}</span>
              {o.org_id === scopeOrg && <Check className="size-3.5" />}
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
