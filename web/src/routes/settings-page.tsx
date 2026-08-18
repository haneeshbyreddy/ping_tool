import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import {
  Building2, Check, KeyRound, MapPin, MessageCircle,
  Pencil, Plus, Radio, Trash2, Users, X, type LucideIcon,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { cn } from "@/lib/utils"
import { orgsApi, regionsApi, usersApi, ApiError } from "@/lib/api"
import { DEFAULT_MAP_REGION, MAP_REGIONS, mapRegionOf } from "@/lib/map-regions"
import type { AccountUser, Role } from "@/lib/types"
import { AssignmentCard } from "@/components/assignment-card"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { FieldTrackingCard } from "@/components/field-tracking-card"
import { NeedsOrg } from "@/components/needs-org"
import { SnmpProfilesCard } from "@/components/snmp-profiles-card"
import { GponProfilesCard } from "@/components/gpon-profiles-card"
import { WebOpticsCard } from "@/components/web-optics-card"
import { RadiusCard } from "@/components/radius-card"
import { WebProxyCard } from "@/components/web-proxy"
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

function OrgSettingsCard({ org, canWrite }: { org: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["orgs", org],
    queryFn: () => orgsApi.list(org),
    enabled: !!org,
  })
  const current = data?.orgs.find((o) => o.org_id === org)

  const [name, setName] = useState("")
  const [mapRegion, setMapRegion] = useState(DEFAULT_MAP_REGION)
  const [pollInterval, setPollInterval] = useState("")
  const [testResult, setTestResult] = useState("")

  useEffect(() => {
    if (!current) return
    setName(current.name || "")
    setMapRegion(mapRegionOf(current.map_region).key)
    setPollInterval(current.poll_interval_s ? String(current.poll_interval_s) : "")
  }, [current])

  const save = useMutation({
    mutationFn: () => orgsApi.save({
      org_id: org, name: name.trim() || null,
      map_region: mapRegion,
      poll_interval_s: pollInterval.trim() ? Number(pollInterval) : null,
    }),
    onSuccess: () => { toast.success("Settings saved"); queryClient.invalidateQueries({ queryKey: ["orgs"] }) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  const test = useMutation({
    mutationFn: () => orgsApi.testAlert(org),
    onSuccess: (r) => setTestResult(r.ok ? "✓ sent" : `Failed: ${r.detail || ""}`),
    onError: (e) => setTestResult(e instanceof ApiError ? e.message : "Failed"),
  })

  if (isLoading) return <Skeleton className="h-48 w-full" />

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Organization &amp; alerts</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Org name</Label>
          <Input value={name} disabled={!canWrite} onChange={(e) => setName(e.target.value)} className="max-w-sm" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Map area</Label>
          <Select value={mapRegion} onValueChange={setMapRegion} disabled={!canWrite}>
            <SelectTrigger className="w-full max-w-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              {MAP_REGIONS.map((r) => (
                <SelectItem key={r.key} value={r.key}>{r.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="max-w-lg text-xs text-muted-foreground">
            The Map view opens on this area and stays inside it. Pick your state so the
            map is your network, not the whole country.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Probe interval (seconds)</Label>
          <Input
            className="max-w-sm"
            type="number"
            min={10}
            max={120}
            placeholder="automatic (60s)"
            disabled={!canWrite}
            value={pollInterval}
            onChange={(e) => setPollInterval(e.target.value)}
          />
          <p className="max-w-lg text-xs text-muted-foreground">
            How often probes ping their devices and report back. A device is
            confirmed down after 3 failed cycles, so 30s is about 90s to a page.
            Blank is automatic (60s). Range 10 to 120 seconds, applied within one
            cycle. Lower is faster but more ICMP load on your gear.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="flex items-center gap-2">
            <MessageCircle className="size-3.5 text-muted-foreground" /> Alerts (WhatsApp)
          </Label>
          <p className="max-w-lg text-xs text-muted-foreground">
            Every owner and worker account with a WhatsApp number gets every alert.
            Set numbers under <span className="font-medium">Users</span> below; the
            Meta API config lives in <span className="font-medium">Platform</span>
            settings.
          </p>
          {canWrite && (
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={test.isPending} onClick={() => test.mutate()}>
                Send test alert
              </Button>
              {testResult && <span className="text-xs text-muted-foreground">{testResult}</span>}
            </div>
          )}
        </div>
        {canWrite && (
          <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

function RegionsCard({ org, canWrite }: { org: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const [newName, setNewName] = useState("")
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameTo, setRenameTo] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["regions", org],
    queryFn: () => regionsApi.list(org),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["regions"] })
    queryClient.invalidateQueries({ queryKey: ["inventory"] })
  }
  const add = useMutation({
    mutationFn: () => regionsApi.create(org, newName.trim()),
    onSuccess: () => { setNewName(""); invalidate() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to add region"),
  })
  const rename = useMutation({
    mutationFn: () => regionsApi.rename(org, renaming!, renameTo.trim()),
    onSuccess: () => {
      toast.success("Region renamed. Devices follow")
      setRenaming(null); invalidate()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to rename region"),
  })
  const remove = useMutation({
    mutationFn: (name: string) => regionsApi.remove(org, name),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to delete region"),
  })

  const regions = data?.regions ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <MapPin className="size-4 text-muted-foreground" /> Regions
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-10 w-full" /></div>}
        {!isLoading && regions.length === 0 && (
          <p className="px-4 pb-3 text-xs text-muted-foreground">
            No regions yet. Add one here, or pick "New region…" while editing a device.
          </p>
        )}
        {regions.map((r) => {
          const inUse = r.device_count
          const usage = inUse > 0 ? `${inUse} device${inUse === 1 ? "" : "s"}` : ""
          if (canWrite && renaming === r.name) {
            return (
              <div key={r.name} className="flex items-center gap-2 border-t px-4 py-2 first:border-t-0">
                <Input autoFocus className="h-8 max-w-48" value={renameTo}
                  onChange={(e) => setRenameTo(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && renameTo.trim()) rename.mutate() }} />
                <Button variant="ghost" size="icon" className="size-7"
                  disabled={!renameTo.trim() || rename.isPending} onClick={() => rename.mutate()}>
                  <Check className="size-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="size-7" onClick={() => setRenaming(null)}>
                  <X className="size-3.5" />
                </Button>
              </div>
            )
          }
          return (
            <div key={r.name} className="group flex h-10 items-center gap-3 border-t px-4 first:border-t-0">
              <span className="min-w-0 truncate text-sm font-medium">{r.name}</span>
              <span className="text-xs text-muted-foreground">{usage || "unused"}</span>
              {canWrite && (
                <div className="ml-auto flex shrink-0 items-center gap-1 opacity-60 group-hover:opacity-100">
                  <Button variant="ghost" size="icon" className="size-7" title="Rename (devices and members follow)"
                    onClick={() => { setRenaming(r.name); setRenameTo(r.name) }}>
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button variant="ghost" size="icon" className="size-7" disabled={inUse > 0 || remove.isPending}
                    title={inUse > 0 ? "In use. Reassign its devices/members first" : "Delete region"}
                    onClick={() => remove.mutate(r.name)}>
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              )}
            </div>
          )
        })}
        {canWrite && (
          <div className="flex items-center gap-2 border-t p-4">
            <Input placeholder="new region, e.g. north-dc" className="h-8 max-w-56" value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && newName.trim()) add.mutate() }} />
            <Button size="sm" variant="outline" disabled={!newName.trim() || add.isPending}
              onClick={() => add.mutate()}>
              <Plus className="size-3.5" /> Add
            </Button>
          </div>
        )}
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
    onError: (e) => setError(e instanceof ApiError ? e.message : "Failed to reset password"),
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
          <DialogTitle>Reset password: {target.username}</DialogTitle>
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

function WhatsappField({ u }: { u: AccountUser }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState(u.whatsapp_number || "")
  useEffect(() => { setValue(u.whatsapp_number || "") }, [u.whatsapp_number])

  const save = useMutation({
    mutationFn: (num: string) => usersApi.setWhatsapp(num, u.id),
    onSuccess: (r) => {
      setValue(r.whatsapp_number || "")
      queryClient.invalidateQueries({ queryKey: ["users"] })
      toast.success(r.whatsapp_number ? "WhatsApp number saved" : "WhatsApp number cleared")
    },
    onError: (e) => {
      setValue(u.whatsapp_number || "")
      toast.error(e instanceof ApiError ? e.message : "Failed to save number")
    },
  })

  const dirty = value.trim() !== (u.whatsapp_number || "")
  return (
    <div className="flex items-center gap-2">
      <MessageCircle className="size-3.5 shrink-0 text-muted-foreground" />
      <Input
        className="h-8 max-w-64 font-mono text-xs"
        placeholder="WhatsApp, e.g. +919000000000"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur() }}
        onBlur={() => { if (dirty && !save.isPending) save.mutate(value.trim()) }}
      />
      {dirty && <span className="text-2xs text-muted-foreground">unsaved</span>}
    </div>
  )
}

function UsersCard({ org }: { org: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<Role>("worker")
  const [error, setError] = useState("")
  const [deleting, setDeleting] = useState<AccountUser | null>(null)

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
    onError: (e) => setError(e instanceof ApiError ? e.message : "Failed to create"),
  })
  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: number; active: boolean }) => usersApi.setActive(id, active),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to update"),
  })
  const remove = useMutation({
    mutationFn: (id: number) => usersApi.remove(id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to delete"),
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
          <div key={u.id} className="flex flex-col gap-2 border-t px-4 py-2.5 first:border-t-0">
            <div className="flex items-center justify-between gap-2">
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
                  <Button variant="ghost" size="icon" className="size-7" title="Delete account"
                    onClick={() => setDeleting(u)}>
                    <Trash2 className="size-3.5" />
                  </Button>
                )}
              </div>
            </div>
            <WhatsappField u={u} />
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
                <SelectItem value="owner">Owner · full access</SelectItem>
                <SelectItem value="worker">Worker · field app, triage only</SelectItem>
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
        <ConfirmDialog
          open={!!deleting}
          onOpenChange={(o) => { if (!o) setDeleting(null) }}
          title={`Delete login account ${deleting?.username ?? ""}?`}
          description="They are signed out and can no longer log in. This cannot be undone."
          onConfirm={() => { if (deleting) remove.mutate(deleting.id) }}
        />
      </CardContent>
    </Card>
  )
}

type SectionCtx = {
  org: string | null
  canWrite: boolean
  isSuperadmin: boolean
  hasWebProxy: boolean
}

type Panel = {
  id: string
  label: string
  visible?: (c: SectionCtx) => boolean
  render: (c: SectionCtx) => React.ReactNode
}

const SECTIONS: Array<{
  id: string
  label: string
  icon: LucideIcon
  visible: (c: SectionCtx) => boolean
  panels: Panel[]
}> = [
  {
    id: "organization",
    label: "Organization",
    icon: Building2,
    visible: (c) => !!c.org,
    panels: [
      {
        id: "org-routing", label: "Organization & alerts",
        render: (c) => <OrgSettingsCard org={c.org!} canWrite={c.canWrite} />,
      },
      { id: "regions", label: "Regions", render: (c) => <RegionsCard org={c.org!} canWrite={c.canWrite} /> },
    ],
  },
  {
    id: "monitoring",
    label: "Monitoring",
    icon: Radio,
    visible: (c) => c.canWrite && (!!c.org || c.isSuperadmin),
    panels: [
      {
        id: "web-proxy", label: "Device web UI sessions",
        visible: (c) => !!c.org && c.hasWebProxy,
        render: (c) => <WebProxyCard org={c.org!} />,
      },
      {
        id: "radius", label: "Billing / RADIUS panel",
        visible: (c) => !!c.org,
        render: (c) => <RadiusCard org={c.org!} />,
      },
      // Authoring a recipe is a platform-admin job (every profile WRITE route is
      // superadmin-only), so these two panels are hidden rather than shown as a
      // form that 403s. Selecting a vendor stays with the ISP: that lives on the
      // device form and in the cards above, and is deliberately not gated here.
      {
        id: "snmp-profiles", label: "SNMP health profiles",
        visible: (c) => c.isSuperadmin,
        render: (c) => <SnmpProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} />,
      },
      {
        id: "gpon-profiles", label: "GPON vendor profiles",
        visible: (c) => c.isSuperadmin,
        render: (c) => <GponProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} />,
      },
      { id: "web-optics", label: "Web-UI optics vendors", render: (c) => <WebOpticsCard org={c.org} isSuperadmin={c.isSuperadmin} /> },
    ],
  },
  {
    id: "accounts",
    label: "Users",
    icon: Users,
    visible: (c) => !!c.org && c.canWrite,
    panels: [
      { id: "users", label: "Login accounts", render: (c) => <UsersCard org={c.org!} /> },
      { id: "assignments", label: "Device responsibility", render: (c) => <AssignmentCard org={c.org!} /> },
      { id: "tracking", label: "Location tracking", render: (c) => <FieldTrackingCard org={c.org!} /> },
    ],
  },
]

// Billing left this table on 2026-08-17: it stopped being a plan you pick and
// became a ledger with its own page (/billing, off the account menu).
const SECTION_ORDER = ["organization", "monitoring", "accounts"]
const sectionRank = (id: string) => {
  const i = SECTION_ORDER.indexOf(id)
  return i === -1 ? SECTION_ORDER.length : i
}

export function SettingsPage() {
  const { user, scopeOrg, canWrite } = useAuth()
  const isSuperadmin = !!user?.is_superadmin
  const { section } = useParams()
  const navigate = useNavigate()

  const { data: orgsData } = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg && canWrite,
  })
  const hasWebProxy = !!orgsData?.orgs.find((o) => o.org_id === scopeOrg)?.web_proxy

  if (user && !isSuperadmin && user.role === "worker") {
    return <Navigate to="/" replace />
  }

  const ctx: SectionCtx = { org: scopeOrg, canWrite, isSuperadmin, hasWebProxy }
  const panelsOf = (s: (typeof SECTIONS)[number]) =>
    s.panels.filter((p) => (p.visible ? p.visible(ctx) : true))
  // A section whose cards all gate themselves off renders as a blank tab, which
  // a role-only predicate caused here once. The nav entry is DERIVED from the
  // panels that actually render, so a section predicate cannot drift from the
  // conditions its own cards gate on.
  const shown = SECTIONS
    .filter((s) => s.visible(ctx) && panelsOf(s).length > 0)
    .sort((a, b) => sectionRank(a.id) - sectionRank(b.id))
  const landingId = "organization"
  const active =
    shown.find((s) => s.id === section) ??
    shown.find((s) => s.id === landingId) ??
    shown[0]

  if (!scopeOrg && !isSuperadmin) {
    return (
      <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
        <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        <NeedsOrg />
      </div>
    )
  }
  if (!active) return null

  const panels = panelsOf(active)

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <h1 className="text-lg font-semibold tracking-tight">Settings</h1>

      <div className="flex flex-col gap-4 md:grid md:grid-cols-[11rem_minmax(0,1fr)] md:items-start md:gap-6">
        <nav className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 md:sticky md:top-4 md:mx-0 md:flex-col md:overflow-visible md:px-0 md:pb-0">
          {shown.map((s) => {
            const on = s.id === active.id
            return (
              <button
                key={s.id}
                onClick={() => navigate(`/settings/${s.id}`)}
                aria-current={on ? "page" : undefined}
                className={cn(
                  "flex h-8 shrink-0 items-center gap-2 rounded-lg px-3 text-xs font-medium whitespace-nowrap transition-colors md:w-full md:justify-start",
                  on
                    ? "bg-foreground/[0.07] text-foreground shadow-[inset_2px_0_0_var(--foreground)]"
                    : "text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
                )}
              >
                <s.icon className="size-3.5 shrink-0" />
                {s.label}
              </button>
            )
          })}
        </nav>

        <div className="flex min-w-0 flex-col gap-4">
          {panels.length > 1 && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <span className="text-2xs font-medium tracking-wide text-faint-foreground uppercase">
                On this page
              </span>
              {panels.map((p) => (
                <button
                  key={p.id}
                  onClick={() =>
                    document.getElementById(`panel-${p.id}`)
                      ?.scrollIntoView({ behavior: "smooth", block: "start" })
                  }
                  className="rounded-md border px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground"
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}
          {panels.map((p) => (
            <div key={p.id} id={`panel-${p.id}`} className="flex scroll-mt-4 flex-col gap-4">
              {p.render(ctx)}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
