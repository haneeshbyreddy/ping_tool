import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronDown, ExternalLink, Globe, KeyRound, Lock } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, proxyApi, inventoryApi, ApiError } from "@/lib/api"
import type { OrgDevice, ProxySession } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Badge } from "@/components/ui/badge"

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

export function useWebProxy(): boolean {
  const { user } = useAuth()
  const flag = useOrgProxyFlag()
  const roleOk = !!user && (user.is_superadmin || user.role === "owner")
  return flag && roleOk
}

export function useCanManageCreds(): boolean {
  const { user } = useAuth()
  const flag = useOrgProxyFlag()
  const roleOk = !!user && (user.is_superadmin || user.role === "owner")
  return flag && roleOk
}

export function WebUiCredentialsButton({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [authMode, setAuthMode] = useState<"basic" | "form">("form")
  const [altAddr, setAltAddr] = useState(false)
  const [webIp, setWebIp] = useState("")
  const [webPort, setWebPort] = useState("")

  const creds = useQuery({
    queryKey: ["webui-creds", device.id],
    queryFn: () => inventoryApi.credentials(device.id),
    enabled: open,
  })
  const hasPassword = !!creds.data?.credentials.has_password
  const hasOverride = overridePinsEndpoint(device)

  function onOpenChange(next: boolean) {
    if (next) {
      setPassword("")
      setUsername("")
      setAuthMode("form")
      setAltAddr(overridePinsEndpoint(device))
      setWebIp(device.web_ip ?? "")
      setWebPort(device.web_port != null ? String(device.web_port) : "")
      void creds.refetch().then((r) => {
        setUsername(r.data?.credentials.username ?? "")
        setAuthMode(r.data?.credentials.auth_mode ?? "form")
      })
    }
    setOpen(next)
  }

  const save = useMutation({
    mutationFn: async () => {
      const ip = webIp.trim()
      const portNum = webPort.trim() ? Number(webPort.trim()) : null
      const store = altAddr && (
        (!!ip && ip !== device.ip_address) ||
        (portNum != null && portNum !== 80 && portNum !== 443))
      await inventoryApi.setWebAccess(device.id, store
        ? { web_ip: ip || null, web_port: portNum, web_scheme: null }
        : { web_ip: null, web_port: null, web_scheme: null })
      await inventoryApi.setCredentials(device.id, {
        username: username.trim(),
        auth_mode: authMode,
        ...(password === "" ? {} : { password }),
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webui-creds", device.id] })
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      toast.success(`Saved web UI settings for ${device.name}`)
      setOpen(false)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to save"),
  })
  const clear = useMutation({
    mutationFn: () => inventoryApi.clearCredentials(device.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webui-creds", device.id] })
      toast.success(`Removed the stored login for ${device.name}`)
      setOpen(false)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to remove the login"),
  })
  const busy = save.isPending || clear.isPending

  const typedIp = webIp.trim()
  const typedPortNum = webPort.trim() ? Number(webPort.trim()) : null
  const altRedundant = altAddr &&
    !((!!typedIp && typedIp !== device.ip_address) ||
      (typedPortNum != null && typedPortNum !== 80 && typedPortNum !== 443))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <Button variant="outline" size="sm" className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
        title="Configure this device's web UI address & login" onClick={() => onOpenChange(true)}>
        <KeyRound className="size-3.5 text-muted-foreground" /> Login
        {(hasPassword || hasOverride) && <span className="size-1.5 rounded-full bg-success" />}
      </Button>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Web UI: {device.name}</DialogTitle>
          <DialogDescription>
            Where the admin page lives and how to sign in. Stored encrypted so a
            tech never retypes it; the password is write-only here.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 py-1">
          <div className="rounded-lg border bg-muted/40 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <Label htmlFor="wui-alt" className="text-xs font-medium">Different web UI address</Label>
                <p className="mt-0.5 text-2xs text-muted-foreground">
                  Turn on only if the admin page isn't at {device.ip_address} on
                  80/443, for example port-forwarded to another IP or port.
                </p>
              </div>
              <Switch id="wui-alt" checked={altAddr} onCheckedChange={setAltAddr}
                className="mt-0.5 shrink-0" />
            </div>
            {altAddr && (
              <>
                <div className="mt-3 flex gap-2">
                  <div className="flex flex-1 flex-col gap-1.5">
                    <Label htmlFor="wui-ip" className="text-2xs">IP address</Label>
                    <Input id="wui-ip" autoComplete="off" value={webIp} placeholder={device.ip_address}
                      onChange={(e) => setWebIp(e.target.value)} className="h-8 text-xs" />
                  </div>
                  <div className="flex w-24 flex-col gap-1.5">
                    <Label htmlFor="wui-port" className="text-2xs">Port</Label>
                    <Input id="wui-port" autoComplete="off" inputMode="numeric" value={webPort}
                      placeholder="80" onChange={(e) => setWebPort(e.target.value)} className="h-8 text-xs" />
                  </div>
                </div>
                {altRedundant && (
                  <p className="mt-2 text-2xs text-muted-foreground">
                    That's the device's own address, so saving switches this off.
                  </p>
                )}
              </>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wui-user">Username</Label>
            <Input id="wui-user" autoComplete="off" value={username}
              onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wui-pass">Password</Label>
            <Input id="wui-pass" type="password" autoComplete="new-password"
              value={password} onChange={(e) => setPassword(e.target.value)}
              placeholder={hasPassword ? "•••••••• (leave blank to keep)" : "not set"} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wui-mode">Login type</Label>
            <Select value={authMode} onValueChange={(v) => setAuthMode(v as "basic" | "form")}>
              <SelectTrigger id="wui-mode"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="form">Login form</SelectItem>
                <SelectItem value="basic">Basic auth (browser popup)</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-2xs text-muted-foreground">
              {authMode === "basic"
                ? "Signed in automatically when you open the web UI. The login never touches your browser."
                : "The login page is pre-filled when you open the web UI; you still solve any captcha and click sign in."}
            </p>
          </div>
        </div>
        <DialogFooter className="gap-2 sm:justify-between">
          {(hasPassword || (creds.data?.credentials.username ?? "") !== "") ? (
            <Button variant="ghost" size="sm" className="text-destructive"
              disabled={busy} onClick={() => clear.mutate()}>Remove login</Button>
          ) : <span />}
          <Button size="sm" disabled={busy} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

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

export function WebUiLiveIcon({ device }: { device: OrgDevice }) {
  const allowed = useWebProxy()
  const sess = useLiveWebSession(device)
  if (!allowed || !sess) return null
  return (
    <span title={`Web UI session live · opened ${ago(sess.created_at)} · click to open`}
      className="inline-flex cursor-pointer"
      onClick={(e) => {
        e.stopPropagation()
        watchSessionTab(sess.sid, window.open(`/api/proxy/${sess.sid}/`, "_blank"))
      }}>
      <Globe className="size-3.5 animate-pulse text-success" />
    </span>
  )
}

export function canOpenWebUi(device: OrgDevice): boolean {
  return !!device.ip_address && !!device.assigned_node_id
}

function overridePinsEndpoint(device: OrgDevice): boolean {
  const distinctIp = !!device.web_ip && device.web_ip !== device.ip_address
  const distinctPort =
    device.web_port != null && device.web_port !== 80 && device.web_port !== 443
  return distinctIp || distinctPort
}

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

const _openTabs = new Map<string, Window>()
let _tabWatch: number | null = null

function sweepClosedTabs(): void {
  for (const [sid, tab] of [..._openTabs]) {
    if (!tab.closed) continue
    _openTabs.delete(sid)
    void proxyApi.close(sid).catch(() => { /* it will time out on its own */ })
  }
  if (_openTabs.size === 0 && _tabWatch != null) {
    window.clearInterval(_tabWatch)
    _tabWatch = null
  }
}

function watchSessionTab(sid: string, tab: Window | null): void {
  if (!tab) return   // popup blocked, or opened without a handle — TTL covers it
  _openTabs.set(sid, tab)
  _tabWatch ??= window.setInterval(sweepClosedTabs, 3_000)
}

export async function openDeviceWebUi(device: OrgDevice, port: 80 | 443): Promise<boolean> {
  const tab = window.open("", "_blank")
  const tid = `webui-${device.id}`
  toast.loading(`Connecting to ${device.name}…`, {
    id: tid, description: "The probe is checking the device's web UI.",
  })
  try {
    const sess = await proxyApi.open(device.id, port)
    rememberPort(device.id, port)
    let shown: Window | null = tab
    if (tab) tab.location.replace(sess.url)
    else shown = window.open(sess.url, "_blank")
    watchSessionTab(sess.sid, shown)
    toast.success(`Connected. Opening ${device.name}'s web UI…`, {
      id: tid,
      description: "If the tab stalls, the probe may still be waking. Refresh it once.",
    })
    return true
  } catch (e) {
    tab?.close()
    toast.error(e instanceof ApiError ? e.message : "Failed to open a web UI session",
      { id: tid, duration: 12_000 })
    return false
  }
}

export function WebUiButton({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const last = lastPort(device.id)
  const order: Array<80 | 443> = last === 443 ? [443, 80] : [80, 443]
  const primary = order[0] // last used, or http for a device never opened
  const open = (p: 80 | 443) => void openDeviceWebUi(device, p).then((ok) => {
    if (ok) queryClient.invalidateQueries({ queryKey: ["proxy-sessions"] })
  })
  const hasOverride = overridePinsEndpoint(device)
  if (hasOverride) {
    const scheme = device.web_scheme || (device.web_port === 443 ? "https" : "http")
    const host = device.web_ip || device.ip_address
    const port = device.web_port ?? (scheme === "https" ? 443 : 80)
    return (
      <Button variant="outline" size="sm" className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
        title={`Open the web UI at ${scheme}://${host}:${port}`}
        onClick={() => open(primary)}>
        <Globe className="size-3.5 text-muted-foreground" /> Connect
      </Button>
    )
  }
  return (
    <div className="flex shrink-0 items-center">
      <Button variant="outline" size="sm"
        className="h-7 gap-1.5 rounded-r-none border-r-0 px-2.5 text-xs"
        title={`Open the web UI over ${primary === 443 ? "https" : "http"}`}
        onClick={() => open(primary)}>
        <Globe className="size-3.5 text-muted-foreground" /> Connect
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className="h-7 rounded-l-none px-1.5"
            aria-label="Choose http or https">
            <ChevronDown className="size-3 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="z-[1100]">
          {order.map((p) => (
            <DropdownMenuItem key={p} onClick={() => open(p)}>
              {p === 443 ? <Lock /> : <Globe />} {p === 443 ? "https" : "http"}
              {last === p && <span className="ml-auto pl-2 text-2xs text-muted-foreground">last used</span>}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

function sessionBadge(s: ProxySession) {
  if (s.status === "open" && s.live) {
    return <Badge className="bg-success-soft text-success" variant="secondary">live</Badge>
  }
  const label = s.status === "open" ? "expired" : s.status
  return <Badge variant="secondary" className="text-muted-foreground">{label}</Badge>
}

function auditTone(status: number | null): string {
  if (status == null || status >= 500) return "text-destructive"
  if (status >= 400) return "text-warning"
  return "text-muted-foreground"
}

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
              {s.status === "open" && s.live && isOwner && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  onClick={() => watchSessionTab(
                    s.sid, window.open(`/api/proxy/${s.sid}/`, "_blank"))}>
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
