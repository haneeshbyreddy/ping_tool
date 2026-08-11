import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { BellRing, CornerLeftUp, Search, Users } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Chip } from "@/components/status-badge"
import { inventoryApi } from "@/lib/api"
import { responsibilityFor, scopeOf } from "@/lib/assignment"
import { isPassiveType, type AssignableAccount, type OrgDevice } from "@/lib/types"
import { cn } from "@/lib/utils"

function DevicePicker({ account, devices, onClose }: {
  account: AssignableAccount
  devices: OrgDevice[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState("")
  const [region, setRegion] = useState<string | null>(null)
  const initial = useMemo(
    () => new Set(devices.filter((d) => (d.assignee_ids ?? []).includes(account.user_id))
      .map((d) => d.id)),
    [devices, account.user_id])
  const [ticked, setTicked] = useState<Set<number>>(initial)
  useEffect(() => { setTicked(initial) }, [initial])

  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const regions = useMemo(() => {
    const seen = new Map<string, number>()
    for (const d of devices) {
      const r = d.region || "—"
      seen.set(r, (seen.get(r) ?? 0) + 1)
    }
    return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [devices])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    return devices.filter((d) => {
      if (region !== null && (d.region || "—") !== region) return false
      if (!q) return true
      return d.name.toLowerCase().includes(q) || d.ip_address.toLowerCase().includes(q)
    })
  }, [devices, query, region])

  const save = useMutation({
    mutationFn: async () => {
      const add = [...ticked].filter((id) => !initial.has(id))
      const remove = [...initial].filter((id) => !ticked.has(id))
      const results: string[] = []
      if (add.length) {
        const r = await inventoryApi.bulkAssign(add, [account.user_id], "add")
        results.push(...(r.unreachable ?? []))
      }
      if (remove.length) {
        await inventoryApi.bulkAssign(remove, [account.user_id], "remove")
      }
      return { added: add.length, removed: remove.length, unreachable: results }
    },
    onSuccess: (r) => {
      if (r.unreachable.length) {
        toast.warning(`Saved. ${account.username} has no WhatsApp number, so no page reaches them yet.`)
      } else {
        const parts = [
          r.added ? `${r.added} added` : null,
          r.removed ? `${r.removed} removed` : null,
        ].filter(Boolean)
        toast.success(parts.length ? `${account.username}: ${parts.join(", ")}` : "No changes")
      }
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      queryClient.invalidateQueries({ queryKey: ["assignments"] })
      onClose()
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to save"),
  })

  const reach = useMemo(() => {
    const projected = devices.map((d) => ({
      ...d,
      assignee_ids: ticked.has(d.id)
        ? [...new Set([...(d.assignee_ids ?? []), account.user_id])]
        : (d.assignee_ids ?? []).filter((u) => u !== account.user_id),
    }))
    return scopeOf(account.user_id, projected).size
  }, [devices, ticked, account.user_id])

  const dirty = ticked.size !== initial.size || [...ticked].some((id) => !initial.has(id))
  const toggleAllShown = () => {
    const allOn = shown.every((d) => ticked.has(d.id))
    const next = new Set(ticked)
    for (const d of shown) {
      if (allOn) next.delete(d.id)
      else next.add(d.id)
    }
    setTicked(next)
  }

  return (
    <DialogContent className="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>Devices {account.username} is paged for</DialogTitle>
        <DialogDescription>
          Ticking a device also covers everything below it, so one switch or OLT can
          carry a whole region. Notifications only: {account.username} can already
          see the entire network.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint-foreground" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name or IP…" className="h-8 pl-8 text-xs" />
          </div>
          <Button variant="outline" size="sm" className="h-8 shrink-0 text-xs"
            onClick={toggleAllShown} disabled={shown.length === 0}>
            {shown.every((d) => ticked.has(d.id)) && shown.length > 0
              ? "Untick shown" : `Tick all ${shown.length}`}
          </Button>
        </div>

        <div className="flex flex-wrap gap-1">
          <button type="button" onClick={() => setRegion(null)}
            className={cn("rounded-md border px-2 py-0.5 text-2xs",
              region === null ? "bg-accent" : "text-muted-foreground hover:bg-foreground/5")}>
            all regions
          </button>
          {regions.map(([r, n]) => (
            <button key={r} type="button" onClick={() => setRegion(r)}
              className={cn("rounded-md border px-2 py-0.5 text-2xs",
                region === r ? "bg-accent" : "text-muted-foreground hover:bg-foreground/5")}>
              {r} <span className="text-faint-foreground">{n}</span>
            </button>
          ))}
        </div>

        <div className="max-h-[45svh] overflow-y-auto rounded-lg border">
          {shown.length === 0 && (
            <p className="px-3 py-4 text-xs text-faint-foreground">No devices match.</p>
          )}
          {shown.map((d) => {
            const { inherited } = responsibilityFor(d, byId)
            const via = inherited.find((i) => i.user_id === account.user_id)?.from
            return (
              <label key={d.id}
                className="flex cursor-pointer items-center gap-2.5 border-t px-3 py-1.5 text-xs first:border-t-0 hover:bg-foreground/5">
                <Checkbox checked={ticked.has(d.id)}
                  onCheckedChange={(v) => {
                    const next = new Set(ticked)
                    if (v) next.add(d.id)
                    else next.delete(d.id)
                    setTicked(next)
                  }} />
                <span className="min-w-0 truncate font-mono">{d.name}</span>
                {d.device_type && (
                  <span className="shrink-0 text-faint-foreground">{d.device_type}</span>
                )}
                {isPassiveType(d.device_type) && <Chip tone="muted">passive</Chip>}
                {via && !ticked.has(d.id) && (
                  <span className="ml-auto flex shrink-0 items-center gap-1 text-faint-foreground">
                    <CornerLeftUp className="size-3" /> via {via.name}
                  </span>
                )}
              </label>
            )
          })}
        </div>
      </div>

      <DialogFooter className="items-center sm:justify-between">
        <span className="text-xs text-faint-foreground">
          {ticked.size} ticked · paged for {reach} device{reach === 1 ? "" : "s"}
        </span>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}>Save</Button>
        </div>
      </DialogFooter>
    </DialogContent>
  )
}

export function AssignmentCard({ org }: { org: string }) {
  const [editing, setEditing] = useState<AssignableAccount | null>(null)

  const roster = useQuery({
    queryKey: ["assignments", org],
    queryFn: () => inventoryApi.assignments(org),
  })
  const inventory = useQuery({
    queryKey: ["inventory", org],
    queryFn: () => inventoryApi.list(org),
  })

  const accounts = roster.data?.accounts ?? []
  const devices = inventory.data?.devices ?? []
  const workers = accounts.filter((a) => a.role === "worker")
  const unassigned = roster.data?.unassigned ?? 0

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Device responsibility</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        <p className="px-4 pb-3 text-xs text-muted-foreground">
          Who gets a WhatsApp page when a device or one of its monitored ports goes
          down. Covers everything below the device it's set on. Owners are always
          paged, and nobody's view of the dashboard changes.
        </p>

        {(roster.isLoading || inventory.isLoading) && (
          <div className="px-4 pb-4"><Skeleton className="h-16 w-full" /></div>
        )}

        {roster.isSuccess && workers.length === 0 && (
          <p className="px-4 pb-4 text-xs text-faint-foreground">
            No field accounts yet. Add one under Login accounts above, then assign
            its devices here.
          </p>
        )}

        {workers.map((a) => (
          <div key={a.user_id}
            className="flex items-center justify-between gap-3 border-t px-4 py-2.5">
            <div className="min-w-0">
              <p className="flex items-center gap-2 truncate text-sm font-semibold">
                {a.username}
                {!a.has_whatsapp && <Chip tone="warning">no WhatsApp number</Chip>}
              </p>
              <p className="text-xs text-muted-foreground">
                {a.devices === 0 ? (
                  <>Not assigned · paged for every unassigned device</>
                ) : (
                  <>Paged for {a.devices} device{a.devices === 1 ? "" : "s"}
                    {a.assigned !== a.devices && <> · {a.assigned} set directly</>}</>
                )}
              </p>
            </div>
            <Button variant="outline" size="sm" className="shrink-0"
              onClick={() => setEditing(a)}>
              <Users className="size-4" /> Devices
            </Button>
          </div>
        ))}

        {roster.isSuccess && (
          <div className="flex items-start gap-2 border-t px-4 py-2.5 text-xs text-muted-foreground">
            <BellRing className="mt-0.5 size-3.5 shrink-0 text-faint-foreground" />
            {unassigned === 0 ? (
              <span>Every device has someone responsible for it.</span>
            ) : (
              <span>
                {unassigned} device{unassigned === 1 ? "" : "s"} nobody is assigned to,
                so every worker is paged for {unassigned === 1 ? "it" : "them"}. That's
                the safe default, not an error.
              </span>
            )}
          </div>
        )}
      </CardContent>

      <Dialog open={!!editing} onOpenChange={(v) => !v && setEditing(null)}>
        {editing && (
          <DevicePicker account={editing} devices={devices}
            onClose={() => setEditing(null)} />
        )}
      </Dialog>
    </Card>
  )
}
