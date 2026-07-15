import { useEffect, useRef, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { billingApi, orgsApi } from "@/lib/api"
import { BillingBanner, BillingLock, BillingLockedNote } from "@/components/billing-lock"
import { PlanChip } from "@/components/plan-chip"
import { NAV_ITEMS, MORE_ITEMS } from "./nav-items"
import { AlarmChips } from "./alarm-chips"
import { OrgSwitcher } from "./org-switcher"
import { UserMenu } from "./user-menu"
import { CommandPalette } from "./command-palette"
import {
  Sidebar, SidebarContent, SidebarGroup, SidebarGroupContent, SidebarHeader, SidebarInset,
  SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger,
} from "@/components/ui/sidebar"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"
import { MoreHorizontal } from "lucide-react"

function Brand() {
  const { user } = useAuth()

  const { data } = useQuery({
    queryKey: ["orgs", "brand", user?.org_id],
    queryFn: () => orgsApi.list(),
    enabled: !!user && !user.is_superadmin,
    staleTime: 5 * 60 * 1000,
  })
  const orgName = !user?.is_superadmin
    ? data?.orgs[0]?.name || user?.org_id
    : null

  return (
    <NavLink to="/" className="flex min-w-0 shrink-0 items-center gap-2 px-1">
      <span className="truncate whitespace-nowrap text-sm font-semibold tracking-tight text-foreground">
        {orgName || "WISP Central"}
      </span>
    </NavLink>
  )
}

export function AppShell() {
  const [searchOpen, setSearchOpen] = useState(false)
  const { user, scopeOrg } = useAuth()
  const { pathname } = useLocation()
  const queryClient = useQueryClient()
  const navItems = NAV_ITEMS.filter((i) => !i.superadminOnly || user?.is_superadmin)
  const moreItems = MORE_ITEMS.filter((i) => !i.superadminOnly || user?.is_superadmin)

  const isNavActive = (to: string) => (to === "/" ? pathname === "/" : pathname.startsWith(to))

  // Paywall: /api/billing stays reachable while everything else 402s, so this
  // one poll drives the lock screen AND its automatic release after payment.
  const billingOrg = user ? (user.is_superadmin ? scopeOrg : user.org_id) : null
  const { data: billing } = useQuery({
    queryKey: ["billing", billingOrg],
    queryFn: () => billingApi.get(billingOrg),
    enabled: !!billingOrg,
    refetchInterval: 60_000,
  })

  // A 402 mid-session (month rolled over unpaid) re-checks billing immediately
  // instead of waiting out the poll.
  useEffect(() => {
    const handler = () => queryClient.invalidateQueries({ queryKey: ["billing"] })
    window.addEventListener("wisp:payment-required", handler)
    return () => window.removeEventListener("wisp:payment-required", handler)
  }, [queryClient])

  // On unlock, every query that 402'd while locked is stale — refetch the lot.
  const wasLocked = useRef(false)
  useEffect(() => {
    if (wasLocked.current && billing && !billing.locked) queryClient.invalidateQueries()
    wasLocked.current = !!billing?.locked
  }, [billing, queryClient])

  if (billing?.locked && user && !user.is_superadmin) {
    return <BillingLock billing={billing} />
  }

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" className="hidden md:flex">
        <SidebarHeader>
          <Brand />
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {navItems.map((item) => (
                  <SidebarMenuItem key={item.to}>
                    <SidebarMenuButton asChild tooltip={item.label}>
                      <NavLink
                        to={item.to}
                        end={item.to === "/"}
                        className={cn(
                          isNavActive(item.to) &&
                            "bg-primary-soft text-primary hover:bg-primary-soft hover:text-primary",
                        )}
                      >
                        <item.icon />
                        <span>{item.label}</span>
                      </NavLink>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>

      <SidebarInset>
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b bg-background px-3 md:px-5">
          <SidebarTrigger className="hidden md:flex" />
          <div className="md:hidden">
            <Brand />
          </div>
          <OrgSwitcher />
          {billing && <PlanChip billing={billing} />}
          <div className="flex-1" />
          <AlarmChips />
          {/* input-shaped so the palette is discoverable; icon-only on mobile */}
          <button
            className="hidden h-8 w-52 items-center gap-2 rounded-lg border bg-muted/30 px-2.5 text-xs text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground md:flex"
            onClick={() => setSearchOpen(true)}>
            <Search className="size-3.5 shrink-0" />
            <span className="flex-1 text-left">Search…</span>
            <kbd className="pointer-events-none rounded border bg-muted px-1.5 py-px font-mono text-2xs">
              {navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K"}
            </kbd>
          </button>
          <Button variant="outline" size="icon" className="size-8 md:hidden" aria-label="Search"
            onClick={() => setSearchOpen(true)}>
            <Search className="size-4" />
          </Button>
          <UserMenu />
        </header>

        {billing && billingOrg && (
          user?.is_superadmin
            ? <BillingLockedNote billing={billing} />
            : <BillingBanner billing={billing} org={billingOrg} />
        )}

        <main className="flex-1 overflow-y-auto pb-16 md:pb-0">
          <Outlet />
        </main>

        {/* Mobile bottom tab bar — mirrors the mockup's 5-icon nav (More folds Team/
            Settings/Logs, which get their own sidebar entries on desktop). */}
        <nav className="fixed inset-x-0 bottom-0 z-30 flex items-stretch justify-around border-t bg-sidebar px-1 pb-[env(safe-area-inset-bottom)] md:hidden">
          {navItems.filter((i) => i.mobile).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex min-w-14 flex-col items-center gap-0.5 px-2 py-2 text-2xs font-medium",
                  isActive ? "text-primary" : "text-muted-foreground",
                )
              }
            >
              <item.icon className="size-5" />
              {item.label}
            </NavLink>
          ))}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="flex min-w-14 flex-col items-center gap-0.5 px-2 py-2 text-2xs font-medium text-muted-foreground">
                <MoreHorizontal className="size-5" />
                More
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="mb-2">
              {moreItems.map((item) => (
                <DropdownMenuItem key={item.to} asChild>
                  <NavLink to={item.to}>
                    <item.icon />
                    {item.label}
                  </NavLink>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </nav>
      </SidebarInset>

      <CommandPalette open={searchOpen} onOpenChange={setSearchOpen} />
    </SidebarProvider>
  )
}
