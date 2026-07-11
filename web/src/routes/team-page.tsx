import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Plus, Trash2, Pencil, Check, X, MoreVertical } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { teamApi, ApiError } from "@/lib/api"
import type { AttendanceOperator, Role, Worker } from "@/lib/types"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { NeedsOrg } from "@/components/needs-org"
import { RegionSelect } from "@/components/region-select"
import { cn } from "@/lib/utils"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

const ROLES: Role[] = ["operator", "owner", "tech"]

function EditWorkerRow({ org, worker, onSave, onCancel, saving }: {
  org: string
  worker: Worker
  onSave: (fields: { name: string; role: Role; region?: string }) => void
  onCancel: () => void
  saving: boolean
}) {
  const [name, setName] = useState(worker.name)
  const [role, setRole] = useState<Role>(worker.role)
  const [region, setRegion] = useState(worker.region ?? "")

  return (
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2 last:border-b-0">
      <Input className="h-8 w-36" value={name} onChange={(e) => setName(e.target.value)} />
      <Select value={role} onValueChange={(v) => setRole(v as Role)}>
        <SelectTrigger className="h-8 w-32"><SelectValue /></SelectTrigger>
        <SelectContent>
          {ROLES.map((r) => <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>)}
        </SelectContent>
      </Select>
      <RegionSelect org={org} value={region} onChange={setRegion}
        className="h-8 w-32" inputClassName="h-8 w-28" />
      <div className="ml-auto flex gap-1">
        <Button
          variant="ghost" size="icon" className="size-7 shrink-0"
          disabled={!name.trim() || saving}
          onClick={() => onSave({ name: name.trim(), role, region: region.trim() || undefined })}
        >
          <Check className="size-3.5" />
        </Button>
        <Button variant="ghost" size="icon" className="size-7 shrink-0" onClick={onCancel}>
          <X className="size-3.5" />
        </Button>
      </div>
    </div>
  )
}

function HistoryStrip({
  op, days, today, canWrite, onToggle,
}: {
  op: AttendanceOperator
  days: string[]
  today: string
  canWrite: boolean
  onToggle: (day: string, present: boolean) => void
}) {
  const past = days.filter((d) => d !== today)
  return (
    <div className="hidden items-center gap-1 sm:flex">
      {past.map((day) => {
        const present = !!op.days[day]
        return (
          <button
            key={day}
            title={`${day}: ${present ? "present" : "absent"}${canWrite ? " (click to change)" : ""}`}
            disabled={!canWrite}
            onClick={() => onToggle(day, !present)}
            className={cn(
              "size-2.5 rounded-[3px]",
              present ? "bg-success/70" : "bg-muted",
              canWrite && "cursor-pointer hover:ring-1 hover:ring-ring",
            )}
          />
        )
      })}
    </div>
  )
}

export function TeamPage() {
  const { scopeOrg, canWrite } = useAuth()
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [name, setName] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [region, setRegion] = useState("")
  const [editingId, setEditingId] = useState<number | null>(null)
  const [removing, setRemoving] = useState<Worker | null>(null)

  const team = useQuery({
    queryKey: ["team", scopeOrg],
    queryFn: () => teamApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const attendance = useQuery({
    queryKey: ["attendance", scopeOrg],
    queryFn: () => teamApi.attendance(scopeOrg),
    enabled: !!scopeOrg,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["team"] })
    queryClient.invalidateQueries({ queryKey: ["attendance"] })
    queryClient.invalidateQueries({ queryKey: ["regions"] })
  }

  const add = useMutation({
    mutationFn: () => teamApi.add({ org_id: scopeOrg!, name: name.trim(), role, region: region.trim() || undefined }),
    onSuccess: () => { invalidate(); setName(""); setRegion(""); setAddOpen(false) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to add"),
  })
  const update = useMutation({
    mutationFn: ({ id, fields }: { id: number; fields: { name: string; role: Role; region?: string } }) =>
      teamApi.update(id, fields),
    onSuccess: () => { invalidate(); setEditingId(null) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to save"),
  })
  const remove = useMutation({
    mutationFn: (id: number) => teamApi.remove(id),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to remove"),
  })
  const setPresent = useMutation({
    mutationFn: ({ workerId, day, present }: { workerId: number; day?: string; present: boolean }) =>
      teamApi.setPresent(workerId, present, day),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["attendance"] }),
    onError: () => toast.error("Failed to update attendance"),
  })

  if (!scopeOrg) return <NeedsOrg />

  const workers = team.data?.team ?? []
  const att = attendance.data
  const attByWorker = new Map((att?.operators ?? []).map((o) => [o.id, o]))
  const onDuty = (att?.operators ?? []).filter((o) => o.present_today).length

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-3 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold tracking-tight">Team</h1>
          {att && att.operators.length > 0 && (
            <p className="text-xs text-muted-foreground">
              <span className={cn("font-semibold", onDuty > 0 ? "text-success" : "text-destructive")}>
                {onDuty} on duty
              </span>
              {" "}of {att.operators.length} operator{att.operators.length === 1 ? "" : "s"} today
            </p>
          )}
        </div>
        {canWrite && !addOpen && (
          <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => setAddOpen(true)}>
            <Plus className="size-3.5" /> Add member
          </Button>
        )}
      </div>

      {addOpen && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-2">
          <Input autoFocus placeholder="name" className="h-8 w-36" value={name} onChange={(e) => setName(e.target.value)} />
          <Select value={role} onValueChange={(v) => setRole(v as Role)}>
            <SelectTrigger className="h-8 w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              {ROLES.map((r) => <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>)}
            </SelectContent>
          </Select>
          <RegionSelect org={scopeOrg} value={region} onChange={setRegion}
            className="h-8 w-32" inputClassName="h-8 w-28" />
          <div className="ml-auto flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setAddOpen(false)}>Cancel</Button>
            <Button size="sm" disabled={!name.trim() || add.isPending} onClick={() => add.mutate()}>Add</Button>
          </div>
        </div>
      )}

      {team.isLoading && <Skeleton className="h-24 w-full" />}
      {!team.isLoading && workers.length === 0 && (
        <p className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
          No team members yet. Add one above.
        </p>
      )}
      {workers.length > 0 && (
        <Card className="gap-0 overflow-hidden py-0">
          {workers.map((w) => {
            if (canWrite && editingId === w.id) {
              return (
                <EditWorkerRow
                  key={w.id}
                  org={scopeOrg}
                  worker={w}
                  saving={update.isPending}
                  onCancel={() => setEditingId(null)}
                  onSave={(fields) => update.mutate({ id: w.id, fields })}
                />
              )
            }
            const op = attByWorker.get(w.id)
            return (
              <div key={w.id} className="group flex h-12 items-center gap-3 border-b px-3 last:border-b-0 hover:bg-foreground/5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold">{w.name}</p>
                  <p className="text-xs text-muted-foreground capitalize">
                    {w.role}{w.region ? <span className="normal-case"> · {w.region}</span> : ""}
                  </p>
                </div>
                {op && att && (
                  <>
                    <HistoryStrip
                      op={op} days={att.days} today={att.today} canWrite={canWrite}
                      onToggle={(day, present) => setPresent.mutate({ workerId: w.id, day, present })}
                    />
                    <label className="flex shrink-0 items-center gap-2">
                      <span className={cn(
                        "text-xs",
                        op.present_today ? "font-semibold text-success" : "text-muted-foreground",
                      )}>
                        {op.present_today ? "On duty" : "Off"}
                      </span>
                      <Switch
                        checked={op.present_today}
                        disabled={!canWrite || setPresent.isPending}
                        onCheckedChange={(v) => setPresent.mutate({ workerId: w.id, present: v })}
                      />
                    </label>
                  </>
                )}
                {canWrite && (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon"
                        className="size-6 shrink-0 text-muted-foreground opacity-60 group-hover:opacity-100 data-[state=open]:opacity-100">
                        <MoreVertical className="size-3.5" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onClick={() => setEditingId(w.id)}>
                        <Pencil /> Edit
                      </DropdownMenuItem>
                      <DropdownMenuItem variant="destructive" onClick={() => setRemoving(w)}>
                        <Trash2 /> Remove
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </div>
            )
          })}
        </Card>
      )}
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => { if (!o) setRemoving(null) }}
        title={`Remove ${removing?.name ?? ""}?`}
        description="Their attendance history goes with them. This cannot be undone."
        confirmLabel="Remove"
        onConfirm={() => { if (removing) remove.mutate(removing.id) }}
      />
      {/* the strip itself is sm+-only, so the hint hides with it */}
      {att && att.days.length > 1 && workers.some((w) => attByWorker.has(w.id)) && (
        <p className="hidden text-xs text-faint-foreground sm:block">
          Squares show the past {att.days.length - 1} days{canWrite ? ". Click one to correct a missed day." : "."}
        </p>
      )}
    </div>
  )
}
