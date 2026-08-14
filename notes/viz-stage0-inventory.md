# Stage 0 — inventory: what history exists, what only exists as "now", what the operator asks

Verified against `store.py:_SCHEMA` and a read-only snapshot of prod taken 2026-08-14
(62.4 MB, 7 orgs, 57 gear devices + 66 passives, 29 OLTs, 163 PONs, 5,205 roster ONUs of
which 3,061 carry an Rx, 6,400 walked ports of which 177 are monitored/linked/thresholded).
Naming chart forms is out of scope for this document by instruction.

---

## (a) Durable history that already exists

For each: what clock writes it, how long it lives, what NULL/absence means, and what
question it can already answer. **Raw-payload holders are marked — the historian must
never duplicate those.**

### `outages` — 3,517 rows since 2026-07-02, NO prune (grows forever, 452 KB/6wk)
- **Clock:** the FSM, on transition only (open on DOWN-family entry, resolve on recovery).
- **Nulls:** `resolved_at NULL` = still open. `root_cause NULL` = no post-mortem yet
  (rapidnetworks: 1,188 of 2,896 resolved rows carry no cause — a real gap in any
  cause-breakdown question). `acknowledged_at` NULL = never triaged.
- **Answers already:** per-device and per-org downtime over any range (this is what
  `analytics.device_reliability` computes), outage frequency, time-to-resolve, wave
  shapes (15-min start-gap grouping, the `incidents.py` rule), triage latency
  (started→acknowledged), cause mix where entered. **This table is the whole Wave-1
  reliability story and needs no historian help.** rapidnetworks alone holds 2,896
  outages in six weeks — a genuinely flappy fleet with real material in it.
- Note: durations must be computed `resolved_at − started_at` with `core/analytics._parse`
  (both stamp formats live in the wild).

### `alert_log` — 27,988 rows since 2026-07-02, NO prune (2.9 MB + 1.2 MB cooldown index /6wk)
- **Clock:** every emit through dispatch / the governor / the watchdog, one row per
  attempt, including the ones that did NOT send.
- **Nulls:** `kind NULL` = rows from before the column existed (4,915 rows) — any
  by-kind series must bucket them as "(untagged era)" or clip its range to where `kind`
  starts, never silently drop them. `outage_id`/`device_id` NULL = page not tied to a
  device (probe/billing/admin).
- **Status vocabulary is the governor's story in one column:** on prod today —
  PON_FAULT `suppressed` 4,594 vs DEVICE_DOWN `sent` 2,645, DIGEST `failed` 3,808,
  PORT_DOWN `sent` 212. "What would page if I re-enabled optics kinds" is answerable
  RIGHT NOW from `suppressed` rows.
- **Answers already:** paging volume by kind/status/day, per-org noise level, the
  sent-vs-suppressed ratio the `_ACTIVE_KINDS` decision rests on, failure rate of the
  WhatsApp channel.
- **Holds near-raw:** `payload` TEXT (the notification body). The historian must not
  copy anything out of it; counts come from GROUP BY, at read time.
- **Finding:** unbounded and unpruned. Cheap today; a retention decision belongs to
  Stage 1's write-up (it is the input to a flagship chart, so its lifetime becomes
  load-bearing).

### `escalations` — 3,068 rows, NO prune
- **Clock:** hourly re-broadcast rows per open outage, `UNIQUE(outage_id, kind)`.
- **Answers already:** how long outages stayed loud; joins `outages` for "escalated
  N times before anyone acked".

### `device_rollups` — 32,096 rows, hourly buckets, **30-day retention** (`rollup.py`)
- **Clock:** folded on every FULL report cycle (never a recheck); running sums per
  (org, device, epoch-hour); averages computed at read (`device_rollup_series`).
- **Nulls/absence:** `latency_count` < `samples` = some polls had 100% loss. A missing
  bucket = the probe did not report that hour — the SPA (`HourStrip`) already renders
  that as an empty bordered cell, which is the gap grammar working.
- **Answers already:** per-device latency/loss/down-fraction at hour grain for 30 days
  — "what does a normal Tuesday look like" is answerable for the last 4 Tuesdays,
  today, from this table.
- **Limitations:** DOWN-family only (`down_samples`); DEGRADED is not counted. Sums
  only — no percentiles, so an hour's p95 is unrecoverable. Dies at 30 days: any
  month-over-month question needs the historian to fold a daily row **before the
  prune discards the hour**.

### `device_perf_samples` — ring buffer, newest `cfg.perf_window`=20 rows per device
- Deliberately NOT history (the docstring says so): the intra-hour slowdown window for
  `evaluate_perf`. 1,140 rows = 57 devices × 20. **Answers:** the Sparkline behind the
  panel. Nothing longer. Leave it alone.

### `worker_locations` — 7-day prune, currently **0 rows**
- The tracking feature is live but no worker transmits yet. Retention IS the privacy
  contract — the historian must not extend or aggregate it (an aggregate of staff
  movement outlives the window's promise). Excluded from everything downstream.

### `proxy_audit` — 151,197 rows, 60-day lazy prune, **the biggest table in the DB (17 MB + 3.4 MB index)**
- **Holds raw:** one row per proxied request (path, status, ts). It has been this
  codebase's best forensic instrument (found the 739-hit onumacinfo workflow, the
  1.00s SYN-retransmit signature). Read-only input for ad-hoc analysis; the historian
  duplicates nothing out of it.

### `snmp_walks` — newest-10 per device, 233 rows, 7 MB
- **Holds raw:** varbind dumps. Bounded by design. Never duplicated.

### `radius_customers.seen_seq` / `first_seen_at` / `last_seen_at` — 8,263 rows, never deleted
- **Clock:** each panel sync stamps its own counter; rows missing from the latest read
  are kept (a filtered export must not read as churn).
- **Answers already:** "when did this customer first/last appear in billing" per
  customer. NOT a time series: `status`/`expiry`/`package` are overwritten in place,
  so "how many were expired last month" is unanswerable today (see (b)).

### `onu_user_macs` — 48,902 rows, never deleted, `first_seen_at`/`last_seen_at`
- **Answers already:** per-slot address history ("this router appeared 2026-08-02").
  Learned-table semantics; the stale row is the feature. No aggregation wanted.

### `events` — 9,394 rows, NO prune
- Central-originated log lines. Feeds the Logs page. Not a metrics source.

### Small transition stores that carry `since` but no episodes
`pon_fault_state`, `pon_capacity_state`, `onu_dup_mac_state`, `olt_optics.alarm_since`,
`device_perf.since`, `switch_ports.alarm_since`/`bw_alarm_since`: each remembers the
CURRENT episode's start and forgets the previous one on recovery. They answer "since
when", never "how often". (Episode history for the paged kinds is recoverable from
`alert_log` sent/suppressed pairs; for the rest it does not exist.)

### `onu_optics.rx_ref_dbm` / `rx_ref_at` — the one drift record that exists
A rolling ~7-day-old reference per ONU, refreshed when it ages out. Answers "is this
ONU weaker than last week" for the panel — one number, no curve. It is the ceiling of
what per-ONU questions can be answered without per-ONU storage, and Stage 1 keeps it
that way.

---

## (b) Current-state-only data where history must start accumulating

Each of these is overwritten in place on its own clock. Yesterday is unrecoverable.
**Every unrecorded day stays unrecorded — this list is why the historian ships first.**

| Data | Where it lives now | Overwritten by | Clock |
|---|---|---|---|
| Per-ONU severity/rx/state | `onu_optics` upsert (`sync_device`) | every optics walk | `gpon_interval_s` 300s (+ web scrape 900s merged in) |
| Per-OLT counts (total/online/warn/crit) | `olt_optics` one row per OLT | every walk | 300s |
| Per-PON composition (total/online/dark per `pon_port`) | derived from `current_roster` on demand, stored nowhere | every walk | 300s |
| Port oper state + `in_bps`/`out_bps` | `switch_ports` upsert | every port walk | `port_interval_s` 300s |
| Device CPU/RAM/temp | `device_health` one row | every health walk | `snmp_interval_s` 300s |
| Fleet state counts (N up / degraded / down per org) | `device_states` current row per device | every report cycle | 60s |
| RADIUS runway (active/expired/expiring-soon counts) | derived from `radius_customers.status`+`expiry` on demand | every sync overwrites `status` | `radius_interval_s` 3600s |
| Measured-coverage ("N of M ONUs carry a dBm") | `list_org_devices.onus_rx`, computed | every walk | 300s |

Schema-verified notes:
- `switch_ports` rates: `throughput_bps` already returns **None on a negative delta**
  (`ingress/snmp.py:75`), so a counter reset/reboot reads as "no rate" today — the
  historian inherits wrap-safety by sampling the computed rate, not the counters.
- Fleet state counts at hour grain for ≤30d ARE recoverable from `device_rollups`
  (down fraction only, no DEGRADED). Beyond 30d, nothing.
- Per-PON dark counts: `ponfault`/`onuroster` recompute from the freshest walk each
  time; `pon_fault_state` keeps only the current episode.

---

## (c) Operator questions, ranked by operational value

Grounded in real faults and real months on this fleet. Ranking = (how often the
question comes up) × (whether an action hangs on the answer). Each question names the
action, because a chart with no action is decoration (hard constraint 7).

1. **"Which devices flap, and is it getting better or worse?"** — rapidnetworks logged
   2,896 outages in six weeks; nobody can currently see whether this month is worse
   than last. *Action: prioritise which site gets the truck/UPS/re-parent; show the
   ISP their money changed something.* (Answerable from `outages` today — Wave 1.)
2. **"How long do we take to fix things, and is that improving?"** — time-to-resolve
   and time-to-acknowledge per org/month. *Action: staffing and escalation-setting;
   the owner's own KPI.* (From `outages` — Wave 1.)
3. **"Did crit ONUs spike after that splice / storm / change?"** — 269 crit + 625 warn
   ONUs on the fleet right now; after a splice the tech needs before/after at OLT and
   PON grain. *Action: roll the crew back or close the ticket.* (Needs historian:
   per-OLT/per-PON counts over time.)
4. **"Is this PON degrading week over week?"** — the slow-death case: water in a
   closure drops a PON's median Rx 0.5 dB/week for a month before subscribers call.
   *Action: preventive truck roll to the named splitter, before the outage.* (Needs
   historian: per-PON rx percentiles at day grain.)
5. **"Which uplink saturates in the evening, and how close to the ceiling?"** —
   PORT_BW_HIGH paged 135 times; evening peak is the capacity-planning fact. *Action:
   buy bandwidth / re-balance a region before complaints.* (Needs historian: per-port
   rates; the region head's UPLINK port is the area's number — never a sum of children,
   which double-counts.)
6. **"What did the governor eat?"** — 4,594 suppressed PON_FAULT pages vs 2,645 sent
   DEVICE_DOWN. Re-enabling a kind in `_ACTIVE_KINDS` is a one-line decision currently
   made blind. *Action: turn a kind back on (or not) with its would-be volume in
   view.* (From `alert_log` — Wave 1.)
7. **"What does a normal Tuesday look like?"** — the baseline against which "is
   tonight weird" is judged: latency by hour, subscribers online by hour. *Action:
   distinguishes an incident from an evening; feeds the witness/power story.*
   (≤30d: `device_rollups` today; longer + ONU-online curve: historian.)
8. **"Is the paying base drifting away?"** — rn_giga_fiber holds 721 active vs 2,610
   expired customers; the runway (expiring ≤7d) moves weekly. *Action: collections
   push; the customers page shows today's number, no trend.* (Needs historian: daily
   counts per org.)
9. **"Was this drop already sick before the complaint?"** — per-ONU history. **Refused
   as storage** (5,205 ONUs × any useful cadence dwarfs the DB; see Stage 1 budget) —
   answered by proxy: the PON's percentile curve + the existing `rx_ref_dbm` drift
   figure + `last_online_at`. The panel says which proxy it is showing.
10. **"Did the probe itself misbehave?"** — sweep-coverage over time (samples-per-hour
    against expectation) as the honesty backstop under every other chart. *Action:
    fix the edge, not the network.* (Falls out of the historian's `samples` columns
    for free — never a separate collector.)

Questions deliberately NOT served: staff-movement analytics over time (the 7-day
`worker_locations` window is a privacy promise, not a budget), per-request proxy
latency trends (`proxy_audit` stays ad-hoc forensic), and anything per-subscriber
beyond what (9) states.
