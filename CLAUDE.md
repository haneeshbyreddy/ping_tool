# CLAUDE.md

Working notes for Claude Code in this repo — **only** the conventions, invariants, and
gotchas that aren't obvious from the code. For everything else, don't duplicate it here,
read it: `README.md` (what it is, how to run it, the directory layout, the module/layer
map, config, behaviors) and `plan.md` (design rationale, what's done, what's next).

## Architecture at a glance

Central runs the brain, for every tenant. The edge is a thin probe, full stop — there is
exactly one daemon mode (`WISP_CENTRAL_BRAIN=1` + `WISP_CENTRAL_URL`, effectively
mandatory: the daemon has no other path). It fetches its topology from central, probes
with real ICMP under a bounded-concurrency fan-out, and reports raw per-IP samples back.
No local database, dashboard, PIN, or FSM lives on the edge box.

Central owns the FSM, topology-aware suppression, fast-confirm detection, the alerting
ladder, the multi-tenant dashboard, and the fleet's version/rollout state. ISPs log into
central's own dashboard with a per-org account, manage their device topology/team/alert
routing there, and self-service-register the physical nodes they run — one ISP can run
one node or many, each with its own enrollment credential. See "Central management
plane", "Central runs the brain", and "Self-service node enrollment" below for the
detail; `plan.md` for the design rationale and what's left.

310 tests, `python -m unittest discover -s tests`. Verify claims about what's done
against the code, not just this file — a stale doc claiming something is "deferred"
after it's already shipped (or vice versa) is exactly the kind of drift to watch for.

The central server + dashboard are pure stdlib; the edge probe needs a small venv
(`requirements.txt`: `icmplib`/`httpx`) — install into a `.venv`, **never globally**
(system Python is PEP 668-locked) — and the kernel ping group enabled for unprivileged
ICMP (`sysctl net.ipv4.ping_group_range="0 2147483647"`). See `plan.md` for what's not
yet done: real production hosting (needs the operator's provider/region/domain) and the
last mile of fleet-update signing (a real minisign keypair + Windows code-signing cert as
CI secrets, plus a genuine tagged release run tested on real hardware) — not more
engineering that can be done sight-unseen in this repo.

## Removed — don't go looking for these

An earlier, single-tenant version of this tool (one daemon + one local dashboard, no
central server at all) existed and was fully retired; its code was deleted wholesale, not
deprecated or left behind a flag. If you're looking for that old design's detail, it's in
git history, not this file. Gone:

- `apps/dashboard/` (the edge's own local web UI), `src/wisp/server/` (its
  routes/services/auth/watchdog — `LoginThrottle` was the one piece still needed and now
  lives in `central/auth.py`).
- `egress/shipper.py` + `database/outbox.py` (the old store-and-forward outbox — moot
  once the edge ships raw samples instead of finished events), `egress/ports.py` +
  `egress/ack.py`, `core/rollup.py`.
- The `AlertDispatcher` class + `role_topic`/`acknowledge_outage` out of
  `egress/notifiers.py` — that file now holds only the ntfy **channel**
  (`NtfyNotifier`, `send_with_retry`, `build_notifier`), which central's own dispatcher
  imports. The DB-coupled alerting *policy* lives in `central/dispatch.py`'s
  `CentralAlertDispatcher` instead.
- Everything in `apps/daemon/main.py` that used to drive a **local** `MonitorEngine`:
  `run_forever`, `run_cycle`, `_confirm_down`/`_confirm_up`, `_between_cycle_watch`,
  `_persist`, `prune_old_polls`, `snmp_cycle`. What's left is just the probe loop:
  `_gather_pings`, `_gentle_probe_plan`, `run_cycle_central_brain`,
  `run_forever_central_brain`, `_follow_recheck`.
- The entire legacy per-edge SQLite layer: `src/wisp/database/` (`client.py` — WAL conn +
  migration runner) and `migrations/*.sql`. Nothing calls it anymore now that the edge
  keeps no database of its own; central has its own schema (`central/store.py`) built
  independently. Along with it: `core/state_machine.py`'s old DB-glue functions
  (`load_device_meta`/`build_engine`/`apply_events`, distinct from central's own
  same-named functions in `central/engine.py`, which are what's actually called) and
  `ingress/snmp.py`'s `load_snmp_targets` (had zero callers, not even a test). `core/
  state_machine.py`, `core/analytics.py` (now just the shared `_parse`/`_now` timestamp
  helpers), and `core/baseline.py` are still very much alive — central imports them
  directly (`central/engine.py`, `central/dispatch.py`, `central/rollout.py`,
  `central/watchdog.py`, `central/perf.py`) — it was only their DB-glue tail that was
  edge-only dead weight. Grep before deleting anything in `core/` — it's reused more
  widely than a first glance suggests.
- The single-box install path: `deploy/install.sh`, `deploy/install.ps1`,
  `deploy/wisp-monitor.service`. Every edge node — an ISP's first or its fifth — now
  installs the same way, through the frozen-binary fleet path (`deploy/install-edge.sh`
  / `install-edge.ps1` + `wisp-edge.service`, self-updating via the supervisor). There is
  no separate "simple" install mode to choose between anymore.

`runtime/supervisor.py`, `apps/supervisor/main.py`, and the whole staged-rollout /
self-update feature are untouched by any of the above — orthogonal to the
dashboard/FSM/single-box removal.

## Fleet update signing

- **Two install scripts, one per OS.** `deploy/install-edge.sh` (Linux) /
  `deploy/install-edge.ps1` (Windows): frozen binary + the supervisor
  (`runtime/supervisor.py`) owns self-update, staged rollout applies. Both exist and are
  exercised for real on every push via CI's `build` job (unsigned).
- **CI signing (`.github/workflows/release.yml`) is real, not a placeholder, but still
  needs real secrets to produce a signed artifact.** Authenticode signs the Windows
  `.exe`s directly (per-binary, in `build`, needs `WINDOWS_CODESIGN_PFX`/`_PASSWORD`);
  minisign signs the assembled `SHA256SUMS` **once** in `release` (needs `MINISIGN_KEY` —
  generate with `minisign -G -W`, i.e. **no password**, since CI can't answer a
  passphrase prompt). One minisign signature over the checksums manifest covers every
  platform's artifact transitively (each is already sha256-checked against that same
  file) — don't add a per-artifact `.minisig`, that's redundant. Both steps are
  `if: env.<SECRET> != ''` no-ops when unset, so forks/PRs still build unsigned.
- **The minisign PUBLIC key is not a secret — commit it to `deploy/minisign.pub`** once a
  real keypair exists; don't fabricate a placeholder file there (an invalid pubkey would
  make both installers hard-fail signature verification even on a legitimately-unsigned
  release, since the file's mere presence is what triggers the check).
- **Both fleet installers are self-activating on signing, not hard-required.** No
  pubkey/signature published (Linux) or an Authenticode status of `NotSigned` (Windows) is
  a warning + sha256-only fallback — but a signature that IS present and doesn't verify is
  a hard `err`/`Die`. This lets today's unsigned releases keep installing while the
  operator hasn't set up keys, and installers start enforcing automatically the moment
  they do, with no installer-script change needed. Same pattern mTLS enrollment (below)
  and self-service node tokens use for their own auth gates.
- **`deploy/wisp-edge.spec`'s `Analysis` paths are built off `os.path.dirname(SPECPATH)`,
  not bare relative strings — don't revert that.** PyInstaller resolves a *loaded* `.spec`
  file's relative paths against the spec's own directory (`deploy/`), not the cwd
  `pyinstaller` was run from — `Analysis(["apps/daemon/main.py"], pathex=["src"], ...)`
  with bare strings looks in `deploy/apps/...` / `deploy/src` instead of the repo root,
  caught only by pushing to real CI runners with PyInstaller actually installed. The
  inline supervisor build in `release.yml` (`pyinstaller --onefile --name
  wisp-supervisor ... apps/supervisor/main.py`, no `.spec` file) doesn't have this problem
  — CLI-invoked PyInstaller without a spec resolves relative to cwd, which IS the repo
  root in that step.
- **None of this has run against a real signing key or real Windows/Linux hardware** —
  that needs the platform operator's actual minisign keypair + code-signing cert (not
  something to fabricate in a coding session) and a genuine `v*` tag release. The
  multi-arch PyInstaller build itself (unsigned) DOES validate for real on every push via
  the existing `build` job — that part doesn't need secrets to exercise.

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
  returns committed states + `Event`s, no I/O of its own. Central owns building/
  rehydrating it and persisting its events (`central/engine.py`); that separation is what
  makes the FSM unit-testable with no database at all. Don't put DB/network calls in the
  engine itself.
- **`process_cycle(results, ts, subset=None)` has two modes.** `subset=None` is the normal
  full pass (every device + canary/uplink edge + freeze). A `set[int]` runs a
  **confirmation pass**: it advances *only* those FSMs by one more sample (topological
  order preserved so a just-confirmed parent still suppresses its children), skips the
  canary/uplink logic, and returns committed states for the subset only. Central's
  fast-confirm recheck path uses this (see `central/engine.py:run_cycle`'s `subset`
  param). Keep the full-pass path byte-identical (it's the `subset is None` branch) so
  existing behaviour/tests don't move.
- **`probe_plan()` is a reference the edge approximates, not something central calls.**
  Nothing in the runtime path invokes `MonitorEngine.probe_plan()` directly — the edge
  computes its own per-cycle ping counts client-side (`apps/daemon/main.py:
  _gentle_probe_plan`) from the topology `GET /edge/devices` hands it, since it has no
  local engine to ask. `probe_plan()` is unit-tested directly and is the "correct"
  behavior `_gentle_probe_plan` mirrors — including counting a BACKUP parent edge as
  "infra" too (`effective_parents()`), which `_gentle_probe_plan` currently can't do
  since `GET /edge/devices`'s topology reply only carries the primary `parent_device_id`,
  not backup edges. Small, known, low-priority gap — not a regression to chase down
  urgently, since a backup-path device is typically also a primary parent of something
  else already.
- `central/dispatch.py`'s `CentralAlertDispatcher` does network sends OUTSIDE any DB
  transaction, then logs — so a slow API call never holds a write lock.
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
  as phantom loss. See "Engine invariants" above for the backup-parent gap in this
  client-side approximation.
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
  topology size and does **not** currently retune it on a later topology change mid-run — flag
  it if it matters to you. Detection latency floor is `interval × down_consecutive`, though
  fast-confirm usually beats that in practice.

## Central management plane — device inventory, team, settings

- **`org_devices` (central) and `devices` (central) are TWO DIFFERENT TABLES — don't
  conflate them.** `devices` (legacy naming — the table predates the multi-tenant
  redesign) is the edge-ingest global id map: rows exist only for a device an edge has
  actually reported an event/rollup for, keyed by `(tenant_id, node_id, edge_local_id)`.
  `org_devices` is the ISP-managed topology an org builds by hand from the central
  dashboard — it exists **before and independent of** any edge ever connecting, has its
  own autoincrement id space, and is what central-brain mode's engine
  (`central/engine.py`) runs the FSM against. The API reflects the split: `GET
  /api/devices` = the legacy edge-ingest registry (read-only, no CRUD, populated only by
  edges NOT in central-brain mode — there are none of those, so in practice this stays
  empty), `GET/POST /api/inventory*` = the org-managed topology (full CRUD, what the
  dashboard's **Nodes** page uses — note this is a DIFFERENT "Nodes" than the **Edge
  Nodes** page described under "Self-service node enrollment" below; one is device
  topology, the other is physical-probe enrollment credentials). Adding a field that
  belongs to one to the other is the classic mistake here.
- **`central/inventory.py` is pure validation, no storage.** Pure functions
  (`clean_device_payload`, `clean_snmp_payload`, `clean_node_id`) — no DB, unit-tested
  directly (`tests/unit/test_central_inventory.py`). `clean_device_payload`'s `parents`
  map must already be scoped to one tenant by the caller
  (`CentralStore.org_device_parent_map`) — a cross-tenant id is never in the map, so it
  just looks like "parent node does not exist" rather than needing an explicit tenant
  check. `clean_backup_link`'s cycle check walks the FULL edge set (primary + existing
  backups), not just the primary parent chain.
- **Every `org_devices` write in `central/server.py` re-derives the tenant from the DB row,
  not the request body**, via `store.device_tenant(id)`. A body's `tenant_id` is only trusted
  for *create* (where there's no row yet to derive it from); for update/delete/maintenance/snmp
  it would let an authenticated user from org A claim to own org B's device id. `_can_write(user,
  tenant)` still gates on the *derived* tenant. `switch_ports` writes (`/api/inventory/ports/
  monitored`, `/api/inventory/ports/feeds`, `/api/inventory/ports/bandwidth`) follow the
  same pattern via `store.switch_port_tenant(id)`; `feeds` additionally checks the target
  device is in the SAME tenant (`store.device_tenant(feeds) == tenant`) before accepting
  it. `/api/inventory/links` (backup edges) derives tenant from `child_id` the same way and
  additionally checks `parent_id` is in that SAME tenant before accepting the edge.
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
- **New `orgs`/`org_devices`/`switch_ports` columns need the in-code column migration**, not
  just the `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` — that only helps a fresh DB.
  `CentralStore.__init__` runs `_ensure_columns(conn, table, coldefs)` (checks `PRAGMA
  table_info`, `ALTER TABLE ADD COLUMN` for anything missing) right after `executescript`.
  Add any new column there too, or an existing `central.db` silently keeps the old schema.
  A brand-new TABLE (like `org_device_links`/`device_perf_samples`/`node_tokens`) needs no
  such migration — `CREATE TABLE IF NOT EXISTS` alone is enough since there's no existing
  row shape to reconcile.
- **`central/server.py`'s dashboard writes send real pushes (`/api/test-alert`), so
  `make_server`/`_make_handler` take an injectable `notifier`** (defaults to
  `build_notifier(cfg)`, the lazy-httpx-import `NtfyNotifier`) — tests inject a recording
  double, no real network in the suite. Follow this constructor-injection pattern for anything
  else central needs to send.
- **`GET /api/analytics?days=` is outage-history math, not a new store of its own** —
  `central/analytics.py:device_reliability` reads straight off the existing `outages`
  table (`store.outages_in_window`), tenant-scoped through the SAME `_scope_tenant` every
  other dashboard read uses. It reports every ACTIVE device the tenant has configured
  (via `list_org_devices`), not just ones with an outage, so a device with a clean window
  still shows 100% uptime rather than being silently absent. UNREACHABLE outages are
  excluded from the downtime sum — a topology-suppressed child isn't "unreliable" on its
  own account.
- **Tests:** `unit/test_central_inventory` (pure payload/cycle/node-id validation),
  `integration/test_central.OrgDevicesTest` (CRUD round-trip, tenant isolation, parent-map
  scoping, children-block delete, maintenance/SNMP toggles), `integration/test_central_auth`
  (`/api/inventory*` CRUD + 422/403 + cross-tenant-write-rejected over HTTP, `/api/orgs`
  tenant-scoping + superadmin narrow, org role-topic round-trip, `/api/test-alert` via the
  injected recording notifier + missing-topic 422 + write-gated, `/api/analytics` +
  `/api/analytics/trend` tenant scoping + superadmin narrow, `/api/inventory/links*`
  round-trip + cross-tenant-parent-rejected + topology-loop-rejected + write-gated,
  `/api/inventory/ports/bandwidth` round-trip + bad-direction-422), `integration/
  test_central_analytics` (`DeviceReliabilityTest`: window-overlap math, DOWN-only
  excludes UNREACHABLE, a zero-outage device reports 100% up, tenant isolation;
  `DeviceRollupTest`: hour-bucket flooring, same-bucket averaging across cycles, a lost
  sample has no latency but still counts loss/down, different hours land in different
  buckets, prune keeps only recent buckets, tenant isolation).

## Central runs the brain (the only edge mode)

- **`core/state_machine.MonitorEngine` is reused UNCHANGED — only its DB glue is
  central-native.** The FSM itself doesn't know or care whether it's fed by a local
  SQLite or central's multi-tenant one. `central/engine.py`'s
  `load_device_meta`/`build_engine`/`apply_events` build/rehydrate/persist against
  `org_devices`/`device_states`/`outages`. `central/dispatch.py`'s
  `CentralAlertDispatcher` is the alerting policy (dedupe-per-outage, owner+operator on
  open, all-three on the hourly escalation and on resolve, ack-doesn't-stop-
  only-recovery-does).
- **`EngineRegistry` exists because central's HTTP handling is stateless per-request but the
  FSM's flap-suppression counters are NOT.** A device's `down_streak` must accumulate across
  an edge's successive `POST /report` calls, or it could never reach `down_consecutive` — one
  HTTP request only ever feeds the engine ONE sample. `EngineRegistry` (in `central/engine.py`)
  holds one live `MonitorEngine` per tenant in memory. It rebuilds a tenant's engine only when
  that tenant's topology actually changed (a cheap fingerprint over `(id, parent_device_id,
  d.parents)` recomputed every `.get()` — `d.parents` covers backup-link add/remove too, a
  topology change like any other), and a fresh/rebuilt engine rehydrates FSM state from
  `device_states` (restart-safe). One `EngineRegistry` lives per central server process,
  threaded into `_make_handler` alongside the injectable `notifier` — don't build a new one
  per request.
- **The wire format is IP-keyed, not device-id-keyed — no translation needed.**
  `MonitorEngine.process_cycle` already takes `{ip: PingResult}`; a device resolves to its
  `org_devices` row internally via `dev.ip_address`. `POST /report`'s body is
  `{"v":1,"tenant_id":…,"node_id":…,"ts":…,"mode":"full"|"recheck","pings":{"<ip>":{
  "loss_pct":…,"latency_ms":…,"jitter_ms":…}}}` — the edge doesn't need to know or send
  central's device ids at all, it only needs to know which IPs to probe (from
  `GET /edge/devices`, returns that tenant's active `org_devices` topology + `cfg.canary_ip`).
  A `"recheck"` report carries samples for ONLY the suspect IPs named in a
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
- **The daemon has exactly one mode — know this before touching `apps/daemon/main.py`.**
  `main()` unconditionally runs `run_forever_central_brain` behind a `SingleInstance` lock
  (`<db_path>.central-brain.lock` — no schema to migrate, since central-brain mode makes zero
  local DB writes; `db_path` itself is now just a per-node data-directory anchor, not an
  actual database — see "Config" below). It fetches its topology from `GET /edge/devices`
  (re-fetched every cycle, skipped for finite `--cycles` runs — a fetch hiccup keeps the
  last-known set rather than probing nothing), probes with `_gather_pings`/`build_prober`
  (including the gentle-infra cadence via `_gentle_probe_plan`), and `POST /report`s the raw
  per-IP results, following any `recheck` hint via `_follow_recheck`. Central's
  `central/engine.py` + `central/dispatch.py` do 100% of the detecting and paging.
- **SNMP port folding is wired end to end.** The edge walks its snmp-enabled
  `org_devices` (config travels on `GET /edge/devices`'s topology reply —
  `snmp_enabled`/`snmp_community`/`snmp_port`/`snmp_version` — since the edge has no
  local DB of its own to read credentials from) on its OWN slow cadence
  (`cfg.snmp_interval_s`, default 90s, independent of `poll_interval_s` — ports don't flap
  like radio links), via `apps/daemon/main.py:_gather_snmp_ports`, and attaches the haul to
  the SAME "full" `POST /report` under a `ports` key ({device_id: [port dict, ...]}; never a
  recheck report — fast-confirm is ICMP-only). A dead/blocked switch is isolated per-device
  (`try/except` inside `_gather_snmp_ports`) and never sinks the ICMP cycle, same discipline
  as `_gather_pings`. Central's `central/ports.py:CentralPortMonitor` (run from
  `central/server.py:_report`, AFTER the ICMP cycle commits so `open_outage_id` reflects
  this cycle's outages) writes into `CentralStore`'s tenant-scoped `switch_ports` table:
  monitored-only (discovery alone never alarms), admin-down stays silent (reuses
  `PortStatus.is_down()` verbatim — never re-derive that predicate on the central side of
  the wire), and one alarm not two (a monitored port-down folds into the
  `feeds_device_id` device's open outage via `store.stamp_outage_cause` — COALESCE, never
  clobbers an operator's post-mortem — instead of raising a competing alarm; no open
  outage yet = a leading-indicator operator heads-up; SNMP never opens an outage itself,
  ICMP/the FSM still owns that exclusively). A device id in the wire's `ports` key that
  isn't in the reporting tenant's own `eng.meta` is silently ignored
  (`_report:_ingest_ports`) — the same re-derive-tenant-from-what-we-already-know
  discipline as `org_devices` writes, so tenant A can't attribute a port reading to tenant
  B's device. Operator-only page, gated by `cfg.snmp_alerts` (state is always written); no
  escalation ladder of its own. **Bandwidth is wired too.** The same walk's 64-bit octet
  counters (`in_octets`/`out_octets`/`speed_bps`, already parsed by `ingress/snmp.py`, now
  carried on the wire alongside status) get diffed by `central/ports.py`'s
  `throughput_bps` into a live in/out rate; a MONITORED port whose rate falls below its
  operator-assigned per-port threshold (`bw_threshold_mbps`/`bw_direction`, `GET/POST
  /api/inventory/ports/bandwidth`, `clean_port_bandwidth_payload`) for
  `cfg.snmp_bw_consecutive` walks alarms — its OWN streak (`bw_low_streak`/`bw_alarm`),
  separate from the port-down streak, since traffic is burstier than link state. Never
  judged on a down/admin-down port (`bw_eligible` requires `oper_status == "up" and not
  down`) — that alarm already owns the story, so a bw-alarmed port going down clears its
  bw badge SILENTLY (no confusing "bandwidth recovered" chaser). Gated by
  `cfg.snmp_bw_alerts`; the `switch_ports` row (`in_bps`/`out_bps`/`counters_at`) is
  always written.
- **Historical rollups/trend analytics are wired, in two slices.**
  `central/analytics.py:device_reliability` (`GET /api/analytics?days=`) is pure
  outage-history math — no new storage, since central already retains full outage history
  in central-brain mode; it reports every ACTIVE configured device (not just ones with an
  outage) and excludes UNREACHABLE outages from the downtime sum (a topology-suppressed
  artifact of a dead parent, not that device's own fault). `central/rollup.py` is the
  latency/loss TREND chart (`GET /api/analytics/trend?device_id=&days=`): hourly buckets,
  30-day retention (both platform-wide policy, not configurable per org).
  `record_cycle` folds straight off `_report`'s already-computed per-device samples on
  every "full" report (never a recheck — that would badly skew an hour's average with the
  fast-confirm subset's rapid re-probes), as running sums
  (`latency_sum`/`loss_sum`/`down_samples`) rather than storing raw samples — averages are
  computed at READ time. `start_central_rollup_prune_thread` runs a daily sweep, started
  in `central/server.py:serve()` right alongside the fleet watchdog thread.
- **Per-link performance baseline.** `central/perf.py` reuses `core/baseline.py`'s pure
  median+MAD deviation math (`evaluate_perf`) VERBATIM. Central's own contribution is the
  trailing-sample window: `device_perf_samples` is a bounded per-(tenant, device) ring
  buffer (`store.record_perf_sample` inserts then trims to the newest `cfg.perf_window`
  rows in the SAME write), deliberately NOT `central/rollup.py`'s hourly buckets — an
  hourly average would smear out exactly the intra-hour slowdown this tier exists to
  catch; don't conflate the two storages. `record_and_evaluate` runs once per full-report
  cycle (never a recheck), appends this cycle's sample, evaluates the window, and
  persists the badge (`device_perf`, restart-safe — a hard-DOWN device's perf is moot and
  clears its badge SILENTLY, the outage owns that story). Operator-only page on the
  enter/leave edge, gated by `cfg.perf_alerts`.
- **On-backup redundancy needed ZERO engine changes.**
  `core/state_machine.MonitorEngine` already computed `CycleResult.redundancy` generically
  (`DeviceMeta.effective_parents()` combining the primary parent with any BACKUP
  `ParentEdge`s) — the work here was purely wiring the extra edge in: `org_device_links`
  (tenant-scoped, `kind='backup'`), `central/inventory.py:clean_backup_link` (the
  topology-loop check over the FULL edge set — primary + existing backups), and
  `central/engine.py:load_device_meta` populating `DeviceMeta.parents` from
  `store.org_device_backup_edges`. **`EngineRegistry`'s topology fingerprint includes
  `d.parents`, not just `d.parent_device_id`** — a backup-link add/remove is a topology
  change like any other and must trigger a rebuild, same as a reparent.
  `central/redundancy.py:sweep` persists the `device_redundancy` badge every full cycle
  (restart-safe), pages the operator once on the enter/leave edge, and — same invariant
  as every soft-signal tier — NEVER opens an outage or touches the escalation ladder; a
  node that's itself gone hard DOWN clears its badge SILENTLY (the outage owns that
  story, not this). Gated by `cfg.backup_alerts`. Dashboard CRUD:
  `GET/POST /api/inventory/links*`, `GET /api/inventory/redundancy?device_id=`. See
  "Engine invariants" above for the one place this isn't fully threaded through yet (the
  edge's client-side gentle-probe approximation).
- **Every soft-signal tier this platform's edge ever had now exists on central.** SNMP
  port status + bandwidth, outage-history SLA reporting, the latency/loss trend, the
  per-link performance baseline, and on-backup redundancy are all wired. Nothing is
  deliberately deferred at the detection-tier level — see `plan.md` for what's left
  (production hosting, fleet-update signing), which is groundwork, not new tiers.
- **Tests:** `integration/test_central_brain.py` — `CentralEngineTest` (topology mapping
  excludes maintenance, restart rehydration doesn't re-page, `EngineRegistry` streak
  persistence across calls + rebuild-on-topology-change + per-tenant isolation),
  `CentralAlertDispatcherTest` (owner+operator on open, UNREACHABLE suppressed,
  per-outage dedupe, new-outage-after-recovery pages again, resolve broadcasts to all
  three (silent if from UNREACHABLE), hourly escalation fans out + reschedules, ack
  doesn't stop it but recovery does, a missing topic is a soft no-op not a crash),
  `ReportEndpointTest` (`GET /edge/devices` + `POST /report` end-to-end over a real
  socket, bearer-gated, tenant isolation, canary freeze over HTTP, the recheck round trip
  including fast-confirm-within-two-rechecks and a blip clearing the hint without confirming,
  plus SNMP port folding over HTTP: a monitored port-down folds into an open outage's
  `root_cause` and a device id from another tenant's `ports` key is silently ignored;
  plus the trend rollup: a full report folds one bucket, a recheck folds none; plus the
  on-backup redundancy badge driven end to end by 3 down-cycles on the primary while the
  backup + child stay reachable). `integration/test_daemon_central_brain.py` (loads
  `apps/daemon/main.py` by path) — `_gentle_probe_plan` infra-vs-leaf cadence,
  `run_cycle_central_brain` reports every probed IP incl. canary + survives a report
  failure without raising, `run_forever_central_brain` re-fetches topology + reports per
  cycle and aborts loudly (`SystemExit(2)`) if the very first topology fetch fails,
  `_follow_recheck` follows a hint and stops when it's disabled/empty,
  `Config.central_brain_enabled()` requires both flags, `_gather_snmp_ports`/
  `run_cycle_central_brain(snmp_poller=...)` walk only snmp-enabled devices, attach the haul
  to the same full report, and isolate a dead switch's walk failure from the ICMP cycle.
  `integration/test_central_ports.py` (discovery lands unmonitored, flap-suppressed
  monitored-down, a single blip never alarms, admin-down stays silent, fold-into-open-outage
  vs. leading-indicator-no-outage, recovery pages once, the `snmp_alerts` gate mutes the page
  but still writes state, a missing operator topic is a soft no-op; `BandwidthTest`
  (counter-delta rate math, flap-suppressed below-threshold alarm, direction selection,
  recovery, the `snmp_bw_alerts` gate, a bw-alarmed port going down clears silently).
  `integration/test_central_redundancy.py`: a single operator page on enter, one
  recovered notice on leave, a hard-DOWN node clears the badge silently, the
  `backup_alerts` gate, restart doesn't re-page, tenant isolation.
  `integration/test_central_perf.py`: sustained degradation pages once, recovery sends
  one notice, a hard-DOWN device clears the perf badge silently, the `perf_alerts` gate,
  restart doesn't re-page (the window survives in the DB), tenant isolation.

## Reliability invariants (the "trust the alarm" set — don't regress)

- **One logical probe per tenant/node.** Each daemon process holds its own OS advisory lock
  (`runtime/single_instance.py`, `<db_path>.central-brain.lock`) and exits (code 3) if another
  holds it; the kernel frees it on exit/crash. Two probes for the same tenant would just
  double-*report*, which is wasteful and confusing even though central's per-outage dedupe
  (`store.open_outage_if_absent`, `WHERE NOT EXISTS`) makes it harmless. Tests:
  `integration/test_single_instance` (the OS lock itself); the idempotent-open invariant
  is covered directly against `CentralStore` in `integration/test_central.py`'s
  `CentralStoreTest`.
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
- **Cross-edge fleet watchdog is central's job.** `central/watchdog.py`'s
  `CentralWatchdog.check(now)` pages a node's org when its heartbeat is stale (box dead or
  WAN cut), restart-safe and transition-only. There is no edge-side dead-monitor watchdog —
  the edge doesn't run anything that would need one.

## Config (env-var only)

- **Every tunable is a field on the frozen `Config` dataclass** (`config.py`), read once from
  a `WISP_*` env var (or its default) at process start. There is no DB settings layer on
  either side. Change a tunable by exporting the env var and restarting.
- **`Config` is shared between the edge and central processes** — don't assume a field is
  edge-only or central-only just from where you first see it used; grep both `apps/daemon/` and
  `src/wisp/central/` before deleting or renaming a field (`escalate_every_min`,
  `session_timeout_h`, `canary_ip`, and `retry_interval_s` all look edge-only in isolation
  but are read by `central/server.py`/`central/dispatch.py` too).
- **Device topology, team, per-org alert routing, and node enrollment credentials are
  live in central's dashboard, not env vars.** Only process-level tunables (poll cadence,
  retry interval, thresholds, concurrency caps) are `WISP_*` — see `README.md`'s
  Configuration table for the current field list.
- **`db_path` (`WISP_DB`) is not a database anymore — just a per-node data-directory
  anchor.** The edge keeps no database of its own; this path is where the single-instance
  lock file (`apps/daemon/main.py`) and the supervisor's transient download/
  update-request files (`apps/supervisor/main.py`) live.
- **Edge→central ingest auth is any ONE of three:** the global bearer token
  (`WISP_CENTRAL_TOKEN`), a self-service per-node token an ISP issues from its own
  dashboard (see "Self-service node enrollment" below), or mTLS (see "mTLS enrollment"
  below). Plus whatever central's own dashboard session secret is (`central/auth.py`, a
  file under `data/`, 0600). There is no PIN — central uses per-user accounts (see
  "Central runs the brain" / `central/auth.py`).

## Self-service node enrollment

- **An ISP owner/operator registers a node from the dashboard's "Edge Nodes" tab** (not
  the "Nodes" tab — that's device topology, see "Central management plane" above) —
  `POST /api/nodes` issues a fresh credential shown exactly once, `/api/nodes/rotate`
  replaces it, `/api/nodes/revoke` deactivates it. This is the third option alongside the
  platform superadmin running `central.admin enroll-edge` (mTLS) or handing out the one
  shared `WISP_CENTRAL_TOKEN` — an ISP that wants to self-serve doesn't need either.
- **Only a SHA-256 hash of the token is ever stored (`node_tokens` table,
  `central/store.py`), never the plaintext** — same discipline as any API-key UX
  (GitHub PATs, Stripe keys): the plaintext is shown once, at issue time, and can't be
  retrieved again, only rotated. A fast hash is fine here (unlike a user password) since
  the token is already ~256 bits of generated entropy (`secrets.token_urlsafe(32)`), not
  something to defend against a low-entropy-guessing attack.
- **The token rides the EXACT SAME `Authorization: Bearer <token>` header the edge
  already sends** — `HttpCentralClient`/`install-edge.sh`/`install-edge.ps1` needed zero
  changes. Central just checks a presented bearer against three sources instead of one:
  `central/server.py`'s `_ingest_ok(tenant, node)` tries the global token
  (`_token_ok`), then a self-service per-node token (`_node_token_identity` →
  `store.resolve_node_token`, deriving identity FROM the credential and comparing
  against the claimed tenant/node — same discipline as mTLS's `_peer_identity`, never
  trust the envelope's claim alone), then a verified mTLS cert. Any one satisfies it.
- **A node that HAS registered its own credential is gated on presenting it, even on a
  deployment with neither the global token nor mTLS configured.** Without this,
  self-service registration would be decorative on any install that never set up the
  other two mechanisms — `store.node_token_registered(tenant, node)` is checked as a
  hard "credential required" gate before falling back to the open trusted-network
  default (`central/server.py:_ingest_ok`'s last line). An UNREGISTERED node still gets
  that open default, unchanged from before this feature existed.
- **`central/inventory.py:clean_node_id`** validates the id an ISP types in (1–64 chars,
  starts with a letter/digit, otherwise letters/digits/`.`/`_`/`-` only) — deliberately
  boring since it becomes a systemd identity, a path segment under `/etc/wisp` on the
  edge box, and a bare wire value.
- **Tests:** `unit/test_central_inventory` (`clean_node_id`), `integration/test_central.
  NodeTokenTest` (issue/resolve/rotate-invalidates-old/revoke-keeps-row-but-stops-
  resolving/reissue-after-revoke-reactivates/tenant-isolation/heartbeat-join),
  `integration/test_central_node_enrollment.py` (dashboard write-gating — owner/
  superadmin only, 422 on duplicate/bad-id/rotate-of-unregistered, 404 on
  revoke-of-unregistered, tenant-scoped list — and the ingest-auth integration: a freshly
  issued token really does authenticate `/report`/`/edge/devices`, rejects a
  wrong-tenant or wrong-node claim, stops working once revoked, coexists with the global
  token, and — in a deployment with neither the global token nor mTLS configured — an
  unregistered node stays open while a registered one is hard-gated on its own token).

## mTLS enrollment (replaces the bearer-token-only stopgap)

- **`central/pki.py` shells out to `openssl` rather than adding `cryptography` as a
  project dependency.** Cert issuance (`central.admin init-ca`/`enroll-edge`) is a
  one-time admin-CLI operation, not something the server does per-request, so it
  doesn't need to live inside central's pure-stdlib request path — `central/server.py`'s
  actual verification at request time uses only the stdlib `ssl` module. `openssl` needs
  to be on the PATH of whatever box runs the admin CLI (true of essentially every
  Linux/macOS box); `pki.PkiError` gives a clear message if it's missing, rather than a
  raw `FileNotFoundError` from subprocess.
- **Identity is CN-encoded, not a new wire field.** An edge's client cert CommonName is
  `tenant_id:node_id` (`pki.edge_common_name`/`pki.peer_identity`) — central decodes it
  off the verified `ssl.SSLSocket.getpeercert()` at the TCP layer, so `POST /report`'s
  JSON envelope is completely unchanged.
- **The bearer token, a self-service per-node token, or a verified matching cert — any
  one satisfies ingest auth**, none required. `central/server.py`'s `_ingest_ok(tenant,
  node)` checks the global token, then a self-service token, then falls back to
  `_peer_identity()` (cert CN must match the CLAIMED tenant, and node where the route has
  one — `/edge/devices` has no node in its query, so that check is tenant-only there;
  `/ingest`/`/heartbeat`/`/report` check both). If NONE of the three is
  configured/registered, ingest stays fully open — the same trusted-network default from
  before any of this existed. Turning any one of them on is opt-in, never a hard cutover
  that could lock out an unmigrated edge fleet.
- **Central terminates TLS itself when configured — stdlib `ssl`, no new
  dependency.** `make_server` wraps the listener in a `_TLSThreadingHTTPServer` only
  when `WISP_CENTRAL_TLS_CERT`/`_KEY` are BOTH set; if neither is set (the default),
  central serves plain HTTP exactly as it always has. `WISP_CENTRAL_CLIENT_CA` is
  independent of that: it turns on `CERT_OPTIONAL` client-cert verification (requested,
  not required — dashboard browsers and not-yet-enrolled edges have none and still
  connect fine) once TLS itself is on. A terminator (nginx/Caddy) in front is still a
  valid choice too — this doesn't retire that option, it adds a stdlib-only path that
  doesn't need one.
- **The TLS handshake happens inside each request's own worker thread, not the shared
  accept loop.** `_TLSThreadingHTTPServer` overrides `finish_request` (not
  `get_request`) to call `ssl_context.wrap_socket` — `ThreadingMixIn` already calls
  `finish_request` off-thread per connection, so one client's slow/failed handshake
  can't stall new connections arriving on the same port. A handshake failure raises
  there and is caught by `ThreadingMixIn`'s own per-request exception handling, same as
  any other request exception — never takes the server down. `handle_error` is
  overridden to log an `ssl.SSLError` quietly (routine noise on an ingest port exposed
  to the internet — a scanner, a stale client, a rejected cert) but let any OTHER
  exception fall through to the base class's loud default, so a real bug is never
  silently swallowed.
- **`central.admin init-ca --host <name-or-ip>`** creates the CA (idempotent — reruns
  reuse the existing CA) plus central's OWN server cert, with `--host` (repeatable)
  becoming the cert's SAN so edges can verify it with hostname checking ON rather than
  disabling it. Skipping `--host` still works but edges then need to trust the CA
  without SAN-based hostname verification. **`enroll-edge --tenant --node`** issues one
  client cert per edge off that same CA. Both print the exact env vars to set on the
  respective side (`WISP_CENTRAL_TLS_CERT`/`_KEY`/`_CLIENT_CA` for central,
  `WISP_CENTRAL_CLIENT_CERT`/`_KEY`/`WISP_CENTRAL_CA_CERT` for the edge).
- **No CRL or cert rotation tooling yet.** Revoking a compromised edge cert today means
  rotating the CA (`enroll-edge` always reuses the same CA files under
  `WISP_CENTRAL_PKI_DIR` unless you point it at a fresh directory) — acceptable at
  today's fleet size; a real CRL/short-lived-cert-rotation story is future work if that
  becomes an actual operational need, not something to pre-build speculatively.
- **Tests:** `unit/test_central_pki` (CA/cert issuance against real `openssl`, skipped
  if it's not on PATH; `peer_identity`'s CN decode is pure and always runs),
  `integration/test_central_mtls` (a REAL TLS socket via `make_server` wrapped for real
  — a plain-HTTP client can't complete the handshake, a valid cert authenticates
  `/edge/devices` and `/report`, a valid-but-wrong-tenant cert is rejected, a cert
  claiming the wrong node on `/report` is rejected even with the right tenant, no cert
  + no token is 401, `/healthz` stays reachable over HTTPS with no client cert at all,
  and the bearer token still works standalone even with mTLS also configured).

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
- **Escalation model (the alarm ladder):** a fresh DOWN pages **owner +
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
- **Schema changes:** central's own schema (`central/store.py`'s `_SCHEMA` +
  `_ensure_columns`) is the only schema in this codebase now — see "Central management
  plane" above for the column-migration convention new fields need.

## Tests

Run `python -m unittest discover -s tests` after any logic change (310 tests). Layout:
`unit/test_state_machine` (FSM + overrides + `probe_plan` gentle-infra + the subset
confirmation pass + adaptive cadence — the shared engine both the tests and central build on),
`unit/test_baseline` (pure perf-deviation math — now wired by `central/perf.py`; see
"Per-link performance baseline" above), `unit/test_snmp` (pure SNMP parser/throughput math,
incl. `is_down()` — the same predicate `central/ports.py` reuses for the folding alarm
condition), `unit/test_supervisor` (the self-update logic, untouched by anything above),
`unit/test_central_inventory` (pure central payload/cycle/node-id validation, incl.
`clean_backup_link`'s full-edge-set cycle check and `clean_port_bandwidth_payload`),
`unit/test_central_pki` (CA/cert issuance + CN decode — see "mTLS enrollment"),
`integration/test_daemon` (the edge's shared `_gather_pings`: concurrency-bound semaphore,
per-IP count map, the config-error-vs-per-host-error policy), `integration/test_daemon_central_brain`
(the edge probe loop end to end against a recording central client double, incl. the SNMP
task's `_gather_snmp_ports`/dead-switch isolation), `integration/test_central_ports`
(`CentralPortMonitor` against `CentralStore` — monitored-only, admin-down-silent, fold vs.
leading-indicator, recovery, the alerts gate, plus the bandwidth tier's throughput/alarm
math), `integration/test_central_redundancy` (the on-backup sweep against `CentralStore`),
`integration/test_central_perf` (the perf-baseline sweep against `CentralStore`),
`integration/test_central_mtls` (real-TLS-socket mTLS auth — see "mTLS enrollment"),
`integration/test_central_node_enrollment` (self-service token issuance/auth — see
"Self-service node enrollment"), `integration/test_notifiers`
(the `send_with_retry` policy — pure, no DB/network), `integration/test_single_instance` (the OS
lock — see "Reliability invariants" for where the idempotent-open coverage now lives),
`integration/test_central_analytics`
(outage-window/downtime math over `CentralStore`'s tenant-scoped `outages`, feeding `GET
/api/analytics`, plus `central/rollup.py`'s hourly trend-bucket folding/pruning feeding
`GET /api/analytics/trend`), `integration/test_central*` (store, auth, brain, rollout,
watchdog — see "Central runs the brain" / "Central management plane" for what each
covers). Tests inject a recording notifier/client double where a real network call
would otherwise be needed — no real ntfy/central network in the suite.
