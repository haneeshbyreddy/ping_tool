import {
  LayoutDashboard, TriangleAlert, Network, Radio, Users, Settings, Terminal, Building2,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  // Shown on both the mobile bottom bar (5 slots, mirroring the mockup) and the desktop
  // sidebar (all 7) — `mobile: true` marks which ones make the cut for the bottom bar.
  mobile: boolean
  // Cross-tenant org directory management — only a superadmin has anywhere to use it.
  superadminOnly?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true },
  { to: "/outages", label: "Triage", icon: TriangleAlert, mobile: true },
  { to: "/topology", label: "Topology", icon: Network, mobile: true },
  { to: "/nodes", label: "Probes", icon: Radio, mobile: true },
  { to: "/team", label: "Team", icon: Users, mobile: false },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true },
]

// The mockup's 5th tab is "More" (account/appearance/roster/settings all folded into
// one sheet) — real app has dedicated Team/Settings/Logs pages instead, so the mobile
// bottom bar's 5th slot is a "More" menu that links to those three (plus Organizations,
// filtered to superadmins by the caller).
export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
