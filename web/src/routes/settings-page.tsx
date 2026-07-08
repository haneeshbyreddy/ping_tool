import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Copy, Dices, KeyRound, Plus, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { orgsApi, usersApi, ApiError } from "@/lib/api"
import type { AccountUser, Role } from "@/lib/types"
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
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"

const ROLE_TOPICS: Array<{ key: "owner" | "operator" | "tech"; label: string }> = [
  { key: "owner", label: "Owner" }, { key: "operator", label: "Operator" }, { key: "tech", label: "Tech" },
]

function randomTopic(role: string): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  let suffix = ""
  for (const b of bytes) suffix += alphabet[b % alphabet.length]
  return `wisp-${role}-${suffix}`
}

function OrgSettingsCard({ org, canWrite }: { org: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["orgs", org],
    queryFn: () => orgsApi.list(org),
    enabled: !!org,
  })
  const current = data?.orgs.find((o) => o.org_id === org)

  const [name, setName] = useState("")
  const [topics, setTopics] = useState({ owner: "", operator: "", tech: "" })
  const [testResults, setTestResults] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!current) return
    setName(current.name || "")

    setTopics({
      owner: current.ntfy_topic_owner || randomTopic("owner"),
      operator: current.ntfy_topic_operator || randomTopic("operator"),
      tech: current.ntfy_topic_tech || randomTopic("tech"),
    })
  }, [current])

  const save = useMutation({
    mutationFn: () => orgsApi.save({
      org_id: org, name: name.trim() || null,
      ntfy_topic_owner: topics.owner.trim() || null,
      ntfy_topic_operator: topics.operator.trim() || null,
      ntfy_topic_tech: topics.tech.trim() || null,
    }),
    onSuccess: () => { toast.success("Settings saved"); queryClient.invalidateQueries({ queryKey: ["orgs"] }) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "save failed"),
  })

  const test = useMutation({
    mutationFn: (role: "owner" | "operator" | "tech") => orgsApi.testAlert(org, role),
    onSuccess: (r, role) => setTestResults((t) => ({ ...t, [role]: r.ok ? "✓ sent" : `failed: ${r.detail || ""}` })),
    onError: (e, role) => setTestResults((t) => ({ ...t, [role]: e instanceof ApiError ? e.message : "failed" })),
  })

  if (isLoading) return <Skeleton className="h-48 w-full" />

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Organization &amp; alert routing</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Org name</Label>
          <Input value={name} disabled={!canWrite} onChange={(e) => setName(e.target.value)} className="max-w-sm" />
        </div>
        {ROLE_TOPICS.map(({ key, label }) => (
          <div key={key} className="flex flex-col gap-1.5">
            <Label>{label} ntfy topic</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                readOnly
                className="max-w-sm font-mono text-xs"
                value={topics[key]}
                onFocus={(e) => e.target.select()}
              />
              <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" title="Copy topic"
                onClick={() => { navigator.clipboard.writeText(topics[key]); toast.success("Topic copied") }}>
                <Copy className="size-3.5" />
              </Button>
              {canWrite && (
                <Button variant="outline" size="sm" title="Generate a new random topic"
                  onClick={() => setTopics({ ...topics, [key]: randomTopic(key) })}>
                  <Dices className="size-3.5" /> Randomize
                </Button>
              )}
              {canWrite && (
                <Button variant="outline" size="sm" disabled={test.isPending} onClick={() => test.mutate(key)}>
                  Send test
                </Button>
              )}
              {testResults[key] && <span className="text-xs text-muted-foreground">{testResults[key]}</span>}
            </div>
          </div>
        ))}
        <p className="max-w-lg text-xs text-muted-foreground">
          Topics are generated, not chosen — anyone who knows a topic name can subscribe to it
          on ntfy, so a random one is the only safe kind. Randomize, save, then re-subscribe
          the team's phones to the new topic.
        </p>
        {canWrite && (
          <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

function ChangePasswordCard() {
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirm, setConfirm] = useState("")
  const [error, setError] = useState("")

  const change = useMutation({
    mutationFn: () => usersApi.changePassword({ current_password: current, new_password: next }),
    onSuccess: () => {
      toast.success("Password changed")
      setCurrent(""); setNext(""); setConfirm(""); setError("")
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "failed to change password"),
  })

  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit = current.length > 0 && next.length >= 8 && next === confirm && !change.isPending

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <KeyRound className="size-4 text-muted-foreground" /> Your password
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        <div className="flex flex-col gap-1.5">
          <Label>Current password</Label>
          <Input type="password" autoComplete="current-password" className="max-w-sm"
            value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>New password</Label>
          <Input type="password" autoComplete="new-password" placeholder="min 8 characters" className="max-w-sm"
            value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Confirm new password</Label>
          <Input type="password" autoComplete="new-password" className="max-w-sm"
            value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {mismatch && <p className="text-xs text-destructive">Passwords don't match.</p>}
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Button size="sm" className="w-fit" disabled={!canSubmit} onClick={() => change.mutate()}>
          {change.isPending ? "Changing…" : "Change password"}
        </Button>
      </CardContent>
    </Card>
  )
}

function ResetPasswordDialog({ target }: { target: AccountUser }) {
  const [open, setOpen] = useState(false)
  const [next, setNext] = useState("")
  const [error, setError] = useState("")

  const reset = useMutation({
    mutationFn: () => usersApi.changePassword({ id: target.id, new_password: next }),
    onSuccess: () => {
      toast.success(`Password reset for ${target.username}`)
      setOpen(false); setNext(""); setError("")
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "failed to reset password"),
  })

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) { setNext(""); setError("") } }}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" className="size-7" title="Reset password">
          <KeyRound className="size-3.5" />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Reset password — {target.username}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <Label>New password</Label>
          <Input type="password" autoComplete="new-password" placeholder="min 8 characters"
            value={next} onChange={(e) => setNext(e.target.value)} autoFocus />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
          <Button size="sm" disabled={next.length < 8 || reset.isPending} onClick={() => reset.mutate()}>
            Reset
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function UsersCard({ org }: { org: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [error, setError] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["users", org],
    queryFn: () => usersApi.list(org),
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["users"] })

  const create = useMutation({
    mutationFn: () => usersApi.create({
      org_id: user?.is_superadmin ? org : undefined, username: username.trim(), password, role,
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
              {u.id !== user?.id && <ResetPasswordDialog target={u} />}
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
  const { scopeOrg, canWrite } = useAuth()

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 p-4 md:p-6">
      <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
      {scopeOrg && <OrgSettingsCard org={scopeOrg} canWrite={canWrite} />}
      <ChangePasswordCard />
      {scopeOrg && canWrite && <UsersCard org={scopeOrg} />}
      {!scopeOrg && <NeedsOrg />}
    </div>
  )
}
