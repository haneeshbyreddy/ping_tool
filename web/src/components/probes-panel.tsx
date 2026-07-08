import { useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Check, Copy, Download, KeyRound, MoreVertical, Plus, Power, Trash2 } from "lucide-react"
import { useNow } from "@/hooks/use-now"
import { nodesApi, ApiError } from "@/lib/api"
import {
  WINDOWS_SETUP_EXE, linuxInstallCmd, probeIdentity, releaseAsset, windowsSilentCmd,
} from "@/lib/install"
import type { NodeToken, OrgRollout } from "@/lib/types"
import { ConfirmDialog, useConfirm } from "@/components/confirm-dialog"
import { StatusDot } from "@/components/status-badge"
import { ago, fmtBytes, isStale } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

const copy = (text: string) => { navigator.clipboard.writeText(text); toast.success("Copied") }

function CopyField({ label, value, mask }: { label: string; value: string; mask?: boolean }) {
  return (
    <div className="flex h-9 items-center gap-2 border-b px-3 last:border-b-0">
      <span className="w-36 shrink-0 text-xs text-muted-foreground">{label}</span>
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs">
        {mask ? "•".repeat(24) : value}
      </code>
      <Button variant="ghost" size="icon" className="size-6 shrink-0 text-muted-foreground"
        title={`Copy ${label.toLowerCase()}`} onClick={() => copy(value)}>
        <Copy className="size-3.5" />
      </Button>
    </div>
  )
}

function CommandBlock({ command }: { command: string }) {
  return (
    <div className="relative rounded-lg bg-black p-3">
      {/* wrap, don't scroll: an overflow-x pre needs min-w-0 on every flex ancestor
          up the card, and one miss stretches the whole page (seen in review) */}
      <pre className="whitespace-pre-wrap break-all pr-8 font-mono text-xs leading-relaxed text-emerald-400">
        {command}
      </pre>
      <Button variant="ghost" size="sm" className="absolute top-1.5 right-1.5 text-primary"
        onClick={() => copy(command)}>
        <Copy className="size-3.5" />
      </Button>
    </div>
  )
}

function Step({ n, children }: { n: number; children: ReactNode }) {
  return (
    <div className="flex gap-2.5">
      <span className="mt-px flex size-4.5 shrink-0 items-center justify-center rounded-full bg-muted font-mono text-[0.6875rem] font-semibold text-muted-foreground">
        {n}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2 text-xs leading-relaxed text-foreground">
        {children}
      </div>
    </div>
  )
}

function CredentialReveal({
  org, nodeId, token, onDismiss,
}: { org: string; nodeId: string; token: string; onDismiss: () => void }) {
  const id = probeIdentity(org, nodeId, token)
  const [linuxArch, setLinuxArch] = useState<"amd64" | "arm64">("amd64")
  return (
    <Card className="border-primary/30">
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex items-center gap-2">
          <KeyRound className="size-4 text-primary" />
          <p className="text-sm font-semibold">Probe credential for {nodeId} — shown once</p>
        </div>
        <p className="text-xs text-muted-foreground">
          Copy the token now — it will not be shown again (rotate it if lost). Then install
          the probe on the machine that will do the pinging:
        </p>
        <div className="flex items-center justify-between gap-2 rounded-lg border bg-muted/50 px-4 py-2.5">
          <code className="overflow-x-auto whitespace-nowrap font-mono text-xs">{token}</code>
          <Button variant="ghost" size="sm" onClick={() => copy(token)}><Copy className="size-3.5" /></Button>
        </div>

        <Tabs defaultValue="windows">
          <TabsList>
            <TabsTrigger value="windows">Windows</TabsTrigger>
            <TabsTrigger value="linux">Linux (Debian/Ubuntu)</TabsTrigger>
          </TabsList>

          <TabsContent value="windows" className="flex flex-col gap-3 pt-1">
            <Step n={1}>
              <div className="flex flex-wrap items-center gap-2">
                <span>Download the installer on the probe machine:</span>
                <Button asChild size="sm" variant="outline" className="h-7">
                  <a href={releaseAsset(WINDOWS_SETUP_EXE)}>
                    <Download className="size-3.5" /> {WINDOWS_SETUP_EXE}
                  </a>
                </Button>
              </div>
            </Step>
            <Step n={2}>
              <span>
                Run it (accept the admin prompt) and paste these into the{" "}
                <span className="font-medium">Connect to WISP Central</span> page:
              </span>
              <div className="overflow-hidden rounded-lg border bg-muted/30">
                <CopyField label="Central URL" value={id.central} />
                <CopyField label="Enrollment token" value={id.token} mask />
                <CopyField label="Organization id" value={id.org} />
                <CopyField label="Node id" value={id.nodeId} />
              </div>
            </Step>
            <Step n={3}>
              <span>
                Done. The WISP tray icon (near the clock, under "Show hidden icons") turns
                green once the probe reports — and this probe's row below goes live, usually
                within a minute.
              </span>
            </Step>
            <details className="min-w-0 text-xs text-muted-foreground">
              <summary className="cursor-pointer select-none hover:text-foreground">
                Installing many probes? Silent install (PowerShell, as admin)
              </summary>
              <div className="pt-2">
                <CommandBlock command={windowsSilentCmd(id)} />
              </div>
            </details>
          </TabsContent>

          <TabsContent value="linux" className="flex flex-col gap-3 pt-1">
            <Step n={1}>
              <div className="flex flex-wrap items-center gap-2">
                <span>Paste this on the probe box — installs, configures, and starts it:</span>
                <div className="ml-auto flex gap-1">
                  {(["amd64", "arm64"] as const).map((a) => (
                    <Button key={a} size="sm" variant={linuxArch === a ? "secondary" : "ghost"}
                      className="h-6 px-2 font-mono text-[0.6875rem]" onClick={() => setLinuxArch(a)}>
                      {a}
                    </Button>
                  ))}
                </div>
              </div>
              <CommandBlock command={linuxInstallCmd(id, linuxArch)} />
            </Step>
            <Step n={2}>
              <span>
                Done. <code className="font-mono">systemctl status wisp-edge</code> shows the
                service; this probe's row below goes live within a minute. Config lives in{" "}
                <code className="font-mono">/etc/wisp/edge.env</code> if you ever need to
                change it.
              </span>
            </Step>
          </TabsContent>
        </Tabs>
        <Button variant="ghost" size="sm" className="self-center" onClick={onDismiss}>
          <Check className="size-3.5" /> I've saved the token
        </Button>
      </CardContent>
    </Card>
  )
}

function ProbeRow({
  node, org, canWrite, onReveal, latestVersion, rollout,
  deviceCount, filtered, onFilter,
}: {
  node: NodeToken
  org: string
  canWrite: boolean
  onReveal: (r: { node_id: string; token: string }) => void
  latestVersion: string | null
  rollout: OrgRollout | null
  deviceCount?: number
  filtered?: boolean
  onFilter?: () => void
}) {
  const queryClient = useQueryClient()
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["nodes"] })
  const confirmDelete = useConfirm()
  useNow()

  const rotate = useMutation({
    mutationFn: () => nodesApi.rotate(org, node.node_id),
    onSuccess: (r) => { onReveal(r); invalidate() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Rotate failed"),
  })
  const update = useMutation({
    mutationFn: () => nodesApi.update(org, node.node_id),
    onSuccess: (r) => {
      toast.success(`Update to ${r.target_version} queued — the probe pulls it on its next heartbeat`)
      invalidate()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Update failed"),
  })
  const revoke = useMutation({
    mutationFn: () => nodesApi.revoke(org, node.node_id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Revoke failed"),
  })
  const remove = useMutation({
    mutationFn: () => nodesApi.remove(org, node.node_id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })

  const stale = !!node.last_seen && isStale(node.last_seen)

  const updateAvailable = !!(node.version && latestVersion && node.version !== latestVersion)
  const rolloutCoversNode = !!(rollout && updateAvailable
    && rollout.target_version === latestVersion
    && (rollout.state === "promoted" || rollout.canary.includes(node.node_id)))
  const updateInFlight = rolloutCoversNode && rollout!.state !== "halted"
  const updateStalled = !!(rollout && updateAvailable
    && rollout.target_version === latestVersion && rollout.state === "halted"
    && rollout.canary.includes(node.node_id))

  const status = !node.registered
    ? { label: node.last_seen
          ? (stale ? `stale · ${ago(node.last_seen)}` : ago(node.last_seen))
          : "reporting",
        tone: "muted" as const }
    : node.revoked_at
      ? { label: "revoked", tone: "destructive" as const }
      : node.last_seen
        ? { label: stale ? `stale · ${ago(node.last_seen)}` : ago(node.last_seen),
            tone: stale ? "destructive" as const : "success" as const }
        : { label: "never connected", tone: "muted" as const }

  return (
    <div className={cn(
      "group flex h-11 items-center gap-2.5 border-b px-4 last:border-b-0 hover:bg-accent/40",
      !node.registered && "bg-muted/20",
    )}>
      <StatusDot tone={status.tone} />
      <span className={cn(
        "min-w-0 truncate font-mono text-xs font-medium",
        !node.registered && "text-muted-foreground",
      )}>{node.node_id}</span>
      {onFilter != null && (deviceCount ?? 0) > 0 && (
        <button
          className={cn(
            "shrink-0 rounded-full border px-2 py-0.5 text-[0.6875rem] font-medium transition-colors",
            filtered ? "border-primary/40 bg-primary-soft text-primary"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
          title={filtered ? "Showing only this probe's devices — click to clear"
            : "Show only this probe's devices"}
          onClick={onFilter}>
          {deviceCount} device{deviceCount === 1 ? "" : "s"}
        </button>
      )}
      {!node.registered && (
        <span className="shrink-0 rounded-sm bg-muted px-1.5 py-0.5 text-[0.6875rem] font-medium text-muted-foreground"
          title="Reporting to central without a dashboard-issued credential — a leftover or rogue probe. Delete to forget it, or Register a probe with this id to manage it.">
          unregistered
        </span>
      )}
      {node.version && (
        <span className={cn(
          "hidden shrink-0 font-mono text-xs sm:inline",
          updateAvailable ? "text-warning" : "text-muted-foreground/70",
        )} title={updateAvailable ? `${latestVersion} available` : undefined}>
          {node.version}
        </span>
      )}
      {updateInFlight && (
        <span className="shrink-0 rounded-sm bg-muted px-1.5 py-0.5 text-[0.6875rem] font-medium text-muted-foreground"
          title={`Updating to ${rollout!.target_version} — applied by the probe's supervisor after its next heartbeat`}>
          updating…
        </span>
      )}
      {updateStalled && (
        <span className="shrink-0 rounded-sm bg-muted px-1.5 py-0.5 text-[0.6875rem] font-medium text-warning"
          title={`The rollout to ${rollout!.target_version} halted — the probe never came back healthy on the new version within the window. Check logs\\edge.log on the probe box, then retry from the menu.`}>
          update stalled
        </span>
      )}
      {(node.rss_bytes != null || node.mem_available_bytes != null) && (
        <span className="hidden shrink-0 text-[0.75rem] text-muted-foreground/70 md:inline"
          title="Probe process memory · host RAM free">
          {node.rss_bytes != null && fmtBytes(node.rss_bytes)}
          {node.mem_available_bytes != null && (
            <span className="text-muted-foreground/50">
              {node.rss_bytes != null ? " · " : ""}{fmtBytes(node.mem_available_bytes)} free
            </span>
          )}
        </span>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-3">
        <span className={cn(
          "text-xs",
          status.tone === "destructive" ? "font-semibold text-destructive" : "text-muted-foreground",
        )}>
          {status.label}
        </span>
        {canWrite && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon"
                className="size-6 text-muted-foreground opacity-60 group-hover:opacity-100 data-[state=open]:opacity-100">
                <MoreVertical className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {updateAvailable && (
                <DropdownMenuItem disabled={update.isPending}
                  onClick={() => update.mutate()}>
                  <Download /> {updateStalled ? `Retry update to ${latestVersion}` : `Update to ${latestVersion}`}
                </DropdownMenuItem>
              )}
              {node.registered && (
                <DropdownMenuItem onClick={() => rotate.mutate()}>
                  <KeyRound /> Rotate credential
                </DropdownMenuItem>
              )}
              {node.registered && !node.revoked_at && (
                <DropdownMenuItem onClick={() => revoke.mutate()}>
                  <Power /> Revoke
                </DropdownMenuItem>
              )}
              <DropdownMenuItem variant="destructive" onClick={() => confirmDelete.ask()}>
                <Trash2 /> Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        <ConfirmDialog {...confirmDelete.props}
          title={node.registered
            ? `Delete probe ${node.node_id}?`
            : `Forget unregistered probe ${node.node_id}?`}
          description={node.registered
            ? "Its credential stops working and its devices go unmonitored until reassigned. This cannot be undone."
            : "It will reappear here if it keeps reporting."}
          confirmLabel={node.registered ? "Delete" : "Forget"}
          onConfirm={() => remove.mutate()} />
      </div>
    </div>
  )
}

export function ProbesPanel({
  org, canWrite, deviceCounts, probeFilter, onProbeFilter,
}: {
  org: string
  canWrite: boolean
  deviceCounts?: Map<string, number>
  probeFilter?: string | null
  onProbeFilter?: (nodeId: string | null) => void
}) {
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [newId, setNewId] = useState("")
  const [reveal, setReveal] = useState<{ node_id: string; token: string } | null>(null)
  const [error, setError] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["nodes", org],
    queryFn: () => nodesApi.list(org),
    enabled: !!org,

    refetchInterval: 30_000,
  })

  const register = useMutation({
    mutationFn: () => nodesApi.register(org, newId.trim()),
    onSuccess: (r) => {
      setReveal(r); setAddOpen(false); setNewId(""); setError("")
      queryClient.invalidateQueries({ queryKey: ["nodes"] })
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Registration failed"),
  })

  const nodes = data?.nodes ?? []

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">
          Probes
          {nodes.length > 0 && <span className="ml-2 font-normal text-muted-foreground">{nodes.length}</span>}
        </h2>
        {canWrite && !addOpen && (
          <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => setAddOpen(true)}>
            <Plus className="size-3.5" /> Register
          </Button>
        )}
      </div>

      {reveal && (
        <CredentialReveal org={org} nodeId={reveal.node_id} token={reveal.token}
          onDismiss={() => setReveal(null)} />
      )}

      {addOpen && (
        <div className="flex items-center gap-2 rounded-lg border bg-card p-2">
          <Input autoFocus placeholder="probe id, e.g. edge-a1" className="h-8 flex-1 font-mono text-xs"
            value={newId} onChange={(e) => setNewId(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && newId.trim()) register.mutate() }} />
          <Button variant="ghost" size="sm" onClick={() => { setAddOpen(false); setError("") }}>Cancel</Button>
          <Button size="sm" disabled={!newId.trim() || register.isPending} onClick={() => register.mutate()}>
            Generate
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}

      {isLoading && <Skeleton className="h-10 w-full" />}
      {!isLoading && nodes.length === 0 && (
        <p className="rounded-lg border border-dashed py-4 text-center text-xs text-muted-foreground">
          No probes registered yet — register one to start monitoring.
        </p>
      )}
      {nodes.length > 0 && (
        <Card className="gap-0 overflow-hidden py-0">
          {nodes.map((n) => (
            <ProbeRow key={n.node_id} node={n} org={org} canWrite={canWrite} onReveal={setReveal}
              latestVersion={data?.latest_version ?? null} rollout={data?.rollout ?? null}
              deviceCount={deviceCounts?.get(n.node_id)}
              filtered={probeFilter === n.node_id}
              onFilter={onProbeFilter
                ? () => onProbeFilter(probeFilter === n.node_id ? null : n.node_id)
                : undefined} />
          ))}
        </Card>
      )}
    </section>
  )
}
