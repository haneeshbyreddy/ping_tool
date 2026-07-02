import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { Plus, ArrowRight, Building2, Radio } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, ApiError } from "@/lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Badge } from "@/components/ui/badge"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"

function orgInitials(o: { name: string | null; org_id: string }): string {
  return (o.name || o.org_id).slice(0, 2).toUpperCase()
}

// Superadmin-only cross-org org directory — create a brand-new org, rename an
// existing one inline, or jump into an org's own Settings page (org switcher +
// /settings) for the rest (ntfy topics, login accounts, team roster).
export function OrganizationsPage() {
  const { user, setScopeOrg } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ["orgs"],
    queryFn: () => orgsApi.list(),
    enabled: !!user?.is_superadmin,
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["orgs"] })

  const [orgId, setOrgId] = useState("")
  const [name, setName] = useState("")
  const [error, setError] = useState("")
  const create = useMutation({
    mutationFn: () => orgsApi.create({ org_id: orgId.trim(), name: name.trim() || undefined }),
    onSuccess: () => { invalidate(); setOrgId(""); setName(""); setError(""); toast.success("Org created") },
    onError: (e) => setError(e instanceof ApiError ? e.message : "failed to create"),
  })

  const [edits, setEdits] = useState<Record<string, string>>({})
  const rename = useMutation({
    mutationFn: (id: string) => orgsApi.save({ org_id: id, name: edits[id]?.trim() || null }),
    onSuccess: (_r, id) => {
      invalidate()
      setEdits((e) => { const next = { ...e }; delete next[id]; return next })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to rename"),
  })

  const manage = (id: string) => { setScopeOrg(id); navigate("/settings") }

  if (!user?.is_superadmin) return null
  const orgs = data?.orgs ?? []

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-bold">Organizations</h1>
        <p className="text-sm text-muted-foreground">Every org on this platform — create new ISPs and manage their topology, team, and alert routing.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Plus className="size-4 text-muted-foreground" /> New org
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>Org id</Label>
            <Input placeholder="e.g. acme-wisp" className="max-w-sm font-mono text-xs"
              value={orgId} onChange={(e) => setOrgId(e.target.value)} />
            <p className="text-xs text-muted-foreground">Lowercase, no spaces — used internally and can't be changed later.</p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Display name</Label>
            <Input placeholder="Acme WISP" className="max-w-sm" value={name}
              onChange={(e) => setName(e.target.value)} />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <Button size="sm" className="w-fit" disabled={!orgId.trim() || create.isPending}
            onClick={() => create.mutate()}>
            <Plus className="size-4" /> {create.isPending ? "Creating…" : "Create org"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Building2 className="size-4 text-muted-foreground" /> All orgs
            <Badge variant="secondary" className="ml-auto font-mono">{orgs.length}</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-0 p-0">
          {isLoading && (
            <div className="flex flex-col gap-3 px-4 pb-4">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          )}
          {!isLoading && orgs.length === 0 && (
            <p className="px-4 pb-4 text-sm text-muted-foreground">No orgs yet — create one above to get started.</p>
          )}
          {orgs.map((o) => (
            <div key={o.org_id}
              className="flex flex-col gap-3 border-t px-4 py-4 first:border-t-0 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 flex-1 items-center gap-3">
                <Avatar className="size-9 shrink-0">
                  <AvatarFallback className="bg-primary/10 text-xs font-bold text-primary">
                    {orgInitials(o)}
                  </AvatarFallback>
                </Avatar>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Input
                      className="h-8 max-w-56 text-sm font-medium"
                      value={edits[o.org_id] ?? o.name ?? ""}
                      placeholder={o.org_id}
                      onChange={(e) => setEdits((v) => ({ ...v, [o.org_id]: e.target.value }))}
                    />
                    {o.org_id in edits && edits[o.org_id] !== (o.name ?? "") && (
                      <Button size="sm" variant="outline" disabled={rename.isPending}
                        onClick={() => rename.mutate(o.org_id)}>
                        Save
                      </Button>
                    )}
                  </div>
                  <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{o.org_id}</p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3 pl-12 sm:pl-0">
                <Badge variant="outline" className="gap-1 font-normal text-muted-foreground">
                  <Radio className="size-3" /> {o.node_count} node{o.node_count === 1 ? "" : "s"}
                </Badge>
                <Button variant="outline" size="sm" onClick={() => manage(o.org_id)}>
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
