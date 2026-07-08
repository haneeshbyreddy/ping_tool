import { Navigate, Outlet } from "react-router-dom"
import { useAuth } from "@/hooks/use-auth"
import { useEventStream } from "@/hooks/use-event-stream"

export function RequireAuth() {
  const { user, isLoading, scopeOrg } = useAuth()

  useEventStream(scopeOrg)

  if (isLoading) {
    return <div className="flex min-h-svh items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  return <Outlet />
}
