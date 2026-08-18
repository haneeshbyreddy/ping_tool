import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Activity, Play, Square } from "lucide-react"
import { toast } from "sonner"
import { ApiError, livePingApi } from "@/lib/api"
import { ago, fmtMs } from "@/lib/format"
import type { LivePingSample, LivePingSession, OrgDevice } from "@/lib/types"
import { Reading, type ReadingState } from "@/components/reading"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * Live ping: one line per packet, for a technician standing at the device.
 *
 * Two rules shape everything here.
 *
 * SEQUENCE NUMBERS ARE THE POINT. Three consecutive losses and three
 * scattered losses are different faults and must not look alike, so every
 * line carries its own sequence and a run of losses reads as a run. A batch
 * that never reached us is drawn as its OWN kind of row: "not reported" is
 * neither a reply nor a timeout, and collapsing it into either would invent a
 * measurement.
 *
 * "THE PROBE WENT AWAY" AND "THE DEVICE IS DOWN" ARE DIFFERENT SENTENCES.
 * The headline number is a `<Reading>` and its state carries which one is
 * true: `frozen` means we are out of contact with the probe, `absent` means
 * we are measuring and nothing has replied, `stale` means it replied but not
 * lately. A spinner would say neither.
 */

// Enough scrollback for a whole session at the fast cadence; the panel keeps
// the NEWEST lines, which are the ones being watched.
const MAX_LINES = 400

// How long past the probe's own check-in cadence we keep saying "waiting"
// before saying "it has not picked this up". Two cadences plus a breath: one
// missed check-in is normal, two is a finding.
const WAIT_GRACE_S = 15

type Row =
  | { kind: "sample"; seq: number; rtt: number | null }
  | { kind: "gap"; seq: number; count: number }

/** Turn the raw samples into rows, making a skipped batch visible as one. */
function toRows(samples: LivePingSample[]): Row[] {
  const out: Row[] = []
  let prev: number | null = null
  for (const [seq, rtt] of samples) {
    if (prev != null && seq > prev + 1) {
      out.push({ kind: "gap", seq: prev + 1, count: seq - prev - 1 })
    }
    out.push({ kind: "sample", seq, rtt })
    prev = seq
  }
  return out
}

/** How many packets in a row, at the end, failed to answer. */
function trailingLoss(samples: LivePingSample[]): number {
  let n = 0
  for (let i = samples.length - 1; i >= 0; i--) {
    if (samples[i][1] != null) break
    n++
  }
  return n
}

function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
}

function StopLine({ session }: { session: LivePingSession }) {
  if (session.stop_reason === "expired") {
    return (
      <span className="text-2xs text-muted-foreground">
        Session ended. Live ping stops itself after 5 minutes. Start it again if you need more.
      </span>
    )
  }
  if (session.stop_reason === "refused") {
    return (
      <span className="text-2xs text-warning">
        The probe refused this target: {session.stop_detail || "no reason given"}.
      </span>
    )
  }
  return <span className="text-2xs text-muted-foreground">Session stopped.</span>
}

export function LivePingPanel({ device }: { device: OrgDevice }) {
  const cursor = useRef(0)
  const sidRef = useRef<string | null>(null)
  const scroller = useRef<HTMLDivElement | null>(null)
  const pinned = useRef(true)
  const [samples, setSamples] = useState<LivePingSample[]>([])
  const [waitingSince, setWaitingSince] = useState<number | null>(null)

  const enabled = !!device.ip_address && !!device.assigned_node_id
  const status = useQuery({
    queryKey: ["liveping", device.id],
    queryFn: () => livePingApi.status(device.id, cursor.current),
    enabled,
    // Once a second while packets are arriving; a slow idle poll so a session
    // a colleague started on this device shows up here too.
    refetchInterval: (q) => (q.state.data?.session?.live ? 1000 : 20_000),
    refetchOnWindowFocus: true,
  })

  const session = status.data?.session ?? null

  useEffect(() => {
    const data = status.data
    if (!data) return
    const sid = data.session?.sid ?? null
    if (sid !== sidRef.current) {
      sidRef.current = sid
      cursor.current = 0
      setSamples([])
      setWaitingSince(sid && !data.session?.picked_up ? Date.now() : null)
      return
    }
    if (data.session?.picked_up) setWaitingSince(null)
    if (data.samples.length) {
      cursor.current = data.cursor
      setSamples((prev) => [...prev, ...data.samples].slice(-MAX_LINES))
    }
  }, [status.data])

  useEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [samples])

  const start = useMutation({
    mutationFn: () => livePingApi.start(device.id),
    onSuccess: (data) => {
      sidRef.current = data.session.sid
      cursor.current = 0
      setSamples([])
      setWaitingSince(data.session.picked_up ? null : Date.now())
      status.refetch()
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not start live ping"),
  })

  const stop = useMutation({
    mutationFn: (sid: string) => livePingApi.stop(sid),
    onSuccess: () => status.refetch(),
  })

  const rows = useMemo(() => toRows(samples), [samples])
  const lastReply = useMemo(() => {
    for (let i = samples.length - 1; i >= 0; i--) {
      if (samples[i][1] != null) return samples[i][1]
    }
    return null
  }, [samples])
  const stuckLoss = trailingLoss(samples)

  if (!enabled || !status.data) return null
  const info = status.data
  if (!info.enabled) return null

  const live = !!session?.live
  const waiting = live && !session?.picked_up
  const waitedS = waitingSince ? (Date.now() - waitingSince) / 1000 : 0
  const overdue = waiting && waitedS > info.wait_hint_s * 2 + WAIT_GRACE_S
  // Four packet intervals with nothing arriving is the channel, not the
  // device: at this cadence a dark device still produces a timeout LINE every
  // interval, so silence means the probe stopped talking to us.
  const silent = live && !waiting
    && session!.silent_s > Math.max(6, (session!.interval_ms / 1000) * 4)

  // The headline reading, and the whole reason it is a <Reading>: each state
  // is a different claim about the world.
  let readingState: ReadingState = "current"
  let readingValue: string = lastReply == null ? "" : fmtMs(lastReply)
  let readingReason: string | undefined
  if (!live) {
    readingState = "absent"
    readingReason = "not measuring"
  } else if (waiting) {
    // Out of contact with the INSTRUMENT. Not a claim about the device.
    readingState = "frozen"
    readingValue = "—"
    readingReason = "waiting for the probe"
  } else if (silent) {
    // Same claim, later in the session: the probe went quiet, so the last
    // number on screen describes a moment that has passed.
    readingState = "frozen"
    readingReason = "probe stopped reporting"
  } else if (lastReply == null) {
    // We ARE measuring and nothing has answered: there is no latency to
    // print, which is the dead zone. The reason has to carry the count, or a
    // device that is down for the whole session leaves the panel looking like
    // it never started.
    readingState = "absent"
    readingReason = session!.sent > 0
      ? `no reply to any of the ${session!.sent} packets sent`
      : "no reply yet in this session"
  } else if (stuckLoss > 0) {
    readingState = "stale"
  }

  const canStart = info.supported && !info.unprobed
  const busy = start.isPending || stop.isPending

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/40 p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="flex items-center gap-1.5 text-2xs font-medium text-muted-foreground">
          <Activity className="size-3" aria-hidden />
          Live ping
        </span>
        {live && (
          <>
            <span className="font-mono text-2xs text-faint-foreground">{session!.device_ip}</span>
            <span className="text-2xs text-faint-foreground">
              {session!.interval_ms >= 2000
                ? `1 packet every ${Math.round(session!.interval_ms / 1000)} s`
                : "1 packet a second"}
              {session!.infra && " · gentle, this box feeds others"}
            </span>
          </>
        )}
        <span className="ml-auto flex items-center gap-2">
          {live && (
            <span className="font-mono text-2xs text-muted-foreground">
              {clock(session!.remaining_s)} left
            </span>
          )}
          {live ? (
            <Button size="sm" variant="outline" disabled={busy}
              onClick={() => stop.mutate(session!.sid)}>
              <Square className="size-3" aria-hidden /> Stop
            </Button>
          ) : (
            <Button size="sm" variant="outline" disabled={busy || !canStart}
              title={canStart ? undefined : `This probe is on v${info.node_version ?? "?"}`}
              onClick={() => start.mutate()}>
              <Play className="size-3" aria-hidden />
              {canStart ? "Start" : `Probe needs v${info.needs_version}`}
            </Button>
          )}
        </span>
      </div>

      {!live && canStart && !session && (
        <p className="text-2xs text-muted-foreground">
          Watch one packet at a time while you work on it. Stops itself after {Math.round(info.max_s / 60)} minutes.
        </p>
      )}
      {!canStart && info.unprobed && (
        <p className="text-2xs text-muted-foreground">This device has no IP address to ping.</p>
      )}
      {!canStart && !info.unprobed && (
        <p className="text-2xs text-muted-foreground">
          The probe {info.node_id ? <span className="font-mono">{info.node_id}</span> : "for this device"} runs
          v{info.node_version ?? "an older build"} and has no live ping. It arrives with v{info.needs_version}.
        </p>
      )}
      {canStart && info.node_stale && !live && (
        <p className="text-2xs text-warning">
          The probe last checked in {ago(info.node_seen)}. Live ping will not start until it comes back.
        </p>
      )}
      {!live && session && <StopLine session={session} />}

      {live && (
        <>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Reading value={readingValue || "—"} unit={readingState === "current" || readingState === "stale" ? "ms" : undefined}
              state={readingState} reason={readingReason} className="text-sm font-semibold" />
            <span className="text-2xs text-muted-foreground">
              {session!.sent} sent · {session!.received} replied
              {session!.lost > 0 && (
                <span className="font-semibold text-destructive">
                  {" "}· {session!.lost} lost ({Math.round((session!.lost * 100) / Math.max(1, session!.sent))}%)
                </span>
              )}
            </span>
            {stuckLoss >= 3 && (
              <span className="text-2xs font-semibold text-destructive">
                {stuckLoss} in a row unanswered
              </span>
            )}
            {session!.started_by && (
              <span className="ml-auto text-2xs text-faint-foreground">
                started by {session!.started_by}
              </span>
            )}
          </div>

          {waiting ? (
            <p className={cn("text-2xs", overdue ? "text-warning" : "text-muted-foreground")}>
              {overdue ? (
                <>
                  The probe has not picked this up after {Math.round(waitedS)} s. It may be offline.
                  Nothing here says anything about the device yet.
                </>
              ) : (
                <>
                  Waiting for the probe. It checks in about every {info.wait_hint_s} s, and packets start
                  the moment it does.
                </>
              )}
            </p>
          ) : (
            <>
            {silent && (
              <p className="text-2xs text-warning">
                No packets have reached us for {Math.round(session!.silent_s)} s. The probe has gone
                quiet. This says nothing about the device: the lines below are what it last sent.
              </p>
            )}
            <div ref={scroller}
              onScroll={(e) => {
                const el = e.currentTarget
                pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
              }}
              className="h-44 overflow-y-auto rounded border bg-background/60 px-2 py-1 font-mono text-2xs">
              {rows.length === 0 ? (
                <p className="py-2 text-center text-faint-foreground">no packets yet</p>
              ) : rows.map((row) => (
                row.kind === "gap" ? (
                  <div key={`g${row.seq}`} className="flex gap-3 text-faint-foreground">
                    <span className="w-10 shrink-0 text-right">{row.seq}+</span>
                    <span>{row.count} packet{row.count === 1 ? "" : "s"} not reported</span>
                  </div>
                ) : (
                  <div key={row.seq} className="flex gap-3">
                    <span className="w-10 shrink-0 text-right text-faint-foreground">{row.seq}</span>
                    {row.rtt == null ? (
                      <span className="text-destructive">timeout</span>
                    ) : (
                      <span>{fmtMs(row.rtt)} ms</span>
                    )}
                  </div>
                )
              ))}
            </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
