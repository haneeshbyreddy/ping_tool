import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { HashRouter, Routes, Route } from "react-router-dom"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ErrorBoundary } from "@/components/error-boundary"
import { AuthProvider } from "@/hooks/use-auth"
import { RequireAuth } from "@/components/layout/require-auth"
import { AppShell } from "@/components/layout/app-shell"
import { LoginPage } from "@/routes/login-page"
import { HomePage } from "@/routes/home-page"
import { TopologyPage } from "@/routes/topology-page"
import { EdgeNodesPage } from "@/routes/edge-nodes-page"
import { TeamPage } from "@/routes/team-page"
import { SettingsPage } from "@/routes/settings-page"
import { LogsPage } from "@/routes/logs-page"
import { OrganizationsPage } from "@/routes/organizations-page"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
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
                    <Route path="nodes" element={<EdgeNodesPage />} />
                    <Route path="team" element={<TeamPage />} />
                    <Route path="settings" element={<SettingsPage />} />
                    <Route path="logs" element={<LogsPage />} />
                    <Route path="orgs" element={<OrganizationsPage />} />
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
