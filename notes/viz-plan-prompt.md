# Prompt: historical analytics + data visualization plan

Paste the block below into a fresh Claude Code session (plan mode).

---

We're adding historical analytics + data visualization. The dashboard today is
live-state only — it answers "what is broken now" and nothing about yesterday.
Plan in stages; no production code until approved. Work against a copy of prod.

READ FIRST, all binding:
- Invoke the `dataviz` skill.
- CLAUDE.md: "THE TWO COLOUR AXES", "The instrument grammar", "Honesty rules",
  "THE MARK SCALE", "Theme & palette", the SNMP section (the clocks), "Rollups".
- House charts: sparkline.tsx, HourStrip, RxScale, OnuBar. Grow these into one
  family; never ship a parallel system.

STAGE 0 — inventory, three lists (a document; naming chart forms is forbidden):
 a) DURABLE HISTORY THAT EXISTS: outages, alert_log, escalations, rollup.py
    hourly trend, device_perf_samples, field trails, proxy_audit,
    radius_customers seen_seq, onu_user_macs first/last_seen. For each: clock,
    retention, null semantics, what question it can already answer. Note which
    of these hold raw payloads — the historian must never duplicate those.
 b) CURRENT-STATE-ONLY DATA WHERE HISTORY MUST START ACCUMULATING: onu_optics
    severity/rx (overwritten per sweep), switch_ports rates, PON dark counts,
    fleet state counts, radius expiry runway. Verify against the schema.
 c) Operator questions from real faults and real months, ranked by operational
    value: e.g. "is this PON degrading week over week", "which area saturates
    its uplink in the evening", "did crit ONUs spike after that splice",
    "what does a normal Tuesday look like".

STAGE 1 — THE HISTORIAN (ships FIRST — unrecorded days are unrecoverable):
 A bounded time-series layer. Hard rules:
 - STORE ANSWERS, NEVER EVIDENCE. The historian stores derived numbers a named
   chart reads — counts, rates, percentiles — never raw material (walk dumps,
   HTTP bodies, rosters, whole responses). Debug raw stays in its existing
   bounded homes (snmp_walks newest-10, proxy_audit) and is not duplicated.
 - Every column is claimed by a Stage 0(c) question. A column no pixel
   consumes is not written. "Might be useful later" is not a question.
 - Numbers only, wide rows: INTEGER/REAL columns, one row per
   (org, device, sweep) with count columns. No key/value rows, no JSON blobs,
   no formatted strings.
 - Samples are a BYPRODUCT of sweeps that already run. Zero new SNMP load,
   zero new walk clocks. Sample the folded truth AFTER every gate (rail guard,
   freshness, merge) — one path, same numbers the panels show.
 - Aggregates, not individuals: per-OLT/per-PON severity counts + rx
   percentiles (median/p10/worst); per-port rate per walk, hourly-rolled;
   per-org fleet state counts. Justify any per-individual series against a
   computed disk budget (SQLite + nightly backups — size is a real cost here).
 - A missed sweep writes NOTHING; a frozen/stale reading is NEVER sampled.
   The gap is the record. This is the five-state grammar applied to storage.
 - Change-only (transition) encoding is allowed ONLY with an explicit
   covered-through stamp — without one, "no row" is ambiguous between
   "unchanged" and "not measured", which destroys gap semantics. Default is
   fixed-cadence at the hourly/daily tiers; argue per table.
 - Ladder: raw ~48h → hourly ~90d → daily ~1-2y, pruned daily (rollup.py /
   field-prune precedent). State expected rows/day and MB/year per table, and
   give every table a cap that makes unbounded growth impossible (ring-buffer
   / newest-N precedents). The disk has been quietly filled before.
 - Area bandwidth = the region head's UPLINK port, never a sum of children
   (an uplink already carries its children's traffic; a sum double-counts).
 - Counter mechanics: rates from ifHC 64-bit counters where walked; handle
   wrap/reset (a reboot must read as a gap, not a negative spike).
 - Every table carries org_id (the org-delete sweep introspects for it);
   migration + central restart in the same breath.

STAGE 2 — the grammar and the kit:
 One doc + ONE primitive family (the wisp-* precedent): axis, scale, gap,
 frozen band, dead zone, decision-boundary mark, annotation, legend chip,
 empty state ("recording since <date>" — a young historian must say so, never
 render as "no data"), export behaviour. Substrate default: d3-scale/d3-shape/
 d3-array as math, React owns the SVG — argue out of it only with bundle
 numbers and a concrete grammar conflict.

HARD CONSTRAINTS on every chart (from the existing system):
 1. Series colours are --chart-1..5 (the five planes). Status hues only where
    the mark claims failure. No new hues, gradients, glow. Legends = neutral
    text + coloured dot.
 2. Five-state table (current/stale/frozen/absent/suppressed) declared per
    chart, each state with a non-colour channel. Gaps break lines. Absent is
    a dead zone, never zero.
 3. Count agreement with the tile/chip/list beside it; reuse onuSev, isFresh,
    isDownState, current_roster — never re-derive a verdict.
 4. UTC stored, WISP_DISPLAY_TZ displayed, epoch-hour bucket floors.
 5. Bounded endpoints, SQL aggregation, no per-device round trips. Charts
    inert under useNow()/SSE — memoized paths, no animation replay on refetch.
 6. Tunables are Settings → Platform controls (Map detail precedent).
 7. Every chart names the operator question + the action taken. No action =
    decoration = cut. No pies, no 3D, no count-ups, no delta-arrow filler.

STAGE 3 — flagships in two waves:
 Wave 1 (history we already hold — shippable now): outage/reliability timeline
 per device and org; paging volume by kind over time (the governor's story);
 MTTR and outage-wave views from the outages table.
 Wave 2 (as the historian matures): crit-ONU trend per OLT with sweep gaps and
 frozen spans honest; per-port and per-area bandwidth over time with evening-
 peak legibility; PON rx drift vs sibling median.
 For each: form, why it beats the obvious form, five-state table, endpoint,
 and which existing surface it lives beside.

STAGE 4 — rollout: embedded beside the data it explains (map, panels, tabs) —
 this product's instinct. A dedicated analytics/history page must be justified
 per chart, not assumed.

STAGE 5 — exports: pdf.py is pure stdlib — PDF charts are VECTOR ops with the
 same five-state grammar, never raster. xlsx gets typed cells, never images.

STAGE 6 — audit: measure new tones/weights against status tones in BOTH themes
 on real screens (nothing outshouts an alarm). Verify on the prod copy with
 the ugly data: NULL-rx fleet, 28-interface OLT, a down OLT freezing
 everything behind it, and a historian only 3 days old.
