import { useNavigate } from "react-router-dom"
import { LogOut, Moon, Receipt, Settings, Sun, UserRound, ChevronsUpDown } from "lucide-react"
import { useState } from "react"
import { useAuth } from "@/hooks/use-auth"
import { applyTheme, getStoredTheme, type ThemeMode } from "@/lib/theme"
import type { BillingInfo } from "@/lib/types"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

// `billing` is not rendered here — it is the shell's answer to "is there an org
// whose bill this is", which is false for a superadmin sitting in All orgs.
//
// This used to read "the amount belongs on /billing, once, where the ledger
// explains it", and that was reversed on purpose (2026-08-17): the running
// figure now lives in the top bar as `BillTape`, because postpaid billing that
// only ever announces itself when an invoice is already late is a surprise,
// and a meter you can see every day is not. The reasoning behind the old rule
// survives HERE though — this menu still shows no amount. Two figures in the
// same chrome, one of them stale by a poll, is how a number stops being
// trusted; the tape is the one place the chrome quotes money.
export function AccountMenu({ billing }: { billing?: BillingInfo }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<ThemeMode>(getStoredTheme())
  if (!user) return null

  const role = user.is_superadmin ? "Superadmin" : user.role
  const isWorker = !user.is_superadmin && user.role === "worker"
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
              {role}
            </span>
          </span>
          <ChevronsUpDown className="size-3.5 shrink-0 text-faint-foreground group-data-[collapsible=icon]:hidden" />
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" align="start" sideOffset={8} className="w-60">
        <DropdownMenuLabel className="font-normal">
          <div className="truncate text-xs font-medium">{user.username}</div>
          <div className="truncate text-2xs text-faint-foreground">
            {org ? `${org} · ${role}` : role}
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
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
        {/* The only path to /billing on desktop: the nav item is account-scoped,
            so it lives here and in the mobile More menu, never in the rail. */}
        {!isWorker && billing && (
          <DropdownMenuItem onClick={() => navigate("/billing")}>
            <Receipt />
            Billing
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

export function shortcutHint(): string {
  return navigator.platform.includes("Mac") ? "⌘," : "Ctrl ,"
}
