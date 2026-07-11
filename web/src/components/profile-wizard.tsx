// Walk dump → vendor health profile, without knowing SNMP. Pick a finished walk,
// tag the CPU/RAM/temperature rows out of its numeric enterprise-tree varbinds,
// and save — the edge starts filling the vitals on its next sweep, no rollout.
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { snmpApi, ApiError } from "@/lib/api"
import type { OrgDevice } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const ENTERPRISE_PREFIX = "1.3.6.1.4.1."
const OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
const NUMERIC = /^-?\d+(\.\d+)?$/
const MAX_ROWS_SHOWN = 200

// Mirrors the backend's closed vocabulary (inventory.PROFILE_METRICS); the save
// round-trips through the same validation, so a drift fails loudly, not silently.
const METRIC_LABEL: Record<string, string> = {
  cpu_pct: "CPU %",
  mem_pct: "RAM %",
  mem_used_bytes: "RAM used (bytes)",
  mem_total_bytes: "RAM total (bytes)",
  temp_c: "Temperature °C",
}

function decodePreview(raw: number, decode: string): number {
  switch (decode) {
    case "div10": return raw / 10
    case "div100": return raw / 100
    case "signed_div100": return (raw > 32767 ? raw - 65536 : raw) / 100
    default: return raw
  }
}

/** The classic encodings, cheapest tell first: a value past signed-16-bit range is
 * almost always a negative reading stored as unsigned hundredths (dBm/temp). */
function suggestDecode(raw: number): string {
  return raw > 32767 ? "signed_div100" : "as_is"
}

interface Tag {
  oid: string
  value: number
  decode: string
}

export function ProfileWizard({ device, sysObjectId, open, onOpenChange }: {
  device: OrgDevice
  /** device sysObjectID if the diagnosis already knows it — seeds the match prefix */
  sysObjectId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const qc = useQueryClient()
  const walksQ = useQuery({
    queryKey: ["snmp-walks", device.id],
    queryFn: () => snmpApi.walks(device.id),
    enabled: open,
  })
  const vocabQ = useQuery({
    queryKey: ["snmp-profiles", device.org_id],
    queryFn: () => snmpApi.profiles(device.org_id),
    enabled: open,
  })
  const doneWalks = useMemo(
    () => (walksQ.data?.walks ?? []).filter((w) => w.status === "done" && (w.varbind_count ?? 0) > 0),
    [walksQ.data])

  const [walkId, setWalkId] = useState<number | null>(null)
  const selectedWalk = walkId ?? doneWalks[0]?.id ?? null
  const resultQ = useQuery({
    queryKey: ["snmp-walk-result", selectedWalk],
    queryFn: () => snmpApi.walkResult(selectedWalk!),
    enabled: open && selectedWalk != null,
  })
  const rows = useMemo(() => resultQ.data?.walk?.result ?? [], [resultQ.data])

  // Candidates: numeric values in the vendor's private tree — where CPU/temp hide.
  const candidates = useMemo(
    () => rows.filter(([oid, val]) => oid.startsWith(ENTERPRISE_PREFIX) && NUMERIC.test(val.trim())),
    [rows])
  const [filter, setFilter] = useState("")
  const shown = useMemo(() => {
    const f = filter.trim()
    const hits = f ? candidates.filter(([oid, val]) => oid.includes(f) || val.includes(f)) : candidates
    return hits.slice(0, MAX_ROWS_SHOWN)
  }, [candidates, filter])

  const [tags, setTags] = useState<Record<string, Tag>>({}) // metric -> tagged row
  const [name, setName] = useState("")
  const [match, setMatch] = useState("")
  const [error, setError] = useState("")

  // Seed the match prefix once per open: the diagnosis' sysObjectID, else the
  // walk's own sysObjectID varbind (a System-subtree walk carries it).
  const walkSysObjectId = useMemo(() => {
    const hit = rows.find(([oid]) => oid === OID_SYS_OBJECT_ID)
    return hit ? hit[1].trim() : null
  }, [rows])
  useEffect(() => {
    if (open) setMatch((m) => m || sysObjectId || walkSysObjectId || "")
  }, [open, sysObjectId, walkSysObjectId])

  const oidToMetric = useMemo(() => {
    const map = new Map<string, string>()
    for (const [metric, t] of Object.entries(tags)) map.set(t.oid, metric)
    return map
  }, [tags])

  const setTag = (oid: string, rawVal: string, metric: string) => {
    setTags((prev) => {
      const next = { ...prev }
      // one OID carries one metric; retagging moves it
      for (const [m, t] of Object.entries(next)) if (t.oid === oid) delete next[m]
      if (metric !== "none") {
        const value = Number(rawVal.trim())
        next[metric] = { oid, value, decode: suggestDecode(value) }
      }
      return next
    })
  }

  const save = useMutation({
    mutationFn: () => {
      const metrics: Record<string, { oid: string; decode: string; select: string }> = {}
      for (const [metric, t] of Object.entries(tags)) {
        metrics[metric] = { oid: t.oid, decode: t.decode, select: "first" }
      }
      return snmpApi.createProfile({
        org_id: device.org_id, name: name.trim(), match_sysobjectid: match.trim(),
        metrics, enabled: true,
      })
    },
    onSuccess: () => {
      toast.success("Profile created — the probe applies it on its next sweep")
      qc.invalidateQueries({ queryKey: ["snmp-profiles"] })
      qc.invalidateQueries({ queryKey: ["snmp-status", device.id] })
      onOpenChange(false)
    },
    onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : "Save failed"),
  })

  const metricsVocab = (vocabQ.data?.metrics ?? Object.keys(METRIC_LABEL))
  const decodesVocab = (vocabQ.data?.decodes ?? ["as_is", "div10", "div100", "signed_div100"])
  const tagged = Object.entries(tags)
  const canSave = tagged.length > 0 && name.trim() !== "" && match.trim() !== "" && !save.isPending

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Build a health profile: {device.name}</DialogTitle>
          <DialogDescription>
            Tag the CPU / RAM / temperature rows in a walk dump. The probe matches the
            profile to this hardware by sysObjectID and starts reporting the vitals —
            no software update needed.
          </DialogDescription>
        </DialogHeader>

        {doneWalks.length === 0 ? (
          <p className="rounded-lg border bg-muted/40 px-3 py-4 text-xs text-muted-foreground">
            No finished walk to read from yet. Run an SNMP walk of the enterprise tree
            (1.3.6.1.4.1) first — it's the “Run SNMP walk” button one step back.
          </p>
        ) : (
          <div className="flex min-h-0 flex-col gap-3">
            {/* 1 · which dump ------------------------------------------------- */}
            <div className="flex flex-wrap items-end gap-2">
              <div className="flex flex-col gap-1.5">
                <Label>Walk</Label>
                <Select value={String(selectedWalk)} onValueChange={(v) => { setWalkId(Number(v)); setTags({}) }}>
                  <SelectTrigger className="h-8 w-64 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {doneWalks.map((w) => (
                      <SelectItem key={w.id} value={String(w.id)}>
                        {w.root_oid} · {w.varbind_count} rows · {ago(w.completed_at ?? w.created_at)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-1 flex-col gap-1.5">
                <Label>Filter rows</Label>
                <Input className="h-8 text-xs" placeholder="OID fragment or value…"
                  value={filter} onChange={(e) => setFilter(e.target.value)} />
              </div>
            </div>

            {/* 2 · tag the rows ------------------------------------------------ */}
            <div className="min-h-0 flex-1 overflow-auto rounded-lg border">
              <div className="sticky top-0 border-b bg-muted px-3 py-1.5 text-2xs font-medium text-muted-foreground">
                {resultQ.isLoading ? "Loading dump…"
                  : `${candidates.length} numeric vendor-tree row${candidates.length === 1 ? "" : "s"}`
                    + (shown.length < candidates.length && filter.trim() === ""
                      ? ` · showing first ${MAX_ROWS_SHOWN}, filter to narrow` : "")}
              </div>
              {!resultQ.isLoading && candidates.length === 0 && (
                <p className="px-3 py-4 text-xs text-muted-foreground">
                  This dump has no numeric rows under the vendor tree — walk
                  “Enterprise (private)” (1.3.6.1.4.1) instead.
                </p>
              )}
              {shown.map(([oid, val]) => {
                const taggedAs = oidToMetric.get(oid)
                return (
                  <div key={oid} className={cn("flex items-center gap-2 border-b px-3 py-1 last:border-b-0",
                    taggedAs && "bg-accent/50")}>
                    <span className="min-w-0 flex-1 truncate font-mono text-[0.6875rem]" dir="rtl">
                      <span dir="ltr">{oid}</span>
                    </span>
                    <span className="w-24 shrink-0 truncate text-right font-mono text-xs font-semibold tabular-nums">
                      {val}
                    </span>
                    <Select value={taggedAs ?? "none"} onValueChange={(m) => setTag(oid, val, m)}>
                      <SelectTrigger className={cn("h-6 w-36 text-2xs", !taggedAs && "text-faint-foreground")}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">—</SelectItem>
                        {metricsVocab.map((m) => (
                          <SelectItem key={m} value={m}>{METRIC_LABEL[m] ?? m}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )
              })}
            </div>

            {/* 3 · sanity-check the decodes ------------------------------------ */}
            {tagged.length > 0 && (
              <div className="rounded-lg border bg-muted/40 px-3 py-2">
                <p className="mb-1.5 text-2xs font-medium text-muted-foreground">
                  Reads as — fix the decode if a value looks wrong
                </p>
                <div className="flex flex-col gap-1">
                  {tagged.map(([metric, t]) => (
                    <div key={metric} className="flex items-center gap-2 text-xs">
                      <span className="w-36 shrink-0 font-medium">{METRIC_LABEL[metric] ?? metric}</span>
                      <Select value={t.decode}
                        onValueChange={(d) => setTags((p) => ({ ...p, [metric]: { ...t, decode: d } }))}>
                        <SelectTrigger className="h-6 w-32 text-2xs"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {decodesVocab.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
                        </SelectContent>
                      </Select>
                      <span className="font-mono tabular-nums text-muted-foreground">
                        {t.value} → <span className="font-semibold text-foreground">
                          {decodePreview(t.value, t.decode)}</span>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 4 · name it, save it --------------------------------------------- */}
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label>Profile name</Label>
                <Input placeholder="e.g. dbc-epolt-3304" value={name}
                  onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Match sysObjectID (prefix)</Label>
                <Input className="font-mono text-xs" placeholder="1.3.6.1.4.1.…" value={match}
                  onChange={(e) => setMatch(e.target.value)} />
              </div>
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
            <div className="flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button size="sm" disabled={!canSave} onClick={() => save.mutate()}>
                Create profile
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
