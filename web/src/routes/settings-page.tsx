import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Plus, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, usersApi, ApiError } from "@/lib/api"
import type { Role } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const ROLE_TOPICS: Array<{ key: "owner" | "operator" | "tech"; label: string }> = [
  { key: "owner", label: "Owner" }, { key: "operator", label: "Operator" }, { key: "tech", label: "Tech" },
]

function OrgSettingsCard({ tenant, canWrite }: { tenant: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["orgs", tenant],
    queryFn: () => orgsApi.list(tenant),
    enabled: !!tenant,
  })
  const org = data?.orgs.find((o) => o.tenant_id === tenant)

  const [name, setName] = useState("")
  const [topics, setTopics] = useState({ owner: "", operator: "", tech: "" })
  const [testResults, setTestResults] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!org) return
    setName(org.name || "")
    setTopics({
      owner: org.ntfy_topic_owner || "", operator: org.ntfy_topic_operator || "", tech: org.ntfy_topic_tech || "",
    })
  }, [org])

  const save = useMutation({
    mutationFn: () => orgsApi.save({
      tenant_id: tenant, name: name.trim() || null,
      ntfy_topic_owner: topics.owner.trim() || null,
      ntfy_topic_operator: topics.operator.trim() || null,
      ntfy_topic_tech: topics.tech.trim() || null,
    }),
    onSuccess: () => { toast.success("Settings saved"); queryClient.invalidateQueries({ queryKey: ["orgs"] }) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "save failed"),
  })

  const test = useMutation({
    mutationFn: (role: "owner" | "operator" | "tech") => orgsApi.testAlert(tenant, role),
    onSuccess: (r, role) => setTestResults((t) => ({ ...t, [role]: r.ok ? "✓ sent" : `failed: ${r.detail || ""}` })),
    onError: (e, role) => setTestResults((t) => ({ ...t, [role]: e instanceof ApiError ? e.message : "failed" })),
  })

  if (isLoading) return <Skeleton className="h-48 w-full" />

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Settings — {tenant}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Org name</Label>
          <Input value={name} disabled={!canWrite} onChange={(e) => setName(e.target.value)} className="max-w-sm" />
        </div>
        {ROLE_TOPICS.map(({ key, label }) => (
          <div key={key} className="flex flex-col gap-1.5">
            <Label>{label} ntfy topic</Label>
            <div className="flex items-center gap-2">
              <Input
                placeholder={`ntfy topic for ${label.toLowerCase()} alerts`}
                className="max-w-sm font-mono text-xs"
                disabled={!canWrite}
                value={topics[key]}
                onChange={(e) => setTopics({ ...topics, [key]: e.target.value })}
              />
              {canWrite && (
                <Button variant="outline" size="sm" disabled={test.isPending} onClick={() => test.mutate(key)}>
                  Send test
                </Button>
              )}
              {testResults[key] && <span className="text-xs text-muted-foreground">{testResults[key]}</span>}
            </div>
          </div>
        ))}
        {canWrite && (
          <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

function UsersCard({ tenant }: { tenant: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [error, setError] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["users", tenant],
    queryFn: () => usersApi.list(tenant),
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["users"] })

  const create = useMutation({
    mutationFn: () => usersApi.create({
      tenant_id: user?.is_superadmin ? tenant : undefined, username: username.trim(), password, role,
    }),
    onSuccess: () => { invalidate(); setAddOpen(false); setUsername(""); setPassword(""); setError("") },
    onError: (e) => setError(e instanceof ApiError ? e.message : "failed to create"),
  })
  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: number; active: boolean }) => usersApi.setActive(id, active),
    onSuccess: invalidate,
    onError: () => toast.error("failed to update"),
  })
  const remove = useMutation({
    mutationFn: (id: number) => usersApi.remove(id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to delete"),
  })

  const users = data?.users ?? []

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="text-sm">Login accounts</CardTitle>
        {!addOpen && (
          <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}><Plus className="size-4" /> Add user</Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-12 w-full" /></div>}
        {users.map((u) => (
          <div key={u.id} className="flex items-center justify-between gap-2 border-t px-4 py-2.5 first:border-t-0">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{u.username}</p>
              <p className="text-xs text-muted-foreground capitalize">{u.role}</p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                {u.is_active ? "active" : "deactivated"}
                <Switch checked={!!u.is_active}
                  onCheckedChange={(v) => setActive.mutate({ id: u.id, active: v })} />
              </label>
              {u.id !== user?.id && (
                <Button variant="ghost" size="icon" className="size-7"
                  onClick={() => { if (confirm(`Delete login account "${u.username}"? This cannot be undone.`)) remove.mutate(u.id) }}>
                  <Trash2 className="size-3.5" />
                </Button>
              )}
            </div>
          </div>
        ))}
        {addOpen && (
          <div className="flex flex-col gap-2.5 border-t p-4">
            <Input placeholder="username" value={username} onChange={(e) => setUsername(e.target.value)} />
            <Input placeholder="password (min 8 chars)" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)} />
            <Select value={role} onValueChange={(v) => setRole(v as Role)}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="owner">Owner</SelectItem>
                <SelectItem value="operator">Operator</SelectItem>
                <SelectItem value="tech">Tech</SelectItem>
              </SelectContent>
            </Select>
            {error && <p className="text-xs text-destructive">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setAddOpen(false)}>Cancel</Button>
              <Button size="sm" disabled={!username.trim() || password.length < 8 || create.isPending}
                onClick={() => create.mutate()}>
                Create
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function SettingsPage() {
  const { scopeTenant, canWrite } = useAuth()
  if (!scopeTenant) return <NeedsOrg />

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-6">
      <h1 className="text-xl font-bold">Settings</h1>
      <OrgSettingsCard tenant={scopeTenant} canWrite={canWrite} />
      {canWrite && <UsersCard tenant={scopeTenant} />}
    </div>
  )
}
