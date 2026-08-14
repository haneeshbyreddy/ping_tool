import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { HashRouter, Routes, Route, Navigate } from "react-router-dom"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ErrorBoundary } from "@/components/error-boundary"
import { AuthProvider } from "@/hooks/use-auth"
import { SplitProvider } from "@/hooks/use-split-view"
import { RequireAuth } from "@/components/layout/require-auth"
import { AppShell } from "@/components/layout/app-shell"
import { LoginPage } from "@/routes/login-page"
import { HomePage } from "@/routes/home-page"
import { TopologyPage } from "@/routes/topology-page"
import { MapPage } from "@/routes/map-page"
import { SettingsPage } from "@/routes/settings-page"
import { AccountPage } from "@/routes/account-page"
import { IssuesPage } from "@/routes/issues-page"
import { CustomersPage } from "@/routes/customers-page"
import { SurveyPage } from "@/routes/survey-page"
import { LogsPage } from "@/routes/logs-page"
import { OrganizationsPage } from "@/routes/organizations-page"
import { OverviewPage } from "@/routes/overview-page"
import { PlatformPage } from "@/routes/platform-page"

const queryClient = new QueryClient({
  defaultOptions: {
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
              <SplitProvider>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route element={<RequireAuth />}>
                  <Route element={<AppShell />}>
                    <Route index element={<HomePage />} />
                    <Route path="topology" element={<TopologyPage />} />
                    <Route path="map" element={<MapPage />} />
                    <Route path="nodes" element={<Navigate to="/topology" replace />} />
                    <Route path="settings" element={<SettingsPage />} />
                    <Route path="settings/:section" element={<SettingsPage />} />
                    <Route path="account" element={<AccountPage />} />
                    <Route path="issues" element={<IssuesPage />} />
                    <Route path="customers" element={<CustomersPage />} />
                    <Route path="survey" element={<SurveyPage />} />
                    <Route path="logs" element={<LogsPage />} />
                    <Route path="orgs" element={<OrganizationsPage />} />
                    <Route path="overview" element={<OverviewPage />} />
                    <Route path="platform" element={<PlatformPage />} />
                  </Route>
                </Route>
              </Routes>
              </SplitProvider>
            </HashRouter>
            <Toaster />
          </TooltipProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

export default App
