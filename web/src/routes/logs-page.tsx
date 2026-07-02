import { useInfiniteQuery } from "@tanstack/react-query"
import { useAuth } from "@/hooks/use-auth"
import { logsApi } from "@/lib/api"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { stateTone, ago } from "@/lib/format"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

const PAGE_SIZE = 50

export function LogsPage() {
  const { scopeTenant } = useAuth()
  const { data, isLoading, fetchNextPage, hasNextPage, isFetchingNextPage } = useInfiniteQuery({
    queryKey: ["logs", scopeTenant, "full"],
    queryFn: ({ pageParam }) => logsApi.list(scopeTenant, PAGE_SIZE, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.events.length < PAGE_SIZE ? undefined : lastPage.events.at(-1)?.id,
    enabled: !!scopeTenant,
  })

  if (!scopeTenant) return <NeedsOrg />

  const events = data?.pages.flatMap((p) => p.events) ?? []

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-3 p-4 md:p-6">
      <h1 className="text-xl font-bold">Logs</h1>
      <Card className="gap-0 overflow-hidden py-0">
        <CardContent className="flex flex-col gap-0 p-0">
          {isLoading && <div className="p-4"><Skeleton className="h-32 w-full" /></div>}
          {!isLoading && events.length === 0 && (
            <p className="p-6 text-center text-sm text-muted-foreground">No events yet.</p>
          )}
          {events.map((ev) => (
            <div key={ev.id} className="flex items-center gap-2.5 border-t px-4 py-2.5 first:border-t-0">
              <StatusDot tone={stateTone(ev.state)} />
              <span className="w-28 shrink-0 truncate font-mono text-[11px] text-muted-foreground">{ev.node_id}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-[12.5px] font-semibold">{ev.device_name || ev.type}</p>
                <p className="text-[11.5px] text-muted-foreground">{ev.type}{ev.state ? ` · ${ev.state}` : ""}</p>
              </div>
              <p className="shrink-0 text-[11px] text-muted-foreground">{ago(ev.received_at)}</p>
            </div>
          ))}
        </CardContent>
      </Card>
      {hasNextPage && (
        <Button variant="outline" size="sm" className="self-center" disabled={isFetchingNextPage}
          onClick={() => fetchNextPage()}>
          {isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
    </div>
  )
}
