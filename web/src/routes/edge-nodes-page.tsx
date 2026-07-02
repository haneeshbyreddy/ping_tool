import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Check, Copy, KeyRound, Plus, Power, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { nodesApi, ApiError } from "@/lib/api"
import type { NodeToken } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { ago } from "@/lib/format"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

// Ported from the old static/app.js's installCmdLinux/installCmdWindows — these are the
// only two real installers server.py serves (install-edge-src.sh/.ps1); the mockup's
// "Docker" option doesn't exist here, so the toggle is Linux/Windows instead.
function installCmdLinux(tenant: string, nodeId: string, token: string): string {
  const c = window.location.origin
  return `curl -fsSL ${c}/install-edge-src.sh | sudo sh -s -- \\\n`
    + `    --central ${c} --token ${token} --tenant ${tenant} --node ${nodeId}`
}
function installCmdWindows(tenant: string, nodeId: string, token: string): string {
  const c = window.location.origin
  return `& ([scriptblock]::Create((irm ${c}/install-edge-src.ps1))) \`\n`
    + `    -Central ${c} -Token ${token} -Tenant ${tenant} -Node ${nodeId}`
}

function CredentialReveal({
  tenant, nodeId, token, onDismiss,
}: { tenant: string; nodeId: string; token: string; onDismiss: () => void }) {
  const copy = (text: string) => { navigator.clipboard.writeText(text); toast.success("Copied") }
  return (
    <Card className="border-primary/30">
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex items-center gap-2">
          <KeyRound className="size-4 text-primary" />
          <p className="text-sm font-bold">Probe credential for {nodeId} — shown once</p>
        </div>
        <p className="text-xs text-muted-foreground">
          Copy this now. It will not be shown again — you'll need to rotate it if lost.
        </p>
        <div className="flex items-center justify-between gap-2 rounded-lg border bg-muted/50 px-3 py-2">
          <code className="overflow-x-auto whitespace-nowrap font-mono text-[11.5px]">{token}</code>
          <Button variant="ghost" size="sm" onClick={() => copy(token)}><Copy className="size-3.5" /></Button>
        </div>
        <Tabs defaultValue="linux">
          <TabsList>
            <TabsTrigger value="linux">Linux (systemd)</TabsTrigger>
            <TabsTrigger value="windows">Windows</TabsTrigger>
          </TabsList>
          <TabsContent value="linux">
            <div className="relative rounded-lg bg-black p-3">
              <pre className="whitespace-pre-wrap font-mono text-[11px] text-emerald-400">
                {installCmdLinux(tenant, nodeId, token)}
              </pre>
              <Button variant="ghost" size="sm" className="absolute top-1.5 right-1.5 text-primary"
                onClick={() => copy(installCmdLinux(tenant, nodeId, token))}>
                <Copy className="size-3.5" />
              </Button>
            </div>
          </TabsContent>
          <TabsContent value="windows">
            <div className="relative rounded-lg bg-black p-3">
              <pre className="whitespace-pre-wrap font-mono text-[11px] text-emerald-400">
                {installCmdWindows(tenant, nodeId, token)}
              </pre>
              <Button variant="ghost" size="sm" className="absolute top-1.5 right-1.5 text-primary"
                onClick={() => copy(installCmdWindows(tenant, nodeId, token))}>
                <Copy className="size-3.5" />
              </Button>
            </div>
          </TabsContent>
        </Tabs>
        <Button variant="ghost" size="sm" className="self-center" onClick={onDismiss}>
          <Check className="size-3.5" /> I've saved it
        </Button>
      </CardContent>
    </Card>
  )
}

function NodeRow({ node, tenant }: { node: NodeToken; tenant: string }) {
  const queryClient = useQueryClient()
  const [reveal, setReveal] = useState<{ node_id: string; token: string } | null>(null)
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["nodes"] })

  const rotate = useMutation({
    mutationFn: () => nodesApi.rotate(tenant, node.node_id),
    onSuccess: (r) => { setReveal(r); invalidate() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "rotate failed"),
  })
  const revoke = useMutation({
    mutationFn: () => nodesApi.revoke(tenant, node.node_id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "revoke failed"),
  })
  const remove = useMutation({
    mutationFn: () => nodesApi.remove(tenant, node.node_id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "delete failed"),
  })

  const stale = node.last_seen && (Date.now() - new Date(node.last_seen).getTime()) / 1000 > 180
  const status = node.revoked_at
    ? { label: "revoked", tone: "destructive" as const }
    : node.last_seen
      ? { label: ago(node.last_seen), tone: stale ? "destructive" as const : "success" as const }
      : { label: "never connected", tone: "muted" as const }

  return (
    <>
      <Card className="py-3.5">
        <CardContent className="flex flex-col gap-3 px-4">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-[13.5px] font-bold">{node.node_id}</p>
              <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">{node.version || "—"}</p>
            </div>
            <span className="flex shrink-0 items-center gap-1.5 rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-bold whitespace-nowrap">
              <StatusDot tone={status.tone} />
              {status.label}
            </span>
          </div>
          <div className="flex gap-1.5 border-t pt-2.5">
            <Button variant="secondary" size="sm" className="flex-1 gap-1.5" onClick={() => rotate.mutate()}>
              <KeyRound className="size-3.5" /> Rotate
            </Button>
            {!node.revoked_at && (
              <Button variant="secondary" size="sm" className="flex-1 gap-1.5 text-warning"
                onClick={() => revoke.mutate()}>
                <Power className="size-3.5" /> Revoke
              </Button>
            )}
            <Button variant="secondary" size="sm" className="flex-1 gap-1.5 text-destructive"
              onClick={() => {
                if (confirm(`Delete probe ${node.node_id} permanently?`)) remove.mutate()
              }}>
              <Trash2 className="size-3.5" /> Delete
            </Button>
          </div>
        </CardContent>
      </Card>
      {reveal && (
        <CredentialReveal tenant={tenant} nodeId={reveal.node_id} token={reveal.token} onDismiss={() => setReveal(null)} />
      )}
    </>
  )
}

export function EdgeNodesPage() {
  const { scopeTenant, canWrite } = useAuth()
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [newId, setNewId] = useState("")
  const [reveal, setReveal] = useState<{ node_id: string; token: string } | null>(null)
  const [error, setError] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["nodes", scopeTenant],
    queryFn: () => nodesApi.list(scopeTenant),
    enabled: !!scopeTenant,
  })

  const register = useMutation({
    mutationFn: () => nodesApi.register(scopeTenant!, newId.trim()),
    onSuccess: (r) => {
      setReveal(r); setAddOpen(false); setNewId(""); setError("")
      queryClient.invalidateQueries({ queryKey: ["nodes"] })
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "register failed"),
  })

  if (!scopeTenant) return <NeedsOrg />
  const nodes = data?.nodes ?? []

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-3 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Probes</h1>
        {canWrite && !addOpen && (
          <Button size="sm" onClick={() => setAddOpen(true)}><Plus className="size-4" /> Register</Button>
        )}
      </div>

      {reveal && (
        <CredentialReveal tenant={scopeTenant} nodeId={reveal.node_id} token={reveal.token} onDismiss={() => setReveal(null)} />
      )}

      {addOpen && (
        <Card>
          <CardContent className="flex flex-col gap-3 px-4">
            <Input placeholder="probe id, e.g. edge-a1" value={newId} onChange={(e) => setNewId(e.target.value)} />
            {error && <p className="text-xs text-destructive">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setAddOpen(false)}>Cancel</Button>
              <Button size="sm" disabled={!newId.trim() || register.isPending} onClick={() => register.mutate()}>
                Generate
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {isLoading && <Skeleton className="h-20 w-full" />}
      {!isLoading && nodes.length === 0 && (
        <p className="py-16 text-center text-sm text-muted-foreground">No probes registered yet.</p>
      )}
      {nodes.map((n) => <NodeRow key={n.node_id} node={n} tenant={scopeTenant} />)}
    </div>
  )
}
