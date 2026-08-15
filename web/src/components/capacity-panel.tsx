// BUSY HOUR — the "when do I buy backhaul" answer (Wave 2, chart E).
// Question: "which port is closest to its ceiling, and at what time of day?"
// Action: buy capacity, or re-balance a region before the complaints start.
//
// THIS IS A CAPACITY LIST, NOT AN ALARM SURFACE. A port pinned near its
// ceiling is a purchase decision, and a purchase decision is not a fault — so
// the ranking is neutral text plus a meter in the TRAFFIC plane, and the only
// tones here are the two claims ports.py already made (`alarm` = the port is
// down, `bw_high_alarm` = it is over its ceiling RIGHT NOW). Painting the list
// red would fabricate alarms out of arithmetic.
//
// Owner-only, twice: the endpoint refuses a worker, and the panel never
// mounts for one. An org-wide port list leaks past assignment scope.
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { Heatmap, HeatmapLegend } from "@/chart/heatmap"
import type { HeatmapRow } from "@/chart/heatmap"
import { useAuth } from "@/hooks/use-auth"
import { busyArrow, capacityApi, fmtRate, hourLabel, hourSlots, portRecords } from "@/lib/capacity-api"
import type { CapacityRow, HeatCell, PortHistoryReply } from "@/lib/capacity-api"
import { isDownState, toUtcDate } from "@/lib/format"
import type { OrgDevice, SwitchPort } from "@/lib/types"
import { Chip } from "@/components/status-badge"

const TRAFFIC = "var(--plane-traffic)"
const DAYS = 30
const RANK_ROWS = 6
const HEAT_ROWS = 6
const DRILL_DAY_ROWS = 5

function shortDay(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined,
    { day: "numeric", month: "short" })
}

function cellTitle(slotLabel: string, c: HeatCell | undefined): string {
  if (!c) return `${slotLabel} · not measured`
  return `${slotLabel} · ↓${fmtRate(c.in_bps)} ↑${fmtRate(c.out_bps)}`
    + ` · mean of ${c.n} sample${c.n === 1 ? "" : "s"} over ${c.days}`
    + ` day${c.days === 1 ? "" : "s"}`
}

// -- the ranking row -----------------------------------------------------------

function CeilingBar({ row }: { row: CapacityRow }) {
  // No ceiling recorded is a DIFFERENT sentence from 0% used, so it gets no
  // track at all: an empty bar would read as "plenty of room" and a full one
  // as trouble, and neither is a thing we know.
  if (row.util_pct == null) {
    return (
      <span className="text-2xs text-faint-foreground">
        {row.rate_n === 0 ? "not recording yet" : "no ceiling recorded"}
      </span>
    )
  }
  return (
    <>
      <span className="h-1 min-w-8 flex-1 overflow-hidden rounded-full bg-muted">
        <span className="block h-full rounded-full"
          style={{ width: `${Math.min(100, row.util_pct)}%`,
                   background: TRAFFIC, opacity: 0.85 }} />
      </span>
      <span className="w-10 shrink-0 text-right font-mono text-2xs tabular-nums text-muted-foreground">
        {Math.round(row.util_pct)}%
      </span>
    </>
  )
}

function RankRow({ row, refMs }: { row: CapacityRow; refMs: number }) {
  const down = isDownState(row.device_state)
  return (
    <Link to="/topology" state={{ deviceId: row.device_id }}
      title={`${row.label} on ${row.device_name ?? "this device"} · busy hour`
        + ` ${hourLabel(row.busy_hour, refMs)} · ↓${fmtRate(row.busy_in_bps)}`
        + ` ↑${fmtRate(row.busy_out_bps)} · peak ↓${fmtRate(row.peak_in_bps)}`
        + ` · ${row.days} day${row.days === 1 ? "" : "s"} recorded`}
      className="flex h-12 items-center gap-3 px-4 transition-colors hover:bg-foreground/5">
      <span className="w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="min-w-0 truncate font-mono text-xs font-medium">
            {row.label}
          </span>
          <span className="min-w-0 truncate text-2xs text-faint-foreground">
            {row.device_name}
          </span>
          {row.alarm === 1 && <Chip tone="destructive">port down</Chip>}
          {row.bw_high_alarm === 1 && <Chip tone="warning">over ceiling</Chip>}
          {down && row.alarm !== 1 && (
            <span className="shrink-0 text-2xs text-faint-foreground">device down</span>
          )}
        </span>
        <span className="mt-1.5 flex items-center gap-2">
          <CeilingBar row={row} />
        </span>
      </span>
      <span className="shrink-0 text-right">
        {/* The figure the ranking is SORTED on, with the arrow naming the
            direction that sort used. Printing ↓ beside a row ordered by its
            upload is a list ordered by a number nobody can see. */}
        <span className="block font-mono text-xs font-semibold tabular-nums">
          {busyArrow(row)}{fmtRate(row.busy_bps)}
        </span>
        <span className="block font-mono text-2xs tabular-nums text-faint-foreground">
          {row.busy_hour == null ? "no busy hour yet" : hourLabel(row.busy_hour, refMs)}
        </span>
      </span>
    </Link>
  )
}

// -- the Home panel ------------------------------------------------------------

export function CapacityPanel() {
  const { scopeOrg, canWrite } = useAuth()
  const q = useQuery({
    queryKey: ["capacity", scopeOrg],
    queryFn: () => capacityApi.org(scopeOrg, DAYS),
    enabled: !!scopeOrg && canWrite,
    staleTime: 300_000,
    refetchInterval: 600_000,
  })

  const data = q.data
  const model = useMemo(() => {
    if (!data) return null
    const refMs = toUtcDate(data.until).getTime()
    const slots = hourSlots(refMs)
    const byKey = new Map(data.ranking.map((r) => [`${r.device_id}:${r.if_index}`, r]))
    const heat: HeatmapRow[] = []
    for (const hm of data.heatmap.slice(0, HEAT_ROWS)) {
      const row = byKey.get(`${hm.device_id}:${hm.if_index}`)
      if (!row) continue          // heatmap rows ARE ranking rows; never invent one
      const cells = new Map(hm.cells.map((c) => [c.h, c]))
      heat.push({
        key: `${hm.device_id}:${hm.if_index}`,
        // The bare interface, not the alias-expanded label: an alias that
        // repeats the port name ("GE0/5 INPUT (GE0/5 INPUT)") ate the row's
        // whole label column and truncated the device with it. The ranking
        // beside this carries the full name.
        label: (
          <span className="flex items-baseline gap-1.5">
            <span className="min-w-0 truncate font-mono text-foreground">
              {row.if_name || `if${row.if_index}`}
            </span>
            <span className="min-w-0 truncate text-faint-foreground">{row.device_name}</span>
          </span>
        ),
        values: slots.map((s) => cells.get(s.h)?.bps ?? null),
        max: row.busy_bps,
        title: (i) => `${row.label} · ${cellTitle(slots[i].label, cells.get(slots[i].h))}`,
      })
    }
    return { refMs, slots, heat, columns: slots.map((s) => ({ key: s.h, label: s.label })) }
  }, [data])

  if (!scopeOrg || !canWrite) return null
  // Nothing to plan capacity for: no port on this fleet is watched, feeds a
  // device or carries a limit, so the historian samples none of them.
  if (!data || data.eligible === 0 || !model) return null

  const recording = data.recording_since
    ? toUtcDate(data.recording_since)
    : null
  // The window the server actually served, not the one asked for: `since` is
  // already clamped to where recording began, so this is the honest span at
  // every age of the historian rather than a threshold somebody picked.
  const covered = Math.max(1, Math.round(
    (toUtcDate(data.until).getTime() - toUtcDate(data.since).getTime()) / 86_400_000))
  const short = covered < data.days
  const top = data.ranking.slice(0, RANK_ROWS)

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <h2 className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-foreground">Busy hour</span>
          <span className="text-xs text-faint-foreground">
            last {covered} day{covered === 1 ? "" : "s"}
            {short && recording
              && ` · recording since ${shortDay(recording.getTime())}`}
            {" · "}{data.sampled} of {data.eligible} ports
          </span>
        </h2>
        <Link to="/issues?kind=bandwidth"
          className="shrink-0 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
          Issues →
        </Link>
      </div>

      {data.sampled === 0 ? (
        <p className="px-4 py-8 text-center text-xs text-faint-foreground">
          No traffic recorded yet.{" "}
          {recording
            ? `Recording began ${shortDay(recording.getTime())}; `
            : ""}
          {data.eligible} port{data.eligible === 1 ? "" : "s"} will fill in as
          the SNMP sweeps land.
        </p>
      ) : (
        <div className="grid items-start gap-x-4 @3xl:grid-cols-[1.05fr_1fr]">
          <div className="flex flex-col py-1.5">
            {top.map((r) => (
              <RankRow key={`${r.device_id}:${r.if_index}`} row={r} refMs={model.refMs} />
            ))}
            <p className="px-4 pt-2 pb-1 text-2xs text-faint-foreground">
              Closest to the recorded ceiling first.
              {data.sampled > top.length
                && ` ${data.sampled - top.length} more recording.`}
              {data.no_ceiling > 0
                && ` ${data.no_ceiling} port${data.no_ceiling === 1
                  ? " carries" : "s carry"} traffic with no ceiling recorded,`
                  + ` so ${data.no_ceiling === 1 ? "it cannot" : "they cannot"}`
                  + " be ranked against one."}
              {data.clamped
                && ` Asked for ${data.days_requested} days; the hourly record`
                  + ` keeps ${data.max_days}.`}
            </p>
          </div>
          <div className="px-4 pt-3 pb-4">
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="text-2xs font-medium text-muted-foreground">
                Hour of day
              </span>
              <span className="text-2xs text-faint-foreground">
                each row against its own busiest hour
              </span>
            </div>
            <Heatmap rows={model.heat} columns={model.columns} labelWidth="12rem" />
            <HeatmapLegend className="mt-2" />
          </div>
        </div>
      )}
    </section>
  )
}

// -- the per-port drill (device panel, Ports tab) ------------------------------

// ONE LINE PER DIRECTION. The first cut put "Busy hour 17:00" over a single
// ↓ figure, on a 380px column that hid ↑ behind a container query — so a port
// carrying a gigabit of upload read as 943 b/s, and its busy hour was named
// by the direction that was doing nothing. Each direction now states its own
// figure, its own hour and its own peak, and neither can stand in for the other.
function DirLine({ arrow, bps, hour, peak, refMs }: {
  arrow: string
  bps: number | null
  hour: number | null
  peak: number | null
  refMs: number
}) {
  return (
    <div className="flex items-baseline gap-2 text-2xs">
      <span className="w-3 shrink-0 font-mono text-muted-foreground">{arrow}</span>
      <span className="font-mono font-semibold tabular-nums text-foreground">
        {fmtRate(bps)}
      </span>
      <span className="text-muted-foreground">
        at {hourLabel(hour, refMs)}
      </span>
      <span className="ml-auto font-mono tabular-nums text-faint-foreground">
        peak {fmtRate(peak)}
      </span>
    </div>
  )
}

function DrillFigures({ data, refMs }: {
  data: PortHistoryReply
  refMs: number
}) {
  // util_pct is resolved SERVER-side, through the same direction rule and the
  // same helpers the org ranking uses — so this can never grade the port
  // differently from the list it was opened from.
  return (
    <div className="flex flex-col gap-1">
      <DirLine arrow="↓" bps={data.busy_in_bps} hour={data.busy_in_hour}
        peak={data.peak_in_bps} refMs={refMs} />
      <DirLine arrow="↑" bps={data.busy_out_bps} hour={data.busy_out_hour}
        peak={data.peak_out_bps} refMs={refMs} />
      <div className="flex items-baseline gap-2 text-2xs">
        {data.util_pct != null ? (
          <span className="text-muted-foreground">
            <span className="font-mono font-semibold text-foreground">
              {Math.round(data.util_pct)}%
            </span>{" "}
            of the {data.bw_max_mbps} Mbps ceiling, on {data.bw_direction}
          </span>
        ) : (
          <span className="text-faint-foreground">no ceiling recorded</span>
        )}
        <span className="ml-auto text-faint-foreground">
          {data.days_covered} day{data.days_covered === 1 ? "" : "s"} recorded
        </span>
      </div>
    </div>
  )
}

export function PortTrafficProfile({ device, port }: {
  device: OrgDevice
  port: SwitchPort
}) {
  const q = useQuery({
    queryKey: ["port-history", device.id, port.if_index],
    queryFn: () => capacityApi.port(device.id, port.if_index, DAYS),
    staleTime: 300_000,
  })
  const data = q.data
  const model = useMemo(() => {
    if (!data) return null
    const refMs = toUtcDate(data.until).getTime()
    const slots = hourSlots(refMs)
    const cells = new Map(data.hours.map((c) => [c.h, c]))
    // Both directions share ONE normaliser: two rows each scaled to their own
    // max would draw a 2 Mb/s upload as busy as a 900 Mb/s download.
    const max = Math.max(0, ...data.hours.flatMap(
      (c) => [c.in_bps ?? 0, c.out_bps ?? 0]))
    const mk = (label: string, pick: (c: HeatCell) => number | null): HeatmapRow => ({
      key: label,
      label: <span className="font-mono">{label}</span>,
      values: slots.map((s) => {
        const c = cells.get(s.h)
        return c ? pick(c) : null
      }),
      max,
      title: (i) => cellTitle(slots[i].label, cells.get(slots[i].h)),
    })
    return {
      refMs, slots,
      columns: slots.map((s) => ({ key: s.h, label: s.label })),
      rows: [mk("↓ down", (c) => c.in_bps), mk("↑ up", (c) => c.out_bps)],
      recent: data.series.slice(-DRILL_DAY_ROWS).reverse(),
    }
  }, [data])

  if (q.isLoading) {
    return <p className="text-2xs text-muted-foreground">loading…</p>
  }
  if (q.error || !data || !model) {
    return <p className="text-2xs text-destructive">Couldn't load the traffic history.</p>
  }
  if (data.rate_n === 0) {
    const recording = data.recording_since ? toUtcDate(data.recording_since) : null
    return (
      <p className="text-2xs text-faint-foreground">
        No traffic recorded for this port yet.
        {recording && ` Recording began ${shortDay(recording.getTime())}.`}
        {data.samples > 0
          && ` It was walked ${data.samples} time${data.samples === 1 ? "" : "s"},`
             + " with no rate computable."}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <DrillFigures data={data} refMs={model.refMs} />
      <Heatmap rows={model.rows} columns={model.columns} labelWidth="4rem"
        axisEvery={6} />
      <HeatmapLegend />
      {model.recent.length > 0 && (
        <div className="flex flex-col gap-0.5 border-t pt-2">
          {model.recent.map((d) => (
            <div key={d.day} className="flex items-baseline gap-2 text-2xs">
              <span className="w-14 shrink-0 tabular-nums text-muted-foreground">
                {shortDay(d.day * 1000)}
              </span>
              <span className="font-mono tabular-nums text-foreground">
                ↓{fmtRate(d.busy_in_bps)}
              </span>
              <span className="hidden font-mono tabular-nums text-muted-foreground @[20rem]:inline">
                ↑{fmtRate(d.busy_out_bps)}
              </span>
              <span className="ml-auto font-mono tabular-nums text-faint-foreground">
                {d.busy_in_hour == null
                  ? "no rate that day"
                  : `busiest ${hourLabel(d.busy_in_hour, model.refMs)}`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// The port whose profile opens with the tab: a NEW chart may not ship buried
// in a closed fold, and the backhaul port is the one this panel has a story
// about. Exactly one candidate or none — an arbitrary pick would teach the
// operator that the open row means nothing.
export function autoProfilePort(ports: SwitchPort[]): number | null {
  const uplinks = ports.filter((p) => p.uplink_device_id != null)
  if (uplinks.length === 1) return uplinks[0].id
  const recording = ports.filter(portRecords)
  return recording.length === 1 ? recording[0].id : null
}

export function useProfileState(ports: SwitchPort[]) {
  const [open, setOpen] = useState<number | null | undefined>(undefined)
  const value = open === undefined ? autoProfilePort(ports) : open
  return [value, setOpen] as const
}
