import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { authApi, orgsApi } from "@/lib/api"
import { SESSION_EXPIRED_KEY } from "@/lib/session"
import type { MeResponse, User } from "@/lib/types"

const SCOPE_STORAGE_KEY = "wisp-central-org-scope"

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  login: (username: string, password: string, remember?: boolean,
          second?: { totp?: string; recovery?: string }) => Promise<void>
  logout: () => Promise<void>
  canWrite: boolean
  isWorker: boolean

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
    refetchOnWindowFocus: "always",
  })
  const [superadminScope, setSuperadminScope] = useState<string | null>(
    () => localStorage.getItem(SCOPE_STORAGE_KEY),
  )
  const scopeResolved = useRef(false)

  useEffect(() => {
    const handler = () => {
      if (queryClient.getQueryData<MeResponse>(["me"])?.user) {
        sessionStorage.setItem(SESSION_EXPIRED_KEY, "1")
      }
      queryClient.setQueryData(["me"], undefined)
    }
    window.addEventListener("wisp:unauthorized", handler)
    return () => window.removeEventListener("wisp:unauthorized", handler)
  }, [queryClient])

  const user = meQuery.data?.user ?? null

  const login = async (username: string, password: string, remember = false,
                       second?: { totp?: string; recovery?: string }) => {
    const data = await authApi.login(username, password, remember, second)
    queryClient.setQueryData(["me"], data)
  }

  const logout = async () => {
    await authApi.logout()

    queryClient.clear()
    queryClient.setQueryData(["me"], null)
    setSuperadminScope(null)
    localStorage.removeItem(SCOPE_STORAGE_KEY)
    scopeResolved.current = false
  }

  const setScopeOrg = (org: string | null) => {
    setSuperadminScope(org)
    if (org) localStorage.setItem(SCOPE_STORAGE_KEY, org)
    else localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user?.is_superadmin,
  })
  useEffect(() => {
    if (!user?.is_superadmin || scopeResolved.current) return
    if (localStorage.getItem(SCOPE_STORAGE_KEY)) {
      scopeResolved.current = true
      return
    }
    const first = orgsQuery.data?.orgs[0]?.org_id
    if (first) {
      scopeResolved.current = true
      setScopeOrg(first)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, orgsQuery.data])

  const scopeOrg = user ? (user.is_superadmin ? superadminScope : user.org_id) : null

  const value: AuthContextValue = {
    user,
    isLoading: meQuery.isLoading,
    login,
    logout,
    canWrite: !!user && (user.is_superadmin || user.role === "owner"),
    isWorker: !!user && !user.is_superadmin && user.role === "worker",
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
