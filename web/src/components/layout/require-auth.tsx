import { Navigate, Outlet } from "react-router-dom"
import { useAuth } from "@/hooks/use-auth"
import { useEventStream } from "@/hooks/use-event-stream"

export function RequireAuth() {
  const { user, isLoading, scopeTenant } = useAuth()
  // Live push (server.py's SSE /api/events) only makes sense once we know who's asking
  // and what tenant to scope it to — see central/engine.py's docs on why this rides
  // the whole authenticated shell rather than each page reconnecting separately.
  useEventStream(scopeTenant)

  if (isLoading) {
    return <div className="flex min-h-svh items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  return <Outlet />
}
