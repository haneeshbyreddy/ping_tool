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
import { Chip, PlaneChip, PlaneDot, StatusDot, type Tone } from "@/components/status-badge"
import { ago, fmtDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import { KIND_PLANE } from "@/lib/planes"
import { OnuBar } from "@/components/onu-bar"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"

const TONE: Record<IssueSeverity, Tone> = {
  critical: "destructive",
  warning: "warning",
  info: "muted",
}

const COLS = "grid grid-cols-[0.5rem_7.5rem_minmax(0,1.15fr)_minmax(0,1.35fr)_5rem] items-center gap-3.5 px-4"

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

  const chips = (query.data?.kinds ?? []).filter((k) => (counts[k] ?? 0) > 0)

  const setKind = (kind: IssueKind | null) => {
    const next = new URLSearchParams(params)
    if (kind) next.set("kind", kind)
    else next.delete("kind")
    setParams(next, { replace: true })
  }

  const exportAs = async (format: "pdf" | "xlsx") => {
    setDownloading(true)
    try {
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
          {chips.map((k) => {
            const plane = KIND_PLANE[k] ?? null
            return (
              <button key={k} type="button" aria-pressed={picked.has(k)}
                onClick={() => setKind(picked.has(k) && picked.size === 1 ? null : k)}
                className={cn(
                  "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors",
                  picked.has(k)
                    ? "border-border-strong bg-popover text-foreground"
                    : "border-border bg-card text-muted-foreground hover:text-foreground")}>
                {plane && <PlaneDot plane={plane} />}
                {labels[k] ?? k}
                <span className="font-mono text-2xs text-faint-foreground">{counts[k]}</span>
              </button>
            )
          })}
        </div>
      )}

      <div className="wisp-panel">
        <div className={cn(COLS, "wisp-thead h-9")}>
          <span />
          <span>Issue</span>
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
        {groupIssues(rows).map((g) => (
          <div key={g.key}>
            <GroupHead g={g} />
            {g.issues.map((i, idx) => (
              <Row key={`${i.kind}:${i.subject}:${idx}`} issue={i}
                hideKind={!!g.oneKind || (idx > 0 && g.issues[idx - 1].kind === i.kind)} />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

type IssueGroup = {
  key: string
  device: string
  deviceId: number | null
  region: string | null
  issues: Issue[]
  critical: number
  warning: number
  oneKind: string | null
}

function groupIssues(rows: Issue[]): IssueGroup[] {
  const out: IssueGroup[] = []
  const byKey = new Map<string, IssueGroup>()
  for (const i of rows) {
    const key = `${i.device_id ?? "probe"}·${i.device_name}`
    let g = byKey.get(key)
    if (!g) {
      g = { key, device: i.device_name, deviceId: i.device_id, region: i.region,
            issues: [], critical: 0, warning: 0, oneKind: i.kind }
      byKey.set(key, g)
      out.push(g)
    }
    g.issues.push(i)
    if (i.severity === "critical") g.critical++
    if (i.severity === "warning") g.warning++
    if (g.oneKind !== i.kind) g.oneKind = null
    if (g.region !== i.region) g.region = null
  }
  return out
}

function GroupHead({ g }: { g: IssueGroup }) {
  return (
    <div className="flex items-center gap-3 border-t border-border-subtle bg-muted px-4 py-1.5">
      {g.deviceId != null
        ? <Link to="/topology" state={{ deviceId: g.deviceId }}
            className="shrink-0 font-mono text-xs font-semibold text-foreground hover:underline">
            {g.device}
          </Link>
        : <span className="shrink-0 font-mono text-xs font-semibold text-foreground">{g.device}</span>}
      {g.region && <span className="shrink-0 text-2xs text-faint-foreground">{g.region}</span>}
      {g.oneKind && KIND_PLANE[g.oneKind] && (
        <PlaneChip plane={KIND_PLANE[g.oneKind]!} label={g.issues[0].kind_label} />
      )}
      <OnuBar total={g.issues.length} crit={g.critical} warn={g.warning}
        online={g.issues.length} className="ml-auto" />
      <span className="flex shrink-0 items-baseline gap-2 font-mono text-2xs">
        {g.critical > 0 && <span className="font-semibold text-destructive">{g.critical} critical</span>}
        {g.warning > 0 && <span className="font-semibold text-warning">{g.warning} warning</span>}
        <span className="text-faint-foreground">{g.issues.length} total</span>
      </span>
    </div>
  )
}

function Row({ issue, hideKind }: { issue: Issue; hideKind?: boolean }) {
  const tone = TONE[issue.severity]
  return (
    <div className={cn(COLS, "h-10 wisp-row transition-colors hover:bg-foreground/5")}>
      <StatusDot tone={tone} />
      <span className="min-w-0">
        {hideKind ? null : KIND_PLANE[issue.kind]
          ? <PlaneChip plane={KIND_PLANE[issue.kind]!} label={issue.kind_label} />
          : <Chip tone="muted">{issue.kind_label}</Chip>}
      </span>
      <span className="truncate font-mono text-xs text-muted-foreground" title={issue.subject}>
        {issue.subject}
      </span>
      <span className={cn("min-w-0 truncate text-xs",
        tone === "destructive" ? "text-destructive"
          : tone === "warning" ? "text-warning" : "text-muted-foreground")}
        title={issue.detail}>
        {issue.detail}
      </span>
      <span className="text-right font-mono text-xs text-faint-foreground"
        title={issue.since ? fmtDateTime(issue.since) : undefined}>
        {issue.since ? ago(issue.since) : "—"}
      </span>
    </div>
  )
}
