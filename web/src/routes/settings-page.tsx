import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import {
  Building2, Check, Copy, Dices, IndianRupee, KeyRound, MapPin, MessageCircle,
  Pencil, Plus, Radio, Trash2, Users, X, type LucideIcon,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { cn } from "@/lib/utils"
import { orgsApi, regionsApi, usersApi, ApiError } from "@/lib/api"
import { DEFAULT_MAP_REGION, MAP_REGIONS, mapRegionOf } from "@/lib/map-regions"
import type { AccountUser, Role } from "@/lib/types"
import { BillingCard } from "@/components/billing-card"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { NeedsOrg } from "@/components/needs-org"
import { SnmpProfilesCard } from "@/components/snmp-profiles-card"
import { GponProfilesCard } from "@/components/gpon-profiles-card"
import { WebOpticsCard } from "@/components/web-optics-card"
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

const ROLE_TOPICS: Array<{ key: "owner" | "worker"; label: string; hint: string }> = [
  { key: "owner", label: "Owner", hint: "Device and uplink outages" },
  { key: "worker", label: "Worker", hint: "Fibre/ONU/port findings, the hourly re-nag and the digest" },
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
  const [topics, setTopics] = useState({ owner: "", worker: "" })
  const [mapRegion, setMapRegion] = useState(DEFAULT_MAP_REGION)
  const [pollInterval, setPollInterval] = useState("")
  const [testResults, setTestResults] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!current) return
    setName(current.name || "")
    setMapRegion(mapRegionOf(current.map_region).key)
    setPollInterval(current.poll_interval_s ? String(current.poll_interval_s) : "")

    setTopics({
      owner: current.ntfy_topic_owner || randomTopic("owner"),
      worker: current.ntfy_topic_worker || randomTopic("worker"),
    })
  }, [current])

  const save = useMutation({
    mutationFn: () => orgsApi.save({
      org_id: org, name: name.trim() || null,
      ntfy_topic_owner: topics.owner.trim() || null,
      ntfy_topic_worker: topics.worker.trim() || null,
      map_region: mapRegion,
      poll_interval_s: pollInterval.trim() ? Number(pollInterval) : null,
    }),
    onSuccess: () => { toast.success("Settings saved"); queryClient.invalidateQueries({ queryKey: ["orgs"] }) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  const test = useMutation({
    mutationFn: (role: "owner" | "worker") => orgsApi.testAlert(org, role),
    onSuccess: (r, role) => setTestResults((t) => ({ ...t, [role]: r.ok ? "✓ sent" : `Failed: ${r.detail || ""}` })),
    onError: (e, role) => setTestResults((t) => ({ ...t, [role]: e instanceof ApiError ? e.message : "Failed" })),
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
            How often every probe in this org pings its devices and reports back.
            Each cycle is one ping sweep plus one report, and outage detection speed
            follows it (a device is confirmed DOWN after 3 failed cycles, so 30s
            &asymp; 90s to a page, 60s &asymp; 3 min). Probes pick a change up within
            one cycle, no restart. 10&ndash;120s; blank = automatic (60s). Lower is
            faster detection but more ICMP load on your gear.
          </p>
        </div>
        {ROLE_TOPICS.map(({ key, label, hint }) => (
          <div key={key} className="flex flex-col gap-1.5">
            <Label>{label} ntfy topic</Label>
            <p className="text-xs text-muted-foreground">{hint}</p>
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
          Topics are generated, not chosen. Anyone who knows a topic name can subscribe to it
          on ntfy, so a random one is the only safe kind. Randomize, save, then re-subscribe
          the team's phones to the new topic.
        </p>
        <p className="max-w-lg text-xs text-muted-foreground">
          If WhatsApp alerts are enabled (a platform setting), each role is also paged on the
          WhatsApp numbers of its accounts &mdash; owners on owner accounts, workers on worker
          accounts. Set those under <span className="font-medium">Accounts</span>.
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
    // a rename cascades onto device rows
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

// Per-account WhatsApp page number, saved on blur/Enter. The account list is
// where an owner sets these for the whole team (workers never reach Settings);
// each person is then paged on their own number for their role's alerts.
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
                <SelectItem value="owner">Owner — full access</SelectItem>
                <SelectItem value="worker">Worker — field app, triage only</SelectItem>
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

/** What the page can show, in nav order.
 *
 *  Settings was ten cards in one scroll — every visit meant re-scanning the
 *  whole page to find the one field you came for, and the ordering had to serve
 *  both "most important" and "where things live" at once. Sections give each
 *  concern a stable address you can learn once and go straight to.
 *
 *  `visible` decides whether the section is offered AT ALL: a section that
 *  renders to nothing must never appear in the nav, or the page teaches you a
 *  destination and then shows you an empty room.
 */
type SectionCtx = {
  org: string | null
  canWrite: boolean
  isSuperadmin: boolean
  /** web-proxy capability grant for the scoped org (superadmin-set). The
   *  Monitoring "Device web UI sessions" panel self-nulls without it, so both
   *  that panel and its jump-index entry ride this flag — otherwise the index
   *  would advertise an anchor that scrolls to nothing. */
  hasWebProxy: boolean
}

/** One card within a section. A section is a LIST of named panels rather than a
 *  single opaque `render`, which is what lets the page offer a per-sub-page "On
 *  this page" jump index: it can name a section's panels and scroll straight to
 *  one instead of the operator hunting down a tall column. `visible` defaults to
 *  shown — only a panel with its OWN extra gate beyond the section's (Web proxy)
 *  needs one. */
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
    id: "billing",
    label: "Plan & billing",
    icon: IndianRupee,
    visible: (c) => !!c.org,
    panels: [
      { id: "billing", label: "Plan & billing", render: (c) => <BillingCard org={c.org!} /> },
    ],
  },
  {
    id: "organization",
    label: "Organization",
    icon: Building2,
    visible: (c) => !!c.org,
    panels: [
      {
        id: "org-routing", label: "Organization & alert routing",
        render: (c) => <OrgSettingsCard org={c.org!} canWrite={c.canWrite} />,
      },
      { id: "regions", label: "Regions", render: (c) => <RegionsCard org={c.org!} canWrite={c.canWrite} /> },
    ],
  },
  {
    id: "monitoring",
    label: "Monitoring",
    icon: Radio,
    // Vendor profiles: superadmin manages the global set, an org owner adds
    // org-local ones. A superadmin with no org scoped still manages globals.
    // The three profile cards render for exactly this predicate, so the section
    // can't come up empty. (It once needed a `hasWebProxy` data probe to gate
    // the whole section: a read-only OPERATOR saw nothing here but the proxy
    // card, so a role-only predicate offered them a blank page. Roles collapsed
    // to owner+worker on 2026-07-21 and workers never reach the shell — every
    // visitor here now has canWrite. The probe is back, but ONLY to gate the
    // proxy PANEL and its index entry, never the whole section.)
    visible: (c) => c.canWrite && (!!c.org || c.isSuperadmin),
    panels: [
      // Self-nulls until the superadmin grants the org the capability, so both
      // the card and its index entry ride the grant.
      {
        id: "web-proxy", label: "Device web UI sessions",
        visible: (c) => !!c.org && c.hasWebProxy,
        render: (c) => <WebProxyCard org={c.org!} />,
      },
      { id: "snmp-profiles", label: "SNMP health profiles", render: (c) => <SnmpProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} /> },
      { id: "gpon-profiles", label: "GPON vendor profiles", render: (c) => <GponProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} /> },
      // The vendors whose per-ONU dBm exists only on the OLT's own web page.
      // Central-side, so it rides the section predicate rather than the proxy
      // grant — a recipe is worth writing before the capability is granted.
      { id: "web-optics", label: "Web-UI optics vendors", render: (c) => <WebOpticsCard org={c.org} isSuperadmin={c.isSuperadmin} /> },
    ],
  },
  {
    id: "accounts",
    label: "Users",
    icon: Users,
    // Org member accounts only. Personal password / 2FA / WhatsApp moved to the
    // "Your account" page (routes/account-page.tsx), reachable by every role
    // from the account menu — so a worker (who never opens Settings) can still
    // change its own password.
    visible: (c) => !!c.org && c.canWrite,
    panels: [
      { id: "users", label: "Login accounts", render: (c) => <UsersCard org={c.org!} /> },
    ],
  },
]

// The Settings nav order is EXPLICIT, not the array's incidental authoring
// order: Organization first (the section every role lands on), then plan &
// billing, monitoring and users. Settings is org-scoped for everyone now — the
// superadmin's server-wide config moved to its own /platform page — so there is
// no longer a platform-wide section to sort ahead of the org's own. An id
// missing from this list sorts last, so adding a section is never silently
// promoted to the front.
const SECTION_ORDER = ["organization", "billing", "monitoring", "accounts"]
const sectionRank = (id: string) => {
  const i = SECTION_ORDER.indexOf(id)
  return i === -1 ? SECTION_ORDER.length : i
}

export function SettingsPage() {
  const { user, scopeOrg, canWrite } = useAuth()
  const isSuperadmin = !!user?.is_superadmin
  const { section } = useParams()
  const navigate = useNavigate()

  // web_proxy grant for the scoped org — drives whether the Monitoring "Device
  // web UI sessions" panel renders (the card self-nulls without it), so the jump
  // index can gate its entry on the same flag. Same ["orgs", org] query
  // OrgSettingsCard/WebProxyCard already fetch, so it's a cache hit; gated on
  // canWrite so a worker's brief hand-typed-URL render doesn't fire a 403.
  const { data: orgsData } = useQuery({
    queryKey: ["orgs", scopeOrg],
    queryFn: () => orgsApi.list(scopeOrg),
    enabled: !!scopeOrg && canWrite,
  })
  const hasWebProxy = !!orgsData?.orgs.find((o) => o.org_id === scopeOrg)?.web_proxy

  // Settings is owner/superadmin-only. A read-only worker gets no nav entry to
  // it, so this only catches a hand-typed URL — send them Home.
  if (user && !isSuperadmin && user.role === "worker") {
    return <Navigate to="/" replace />
  }

  const ctx: SectionCtx = { org: scopeOrg, canWrite, isSuperadmin, hasWebProxy }
  const shown = SECTIONS
    .filter((s) => s.visible(ctx))
    .sort((a, b) => sectionRank(a.id) - sectionRank(b.id))
  // Land on an EXPLICIT home section, never shown[0] (an owner used to sort onto
  // billing). Settings is org-scoped for every role now — the superadmin's
  // server-wide config moved to its own /platform page — so both a superadmin
  // and an owner land on Organization. Falls through to the first shown section
  // only if that landing isn't visible in this context (e.g. a superadmin at
  // "All orgs" with no org scoped, who sees only global Monitoring).
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

  // The panels this section actually renders in this context — drives both the
  // cards below and the "On this page" jump index (so the two never disagree).
  const panels = active.panels.filter((p) => (p.visible ? p.visible(ctx) : true))

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <h1 className="text-lg font-semibold tracking-tight">Settings</h1>

      {/* Two-column on desktop, a scrollable rail of chips on mobile — the same
          section list either way, so the mental model doesn't change with the
          viewport. */}
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
          {/* "On this page" jump index — only worth drawing when a section has
              more than one panel to jump between; on a single card it's noise.
              A plain scrollIntoView (not an href="#…" anchor, which HashRouter
              would hijack) so the router stays out of it. */}
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
