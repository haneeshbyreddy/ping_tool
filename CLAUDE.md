# CLAUDE.md

Working notes for Claude Code in this repo — **only** the conventions, invariants, and
gotchas that aren't obvious from the code. For everything else, don't duplicate it here,
read it: `README.md` (what it is, how to run it, the directory layout, the module/layer
map, config, behaviors) and `plan.md` (design rationale, what's done, what's next).

## Status

**The edge is a thin probe, full stop; central runs the brain, for every tenant.** This is
the whole platform now, not an add-on to some other mode — there is exactly one daemon mode
(`WISP_CENTRAL_BRAIN=1` + `WISP_CENTRAL_URL`, and it's effectively mandatory: the daemon has
no other path). An earlier single-box version of this tool (one daemon + one local dashboard,
no multi-tenancy) existed and was fully retired — its local dashboard/server/FSM/outbox were
deleted wholesale, not just deprecated. See "Removed" below before assuming a described
behavior still lives on the edge; if you're looking for that old design's detail, it's in git
history, not this file. 206 tests.

**Central's own management plane and FSM/alerting are done and tested end to end**, including
the fast-confirm round-trip and canary/uplink freeze over a real socket — see "Central
management plane" and "Central runs the brain" below. Verify claims about what's done against
the code, not just this file (an earlier draft of this doc called the fast-confirm round-trip
"deferred" after it had already shipped — that drift is exactly the kind of thing to watch for).

**SNMP port status is now wired end to end, on central** — see "SNMP port folding" below.
Still genuinely missing on central: bandwidth/throughput (the `ifHCIn/OutOctets` +
`ifHighSpeed` fields `ingress/snmp.py` already parses are captured on the wire but not yet
stored/alerted on), the per-link performance baseline, and the on-backup redundancy signal.
The latter two existed on the old single-box edge (deleted, see below) and were never
ported to central — central would need its own trailing-sample storage to reintroduce
them, which doesn't exist yet. See `plan.md` for the full list of what's next and in what
order.

The central server + dashboard are pure stdlib; the edge probe needs a small venv
(`requirements.txt`: `icmplib`/`httpx`) — install into a `.venv`, **never globally** (system
Python is PEP 668-locked) — and the kernel ping group enabled for unprivileged ICMP
(`sysctl net.ipv4.ping_group_range="0 2147483647"`).

## Removed in Phase C (don't go looking for these)

Deleted wholesale, not moved: `apps/dashboard/` (the edge's local web UI), `src/wisp/server/`
(its routes/services/auth/watchdog — `LoginThrottle` was the one piece still needed and now
lives in `central/auth.py`), `egress/shipper.py` + `database/outbox.py` (Phase 10's
store-and-forward outbox — obsolete once the edge ships raw samples instead of finished
events), `egress/ports.py` + `egress/ack.py`, `core/rollup.py`, and the `AlertDispatcher`
class + `role_topic`/`acknowledge_outage` out of `egress/notifiers.py` (that file now holds
only the ntfy **channel** — `NtfyNotifier`, `send_with_retry`, `build_notifier` — which
central's own dispatcher imports; the DB-coupled alerting *policy* is `central/dispatch.py`'s
`CentralAlertDispatcher`). `apps/daemon/main.py` lost `run_forever`, `run_cycle`,
`_confirm_down`/`_confirm_up`, `_between_cycle_watch`, `_persist`, `prune_old_polls`,
`snmp_cycle` — everything that existed to drive a **local** `MonitorEngine`. What's left is
just the probe loop: `_gather_pings`, `_gentle_probe_plan`, `run_cycle_central_brain`,
`run_forever_central_brain`, `_follow_recheck`.

`core/state_machine.py`, `core/analytics.py`, `core/baseline.py`, and `database/client.py`
(+ `migrations/`) are **still in the tree, untouched** — central imports the state machine
and analytics helpers directly (`central/engine.py`, `central/dispatch.py`,
`central/rollout.py`, `central/watchdog.py` all import from them), and `state_machine.py`
itself hard-imports `database/client.py`'s `connect` for its own DB-glue functions
(`build_engine`/`apply_events`/`load_device_meta` — the edge-local versions, still tested by
`unit/test_state_machine.py`, just no longer called by the daemon; central has its own
equivalents in `central/engine.py`). Don't delete these thinking they're edge-only — grep
before removing anything reused this widely.

`runtime/supervisor.py`, `apps/supervisor/main.py`, and the whole staged-rollout / self-update
feature are **untouched** — orthogonal to the FSM/dashboard removal.

## Imports & paths (the main trap)

Src layout, zero-install. What bites:

- Imports are absolute under `wisp.*` (`from wisp.core.state_machine import …`). Don't
  reintroduce flat top-level imports when adding or moving modules.
- Nothing is installed. `apps/daemon/main.py` and `apps/central/main.py` prepend
  `<repo>/src` to `sys.path` themselves; the admin CLI needs `PYTHONPATH=src python -m
  wisp.…`; tests bootstrap their own path.
- `config.PROJECT_ROOT` is the repo root (`parents[2]` of `config.py`); `central_db` defaults
  to `data/central.db`; `central/server.py` resolves the dashboard SPA from
  `central/static/`.

## Engine invariants (don't break)

- `core/state_machine.py` `MonitorEngine` is **pure** — takes `{ip: PingResult}` + ts,
  returns committed states + `Event`s, no I/O. DB glue (`build_engine`, `apply_events`) is
  separate; that's what makes it unit-testable. Don't put DB/network calls in the engine.
  Central reuses this module **unchanged** — the FSM doesn't know or care whether it's fed
  by a single-tenant SQLite or central's multi-tenant one (see "Central runs the brain").
- **`process_cycle(results, ts, subset=None)` has two modes.** `subset=None` is the normal full
  pass (every device + canary/uplink edge + freeze). A `set[int]` runs a **confirmation pass**:
  it advances *only* those FSMs by one more sample (topological order preserved so a just-confirmed
  parent still suppresses its children), skips the canary/uplink logic, and returns committed
  states for the subset only. Central's fast-confirm recheck path uses this (see
  `central/engine.py:run_cycle`'s `subset` param). Keep the full-pass path byte-identical
  (it's the `subset is None` branch) so existing behaviour/tests don't move.
- `central/dispatch.py`'s `CentralAlertDispatcher` does network sends OUTSIDE any DB
  transaction, then logs — so a slow API call never holds a write lock. Same discipline the
  old edge `AlertDispatcher` had before Phase C.
- Prober/Notifier live behind small interfaces (`ingress/probers.py`, `egress/notifiers.py`)
  with one real impl each — `IcmpProber` (unprivileged ICMP via icmplib, needs the ping group) and
  `NtfyNotifier` (ntfy push, needs httpx). `build_prober`/`build_notifier` are the swap point;
  keep any new providers behind those interfaces.

## Scaling invariants (don't regress at fleet size)

- **Probe fan-out is bounded.** `apps/daemon/main.py:_gather_pings` runs probes under an
  `asyncio.Semaphore(cfg.probe_max_inflight)` (`WISP_MAX_INFLIGHT`, default 256). An
  unbounded `gather` would open one ICMP socket *per device per tick* — past `ulimit -n` the
  kernel refuses sockets and the generic-`Exception` guard masks each failure as 100% loss,
  i.e. a **fake mass outage exactly at peak fleet size**. Don't reintroduce an unbounded
  fan-out; raise `ulimit -n` on the box too.
- **Aggregation gear is probed gently.** `apps/daemon/main.py:_gentle_probe_plan` (client-side,
  computed from the central-supplied topology) mirrors `MonitorEngine.probe_plan()`'s rule:
  any device that is a **parent** of another (tower/switch/AP) gets `cfg.pings_per_poll_infra`
  (`WISP_PINGS_PER_POLL_INFRA`, default 2), leaf CPEs + canary get `pings_per_poll` (5). Fewer
  echoes = smaller burst into the box's control plane, so its ICMP rate-limiter doesn't read
  as phantom loss. No backup edges yet (Phase A/B `org_devices` has primary-only parents), so
  this is exactly the primary-chain case of the real `probe_plan`.
- **The fast-confirm round trip is central-driven, not edge-timed.** `central/engine.py`'s
  `compute_recheck` names the suspect IPs (down streak started but not yet confirmed, or a
  recovery streak started but not yet confirmed) in the reply to `POST /report`; the edge's
  `_follow_recheck` re-probes JUST those IPs with a single fast echo every
  `WISP_RETRY_INTERVAL_S` and reports back (`mode="recheck"`) until the hint is empty — central
  keeps naming suspects after every report, full or recheck, so this self-terminates the moment
  a streak commits or resets. This collapses the wall-clock cost of `down_consecutive`/
  `recover_consecutive` samples to a few seconds without weakening the count itself. A frozen
  cycle (canary down + `canary_freeze`) never yields a hint — don't work around a freeze with
  rapid rechecking. See `central/engine.py:compute_recheck`'s docstring for the exact rule.
- **Adaptive cadence is fleet-size-derived, opt-in.** `Config.effective_interval(device_count)`
  returns `poll_interval_small_s` (30) while the active fleet is `<= small_fleet_max` (1000) and
  `poll_interval_adaptive` is on (`WISP_POLL_INTERVAL_ADAPTIVE`), else `poll_interval_s` (60).
  Off by default. `run_forever_central_brain` computes it at startup from central's reported
  topology size and does **not** currently retune it on a later topology change mid-run (that
  refinement wasn't part of the Phase C scope — flag it if it matters to you). Detection latency
  floor is `interval × down_consecutive`, though fast-confirm usually beats that in practice.

## Central management plane — device inventory, team, settings (New Architecture Phase A)

- **`org_devices` (central) and `devices` (central) are TWO DIFFERENT TABLES — don't
  conflate them.** `devices` (Phase 10 Part B legacy naming — the table predates Phase C) is
  the edge-ingest global id map: rows exist only for a device an edge has actually reported
  an event/rollup for, keyed by `(tenant_id, node_id, edge_local_id)`. `org_devices` (Phase A)
  is the ISP-managed topology an org builds by hand from the central dashboard — it exists
  **before and independent of** any edge ever connecting, has its own autoincrement id space,
  and is what central-brain mode's engine (`central/engine.py`) runs the FSM against. The API
  reflects the split: `GET /api/devices` = the legacy edge-ingest registry (read-only, no
  CRUD, populated only by edges NOT in central-brain mode — there are none of those anymore
  post-Phase-C, so in practice this stays empty on a Phase-C-only fleet), `GET/POST
  /api/inventory*` = the org-managed topology (full CRUD, what the dashboard's Nodes page
  uses). Adding a field that belongs to one to the other is the classic mistake here.
- **Phase A has no backup/redundancy links.** `central/inventory.py`'s cycle check walks the
  PRIMARY parent chain only. The edge's old `device_links`-equivalent redundancy concept was
  deleted in Phase C along with the rest of the perf/redundancy soft-signal tier — don't add
  it to central until there's live detection to fail over between two paths.
- **`central/inventory.py` mirrors the edge's OLD validation, not its storage.** Pure functions
  (`clean_device_payload`, `clean_snmp_payload`) — no DB, unit-tested directly
  (`tests/unit/test_central_inventory.py`). `clean_device_payload`'s `parents` map must already
  be scoped to one tenant by the caller (`CentralStore.org_device_parent_map`) — a cross-tenant
  id is never in the map, so it just looks like "parent node does not exist" rather than needing
  an explicit tenant check.
- **Every `org_devices` write in `central/server.py` re-derives the tenant from the DB row,
  not the request body**, via `store.device_tenant(id)`. A body's `tenant_id` is only trusted
  for *create* (where there's no row yet to derive it from); for update/delete/maintenance/snmp
  it would let an authenticated user from org A claim to own org B's device id. `_can_write(user,
  tenant)` still gates on the *derived* tenant. `switch_ports` writes (`/api/inventory/ports/
  monitored`, `/api/inventory/ports/feeds`) follow the same pattern via
  `store.switch_port_tenant(id)`; `feeds` additionally checks the target device is in the SAME
  tenant (`store.device_tenant(feeds) == tenant`) before accepting it.
- **`/api/orgs` must stay tenant-filtered.** Any authenticated org user must only ever get their
  own org's row back — including their own `ntfy_topic*` alert channels, never another
  tenant's. The scoping is the same `tenant` value `_scope_tenant` computes for every other
  route (pinned for an org user, optional `?tenant=` for a superadmin) — don't add a new
  endpoint here that skips that filter.
- **The org's three role topics (`orgs.ntfy_topic_owner/operator/tech`) are separate from
  `orgs.ntfy_topic`** (the fleet-watchdog's `NODE_STALE`/`NODE_OK` page target). One is "is
  this ISP's box alive" (platform-operational, routed by us), the other is "route this ISP's
  own outage pages by role" (customer-configured, read by `central/dispatch.py`). Both live on
  `orgs` but serve different alarms — don't merge them.
- **New `orgs`/`org_devices` columns need the in-code column migration**, not just the `CREATE
  TABLE IF NOT EXISTS` in `_SCHEMA` — that only helps a fresh DB. `CentralStore.__init__` runs
  `_ensure_columns(conn, table, coldefs)` (checks `PRAGMA table_info`, `ALTER TABLE ADD COLUMN`
  for anything missing) right after `executescript`. Add any new column there too, or an
  existing `central.db` silently keeps the old schema.
- **`central/server.py`'s dashboard writes send real pushes (`/api/test-alert`), so
  `make_server`/`_make_handler` take an injectable `notifier`** (defaults to
  `build_notifier(cfg)`, the lazy-httpx-import `NtfyNotifier`) — tests inject a recording
  double, no real network in the suite. Follow this constructor-injection pattern for anything
  else central needs to send.
- **Tests:** `unit/test_central_inventory` (pure payload/cycle validation),
  `integration/test_central.OrgDevicesTest` (CRUD round-trip, tenant isolation, parent-map
  scoping, children-block delete, maintenance/SNMP toggles), `integration/test_central_auth`
  (`/api/inventory*` CRUD + 422/403 + cross-tenant-write-rejected over HTTP, `/api/orgs`
  tenant-scoping + superadmin narrow, org role-topic round-trip, `/api/test-alert` via the
  injected recording notifier + missing-topic 422 + write-gated).

## Central runs the brain (New Architecture Phase B — the ONLY edge mode, post-Phase-C)

- **`core/state_machine.MonitorEngine` is reused UNCHANGED — only its DB glue is new.** The
  FSM itself doesn't know or care whether it's fed by a local SQLite or central's
  multi-tenant one. `central/engine.py`'s `load_device_meta`/`build_engine`/`apply_events` are
  the central-native equivalents of the same-named functions at the bottom of
  `core/state_machine.py`, over `org_devices`/`device_states`/`outages` instead of
  `devices`/`poll_results`/`outages`. Same for `central/dispatch.py`'s `CentralAlertDispatcher`
  vs. the old edge `AlertDispatcher` (deleted in Phase C) — same policy (dedupe-per-outage,
  owner+operator on open, all-three on the hourly escalation and on resolve, ack-doesn't-stop-
  only-recovery-does), different DB layer.
- **`EngineRegistry` exists because central's HTTP handling is stateless per-request but the
  FSM's flap-suppression counters are NOT.** A device's `down_streak` must accumulate across
  an edge's successive `POST /report` calls, or it could never reach `down_consecutive` — one
  HTTP request only ever feeds the engine ONE sample. `EngineRegistry` (in `central/engine.py`)
  holds one live `MonitorEngine` per tenant in memory. It rebuilds a tenant's engine only when
  that tenant's topology actually changed (a cheap `(id, parent_device_id)` fingerprint
  recomputed every `.get()`), and a fresh/rebuilt engine rehydrates FSM state from
  `device_states` (restart-safe). One `EngineRegistry` lives per central server process,
  threaded into `_make_handler` alongside the injectable `notifier` — don't build a new one
  per request.
- **The wire format is IP-keyed, not device-id-keyed — no translation needed.**
  `MonitorEngine.process_cycle` already takes `{ip: PingResult}`; a device resolves to its
  `org_devices` row internally via `dev.ip_address`. `POST /report`'s body is
  `{"v":1,"tenant_id":…,"node_id":…,"ts":…,"mode":"full"|"recheck","pings":{"<ip>":{
  "loss_pct":…,"latency_ms":…,"jitter_ms":…}}}` — the edge doesn't need to know or send
  central's device ids at all, it only needs to know which IPs to probe (from
  `GET /edge/devices`, bearer-authed, returns that tenant's active `org_devices` topology +
  `cfg.canary_ip`). A `"recheck"` report carries samples for ONLY the suspect IPs named in a
  prior reply, and a reply to it may carry ANOTHER `recheck` hint — the edge's
  `_follow_recheck` just keeps following it until empty (round cap as a safety net against a
  central-side bug wedging the probe loop, not the normal termination path).
- **Escalation sweeping rides the edge's own report cadence, not a background timer.**
  `CentralAlertDispatcher.sweep(ts)` is called once per full `/report` (not per recheck —
  recheck reports skip it, since escalation timing is due-at-gated and idempotent, and a
  recheck fires every few seconds — no need to re-check due escalations that often), scoped
  to just that tenant's due `escalations` rows. In practice this is fine (an edge reports every
  `poll_interval_s`, far more often than the hourly ladder needs), but it does mean escalations
  for a tenant silently stop advancing if that tenant's edge goes fully stale — accepted since
  the fleet watchdog (`central/watchdog.py`) already separately pages the org when a node's
  heartbeat goes stale; that's a different alarm for a different failure.
- **The daemon has exactly one mode now — know this before touching `apps/daemon/main.py`.**
  `main()` unconditionally runs `run_forever_central_brain` behind a `SingleInstance` lock
  (`<db_path>.central-brain.lock` — no schema to migrate, since central-brain mode makes zero
  local DB writes). It fetches its topology from `GET /edge/devices` (re-fetched every cycle,
  skipped for finite `--cycles` runs — a fetch hiccup keeps the last-known set rather than
  probing nothing), probes with `_gather_pings`/`build_prober` (including the gentle-infra
  cadence via `_gentle_probe_plan`), and `POST /report`s the raw per-IP results, following any
  `recheck` hint via `_follow_recheck`. Central's `central/engine.py` + `central/dispatch.py`
  do 100% of the detecting and paging.
- **SNMP port folding is wired end to end (plan.md item 1, no longer deferred).** The edge
  walks its snmp-enabled `org_devices` (config now travels on `GET /edge/devices`'s topology
  reply — `snmp_enabled`/`snmp_community`/`snmp_port`/`snmp_version` — since the edge has no
  local DB of its own to read credentials from) on its OWN slow cadence
  (`cfg.snmp_interval_s`, default 90s, independent of `poll_interval_s` — ports don't flap
  like radio links), via `apps/daemon/main.py:_gather_snmp_ports`, and attaches the haul to
  the SAME "full" `POST /report` under a `ports` key ({device_id: [port dict, ...]}; never a
  recheck report — fast-confirm is ICMP-only). A dead/blocked switch is isolated per-device
  (`try/except` inside `_gather_snmp_ports`) and never sinks the ICMP cycle, same discipline
  as `_gather_pings`. Central's `central/ports.py:CentralPortMonitor` (run from
  `central/server.py:_report`, AFTER the ICMP cycle commits so `open_outage_id` reflects
  this cycle's outages) is the direct port of the old single-box `egress/ports.py` onto
  `CentralStore`'s tenant-scoped `switch_ports` table: monitored-only (discovery alone never
  alarms), admin-down stays silent (reuses `PortStatus.is_down()` verbatim — never re-derive
  that predicate on the central side of the wire), and one alarm not two (a monitored
  port-down folds into the `feeds_device_id` device's open outage via
  `store.stamp_outage_cause` — COALESCE, never clobbers an operator's post-mortem — instead
  of raising a competing alarm; no open outage yet = a leading-indicator operator heads-up;
  SNMP never opens an outage itself, ICMP/the FSM still owns that exclusively). A device id
  in the wire's `ports` key that isn't in the reporting tenant's own `eng.meta` is silently
  ignored (`_report:_ingest_ports`) — the same re-derive-tenant-from-what-we-already-know
  discipline as `org_devices` writes, so tenant A can't attribute a port reading to tenant
  B's device. Operator-only page, gated by `cfg.snmp_alerts` (state is always written); no
  escalation ladder of its own. Bandwidth (`in_octets`/`out_octets`/`speed_bps`) is parsed by
  `ingress/snmp.py` but not yet carried on the wire or stored centrally — still a follow-up.
- **Deliberately deferred, not forgotten:** SNMP bandwidth/throughput, the per-link perf
  baseline and on-backup redundancy soft-signal tiers (need trailing sample history central
  doesn't store), and the hourly rollup/prune sweeps (nothing local to roll up or prune in
  this mode — central doesn't fold `device_states` into trend rollups yet either). Don't be
  surprised these don't fire; they're follow-up work, not bugs.
- **Tests:** `integration/test_central_brain.py` — `CentralEngineTest` (topology mapping
  excludes maintenance, restart rehydration doesn't re-page, `EngineRegistry` streak
  persistence across calls + rebuild-on-topology-change + per-tenant isolation),
  `CentralAlertDispatcherTest` (mirrors the old edge notifier tests: owner+operator on open,
  UNREACHABLE suppressed, per-outage dedupe, new-outage-after-recovery pages again, resolve
  broadcasts to all three (silent if from UNREACHABLE), hourly escalation fans out +
  reschedules, ack doesn't stop it but recovery does, a missing topic is a soft no-op not a
  crash), `ReportEndpointTest` (`GET /edge/devices` + `POST /report` end-to-end over a real
  socket, bearer-gated, tenant isolation, canary freeze over HTTP, the recheck round trip
  including fast-confirm-within-two-rechecks and a blip clearing the hint without confirming,
  plus SNMP port folding over HTTP: a monitored port-down folds into an open outage's
  `root_cause` and a device id from another tenant's `ports` key is silently ignored).
  `integration/test_daemon_central_brain.py` (loads `apps/daemon/main.py` by path) —
  `_gentle_probe_plan` infra-vs-leaf cadence, `run_cycle_central_brain` reports every probed IP
  incl. canary + survives a report failure without raising, `run_forever_central_brain`
  re-fetches topology + reports per cycle and aborts loudly (`SystemExit(2)`) if the very first
  topology fetch fails, `_follow_recheck` follows a hint and stops when it's disabled/empty,
  `Config.central_brain_enabled()` requires both flags, `_gather_snmp_ports`/
  `run_cycle_central_brain(snmp_poller=...)` walk only snmp-enabled devices, attach the haul
  to the same full report, and isolate a dead switch's walk failure from the ICMP cycle.
  `integration/test_central_ports.py` (mirrors the old single-box `test_ports.py` against
  `CentralPortMonitor`/`CentralStore`): discovery lands unmonitored, flap-suppressed
  monitored-down, a single blip never alarms, admin-down stays silent, fold-into-open-outage
  vs. leading-indicator-no-outage, recovery pages once, the `snmp_alerts` gate mutes the page
  but still writes state, a missing operator topic is a soft no-op.

## Reliability invariants (the "trust the alarm" set — don't regress)

- **One logical probe per tenant/node.** Each daemon process holds its own OS advisory lock
  (`runtime/single_instance.py`, `<db_path>.central-brain.lock`) and exits (code 3) if another
  holds it; the kernel frees it on exit/crash. Two probes for the same tenant would just
  double-*report*, which is wasteful and confusing even though central's per-outage dedupe
  makes it harmless. Tests: `integration/test_single_instance` (the OS lock itself + idempotent
  `OutageOpened` in `apply_events`, exercised against the shared `state_machine.py`/
  `database/client.py` glue that both the old edge and central's engine module build on).
- **A page must not vanish to a blip.** `NtfyNotifier.send` retries via the pure
  `send_with_retry(attempt, attempts, backoff, sleep)` helper: network/timeout/5xx are
  **retryable** (exponential backoff), a **4xx fails fast** (bad topic/config won't self-heal).
  Tunable via `WISP_NTFY_RETRIES` / `WISP_NTFY_RETRY_BACKOFF_S`. Test the helper directly with a
  fake `attempt`/`sleep` (no httpx) — don't reintroduce a network-touching notifier test.
- **The probe loop never dies on one bad cycle.** `apps/daemon/main.py:run_forever_central_brain`
  wraps each cycle's `run_cycle_central_brain` in try/except that **logs and continues** — a
  transient network error, a probe library blowing up, or a bug skips that cycle, it does not
  kill the probe. Keep any new per-cycle work inside that guard. `_gather_pings` separately
  swallows per-probe errors (but re-raises a `RuntimeError` config/permission failure loudly —
  see its docstring).
- **Cross-edge fleet watchdog is central's job now (`central/watchdog.py`).** There is no
  edge-side dead-monitor watchdog anymore (the old `server/watchdog.py` was deleted with the
  rest of `server/` in Phase C) — central's `CentralWatchdog.check(now)` pages a node's org
  when its heartbeat is stale (box dead or WAN cut), restart-safe and transition-only. See
  "Central management plane" area of `central/`.

## Config (env-var only)

- **Every tunable is a field on the frozen `Config` dataclass** (`config.py`), read once from
  a `WISP_*` env var (or its default) at process start. There is no DB settings layer on
  either side. Change a tunable by exporting the env var and restarting.
- **`Config` is shared between the edge and central processes** — don't assume a field is
  edge-only or central-only just from where you first see it used; grep both `apps/daemon/` and
  `src/wisp/central/` before deleting or renaming a field. (This bit the Phase C config trim:
  `escalate_every_min`, `session_timeout_h`, `canary_ip`, and `retry_interval_s` all looked
  edge-only in isolation but are read by `central/server.py`/`central/dispatch.py` too.)
- **Device topology, team, and per-org alert routing are live in central's dashboard, not env
  vars.** Only process-level tunables (poll cadence, retry interval, thresholds, concurrency
  caps) are `WISP_*` — see `README.md`'s Configuration table for the current field list.
- **The only secret is `WISP_CENTRAL_TOKEN`** (edge→central bearer auth) plus whatever central's
  own dashboard session secret is (`central/auth.py`, a file under `data/`, 0600). There is no
  PIN anymore — that was the edge dashboard's auth model, deleted in Phase C; central uses
  per-user accounts (see "Central runs the brain" / `central/auth.py`).

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2,
  recovery = 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override applied
  in `MonitorEngine.process_cycle` after `feed()`. Don't regress these counts. (The fast-confirm
  round trip gathers DOWN's 3 samples in seconds via rapid re-probe — see "Scaling invariants" —
  but the *count* is the same; it changes when the samples arrive, not how many.)
- **Topology order:** devices are processed parent-before-child (`_topological_order`) so a
  parent's new state is known when evaluating its children.
- **No automatic cause inference.** The engine does **not** guess why a device is down. Cause
  is only ever an operator-entered post-mortem at resolution (a central dashboard field) — don't
  reintroduce an inferred cause.
- **Escalation model (the alarm ladder), now on central:** a fresh DOWN pages **owner +
  operator**, immediately (`CentralAlertDispatcher._on_open` → `_publish("owner", …)` → owner
  topic + the operator copy; the tech channel is held back to the hourly escalation). Dedupe is
  **per-outage** (was there already a `sent` row for this outage id?), NOT a time window — a
  device that recovers and fails again is a new outage and pages again. It also queues **one**
  `escalations` row of kind `"hourly"` due at `now + cfg.escalate_every_min` (default 60, env
  `WISP_ESCALATE_EVERY_MIN`). Each `sweep` that finds it due while the outage is **still open**
  fires an **all-hands broadcast** (owner + operator + tech) stating the running duration and
  who acked it (if anyone), then **reschedules the same row** (doesn't mark it executed).
  **Acknowledgement does NOT stop this loop; only recovery does.**
- **Escalations are DB-derived** (`escalations.due_at` + sweeper), not in-memory timers, so
  restarts don't drop them. `UNIQUE(outage_id, kind)` keeps them idempotent.
- **Restart safety:** `central/engine.py`'s `build_engine`/`EngineRegistry` rehydrate each FSM
  from `device_states`; breaking that re-pages everyone on restart.
- **Timestamps:** poll/outage stamps are ISO8601 `+00:00`; SQLite `datetime('now')` (acks) is
  space-separated naive. `core/analytics._parse` normalises both to naive UTC — reuse it (it's
  also what `central/rollout.py` and `central/watchdog.py` import for the same reason).
- **Schema changes:** central's own schema (`central/store.py`'s `_SCHEMA` + `_ensure_columns`)
  is separate from the legacy `migrations/000N_*.sql` runner (`database/client.py`), which
  still exists only because `core/state_machine.py`'s DB-glue functions are built against it
  and are still unit-tested. Don't confuse the two schemas.

## Tests

Run `python -m unittest discover -s tests` after any logic change (206 tests). Layout:
`unit/test_state_machine` (FSM + overrides + `probe_plan` gentle-infra + the subset
confirmation pass + adaptive cadence — the shared engine both the tests and central build on),
`unit/test_baseline` (pure perf-deviation math — module kept, not currently wired into anything
that calls it; see Status), `unit/test_snmp` (pure SNMP parser/throughput math, incl.
`is_down()` — the same predicate `central/ports.py` reuses for the folding alarm condition),
`unit/test_supervisor` (Part D, untouched by
Phase C), `unit/test_central_inventory` (pure central payload/cycle validation),
`integration/test_daemon` (the edge's shared `_gather_pings`: concurrency-bound semaphore,
per-IP count map, the config-error-vs-per-host-error policy), `integration/test_daemon_central_brain`
(the edge probe loop end to end against a recording central client double, incl. the SNMP
task's `_gather_snmp_ports`/dead-switch isolation), `integration/test_central_ports`
(`CentralPortMonitor` against `CentralStore` — monitored-only, admin-down-silent, fold vs.
leading-indicator, recovery, the alerts gate), `integration/test_notifiers`
(the `send_with_retry` policy — pure, no DB/network), `integration/test_single_instance` (the OS
lock + idempotent `OutageOpened`), `integration/test_analytics` (outage-window/downtime math,
shared by central), `integration/test_central*` (store, auth, brain, rollout, watchdog — see
"Central runs the brain" / "Central management plane" for what each covers). Tests inject a
recording notifier/client double where a real network call would otherwise be needed — no real
ntfy/central network in the suite.
