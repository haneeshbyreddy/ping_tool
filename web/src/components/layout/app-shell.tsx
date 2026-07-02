import { useState } from "react"
import { NavLink, Outlet } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi } from "@/lib/api"
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
  // Superadmins operate across every org (the OrgSwitcher next to this is their org
  // picker), so they keep the platform brand. An org user is always pinned to one
  // org, so their own org's name is more useful here than the generic platform name.
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
      <span className="truncate whitespace-nowrap font-serif text-lg italic font-semibold tracking-tight text-foreground">
        {orgName || "WISP Central"}
      </span>
    </NavLink>
  )
}

export function AppShell() {
  const [searchOpen, setSearchOpen] = useState(false)
  const { user } = useAuth()
  const navItems = NAV_ITEMS.filter((i) => !i.superadminOnly || user?.is_superadmin)
  const moreItems = MORE_ITEMS.filter((i) => !i.superadminOnly || user?.is_superadmin)

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
                        className={({ isActive }) =>
                          cn(isActive && "bg-sidebar-accent text-sidebar-accent-foreground")
                        }
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
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b bg-background px-3 md:px-4">
          <SidebarTrigger className="hidden md:flex" />
          <div className="md:hidden">
            <Brand />
          </div>
          <OrgSwitcher />
          <div className="flex-1" />
          <AlarmChips />
          <Button variant="outline" size="icon" className="size-8" aria-label="Search"
            onClick={() => setSearchOpen(true)}>
            <Search className="size-4" />
          </Button>
          <UserMenu />
        </header>

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
                  "flex min-w-14 flex-col items-center gap-0.5 px-2 py-2 text-[10px] font-medium",
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
              <button className="flex min-w-14 flex-col items-center gap-0.5 px-2 py-2 text-[10px] font-medium text-muted-foreground">
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
