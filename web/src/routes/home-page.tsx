import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { Clock, TriangleAlert, ArrowDown } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { summaryApi, inventoryApi, outagesApi, logsApi } from "@/lib/api"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { ago, stateTone } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

export function HomePage() {
  const { scopeTenant } = useAuth()

  const summary = useQuery({
    queryKey: ["summary", scopeTenant],
    queryFn: () => summaryApi.get(scopeTenant),
    enabled: !!scopeTenant,
  })
  const devices = useQuery({
    queryKey: ["inventory", scopeTenant],
    queryFn: () => inventoryApi.list(scopeTenant),
    enabled: !!scopeTenant,
  })
  const outages = useQuery({
    queryKey: ["outages", scopeTenant],
    queryFn: () => outagesApi.list(scopeTenant),
    enabled: !!scopeTenant,
  })
  const logs = useQuery({
    queryKey: ["logs", scopeTenant, "recent"],
    queryFn: () => logsApi.list(scopeTenant, 6),
    enabled: !!scopeTenant,
  })

  if (!scopeTenant) return <NeedsOrg />

  const deviceList = devices.data?.devices ?? []
  const online = deviceList.filter((d) => d.state === "UP").length
  const openOutages = (outages.data?.outages ?? []).filter((o) => !o.resolved_at).length
  const lowBw = summary.data?.low_bandwidth ?? []
  const uplinkDown = summary.data?.uplink_down ?? false

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4 md:p-6">
      {uplinkDown && (
        <Link
          to="/outages"
          className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive-soft px-4 py-3"
        >
          <span className="relative flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-destructive opacity-75" />
            <span className="relative inline-flex size-2 rounded-full bg-destructive" />
          </span>
          <span className="flex-1 text-sm font-semibold">Uplink down</span>
        </Link>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
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
        <Card className="col-span-2 gap-1.5 py-4 md:col-span-2">
          <CardContent className="flex items-center justify-between px-4">
            <p className="text-xs font-semibold text-muted-foreground">Uplink status</p>
            <span className={cn(
              "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold capitalize",
              uplinkDown ? "bg-destructive-soft text-destructive" : "bg-success-soft text-success",
            )}>
              <StatusDot tone={uplinkDown ? "destructive" : "success"} />
              {uplinkDown ? "down" : "up"}
            </span>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
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

        <Card className={lowBw.length === 0 ? "md:col-span-2" : ""}>
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
    </div>
  )
}
