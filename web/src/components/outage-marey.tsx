// THE OUTAGE MAREY — one row per device, time across, down intervals as bars.
// Named for Marey's train schedule: the whole point is that the eye reads
// SIMULTANEITY off vertical alignment, which is the one thing this product's
// hardware cannot report. The C-Data/DBC fleet publishes no dying-gasp and no
// LOS, so a power cut and a fibre cut arrive as identical bare offlines; what
// separates them is that a power cut drops several OLTs in the same minute.
// That tell is invisible in a list of outages and obvious here.
//
// SO ALIGNMENT IS THE FEATURE, and it is exact by construction: every row is
// positioned by ONE scale over ONE measured track width, in the same grid
// column, so two bars that start in the same second start at the same pixel.
// No per-row scale, no per-row SVG viewBox that could round differently.
//
// IT SHOWS AND DOES NOT CONCLUDE. There is no "power cut" verdict here and
// there must not be one: `ponfault` earns its verdicts from evidence this
// panel does not have, and a chart that grades an alignment would be inventing
// the discriminator the hardware is missing. The human judges.
//
// It reads the SAME reconstruction the map replay reads and nothing else, so
// the bar and the pin can never disagree about when a box dropped.
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"
import { fmtDurS, fmtTime, timeScale } from "@/chart/scale"
import { fmtDateTime } from "@/lib/format"
import { isPassiveType, type OrgDevice } from "@/lib/types"
import type { Reconstruction } from "@/lib/replay"
import { cn } from "@/lib/utils"

const ROW_H = 18
const BAR_H = 10

export interface MareyRow {
  id: number
  name: string
  depth: number
}

// TOPOLOGY ORDER, mirroring the Network tree's own walk: a device sits under
// its PRIMARY parent, and a detached row (`tree_detached`) is a top-level row
// exactly as the tree draws it. Gear only — a passive has no state, no FSM and
// no outage of its own, so a row for one could only ever be empty, and 40
// empty rows would bury the boxes that do drop.
export function mareyRows(devices: OrgDevice[]): MareyRow[] {
  const gear = devices.filter((d) => !isPassiveType(d.device_type))
  const byId = new Map(gear.map((d) => [d.id, d]))
  const parentOf = (d: OrgDevice) =>
    (d.parent_device_id == null || d.tree_detached === 1
      ? undefined : byId.get(d.parent_device_id))
  const kids = new Map<number, OrgDevice[]>()
  for (const d of gear) {
    const p = parentOf(d)
    if (!p) continue
    if (!kids.has(p.id)) kids.set(p.id, [])
    kids.get(p.id)!.push(d)
  }
  const byName = (a: OrgDevice, b: OrgDevice) => a.name.localeCompare(b.name)
  const out: MareyRow[] = []
  const seen = new Set<number>()
  const emit = (d: OrgDevice, depth: number) => {
    if (seen.has(d.id)) return          // a cycle may never spin a render
    seen.add(d.id)
    out.push({ id: d.id, name: d.name, depth })
    for (const k of (kids.get(d.id) ?? []).sort(byName)) emit(k, depth + 1)
  }
  for (const d of gear.filter((x) => !parentOf(x)).sort(byName)) emit(d, 0)
  for (const d of gear.sort(byName)) emit(d, 0)   // anything a cycle orphaned
  return out
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
      <span aria-hidden className={cn("h-2 w-4 rounded-[2px]", className)} />
      {label}
    </span>
  )
}

// The dead zone: a hairline track where the axis runs but no instrument can
// answer (the OTDR concept `<Reading>` already uses). A NON-COLOUR channel —
// texture, not tone — so it survives greyscale and a screenshot pasted into
// WhatsApp, and so it can never be mistaken for a quiet alarm.
const UNKNOWN_FILL =
  "repeating-linear-gradient(135deg, var(--muted-foreground) 0 1px,"
  + " transparent 1px 5px)"

export function OutageMarey({
  recon, rows, at, onScrub, onPick, selectedId, className,
}: {
  recon: Reconstruction
  rows: MareyRow[]
  at: number
  onScrub: (t: number) => void
  onPick?: (id: number) => void
  selectedId?: number | null
  className?: string
}) {
  const [w, setW] = useState(0)
  const trackRef = useRef<HTMLDivElement | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const hostRef = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    trackRef.current = el
    if (!el) return
    const ro = new ResizeObserver(([e]) => {
      const next = Math.round(e.contentRect.width)
      setW((prev) => (prev === next ? prev : next))
    })
    ro.observe(el)
    observer.current = ro
    setW(Math.round(el.getBoundingClientRect().width))
  }, [])
  useEffect(() => () => observer.current?.disconnect(), [])

  const spanMs = (recon.until - recon.since) * 1000
  const x = useMemo(
    () => timeScale([recon.since * 1000, recon.until * 1000], [0, Math.max(1, w)]),
    [recon.since, recon.until, w])
  const ticks = useMemo(
    () => (w > 0 ? x.ticks(Math.max(2, Math.floor(w / 96))) : []), [x, w])

  // Bars are memoized on the reconstruction, never on the cursor: scrubbing
  // moves one absolutely-positioned rule and recomputes no geometry.
  const bars = useMemo(() => rows.map((r) => ({
    id: r.id,
    down: recon.downBars(r.id),
    unknown: recon.unknownBars(r.id),
    downS: recon.downSecondsIn(r.id, recon.since, recon.until),
    outages: recon.outageCountIn(r.id, recon.since, recon.until),
  })), [rows, recon])

  // POSITIONED IN MEASURED PIXELS, not percentages, and the reason is the
  // simultaneity read this chart exists for. A two-minute flap is 0.02% of a
  // week: as a percentage width it rounds to a fifth of a pixel and vanishes,
  // and several OLTs flapping in the same minute is exactly the case that must
  // NOT vanish. A floor of 2px keeps the shortest real outage visible, and
  // everything on the plot reads the one scale, so alignment stays exact.
  const px = (a: number, b: number) => {
    const left = x(a * 1000)
    return { left, width: Math.max(2, x(b * 1000) - left) }
  }

  const scrubbing = useRef(false)
  const seek = useCallback((clientX: number) => {
    const el = trackRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const t = Math.round(x.invert(clientX - rect.left).getTime() / 1000)
    onScrub(Math.min(recon.until, Math.max(recon.since, t)))
  }, [x, onScrub, recon.since, recon.until])

  const anyOutage = bars.some((b) => b.down.length > 0)
  const cursorLeft = x(at * 1000)

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 pb-1.5">
        <Swatch className="bg-destructive" label="down" />
        <Swatch className="bg-success/25" label="up" />
        <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
          <span aria-hidden className="h-2 w-4 rounded-[2px]"
            style={{ backgroundImage: UNKNOWN_FILL }} />
          not recorded
        </span>
        <span className="text-2xs text-faint-foreground">
          {rows.length} device{rows.length === 1 ? "" : "s"}
          {anyOutage ? "" : " · no outages in this window"}
        </span>
      </div>

      {rows.length === 0 && (
        <p className="px-3 py-6 text-center text-xs text-muted-foreground">
          No devices to replay here.
        </p>
      )}

      {/* THE RULER LIVES INSIDE THE SCROLLER, stuck to its bottom, and that is
          not a layout preference. A ruler in a sibling element sits in a box
          the scrollbar has not narrowed, so its scale would run a few pixels
          wider than the bars above it: the labels would drift and a click
          would seek to slightly the wrong time. Sharing one scroll box makes
          both the same width by construction, which is the same reason every
          row shares one scale. */}
      <div className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="relative grid"
          style={{ gridTemplateColumns: "var(--marey-gutter) 1fr" }}>
          {rows.map((r, i) => {
              const b = bars[i]
              return (
                <div key={r.id} className="contents">
                  <button type="button"
                    onClick={() => onPick?.(r.id)}
                    style={{ height: ROW_H, paddingLeft: 8 + r.depth * 9 }}
                    className={cn(
                      "flex items-center overflow-hidden pr-2 text-left font-mono text-2xs whitespace-nowrap",
                      selectedId === r.id
                        ? "bg-selected text-foreground"
                        : "text-muted-foreground hover:bg-foreground/5")}>
                    <span className="truncate">{r.name}</span>
                  </button>
                  <div
                    style={{ height: ROW_H }}
                    className={cn("relative", selectedId === r.id && "bg-selected")}
                    title={b.downS > 0
                      ? `${r.name}: down ${fmtDurS(b.downS)} in ${b.outages} outage${b.outages === 1 ? "" : "s"}`
                      : `${r.name}: no outage recorded in this window`}>
                    <span aria-hidden
                      className="absolute inset-x-0 rounded-[2px] bg-success/25"
                      style={{ top: (ROW_H - BAR_H) / 2, height: BAR_H }} />
                    {w > 0 && b.unknown.map(([a, z]) => (
                      <span key={`u${a}`} aria-hidden
                        className="absolute rounded-[2px]"
                        style={{ ...px(a, z), top: (ROW_H - BAR_H) / 2,
                                 height: BAR_H, backgroundImage: UNKNOWN_FILL,
                                 opacity: 0.55 }} />
                    ))}
                    {w > 0 && b.down.map(([a, z]) => (
                      <span key={`d${a}`} aria-hidden
                        className="absolute rounded-[1px] bg-destructive"
                        style={{ ...px(a, z), top: (ROW_H - BAR_H) / 2,
                                 height: BAR_H }} />
                    ))}
                  </div>
                </div>
              )
            })}

          {/* The axis and the seek surface, stuck to the bottom of the same
              scroller. Dragging anywhere on it moves the map clock; the map
              moves this cursor back. It is also the MEASURED element, so one
              rect answers "how wide is the track" and "where did they click"
              without depending on a row that may unmount. */}
          <span className="sticky bottom-0 z-10 flex h-6 items-center border-t bg-popover pl-2 text-2xs text-faint-foreground">
            scrub
          </span>
          <div
            ref={hostRef}
            className="sticky bottom-0 z-10 h-6 cursor-ew-resize touch-none border-t bg-popover select-none"
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId)
              scrubbing.current = true
              seek(e.clientX)
            }}
            onPointerMove={(e) => { if (scrubbing.current) seek(e.clientX) }}
            onPointerUp={(e) => {
              scrubbing.current = false
              e.currentTarget.releasePointerCapture(e.pointerId)
            }}
            onPointerCancel={() => { scrubbing.current = false }}>
            {ticks.map((d) => (
              <span key={+d}
                className="absolute top-1 -translate-x-1/2 text-2xs tabular-nums text-faint-foreground"
                style={{ left: x(+d) }}>
                {fmtTime(d, spanMs)}
              </span>
            ))}
            <span aria-hidden
              className="absolute bottom-0 h-3 w-0.5 -translate-x-1/2 rounded-full bg-primary"
              style={{ left: cursorLeft }}
              title={fmtDateTime(new Date(at * 1000).toISOString())} />
          </div>

          {/* Gridlines and the cursor run over every row at once, in the track
              column, so the vertical read is one unbroken line. The sticky
              ruler paints over their tail, and carries its own cursor pip. */}
          <div aria-hidden
            className="pointer-events-none absolute inset-y-0 right-0"
            style={{ left: "var(--marey-gutter)" }}>
            {ticks.map((d) => (
              <span key={+d}
                className="absolute inset-y-0 w-px bg-border"
                style={{ left: x(+d), opacity: 0.5 }} />
            ))}
            <span className="absolute inset-y-0 w-px bg-primary"
              style={{ left: cursorLeft }} />
          </div>
        </div>
      </div>
    </div>
  )
}

export function MareyToggle({ open, onToggle }: {
  open: boolean; onToggle: () => void
}) {
  return (
    <button type="button" onClick={onToggle}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs text-muted-foreground hover:bg-foreground/5"
      title={open ? "Hide the outage timeline" : "Show the outage timeline"}>
      {open ? <ChevronDown className="size-3" /> : <ChevronUp className="size-3" />}
      timeline
    </button>
  )
}
