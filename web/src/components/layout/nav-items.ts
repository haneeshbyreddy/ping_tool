import {
  LayoutDashboard, Network, Settings, Terminal, Building2, Gauge, Map,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon

  mobile: boolean

  superadminOnly?: boolean

  /** Reached from the sidebar's account menu instead of the primary nav. The
   *  primary nav lists PLACES IN THE NETWORK; account-scoped config isn't one,
   *  and a permanent slot for something opened a few times a month crowds the
   *  four destinations an operator actually lives in. Still listed on mobile,
   *  where the sidebar (and so the account menu) doesn't exist. */
  account?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true },
  { to: "/topology", label: "Network", icon: Network, mobile: true },
  { to: "/map", label: "Map", icon: Map, mobile: true },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false, account: true },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false },
  { to: "/overview", label: "Overview", icon: Gauge, mobile: false, superadminOnly: true },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true },
]

export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
