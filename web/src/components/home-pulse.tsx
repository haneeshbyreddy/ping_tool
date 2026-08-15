// THE COCKPIT (Home's instrument band, 2026-08-15 — the operator asked for a
// visual Home: "critical, warning, online ONUs in one circular visual"). One
// panel, three zones: the NETWORK ring (devices by live state), the
// SUBSCRIBERS ring (the ONU fleet in OnuBar's exact tones and order), and the
// WATCH column (every other alarm count as a toned row). It replaces the grid
// of numbered tiles — a healthy fleet now renders as a quiet, present shape
// instead of a page of zeros, and trouble is a red arc at 12 o'clock rather
// than one number among eleven.
//
// Count agreement is the contract: every figure here is the same derivation
// the old tiles used (monitored = assigned to a registered probe; ONU counts
// straight off pon_summary; ok = online − crit − warn, exactly OnuBar), and
// every drill-through keeps the tiles' two destinations — the body filters the
// Network tree (statusFilter state), the corner lists /issues by kind.
//
// Honesty rules kept: a no-Rx fleet drops the crit/warn claim from the ring
// (roster states only) and says "no OLT reports dBm" — "nothing is wrong" and
// "nothing is measured" may not render alike; partial coverage prints
// "N of M measured"; offline wears the muted step, never destructive.
import { useMemo } from "react"
import { Link } from "react-router-dom"
import { ListTree } from "lucide-react"
import { StateRing, type RingSeg } from "@/chart/ring"
import { Chip, PlaneDot, StatusDot } from "@/components/status-badge"
import { Skeleton } from "@/components/ui/skeleton"
import { isStale } from "@/lib/format"
import type { Plane } from "@/lib/planes"
import type { IssueKind, OrgDevice, PonSummary } from "@/lib/types"
import { cn } from "@/lib/utils"

// The OnuBar segment fills, verbatim (.wisp-onubar__seg--ok / --off): the ring
// is that grammar at fleet resolution, and two quiet greens would read as two
// different claims about the same ONU.
const QUIET_OK = "color-mix(in oklab, var(--success) 40%, transparent)"
const QUIET_OFF = "color-mix(in oklab, var(--muted-foreground) 30%, transparent)"

export interface WatchItem {
  key: string
  label: string
  // Singular form for the verdict sentence — "1 cameras dark" is the kind of
  // seam that makes a dashboard read machine-written.
  one?: string
  value: number
  detail?: string
  tone: "destructive" | "warning"
  plane: Plane | null
  filter?: { label: string; ids: number[] }
  issueKind: IssueKind
}

function ZoneHead({ label, to }: { label: string; to?: string }) {
  return (
    <div className="flex w-full items-center justify-between gap-3">
      <span className="wisp-eyebrow">{label}</span>
      {to && (
        <Link to={to}
          className="text-2xs font-medium text-faint-foreground transition-colors hover:text-foreground">
          Issues →
        </Link>
      )}
    </div>
  )
}

function FilterChip({ tone, label, filter }: {
  tone: "destructive" | "warning" | "muted"
  label: string
  filter?: { label: string; ids: number[] }
}) {
  const chip = <Chip tone={tone}>{label}</Chip>
  if (!filter || filter.ids.length === 0) return chip
  return (
    <Link to="/topology" state={{ statusFilter: filter }}
      className="transition-[filter] hover:brightness-125">
      {chip}
    </Link>
  )
}

function IssueChip({ tone, label, kind }: {
  tone: "destructive" | "warning" | "muted"
  label: string
  kind: IssueKind
}) {
  return (
    <Link to={`/issues?kind=${kind}`} className="transition-[filter] hover:brightness-125">
      <Chip tone={tone}>{label}</Chip>
    </Link>
  )
}

// -- the NETWORK ring ---------------------------------------------------------

function DeviceZone({ monitored, loading, className }: {
  monitored: OrgDevice[]
  loading: boolean
  className?: string
}) {
  const m = useMemo(() => {
    const down: number[] = []
    const degraded: number[] = []
    const stale: number[] = []
    let up = 0
    for (const d of monitored) {
      if (isStale(d.state_updated_at)) stale.push(d.id)
      else if (d.state === "DOWN" || d.state === "UNREACHABLE") down.push(d.id)
      else if (d.state === "DEGRADED") degraded.push(d.id)
      else up++
    }
    return { down, degraded, stale, up }
  }, [monitored])

  const segs = useMemo<RingSeg[]>(() => [
    { key: "down", label: "down", value: m.down.length, color: "var(--destructive)" },
    { key: "degraded", label: "degraded", value: m.degraded.length, color: "var(--warning)" },
    { key: "up", label: "up", value: m.up, color: QUIET_OK, ink: "var(--success)" },
    { key: "stale", label: "state stale", value: m.stale.length,
      color: QUIET_OFF, ink: "var(--muted-foreground)" },
  ], [m])

  const trouble = m.down.length + m.degraded.length + m.stale.length

  return (
    <div className={cn("flex flex-col items-center gap-3 px-5 py-5", className)}>
      <ZoneHead label="Network" to="/issues?kind=device_down" />
      {loading ? (
        <Skeleton className="size-[172px] rounded-full" />
      ) : (
        <StateRing segs={segs}
          hero={m.up.toLocaleString()}
          sub={monitored.length
            ? `of ${monitored.length.toLocaleString()} devices up`
            : "no probe assigned"}
          ariaLabel={`${m.up} of ${monitored.length} devices up · ${m.down.length} down · ${m.degraded.length} degraded · ${m.stale.length} stale`} />
      )}
      <div className="flex min-h-5 flex-wrap items-center justify-center gap-1.5">
        {!loading && m.down.length > 0 && (
          <FilterChip tone="destructive" label={`${m.down.length} down`}
            filter={{ label: "Down", ids: m.down }} />
        )}
        {!loading && m.degraded.length > 0 && (
          <FilterChip tone="warning" label={`${m.degraded.length} degraded`}
            filter={{ label: "Degraded", ids: m.degraded }} />
        )}
        {!loading && m.stale.length > 0 && (
          <FilterChip tone="muted" label={`${m.stale.length} stale`}
            filter={{ label: "State stale", ids: m.stale }} />
        )}
        {!loading && trouble === 0 && monitored.length > 0 && (
          <span className="text-2xs text-faint-foreground">every device up</span>
        )}
      </div>
    </div>
  )
}

// -- the SUBSCRIBERS ring -----------------------------------------------------

function OnuZone({ pon, loading, className }: {
  pon?: PonSummary
  loading: boolean
  className?: string
}) {
  const total = pon?.onus_total ?? 0
  const online = Math.min(pon?.onus_online ?? 0, total)
  const crit = Math.max(0, pon?.onus_crit ?? 0)
  const warn = Math.max(0, pon?.onus_warn ?? 0)
  const ok = Math.max(0, online - crit - warn)
  const off = Math.max(0, pon?.onus_offline ?? 0)
  const rx = pon?.onus_rx ?? 0
  const noRx = !loading && total > 0 && rx === 0
  const partialRx = rx > 0 && rx < total

  const segs = useMemo<RingSeg[]>(() => (noRx
    // No OLT reports dBm — the ring may only claim roster states, or a fleet
    // measuring no light at all renders exactly like a healthy one.
    ? [
      { key: "online", label: "online", value: online, color: QUIET_OK, ink: "var(--success)" },
      { key: "off", label: "offline", value: off, color: QUIET_OFF, ink: "var(--muted-foreground)" },
    ]
    : [
      { key: "crit", label: "critical Rx", value: crit, color: "var(--destructive)" },
      { key: "warn", label: "weak Rx", value: warn, color: "var(--warning)" },
      { key: "ok", label: "online · no alarm", value: ok, color: QUIET_OK, ink: "var(--success)" },
      { key: "off", label: "offline", value: off, color: QUIET_OFF, ink: "var(--muted-foreground)" },
    ]), [noRx, online, off, crit, warn, ok])

  return (
    <div className={cn("flex flex-col items-center gap-3 px-5 py-5", className)}>
      <ZoneHead label="Subscribers" to="/issues?kind=onu_offline" />
      {loading ? (
        <Skeleton className="size-[172px] rounded-full" />
      ) : (
        <StateRing segs={segs}
          hero={online.toLocaleString()}
          sub={total ? `of ${total.toLocaleString()} ONUs online` : "no ONUs walked yet"}
          ariaLabel={`${online} of ${total} ONUs online · ${noRx ? "no Rx reported" : `${crit} critical · ${warn} weak`} · ${off} offline`} />
      )}
      <div className="flex min-h-5 flex-wrap items-center justify-center gap-1.5">
        {!loading && !noRx && crit > 0 && (
          <IssueChip tone="destructive" label={`${crit} critical`} kind="onu_crit" />
        )}
        {!loading && !noRx && warn > 0 && (
          <IssueChip tone="warning" label={`${warn} weak`} kind="onu_warn" />
        )}
        {!loading && off > 0 && (
          <IssueChip tone="muted" label={`${off.toLocaleString()} offline`} kind="onu_offline" />
        )}
        {!loading && !noRx && crit === 0 && warn === 0 && off === 0 && total > 0 && (
          <span className="text-2xs text-faint-foreground">all measured fine</span>
        )}
      </div>
      {!loading && (noRx || partialRx) && (
        <p className="-mt-1 text-center text-2xs text-faint-foreground">
          {noRx
            ? "no OLT reports dBm — check the Optical tab"
            : `${rx.toLocaleString()} of ${total.toLocaleString()} ONUs measured`}
        </p>
      )}
    </div>
  )
}

// -- the WATCH column ---------------------------------------------------------

function WatchRow({ it }: { it: WatchItem }) {
  return (
    <div className="group relative">
      <Link to="/topology"
        state={it.filter && it.filter.ids.length > 0 ? { statusFilter: it.filter } : undefined}
        className="flex h-9 items-center gap-2.5 rounded-lg px-2 pr-9 transition-colors hover:bg-foreground/5">
        {it.plane ? <PlaneDot plane={it.plane} /> : <StatusDot tone="muted" />}
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{it.label}</span>
        {it.detail && (
          <span className="hidden shrink-0 text-2xs text-faint-foreground @2xl:inline">
            {it.detail}
          </span>
        )}
        <span className={cn("shrink-0 font-mono text-sm font-semibold tabular-nums",
          it.tone === "destructive" ? "text-destructive" : "text-warning")}>
          {it.value}
        </span>
      </Link>
      <Link to={`/issues?kind=${it.issueKind}`} title="List these issues"
        className="absolute top-1/2 right-1 flex size-6 -translate-y-1/2 items-center justify-center rounded-md text-faint-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:bg-foreground/5 hover:text-foreground">
        <ListTree className="size-3.5" />
      </Link>
    </div>
  )
}

function WatchZone({ items, loading, className }: {
  items: WatchItem[]
  loading: boolean
  className?: string
}) {
  const loud = items.filter((i) => i.value > 0)
  const quiet = items.filter((i) => i.value === 0)
  return (
    <div className={cn("flex min-w-0 flex-col gap-2 px-5 py-5", className)}>
      <ZoneHead label="Watch" />
      {loading ? (
        <div className="flex flex-col gap-2 pt-1">
          <Skeleton className="h-7 w-full" />
          <Skeleton className="h-7 w-full" />
          <Skeleton className="h-7 w-2/3" />
        </div>
      ) : (
        <>
          {loud.length === 0 ? (
            <div className="flex flex-1 items-center justify-center py-8">
              <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                <StatusDot tone="success" />
                Nothing needs watching
              </span>
            </div>
          ) : (
            <div className="-mx-2 flex flex-col">
              {loud.map((it) => <WatchRow key={it.key} it={it} />)}
            </div>
          )}
          {quiet.length > 0 && loud.length > 0 && (
            <p className="mt-auto pt-2 text-2xs leading-relaxed text-faint-foreground">
              <span className="mr-1.5 inline-block size-1.5 rounded-full bg-success/50 align-middle" aria-hidden />
              Quiet: {quiet.map((q) => q.label.toLowerCase()).join(" · ")}
            </p>
          )}
        </>
      )}
    </div>
  )
}

// -- the band -----------------------------------------------------------------

export function PulseBand({ monitored, devicesLoading, pon, ponLoading, hasOptics, watch, watchLoading }: {
  monitored: OrgDevice[]
  devicesLoading: boolean
  pon?: PonSummary
  ponLoading: boolean
  hasOptics: boolean
  watch: WatchItem[]
  watchLoading: boolean
}) {
  return (
    <section className="wisp-panel">
      <div className={cn("grid", hasOptics
        ? "@xl:grid-cols-2 @4xl:grid-cols-[1fr_1fr_1.25fr]"
        : "@xl:grid-cols-[1fr_1.25fr]")}>
        <DeviceZone monitored={monitored} loading={devicesLoading} />
        {hasOptics && (
          <OnuZone pon={pon} loading={ponLoading}
            className="border-t border-border-subtle @xl:border-t-0 @xl:border-l" />
        )}
        <WatchZone items={watch} loading={watchLoading}
          className={cn("border-t border-border-subtle", hasOptics
            ? "@xl:col-span-2 @4xl:col-span-1 @4xl:border-t-0 @4xl:border-l"
            : "@xl:border-t-0 @xl:border-l")} />
      </div>
    </section>
  )
}
