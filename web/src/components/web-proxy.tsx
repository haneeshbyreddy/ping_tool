// Device web-UI proxy (webplan.md M3): open a tunnel session against a
// switch/OLT and drive its native web UI from the dashboard. The heavy lifting
// is server-side (browser → central → edge → device); this file is the "Open
// web UI" menu entry, the sessions/audit card, and nothing else.
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

/** Whether the current user may open (and drive) web-UI sessions in the scoped
    org: the org capability flag AND owner role — same gate the server enforces
    on POST /api/proxy/session and the /api/proxy/<sid>/ browse path. Operators
    and techs are locked out of device admin UIs entirely. */
export function useWebProxy(): boolean {
  const { user } = useAuth()
  const flag = useOrgProxyFlag()
  const roleOk = !!user && (user.is_superadmin || user.role === "owner")
  return flag && roleOk
}

/** Whether the current user may MANAGE stored device logins: the org proxy
    capability AND owner role — the same gate the server enforces on
    POST /api/inventory/credentials. Same owner-only gate as opening a session. */
export function useCanManageCreds(): boolean {
  const { user } = useAuth()
  const flag = useOrgProxyFlag()
  const roleOk = !!user && (user.is_superadmin || user.role === "owner")
  return flag && roleOk
}

/** Owner-only editor for a device's web-UI login. Stored encrypted server-side
    (central/secretbox.py); the password is write-only from here — we only ever
    learn whether one is set, never read it back. Caller gates with
    useCanManageCreds()/canOpenWebUi. */
export function WebUiCredentialsButton({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [authMode, setAuthMode] = useState<"basic" | "form">("form")
  // Address override: where the admin page actually lives. Off = the device's
  // own IP on 80/443; on = a different IP and/or port. The scheme is inferred
  // from the port (443 = https, everything else http) — no separate control.
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

  // seed the fields once the current values land
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
    // password left blank => omit it so a username-only edit keeps the stored
    // password; a typed value replaces it. The address override is saved
    // alongside so one dialog covers "how to reach + how to log into" the UI.
    mutationFn: async () => {
      // Only store an override that reaches somewhere the plain Connect buttons
      // can't (a different IP or a non-standard port). Toggle off — or a
      // redundant same-IP:80/443 entry — clears it; the server normalizes the
      // same way (inventory.normalize_web_access).
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

  // The toggle is on but the entry reaches nowhere new (same IP, standard/blank
  // port) — saving will collapse it to no override, so say so.
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
                  80/443 — for example port-forwarded to a different IP or port.
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
                    That's the device's own address — saving will switch this off.
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
                ? "Signed in automatically when you open the web UI — the login never touches your browser."
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
  const allowed = useWebProxy()
  const sess = useLiveWebSession(device)
  if (!allowed || !sess) return null
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

/** Whether a stored web-UI override points somewhere the plain http/https
    buttons can't already reach — a DIFFERENT host, or a NON-standard port. Only
    then does the override pin the endpoint (and the split button collapses to a
    single Connect). A same-IP:80/443 or bare-scheme override is redundant: the
    server normalizes it away (inventory.normalize_web_access), but we mirror the
    check here so a legacy row still keeps its http/https fallback. */
function overridePinsEndpoint(device: OrgDevice): boolean {
  const distinctIp = !!device.web_ip && device.web_ip !== device.ip_address
  const distinctPort =
    device.web_port != null && device.web_port !== 80 && device.web_port !== 443
  return distinctIp || distinctPort
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
  const tid = `webui-${device.id}`
  // Session open now includes the probe's preflight (it checks what the device
  // actually answers — http/https/nothing), so this can take a few seconds and
  // a definite "unreachable" comes back HERE instead of a dead tab later.
  toast.loading(`Connecting to ${device.name}…`, {
    id: tid, description: "The probe is checking the device's web UI.",
  })
  try {
    const sess = await proxyApi.open(device.id, port)
    rememberPort(device.id, port)
    if (tab) tab.location.replace(sess.url)
    else window.open(sess.url, "_blank")
    toast.success(`Connected — opening ${device.name}'s web UI`, {
      id: tid,
      description: "If the tab stalls, the probe may still be waking — refresh it once.",
    })
    return true
  } catch (e) {
    tab?.close()
    toast.error(e instanceof ApiError ? e.message : "Failed to open a web UI session",
      { id: tid, duration: 12_000 })
    return false
  }
}

/** The device panel's "Web UI" button (right of the Health/Optical/Ports
    tabs): a split button — the primary "Connect" opens in one click using the
    last-used port (http by default), and the chevron still offers http/https
    explicitly. Both stay reachable — a device whose firmware moves ports must
    remain switchable. Caller gates with useWebProxy()/canOpenWebUi. */
export function WebUiButton({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const last = lastPort(device.id)
  const order: Array<80 | 443> = last === 443 ? [443, 80] : [80, 443]
  const primary = order[0] // last used, or http for a device never opened
  const open = (p: 80 | 443) => void openDeviceWebUi(device, p).then((ok) => {
    // surface the live strip / Settings card row now, not on the next poll
    if (ok) queryClient.invalidateQueries({ queryKey: ["proxy-sessions"] })
  })
  // A device whose override pins a DISTINCT endpoint (different host or
  // non-standard port) has a fixed target — the port the server uses comes from
  // the override, so the http/https chooser is moot. Show a single Connect
  // button that names where it goes. A redundant same-IP:80/443 override does
  // NOT pin anything (the server normalizes it away), so keep the split button.
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
        {/* z above the map's z-[1000] device panel — that Card is a backdrop-blur
            stacking context, so the default z-50 menu paints behind it (invisible
            on /map). Higher wins everywhere else too. */}
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
              {s.status === "open" && s.live && isOwner && (
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
