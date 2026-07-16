// Device web-UI proxy (webplan.md M3): open a tunnel session against a
// switch/OLT and drive its native web UI from the dashboard. The heavy lifting
// is server-side (browser → central → edge → device); this file is the "Open
// web UI" menu entry, the sessions/audit card, and nothing else.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronDown, ExternalLink, Globe, Lock } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, proxyApi, ApiError } from "@/lib/api"
import type { OrgDevice, ProxySession } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Badge } from "@/components/ui/badge"

/** The scoped org's web-proxy capability flag (superadmin-granted). */
function useOrgProxyFlag(): boolean {
  const { scopeOrg } = useAuth()
  const { data } = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  return !!data?.orgs.find((o) => o.org_id === scopeOrg)?.web_proxy
}

/** Whether the current user may open web-UI sessions in the scoped org:
    the org capability flag AND owner/operator role — same gate the server
    enforces on POST /api/proxy/session. */
export function useWebProxy(): boolean {
  const { user } = useAuth()
  const flag = useOrgProxyFlag()
  const roleOk = !!user && (user.is_superadmin
    || user.role === "owner" || user.role === "operator")
  return flag && roleOk
}

/** The live tunnel session against one device, if any. Shares the
    ["proxy-sessions", org] cache with the Settings card, so however many
    device panels are open there's one poll. */
export function useLiveWebSession(device: OrgDevice): ProxySession | undefined {
  const { scopeOrg } = useAuth()
  const flag = useOrgProxyFlag()
  const { data } = useQuery({
    queryKey: ["proxy-sessions", scopeOrg],
    queryFn: () => proxyApi.sessions(scopeOrg),
    enabled: !!scopeOrg && flag,
    refetchInterval: 15_000,
  })
  return data?.sessions.find(
    (s) => s.device_id === device.id && s.status === "open" && s.live)
}

/** Device-row capability icon (sits beside the optics/ports icons): a pulsing
    globe while this device's web UI tunnel is live, nothing otherwise. Click
    jumps back into the session tab. */
export function WebUiLiveIcon({ device }: { device: OrgDevice }) {
  const sess = useLiveWebSession(device)
  if (!sess) return null
  return (
    <span title={`Web UI session live · opened ${ago(sess.created_at)} — click to open`}
      className="inline-flex cursor-pointer"
      onClick={(e) => {
        e.stopPropagation()
        window.open(`/api/proxy/${sess.sid}/`, "_blank")
      }}>
      <Globe className="size-3.5 animate-pulse text-success" />
    </span>
  )
}

export function canOpenWebUi(device: OrgDevice): boolean {
  // needs a probe to fetch through and an IP to fetch — passives have neither
  return !!device.ip_address && !!device.assigned_node_id
}

// Last port that worked per device — the OLT that refuses port 80 (HILL-OLT-1
// field lesson) should have its https choice float to the top next time.
const PORT_KEY = "wisp:webui-port"

function lastPort(deviceId: number): 80 | 443 | null {
  try {
    const p = (JSON.parse(localStorage.getItem(PORT_KEY) || "{}") as Record<string, unknown>)[deviceId]
    return p === 443 || p === 80 ? p : null
  } catch {
    return null
  }
}

function rememberPort(deviceId: number, port: number): void {
  try {
    const map = JSON.parse(localStorage.getItem(PORT_KEY) || "{}") as Record<string, number>
    map[deviceId] = port
    localStorage.setItem(PORT_KEY, JSON.stringify(map))
  } catch { /* private mode etc. — a lost preference is fine */ }
}

export async function openDeviceWebUi(device: OrgDevice, port: 80 | 443): Promise<boolean> {
  // The tab must open synchronously inside the click gesture or popup blockers
  // eat it — open blank now, point it at the session once central answers.
  const tab = window.open("", "_blank")
  try {
    const sess = await proxyApi.open(device.id, port)
    rememberPort(device.id, port)
    if (tab) tab.location.replace(sess.url)
    else window.open(sess.url, "_blank")
    toast.success(`Web UI session opened for ${device.name}`, {
      description: "First load can take up to a minute while the probe connects — refresh the tab if it times out.",
    })
    return true
  } catch (e) {
    tab?.close()
    toast.error(e instanceof ApiError ? e.message : "Failed to open a web UI session")
    return false
  }
}

/** The device panel's "Web UI" button (right of the Health/Optical/Ports
    tabs): a compact dropdown offering http/https, last-used first. Both stay
    reachable — a device whose firmware moves ports must remain switchable.
    Caller gates with useWebProxy()/canOpenWebUi. */
export function WebUiButton({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const last = lastPort(device.id)
  const order: Array<80 | 443> = last === 443 ? [443, 80] : [80, 443]
  const open = (p: 80 | 443) => void openDeviceWebUi(device, p).then((ok) => {
    // surface the live strip / Settings card row now, not on the next poll
    if (ok) queryClient.invalidateQueries({ queryKey: ["proxy-sessions"] })
  })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-7 shrink-0 gap-1.5 px-2.5 text-xs">
          <Globe className="size-3.5 text-muted-foreground" /> Web UI
          <ChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {order.map((p) => (
          <DropdownMenuItem key={p} onClick={() => open(p)}>
            {p === 443 ? <Lock /> : <Globe />} {p === 443 ? "https" : "http"}
            {last === p && <span className="ml-auto pl-2 text-2xs text-muted-foreground">last used</span>}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function sessionBadge(s: ProxySession) {
  if (s.status === "open" && s.live) {
    return <Badge className="bg-success-soft text-success" variant="secondary">live</Badge>
  }
  // an 'open' row the hub forgot (central restart) is a zombie — say expired
  const label = s.status === "open" ? "expired" : s.status
  return <Badge variant="secondary" className="text-muted-foreground">{label}</Badge>
}

function auditTone(status: number | null): string {
  if (status == null || status >= 500) return "text-destructive"
  if (status >= 400) return "text-warning"
  return "text-muted-foreground"
}

/** Settings card: who has (had) a tunnel open against which device, plus the
    owner-only per-request audit trail. Renders nothing while the org lacks
    the capability flag — the feature stays invisible until granted. */
export function WebProxyCard({ org }: { org: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const isOwner = !!user && (user.is_superadmin || user.role === "owner")

  const orgQ = useQuery({
    queryKey: ["orgs", org],
    queryFn: () => orgsApi.list(org),
    enabled: !!org,
  })
  const flag = !!orgQ.data?.orgs.find((o) => o.org_id === org)?.web_proxy

  const sessions = useQuery({
    queryKey: ["proxy-sessions", org],
    queryFn: () => proxyApi.sessions(org),
    enabled: flag,
    refetchInterval: 15_000, // liveness moves with the tunnel, SSE doesn't cover it
  })
  const audit = useQuery({
    queryKey: ["proxy-audit", org],
    queryFn: () => proxyApi.audit(org, 50),
    enabled: flag && isOwner,
    refetchInterval: 30_000,
  })
  const close = useMutation({
    mutationFn: (sid: string) => proxyApi.close(sid),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["proxy-sessions", org] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to close the session"),
  })

  if (!flag) return null
  const rows = sessions.data?.sessions ?? []
  const openRows = rows.filter((s) => s.status === "open")
  const pastRows = rows.filter((s) => s.status !== "open").slice(0, 5)
  const auditRows = audit.data?.audit ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Globe className="size-4 text-muted-foreground" /> Device web UI sessions
          {openRows.length > 0 && (
            <Badge variant="secondary" className="ml-auto font-mono">{openRows.length} open</Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted-foreground">
          Tunnels into device web UIs opened from the Network page. Sessions expire
          on their own; closing one cuts it immediately.
        </p>
        {rows.length === 0 && (
          <p className="text-xs text-faint-foreground">No sessions yet.</p>
        )}
        {[...openRows, ...pastRows].map((s) => (
          <div key={s.sid} className="flex items-center gap-2.5 rounded-lg border bg-muted/40 px-3 py-2">
            {sessionBadge(s)}
            <span className="min-w-0 truncate font-mono text-xs font-medium">
              {s.device_name ?? `device ${s.device_id}`}
            </span>
            <span className="hidden text-2xs text-muted-foreground sm:inline">
              via {s.node_id} · {ago(s.created_at)}
            </span>
            <div className="ml-auto flex shrink-0 items-center gap-1.5">
              {s.status === "open" && s.live && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  onClick={() => window.open(`/api/proxy/${s.sid}/`, "_blank")}>
                  <ExternalLink className="size-3" /> Open
                </Button>
              )}
              {s.status === "open" && (isOwner || s.created_by === user?.id) && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs text-destructive"
                  disabled={close.isPending} onClick={() => close.mutate(s.sid)}>
                  Close
                </Button>
              )}
            </div>
          </div>
        ))}
        {isOwner && auditRows.length > 0 && (
          <div className="overflow-hidden rounded-lg border">
            <div className="border-b bg-muted/40 px-3 py-1.5 text-2xs font-medium text-muted-foreground">
              Recent proxied requests
            </div>
            <div className="max-h-56 overflow-y-auto">
              {auditRows.map((a) => (
                <div key={a.id} className="flex items-center gap-2 border-b px-3 py-1 font-mono text-2xs last:border-b-0">
                  <span className="shrink-0 text-faint-foreground">{ago(a.ts)}</span>
                  <span className="shrink-0 text-muted-foreground">{a.device_name ?? a.device_id}</span>
                  <span className="min-w-0 truncate">{a.method} {a.path}</span>
                  <span className={cn("ml-auto shrink-0 font-semibold", auditTone(a.status))}>
                    {a.status ?? "—"}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
