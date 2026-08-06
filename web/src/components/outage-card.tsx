import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Check, Clock, UserCheck } from "lucide-react"
import { outagesApi, ApiError } from "@/lib/api"
import type { Outage, OutageStatus } from "@/lib/types"
import { ROOT_CAUSES } from "@/lib/types"
import { AssignOutage } from "@/components/assign-outage"
import { durationSince, fmtDateTime, fmtDur, toUtcDate } from "@/lib/format"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

// `assigned` keeps the DESTRUCTIVE tone of `unassigned` on purpose: the owner
// naming somebody does not make a device less down, and the badge is read at a
// glance by people deciding what still needs attention. It says what is missing
// — a reply — rather than pretending the job is under way.
const STATUS_META: Record<OutageStatus, { label: string; className: string; border: string }> = {
  unassigned: { label: "Unassigned", className: "text-destructive border-destructive/30 bg-destructive-soft", border: "border-l-destructive" },
  assigned: { label: "Down · awaiting response", className: "text-destructive border-destructive/30 bg-destructive-soft", border: "border-l-destructive" },
  in_progress: { label: "In progress", className: "text-primary border-primary/30 bg-primary-soft", border: "border-l-primary" },
  pending_postmortem: { label: "Needs post-mortem", className: "text-muted-foreground border-border bg-muted", border: "border-l-muted-foreground/40" },
}

function OutageDuration({ outage }: { outage: Outage }) {
  useNow()
  if (outage.resolved_at) {
    const seconds = (toUtcDate(outage.resolved_at).getTime() - toUtcDate(outage.started_at).getTime()) / 1000
    return <span className="font-mono text-xs text-muted-foreground">lasted {fmtDur(seconds)}</span>
  }
  return <span className="font-mono text-xs font-semibold text-destructive">{durationSince(outage.started_at)}</span>
}

export function OutageCard({ outage }: { outage: Outage }) {
  const queryClient = useQueryClient()
  // The card is shared by Home and the worker view, so it decides per session
  // what it may offer: dispatch is owner-only, triage is everyone's.
  const { canWrite, user } = useAuth()
  const username = user?.username
  const [closing, setClosing] = useState(false)
  const [rootCause, setRootCause] = useState("")
  const [notes, setNotes] = useState("")

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["outages"] })

  const ack = useMutation({
    mutationFn: () => outagesApi.acknowledge(outage.id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to acknowledge"),
  })
  // "I'm on it" — the same thing the [✅ I'm on it] button on the WhatsApp
  // assignment page presses, for whoever is at a desk instead of a pole.
  const accept = useMutation({
    mutationFn: () => outagesApi.accept(outage.id),
    onSuccess: (res) => {
      invalidate()
      toast.success(res.already ? "You had already accepted this"
                                : "Marked as on the way")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to accept"),
  })
  const postmortem = useMutation({
    mutationFn: () => outagesApi.postmortem(outage.id, rootCause, notes || undefined),
    onSuccess: () => { invalidate(); setClosing(false); toast.success("Post-mortem saved") },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to save"),
  })

  const discard = () => {
    setClosing(false)
    setRootCause("")
    setNotes("")
  }

  const meta = STATUS_META[outage.status]
  const mine = !outage.resolved_at && !!username
    && outage.assigned_to.includes(username)
  const accepted = !!username && outage.accepted_by.includes(username)

  return (
    <Card className={`border-l-2 py-4 ${meta.border}`}>
      <CardContent className="flex flex-col gap-3 px-5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-mono text-sm font-semibold">{outage.device_name}</p>
            {outage.region && <p className="text-xs text-muted-foreground">{outage.region}</p>}
          </div>
          <span className={`shrink-0 rounded-full border px-2 py-0.5 text-2xs font-semibold whitespace-nowrap ${meta.className}`}>
            {meta.label}
          </span>
        </div>

        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock className="size-3 shrink-0" />
          Down {fmtDateTime(outage.started_at)}
          {outage.resolved_at && <> – {fmtDateTime(outage.resolved_at)}</>}
        </p>

        <div className="flex items-center justify-between gap-2">
          <OutageDuration outage={outage} />
          <div className="flex flex-wrap justify-end gap-2">
            {/* Owners get ASSIGN as the primary action — deciding who goes out is
                the owner's job, and "acknowledge" only ever said that somebody
                saw it. Acknowledge stays available (an owner handling it alone
                shouldn't have to assign themselves) but steps back to outline.
                A worker sees only Acknowledge: it can triage, not dispatch. */}
            {!outage.resolved_at && canWrite && <AssignOutage outage={outage} />}
            {/* The assignee's own answer. Offered only to somebody actually
                named on this outage and only until they have answered — a
                button that everyone can press would make "who accepted" mean
                nothing, and one that stays after a yes invites a second press
                that changes nothing. */}
            {mine && !accepted && (
              <Button size="sm" className="gap-1.5" disabled={accept.isPending}
                onClick={() => accept.mutate()}>
                <Check className="size-3.5" />
                {accept.isPending ? "Accepting…" : "I'm on it"}
              </Button>
            )}
            {outage.status === "unassigned" && (
              <Button size="sm" variant={canWrite ? "outline" : "default"}
                onClick={() => ack.mutate()} disabled={ack.isPending}>
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

        {outage.assigned_to.length > 0 && (
          <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <UserCheck className="size-3 shrink-0" />
            <span>Assigned to</span>
            {/* Every assignee carries their OWN answer: a tick for a yes, a
                hollow chip for silence. One chip list rather than two, because
                "who was asked" and "who is going" are the same question read at
                different moments — and a name with no tick is the thing an owner
                needs to see. */}
            {outage.assigned_to.map((who) => {
              const yes = outage.accepted_by.includes(who)
              return (
                <span key={who} className={cn(
                  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-2xs",
                  yes
                    ? "border-success/30 bg-success-soft text-success"
                    : who === username
                      ? "border-primary/30 bg-primary-soft text-primary"
                      : "border-border bg-muted text-foreground")}>
                  {yes && <Check className="size-2.5 shrink-0" />}
                  {who === username ? "you" : who}
                </span>
              )
            })}
            {outage.assigned_by && (
              <span className="text-faint-foreground">by {outage.assigned_by}</span>
            )}
          </p>
        )}

        {/* Said plainly under the chips, because the chips answer "who" and this
            answers "and now what": an assignment nobody has replied to is a job
            that may not be happening, and that is exactly what the badge above
            is claiming. */}
        {outage.status === "assigned" && (
          <p className="text-xs text-muted-foreground">
            Waiting for {outage.assigned_to.length === 1 ? "a reply" : "someone"} to
            accept. Still down, and nobody has confirmed they are going.
          </p>
        )}
        {outage.status === "in_progress" && outage.accepted_by.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {outage.accepted_by.join(", ")} accepted
            {outage.accepted_at ? ` ${fmtDateTime(outage.accepted_at)}` : ""}
            {outage.accepted_by.length < outage.assigned_to.length
              ? ` · ${outage.assigned_to.length - outage.accepted_by.length} still to reply`
              : ""}.
          </p>
        )}

        {/* Assignment already names who owns it, and stamps the ack — so don't
            also say "acknowledged by <the owner who assigned it>", which reads as
            a second, competing claim about who is on the job. */}
        {outage.status === "in_progress" && outage.assigned_to.length === 0 && (
          <p className="text-xs text-muted-foreground">Acknowledged by {outage.acknowledged_by}. Waiting for recovery.</p>
        )}

        {outage.root_cause && (
          <p className="text-xs text-muted-foreground">
            <span className="font-semibold text-foreground">{outage.root_cause}</span>
            {outage.resolution_notes ? `: ${outage.resolution_notes}` : ""}
          </p>
        )}

        {closing && (
          <div className="flex flex-col gap-2.5 border-t pt-3">
            <div>
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">Root cause</p>
              <Select value={rootCause} onValueChange={setRootCause}>
                <SelectTrigger className="w-full"><SelectValue placeholder="Select…" /></SelectTrigger>
                <SelectContent>
                  {ROOT_CAUSES.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">Notes</p>
              <Textarea
                placeholder="What happened, what fixed it…"
                rows={3}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="ghost" onClick={discard}>Discard</Button>
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
