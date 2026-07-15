import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { authApi } from "@/lib/api"
import { SESSION_EXPIRED_KEY } from "@/lib/session"
import type { MeResponse, User } from "@/lib/types"

const SCOPE_STORAGE_KEY = "wisp-central-org-scope"

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  login: (username: string, password: string, remember?: boolean) => Promise<void>
  logout: () => Promise<void>
  canWrite: boolean

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
    // Re-check the session whenever the tab regains focus so a restored/backgrounded
    // tab whose session lapsed lands on the login page, not a stale dashboard —
    // "always" because staleTime:Infinity would otherwise skip the refetch.
    refetchOnWindowFocus: "always",
  })
  const [superadminScope, setSuperadminScope] = useState<string | null>(
    () => localStorage.getItem(SCOPE_STORAGE_KEY),
  )

  useEffect(() => {
    const handler = () => {
      // Only a 401 that kills a live session is an "expiry" — a cold visit
      // hitting /api/me unauthenticated is just the normal login flow.
      if (queryClient.getQueryData<MeResponse>(["me"])?.user) {
        sessionStorage.setItem(SESSION_EXPIRED_KEY, "1")
      }
      queryClient.setQueryData(["me"], undefined)
    }
    window.addEventListener("wisp:unauthorized", handler)
    return () => window.removeEventListener("wisp:unauthorized", handler)
  }, [queryClient])

  const user = meQuery.data?.user ?? null

  const login = async (username: string, password: string, remember = false) => {
    const data = await authApi.login(username, password, remember)
    queryClient.setQueryData(["me"], data)
  }

  const logout = async () => {
    await authApi.logout()

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
