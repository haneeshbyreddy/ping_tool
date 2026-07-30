import {
  LayoutDashboard, Network, Settings, Terminal, Building2, Gauge, Map, ServerCog,
  TriangleAlert, MapPinPlus,
  type LucideIcon,
} from "lucide-react"

/** Sidebar sections. The flat list read as one undifferentiated column of six;
 *  grouping it gives the eye anchors and names the two planes the app actually
 *  has — the org's own network vs. the superadmin's cross-org plane. Array order
 *  is render order (org plane first, Platform last). */
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

  /** Which sidebar section it sits under. Account entries (Settings) live in the
   *  account menu at the sidebar's foot, not the primary nav, so they carry no
   *  group. */
  group?: NavGroup

  superadminOnly?: boolean

  /** Reached from the sidebar's account menu instead of the primary nav. The
   *  primary nav lists PLACES IN THE NETWORK; account-scoped config isn't one,
   *  and a permanent slot for something opened a few times a month crowds the
   *  destinations an operator actually lives in. Still listed on mobile, where
   *  the sidebar (and so the account menu) doesn't exist. */
  account?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Home", icon: LayoutDashboard, mobile: true, group: "monitor" },
  { to: "/topology", label: "Network", icon: Network, mobile: true, group: "infrastructure" },
  { to: "/map", label: "Map", icon: Map, mobile: true, group: "infrastructure" },
  { to: "/settings", label: "Settings", icon: Settings, mobile: false, account: true },
  // Sits directly under Home: the tiles there link into it, and "what is wrong
  // right now" is the second thing an operator opens after the overview. Mobile
  // too — it is the one screen worth carrying to a site visit.
  { to: "/issues", label: "Issues", icon: TriangleAlert, mobile: true, group: "monitor" },
  // Field capture. Mobile-first in the strictest sense — it is the only screen
  // here whose primary user is holding a phone in one hand at a pole, so it
  // takes a mobile slot and sits under Infrastructure, which is what it edits.
  { to: "/survey", label: "Survey", icon: MapPinPlus, mobile: true, group: "infrastructure" },
  { to: "/logs", label: "Logs", icon: Terminal, mobile: false, group: "monitor" },
  { to: "/overview", label: "Overview", icon: Gauge, mobile: false, superadminOnly: true, group: "platform" },
  { to: "/orgs", label: "Organizations", icon: Building2, mobile: false, superadminOnly: true, group: "platform" },
  // Server-wide config (app_settings): appearance, Google Maps key, payments,
  // WhatsApp. Labelled "Platform settings" so it doesn't collide with the
  // "Platform" group name it sits under. Lifted out of the org-scoped Settings
  // page, which is now identical for a superadmin and an owner.
  { to: "/platform", label: "Platform settings", icon: ServerCog, mobile: false, superadminOnly: true, group: "platform" },
]

export const MORE_ITEMS = NAV_ITEMS.filter((i) => !i.mobile)
