import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { BellRing, ChevronRight, CornerLeftUp } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Chip } from "@/components/status-badge"
import { useAuth } from "@/hooks/use-auth"
import { inventoryApi } from "@/lib/api"
import { responsibilityFor } from "@/lib/assignment"
import type { OrgDevice } from "@/lib/types"
import { cn } from "@/lib/utils"

// Who gets PAGED about this device — and only that. Assignment changes no view:
// every account still sees the whole fleet, so this panel is careful to talk about
// notifications and never about access.
//
// Two things it must always answer, because getting either wrong is how a page
// goes missing without anyone noticing:
//   * "nobody assigned" is NOT a gap — it means every worker is paged, the safe
//     default, and the panel says so in words rather than showing an empty list.
//   * a device can be covered from ABOVE. Responsibility flows down the tree, so
//     the panel names the ancestor it was inherited from instead of rendering an
//     empty set on a device that is in fact somebody's job.
export function AssignmentPanel({ device }: { device: OrgDevice }) {
  const { scopeOrg, canWrite } = useAuth()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<number[] | null>(null)
  useEffect(() => { setOpen(false); setDraft(null) }, [device.id])

  const inventory = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 30_000,
  })
  // Owner-only endpoint (it enumerates accounts), so a worker session never asks
  // — it reads the panel without the editor.
  const roster = useQuery({
    queryKey: ["assignments", scopeOrg],
    queryFn: () => inventoryApi.assignments(scopeOrg),
    enabled: !!scopeOrg && canWrite,
    staleTime: 30_000,
  })

  const devices = inventory.data?.devices ?? []
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  // the fresh row — `device` may predate a save
  const self = byId.get(device.id) ?? device
  const { own, inherited, effective } = useMemo(
    () => responsibilityFor(self, byId), [self, byId])

  const save = useMutation({
    mutationFn: (userIds: number[]) => inventoryApi.setAssignees(device.id, userIds),
    onSuccess: (res) => {
      setDraft(null)
      // An assignee with no WhatsApp number is stored, not refused — they have
      // been made responsible for something nothing will tell them about, which
      // is a fact to surface rather than a reason to reject the operator's edit.
      if (res.unreachable?.length) {
        toast.warning(
          `Saved — but ${res.unreachable.join(", ")} ${res.unreachable.length === 1 ? "has" : "have"} no WhatsApp number, so no page will reach ${res.unreachable.length === 1 ? "them" : "them"}`)
      } else {
        toast.success("Paging responsibility updated")
      }
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      queryClient.invalidateQueries({ queryKey: ["assignments"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to save"),
  })

  // Workers only. An owner is paged for everything by definition, so a checkbox
  // beside their name is a control that changes nothing — and a no-op control in
  // a notification screen invites the operator to believe they've narrowed
  // something. (The API still accepts an owner id; this is a UI choice.)
  const accounts = (roster.data?.accounts ?? []).filter((a) => a.role === "worker")
  const nameFor = (uid: number) =>
    accounts.find((a) => a.user_id === uid)?.username ??
    roster.data?.assignments.find((a) => a.user_id === uid)?.username ?? `#${uid}`

  // Closed-header summary: the answer to "who hears about this box" without
  // opening anything.
  const summary = effective.length === 0
    ? "every worker"
    : effective.map(nameFor).join(", ")

  const ticked = draft ?? own
  const dirty = draft !== null
    && (draft.length !== own.length || draft.some((u) => !own.includes(u)))

  return (
    <div className="flex flex-col rounded-lg border bg-muted/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className={cn("flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-foreground/5",
          open ? "rounded-t-lg" : "rounded-lg")}
        title="Which field accounts get a WhatsApp page when this device or one of its ports goes down. Does not affect what anyone can see.">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <span className="text-2xs font-medium text-muted-foreground">Paged for this device</span>
        {!open && (
          <span className="min-w-0 truncate font-mono text-2xs text-faint-foreground">
            {summary}
          </span>
        )}
      </button>

      {open && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          {/* Always state the CURRENT rule in words first. An empty checkbox list
              on its own reads as "nobody is being told", which is the opposite of
              what an unassigned device does. */}
          <p className="text-xs text-faint-foreground">
            {effective.length === 0 ? (
              <>Not assigned — <span className="text-muted-foreground">every worker</span> is paged
                when this device or one of its monitored ports goes down. Owners are always paged.</>
            ) : (
              <>Owners, plus the accounts below, are paged when this device or one of its
                monitored ports goes down. Other workers are not.</>
            )}
          </p>

          {/* Inherited coverage, named with its source. Without this a device
              covered from its region head looks unassigned. */}
          {inherited.length > 0 && (
            <div className="flex flex-col gap-1">
              {inherited.map(({ user_id, from }) => (
                <div key={user_id} className="flex items-center gap-2 text-xs">
                  <CornerLeftUp className="size-3.5 shrink-0 text-faint-foreground" />
                  <span className="font-mono">{nameFor(user_id)}</span>
                  <span className="min-w-0 truncate text-faint-foreground">
                    via {from.name}
                  </span>
                </div>
              ))}
            </div>
          )}

          {canWrite ? (
            <div className="flex flex-col gap-1.5">
              {roster.isLoading && (
                <p className="text-xs text-faint-foreground">Loading accounts…</p>
              )}
              {roster.isSuccess && accounts.length === 0 && (
                <p className="text-xs text-faint-foreground">
                  No field accounts yet — add one in Users to assign it here.
                </p>
              )}
              {accounts.map((a) => {
                const on = ticked.includes(a.user_id)
                const covered = inherited.some((i) => i.user_id === a.user_id)
                return (
                  <label key={a.user_id}
                    className="flex cursor-pointer items-center gap-2.5 rounded-md px-1 py-1 text-xs hover:bg-foreground/5">
                    <Checkbox checked={on}
                      onCheckedChange={(v) => {
                        const next = new Set(ticked)
                        if (v) next.add(a.user_id)
                        else next.delete(a.user_id)
                        setDraft([...next])
                      }} />
                    <span className="font-mono">{a.username}</span>
                    {/* the one thing that makes an assignment a lie */}
                    {!a.has_whatsapp && <Chip tone="warning">no number</Chip>}
                    {covered && !on && (
                      <span className="text-faint-foreground">already covers this</span>
                    )}
                    {a.devices > 0 && (
                      <span className="ml-auto shrink-0 font-mono text-2xs text-faint-foreground">
                        {a.devices} dev
                      </span>
                    )}
                  </label>
                )
              })}
              {dirty && (
                <div className="mt-1 flex items-center gap-2">
                  <Button size="sm" className="h-7 text-xs"
                    disabled={save.isPending}
                    onClick={() => save.mutate(ticked)}>
                    <BellRing className="size-3" />
                    {ticked.length === 0 ? "Clear — page every worker" : "Save"}
                  </Button>
                  <Button size="sm" variant="ghost" className="h-7 text-xs"
                    onClick={() => setDraft(null)}>
                    Cancel
                  </Button>
                </div>
              )}
            </div>
          ) : (
            own.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {own.map((uid) => (
                  <span key={uid} className="font-mono text-xs">{nameFor(uid)}</span>
                ))}
              </div>
            )
          )}

          {/* Responsibility flows DOWN, so say what this row actually costs. */}
          {self.child_count > 0 && (
            <p className="border-t pt-2 text-2xs text-faint-foreground">
              Also covers everything below this device — {self.child_count} direct
              {self.child_count === 1 ? " child" : " children"} and their subtrees.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
