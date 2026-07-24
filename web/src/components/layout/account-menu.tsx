import { useNavigate } from "react-router-dom"
import { CreditCard, LogOut, Moon, Settings, Sun, UserRound, ChevronsUpDown } from "lucide-react"
import { useState } from "react"
import { useAuth } from "@/hooks/use-auth"
import { applyTheme, getStoredTheme, type ThemeMode } from "@/lib/theme"
import type { BillingInfo } from "@/lib/types"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

/** Account menu — the sidebar's foot row, and the only place identity-scoped
 *  actions live.
 *
 *  It replaces a top-level "Settings" nav entry on purpose. Settings is not a
 *  *place in the network* the way Home/Network/Map/Logs are; putting it in the
 *  same list implied it was, and cost a permanent slot in the primary nav for
 *  something reached a few times a month. Folding it under "who am I" also gives
 *  the plan, the theme and sign-out one obvious home instead of three.
 */
export function AccountMenu({ billing }: { billing?: BillingInfo }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<ThemeMode>(getStoredTheme())
  if (!user) return null

  const role = user.is_superadmin ? "Superadmin" : user.role
  // A read-only worker doesn't see Settings or billing at all (the config surface
  // is owner-only), so its account menu is just theme + sign out.
  const isWorker = !user.is_superadmin && user.role === "worker"
  const plan = !isWorker && billing ? (billing.plans[billing.plan]?.label ?? billing.plan) : null
  const org = user.is_superadmin ? null : user.org_name || user.org_id

  const toggleTheme = () => {
    const next = mode === "dark" ? "light" : "dark"
    applyTheme(next)
    setMode(next)
  }

  const onLogout = async () => {
    await logout()
    navigate("/login", { replace: true })
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors hover:bg-foreground/5 aria-expanded:bg-foreground/5 group-data-[collapsible=icon]:border-transparent group-data-[collapsible=icon]:px-0"
          aria-label="Account menu">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full border bg-accent text-2xs font-semibold text-muted-foreground">
            {user.username.slice(0, 2).toUpperCase()}
          </span>
          <span className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
            <span className="block truncate text-xs font-medium text-foreground">{user.username}</span>
            <span className="block truncate text-2xs text-faint-foreground capitalize">
              {role}{plan ? ` · ${plan}` : ""}
            </span>
          </span>
          <ChevronsUpDown className="size-3.5 shrink-0 text-faint-foreground group-data-[collapsible=icon]:hidden" />
        </button>
      </DropdownMenuTrigger>

      {/* side="top": the trigger sits at the bottom of the viewport, so the menu
          has to grow upward or it opens off-screen. */}
      <DropdownMenuContent side="top" align="start" sideOffset={8} className="w-60">
        <DropdownMenuLabel className="font-normal">
          <div className="truncate text-xs font-medium">{user.username}</div>
          <div className="truncate text-2xs text-faint-foreground">
            {org ? `${org} · ${role}` : role}
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {/* Personal settings — every role, including a worker (its only
            password/2FA/WhatsApp surface). Distinct from the org-config Settings
            page below, which stays owner+. */}
        <DropdownMenuItem onClick={() => navigate("/account")}>
          <UserRound />
          Your account
        </DropdownMenuItem>
        {!isWorker && (
          <DropdownMenuItem onClick={() => navigate("/settings")}>
            <Settings />
            Settings
            <DropdownMenuShortcut>{shortcutHint()}</DropdownMenuShortcut>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onClick={toggleTheme}>
          {mode === "dark" ? <Sun /> : <Moon />}
          {mode === "dark" ? "Light mode" : "Dark mode"}
        </DropdownMenuItem>
        {/* Only an org has a plan — a superadmin session is not billed, so the
            entry would lead to a section that renders nothing for them; a worker
            doesn't see billing at all. */}
        {!isWorker && billing && (
          <DropdownMenuItem onClick={() => navigate("/settings/billing")}>
            <CreditCard />
            Plan &amp; billing
            <DropdownMenuShortcut className="tracking-normal capitalize">{plan}</DropdownMenuShortcut>
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onClick={() => onLogout()}>
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** The hint must match what the handler in app-shell actually binds, or it is a
 *  lie the first keypress exposes. */
export function shortcutHint(): string {
  return navigator.platform.includes("Mac") ? "⌘," : "Ctrl ,"
}
