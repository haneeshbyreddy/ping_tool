// What the secondary pane may hold, and the router that holds it.
//
// The vocabulary is DERIVED from NAV_ITEMS rather than listed again: the split
// menu offering a destination the sidebar doesn't (or missing one it does) is
// the kind of drift nobody notices until an operator asks why Issues can be
// opened in one place and not the other. Adding a destination therefore costs a
// row in nav-items.ts plus an element below — and a destination with no element
// simply doesn't appear, rather than rendering an empty pane.
import { useEffect, type ReactElement, type ReactNode } from "react"
import {
  MemoryRouter, Navigate, Route, Routes, useLocation,
  UNSAFE_LocationContext, UNSAFE_NavigationContext, UNSAFE_RouteContext,
} from "react-router-dom"
import type { LucideIcon } from "lucide-react"
import { NAV_ITEMS } from "./nav-items"
import { useSplit } from "@/hooks/use-split-view"
import { HomePage } from "@/routes/home-page"
import { TopologyPage } from "@/routes/topology-page"
import { MapPage } from "@/routes/map-page"
import { IssuesPage } from "@/routes/issues-page"
import { LogsPage } from "@/routes/logs-page"
import { SurveyPage } from "@/routes/survey-page"
import { SettingsPage } from "@/routes/settings-page"
import { AccountPage } from "@/routes/account-page"
import { OrganizationsPage } from "@/routes/organizations-page"
import { OverviewPage } from "@/routes/overview-page"
import { PlatformPage } from "@/routes/platform-page"

/** Path → element. Keyed on the same strings NAV_ITEMS uses. */
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
  /** Mirrors NAV_ITEMS: account-scoped config, hidden from workers exactly as
   *  the sidebar and the mobile "More" sheet hide it. */
  account?: boolean
}

export const PANE_VIEWS: PaneView[] = NAV_ITEMS.filter((i) => i.to in ELEMENTS).map(
  ({ to, label, icon, superadminOnly, account }) => ({ to, label, icon, superadminOnly, account }),
)

/** The views this session may put in a pane — same two filters the sidebar
 *  applies, so a pane can never reach a page its own nav refuses to show. */
export function paneViewsFor(opts: { isSuperadmin: boolean; isWorker: boolean }): PaneView[] {
  return PANE_VIEWS.filter(
    (v) => (!v.superadminOnly || opts.isSuperadmin) && !(v.account && opts.isWorker),
  )
}

/** Label for an arbitrary stored path — matched on the leading segment so
 *  `/issues?kind=port_down` and `/settings/monitoring` still name themselves.
 *  Longest match wins, or "/" would claim every path there is. */
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

/** Keeps the provider's persisted path in step with where this pane's own
 *  router has navigated to. Inside the MemoryRouter by necessity — that router
 *  is invisible from the app's HashRouter above it. */
function PaneLocationSync() {
  const { pathname, search } = useLocation()
  const { reportPaneLocation } = useSplit()
  useEffect(() => {
    reportPaneLocation(pathname + search)
  }, [pathname, search, reportPaneLocation])
  return null
}

/** Hides the app's own router from the subtree below, so a second one may mount
 *  inside it.
 *
 *  react-router asserts `!useInRouterContext()` in `<Router>` and that check
 *  reads exactly ONE thing — `LocationContext != null` (react-router 7.18,
 *  `chunk-*.mjs`) — so nulling it is what makes the nested MemoryRouter legal.
 *  This is a deliberate defeat of a guardrail, and the guardrail is right about
 *  the case it was written for: two routers fighting over ONE address bar. Here
 *  the second router owns no address at all (see `use-split-view.tsx`), which is
 *  the whole reason a MemoryRouter is the right shape for a pane.
 *
 *  All three contexts are reset, not just the one the assert reads:
 *  - `LocationContext` / `NavigationContext` — MemoryRouter supplies its own
 *    immediately, but leaving the outer pair visible for even one render means
 *    a hook could resolve against the wrong router.
 *  - `RouteContext` — the one with teeth. `<Routes>` resolves its children
 *    RELATIVE to the parent matches it finds, and the outer tree has already
 *    matched `RequireAuth` → `AppShell` → the current page. Left in place, every
 *    path in the pane would be resolved under whatever the shell had matched,
 *    which is not an error — it is a pane that silently renders the wrong page.
 *
 *  The `UNSAFE_` prefix marks these as react-router internals with no
 *  compatibility promise, so this is the one place in the app that may touch
 *  them: a react-router upgrade needs the assert above re-read, and nothing else
 *  here changes. */
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

/** The secondary pane's router.
 *
 *  `initialEntries` is read once at mount, so switching views is a REMOUNT
 *  keyed on the provider's `epoch` (done by the caller) — never on the path,
 *  which the pane changes itself every time it navigates. */
export function PaneRouter({ entry }: { entry: string }) {
  return (
    <OutsideRouter>
    <MemoryRouter initialEntries={[entry]}>
      <PaneLocationSync />
      <Routes>
        {Object.entries(ELEMENTS).map(([path, element]) => (
          <Route key={path} path={path} element={element} />
        ))}
        {/* Sections are addressable in the shell, so they are here too — a pane
            parked on Settings → Monitoring should come back to it. */}
        <Route path="/settings/:section" element={<SettingsPage />} />
        {/* An unknown path (an older stored value, a link into a route this
            pane doesn't carry) lands on Home rather than rendering blank: an
            empty pane reads as a broken feature, not as a bad address. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </MemoryRouter>
    </OutsideRouter>
  )
}
