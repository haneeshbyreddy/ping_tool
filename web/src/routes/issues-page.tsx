import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router-dom"
import { toast } from "sonner"
import { ChevronDown, Download, FileText, Search, Table2 } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useNow } from "@/hooks/use-now"
import { issuesApi, ApiError } from "@/lib/api"
import type { Issue, IssueKind, IssueSeverity } from "@/lib/types"
import { NeedsOrg } from "@/components/needs-org"
import { Chip, StatusDot, type Tone } from "@/components/status-badge"
import { ago, fmtDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"

// The tiles on Home drill into the Network TREE, filtered to the devices behind
// a number — which answers "which boxes", not "what is wrong". This page is the
// other half: one row per problem, so the tile's count and the list's length are
// the same number, and the whole thing exports as a PDF for a shift handover.

const TONE: Record<IssueSeverity, Tone> = {
  critical: "destructive",
  warning: "warning",
  info: "muted",
}

// ONE grid template for the header strip and every row — declared once so an
// unrelated edit can't drift them apart.
//
// Item and Detail are both FRACTIONAL, not one capped track and one greedy `1fr`.
// Item carries the thing being looked up — a 17-character MAC plus its PON, in
// mono — so a fixed 10rem cap truncated the identifier while Detail sat on
// hundreds of unused pixels. Item leads slightly (mono runs wider per character)
// and Detail takes the rest; at any width the two shrink together instead of one
// starving.
const COLS = "grid grid-cols-[0.5rem_7.5rem_minmax(0,9rem)_minmax(0,1.15fr)_minmax(0,1.35fr)_5rem] items-center gap-3.5 px-4"

/** `?kind=a,b` — the tiles link here with their own kind, and the chips keep the
 *  URL in step so a filtered list is shareable and survives a reload. */
function parseKinds(raw: string | null): IssueKind[] {
  return (raw ?? "").split(",").map((k) => k.trim()).filter(Boolean) as IssueKind[]
}

export function IssuesPage() {
  const { scopeOrg } = useAuth()
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState("")
  const [downloading, setDownloading] = useState(false)
  useNow()

  const kinds = useMemo(() => parseKinds(params.get("kind")), [params])

  // Fetched UNFILTERED and narrowed in the browser: every chip's count comes
  // from the same payload, so switching filters is instant and can never show a
  // count that disagrees with the rows underneath it.
  const query = useQuery({
    queryKey: ["issues", scopeOrg],
    queryFn: () => issuesApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 60_000,
  })

  if (!scopeOrg) return <NeedsOrg />

  const all = query.data?.issues ?? []
  const counts = query.data?.counts ?? {}
  const labels = query.data?.kind_labels ?? {}
  const picked = new Set(kinds)
  const needle = search.trim().toLowerCase()
  const rows = all.filter((i) =>
    (picked.size === 0 || picked.has(i.kind)) &&
    (!needle ||
      i.device_name.toLowerCase().includes(needle) ||
      i.subject.toLowerCase().includes(needle) ||
      i.detail.toLowerCase().includes(needle) ||
      (i.region ?? "").toLowerCase().includes(needle)))

  // Only kinds that actually have rows get a chip: an org with no fiber should
  // not be offered six empty optical filters.
  const chips = (query.data?.kinds ?? []).filter((k) => (counts[k] ?? 0) > 0)

  // Picking a chip shows ONLY that kind — the chips are a single choice, like the
  // Logs page's type filter, not an accumulating union. Clicking the active chip
  // (or "All") clears back to everything.
  //
  // A FRESH URLSearchParams every time: mutating the instance `useSearchParams`
  // handed us edits the router's own memoized object in place, so the value the
  // render reads can move without the reference the memo is keyed on changing —
  // which presents as a chip that highlights but never filters.
  const setKind = (kind: IssueKind | null) => {
    const next = new URLSearchParams(params)
    if (kind) next.set("kind", kind)
    else next.delete("kind")
    setParams(next, { replace: true })
  }

  const exportAs = async (format: "pdf" | "xlsx") => {
    setDownloading(true)
    try {
      // Server-rendered, and filtered by the chips you can see — what gets filed
      // should be the list you were looking at.
      const { blob, filename } = await issuesApi.download(format, scopeOrg, kinds)
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message
        : `Could not build the ${format === "pdf" ? "PDF" : "spreadsheet"}`)
    } finally {
      setDownloading(false)
    }
  }

  // Counted over the VISIBLE rows, not the whole list: with a filter on, "23 of
  // 92 open · 52 warning" describes two different sets in one sentence.
  const bySeverity = (s: IssueSeverity) => rows.filter((i) => i.severity === s).length

  return (
    <div className="wisp-page flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Issues</h1>
        {!query.isLoading && (
          <span className="text-xs text-faint-foreground">
            {rows.length === all.length ? all.length : `${rows.length} of ${all.length}`}
            {" open · "}
            {bySeverity("critical")} critical · {bySeverity("warning")} warning
          </span>
        )}
        <div className="relative ml-auto w-full sm:w-64">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="device, item, detail…" className="h-8 bg-muted pl-8 text-xs" />
        </div>
        {/* One button, two formats: a PDF to file or hand over, a spreadsheet to
            sort and filter. Both are server-rendered from the same rows and
            honour the chips above, so neither can disagree with the screen. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="gap-1.5"
              disabled={downloading || query.isLoading}>
              <Download className="size-3.5" />
              {downloading ? "Building…" : "Export"}
              <ChevronDown className="size-3.5 text-faint-foreground" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => exportAs("pdf")} className="gap-2">
              <FileText className="size-3.5" /> PDF
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => exportAs("xlsx")} className="gap-2">
              <Table2 className="size-3.5" /> Excel (.xlsx)
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" onClick={() => setKind(null)}
            aria-pressed={picked.size === 0}
            className={cn(
              "h-7 rounded-md border px-2.5 text-xs font-medium transition-colors",
              picked.size === 0
                ? "border-border-strong bg-popover text-foreground"
                : "border-border bg-card text-muted-foreground hover:text-foreground")}>
            All {all.length}
          </button>
          {chips.map((k) => (
            <button key={k} type="button" aria-pressed={picked.has(k)}
              onClick={() => setKind(picked.has(k) && picked.size === 1 ? null : k)}
              className={cn(
                "h-7 rounded-md border px-2.5 text-xs font-medium transition-colors",
                picked.has(k)
                  ? "border-border-strong bg-popover text-foreground"
                  : "border-border bg-card text-muted-foreground hover:text-foreground")}>
              {labels[k] ?? k}
              <span className="ml-1.5 font-mono text-2xs text-faint-foreground">{counts[k]}</span>
            </button>
          ))}
        </div>
      )}

      <div className="wisp-panel">
        <div className={cn(COLS, "wisp-thead h-9")}>
          <span />
          <span>Issue</span>
          <span>Device</span>
          <span>Item</span>
          <span>Detail</span>
          <span className="text-right">Since</span>
        </div>
        {query.isLoading && <div className="p-4"><Skeleton className="h-40 w-full" /></div>}
        {query.isError && (
          <p className="p-8 text-center text-xs text-destructive">
            {query.error instanceof ApiError ? query.error.message : "Could not load issues"}
          </p>
        )}
        {query.isSuccess && rows.length === 0 && (
          <p className="p-8 text-center text-xs text-faint-foreground">
            {all.length === 0
              ? "Nothing is wrong right now."
              : "Nothing matches the current filter."}
          </p>
        )}
        {rows.map((i, idx) => <Row key={`${i.kind}:${i.device_id ?? "n"}:${i.subject}:${idx}`} issue={i} />)}
      </div>
    </div>
  )
}

function Row({ issue }: { issue: Issue }) {
  const tone = TONE[issue.severity]
  // A probe has no row in the device tree, so only a device-backed issue links
  // out — a link that lands nowhere is worse than plain text.
  const label = (
    <span className="truncate font-mono text-xs font-medium">{issue.device_name}</span>
  )
  return (
    <div className={cn(COLS, "h-10 wisp-row transition-colors hover:bg-foreground/5")}>
      <StatusDot tone={tone} />
      <span className="min-w-0">
        <Chip tone={tone}>{issue.kind_label}</Chip>
      </span>
      {issue.device_id != null
        ? <Link to="/topology" state={{ deviceId: issue.device_id }}
            className="min-w-0 truncate hover:underline">{label}</Link>
        : <span className="min-w-0">{label}</span>}
      <span className="truncate font-mono text-xs text-muted-foreground" title={issue.subject}>
        {issue.subject}
      </span>
      <span className="min-w-0 truncate text-xs text-muted-foreground" title={issue.detail}>
        {issue.detail}
        {issue.region && <span className="text-ghost-foreground"> · {issue.region}</span>}
      </span>
      <span className="text-right font-mono text-xs text-faint-foreground"
        title={issue.since ? fmtDateTime(issue.since) : undefined}>
        {issue.since ? ago(issue.since) : "—"}
      </span>
    </div>
  )
}
