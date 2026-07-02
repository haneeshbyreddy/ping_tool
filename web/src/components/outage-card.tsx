import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { outagesApi, ApiError } from "@/lib/api"
import type { Outage, OutageStatus } from "@/lib/types"
import { ROOT_CAUSES } from "@/lib/types"
import { durationSince, fmtDur, toUtcDate } from "@/lib/format"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const STATUS_META: Record<OutageStatus, { label: string; className: string; border: string }> = {
  unassigned: { label: "Unassigned", className: "text-warning border-warning/30 bg-warning-soft", border: "border-l-warning" },
  in_progress: { label: "In progress", className: "text-primary border-primary/30 bg-primary-soft", border: "border-l-primary" },
  pending_postmortem: { label: "Pending post-mortem", className: "text-success border-success/30 bg-success-soft", border: "border-l-success" },
}

function OutageDuration({ outage }: { outage: Outage }) {
  if (outage.resolved_at) {
    const seconds = (toUtcDate(outage.resolved_at).getTime() - toUtcDate(outage.started_at).getTime()) / 1000
    return <span className="font-mono text-xs text-muted-foreground">lasted {fmtDur(seconds)}</span>
  }
  return <span className="font-mono text-xs font-semibold text-destructive">{durationSince(outage.started_at)}</span>
}

export function OutageCard({ outage }: { outage: Outage }) {
  const queryClient = useQueryClient()
  const [closing, setClosing] = useState(false)
  const [rootCause, setRootCause] = useState("")
  const [notes, setNotes] = useState("")

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["outages"] })

  const ack = useMutation({
    mutationFn: () => outagesApi.acknowledge(outage.id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to acknowledge"),
  })
  const postmortem = useMutation({
    mutationFn: () => outagesApi.postmortem(outage.id, rootCause, notes || undefined),
    onSuccess: () => { invalidate(); setClosing(false); toast.success("Post-mortem saved") },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to save"),
  })

  const meta = STATUS_META[outage.status]

  return (
    <Card className={`border-l-4 py-3.5 ${meta.border}`}>
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-mono text-sm font-bold">{outage.device_name}</p>
            <p className="text-xs text-muted-foreground">{outage.region || "—"}</p>
          </div>
          <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${meta.className}`}>
            {meta.label}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <OutageDuration outage={outage} />
          <div className="flex gap-2">
            {outage.status === "unassigned" && (
              <Button size="sm" onClick={() => ack.mutate()} disabled={ack.isPending}>
                Acknowledge
              </Button>
            )}
            {outage.status === "pending_postmortem" && !closing && (
              <Button size="sm" variant="outline" onClick={() => setClosing(true)}>
                Add post-mortem
              </Button>
            )}
          </div>
        </div>

        {outage.status === "in_progress" && (
          <p className="text-xs text-muted-foreground">Acknowledged by {outage.acknowledged_by} — waiting for recovery.</p>
        )}

        {closing && (
          <div className="flex flex-col gap-2.5 border-t pt-3">
            <div>
              <p className="mb-1.5 text-[10.5px] font-bold tracking-wide text-muted-foreground uppercase">Root cause</p>
              <Select value={rootCause} onValueChange={setRootCause}>
                <SelectTrigger className="w-full"><SelectValue placeholder="Select…" /></SelectTrigger>
                <SelectContent>
                  {ROOT_CAUSES.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <p className="mb-1.5 text-[10.5px] font-bold tracking-wide text-muted-foreground uppercase">Notes</p>
              <Textarea
                placeholder="What happened, what fixed it…"
                rows={3}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="ghost" onClick={() => setClosing(false)}>Cancel</Button>
              <Button
                size="sm"
                disabled={!rootCause || postmortem.isPending}
                onClick={() => postmortem.mutate()}
              >
                Save &amp; close
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
