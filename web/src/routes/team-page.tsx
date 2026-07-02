import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Plus, Trash2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { teamApi, ApiError } from "@/lib/api"
import type { Role } from "@/lib/types"
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

function RosterCard({ tenant, canWrite }: { tenant: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [region, setRegion] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["team", tenant],
    queryFn: () => teamApi.list(tenant),
    enabled: !!tenant,
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["team"] })

  const add = useMutation({
    mutationFn: () => teamApi.add({ tenant_id: tenant, name: name.trim(), role, region: region.trim() || undefined }),
    onSuccess: () => { invalidate(); setName(""); setRegion("") },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to add"),
  })
  const remove = useMutation({
    mutationFn: (id: number) => teamApi.remove(id),
    onSuccess: invalidate,
    onError: () => toast.error("failed to remove"),
  })

  const team = data?.team ?? []

  return (
    <Card>
      <CardHeader><CardTitle className="text-sm">Team — {tenant}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-0 p-0">
        {isLoading && <div className="px-4 pb-4"><Skeleton className="h-16 w-full" /></div>}
        {!isLoading && team.length === 0 && (
          <p className="px-4 pb-4 text-sm text-muted-foreground">No team members yet.</p>
        )}
        {team.map((w) => (
          <div key={w.id} className="flex items-center justify-between gap-2 border-t px-4 py-2.5 first:border-t-0">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{w.name}</p>
              <p className="text-xs text-muted-foreground capitalize">{w.role} · {w.region || "—"}</p>
            </div>
            {canWrite && (
              <Button variant="ghost" size="icon" className="size-7 shrink-0" onClick={() => remove.mutate(w.id)}>
                <Trash2 className="size-3.5" />
              </Button>
            )}
          </div>
        ))}
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

function AttendanceCard({ tenant, canWrite }: { tenant: string; canWrite: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["attendance", tenant],
    queryFn: () => teamApi.attendance(tenant),
    enabled: !!tenant,
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
  const { scopeTenant, canWrite } = useAuth()
  if (!scopeTenant) return <NeedsOrg />

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-6">
      <h1 className="text-xl font-bold">Team</h1>
      <RosterCard tenant={scopeTenant} canWrite={canWrite} />
      <AttendanceCard tenant={scopeTenant} canWrite={canWrite} />
    </div>
  )
}
