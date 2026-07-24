import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import {
  Building2, Check, Copy, Dices, IndianRupee, KeyRound, MapPin, MessageCircle,
  Pencil, Plus, Radio, ServerCog, Trash2, X, type LucideIcon,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { cn } from "@/lib/utils"
import { adminApi, orgsApi, regionsApi, usersApi, ApiError } from "@/lib/api"
import { DEFAULT_MAP_REGION, MAP_REGIONS, mapRegionOf } from "@/lib/map-regions"
import type { AccountUser, Role } from "@/lib/types"
import { AppearanceCard } from "@/components/appearance-card"
import { BillingCard } from "@/components/billing-card"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { NeedsOrg } from "@/components/needs-org"
import { QrImage } from "@/components/qr-image"
import { SnmpProfilesCard } from "@/components/snmp-profiles-card"
import { TwoFactorCard } from "@/components/two-factor-card"
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

// Server-wide, superadmin-only: ONE Google Maps key lights up the Google
// basemaps on every org's Map view — individual ISPs never paste anything.
function GoogleMapsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const [key, setKey] = useState("")
  useEffect(() => { if (data) setKey(data.google_maps_key || "") }, [data])

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({ google_maps_key: key.trim() }),
    onSuccess: () => {
      toast.success("Google Maps key saved for all organizations")
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
      // every org's Map view reads the key off its /api/orgs row
      queryClient.invalidateQueries({ queryKey: ["orgs"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  if (isLoading) return <Skeleton className="h-24 w-full" />

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <MapPin className="size-4 text-muted-foreground" /> Google Maps (all organizations)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        <div className="flex flex-col gap-1.5">
          <Label>Map Tiles API key</Label>
          <Input value={key} placeholder="AIza…" className="max-w-sm font-mono text-xs"
            spellCheck={false} onChange={(e) => setKey(e.target.value)} />
        </div>
        <p className="max-w-lg text-xs text-muted-foreground">
          Pasted once here, this key enables the Google basemaps on every organization's
          Map view. Org owners don't configure anything. It is sent to signed-in
          browsers, so use a referrer-restricted key. Leave blank to hide the Google
          options everywhere.
        </p>
        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

// Server-wide, superadmin-only: how subscribers pay. Payment is manual — orgs
// pay the GPay number or scan the uploaded QR, tap "I've paid", and their name
// lands on the confirmations channel so the admin marks the month by hand.
function PlatformBillingCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const [gpay, setGpay] = useState("")
  const [paidTopic, setPaidTopic] = useState("")
  const [qr, setQr] = useState("")   // data URI, or "" for none
  const fileRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (data) {
      setGpay(data.billing_gpay_number || "")
      setPaidTopic(data.billing_paid_topic || "")
      setQr(data.billing_qr_image || "")
    }
  }, [data])

  const pickFile = (file?: File | null) => {
    if (!file) return
    // SVG counts (type image/svg+xml); some OSes report an empty type for it,
    // so accept by extension too
    const isImage = file.type.startsWith("image/") || /\.svg$/i.test(file.name)
    if (!isImage) { toast.error("Choose an image file (PNG, SVG or JPG)"); return }
    if (file.size > 400_000) { toast.error("Image too large — use a QR under 400 KB"); return }
    const reader = new FileReader()
    reader.onload = () => setQr(String(reader.result || ""))
    reader.onerror = () => toast.error("Couldn't read that file")
    reader.readAsDataURL(file)
  }

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({
      billing_gpay_number: gpay.trim(),
      billing_paid_topic: paidTopic.trim(),
      billing_qr_image: qr,
    }),
    onSuccess: () => {
      toast.success("Payment settings saved")
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
      queryClient.invalidateQueries({ queryKey: ["billing"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  if (isLoading) return <Skeleton className="h-24 w-full" />

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <IndianRupee className="size-4 text-muted-foreground" /> Payments (all organizations)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>GPay number</Label>
          <Input value={gpay} placeholder="10-digit GPay number" className="max-w-sm font-mono text-xs"
            spellCheck={false} onChange={(e) => setGpay(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            Shown on every paid org's lock screen and reminders. You mark months
            paid from Organizations → Billing once payment lands.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Payment QR (optional)</Label>
          <div className="flex items-center gap-3">
            {qr
              ? <QrImage src={qr} imgClassName="size-28 rounded-md border object-contain p-1" />
              : <div className="flex size-28 items-center justify-center rounded-md border bg-muted text-center text-2xs text-muted-foreground">No QR</div>}
            <div className="flex flex-col gap-2">
              <input ref={fileRef} type="file" accept="image/*,.svg" className="hidden"
                onChange={(e) => pickFile(e.target.files?.[0])} />
              <Button variant="outline" size="sm" className="w-fit"
                onClick={() => fileRef.current?.click()}>
                {qr ? "Replace QR" : "Upload QR"}
              </Button>
              {qr && (
                <Button variant="ghost" size="sm" className="w-fit text-muted-foreground"
                  onClick={() => setQr("")}>
                  <Trash2 className="size-3.5" /> Remove
                </Button>
              )}
            </div>
          </div>
          <p className="max-w-lg text-xs text-muted-foreground">
            A UPI QR image (PNG, SVG or JPG) orgs scan to pay, shown beside the
            GPay number on the lock screen — they can tap it to enlarge. Leave
            empty to show just the number.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Payment confirmations channel</Label>
          <Input value={paidTopic} placeholder="ntfy topic (e.g. wisp-payments-abc123)"
            className="max-w-sm font-mono text-xs" spellCheck={false}
            onChange={(e) => setPaidTopic(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            When an org taps "I've paid", their name is pushed to this ntfy topic
            so you can verify and mark the month. Leave empty to use your central
            admin channel.
          </p>
        </div>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

// Server-wide, superadmin-only: the experimental WhatsApp channel. This is the
// business sender (Meta Cloud API) config only — the per-person page numbers are
// on each login account (Accounts section), not here. WhatsApp is additive: it
// rides beside ntfy and can never break a page.
function WhatsAppCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const [enabled, setEnabled] = useState(false)
  const [phoneId, setPhoneId] = useState("")
  const [template, setTemplate] = useState("")
  const [lang, setLang] = useState("")
  const [apiVersion, setApiVersion] = useState("")
  const [token, setToken] = useState("")   // write-only; blank leaves the stored one
  // `whatsapp` is absent from an OLD backend that hasn't been restarted with this
  // feature — degrade to empty defaults rather than white-screening the page.
  const tokenSet = data?.whatsapp?.token_set ?? false

  useEffect(() => {
    const wa = data?.whatsapp
    if (!wa) return
    setEnabled(wa.enabled)
    setPhoneId(wa.phone_id)
    setTemplate(wa.template)
    setLang(wa.lang)
    setApiVersion(wa.api_version)
    setToken("")
  }, [data])

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({
      whatsapp: {
        enabled, phone_id: phoneId.trim(), template: template.trim(),
        lang: lang.trim(), api_version: apiVersion.trim(),
        ...(token.trim() ? { token: token.trim() } : {}),
      },
    }),
    onSuccess: () => {
      toast.success("WhatsApp settings saved")
      setToken("")
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  const clearToken = useMutation({
    mutationFn: () => adminApi.saveSettings({ whatsapp: { token_clear: true } }),
    onSuccess: () => {
      toast.success("WhatsApp token removed")
      setToken("")
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed"),
  })

  if (isLoading) return <Skeleton className="h-24 w-full" />

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <MessageCircle className="size-4 text-muted-foreground" /> WhatsApp alerts (experimental)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center justify-between gap-3 max-w-sm">
          <span className="flex flex-col">
            <span className="text-sm font-medium">Send alerts over WhatsApp</span>
            <span className="text-xs text-muted-foreground">Additive — ntfy keeps working either way</span>
          </span>
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </label>

        <div className="flex flex-col gap-1.5">
          <Label>Phone number ID</Label>
          <Input value={phoneId} placeholder="the PHONE_NUMBER_ID, not the phone number"
            className="max-w-sm font-mono text-xs" spellCheck={false}
            onChange={(e) => setPhoneId(e.target.value)} />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Access token</Label>
          <Input type="password" autoComplete="off" spellCheck={false}
            className="max-w-sm font-mono text-xs"
            placeholder={tokenSet ? "•••••••• stored — leave blank to keep" : "paste the Meta access token"}
            value={token} onChange={(e) => setToken(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            A permanent System User token with <code>whatsapp_business_messaging</code>.
            Stored server-side and never shown again.
            {tokenSet && (
              <> <button type="button" className="underline hover:text-foreground"
                onClick={() => clearToken.mutate()}>Remove stored token</button>.</>
            )}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 max-w-sm">
          <div className="flex flex-col gap-1.5">
            <Label>Template</Label>
            <Input value={template} placeholder="wisp_alert" className="font-mono text-xs"
              spellCheck={false} onChange={(e) => setTemplate(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Language</Label>
            <Input value={lang} placeholder="en" className="font-mono text-xs"
              spellCheck={false} onChange={(e) => setLang(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>API version</Label>
            <Input value={apiVersion} placeholder="v20.0" className="font-mono text-xs"
              spellCheck={false} onChange={(e) => setApiVersion(e.target.value)} />
          </div>
        </div>

        <p className="max-w-lg text-xs text-muted-foreground">
          The approved template's body must take 4 parameters:{" "}
          <code>{"🔻 {{1}} — {{2}} ({{3}}) · {{4}}"}</code> (subject, status, detail,
          time). Each person is paged on their own number, set on their account in
          Accounts. Use the "Send test" buttons under an org's alert routing to verify.
        </p>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
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
    onError: (e) => setError(e instanceof ApiError ? e.message : "Failed to change password"),
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
}

const SECTIONS: Array<{
  id: string
  label: string
  icon: LucideIcon
  visible: (c: SectionCtx) => boolean
  render: (c: SectionCtx) => React.ReactNode
}> = [
  {
    id: "billing",
    label: "Plan & billing",
    icon: IndianRupee,
    visible: (c) => !!c.org,
    render: (c) => <BillingCard org={c.org!} />,
  },
  {
    id: "organization",
    label: "Organization",
    icon: Building2,
    visible: (c) => !!c.org,
    render: (c) => (
      <>
        <OrgSettingsCard org={c.org!} canWrite={c.canWrite} />
        <RegionsCard org={c.org!} canWrite={c.canWrite} />
      </>
    ),
  },
  {
    id: "monitoring",
    label: "Monitoring",
    icon: Radio,
    // Vendor profiles: superadmin manages the global set, an org owner adds
    // org-local ones. A superadmin with no org scoped still manages globals.
    // Both cards render for exactly this predicate, so the section can't come
    // up empty. (It used to need a `hasWebProxy` data probe as well: a
    // read-only OPERATOR saw nothing here but the proxy card, so a role-only
    // predicate offered them a blank page. Roles collapsed to owner+worker on
    // 2026-07-21 and workers never reach the shell — every visitor here now
    // has canWrite, so the probe went with the role.)
    visible: (c) => c.canWrite && (!!c.org || c.isSuperadmin),
    render: (c) => (
      <>
        {/* renders nothing until the superadmin grants the org the capability */}
        {c.org && <WebProxyCard org={c.org} />}
        {c.canWrite && (c.org || c.isSuperadmin) && (
          <SnmpProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} />
        )}
        {c.canWrite && (c.org || c.isSuperadmin) && (
          <GponProfilesCard org={c.org} isSuperadmin={c.isSuperadmin} />
        )}
        {/* The vendors whose per-ONU dBm exists only on the OLT's own web page.
            Central-side, so it rides the same predicate as the two above rather
            than needing the web-proxy probe — a recipe is worth writing before
            the capability is granted. */}
        {c.canWrite && (c.org || c.isSuperadmin) && (
          <WebOpticsCard org={c.org} isSuperadmin={c.isSuperadmin} />
        )}
      </>
    ),
  },
  {
    id: "accounts",
    label: "Accounts",
    icon: KeyRound,
    visible: () => true, // everyone can at least change their own password
    render: (c) => (
      <>
        {c.org && c.canWrite && <UsersCard org={c.org} />}
        <ChangePasswordCard />
        <TwoFactorCard />
      </>
    ),
  },
  {
    id: "platform",
    label: "Platform",
    icon: ServerCog,
    visible: (c) => c.isSuperadmin,
    render: () => (
      <>
        <AppearanceCard />
        <GoogleMapsCard />
        <PlatformBillingCard />
        <WhatsAppCard />
      </>
    ),
  },
]

// The Settings nav order is EXPLICIT, not the array's incidental authoring
// order: broadest surface first (a superadmin's platform-wide config) down to
// the scoped org's own settings, so each role's landing section is also the
// first thing it sees. An id missing from this list sorts last, so adding a
// section is never silently promoted to the front.
const SECTION_ORDER = ["platform", "organization", "billing", "monitoring", "accounts"]
const sectionRank = (id: string) => {
  const i = SECTION_ORDER.indexOf(id)
  return i === -1 ? SECTION_ORDER.length : i
}

export function SettingsPage() {
  const { user, scopeOrg, canWrite } = useAuth()
  const isSuperadmin = !!user?.is_superadmin
  const { section } = useParams()
  const navigate = useNavigate()

  // Settings is owner/superadmin-only. A read-only worker gets no nav entry to
  // it, so this only catches a hand-typed URL — send them Home.
  if (user && !isSuperadmin && user.role === "worker") {
    return <Navigate to="/" replace />
  }

  const ctx: SectionCtx = { org: scopeOrg, canWrite, isSuperadmin }
  const shown = SECTIONS
    .filter((s) => s.visible(ctx))
    .sort((a, b) => sectionRank(a.id) - sectionRank(b.id))
  // Land on the role's EXPLICIT home section, never shown[0]: a superadmin at
  // "All orgs" used to sort onto Monitoring (vendor profiles) with Platform
  // buried last, and an owner onto billing. Falls through to the first shown
  // section only if that landing isn't visible in this context.
  const landingId = isSuperadmin ? "platform" : "organization"
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

        <div className="flex min-w-0 flex-col gap-4">{active.render(ctx)}</div>
      </div>
    </div>
  )
}
