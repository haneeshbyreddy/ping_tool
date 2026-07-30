import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { IndianRupee, MapPin, MessageCircle, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { adminApi, ApiError } from "@/lib/api"
import { AppearanceCard } from "@/components/appearance-card"
import { QrImage } from "@/components/qr-image"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"

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
// is sent to the admin WhatsApp number (Platform → WhatsApp) so the admin marks
// the month by hand.
function PlatformBillingCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const [gpay, setGpay] = useState("")
  const [qr, setQr] = useState("")   // data URI, or "" for none
  const fileRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (data) {
      setGpay(data.billing_gpay_number || "")
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

        <p className="max-w-lg text-xs text-muted-foreground">
          When an org taps "I've paid", their name is sent to the admin WhatsApp
          number (set in the WhatsApp card below) so you can verify and mark the
          month.
        </p>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

// Server-wide, superadmin-only: the experimental WhatsApp channel. This is the
// business sender (Meta Cloud API) config only — the per-person page numbers are
// on each login account (Accounts section), not here. WhatsApp is the SOLE alert
// channel (ntfy removed 2026-07-24); the admin number here also receives the
// superadmin ops pings (org 'I've paid' / churn / release-sync failing).
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
  const [adminNumber, setAdminNumber] = useState("")
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
    setAdminNumber(wa.admin_number)
    setToken("")
  }, [data])

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({
      whatsapp: {
        enabled, phone_id: phoneId.trim(), template: template.trim(),
        lang: lang.trim(), api_version: apiVersion.trim(),
        admin_number: adminNumber.trim(),
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
          <MessageCircle className="size-4 text-muted-foreground" /> WhatsApp alerts
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center justify-between gap-3 max-w-sm">
          <span className="flex flex-col">
            <span className="text-sm font-medium">Send alerts over WhatsApp</span>
            <span className="text-xs text-muted-foreground">The only alert channel — off means no pages go out</span>
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
            <Input value={template} placeholder="wisp_alert1" className="font-mono text-xs"
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

        <div className="flex flex-col gap-1.5">
          <Label>Admin ops number</Label>
          <Input value={adminNumber} placeholder="e.g. +919000000000"
            className="max-w-sm font-mono text-xs" spellCheck={false}
            onChange={(e) => setAdminNumber(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            The superadmin's WhatsApp number for platform ops pings &mdash; an org tapping
            "I've paid", a self-downgrade to Free, and release-mirror failures. It also
            joins every org's alert audience. Leave blank to skip the ops pings.
          </p>
        </div>

        <p className="max-w-lg text-xs text-muted-foreground">
          The approved template's body takes 4 parameters, in order: Device, Status,
          Detail, Time Logged (<code>{"{{1}}"}</code>&hellip;<code>{"{{4}}"}</code>). Each
          person is paged on their own number, set on their account in Accounts. Use the
          "Send test alert" button under an org's Settings &rarr; Organization to verify.
        </p>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

/** The platform plane's own settings — server-wide config that reads and writes
 *  `app_settings`, NOT the scoped org. It used to sit as a "Platform" section
 *  inside the org-scoped Settings page, which was a category error: everything
 *  else in Settings is one organization's config, these four cards are the whole
 *  server's. Lifting them onto their own top-level page (reached from the
 *  Platform nav group, beside Overview and Organizations) leaves a superadmin's
 *  Settings page identical to an owner's — Organization / Plan & billing /
 *  Monitoring / Users — which is the point: Settings is now unambiguously
 *  org-scoped for everyone.
 *
 *  Self-guards on superadmin like the other platform-plane pages; the nav item
 *  is superadminOnly, so this only catches a hand-typed URL. */
export function PlatformPage() {
  const { user } = useAuth()
  if (!user?.is_superadmin) return null

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Platform settings</h1>
        <p className="text-sm text-muted-foreground">
          Server-wide configuration that applies to every organization: the look of the
          dashboard, the Google Maps key, how subscribers pay, and the WhatsApp channel.
        </p>
      </div>

      <AppearanceCard />
      <GoogleMapsCard />
      <PlatformBillingCard />
      <WhatsAppCard />
    </div>
  )
}
