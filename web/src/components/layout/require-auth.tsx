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
  // One login, one application. A worker gets the SAME responsive shell as
  // everyone else, at every viewport — read-only, because writes are gated by
  // canWrite=false and, authoritatively, by the server's _WORKER_GET/_WORKER_POST
  // allowlists (a superadmin, org_id IS NULL with a meaningless `role`, is never
  // a worker — identity before role, exactly like server.py:_worker_blocked).
  //
  // The old 768px fork swapped a worker into WorkerPage on narrow screens, so a
  // resize changed the whole app. That's retired here; WorkerPage's file stays
  // until the Incidents page replaces it (Release Two).
  return <Outlet />
}
