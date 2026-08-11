# CLAUDE.md

Invariants and gotchas that aren't obvious from the code. What/how/layout/config lives in
`README.md`. Verify claims about what's done against the code — stale docs drift.

Each bullet is a rule plus the reason it exists. **The reason is load-bearing:** most were
paid for by a field incident, and without the why the next session redoes the thing that
broke. Where a test pins an invariant it's named — that's the cheap way to check you haven't
undone it.

## Architecture

Central runs the brain for every org: FSM, topology-aware suppression, fast-confirm, the
alerting ladder, the multi-org dashboard, fleet version/rollout state. The edge is a thin
probe with exactly one mode (`WISP_CENTRAL_URL` mandatory): fetch topology, probe ICMP under
bounded fan-out, report raw per-IP samples, heartbeat its version. No local DB, dashboard, or
FSM on the edge.

Central (`central/server.py`/`store.py`/`auth.py`/…) is pure stdlib. Its dashboard is a
build-time React/TS/Tailwind/shadcn SPA (`web/` → built into `central/static/`; Node is
dev-only, the committed build is what deploys). Edge needs a `.venv` (`icmplib`/`httpx`;
system Python is PEP 668-locked) + `sysctl net.ipv4.ping_group_range="0 2147483647"`.

**Locked decisions (don't relitigate):** brain on central, always; monitor shared infra, not
end-user routers; WhatsApp (Meta Cloud API) is the SOLE notification channel since 2026-07-24
(ntfy removed), recipients are per-account E.164 numbers; every read/write
org-scoped; edge dials central, never the reverse; updates pull-based over the report
channel, staged + health-gated; probers/notifiers behind interfaces, tests inject doubles.

## Imports & paths (the main trap)

Absolute imports under `wisp.*`; src layout, nothing installed. `apps/daemon/main.py` and
`apps/central/main.py` prepend `<repo>/src` to `sys.path`; the admin CLI needs
`PYTHONPATH=src`; tests bootstrap their own path. `config.PROJECT_ROOT` = repo root
(`parents[2]` of `config.py`); `central_db` defaults to `data/central.db`; the SPA resolves
from `central/static/`.

## Engine invariants

- `core/state_machine.py:MonitorEngine` is **pure** — `{ip: PingResult}` + ts in, states +
  `Event`s out, no I/O. Central owns build/rehydrate/persist (`central/engine.py`).
- `process_cycle(results, ts, subset=None)`: `None` = full pass (all devices + canary/uplink
  + freeze); a `set[int]` = confirmation pass (those FSMs only, topo order, skips
  canary/uplink). Keep the full-pass path byte-identical.
- `probe_plan()` is a reference the edge approximates (`_gentle_probe_plan`), not something
  central calls. Known gap: it counts a BACKUP parent as infra; the edge can't
  (`GET /edge/devices` carries only `parent_device_id`).
- `central/dispatch.py:CentralAlertDispatcher` sends OUTSIDE any DB transaction — a slow API
  call must never hold a write lock.
- Prober/Notifier live behind `build_prober`/`build_notifier`; new providers go behind them.
- **Windows probes via `SingleSocketIcmpProber`, never icmplib** (picked by `sys.platform`;
  `WISP_PROBER` forces one). Windows raw sockets are promiscuous — N sockets each see every
  reply (O(N²)) and asyncio stamps arrival at coroutine-read time (~150ms floor). Fix: ONE
  shared raw socket + ONE receiver thread stamping `perf_counter()` right after `recvfrom`,
  matched by ICMP id (pid-derived) + seq + reply source IP. Linux keeps icmplib's
  unprivileged datagram sockets (raw would need root and break the ping-group invariant).
  Tests: `unit/test_probers`.

## Scaling invariants

- **Probe fan-out is bounded** by `Semaphore(cfg.probe_max_inflight)` (256). Unbounded gather
  past `ulimit -n` reads as a fake mass outage (socket refusals masked as 100% loss).
- **Aggregation gear is probed gently**: parents get `pings_per_poll_infra` (2), leaves +
  canary `pings_per_poll` (5) — or ICMP rate-limiters read as phantom loss.
- **Fast-confirm is central-driven**: `engine.compute_recheck` names suspect IPs in the
  `/report` reply; the edge re-probes just those every `WISP_RETRY_INTERVAL_S`
  (`mode="recheck"`) until the hint is empty. A frozen cycle (canary down) yields no hint.
- **Adaptive cadence** (`Config.effective_interval`): 30s while fleet ≤ 1000 and
  `poll_interval_adaptive` on (off by default), else 60s — computed once at startup. EXCEPT
  the org override `orgs.poll_interval_s`, which rides the `/edge/devices` reply and
  re-applies every cycle, no restart. Precedence: CLI `--interval` > org value > env/adaptive.
  **Clamped 10–120s on BOTH sides** — past 120s a healthy probe outlasts the watchdog's 180s
  NODE_STALE threshold and pages as dead. NULL = automatic.

## SNMP: background, three clocks, one airtime gate

- **Background asyncio task, never inline in the probe cycle** (inline walks once made the
  edge report every 4 minutes). Ports attach to full reports only, never recheck.
- **Three separate walk CAPS, not one**: health `snmp_walk_timeout_s` (20s),
  `port_walk_timeout_s` (60s, ifTable), `gpon_walk_timeout_s` (75s, ONU roster). A
  200+-interface OLT can't finish in 20s, and timing out leaves that table permanently stale
  while the smaller walks stay fresh on the same box. Field-diagnosed 2026-07-09; don't
  collapse them.
- **Three separate sweep CLOCKS too** (2026-07-17): `snmp_interval_s` is health AND the
  master gate (`<=0` = all SNMP off); `port_interval_s`/`gpon_interval_s` ride their own. All
  default 300s — don't raise past ~600s without revisiting the 900s staleness gates that
  freeze roster/port alert state. Until the split, one 90s clock fired all three and the next
  sweep waited on ALL THREE: a slow roster walk starved the ifTable walk and (next_snmp
  stamped at sweep START) re-fired immediately, walking that agent back-to-back all day.
  HILL-OLT-1/PYLON sat at 0% port-walk success while optics stayed fresh — **the polling
  caused the failure.**
- **One `_SnmpAirtime` gate spans every SNMP subsystem** (ports/health/optics +
  `_DiagWalkRunner`): a fleet-wide `Semaphore(snmp_max_inflight)` (4, global not
  per-subsystem) PLUS a per-device lock, because equal 300s periods make the clocks fire on
  the same tick every sweep. Device lock acquires BEFORE the semaphore (waiting on a busy box
  must not pin a fleet slot). `SharedAirtimeGateTest`.
- **Per-request patience matches gpon's**: ports/health/diag use `snmp_request_timeout_s`
  (5s) × `snmp_request_retries` (3), not the bare 2s × 1. Weak agents answer whoever retries
  longest; the strict window got ZERO responses for 26h fleet-wide while optics (already
  patient) stayed fresh on the same boxes. Don't "optimize" the retries back down.
- **One `SnmpEngine` per poller instance, NEVER per walk** — a per-walk engine leaks ~1 MiB +
  one FD forever (transport stays registered with the loop), and FD exhaustion reads as a
  fake mass outage. `PysnmpPoller`/`PysnmpGponPoller` lazily reuse `self._engine` (concurrent
  walks are safe — request-id demux). `EngineReuseTest` in `unit/test_snmp` + `unit/test_gpon`.
- **These agents QUIT a GETBULK mid-table and pysnmp reports a clean finish** (2026-08-03).
  Proven over every diagnostic walk against badri_fiber: EVERY early stop landed on an exact
  multiple of the 25 max-repetitions (400/800/2275/2475/5550/5700), EVERY genuine end did not
  (194/180/8/3). The generator just ends, so nothing raises and `truncated` stayed 0 — a walk
  missing whole columns stored as complete. That is why a whole-PEN walk "proved" there was no
  ONU roster in July. The budget is per-RESPONSE, not a fixed count (same root: 2275 on one OLT,
  2475 on the other; the slow DDM table dies far sooner than the static ones), so **no cap value
  makes a broad walk safe** — `walker.py` RESUMES from the last OID until the agent proves it is
  done. **`lexicographicMode` MUST be True to resume**: with it False pysnmp bounds the walk to
  the SUBTREE OF THE START OID, so resuming from a leaf returns nothing — which reads as proof
  the agent had no more data. The subtree bound is enforced by our own prefix test instead; the
  varbind cap still bounds a resumed walk. `unit/test_walker`.
- **An ABSENT state cell is `unknown`, never `state_default`** (`gpon._metric_state`).
  `parse_onu_table` builds rows from whichever columns arrive (`_place` does `setdefault`), so a
  truncated STATE column left the tail ONUs with no state → the default → **`offline`**: live
  subscribers rendered dark and `ponfault` counting them as a mass-drop cohort, i.e. a fabricated
  fibre cut with nothing reporting an error. `ponfault.DARK_STATES` ALREADY excluded `unknown`
  ("a vendor decode gap must not read as an outage cohort") — the contract existed, the edge
  never produced it. A profile mapping NO state OID still gets its default: an absent column is a
  fact about the firmware, a missing value in a column that EXISTS is a fact about the row. Only
  bites profiles whose default is `offline` (the HUAWEI built-in already defaulted to unknown).
- **The DDM sensor-rail guard covers SNMP too, and lives in `optics.py`** — `sane_rx`/`sane_tx`,
  imported BY `weboptics` so there is ONE definition on the one path every reading crosses
  whatever transport carried it. The Syrotech GPON build reports `0.00` across the whole DDM
  block for a dark ONU, so 114 of badri_fiber's 378 ONUs stored 0.0 dBm and counted as MEASURED
  in `onus_rx` — "nothing is wrong" and "nothing is measured" rendering alike. Keyed on PHYSICS,
  not state: blanking whenever an ONU is offline would throw away the last good reading the panel
  legitimately shows, and would miss the ONLINE ONU also reading 0.0 (a dead sensor grading `ok`).
  **Rx and Tx are asymmetric ON PURPOSE** — 0.0 dBm RECEIVED is impossible, 0.0 dBm TRANSMITTED is
  an ordinary launch power. `sane_tx` is deliberately weak and says so: it does NOT catch the
  +8.16 dBm high rail, which needs the supply-VOLTAGE column no SNMP profile maps yet.
  `DdmRailOnTheSnmpPathTest`.
- **pysnmp 7 walk commands take exactly ONE varbind** — passing ten ifTable columns to
  `bulk_walk_cmd` positionally is a TypeError swallowed as "walk failed" (froze every fleet
  port table for 30h in v0.15.3; only `bulk_cmd` takes `*varBinds`). The combined walk is
  `MultiColumnWalk` over raw `bulk_cmd`, and ANY failure falls back to per-column
  `bulk_walk_cmd` — keep the fallback; port coverage must never ride on one optimization.
  `CombinedWalkDriverTest`.
- **`PysnmpPoller` is ADAPTIVE and vendor-agnostic** (2026-07-19): weak C-Data OLTs serve
  health+optics but drop the heavy combined ifTable GETBULK. A per-device ladder walked from
  the gentlest level that last WORKED — 0 combined (time-boxed to `_FAST_PATH_MAX_S`=15s so a
  big box hands the rest of the budget to the net), 1 per-column/25, 2 per-column/4 —
  with `_device_level`/`_promote_at` persisting across sweeps and re-probing one rung faster
  every 6h so a firmware change self-heals. NO vendor hardcode. **The per-column net is
  TOLERANT**: a dropped column is skipped, never fatal (the old raise-on-first-drop is why
  small OLTs no_responsed though the same box's health walk succeeded); status columns first
  so a budget-bounded partial still yields port up/down. A genuinely dead agent still
  RE-RAISES (keeps the no_response classification); an agent with no ifTable returns `[]`.
  `AdaptivePortWalkTest`.
- **Unit tests inject fake pollers, so a bad HLAPI call only surfaces on a REAL walk** —
  verify `device_snmp_status` after any edge SNMP rollout.
- **Remote walks: the edge is central's hands, poll-only.** Queued from the dashboard,
  delivered in the next FULL `/report` reply under `snmp_walks` (the edge NEVER accepts
  inbound), run by a sequential `_DiagWalkRunner`, result POSTs to `/edge/snmp-walk`. Pending
  walks re-deliver until a result lands; one pending per device; newest 10 kept. **The runner
  refuses any target IP not in the node's device list** — no lateral-movement primitive.
  Server double-bounds upload size. A walk stopped at the varbind cap or 60s budget carries
  `truncated` to the dashboard row: a partial dump that looks complete turns "that OID holds
  nothing" into a false negative costing a vendor-onboarding session. A narrower root is the
  fix, never a bigger cap.
- **Vendor health profiles are DATA, not edge code** (`snmp_profiles`; org_id NULL = global).
  Metric → OID + decode from a CLOSED vocabulary (`as_is/div10/div100/signed_div100`, select
  `first/avg/max/sum`). `ingress/health.py` walks `sysObjectID`, matches by LONGEST prefix,
  walks profile OIDs BEFORE standard MIBs, fills only fields still None; hardcoded
  MikroTik/Fiberhome fallbacks stay for fleets on an older central. Onboarding a vendor = a
  profile row, never a rollout. Keep the vocabulary tiny.

## Central runs the brain

- `central/engine.py` is the only DB glue around the unchanged `MonitorEngine`;
  `central/dispatch.py` is the alerting policy (dedupe per outage, owner+worker on open, both
  on resolve, ack never stops it — only recovery does).
- **`EngineRegistry`: one live engine per org** (flap streaks accumulate across stateless
  `/report` calls). Rebuilds only when the fingerprint `(id, parent_device_id, d.parents)`
  changes; rehydrates from `device_states`. Breaking rehydration re-pages everyone on restart.
- **Wire format is IP-keyed**: `POST /report` `{"v":1,"org_id","node_id","ts","mode":"full"|
  "recheck","pings":{ip:{loss_pct,latency_ms,jitter_ms}}}`. The edge never sees device ids.
- **Escalation sweeping rides the report cadence** — `sweep(ts)` once per full `/report`,
  scoped to that org. Stalls if an edge goes stale; the watchdog pages for that separately.
- **The heartbeat is the self-update channel, not liveness.** Reply may carry an `update`
  directive (`central/rollout.py`) written ATOMICALLY as `update_request.json`, or
  `restart: true` (one-shot `nodes.restart_pending`). Liveness is `touch_node` off `/report`;
  a failed heartbeat is a warning, never a crashed cycle. DELIVERY clears the restart flag: a
  directive lost in flight means the operator clicks again, never a loop.
- **Auto-update is org-opt-in** (`orgs.auto_update`): `maybe_auto_rollout` arms the SAME
  staged canary rollout the manual button uses. A HALTED rollout for the same target is NEVER
  auto-retried — a build that failed its health gate re-arms only via a human's Retry; `done`
  re-arms freely.

### The notification governor (the choke point)

`central/notify_policy.py`. Every paging shell (dispatch / ponalert / onualert / ports / perf
/ redundancy / optics) routes send + status + log through `AlertRouter.emit(kind, …)` rather
than calling the notifier inline.

**Since ntfy was removed (2026-07-24) only an ALLOWLIST of kinds pages** — `_ACTIVE_KINDS =
{PORT_DOWN, PORT_RESTORED}`. Device/uplink up/down go straight through `dispatch.py` (NOT the
governor) and probe up/down through the watchdog, so the operator's chosen set is
**device / uplink / port / probe, each up and down**. EVERYTHING ELSE is turned OFF "for now"
(optics/PON/ONU/perf/backup/port-bandwidth/hourly-escalation): `emit` logs the kind
`suppressed` and sends nothing. **Re-enable a kind by adding it to `_ACTIVE_KINDS`** — one line,
the single knob.

The PUSH/DIGEST two-tier machinery (`_DIGEST_KINDS`, `queue_digest`/`flush_digests`,
`compose_digest`, `cfg.digest_interval_min`) is INTACT but DORMANT — no active kind routes to
the digest. Kept so re-enabling a digest kind is just the allowlist edit (if it's also in
`_DIGEST_KINDS` it resumes queuing). PUSH cooldown backstop = `cfg.alert_cooldown_min` per
`(device,kind)` (ports pass 0 — per-if_index, already gated). **State rows are written by the
shells regardless of the allowlist** — the dashboard stays fully live; this governs only the
notification. `tier_for` still classifies every kind.

### WhatsApp is the SOLE notification channel

ntfy was REMOVED 2026-07-24. `build_notifier(cfg, store)` returns a bare `WhatsAppNotifier`
(Meta WhatsApp Cloud API) — no more `MultiNotifier`/`NtfyNotifier`; `alert_log.channel` is
always `whatsapp`.

- **`send(title, body, priority=3, *, whatsapp=…, facts=…)`** — the ntfy `recipient` positional
  is GONE. A page's audience is a list of E.164 numbers; `WhatsAppFacts(subject,status,detail,
  timestamp)` fills the approved 4-param template (default `wisp_alert1`:
  Device/Status/Detail/Time Logged, param order == `WhatsAppFacts.params()`). Only `wisp_alert1`
  is needed — it carries device/port/probe pages, admin pings, and a compacted digest.
- **A send can never crash the report cycle** — every WhatsApp failure returns
  `NotifyResult(False,…)` and NOTHING raises (same discipline as when it was secondary); the
  result now drives the logged sent/failed status. `send_with_retry`: network/5xx retry, 4xx
  fail-fast, `cfg.notify_retries`/`notify_retry_backoff_s` (RENAMED from the ntfy_* ones).
- **ONE audience, NO role routing** (operator choice 2026-07-24): every alert reaches
  `store.org_alert_recipients(org)` = owner + worker per-account numbers
  (`users.whatsapp_number`), de-duped, in ONE `send` (was a per-role-topic fan-out).
  dispatch/emit/watchdog/billing all page this same set. **The superadmin ops number is NOT in
  the org audience** (operator choice 2026-07-25 — the platform admin can't be buried under every
  org's device/uplink/port/probe/billing pings; probe-down/NODE_STALE is an org page like any
  other and is excluded too). Instead the ops number
  (`app_settings.whatsapp_admin_number`, env fallback `WISP_WHATSAPP_ADMIN_NUMBER`) carries ONLY
  the topic-less pings that have no org role: org "I've paid" (`billing_paid`), self-downgrade
  churn, and release-sync failing — resolved through the SEPARATE `orgs._admin_whatsapp` /
  `releasesync._admin_numbers`, never `org_alert_recipients`. Don't re-add it to the org audience.
- **CENTRAL-ONLY by construction**: built with a `store` (reads live config from
  `app_settings`); the edge passes none and never calls `send`, so a store-less notifier is
  inert (nowhere to read a token/numbers from).
- **Config is the SUPERADMIN's, not env**: enable toggle + token + phone-id + template/lang/
  version + admin number in `app_settings` (Settings → Platform), read FRESH each send (no
  restart). `WISP_*` are fallback defaults; `enable_whatsapp` now defaults ON. Token is
  WRITE-ONLY (`token_set` boolean; blank leaves the stored one). Per-account numbers are set in
  Users / Your account (`/api/users/whatsapp`, self-service so worker-reachable).
- **"Time Logged" is rendered in the OPERATOR's zone, at ONE choke point**
  (`notifiers._wa_time`, called from `WhatsAppFacts.params()`). Central stores UTC
  everywhere and the dashboard localises in the browser, so a page is the only place a
  stored timestamp reaches a human with nothing to convert it — it shipped raw and every
  alert read 5h30m behind the Indian wall clock. Zone is `WISP_DISPLAY_TZ` (`cfg.display_tz`,
  default `Asia/Kolkata`); an unknown zone or an unparseable value degrades (UTC / pass
  through), never raises inside a send. Put it in `params()` rather than at the ~8 shells
  that build facts so a NEW paging shell can't reintroduce the bug. DISPLAY only — nothing
  stored or compared changes zone. `DisplayTimeTest` in `unit/test_whatsapp`.
- **Dead ntfy plumbing left IN PLACE** (project convention, like the operator/tech columns):
  the `orgs.ntfy_topic*` columns and `store.org_role_topic`/`org_topic` methods survive UNUSED —
  don't wire them back or "clean them up" into a migration.

**Why the governor exists at all:** a DBC area power cut darkened many PONs → dozens of false
"fiber cut" pages → ntfy 429s that dropped REAL pages (~497→~76/day). The allowlist is the
current, blunter answer (those SNMP kinds are simply off); the digest machinery is the finer
one, kept warm. Tests: `unit/test_notify_policy`, `unit/test_whatsapp`,
`integration/test_central_whatsapp`.

### Paging responsibility: devices → field accounts (2026-07-26)

`central/assignment.py` (pure rules + `PagingAudience` resolver), `store_assign.py`
(storage), `org_device_workers` (org_id, device_id, user_id). Narrows WHO an alert reaches:
owners always page for everything, and a device's WORKERS are its assignees.

- **It is a NOTIFICATION rule and NOTHING else.** Deliberately not read by any view, list,
  KPI, map or export — every account still sees the whole fleet (operator's explicit call:
  "the only thing we are doing the assigning is for workers to only get notifications for the
  devices they are responsible for"). So it is not a permission table and a bug here can lose
  a page but can never leak or withhold data. `test_central_assign:VisibilityTest` fails if a
  read path ever starts filtering on it.
- **An UNASSIGNED device pages EVERY worker** — `audience_for` returns `None`, which is NOT
  the empty set. That distinction is the whole safety property: switching this feature on
  narrowed nothing until someone was actually assigned, so it cannot silence a fleet. Same
  instinct as the governor writing state rows regardless of its allowlist. The roster reply
  carries the count of such devices so the UI states it rather than the operator inferring it
  from an absence.
- **Responsibility flows DOWN the tree and UNIONS; it never overrides.** One row on a region
  head covers the region (the only thing that scales on a fleet growing weekly), and naming a
  second worker on an OLT below it does NOT un-page whoever owns the head. Nearest-ancestor-
  wins was rejected for exactly that: a narrow assignment silently dropping a wide one is the
  failure this subsystem must not introduce. Derived from the LIVE parent chain every time,
  so re-parenting moves responsibility with the device and adding a splitter needs no click.
  PRIMARY parents only — a backup parent is a failover path and a peer is a cable, neither a
  chain of command. Cycle-guarded: validation rejects cycles on the way in, but a page is the
  last thing that may spin on a bad row.
- **A DEACTIVATED assignee doesn't count as "somebody is responsible"** (`device_assignment_map`
  joins `users.is_active`) — otherwise switching an account off would silently narrow its
  devices to owners only. The row survives so the operator can see and clear it.
- **An assignee with no `whatsapp_number` is REPORTED, never widened around.** The assign API
  answers `unreachable: [username]` and the UI warns; the audience stays narrowed (that
  device pages owners only). Widening back would mean an assignment quietly undone by
  somebody's empty profile field.
- **Wiring**: `dispatch._recipients(device_id)` for device up/down (uplink events carry no
  device → org-wide), `AlertRouter.emit` for everything through the governor (it builds its
  own resolver when a shell doesn't pass one, so a NEW paging shell narrows by default), and
  `watchdog` via `for_node` — a probe is not a device, so its audience is whoever owns what it
  carries, falling back to org-wide because a dark probe blinds a slice of the fleet.
  `alert_log.recipient` records the NARROWED set, so "who was actually told" stays answerable.
- **`emit` resolves the audience AFTER the allowlist/gate checks** — most SNMP kinds are
  suppressed, and resolving a per-device audience for a page that was never going to send put
  three queries per emit on the report cycle. A suppressed row therefore logs no recipient.
  `PagingAudience` caches for its own lifetime (one sweep) — the watchdog builds a fresh one
  per page because that thread is long-lived and would otherwise page a stale audience.
- UI: Settings → Users → **Device responsibility** (bulk, per account, region-filtered) and the
  device panel's **"Paged for this device"** fold (per device, naming the ancestor an
  inherited assignee comes from). Both say "paged", never "sees". Owner-only writes; the
  roster GET is owner-only too (it enumerates accounts) and ships `has_whatsapp` as a boolean,
  never the number. The outage-assign dialog marks accounts already responsible for that
  device and sorts them first — a suggestion, never a filter or an auto-assignment.
- Tests: `unit/test_assignment` (the rules), `integration/test_central_assign` (device/port/
  probe pages narrowing, the API, and the visibility guard).

### Alerting subsystems

> **Paging for these is OFF for now** (the `_ACTIVE_KINDS` allowlist — 2026-07-24). Everything
> below still DETECTS and WRITES STATE exactly as described (dashboard, badges, folding) — only
> the WhatsApp page is suppressed. Port UP/DOWN is the one exception that still pages; port
> BANDWIDTH does not. Re-enabling is a one-line allowlist edit. Read the descriptions as "the
> state this writes", not "what it pages".

- **Port alarms** (`central/ports.py`): monitored-only, admin-down silent, one alarm not two —
  a port-down folds into the open outage via `stamp_outage_cause` COALESCE (never clobbers a
  post-mortem); no open outage = heads-up; SNMP never opens an outage. Gated
  `cfg.snmp_alerts`, state always written. **Bandwidth has floor AND ceiling**
  (`bw_threshold_mbps`/`bw_max_mbps`, `snmp_bw_consecutive` walks to alarm); never judged on a
  down port; gated `cfg.snmp_bw_alerts`.
- **Perf baseline** (`central/perf.py`): median+MAD over a bounded per-device ring buffer
  (`device_perf_samples` — NOT the hourly rollup; an hourly average smears the slowdown).
  Badge persisted, clears on hard-DOWN, operator-only, gated `cfg.perf_alerts`.
- **On-backup redundancy**: the engine already computes it via `effective_parents()`.
  `org_device_links` (`kind='backup'`, cycle-checked over the FULL edge set);
  `redundancy.sweep` pages enter/leave, never opens an outage, gated `cfg.backup_alerts`.
- **Rollups**: `analytics.device_reliability` (`/api/analytics?days=`) is pure outage math,
  every active device, UNREACHABLE excluded. `rollup.py` (`/api/analytics/trend`) is hourly
  buckets, 30-day retention, pruned daily.
- **Incident shape** (`central/incidents.py`): open-outage waves (15-min start gaps) ×
  independent branches (a root = a down device whose parent is NOT down, judged against the
  FULL down set so victims of an older outage never count) × geography. ≥2 branches inside
  3 km = "power"; 1 branch = "upstream"; scattered multi-branch = SILENT (no verdict beats a
  wrong one). ANNOTATION ONLY — never mutes, reroutes, or replaces a page.

### PON / ONU

- **PON mass-drop verdicts** (`ponfault.py` pure math, `ponalert.py` paging shell):
  `onu_optics.last_online_at` FREEZES when an ONU leaves `online`, so "≥3 ONUs on one PON dark
  with a recent last_online_at" IS the event — no history table. Dying-gasp majority = POWER
  (recorded, NEVER pages — the whole point is not rolling a splicing crew for the DISCOM); LOS
  majority = fiber, cut bracketed (max-online-short-of-dark, min-dark] in RANGING metres
  (optical path with slack — always a stretch, never a point). An OLT whose walk is >15 min
  stale is skipped (the ICMP outage owns that page). Transition-only via `pon_fault_state`,
  gated `cfg.pon_fault_alerts`, state written even when gated off. Suspect naming = the
  deepest passive whose route distance (link_routes geometry, chord where undrawn, ABORTS on
  any unplaced hop — never fabricate) lands in the interval. **Hardware gap:** the C-Data/DBC
  EPON fleet reports only online/offline — never dying_gasp/LOS — so POWER can't fire there
  and area power cuts page as "fiber" (simultaneous multi-PON/multi-OLT drops are the tell).
  Tests: `unit/test_ponfault`, `integration/test_central_ponalert`.
- **ONU-roster hygiene** (`onuroster.py` math, `onualert.py` shell), both transition-only on
  the same optics fold:
  - **Per-PON cap** — EPON tops out at 1:64, so a PON at its limit pages "at capacity"
    (`cfg.onu_pon_limit` 64; per-OLT `org_devices.onu_pon_limit` override so a 1:128 GPON box
    doesn't false-page). `list_org_devices` MUST carry that column or the override silently
    no-ops in paging. **Set as "PON type" on the OLT's device form** (EPON 1:64 / GPON 1:128,
    beside the GPON vendor override) — it rode `clean_device_payload`/create/update only from
    2026-07-30; before that the column existed with every reader wired and no UI, so a mixed
    fleet had one cap. NOTHING DETECTS the split standard (it is in no MIB we walk), so this is
    the operator's claim and **UNSET means the global cap, never 64** — writing 64 on save would
    stop `cfg.onu_pon_limit` from ever reaching a box somebody had edited. The column stays a
    plain integer (a 1:16/1:32 build set through `optics-thresholds` survives, and the form
    offers it back as "custom"), and it now rides the device payload like `split_ratio` — an
    absent key reads as "not set", so any NEW caller of `update_org_device` must carry it
    forward or it silently drops a GPON box back to the EPON cap.
  - **Redundant MAC** — a serial/MAC on ≥2 slots org-wide is a duplicate, but it PAGES only
    when ≥2 are ONLINE at once: C-Data reg tables keep every slot an ONU ever occupied, so the
    byreddy fleet had 178 "duplicates" of which 2 were live clones. Dead-member dups are
    history — state-only, never ntfy.
  - Both read `onuroster.current_roster` — per OLT, rows from the FRESHEST walk (identical
    `updated_at`), skipping an OLT staler than 900s — because `onu_optics` NEVER deletes
    removed-ONU rows, so raw counts over-count the cap and fake duplicates off zombies.
  - **A stale OLT FREEZES its alert states, never clears them** (dup absence checked against
    the staleness-blind `duplicate_macs(stale_s=None)` shadow; capacity clears need the device
    in `fresh_device_ids`) — clearing on staleness re-paged 178 MACs every time a slow walk
    stalled. Tests: `unit/test_onuroster`, `integration/test_central_onualert`.
- **ONU lookup by MAC/name** (`/api/inventory/onu-search`): a tech holds the sticker MAC or
  the provisioned name, neither of which reached anything before — the roster was visible only
  once you knew which OLT to open, the thing being looked up. Hits deep-link into that OLT's
  Optical tab via `focusOnuId`; the OLT stays in the tree (`filterWithAncestors`' `extraIds`).
  **`search_key` (alphanumeric, upper) is SEARCH-only and must never replace `_norm_mac`** —
  identity stays separator-exact, or differently-punctuated strings collapse into fabricated
  dup-MAC pages. It's a registered SQLite function (`wisp_search_key`), not a REPLACE chain:
  the chain knew only the four MAC separators and silently missed the `_` in a real name like
  `hc_kiran`, and ONE normalizer is what guarantees a typed `%`/`_` can't reach LIKE. Searches
  the CURRENT roster, not raw `onu_optics` — a zombie slot would deep-link to a tab that
  doesn't list it. 3-char floor, 50-row cap. Tests: `integration/test_central_onusearch`.
- **GPON vendor auto-detects from sysObjectID; unmatched = optics OFF, never guess** (a
  fabricated dBm is the DBC placeholder trap). `GponPollerPool.resolve` precedence: device
  `gpon_vendor` override > `WISP_GPON_VENDOR` > sysObjectID longest-prefix > None. Detection
  cached per device (1h ok / 15min on silence — catches a hardware swap), inside the SNMP
  semaphore, one lazy engine. Tests: `unit/test_gpon`.
- **An ONU's identity is its SLOT, never its serial — on BOTH parse paths** (`gpon.py`).
  The registration path always keyed on `pon.onu`; the metric path used `serial or idx` until
  2026-08-02, and that asymmetry is why `syrotech_gpon` shipped with its serial column
  deliberately UNMAPPED (so the GPON boxes showed no sticker ID at all). These OLTs never drop
  a vacated registration, so a re-registered ONU is reported on both its old and new slot: on
  Gpon_08 today, 9 serials of 194 sit on 2–3 slots, and a serial key stored **184 rows for 194
  slots with 5 LIVE ONUs written as dark** wherever a zombie landed last. The serial is a fact
  to REPORT about a row, never the name of the row. Corollary: mapping `oids.serial` on a
  metric-only profile is safe only once every probe serving it runs a slot-keying build —
  `tools/gpon_enable_serial.py` gates on the reported version rather than trusting the order,
  because the damage (collapsed rows + phantom keys `onu_optics` never deletes) needs a manual
  sweep. `unit/test_gpon:test_one_serial_on_two_slots_stays_two_rows`.
- **GPON profiles are DATA** (`gpon_profiles`), served in the `/edge/devices` reply. Built-in
  callables travel as a CLOSED vocabulary (`state_map`+`state_default`, `pon_index`
  `as_is|first_segment|packed_ifindex`, `pon_label` template); `gpon_profile_from_dict` rejects
  the WHOLE profile on anything outside it — never a best-effort partial. A same-named row shadows a
  built-in (huawei/dbc stay in edge code as fallbacks for older fleets). `set_profiles` runs
  every cycle and MUST stay a fingerprint-gated no-op on an unchanged payload — rebuilding
  pollers churns SnmpEngines (the leak invariant).
  **`org_devices.gpon_vendor` validation therefore CANNOT be the built-in list**
  (`inventory._gpon_vendors(extra)`, fed by `api/devices._gpon_vendor_names`). It was, until
  2026-07-30, so badri_fiber's two `syrotech_gpon` OLTs 422'd "GPON vendor must be one of: dbc,
  huawei" on EVERY edit — a rename, a region, a PON type — because the form faithfully sends
  the stored vendor back; the dropdown had offered the profile all along. Profiles are the
  vocabulary, built-ins are only its floor. **DISABLED rows count** (a tombstone, not an
  absence): dropping them would lock the operator out of the one form that could correct the
  vendor. The SPA likewise keeps the device's CURRENT vendor as a dropdown item even when no
  profile offers it — a Select with no item for its own value renders blank, and saving that
  blank silently unstamps the vendor. Still a closed set: a name no profile carries is refused,
  since a typo'd vendor reads on screen as "this OLT has no optics".
- **`packed_ifindex` exists because a PON can live in the OID INDEX and nowhere else**
  (2026-08-07, chandana-network MAIN_OLT4, a Stelfiber **STGP08X** under its own **PEN 50224** —
  the box names no maker, `sysDescr` is a bare `STGP08X`, so the vendor is the OPERATOR's
  identification and the PEN is the only machine-checkable half).
  That OLT keys its ONU roster by a byte-packed ifIndex — `chassis<<24 | slot<<16 | pon<<8 | onu`,
  so PON 1 ONU 0 is 16777472 (0x01000100) — and carries NO PON column at all: every roster
  column was checked against the decoded PON and none matches (col17/col18 are the subscriber's
  PPPoE username/password). `as_is` would have made pon_port the raw ifIndex, i.e. 310 singleton
  "PONs" on an 8-PON box. **The strategy supplies BOTH halves on purpose** — `_ONU_INDEX_STRATEGIES`
  is keyed by the same name, because deriving the PON from a packed index while reading the onu
  id off the same index unpacked reports ONU 16777472 on PON 1. Verified against the OLT's own
  text column (`.3.12.2.1.2` = "ONT01/000") on all 310 rows and against its own per-PON counters
  (`.3.2.3.1`). `unit/test_gpon:CentralProfileTest`, and the profile itself is
  `tools/gpon_add_stgp08x.py` (version-gated: an edge ≤ v0.15.14 rejects the whole profile).
  Its state column maps **0 → unknown, never offline** — those 68 slots are authorisation
  entries that never registered (blank vendor, distance 0, last-seen "-"), and dark-by-default
  would have handed `ponfault` a fabricated mass-drop cohort. Its per-ONU optics DO exist
  (`.3.12.3.1`: Rx dBm×100, the 3.3 V rail, temperature) but are indexed `<ifindex>.0.0` against
  the roster's `<ifindex>`, so `parse_onu_table` keys them as different rows — mapping rx yields
  448 rows of which ZERO carry both a serial and a reading. Joining them is a parser change, not
  a profile edit.

### Reference ONUs: the witness that replaces dying-gasp (2026-07-28)

`onu_places` (sparse), `ponfault._witness_verdict`, `map/refonu.ts`,
`components/reference-onu.tsx`. An operator places the handful of subscribers it
knows run on a UPS/solar/tower supply; those become WITNESSES in the PON mass-drop
verdict. It exists because the C-Data/DBC fleet reports neither `dying_gasp` nor
`los` — every drop arrives as a bare `offline`, so the power/fiber cross collapses
and an area power cut pages as a fiber cut. This is the only discriminator that
works on that hardware, and it needs no rollout.

- **PLACING IS THE CLAIM — there is no power column and nothing detects one.**
  The operator's explicit call: they select the reliable ones. So the *act* of
  placing carries the whole meaning, which is why the dialog states the contract
  in a warning block and the map banner restates it at the click. A pin dropped
  "to complete the map" silently corrupts verdicts. Never soften that copy to
  "location" or reduce the dialog to a one-click toggle.
- **Keyed on the MAC (`onuroster._norm_mac`), never `(device, onu_key)`** —
  `onu_optics` never deletes a vacated slot and a re-registered ONU moves, so a
  slot key rots. Re-homing a drop (even to another OLT) carries the point with it.
  An RMA'd box orphans the row, which is CORRECT and is reported (`matched:false`)
  rather than hidden — a pin that quietly stopped witnessing is the one failure
  this list must not conceal. Normalized at exactly ONE place on the write path
  (`inventory.clean_onu_place_payload`) or one sticker becomes two witnesses.
- **The rules** (`unit/test_ponfault:WitnessTest`): a witness dark SILENTLY →
  `fiber`, now evidence rather than assumption. Every witness still online AND
  reaching past the dark set → `power`, no crew. **A witness reporting
  `dying_gasp` counts in NEITHER tally** — the ONU testified it lost power, which
  outranks the operator's label (its backup failed, or the label was wrong).
  Hardware beats paperwork. Witnesses OUTRANK the gasp majority.
- **`_reaches_past` compares ORDER only, never the unit** — that is what makes it
  safe on the dbc profile whose `distance_m` is EPON time quanta (~39% short).
  A survivor SHORT of the dark set does not flip anything: a cut in a distribution
  branch leaves everything closer in lit.
- **`PonFault.evidence` is `witness | dying_gasp | silence`, and the three must
  never render alike.** `silence` is the honest name for what the DBC fleet used
  to produce indistinguishably from a finding — the UI says so and invites a
  reference point; the page drops "suspected" only when `witness_dark > 0`.
- **Every ponfault caller passes `witness_macs`** (ponalert, `api/outages`
  faults + pon_summary, `api/devices._stamp_optical_faults`, `issues.collect`) —
  the count-agreement rule: a tile, a chip and a page disagreeing about the same
  PON is worse than any of them being absent.
- **The map layer is OPT-IN and subordinate — by SHAPE, TONE and STACKING, not by
  size** (2026-07-29). 90% of what hangs off a fleet's ports is an ONU; the layer
  defaults OFF (localStorage), keeps a mark of its own (a diamond — devices round,
  passives squarer), stays muted rather than status-toned, sits below every device
  pin (`refZIndex` is negative) and stays OUT of the clustering pass (a site badge
  mixing plant with subscribers would count nonsense). A DARK witness is the one
  thing allowed to get louder — it is a fiber cut with a coordinate.
  **SIZE is no longer one of the four**: it was drawn "smallest mark on the map"
  twice and came back unreadable both times (11px, then 9px for a located
  subscriber). A diamond covers HALF the area of the circle bounding it, so an
  11px one carried a quarter of a 14px device dot's ink — the marks now match a
  device dot by AREA (14/13/17px), and a layer nobody can see ranks below
  everything anyway. Judge these by ink, never by the number.
- **The line to its OLT is DOTTED, and that is not decoration.** Every other line
  on the map is a drawn cable route or the chord standing in for one — a claim
  about plant. This one is a LOGICAL association with no surveyed path, and a
  splicing crew quotes drum off lines that look traced. Weights match the topology
  lines and then past them (2.5 / 3.5 / 4.5 by dark-ness, plus the same black casing every other line
  gets — satellite runs near-white to near-black inside one viewport, and this
  layer went casing-less while it was hairline-thin, which is most of why it
  vanished). **The DASH is what carries the ranking, not the weight** — and
  because SVG dash lengths are absolute px while the stroke widened, both periods
  had to open up with it or a dotted line silently becomes a solid one, i.e. traced
  fibre. `REF_DASH` ("1 10", an 11px period) stays sparser than `DROP_DASH` and apart
  from the backup ("5 8") and cross-link ("1.5 7") dashes so the four stay
  distinct. Drawn ONLY when the OLT is known (not ambiguous, not orphaned) AND
  itself placed. `interactive={false}`, like every topology polyline.
- **…EXCEPT under the cursor, where it goes SOLID — bounded, and don't "fix" it back**
  (`refonu.ts:REF_HOVER_BOOST`, 2026-08-03, operator's ask). Hovering a subscriber
  lights its drop line solid at +1.5 weight and full opacity, and opens a card beside
  the pin (`map/refhover.tsx`). What the dash protects against is a RESTING map that
  looks surveyed — a line read across the room, screenshotted, or quoted drum off. A
  hover is none of those: pointer-bound, exactly one line solid at a time, and
  NARRATED, because the card names the span in words ("Drop · SPL-4" / "Drop · not
  recorded"). The resting map says "unsurveyed" silently; the hovered one says it out
  loud. **The line still may never be MEASURED**: `linkhover` probes drawn topology
  only and is suppressed outright while a subscriber is hovered (they also collide —
  a drop runs within a fingertip of its splitter's feeder). Tone is untouched by
  hover — it may make a line findable, never make it look healthy.
  - Hover is **React state, not `:hover`**, because the line and the card are outside
    the mark — but it must NEVER enter `refOnuIcon`'s html (cached by string, so a
    hover class there remounts the diamond and replays its fade-in on every pointer
    crossing). Same reason the card keeps its OWN one-entry icon cache instead of the
    shared `cachedDivIcon`: MapPage re-renders every second off `useNow()`, and a
    fresh divIcon per tick swaps the DOM node and replays the card's animation.
  - **The card is the one surface with room to obey the blank-reading rules
    PROPERLY.** A map label must silently omit a dBm it can't stand behind; this can
    say WHICH of the reasons it is (`not measured on this OLT` / `last reading is
    stale` / `frozen — its OLT is down`). Frozen is said ONCE under a "Readings" key
    and the Traffic row stands down — "port walk stale" beside a DOWN box is true and
    useless, and points at an SNMP fault that isn't there. The verdict row is graded
    on what is PRINTABLE, so a crit severity with no printable reading can't paint the
    band red with nothing to explain it. The WORD carries the tone's meaning
    ("Online · critical signal"), or a red band reading "Online" gets read as "down".
  - **Its width is REM and the clamp maths reads the live root font-size.** A fixed
    236px card looked right at 1440px and truncated "NDN-OLT · PON EPON0/5" at 1600px,
    where the root scales up and the type grew but the card didn't — the PDF column
    solver's lesson, in CSS: never let a fixed width starve the identifier column.
  - Card ABOVE by default (the name plate is below the diamond), flipping below only
    near the top edge; a tail at `calc(50% - var(--sx))` cancels the edge clamp so it
    aims at the pin either way (measured: 0.0px off, clamped or not). `refZIndex`
    lifts the hovered mark — marks are out of the clustering pass and a real pair sits
    10.8px apart, so without it a pixel of jitter flickers between two customers.
- **The rate on that line is the ONU's OWN ifTable row, NEVER the PON's.**
  C-Data EPON publishes an interface per ONU (`EPON01ONU3`), which is the only
  reason a per-subscriber rate exists here at all; the PON's row (`EPON0/1`) is
  the aggregate of up to 64 subscribers and printing it would put one big number
  on every reference point (`test_the_PON_AGGREGATE_is_never_reported_as_one_
  subscribers_rate`). Key on the FIRST TOKEN of `if_name`, never `if_alias` — the
  alias holds the default `EPON0/1:3` only until somebody types a description,
  after which it reads `BSNL-149`; `if_name` appends the description and keeps
  the token. `onuroster.onu_if_token` is vendor-specific and MEASURED, not
  assumed (2026-07-28: PYLON 177/177, PDVR 102/102, Epon_8 208/209, HLY-OLT-2
  313/326; **zero** on Gpon_04/Gpon_08/TMG/SRPL/NLK). A miss degrades to "no
  reading" — never a guess, never the aggregate.
- **"No rate" and "0 Mb/s" are DIFFERENT sentences, and so is "this firmware has
  no per-ONU interface".** HILL-OLT-1 is the live proof: 227 interfaces matched,
  33 carry a counter, because that box's port walk is failing. `refHasRate` gates
  on `isFresh(port_updated_at)` so a stale walk can't print a weeks-old number as
  now; the card spells out which of the three states it is in.
  **The CHIP no longer attempts it at all** (`refHasChip`, 2026-08-05, operator's
  ask): a drop with no reading now draws NOTHING, where it used to draw a badge
  reading "no rate". That was the common case, not an edge — `onu_if_token`
  matches zero rows on every GPON build (Gpon_04/Gpon_08/TMG/SRPL/NLK), so a
  surveyed street drew one chip per drop line to announce an absence, and a badge
  has room for none of the three sentences above. **The budget reads the same
  predicate as the render**, or an absence goes on reserving pixels away from a
  live reading and a customer name that would have drawn. `refBwIcon` returns
  `null` past the gate, so an ungated future caller is a type error rather than a
  blank pill on a line. `dark` and `idle` are untouched.
- **The line's tone follows the OPTICAL ROSTER (`isRefDark`), not `port_state`.**
  The two ride different clocks (they agreed on 1542 of 1557 live rows) and the
  roster is what the pin and the witness verdict already use — pin and line
  contradicting each other on a wall map is worse than either being wrong.
  `port_state` ships as a second opinion and colours nothing.
- **This is a NOTIFICATION/verdict input, not a registry.** ONUs are deliberately
  NOT `org_devices` rows (that would wreck the tree, `list_org_devices` and the
  engine fingerprint), and the map card is deliberately not the device panel — an
  ONU has no health tab, no ports and no outage of its own.
- Tests: `unit/test_ponfault:WitnessTest`, `integration/test_central_onuplaces`,
  the witness cases in `integration/test_central_ponalert`.

### Splitters and subscriber drops: the distribution network (2026-07-28)

`onu_drops` (sparse-ish, one row per recorded subscriber), `org_devices.split_ratio`,
`central/drops.py` (pure math), `api/devices.py:onu_drops`/`onu_drop_subscribers`/
`set_onu_drops`, `map/drops.ts`, `components/splitter-panel.tsx`. The map drew a
subscriber straight to its OLT; ISPs said that isn't the network. Reality is
`OLT PON → feeder → splitter (1:2/1:4/1:8) → [another splitter] → drop → ONU`, and a
customer is hung off whichever splitter is NEAREST — so the straight line skipped the
entire plant a crew works on.

- **The splitter chain was already there; only the LAST hop was missing.** Passives are
  `org_devices` rows with parent chains, `pon_port` and drawn `link_routes` — so this
  feature is one table (which passive feeds which MAC) plus one column (how many ways a
  box splits), NOT a second topology. Don't build one.
- **Keyed on the MAC (`onuroster._norm_mac`), never `(device, onu_key)`** — same reason
  `onu_places` is: `onu_optics` never deletes a vacated slot and a re-registered ONU
  moves, so a slot key rots. Normalized at exactly ONE place on the write path
  (`inventory.clean_onu_drops_payload`) or one sticker inflates a splitter's load. The
  PON is deliberately NOT stored — it comes from the roster, and a second copy could
  disagree with the walk about which PON a subscriber is on.
- **"Recorded" is NEVER "occupied".** Six drops on a 1:8 does not make two legs free —
  nobody wrote those down, and unknown is not spare. The bar says "of 8 legs recorded"
  and the caption says so outright. The ONE capacity claim that survives an incomplete
  record is OVER-subscription (more recorded drops than legs is provable either way), so
  it is the only one made. Same instinct as "nothing is wrong" vs "nothing is measured".
- **`SPLIT_RATIOS` is CLOSED at (2, 4, 8, 16)** — what the ISPs actually stock; 16 arrived
  by request 2026-08-08, and 32/64 are the same one line in `inventory.py` + `types.ts`.
  The ratio feeds the load bar and the cumulative split down a cascade (`1:4 × 1:8 = 1:32`,
  which is what says whether a PON has budget left), so "1:7" would produce arithmetic
  nobody can act on. `cumulativeSplit` returns null if ANY box in the chain lacks a ratio —
  a partial product UNDERSTATES the split, and understating it is how a PON gets over-built.
- **`split_inputs` is the OTHER axis (1 or 2), and it is NOT a second ratio**
  (`SPLIT_INPUTS`, 2026-08-08 — the operator asked for "1/16 and 2/16"). A 2:16 has a second
  input for a protection feed and still splits SIXTEEN ways, so nothing multiplies it and
  `cumulativeSplit` reads outputs alone. Three things follow:
  - **NULL means ONE, not "unrecorded"** — the one place in this schema where absence takes
    a default instead of reading as a gap. Every splitter predating the column was already
    drawn `1:N` by a label that assumed one input, so a gap reading would have marked all 41
    on the live fleet incomplete overnight to document something nobody had got wrong. `1` is
    likewise STORED as NULL (sparse storage, same rule as the theme overrides).
  - **It says how many ports the box was MANUFACTURED with, never how many are CONNECTED** —
    that is `org_device_links`. Keeping the two apart is exactly what lets the panel say the
    useful thing: *"two inputs · 1 feed recorded"*, i.e. either genuinely unprotected or
    simply undrawn, which need opposite actions. Feeds counted = primary parent + backup
    links; a PEER is a cable between equals and is excluded, or a cross-link would report a
    protection feed that isn't there.
  - **A second input needs a ratio first** (a "2:?" names no product), and clearing the ratio
    clears it in the SAME write. Enforced server-side, and the picker disables the inputs
    group until a ratio is chosen so the form cannot produce a 422.
- **The ratio STRING is built in exactly one place**, `drops.ts:ratioLabel`/`deviceRatioLabel`.
  It was hand-written as `1:{ratio}` at EIGHT render sites, and the day inputs became real
  every one of them would have gone on printing `1:` over a 2:16 — the same failure
  `onuName` was extracted to kill. `SplitRatioField` is likewise ONE control across the
  splitter panel, plant-create, the device form and the survey sheet; it lays the two axes
  out either side of a literal `:` so the control spells the answer that is written on the
  box's casing.
- **A branch fault names a SPAN, not a distance.** Every recorded subscriber below one
  passive dark while a sibling branch stays lit ⇒ the break is in the ONE span feeding
  it. This beats the ranging bracket outright on the C-Data fleet, whose `distance_m` is
  EPON time quanta (~39% short) — two pins and the cable between them are where a van
  drives. Self-limiting by construction: when the fault is higher up, the branches below
  have no lit sibling and drop out on their own, leaving the topmost dark node. There is
  no "deepest wins" rule to get backwards.
- **It DETECTS and RENDERS; it never pages and never touches a `ponfault` verdict.**
  `drops.py` is imported by no alerting shell — structurally incapable of paging, which
  is what lets it be as opinionated as it is. Deliberate for a first cut (operator's
  call): feeding branch darkness into the PON-fault verdict is its own session.
- **Same refusals `ponfault` keeps, for the same reasons.** A stale/down OLT is SKIPPED
  (a dead edge makes every branch behind it look dark; the ICMP outage owns that page).
  A dying-gasp majority reads `power`, not a cut — and a dark power-backed reference ONU
  in the branch OUTRANKS that majority, while a GASPING witness counts in neither tally
  (hardware beats paperwork). `MIN_BRANCH_DARK` = 2: one dark subscriber is a subscriber
  problem.
- **Unrecorded subscribers are never assumed lit or dark**, so a thin plant record can
  produce a wrong-looking "all dark". Two mitigations, both required: every string says
  "recorded", and the layers menu states `N of M subscribers mapped to a splitter` —
  leaving coverage to be inferred from thin-looking splitters is how a partial map gets
  read as a complete one (same reason the paging roster ships its unassigned count).
- **Rx is compared against SIBLINGS, never a modelled budget** (`OUTLIER_DB` = 3.0).
  ONUs on one splitter share the feeder and the split loss, so they differ only by drop
  length — at 0.25 dB/km a 3 dB gap would be twelve kilometres of drop cable, i.e. a bad
  splice, a bend or a dirty connector on THAT drop. An absolute budget would need the
  OLT's launch power, which no vendor here publishes, so a modelled number would be a
  guess wearing a decimal point. Corollary that must not be "fixed": a uniformly low
  splitter is NOT a box full of outliers (each reads normal against its own median) —
  that case is the feeder, and it surfaces as the median sitting below its siblings'.
- **Recording is BULK, one dialog per splitter** — the question an operator can answer is
  "which customers are on this box", asked once while standing at it. Eight dropdowns on
  eight ONU rows is how a plant record never gets written at all. Candidates come from
  the OLT ancestor's roster, restricted to the splitter's own `pon_port`. An ONU already
  recorded elsewhere shows WHICH box, because ticking it MOVES the drop and a silent
  re-parent would surface later as a wrong load count.
- **A drop may only hang off `PASSIVE_TYPES`** — pointing one at a switch would put
  subscribers on a box that has an FSM and an outage of its own. Deleting a passive
  DELETES its drops (the box is gone, so the question has no answer) rather than
  dangling them; the subscribers themselves live in the SNMP roster and are untouched.
  `test_delete_cascade_handles_every_fk_table` catches a forgotten cascade.
- **A passive stays QUIET until its subscribers aren't.** Plant is reference material and
  must not compete with gear for the eye (why passives render small and muted). The one
  exception is worth making — a splitter whose recorded customers are dark is the most
  useful object on the map during a cut. SIZE never changes, only tone, and the dot does
  NOT pulse: a pulse means "this box is down", and a splitter is never down.
- **Its pin runs on BOTH colour axes: identity for the ordinary states, status only for an
  alarm** (`index.css:--map-plant`, 2026-08-06, operator: "change the colour of splitter's
  pin"). A splitter used to be painted entirely out of the STATUS vocabulary — grey with no
  record, GREEN when its recorded drops were all online — and green is this app's "healthy",
  which a box with no state, no FSM and no outage cannot claim; it also put plant in the same
  colour as the located subscribers hanging off it. So `quiet`/`ok` leave Axis A for
  `--plane-plant` (299°, the "physical, surveyed" identity hue `--chart-4` already mirrors)
  and `weak`/`dark` still take `--warning`/`--destructive` outright: identity says what a
  thing IS, an alarm outranks it. **Measured, not chosen by eye** — plant sits 90–145° from
  every status hue, its chroma is 51–56% of theirs (the documented identity cap), and it
  contrasts 3.89:1 dark / 3.22:1 light against the canvas versus 4.6–9.5:1 for the status
  tones, so it can neither be mistaken for an alarm nor outshout one. **TWO violet steps, and
  the step is CHROMA at fixed hue** (never a mix toward the grey — see QUIETING A STATUS
  COLOUR): full for `ok`, drained for `quiet`, because "nobody recorded a drop here" and
  "every recorded drop is up" are different sentences this map may not paint alike. The hover
  card still grades a healthy box `success` in words and green, and that is NOT the
  pin-vs-card disagreement the 2026-08-05 note fixed: a mark and its card may never grade one
  box two ways ON THE STATUS AXIS, and the pin has left that axis precisely where it was
  making a health claim it has no standing for. Judged on real satellite tiles in both themes.
- **Its plate carries the SPLIT RATIO and nothing else — not even its name**
  (`drops.passivePinLabel`, 2026-08-05, operator's ask). It read `medha · 1:8 · 6 · 2 dark`;
  it reads `1:8`. A splitter is not looked at for what somebody called it — the ratio is
  what says whether a PON has budget left, and it is three characters against fifteen on a
  fleet whose plant is named `manjulapur`/`collectorate`/`IK REDDY`. Nothing is lost: the
  recorded count and the dark count are still the MARK's tone (`dropTone`), the hover card's
  rows, the panel's bar and `passiveTitle`'s full sentence — this drops ink, not facts, which
  is why the tone rules moved from the old `.wisp-pin__sub` span onto `.wisp-pin__label`
  rather than going away. **A box with no recorded ratio keeps its NAME**, and that is not a
  hedge: since plant left the clustering pass the plate is one of only two channels (with the
  hole in the mark) telling a splitter from a customer, both being teardrop pins at nearly
  equal ink, so a blank plate would spend one of the two. FDB/closure have no ratio by nature;
  every splitter on the live fleet has one.
- **The drop line is DOTTED and TIGHTER than every other dash** (`DROP_DASH` "1 7", an
  8px period, vs the ref-ONU "1 10", backup "5 8", cross-link "1.5 7"): it is the least
  surveyed span on the map. Periods were opened when the strokes widened (2026-07-29) —
  a dash array is absolute px, so widening a dotted line without opening its gaps closes
  them into a solid one. A reference ONU with no recorded drop still falls back to its OLT — rendered
  WEAKER and saying so — because a reference point must not vanish for want of plant
  records, but "routed through its splitter" and "we only know the PON" may not look
  alike.
- **…UNLESS IT HAS BEEN TRACED, and then it is SOLID** (`onu_drops.waypoints`,
  `POST /api/inventory/drop-route`, 2026-08-08 — the operator asked for "customer line
  route editing"). The last hop was the only span on this map that could never be drawn,
  and a drop is not straight: it runs down a pole line and along a street, which is where a
  van goes when it breaks. Tracing one therefore EARNS the solid stroke a drawn cable route
  gets — the dash means "nobody surveyed this", somebody just did, and the two states may
  not look alike in EITHER direction (an untraced drop may not look surveyed; a surveyed one
  may not go on apologising for itself).
  - **ONE editor, two kinds of span** (`map-page.tsx:RouteEdit`, a `link | drop` union).
    Same gesture, same waypoint list, same undo/straighten/vertex-drag — only the row it
    saves into differs. A second editor would be two sets of behaviours to keep in step, and
    the day they drifted the map would have taught two ways to trace one network.
  - **Keyed on the MAC and requiring a RECORDED DROP** — 404 otherwise, never a row invented
    on the spot. With no recorded splitter the map falls back to the OLT, and that line is an
    ADMITTED GUESS ("we only know the PON"); tracing it would promote the guess into surveyed
    geometry a crew orders drum against. Record the splitter first, which is the order the
    work happens in anyway.
  - **RE-HOMING A DROP DISCARDS ITS ROUTE** (`set_onu_drops`, guarded on the passive actually
    CHANGING). That path was walked to the box the customer no longer hangs off, so keeping it
    leaves a solid — i.e. surveyed — line running to the wrong splitter. The guard is what
    lets the bulk dialog re-save its whole set idempotently without destroying traced work.
  - **Waypoints run SPLITTER → ONU**, matching `dropAnchor`'s return order, so no renderer
    ever reverses a list — the same discipline `link_routes` keeps by bending the peer KEY
    rather than the waypoint order.
  - **A traced drop may finally be MEASURED, and only then**: the hover card prints
    along-cable metres (`polyKm`, segment-by-segment, because Mercator stretches with
    latitude and this is a drum figure). An untraced one prints none — the resting map's
    refusal to measure an unsurveyed span, kept on the one surface with room to explain it.
  - `refChipPos` is ONE function because the render AND the shared chip budget both need the
    midpoint of the line as DRAWN; on a traced drop that is nowhere near the chord's, and the
    two computing it separately is how a budget reports itself clear over a visible collision.
- Tests: `unit/test_drops` (the rules and every refusal),
  `integration/test_central_drops` (identity, passive-only, org isolation, bulk/detach,
  cascade, and the count-agreement between the rollup and the drill-down),
  `integration/test_central_cableplant:DropRouteTest` (the 404, the re-home discard, the
  idempotent re-save that must NOT discard, and separator-exact MAC identity).

### Field survey: the worker places the plant (2026-07-28)

`/survey` (`routes/survey-page.tsx`), `hooks/use-gps-fix.ts`, `inventory.clean_field_
location_payload`/`clean_field_passive_payload`, `store_devices.place_org_device`,
`api/devices.field_location`/`field_passive`, `api/common.can_survey`. ISPs asked for the
mobile worker view to geo-tag every device, active and passive. Coordinates ONLY — no
connections; the owner wires topology on the desktop afterwards.

- **It is the FIRST inventory write the worker role has, and it stays two operations
  wide.** `_WORKER_POST` gains exactly `field-location` and `field-passive`. What makes
  that safe is not a role check but what the routes CANNOT do: `field-location` cannot
  clear a pin, `field-passive` cannot set a parent, an IP or a probe. Both are separate
  functions from the owner's `/api/inventory/location` rather than the same one widened,
  because `clean_location_payload`'s contract INCLUDES both-null = delete — a
  worker-facing route must not be one missing UI guard away from erasing a surveyed
  fleet. `can_survey` is its own predicate beside `can_triage` for the same reason: so
  "workers can place pins" can't drift into "workers can write inventory" the next time
  somebody reaches for `_can_write`.
- **A passive created in the field reaches NO engine**, and that is the whole argument
  for handing creation to a worker. Passives are excluded from `org_device_topology` —
  the single choke point the FSMs, the rebuild fingerprint and `/edge/devices` all read —
  so recording one cannot re-page a fleet, and billing never meters it. Field creation is
  REQUIRED, not a convenience: most splitters have no row until somebody stands at one,
  so a tag-only survey would leave the passive plant (what branch-fault localization runs
  on) permanently unmapped. `test_a_field_passive_never_touches_the_engine_fingerprint`.
- **PROVENANCE IS THE FEATURE** (`accuracy_m`, `place_source`, `placed_by`, `placed_at`).
  A phone's first fix is a cell/wifi estimate at 30–80 m that GPS overtakes over ~10 s,
  so a field capture and a surveyed point are DIFFERENT CLAIMS about the same two
  numbers, and a splitter pinned 40 m off is a crew walking the wrong side of a road.
  Same rule as "nothing is wrong" vs "nothing is measured": the map may not render them
  alike. Unrecoverable if skipped — once 300 pins exist with no stamp, nobody can say
  which were measured.
  - **`use-gps-fix` WATCHES and keeps the TIGHTEST fix, never the first or the latest.**
    `getCurrentPosition` returns whatever is ready, which is the estimate; accuracy also
    doesn't improve monotonically, so a late loose reading must not undo a good one.
    Settles early at ≤8 m, gives up at 12 s, and stops the watch on unmount (a live watch
    drains a phone that spends all day in the field).
  - **A `gps` claim with no `accuracy` is DOWNGRADED to `manual`, not rejected** — every
    browser that can produce a fix produces `coords.accuracy` with it, so an absent one
    means the number came from somewhere else. The coordinates are still worth keeping,
    just not as a measurement.
  - **A desktop drag WIPES the stamp** (`set_org_device_location`). Keeping it would
    leave the map claiming a 9 m GPS fix for a point somebody dragged across a village;
    "unknown provenance" is the honest reading of a hand-placed pin.
- **The pin is DRAGGABLE, and that is the only way a handset beats its own chip**
  (`components/pin-adjust-map.tsx`, 2026-07-28). A GPS fix is a circle, not a point — 25 m
  of it is a whole compound — and the person standing there can see which rooftop the box
  is on. SATELLITE by default: a roadmap shows a street name, imagery shows the building
  and the pole line, and nobody identifies a drop from a road label. Deliberately not the
  map page in miniature (no clustering, no topology, no device pins) — one marker, one job,
  or it becomes a second map implementation to keep in step with the first.
  - **A dragged pin records `manual` with NULL accuracy**, and precedence is nudge >
    same-spot > GPS. Not because a hand-placed point is worse — it is usually far better —
    but because `accuracy_m` means "the radius this measurement is good to", and a
    hand-placed point has no such radius. Carrying the GPS figure over would attach a
    measurement to something never measured. Same rule `set_org_device_location` follows
    for a desktop drag.
  - **The map is FOLDED** (operator's call), like the device panel's Uplinks and "Paged for
    this device" sections: the common capture is "stand there, press save", and 208px of map
    ahead of the name field and the button made the routine case pay for the exception. The
    trigger prints the current coordinates so the fold never hides a decision, and it
    springs OPEN for a reopened placement — there, seeing where the pin already sits is the
    reason for coming back.
  - `FixReadout` must STOP leading with the accuracy chip once nudged, or the sheet prints
    "±8 m" above a point that number no longer describes. The sheet's action row is STICKY
    for the same class of reason: a 208px map pushed the save button below the fold of a
    scrolling container, and a capture flow whose save button must be hunted for is one
    that gets abandoned halfway up a pole.
  - **ANY second Leaflet map MUST keep `attributionControl` ON.** `GoogleLayer` mounts
    `GoogleAttribution`, which swaps its ToS line through `map.attributionControl` — and
    Leaflet only assigns that property when the control is created (`leaflet-src.js`:
    `if (this.options.attributionControl)`), so `attributionControl={false}` makes it
    `undefined` and the map throws "undefined is not an object" on mount. **It fails ONLY
    for an org that HAS a working Google Maps key**: a keyless org, or any org whose
    `createGoogleSession` fails, falls back to `StreetsTiles`, whose attribution is a
    static prop — so it passes every local test against a seeded DB and breaks in
    production. Showing Google's tiles without attribution is a terms violation anyway,
    which is why the wordmark overlay is duplicated here too.
- **A poor fix is NEVER a hard refusal.** Past `GOOD_FIX_M` (25 m) the primary button
  demotes to "Save anyway at ±N m" and the server keeps the number with its accuracy —
  blocking the save is how coordinates end up in a WhatsApp message instead of the DB. A
  worker under canopy still needs to record something; a loose pin that SAYS it is loose
  beats no pin. The server only rejects the absurd (>10 km).
- **The map is the VERIFICATION surface, not the capture one.** Pinch-zooming to drop a
  pin one-handed in the sun is how a splitter lands 200 m into a field, so `/survey` is a
  list + one big thumb-zone button and coordinates are never typed or dragged there. It
  is a separate route rather than a mode bolted onto `map-page.tsx`, which is already
  2,000 lines carrying three placement modes.
- **"Same spot as…" BORROWS the neighbour's exact coordinates** rather than taking its
  own fix: boxes in one rack are at ONE point, and independent fixes would scatter them
  by the accuracy radius and read as several sites (the same instinct as the map's 24 px
  drag-snap). A borrowed capture is `manual` with NULL accuracy — it inherits a position,
  not a measurement.
- **Live write + audit trail, deliberately no approval queue** (operator's call): 200
  pins × one approval click each is how a survey gets abandoned. "Placed today" with the
  accuracy chip is the field-side check; the owner reviews in bulk off `placed_by`.
- **A WORKER ON A PHONE GETS `/survey` AND NOTHING ELSE** (operator's call, 2026-07-28):
  `app-shell.tsx:FieldShell` — no sidebar, no bottom bar, every other path
  `<Navigate replace>`s to it, and the only chrome is the org name plus the account menu
  (logout has to stay reachable). The field handset is a survey tool, not a shrunken NOC,
  and everything else there was read-only anyway. This deliberately REINTRODUCES a
  viewport fork, retired once because a desktop resize changed the whole app — acceptable
  because nobody resizes a phone and the same worker on a laptop still gets the full
  read-only shell. It sits AFTER the billing-lock check so a locked org still shows its
  lock screen. `use-mobile.ts` had to start reading the viewport SYNCHRONOUSLY for this:
  its old `undefined`-then-effect value claimed "desktop" on first paint, which now
  flashes the entire desktop chrome before collapsing.
- The mobile tab bar (owners) is now SIX destinations, so its items are `flex-1 min-w-0`
  — six at a fixed `min-w-14` overflows a 320px handset.

**ONUs: LOCATING IS NOT WITNESSING** (the sharpest edge in this feature). Subscribers are
locatable too, but they are not `org_devices` rows — they live in the SNMP roster, so the
survey looks them up through `onu-search` (sticker MAC or provisioned name) and the pin
lands in `onu_places`, the table the REFERENCE-ONU feature already owned.

- **`onu_places.witness` splits the two claims, and `onu_place_macs` filters on it.**
  Placing a reference ONU IS the operator's claim that a subscriber's power is reliable —
  nothing detects it, and `ponfault._witness_verdict` reads it to call a dark PON a power
  cut (no crew) rather than a fibre cut (roll a van). Without the split, geo-tagging a
  street would enrol every drop as a power-backed witness and the next dark subscriber
  would read as PROOF of a cut. The column is `DEFAULT 1` because every row predating the
  survey WAS a witness — that backfills them correctly; the write paths pass it
  explicitly and never lean on the default.
- **The field route can neither create a witness nor destroy one.**
  `clean_field_onu_payload` has no `witness` key at all (not merely ignored — unsayable),
  and `field_onu` re-reads the existing flag and preserves it. The second half matters
  more: that claim is invisible on a handset, so a tech recording where a box sits must
  never silently cancel it. Reference placement stays owner-only on
  `/api/inventory/onu-place`. `test_locating_does_NOT_create_a_witness` /
  `test_locating_a_reference_ONU_does_not_cancel_its_claim`.
- **The MAC must be in the roster** (404 otherwise). A scrape can never add an ONU and
  neither can this; a pin on a typo'd sticker renders at a coordinate with nothing behind
  it. `place_onu_in_field` also leaves `notes` alone — desk knowledge about a site is not
  a location capture's to erase.
- **The map renders the two differently or the split is pointless**: `refKind` says
  "subscriber" vs "reference ONU", `.wisp-refonu--plain` keeps a located drop small and
  unhaloed, and `refZIndex` lifts a DARK pin only when it is a witness — a dark witness is
  a fibre cut with a coordinate, a dark subscriber is Tuesday. Once a fleet tags its drops
  these outnumber witnesses ~100:1.
- **The subscriber's NAME goes to `onu_places.label`, NEVER `onu_optics.name`.** The
  roster's name is whatever the OLT reports and the SNMP upsert rewrites it
  (`name=excluded.name`) every sweep, so a name typed into it would vanish inside ~300s —
  worse than not offering the field. The label is operator-owned and no walk touches it;
  `label || walked_name || serial || mac` is the display order everywhere (`refTitle`
  already used it). The capture sheet shows the walked name as the placeholder so the tech
  can see what the OLT calls it without being able to edit that.
  - **That display order is a FUNCTION, `onuroster.display_name` / `format.ts:onuName` —
    never a rule each screen re-implements** (2026-07-29). The first cut left it to the
    callers and every one of them named an ONU off `onu_optics.name` ALONE, so a name a
    worker typed reached the DB correctly and then rendered nowhere: the OLT's Optical tab
    printed "unnamed", ONU search couldn't find it, the WhatsApp lookup and the issue list
    both missed it. **A name visible only on the screen that captured it is
    indistinguishable from a name that was never saved** — which is exactly how it was
    reported from the field. What makes forgetting impossible now is that the row CARRIES
    the label: `store_snmp.list_onu_optics` and `org_onu_rows` LEFT JOIN `onu_places` (the
    join key is `_norm_mac` REGISTERED as a SQL function beside `wisp_search_key`, so SQL
    identity can't drift from Python identity), and both search paths — the
    `onu_search_device_ids` prefilter AND the in-Python filter — match `label` too, or an
    OLT whose only hit is a typed name never gets scanned.
  - **Customer names are stored UPPERCASE** (operator's call, 2026-07-29), normalized on the
    WRITE path at `inventory._onu_label`, the one helper all three writers to
    `onu_places.label` share (field capture, field rename, desktop reference-ONU dialog).
    Write-side rather than display-side so SEARCH matches what the operator sees and two
    entry points can't disagree about one sticker; `store._upper_onu_labels` carries existing
    rows across at startup so the list isn't half shouting. Both inputs uppercase AS TYPED —
    a field that shows something other than what gets saved is how a name gets re-typed. The
    **WALKED name is never touched**: that string belongs to the OLT.
- **RENAMING IS ITS OWN ROUTE** (`/api/inventory/field-onu-name`), not a placement with the
  old coordinates. Re-placing restamps `accuracy_m`/`place_source`/`placed_by`, so
  correcting a spelling would downgrade a real 6 m GPS fix to a hand-placed point and
  reattribute the visit. So: reopening a LOCATED subscriber seeds the map at its stored
  pin and the sheet becomes "Save name"; dragging the pin (or "Back to GPS") turns it back
  into a real placement. The SPA tracks `pin` and `moved` separately for exactly this — a
  seeded pin and a dragged one mean opposite things. A rename on an unplaced MAC is a 404,
  never an invented pin-less row that would enter the coverage count. It carries the NUMBER
  too (below) — the route is contact details, not a label.
- **NAME + NUMBER + LOCATION are captured together or not at all** (operator's call,
  2026-07-31; `onu_places.phone`). Two of the three don't make a row a crew can act on: a
  coordinate with nobody's name is a house you can't ask for, a name with no number is a
  visit you can't arrange. Enforced on the SERVER (`clean_field_onu_payload`), not in the
  sheet alone, so a SPA that forgets one can't write a row the field has to re-walk. **The
  correction route enforces the SAME three** — blanking a label used to be allowed there
  ("descriptive text can honestly be absent"), and once the field may not RECORD a nameless
  subscriber, leaving that door open would produce the same unusable row from the other
  side. Three consequences: `phone` is NULLABLE in the schema (every pin placed before this
  has no number, and a tech reopening one fills it in — that IS the backfill); the rule is
  deliberately looser than `api/users._WA_RE` (that one is a WhatsApp recipient and Meta
  demands international format; this is a number a human dials, so separators are stripped
  rather than refused and a bare 10-digit local number is valid); and the DESKTOP
  reference-ONU dialog keeps both OPTIONAL, because what that write means is the
  power-supply claim a PON verdict reads — refusing it for a missing customer record would
  trade a fibre-cut/power-cut discrimination for paperwork. A blank there never erases what
  the field captured (`COALESCE` on both columns, same discipline as the label).
- **The handset offers NEITHER the per-OLT customer expand NOR "New splitter / closure
  here"** (operator's call, 2026-07-31; both gated on the same `isWorker && isMobile` the
  shell uses). The OLT rows STAY — progress per OLT is the only thing that says how much of
  the job is left — but they report rather than open, so they render as a `div`, not an
  inert `<button>` that still depresses under the thumb, and the drill query is disabled
  rather than merely hidden. The way in is the search box: a tech at a drop is holding one
  sticker, not browsing somebody else's customer list. This narrows the SCREEN, not the
  permission — `field-passive` stays worker-reachable and unchanged, and an owner surveying
  on a desktop still gets the button.
- **A PLACED SUBSCRIBER HAS TO BE REACHABLE, or the survey looks broken.** The first
  placement (`hcs_babu`, 2026-07-28) saved correctly and was invisible: the subscriber
  layer is OFF by default (localStorage) AND only draws from a zoom floor (then a hardcoded 16,
  now an operator setting defaulting to 14 — `map/detail.ts`), so a fresh pin fails both gates
  at once. Three fixes, all needed: the Layers entry is
  now named **Subscribers** with a count (it said "Reference ONUs", which hid the survey's
  whole output under a toggle nobody would look under); `/map?onu=<MAC>` enables the layer,
  flies past the zoom floor and selects the pin — a QUERY param, not nav state, so it
  survives a reload and a shared link; and both the search row and the post-save toast
  offer it. **Not offered on the field handset** — `FieldShell` redirects `/map` back to
  `/survey`, so the survey page gates those affordances on the SAME `isWorker && isMobile`
  the shell uses, or it would render a link that bounces.
- **The zoom floor is SPLIT: marks at 14, their LINES and CHIPS at 16** (2026-08-02, operator
  asked for subscribers to survive zooming out longer). One floor at 16 was set on the
  assumption the layer arrives all together, and it made a surveyed area vanish at exactly the
  zoom where "where are the dark customers" gets asked. **What forced 16 was never the marks —
  it was the dotted drop lines**: below ~16 the whole span is a handful of pixels, it can't be
  traced, and a few dozen of them with their black casings smear into a smudge around every
  splitter, burying the plant the layer is subordinate to. Marks alone are safe two levels lower
  because a live located drop is already the quietest fill on the map (`--success` 32%), so a
  town's worth reads as texture while a dark one still shouts. **Chips gate on the LINE, not the
  mark** — a chip rides the line, and one that outlived it would also reserve pixels in the
  shared budget away from a link chip that really is drawn. `flyToOnu` (the `?onu=` deep link
  and the search box) lands past the LINE floor: a lookup naming one subscriber should arrive
  where everything about that subscriber draws, not where its pin merely starts existing.
- **The CUSTOMER NAME draws beside the mark, on its own floor (17)** (`refonu.ts:refNameIcon`,
  2026-08-02). The name a worker types while standing at the drop reached the DB correctly and
  then lived only in the hover title and the subscriber card — so a surveyed street rendered as a
  field of anonymous diamonds and "which of these is the complaint" meant clicking them one at a
  time. Four things it must keep:
  - **It is a SEPARATE MARKER, never a span inside `refOnuIcon`.** Icons are cached by their html
    string, so folding in a name the collision budget can turn on and off would swap the
    diamond's DOM node every time panning changed the budget — remounting every pin and replaying
    `wisp-mark-in`, i.e. a layer that flickers while it is being read. The rate chip is a separate
    marker for the same class of reason. The device pin's label can live INSIDE its icon only
    because it is gated by zoom alone, through a class on the wrapper, and never per pin.
  - **It joins the EXISTING screen-space budget, ranked last** (`chipShown.names`) — the
    documented rule that a new chip family joins rather than starting a third. A name and a rate
    chip collide with each other just as readably as two names do, and two budgets would each
    report themselves clear while the screen showed a smear. Going last is what makes the layer
    safe to leave on: in a dense area names thin out on their own instead of burying the readings
    and the plant. Ranked DARK-first within itself, like the rate chips. It reserves pixels at
    `REF_NAME_DY` BELOW the mark — where the text lands, not where the diamond does — so that
    constant and the CSS translate must stay in step.
  - **A dark WITNESS is named at every zoom the marks draw at**, the same exemption
    `.wisp-map-lowzoom` makes for a device pin in trouble: it is the name somebody is about to
    phone. Bounded by `refVisible`, or a name would float over a mark that isn't drawn. An
    ordinary offline customer waits for the floor — see the "only the dot" rule below.
  - **Quieter than a device label in ink, not in legibility** — `--muted-foreground` on an
    88% card plate, vs `--foreground` on 96%; only the dark one reaches full weight. **The plate
    is REQUIRED**: this sits over raster tiles running near-white to near-black inside one
    viewport, and the twice-paid lesson of this layer is that anything uncased vanishes over
    satellite. Judge it on real tiles in both themes.
  - **The label carries the Rx dBm, and its THREE REFUSALS are the feature** (`refHasRx`,
    operator's ask 2026-08-02). Each is a documented way this product has rendered a lie: a NULL
    reading prints NOTHING, never a dash (most of the C-Data/DBC fleet walks a complete roster
    with every `rx_dbm` NULL — "nothing is wrong" and "nothing is measured" must not render
    alike, and a map chip has nowhere to explain a dash); a STALE walk prints nothing (a dBm on
    screen carries no date, which is why `RxFreshness` exists on the panel — there is nowhere to
    put a date on a map label, so the gate replaces it); and a DARK ONU prints nothing (its
    stored Rx is whatever the last good walk saw, i.e. not now). A FOURTH, `frozen`, is passed in
    by map-page: a DOWN OLT freezes every reading behind it up to 15 min before staleness would
    notice, so the label drops the number and the HOVER CARD says why — the frozen rule's
    "always pair it with a live reason". (That reason used to be documented as living in the
    label's `title`, which was never reachable: `.wisp-refonu-name` is `pointer-events: none`,
    because a name plate that swallowed clicks would make a subscriber harder to open than
    before it was labelled. The card is where it actually gets said.) Gated on
    `optics_updated_at`, the OPTICS walk's own
    stamp shipped for this, **never `port_updated_at`** — different sweeps, and a fresh port
    table says nothing about the age of the light reading beside it.
  - **Graded by the SHARED `onuSev`, and TINTED ONLY WHEN WRONG.** `onuSev` moved from
    `optical-panel.tsx` to `lib/format.ts` for this — four screens now grade the same subscriber
    and a pure map helper importing a panel to get one rule is how a module graph knots. Never
    re-derive a verdict from `rx_dbm`: thresholds are per-OLT, so a second rule would call one
    drop healthy while the Optical tab calls it critical. `ok` stays the label's own muted ink
    (a healthy Rx is the overwhelming majority and none of it is news, so scanning stays a search
    for colour); warn/crit take the status tones at full strength — the one thing in this layer
    allowed to outrank its own quiet.
- **AN OFFLINE CUSTOMER GETS THE RED DOT AND NOTHING ELSE** (`refonu.ts:isRefEvidence`,
  operator's call 2026-08-02: "for offline customers i don't want anything special treatment,
  only dot should be red thats it"). **Dark emphasis is for WITNESSES**, and `isRefEvidence`
  (`witness && isRefDark`) is the ONE predicate all of it gates on: the drop line's tone and
  weight (`refLineTone`), the name's ink and weight, the name's exemption from the zoom floor,
  first claim in both chip budgets, the rate chip's alarm tone, and `refZIndex`'s lift. The mark
  itself needs no gate — `.wisp-refonu--plain` already resets the size bump and halo after
  `--dark`, which is why that source ordering is load-bearing twice over.
  - **Why it is not just taste**: power cannot darken a subscriber the operator vouched for, so
    a dark witness IS a fibre cut with a coordinate — but thousands of ordinary drops go offline
    every evening, and drawing each one as an alarm is a wall of red nobody can act on. Same
    sentence CLAUDE.md already carried for `refZIndex` ("a dark subscriber is Tuesday"), now
    applied to every channel instead of just stacking order.
  - **`isRefDark` still answers "is this ONU down"** for counts, PON chips, the subscriber card
    and the search panel's dot. `isRefEvidence` answers only "may the map raise its voice", and
    nothing outside display may ask it. The one deliberate exception is `troubleOnly`, which
    reads bare `isRefDark`: that switch is the operator asking to SEE problems, and an offline
    customer is one — it decides what is drawn, never how loud.
- **…and those floors are a SUPERADMIN SETTING, not constants** (`central/mapdetail.py` +
  `map/detail.ts`, 2026-08-02). Hand-tuning a threshold every time somebody says the map is too
  busy or too empty is the same shape of ask as "make the palette warmer", and gets the same
  answer the theme panel already gives: **a density ask is a dashboard control, not a code
  edit** — check this before editing a number. Five rows (device NAMES / PASSIVE PLANT + the
  cable into it / subscriber marks / subscriber NAMES / drop lines + their chips), in
  **Settings → Platform → Map detail**, stored
  in `app_settings.map_detail`. The card is driven off `DETAIL_ROWS`, so a new layer is a row in
  that table plus a key in both defaults — never JSX (`test_mapdetail:
  test_every_field_is_OFFERED_a_row_in_the_settings_card` fails if the two drift, because a
  field central validates but the form never shows is a knob nobody can reach).
  - **ONE configuration for the whole install** (operator's call). It shipped first as a
    per-browser localStorage preference in the map's Layers popover and was pulled back the same
    day: density is a judgement about how this product should read, made by the person who looks
    at the fleet all day, and handing it to every account buys a support surface ("my map looks
    different from yours") in exchange for a choice nobody else asked to make. If a per-user
    override is ever wanted again it goes ON TOP of these values, never instead of them.
  - **It rides the `/api/orgs` reply, exactly like `google_maps_key`** — the map already reads
    that row for the key, so a server-wide map setting costs no extra fetch and shares its
    invalidation (the Platform card invalidates `["orgs"]` as well as `["admin-settings"]`).
    It is NOT org data: every row carries the identical object. Workers read it too — a map
    drawing on different rules for them is the same problem in miniature.
  - **ONE ordering invariant — nothing draws at a zoom where its own MARK doesn't**, **and it is
    not cosmetic**: `refLinesVisible` and `refNamesVisible` are both `refVisible && …`, and a
    drop line is suppressed with the splitter it runs to, so a floor set below its mark's doesn't
    draw it earlier, it does nothing — and a setting that silently no-ops is worse than one
    that refuses. `subscriber_names` floors at `subscribers`; `drop_lines` floors at BOTH
    `subscribers` and `passives`, because a drop line is the one row with a mark at EACH END and
    a dotted line running to a point where nothing is drawn reads as a rendering fault. The two
    dependents are INDEPENDENT OF EACH OTHER (a name rides the mark, a
    rate chip rides the line), so naming subscribers without drawing their drop lines is a
    legitimate setting and must not be "repaired" — and `passives` is likewise independent of
    `subscribers` in both directions (the shipped default draws plant one level EARLIER).
    `detailMin` is EXPORTED and `normalizeDetail`
    is written in terms of it, so the stepper's disabled state and the value stored can't
    disagree about where the floor is; `mapdetail.clean` repairs it AGAIN server-side, on the
    write AND on the read, so a hand-edited SQLite row can't reach the map broken either. One
    repair covers every way of breaking it, so no "which knob moved" state is needed.
  - **`passives` hides the PIN AND THE CABLE INTO IT, and it is the one row with an ALARM
    EXEMPTION** (2026-08-05, operator: "i want splitters to disappear as well at certain
    configurable zoom level when zooming out"). It is the other half of taking plant out of the
    clustering pass the same day: the accepted cost there was that dense plant OVERLAPS at low
    zoom instead of folding, exactly what subscribers do — so plant gets the answer subscribers
    already had, a zoom floor. Four things it must keep:
    (1) the pin and its cable stand down TOGETHER — `map-page.tsx:drawnDevices` feeds BOTH
    `buildClusters` and the `links` memo, so the hover probe, the rate chips and the branch-fault
    overlay all inherit one decision instead of re-deriving what is visible, and no line can end
    in empty ground;
    (2) a passive whose recorded subscribers are DARK is exempt at every zoom **and so is the
    plant ABOVE it** — a branch fault names the SPAN between two pins, which is the whole output
    of that feature and where a van drives, so dropping either end takes an alarm off the map;
    the ancestor walk is also what keeps that overlay's own link in `drawnLinks`. `frozen` is
    computed exactly as the pin computes it, so the exemption can never disagree with the tone it
    is drawn from;
    (3) the SELECTED passive is exempt — its panel is open, and a panel floating over nothing is
    the failure this map is careful about — as is every input surface where plant is what the
    cursor is aiming at (`plantPinned`);
    (4) it filters `drawnDevices`, **never `placed`** — the "N / M on map" census, Fit-all,
    search and the drag-snap still reach every placed box, because hiding a reference layer must
    not make the fleet look smaller than it is.
  - **An untouched install stores NOTHING** (`mapdetail.save` clears the row at defaults, which
    is also what Reset posts), so a future change to the shipped numbers still reaches everyone
    who never expressed an opinion — the same sparse-storage rule as the theme overrides.
  - **Defaults are MIRRORED in Python and TS on purpose** — the SPA must draw before
    `/api/orgs` resolves and central must validate without asking a browser. `test_mapdetail.py:
    SpaAgreementTest` reads the TS source to pin them together, the same way the theme allowlist
    test does; a drift would have the map render one thing and the settings form report another.
  - Coercion is **per field, never per object**, on both sides: a row written before a field
    existed must not discard the fields it does carry, and junk must degrade to the shipped
    number rather than to NaN/None — those make every `zoom >= n` false and read as "the layer
    is broken" rather than as a bad setting.
  - **`lowZoom` is now DERIVED** (`zoom < detail.labels`) rather than a second state set beside
    `setZoom`. That pairing is fine while the threshold is a constant and a stale closure the
    moment it becomes a setting the callback would have to close over.
  - Nothing outside display reads any of it — no alarm, count, verdict or page — which is what
    makes it safe to hand to a form. Device pin DOTS, their status tone and the down-pulse ignore
    `labels` entirely, and trouble/selected keep their names at every zoom, so even the extreme
    setting cannot hide a device that is down.
  - Tests: `unit/test_mapdetail`, `integration/test_central_mapdetail` (superadmin-only write,
    owner/worker read off the org row, the invariant repaired server-side, and that saving some
    other platform setting doesn't reset the map).
- **…and once a fleet surveys in bulk, REACHABLE stops meaning ALL AT ONCE** (2026-07-29).
  Thousands of drops drawn together is a texture, not a map, and it does not match how a
  fault arrives — "EPON0/4 has five crit ONUs" is a question about ONE PON.
  `map-page.tsx:onuScope` ({deviceId, pons}) focuses the layer on one OLT and any SUBSET of
  its PONs, entered from the map device panel's **located** row — a dropdown of checkboxes,
  its trigger reading the current selection ("Show on map" / "All PONs" / the PON / "N PONs")
  — and toggled just as freely from the PON chips in the status strip. `pons` is a SET and an
  EMPTY one means EVERY PON: un-ticking the last one has to land on the whole OLT, never on a
  focus that draws nothing, and "All PONs" therefore CLEARS the ticks rather than being one
  more of them. Multi-select is the operator's explicit call (2026-07-29, replacing
  one-PON-at-a-time): a village's feeder and cascade carry two or three PONs, so "is the whole
  area out or just that PON" is a question about a set, and clicking between chips from memory
  is what a map is supposed to spare you. The menu STAYS OPEN across ticks (`onSelect`
  prevented) and the map re-fits under it, so the selection is built and judged in one gesture;
  an OLT with a single surveyed PON keeps the plain toggle button, since a menu whose every
  path does the same thing is worse than the button it replaced. Five things it gets right and
  must keep: it is SEPARATE state from the
  `refOnus` toggle (that is the operator's remembered preference, a scope is what they're
  working on now — clearing it must restore their setting, not decide for them); it
  **bypasses BOTH zoom floors**, because they exist to stop a fleet's worth of pins,
  not to hide one OLT's dozen; it **NEVER TOUCHES THE ZOOM** (below); the bar lives IN the
  status strip, not as
  a floating card, because `top-14 left-3` already belongs to the unplaced drawer and the
  subscriber card — and a map hiding most of its content must SAY SO on the map, or the next
  person at the wall reads a scoped view as the whole network. And PON chips are built from
  PLACED subscribers with a DARK count, never from the OLT's PON list: a chip for a PON
  nobody surveyed would filter to an empty map and read as "this PON is dark". The panel row
  says **"N located"** (of the roster where known), never "N subscribers" — same rule as the
  splitter panel's "recorded". The strip sits at **z-[1002], one rung above every floating
  card** (all z-1000): the unplaced drawer, the site card and the subscriber card all open at
  `top-14 left-3`, exactly where the strip wraps once the focus bar joins it, and a z-index tie
  breaks on DOM order — so those cards were burying the search results and the PON chips.
  - **…and the focus narrows PASSIVE PLANT with the drops** (`plant.ts:plantInScope`,
    2026-08-06, operator: "only customers are being filtered, i also want splitters filtered as
    well"). It used to PIN plant instead — `onuScope` was one of `plantPinned`'s clauses — so
    focusing one OLT during a cut still drew every splitter in the org. A focus is not "show me
    everything"; it is the operator naming one OLT, so plant now follows the subscribers.
    Measured on badri_fiber: 19 placed splitters → 8 / 5 / 6 under the three OLTs, which
    partitions them exactly. Four rules:
    - **A box a DRAWN subscriber hangs off is kept whatever the topology says.** That drop line
      has to have both ends — the same reason `drop_lines` may never floor below `passives` —
      so a mis-recorded drop pointing at another OLT's splitter keeps that splitter drawn.
      Fed `shownPlaces`, not `places`, so a PON pick narrows the net with the drops.
    - **Otherwise it is the FEED CHAIN**: `feedChain(d).head` is the scoped OLT, on a picked
      PON (`ponFor` walks the chain, since a cascade leaves `pon_port` blank below the first
      hop). The chain is what holds for a splitter nobody has recorded a drop on yet, which on
      a fresh survey is most of them. Plus the chain ABOVE anything kept, or a cable ends in
      empty ground.
    - **A box the record can't place on a PON STAYS under a PON pick.** `pon_port` is
      operator-entered and gets filled in out of order — on the live fleet 20 of 22 splitters
      carry none — so a blank column says nobody wrote it down, never "on some other PON". A
      stricter rule empties the map. Same instinct as "recorded is never occupied".
    - **GEAR IS NEVER NARROWED, and `plantPinned` still wins outright**: a switch has a state
      and an outage of its own, and picking a parent for a new splitter means reaching boxes
      outside the focus. The narrowing deliberately does NOT carry the dark-splitter zoom-floor
      exemption across — it is announced on the map and one click to leave, and it already
      hides the dark SUBSCRIBERS under another OLT's plant, so keeping their branch-fault span
      drawn over customers that aren't would be the louder lie.
  - **THE FOCUS MAY NOT MOVE THE ZOOM** (2026-08-06, operator: "if I were to add a filter on
    OLT, zoom is being adjusted — I don't want that"). It used to `flyToBounds` the scoped set,
    documented above as one of the five things it "gets right and must keep". That was
    backwards: an operator picks the zoom that suits the street they are looking at, and a
    FILTER is supposed to thin what is drawn there, not re-frame the map underneath. Adding
    plant to the fit the same day made it obvious — one splitter across town pulled the frame
    out to the whole district. What the old fit was right about survives as a floor: the map
    **PANS, at the current zoom, and only when nothing the focus reveals is on screen at all**,
    so a filter can still never leave you staring at an empty map. A `panTo`, never a `flyTo`,
    which arcs through other zooms on the way. The on-screen test EXCLUDES the OLT — the focus
    is entered from its own panel, so counting it would make "reveals nothing" unreachable.
    Ticking PONs therefore never moves anything, which is the case that matters: comparing two
    PONs means watching ONE patch of ground gain and lose drops, and a re-frame per tick
    destroys exactly that. Measured: apply = 0 zoom / 0 m, PON tick = 0 / 0, a focus dragged
    off screen = 0 zoom / 8.4 km pan.
- **The map SEARCH box finds subscribers, not just devices** (2026-07-29, `map/search.tsx`).
  A tech holds a sticker MAC or the customer name typed in the survey, and neither is an
  `org_devices` row, so a box that only knew devices answered "nothing found" about a drop
  surveyed that morning. TWO sources on purpose: the PLACED set (`onu-places`, matched
  client-side and instantly, sharing map-page's own cache — the answer to "where did my pin
  go") and the ROSTER (`onu-search`, debounced like the geocoder, cache shared with Network and
  Survey), because mid-survey nearly every subscriber is unplaced and "no such subscriber"
  would be the wrong answer. A placed hit flies + selects through the SAME `flyToOnu` the
  `?onu=` deep link uses; an unplaced one names its OLT and STOPS — it deliberately does not
  arm placement the way an unplaced DEVICE does, because map placement writes a WITNESS claim,
  and that is made where the contract is stated, never as a side effect of a search. Rows are
  two-line (name over "OLT · PON"): on a 288px panel one line truncates the half that answers
  the question. `focusFlying` guards the selection during the flight — `zoom` state only lands
  at zoomend, so selecting a pin as a flyTo STARTS had the visibility guard judge it against
  the zoom being left and close the card before it drew (this hit `?onu=` links too, from any
  view below zoom 16). The punctuation-blind needle is `format.ts:onuSearchKey`, ONE mirror of
  `onuroster.search_key` shared with the Network page.
- **`onu_places.witness` ships as a real BOOLEAN** (`api/devices.py:onu_places` casts it).
  SQLite hands back 0/1 and `{**p}` shipped that raw against a SPA type declaring `boolean` —
  which JS reads wrong in both directions: `w === true` is never true (the survey list's
  "reference" chip could not render, the one warning that stops a witness being re-pinned) and
  `{w && <Chip/>}` renders a literal "0" beside the name. Cast at the edge, where the type is
  declared.
- **Coverage is per-OLT and the denominator is the freshest-walk roster**
  (`/api/inventory/onu-coverage`). The survey's first cut counted only `org_devices` and so
  read "0 left" the moment the gear was done, while 2,155 of 2,156 subscribers had no pin —
  a coverage figure nobody can see is a survey nobody finishes. Equipment and subscribers
  are counted SEPARATELY (tens of boxes vs thousands of drops; one merged "N left" serves
  neither), and the unplaced list ships only for a NAMED OLT because the fleet-wide set is
  thousands of rows and nobody works a list like that — they work an area. Sorted by
  slot, not by state, so a tech keeps their place between visits. Zombie slots are
  excluded by `current_roster`, or the denominator would include drops nobody can find.
  `onu_place_macs(witness_only=False)` is the one caller that asks the wider question —
  it defaults True so no alerting path can accidentally widen to every located drop.
- **An opened OLT lists the LOCATED half too, first, under a green wash** (2026-08-01). It
  used to show only what was left, so the expanded list contradicted the `placed/total` on
  the row that opened it, and a tech back on a street they had half-surveyed could not see
  which drops were already recorded. Four things it must keep: (1) both halves come off ONE
  roster pass and one placement set in `onu_coverage`, so `len(located)` IS that counter
  rather than a second derivation — deriving it client-side from `onu-places` was rejected
  because an AMBIGUOUS MAC resolves to no `device_id` there and would silently shorten the
  list below the count. (2) A done row CARRIES its label, phone and pin, because the only
  reason to tap one is to correct something and the sheet defaults to a RENAME — arriving
  without the pin would re-place the subscriber and restamp a real GPS fix as hand-placed
  (`field_onu_name` exists for exactly that split). (3) Done rows are their own BLOCK, not
  interleaved into slot order: HLY-OLT-2 carries 219 subscribers of which 5 are located, so
  in walk order the green sits three screens down — a "done" mark nobody can see. Slot order
  still holds within each block. (4) The green is measured PER MODE
  (`bg-success/[0.18] dark:bg-success/[0.11]`) — the two `--success` tones sit at opposite
  luminances by design, and the alpha that reads as a clear tint on near-black lands at
  #E7EEEB on white, a grey nobody would call green. Judge it on screen in both themes, never
  by matching the two numbers.
- Tests: `integration/test_central_survey` (the gate, the refusals, provenance, the
  engine-fingerprint guard, the whole witness split, and the located half's count agreement).

### Worker location tracking: an off-the-shelf tracker, not an APK (2026-08-01)

`central/field.py` (ingest rules + prune), `store_field.py`, `api/field.py`, the public
`GET|POST /field/track` in `server.py`, `map/workers.ts`, `components/field-tracking-card.tsx`.
Workers run **Traccar Client** — free, open source, Android AND iOS — which POSTs OsmAnd
fixes; the owner sees the crew on `/map` under Layers → Workers. **No APK work at all**: the
`wisp-field-app` Capacitor shell stays a pure webview and this feature never touches it.

- **Off-the-shelf, not ours** (operator's call). Location was the last reason to write native
  code — push was declined, because the WhatsApp bot already delivers an assignment with a
  working [✅ I'm on it] button. Traccar brings years of Doze/OEM-battery tuning we would not
  match, and iOS for free. Don't re-propose a tracking APK.
- **ON-SHIFT ONLY, and the tracker's own ON/OFF switch is the real toggle.** When it is off
  the phone transmits nothing. "Always transmit, discard off-shift server-side" was
  deliberately NOT built: receiving a worker's evening and choosing not to store it is a much
  worse promise than not receiving it. The **Start/End shift button** (`/survey`, worker's
  only mobile screen) is a SECOND, explicit declaration and the two-tap cost is the point —
  **when somebody marks on-shift and no fixes arrive, that gap IS the "the OEM battery
  manager killed the service" alarm**, which no server-side code could otherwise detect. It
  is a feature, not redundancy, so nothing may infer a shift from the fixes.
- **Auth is a per-worker token in Traccar's `id` field**, the node-token pattern verbatim
  (SHA-256 hash only, shown once, rotatable, never recoverable — the panel says so). That
  placement is what keeps the **server URL identical for every worker**: one string on
  screen, in a QR, and readable down a phone line, while identity stays per-person. Resolves
  to `(org_id, user_id)`; a revoked token or a DEACTIVATED account resolves to nothing.
- **BOTH VERBS, query string OR form body.** Client builds differ and a fix silently dropped
  because we handled one shape is the worst failure here — it is invisible from every screen.
- **A REFUSAL THAT IS OURS ANSWERS 200, NOT 4xx.** Traccar re-sends anything it did not get a
  2xx for, IN ORDER, so a 4xx on a fix we will never accept WEDGES the offline buffer behind
  it and the newer positions never arrive. So: too vague (accuracy > `field_track_max_accuracy_m`,
  500 m) and too old/future → `200 {"stored": false, "reason": …}`; only a MALFORMED request
  (no/out-of-range coordinates) 400s; a bad token is a flat 401 (right to wedge — fix the
  handset and the backlog delivers itself); the rate cap 429s. The past window is
  deliberately generous (24h): offline buffering is a setting we RECOMMEND, so a morning
  replaying at once is the feature working.
- **The rate cap is a token BUCKET, never a minimum gap** — a gap would throw away exactly
  that buffered burst. `UNIQUE(org_id, user_id, ts)` + INSERT OR IGNORE makes a replay
  idempotent, or a flaky link renders as a stutter in the trail.
- **Speed arrives in KNOTS** (the OsmAnd protocol's unit; Traccar Client converts m/s before
  sending) and is converted ONCE at ingest. Same class of trap as the dbc profile's
  `distance_m` in time quanta.
- **7-day retention, pruned daily** (`field.start_field_prune_thread`, the `rollup.py` shape).
  Not housekeeping: the window is the whole answer to what this keeps about staff. A crew at
  the designed 90 s cadence is ~6 MB/month, so this was never a volume problem.
- **Ungated by billing**, consistent with edge ingest — a lapsed bill must not silently stop
  recording where staff are. Not an `/api/*` route, so it is out of `api/__init__`'s tables
  and out of the worker allowlists by construction, beside `/whatsapp/webhook` and `/report`.
  **Nothing logs the request line** — the token rides in the query string and `log_message` is
  `log.debug` for exactly that reason.
- **FOUR STATES, never collapsed** (`map/workers.ts:workerState`) — the specific lie this
  prevents is always the same one, "last known 40 minutes ago" drawn as "here now":
  `live` (on shift, fix fresh) · `quiet` (on shift, gone quiet — THE alarm) · `off` (shift
  ended, not a fault) · `never` (set up but never reported). `never` has no coordinates, so
  it is not a mark but a COUNT on the layer toggle — without it a crew whose phones were
  never provisioned renders identically to one that has all gone home (same rule as the
  splitter layer's "N of M mapped"). **Classified in the SPA, not shipped**: freshness ticks
  with the clock, and a state stamped at response time keeps claiming "here now" for as long
  as the tab is open. The THRESHOLD is server-owned (`fresh_s`) so it still has one source,
  and `field-tracking-card.tsx` reads the SAME `workerState` — the panel and the map
  disagreeing about whose phone is working would be worse than either being absent.
- **The layer keeps the Subscribers layer's discipline**: opt-in + remembered
  (`wisp:map:workers`), out of the clustering pass, every element `interactive={false}`, and
  stacked BELOW every device pin AND below the subscriber layer (`workerZIndex`, -250/-300).
  Its mark is a **rounded square carrying INITIALS** — the only mark here with text in it,
  which is what stops a person reading as plant, and it answers the question three vans
  actually raise ("which one"). It takes **no status tone**: success/warning/destructive here
  are claims about the NETWORK. The one exception is `quiet`, which really is an alarm.
- **Two things were fixed only because they were checked on real satellite tiles** — the
  ref-ONU lesson, twice over. A hollow dashed `off` mark and an uncased trail BOTH vanished
  outright over imagery. The three states now differ by HUE (grey / steel / amber), which
  survives any backdrop, and the trail carries the same dark casing every other line does.
  Judge these on tiles in both themes, never on a swatch. The trail is **SOLID**, unlike every
  other subordinate line: a dash here means "not a surveyed path", and a GPS trail is the one
  line on the map that IS measured.
- **"Today" is the OPERATOR'S day** (`field.trail_since` → `notifiers._display_zone`,
  `WISP_DISPLAY_TZ`). A UTC-midnight boundary cuts an IST trail at 05:30 and drops the
  morning's driving while the van is still out. Same zone choke point WhatsApp times use.
- **`/api/field/shift` is the ONLY worker-writable route this adds** (and its GET the only
  worker-readable one): a statement about themselves, with `org_id`/`user_id` from the
  SESSION, writing no location and naming no device. Both start and end are IDEMPOTENT — the
  button and a stale tab press the same thing, and two overlapping shifts would make "when did
  he start" unanswerable. `/api/field/workers` and `/api/field/tokens` are owner-only: where
  the crew is, is the owner's view of the org.
- **The setup panel is Settings → Users → Location tracking**, written as STEPS AN OWNER READS
  OUT, because that is literally how it gets used. It carries the constant server URL, the
  one-time token + QR (generated CLIENT-side with the `qrcode` dep the 2FA card already uses —
  central stays pure stdlib), the duty cycle we designed (**90 s / 30 m / High / offline
  buffering ON** — the app's defaults are not it), and the **OEM warning**, prominently:
  Xiaomi/Realme/Vivo/Oppo autostart managers kill background services silently and **no
  server-side code can fix it**. That warning is the single most likely reason tracking will
  look broken; an owner who doesn't know it concludes the feature is broken.
- The roster is driven off `users`, not off issued credentials — a list of only the people
  already set up can never show you who is missing.
- **Before rolling out to a fleet:** run one Xiaomi or Realme handset for a full working day.
  If it survives that it will survive the rest, and if it does not, no amount of server work
  fixes it.
- Tests: `integration/test_central_field` (both verbs and both param shapes, every refusal and
  which status it answers, token/deactivation/rotation, org scoping, the worker allowlist,
  shift idempotence, the prune, and the org/account delete cascades).

### C-Data / DBC: two hardware truths (don't re-derive)

- **Per-ONU Rx exists NOWHERE in that firmware's SNMP.** FINAL — warm-capture against web-UI
  truth values, then the whole vendor ONU area swept (`…5.12.1.27`–`.33`): the only optical
  column is the OLT's burst-receiver level, uncorrelated with the ONUs it indexes, and it
  covered *exactly* the one PON that had been opened in a browser. The OLT queries each ONU
  live over EPON-OAM when you open OPM Diag and stores nothing. Blank Rx on the `dbc` profile
  is CORRECT; the web page is the only source. Tool for the next vendor: `oidhunt.py`
  (correlates a walk dump against a pasted web table by MAC join).
- **`distance_m` on the `dbc` profile is RTT in EPON time quanta, NOT metres.** The OLT's own
  ONU-list page heads that column `RTT(TQ)`, matching our stored values. True metres =
  `1.6393 × TQ − 157` — so **every fibre-cut bracket we have printed is ~39% short**, and
  splicing crews quote drum off those. The OLT publishes real metres itself in field 14 of the
  `.31.1.3` CSV. Parsing a packed CSV is outside the profile vocabulary, so the
  exact fix needs edge code; `scales.distance = 1.6393` is the zero-rollout approximation
  (157 m high). Don't "fix" this by widening the scale vocabulary alone.
- **The SYROTECH GPON build publishes NO ranging distance at all** (2026-08-03, narrow probes
  256–261 — the whole ONU MIB under `…1.1.6` is now mapped and the hunt is CLOSED). T1 has 5
  columns, T2 6, T3 7 (`.3.1.8` returns zero varbinds), T4 8+ whose cols 6–8 read `0` on all 195
  slots, T5 is 3 scalars. Those zeros are unpopulated counters, NOT distance: mapping one prints
  "0 m" for every ONU, which the Optical tab renders as *unranged*. Same class of verdict as "no
  per-ONU Rx in the EPON firmware" — a hardware fact, not a gap to code around, and the web-UI
  route is the only way in (that build 404s on `OPM_PATH`, so it needs a real `proxy_audit`
  capture, never a guessed page). Cost is low: branch-fault localization off splitter/drop
  records already beats the ranging bracket on this fleet. Also settled — `col2` **is** the row
  index in both T1 and T2 across all 374 slots, so `onu_id` is correct; an older note claiming it
  hid a separate ONU id was wrong. T4.col5 is a second, WORSE serial column (122 unique with
  blanks vs T2.col5's 184 at full coverage).

### Per-ONU Rx from the OLT's web UI

`central/weboptics.py` (transport+parse+merge) + `weboptics_sweep.py` (shell). CENTRAL-ONLY by
construction: it rides the web-proxy tunnel the edge already serves — no edge code, no rollout.

- **The scrape is an INPUT to the optics fold, never a second pipeline.** Readings land in
  `onu_web_optics` (its own table — the two run on independent clocks, and one row carrying both
  would make "which half is fresh?" unanswerable), and `_merge_web_optics` folds them in BEFORE
  `CentralOpticsMonitor.sync_device`. Severity, the OLT badge, PON-fault and the Optical tab
  stay ONE path that never learns where a number came from. Don't teach it a second source.
- **Merge is by MAC, ONLINE slots only, ambiguity DROPPED.** MAC survives the two firmware
  subsystems disagreeing about slot numbering; the online restriction disposes of the zombie
  problem for free. A still-ambiguous MAC gets NOTHING — a reading pinned to the wrong drop
  sends a tech to the wrong house. Matching is punctuation-blind (`_match_key`), a THIRD
  normalizer beside `_norm_mac` (identity) and `search_key` (search), deliberately: exact
  matching across two views would merge nothing while looking healthy, and a blank Rx column
  reads as "this vendor has no Rx" — the exact false negative this feature exists to kill.
- **`distance_m` is stored but NOT merged, on purpose.** The page carries metres, the dbc
  profile time quanta, and the page returns only ONLINE ONUs — so `onu_optics.distance_m` would
  go metres-for-survivors / quanta-for-dark, and `ponfault` brackets a cut between exactly those
  two groups. A mixed-unit interval INVERTS; a uniformly wrong one stays monotonic. Fix the UNIT
  first. `_MERGED_FIELDS` is the knob.
- **SNMP stays authoritative for the roster.** A scrape can never add an ONU and a blank scraped
  column never erases a walked value — which is what makes a partial scrape safe (the OLT holds
  ONE session slot, so ending early is normal). UPSERT, never delete-then-insert. Freshness is
  judged on CENTRAL's clock, NOT the report's `ts` (that rides in from the edge, and a probe
  with a slow clock would discard seconds-old readings). Past `web_optics_max_age_s` (1h)
  readings are dropped whole rather than aged in — a badge is a claim about now.
- **The sweeper's restraint is the feature.** Own slow clock (`web_optics_interval_s`, 900s — Rx
  drifts over days), strictly sequential, per-OLT lock. SKIPS a node whose tunnel isn't
  long-polling, and an OLT someone is actively browsing (that firmware keeps no cookie and holds
  one session slot, so scraping mid-browse logs the operator out of the box they're working on).
  Handed NO notifier, so a failed scrape can page nobody by construction.
- **The browse gate keys on ACTIVELY browsing, not "has a session"**: nothing tells central a tab
  closed (the tunnel is request/response), so an abandoned session lived its full
  `proxy_session_ttl_s` (600s) — and the gate is per-NODE, so one forgotten tab suppressed the
  optical read of EVERY OLT behind that probe, indefinitely against a self-refreshing device UI.
  Three parts, the second the one that always holds: `last_used_at` + an `idle_s` arg to
  `active_sessions_for` that the sweeper passes (`web_optics_browse_idle_s`, 180s) and the EDGE
  path deliberately does not; `ProxyHub.reap_expired` at the top of each sweep (sessions were
  only ever dropped when something looked one up, and the browser is what went away); and the
  dashboard closing a session when its tab vanishes (`watchSessionTab`) — a nicety, deliberately
  NOT hooked to `pagehide`, which fires on a reload and would kill a session in use elsewhere.
  `has_session` is expiry-aware for the same reason: it fed the "live" badge and pulsing globe.
- **The 15-minute clock is right for the VALUE and wrong for the MOMENT.** Rx drifts over days so
  the sweep shouldn't hurry — but a tech at a pole who just reseated a connector needs the answer
  now, and "wait 15 minutes" is how a diagnosis becomes a second site visit.
  `POST /api/inventory/rx-refresh` (owner-only — it spends the OLT's stored admin login) drives
  `scrape_one` on the SAME sweeper instance so the per-OLT lock covers both. It widens WHO may
  ask and WHEN, never what a read may do: every gate applies unchanged, and eligibility has
  exactly ONE source — `WebOpticsSweeper.target()`, which the route gates on AND `rx_status`
  answers `can_refresh` from, because the button, the route and the sweep must agree about what
  is readable. An ineligible device is refused 400 and records NOTHING (a `web_optics_status` row
  reports an ATTEMPT; writing "you can't read this" would erase what really happened last). It
  answers at once and scrapes on a thread — one OLT costs up to `web_optics_device_budget_s`
  (120s), and a request held that long is a browser timeout. A second click 409s rather than
  racing the lock and overwriting a good verdict.
- **ELIGIBILITY IS THE VENDOR PROFILE, NOT THE DROPDOWN** — this took the subsystem off PYLON and
  onto the fleet (2 targets → 12, ~1,030 more ONUs with a real dBm). `web_optics_targets` accepts
  an explicit `gpon_vendor='dbc'` OR the edge's own sysObjectID match, which arrives on every
  report as `device_snmp_status(subsystem='optics').profile` + `.sysobjectid`. The original gate
  was justified by "auto-detection lives on the edge and is never reported to central" — simply
  WRONG, and it cost this feature a fleet: 13 of 19 OLTs were already stamped `dbc` off the
  C-Data PEN arc `1.3.6.1.4.1.37950` while only 3 carried the hand-typed vendor. Detection is the
  STRONGER signal — the box's maker ID versus a human's recollection. Requiring `sysobjectid`
  non-empty is load-bearing: `profile` is echoed for an override too, so a fleet-wide
  `WISP_GPON_VENDOR` could otherwise launder itself into a "detection".
- **A roster is required, and PON COUNT COMES FROM IT.** `DEFAULT_PONS = (1,2,3,4)` was PYLON's
  port count read as the fleet's; the same firmware ships 3–8 PONs with GAPS (HILL-OLT-1 runs
  1,3,4,5,6,7,8), so widening the vendor gate alone would have skipped over half the fleet's
  online ONUs **while logging success** — and blank Rx meaning "we never asked" is
  indistinguishable on screen from "this vendor has none". `pon_indices` parses the roster's
  distinct `pon_port` labels (`EPON0/3` → 3; junk like a bare `60` or an empty label DROPPED,
  never guessed); `DEFAULT_PONS` survives only as the fallback for an unrecognised shape.
  Requiring a roster follows from the merge: readings land ONTO walked slots and can never create
  one, so an OLT with no roster has nothing a scrape could surface.
- **A SENSOR RAIL IS NOT A READING** (`_sane_optics`). An ONU whose diagnostics are dead prints
  the raw register on every DDM field at once: 0xFFFF reads 6.55 V / 131.07 mA / **+8.16 dBm**,
  0x0000 reads 0.0 V / 0.22 mA / **−40.0 dBm**. Both were live on HILL-OLT-1 and they fail
  OPPOSITE ways, which is why a range check on Rx alone won't do: +8.16 grades comfortably `ok`
  (dead optics rendering as the healthiest drop on the PON) while −40.0 grades `crit` and joins
  an OPTICAL_CRIT page. **Supply voltage is the discriminator, not any optical threshold** — an
  ONU's rail is 3.3 V by design on every ONU ever built, the one field whose correct value is
  known a priori. Outside 2.0–5.0 V the WHOLE optical block is blanked, because these fields rail
  together and trusting a Tx from the same dead register just moves the lie one column over.
  Identity survives; only readings go. The guard rejects RAILS, never merely-bad optics (PYLON's
  −28.24 crit and −2.87 over-driven ONU both stay). `DdmRailTest`.
- **The login-page GET is a GATE, not a preamble.** Its reply used to be discarded, so the admin
  credential went out no matter what answered — tolerable for one hand-verified OLT, not once
  eligibility is inferred fleet-wide. A non-OK reply aborts with "credentials NOT sent", and a
  404 on `OPM_PATH` is reported as "this firmware has no OPM Diag page" rather than a retryable
  fault. `web_optics_device_budget_s` bounds one OLT's whole scrape; the per-request timeout only
  ever bounded a hop.
- **THE VENDOR RECIPE IS DATA** (`web_optics_profiles`) — the third profile table, done like
  `gpon_profiles`: CLOSED vocabulary, whole profile REJECTED on anything outside it, org_id NULL
  = global, a same-named row SHADOWS the built-in, a DISABLED row switches it off (a tombstone,
  not an absence, or the toggle would lie on exactly the OLTs that shipped with a built-in).
  `weboptics.py` keeps every vendor name as the DBC built-in's default argument, so the one
  field-verified path stays byte-identical while the module is profile-driven. `name` is
  deliberately the SAME token as `gpon_profiles.name` / `org_devices.gpon_vendor` — a second
  web-only notion of "which vendor is this" could disagree with the first about the same OLT.
  Four vocabulary choices, each learned the hard way: (1) columns map BY HEADING, with
  `column_order` only as the positional fallback for a header-less table — and a profile that
  declares headings, meets a page with none, and has no order reads NOTHING rather than guessing
  (`ColumnMappingTest`), because position-mapping is how transmit power gets reported as received
  power, a confident lie; (2) the session is a FLOW (`rotating-key` | `cookie`), not a regex;
  (3) charset is per-vendor (`gb2312` here) and getting it wrong looks like "no optics"; (4) a
  profile may NEVER carry a host — `_clean_path` refuses a URL outright rather than stripping it,
  because path-only is what stops the tunnel being a lateral-movement primitive. Keep the
  live-browse skip unconditional even for vendors allowing concurrent sessions. `_sane_optics`
  takes `expect_voltage` from whether the PROFILE maps a voltage column — the rail guard keys on
  a rail every ONU has by design but not every page prints, and running it blind would blank
  every reading such a vendor produced. An ABSENT column is a fact about the firmware; a MISSING
  value in a column that exists is a fact about the ONU, and only the second is a dead sensor.
- **A SCRAPE'S OUTCOME IS PERSISTED, NOT JUST LOGGED** (`web_optics_status`). It used to live
  only in the log, so a blank dBm column had no explanation a user could reach — and "this vendor
  has no Rx", "nobody typed the OLT's password" and "the scrape has been failing for a day"
  render identically as an empty column while taking OPPOSITE actions. Every outcome INCLUDING
  skips is recorded under a closed vocabulary (ok | partial | skipped | no_profile |
  no_credentials | unreachable | login | error); `last_ok_at` survives a failure so a panel can
  say "was working until <ts>". The SPA composes the sentence from facts rather than the server
  shipping a verdict.
- **Next profile to write is C-Data's own GPON build; `proxy_audit` is how to find its page.**
  Gpon_04/Gpon_08 are the SAME vendor and SAME `/action/` UI (login flow and key rotation carry
  over) but `/action/onuopmdiag.html` 404s — that build's menu is GPON vocabulary and its optical
  page is elsewhere under another name. DO NOT guess it; a guessed page that parses is how a
  fabricated dBm ships. `proxy_audit` records every path a human opens through the tunnel, so an
  operator clicking the optics menu once writes the answer into the DB — the same table that
  proved the EPON path live on NLK-OLT/NDN-OLT. Those boxes also have no SNMP roster, so they're
  ineligible on that ground too until their walk works.
- Tests: `unit/test_weboptics`, `unit/test_weboptics_profiles`, `unit/test_weboptics_sweep`,
  `integration/test_central_weboptics`, `integration/test_central_rxstatus`.

### Topology extras

- **Passive plant lives in org_devices** (`inventory.PASSIVE_TYPES` = splitter/closure/fdb —
  a `closure` is the joint box a cable end lands on; `coupler` lingers in the tuple, unused and
  uncreatable, because removing a type promotes any surviving row to gear that can page),
  NOT a second registry — parent chains, map pins and routes all come free. `ip_address` stays
  `''`; validation rejects an IP/probe on a passive and a PASSIVE PARENT for a monitored device
  (passive-under-passive is fine — plant hangs below gear). Containment is
  `org_device_topology` (the single choke point: engine FSMs, the rebuild fingerprint — adding
  a splitter must NOT re-page — and `/edge/devices`) plus a `device_reliability` skip.
  `org_devices.pon_port` (passives only) binds a splitter to its PON for suspect naming.
- **Peer links are a KIND, not a second graph**: real plant isn't a tree — aggregation switches
  cross-link. Those ride `org_device_links` under `kind='peer'`, canonicalized to
  `(min, max)` so one cable is ONE row whichever end declared it; `list_org_devices` expands it
  symmetrically into `peer_ids`. **Invisible to the engine by construction** — every dependency
  read path already filters `kind='backup'`, so declaring cabling can NEVER rebuild an engine
  or re-page a fleet (`test_central_peerlinks` pins both directions). Deliberately **no cycle
  check** — a ring of cross-links IS a cycle, which is the whole reason they can't be backup
  edges. A pair already joined by any edge is refused, so port bindings are never ambiguous.
  Peers are **descriptive only**: whether traffic reroutes over one depends on STP/routing
  state central can't see, so an operator wanting real failover declares a backup uplink in the
  direction traffic actually reroutes. Port binding uses `uplink_device_id` on BOTH ends, never
  `feeds_device_id` — a cross-link dropping is not the peer's outage cause.
- **Port-level links live ON switch_ports, never a second table**: the parent side is
  `feeds_device_id` (the SAME column ports.py folds a port-down through), the child side its
  mirror `uplink_device_id` — one registry, so the map's bandwidth labels and outage folding
  can't disagree about which port carries a link. Both are operator columns and the walk upsert
  deliberately omits them (a sweep must not clobber declared cabling). The picker is the device
  panel's Uplinks section, also the ONLY backup-link ("ring") management UI — **collapsed by
  default**, since cabling is reference material, not status, and nothing alarm-shaped hides in
  the fold (a down uplink port is already a tree-row chip and a Ports-tab row). Tests:
  `integration/test_central_linkports`.

## Device web-UI proxy (reverse tunnel through the edge)

Drive a switch/OLT's native web UI from the dashboard: **browser → central → edge → device**.
The edge is already central's hands on the LAN for SNMP; this makes it central's hands for
HTTP. Shipped 2026-07-16, field-proven; `weboptics` rides it.

- **Activation is the per-org `orgs.web_proxy` flag** (default off, superadmin-set).
  `WISP_PROXY_ENABLED` defaults ON since v0.15.8 and is only the emergency kill switch — the
  original double-dark shipped every fresh edge with the tunnel off, surfacing as an
  undiagnosable 504. The edge stays DORMANT until a `/report` reply carries a live session, so
  default-on costs zero idle long-polls.
- **Diagnosing a failure: 504 = the edge never picked up; 502 = the edge fetched and failed.**
  Many OLTs refuse port 80 — create the session with `port: 443`.
- **Request headers forward browser→device** (`_forward_headers`) on an allow-list: device
  cookies travel, central's `wisp_central_session` is stripped; Referer/Origin rewritten to the
  device origin (firmware CSRF checks); Authorization forwarded only for `Basic` (a bearer
  would be central's own token). Without this, logins bounce.
- **The autofill bootstrap must never be spliced into JAVASCRIPT** (`proxy._injection_point`).
  A form-login device gets a multi-line `<script>` appended to every proxied HTML document, and
  the old "last `</body>` wins" rule put it inside `document.write("…</body>…")` on DCN's .asp
  UI — a raw newline in a JS string literal ("SyntaxError: string literal contains an unescaped
  line break") killed the page's OWN script before it navigated the content frame. **The failure
  is invisible in `proxy_audit`: every request still 200s**, the tab bar renders, and the content
  page is simply never requested. Two gates now: the insertion point is the last `</body>`
  OUTSIDE any `<script>` (skipped entirely when the body ends inside an unterminated one), and
  the payload must START with `<` — old firmware serves `common.js` with no Content-Type, and
  the doc SNIFF matches any body merely CONTAINING `</body>`, so a JS file was a candidate
  document. Diagnosis method worth reusing: a device whose credentials were never stored has
  autofill disarmed and nothing injected, so `proxy_audit` gives a free A/B against an identical
  firmware. Tests: `unit/test_proxy_autofill:InjectionPointTest`.
- **Escape rescue** (`server.py:_proxy_rescue`): a JS-built root-absolute URL lands on central
  as an unknown route; if the Referer names a LIVE session it 307s back inside the prefix
  (method+body preserved). Path-prefix rewriting will never fully tame JS-built absolute URLs —
  wildcard-host is the real fix if this keeps biting.
- **One tunnel per probe**: `session_create` replaces whatever was open on that node, so the
  operator never hunts a forgotten session.
- **Owner-only** (`_PROXY_ROLES`, also behind the worker whitelist), org-flag gated, every path
  audited to `proxy_audit` — which doubles as the cheapest way to discover a vendor's page
  names.
- **Known limits, honest:** long-polls consume central worker threads while a session is live —
  fine for a handful of techs, not hundreds of always-open tunnels; the dormant-until-requested
  model is what keeps this bounded, so don't make it always-on. Cold start is up to one report
  interval. This is a genuine threat-model change for the edge: the allow-list + audit +
  opt-in-session + capability-flag controls are the price of admission, and shipping any subset
  is not acceptable.

### Why a tunnelled page was 5s/click (2026-07-29) — don't undo these

The tunnel opened a **fresh TCP connection to the device for every asset**, and the weak boxes
could not accept them that fast. A browser on that LAN opens ~2 and keeps them alive for the
whole session, which is exactly why the same OLT felt instant locally and crawled through the
tunnel. Measured over the whole of `proxy_audit`: SRPL-OLT cost **1.00s per asset** (8 assets =
a 7-second page) with a 4.3% fetch-failure rate, against **0.25s and 0.1%** for HLY-OLT-1 —
*same probe, same vendor, same firmware, ICMP 3.8ms/0% vs 3.2ms/0%*. Every shared component
held constant and it was still 4× slower, so the box is the variable. Diagnose the next one
this way: hold the infrastructure constant across two devices on ONE probe before touching
anything.

**The cause is PROVEN, and the proof only existed because failures started being logged.**
Every 502 reads `connect timeout to 172.168.99.245:443` — the TCP connect never completed, so
this was never a slow page, a slow link or a slow CPU. A box that silently DROPS connection
attempts rather than refusing them is an overrun accept queue, and the client's SYN-retransmit
timer is why the surviving requests measured a dead-on **1.00s** median: one retransmit each.
The ones that lost the race outright burned the whole 5s connect budget, 502'd, and the browser
asked again — adding load to a box already at its limit. **The lever is how many connections at
once the device is asked to accept**, which is why the concurrency ladders below exist and why
raising `proxy_connect_timeout_s` would be treating the symptom.

- **Central keeps a per-session static-asset cache** (`proxy.py:AssetCache`, on the
  `ProxySession`). 44% of every request this tunnel had ever carried was a re-fetch of an
  unchanging asset **inside one session** — `jquery-1.7.1.min.js` alone is 553 fetches of one
  OLT — because the firmware ships no cache headers and its frameset re-requests the whole
  script set per click. Four things are load-bearing: it stores the **device's RAW reply**,
  before `rewrite_body`/`inject_autofill`, so a hit replays the identical pipeline and there
  stays exactly ONE rewriting path (`_finish` is the single exit for hit and miss); it lives
  **on the session**, so it dies with the credential and there is no cross-session — let alone
  cross-org — key to get wrong; the **query string is part of the key**, so the firmware's own
  `?rand=` busting keeps working (second-guessing a deliberate bust is how a cache serves a
  stale page nobody can explain); and `_CACHEABLE_EXT` is a **CLOSED extension list** because
  this vendor's DYNAMIC pages are `.html` (`/action/onuauthinfo.html`) — precisely what a
  looser rule would start serving stale. `cache_refusal` then refuses anything carrying state
  (Set-Cookie / `no-store` / a `Vary` beyond Accept-Encoding) — a static extension is a hint
  about the URL, never a promise about the response. Checked BEFORE the in-flight ceiling: a
  cached asset consumes no tunnel, so a page's script set must never 429 against a limit it
  isn't using. **A hit is still audited** — the audit answers "who opened what".
  - **`no-cache` and `private` are deliberately DEFIED, `no-store` is not.** `private` addresses
    SHARED caches and this one is per-session and in-process, so honouring it was a misreading;
    `no-cache` means store-then-revalidate, and this firmware ships neither ETag nor
    Last-Modified, so honouring it literally means the cache can never work on the entire fleet.
    What makes that safe is the vendor's OWN signal: this UI cache-busts the JS it considers
    volatile (`/js/misc.js?rand=62245`, a fresh number per page load) and leaves the stable
    files bare — and the query is part of the key, so everything it marks as changing keeps
    missing. Don't "correct" this back to strict HTTP semantics without re-reading that.
  - **jQuery's OWN `_=` buster is stripped from the key; the vendor's `rand=` is not**
    (`cache_key`). `$.ajax({cache:false})` appends `_=<ts>` to everything it sends — a CLIENT
    LIBRARY statement about the browser's cache, not a vendor statement about the resource —
    whereas `rand=` is written by this firmware's own HTML per script tag. The distinction is
    worth 20% of all tunnel traffic: static `.properties` translation tables
    (`/i18N/error_en_US.properties?_=…` alone is 5,919 fetches fleet-wide of a file that has
    never changed) are otherwise a permanent miss. A param merely STARTING with `_` is somebody
    else's and is kept.
  - **A refusal is LOGGED, once per (session, reason).** A working cache and an empty one look
    identical from outside — the only symptom is that the tunnel stayed slow — so the reason
    has to be reachable without attaching a debugger to production. Deduped, or a device that
    stamps one header on every asset turns a useful log into an ignored one.
- **The edge reuses ONE client per `(scheme, ip, port)`** (`webproxy.py:_ClientPool`). Socket
  hygiene is httpx's own `keepalive_expiry` (`proxy_keepalive_idle_s`), which can't disturb a
  live request; the pool's own reap only bounds the dict and uses a cutoff well past the
  longest fetch, or it would tear a client down under an in-flight request. A device closing a
  pooled connection is NORMAL and costs **one silent retry — GET/HEAD only**: a POST that died
  without a reply may still have been applied, and re-submitting a config write is worse than
  the 502.
- **Per-DEVICE concurrency is an adaptive ladder on BOTH sides** (`proxy_device_max_inflight`
  → 2 → 1): `proxy.py:_DeviceThrottle` on central, `webproxy.py:_DeviceGate` on the edge. Not
  redundant — `proxy_workers` bounds a NODE's tunnel, these bound one BOX, and **central's is
  the one that ships without a fleet rollout**, which is the whole reason it exists there.
  They converge on the same rung and the tighter wins. Same shape as `PysnmpPoller`'s and for
  the same reason — **no vendor hardcode, no operator-kept list of weak boxes**: drop a rung
  only on a CONNECT failure (a 404 or a slow page proves nothing about capacity), re-probe one
  rung faster every 3h so a reboot heals silently. Rungs above the configured ceiling are
  dropped, not clamped.
  - **The ladder FLOORS AT 2, and that is measured, not cautious.** SRPL-OLT, median in-burst
    gap between assets: limit 4 → **1.00s** (connections dropped, 5s each), limit 2 → **0.00s**,
    limit 1 → **1.50s, the worst of the three**. One-at-a-time loses because the tunnel is a
    PIPELINE — while the edge uploads one reply and re-issues its long-poll, a second request
    should be in flight covering those WAN legs, and serialising makes every asset pay for that
    dead air. The failures the ladder exists to stop are real, but curing them by going to 1
    costs more than the failures did. Don't "finish" the ladder by re-adding a rung at 1.
  - Central's gate is taken **inside `ProxyHub.submit`**, so a tab, the web-optics sweeper and
    the session-open preflight are all bounded by it — a device is a device whoever is asking,
    and a gate a new caller can forget to take is not a gate. It is a **Condition, not a
    Semaphore**, because the capacity moves: a semaphore's value is fixed at construction, so
    narrowing would mean swapping the object and hoping every holder released the one it took.
    Keyed on the DEVICE, so a reopened tab inherits what we learned about the box. Failing to
    get a slot returns None, i.e. reads as a timeout — which is what it is from the browser.
  - **Central classifies the EDGE's failure PROSE** (`is_connect_failure` vs
    `_friendly_fetch_error`), because the fleet cannot be updated in the same breath as
    central. That is a cross-module coupling on strings, so `ConnectFailureWordingTest` drives
    the real function with real httpx exceptions and fails if the wording drifts. A TLS-version
    mismatch and a non-HTTP reply are deliberately NOT capacity signals — they fail identically
    at any concurrency, and narrowing would slow a device down to punish it for a wrong port.
  - `DeviceFetchError.connect_failure` carries the same classification on the edge, where the
    exception is still in hand; it subclasses RuntimeError so every existing handler is
    untouched.
- **Every slow asset logs WHERE the time went** (`proxy.py:_log_slow`, over 1.0s).
  `_Pending` stamps the round trip at the only two points central can see it: when the edge's
  long-poll CLAIMED the request and when its reply landed. `queued` (park → claim) is tunnel
  cost — no worker free, or none polling; `edge` (claim → reply) is the device fetch plus the
  reply upload. Without that split a slow page is only "slow somewhere", which is what cost
  this subsystem two restarts of guessing. Keep it: the device-side and tunnel-side cures are
  opposite, and picking the wrong one made things measurably worse once already.
- **A failed fetch is now LOGGED on central** (`api/proxy.py`). The edge writes one human
  sentence per failure mode and it used to reach the browser's 502 body and *nothing else*, so
  a fleet running 4-5% failures had no record of why — this diagnosis could not tell a refused
  connection from a TLS mismatch after the fact. 504s log too. One line; keep it.
- Tests: `unit/test_webproxy` (`AssetCacheTest`, `ClientPoolTest`, `DeviceGateTest`,
  `CentralDeviceThrottleTest`, `ConnectFailureWordingTest`), `integration/test_central_proxy`
  (the cache's refusals, and that a hit is byte-identical to a miss).

## Central management plane

- **`org_devices` is THE device table.** The single-box `devices`/`rollups` tables and
  `POST /ingest` are DELETED — don't reintroduce a second registry. `events` survives:
  central-originated log lines only.
- `central/inventory.py` is pure validation, no storage; `clean_device_payload`'s `parents` map
  is pre-scoped to one org by the caller.
- **Every `org_devices` write re-derives org from the DB row** via `store.device_org(id)` (body
  `org_id` trusted only on create); same for `switch_ports`, feeds, links.
- `orgs.ntfy_topic_owner/worker` (outage routing) are separate from `orgs.ntfy_topic`
  (fleet-watchdog NODE_STALE/OK) — don't merge.
- **New columns on existing tables need `_ensure_columns`** in `CentralStore.__init__` or an
  existing `central.db` keeps the old schema. New tables need nothing.
- `make_server`/`_make_handler` take an injectable `notifier` — tests inject a recording double;
  follow this for anything central sends.
- **Routes live in `central/api/` route tables**, not `server.py` if/elif chains:
  `api/__init__.py` maps exact paths to handlers in `api/{edge,orgs,users,fleet,devices,
  outages,field}.py`. GET handlers are `fn(h, qs)`, dashboard POSTs `fn(h, user, body)`; `h` carries
  `cfg/store/notifier/registry` plus `_reply`/`_reader`/`_scope_org`/`_can_write`. `server.py`
  keeps only transport, auth helpers, static/SSE/download serving, login, and edge-ingest
  special cases. Adding an endpoint = a function + a table row. `api/common.py:DENIED` is the
  "403 already sent" sentinel — don't test scope helpers with `if not org` (superadmin
  legitimately yields org=None).
- **Every JSON API reply is `Cache-Control: no-store`** (`server.py:_reply`). With no header a
  200 GET is heuristically cacheable and `_reply` sends no validator, so a browser can serve a
  stale body without ever asking. Surfaced on ONU search, whose URL carries the needle: a
  `?q=BSNL` answered while search was still serial-only pinned its EMPTY reply to that string,
  so "BSNL" stayed blank while "BSN" worked. Freshness is react-query's job in memory, never
  the HTTP cache's.
- **Deleting an org sweeps DISCOVERED tables, and `org_devices` goes LAST**
  (`store_orgs.delete_org`). Org-scoped tables are found by introspecting for an `org_id`
  column rather than listed: this schema grows a table most months, a hardcoded list silently
  orphans rows in whatever was added last, and org ids are REUSABLE — those rows would surface
  inside a later org of the same name. `org_id IS NULL` is global by construction and the
  equality match spares it. Order matters because `_connect` runs `PRAGMA foreign_keys=ON` and
  every FK points at `org_devices(id)`: sweeping it alphabetically dangles those rows and
  SQLite aborts. Constraints stay IMMEDIATE — a violation surviving the ordering means a
  cross-org reference, which should fail loudly. Superadmin-only AND gated on an ECHOED org id,
  enforced server-side so a SPA refactor can't drop it; `EngineRegistry.forget` evicts the
  in-memory engine. Deliberately NO tombstone: `_ensure_org` on the ingest path IS how a probe
  bootstraps its org, so an edge still pointed at a deleted org re-creates an empty shell — the
  dialog says to uninstall the probe rather than break self-enrollment for everyone. Tests:
  `integration/test_central_orgdelete`.
- **Superadmin Overview = coverage, not alarms** (`/api/admin/overview`): per-org
  SNMP/optics/ports enabled-vs-working (working = a reading fresher than 900s); never-reported
  vs gone-stale are distinct reasons; optics/ports problems suppressed when SNMP is dead
  outright. Pure read-side, never pages.

### Roles: owner and worker (only)

`auth.ROLES`. The read-only `operator`/`tech` roles were removed — an org has people who run it
and people in the field, nothing in between. `store._collapse_roles` migrates existing accounts
to `worker` on open; a row on a role outside `ROLES` fails every check and reads as a broken
login.

- **The operator ntfy TOPIC became the worker topic, value carried across**
  (`ntfy_topic_operator` → `ntfy_topic_worker`) — that column is where nearly every page
  actually lands, so dropping it would have silenced a live fleet until every handset
  re-subscribed. The dead `ntfy_topic_operator`/`_tech` columns linger unread in upgraded DBs;
  don't "clean them up" into a migration.
- **Worker scope is ONE choke point**, `server.py:_WORKER_ROUTES`: a worker reaches
  me/outages/SSE/ack/post-mortem/own-password and 403s on every other `/api/*`, so a NEW route
  is worker-blocked by default — widen the set deliberately.
- **Every worker check gates on IDENTITY before role — server AND SPA.** A superadmin is
  `org_id IS NULL` and its `role` is meaningless, so it must be exempted first, in BOTH
  `_worker_blocked` and `require-auth.tsx`. When the collapse made the org default `worker` the
  superadmin row flipped too: the server 403'd it off every `/api/*` and the SPA served the
  platform admin the stripped field view with no way back. Belt AND braces: `create_user`
  forces `owner` for `org_id IS NULL`, and `_collapse_roles` both SPARES superadmins and
  REPAIRS any already flipped. Pinned by `test_superadmin_is_never_worker_blocked`,
  `test_superadmin_provisioned_bare_is_not_a_worker`,
  `test_role_collapse_spares_and_repairs_superadmins`.
- Ack/post-mortem rights = `api/common.py:can_triage` (every role now, but it stays its own
  predicate because WRITE rights are still owner-only: a worker triages, it doesn't
  reconfigure). The SPA routes worker sessions to `routes/worker-page.tsx` for EVERY path.
  Since workers never reach the shell, every visitor to the dashboard has `canWrite`.

### Billing / payments

- **Paywall** (`central/billing.py`): plans free/pro/vip on `orgs.plan`, paid months in
  `org_billing_months` ('YYYY-MM' UTC; pre-marking future months IS the "no reminder" switch).
  A pro/vip org whose CURRENT month is unpaid 402s on every `/api/*` except `_BILLING_EXEMPT`
  (me, billing, login/logout, paid-ping + free-plan — what the lock screen needs); **edge
  ingest, monitoring and outage paging are NEVER gated** — a lapsed bill must not silence an
  alarm. Device caps (5/500/∞) enforce on CREATE only (a downgrade never breaks existing
  monitoring); passives never count. Reminders are transition-only (`billing_notices`): owner
  topic at ≤3 days runway and on lock; failed sends retry next sweep, 'skipped' (no topic)
  doesn't. Free never locks.
- **Payments are manual — GPay number or QR, NO gateway.** The superadmin sets a number and
  optionally a QR image (`billing_gpay_number`/`billing_qr_image`, a `data:image/…` URI capped
  ~512 KB); both ride the `/api/billing` reply. An org pays out-of-band then taps **"I've
  paid"** → `POST /api/billing/paid` (billing-exempt, ANY signed-in member — everyone sees the
  lock screen) pushing to the dedicated payments channel (`billing_paid_topic`, blank falls
  back to `cfg.central_ntfy_topic`; the ONE choke point is `orgs._admin_payments_topic`). The
  admin then marks the month by hand. NO ledger, NO settlement callback — the admin IS the
  confirmation; the button only sends a heads-up (`notified:false` when unconfigured, never an
  error). `POST /api/billing/plan` accepts ONLY 'free' (the no-payment escape hatch).
- Tests: `unit/test_billing`, `integration/test_central_billing`.

## Dashboard (web/ → central/static/)

Built output is committed (`cd web && npm install && npm run build`); `./run.sh` needs no Node.

- **`/` = marketing landing, SPA at `/app`.** `landing.html`'s SOURCE is
  `web/public/landing.html` (vite copies it) — edit there, never the built copy. Marketing
  overlays are SERVER-INJECTED (`_inject_showcase`, gated `WISP_SHOWCASE`) because the bundle
  rebuilds its whole DOM; `showcase.js` re-mounts via MutationObserver.
- **`HashRouter`, not `BrowserRouter`** — the server 404s non-file paths, no SPA fallback.
- **No frontend test suite** — verify via `tsc --noEmit`, `npm run build`, manual Playwright
  (in `web/node_modules`, run from inside `web/`).
- **Mockup-only fakes — don't "finish" them**: Clients online, manual Resolve, Docker install,
  Notification history.

### Theme & palette

- **Minimal-gray, dark default, WARM-SLATE**: canvas `#0c0e12`, card `#1c1f24`, raised
  `#24272e`; surface steps + borders, never shadows; desaturated accents so status colors stay
  loudest. Spacing is 8px-grid GCP-loose (`h-11` rows, `px-4`–`px-5`) — density comes from
  filling width, not shaving padding. **Type scale is rem-only** (`--text-2xs` 12px,
  `--text-xs` 13px, `--text-sm` 15px, root scales up ≥1600px) — a `text-[12px]` literal opts
  out. A resolved outage pending post-mortem renders NEUTRAL, never green.
- **Colors are OPERATOR-SETTABLE — a palette ask is a dashboard task, not a code edit.**
  Settings → Platform → Appearance (superadmin, server-wide) writes
  `app_settings.theme_overrides`; `central/theme.py` validates, `server.py:_inject_theme`
  renders it into a `<style id="wisp-theme">` at the END of `<head>`. **Check whether the ask
  is just this panel before touching index.css** — that was the whole point. index.css stays
  the DEFAULT and the design record.
  - **Storage is a SPARSE DIFF, never a snapshot** — an untouched seed emits nothing, so a
    stock install is byte-identical to the shipped CSS and future palette work still reaches
    every token an operator hasn't taken over. Writing a full palette on first save would
    freeze each install on that day's colors. `{}` CLEARS the row (the reset); the key absent
    from the POST means "leave colors alone" (so saving the Maps key can't wipe them).
  - The UI edits ~7 SEEDS, not 44 raw tokens: `lib/theme-tokens.ts` holds the OKLab math and
    re-derives each family, so the surface ladder's measured steps survive an operator drag —
    and `readableInk` picks every `*-foreground` by CONTRAST MEASUREMENT, which keeps
    "`--primary-foreground` is dark ink, not white" true for colors nobody has picked yet.
    `ADVANCED_TOKENS` is the raw escape hatch.
  - **`theme.py:_TOKENS` and the SPA's `ALL_TOKENS` are two hand-kept lists** — a token added
    on one side only is silently dropped on save (presents as "the color didn't stick");
    `test_allowlist_matches_spa` reads the TS source to pin them, which is why tone tokens are
    spelled out as literals rather than built from a prefix. The allowlist + value regex are a
    SECURITY boundary (values land in a `<style>` block), re-applied on READ so a hand-edited
    DB row can't reach the page. Injected rather than fetched so there's no flash of the
    default palette and the pre-session login/lock screens are themed.
  - **Mode scoping broke dark mode TWICE — get both halves right.** (1) The live preview must
    be CSS in a `<style>`, never inline styles on `<html>`: inline root styles outrank EVERY
    stylesheet rule including `.dark{}` and carry no mode, so a light preview survived a toggle
    to dark. (2) Selectors must be `:root:not(.dark)` / `:root.dark`, NOT index.css's
    `:root`/`.dark` pair — those have EQUAL specificity (0,1,0), so an injected `:root{}` of
    light beats the bundle's `.dark{}` on source order and applies IN DARK MODE. The symptom is
    nasty because it's partial: customised tokens flip to light while untouched ones stay dark
    → light text on white. The `:not()`/`.dark` pair is (0,2,0) and mutually exclusive, so
    placement stops mattering. index.css keeps the simple pair because it controls its own
    source order; an injected layer can't assume that. Pinned by
    `test_mode_selectors_are_mutually_exclusive_and_outrank_the_bundle` and
    `test_preview_never_writes_inline_root_styles`. **Verify a palette change in a REAL browser
    in BOTH modes** — both regressions passed static review and unit tests.
  - Tests: `unit/test_theme`, `integration/test_central_theme`.
- **Two things about the warm-dark palette are load-bearing.** (1) `--primary-foreground` is
  DARK ink (`#0b1920`), not white — the steel blue sits at L*≈68, so white on a filled primary
  button measures 2.5:1 and fails AA; a later "restore white for contrast" would invert the
  measurement. (2) The border alphas did NOT change — `rgba(255,255,255,.10)` resolves to
  exactly `#31353d` on the card, which is the argument for alpha borders rather than hex, so
  don't re-hex them to the swatch. Also: **`--muted` no longer equals `--background`** (at the
  ~8.3 ΔL* gap a canvas-colored well inside a card reads as a black band, not a recess), and
  the canvas sits within ~0.4 ΔL* of the near-black the halation argument rejects, so **a
  further "darker" ask should raise card with it rather than widen the gap again.** Promoting
  the hover wash (`#26292d`) to `--card` was tried and REVERTED within the hour ("too bright"):
  a hover tone is a transient highlight, not a resting surface. Don't redo it.
- **Two choices from the NOC design file were deliberately NOT taken** and must not be
  re-imported: **the font** (it specifies Geist; Geist was tried and pulled 2026-07-09 because
  operators found it hard to read — UI text stays **Inter Variable**; Geist MONO is fine, the
  mono was never the complaint), and **the type scale** (it runs 13px body / 11px labels; the
  app keeps its raised floor, and there is deliberately no 3xs step). Both were the operator's
  explicit call. Borders are **alpha, not hex**, so one token holds a constant relationship on
  canvas, card, popover AND raster map tiles; `--border-subtle` is the INTERNAL rule inside an
  already-bordered object, and keeping it quieter than `--border` is most of why a panel reads
  as one clean object. `--ghost-foreground` is the 4th text step below `--faint-foreground`.
- **Surface ladder**: `--muted` sits BELOW its surface (wells recess), `--popover` is THE
  raised/focus surface (drill-in, map chrome, menus), `--accent` is the interaction fill on
  raised surfaces — so a fill meaning "hover/selected/skeleton" must use accent, never muted.
  Row hover is `hover:bg-foreground/5` (works on every surface). Hover ≠ selected. Faint text =
  `text-faint-foreground`, not opacity hacks; maint/stale chips render neutral, never amber.
  **The ≥3 ΔL\* floor is measured on ADJACENCY — what shares an edge on screen — not on a
  sorted list of the tokens** (2026-08-05): `--secondary` is used TWICE in the whole SPA, both
  as a button fill, and `--sidebar` never touches `--muted`, so ranking them by lightness
  invents steps that do not exist and hides the ones that do. The six real pairs are card-on-
  background, muted-well-in-card, popover-on-card, accent-on-popover, sidebar-vs-background and
  sidebar-vs-card.

### THE TWO COLOUR AXES (2026-08-05) — the cure for "too bland"

The operator's report was *"too bland, things blend into the background"*, and it was a
measurement, not a mood. **6 of 98 elements on a healthy Home carried any chroma at all** — and
two of those six were a 30-day uptime rollup mis-toned as a warning. The cause is structural:
this product had ONE colour channel and it is correctly reserved for failure, so *being fine had
no visual form* and rendered as absence.

**Axis A — STATUS: "is this broken?"** Red/amber/green/info. Reserved, supreme, UNCHANGED by
this work.

**Axis B — IDENTITY: "what KIND of fact is this?"** Five MEASUREMENT PLANES (`lib/planes.ts`,
`--plane-*` in index.css): **optical 200° · traffic 247° · vitals 273° · plant 299° ·
fleet 325°**. Constant, always present, never alarming.

- **The planes are not invented for the palette — the SCHEMA already models them.** One
  `org_devices` row carries FOUR separate freshness stamps (`optics_`/`ports_`/`health_`/
  `state_updated_at`), the SNMP layer runs THREE separate walk clocks with three separate
  timeout caps, and the device panel's tabs are literally Optical | Health | Ports. All of it
  rendered in the same grey until now. Cloudscape's design system independently ships **5**
  primary categorical hues; the schema and the reference agree on the number.
- **REACHABILITY GETS NO HUE, and that is what makes five fit.** A plane earns a hue only if it
  produces facts that survive the thing being healthy. Optical has an Rx, plant has a split
  ratio, fleet has a version. Reachability has latency, loss and up/down — every one a STATE.
  There is no constant reachability fact: it IS Axis A. Six hues do not fit the 200–330° band at
  a readable separation; five do, at 26°.
- **Hue is fenced MECHANICALLY**: inside 200–330°, NEVER inside 20–140° (red/amber/green), and
  every plane ≥18° clear of `--primary` (229°) so identity can never read as interaction. Solved
  by allowing a GAP across the reserved accent rather than spacing evenly — that buys 26° where
  even spacing gave 20.
- **Chroma is capped at 55% of the QUIETEST status tone on the same screen** (not the loudest —
  "must not exceed the status tones" means all of them), contrast in **[3.0, 80% of that tone]**:
  a floor from Cloudscape's data-vis minimum, a ceiling from the alarm channel. Measured: warning
  is the quiet one at C=0.1208 dark.
- **AN IDENTITY HUE IS A MARK COLOUR AND NEVER A TEXT COLOUR**, and that fell out of the
  arithmetic rather than taste: in LIGHT mode the ceiling lands at **3.72:1**, below the 4.5:1 AA
  floor for body text, because light's quietest status tone is only 4.65:1. A hue that cannot
  legally be read as text can still be a mark. So a **status chip is COLOURED TEXT** and an
  **identity chip is NEUTRAL TEXT beside a coloured DOT** — identical structural weight,
  impossible to confuse, and the accessibility constraint and the never-confuse-them requirement
  turn out to be the same fact.
- **NOT operator-settable**, same reason `--map-line-*` isn't: they are an ENCODING solved
  against a budget derived from the status tones, and a hand-typed value could breach the ceiling
  that keeps identity from reading as an alarm. `--chart-1..5` MIRROR them and ARE settable
  (`ADVANCED_TOKENS`), because a chart series is decoration over data rather than a claim about
  it.
- **Identity does NOT go on the left rail.** That rail already carries the operator's tag/probe
  colour, and two meanings on one rail is the failure Datadog documents by name: *"if multiple
  groupers are used with the semantic palette, the colors no longer correspond to a specific
  meaning — red no longer necessarily indicates bad."* It goes on the chip's dot.

**`--chart-1..5` WERE THE STATUS TONES, derived from the status SEEDS.** `toneFamily()` built
`--chart-1` inside the primary family, `-2` success, `-3` warning, `-4` destructive — so every
chart in this product would have drawn its series in ALARM colours, and recolouring "Down" in
Settings → Appearance silently recoloured chart series 4. The live install's stored overrides are
the proof: they carry `--chart-1` and `--chart-4` and not `-2/-3/-5`, exactly the two seeds that
were moved. `toneFamily` no longer takes a `chart` key at all, so it cannot be reattached by
accident.
### The separation pass (2026-08-05) — what the ladder, the borders and selection now are

Measured before and after, on RENDERED PIXELS, in both modes. Every adjacency now clears the
≥3 ΔL\* floor; **four did not**.

| adjacency | dark before → after | light before → after |
|---|---|---|
| card on background | 4.70 → **9.19** | 3.04 → **4.94** |
| muted well inside card | **2.52** → 3.96 | 5.96 → 3.18 |
| popover on card | 3.95 → 4.77 | **0.00** → **3.19** |
| sidebar vs card | **0.50** → 5.47 | **0.94** → **8.13** |

- **`--popover` WAS `--card` IN LIGHT MODE — both `#ffffff`, 0.00 ΔL\*.** The documented
  "raised/focus surface" had no surface signal at all there, so every drill-in, menu, map card
  and dropdown floated over something identical to itself and rode entirely on its outline.
  Nobody had measured light mode. Four levels needing 3 ΔL\* each do not fit under a hard white
  ceiling, so the whole light ladder steps DOWN to make room and the light **sidebar is now
  DARKER than the canvas** — which also makes the two themes structurally the same (the sidebar
  is the recessed rail in both, instead of recessed in dark and raised in light).
- **THE CANVAS DOES NOT MOVE** (3.94 → 3.87 ΔL\*). The halation note says a third darkening must
  raise card with it; card, popover and accent rose instead.
- **The border alphas DID change** (`--border` .10 → .17, taking a panel edge from **1.343 →
  1.731:1**) against a note saying they must not. That note protects the ALPHA FORM — one token
  holding a constant relationship on canvas, card, popover and raster tiles — which is intact.
  Its justification for the VALUE ("resolves to exactly the palette's `#31353d` on the card")
  stops being true the moment the card moves. 1.343 was also exactly the ceiling CLAUDE.md sets
  for basemap ROAD GEOMETRY: roads were dulled until they were as quiet as a panel edge, and
  nobody checked whether the panel edge was loud enough to be one.
- **SELECTION IS A STATE OF THE ROW, NOT ANOTHER LAYER.** `.wisp-drillin` was popover-bg + a
  strong outline, i.e. exactly ONE ELEVATION STEP — the same distance that means "this is a menu
  floating above you". Linear ships the rule and states the reason: a selected row's delta
  must EXCEED the elevation step and it must have NO EDGE LIGHTING, because an outline is what
  makes a highlight read as a separate z-plane. Now: no outline (transparent border keeps the
  geometry), no inset top edge (it is excluded from the `.wisp-panel` highlight rule for this
  reason), and a fill clearing the step — 6.88 vs 4.77 dark, 5.44 vs 3.19 light.
  It is a **CHROMATIC lift, not a brightness one**, which is what lets it coexist with hover:
  `--accent` is the achromatic hover wash, `--selected` sits at similar LIGHTNESS carrying the
  accent HUE at 2.1× (dark) / 5.0× (light) the chroma. A first attempt separated them by
  brightness and put the selected row 15 ΔL\* above the card — brighter than `--accent`, i.e.
  straight back to looking like a new surface. Derived from `--primary` via `oklch(from …)` with
  the documented `@supports` fallback, so recolouring the accent carries through.
- **THE ACCENT HAS EXACTLY THREE JOBS**: focus ring, selected row, active nav — plus the
  pre-existing `info` tone and the map's own "you are here"/impact rings. The active-nav rail
  moved from `--foreground` to `--primary`, REVERSING an earlier call ("accent stays reserved…
  so status colors keep being the loudest thing on screen") on a measurement that says the
  opposite: `--foreground` on the sidebar is 14.16:1 dark / 13.38:1 light and `--primary` is
  7.40 / 3.95, so the rail this replaced was TWICE as loud as the accent.
- **The lit top edge is `inset 0 1px 0 rgb(255 255 255 / 0.06)`, DARK ONLY.** 0.04 was specified
  and measures 1.123:1 on the raised card — faint enough to do nothing; 0.06 lands at 1.190:1,
  clearly under the panel's own border at 1.731. In LIGHT a white inset measures **exactly
  1.0000:1** — no effect, because the card is already the lightest surface in the room. No drop
  shadows: on a near-black canvas a cast shadow has nothing to darken.

### The instrument grammar (2026-08-05)

- **`<Reading>` (`components/reading.tsx`) is ONE FORM for every number that can be uncertain.**
  The honesty rules existed in prose and every screen re-implemented them, which is how "nothing
  is measured" kept escaping as a green zero. Five states, each with a **non-colour channel** so
  the grammar survives greyscale and a screenshot pasted into WhatsApp: `current` · `stale`
  (dotted underline) · `frozen` (desaturated + pause glyph + reason) · `absent` (the dead zone)
  · `suppressed` (struck bell — a suppressed ALERT must never look like a suppressed FACT).
  - **THE DEAD ZONE comes from the domain, not from taste.** An OTDR has had a first-class
    visual concept for "the instrument cannot measure here" for forty years, and it is a marked
    REGION on the scale rather than an absence of trace. So `absent` is a dash on a hairline
    track occupying the box a real reading would, holding its column. A blank cell says "nobody
    looked"; a dead zone says "we looked and this instrument cannot answer". Most of the
    C-Data/DBC fleet is in exactly this state.
  - **`at` DERIVES the state and does not PRINT.** A list shares one walk, so dating each figure
    repeats the same four characters down the column — the first wiring put "7h ago" beside all
    fourteen PON readings in a panel whose header already said it once. `showAge` opts in, for a
    solitary reading with nothing else to date it.
  - **FROZEN is the only state that surrenders its status tone**, mirroring `.wisp-frozen`, which
    greys on DOWN and not on merely stale. An unreachable box's stored "crit" is not a claim
    anyone can stand behind; a stale one's still is.
- **`RxScale` is a POWER METER, not a progress bar** — fixed marks you are on one side of, not a
  fill showing how far along you are. **The domain is the DECISION BOUNDARY, `[crit-3, warn+3]`,
  not the optical range**: readings span ~-7 to -30 dBm while warn and crit sit 3 dB apart, so a
  linear track over the real span would spend two thirds of its width where nothing is decided.
  Anything comfortably healthy **PEGS**, which is what a meter in pass/fail mode does. `ok` gets
  NO band. Thresholds are threaded per-OLT from the optics reply, never a global default, because
  `optics.py:_severity` grades per box. **NO UPPER BOUND, deliberately**: an over-driven ONU is a
  real fault (PYLON has one at -2.87) but this product models no upper threshold, and a scale
  that disagrees with what pages is worse than one that is merely incomplete.
- **`OnuBar` is the PON heat strip's grammar at aggregate resolution.** The real strip draws one
  cell per ONU from `pon.onus[]`; a tree row or an Issues group header carries only counts, so
  this is proportional segments in the same tones and the same order. A PROPORTION is the point:
  "26 ONUs crit" reads identically on a 30-ONU box and a 600-ONU box, and those are opposite
  situations. Offline takes the MUTED step, never destructive — hundreds go dark every evening.

- **Status tone is ONE formula** (`components/status-badge.tsx`): tone color for text, same hue
  at 13% fill, 30% edge — shared by `Chip`, `StateBadge`/`TonePill` and `StatusDot`. `info` is
  a real tone: an ACKED outage means a human owns a still-live incident, which muted hid. On
  Home a stat tile is tinted ONLY when something is wrong, so scanning is a search for color
  rather than a read of eight numbers.
- **Operator colours are ONE closed vocabulary** (`lib/palette.ts` ↔ `inventory.PALETTE`):
  violet/magenta/teal/lime/indigo/chalk, shared by the map's per-link palette, TAGS and PROBES.
  One set on purpose (a second would drift the first time either grew) and closed on purpose: a
  free hex lets an operator paint a healthy thing the same red as a broken one, faking an alarm
  on the screens that exist to show them (`test_central_colors` pins the refusal). Nothing here
  is a status tone. Storage is `org_colors (org_id, kind, key, color)`, sparse — no row IS
  uncoloured, clearing is a DELETE — keyed on TEXT, not a foreign key, because a tag exists
  only inside `org_devices.tags` and a probe may live in `node_tokens`, `nodes`, or both. A
  device takes the colour of the FIRST of its tags that has one, falling back to its probe's —
  most-specific-wins, with the operator's own tag order as tie-break because any other rule
  reshuffles colours as tags are added elsewhere. It renders as a left RAIL outside the tree
  indent guides; the status dot, alarm chips and map stroke all still win, so a colour can never
  hide an outage. **Tag names are NOT chipped on the device row**: every other chip in
  `DeviceChips` is a claim about state, and organisational labels beside them are noise. **The
  palette members are STROKE colours** — on light mode's near-white card they measure ~2:1 as
  chip text, so `.wisp-tag` mixes them toward black there; that's why a coloured chip is a class
  taking `--tag` rather than an inline style. Adding another colour-coded thing = a new `kind`,
  never a new palette.
- **Five `wisp-*` primitives carry the repeated patterns** (index.css — don't re-copy their
  Tailwind strings per page; that drift is what makes screens stop looking like one product):
  `.wisp-panel`(+`-head`), `.wisp-row` (the rule under the header is dropped via
  `.wisp-panel-head + .wisp-row`, an adjacent-sibling selector ON PURPOSE because
  `:first-of-type` matches per element type and silently fails when both are `<div>`s — they
  are), `.wisp-eyebrow`, `.wisp-thead` (a muted WELL, so it frames rather than competing with
  row one), `.wisp-frozen`.
- **Page measure is TWO widths, not one per route**: `.wisp-page` (105rem) for anything
  scannable and `.wisp-page--narrow` (66rem) for form-shaped pages where line length hurts. The
  app had six measures (48–105rem) and visibly changed shape as you navigated. Don't add a third.

### The issue plane (`/issues`, `central/issues.py`, `central/pdf.py`)

A Home KPI tile drills into the Network TREE filtered to the devices behind its number — which
answers "which boxes", not "what is wrong". `/issues` is the other half: ONE ROW PER PROBLEM
(port, ONU, PON, probe), because a switch with four dark ports is one tree row and four jobs.
Both actions stay on the tile: the body links to the tree, a corner button (`ListTree`) links to
`/issues?kind=<kind>`. Read-side only — it writes nothing and pages nobody.

- **The tile's count and the list's length MUST be the same number** — a drill-down that
  disagrees with the tile it was opened from is worse than none. So `issues.collect` re-derives
  NOTHING: it composes the same store reads and the same gates the tiles use (`olt_liveness`,
  `current_roster`'s freshest-walk-per-OLT view, `low/high_bandwidth_alarms`, `ponfault`,
  `onuroster`), and "monitored" means what the tile means — assigned to a REGISTERED, unrevoked
  probe, maintenance excluded. `store.down_ports` counts the flap-suppressed `alarm` flag, the
  same column `list_org_devices.ports_down` counts, NOT a raw `oper_status`.
- **`KINDS` is a CLOSED vocabulary, one kind per tile** (`issues.py` ↔ `types.ts:IssueKind`).
  `pon_fiber` and `pon_power` are SEPARATE kinds although `ponfault` yields one verdict type:
  the tile counts suspected cuts only (a power drop is recorded and deliberately never paged),
  and one merged kind would make the chip's count exceed the tile's. Adding a tile = adding a
  kind on both sides.
- **An unknown `?kind=` shows the WHOLE list, never an empty one** (`_kinds_arg` intersects with
  `KINDS` and a request left with none reads as "no filter"): a link written against an older
  vocabulary must not render as an all-clear. `counts` always ride the UNFILTERED list so a chip
  can say how many rows it would show before it is clicked.
- **A port-down on an unreachable switch is KEPT but demoted to `info` and annotated** "reading
  frozen" — the honesty rule and the count-agreement rule pull opposite ways here, and dropping
  the row would leave the tile's number unexplained. Bandwidth alarms are the opposite case (a
  rate reading, already excluded upstream by `_bandwidth_alarms`) — don't "fix" the asymmetry.
- **The PDF is SERVER-rendered, pure stdlib** (`central/pdf.py`, ~250 lines: three base-14 fonts,
  no embedding, paginated, no wrapping) rather than a browser print stylesheet — what gets filed
  after a shift should be the rows the server holds, and "print to PDF" is not a thing on a
  phone. It is filtered by the chips you can see. Four traps it already survives: an unescaped
  `)` from a real `if_alias` ends the string operand and corrupts the rest of the page; the
  xref must record the bytes actually written (offsets are captured as each object is appended,
  never computed); the fold is **cp1252, NOT latin-1** — the fonts declare `/WinAnsiEncoding`
  and latin-1 lacks the 0x80–0x9F band, so an em dash printed a report titled "Open issues ?
  ispA"; and a mono cell measured with the Helvetica table overruns its column. Unencodable
  glyphs degrade to `?`; nothing may raise inside a download.
  `_send_binary` in `server.py` is the only non-JSON reply that keeps the security headers.
- **Column widths are MEASURED from content, never a share of the page** (`_solve_widths`).
  `Column.weight` only breaks the tie when the content doesn't fit. Proportional sizing got the
  first cut exactly backwards: Detail sat on ~180pt of white carrying `Rx -28.86 dBm` while Item
  truncated the 17-char MAC the report exists to communicate. Fits → surplus shared in proportion
  to need; doesn't fit → water-fill, so small columns are satisfied outright and only genuinely
  hungry ones absorb the shortfall (one long free-text column can never starve five identifier
  columns). Same failure had to be fixed on screen: the Issues grid gives Item and Detail
  FRACTIONAL tracks, not one capped and one greedy `1fr`. Corollary in `issues.py`: the PON-fault
  detail puts the cut bracket and named passive FIRST — it's the longest string the list
  produces, so what gets cut off in print should be the ONU count, not what a crew drives to.
- **Excel export is a REAL .xlsx, hand-written with `zipfile`** (`central/xlsx.py`) — not a CSV
  wearing the name: someone asking for Excel wants to sort and filter, and a CSV hands them a
  re-import dialog plus text-typed dates. Bold frozen header, autofilter, content-measured column
  widths, and `since` as a **real date cell** so sorting orders by time (text stamps put "26 Jul"
  before "3 Aug"). Severity IS a column here though not in the PDF — it's the first thing anyone
  autofilters on, and on paper the word couldn't be coloured anyway. Traps: `autoFilter` must come
  AFTER `sheetData` (schema element order) or Excel reports the file as needing repair; a `count`
  attribute in styles.xml that disagrees with its children is the same silent repair; the two
  default fills (none, gray125) are mandatory; the epoch is **1899-12-30**, which is what cancels
  the format's 1900-leap-year bug; XML-forbidden control characters really do arrive in an
  `if_alias` and one corrupts the workbook. Inline strings, no sharedStrings — one less index to
  keep consistent. Output is deterministic (fixed zip timestamps).
- **Timestamps go through ONE zone conversion**, `notifiers._wa_local` — `_wa_time` renders it as
  text for a WhatsApp page and the PDF, the spreadsheet renders it as a date cell. Two renderers,
  one notion of "local", or half an export ends up 5h30m out from the other half. That only works
  because EVERY stamp lives in the `since` field and none is interpolated into `detail`.
- **Filter chips are a SINGLE choice** (`setKind`, not a toggle-into-a-set): clicking a kind shows
  only that kind, matching the Logs page and the one-kind links the tiles emit. A multi-kind URL
  still renders. Build the next `URLSearchParams` fresh — mutating the instance
  `useSearchParams` returns edits react-router's own memoized object, so the value a render reads
  moves without the reference the memo is keyed on changing (presents as a chip that highlights
  but never filters).
- **Workers CAN read `/api/issues{,/pdf,/xlsx}`** (2026-07-26). Deny-by-default kept them out at
  first, but a worker now gets the FULL shell and the sidebar lists Issues as a mobile
  destination — "the one screen worth carrying to a site visit" — so the entry rendered and
  403'd. Read-side only (`collect` writes nothing, pages nobody) and still org-pinned by
  `_scope_org`; the exports are the same rows a worker can already see. Writes stay owner-only.
  Tests: `unit/test_pdf`, `unit/test_xlsx`, `integration/test_central_issues`.

### Honesty rules (what the UI may and may not claim)

- **A DOWN device's SNMP readings are FROZEN, and must look it.** Ports/vitals/optics rows
  PERSIST — nothing deletes them when a box drops — so a down OLT went on rendering green "up"
  ports, live-looking bit rates and a healthy CPU meter. `format.ts:isDownState` is the trigger,
  NOT the 900s `isFresh` rule: unreachable is proof the data is stale up to 15 minutes before
  staleness would notice. Three layers, answering different questions:
  1. **the panel GRAYS** — `.wisp-frozen` on the readings container in `device-detail.tsx` and
     `optical-panel.tsx`, applied to the SUBTREE rather than per-tone conditionals so a reading
     added later can't opt out by forgetting to check. Always pair it with a live reason OUTSIDE
     the frozen block; gray with no explanation reads as a broken panel.
  2. **the tree row SUPPRESSES** — `DeviceChips` gates every SNMP chip on
     `liveSnmp = !isDown && !isStale(...)`. A chip is a claim about NOW: "port down" on an
     unreachable switch is the outage reported twice.
  3. **the summary EXCLUDES** — `store_snmp._bandwidth_alarms` drops DOWN/UNREACHABLE so the
     top-bar chip and Home tile stop pointing at a dead switch. Display-only.
  **Alarm STATE and PAGING are untouched at every layer** — same discipline as the governor (a
  suppressed chip must never mean a suppressed page). The ICMP verdict itself stays at FULL
  strength — that's live truth, not a stale reading. Pinned by
  `test_bandwidth_summary_hides_alarms_on_an_unreachable_device`.
- **"Nothing is wrong" and "nothing is measured" must never render alike.** A C-Data/DBC OLT
  walks a COMPLETE roster with every `rx_dbm` NULL, so the optics badge goes green and
  crit/warn sit at 0 on a box measuring no light at all — byte-identical to a healthy fleet.
  Three places separate them, all off ONE number (`list_org_devices.onus_rx` per OLT,
  `pon_summary.onus_rx`/`olts_rx` org-wide): Home tiles render an em dash + "no OLT reports
  dBm" rather than a green 0, and print `N of M ONUs measured` when coverage is partial;
  `DeviceCapabilityIcons` carries a `Gauge` icon green ONLY when `onus_rx > 0` AND the walk is
  fresh — deliberately NOT tinted by severity, because a missing measurement is a coverage gap,
  not an alarm; and the Optical tab explains the gap. `onus_rx` counts the RAW `onu_optics`
  table, zombies included, because it's a CAPABILITY signal paired with `optics_updated_at` for
  freshness — and because the scrape folds Rx in on its own clock, so a count stamped by the
  SNMP sweep would read zero for up to 15 min after a scrape landed.
- **"This OLT doesn't report Rx" was a GUESS stated as a hardware fact and is GONE.** The same
  blank column is produced by a vendor with no recipe, an OLT nobody stored a password for, and
  a scrape failing for a day — and on the fleet that started this the honest answer was "we
  never asked". `components/rx-diagnosis.tsx` composes the real reason from
  `/api/inventory/rx-status` (vendor + how it resolved, recipe coverage, credentials, proxy
  grant, last scrape outcome) — the same split `SnmpDiagnosis` runs for a blank Ports panel:
  the server ships FACTS, the SPA writes the sentence. It must never trigger a scrape — the
  sweeper's slow clock is the feature (`refetchInterval` 120s for the same reason).
- **A dBm ON SCREEN CARRIES NO DATE.** The Optical panel's freshness stamp dated the SNMP
  roster only, so on a scrape-fed vendor the walk could be seconds old while the figures beside
  it were from yesterday — and "last week's numbers" looked exactly like "measured four minutes
  ago" while a tech decided whether a splice had helped. `RxFreshness` gives the web read its
  own stamp with three states never collapsed: read / failing-since (`last_ok_at` survives, so
  "was working until <ts>" stays sayable) / never. `rx-status` carries `can_refresh` +
  `refreshing` so the button is drawn off the server's own eligibility answer; re-deriving it in
  the SPA would drift into a button promising a reading nothing will take.
- **Optical drill-down degrades, never dead-ends**: a PON whose ONUs all have NULL Rx renders a
  roster ordered by ONU id (state, ranging distance — 0 m renders "—", meaning unranged,
  time-dark from `last_online_at`), a stable slot order the tech reads down rather than one
  shuffled by which ONUs are up; the worst-PON/threshold header hides when the whole OLT has no
  readings. Keep the empty-card branch unreachable.
- **`OnuRow` is ONE COLUMN PER FACT — no cell stands in for another.** The Rx cell used to FALL
  BACK to ranging distance or time-dark so the 380px panel kept one useful number, but the
  dedicated distance column renders at `@2xl`, so a no-Rx online ONU printed the same "2.78 km"
  twice in one row. Both are now their own columns at EVERY width and the Rx column is dropped
  entirely when the PON is `rosterOnly`. The Rx-drift-vs-baseline column is GONE for good — it
  reads `rx_dbm - rx_ref_dbm`, so on a no-Rx vendor it could only render "—", contradicting the
  header note two lines above. `rx_ref_dbm` still rides the API and the optics baseline.
- **The Network tree's shape is a VIEW, not the topology** (`org_devices.tree_detached`): a
  device buried under a huge subtree can be lifted to the top level by the row menu.
  Presentation ONLY — like `tags` it's read solely by `list_org_devices`, never by
  `org_device_topology`, so the parent link, suppression, the map, `/edge/devices` and paging
  are untouched and toggling it can NEVER re-page a fleet (`test_central_treedetach`). Two
  consequences: a lifted row RENDERS its parent's name beside a corner-up icon (the tree
  stopping short of saying where a box hangs is the one thing this could get dishonestly
  wrong), and `treeOrder`'s chevron gates on children it actually emitted, not the server's
  `child_count`, or a parent whose only children were lifted offers an expander over nothing.
  `update_org_device` clears the flag when the parent goes NULL.

### Layout & navigation

- **Settings is SECTIONED, and a section that would render empty is not offered**
  (`routes/settings-page.tsx`): five addressable sections (`/settings/:section`) behind a
  `SECTIONS` table whose `visible(ctx)` predicate decides whether it appears at all. That
  predicate must model the SAME conditions its cards gate on, including data-dependent ones —
  `WebProxyCard` returns null unless `orgs.web_proxy` is granted, so the page runs the
  `["orgs", org]` query (deduped with the card's) and feeds `hasWebProxy` in. A role-only
  predicate showed a read-only operator a Monitoring tab that rendered blank. Adding a card =
  putting it in a section, not appending to a list.
- **Account menu, not a Settings nav item** (`layout/account-menu.tsx`): primary nav lists
  PLACES IN THE NETWORK, so account-scoped config lives in a dropdown on the sidebar's foot
  identity row instead of spending a permanent nav slot. `NavItem.account` marks such entries;
  the sidebar filters them out and the MOBILE "More" sheet keeps them, because below `md` there
  is no sidebar and so no account menu. The menu advertises `⌘,`, bound in `app-shell.tsx` and
  ignored while a field has focus; if that binding goes, the hint goes with it.
- **Viewport breakpoints are wrong inside the device panel** — it's a fixed 380px on a wide
  screen, so `sm:`/`md:` guards all pass and columns overflow (the ONU heat-strip once
  collapsed to one cell wide). Width-conditional columns there use CONTAINER queries.
- **Epoch-hour trap**: `HourStrip` cells floor on EPOCH hours to match `bucket_of`, never local
  hours (half-hour zones like IST shift every cell). A query error in the device panel renders
  as an error, never the empty state.
- **Sort by `occurred_at ?? received_at`, NOT insert id** (Logs day-grouping, Home activity) —
  acks/post-mortems insert long after the outage. Log group keys include the first row's event
  id (day labels repeat).
- Home is a NOC overview, never empty when healthy; outage triage folds into it (status derived
  from `acknowledged_at`/`resolved_at`/`root_cause`, never stored; recovery is FSM-automatic —
  no manual resolve, ever).
- Auth rides the session cookie; 401 dispatches a `wisp:unauthorized` window event; org scoping
  mirrors `_scope_org`. **Live updates**: one SSE `EventSource` per org scope (`/api/events`)
  invalidating react-query keys off `store.data_version` (which includes `MAX(nodes.last_seen)`
  so a bare heartbeat un-stales a probe without a refresh). `list_org_devices()` LEFT JOINs
  `device_states` (+ `switch_ports` aggregates) so rows color without per-device round trips.

### Map (`/map`)

Leaflet + raster tiles fetched by the BROWSER (central needs no egress). Helpers live in
`web/src/map/` — `pins.ts`, `clusters.ts`, `cut.ts`, `geometry.ts`, `basemaps.tsx`, `view.tsx`,
`search.tsx` — with page composition in `routes/map-page.tsx`. New map logic goes in the
matching module, not back into map-page.tsx.

**Leaflet is the RENDERER; Google is only the TILE SOURCE — that is the design, not an
accident.** The Map Tiles API exists precisely to serve Google imagery into a third-party
renderer, and every overlay this product has (device pins, cable routes, the cut overlay,
site clusters, link-hover distance, drop lines, reference ONUs) is built on Leaflet
primitives. Swapping to the Google Maps JS SDK would mean rewriting all of it, re-billing
per map load, and giving up the keyless CARTO fallback that keeps the map from ever being
blank. Google's ATTRIBUTION is required and stays; Leaflet's own "Leaflet" prefix is a
courtesy, not a licence condition (BSD-2-Clause wants the notice in the distributed source,
which the bundle keeps), so `AttributionPrefix` drops it — naming the renderer beside "Map
data ©Google" only reads as confusion about who supplies what.

- **Basemaps are Google / Google Satellite ONLY** (the CARTO/Esri/Dim entries were removed at
  the operator's request the same day they shipped) via the Map Tiles API — the sanctioned
  third-party-renderer API, NOT the SDK-only Maps tiles. **The key is SERVER-WIDE,
  superadmin-pasted once** (`app_settings`); org owners never see a key field, and the
  `/api/orgs` reply injects it into every org row (referrer-restricted, ships to browsers by
  design, central still makes NO Google calls; no key = no Layers button). CARTO Voyager is the
  KEYLESS FALLBACK, never a menu option: it renders for no-key orgs, under a still-creating
  session, and after a Google failure — the map is never blank.
- `lib/google-tiles.ts`: session token (~2wk) cached in localStorage per mapType; **dpr>1
  sessions request `scaleFactor2x`+`highDpi`** (512px tiles at 256 CSS px — plain 256 rasters
  are why Google "looked blurry" on scaled displays; the cache key carries the scale).
- **"Google labels" (Layers menu) strips Google's OWN writing and POI pins** — a dense town
  ships more Google markers than we draw, each competing with a device pin. ONE styler
  (`elementType: "labels"`, no featureType) covers every feature's text AND its icon; a POI
  marker is a label, not geometry. **GEOMETRY IS LEFT ALONE** — switching `poi` off wholesale
  would take park fills and building footprints with it, and those are what a crew navigates by
  once the names are gone; a blank map is not the ask. **ROADMAP ONLY** and the menu entry
  hides on satellite: an imagery session carries no labels in the first place (they would be an
  explicit `layerTypes: ["layerRoadmap"]` overlay we never request), and a switch that does
  nothing where it is shown is worse than one that isn't there. Each combination is its OWN
  SESSION — the cache key carries a `:s<variant>-<hash>` (n = night, p = plain), pruning stays
  scoped to one variant so a theme or label flip doesn't evict the other's token and pay for a
  fresh `createSession`, and the layer's React key carries it too so a flip REMOUNTS.
- **Dark basemaps follow the app theme**: `createSession` takes a `styles` array and we send
  Google's night array with GEOMETRY intact and **LABEL TEXT dimmed**. Stock shipped first and
  came back "too contrasty" within the hour — it put road labels **louder than every status
  tone**, so the basemap's road names outshouted a device being down. Ours ranks under all
  three tones and is neutral grey rather than stock's tan/gold (which sat right on
  `--warning`). Re-measure before touching those hexes — ranking BELOW the alarms
  is the requirement; matching Google's sample is not. Three constraints: **(1) `styles` is
  roadmap-ONLY** (the API ignores styling on satellite, so there is no dark satellite and the
  Layers menu must not imply one); **(2) an oversized style array is dropped SILENTLY** — no
  error, tiles just come back light, and Google publishes no limit (ours is ~1.5 KB; check a
  real tile after editing); **(3) there is no `mapId`/cloud styling for 2D tiles**, inline JSON
  only. **The session cache key carries a HASH of the style array**, not just `:night` — a token
  bakes in the style it was minted with and lives ~2 weeks, so a palette edit would otherwise
  keep serving OLD tiles to every browser that already had one, invisibly to whoever made the
  edit. Derived, not hand-bumped, because nobody remembers to bump it. Theme is read reactively
  from the `.dark` class via `hooks/use-dark-mode.ts` — there is no theme provider and THREE
  components each keep their own copy, so the root class is the only signal they all agree on.
  The keyless CARTO fallback switches with it (style in the TileLayer key — a TileLayer won't
  re-fetch on a bare url change).
- ToS needs the per-viewport copyright in the attribution control + a Google wordmark overlay.
  Failure ladder: tile-error BURST (3 in 5s, once per token — a stray rural-z20 404 must not
  nuke the basemap) → recreate session once → toast + fallback tiles, WITHOUT overwriting the
  user's saved pick.

### The map must rank what it draws (2026-08-01) — the "can't tell what matters" pass

Operators reported the map as hard to read. It wasn't any one element; it was that REFERENCE
material was drawn as loud as STATE, so nothing could be dismissed at a glance. Every fix below
is the same rule — *status tones are the loudest thing on this map* — applied to something that
had quietly opted out of it.

- **THE GROUND TONE was the biggest one, and it is the operator's own diagnosis.** Dark ran on
  Google's night navy `#242f3e`, which measures LIGHTER than `--popover` (`#24272e`) — the
  basemap, the thing everything else sits on, was brighter than the highest surface in the
  product, and blue inside a warm-slate app. Light sent **no style array at all** and got stock
  beige + gold landuse + cyan water. Both now sit where a backdrop belongs: dark `#15181d`
  (between `--background` and `--card`), light `#eaeaef` (just under `--muted`, so plain white
  roads keep separation — `#f2f2f4` put them at 1.12:1 and the network read as a ghost).
  **Every geometry and label value moved WITH the ground**, re-solved to hold its original ratio
  within 0.05, because darkening alone raises the labels too. Payoff: destructive 4.66 → 6.13:1,
  warning 6.64 → 8.73, success 5.92 → 7.78. And the rule was closer to broken than anyone knew —
  the loudest basemap label sat at 4.55:1 against destructive's 4.66, a **2% gap**; it went to 33%.
  Light was worse still and had never been measured: its stock locality text ran 7.37:1 against
  destructive's 5.36 — *the basemap's writing outranked every alarm.*
- **TWO CEILINGS, NOT THE STATUS TONES** (2026-08-02 — dulled three times in one day at the
  operator's request; the last ask was "even more dull, just slightly visible enough to grasp
  it"). Geometry and writing fail differently, so one flat number cannot serve both:
  - **GEOMETRY ≤ `--border`**, the app's own hairline (**1.35:1 dark, 1.28:1 light**). A road is
    at most as loud as the edge of a panel: structure you can trace when you follow it, never
    something that competes.
  - **LABELS ≤ ~2.05:1 dark / ~1.75:1 light.** A LEGIBILITY FLOOR, not taste — below it place
    names stop resolving as text, and an unreadable name is worse than none because it still
    costs ink and attention. **If the map must be quieter than this the control is the EXISTING
    Layers → "Google labels" switch**, which removes the writing outright. Don't chase it with
    colour past this point.

  Now **2.06:1 dark / 1.74:1 light** for labels and **1.30 / 1.18** for geometry — ~65% under
  `--destructive`, where the loudest label began at 4.60 / 3.81. The previous rule
  (`--ghost-foreground`, the app's quietest text step, 3.42 / 2.63) is now satisfied rather than
  binding, and the original one ("rank under the status tones") was never the real constraint:
  what exposed it was `--map-link`, the colour EVERY cable on this map is drawn in, sitting at
  4.75:1 while a road NAME sat at 4.60 — a **3% gap between Google's writing and our own network
  drawing**, which is not a hierarchy.
  Every ladder was re-solved by scaling in **LINEAR RGB** (preserves hue and saturation exactly,
  moves only the level), never clipped at the top, so the internal ordering survives each pass —
  flatter and lower each time, with the TOP compressed hardest: dark highway labels went
  4.60 → 3.04 → 2.06 while road labels went 3.41 → 2.60 → 1.84, because a route number is not
  more important than a town name and was being drawn as though it were.
  **Road geometry is dulled AGAINST THE GROUND, never against its own casing** — that fill/casing
  pair is what actually draws a road. Dulling road-vs-ground makes the network recede;
  road-vs-casing makes it DISSOLVE, and light mode has already proved once that a dissolved
  network reads as a broken map rather than a quiet one. So light's brightest object, the
  pure-`#ffffff` road fill, stops at `#f9f9fc` and only its hard highway casing came down again
  (1.37 → 1.24 → 1.18, now under `--border`): on a light ground the ribbon IS the road. Dark
  holds its road casing at 1.06 for the same reason. **Dulling runs the OPPOSITE direction per
  mode** and is easy to get backwards: on a light ground quieter means LIGHTER.
  These are SOLVED SETS. Do not nudge one value; re-run the whole ratio table against BOTH
  ceilings, then `--destructive`/`--warning`/`--success`.
- **`ICONS_OFF` is unconditional on roadmap and is NOT the Layers toggle.** Google's POI
  markers are white discs with a dark ring — the exact grammar of a device pin — and a rural
  viewport renders five of them against one OLT; its highway shields are YELLOW, i.e. sitting
  on `--warning`, and stamp the same route number five times along one road. `labels.icon`
  strips both while KEEPING the place-name text a crew navigates by; the Layers switch is
  all-or-nothing (`elementType: "labels"` covers text AND icon deliberately), so it could only
  ever have traded the discs for a nameless map. **Geometry still untouched** — parks, water
  and footprints survive. Route numbers are the one real loss; restoring them is one line.
- **`road.highway` geometry was stock tan (`#746855`) the whole time** although the night-style
  comment claimed "deliberately NEUTRAL now, not tan" — only the label FILLS were ever changed.
  It ran 2.49:1 against ordinary road's 1.31:1, warm, at the scale of a river. Neutralised to
  `#3b414b` (1.73:1), then 1.44, now `#292e36` (**1.30:1**, just under `--border`): still a clear
  step above ordinary road (1.18:1), cool, so nothing on the basemap can be mistaken for
  `--warning`. This is the ONE ratio in the array deliberately not preserved. **A comment
  claiming a colour was fixed is not evidence that it was** — that one was wrong for months.
- **`variantOf` may never return `""` for roadmap.** A light+labelled roadmap used to be the
  one UNSTYLED variant and so keyed with no `:s…` suffix — now that ICONS_OFF and DAY_STYLE
  always apply, leaving it that way would let every browser holding such a token serve stock
  tiles for the ~2 weeks until it expired, invisibly to whoever shipped the change. Hence the
  `|| "b"` fallback. (The style HASH already in the key covers subsequent palette edits.)
- **Rate chips are kilobit-floored and collapse to one word when idle**
  (`linklabel.ts:IDLE_BPS`/`bwIsIdle`/`fmtShort`, shared with `refonu.ts` — ONE copy, or a
  trunk's chip and a drop's chip teach two readings of the same badge). `↓29 ↑171` is
  twenty-nine BITS per second rendered at the weight of `↓3.7M`; a viewport of those can only
  be read, never skimmed. Chrome dropped to a hairline border + weight 500 to match: a rate is
  reference data and was outranking the one red pin that was the reason to look.
- **ONE screen-space budget reserves pixels for BOTH chip families**
  (`map-page.tsx:chipShown`). Link chips sit at the midpoint and links CONVERGE on devices, so
  they landed on top of each other — and a subscriber chip collides with a link chip just as
  readably, so two independent budgets would each report themselves clear. Greedy, ranked by
  `bwRank` (trouble outranks every rate, then the busiest link; subscriber chips are offered
  pixels last, dark ones first). A suppressed chip is not lost — zoom spreads the midpoints and
  it returns. **A NEW chip family must join this budget, not start a third one.**
- **A LIVE located subscriber is the quietest fill on the map** (`--success 32%`, vs the
  witness's 70%). It is the single most common mark on a surveyed fleet and not one of them is
  news. Quieted by SATURATION only — **not size**, which was tried twice and came back
  unreadable both times; the dark mark keeps full destructive tone, so the gap between "fine"
  and "not fine" widens rather than narrows.

**Stroke weight is SCALED TO ZOOM** (`map/stroke.ts`, 2026-08-02 — operator: "when i zoom out
lines look thick enough but while zooming in they become too much thin"). Every line here is a
fixed PIXEL width, which is not fixed in the sense that matters: each zoom level doubles the
ground scale, so the same 2.5px stroke has to span twice the screen and a cable that read as a
link across a town becomes a hairline crossing the viewport.

- **The curve is linear in the ZOOM LEVEL, i.e. logarithmic in scale** (`1 + 0.135·(z − 13)`,
  clamped to [1, 1.85]). Holding stroke-to-span ratio constant would mean DOUBLING the weight
  per level — 2.5px would reach 160px by z19. What is wanted is a constant addition per
  doubling. A feed goes 2.5 → 3.85px at z17, → 4.53 at z19.
- **It FLOORS at 1.0** and so changes nothing at or below z13. The zoomed-out end was reported
  as already right, so this may only thicken, never thin — which also keeps fleet altitude, the
  zoom every weight in this app was originally judged at, byte-identical.
- **ONE multiplier for every line, and that is the load-bearing part.** The relative weights
  carry meaning that was tuned by eye: a feed (2.5) outranks a peer (2), a selected path (3.5)
  outranks both, a dark drop (4.5) is heaviest in its layer, a hover adds half what emphasis
  adds. A uniform factor preserves every ratio by construction; a per-kind curve would silently
  re-rank them at some zoom nobody tested.
- **The dash MUST scale with the weight, and `strokeAt`/`casingAt` take both together so a
  caller cannot do one and forget the other.** This is the documented trap: SVG dash lengths are
  absolute px, so widening a dotted line without opening its gaps closes it into a SOLID one —
  and on this map solid means traced fibre a crew quotes drum off. Scaling both is exactly
  zoom-invariant rather than approximately so: with round caps a dash paints `on + w` separated
  by `off − w`, and `(on·k + w·k)/(off·k − w·k)` cancels k. Verified — the ref-ONU line reads
  1.00 dot-to-gap at z13, z16 and z19 alike. `casingAt` applies the phase correction BEFORE
  scaling, so casing and stroke periods stay equal at every zoom (8.5 → 15.39 together).
- Applied to every geographic stroke: topology links + casings, ref/drop lines + casings, the
  cut overlay, the branch-fault span, the power hull, the route editor, the worker trail + its
  casing, the locate accuracy circle. Screen-space quantities are deliberately NOT scaled — the
  chip collision boxes, the chip min-span tests and `CLUSTER_PX` are facts about the screen, not
  about the ground.
- **`ROUTE_FOLD_SLACK_PX` used to be on that list, did not belong there, and is now GONE
  ENTIRELY** (2026-08-05) — see the next entry. Half the lesson still stands on its own: *a
  constant in screen px is a bug wherever the other operand is a fact about the ground.* That one
  was compared against a pin's displacement from its cluster centroid, which doubles per zoom
  level, so it passed zoomed out and failed zoomed in.

**A TRACED ROUTE IS DRAWN UNLESS IT HAS NOWHERE TO GO** (`map-page.tsx:foldedTogether`,
2026-08-05). A cluster fold moves a pin to its site's centroid, so a route anchored on it starts
slightly off where it was surveyed. **Three rules in a row tried to answer "how far is too far",
and all three were wrong** — the third was found by the operator in one afternoon on
badri_fiber's `Gpon_08→Gpon_04`, at exactly z17 and nowhere else.

- The attempts: exact equality (racked gear never drew a route at all); then 10 screen px (the
  ground-vs-screen unit bug above, permanent past z20); then 10px OR 8% of the anchored segment
  — which still failed wherever the anchored segment is itself short. At z17 that OLT folds in
  with SPL-1/SPL-5, the centroid sits **11.9px** off and the last segment is **24px**, so neither
  clause could pass. z16 and z18 were fine, which is what "at specific zoom levels" means.
- **Three attempts is the tell that the QUESTION is wrong**, because the outcomes are not
  commensurable: a fold nudges the FIRST OR LAST SEGMENT by at most a cluster radius, leaves
  every waypoint between untouched, and self-heals when the cluster splits — while a chord
  replaces the whole surveyed path with a straight line **indistinguishable from a real one**,
  which crews order drum off. Bounded cosmetic artifact versus unbounded false claim. There is no
  distance at which the second wins, so the rule stops measuring and asks only whether the route
  can be drawn at all: it can, unless BOTH ends landed on one point (every device in one badge,
  where the route would be a scribble looping from a dot back to itself and a zero-length chord
  is correct). Verified across z10–22 on every traced route both orgs have: the only folds left
  are z10–11 on a fully collapsed org.
- **`drawnLinks` no longer depends on `zoom`, and that is a property worth keeping** — the old
  rule could change its mind per zoom level, and a map that draws surveyed cable at z16, a
  straight line at z17 and cable again at z18 teaches an operator not to trust any of it.
- Fixed alongside: an empty waypoint list (a `link_routes` row kept alive by a colour or a label
  position) was setting `drawn: true`, which suppressed `linkhover`'s "straight-line" note on
  what is geometrically a chord.

**PASSIVE PLANT IS OUT OF THE CLUSTERING PASS** (`clusters.ts:buildClusters`, 2026-08-05 —
operator: "passive devices should not combine with active devices, they should be treated like
customer locations"). **A site badge is a claim about GEAR**: a count, a status ring and a card
of boxes that each have a state and an outage. A splitter has none of those, so folding one in
makes the count answer a question nobody asked ("4 devices, 1 down" where two are plastic) and
hides the box a crew drives to behind a number. It is the same argument that already kept
subscribers out of this pass — plant was simply on the wrong side of that line.

- Each passive comes back as its OWN single-member cluster rather than being dropped. The render
  walks this list, so a passive missing from it would vanish from the map, lose its `pinPos`
  entry and orphan its drop lines.
- It was not a corner case: badri_fiber's **Gpon_04 was buried inside a badge with up to four
  splitters from z12 through z17** — that is the OLT an operator opens.
- **This independently kills the z17 straight-line fold above**, since Gpon_04 was folding with
  SPL-5 exactly there. The `foldedTogether` rule and this one are separate fixes for one report
  and both are worth keeping: one stops plant hiding gear, the other stops any fold discarding
  surveyed geometry.
- Cost, accepted: dense plant now OVERLAPS at low zoom rather than folding — what subscribers
  already do. Plant is far sparser than drops, and a splitter you can see beats a badge that
  counts it. Consequence to keep in step: the clustering pass is no longer one of the things
  telling a splitter from a subscriber, so the hole, the ratio plate and the plant-vs-drop rank
  carry that alone.

**MARK SIZE IS A LADDER, NOT A NUMBER** (`index.css:THE MARK SCALE`, 2026-08-05 — operator:
"every time I add a new type like splitter they are again small size; make the sizing a proper
standard so this won't happen"). Same shape of fix as the stroke multiplier, for the same
failure: every new kind got a hand-picked px, every hand-picked px came in under the last, and
the kind arrived unreadable. Splitter 40px² against an FDB's 81px²; a located subscriber drawn
"the smallest mark on the map" TWICE (11px, then 9px), illegible both times; CPE at 10px, less
than half the router beside it, with no comment saying why.

- **A mark picks a RANK and a SHAPE FACTOR. It never types a px** — that is the whole rule, and
  a new kind's only question is what it CLAIMS. Four ranks: `--mark-evidence` (a DARK reference
  ONU, the one mark allowed to outrank gear, gated on `isRefEvidence` and nothing looser) >
  `--mark-gear` (has a state and an outage) > `--mark-plant` (a passive box a crew drives to)
  > `--mark-drop` (one subscriber).
- **Ranks are stated as INK, so a rank is one number and every silhouette in it matches.** A
  shape's area is not its width: at one width a square carries 0.97·W², a rounded square 0.89,
  a teardrop 0.84, a circle 0.785, a diamond stated by its BOUNDING BOX 0.5 — which is exactly
  how two kinds both reading "9px" in the source became a 2× ink difference on screen. A rank
  is therefore the diameter of the equivalent CIRCLE and `--markshape-*` converts. Measured on
  the OUTER silhouette: a hole (the core donut, the splitter) is how a kind is TOLD APART,
  never how it is sized. Rotation is free — a rotated square is a `--markshape-square`.
- **`--mark-gear` is the calibration and the OLT is what it was solved from** (the operator's
  own reference: the OLT is "the right size, visible properly and easy to grasp"). 15.6px ×
  `--markshape-square` = the 14px square it has always been, so the anchor is byte-identical to
  the one mark already judged right on real tiles. The other ranks are RATIOS of it. **A "make
  the marks bigger" ask is now ONE number** and no kind can be left behind by it. Measured after
  the pass: within-rank ink agrees to ~1.5%.
- **The steps between the three lower ranks are SMALL (1.10 / 1.00 / 0.98 / 0.96), and that is
  the point, not a fudge: ink encodes ORDER here, never EMPHASIS.** Emphasis is tone (a live drop
  is the quietest fill on the map, plant is always muted, only gear takes a status colour),
  stacking (`refZIndex`/`workerZIndex` are negative), the clustering pass only GEAR joins, and
  the Map-detail zoom floors. Ink's only other job is to clear a
  LEGIBILITY floor. The first cut of this ladder still gave drop a 15% step down from plant and
  the operator called it small AGAIN, an hour later and having been shown the ladder — the third
  such report in three weeks, against an invariant ("shrinking is not a subordination channel")
  that had been written down since 2026-07-29 and quoted rather than applied. A customer pin's
  round head is now 14.50px against the OLT square's 14.01. **If a surveyed street ever reads as
  too busy the levers are the zoom floor, the layer toggle, the PON scope and the fill
  saturation; this number has been tried three times.**
- **Corner radii, holes and the teardrop's tip offset are PROPORTIONS** (`%`, `--mark-tip`), not
  px. A fixed px there makes a shape's factor drift with its rank, i.e. reintroduces the bug one
  level down — and the subscriber pin's tip now lands ON its coordinate at every rank instead of
  within ~1px at the one rank somebody measured.
- What changed with it, all in the same direction: subscriber 101 → **176px²** of ink (the
  actual complaint), dark witness 164 → 231, splitter 142 → 184, CPE 79 → 191, FDB/closure
  ~79 → ~181, gear circles 154 → 191, OLT and backhaul UNCHANGED. **The witness/plain size gap
  is retired**
  — it was 1px, too small to be a distinction, and it was the reason a located subscriber was
  hard to find; the RING (`--plain` drops it) and the tone carry that job now.
- Subordination did NOT move: plant is still under gear and muted, a live drop is still the
  quietest fill on the map, subscribers still stay out of the clustering pass and under every
  device pin. Those are TONE, STACKING and CLUSTERING — **shrinking is not one of the channels**,
  and it is the one that failed twice.

**QUIETING A STATUS COLOUR MEANS CHROMA AT FIXED HUE, NEVER A MIX TOWARD THE GREY**
(`index.css:--map-live`/`--map-live-quiet`, 2026-08-05 — operator: "use proper green for the
pins because its blueish right now and its similar to dotted line"). `color-mix(… var(--success)
N%, var(--muted-foreground))` does not desaturate green: this palette's greys lean COOL, so the
mix drags HUE as it drops chroma, and the less chroma is left the more the cast is all there is.

- **Measured, because it is invisible in a swatch and in review.** Dark: `--success` is hue 154°
  chroma 0.126; the located-subscriber fill at 32% came out **hue 167° chroma 0.040**, light mode
  **175°** (cyan) and 0.04 of lightness lower than anything asked for. `--map-link`, the DOTTED
  DROP LINE the operator compared it to, carries chroma 0.057 — **the line was more colourful
  than the pin on the end of it.** Now hue 155° at 0.085/0.072, 74–79° off the line.
- The two tokens are `oklch(from var(--success) l calc(c * K) h)` — **derived from the token, not
  a hex**, so a superadmin recolouring `--success` in Settings → Appearance carries through. They
  are deliberately NOT in `theme.py:_TOKENS`, same as `--map-line-*`: cartography, not branding.
- **The `@supports` pair is load-bearing, not ceremony.** A custom property holding an
  unparseable value fails INVALID AT COMPUTED-VALUE TIME where it is USED, so `background` would
  resolve to `unset` and the mark would vanish — the earlier-declaration fallback does not rescue
  a `var()` indirection. (Lightning CSS then adds its own `color-mix` tier below ours; the built
  chain is `var(--success)` → mix → oklch, in that cascade order.)
- Fixed in all three places that had it: the live reference ONU, the located subscriber, and a
  splitter whose drops are healthy. **If any of these needs to be quieter again, move the chroma
  ratio and nothing else** — the moment it is mixed toward a grey it bends back toward the line.

**Hover and zoom** (same pass): hovering a pin lights the CABLES INTO IT (`hoverId`, direct
links only, +0.75 weight — selection still owns the whole downstream PATH, and if the two
looked alike sweeping a dense site would read as the selection jumping). Mark hover affordance
is **pure CSS on `transform`** — icons are cached by html string precisely so a re-render can't
swap every marker's DOM node, and `useNow()` ticks this component every second, so a hover
routed through React state would restart the down-pulse on mouseover; `width`/`height` are out
because the dot anchors the optical ring, the impact ring and the pulse. `zoomSnap={0.25}` +
`wheelPxPerZoomLevel={120}` because one wheel notch was a 2x scale jump and the level you want
is usually between two of them — every threshold here is a `>=` and both chip budgets are in
projected pixels, so fractional zoom is safe. The subscriber layer fades in (`wisp-mark-in`,
opacity only, **no `to` keyframe** so a `--dim` mark still lands at 0.28). **A mount animation
may ONLY go on a mark whose html string is stable** — Leaflet swaps the DOM node whenever
`cachedDivIcon` returns a new icon, which REPLAYS the animation: rate chips rebuild on every
reading, and worker marks embed `ago(last_fix.ts)` in their title so they rebuild about once a
minute. Both are deliberately excluded; `refTitle()` carries no relative time, so diamonds are
safe.

Verified in a real browser, both themes, on live Google tiles against a copy of prod's DB —
which is how the POI discs were found at all; they are invisible in a code review and were
never in a screenshot anybody had cropped.

**Hovering a BOX opens the same card a subscriber does** (2026-08-05, operator's ask). One
frame, `map/hovercard.tsx` (`.wisp-mapcard*`, renamed from `wisp-refcard` when it stopped being
subscriber-only): surface, tail, edge clamp, tinted verdict row, label/value rows, the
two-entry icon cache. `map/refhover.tsx` and `map/devhover.tsx` supply only a MODEL of what
each may claim — a second card grammar for the second thing that opens one is how a dashboard
stops reading as one product.

- **Gear is toned by `pinTone`, plant by `dropTone`** — the very functions that fill the two
  marks, called (not re-derived) so a card can never grade a box differently from the pin that
  opened it. Gear's hero number is its round trip, plant's is its recorded drops' MEDIAN Rx
  (always `quiet`: it is reference, not a verdict, and it is measured on the drops still up).
- **Every frozen rule carries over.** A DOWN box drops its ONU/port/vitals rows for one
  `Readings: frozen while it is down`; a splitter whose OLT is down reads `N recorded · state
  unknown` and names that OLT, because "6 of 6 dark" there is the OLT's outage restated on a
  box that has no outage. That last one also FIXED THE PIN: `dropTone`, `passiveSubLabel` and
  `passiveTitle` now take `frozen` and stand down together, or the tooltip contradicts the mark
  it is attached to. Detection is untouched — `drops.branch_faults` already skips a down OLT.
- **Rows are content-measured in a BROWSER, not estimated.** Three shipped truncated and were
  caught only on screen: RAM pushed the vitals row past the value column (dropped — nobody acts
  on a switch's memory), median+worst shared one row (split, median to the hero), and "below
  this box's own median" ellipsised away the word `median`. `.wisp-mapcard__k` is 3rem and does
  NOT ellipsize; "Readings" (~52px) is the longest key that fits, overhanging into the gap.
- **The link-distance readout stands down near a pin** (`linkhover.tsx:PIN_KEEPOUT_PX`, the
  operator's actual complaint). Within a box's reach every cable into it is inside
  `HOVER_SLACK_PX`, so the readout has nothing to say — ~0 to one end, the whole span to the
  other — and it fought the card. 32px, and the number is set by WHERE THE RING IS CENTRED:
  `.wisp-pin` is a dot-over-label column translated -50%, so the coordinate the lines converge
  on sits ~12px BELOW the visible dot. A ring sized to the dot's own radius left the readout
  alive to within 18px of it (measured). Suppressed outright while any card is open, same rule
  as selection and subscriber hover.
- **Passive marks are sized by INK and were left out of the subscriber pass.** All three read
  "9px" and were three different marks — a rotated square covers half its bounding box, a circle
  ~79% — so the SPLITTER, the box a crew drives to, carried 40px² against the FDB's 81px². Now
  ~72–81px² each (splitter 12px, closure 10px, FDB 9px): half a device dot (154px²), level with
  a located subscriber's diamond (13px, 84px²). Checked on real tiles at 9/12/13/14 — 14 read as
  competing with the dark-subscriber mark, the one thing on that layer allowed to shout.
- **Chrome-over-tiles trap**: shadcn outline Buttons carry `dark:bg-input/30`, which BEATS a
  plain `bg-popover/95` override in dark mode — map chrome needs
  `bg-popover/95 dark:bg-popover/95`.
- **`.wisp-map-wrap { isolation: isolate }` is load-bearing**: Leaflet stacks its own panes at
  z-index 400–1000, so with the wrapper at `z-index:auto` those land in the ROOT stacking
  context and BEAT every Radix portal (dialogs, dropdowns, selects, the command palette are all
  `z-50` on `<body>`) — anything opened over the map rendered behind the tiles, and Leaflet
  painted over the app header. Isolating confines the ladder to the map; its own chrome is
  INSIDE the wrapper so `z-[1000]` still wins. Don't "fix" a future instance by bumping portal
  z-indexes — that fights the whole shadcn layer. Floating map chrome belongs INSIDE the control
  column, not at a hardcoded `top-[Nrem]`: the stack's height depends on which controls render,
  and the edit-mode hint's fixed offset landed on the very button that leaves edit mode.
- **Leaflet trap**: `pathOptions.className` is silently DROPPED (setStyle ignores it) — pass
  `className` as a top-level react-leaflet prop and include the tone in the key so a state
  change remounts the path. **Topology polylines MUST stay `interactive={false}`** or they
  swallow placement clicks.
- **Placement & pins**: `lat/lng` write only via `POST /api/inventory/location`
  (paired-or-both-null; dashboard-side only — the edge never sees coordinates). The
  click-through panel is the same `device-detail.tsx` the tree rows use (extracted, not forked —
  keep it shared). **Map divIcons are CACHED by html string** — `useNow()` re-renders every
  second, and an uncached icon swaps every marker's DOM node per tick, restarting the
  down-pulse. **Role shapes**: `wisp-pin--t-<device_type>` picks the SILHOUETTE (fill stays
  health, ::after ring stays optics) via border-radius / rotation / ::before ONLY — clip-path
  would clip the selection box-shadow and the down-pulse. Passives render small + always-muted.
- **The viewport is LOCKED to `orgs.map_region`** (bounds in `lib/map-regions.ts`, unknown key →
  all-India, `world` = no lock). All view logic lives in ONE `useMap()` child
  (`ViewController`) — a ref on `MapContainer` isn't set yet when a query resolves — and the fit
  MUST run before `setMinZoom`: min-zooming a zoomed-out map fires an ANIMATED setZoom that
  lands after and overrides an `animate:false` fitBounds. Control buttons shift left of the open
  device panel on desktop or it covers them.
- Map search = device match (instant) + OSM Nominatim geocoding (browser-side, debounced 450ms
  + 3-char floor — stay a polite keyless client; results boxed to the org's map area). Picking
  an unplaced device starts placement.
- **Site clusters** (the spider-fan was REMOVED): pins that would overlap on screen fold into a
  count badge with a conic composition ring — SCREEN-SPACE and zoom-dependent, greedy in Web
  Mercator px. The fan scattered pins onto real coordinates and read as geography (the ONU-spoke
  lesson again), so co-located members resolve in UI SPACE now: badge click = fitBounds when
  genuinely spread, else a SITE CARD listing members, anchored by member DEVICE id so it
  survives zoom reshuffles. Placing devices at the same coords IS the "rack", no schema — and
  co-location is deliberate: in placement mode a badge/pin click snaps to that site's EXACT
  coords, and an edit-pins drag dropped within 24 px of a neighbor snaps to it (near-stacks were
  the fan's original sin). `pinPos` is raw-or-centroid ONLY; a folded selection highlights the
  badge and auto-opens the card, so search never lands on a hidden pin. **Don't reintroduce
  display positions that aren't true locations.**
- **PON on the map**: OLT pins ring amber/red off `onus_warn/crit` (suppressed when
  maint/down/stale-optics). The per-ONU spoke fan was REMOVED at the operator's request — EPON
  ranging gives distance but no bearing, so spoke angles were fabricated, and on a map
  everything reads as geography. **The map shows only true locations**; ONU severity lives in
  the pin ring + the Optical tab. ONUs return only if they ever get real coordinates
  (`focusOnuId` threading survives for that).
- **Cable routes**: `link_routes` keyed (org, child, parent) covers primary AND backup links;
  waypoints are INTERMEDIATE vertices only, parent→child — endpoints stay implicit so moving a
  pin rubber-bands the route. `list_link_routes` joins against the live link, so a re-parent
  hides stale geometry instead of drawing a lie. `/api/inventory/routes` is map-only,
  deliberately NOT folded into `list_org_devices()`. The renderer falls back to the straight
  chord whenever an endpoint folds into a cluster (a route snaking into a centroid reads as an
  error), and the editor is ~100 lines of plain react-leaflet — no leaflet-geoman. The device
  panel shows "cable" (route length) AND "straight-line" (chord), labeled honestly, because
  splicing crews quote drum metres off this.
- **Cross-links are keyed (child=higher, parent=lower) in `link_routes`** — the OPPOSITE of
  `org_device_links`' (min, max) — so waypoints run parent→child for every kind and the map
  needs no per-kind reversal. `list_link_routes` and `_link_write_scope` match a peer in either
  order. Until this was fixed, **every peer route save 400'd** although the map offered the
  editor.
- **A span's own record rides `link_routes`, not a second table**: that row is keyed exactly
  "one span", so `cable_id`, `core_no` and `label_pos` live there beside the geometry.
  Consequences: `set_link_route` no longer DELETEs on an empty waypoint list
  (`_prune_link_route` drops a row only when waypoints AND cable AND strand AND label_pos are
  all empty, or straightening a path would silently forget which cable it is in), and
  `set_link_style` is SPARSE so the cable picker, the strand picker and a chip drag can't
  clobber each other.
  **THE PER-LINK COLOUR IS GONE** (2026-08-08, operator's call) — see the cable section below.
  Its only real use was saying "these spans are one physical cable", which `org_cables` now says
  properly; `map/linkcolor.ts`, `paintedLineColor` and `inventory.LINK_COLORS` are DELETED and a
  line's colour is its tone plus selection emphasis, nothing else. The PALETTE itself survives
  under `inventory.PALETTE` for tags and probes, and the `--map-line-*` token prefix stays for
  history. Those tokens are ONE set for both themes (unlike every other colour here): the
  backdrop is raster tiles, equally bright under either app theme, and the dark casing does the
  contrast work. Deliberately NOT in `theme.py:_TOKENS` — cartography, not brand theming.

### THE FIBRE PLANT: cables between couplers, and what each core is joined to (2026-08-09)

`central/fiber.py` ↔ `web/src/lib/fiber.ts`, `central/cablepath.py`, `org_cables` +
`org_fibre_joints` + `org_cable_cores`, `components/cable-record.tsx`,
`components/coupler-tray.tsx`, `map/cables.ts`, the cable card and the Fibre section in
`map-page.tsx`.

**THE ISPs CORRECTED THE MODEL AND THEY WERE RIGHT.** Their own sentence: *fibre runs
between two couplers, and at a coupler you join cable to cable, or take a core out to a
device on a single fibre* — plus two facts that follow, *any core may carry anything,
including a customer line*, and therefore *a customer point is a coupler too* (which is
what makes a lane of daisy-chained houses possible: core 1 into this one, cores 2–4
onward to the next three). They also agreed to ABANDON the recorded cable and lay it
again rather than carry two models, which is what made replacing it possible.

**A CABLE IS A SEGMENT WITH TWO RECORDED ENDS.** That is the whole change, and every
other change here is a deletion that falls out of it:

- **The RUN is gone.** `org_cable_runs` was *(cable, core, device A, device B)* — glass
  could only be recorded between two BOXES. A cable knows its ends now, so core N of it
  runs between them BY DEFINITION and there is nothing left to write down.
- **The TAP is gone.** `org_cable_taps` recorded where a sheath is opened for a box,
  because the boxes hung off a cable were not ON it. A cable end IS the tap. No
  projection, no lateral, no re-snapping on retrace, and `cablepath.between`/`span_path`
  went with it.
- **The DOUBLE-BOOKING CHECKER is gone.** `core_path`/`core_faults` existed to catch two
  unrelated runs both written as core 7 — the error that sends a splicer to cut a live
  customer. A core of a segment has exactly two ends and cannot be two disconnected runs,
  so that state is UNREPRESENTABLE rather than merely unreported.
- **The IMPLICIT-CONTINUITY RULE is gone with it.** "Two sections of one cable on one core
  meeting at a box are continuous by definition" was the load-bearing sentence of the old
  model, and the reason a splice restating it had to be refused. Two sections of one cable
  can no longer meet — opening a sheath mid-span SPLITS it and splices every core through.
  One fact, one home, and the home is now a row somebody can see.

`unit/test_fiber:SegmentModelTest` and `unit/test_cablepath:SegmentModelTest` assert those
names STAY deleted, the same way `LINK_COLORS` must stay deleted: re-exporting one is how
the old model creeps back.

- **A FIBRE POINT is a device OR a subscriber, and it is NOT a table.** Passive plant
  already lives in `org_devices` and customers in `onu_places`; a third registry of places
  is the thing this codebase refuses everywhere else. Carried as a nullable PAIR
  (`a_device_id`/`a_mac`) so the device side keeps a real foreign key — that is what makes
  deleting a box take its cable with it — while the subscriber side is a MAC, because
  `onu_places` is keyed (org, mac) and has no stable id to point at. `fiber.py` treats a
  point as an opaque hashable and never looks inside one, which is what lets its walks be
  tested against plain tuples.
- **THE JOINT BOX IS A `closure`.** This line used to read *"`coupler` is the ISPs' OWN
  WORD"* — it was not, it was ours, and it was corrected on 2026-08-11; see THE RECORD STARTS
  FULL below for the rename and why the type is nevertheless kept in the tuple. The reasoning
  around it survives intact: a cable end has to land on something, laying one creates a box at
  each end that lands on empty ground, and a vocabulary an operator has to translate their own
  plant into is the first place a survey stalls. Only the word was wrong.
- **A cable ends on WHATEVER IT IS DROPPED ON** — a closure, an OLT, a splitter, a
  customer (operator's call). Taken literally, "always between two couplers" would mean a
  second pin beside every OLT, splitter and customer: 76 extra marks on the current fleet.
  The rule it was really protecting — *a line must never appear because of a setting* — is
  kept and was already true.
- **`parent_device_id` IS UNTOUCHED.** It is the monitoring dependency that decides
  suppression and therefore what pages, so a recorded splice may never move it. Nothing in
  this feature writes to `org_devices` or `org_device_links` — except the coupler a SPLIT
  creates, which is safe for exactly one reason: a passive is excluded from
  `org_device_topology` and is created with no parent.
  `test_recording_fibre_NEVER_reaches_the_engine`.
- **The PLANT chain still comes from the fibre when nothing is declared**
  (`org_plant_feed_map` over `fiber.feed_map`), and DECLARED still wins. A feed arriving
  THROUGH a customer is dropped rather than reported: the walk follows a daisy chain
  correctly, but that map is device→device and there is no id to name a subscriber with.

**A FIBRE LANDS ON A PORT** (2026-08-10, the ISPs' second correction, on first contact
with the map: *"when I select an OLT it should show me ports, that's what we connect"*).
A termination said only "into the box standing here", so an 8-PON OLT with eight fibres
on it drew eight identical rows and the record could not name a single PON. It is the
noun the segment model was missing, and the ISPs' two points — this, and opening a
closure mid-span — turned out to be one point: **what you connect to is a port**.

- **A PORT IS AN ATTRIBUTE, NOT A TABLE** (`fiber.PORT_KINDS` = `pon | leg | in`, two
  nullable columns on `org_fibre_joints` + `onu_drops.leg_no`). The enumeration is
  DERIVED (`fiber.port_slots`) — a splitter's ports are its `split_inputs` and
  `split_ratio`, an OLT's are the PONs it reports — so a registry of rows nobody creates
  would be `org_cable_runs`/`org_cable_taps`/`org_splices` for a fourth time. **Additive:
  nothing rebuilt, nothing wiped**, and every joint written before it reads "port not
  recorded", which is what it is.
- **IT IS NOT AN INTERFACE, and must never become a `switch_ports` row.** That table is
  the WALK's view of an ifTable row — upserted every sweep, keyed on `if_index`, and read
  by `ports.py`, **which pages**. Ports here exist on boxes with no SNMP at all (three
  OLTs on the live fleet walk nothing, and no splitter ever will) and may never alarm.
  Where a box does walk its PONs the two are matched for display and never merged.
- **AN ENCLOSURE HAS NO PORTS, and that is what kept this change small.** A coupler, a
  closure and an FDB are boxes where sheaths are OPENED: every fibre in one is a splice,
  and the schedule already answers for them. **So the tray has TWO ORDERINGS of one
  read** — at a coupler the rows are CORES (*what does each fibre do*), at a box they are
  PORTS (*what is on each one*). A SECTION, never a mode: a box can genuinely have both,
  and a view you must choose before you can ask a question is what the two-column tray
  was retired for. The panel that was wrong was never the coupler's.
- **BOUNDED WHERE WE HOLD THE FACT, UNBOUNDED WHERE WE DO NOT** (`fiber.port_bound`).
  `leg`/`in` are bounded by the ratio — leg 9 of a 1:8 is a typo we can prove — and
  **`pon` is deliberately unbounded**, because nothing tells us how many PONs a box has
  and refusing PON 9 would be refusing the operator's own sight of it. So an SNMP-silent
  OLT gets a row to NAME a port (`port_add`), or that promise lives only in a docstring.
- **NAMING A PORT IS PROMPTED, NEVER REQUIRED** (operator's call). A tech at a closure
  routinely cannot see which PON the far end took, and refusing the write would trade the
  connection we could record for a port number we cannot. `port_taken` (one port, one
  fibre) and `port_splice` (a port belongs to a termination, not to a splice inside the
  sheath) are the two new named refusals.
- **THE PON A BOX IS ON IS NOW WALKED, NOT TYPED** (`fiber.pon_of_points` →
  `store.org_fibre_pon_map` → `OrgDevice.fibre_pon`). This is what the port PAYS BACK and
  the reason the record stops being write-only documentation: `org_devices.pon_port` is
  free text filled in on THREE of the forty-seven splitters on the live fleet, and a fibre
  landed on PON 3 answers the same question for every box below it, inherited down the
  plant chain. **It never overwrites the typed field** — one is the operator's claim, one
  is the record's, and the splitter panel shows BOTH when they differ, because a
  disagreement is a mis-recorded splice or a re-patched port, i.e. a finding rather than a
  conflict to resolve silently. Two PONs reaching one box reads `ambiguous`, never a pick.
- **A ROSTER LABEL AND AN INTERFACE NAME ARE PARSED BY DIFFERENT RULES**
  (`fiber.pon_index` vs `pon_index_of_interface`), and this was found by rehearsing
  against production rather than by review: read permissively, `GE0/9`, `GE016` and
  `VLAN10` are PON ports — so two 8-PON OLTs offered SIXTEEN and an Ethernet uplink became
  somewhere to land a subscriber's fibre. **The source decides the rule**: every row of
  `onu_optics` is an ONU on a PON, so a bare `3` there is one (the Syrotech build writes
  it that way); an interface name is one only if it SAYS so. The list is a SET, never a
  range — HILL-OLT-1 really runs 1,3,4,…8, and a stray `60` from a partial walk costs one
  odd row instead of fifty-two invented ones.
- **A DROP IS A FIBRE ON A LEG, and `onu_drops` is not being merged into anything.** It is
  live, it carries the map's drop geometry and branch-fault localization runs on it — so
  the leg rows REPORT it (read-only; recording stays the splitter's bulk dialog, or one
  drop gets two ways to be written) and `leg_no` is sparse and optional. A drop with no leg
  is stated outright on the panel rather than left to be inferred from legs that look empty.
- Tests: `unit/test_fiber:PortTest`/`PonReachTest`, `integration/test_central_cableplant:
  PortTest` (both orderings, both refusals, the unbounded PON, the SNMP-silent OLT, the
  drop-on-a-leg, the derived PON and its inheritance, and the engine-fingerprint guard).

**CONNECT A PORT STRAIGHT TO A BOX — the gesture the whole subsystem was missing**
(2026-08-10, the operator's third correction and the sharpest: *"first we had to create
couplers, then connect, then go to that device and configure the usage — way too round
about"*). Recording *"Gpon_04 PON 2 feeds SPL-5"* took **EIGHTEEN interactions across three
panels**: trace a route, name it, pick a fibre count, then open each box in turn and pick a
cable and a core on each. Every one of those existed because every write started from a
CABLE — correct about plant, backwards about work. It is now **four**: open the box, click
the port, pick the box, done.

- **ONE MENU ON THE PORT ROW, TWO WAYS TO ANSWER IT** (`SourceMenu`). *A core of a cable
  already landing here* — right at a closure with a 24F in it — or *a box*, and the cable is
  laid and landed at both ends in one write (`POST /api/inventory/fibre/connect` →
  `store.connect_points`). Nearest-first with the distance printed, refusing nothing, exactly
  as the tail picker does.
- **A MACRO, AND IT MUST STAY ONE.** It writes the rows a patient operator could write by
  hand — a cable between the two points, a termination at each end — so `trace`,
  `split_org_cable`, the delete cascade and the tray's own refusals need no knowledge that a
  shortcut exists. The moment it records something the tray cannot, there are two models of a
  connection again.
- **THE CABLE IS 1F AND NAMED FOR THE CONNECTION** (`Gpon_04 PON 2 → SPL-5`). One core out to
  a box is one strand; an operator with a real 12F feeder raises the count on the cable panel
  and the other eleven cores become available to the port rows at both ends, which is one
  sheath carrying eight PONs. Naming it for the PORT is what tells eight of them apart — the
  lesson `_tail_name` already paid for.
- **THE FAR END LANDS ON THE ONLY PORT IT COULD** (`_sole_input`). A feed into a splitter with
  ONE input has exactly one place to go, so recording it there is not a guess — it is the only
  port that exists, and leaving it blank would make the operator open a second panel to state
  something the schema already fixes. A 2:N splitter has a real choice and gets nothing.
- **A LEG CONNECTS STRAIGHT TO A CUSTOMER, through the drop `onu_drops` already owns**
  (`leg_no`, sparse). One drop keeps exactly one home — the map's drop line and branch-fault
  localization are untouched — this is a second door to it, not a second store. **The bulk
  dialog sends no leg at all, so a leg is only ever SET by that write and never cleared**, or
  re-saving the whole set (the idempotent case it depends on) would wipe what was recorded a
  leg at a time. A RE-HOME does clear it, for the reason it clears the traced route.
- **THE LIKELIEST ANSWER GOES FIRST, and what that is depends on the PORT.** On a leg it is a
  customer — and the splitter's OWN legless drops before any other, because that is the list
  the panel is complaining about two lines below. On a PON or an input it is a box.
- **THREE PLACES THE OLD ORDERING WAS STILL HIDING THE NEW GESTURE**, all found in a browser
  and none visible in review: the fold defaulted CLOSED on gear and its closed line read *"no
  cable recorded"* (a sentence about plant, on the one control that connects a PON without
  any — an operator has no reason to open that); and BOTH the panel and the tray short-
  circuited an empty cable list to *"Lay one on the map"*, rendering that instead of the port
  list. The closed line now counts PORTS, the fold opens on a box that has them, and the
  empty-cable message only speaks where there is neither a cable nor a port.
- **A PORT ROW NAMES WHERE THE FIBRE GOES, NOT THE SHEATH IT GOES IN.** Standing at Gpon_04
  on PON 1, "Gpon_04 PON 1 → RAJINI HOSPITAL" repeats the row back at you and truncates the
  only new word in it. Same rule the schedule keeps for a tail; the cable's name stays one
  hover away.
- **THE CORE SCHEDULE FOLDS AWAY ON A BOX** and opens where there are no ports — the
  enclosures it was built for. At an OLT the port list is the answer, and a second table open
  underneath made the operator read two of them to find which question they had asked.
- Tests: `integration/test_central_cableplant:PortTest` (one call landing both ends, the
  ambiguous 2-input splitter, the shared refusals, that it writes only rows the long way
  could, and that a bulk drops write never wipes a leg).

### THE RECORD STARTS FULL (2026-08-11) — why five correct fixes changed nothing

Between 2026-08-08 and 2026-08-10 this surface was corrected FIVE times, each time
correctly, and the fifth ended *"we have been working on this small thing for way too
long."* The reason none of them worked is in production, not in the code:

- **badri_fiber laid FOUR cables in 36 minutes on 2026-08-10 and recorded ZERO fibre.**
  Every one produced two phantom couplers (`Syrotech JC1`/`JC2`, …); one was deleted;
  all eight were near-misses on real boxes (3.3 m from Gpon_04, 2.8 m from the striker
  splitter, 8.2 m from SPL-Pon2, 13 m from customer ARUN G). They were drawing cables
  BETWEEN BOXES ALREADY ON THE MAP.
- **The only fibre joints that have ever existed in prod are the two Haneesh made
  testing, seven minutes before writing the brief** — both with `port_kind` NULL.
- **Under the previous model it was the same**: every ISP-visible cable row came from a
  migration; the only hand-made ones were four abandoned tests.
- Meanwhile `parent_device_id` already held **all 44** of badri_fiber's splitter feeds
  and all 15 of byreddy's.

**The diagnosis: we were asking the operator to build their network a SECOND TIME, in
glass, in a vocabulary they did not have — when they had already built it once, in
`parent_device_id`, in one they did.** `Gpon_04 feeds SPL-5` was recorded weeks ago; the
only genuinely missing fact was WHICH PON, and we were asking for three. Every previous
fix made the second act of recording cheaper. None asked why there was a second act.

- **So the panel OPENS FULL** (`fiber.undrawn`, `store._undrawn_here`, `PointFibre.undrawn`).
  A box lists the connections its own topology claims and has no fibre for, each asking the
  one open question, under the heading *"On the network map, not yet in the fibre"*.
  Confirming one is **two clicks** (Connect… → PON 2) and writes the cable and both
  terminations. Measured on prod's own data: Gpon_04 opens with 8 rows, HALIYA-LAN-SW with 1.
- **A DRAFT, NEVER A CLAIM.** Nothing is stored until a row is confirmed; `parent_device_id`
  is READ and never written, by this or by anything downstream of it. Same standing as the
  outage-assign dialog marking accounts already responsible: a suggestion, never a filter and
  never an auto-assignment. A declared edge may equally be copper or a rack patch, which is
  why the heading says whose claim it is. `test_the_DRAFT_IS_NEVER_A_CLAIM`,
  `test_CONFIRMING_one_never_reaches_the_engine_either`.
- **The picker is ordered by the DECLARED TOPOLOGY, then distance** (`TrayBox.declared`,
  rendered as "on the map"). Nearest-first alone put the three junk couplers left over from
  the abandoned traces at the TOP of Gpon_04's menu while SPL-Pon2, SPL-5 and SPL-4 — the
  boxes the operator had already recorded as fed by it — sat scattered below by metres.
- **GLASS RECORDED THROUGH A CLOSURE IS RECORDED** (`fiber.connected_points(cables, through)`).
  `Gpon_04 → JC-1 → SPL-5` IS the fibre between those two boxes — a closure is where the
  sheath is opened, not another place the light stops — so a draft that only subtracted DIRECT
  cables would ask for a pair somebody had just finished recording, and clicking it would lay
  a second parallel cable. **The hop set is ENCLOSURES, never gear**, and that bound is the
  whole safety of it: plain reachability would mark every splitter under an OLT connected the
  moment its trunk was recorded, dropping every genuine last hop — over-suppression, which is
  worse than the duplicate it prevents.
- The closed fold counts it too (`FibrePanel.todo`, computed by map-page from the device and
  cable lists it already holds). The fold's own read is gated on being OPEN, and a closed fold
  reading "nothing recorded yet" on a box owing eight connections is the documented failure
  that hid this feature once already — nobody opens a control that advertises emptiness.

**EVERY BOX HAS PORTS** (`PORT_KINDS` gains `port`; `fiber.port_slots`/`port_kind_for`,
`store.device_ports`). `port_slots` returned `[]` for a switch, a router, a gateway and a
CPE, so a fibre walked all the way to one had nowhere to land — *"go and set the port on
that device"* was a **dead end on the very gesture that had just become one click**. A port
is a thing on the box, not a property of which of our tables the box came from.

- **A switch's ports are the interfaces it already walks**, read through `fiber.if_port_no`,
  which takes the number off the END of the name because that is what is silkscreened on the
  metal. **NOT the ifIndex** — `gigabitEthernet 1/0/5` is 49157 on HALIYA-LAN-SW, a number
  written on nothing. The walk's own string rides along as `device_label` for display, matched
  and never merged, the rule a PON already keeps.
- A VLAN, loopback, bridge or port-channel is REFUSED (`_VIRTUAL_IF`) — the same failure
  `pon_index_of_interface` was fixed for, where a permissive read made `VLAN10` somewhere to
  terminate a subscriber.
- **An ENCLOSURE still has none**, and that half is unchanged: a coupler is where sheaths are
  OPENED, every fibre in one is a splice, and the schedule answers for them.
- **`port` and `pon` are both UNBOUNDED**: a walk listing 28 interfaces is a floor, not a
  ceiling, and four OLTs on this fleet report none at all. A number is refused for being
  impossible (leg 9 of a 1:8), never for being unusual.

**BOTH PORTS IN ONE GESTURE** (`to_port_kind`/`to_port_no`, `BoxItem`'s submenu, `PortPick`'s).
Naming only the near port left a second trip to the far box's panel — the exact complaint that
had just been fixed, reported straight back in the other direction within a day. Optional at
every layer, because a tech at a closure routinely cannot see which PON the far end took, and
refusing the write would trade the fact we can record for one we cannot.

- **A FAR PORT IS ASKED ABOUT ONLY WHERE THERE IS A CHOICE** (`askFarPort`). The commonest
  draft by far is an OLT feeding a 1-input splitter, and there the far end is not a question —
  `_sole_input` lands it on the only port that exists. A submenu there would be one more click
  to confirm the only possible answer, on the gesture this surface has been corrected five
  times for being too long. A switch, an OLT or a 2-input splitter gets the submenu.
- **`boxOf` resolves ANY box, not the capped nearest-first `boxes`.** The cap keeps a picker a
  menu; a drafted connection routinely names a box well outside the twelve nearest, and its
  ports still have to be offerable.

**EVERY BOX'S PORTS ARE ONE ORG-WIDE READ** (`store.org_device_ports`, `GET
/api/inventory/fibre/ports`). `point_fibre` answers for the box you are STANDING AT, and every
gesture on that panel also names a box somewhere else — so the SPA was deriving what it could
(a splitter's legs, from its ratio) and offering a bare number field for everything else. At a
closure taking a core out to an OLT that is **"ports are not being detected"**, reported
2026-08-11. A port list is a fact about a box, so it is available wherever a box is named. Four
queries, same derivation as `point_fibre` — a second rule would offer a port the panel refuses.

- **NOT `/api/inventory/ports`, and that near-miss is the lesson.** That path had been the
  per-device SNMP port list for weeks, and **a duplicate key in a dict literal loses SILENTLY**
  — the new route was in the source, reviewed, typechecked and shipped while answering somebody
  else's handler, three layers from the symptom. `unit/test_routes` parses `api/__init__.py`'s
  SOURCE (the built dict has already collapsed) and fails on any repeat; all three of its
  assertions fire on the real bug.

**THE JOINT BOX IS A `closure`, NOT A `coupler`** (operator, 2026-08-11). `coupler` entered on
2026-08-09 under a note in this file claiming it was *"THE ISPs' OWN WORD"*. It was not — it was
ours, and every cable traced on the map stood two of them at its ends, so the word was on screen
constantly. The whole argument for choosing it was that a vocabulary an operator has to
translate their own plant into is the first place a survey stalls; the argument was right and
the word was wrong. **A claim in a comment that a word is theirs is not evidence that it is** —
the same failure as the night-style comment that claimed highway geometry had been neutralised
when it was still stock tan.

- Nothing creates a coupler now (`split_org_cable`, the trace's end-creation) and no surface
  says it; `store._couplers_are_closures` renames the rows. Provably safe rather than
  apparently: every coupler row that ever existed in production was auto-created by a cable
  trace — four of them, all `<cable> JC1`/`JC2` — so nobody's choice is overridden. Nothing is
  deleted, both words were already enclosures, and one column changes.
- **`coupler` STAYS in `PASSIVE_TYPES`/`PASSIVE_DEVICE_TYPES` FOREVER, and that is not
  tidiness — it is the safety property those lists exist for.** Removing a type PROMOTES any
  surviving row of it to monitored gear: into `org_device_topology`, with an FSM and the
  ability to page. A DB restored from an older backup is exactly that row. The word is retired
  from what an operator ever sees; the type is kept so a straggler stays silent plant.
  `plantmenu.PASSIVE_WORD` maps it to "closure" so such a row still renders honestly.

**A CABLE NOBODY LAID IS NOT A CABLE** (`fiber.is_plumbing`, `TrayCable.plumbing`) — the
vocabulary fix, made ONCE instead of at each of the six call sites that print a cable. The
connect and tail macros write a real 1F cable, because a fibre between two points IS one.
What was wrong is that they NAMED it, and a name has to be shown: so the panel of the box
called HALIYA-LAN-SW read **`1F a1 core 1 → HALIYA-LAN-SW`**, a coupler's schedule read
**`on a tail`**, and the operator asked what a tail was. Every one of those strings is this
codebase's bookkeeping read aloud to somebody who asked about their network.

- Plumbing = **unnamed AND ≤1 core AND untraced**. It is never listed as a cable, never
  labelled on the map, never offered in a picker, never counted; the connection is reported
  as **the box at the far end**, which is what the operator said. Its core number is dropped
  too — "core 1 of 1" is our row numbering, not a strand a splicer picks out of a tube.
- It stops being plumbing the instant somebody makes it an object — names it, raises the
  count, traces it — and NO DATA CHANGES when that happens.
- `_connect_name`/`_tail_name` are **DELETED** and must not come back.
  `store._unname_plumbing` clears the names they already wrote: **not a migration and not a
  deletion** — every cable, end and joint survives, one column this code wrote goes back to
  empty. It **rebuilds the template from the row's own ends and clears only on an EXACT
  match**, because an operator may legitimately name a cable `JC1 → OLT` and a heuristic on
  the arrow would erase it. Verified on a copy of prod: 1 machine name cleared, all 4
  operator-laid names kept.
- **MIND THAT `path` IS `None` IN PYTHON AND `[]` IN THE SPA.** `![]` is `false`, so the
  obvious mirror marked every cable a sheath and the whole rule silently did nothing. It
  shipped that way and was caught in a browser, on a coupler reading `6F a1 · 1F`. A
  `TrayCable` carries no `path` at all, which is why the server ships the `plumbing` FLAG and
  no surface re-derives it.

**A RUN OF EMPTY PORTS COLLAPSES**, exactly as a run of free cores does one table down and
for the reason the schedule already states: twenty-four identical lines are not information.
Found only in a browser — an OLT has 4 PONs and enumerating them is the ISPs' own ask, but
HALIYA-LAN-SW walks **28**, and giving each its own "Connect…" turned the dead end into a
wall. A port with something on it never collapses; the collapsed row still carries its own
Connect and names the port it acts on.

**VOCABULARY: whose word is it?** Kept because a splicer says them — *coupler, closure,
splice, straight through, core, tube, 24F, leg, PON, drop*. Removed because only we said
them — *"on a tail"* (gone with the naming), *"not recorded"* on a port (now the verb,
**"Connect…"** — a row describing its own emptiness invites nothing), *"Core plan · 0 cables
here"* (a count of nothing in our filing word, on a panel whose whole point is that no cable
is needed → **"Splices in the 24F trunk"**, and absent when there is no sheath).

Tests: `unit/test_fiber` (`PlumbingTest`, `EveryBoxHasPortsTest`, `UndrawnTest`),
`integration/test_central_cableplant` (`TheRecordStartsFullTest`, `EveryBoxHasPortsTest`).

**THE FIVE GESTURES.** Each is one action; there are no create-then-attach chains.

1. **Lay a cable.** Right-click → *Trace a cable from here*, or *Lay a cable* in the cable
   list. Click along the street; a click that lands on a pin snaps to it and RECORDS WHICH
   — `RouteEdit.endA`/`endB`, set as they are clicked and never inferred afterwards from
   coordinates, because two boxes racked at one point would be indistinguishable. One
   sheet then asks for a name and a fibre count (a row of chips, not a select — the count
   is read off a drum tag). **An end that landed on open ground becomes a COUPLER**, which
   is what makes "a cable runs between two couplers" true by CONSTRUCTION rather than by a
   rule.
   - **THE SNAP HAS TWO BUDGETS AND THE LARGER WINS** (`SNAP_PX` 24 / `SNAP_M` 8), and one
     budget cost this feature its first two real users. On 2026-08-10 badri_fiber traced
     two cables and abandoned both within minutes: `Syrotech JC1` was written **3.3 m from
     the Gpon_04 OLT** and `Dbc JC1` **2.8 m from the striker splitter**. They were
     starting the cable AT the box; what they got was a junk joint box beside it and a
     cable attached to nothing. Two defects, both silent: the budget was measured from the
     pin's ANCHOR while the operator aims at the DOT ~12 px above it (`MARK_DY_PX` — the
     offset `linkhover` had already measured and nothing else used), and a pixel budget
     shrinks in ground terms as you zoom IN, so at z21 — where you zoom to trace a street
     properly — 24 px is 1.7 m. Pixels are right for a cursor affordance; whether a cable
     ENDS ON A BOX is a fact about the ground. Taking the larger keeps both and can only
     ever widen, so no zoom that snapped before stops snapping.
   - **THE BANNER NAMES WHAT EACH END CAUGHT, WHILE THERE IS STILL TIME TO UNDO.** It used
     to name neither: the first and only mention was the naming sheet, after the geometry
     was drawn and the operator was already in "name it and be done" mode. Plain words
     ("open ground"), never a warning tone — ending mid-street is the ordinary case.
   - **A NEAR MISS IS ASKED ABOUT, NOT GUESSED** (`NEAR_MISS_M` 25). An end on open ground
     with a box within 25 m offers *"Starts 3 m from Gpon_04 — land it there"*. Widening a
     threshold always has a wrong side; noticing does not. Accepting moves the END and
     **never the geometry** — the route stays where it was walked.
2. **Open a closure mid-span** (`cablepath.split`, `POST /api/inventory/cable/split`). The
   segment is cut at the click, a coupler stands at the cut, **every core is spliced
   straight through**, and the joints at each far end move to the half that still reaches
   them. This is what keeps segment-per-span from being a tax: a crew tapping a street does
   not redraw the street. **Every core, not just the recorded ones** — splitting a sheath
   must not change what any fibre does, and a core left discontinuous would silently change
   the answer for every fibre nobody had got round to recording. It is the physical default
   and every one of them is clearable in the tray. Both halves keep the drum's NAME; the
   ends tell them apart. Cutting at an extreme end is refused rather than making a stub.
   - **IT IS OFFERED WHERE THE OPERATOR IS POINTING** (the map's right-click menu,
     `CUT_SLACK_PX`), and named for the JOB rather than for the row it writes. The ISPs
     asked for this and it was ALREADY BUILT — as the cable panel's "Open a coupler" — but
     the polyline is `interactive={false}` (it must stay so, or it swallows the placement
     clicks this map is also for), so reaching it meant knowing to click the cable's name
     chip first. A capability nobody can find is indistinguishable from one that is
     missing. "Coupler" is the row; "closure" is what a crew straps to a pole.
   - **AN UNTRACED CABLE CAN BE OPENED TOO.** The cut needs a line, so with no route the
     honest line is the CHORD between the two recorded ends — the dashed line the map is
     already drawing, and the one that was just pointed at. Refusing made "put a closure
     in this cable" depend on somebody having walked the street first, which is the wrong
     way round: the closure is usually why they went. Both halves stay UNTRACED, so
     neither inherits a surveyed look it has not earned.
3. **Splice** (`components/coupler-tray.tsx`) — **A SPLICE SCHEDULE: ONE ROW PER FIBRE, and
   the destination lives ON THE ROW.** The two-column facing-pages tray is GONE (2026-08-10),
   and the reason is a real ask it could not serve: *core 1 to OLT1, core 2 to OLT2, core 3 to
   a customer*. Facing pages make the destination a property of the PANEL, but a closure's
   terminations fan out to many boxes, so that arrangement could be ENTERED and never
   DISPLAYED — the cores that went elsewhere drew as EMPTY cells, and empty reads as "nothing
   here" when it means "spoken for, elsewhere". The panel hid the work you had just done, on
   the one screen built to show it. Three more followed from the same shape: three
   destinations meant three trips through a dropdown setting a MODE before each click; undo
   only worked while the right destination happened to be on show; and a customer could not
   be picked at all (the list was built from devices, so "this core is that drop" was
   unsayable — though the tail route had always accepted `to_mac`).
   Five rules hold the schedule together:
   - **ONE searchable menu per row**, grouped *Splice into* / *Take into* / *Customer*, because
     an operator knows the NAME of the destination and should not have to know our filing.
     A splice takes a second click for the CORE — auto-picking one would be a capacity claim,
     and "recorded is never occupied" is what this subsystem is built on.
   - **TWO KINDS OF RUN COLLAPSE, and omitting either buries the rows that matter.**
     Straight-through, because nine closures in ten are 1:1 and 24 identical lines are not
     information; and UNRECORDED, because a 96F with four cores in use would otherwise put 92
     rows of "+ join" around the four you came to read. **A CROSSING NEVER COLLAPSES** — core
     3 to core 7 is the one thing at a closure worth reading twice.
   - **A RUN NEVER CROSSES A BUFFER TUBE.** A crew opens one tube at a time, so "1–24 straight
     through" describes a job nobody does in one go.
   - **A COLLAPSED FREE RUN STAYS ACTIONABLE, and getting this wrong made a fresh cable a DEAD
     END** — every core of a new 12F is unrecorded, so the whole schedule folded to one grey
     line with no visible way to join anything. The row NAMES the core it will act on
     (`join core 4`), so one click does the common thing while the chevron still opens the run.
   - **A cable with every core already joined HERE is shown DISABLED, not hidden**
     (`all joined here`). Taking a core out to a box lays a 1F tail that lands at this point
     too, so a closure feeding an 8-PON OLT grows eight one-fibre cables that were being
     offered as splice targets with nothing to give. "Full" and "not a cable here" are
     different answers — the same reason a used core is greyed rather than dropped.
   **A TAIL IS REPORTED AS THE BOX IT REACHES**, never as the 1F sheath in between: storage is
   three rows, but what the operator did was send that core to that OLT, and a schedule
   answering "→ a1 core 4 → HLY-OLT-2, core 1" describes its own bookkeeping back at them.
   **`Splice all through` is still one button** — it SKIPS what is already joined, so pressing
   it after hand-work leaves the hand-work, and the schedule then shows the run BROKEN around
   the core that was taken (5–8, then core 9 free, then 10–12). That readout is the clearest
   argument for the form: the two-column view could not express it at all.
   **The arcs are gone and nothing was lost** — their one job was showing 1:1 across two
   cables, and a collapsed run says that in words AND states the core numbers.
4. **Take a core to a box** — a joint with no second cable. `b_cable_id IS NULL` is the
   TERMINATION, and it is the only way a core is attached to equipment, which is why
   connecting a device needs no route and no table of its own.
   **THE BOX MAY BE SOMEWHERE ELSE, AND THAT HALF WAS UNSAYABLE UNTIL 2026-08-09.** The
   ISPs' sentence was *"at a coupler you join cable to cable, or take a core out to a
   device ON A SINGLE FIBRE"*, and only the case where the device already stands at the
   cable's end was built. Two walls met in the middle: a strand may be joined only where
   its own sheath is opened (`joint_refusal` → `absent`, correct physics and staying), and
   the single fibre that physically reaches the OLT could not be laid because **1 was not
   in `FIBER_COUNTS`**. So the commonest tail in an access network — a pass-through closure
   feeding the OLT beside it — had no route through this record at all, which is exactly
   how it was reported ("I can't figure out how to connect it to an OLT/switch").
   `POST /api/inventory/fibre/tail` → `store.take_core_to_box`, driven from the tray's own
   destination picker. Six things hold:
   - **IT IS A MACRO, NOT A NEW CONCEPT.** It writes the three rows a patient operator
     would write by hand — a 1F cable between the two points, a splice here, a termination
     there — so `trace`, `split_org_cable`, the delete cascade and the tray's own refusals
     all keep working with no knowledge that a shortcut exists. Nothing it can record is
     unrecordable without it. The moment it writes something the manual path cannot, there
     are two models of a tail again.
   - **ONE TRANSACTION, AND THE FIBRE IS CHECKED BEFORE THE CABLE IS LAID.** A refused
     splice that still left its tail behind would put a new line on the map, which an
     operator reasonably reads as the connection having been made.
   - **THE TAIL IS 1F AND UNTRACED, deliberately.** One core out is one strand; nobody
     surveys the two metres from a closure to the rack beside it, and an empty path draws
     the dashed chord — this map's own word for "recorded, not walked". `length_m` stays
     null, because zero would be a measurement. An operator with a real 8F tail lays it by
     hand and splices in the tray, which has always worked.
   - **THE NAME CARRIES THE SOURCE CABLE AND CORE** (`a1 core 4 → HLY-OLT-2`). An 8-PON
     OLT fed off one closure gets eight tails between the same two points; named for the
     points alone all eight are one string and the OLT's picker offers eight identical
     rows. The first cut used the POINT name (`a1 JC1 core 4 → …`) and that is a
     half-fact — a closure is not a cable, so "core 4" had no sheath to be a core of.
   - **THE PICKER IS NEAREST-FIRST WITH THE DISTANCE PRINTED, and refuses nothing.** A tail
     is a real cable appearing on the map, so the box 40 km away must not sit one careless
     click under the box 30 m away — but a long tail is unusual rather than impossible, and
     this record does not block real plant for looking strange. Computed in `map-page`
     (the page holds every pin; a route for it would be a second answer to drift from),
     capped at 12, unplaced boxes excluded, and a box already cabled from here says so
     because the honest action then is to splice onto that cable instead.
   - **The tray OPENS ON THE BIGGEST CABLE**, ties broken by the server's feed hint. It was
     the feed outright until tails existed: light at a closure now arrives up a 1F tail, so
     feed-first opened the tray on a single strand with the 24F trunk — the thing you came to
     work on — hidden behind a dropdown.
   - **Re-picking watches the POINT, not just its cables.** One cable's two ends are two
     points, so walking to the far end of the same sheath leaves the pick perfectly valid
     while the schedule on screen is about the wrong end.
   Tests: `integration/test_central_cableplant:FibreTailTest`, `unit/test_fiber`.

   > A ONE-DAY DESIGN, KEPT ONLY AS A LESSON: on 2026-08-09 the same complaint was answered
   > by putting THE BOX in the two-column tray's right-hand picker, with a dashed socket per
   > strand. It fixed the OLT case and was superseded the next morning by the schedule above,
   > because the second column can hold exactly one destination and a closure's terminations
   > do not. What survives is the diagnosis, twice over: **a mode you can only reach by
   > stumbling into it is not shipped**, and **a panel that cannot DISPLAY an arrangement it
   > can RECORD is worse than one that refuses it**. Do not re-add facing columns.
5. **Trace** (`fiber.trace`, `GET /api/inventory/fibre/trace?cable=&core=`). Walks both
   directions across sheaths and joints. A fork or a loop STOPS the walk at that point and
   returns the unambiguous part plus `fault_at`: drawing a confident line past a fork is how
   a splicer ends up at the wrong closure.

- **THE HALF-COUPLER IS A VIEW, NOT A CONSTRAINT** (his idea, 2026-08-09: *"make coupler as
  two part so that line will always be between two those half coupler parts"*). The instinct
  is right — a cable end is a real thing you point at, and a splice tray genuinely has two
  sides — so the tray is two columns and **the operator picks which cable is on each**. It
  is wrong as a rule because a closure routinely has three to six cable entries (trunk in,
  trunk out, two branches), and cable-to-cable joins happen AT couplers: a hard two-half
  model makes branching a trunk impossible without a device in the middle. So a coupler
  holds N cable ends and the tray shows two at a time.
- **ONE FIBRE JOINS EXACTLY ONE FIBRE, enforced on the WRITE** (`fiber.joint_refusal` →
  `absent` / `self` / `taken`), so an operator finds out while looking at the tray rather
  than as a fault chip later. The route answers **200 with a NAMED refusal**, not a bare
  400: on a splice tray an unexplained rejection is indistinguishable from a broken button.
  What is deliberately NOT refused: the same core number of two DIFFERENT cables (a 12F
  spliced through to a 12F is twelve of those — the old model had to refuse it and keeping
  that would block the commonest closure there is), and a U-turn within one cable (rare,
  buildable, and `trace` reports it if it ever matters).
- **RECORDED IS NEVER OCCUPIED**, the splitter-legs rule verbatim. `cores_recorded` counts a
  core with a joint at EITHER end or a label; counting only joints printed "0 of 12 cores
  recorded" above a plainly recorded core, which is the count-agreement rule broken inside
  one card. The reply carries no `cores_free`/`spare` key at all (pinned).
- **WHERE A CORE GOES IS DERIVED; WHAT IT CARRIES IS TYPED.** `org_cable_cores` survives as
  free text ("BSNL leased line", "village A tower", "reserved") and the core plan renders it
  apart from the joints, so a note can never be mistaken for a finding.
- **TWO REFUSALS ON THE CABLE ITSELF.** Shrinking the count under a core IN USE is refused
  (a joint naming core 19 of a 12F would render a tube and a colour in full confidence);
  clearing the count entirely is a different statement and clears every joint and label with
  it. **MOVING AN END discards the joints made at the end that moved** — a splice is a fact
  about a particular closure — guarded on the end actually CHANGING so a rename is
  idempotent, the same shape as `set_onu_drops` discarding a route only on a real re-home.
- **ORIENTATION IS MEASURED, NEVER STORED** (`cablepath.orient`, mirrored in
  `map/cables.ts`). A cable's vertices are in the order somebody drew them, which says
  nothing about which end the record calls `a`; a stored claim would have to be kept true
  through every retrace. Decided on the TOTAL of the two end stubs, never either alone — a
  pin can easily be nearer the wrong end of a street that doubles back, and deciding each
  end independently is how both stubs get drawn to one vertex with the cable crossing
  itself. An unplaced end abstains.
- **LENGTH is walked segment by segment** (`cablepath.length_m`), because crews order drum by
  the metre and Mercator stretches with latitude. An untraced cable has NO length rather than
  zero — nobody walked it, and zero would be a measurement.
- **The line takes `--map-plant` at FULL chroma**, weight `2 + fiberBoost(cores)`, no status
  tone (a cable has no state; what is broken is the topology drawn over it). An UNTRACED
  cable draws the chord between its two pins, DASHED (`CABLE_DASH`, a wider period than the
  drop's because a dash array is absolute px and a finer period on a wider stroke closes into
  a solid line, i.e. into a claim somebody surveyed it). Tracing a core lights its whole path
  by EMPHASIS — weight and opacity, never hue.
- **THE CABLE SAYS ITS OWN NAME ON THE MAP** (`cables.ts:cableIcon`/`cableLabelPos`,
  2026-08-09, from an ISP walkthrough of a real street). Four violet lines met at a closure
  and NOTHING on any of them said which was the 24F trunk and which the 4F branch —
  identifying one meant clicking a box and reading a list, on a map, where the object you
  are looking at is the LINE. Every other family here already earned a chip (the link's
  rate, the drop's); the cable, which this whole view is now about, had none. Four rules:
  - **The NAME leads, the count follows.** A bare `24F` separates a trunk from a branch but
    not one trunk from another, and segment-per-span means a drum is several cables sharing
    a name — so the name is what makes four lines read as one route. Clamped in CSS (14ch)
    with the full string, the walked length and `coresRecordedLabel` coverage in the title;
    a chip that grew to fit its text would overrun the box the budget reserved for it.
  - **It joins the shared collision budget as a THIRD family** — the documented rule — and
    it forced that budget to stop using ONE box for everything. 78px was a fair overestimate
    for `↓3.7M ↑1.2M` and a large under-estimate for `HALIYA TRUNK 24F` (**measured at
    134px**), so two wide chips 80px apart passed the test and visibly overlapped. A claim
    now carries its own half-width (`CHIP_HALF`) and a pair is judged on the SUM: narrow
    families keep their density, only the wide one spreads. Re-measure on any content change.
  - **The CHIP is the click target, not the polyline.** Clicking it opens the cable panel.
    Making the line interactive would have been the obvious move and is the documented trap —
    an interactive polyline swallows the placement and route-drawing clicks the map is also
    for, which is why every topology line here is `interactive={false}`.
  - **`cableLines` resolves the geometry ONCE** for the render and the budget. On a traced
    street the midpoint of the line as drawn is nowhere near the chord's, and computing it
    twice is exactly how a budget reports itself clear over a visible collision.
- **The Layers control is "Dependency links", NOT "Links with no cable"** — the old name
  went stale the day the segment model landed. It was written when a topology link could
  carry a cable and most did not; a link now carries no plant record BY CONSTRUCTION, so the
  set it hides is not "the ones nobody got to yet" but all of them, always. A control naming
  a state that can no longer occur reads as broken, and this one is the answer to the
  commonest fibre complaint (dashed dependency lines shouting over surveyed cable): switching
  it off leaves a pure plant map. The legend gained the two CABLE rows it never had — traced
  and untraced, the pair a crew must tell apart because one is a route they quote drum
  against and the other is an admitted straight line.
- **A STRAND COLOUR MAY NEVER BE A LINE'S STROKE, AND NEVER TEXT.** TIA-598-D contains red,
  orange, yellow and green — the exact hues reserved for alarms — so a cable painted red
  because it is core 7 is a fabricated outage. It renders as a DOT in a neutral chip and a
  SWATCH in a panel: the identity-chip grammar the two-colour-axes pass settled. The tray's
  CONNECTOR is neutral for the same reason plus one more: two dozen coloured arcs crossing
  one gutter on a 24F is a pattern, not information.
- **PAST 12 FIBRES THE SEQUENCE RESTARTS INSIDE A BUFFER TUBE**, and the tube is what a crew
  opens first. Core 25 of a 48F is the BLUE fibre in the GREEN tube. Stored as plain
  integers, presented through `locate()`/`strandAt()`; the core plan and the tray both draw
  twelve to a row so each row IS a tube, which teaches the arithmetic instead of explaining
  it. `unit/test_fiber:TubeTest`.
- **`FIBER_COUNTS` is CLOSED** (1/2/4/6/8/12/24/48/96) and MIRRORED in TS, like the theme
  allowlist and the map-detail defaults — a browser draws swatches before any request
  resolves and central validates without asking a browser. `SpaAgreementTest` reads the TS
  source and fails on drift, including every hex: a name drift mislabels a colour in words, a
  hex drift draws the wrong swatch beside the right word, and the second is worse.
  **1F is a single-fibre TAIL, not a rounding of "small"** — see gesture 4. Leaving it out is
  what made a closure feeding an OLT unrecordable, so if this list is ever trimmed, 1 is the
  one entry that costs a feature rather than a convenience.
- **A TOPOLOGY LINK CARRIES NO PLANT RECORD.** `link_routes` is CARTOGRAPHY only
  (`label_pos`); a body still naming `cable_id`/`core_no`/`cores` is REFUSED with a message
  naming the new route, and `/cable/run`, `/cable/tap` and `/cable/splice` answer "fibre is
  recorded on the cable itself now — reload the page" rather than 404ing. The SPA deploys the
  instant it is built while central needs a restart, so that pairing is routine and a silent
  404 there reads as a bug. A link with no cable over its pair still draws its dotted chord —
  after the wipe that is every link, which is the honest state and doubles as the to-do list.
- **THE WIPE** (`store._rebuild_fibre_plant`, guarded by an `app_settings` marker). Cables,
  runs, taps, splices, core labels and the span geometry that only existed to draw them are
  DESTROYED; `org_devices` (every pin, ratio, parent, tag), `onu_places` (every customer pin,
  name, number) and `onu_drops` are untouched. A migration could have guessed a cable's ends
  from the runs on it — and a guessed end is a closure a crew drives to, which is the one
  thing this subsystem must never invent. ORDER IS EVERYTHING (`PRAGMA foreign_keys=ON`):
  children, then `link_routes.cable_id`, then `DROP TABLE org_cables`. That dead column still
  PINNED its parent, which is how a migrated cable became undeletable in the field.
  Rehearsed on a copy of prod: 99 devices / 81 placed and 289 customers / 285 placed
  unchanged, 106 drops kept, 58 cables and 57 runs gone.
- **`onu_drops` SURVIVES, and is not a duplicate** (operator's call). A drop says which
  splitter LEG a customer uses — an optical split, which is what capacity and branch-fault
  localization run on; a cable says which glass carries it. Different facts.
- Tests: `unit/test_fiber`, `unit/test_cablepath`,
  `integration/test_central_cableplant` (`CableRecordTest`, `CableRouteTest`,
  `FibreJointTest`, `FibreTraceTest`).
- **Still to do: SHARED RISK.** N cables through one duct is a common-cause group — today a
  cut behind a 12F reads as N unrelated outages and `incidents.py` can even grade it `power`.
  Feeding cable membership in as a hypothesis is the monitoring payoff and was deliberately
  scoped out.

## Config

Every tunable is a field on the frozen `Config` dataclass, read once from `WISP_*` env vars. No
DB settings layer; topology/team/routing/credentials live in the dashboard, not env vars.
**`Config` is shared between edge and central** — grep both `apps/daemon/` and
`src/wisp/central/` before deleting/renaming a field. `db_path` (`WISP_DB`) is not a database —
just where the lock file and supervisor transient files live.

## Ingest auth & enrollment

- Any ONE of three: global bearer (`WISP_CENTRAL_TOKEN`), a self-service per-node token, or
  mTLS. None configured = ingest stays open (trusted network).
- **Node tokens**: registered from Network → Probes; only a SHA-256 hash stored, plaintext shown
  once, rotatable only. A node that HAS a credential is gated on presenting it; identity comes
  FROM the credential, not the envelope. `clean_node_id` validates (it becomes a systemd
  identity and a path segment).
- **mTLS**: `central/pki.py` shells out to `openssl` (admin-CLI one-time op); identity is
  CN-encoded `org_id:node_id` and must match the claimed org/node. Central terminates TLS when
  `WISP_CENTRAL_TLS_CERT/_KEY` are set; `WISP_CENTRAL_CLIENT_CA` turns on CERT_OPTIONAL
  (browsers stay certless). The handshake runs in the request's worker thread (`finish_request`
  override) so one slow handshake can't stall the listener. No CRL — revoking means rotating
  the CA.

## Reliability ("trust the alarm")

- One probe per org/node via an OS advisory lock (exit 3); central's per-outage dedupe
  (`open_outage_if_absent`) is idempotent anyway.
- **A page must not vanish to a blip**: `send_with_retry` — network/timeout/5xx retry with
  backoff, 4xx fails fast.
- The probe loop never dies on one bad cycle (per-cycle try/except; keep new per-cycle work
  inside it). `_gather_pings` swallows per-probe errors but re-raises config/permission
  `RuntimeError` loudly.
- **Fleet watchdog is central's** (`central/watchdog.py`), transition-only, restart-safe. Input
  is `store.node_liveness()`, NOT `SELECT * FROM nodes` (that table remembers every identity
  ever seen); `delete_node_token` purges the heartbeat row too, or a deleted probe pages
  NODE_STALE forever.

## Fleet packaging & self-update

- Two FIRST-INSTALL-only artifacts: `.deb` and the Windows setup exe. Both run the
  **supervisor** (`runtime/supervisor.py`), which owns all agent self-updates (download → verify
  sha256 → swap → health-gate → rollback). The manifest builder skips them — an installer must
  never become an "agent artifact".
- **The supervisor STOPS the agent before `os.replace`** (Windows delete-locks a running image —
  the v0.11.0 live-swap crash), and any mid-apply exception yields FAILED + discards
  `update_request.json` (retry rides the poll cadence, never a tight loop). The health gate needs
  `stable_polls` (3) CONSECUTIVE healthy polls or rollback is unreachable for a crash-looping
  build. Supervisors are NOT in the self-update channel — only an installer re-run updates them.
  Ditto `wisp-tray.exe` (per-user pure-ctypes tray; keep it dependency-free; control via elevated
  `schtasks`, never parse its localized output).
- **The tray's ONLY status truth is status.json** — never gate its menu on `schtasks /Query`: the
  SYSTEM task is unreadable from a non-elevated session and that failure is indistinguishable
  from "not installed" (it kept healthy probes reading "task not installed" for days). It shows
  the AGENT's version from status.json, since its own compiled stamp goes stale the first time
  the agent self-updates.
- **The Windows installer upgrades in place**: `PrepareToInstall` ends the task and taskkills
  tray/supervisor/agent before file copy — a running fleet delete-locks its images and reinstalls
  used to dead-end on "old files exist". Don't remove it.
- **Edge health is on disk**: `status.json` (atomic, best-effort — a full disk must never kill
  the probe loop) + `logs/edge.log` (rotated at task start >5MB). Headless boxes read it via
  `wisp-edge status` (exit 0 healthy / 1 starting-degraded / 2 stale-error). The Windows
  installer WAITS for a fresh `status.json` (exit 10 = unconfirmed) — never move that back to
  fire-and-forget. Re-running with `-Central` rewrites `edge.env.ps1` (write-once made one bad
  install permanently dead). Scheme-less URLs normalize to `https://`.
- **Central is the release mirror; edges never touch GitHub.** `central/releasesync.py` pulls the
  latest release **unauthenticated by default** (`WISP_GITHUB_TOKEN` only for a private repo /
  rate limits — an expired PAT once silently blocked a rollout), verifies each binary's sha256
  against the manifest, caches under `data/releases/<ver>/`, and rewrites URLs to central-relative
  `/download/<ver|latest>/<name>`. GitHub asset downloads 302 to S3 which REJECTS an
  Authorization header — capture Location and re-fetch clean (`_NoRedirect`). **Nothing prunes
  `data/releases/`** — it grows ~174 MB per version forever; prune by hand, keeping the rollback
  floor + current + previous. A `releases` row whose cached dir is gone simply can't be served.
- **Install-artifact names are VERSION-LESS and load-bearing**
  (`wisp-edge-setup-win-amd64.exe`, `wisp-edge-linux-<arch>.deb`) — the install card links
  `${origin}/download/latest/<asset>`.
- **The field-app APK mirror is store-less BY DESIGN** (`sync_app_release`): the separate PUBLIC
  `wisp-field-app` repo's latest `.apk` lands in the FIXED `release_cache_dir/app/` and serves at
  `/download/app/wisp-field.apk` (any non-`latest` dir serves without a store lookup). **Never
  `set_release` an app version** — the store's release table drives edge self-update "latest",
  and an app tag there would roll the fleet. Rides the release-sync timer best-effort; fetched
  UNAUTHENTICATED (the fine-grained release-sync PAT would 403 on the app repo). Gated
  `WISP_APP_RELEASES_REPO`.
- CI signing (`release.yml`): Authenticode per Windows binary, minisign once over `SHA256SUMS`;
  both no-op while secrets are unset. Commit `deploy/minisign.pub` only once a real keypair
  exists. Nothing has run against real keys yet.
- `deploy/wisp-edge.spec` `Analysis` paths use `os.path.dirname(SPECPATH)`, not bare relative
  strings — a loaded `.spec` resolves against its own dir, not cwd.

## Conventions & gotchas

- States: `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`. Import from
  `core/state_machine.py`, don't hardcode.
- Hysteresis: DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2, recovery = 2 healthy. The FSM
  never emits `UNREACHABLE` — that's a topology override after `feed()`. Fast-confirm changes
  when samples arrive, not how many.
- Topology order: parent-before-child (`_topological_order`).
- No automatic cause inference — cause is operator-entered at resolution only.
- Escalation: fresh DOWN pages owner+worker; one `escalations` row (`kind="hourly"`,
  `UNIQUE(outage_id, kind)`) re-broadcasts all-hands every `cfg.escalate_every_min` while open.
  Ack doesn't stop it; recovery does.
- **Assignment is triage, and its page is NARROW** (`outages.assigned_to/_at/_by`, JSON list of
  usernames; `POST /api/outages/assign`, 2026-07-26). Three decisions:
  (1) **OWNER-ONLY** — deciding who goes out is running the org, so it gates on `_can_write`
  while ack stays `can_triage`; the SPA shows Assign as the owner's primary action and keeps
  Acknowledge as an outline (an owner handling it alone shouldn't assign itself). A worker sees
  only Acknowledge.
  (2) **Assigning is an ASK, and it does NOT stamp the ack** (changed 2026-07-26 — it did at
  first). Naming somebody is the owner asking; it is not that person answering, and stamping
  the ack made an untouched outage render "In progress" the instant it was handed over — the
  one claim a NOC screen must not make falsely. Status stays DOWN (its own `assigned` state,
  destructive-toned, "Down · awaiting response") until an assignee ACCEPTS. An explicit ack
  still counts, so assigning on top of one leaves it `in_progress` under that person's name.
  Escalation is untouched either way.
  (3) **It pages EXACTLY the assignees** (`store.named_whatsapp`), NEVER
  `org_alert_recipients` — the point of naming two people is that those two hear about it, and
  "assigned to you" sent to the whole team means nothing. The reply carries `notified`, because
  an assignee with no `whatsapp_number` has been given a job nobody told them about; the send is
  best-effort as always and can't undo a committed assignment.
  **An empty list is REFUSED** (422): there is no "assigned to nobody" state to interpret, so
  re-assigning replaces the set. Usernames are re-resolved against the org's ACTIVE accounts —
  the body's spelling is never trusted. Tests: `integration/test_central_outage_assign`.
- **Accepting is the other half** (`outages.accepted_by/_at`, `POST /api/outages/accept`,
  2026-07-26) — the assignee saying yes, and the ONLY thing that moves an assigned outage to
  `in_progress`. `can_triage`, not `_can_write` (a worker must be able to answer a job it was
  given), on the whitelist as `/api/outages/accept`; the real gate is the store, which refuses
  anyone not NAMED on the outage — a yes from whoever else saw the card would make "who
  accepted" mean nothing. Idempotent (`already`), since the dashboard button and the WhatsApp
  button press the same thing. The first acceptance also stamps acknowledged_at/_by (COALESCE)
  — accepting IS acknowledging, and a worker shouldn't press two buttons.
  **`accepted_by` is a SUBSET of `assigned_to`, and both render**: one yes moves the outage but
  the card still shows who has not replied. Re-assigning KEEPS the acceptances of anyone still
  named (adding a second name must not re-ask somebody who already said yes) and drops the rest
  with the job.
  **The page carries the button**: `assign` tries an interactive [✅ I'm on it] / [📍 On map]
  message per recipient FIRST — a worker at a pole answering from the notification is the whole
  point — and falls back to the `wisp_alert1` template for anyone whose 24h window is shut
  (Meta only permits free-form inside it). One message each either way; `notified` counts both.
  `whatsapp_bot.py` handles `acc:<outage_id>` on the SAME store method, and both paths tell
  whoever assigned it that the answer came in (free-form, template fallback — the assigner's own
  window is usually shut, since they didn't just message us).
  Tests: `integration/test_central_outage_assign`, `integration/test_central_whatsapp`.
- Timestamps: poll/outage are ISO8601 `+00:00`; SQLite `datetime('now')` is space-separated
  naive. `core/analytics._parse` normalizes both — reuse it.
- Schema: `central/store.py`'s `_SCHEMA` + `_ensure_columns` is the only schema.
- **`CentralStore` is split into domain mixins** (`store_orgs/users/fleet/devices/outages/
  snmp/field.py`, helpers in `store_util.py`) composed in `store.py`, which keeps `_SCHEMA`,
  `__init__`, `_connect`/`_scope` and the class attrs. New store methods go in the matching mixin
  file; import `CentralStore` from `wisp.central.store` as before — the mixins are not a public
  API.

## Tests

**`.venv/bin/python -m unittest discover -s tests`** after any logic change — the interpreter
matters. There is no bare `python` on this box, and the system `python3` has no `httpx`, so
running it that way ERRORS the ~12 proxy/edge tests that import it. That looked like a standing
"pre-existing failure" for several sessions; it never was.

Tests inject recording doubles — no real ntfy/central network. Key files:
`unit/test_state_machine`, `test_probers`, `test_snmp`, `test_gpon`, `test_health`,
`test_supervisor`, `test_releasesync`, `test_central_inventory`, `test_central_pki`,
`test_edge_status`; `integration/test_central*`, `test_daemon*`, `test_notifiers`,
`test_single_instance`.

## Removed — don't go looking for these

**The single-box era** (one daemon + local dashboard, one SQLite per edge) is deleted wholesale,
and **git history no longer has it**: history was truncated to the newest 10 commits (2026-07-09,
force-pushed, no backup), so don't offer to restore it and don't cite `git log` before `5a532a7`.
Gone: `apps/dashboard/`, `src/wisp/server/`, `egress/shipper.py`, `src/wisp/database/` +
`migrations/`, the old local-engine drivers in `apps/daemon/main.py`, `POST /ingest` +
`devices`/`rollups`, the curl-script installers, the vanilla-JS dashboard. `core/state_machine.py`,
`core/analytics.py`, `core/baseline.py` are ALIVE — central imports them; grep before deleting
anything in `core/`.

**BOTH self-serve payment gateways.** Razorpay (2026-07-16 — `central/razorpay.py`,
`lib/razorpay.ts`, the HMAC verify path, `razorpay_key_*`; the account was refused, individual/no
GST), then UPIGateway (2026-07-21 — `central/upigateway.py`, `POST /api/billing/order`/`/verify`/
`/upi-return`, the `billing_payments` ledger, `upigateway_key`, `billing.months_to_pay`, the
`make_server(upi=…)` injection, `PayOnlineButton`): the operator wanted a plain GPay-number/QR
flow with a manual "I've paid" ping. Dead `razorpay_*`/`upigateway_key` rows and an orphaned
`billing_payments` table may linger — harmless, don't "clean them up" into a migration.

**The Team page and the whole roster/attendance model** (2026-07-21): `routes/team-page.tsx` and
its nav entry, `teamApi`, the `Worker`/`Attendance*` types, `/api/team*` + `/api/attendance`, the
`org_workers`/`org_attendance` tables and their store methods, `api/common.py:worker_org`,
`store_util._today`/`_recent_days`. It was a credential-less second list of people that nothing
else read — **who works for an org is now just who has a login account**. Regions lost
`worker_count` with it. The two tables are LEFT IN PLACE in upgraded DBs — dropping a table is
irreversible and nothing reads them. Removed with it: the read-only `operator`/`tech` roles, the
`ntfy_topic_tech` channel, and the `hasWebProxy` predicate probe that existed only so a read-only
operator wasn't offered a blank Monitoring section.

**Doc files folded into this one** (2026-07-24): `webplan.md` (web-proxy design — the feature
shipped; its invariants and caveats are the "Device web-UI proxy" section) and
`whatsapp-notifier-plan.md` (a next-session prompt for work now BUILT; the outcome, which deviated
from the plan on two points, is the WhatsApp section). `webplan.md` is in git history; the
WhatsApp plan was untracked and is gone.

**Tags:** five survive (`v0.13.0`–`v0.15.1`); only `v0.14.0`/`v0.15.0`/`v0.15.1` carry a GitHub
Release. `v0.1.x`–`v0.12.1` went with the history they pointed at. **`v0.14.0` is the rollback
floor** — there is no artifact below it, so an edge on an older build can only roll forward.
