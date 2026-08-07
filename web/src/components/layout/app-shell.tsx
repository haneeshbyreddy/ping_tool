import { useEffect, useRef } from "react"
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { MoreHorizontal, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { useIsMobile } from "@/hooks/use-mobile"
import { billingApi } from "@/lib/api"
import { BillingBanner, BillingLock, BillingLockedNote } from "@/components/billing-lock"
import { NAV_ITEMS, MORE_ITEMS, NAV_GROUPS } from "./nav-items"
import { useSplit } from "@/hooks/use-split-view"
import { SplitControl, SplitView } from "./split-view"
import { AlarmChips } from "./alarm-chips"
import { WorkspaceRow } from "./workspace-row"
import { UserMenu } from "./user-menu"
import { AccountMenu } from "./account-menu"
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel,
  SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"

export function AppShell() {
  const { user, scopeOrg } = useAuth()
  const split = useSplit()
  // Read-only worker: no Settings, no billing. On a phone it gets the survey
  // screen and nothing else (see FieldShell at the foot of this file).
  const isWorker = !!user && !user.is_superadmin && user.role === "worker"
  const isMobile = useIsMobile()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const navItems = NAV_ITEMS.filter((i) => !i.superadminOnly || user?.is_superadmin)
  // Settings is owner+ only (a worker hitting it is bounced to Home), so it's
  // kept out of the worker's mobile "More" sheet just as the sidebar/AccountMenu
  // keep it off desktop — no dead entry now that workers get the full shell.
  const moreItems = MORE_ITEMS.filter(
    (i) => (!i.superadminOnly || user?.is_superadmin) && !(i.account && isWorker),
  )
  // Sidebar shows destinations; account-scoped entries move to the AccountMenu
  // at its foot. The mobile "More" sheet keeps them — there is no sidebar there.
  const sidebarItems = navItems.filter((i) => !i.account)

  const isNavActive = (to: string) => (to === "/" ? pathname === "/" : pathname.startsWith(to))

  // The top bar no longer carries a search of its own: it hands off to the
  // Network page's box, which is the one search in the product (devices AND
  // ONUs, with the tree right there to land in). A fresh nav state each time so
  // clicking it while already on /topology still re-focuses the field.
  const goToSearch = () =>
    navigate("/topology", { state: { focusSearch: Date.now() } })

  // ⌘K / Ctrl+K does the same thing the button does — the button advertises the
  // shortcut, so the two must not diverge. Ignored while typing, or the chord
  // would yank a user out of whatever field they are in.
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

  // ⌘, / Ctrl+, → Settings. The account menu advertises this shortcut, so it has
  // to exist; ignore it while the user is typing, or "," in a device-search box
  // would navigate away mid-word.
  useEffect(() => {
    // A worker has no Settings page, so the shortcut it advertises is gone too.
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

  // On unlock, every query that 402'd while locked is stale — refetch the lot.
  const wasLocked = useRef(false)
  useEffect(() => {
    if (wasLocked.current && billing && !billing.locked) queryClient.invalidateQueries()
    wasLocked.current = !!billing?.locked
  }, [billing, queryClient])

  if (billing?.locked && user && !user.is_superadmin) {
    return <BillingLock billing={billing} org={billingOrg} />
  }

  // A WORKER ON A PHONE GETS ONE SCREEN. Operator's call (2026-07-28): the field
  // handset is a survey tool, not a shrunken NOC, and every other destination on
  // it was read-only anyway — a tab that can only be looked at is a tab that
  // costs a thumb-press to find out. Deliberately reintroduces a viewport fork
  // (retired once because a desktop RESIZE changed the whole app), which is
  // acceptable here for one reason: nobody resizes a phone, and the same worker
  // on a laptop still gets the full read-only shell. Placed AFTER the billing
  // lock so a locked org still shows its lock screen.
  if (isWorker && isMobile) {
    return <FieldShell />
  }

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" className="hidden md:flex">
        <SidebarHeader className="pb-3">
          <WorkspaceRow />
        </SidebarHeader>
        <SidebarContent>
          {/* Grouped into sections (Monitor / Infrastructure / Platform). A group
              with no visible items — e.g. Platform for a non-superadmin — is not
              rendered, so an owner sees two sections, not an empty third. */}
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
                              // Active state is a wash + a 2px inset rail. The rail
                              // carries the ACCENT, which reverses an earlier call
                              // ("accent stays reserved… so status colors keep being
                              // the loudest thing on screen") on a measurement that
                              // says the opposite: --foreground on the sidebar is
                              // 14.16:1 dark / 13.38:1 light, --primary is 7.40 /
                              // 3.95 — so the rail this replaces was TWICE as loud
                              // as the accent, and the change makes it quieter, not
                              // louder. It also gives the accent one consistent job
                              // (focus / selection / where-you-are) instead of
                              // spending --foreground weight on chrome.
                              isNavActive(item.to) &&
                                "bg-foreground/[0.07] text-foreground shadow-[inset_2px_0_0_var(--primary)] hover:bg-foreground/[0.07] hover:text-foreground",
                            )}
                          >
                            <item.icon className="size-4" />
                            <span>{item.label}</span>
                          </NavLink>
                        </SidebarMenuButton>
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

      {/* A SPLIT NEEDS A DEFINITE HEIGHT, and this is the only place that can
          give it one. The shell's column is `min-h-svh` — a FLOOR, so it grows
          to its content — and `flex: 1 1 0%` does NOT stop a flex item
          contributing its content height to that intrinsic measurement (only to
          its minimum during layout). With a page that sizes itself in `svh`
          that is harmless, but a pane sizes its map with `height: 100%`, which
          against an auto-height ancestor resolves to `auto`, i.e. back to
          content: the two definitions chase each other and the whole shell
          settled ~900px taller than the window, scrolling the panes off the
          bottom. `h-svh` turns the floor into an exact height and the
          percentage chain resolves. Applied ONLY while split, so the ordinary
          layout keeps growing exactly as it always has. */}
      <SidebarInset className={cn(split.view && "h-svh overflow-hidden")}>
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b bg-background px-3 md:px-6">
          <SidebarTrigger className="hidden text-muted-foreground md:flex" />
          {/* No sidebar below md, so the workspace row (and, for a superadmin,
              the switcher it carries) rides the mobile header instead. */}
          <div className="md:hidden">
            <WorkspaceRow variant="topbar" />
          </div>
          <div className="flex-1" />
          <AlarmChips />
          {/* Shaped like the input it points at, but kept a BUTTON: a text field
              you cannot type into is a lie the first keystroke exposes. It does
              not search — it takes you to the Network page's search box. */}
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
          {/* Splitting the window is a statement about the WORKSPACE, not about
              the org or the account, so it rides the top bar beside search
              rather than the sidebar (which lists destinations) or the account
              menu (which is per-person config). It hides itself outright on a
              window too small for either layout. */}
          <SplitControl />
          {/* Mobile only: the sidebar (and with it the account menu) is hidden
              below md, so this is the only identity surface there. On desktop it
              would be a second door to the same three actions. */}
          <span className="md:hidden">
            <UserMenu />
          </span>
        </header>

        {/* Billing is owner business — a worker never sees the runway banner
            (the LOCK screen above still shows for every member, by design). */}
        {billing && billingOrg && !isWorker && (
          user?.is_superadmin
            ? <BillingLockedNote billing={billing} />
            : <BillingBanner billing={billing} org={billingOrg} />
        )}

        <ShellMain />

        {/* Mobile bottom tab bar — More folds Settings/Logs, which get their own
            sidebar entries on desktop. */}
        <nav className="fixed inset-x-0 bottom-0 z-30 flex items-stretch justify-around border-t bg-sidebar px-1 pb-[env(safe-area-inset-bottom)] md:hidden">
          {navItems.filter((i) => i.mobile).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  // flex-1/min-w-0, not a fixed min-width: the bar carries six
                  // destinations once Survey is in it, and 6 × 3.5rem overflows
                  // a 320px handset. Dividing the width keeps the bar correct
                  // for whatever the next entry brings.
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
              <button className="flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2 text-2xs font-medium text-faint-foreground">
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
    </SidebarProvider>
  )
}

/** The page area.
 *
 *  With no split open this is the markup it has always been — `flex-1
 *  overflow-y-auto`, one `<Outlet/>`, no wrapper — so the ordinary single-page
 *  case cannot regress on a feature it isn't using. A split swaps the scroll
 *  container for a fixed-height box, because each PANE scrolls itself: leaving
 *  the scroll here would let the whole two-pane layout slide as one. */
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

/** The field handset: one screen, no navigation.
 *
 *  Every path a worker can reach on a phone lands on /survey — including a deep
 *  link out of a WhatsApp page, which is why the redirect is here rather than a
 *  route guard. There is no bottom bar and no sidebar, so the only chrome is the
 *  org name and the account menu (logout has to stay reachable).
 *
 *  The redirect is `replace` so the back button doesn't walk into the route that
 *  just bounced. */
function FieldShell() {
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
      <main className="flex flex-1 flex-col overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
