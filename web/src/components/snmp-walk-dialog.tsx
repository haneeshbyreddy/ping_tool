import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Download, Loader2, Play } from "lucide-react"
import { snmpApi, ApiError } from "@/lib/api"
import type { OrgDevice, SnmpWalk } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

// A curated shortlist for the "walk this branch" picker — the whole point is that
// an operator onboarding a new vendor doesn't need to know OIDs by heart. Enterprise
// = the vendor's private tree (where cheap gear hides CPU/temp); system = the identity
// leaves you always want when starting cold.
const ROOT_PRESETS: Array<{ label: string; oid: string; hint: string }> = [
  { label: "System", oid: "1.3.6.1.2.1.1", hint: "name, descr, sysObjectID (who made it)" },
  { label: "Enterprise (private)", oid: "1.3.6.1.4.1", hint: "vendor tree; CPU/temp/RAM live here" },
  { label: "Interfaces", oid: "1.3.6.1.2.1.2.2", hint: "per-port counters & status" },
  { label: "Everything (MIB-2)", oid: "1.3.6.1", hint: "large, bounded by the varbind cap" },
]

function StatusPill({ w }: { w: SnmpWalk }) {
  if (w.status === "pending") {
    return (
      <span className="inline-flex items-center gap-1 text-[0.75rem] font-semibold text-warning">
        <Loader2 className="size-3 animate-spin" /> running
      </span>
    )
  }
  if (w.status === "error") {
    return <span className="text-[0.75rem] font-semibold text-destructive" title={w.error ?? ""}>failed</span>
  }
  return <span className="text-[0.75rem] font-semibold text-success">{w.varbind_count} rows</span>
}

function downloadResult(deviceName: string, walk: { root_oid: string }, rows: Array<[string, string]>) {
  const body = rows.map(([oid, val]) => `${oid} = ${val}`).join("\n")
  const blob = new Blob([body], { type: "text/plain" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = `snmpwalk-${deviceName}-${walk.root_oid}.txt`
  a.click()
  URL.revokeObjectURL(url)
}

function WalkRow({ device, walk }: { device: OrgDevice; walk: SnmpWalk }) {
  const [open, setOpen] = useState(false)
  const result = useQuery({
    queryKey: ["snmp-walk-result", walk.id],
    queryFn: () => snmpApi.walkResult(walk.id),
    enabled: open && walk.status === "done",
  })
  const rows = result.data?.walk?.result ?? []

  return (
    <div className="border-b last:border-b-0">
      <button
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-accent/40 disabled:cursor-default"
        disabled={walk.status !== "done"}
        onClick={() => setOpen((v) => !v)}>
        <span className="font-mono text-xs">{walk.root_oid}</span>
        <span className="ml-auto flex items-center gap-3">
          <StatusPill w={walk} />
          <span className="text-[0.6875rem] text-muted-foreground">
            {ago(walk.completed_at ?? walk.created_at)}
          </span>
        </span>
      </button>
      {open && walk.status === "done" && (
        <div className="bg-muted/40 px-3 py-2">
          {result.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
          {rows.length > 0 && (
            <>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[0.75rem] text-muted-foreground">
                  {rows.length} varbind{rows.length === 1 ? "" : "s"}
                  {walk.max_varbinds <= rows.length && " (capped)"}
                </span>
                <Button variant="outline" size="sm" className="h-6"
                  onClick={() => downloadResult(device.name, walk, rows)}>
                  <Download className="size-3" /> .txt
                </Button>
              </div>
              <pre className="max-h-64 overflow-auto rounded border bg-card p-2 font-mono text-[0.6875rem] leading-relaxed">
                {rows.map(([oid, val]) => `${oid} = ${val}`).join("\n")}
              </pre>
            </>
          )}
          {!result.isLoading && rows.length === 0 && (
            <p className="text-xs text-muted-foreground">No varbinds returned.</p>
          )}
        </div>
      )}
    </div>
  )
}

export function SnmpWalkDialog({ device, open, onOpenChange }: {
  device: OrgDevice
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [oid, setOid] = useState("1.3.6.1.2.1.1")
  const { data, isLoading } = useQuery({
    queryKey: ["snmp-walks", device.id],
    queryFn: () => snmpApi.walks(device.id),
    enabled: open,
    refetchInterval: (q) =>
      (q.state.data?.walks ?? []).some((w) => w.status === "pending") ? 5_000 : false,
  })

  const start = useMutation({
    mutationFn: () => snmpApi.startWalk(device.id, oid.trim()),
    onSuccess: () => {
      toast.success("Walk queued. It runs on the probe's next report")
      queryClient.invalidateQueries({ queryKey: ["snmp-walks", device.id] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Could not queue the walk"),
  })

  const walks = data?.walks ?? []
  const snmpReady = device.snmp_enabled === 1 && !!device.assigned_node_id

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>SNMP walk: {device.name}</DialogTitle>
          <DialogDescription>
            Ask the assigned probe to dump an OID subtree from {device.ip_address}. Use this
            to discover a new vendor's OIDs, then turn them into a health profile.
          </DialogDescription>
        </DialogHeader>

        {!snmpReady ? (
          <p className="rounded-lg border border-warning/30 bg-warning-soft/40 px-3 py-2 text-xs text-warning">
            {device.snmp_enabled !== 1
              ? "Enable SNMP on this device (with a community) first."
              : "Assign this device to a probe first. The walk runs from its assigned node."}
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-1.5">
              {ROOT_PRESETS.map((p) => (
                <button key={p.oid} title={p.hint}
                  className={cn("rounded-full border px-2.5 py-0.5 text-[0.75rem] transition-colors",
                    oid === p.oid ? "border-primary/50 bg-primary/10 text-foreground"
                      : "text-muted-foreground hover:text-foreground")}
                  onClick={() => setOid(p.oid)}>
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex items-end gap-2">
              <div className="flex flex-1 flex-col gap-1.5">
                <Label>Root OID</Label>
                <Input className="font-mono text-xs" value={oid}
                  onChange={(e) => setOid(e.target.value)} placeholder="1.3.6.1.4.1" />
              </div>
              <Button disabled={!oid.trim() || start.isPending} onClick={() => start.mutate()}>
                <Play className="size-3.5" /> Run walk
              </Button>
            </div>
          </>
        )}

        <div className="overflow-hidden rounded-lg border">
          <div className="border-b bg-muted/40 px-3 py-2 text-[0.75rem] font-medium text-muted-foreground">
            Recent walks
          </div>
          {isLoading && <p className="px-3 py-3 text-xs text-muted-foreground">Loading…</p>}
          {!isLoading && walks.length === 0 && (
            <p className="px-3 py-3 text-xs text-muted-foreground">No walks yet.</p>
          )}
          {walks.map((w) => <WalkRow key={w.id} device={device} walk={w} />)}
        </div>
      </DialogContent>
    </Dialog>
  )
}
