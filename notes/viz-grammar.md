# The chart grammar (Stage 2) — binding on every chart in this product

The kit lives in `web/src/chart/` (`frame.tsx`, `marks.tsx`, `day-strip.tsx`,
`scale.ts`) — the `map/` precedent: one module, page components compose it.
Substrate: d3-scale/d3-shape/d3-array as math only, React owns the SVG
(measured cost: +~28 KB gz on a 386 KB bundle). The house instruments
(`RxScale`, `OnuBar`, `Sparkline`/`HourStrip`) are part of this family;
anything new grows out of it — never a parallel system.

## Colour law

- **Series colours are `--chart-1..5` (the five planes), one plane per plot.**
  Measured (dataviz validator, both themes): adjacent planes sit at ΔE 2.8–4.3
  — far under the 15 a full-vision reader needs — because they were designed
  quiet. So the plane hue says what KIND of fact a chart shows; it never
  distinguishes series. Multi-series separates STRUCTURALLY: direct labels,
  two lightness/opacity steps of one plane, emphasis (one full, siblings
  muted grey), or small multiples. Never two planes paired on one plot.
- **Status hues only where the mark claims failure** — a crit-count series, a
  down day-cell, a failed-page column. Nothing a chart draws may outshout a
  live alarm on the same screen (Stage 6 measures this).
- No new hues, no gradients, no glow. Legends are neutral text + a coloured
  dot (`LegendChip`); values and labels wear text tokens, never series colour.

## The five states (declared per chart, each with a non-colour channel)

| state | channel |
|---|---|
| current | solid marks |
| stale | hollow last point, dotted tail; tooltip dates it |
| frozen | desaturated span + reason OUTSIDE the plot (mirrors `.wisp-frozen`); frozen is display-side only — the historian never stores a frozen sample |
| absent | the dead zone: gaps BREAK lines (`defined()`), a lone surviving point renders as a dot, absence is never zero |
| suppressed | struck-through legend chip (`LegendChip struck`) — a suppressed ALERT never renders like a suppressed FACT |

Counts of EVENTS (outages/week, pages/day) are the one place zero is a real
zero — an empty week draws a zero-height column, not a dead zone. Counts of
SAMPLES are never zero-filled.

## The rest of the law

- **Count agreement**: a chart's headline is recomputed from the same rows the
  tile/list beside it shows, reusing `onuSev`/`isFresh`/`isDownState`/
  `current_roster`/`device_reliability`'s rules — a chart never re-grades.
  (e.g. the availability strip counts `final_state == DOWN` only, exactly as
  `device_reliability` does; UNREACHABLE spans are listed, never counted.)
- **Time**: UTC stored (epoch ints on new endpoints), the viewer's locale on
  axes, epoch-hour/day bucket floors. Weeks anchor on Monday 00:00 UTC.
- **Endpoints**: bounded ranges (`MAX_DAYS` 400), SQL/Python aggregation
  server-side, one query per chart, no per-device fan-out. Replies that read
  the historian carry `recording_since`.
- **Inert under `useNow()`/SSE**: scales memoized on [width, domain], paths
  memoized on data identity, hover is local state, nothing animates on
  refetch or mount.
- **Empty states speak**: a young historian says "recording since <date>";
  an org with no events says "no outages in this window". Never bare
  "no data".
- **Every chart names its operator question and the action taken** (the
  Stage 0(c) list). No action = decoration = cut. No pies, no 3D, no
  count-ups, no delta-arrow filler, no dual axes.
- **Tunables**: chart display knobs that operators ask to move become
  Settings → Platform controls (the Map-detail rule); none are pre-built.
- **Exports** (Stage 5): PDF charts are vector ops in `pdf.py` with this same
  grammar; XLSX gets typed cells (real date cells, REAL columns, a `samples`
  coverage column) — never images.

## Kit inventory

- `TimeChart` — frame: measured width, recessive grid/axes, crosshair +
  tooltip layer (on by default), legend row, empty state.
- `LineMark` / `BandMark` — gap-breaking series + percentile envelopes.
- `ColumnMark` — stacked, baseline-anchored, 2px surface gaps, rounded ends.
- `EventRule` / `RuleMark` — annotations and decision boundaries (the RxScale
  grammar on a time axis).
- `DayStrip` (+ legend) — HourStrip's grammar at day grain with the
  five-state cell vocabulary (down / brief-down / up / probe-silent /
  coverage-unknown).
