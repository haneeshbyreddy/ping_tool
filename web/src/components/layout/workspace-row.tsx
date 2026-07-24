import { useQuery } from "@tanstack/react-query"
import { Check, ChevronDown, ChevronsUpDown, Layers } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi } from "@/lib/api"
import type { Plan } from "@/lib/types"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

function planLabel(plan?: Plan | null): string {
  if (!plan) return ""
  return plan === "vip" ? "VIP" : plan.charAt(0).toUpperCase() + plan.slice(1)
}

/** The org avatar — a squared brand tile. Deliberately a square: the person's
 *  avatar in the foot AccountMenu is a circle, so shape alone says "workspace"
 *  vs "you" (the conventional Slack/Linear split). "All orgs" gets a neutral
 *  glyph tile instead of a letter, because it isn't one org. */
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

/** The sidebar's top-left slot (and the mobile header's identity slot). Names
 *  the SCOPED org — avatar + name + plan — where the old build hard-coded
 *  "WISP Central" for superadmins and never named the org they were looking at.
 *
 *  For a superadmin it IS the org switcher (this folds in the old
 *  layout/org-switcher.tsx). For an owner/worker it is the same row rendered
 *  static — same slot, same shape, one product. */
export function WorkspaceRow({ variant = "sidebar" }: { variant?: "sidebar" | "topbar" }) {
  const { user, scopeOrg, setScopeOrg } = useAuth()
  const isSuperadmin = !!user?.is_superadmin
  const { data } = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user,
  })
  const orgs = data?.orgs ?? []

  // Superadmin at "All orgs" has no scoped org; the platform pages own that view.
  const isAllOrgs = isSuperadmin && !scopeOrg
  // Owners/workers get only their own org back from the API, so orgs[0] is it.
  const current = isSuperadmin ? orgs.find((o) => o.org_id === scopeOrg) : orgs[0]

  const name = isAllOrgs
    ? "All orgs"
    : current?.name || current?.org_id || scopeOrg || user?.org_id || "Workspace"
  const sidebarSub = isAllOrgs
    ? orgs.length
      ? `${orgs.length} organization${orgs.length === 1 ? "" : "s"}`
      : "Platform"
    : planLabel(current?.plan)

  const sidebar = variant === "sidebar"
  const tile = sidebar ? "size-7 text-xs" : "size-6 text-2xs"

  // Shared inner content, so the superadmin (button) and owner (static) rows are
  // byte-identical apart from the chevron and the hover affordance.
  const inner = (
    <>
      <OrgTile letter={name} all={isAllOrgs} className={tile} />
      <span className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
        <span className="block truncate text-sm font-semibold tracking-tight text-foreground">
          {name}
        </span>
        {sidebar && sidebarSub && (
          <span className="block truncate text-2xs text-faint-foreground capitalize">{sidebarSub}</span>
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

  // Owner / worker: the same row, non-interactive.
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
