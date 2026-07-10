import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { HashRouter, Routes, Route, Navigate } from "react-router-dom"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ErrorBoundary } from "@/components/error-boundary"
import { AuthProvider } from "@/hooks/use-auth"
import { RequireAuth } from "@/components/layout/require-auth"
import { AppShell } from "@/components/layout/app-shell"
import { LoginPage } from "@/routes/login-page"
import { HomePage } from "@/routes/home-page"
import { TopologyPage } from "@/routes/topology-page"
import { MapPage } from "@/routes/map-page"
import { TeamPage } from "@/routes/team-page"
import { SettingsPage } from "@/routes/settings-page"
import { LogsPage } from "@/routes/logs-page"
import { OrganizationsPage } from "@/routes/organizations-page"
import { OverviewPage } from "@/routes/overview-page"

const queryClient = new QueryClient({
  defaultOptions: {
    // Refetch on focus is the safety net for backgrounded tabs: mobile browsers
    // freeze the page (killing the SSE stream and pausing poll timers), so on
    // return the last paint is stale. react-query's focus manager fires on
    // visibilitychange — this resyncs every live query the moment the tab is
    // visible again, instead of showing a stale red/green snapshot until a manual
    // refresh. Pairs with the SSE reopen in use-event-stream.ts.
    queries: { retry: 1, refetchOnWindowFocus: true },
  },
})

function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <TooltipProvider>
            <HashRouter>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route element={<RequireAuth />}>
                  <Route element={<AppShell />}>
                    <Route index element={<HomePage />} />
                    <Route path="topology" element={<TopologyPage />} />
                    <Route path="map" element={<MapPage />} />
                    {/* Probes merged into the Network page — keep old bookmarks working */}
                    <Route path="nodes" element={<Navigate to="/topology" replace />} />
                    <Route path="team" element={<TeamPage />} />
                    <Route path="settings" element={<SettingsPage />} />
                    <Route path="logs" element={<LogsPage />} />
                    <Route path="orgs" element={<OrganizationsPage />} />
                    <Route path="overview" element={<OverviewPage />} />
                  </Route>
                </Route>
              </Routes>
            </HashRouter>
            <Toaster />
          </TooltipProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

export default App
