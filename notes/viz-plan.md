# Historical analytics + data visualization — the plan (Stages 1–6)

Companion to `notes/viz-stage0-inventory.md` (the inventory this plan is built on).
All numbers are measured on the 2026-08-14 prod snapshot: 7 orgs, 57 gear devices,
29 OLTs, 163 PONs, 5,205 roster ONUs, 177 eligible ports; DB 62.4 MB; SPA bundle
1.37 MB raw / 386 KB gz. No production code until this is approved.

---

## STAGE 1 — the historian (`central/history.py` + `store_history.py`)

A bounded time-series layer. It stores ANSWERS (counts, rates, percentiles) that a
named chart reads — never evidence. Raw material stays in its existing bounded homes
(`snmp_walks` newest-10, `proxy_audit` 60d) and is not duplicated. Every column below
is annotated with the Stage 0(c) question (Q1–Q10) that claims it.

### Design rules applied

- **Samples are a byproduct of sweeps that already run.** Zero new SNMP load, zero
  new clocks on the report path. Three hooks, all at the folded truth AFTER every
  gate (rail guard, sane_rx, web-optics merge, freshness) — the same numbers the
  panels show:
  1. `optics.py:sync_device` — already iterates the merged roster per OLT walk; a
     small accumulator adds per-PON grouping and an rx percentile pass (≤600 values,
     one sort). Writes the OLT + PON samples.
  2. `ports.py` port sync — after `throughput_bps` and the `switch_ports` upsert,
     for ELIGIBLE ports only (monitored, OR `feeds_device_id`/`uplink_device_id`
     set, OR a bw threshold set — 177 ports today). Writes the port samples.
  3. `radius_sync.py` — after a fully-`ok` org relink, one daily row.
  Plus one nightly thread (`rollup.py`/field-prune shape) that folds day tiers and
  prunes everything.
- **A missed sweep writes NOTHING; a frozen/stale reading is NEVER sampled.** Both
  hold by construction: the hooks only run when a walk actually arrives. A down OLT
  produces a gap, which is the record. (SNMP-fresh-while-ICMP-down still samples —
  the walk is real data; that is the Epon_8 asymmetry behaving correctly.)
- **Counter mechanics:** `throughput_bps` already returns None on a negative delta,
  so a reboot's rate columns are NULL in a row that still exists — "we walked the
  port, no rate computable". A NULL rate never becomes a zero.
- **Wide numeric rows.** INTEGER/REAL only, one row per (org, key, bucket). No
  key/value rows, no JSON, no formatted strings. Time columns are INTEGER epoch
  seconds (UTC) — a deliberate, documented deviation from the ISO-TEXT convention,
  because the prompt's numbers-only rule, row size (−14 B/row), and sort/bucket
  arithmetic all point one way; the API converts at the edge. Buckets floor on
  epoch hours / epoch days (UTC), the `bucket_of` rule.
- **Every table carries `org_id` TEXT** so the org-delete introspection sweeps it
  (pinned by a test). Tables are `WITHOUT ROWID` with the PK as the read path; no
  secondary indexes (device_rollups pays for a redundant one — not repeating that).
- **Gap semantics / covered-through.** Fixed-cadence tiers: a row's absence means
  "not measured", full stop. The hourly/daily rows carry `samples` (coverage
  against the expected 12/hour), which is Q10's probe-honesty channel for free.
  Day tiers are folded nightly from hour tiers; `meta.hist_folded_through` is the
  covered-through stamp that keeps "no day row" unambiguous (absent + folded ≥ day
  = a true gap; folded < day = not yet folded). Startup catch-up folds any complete
  UTC day between the stamp and yesterday — idempotent, safe for ≤14 d of central
  downtime (bounded by the shortest hour-tier retention).
- **No transition encoding anywhere** — every tier is fixed-cadence, so the
  covered-through machinery above is the only one needed.

### Tables

Cost anchor: `device_rollups` measures 66 B/row of data; with WITHOUT ROWID, epoch
INTEGERs and no secondary index I budget **~85 B/row** (≤12 cols), ~70 B (≤8 cols),
~100 B (the wide port-day row).

**`hist_olt_sweep`** — raw tier, one row per OLT walk. PK (device_id, ts).
`org_id, device_id, ts, onus, online, warn, crit, measured, rx_med, rx_p10, rx_min`
— claimed by Q3 (the post-splice check needs 5-min grain for the last two days) and
Q10 (`measured` = the coverage/honesty channel).
29 OLTs × 288/day = **8,352 rows/day**; 48 h retention ≈ 17 k rows ≈ **1.4 MB** steady.

**`hist_olt_hour`** — 90 d. PK (device_id, bucket).
`org_id, device_id, bucket, samples, onus_max, online_min, warn_max, crit_max,
measured_min, rx_med_sum, rx_med_n, rx_min`
— upserted as running sums/extremes at walk time (the device_rollups fold pattern).
`crit_max` is the honest hourly digest of "was there a spike" (Q3); mean-of-sweep-
medians = `rx_med_sum/rx_med_n`, labeled as such (Q4).
696 rows/day; ≈ 63 k rows ≈ **5.3 MB**.

**`hist_olt_day`** — 730 d. Same shape, folded nightly from hours. 29/day;
≈ 21 k rows ≈ **1.8 MB**. (Q3, Q4, Q7 — the year-scale optics story.)

**`hist_pon_hour`** — 14 d. PK (device_id, pon_port, bucket).
`org_id, device_id, pon_port, bucket, samples, onus_max, online_min, crit_max,
rx_med_sum, rx_med_n, rx_min`
— per-PON grain is where a splice lives (Q3) and where dark-count history starts
(Stage 0(b)). Hourly-only at this cardinality: 163 PONs × 24 = 3,912 rows/day;
≈ 55 k rows ≈ **4.7 MB**. The 90 d hourly default is argued DOWN here: 90 d would
cost ~30 MB and no ranked question needs hour grain on a PON beyond the splice-
aftermath window; week-over-week (Q4) reads the day tier.
`pon_port` TEXT is a key, not a sample — the roster's own label, so charts join
`current_roster`/`fibre_pon` without translation.

**`hist_pon_day`** — 730 d, folded nightly. 163/day; ≈ 119 k rows ≈ **10.1 MB**.
(Q4 — the flagship "PON degrading week over week" reads exactly this.)

**`hist_port_sweep`** — raw tier, eligible ports only. PK (device_id, if_index, ts).
`org_id, device_id, if_index, ts, in_bps, out_bps, oper_up`
— claimed by Q5's incident half ("when this afternoon did the uplink flatline/
saturate"). 177 × 288 = **50,976 rows/day**; 48 h ≈ 102 k rows ≈ **7.1 MB** steady.
This is the first table to cut if the budget is contested.

**`hist_port_hour`** — 30 d. PK (device_id, if_index, bucket).
`org_id, device_id, if_index, bucket, samples, rate_n, in_sum, in_max, out_sum,
out_max, up_samples`
— `rate_n` counts samples that HAD a rate (reset-safe mean = `in_sum/rate_n`).
4,248 rows/day; ≈ 127 k rows ≈ **10.8 MB**. 30 d (not 90) matches the
device_rollups precedent; beyond 30 d Q5 reads the day tier's busy-hour columns.

**`hist_port_day`** — 730 d, folded nightly from hours (the fold is WHY day rows
can carry busy-hour columns).
`org_id, device_id, if_index, day, samples, rate_n, in_sum, in_max, out_sum,
out_max, up_samples, busy_in_bps, busy_in_hour, busy_out_bps, busy_out_hour`
— `busy_*` = the max HOURLY MEAN and which UTC hour it fell in: the evening-peak
question at day grain for a year, two columns instead of twenty-four (Q5).
177/day; ≈ 129 k rows ≈ **12.9 MB**.
**Area bandwidth is the region head's UPLINK port** — the chart reads one port's
series; a sum of children double-counts (an uplink already carries its children).

**`hist_device_day`** — 730 d, folded nightly from `device_rollups` (before its own
30 d prune discards the hours). PK (device_id, day).
`org_id, device_id, day, samples, down_samples, latency_sum, latency_n, loss_sum`
— claimed by Q7 beyond 30 d and the month-scale "is this link slowly getting worse"
half of Q1. 57/day; ≈ 42 k rows ≈ **3.5 MB**. DEGRADED is deliberately not counted
(rollups don't carry it; perf episodes are recoverable from `alert_log`'s
PERF_DEGRADED rows if ever charted).

**`hist_radius_day`** — 730 d. PK (org_id, day).
`org_id, day, customers, active, expired, expiring7, linked`
— Q8. Upserted on each fully-`ok` sync (last of the UTC day wins); a partial or
failed read writes nothing — the gap is the record. ~7/day; ≈ 5 k rows ≈ **0.4 MB**.

No `hist_org_*` table: org-level curves are `SUM` over `hist_device_day` /
`hist_olt_*` at read time (57 rows/day — cheap SQL), and outage/MTTR history reads
the never-pruned `outages` table directly. Nothing per-ONU: 5,205 ONUs at even the
hour tier would be ~11 M rows/90 d — refused; Q9 is served by the PON percentile
curves plus the existing `rx_ref_dbm` drift figure, and the panel says so.

### Budget, caps, and what the disk actually pays

| table | rows/day | retention | steady size |
|---|---|---|---|
| hist_olt_sweep | 8,352 | 48 h | 1.4 MB |
| hist_olt_hour | 696 | 90 d | 5.3 MB |
| hist_olt_day | 29 | 730 d | 1.8 MB |
| hist_pon_hour | 3,912 | 14 d | 4.7 MB |
| hist_pon_day | 163 | 730 d | 10.1 MB |
| hist_port_sweep | 50,976 | 48 h | 7.1 MB |
| hist_port_hour | 4,248 | 30 d | 10.8 MB |
| hist_port_day | 177 | 730 d | 12.9 MB |
| hist_device_day | 57 | 730 d | 3.5 MB |
| hist_radius_day | ~7 | 730 d | 0.4 MB |

**Totals: ≈ 33 MB at day 90, ≈ 45 MB at 1 y, ≈ 58 MB steady at the 2-y horizon** —
roughly doubling today's 62 MB DB over two years. Per-unit scaling for fleet growth:
~300 KB per OLT, ~95 KB per PON, ~230 KB per eligible port at horizon.

- **Every table gets a hard row cap = 2× its expected steady rows**, enforced in the
  nightly prune after the age prune (delete oldest past cap). The age prune is the
  policy; the cap is the guarantee that unbounded growth is impossible even if a
  clock runs wild or eligibility explodes. Ring-buffer/newest-N precedents.
- **Nightly backups pay too** (`tools/backup.py` VACUUM INTO + compress, ~4 MB
  bundles today): numeric tables compress ~4–6×, so expect **+8–14 MB per bundle at
  horizon**. Stated so the snapshot-keep count can be revisited then, not discovered.
- Retention knobs are frozen `Config` fields (`WISP_HIST_RAW_HOURS=48`,
  `WISP_HIST_PON_HOUR_DAYS=14`, `WISP_HIST_PORT_HOUR_DAYS=30`,
  `WISP_HIST_OLT_HOUR_DAYS=90`, `WISP_HIST_DAY_DAYS=730`, `WISP_HIST_ENABLED=1`) —
  ops knobs, not display knobs, so NOT a Settings→Platform card (that constraint
  governs chart tunables; see Stage 2/4).
- **`alert_log` retention decision (flagged for the operator):** it feeds a Wave-1
  chart and grows unpruned (~25 MB/yr with index). Proposal: prune at 730 d to match
  the ladder — but `alert_log.recipient` is the "who was actually told" audit record,
  so this is the operator's call, not a default I'll bury in the migration.

### Migration + rollout

- New tables only → `_SCHEMA` append, nothing in `_ensure_columns`. One `meta` key
  `history_since` stamped at first init — every history endpoint ships it as
  `recording_since` so a young historian can say so (Stage 2's empty state).
- **The 15-minute trap applies**: the release-sync timer opens the live DB from repo
  source, so the schema edit is written and central restarted in the same breath —
  never left half-saved on this box. Rehearsed on the scratchpad copy first
  (`central-copy.db`), per the standing migration rule.
- Org-delete: introspection finds `org_id` in every hist table; pinned by a test
  that creates rows in all ten and deletes the org.

### Stage 1 tests

`unit/test_history`: percentile fold, per-PON grouping, epoch-hour/day floors,
missed-sweep-writes-nothing, NULL-rate on counter reset, eligibility predicate, hour
upsert running-sum math, nightly day fold (incl. busy-hour columns), catch-up fold
idempotence, caps. `integration/test_central_history`: a full /report with optics →
sample rows match the badge's numbers (count agreement at the WRITE side), endpoint
replies, org-delete sweep, `recording_since`, worker scoping (below).

---

## STAGE 2 — the grammar and the kit

**One primitive family** in `web/src/chart/` (the `map/` precedent — page-shared
logic in a module, not in components/), plus `notes/viz-grammar.md` shipped with it.
The four existing house charts are the seed, not competitors: `RxScale` and `OnuBar`
are instrument marks and stay as they are; `Sparkline`/`HourStrip`/`TrendSpark`
get re-implemented on the kit when Wave 1 touches their surfaces. Never two systems.

**Substrate:** d3-scale + d3-shape + d3-array as math only; React owns every SVG
element. Bundle cost measured: current 386 KB gz; the trio adds ~25–30 KB gz (~7%).
Accepted — no grammar conflict found, so the argue-out clause is not exercised.

Kit pieces (each a small component/helper, all consuming the same scale objects):
- `scale.ts` — time (UTC in, `WISP_DISPLAY_TZ` labels out via the existing format
  helpers) + linear scales; tick policy floors on epoch hours/days.
- `use-series.ts` — buckets → gap-broken segments (the Sparkline run/flush idiom,
  extracted ONCE) + memoization keyed on data identity so `useNow()`/SSE re-renders
  never recompute paths or replay mounts (the map icon-cache lesson).
- `time-chart.tsx` — frame: recessive axis/grid, plot area, crosshair + tooltip
  layer (on by default, per-mark hover on columns), legend chips (neutral text +
  coloured dot — the identity-chip grammar), the empty state, the frozen band, the
  dead zone, `EventRule` annotations.
- Marks: `LineMark`, `BandMark` (p10↔med envelopes), `ColumnMark` (counts/bucket),
  `StripMark` (HourStrip generalized to arbitrary ranges), `RuleMark` (decision
  boundaries — thresholds/ceilings, the RxScale precedent).

**The colour law, with the measurement that forces it.** Series colours are
`--chart-1..5` (the five planes) and status hues appear only where the mark claims
failure (a crit-count series IS such a claim and takes `--destructive`; a warn
series `--warning`). But the validator run on the planes (dataviz skill,
both-theme surfaces) returns a hard FAIL as a *categorical set*: adjacent-pair
ΔE 2.8–4.3 against a normal-vision floor of 15 — they were designed quiet (chroma
capped at ~55% of the quietest status tone, hues packed 200–330°) and cannot
separate series on one plot. So the grammar rule is:

> **One plane per plot.** The plane hue says what KIND of fact the chart shows
> (optical/traffic/vitals/plant/fleet); it never distinguishes series. Multi-series
> within a plot separates STRUCTURALLY — direct labels, weight/dash, small
> multiples, or emphasis (one series full, siblings in muted grey) — never by
> pairing plane hues. A two-series pair of the same kind (↓/↑ rate) is two
> lightness steps of the one plane, direct-labeled with the map's ↓/↑ vocabulary.

No new hues, no gradients, no glow. Legends: neutral text + coloured dot, always
present at ≥2 series, none for one.

**The five-state table is declared per chart** (each Stage 3 spec carries one), each
state with a non-colour channel, greyscale-safe:
- `current` — solid marks.
- `stale` — hollow last point + dotted connector to "now"; tooltip dates it.
- `frozen` — desaturated span + pause glyph + a reason OUTSIDE the plot ("frozen
  while <OLT> is down"), mirroring `.wisp-frozen`. Frozen data is never *sampled*
  (Stage 1), so frozen spans on charts come only from joining live state (isDownState)
  to the display — the store never holds a frozen sample.
- `absent` — the DEAD ZONE: a hairline track region where the axis runs but no
  instrument can answer (the OTDR concept `<Reading>` already uses). Absent is never
  zero; gaps break lines; nothing interpolates across a missing bucket.
- `suppressed` — struck-bell mark, only on alerting charts (a suppressed ALERT must
  never render like a suppressed FACT).

**Count agreement:** a chart's headline number is recomputed from the same rows the
adjacent tile/list shows and reuses `onuSev`, `isFresh`, `isDownState`,
`current_roster` — a chart never re-derives a verdict. **Endpoints:** bounded ranges,
SQL aggregation, one query per chart, no per-device fan-out; every reply carries
`recording_since` and the young-historian empty state renders "recording since
<date>" — never "no data". **Export behaviour** is part of the grammar doc (Stage 5).

---

## STAGE 3 — flagships

Each spec: the question+action (from Stage 0(c)), form and why it beats the obvious
form, five-state notes, endpoint, and the surface it lives beside. Worker visibility:
device-scoped endpoints ride `device_read_scope` (so a worker sees history only for
devices they see); org-wide charts (B, and org-level A) are owner-only.

### Wave 1 — history we already hold; shippable before the historian ages

**A. Reliability — device strip + org story** (Q1/Q2; action: aim the truck, show
the trend). Per device: a day-grain availability strip over 90 d/1 y — one cell per
UTC day toned by downtime fraction (destructive scale), neutral bordered cell for
"probe didn't report" (the HourStrip gap cell, stretched) — with outage `EventRule`s
carrying duration on hover. Beats an uptime-% line because 99.x% lines pin to the
top and hide clustering; the strip shows *patterns* (night flaps read as columns).
Org level: outages-per-week `ColumnMark` + median/p90 time-to-resolve as two
lightness steps of the fleet plane, direct-labeled; outliers annotated ("3 outages
> 24 h") rather than log-scaled. Five states: gap = probe-silent day (from
device_rollups coverage ≤30 d, else unknowable and rendered as plain absent);
frozen n/a (transitions, not readings); suppressed n/a. Endpoint:
`GET /api/history/reliability?device=|org=&days=` over `outages` (+ rollups for the
coverage row). Surface: device panel History fold (beside the existing HourStrip);
org panel on Home for owners.

**B. The governor's ledger** (Q6; action: re-enable a kind with its would-be volume
in view). Per-week `ColumnMark` of alert_log rows, split sent / suppressed / failed
(sent = fleet plane, suppressed = muted grey with the struck-bell legend chip,
failed = destructive — a failed page IS a failure claim), kind filter as
single-select chips (the Logs/issues pattern, fresh URLSearchParams). Beats a
per-kind multi-line: ~15 kinds spanning 4 orders of magnitude; the question is
always about one kind's sent-vs-suppressed ratio. NULL-kind era rows render as an
"(untagged)" chip, clipped ranges say so. Five states: suppressed is a first-class
DATA series here, not a mark state; gaps = no rows (true zero IS zero for counts —
a week with no pages renders a zero-height column, not a dead zone, because
alert_log rows are events, not samples; the distinction is stated in the spec).
Endpoint: `GET /api/history/paging?days=&kind=` GROUP BY day/kind/status,
owner-only. Surface: the Logs page, beside the alert log it explains.

**C. Triage latency** (Q2; action: staffing/escalation settings). Weekly median +
p90 of started→resolved and started→acknowledged, as median line + p10–p90
`BandMark` in the fleet plane; thresholds none (no invented SLA). Endpoint rides A's.
Surface: Home owner panel beside the outage list. (A and C share one endpoint and
one panel — they are the same table read twice.)

### Wave 2 — as the historian matures (gated on `recording_since`)

**D. Crit-ONU trend per OLT** (Q3; action: roll back the crew or close the ticket).
48 h view from `hist_olt_sweep`, 90 d from hour/day tiers: crit as a destructive
`LineMark`, warn as warning (both are failure claims), online/total as a quiet
optical-plane context line; the coverage row (`samples` vs expected 12/h) as a thin
`StripMark` under the axis — the probe-honesty channel (Q10); OLT outages as
`EventRule`s (count-agrees with the outage list). Gaps break lines — a down OLT's
unwalked hours are the gap, annotated by the outage rule beside them, which is the
five-state grammar doing the explaining. Beats a stacked area of ok/warn/crit:
stacking hides the crit line's absolute movement, and crit is the number acted on.
Endpoint: `GET /api/history/olt?device=&hours=|days=` behind `device_read_scope`.
Surface: device panel Optical tab, above the roster.

**E. Port / area bandwidth** (Q5; action: buy capacity, re-balance a region).
Per-port: ↓/↑ as two lightness steps of the traffic plane, direct-labeled with the
map's ↓/↑ vocabulary; `bw_max_mbps` ceiling as a `RuleMark` decision boundary
(RxScale grammar); ≤48 h at sweep grain, ≤30 d hourly, beyond that the day tier's
busy-hour columns render "evening peak per day" — which is what makes a year of
evenings legible on one screen. Counter resets are NULL-rate rows → gaps, never
negative spikes. "Area" is titled honestly: *this region head's uplink* — never a
sum. Endpoint: `GET /api/history/port?device=&if_index=&days=` behind
`device_read_scope`. Surface: the Ports tab's per-port expand; the region view can
link to the head's uplink chart (no new aggregation).

**F. PON Rx drift vs sibling median** (Q4; action: preventive truck roll to the
named splitter). `hist_pon_day`: this PON's median as a full-tone optical line with
p10↔med `BandMark`; the OLT's *other-PON median* as the muted-grey emphasis
baseline (structural separation, not a second hue); per-OLT warn/crit thresholds as
`RuleMark`s. Sibling comparison beats an absolute-budget chart for the same reason
`OUTLIER_DB` compares siblings: no vendor here publishes launch power, so absolute
is a guess wearing a decimal point. A PON whose `rx_med_n` is 0 (roster walks, no
dBm — the C-Data fleet) renders the dead zone with the `RxDiagnosis` sentence, not
an empty plot. Endpoint: `GET /api/history/pon?device=&pon=&days=` behind
`device_read_scope`. Surface: the Optical tab's per-PON drill.

---

## STAGE 4 — rollout: embedded beside the data it explains

This product's instinct, kept: every flagship above names the existing surface it
joins (device panel History fold, Optical tab, Ports expand, Home owner panel, Logs
page). **No dedicated analytics/history page in v1** — a chart earns a page only
when its surface can't hold it, argued per chart later. Chart display tunables
(default ranges, strip horizon) ship as constants; the first genuine "make it
denser/quieter" ask becomes a Settings→Platform card (the Map-detail rule:
a density ask is a dashboard control, not a code edit) — none is pre-built.

Order of work after approval:
1. Historian (Stage 1) — ships FIRST and alone; unrecorded days are unrecoverable.
   Migration + restart same breath; verify sampling on prod for a day.
2. Kit + grammar doc (Stage 2), re-implementing Sparkline/HourStrip surfaces on it.
3. Wave 1 (A/B/C) — no historian dependency, real data from day one.
4. Wave 2 (D/E/F) as retention fills: D at ~1 week of data, F at ~4 weeks — each
   renders honestly from day one because "recording since" is part of the grammar.

---

## STAGE 5 — exports

- **PDF** (`central/pdf.py`, pure stdlib): charts are VECTOR ops — the existing
  page/xref machinery gains `re`(rect)/`m l S`(path) helpers; the same five-state
  grammar renders as geometry (gaps break paths, dead zones hatch, thresholds are
  dashed rules); labels go through the cp1252 fold; nothing may raise inside a
  download. Never raster.
- **XLSX** (`central/xlsx.py`): a series exports as typed rows — real date cells
  (the 1899-12-30 epoch rule), REAL columns, one row per bucket, `samples` column
  included so coverage travels with the numbers. Never images.
- Scope: v1 exports = reliability (A) as PDF+XLSX riding the /issues export
  machinery; the rest follow demand.

## STAGE 6 — audit

On the prod copy, in a real browser, BOTH themes (the standing lesson: both palette
regressions passed static review):
- Measure every new tone/weight against `--destructive`/`--warning`/`--success` on
  the actual screens — nothing a chart draws may outshout an alarm; the plane-tint
  series must stay under the status tones (the two-axes contract).
- Re-run the palette validator on any chart that seats >1 coloured series; the
  documented FAIL means the structural-separation rule applies — the validator is
  the check that nobody quietly paired two planes.
- The ugly-data pass, all four fixtures: chandana's NULL-rx roster (974 of 1,741
  measured — dead zones, not empty plots); HALIYA-LAN-SW's 28-interface walk (port
  chart pickers stay bounded); a down OLT freezing everything behind it (frozen
  span + reason, gap in samples, outage rule all agreeing); a 3-day-old historian
  ("recording since", Wave-2 charts degrade to their honest short window).
- Count-agreement spot checks: chart headline vs the tile/chip/list beside it, per
  flagship.
- Charts inert under `useNow()`/SSE: React DevTools highlight pass — no path
  recompute, no animation replay on refetch.
