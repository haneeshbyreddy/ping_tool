// The governor's ledger (Wave 1, chart B). Question: "what did the governor
// eat — and what volume would re-enabling a kind add?" Action: the one-line
// _ACTIVE_KINDS decision, made with its would-be volume in view.
//
// Owner-only (the endpoint refuses workers). Weekly stacked columns of
// alert_log rows split by OUTCOME: sent (fleet plane), suppressed (muted —
// the governor ate it; its legend chip is struck, the suppressed-alert
// channel), failed (destructive — a failed page IS a failure claim). Kind
// filter is single-select chips, the Logs/issues pattern; kind '' is the
// pre-kind era, labeled "(untagged)", never dropped. Zero weeks draw
// zero-height columns — counts of events are the one place zero is a zero.
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import { LegendChip, TimeChart } from "@/chart/frame"
import type { TooltipModel } from "@/chart/frame"
import { ColumnMark } from "@/chart/marks"
import { WEEK_MS } from "@/chart/scale"
import { useAuth } from "@/hooks/use-auth"
import { historyApi } from "@/lib/api"
import { toUtcDate } from "@/lib/format"
import { cn } from "@/lib/utils"

const FLEET = "var(--chart-5)"
const SUPPRESSED = "var(--muted-foreground)"
const FAILED = "var(--destructive)"
const MONDAY_MS = 4 * 86_400_000
const DAYS = 90

type Outcome = "sent" | "failed" | "suppressed"

function outcomeOf(status: string): Outcome {
  // 'digest'/'skipped'/'suppressed'/anything else: the page did not reach a
  // handset on its own — the governor's side of the ledger.
  if (status === "sent") return "sent"
  if (status === "failed") return "failed"
  return "suppressed"
}

function weekFloor(ms: number): number {
  return Math.floor((ms - MONDAY_MS) / WEEK_MS) * WEEK_MS + MONDAY_MS
}

export function PagingLedger() {
  const { scopeOrg, canWrite } = useAuth()
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<string | null>(null)
  const q = useQuery({
    queryKey: ["paging-history", scopeOrg],
    queryFn: () => historyApi.paging(scopeOrg, DAYS),
    enabled: open && !!scopeOrg && canWrite,
    staleTime: 300_000,
  })

  const model = useMemo(() => {
    if (!q.data) return null
    const since = toUtcDate(q.data.since).getTime()
    const until = toUtcDate(q.data.until).getTime()
    const kinds = new Map<string, number>()
    for (const r of q.data.rows) {
      kinds.set(r.kind, (kinds.get(r.kind) ?? 0) + r.n)
    }
    const weeks = new Map<number, Record<Outcome, number>>()
    for (const r of q.data.rows) {
      if (kind != null && r.kind !== kind) continue
      const t = weekFloor(toUtcDate(r.day + "T00:00:00+00:00").getTime())
      const w = weeks.get(t) ?? { sent: 0, failed: 0, suppressed: 0 }
      w[outcomeOf(r.status)] += r.n
      weeks.set(t, w)
    }
    const all: number[] = []
    for (let t = weekFloor(since); t < until; t += WEEK_MS) all.push(t)
    const columns = all.map((t) => {
      const w = weeks.get(t) ?? { sent: 0, failed: 0, suppressed: 0 }
      return { t, span: WEEK_MS, segs: [
        { v: w.sent, color: FLEET, opacity: 0.8 },
        { v: w.suppressed, color: SUPPRESSED, opacity: 0.35 },
        { v: w.failed, color: FAILED, opacity: 0.85 },
      ] }
    })
    const maxWeek = Math.max(1, ...all.map((t) => {
      const w = weeks.get(t)
      return w ? w.sent + w.failed + w.suppressed : 0
    }))
    return { since, until, weeks, columns, maxWeek,
             kinds: [...kinds.entries()].sort((a, b) => b[1] - a[1]) }
  }, [q.data, kind])

  if (!scopeOrg || !canWrite) return null

  const tooltip = (tMs: number): TooltipModel | null => {
    if (!model) return null
    const t = weekFloor(tMs)
    const w = model.weeks.get(t) ?? { sent: 0, failed: 0, suppressed: 0 }
    return {
      at: t + WEEK_MS / 2,
      title: "Week of " + new Date(t).toLocaleDateString(undefined,
        { day: "numeric", month: "short" }),
      rows: [
        { label: "sent", value: String(w.sent), color: FLEET },
        { label: "suppressed", value: String(w.suppressed), color: SUPPRESSED },
        { label: "failed", value: String(w.failed), color: FAILED },
      ],
    }
  }

  return (
    <div className="wisp-panel">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-foreground/5"
        title="What paged, what the governor suppressed, and what failed to send — the volume behind re-enabling an alert kind">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        <span className="text-sm font-semibold text-foreground">Paging volume</span>
        <span className="text-xs text-faint-foreground">last {DAYS} days</span>
      </button>
      {open && (
        <div className="flex flex-col gap-3 px-4 pt-1 pb-4">
          {model && model.kinds.length > 1 && (
            <div className="flex flex-wrap gap-1.5">
              <KindChip label="all kinds" active={kind == null}
                onClick={() => setKind(null)} />
              {model.kinds.map(([k, n]) => (
                <KindChip key={k || "(untagged)"}
                  label={`${k || "(untagged)"} · ${n}`}
                  active={kind === k} onClick={() => setKind(k)} />
              ))}
            </div>
          )}
          {q.isLoading ? (
            <p className="text-2xs text-muted-foreground">loading…</p>
          ) : q.error ? (
            <p className="text-2xs text-destructive">Couldn't load the ledger.</p>
          ) : model && (
            <TimeChart domain={[model.since, model.until]} yMax={model.maxWeek}
              height={170} tooltip={tooltip}
              empty={model.columns.every((c) => c.segs.every((s) => s.v === 0))
                ? "No pages in this window." : null}
              legend={<>
                <LegendChip color={FLEET} label="sent" />
                <LegendChip color={SUPPRESSED} label="suppressed" struck />
                <LegendChip color={FAILED} label="failed" />
              </>}>
              <ColumnMark buckets={model.columns} />
            </TimeChart>
          )}
        </div>
      )}
    </div>
  )
}

function KindChip({ label, active, onClick }: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button type="button" onClick={onClick}
      className={cn("rounded-md border px-2 py-0.5 font-mono text-2xs transition-colors",
        active ? "border-primary/40 bg-selected text-foreground"
          : "border-border text-muted-foreground hover:text-foreground")}>
      {label}
    </button>
  )
}
