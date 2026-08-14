import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { CreditCard, Plus, RefreshCw } from "lucide-react"

import { ApiError, radiusApi } from "@/lib/api"
import type { RadiusAccount, RadiusStatus } from "@/lib/types"
import { ago } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Chip } from "@/components/status-badge"

// The vendor names are profile rows, so a brand this build has never heard of can
// still be the one an operator picks — the list comes from the server, never a
// hardcoded set here.
const BRAND_LABEL: Record<string, string> = {
  cbp: "Excell Media (CBP)",
  oneradius: "OneRadius",
}
const brandName = (p: string) => BRAND_LABEL[p] ?? p

// The meanings of a blank customer column, in the operator's words. Same split
// the SNMP and Rx panels keep: the server ships facts, this writes the sentence.
// `forbidden` is the one that earns its own line — the sign-in worked, so telling
// them to check the password sends them after something that is not wrong.
export function statusLine(st: RadiusStatus | null): {
  tone: "success" | "warning" | "destructive" | "muted"; text: string
} {
  if (!st) return { tone: "muted", text: "Not read yet." }
  const was = st.last_ok_at ? ` Last worked ${ago(st.last_ok_at)}.` : ""
  switch (st.state) {
    case "ok":
      return {
        tone: "success",
        text: `${st.customers} customers, ${st.linked} matched to an ONU · ${ago(st.updated_at)}`,
      }
    case "partial":
      return { tone: "warning", text: st.detail || "The export was missing some columns." }
    case "no_credentials":
      return { tone: "warning", text: "No sign-in details stored yet." }
    case "no_profile":
      return { tone: "warning", text: "That billing brand has no recipe on this server." }
    case "forbidden":
      return {
        tone: "warning",
        text: "Signed in, but this login may not export the customer list — ask "
          + "whoever runs the panel to allow it, or use a login that already can.",
      }
    case "login":
      return { tone: "destructive", text: `The panel refused the sign-in — check the username and password.${was}` }
    case "unreachable":
      return { tone: "destructive", text: `Couldn't reach the panel.${was}` }
    default:
      return { tone: "destructive", text: (st.detail || "The last read failed.") + was }
  }
}

const BLANK = {
  id: 0, label: "", profile: "", base_url: "", username: "", password: "",
}
type Draft = typeof BLANK

const draftOf = (a: RadiusAccount): Draft => ({
  id: a.id, label: a.label, profile: a.profile, base_url: a.base_url,
  username: a.username ?? "", password: "",
})

function PanelRow({ org, account, profiles, onDone, onCancel }: {
  org: string
  account: RadiusAccount | null
  profiles: string[]
  onDone: () => void
  onCancel?: () => void
}) {
  const [d, setD] = useState<Draft>(account ? draftOf(account) : { ...BLANK })
  const set = (k: keyof Draft, v: string) => setD((p) => ({ ...p, [k]: v }))

  const save = useMutation({
    mutationFn: () => radiusApi.save(org, {
      ...(d.id ? { id: d.id } : {}),
      label: d.label.trim(), profile: d.profile,
      base_url: d.base_url.trim(), username: d.username.trim(),
      ...(d.password ? { password: d.password } : {}),
    }),
    onSuccess: () => { setD((p) => ({ ...p, password: "" })); onDone() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

  const remove = useMutation({
    mutationFn: () => radiusApi.remove(org, d.id),
    onSuccess: () => { onDone(); toast.success("Billing panel disconnected") },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't disconnect"),
  })

  const sync = useMutation({
    mutationFn: () => radiusApi.syncNow(org, d.id),
    onSuccess: () => {
      toast.success("Reading the customer list — this takes a few seconds")
      setTimeout(onDone, 6000)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't start a read"),
  })

  // A stored panel needs no password re-typed; a new one does.
  const ready = !!d.profile && !!d.base_url.trim() && !!d.username.trim()
    && (!!d.password || !!account?.password_set)
  const st = statusLine(account?.status ?? null)

  return (
    <div className="space-y-3 rounded-md border border-border-subtle p-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label className="text-2xs">Billing brand</Label>
          <Select value={d.profile} onValueChange={(v) => set("profile", v)}>
            <SelectTrigger className="h-9 text-xs">
              <SelectValue placeholder="Choose your billing system" />
            </SelectTrigger>
            <SelectContent>
              {profiles.map((p: string) => (
                <SelectItem key={p} value={p} className="text-xs">
                  {brandName(p)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label className="text-2xs">Panel address</Label>
          <Input value={d.base_url} onChange={(e) => set("base_url", e.target.value)}
            placeholder="https://cbp.example.in" className="h-9 font-mono text-xs" />
        </div>
        <div className="space-y-1.5">
          <Label className="text-2xs">Username</Label>
          <Input value={d.username} onChange={(e) => set("username", e.target.value)}
            placeholder="the login you use for the panel" className="h-9 text-xs" />
        </div>
        <div className="space-y-1.5">
          <Label className="text-2xs">Password</Label>
          <Input type="password" value={d.password} autoComplete="new-password"
            onChange={(e) => set("password", e.target.value)}
            placeholder={account?.password_set ? "stored — leave blank to keep" : "the panel password"}
            className="h-9 text-xs" />
        </div>
        <div className="space-y-1.5 sm:col-span-2">
          <Label className="text-2xs">Name this panel <span className="text-faint-foreground">(optional)</span></Label>
          <Input value={d.label} onChange={(e) => set("label", e.target.value)}
            placeholder="what you call it — e.g. the town or brand it bills"
            className="h-9 text-xs" />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" className="h-8 text-xs" disabled={!ready || save.isPending}
          onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : account ? "Save" : "Connect"}
        </Button>
        {account && (
          <Button size="sm" variant="outline" className="h-8 text-xs"
            disabled={sync.isPending} onClick={() => sync.mutate()}>
            <RefreshCw className={`size-3 ${sync.isPending ? "animate-spin" : ""}`} />
            Read customers now
          </Button>
        )}
        {account && (
          <Button size="sm" variant="ghost" className="h-8 text-xs text-muted-foreground"
            disabled={remove.isPending} onClick={() => remove.mutate()}>
            Disconnect
          </Button>
        )}
        {!account && onCancel && (
          <Button size="sm" variant="ghost" className="h-8 text-xs text-muted-foreground"
            onClick={onCancel}>
            Cancel
          </Button>
        )}
        {account && <span className="ml-auto"><Chip tone={st.tone}>{st.text}</Chip></span>}
      </div>
    </div>
  )
}

export function RadiusCard({ org }: { org: string }) {
  const queryClient = useQueryClient()
  const q = useQuery({
    queryKey: ["radius", org],
    queryFn: () => radiusApi.get(org),
    enabled: !!org,
    refetchInterval: 60_000,
  })
  const [adding, setAdding] = useState(false)

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["radius", org] })
  }

  const accounts = q.data?.accounts ?? []
  const profiles = q.data?.profiles ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <CreditCard className="size-4 text-muted-foreground" /> Billing / RADIUS panel
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">
          Read your customer list from your billing panel, so an ONU shows the
          customer's name and number instead of the OLT's provisioning string.
          Matched on the router address we already read off the OLT. Read-only —
          nothing is ever written back to your panel.
        </p>

        {accounts.map((a) => (
          <PanelRow key={a.id} org={org} account={a} profiles={profiles}
            onDone={() => { invalidate(); toast.success("Billing panel saved") }} />
        ))}

        {(adding || accounts.length === 0) && (
          <PanelRow org={org} account={null} profiles={profiles}
            onDone={() => { setAdding(false); invalidate(); toast.success("Billing panel connected") }}
            onCancel={accounts.length ? () => setAdding(false) : undefined} />
        )}

        {!adding && accounts.length > 0 && (
          <Button size="sm" variant="outline" className="h-8 text-xs"
            onClick={() => setAdding(true)}>
            <Plus className="size-3" /> Add another panel
          </Button>
        )}

        <p className="text-2xs text-faint-foreground">
          The address is the server only — no page path. Your password is encrypted
          and never shown again.
          {accounts.length > 1 && " Where two panels name the same router, the one "
            + "connected first is the one shown."}
        </p>

        {q.data && q.data.customers > 0 && (
          <p className="text-2xs text-faint-foreground">
            Customers are read hourly. A customer who has been disconnected stops
            passing traffic, so their address ages out of the OLT and they can no
            longer be matched — that is expected, not a fault.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
