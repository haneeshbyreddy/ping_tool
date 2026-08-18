import { useEffect, useRef, useState } from "react"
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { MoreHorizontal, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { useIsMobile } from "@/hooks/use-mobile"
import { billingApi } from "@/lib/api"
import { BillingBanner, BillingLock, BillingLockedNote, LockedBand } from "@/components/billing-lock"
import { NAV_ITEMS, MORE_ITEMS, NAV_GROUPS } from "./nav-items"
import { useSplit } from "@/hooks/use-split-view"
import { SplitControl, SplitView } from "./split-view"
import { AlarmChips } from "./alarm-chips"
import { BillTape } from "./bill-tape"
import { WorkspaceRow } from "./workspace-row"
import { UserMenu } from "./user-menu"
import { AccountMenu } from "./account-menu"
import { useTriage } from "@/hooks/use-triage"
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel,
  SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuBadge, SidebarMenuButton, SidebarMenuItem,
  SidebarProvider, SidebarTrigger,
} from "@/components/ui/sidebar"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"

export function AppShell() {
  const { user, scopeOrg } = useAuth()
  const split = useSplit()
  const isWorker = !!user && !user.is_superadmin && user.role === "worker"
  const isMobile = useIsMobile()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const navItems = NAV_ITEMS.filter(
    (i) => (!i.superadminOnly || user?.is_superadmin) && !(i.ownerOnly && isWorker),
  )
  const moreItems = MORE_ITEMS.filter(
    (i) => (!i.superadminOnly || user?.is_superadmin)
      && !((i.account || i.ownerOnly) && isWorker),
  )
  const sidebarItems = navItems.filter((i) => !i.account)

  // The queue left Home for /triage, so the nav item carries its depth: loud
  // while anything needs a response, quiet while only post-mortem paperwork
  // waits. Same useTriage numbers Home's verdict and the page itself quote.
  const triage = useTriage()
  const triageBadge = triage.loading ? null
    : triage.urgent > 0
      ? { n: triage.urgent, cls: "border-destructive/30 bg-destructive-soft text-destructive" }
      : triage.postmortems.length > 0
        ? { n: triage.postmortems.length, cls: "border-border bg-foreground/[0.04] text-muted-foreground" }
        : null

  const isNavActive = (to: string) => (to === "/" ? pathname === "/" : pathname.startsWith(to))

  const goToSearch = () =>
    navigate("/topology", { state: { focusSearch: Date.now() } })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "k" || !(e.metaKey || e.ctrlKey)) return
      const el = document.activeElement
      if (el instanceof HTMLElement &&
        (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return
      e.preventDefault()
      navigate("/topology", { state: { focusSearch: Date.now() } })
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [navigate])

  // WORKERS NEVER FETCH THIS. `/api/billing` is off the worker allowlist, so a
  // worker firing it gets a 403 and a red console line on every shell mount —
  // and there is nothing on the reply they are allowed to see anyway. The org
  // is null for a superadmin in All orgs: there is no one bill to show.
  const billingOrg = user && !isWorker ? (user.is_superadmin ? scopeOrg : user.org_id) : null
  const { data: billing } = useQuery({
    queryKey: ["billing", billingOrg],
    queryFn: () => billingApi.get(billingOrg),
    enabled: !!billingOrg,
    // The event stream deliberately does not carry billing (a ledger does not
    // move on a probe report), so freshness is this poll plus the 402 below.
    refetchInterval: 60_000,
  })

  // Any /api/* answering 402 means the ledger moved under us: the lock landed
  // between polls, or a payment just cleared it.
  const [paymentRequired, setPaymentRequired] = useState(false)
  useEffect(() => {
    const handler = () => {
      setPaymentRequired(true)
      queryClient.invalidateQueries({ queryKey: ["billing"] })
    }
    window.addEventListener("wisp:payment-required", handler)
    return () => window.removeEventListener("wisp:payment-required", handler)
  }, [queryClient])

  useEffect(() => {
    if (isWorker) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "," || !(e.metaKey || e.ctrlKey)) return
      const el = document.activeElement
      if (el instanceof HTMLElement &&
        (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return
      e.preventDefault()
      navigate("/settings")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [navigate, isWorker])

  const wasLocked = useRef(false)
  useEffect(() => {
    if (billing && !billing.locked) setPaymentRequired(false)
    if (wasLocked.current && billing && !billing.locked) {
      queryClient.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "billing" })
    }
    wasLocked.current = !!billing?.locked
  }, [billing, queryClient])

  // The lock screen carries the amount and the pay button, so it needs a
  // billing read: owners only. A superadmin scoped to a locked org keeps the
  // whole app (the server exempts them from the 402 for the same reason).
  if (billing?.locked && user && !user.is_superadmin) {
    return <BillingLock billing={billing} org={billingOrg} />
  }

  if (isWorker && isMobile) {
    return <FieldShell locked={paymentRequired} />
  }

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" className="hidden md:flex">
        <SidebarHeader className="pb-3">
          <WorkspaceRow />
        </SidebarHeader>
        <SidebarContent>
          {NAV_GROUPS.map((grp) => {
            const items = sidebarItems.filter((i) => i.group === grp.id)
            if (!items.length) return null
            return (
              <SidebarGroup key={grp.id}>
                <SidebarGroupLabel>{grp.label}</SidebarGroupLabel>
                <SidebarGroupContent>
                  <SidebarMenu className="gap-0.5">
                    {items.map((item) => (
                      <SidebarMenuItem key={item.to}>
                        <SidebarMenuButton asChild tooltip={item.label} className="h-9 gap-3 px-3">
                          <NavLink
                            to={item.to}
                            end={item.to === "/"}
                            className={cn(
                              "text-xs font-medium text-muted-foreground transition-colors",
                              isNavActive(item.to) &&
                                "bg-foreground/[0.07] text-foreground shadow-[inset_2px_0_0_var(--primary)] hover:bg-foreground/[0.07] hover:text-foreground",
                            )}
                          >
                            <item.icon className="size-4" />
                            <span>{item.label}</span>
                          </NavLink>
                        </SidebarMenuButton>
                        {item.to === "/triage" && triageBadge && (
                          <SidebarMenuBadge
                            className={cn("rounded-md border font-mono text-2xs font-semibold",
                              triageBadge.cls)}>
                            {triageBadge.n > 99 ? "99+" : triageBadge.n}
                          </SidebarMenuBadge>
                        )}
                      </SidebarMenuItem>
                    ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            )
          })}
        </SidebarContent>
        <SidebarFooter>
          <AccountMenu billing={billing} />
        </SidebarFooter>
      </Sidebar>

      <SidebarInset className={cn(split.view && "h-svh overflow-hidden")}>
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b bg-background px-3 md:px-6">
          <SidebarTrigger className="hidden text-muted-foreground md:flex" />
          <div className="md:hidden">
            <WorkspaceRow variant="topbar" />
          </div>
          <div className="flex-1" />
          <AlarmChips />
          {/* Left of search, right of the alarm chips: the chrome zone, not
              the alarm zone. Rendered off the same read the lock and the
              banner use, so the three never quote different money. */}
          {billing && billingOrg && !isWorker && <BillTape billing={billing} />}
          <button
            className="hidden h-8 w-52 items-center gap-2 rounded-lg border bg-muted px-2.5 text-xs text-faint-foreground transition-colors hover:border-border-strong hover:text-muted-foreground lg:flex lg:w-72"
            onClick={goToSearch}>
            <Search className="size-3.5 shrink-0" />
            <span className="flex-1 text-left">Search devices and ONUs…</span>
            <kbd className="pointer-events-none rounded border bg-accent px-1.5 py-px font-mono text-2xs">
              {navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K"}
            </kbd>
          </button>
          <Button variant="ghost" size="icon" className="size-8 lg:hidden" aria-label="Search"
            onClick={goToSearch}>
            <Search className="size-4" />
          </Button>
          <SplitControl />
          <span className="md:hidden">
            <UserMenu />
          </span>
        </header>

        {billing && billingOrg && !isWorker && (
          user?.is_superadmin
            ? <BillingLockedNote billing={billing} />
            : <BillingBanner billing={billing} org={billingOrg} />
        )}
        {isWorker && paymentRequired && <WorkerLockedNote />}

        <ShellMain />

        <nav className="fixed inset-x-0 bottom-0 z-30 flex items-stretch justify-around border-t bg-sidebar px-1 pb-[env(safe-area-inset-bottom)] md:hidden">
          {navItems.filter((i) => i.mobile).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2 text-2xs font-medium",
                  isActive ? "text-foreground" : "text-faint-foreground",
                )
              }
            >
              <item.icon className="size-5" />
              {item.label}
            </NavLink>
          ))}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="relative flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2 text-2xs font-medium text-faint-foreground">
                <span className="relative">
                  <MoreHorizontal className="size-5" />
                  {/* Triage is off the six-slot tab bar, so its urgency dot
                      rides the button that reaches it. */}
                  {triageBadge && (
                    <span className={cn("absolute -top-0.5 -right-1 size-2 rounded-full",
                      triage.urgent > 0 ? "bg-destructive" : "bg-muted-foreground/50")} />
                  )}
                </span>
                More
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="mb-2">
              {moreItems.map((item) => (
                <DropdownMenuItem key={item.to} asChild>
                  <NavLink to={item.to}>
                    <item.icon />
                    {item.label}
                    {item.to === "/triage" && triageBadge && (
                      <span className={cn(
                        "ml-auto rounded-md border px-1.5 font-mono text-2xs font-semibold",
                        triageBadge.cls)}>
                        {triageBadge.n > 99 ? "99+" : triageBadge.n}
                      </span>
                    )}
                  </NavLink>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </nav>
      </SidebarInset>
    </SidebarProvider>
  )
}

function ShellMain() {
  const split = useSplit()
  if (!split.view) {
    return (
      <main className="flex-1 overflow-y-auto pb-16 md:pb-0">
        <Outlet />
      </main>
    )
  }
  return (
    <main className="min-h-0 flex-1 overflow-hidden pb-16 md:pb-0">
      <SplitView><Outlet /></SplitView>
    </main>
  )
}

/** What a WORKER is told when the org is locked. `/api/billing` is off the
 *  worker allow-list, so this shell has no BillingInfo to hand
 *  `BillingLockedNote` and no amount, month or pay action it is allowed to
 *  state. Its only input is a 402 landing on some other call.
 *
 *  Same neutral band and same reasoning as the note it stands in for: a red
 *  bar on a NOC screen is a claim about the NETWORK, and this reader cannot
 *  act on the account anyway. Reload is the honest reset, since nothing else
 *  tells this shell the bill was settled. */
function WorkerLockedNote() {
  return (
    <LockedBand
      lead="This account is locked."
      rest="Monitoring and alerts keep running. Ask an owner to settle the bill."
      action={
        <button type="button" onClick={() => window.location.reload()}
          className="shrink-0 text-xs text-muted-foreground transition-colors hover:text-foreground">
          Reload
        </button>
      } />
  )
}

function FieldShell({ locked }: { locked: boolean }) {
  const { user } = useAuth()
  const { pathname } = useLocation()

  if (pathname !== "/survey") return <Navigate to="/survey" replace />

  return (
    <div className="flex min-h-svh flex-col bg-background">
      <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between gap-3 border-b bg-background px-4">
        <span className="truncate text-sm font-semibold tracking-tight">
          {user?.org_name || user?.org_id}
        </span>
        <UserMenu />
      </header>
      {locked && <WorkerLockedNote />}
      <main className="flex flex-1 flex-col overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
