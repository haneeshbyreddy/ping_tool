import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { authApi } from "@/lib/api"
import type { User } from "@/lib/types"

const SCOPE_STORAGE_KEY = "wisp-central-tenant-scope"

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  canWrite: boolean
  // The tenant a request should be scoped to: a superadmin picks one via the org
  // switcher (persisted across reloads); an org user is always pinned to their own
  // tenant server-side, so this mirrors that rather than letting them pick.
  scopeTenant: string | null
  setScopeTenant: (tenant: string | null) => void
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
    // Order matters: clear() removes every cached query (including "me"), and an
    // active observer whose query was just removed refetches it — since that refetch
    // would still see the pre-clear() cookie state for a moment, it raced with the
    // sign-out and left the shell rendering the last page until a manual reload.
    // Writing "me" last makes it the query's final settled state instead of a
    // refetch target, so RequireAuth sees user=null on the very next render.
    queryClient.clear()
    queryClient.setQueryData(["me"], undefined)
    setSuperadminScope(null)
    localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  const setScopeTenant = (tenant: string | null) => {
    setSuperadminScope(tenant)
    if (tenant) localStorage.setItem(SCOPE_STORAGE_KEY, tenant)
    else localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  // Org users are pinned server-side to their own tenant regardless of what's asked
  // for — mirror that here so the UI never shows a picker they can't actually use.
  const scopeTenant = user ? (user.is_superadmin ? superadminScope : user.tenant_id) : null

  const value: AuthContextValue = {
    user,
    isLoading: meQuery.isLoading,
    login,
    logout,
    canWrite: !!user && (user.is_superadmin || user.role === "owner"),
    scopeTenant,
    setScopeTenant,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within AuthProvider")
  return ctx
}
