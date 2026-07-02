import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { Clock, TriangleAlert, ArrowDown } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { summaryApi, inventoryApi, outagesApi, logsApi } from "@/lib/api"
import { NeedsOrg } from "@/components/needs-org"
import { OutageCard } from "@/components/outage-card"
import { StatusDot } from "@/components/status-badge"
import { ago, stateTone } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

export function HomePage() {
  const { scopeOrg } = useAuth()

  const summary = useQuery({
    queryKey: ["summary", scopeOrg],
    queryFn: () => summaryApi.get(scopeOrg),
    enabled: !!scopeOrg,
  })
  const devices = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const outages = useQuery({
    queryKey: ["outages", scopeOrg],
    queryFn: () => outagesApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const logs = useQuery({
    queryKey: ["logs", scopeOrg, "recent"],
    queryFn: () => logsApi.list(scopeOrg, 6),
    enabled: !!scopeOrg,
  })

  if (!scopeOrg) return <NeedsOrg />

  const deviceList = devices.data?.devices ?? []
  const online = deviceList.filter((d) => d.state === "UP").length
  const outageList = outages.data?.outages ?? []
  const openOutages = outageList.filter((o) => !o.resolved_at).length
  const lowBw = summary.data?.low_bandwidth ?? []

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4 md:p-6">
      <div className="grid grid-cols-2 gap-3">
        <Card className="gap-1.5 py-4">
          <CardContent className="px-4">
            <p className="text-[10.5px] font-semibold tracking-wide text-muted-foreground uppercase">
              Devices online
            </p>
            {devices.isLoading ? <Skeleton className="h-7 w-16" /> : (
              <>
                <p className="text-2xl font-bold leading-none">{online}</p>
                <p className="mt-1 font-mono text-xs text-muted-foreground">of {deviceList.length}</p>
              </>
            )}
          </CardContent>
        </Card>
        <Card className="gap-1.5 py-4">
          <CardContent className="px-4">
            <p className="text-[10.5px] font-semibold tracking-wide text-muted-foreground uppercase">
              Open outages
            </p>
            {outages.isLoading ? <Skeleton className="h-7 w-10" /> : (
              <p className={cn("text-2xl font-bold leading-none", openOutages > 0 && "text-destructive")}>
                {openOutages}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      {lowBw.length > 0 && (
        <Card className="border-warning/30 bg-warning-soft">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <TriangleAlert className="size-4 text-warning" />
              Low bandwidth on {lowBw.length} port{lowBw.length === 1 ? "" : "s"}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
              {lowBw.slice(0, 4).map((p) => (
                <li key={p.port_id} className="flex items-center gap-1.5">
                  <ArrowDown className="size-3 text-warning" />
                  <span className="font-mono">{p.switch_name} {p.label}</span>
                </li>
              ))}
            </ul>
            <Button asChild size="sm" variant="outline" className="w-fit">
              <Link to="/topology">View in Topology</Link>
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <TriangleAlert className="size-4 text-muted-foreground" />
            Triage queue
          </h2>
          <span className="rounded-full border bg-card px-2.5 py-1 text-[11.5px] font-bold text-muted-foreground">
            {outageList.length} open
          </span>
        </div>
        {outages.isLoading && <Skeleton className="h-24 w-full" />}
        {!outages.isLoading && outageList.length === 0 && (
          <p className="py-8 text-center text-sm text-muted-foreground">Nothing needs triage right now.</p>
        )}
        {outageList.map((o) => <OutageCard key={o.id} outage={o} />)}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Clock className="size-4 text-muted-foreground" />
            Recent events
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-0 p-0">
          {logs.isLoading && <div className="p-4"><Skeleton className="h-16 w-full" /></div>}
          {logs.data?.events.length === 0 && (
            <p className="px-4 pb-4 text-sm text-muted-foreground">No events yet.</p>
          )}
          {logs.data?.events.map((ev) => (
            <div key={ev.id} className="flex items-center gap-2.5 border-t px-4 py-2.5 first:border-t-0">
              <StatusDot tone={stateTone(ev.state)} />
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-[12.5px] font-semibold">{ev.device_name || ev.type}</p>
                <p className="text-[11.5px] text-muted-foreground">{ev.state || ev.type}</p>
              </div>
              <p className="shrink-0 text-[11px] text-muted-foreground">{ago(ev.received_at)}</p>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
