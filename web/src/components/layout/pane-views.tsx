import { useEffect, type ReactElement, type ReactNode } from "react"
import {
  MemoryRouter, Navigate, Route, Routes, useLocation,
  UNSAFE_LocationContext, UNSAFE_NavigationContext, UNSAFE_RouteContext,
} from "react-router-dom"
import type { LucideIcon } from "lucide-react"
import { NAV_ITEMS } from "./nav-items"
import { useSplit } from "@/hooks/use-split-view"
import { HomePage } from "@/routes/home-page"
import { MapPage, SettingsPage, SurveyPage, TopologyPage } from "@/routes/lazy"
import { IssuesPage } from "@/routes/issues-page"
import { LogsPage } from "@/routes/logs-page"
import { AccountPage } from "@/routes/account-page"
import { OrganizationsPage } from "@/routes/organizations-page"
import { OverviewPage } from "@/routes/overview-page"
import { PlatformPage } from "@/routes/platform-page"

const ELEMENTS: Record<string, ReactElement> = {
  "/": <HomePage />,
  "/topology": <TopologyPage />,
  "/map": <MapPage />,
  "/issues": <IssuesPage />,
  "/survey": <SurveyPage />,
  "/logs": <LogsPage />,
  "/settings": <SettingsPage />,
  "/account": <AccountPage />,
  "/orgs": <OrganizationsPage />,
  "/overview": <OverviewPage />,
  "/platform": <PlatformPage />,
}

export interface PaneView {
  to: string
  label: string
  icon: LucideIcon
  superadminOnly?: boolean
  account?: boolean
}

export const PANE_VIEWS: PaneView[] = NAV_ITEMS.filter((i) => i.to in ELEMENTS).map(
  ({ to, label, icon, superadminOnly, account }) => ({ to, label, icon, superadminOnly, account }),
)

export function paneViewsFor(opts: { isSuperadmin: boolean; isWorker: boolean }): PaneView[] {
  return PANE_VIEWS.filter(
    (v) => (!v.superadminOnly || opts.isSuperadmin) && !(v.account && opts.isWorker),
  )
}

export function paneViewFor(path: string): PaneView | null {
  const clean = path.split("?")[0] || "/"
  let best: PaneView | null = null
  for (const v of PANE_VIEWS) {
    if (v.to === "/" ? clean === "/" : clean.startsWith(v.to)) {
      if (!best || v.to.length > best.to.length) best = v
    }
  }
  return best
}

function PaneLocationSync() {
  const { pathname, search } = useLocation()
  const { reportPaneLocation } = useSplit()
  useEffect(() => {
    reportPaneLocation(pathname + search)
  }, [pathname, search, reportPaneLocation])
  return null
}

function OutsideRouter({ children }: { children: ReactNode }) {
  return (
    <UNSAFE_RouteContext.Provider value={{ outlet: null, matches: [], isDataRoute: false }}>
      <UNSAFE_NavigationContext.Provider value={null as never}>
        <UNSAFE_LocationContext.Provider value={null as never}>
          {children}
        </UNSAFE_LocationContext.Provider>
      </UNSAFE_NavigationContext.Provider>
    </UNSAFE_RouteContext.Provider>
  )
}

export function PaneRouter({ entry }: { entry: string }) {
  return (
    <OutsideRouter>
    <MemoryRouter initialEntries={[entry]}>
      <PaneLocationSync />
      <Routes>
        {Object.entries(ELEMENTS).map(([path, element]) => (
          <Route key={path} path={path} element={element} />
        ))}
        <Route path="/settings/:section" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </MemoryRouter>
    </OutsideRouter>
  )
}
