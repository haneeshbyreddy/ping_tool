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
  // A superadmin has no home org, so the scope starts null — "All orgs" — where
  // Home/Network/Map/Logs each early-return <NeedsOrg/> and a fresh session lands
  // on four dead pages. We resolve a concrete default (the first org) ONCE per
  // session, and only when nothing is stored; this ref records that we've done
  // so, so an explicit "All orgs" pick afterwards is respected for the rest of
  // the session (a reload re-defaults, which is the whole point).
  const scopeResolved = useRef(false)

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
    // A different account logging in on this same tab must re-resolve its default.
    scopeResolved.current = false
  }

  const setScopeOrg = (org: string | null) => {
    setSuperadminScope(org)
    if (org) localStorage.setItem(SCOPE_STORAGE_KEY, org)
    else localStorage.removeItem(SCOPE_STORAGE_KEY)
  }

  // Superadmin only: fetch the org list so the scope can default to the first
  // org. Shares the ["orgs"] query cache with the workspace switcher, so it is
  // not an extra round trip. Owners/workers take their scope from user.org_id
  // and never reach this.
  const orgsQuery = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user?.is_superadmin,
  })
  useEffect(() => {
    if (!user?.is_superadmin || scopeResolved.current) return
    // Something already stored (a previous pick) — honour it, don't re-default.
    if (localStorage.getItem(SCOPE_STORAGE_KEY)) {
      scopeResolved.current = true
      return
    }
    const first = orgsQuery.data?.orgs[0]?.org_id
    if (first) {
      scopeResolved.current = true
      setScopeOrg(first)
    }
    // The platform pages (/overview, /orgs) don't read scope, so seeding it never
    // hides them — it only rescues the four org-scoped pages from <NeedsOrg/>.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, orgsQuery.data])

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
