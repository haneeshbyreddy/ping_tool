import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { LogOut, Moon, Sun } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { applyTheme, getStoredTheme, type ThemeMode } from "@/lib/theme"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

function initials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}

export function UserMenu() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<ThemeMode>(getStoredTheme())
  if (!user) return null

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
        <button className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground cursor-pointer">
          <Avatar className="size-8">
            <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
              {initials(user.username)}
            </AvatarFallback>
          </Avatar>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>
          <div className="truncate">{user.username}</div>
          <div className="text-xs font-normal text-muted-foreground">
            {user.is_superadmin ? "Superadmin" : `${user.org_name || user.org_id} · ${user.role}`}
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={toggleTheme}>
          {mode === "dark" ? <Sun /> : <Moon />}
          {mode === "dark" ? "Light mode" : "Dark mode"}
        </DropdownMenuItem>
        <DropdownMenuItem variant="destructive" onClick={() => onLogout()}>
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
