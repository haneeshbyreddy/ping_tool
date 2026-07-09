import {
  LayoutDashboard, Network, Users, Settings, Terminal, Building2, Gauge,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon

  mobile: boolean

  superadminOnly?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true },
  { to: "/topology", label: "Network", icon: Network, mobile: true },
  { to: "/team", label: "Team", icon: Users, mobile: true },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false },
  { to: "/overview", label: "Overview", icon: Gauge, mobile: false, superadminOnly: true },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true },
]

export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
