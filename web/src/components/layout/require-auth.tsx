import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAuth } from "@/hooks/use-auth"
import { useEventStream } from "@/hooks/use-event-stream"

export function RequireAuth() {
  const { user, isLoading, scopeOrg } = useAuth()
  const location = useLocation()

  useEventStream(scopeOrg)

  if (isLoading) {
    return <div className="flex min-h-svh items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (!user) {
    // Remember where the session died so login can drop the user back there.
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <Outlet />
}
