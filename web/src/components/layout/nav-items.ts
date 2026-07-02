import {
  LayoutDashboard, Network, Radio, Users, Settings, Terminal, Building2,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  // Shown on both the mobile bottom bar and the desktop sidebar (all items) —
  // `mobile: true` marks which ones make the cut for the bottom bar.
  mobile: boolean
  // Cross-org org directory management — only a superadmin has anywhere to use it.
  superadminOnly?: boolean
}

// Triage (the outages queue) lives on Home now, not a standalone page — see
// CLAUDE.md-adjacent notes in home-page.tsx for why a dedicated route was dropped.
export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true },
  { to: "/topology", label: "Topology", icon: Network, mobile: true },
  { to: "/nodes", label: "Probes", icon: Radio, mobile: true },
  { to: "/team", label: "Team", icon: Users, mobile: false },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true },
]

// The mobile bottom bar's last slot is a "More" menu folding in the desktop-only pages
// (Team/Settings/Logs, plus Organizations for superadmins).
export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
