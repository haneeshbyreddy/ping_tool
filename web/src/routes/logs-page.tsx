import { useMemo, useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { Search } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { logsApi } from "@/lib/api"
import type { LogEvent } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { StatusDot } from "@/components/status-badge"
import { TYPE_LABEL, describeEvent, eventTone } from "@/lib/events"
import { ago, toUtcDate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"

const PAGE_SIZE = 50

const FILTERS: Array<{ key: string | null; label: string }> = [
  { key: null, label: "All" },
  { key: "OUTAGE_OPENED", label: "Outages" },
  { key: "OUTAGE_RESOLVED", label: "Recovered" },
  { key: "OUTAGE_ACKNOWLEDGED", label: "Acked" },
  { key: "OUTAGE_POSTMORTEM", label: "Post-mortems" },
]

function dayLabel(ts: string, now: Date): string {
  const d = toUtcDate(ts)
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(d, now)) return "Today"
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(d, yesterday)) return "Yesterday"
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
}

function timeLabel(ts: string): string {
  return toUtcDate(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
}

export function LogsPage() {
  const { scopeOrg } = useAuth()
  const [typeFilter, setTypeFilter] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  useNow()
  const { data, isLoading, fetchNextPage, hasNextPage, isFetchingNextPage } = useInfiniteQuery({
    queryKey: ["logs", scopeOrg, "full"],
    queryFn: ({ pageParam }) => logsApi.list(scopeOrg, PAGE_SIZE, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.events.length < PAGE_SIZE ? undefined : lastPage.events.at(-1)?.id,
    enabled: !!scopeOrg,
  })

  const events = useMemo(() => data?.pages.flatMap((p) => p.events) ?? [], [data])
  const needle = search.trim().toLowerCase()

  const filtered = events
    .filter((ev) =>
      (!typeFilter || ev.type === typeFilter) &&
      (!needle ||
        (ev.device_name ?? "").toLowerCase().includes(needle) ||
        (ev.device_region ?? "").toLowerCase().includes(needle) ||
        (ev.device_ip ?? "").includes(needle)))
    .sort((a, b) => toUtcDate(b.occurred_at ?? b.received_at).getTime()
      - toUtcDate(a.occurred_at ?? a.received_at).getTime())

  if (!scopeOrg) return <NeedsOrg />

  const now = new Date()
  const groups: Array<{ day: string; events: LogEvent[] }> = []
  for (const ev of filtered) {
    const day = dayLabel(ev.occurred_at ?? ev.received_at, now)
    if (groups.at(-1)?.day === day) groups.at(-1)!.events.push(ev)
    else groups.push({ day, events: [ev] })
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-4 md:p-6 xl:p-8">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold tracking-tight">Logs</h1>
        {events.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {filtered.length === events.length ? events.length : `${filtered.length} of ${events.length}`}
            {hasNextPage ? "+" : ""} events
          </span>
        )}
        <div className="relative ml-auto w-full sm:w-56">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="device, region, IP…" className="h-8 pl-8 text-xs" />
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button key={f.label} onClick={() => setTypeFilter(f.key)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-[0.75rem] font-medium transition-colors",
              typeFilter === f.key
                ? "border-primary/40 bg-primary/10 text-foreground"
                : "text-muted-foreground hover:bg-accent/50",
            )}>
            {f.label}
          </button>
        ))}
      </div>

      <Card className="gap-0 overflow-hidden py-0">
        <CardContent className="flex flex-col gap-0 p-0">
          {isLoading && <div className="p-4"><Skeleton className="h-32 w-full" /></div>}
          {!isLoading && filtered.length === 0 && (
            <p className="p-6 text-center text-sm text-muted-foreground">
              {events.length === 0 ? "No events yet." : "Nothing matches the current filter."}
            </p>
          )}
          {/* keyed by first row id, NOT the day label — the label can legitimately
              repeat across groups and duplicate keys make React leave stale rows */}
          {groups.map((group) => (
            <div key={`${group.day}:${group.events[0].id}`}>
              <p className="sticky top-0 border-y bg-muted/80 px-5 py-1.5 text-[0.75rem] font-semibold tracking-wide text-muted-foreground uppercase backdrop-blur first:border-t-0">
                {group.day}
              </p>
              {group.events.map((ev) => (
                <div key={ev.id} className="flex items-center gap-3 border-t px-5 py-2.5 first:border-t-0 hover:bg-accent/30">
                  <span className="w-16 shrink-0 font-mono text-xs whitespace-nowrap text-muted-foreground">
                    {timeLabel(ev.occurred_at ?? ev.received_at)}
                  </span>
                  <StatusDot tone={eventTone(ev)} />
                  <span className="w-36 shrink-0 truncate font-mono text-xs font-medium md:w-44">
                    {ev.device_name || "—"}
                  </span>
                  <span className="hidden w-24 shrink-0 rounded-full border bg-card px-1.5 py-0.5 text-center text-[0.6875rem] font-semibold text-muted-foreground sm:inline-block">
                    {TYPE_LABEL[ev.type] ?? ev.type}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground"
                    title={describeEvent(ev)}>
                    {describeEvent(ev)}
                    {ev.device_region && (
                      <span className="text-muted-foreground/60"> · {ev.device_region}</span>
                    )}
                  </span>
                  <span className="shrink-0 text-right text-xs text-muted-foreground">
                    {ago(ev.occurred_at ?? ev.received_at)}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </CardContent>
      </Card>
      {hasNextPage && (
        <Button variant="outline" size="sm" className="self-center" disabled={isFetchingNextPage}
          onClick={() => fetchNextPage()}>
          {isFetchingNextPage ? "Loading…" : "Older events"}
        </Button>
      )}
    </div>
  )
}
