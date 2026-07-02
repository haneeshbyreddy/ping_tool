import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Plus, Trash2, Pencil, Check, X } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { teamApi, orgsApi, ApiError } from "@/lib/api"
import type { Role, Worker } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { cn } from "@/lib/utils"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const ROLES: Role[] = ["operator", "owner", "tech"]

function EditWorkerRow({ worker, onSave, onCancel, saving }: {
  worker: Worker
  onSave: (fields: { name: string; role: Role; region?: string }) => void
  onCancel: () => void
  saving: boolean
}) {
  const [name, setName] = useState(worker.name)
  const [role, setRole] = useState<Role>(worker.role)
  const [region, setRegion] = useState(worker.region ?? "")

  return (
    <div className="flex flex-wrap items-center gap-2 border-t px-4 py-2.5">
      <Input className="w-36" value={name} onChange={(e) => setName(e.target.value)} />
      <Select value={role} onValueChange={(v) => setRole(v as Role)}>
        <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
        <SelectContent>
          {ROLES.map((r) => <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>)}
        </SelectContent>
      </Select>
      <Input className="w-28" placeholder="region" value={region} onChange={(e) => setRegion(e.target.value)} />
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

function RosterCard({ org, canWrite }: { org: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [region, setRegion] = useState("")
  const [editingId, setEditingId] = useState<number | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["team", org],
    queryFn: () => teamApi.list(org),
    enabled: !!org,
  })
  const { data: orgsData } = useQuery({ queryKey: ["orgs"], queryFn: () => orgsApi.list() })
  const orgName = orgsData?.orgs.find((o) => o.org_id === org)?.name || org
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["team"] })

  const add = useMutation({
    mutationFn: () => teamApi.add({ org_id: org, name: name.trim(), role, region: region.trim() || undefined }),
    onSuccess: () => { invalidate(); setName(""); setRegion("") },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to add"),
  })
  const update = useMutation({
    mutationFn: ({ id, fields }: { id: number; fields: { name: string; role: Role; region?: string } }) =>
      teamApi.update(id, fields),
    onSuccess: () => { invalidate(); setEditingId(null) },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to save"),
  })
  const remove = useMutation({
    mutationFn: (id: number) => teamApi.remove(id),
    onSuccess: invalidate,
    onError: () => toast.error("failed to remove"),
  })

  const team = data?.team ?? []

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Team — {orgName}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-16 w-full" /></div>}
        {!isLoading && team.length === 0 && (
          <p className="px-4 pb-4 text-sm text-muted-foreground">No team members yet.</p>
        )}
        {team.map((w) =>
          canWrite && editingId === w.id ? (
            <EditWorkerRow
              key={w.id}
              worker={w}
              saving={update.isPending}
              onCancel={() => setEditingId(null)}
              onSave={(fields) => update.mutate({ id: w.id, fields })}
            />
          ) : (
            <div key={w.id} className="flex items-center justify-between gap-2 border-t px-4 py-2.5 first:border-t-0">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{w.name}</p>
                <p className="text-xs text-muted-foreground capitalize">{w.role} · {w.region || "—"}</p>
              </div>
              {canWrite && (
                <div className="flex shrink-0 gap-1">
                  <Button variant="ghost" size="icon" className="size-7" onClick={() => setEditingId(w.id)}>
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button variant="ghost" size="icon" className="size-7" onClick={() => remove.mutate(w.id)}>
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              )}
            </div>
          ),
        )}
        {canWrite && (
          <div className="flex flex-wrap gap-2 border-t p-4">
            <Input placeholder="name" className="w-36" value={name} onChange={(e) => setName(e.target.value)} />
            <Select value={role} onValueChange={(v) => setRole(v as Role)}>
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                {ROLES.map((r) => <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>)}
              </SelectContent>
            </Select>
            <Input placeholder="region" className="w-28" value={region} onChange={(e) => setRegion(e.target.value)} />
            <Button size="sm" disabled={!name.trim() || add.isPending} onClick={() => add.mutate()}>
              <Plus className="size-4" /> Add
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function AttendanceCard({ org, canWrite }: { org: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["attendance", org],
    queryFn: () => teamApi.attendance(org),
    enabled: !!org,
  })

  const setPresent = useMutation({
    mutationFn: ({ workerId, day, present }: { workerId: number; day: string; present: boolean }) =>
      teamApi.setPresent(workerId, present, day),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["attendance"] }),
    onError: () => toast.error("failed to update attendance"),
  })

  if (isLoading) return <Skeleton className="h-32 w-full" />
  if (!data || data.operators.length === 0) return null

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Attendance · last {data.days.length} days</CardTitle></CardHeader>
      <CardContent className="overflow-x-auto">
        <div className="flex min-w-max flex-col gap-2">
          {data.operators.map((op) => (
            <div key={op.id} className="flex items-center gap-2">
              <span className="w-24 shrink-0 truncate text-xs text-muted-foreground">{op.name}</span>
              <div className="flex gap-1">
                {data.days.map((day) => {
                  const present = op.days[day]
                  return (
                    <button
                      key={day}
                      title={day}
                      disabled={!canWrite}
                      onClick={() => setPresent.mutate({ workerId: op.id, day, present: !present })}
                      className={cn(
                        "flex size-6 items-center justify-center rounded-full border text-[10px] font-bold",
                        present
                          ? "border-success/30 bg-success-soft text-success"
                          : "border-border bg-muted text-muted-foreground",
                        canWrite && "cursor-pointer",
                      )}
                    >
                      {present ? "✓" : "·"}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

export function TeamPage() {
  const { scopeOrg, canWrite } = useAuth()
  if (!scopeOrg) return <NeedsOrg />

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-6">
      <h1 className="text-xl font-bold">Team</h1>
      <RosterCard org={scopeOrg} canWrite={canWrite} />
      <AttendanceCard org={scopeOrg} canWrite={canWrite} />
    </div>
  )
}
