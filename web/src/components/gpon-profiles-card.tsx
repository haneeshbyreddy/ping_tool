import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Pencil, Plus, Router, Trash2 } from "lucide-react"
import { gponApi, ApiError, type GponProfilePayload } from "@/lib/api"
import type { GponProfile } from "@/lib/types"
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

// Human hints for the closed OID vocabulary — mirrors ingress/gpon.py.
const OID_HELP: Record<string, string> = {
  rx: "per-ONU Rx power (optical table)",
  tx: "per-ONU Tx power (optical table)",
  state: "ONU state column (optical table)",
  distance: "ranging distance (optical table)",
  serial: "serial/MAC column (optical table)",
  name: "ONU description (optical table)",
  ident_key: "serial/MAC (registration roster, the authoritative ONU list)",
  ident_pon: "PON port number (roster)",
  ident_onu: "ONU id on the PON (roster)",
  ident_state: "ONU state (roster)",
  ident_distance: "ranging distance (roster)",
  ident_name: "ONU description (roster)",
}

interface OidRow { field: string; oid: string }
interface StateRow { raw: string; state: string }

function ProfileForm({
  org, editing, vocab, onDone,
}: {
  org: string | null
  editing: GponProfile | null
  vocab: { oid_fields: string[]; states: string[]; pon_index_strategies: string[] }
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(editing?.name ?? "")
  const [match, setMatch] = useState(editing?.match_sysobjectid ?? "1.3.6.1.4.1.")
  const [enabled, setEnabled] = useState(editing?.enabled ?? true)
  const [oidRows, setOidRows] = useState<OidRow[]>(() =>
    editing
      ? Object.entries(editing.spec.oids).map(([field, oid]) => ({ field, oid }))
      : [{ field: "ident_key", oid: "" }])
  const [stateRows, setStateRows] = useState<StateRow[]>(() => {
    const m = editing?.spec.state_map ?? { "1": "online", "0": "offline" }
    return Object.entries(m).map(([raw, state]) => ({ raw, state }))
  })
  const [stateDefault, setStateDefault] = useState(editing?.spec.state_default ?? "unknown")
  const [ponIndex, setPonIndex] = useState(editing?.spec.pon_index ?? "as_is")
  const [ponLabel, setPonLabel] = useState(editing?.spec.pon_label ?? "")
  const [rxScale, setRxScale] = useState(String(editing?.spec.scales?.rx ?? "0.01"))
  const [txScale, setTxScale] = useState(String(editing?.spec.scales?.tx ?? "0.01"))
  const [distScale, setDistScale] = useState(String(editing?.spec.scales?.distance ?? "1"))
  const [error, setError] = useState("")

  const setOidRow = (i: number, patch: Partial<OidRow>) =>
    setOidRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const setStateRow = (i: number, patch: Partial<StateRow>) =>
    setStateRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  const save = useMutation({
    mutationFn: async () => {
      const oids: Record<string, string> = {}
      for (const r of oidRows) if (r.oid.trim()) oids[r.field] = r.oid.trim()
      if (Object.keys(oids).length === 0) throw new Error("Map at least one OID")
      const state_map: Record<string, string> = {}
      for (const r of stateRows) if (r.raw.trim()) state_map[r.raw.trim()] = r.state
      const scales: Record<string, number> = {}
      for (const [k, v] of [["rx", rxScale], ["tx", txScale], ["distance", distScale]] as const) {
        const f = parseFloat(v)
        if (Number.isFinite(f)) scales[k] = f
      }
      const body: GponProfilePayload = {
        name: name.trim(), match_sysobjectid: match.trim(), oids, scales,
        state_map, state_default: stateDefault, pon_index: ponIndex,
        pon_label: ponLabel.trim(), enabled,
      }
      if (editing) { await gponApi.updateProfile(editing.id, body); return }
      await gponApi.createProfile(org ? { ...body, org_id: org } : body)
    },
    onSuccess: () => {
      toast.success(editing ? "Profile saved" : "Profile created")
      queryClient.invalidateQueries({ queryKey: ["gpon-profiles"] })
      onDone()
    },
    onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : "Save failed"),
  })

  return (
    <div className="flex flex-col gap-3 border-t bg-muted/30 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label>Vendor name</Label>
          <Input placeholder="e.g. vsol" value={name}
            onChange={(e) => setName(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            Same name as a built-in (huawei, dbc) replaces it on every edge.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Match sysObjectID (prefix)</Label>
          <Input className="font-mono text-xs" placeholder="1.3.6.1.4.1.37950" value={match}
            onChange={(e) => setMatch(e.target.value)} />
          <p className="text-2xs text-muted-foreground">
            Auto-detect claims any OLT whose sysObjectID starts with this.
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <Label>ONU table OIDs</Label>
        {oidRows.map((r, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <Select value={r.field} onValueChange={(v) => setOidRow(i, { field: v })}>
              <SelectTrigger className="h-8 w-40 text-xs" title={OID_HELP[r.field]}><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.oid_fields.map((f) => (
                  <SelectItem key={f} value={f} title={OID_HELP[f]}>{f}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input className="h-8 flex-1 font-mono text-xs"
              placeholder="OID column, e.g. 1.3.6.1.4.1.37950.1.1.5.12.1.12.1.6"
              value={r.oid} onChange={(e) => setOidRow(i, { oid: e.target.value })} />
            <Button variant="ghost" size="icon" className="size-8 text-muted-foreground"
              disabled={oidRows.length === 1}
              onClick={() => setOidRows((rs) => rs.filter((_, j) => j !== i))}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button variant="outline" size="sm" className="w-fit"
          onClick={() => setOidRows((rs) => {
            const used = new Set(rs.map((r) => r.field))
            const next = vocab.oid_fields.find((f) => !used.has(f)) ?? vocab.oid_fields[0]
            return [...rs, { field: next, oid: "" }]
          })}>
          <Plus className="size-3.5" /> Add column
        </Button>
        <p className="text-2xs text-muted-foreground">
          ident_* columns come from a registration/roster table (every ONU, online or not);
          the plain columns from an optical table indexed pon.onu. Map only what the vendor
          actually exposes. A column you leave out renders honestly blank, never guessed.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label>State values</Label>
          {stateRows.map((r, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <Input className="h-8 w-24 font-mono text-xs" placeholder="raw" value={r.raw}
                onChange={(e) => setStateRow(i, { raw: e.target.value })} />
              <span className="text-xs text-muted-foreground">&rarr;</span>
              <Select value={r.state} onValueChange={(v) => setStateRow(i, { state: v })}>
                <SelectTrigger className="h-8 flex-1 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {vocab.states.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
              <Button variant="ghost" size="icon" className="size-8 text-muted-foreground"
                disabled={stateRows.length === 1}
                onClick={() => setStateRows((rs) => rs.filter((_, j) => j !== i))}>
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}
          <Button variant="outline" size="sm" className="w-fit"
            onClick={() => setStateRows((rs) => [...rs, { raw: "", state: "online" }])}>
            <Plus className="size-3.5" /> Add value
          </Button>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">anything else &rarr;</span>
            <Select value={stateDefault} onValueChange={setStateDefault}>
              <SelectTrigger className="h-8 w-32 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.states.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>PON label</Label>
            <Input className="h-8 text-xs" placeholder={"e.g. EPON0/{pon} (blank keeps the raw index)"}
              value={ponLabel} onChange={(e) => setPonLabel(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>PON from OID index</Label>
            <Select value={ponIndex} onValueChange={setPonIndex}>
              <SelectTrigger className="h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="as_is">as_is (index is the PON)</SelectItem>
                <SelectItem value="first_segment">first_segment (pon.onu index, take pon)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Scales (raw &times; scale)</Label>
            <div className="flex items-center gap-1.5">
              {([["rx", rxScale, setRxScale], ["tx", txScale, setTxScale],
                 ["dist", distScale, setDistScale]] as const).map(([lbl, val, set]) => (
                <label key={lbl} className="flex items-center gap-1 text-2xs text-muted-foreground">
                  {lbl}
                  <Input className="h-8 w-16 text-xs" value={val}
                    onChange={(e) => set(e.target.value)} />
                </label>
              ))}
            </div>
            <p className="text-2xs text-muted-foreground">
              0.01 when the agent reports dBm &times; 100 (e.g. −2015 &rarr; −20.15 dBm).
            </p>
          </div>
        </div>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <Switch checked={enabled} onCheckedChange={setEnabled} /> Enabled (served to edges)
      </label>
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
        <Button size="sm" disabled={!name.trim() || save.isPending}
          onClick={() => save.mutate()}>
          {editing ? "Save" : "Create"}
        </Button>
      </div>
    </div>
  )
}

export function GponProfilesCard({ org, isSuperadmin }: {
  org: string | null
  isSuperadmin: boolean
}) {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<GponProfile | null>(null)
  const [deleting, setDeleting] = useState<GponProfile | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["gpon-profiles", org],
    queryFn: () => gponApi.profiles(org),
  })

  const remove = useMutation({
    mutationFn: (id: number) => gponApi.removeProfile(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["gpon-profiles"] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })

  const profiles = data?.profiles ?? []
  const vocab = {
    oid_fields: data?.oid_fields ?? [],
    states: data?.states ?? [],
    pon_index_strategies: data?.pon_index_strategies ?? [],
  }
  // A global profile (org_id null) is only editable by a superadmin.
  const canEdit = (p: GponProfile) => (p.org_id === null ? isSuperadmin : true)

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Router className="size-4 text-muted-foreground" /> GPON vendor profiles
        </CardTitle>
        {!adding && !editing && (
          <Button variant="outline" size="sm" onClick={() => setAdding(true)}>
            <Plus className="size-4" /> Add profile
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        <p className="px-4 pb-3 text-xs text-muted-foreground">
          Teach the edge a new OLT vendor's ONU-table OIDs as data. No code change or rollout.
          Edges pick these up within a minute. Built-in huawei/dbc profiles keep working;
          a profile with the same name replaces the built-in.
          {isSuperadmin && " Profiles you add here are global (every org's edges receive them)."}
        </p>
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-12 w-full" /></div>}
        {!isLoading && profiles.length === 0 && !adding && (
          <p className="px-4 pb-4 text-xs text-muted-foreground">
            No custom profiles. OLTs use the built-in huawei/dbc profiles.
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
                  {!p.enabled && (
                    <span className="rounded bg-muted px-1.5 py-px text-2xs font-semibold text-muted-foreground">
                      off
                    </span>
                  )}
                </p>
                <p className="truncate font-mono text-2xs text-muted-foreground">
                  {p.match_sysobjectid || "(manual override only)"} · {Object.keys(p.spec.oids).join(", ")}
                </p>
              </div>
              {canEdit(p) && (
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
              <ProfileForm org={p.org_id} editing={p} vocab={vocab} onDone={() => setEditing(null)} />
            )}
          </div>
        ))}
        {adding && (
          <ProfileForm org={isSuperadmin ? null : org} editing={null} vocab={vocab}
            onDone={() => setAdding(false)} />
        )}
        <ConfirmDialog
          open={!!deleting}
          onOpenChange={(o) => { if (!o) setDeleting(null) }}
          title={`Delete profile ${deleting?.name ?? ""}?`}
          description="Edges fall back to the built-in profile of the same name (if any) on their next topology refresh."
          onConfirm={() => { if (deleting) remove.mutate(deleting.id) }}
        />
      </CardContent>
    </Card>
  )
}
