import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Cpu, Pencil, Plus, Trash2 } from "lucide-react"
import { snmpApi, ApiError } from "@/lib/api"
import type { SnmpProfile } from "@/lib/types"
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

interface MetricRow {
  metric: string
  oid: string
  decode: string
  select: string
}

const DECODE_HELP: Record<string, string> = {
  as_is: "value as reported",
  div10: "÷10 (tenths, e.g. 425 → 42.5)",
  div100: "÷100 (hundredths)",
  signed_div100: "signed ÷100 (e.g. optical dBm: 63021 → −25.15)",
}

function ProfileForm({
  org, editing, vocab, onDone,
}: {
  org: string | null
  editing: SnmpProfile | null
  vocab: { metrics: string[]; decodes: string[]; selects: string[] }
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(editing?.name ?? "")
  const [match, setMatch] = useState(editing?.match_sysobjectid ?? "1.3.6.1.4.1.")
  const [enabled, setEnabled] = useState(editing?.enabled ?? true)
  const [rows, setRows] = useState<MetricRow[]>(() =>
    editing
      ? Object.entries(editing.metrics).map(([metric, s]) => ({
          metric, oid: s.oid, decode: s.decode, select: s.select }))
      : [{ metric: vocab.metrics[0], oid: "", decode: "as_is", select: "first" }])
  const [error, setError] = useState("")

  const setRow = (i: number, patch: Partial<MetricRow>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  const save = useMutation({
    mutationFn: async () => {
      const metrics: Record<string, { oid: string; decode: string; select: string }> = {}
      for (const r of rows) {
        if (!r.oid.trim()) continue
        metrics[r.metric] = { oid: r.oid.trim(), decode: r.decode, select: r.select }
      }
      if (Object.keys(metrics).length === 0) throw new Error("Map at least one metric to an OID")
      const body = { name: name.trim(), match_sysobjectid: match.trim(), metrics, enabled }
      if (editing) { await snmpApi.updateProfile(editing.id, body); return }
      await snmpApi.createProfile(org ? { ...body, org_id: org } : body)
    },
    onSuccess: () => {
      toast.success(editing ? "Profile saved" : "Profile created")
      queryClient.invalidateQueries({ queryKey: ["snmp-profiles"] })
      onDone()
    },
    onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : "Save failed"),
  })

  return (
    <div className="flex flex-col gap-3 border-t bg-muted/30 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label>Name</Label>
          <Input placeholder="e.g. fiberhome-s3330" value={name}
            onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Match sysObjectID (prefix)</Label>
          <Input className="font-mono text-xs" placeholder="1.3.6.1.4.1.5651" value={match}
            onChange={(e) => setMatch(e.target.value)} />
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <Label>Metrics</Label>
        {rows.map((r, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <Select value={r.metric} onValueChange={(v) => setRow(i, { metric: v })}>
              <SelectTrigger className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.metrics.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
            <Input className="h-8 flex-1 font-mono text-xs" placeholder="OID, e.g. 1.3.6.1.4.1.5651.3.901.2.0"
              value={r.oid} onChange={(e) => setRow(i, { oid: e.target.value })} />
            <Select value={r.decode} onValueChange={(v) => setRow(i, { decode: v })}>
              <SelectTrigger className="h-8 w-32 text-xs" title={DECODE_HELP[r.decode]}><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.decodes.map((d) => (
                  <SelectItem key={d} value={d} title={DECODE_HELP[d]}>{d}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={r.select} onValueChange={(v) => setRow(i, { select: v })}>
              <SelectTrigger className="h-8 w-24 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {vocab.selects.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button variant="ghost" size="icon" className="size-8 text-muted-foreground"
              disabled={rows.length === 1} onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button variant="outline" size="sm" className="w-fit"
          onClick={() => setRows((rs) => [...rs,
            { metric: vocab.metrics[0], oid: "", decode: "as_is", select: "first" }])}>
          <Plus className="size-3.5" /> Add metric
        </Button>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <Switch checked={enabled} onCheckedChange={setEnabled} /> Enabled (served to edges)
      </label>
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
        <Button size="sm" disabled={!name.trim() || !match.trim() || save.isPending}
          onClick={() => save.mutate()}>
          {editing ? "Save" : "Create"}
        </Button>
      </div>
    </div>
  )
}

export function SnmpProfilesCard({ org, isSuperadmin }: {
  org: string | null
  isSuperadmin: boolean
}) {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<SnmpProfile | null>(null)
  const [deleting, setDeleting] = useState<SnmpProfile | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["snmp-profiles", org],
    queryFn: () => snmpApi.profiles(org),
  })

  const remove = useMutation({
    mutationFn: (id: number) => snmpApi.removeProfile(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["snmp-profiles"] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Delete failed"),
  })

  const profiles = data?.profiles ?? []
  const vocab = {
    metrics: data?.metrics ?? [],
    decodes: data?.decodes ?? [],
    selects: data?.selects ?? [],
  }
  // A global profile (org_id null) is only editable by a superadmin.
  const canEdit = (p: SnmpProfile) => (p.org_id === null ? isSuperadmin : true)

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Cpu className="size-4 text-muted-foreground" /> SNMP health profiles
        </CardTitle>
        {!adding && !editing && (
          <Button variant="outline" size="sm" onClick={() => setAdding(true)}>
            <Plus className="size-4" /> Add profile
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        <p className="px-4 pb-3 text-xs text-muted-foreground">
          Teach the edge a new vendor's CPU/RAM/temperature OIDs as data. No code change or
          rollout. The edge matches a profile to a device by its sysObjectID and fills any
          reading the standard MIBs don't expose. {isSuperadmin && "Profiles you add here are global (every org's edges receive them)."}
        </p>
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-12 w-full" /></div>}
        {!isLoading && profiles.length === 0 && !adding && (
          <p className="px-4 pb-4 text-xs text-muted-foreground">No profiles yet.</p>
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
                  {p.match_sysobjectid} · {Object.keys(p.metrics).join(", ")}
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
          description="Edges stop receiving it on their next topology refresh."
          onConfirm={() => { if (deleting) remove.mutate(deleting.id) }}
        />
      </CardContent>
    </Card>
  )
}
