import {
  LayoutDashboard, Network, Settings, Terminal, Building2, Gauge, Map, ServerCog,
  TriangleAlert, MapPinPlus, BookUser, Siren,
  type LucideIcon,
} from "lucide-react"

export type NavGroup = "monitor" | "infrastructure" | "platform"

export const NAV_GROUPS: { id: NavGroup; label: string }[] = [
  { id: "monitor", label: "Monitor" },
  { id: "infrastructure", label: "Infrastructure" },
  { id: "platform", label: "Platform" },
]

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon

  mobile: boolean

  group?: NavGroup

  superadminOnly?: boolean

  // The full billing book with phone numbers is the largest PII surface in the
  // product: workers keep the per-subscriber panel, never the enumeration.
  ownerOnly?: boolean

  account?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true, group: "monitor" },
  // The queue lived on Home until 2026-08-15; it moved out so Home could become
  // the visual overview, and the nav badge (app-shell) is what keeps it
  // unmissable. mobile stays false — the tab bar is at its six-destination
  // ceiling — so the More button carries the urgency dot instead.
  { to: "/triage", label: "Triage", icon: Siren, mobile: false, group: "monitor" },
  { to: "/topology", label: "Network", icon: Network, mobile: true, group: "infrastructure" },
  { to: "/map", label: "Map", icon: Map, mobile: true, group: "infrastructure" },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false, account: true },
  { to: "/issues", label: "Issues", icon: TriangleAlert, mobile: true, group: "monitor" },
  { to: "/customers", label: "Customers", icon: BookUser, mobile: false, group: "monitor", ownerOnly: true },
  { to: "/survey", label: "Survey", icon: MapPinPlus, mobile: true, group: "infrastructure" },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false, group: "monitor" },
  { to: "/overview", label: "Overview", icon: Gauge, mobile: false, superadminOnly: true, group: "platform" },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true, group: "platform" },
  { to: "/platform", label: "Platform settings", icon: ServerCog, mobile: false, superadminOnly: true, group: "platform" },
]

export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
