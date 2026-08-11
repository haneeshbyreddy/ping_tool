import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { MessageSquareOff, UserPlus } from "lucide-react"
import { inventoryApi, outagesApi, usersApi, ApiError } from "@/lib/api"
import { responsibilityFor } from "@/lib/assignment"
import type { Outage } from "@/lib/types"
import { useAuth } from "@/hooks/use-auth"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

export function AssignOutage({ outage }: { outage: Outage }) {
  const { scopeOrg } = useAuth()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState<string[]>(outage.assigned_to)

  useEffect(() => {
    if (open) setPicked(outage.assigned_to)
  }, [open, outage.assigned_to])

  const users = useQuery({
    queryKey: ["users", scopeOrg],
    queryFn: () => usersApi.list(scopeOrg),
    enabled: open && !!scopeOrg,
  })
  const inventory = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: open && !!scopeOrg,
    staleTime: 30_000,
  })

  const assign = useMutation({
    mutationFn: () => outagesApi.assign(outage.id, picked),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["outages"] })
      setOpen(false)
      const missed = res.assigned_to.length - res.notified
      toast.success(
        `Assigned to ${res.assigned_to.join(", ")}`,
        missed > 0
          ? { description: `${missed} of them has no WhatsApp number. Tell them another way.` }
          : undefined)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Could not assign"),
  })

  const devices = inventory.data?.devices ?? []
  const device = devices.find((d) => d.id === outage.device_id)
  const responsible = new Set<number>(
    device
      ? responsibilityFor(device, new Map(devices.map((d) => [d.id, d]))).effective
      : [])

  const candidates = (users.data?.users ?? [])
    .filter((u) => u.org_id === scopeOrg && u.is_active)
    .sort((a, b) => {
      const ra = responsible.has(a.id) ? 0 : 1
      const rb = responsible.has(b.id) ? 0 : 1
      if (ra !== rb) return ra - rb
      return a.role === b.role ? a.username.localeCompare(b.username)
        : a.role === "worker" ? -1 : 1
    })

  const toggle = (username: string) =>
    setPicked((prev) => prev.includes(username)
      ? prev.filter((u) => u !== username)
      : [...prev, username])

  return (
    <>
      <Button size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
        <UserPlus className="size-3.5" />
        {outage.assigned_to.length > 0 ? "Reassign" : "Assign"}
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Assign {outage.device_name}</DialogTitle>
            <DialogDescription>
              Everyone you pick is paged on WhatsApp with an “I'm on it” button
              and sees the outage in their own view. The outage stays down until
              somebody accepts. Saving replaces the current assignment.
            </DialogDescription>
          </DialogHeader>

          {users.isLoading && <Skeleton className="h-24 w-full" />}
          {users.isError && (
            <p className="text-xs text-destructive">
              {users.error instanceof ApiError ? users.error.message : "Could not load the team"}
            </p>
          )}
          {users.isSuccess && candidates.length === 0 && (
            <p className="rounded-lg border border-warning/30 bg-warning-soft/40 px-3 py-2 text-xs text-warning">
              This org has no active accounts to assign to. Add a worker login
              under Settings → Users first.
            </p>
          )}

          <div className="flex max-h-64 flex-col overflow-y-auto">
            {candidates.map((u) => (
              <label key={u.id}
                className="flex h-11 cursor-pointer items-center gap-3 border-b border-border-subtle px-1 last:border-0 hover:bg-foreground/5">
                <Checkbox checked={picked.includes(u.username)}
                  onCheckedChange={() => toggle(u.username)} />
                <span className="min-w-0 truncate font-mono text-xs font-medium">
                  {u.username}
                </span>
                <span className="text-2xs text-faint-foreground">{u.role}</span>
                {responsible.has(u.id) && (
                  <span className="rounded-md border border-primary/30 bg-primary-soft px-1.5 text-2xs text-primary">
                    paged for this device
                  </span>
                )}
                {!u.whatsapp_number && (
                  <span className="ml-auto flex items-center gap-1 text-2xs text-faint-foreground">
                    <MessageSquareOff className="size-3" /> no WhatsApp
                  </span>
                )}
              </label>
            ))}
          </div>

          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
            <Button size="sm" disabled={picked.length === 0 || assign.isPending}
              onClick={() => assign.mutate()}>
              {assign.isPending ? "Assigning…" : `Assign ${picked.length || ""}`.trim()}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
