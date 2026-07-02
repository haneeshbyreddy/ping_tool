import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { Plus, ArrowRight } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, ApiError } from "@/lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"

// Superadmin-only cross-tenant org directory — create a brand-new tenant, rename an
// existing one inline, or jump into a tenant's own Settings page (org switcher +
// /settings) for the rest (ntfy topics, login accounts, team roster).
export function OrganizationsPage() {
  const { user, setScopeTenant } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user?.is_superadmin,
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["orgs"] })

  const [tenantId, setTenantId] = useState("")
  const [name, setName] = useState("")
  const [error, setError] = useState("")
  const create = useMutation({
    mutationFn: () => orgsApi.create({ tenant_id: tenantId.trim(), name: name.trim() || undefined }),
    onSuccess: () => { invalidate(); setTenantId(""); setName(""); setError(""); toast.success("Org created") },
    onError: (e) => setError(e instanceof ApiError ? e.message : "failed to create"),
  })

  const [edits, setEdits] = useState<Record<string, string>>({})
  const rename = useMutation({
    mutationFn: (id: string) => orgsApi.save({ tenant_id: id, name: edits[id]?.trim() || null }),
    onSuccess: (_r, id) => {
      invalidate()
      setEdits((e) => { const next = { ...e }; delete next[id]; return next })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to rename"),
  })

  const manage = (id: string) => { setScopeTenant(id); navigate("/settings") }

  if (!user?.is_superadmin) return null
  const orgs = data?.orgs ?? []

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-6">
      <h1 className="text-xl font-bold">Organizations</h1>

      <Card>
        <CardHeader><CardTitle className="text-sm">New org</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-2.5">
          <div className="flex flex-col gap-1.5">
            <Label>Org id</Label>
            <Input placeholder="e.g. acme-wisp" className="max-w-sm font-mono text-xs"
              value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Display name</Label>
            <Input placeholder="Acme WISP" className="max-w-sm" value={name}
              onChange={(e) => setName(e.target.value)} />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <Button size="sm" className="w-fit" disabled={!tenantId.trim() || create.isPending}
            onClick={() => create.mutate()}>
            <Plus className="size-4" /> Create org
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-sm">All orgs ({orgs.length})</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-0 p-0">
          {isLoading && <div className="px-4 pb-4"><Skeleton className="h-16 w-full" /></div>}
          {!isLoading && orgs.length === 0 && (
            <p className="px-4 pb-4 text-sm text-muted-foreground">No orgs yet.</p>
          )}
          {orgs.map((o) => (
            <div key={o.tenant_id} className="flex flex-col gap-2 border-t px-4 py-3 first:border-t-0 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-xs text-muted-foreground">{o.tenant_id}</p>
                <div className="mt-1 flex items-center gap-2">
                  <Input
                    className="h-7 max-w-56 text-sm"
                    value={edits[o.tenant_id] ?? o.name ?? ""}
                    onChange={(e) => setEdits((v) => ({ ...v, [o.tenant_id]: e.target.value }))}
                  />
                  {o.tenant_id in edits && edits[o.tenant_id] !== (o.name ?? "") && (
                    <Button size="sm" variant="outline" disabled={rename.isPending}
                      onClick={() => rename.mutate(o.tenant_id)}>
                      Save
                    </Button>
                  )}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <span className="text-xs text-muted-foreground">{o.node_count} node{o.node_count === 1 ? "" : "s"}</span>
                <Button variant="outline" size="sm" onClick={() => manage(o.tenant_id)}>
                  Manage <ArrowRight className="size-3.5" />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
