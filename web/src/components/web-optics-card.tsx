import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Globe, Pencil, Plus, Trash2 } from "lucide-react"
import { webOpticsApi, ApiError, type WebOpticsProfilePayload } from "@/lib/api"
import type { WebOpticsProfile, WebOpticsProfileSpec } from "@/lib/types"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const FIELD_HELP: Record<string, string> = {
  onu_ref: "required · the cell holding the ONU's identity, how a data row is told from a heading",
  serial: "required · MAC address, what a reading is matched to a subscriber by",
  name: "the ONU's description / subscriber name",
  distance_m: "ranging distance",
  temp_c: "ONU temperature",
  voltage_v: "supply voltage, used to spot a dead sensor printing rails",
  tx_bias_ma: "transmit bias current",
  tx_dbm: "ONU transmit power",
  rx_dbm: "ONU received power, the reading this exists for",
}
const SESSION_HELP: Record<string, string> = {
  "rotating-key": "no cookie; a token in each page's script, changing every response",
  cookie: "an ordinary Set-Cookie login session",
}
const SHAPE_HELP: Record<string, string> = {
  "pon-colon-onu": "the cell names both, e.g. EPON0/3:29",
  "onu-index": "the cell is just the ONU number; the PON comes from the page requested",
}

interface ColRow { field: string; head: string }

function pathish(v: string) {
  return v.startsWith("/") && !v.includes("://")
}

function ProfileForm({ org, editing, vocab, example, onDone }: {
  org: string | null
  editing: WebOpticsProfile | null
  vocab: { fields: string[]; sessions: string[]; methods: string[]
           charsets: string[]; shapes: string[] }
  example: Partial<WebOpticsProfileSpec>
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const s = editing?.spec
  const [name, setName] = useState(editing?.name ?? "")
  const [enabled, setEnabled] = useState(editing?.enabled ?? true)
  const [loginPage, setLoginPage] = useState(s?.login_page_path ?? "")
  const [loginPath, setLoginPath] = useState(s?.login_path ?? "")
  const [opticsPath, setOpticsPath] = useState(s?.optics_path ?? "")
  const [userField, setUserField] = useState(s?.username_field ?? "user")
  const [passField, setPassField] = useState(s?.password_field ?? "pass")
  const [loginStatic, setLoginStatic] = useState(
    JSON.stringify(s?.login_static ?? {}, null, 0))
  const [session, setSession] = useState(s?.session ?? "rotating-key")
  const [keyField, setKeyField] = useState(s?.session_key_field ?? "SessionKey")
  const [method, setMethod] = useState(s?.optics_method ?? "POST")
  const [ponField, setPonField] = useState(s?.pon_field ?? "select")
  const [opticsStatic, setOpticsStatic] = useState(
    JSON.stringify(s?.optics_static ?? {}, null, 0))
  const [charset, setCharset] = useState(s?.charset ?? "utf-8")
  const [shape, setShape] = useState(s?.onu_id_shape ?? "pon-colon-onu")
  const [ponLabel, setPonLabel] = useState(s?.pon_label ?? "")
  const [pons, setPons] = useState((s?.default_pons ?? [1, 2, 3, 4]).join(", "))
  const [markers, setMarkers] = useState((s?.vendor_markers ?? []).join(", "))
  const [cols, setCols] = useState<ColRow[]>(() => {
    const entries = Object.entries(s?.columns ?? {})
    return entries.length
      ? entries.map(([field, head]) => ({ field, head }))
      : [{ field: "onu_ref", head: "" }, { field: "serial", head: "" },
         { field: "rx_dbm", head: "" }]
  })
  const [error, setError] = useState("")

  const setCol = (i: number, patch: Partial<ColRow>) =>
    setCols((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  const loadExample = () => {
    if (!example.optics_path) return
    setLoginPage(example.login_page_path ?? "")
    setLoginPath(example.login_path ?? "")
    setOpticsPath(example.optics_path ?? "")
    setUserField(example.username_field ?? "user")
    setPassField(example.password_field ?? "pass")
    setLoginStatic(JSON.stringify(example.login_static ?? {}))
    setSession(example.session ?? "rotating-key")
    setKeyField(example.session_key_field ?? "SessionKey")
    setMethod(example.optics_method ?? "POST")
    setPonField(example.pon_field ?? "select")
    setOpticsStatic(JSON.stringify(example.optics_static ?? {}))
    setCharset(example.charset ?? "utf-8")
    setShape(example.onu_id_shape ?? "pon-colon-onu")
    setPonLabel(example.pon_label ?? "")
    setPons((example.default_pons ?? [1, 2, 3, 4]).join(", "))
    setMarkers((example.vendor_markers ?? []).join(", "))
    setCols(Object.entries(example.columns ?? {}).map(([field, head]) => ({ field, head })))
  }

  const save = useMutation({
    mutationFn: async () => {
      let login_static: Record<string, string> = {}
      let optics_static: Record<string, string> = {}
      try {
        login_static = loginStatic.trim() ? JSON.parse(loginStatic) : {}
        optics_static = opticsStatic.trim() ? JSON.parse(opticsStatic) : {}
      } catch {
        throw new Error("Fixed form fields must be JSON, e.g. {\"who\": \"100\"}")
      }
      const columns: Record<string, string> = {}
      for (const r of cols) if (r.head.trim()) columns[r.field] = r.head.trim()
      const default_pons = pons.split(",").map((p) => parseInt(p.trim(), 10))
        .filter((n) => Number.isFinite(n))
      const body: WebOpticsProfilePayload = {
        name: name.trim(), enabled,
        login_page_path: loginPage.trim(), login_path: loginPath.trim(),
        optics_path: opticsPath.trim(),
        username_field: userField.trim(), password_field: passField.trim(),
        login_static, session, session_key_field: keyField.trim(),
        optics_method: method, pon_field: ponField.trim(), optics_static,
        charset, onu_id_shape: shape, pon_label: ponLabel.trim(),
        columns, column_order: [], default_pons,
        vendor_markers: markers.split(",").map((m) => m.trim()).filter(Boolean),
      }
      if (editing) { await webOpticsApi.updateProfile(editing.id, body); return }
      await webOpticsApi.createProfile(org ? { ...body, org_id: org } : body)
    },
    onSuccess: () => {
      toast.success(editing ? "Recipe saved" : "Recipe created", {
        description: "Central picks it up on the next optics sweep. No probe update.",
      })
      queryClient.invalidateQueries({ queryKey: ["web-optics-profiles"] })
      onDone()
    },
    onError: (e) => setError(
      e instanceof ApiError || e instanceof Error ? e.message : "Save failed"),
  })

  const badPath = [loginPage, loginPath, opticsPath].some((p) => p && !pathish(p))

  return (
    <div className="flex flex-col gap-3 border-t bg-muted/30 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label>Vendor name</Label>
          <Input placeholder="e.g. cdata-gpon" value={name}
            onChange={(e) => setName(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            Must match the device's GPON vendor: the same token the dropdown
            and the probe's auto-detect use. That's how an OLT is bound to this.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Page encoding</Label>
          <Select value={charset} onValueChange={setCharset}>
            <SelectTrigger className="h-9 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              {vocab.charsets.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
            </SelectContent>
          </Select>
          <p className="text-2xs text-muted-foreground">
            From the page's own &lt;meta charset&gt;. Getting it wrong looks like
            "this OLT has no optics".
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <Label>Pages</Label>
        <div className="grid gap-2 sm:grid-cols-3">
          {([["Login page (GET)", loginPage, setLoginPage, "/action/login.html"],
             ["Login form posts to", loginPath, setLoginPath, "/action/main.html"],
             ["Optics page", opticsPath, setOpticsPath, "/action/onuopmdiag.html"],
            ] as const).map(([lbl, val, set, ph]) => (
            <label key={lbl} className="flex flex-col gap-1 text-2xs text-muted-foreground">
              {lbl}
              <Input className="h-8 font-mono text-xs" placeholder={ph} value={val}
                onChange={(e) => set(e.target.value)} />
            </label>
          ))}
        </div>
        <p className="text-2xs text-muted-foreground">
          Paths only, never a full URL. The address comes from the device, which
          is what stops a recipe pointing central at some other machine.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label>Login form</Label>
          <div className="flex items-center gap-1.5">
            <Input className="h-8 font-mono text-xs" placeholder="username field"
              value={userField} onChange={(e) => setUserField(e.target.value)} />
            <Input className="h-8 font-mono text-xs" placeholder="password field"
              value={passField} onChange={(e) => setPassField(e.target.value)} />
          </div>
          <Input className="h-8 font-mono text-xs" placeholder={'fixed fields, e.g. {"button":"Login"}'}
            value={loginStatic} onChange={(e) => setLoginStatic(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            The field NAMES the box's login form posts, plus any hidden fields it
            sends along. Copy them verbatim, oddities included.
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <Label>Session</Label>
          <Select value={session} onValueChange={setSession}>
            <SelectTrigger className="h-8 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              {vocab.sessions.map((v) => (
                <SelectItem key={v} value={v} title={SESSION_HELP[v]}>{v}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-2xs text-muted-foreground">{SESSION_HELP[session]}</p>
          {session === "rotating-key" && (
            <Input className="h-8 font-mono text-xs" placeholder="token field, e.g. SessionKey"
              value={keyField} onChange={(e) => setKeyField(e.target.value)} />
          )}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label>Asking for one PON</Label>
          <div className="flex items-center gap-1.5">
            <Select value={method} onValueChange={setMethod}>
              <SelectTrigger className="h-8 w-24 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.methods.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
            <Input className="h-8 flex-1 font-mono text-xs" placeholder="PON field, e.g. select"
              value={ponField} onChange={(e) => setPonField(e.target.value)} />
          </div>
          <Input className="h-8 font-mono text-xs"
            placeholder={'fixed fields, e.g. {"port_refresh":"Refresh"}'}
            value={opticsStatic} onChange={(e) => setOpticsStatic(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            Some firmware only measures when the page's Refresh button is part of
            the request. Include it if the capture shows it.
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <Label>PON ports</Label>
          <Input className="h-8 font-mono text-xs" placeholder="1, 2, 3, 4"
            value={pons} onChange={(e) => setPons(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            Fallback only. The real port list comes from the OLT's own SNMP
            roster, gaps and all.
          </p>
          <Input className="h-8 font-mono text-xs" placeholder="words that identify this UI, e.g. epon, olt"
            value={markers} onChange={(e) => setMarkers(e.target.value)} />
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <Label>Table columns</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          <Select value={shape} onValueChange={setShape}>
            <SelectTrigger className="h-8 w-56 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              {vocab.shapes.map((v) => (
                <SelectItem key={v} value={v} title={SHAPE_HELP[v]}>{v}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-2xs text-muted-foreground">{SHAPE_HELP[shape]}</span>
          {shape === "onu-index" && (
            <Input className="h-8 w-44 font-mono text-xs" placeholder={"PON label, e.g. GPON0/{pon}"}
              value={ponLabel} onChange={(e) => setPonLabel(e.target.value)} />
          )}
        </div>
        {cols.map((r, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <Select value={r.field} onValueChange={(v) => setCol(i, { field: v })}>
              <SelectTrigger className="h-8 w-36 text-xs" title={FIELD_HELP[r.field]}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {vocab.fields.map((f) => (
                  <SelectItem key={f} value={f} title={FIELD_HELP[f]}>{f}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input className="h-8 flex-1 text-xs" placeholder="column heading as the page prints it"
              value={r.head} onChange={(e) => setCol(i, { head: e.target.value })} />
            <Button variant="ghost" size="icon" className="size-8 text-muted-foreground"
              disabled={cols.length === 1}
              onClick={() => setCols((rs) => rs.filter((_, j) => j !== i))}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button variant="outline" size="sm" className="w-fit"
          onClick={() => setCols((rs) => {
            const used = new Set(rs.map((r) => r.field))
            const next = vocab.fields.find((f) => !used.has(f)) ?? vocab.fields[0]
            return [...rs, { field: next, head: "" }]
          })}>
          <Plus className="size-3.5" /> Add column
        </Button>
        <p className="text-2xs text-muted-foreground">
          Matched against the table's own heading row, not by position, so a
          firmware update that inserts a column can't quietly turn transmit power
          into received power. A partial heading is enough ("Distance" matches
          "Distance(m)").
        </p>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <Switch checked={enabled} onCheckedChange={setEnabled} /> Enabled
      </label>
      {badPath && (
        <p className="text-xs text-warning">
          Paths must start with "/" and carry no host.
        </p>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex items-center justify-end gap-2">
        {!editing && example.optics_path && (
          <Button variant="ghost" size="sm" className="mr-auto" onClick={loadExample}>
            Start from the built-in example
          </Button>
        )}
        <Button variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
        <Button size="sm" disabled={!name.trim() || save.isPending}
          onClick={() => save.mutate()}>
          {editing ? "Save" : "Create"}
        </Button>
      </div>
    </div>
  )
}

export function WebOpticsCard({ org, isSuperadmin }: {
  org: string | null
  isSuperadmin: boolean
}) {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<WebOpticsProfile | null>(null)
  const [deleting, setDeleting] = useState<WebOpticsProfile | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["web-optics-profiles", org],
    queryFn: () => webOpticsApi.profiles(org),
  })

  const remove = useMutation({
    mutationFn: (id: number) => webOpticsApi.removeProfile(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["web-optics-profiles"] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })

  const profiles = data?.profiles ?? []
  const builtins = data?.builtins ?? []
  const vocab = {
    fields: data?.fields ?? [], sessions: data?.sessions ?? [],
    methods: data?.methods ?? [], charsets: data?.charsets ?? [],
    shapes: data?.onu_id_shapes ?? [],
  }
  // Writing a recipe is a platform-admin job now (every profile write route is
  // superadmin-only), so an owner reads this card and is offered no button
  // that would 403. Which vendors are covered is still theirs to see.
  const canEdit = () => isSuperadmin

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Globe className="size-4 text-muted-foreground" /> Web-UI optics vendors
        </CardTitle>
        {isSuperadmin && !adding && !editing && (
          <Button variant="outline" size="sm" onClick={() => setAdding(true)}>
            <Plus className="size-4" /> Add vendor
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        <p className="px-4 pb-3 text-xs text-muted-foreground">
          Some OLTs report per-ONU dBm nowhere in SNMP, and measure it only when
          their own web page is opened. A recipe lets central read that page
          through the probe's tunnel. Needs the org's web-proxy capability and the
          OLT's stored web login. No probe update.
          {isSuperadmin
            ? " Recipes you add here apply to every organization."
            : " Recipes are added by the platform admin, and reach your OLTs with no update to install."}
        </p>
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-12 w-full" /></div>}
        {!isLoading && profiles.length === 0 && !adding && (
          <p className="px-4 pb-4 text-xs text-muted-foreground">
            No custom recipes.{" "}
            {builtins.length
              ? `Built-in support: ${builtins.join(", ")}. Any other vendor reports no dBm until a recipe is added.`
              : "No vendors are covered yet."}
          </p>
        )}
        {profiles.map((p) => (
          <div key={p.id} className="border-t first:border-t-0">
            <div className="group flex items-center gap-3 px-4 py-2.5">
              <div className="min-w-0">
                <p className="flex items-center gap-2 truncate text-sm font-medium">
                  {p.name}
                  {p.org_id === null && (
                    <span className="rounded bg-muted px-1.5 py-px text-2xs font-semibold text-muted-foreground">
                      global
                    </span>
                  )}
                  {builtins.includes(p.name) && (
                    <span className="rounded bg-muted px-1.5 py-px text-2xs font-semibold text-muted-foreground">
                      replaces built-in
                    </span>
                  )}
                  {!p.enabled && (
                    <span className="rounded bg-muted px-1.5 py-px text-2xs font-semibold text-muted-foreground">
                      off
                    </span>
                  )}
                </p>
                <p className="truncate font-mono text-2xs text-muted-foreground">
                  {p.spec.optics_path} · {p.spec.session} · {p.spec.charset}
                </p>
              </div>
              {canEdit() && (
                <div className="ml-auto flex shrink-0 items-center gap-1 opacity-60 group-hover:opacity-100">
                  <Button variant="ghost" size="icon" className="size-7"
                    onClick={() => { setEditing(p); setAdding(false) }}>
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button variant="ghost" size="icon" className="size-7"
                    onClick={() => setDeleting(p)}>
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              )}
            </div>
            {editing?.id === p.id && (
              <ProfileForm org={p.org_id} editing={p} vocab={vocab}
                example={data?.example ?? {}} onDone={() => setEditing(null)} />
            )}
          </div>
        ))}
        {adding && (
          <ProfileForm org={isSuperadmin ? null : org} editing={null} vocab={vocab}
            example={data?.example ?? {}} onDone={() => setAdding(false)} />
        )}
        <ConfirmDialog
          open={!!deleting}
          onOpenChange={(o) => { if (!o) setDeleting(null) }}
          title={`Delete recipe ${deleting?.name ?? ""}?`}
          description="Those OLTs stop reporting per-ONU dBm on the next sweep, unless a built-in recipe of the same name covers them."
          onConfirm={() => { if (deleting) remove.mutate(deleting.id) }}
        />
      </CardContent>
    </Card>
  )
}
