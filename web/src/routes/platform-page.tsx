import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Check, Copy, IndianRupee, Map, MapPin, MessageCircle, Minus, Plus, RotateCcw,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/hooks/use-auth"
import { adminApi, ApiError } from "@/lib/api"
import type { PaymentSettings } from "@/lib/types"
import {
  DETAIL_DEFAULTS, DETAIL_MAX, DETAIL_ROWS, detailFrom, detailMin,
  isDetailDefault, normalizeDetail, type MapDetail,
} from "@/map/detail"
import { AppearanceCard } from "@/components/appearance-card"
import { BillingConsolePanel } from "@/components/billing-admin"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

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
          Enables the Google basemaps on every organization's Map. It is sent to
          signed-in browsers, so use a referrer-restricted key. Blank hides the Google
          options everywhere.
        </p>
        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

function MapDetailCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const [detail, setDetail] = useState<MapDetail>(DETAIL_DEFAULTS)
  useEffect(() => { if (data?.map_detail) setDetail(detailFrom(data.map_detail)) }, [data])

  const save = useMutation({
    mutationFn: (d: MapDetail) => adminApi.saveSettings({ map_detail: d }),
    onSuccess: () => {
      toast.success("Map detail saved for all organizations")
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
      queryClient.invalidateQueries({ queryKey: ["orgs"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })
  const set = (k: keyof MapDetail, v: number) =>
    setDetail((d) => normalizeDetail({ ...d, [k]: v }))

  if (isLoading) return <Skeleton className="h-24 w-full" />

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Map className="size-4 text-muted-foreground" /> Map detail (all organizations)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        <p className="max-w-lg text-xs text-muted-foreground">
          The zoom level each map layer starts drawing at. Lower shows more while
          zoomed out, higher keeps the map clearer. Zoom runs roughly 4 (country)
          · 10 (city) · 14 (neighbourhood) · 17 (street).
        </p>
        <div className="flex flex-col gap-1.5">
          {DETAIL_ROWS.map((r) => {
            const v = detail[r.key]
            const lo = detailMin(detail, r.key)
            return (
              <div key={r.key} className="flex items-start justify-between gap-4 max-w-lg">
                <div className="min-w-0">
                  <Label className="text-xs">{r.label}</Label>
                  <p className="text-2xs text-muted-foreground">{r.hint}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="outline" size="icon" className="size-7"
                    disabled={v <= lo} aria-label={`${r.label}: show earlier`}
                    onClick={() => set(r.key, v - 1)}>
                    <Minus className="size-3" />
                  </Button>
                  <span className="w-6 text-center text-sm font-medium tabular-nums">{v}</span>
                  <Button variant="outline" size="icon" className="size-7"
                    disabled={v >= DETAIL_MAX} aria-label={`${r.label}: show later`}
                    onClick={() => set(r.key, v + 1)}>
                    <Plus className="size-3" />
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" className="w-fit" disabled={save.isPending}
            onClick={() => save.mutate(detail)}>
            Save
          </Button>
          {!isDetailDefault(detail) && (
            <Button variant="ghost" size="sm" className="w-fit text-muted-foreground"
              disabled={save.isPending}
              onClick={() => { setDetail(DETAIL_DEFAULTS); save.mutate(DETAIL_DEFAULTS) }}>
              <RotateCcw className="size-3" /> Reset to defaults
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

// The Select cannot carry "" as an item value (Radix reserves it for "no
// selection"), so the explicit off option travels as this sentinel and becomes
// "" on the wire. Off is a real, chosen state, not an empty form.
const NO_PROVIDER = "__off__"

/** What is configured and what is missing, composed from the SAVED settings
 *  (never the typed form, which would claim a secret is stored the moment it
 *  is keyed). Ranked by COST, not by order of setup: a half-configured
 *  gateway that takes money it cannot record outranks one that simply refuses
 *  to open, and both outrank the deliberate off state. */
function gatewayStatus(p: PaymentSettings): { text: string; className: string } {
  if (!p.provider) {
    return {
      className: "bg-muted text-muted-foreground",
      text: "Payments off. Orgs see the amount and are told to contact you.",
    }
  }
  if (!p.webhook_secret_set) {
    return {
      className: "bg-destructive/10 text-destructive",
      text: "Checkout can take money that never reaches this ledger: no webhook secret stored.",
    }
  }
  const missing: string[] = []
  if (!p.key_id) missing.push("key id")
  if (!p.key_secret_set) missing.push("key secret")
  if (missing.length) {
    return {
      className: "bg-warning-soft text-warning",
      text: `Nobody can pay: no ${missing.join(" and ")} stored.`,
    }
  }
  return {
    className: "bg-success-soft text-success",
    text: `Live on ${p.provider}. Checkout and webhook are both configured.`,
  }
}

function PaymentsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })
  const pay = data?.payments
  const [provider, setProvider] = useState(NO_PROVIDER)
  const [keyId, setKeyId] = useState("")
  // Write-only, exactly like the WhatsApp token below: blank LEAVES the stored
  // secret alone, so these never carry a value back from the server and reset
  // to blank after every save.
  const [keySecret, setKeySecret] = useState("")
  const [webhookSecret, setWebhookSecret] = useState("")
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!pay) return
    setProvider(pay.provider || NO_PROVIDER)
    setKeyId(pay.key_id)
    setKeySecret("")
    setWebhookSecret("")
  }, [pay])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
    // The pay button on every org's billing page reads this config.
    queryClient.invalidateQueries({ queryKey: ["billing"] })
  }

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({
      payments: {
        provider: provider === NO_PROVIDER ? "" : provider,
        key_id: keyId.trim(),
        ...(keySecret.trim() ? { key_secret: keySecret.trim() } : {}),
        ...(webhookSecret.trim() ? { webhook_secret: webhookSecret.trim() } : {}),
      },
    }),
    onSuccess: () => {
      toast.success("Payment settings saved")
      setKeySecret(""); setWebhookSecret("")
      invalidate()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  const clearSecret = useMutation({
    mutationFn: (which: "key_secret" | "webhook_secret") =>
      adminApi.saveSettings({ payments: { [`${which}_clear`]: true } }),
    onSuccess: (_r, which) => {
      toast.success(which === "key_secret" ? "Key secret removed" : "Webhook secret removed")
      invalidate()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed"),
  })

  const webhookUrl = `${window.location.origin}/payments/webhook`
  const copyWebhook = () => {
    navigator.clipboard.writeText(webhookUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  if (isLoading || !pay) return <Skeleton className="h-24 w-full" />
  const status = gatewayStatus(pay)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <IndianRupee className="size-4 text-muted-foreground" /> Payment gateway (all organizations)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <span className={cn("w-fit rounded-md px-2 py-1 text-xs font-medium", status.className)}>
          {status.text}
        </span>

        <div className="flex flex-col gap-1.5">
          <Label>Provider</Label>
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger className="w-full max-w-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={NO_PROVIDER}>No gateway · payments off</SelectItem>
              {pay.providers.map((p) => (
                <SelectItem key={p} value={p} className="capitalize">{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="max-w-lg text-xs text-muted-foreground">
            Off is a working state, not a broken one: orgs still see what they
            owe and are asked to contact you. Invoices and the ledger run either
            way.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Key id</Label>
          <Input value={keyId} placeholder="rzp_live_…" className="max-w-sm font-mono text-xs"
            spellCheck={false} onChange={(e) => setKeyId(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            Public by design. It is handed to the browser to open checkout, so
            treat it as visible to every org. The secret below is the half that
            must stay here.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Key secret</Label>
          <Input type="password" autoComplete="off" spellCheck={false}
            className="max-w-sm font-mono text-xs"
            placeholder={pay.key_secret_set
              ? "•••••••• stored · leave blank to keep"
              : "paste the gateway key secret"}
            value={keySecret} onChange={(e) => setKeySecret(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            Central signs in with it to open the order, and checks the browser's
            return against it. Stored encrypted and never shown again.
            {pay.key_secret_set && (
              <> <button type="button" className="underline hover:text-foreground"
                disabled={clearSecret.isPending}
                onClick={() => clearSecret.mutate("key_secret")}>Remove stored key secret</button>.</>
            )}
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Webhook secret</Label>
          <Input type="password" autoComplete="off" spellCheck={false}
            className="max-w-sm font-mono text-xs"
            placeholder={pay.webhook_secret_set
              ? "•••••••• stored · leave blank to keep"
              : "paste the webhook signing secret"}
            value={webhookSecret} onChange={(e) => setWebhookSecret(e.target.value)} />
          <p className="max-w-lg text-xs text-muted-foreground">
            The webhook is the ONLY path that records a payment: a browser
            coming back from checkout is treated as "processing" until the
            gateway's own call lands. Without this secret central cannot verify
            that call, so no money is ever posted.
            {pay.webhook_secret_set && (
              <> <button type="button" className="underline hover:text-foreground"
                disabled={clearSecret.isPending}
                onClick={() => clearSecret.mutate("webhook_secret")}>Remove stored webhook secret</button>.</>
            )}
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Webhook URL</Label>
          <div className="flex max-w-lg items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md border bg-muted px-2.5 py-1.5 font-mono text-xs">
              {webhookUrl}
            </code>
            <Button variant="outline" size="sm" className="shrink-0" onClick={copyWebhook}>
              {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <p className="max-w-lg text-xs text-muted-foreground">
            Paste this into the gateway dashboard and subscribe it to
            <code className="mx-1 font-mono">payment.captured</code>
            (<code className="font-mono">payment.failed</code> is read too).
            Until it is set there, payments reach the gateway and never reach
            this ledger.
          </p>
        </div>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

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
            <span className="text-xs text-muted-foreground">The only alert channel. Off means no pages go out.</span>
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
            placeholder={tokenSet ? "•••••••• stored · leave blank to keep" : "paste the Meta access token"}
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
            Platform ops pings only: the daily billing digest and release-mirror
            failures. It is also the number an org is told to contact while the
            payment gateway is off. Org alerts never come here. Leave blank to
            skip them.
          </p>
        </div>

        <p className="max-w-lg text-xs text-muted-foreground">
          The approved template takes 4 parameters in order: Device, Status, Detail,
          Time Logged (<code>{"{{1}}"}</code>&hellip;<code>{"{{4}}"}</code>). Verify with
          "Send test alert" under an org's Settings &rarr; Organization.
        </p>

        <Button size="sm" className="w-fit" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </CardContent>
    </Card>
  )
}

export function PlatformPage() {
  const { user } = useAuth()
  if (!user?.is_superadmin) return null

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Platform settings</h1>
        <p className="text-sm text-muted-foreground">
          Server-wide configuration that applies to every organization: the look of the
          dashboard, the Google Maps key and map detail, the payment gateway every org
          checks out through, and the WhatsApp channel.
        </p>
      </div>

      <AppearanceCard />
      <GoogleMapsCard />
      <MapDetailCard />
      <PaymentsCard />
      {/* The fleet ledger sits under the gateway that feeds it: what each org
          owes, what accrued today, and the manual entries only you can make. */}
      <BillingConsolePanel />
      <WhatsAppCard />
    </div>
  )
}
