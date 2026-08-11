import { useMemo, useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { Search } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { logsApi } from "@/lib/api"
import type { LogEvent } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { Chip, StatusDot } from "@/components/status-badge"
import { TYPE_LABEL, describeEvent, eventTone } from "@/lib/events"
import { ago, toUtcDate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Segmented } from "@/components/ui/segmented"
import { Skeleton } from "@/components/ui/skeleton"

const PAGE_SIZE = 50

const FILTERS = [
  { value: null, label: "All" },
  { value: "OUTAGE_OPENED", label: "Outages" },
  { value: "OUTAGE_RESOLVED", label: "Recovered" },
  { value: "OUTAGE_ACKNOWLEDGED", label: "Acked" },
  { value: "OUTAGE_POSTMORTEM", label: "Post-mortems" },
] as const

const COLS = "grid grid-cols-[4.5rem_0.5rem_minmax(0,9rem)_6.5rem_minmax(0,1fr)_3.5rem] items-center gap-3.5 px-4"

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
    <div className="wisp-page flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Logs</h1>
        {events.length > 0 && (
          <span className="text-xs text-faint-foreground">
            {filtered.length === events.length ? events.length : `${filtered.length} of ${events.length}`}
            {hasNextPage ? "+" : ""} {events.length === 1 && !hasNextPage ? "event" : "events"}
          </span>
        )}
        <div className="relative ml-auto w-full sm:w-64">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="device, region, IP…" className="h-8 bg-muted pl-8 text-xs" />
        </div>
      </div>

      <Segmented value={typeFilter} options={FILTERS} onChange={setTypeFilter} />

      <div className="wisp-panel">
        <div className={cn(COLS, "wisp-thead h-9")}>
          <span>Time</span>
          <span />
          <span>Device</span>
          <span>Status</span>
          <span>Event</span>
          <span className="text-right">Age</span>
        </div>
        {isLoading && <div className="p-4"><Skeleton className="h-32 w-full" /></div>}
        {!isLoading && filtered.length === 0 && (
          <p className="p-8 text-center text-xs text-faint-foreground">
            {events.length === 0 ? "No events yet." : "Nothing matches the current filter."}
          </p>
        )}
        {groups.map((group) => (
          <div key={`${group.day}:${group.events[0].id}`}>
            <p className="wisp-eyebrow sticky top-0 z-10 border-y border-border-subtle bg-sidebar/95 px-4 py-2 backdrop-blur">
              {group.day}
            </p>
            {group.events.map((ev) => (
              <div key={ev.id} className={cn(COLS, "h-10 transition-colors hover:bg-foreground/5")}>
                <span className="font-mono text-xs whitespace-nowrap text-faint-foreground">
                  {timeLabel(ev.occurred_at ?? ev.received_at)}
                </span>
                <StatusDot tone={eventTone(ev)} />
                <span className="truncate font-mono text-xs font-medium">
                  {ev.device_name || "—"}
                </span>
                <span className="min-w-0">
                  <Chip tone={eventTone(ev)}>{TYPE_LABEL[ev.type] ?? ev.type}</Chip>
                </span>
                <span className="min-w-0 truncate text-xs text-muted-foreground"
                  title={describeEvent(ev)}>
                  {describeEvent(ev)}
                  {ev.device_region && (
                    <span className="text-ghost-foreground"> · {ev.device_region}</span>
                  )}
                </span>
                <span className="text-right font-mono text-xs text-faint-foreground">
                  {ago(ev.occurred_at ?? ev.received_at)}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
      {hasNextPage && (
        <Button variant="outline" size="sm" className="self-center" disabled={isFetchingNextPage}
          onClick={() => fetchNextPage()}>
          {isFetchingNextPage ? "Loading…" : "Older events"}
        </Button>
      )}
    </div>
  )
}
