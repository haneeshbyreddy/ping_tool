import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { authApi } from "@/lib/api"
import type { User } from "@/lib/types"

const SCOPE_STORAGE_KEY = "wisp-central-org-scope"

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  canWrite: boolean
  // The org a request should be scoped to: a superadmin picks one via the org
  // switcher (persisted across reloads); an org user is always pinned to their own
  // org server-side, so this mirrors that rather than letting them pick.
  scopeOrg: string | null
  setScopeOrg: (org: string | null) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const meQuery = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    retry: false,
    staleTime: Infinity,
  })
  const [superadminScope, setSuperadminScope] = useState<string | null>(
    () => localStorage.getItem(SCOPE_STORAGE_KEY),
  )

  useEffect(() => {
    const handler = () => queryClient.setQueryData(["me"], undefined)
    window.addEventListener("wisp:unauthorized", handler)
    return () => window.removeEventListener("wisp:unauthorized", handler)
  }, [queryClient])

  const user = meQuery.data?.user ?? null

  const login = async (username: string, password: string) => {
    const data = await authApi.login(username, password)
    queryClient.setQueryData(["me"], data)
  }

  const logout = async () => {
    await authApi.logout()
    // Order matters: clear() removes every cached query (including "me"), and the
    // AuthProvider's still-mounted "me" observer reacts to that removal by refetching
    // it immediately — racing the sign-out and leaving the shell on the last page (or
    // flashing the loading screen) until a manual reload. Writing "me" back to `null`
    // (NOT `undefined` — TanStack Query's setQueryData treats an `undefined` result as
    // a no-op, so it silently leaves the query removed instead of settling it) makes
    // this the query's final, fresh (staleTime: Infinity) state, so no refetch fires
    // and RequireAuth sees user=null on the very next render.
    queryClient.clear()
    queryClient.setQueryData(["me"], null)
    setSuperadminScope(null)
    localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  const setScopeOrg = (org: string | null) => {
    setSuperadminScope(org)
    if (org) localStorage.setItem(SCOPE_STORAGE_KEY, org)
    else localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  // Org users are pinned server-side to their own org regardless of what's asked
  // for — mirror that here so the UI never shows a picker they can't actually use.
  const scopeOrg = user ? (user.is_superadmin ? superadminScope : user.org_id) : null

  const value: AuthContextValue = {
    user,
    isLoading: meQuery.isLoading,
    login,
    logout,
    canWrite: !!user && (user.is_superadmin || user.role === "owner"),
    scopeOrg,
    setScopeOrg,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within AuthProvider")
  return ctx
}
