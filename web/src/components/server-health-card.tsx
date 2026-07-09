import { useQuery } from "@tanstack/react-query"
import { Server } from "lucide-react"
import { systemApi } from "@/lib/api"
import { ago, fmtBytes, fmtDur } from "@/lib/format"
import { Meter } from "@/components/meter"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

/** Host health of the central server itself — superadmin-only (the endpoint 403s otherwise). */
export function ServerHealthCard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["system"],
    queryFn: () => systemApi.get(),
    refetchInterval: 10_000,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Server className="size-4 text-muted-foreground" /> Central server
          {data && (
            <span className="ml-auto flex items-center gap-2 font-normal text-muted-foreground">
              <span className="font-mono text-xs">{data.hostname}</span>
              {data.uptime_s != null && (
                <span className="text-[0.75rem]">up {fmtDur(data.uptime_s)}</span>
              )}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        {isLoading && <Skeleton className="h-20 w-full" />}
        {isError && (
          <p className="text-xs text-destructive">Couldn't read server stats.</p>
        )}
        {data && (
          <>
            <Meter label="CPU" pct={data.cpu.percent}
              detail={data.cpu.load
                ? `load ${data.cpu.load.map((l) => l.toFixed(2)).join(" ")}`
                : `${data.cpu.cores ?? "—"} cores`} />
            <Meter label="Memory" pct={data.memory?.percent ?? null}
              detail={data.memory
                ? `${fmtBytes(data.memory.used_bytes)} / ${fmtBytes(data.memory.total_bytes)}`
                : "—"} />
            <p className="mt-1 text-[0.75rem] text-muted-foreground">
              {data.cpu.cores != null && <>{data.cpu.cores} core{data.cpu.cores === 1 ? "" : "s"} · </>}
              service {fmtBytes(data.process.rss_bytes)} RSS · database {fmtBytes(data.process.db_bytes)}
            </p>
            <p className="text-[0.75rem]">
              {data.release_sync == null ? (
                <span className="text-muted-foreground">Release mirror: never synced</span>
              ) : data.release_sync.ok ? (
                <span className="text-muted-foreground">
                  Release mirror: v{data.release_sync.detail} · synced {ago(data.release_sync.at)}
                </span>
              ) : (
                <span className="text-destructive">
                  Release mirror failing (last try {ago(data.release_sync.at)}) — {data.release_sync.detail}
                </span>
              )}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
