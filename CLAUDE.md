# CLAUDE.md

Working notes for Claude Code in this repo — **only** the conventions, invariants, and
gotchas that aren't obvious from the code. For everything else, don't duplicate it here,
read it: `README.md` (what it is, how to run it, the directory layout, the module/layer
map, config, behaviors) and `plan.md` (design rationale, the remaining Phase 7 go-live
work, and open questions).

## Status

Production build: Phases 1–6 (engine, FSM, alerting, BI, dashboard), **Phase 8**
(team directory, PIN gate, monitor lifecycle), and **Phase 9** — Part A (graph topology /
backup lines + the on-backup signal) and Part B (SNMP port status) — 161 tests. The
daemon now also needs `pysnmp` (lazy-imported; in `requirements.txt`) for the SNMP
ingress. Config is env-var only (no
in-UI control plane); see "Config" below. The mock/simulated
dev path has been removed: the daemon now uses the **real** `IcmpProber` + `NtfyNotifier`
only. **The dashboard + tests are still pure stdlib**, but the daemon needs the venv
(`requirements.txt`: `icmplib`/`httpx`) — install into a `.venv`, **never globally** (system
Python is PEP 668-locked) — and the kernel ping group enabled for unprivileged ICMP
(`sysctl net.ipv4.ping_group_range="0 2147483647"`). There is no demo
seeder anymore; populate real devices/team from the dashboard.

## Imports & paths (the main trap)

Src layout, zero-install (see README "Layout" for the tree). What bites:

- Imports are absolute under `wisp.*` (`from wisp.core.state_machine import …`). Don't
  reintroduce flat top-level imports when adding or moving modules.
- Nothing is installed. The two `apps/*/main.py` entry points prepend `<repo>/src` to
  `sys.path` themselves; admin CLIs need `PYTHONPATH=src python -m wisp.…`; tests bootstrap
  their own path (and `tests/conftest.py` does it for pytest).
- `config.PROJECT_ROOT` is the repo root (`parents[2]` of `config.py`); `db_path` defaults
  to `data/wisp.db`; `routes.py` resolves UI assets from `apps/dashboard/{templates,static}`.

## Engine invariants (don't break)

- `core/state_machine.py` `MonitorEngine` is **pure** — takes `{ip: PingResult}` + ts,
  returns committed states + `Event`s, no I/O. DB glue (`build_engine`, `apply_events`) is
  separate; that's what makes it unit-testable. Don't put DB/network calls in the engine.
- **`process_cycle(results, ts, subset=None)` has two modes.** `subset=None` is the normal full
  pass (every device + canary/uplink edge + freeze). A `set[int]` runs a **confirmation pass**:
  it advances *only* those FSMs by one more sample (topological order preserved so a just-confirmed
  parent still suppresses its children), skips the canary/uplink logic, and returns committed
  states for the subset only. The daemon's fast-retry uses it; keep the full-pass path
  byte-identical (it's the `subset is None` branch) so existing behaviour/tests don't move.
- `egress/notifiers.py` `AlertDispatcher` does network sends OUTSIDE any DB transaction, then
  logs — so a slow API call never holds a write lock.
- Prober/Notifier live behind small interfaces (`ingress/probers.py`, `egress/notifiers.py`)
  with one real impl each — `IcmpProber` (unprivileged ICMP via icmplib, needs the ping group) and
  `NtfyNotifier` (ntfy push, needs httpx). `build_prober`/`build_notifier` are the swap point;
  keep any new providers behind those interfaces.

## Scaling invariants (don't regress at fleet size)

- **Probe fan-out is bounded.** `apps/daemon/main.py:_gather_pings` runs probes under an
  `asyncio.Semaphore(cfg.probe_max_inflight)` (`WISP_MAX_INFLIGHT`, default 256). The old
  unbounded `gather` opened one ICMP socket *per device per tick* — past `ulimit -n` the kernel
  refuses sockets and the generic-`Exception` guard masks each failure as 100% loss, i.e. a
  **fake mass outage exactly at peak fleet size**. Don't reintroduce an unbounded fan-out; raise
  `ulimit -n` on the box too.
- **Aggregation gear is probed gently.** `MonitorEngine.probe_plan()` returns a per-IP ping count:
  any device that is a **parent** of another (tower/switch/AP) gets `cfg.pings_per_poll_infra`
  (`WISP_PINGS_PER_POLL_INFRA`, default 2), leaf CPEs + canary get `pings_per_poll` (5). Fewer
  echoes = smaller burst into the box's control plane, so its ICMP rate-limiter doesn't read as
  phantom loss. It's topology-derived (no schema/UI), keys match `required_ips()`, and `_gather_pings`
  takes either a uniform int or this per-IP map. DOWN detection is unchanged (still 3 consecutive
  100%-loss polls — "all-of-N lost").
- **Raw polls are scratch; rollups are the trend record.** `core/rollup.roll_up` folds
  `poll_results` into one `poll_rollups` row per device per hour (latency min/avg/max, mean loss,
  per-state poll counts), run hourly from the daemon loop (guarded like the prune, skipped for
  finite `--cycles`). It's a single `GROUP BY` (never pulls raw rows into Python), idempotent via a
  `MAX(bucket)` watermark + `INSERT OR IGNORE` on the `(device_id, bucket)` PK, and never rolls the
  in-progress hour. `services.device_trend` reads it for charts. `outages` stays the incident
  source of truth; this tier is analytics-only, so raw retention can be cut short without losing
  history.
- **Fast-confirm + between-cycle watch together close the full detection gap.**
  *Fast-confirm* (`_confirm_down`) fires after the full poll: any device at 100% loss but not yet
  in `DOWN_FAMILY` is re-probed every `retry_interval_s` until it hits `down_consecutive`
  all-lost samples (→ DOWN, paged) or recovers (blip, no page). This collapses samples 2–3 from
  one poll interval each to `retry_interval_s` each. *But* sample 1 still comes from the regular
  poll, so a device that goes down **after** the poll still waits up to `poll_interval_s` before
  any sample lands.
  *Between-cycle watch* (`_between_cycle_watch`) closes that remaining gap: during the inter-poll
  sleep, `run_forever` probes the **whole fleet** with a **single echo** every `retry_interval_s`
  and fast-confirms whichever direction flipped. End-to-end detection from when a device changes
  is then ≤ `retry_interval_s × max(down_consecutive, recover_consecutive)` ≈ seconds, regardless
  of where in the poll cycle it happens. Gated on `retry_interval_s > 0` and disabled for finite
  `--cycles` runs. Both the detection probe AND the rapid confirmation probes use a **single echo**
  (the consecutive-sample COUNT is the hysteresis, not pings-per-sample) so a dead host burns one
  ICMP timeout per sample, not the plan's 5 — without this, DOWN re-confirms were dominated by
  timeouts and ran ~3–4× slower than recovery. Only the regular full poll uses the per-IP plan (it
  needs the multi-ping loss% for DEGRADED). Canary guard: if the canary reads 100% loss and
  `canary_freeze` is on, the tick is skipped (same policy as fast-confirm).
- **Detection is symmetric — down AND up are both fast.** `_confirm_down` (soft→hard) and
  `_confirm_up` (hard→recovery) are mirror images: each re-probes only the transitioning subset
  every `retry_interval_s` until the FSM commits (`down_consecutive` all-lost → DOWN, or
  `recover_consecutive` non-lost → leaves `DOWN_FAMILY` → OutageResolved), or the condition
  reverses (a blip/flap that never pages or never falsely recovers). `run_cycle` runs both after
  the full poll; `_between_cycle_watch` runs both mid-gap (it probes DOWN devices too, so recovery
  isn't stuck waiting for the next full poll — that was the old asymmetry). Both mutate
  `states`/`results` in place so the persisted row reflects the final probe. Don't drop DOWN
  devices from the between-cycle probe set or recovery goes back to `recover_consecutive × poll`.
- **Adaptive cadence is fleet-size-derived, opt-in.** `Config.effective_interval(device_count)`
  returns `poll_interval_small_s` (30) while the active fleet is `<= small_fleet_max` (1000) and
  `poll_interval_adaptive` is on (`WISP_POLL_INTERVAL_ADAPTIVE`), else `poll_interval_s` (60).
  Off by default — existing deployments are unchanged. The daemon computes it at startup and
  **recomputes on device-set reload** (so crossing 1k retunes in-process), but a CLI `--interval`
  always wins. Detection latency is `interval × down_consecutive`. Note `stale_threshold_s()`
  still keys off `poll_interval_s` (the conservative ceiling), not the small cadence — fine, it's
  a forgiving floor.

## Per-link performance tier (soft "slow link" signal — separate from outages)

- **A link slow/jittery vs its OWN baseline, while still pinging "up".** The FSM only
  knows absolute thresholds; `core/baseline.evaluate_perf` (pure: trailing samples +
  prior flag → verdict) flags a *sustained* deviation via median+MAD with symmetric
  hysteresis (`perf_consecutive` deviating to enter, all-clean to leave). Glue is
  `AlertDispatcher.perf_sweep` (reads the trailing `perf_window` of `poll_results`,
  rehydrates `was_degraded` from `device_perf` so a restart never re-pages, upserts the
  badge, and on an **edge** pages the **operator only** once). The daemon calls it once
  per cycle after `dispatch`/`sweep`, isolated in its own try/except. A hard-DOWN device
  clears its badge **silently** (the outage owns it). `WISP_PERF_ALERTS=0` keeps the
  badge but mutes the page. Jitter comes from icmplib (`PingResult.jitter_ms`, needs ≥2
  echoes — meaningful only on the multi-echo poll plan, ~0 on the 1-ping between-cycle
  probe). `device_perf` (migration 0008) is the badge state; `poll_results.jitter_ms`
  (0007) feeds the baseline. `services.nodes_list` exposes `perf` (None unless degraded).
  Tests: `unit/test_baseline`, `integration/test_perf`.

## Graph topology — backup lines + the on-backup signal (Phase 9 Part A)

- **Two sources of truth, on purpose.** `devices.parent_device_id` stays the denormalized
  **PRIMARY** parent (every existing tree/topo/`_culprit` query keeps working unchanged);
  `device_links` (migration 0011) carries only the **extra redundancy edges** (`kind='backup'`,
  `is_active`). `DeviceMeta.parents` loads *only* the backup edges; `DeviceMeta.effective_parents()`
  combines primary + backups. There is **no primary backfill** in the migration — the primary
  already lives on the device row, so duplicating it would just create rows nothing reads. Don't
  start reading the primary out of `device_links`.
- **Suppression is now all-parents-down, not single-parent.** `process_cycle` relabels a child
  `UNREACHABLE` only when **every** monitored parent (primary OR backup) ∈ `DOWN_FAMILY`. If **any**
  parent is alive yet the child won't answer, it's a genuine fault → stays `DOWN` and pages. With
  exactly one parent this is byte-for-byte the old behaviour (the back-compat anchor is
  `test_state_machine.Topology`). `_topological_order` is **Kahn's by in-degree** over the full edge
  set (a node lands after *all* parents); cycles fall out as leftovers and are appended by id.
  `probe_plan` treats a node as gentle-infra if it's **any** parent_id in the edge set (a backup-only
  parent still backhauls).
- **On-backup is computed in the engine, not a DB sweep.** `CycleResult.redundancy: dict[int,bool]`
  (full pass only; `{}` under canary-freeze and in the subset/confirmation pass) flags each
  redundancy-capable node where the **primary parent is down, a backup parent is alive, and the node
  itself still pings**. The daemon persists the badge + pages via `AlertDispatcher.redundancy_sweep(
  redundancy, states, ts)` — note it passes the **FINAL** states (post fast-confirm), so a node that
  confirmed hard DOWN is forced off the badge (its outage owns the story) and cleared **silently**.
- **redundancy_sweep clones the perf-tier discipline (decision #1: on-backup is NOT louder).** Badge
  always written to `device_redundancy` (PK `device_id`, `on_backup`, `primary_down_since`); a single
  **operator** page only on the enter/leave **edge**; restart-safe (prior `on_backup` rehydrated from
  the table so a restart mid-failover never re-pages); **never** opens an outage or an escalation row.
  `WISP_BACKUP_ALERTS=0` keeps the badge, mutes the page. `services.nodes_list` exposes `on_backup`
  (suppressed in the view when the node is hard DOWN); `list_devices` exposes `backup_parents`.
- **Edge CRUD + FK discipline.** `services.add_backup_link`/`remove_backup_link` (API
  `POST`/`DELETE /api/devices/{id}/links[/{parent_id}]`): a backup edge can't be the node itself, its
  existing primary, a duplicate, or close a loop (cycle check over the **combined** edge set, same
  walk `_clean_device_payload` now uses via `_combined_edges`). `device_links` REFERENCES `devices(id)`
  in **both** columns, so `delete_device` clears rows where the device is `child_id` **or** `parent_id`
  (plus `device_redundancy`) before the device row. Blast radius (`triage_outages._culprits`) walks
  **all** parents now — an UNREACHABLE node is credited to each nearest DOWN ancestor across every path.
- **Tests:** `unit/test_state_machine.GraphTopology` (multi-parent order, all-parents-down vs
  one-parent-alive, on-backup enter/leave, backup-parent gentle probing),
  `integration/test_redundancy` (sweep edge page, badge, restart no-repage, node-down silent clear,
  alerts gate, no-outage/no-escalation), `integration/test_api.BackupLinkTest` (edge CRUD + cycle +
  FK-safe delete + diamond `_culprit` + on-backup badge).

## SNMP port status (Phase 9 Part B)

- **A sibling ingress, NOT a Prober impl.** `Prober.ping(ip)` is one reading per IP; a switch
  has N ports, so `ingress/snmp.py` is a parallel poller (`SnmpPoller` protocol +
  `build_snmp_poller`). The wire format is parsed by a **pure** `parse_if_table(varbinds)` — that
  (plus `PortStatus.is_down`) is the boundary the tests exercise with hand-built rows; **no real
  SNMP in the suite**, exactly like the recording-notifier double. `PysnmpPoller` lazy-imports
  `pysnmp` (asyncio HLAPI, `mpModel=1` = v2c) so the dashboard + tests stay pure-stdlib; only the
  daemon venv gains `pysnmp` (in `requirements.txt`). Scope is **IF-MIB oper/admin only** — no
  CPU/mem/temp. `snmp_community` is the **first per-device credential** in the DB (only the PIN hash
  was before); low sensitivity (read-only v2c on a mgmt VLAN) but a conscious change.
- **Own slow cadence, isolated.** The daemon runs `snmp_cycle` on a timed guard
  (`WISP_SNMP_INTERVAL_S`, default 90s; 0 disables) next to the prune/rollup guards, skipped for
  finite `--cycles` runs. Each switch walk is wrapped in its own try/except — **a dead/blocked switch
  or a broken pysnmp must never sink the ICMP cycle.** `build_snmp_poller`/`PortMonitor` are built
  once; `load_snmp_targets` is re-read each pass so a UI enable/disable applies with no restart.
- **`egress/ports.PortMonitor` is the SNMP `AlertDispatcher`.** `sync_device(device_id, ports, ts)`
  upserts every port into `switch_ports` (discovery: a new port lands `monitored=0`; the operator
  ticks which to watch — you do NOT alarm on every access port). Operator-set fields (`monitored`,
  `feeds_device_id`) are never overwritten by a walk. **Flap suppression is in-row** (`down_streak`/
  `alarm`/`alarm_since`), so it's restart-safe: a monitored port needs `WISP_SNMP_DOWN_CONSECUTIVE`
  (default 2) consecutive `oper=down while admin=up` walks to alarm (`is_down`). **admin-down is
  silent** (intentional shut). Edge-only operator page (operator-only, like perf/redundancy), gated
  by `WISP_SNMP_ALERTS` (state always written).
- **Folding (decision #3): a monitored port-down does NOT raise a competing alarm.** If the port has
  a `feeds_device_id` and that device has an **open outage**, the port-down *enriches* it —
  `_stamp_cause` sets `outages.root_cause` via `COALESCE` (never clobbers a post-mortem) with the
  physical cause. If the fed device has **no** open outage it's a *leading indicator* heads-up (ICMP
  still owns outages — SNMP never opens one). A monitored port with no `feeds_device_id` pages a
  plain operator heads-up. Recovery is a single edge page.
- **FK discipline.** `switch_ports` REFERENCES `devices(id)` in **both** `device_id` and
  `feeds_device_id`, so `delete_device` clears `switch_ports WHERE device_id=? OR feeds_device_id=?`
  before the device row. Services: `set_snmp_config`, `list_switch_ports`, `set_port_monitored`
  (re-arms detection: resets streak/alarm), `set_port_feeds` (validates target). API:
  `POST /api/devices/{id}/snmp`, `GET /api/devices/{id}/ports`, `POST /api/ports/{id}/monitored`,
  `POST /api/ports/{id}/feeds`; the Nodes modal carries the SNMP config + a ports panel.
- **Surfaced live in the UI, not just the edit modal.** `nodes_list` (`/api/nodes`) now carries
  a per-switch `ports: {total, monitored, down, bw_low}` summary (`down` = monitored ports in
  `alarm=1`; `bw_low` = monitored ports in `bw_alarm=1`; one GROUP BY — don't pull raw rows) +
  `snmp_enabled`, so a switch with a down (or starved) monitored uplink no longer looks identical to
  a healthy one (badge on the node row + map node). `services.topology_graph`
  (`GET /api/topology`) returns the whole network as `{nodes, edges}` for the **topology map** —
  nodes **reuse `nodes_list`** (one source of truth for state), edges expose all three relationship
  models as `kind` ∈ `primary`|`backup`|`port` (the port edge carries `port_label` + `down`), and
  edges to inactive nodes are dropped so the map never dangles. The Nodes page has a **Tree ⇄ Map**
  toggle (`app.js`, persisted in `localStorage`); the map is **pure SVG, no library** — a tidy-tree
  layout of the primary topology with backup/port edges as overlay curves, pan/zoom via `viewBox`
  math, and a node-click **read-only detail card** (live state + uplinks + which ports are down) with
  an Edit button into the existing modal. A live SSE refresh preserves the current `viewBox` so it
  doesn't reset pan/zoom. A date drill-down (heatmap) forces the list regardless of the toggle.
- **Per-port bandwidth tier (orthogonal to oper/admin status).** The *same* walk now also reads the
  ifXTable 64-bit byte counters (`ifHCInOctets`/`ifHCOutOctets`) + capacity (`ifHighSpeed`→bps, else
  `ifSpeed`); `parse_if_table` carries them on `PortStatus` (`in_octets`/`out_octets`/`speed_bps`).
  The **pure** `snmp.throughput_bps(prev, cur, dt)` turns two counter samples into bits/sec —
  None on a missing sample, a non-positive interval, OR a backwards counter (reboot/wrap → emit
  nothing, not a phantom spike). `PortMonitor.sync_device` diffs each port vs its **prior row**
  (`in_octets`/`out_octets`/`counters_at`, stored as **TEXT** because a Counter64 can exceed SQLite's
  signed-64 INTEGER range — the math is Python arbitrary-precision int), persists the live rate
  (`in_bps`/`out_bps`/`if_speed_bps`), and runs a **second flap-suppressed alarm** alongside the
  down one: a *monitored* port that is **oper=up** and has an operator-set `bw_threshold_mbps`
  trips when its watched rate stays below it for `WISP_SNMP_BW_CONSECUTIVE` (default 3) walks.
  `bw_direction` ∈ `in`|`out`|`either`|`total` (NULL ⇒ `either`) picks which rate matters — "for an
  ISP, whichever is important" for that link. Operator-set fields (`bw_threshold_mbps`/`bw_direction`)
  are **never** overwritten by a walk (like `monitored`/`feeds_device_id`); the alarm state
  (`bw_low_streak`/`bw_alarm`/`bw_alarm_since`, migration 0013) is in-row so a restart never
  re-pages. Edge-only **operator** page gated by `WISP_SNMP_BW_ALERTS` (state always written); it is
  a **soft signal — never folds into / opens an outage** (the port still pings up; a hard port-down
  is the down alarm's job). **A bw-alarmed port that then goes oper-down clears its bw badge
  SILENTLY** (eligibility lost → the down alarm owns the story, no "bandwidth recovered" noise).
  Services: `set_port_bandwidth(port_id, threshold_mbps, direction)` (re-arms detection like
  `set_port_monitored`; validates), `list_switch_ports` exposes `in_mbps`/`out_mbps`/`link_mbps`/
  `bw_threshold_mbps`/`bw_direction`/`bw_alarm`. API: `POST /api/ports/{id}/bandwidth`. The Nodes
  modal ports panel shows live ↓/↑ Mbps + the threshold/direction inputs; the node row + map node
  badge a `bw_low` count.
- **Surfaced on the main dashboard (operator-facing, real-time).** `system_summary` carries a
  `low_bandwidth` list (one entry per monitored port currently in `bw_alarm`: switch, port label,
  live in/out Mbps, threshold/direction, `bw_alarm_since`). The dashboard renders a **"Low Bandwidth"
  card** (shown only when non-empty) + a persistent **header chip** on every page, and toasts a port
  that *newly* crosses below limit (the SPA tracks seen `port_id`s so a reload doesn't re-toast a
  standing alarm). It's **live**: `_data_version` now folds in `switch_ports.updated_at`, so each
  walk pushes via SSE — combined with the faster default SNMP cadence (`WISP_SNMP_INTERVAL_S` 30s)
  the rates + card refresh in near-real-time. The ntfy operator page on the edge is unchanged; this
  is the in-app channel. `set_port_monitored` now also disarms the bw alarm (full re-arm on unwatch).
- **Tests:** `unit/test_snmp` (parser + is_down + counter/speed capture + the pure `throughput_bps`
  math: normal/first-sample/non-positive-dt/counter-reset/Counter64), `integration/test_ports`
  (discovery, flap suppression, admin-down silent, fold-into-outage, leading indicator opens no
  outage, recovery, alerts gate; **+ `PortBandwidthTest`**: rate-from-delta, low-bw flap suppression
  + edge page, single-dip no-page, recovery, direction in/out, unmonitored never alarms, bw alerts
  gate, port-down clears bw silently), `integration/test_api.SnmpPortApiTest` (config/validation,
  targets, toggles, **bw threshold validation + re-arm**, FK delete), `integration/test_api.TopologyTest`
  (per-switch port summary incl. `bw_low`; the three edge kinds + port `down` flag; dangling-edge drop
  on delete), `integration/test_daemon.SnmpCycleWiring` (persist + broken-walk isolation).

## Reliability invariants (the "trust the alarm" set — don't regress)

- **One logical poller per DB.** Each daemon has its own in-memory FSM, so a second
  daemon against the same DB independently confirms every outage and double-pages.
  `apps/daemon/main.py:main()` takes an OS advisory lock (`runtime/single_instance.py`,
  `<db>.lock`) and exits (code 3) if another holds it; the kernel frees it on exit/crash.
  Belt-and-braces: `apply_events` makes `OutageOpened` **idempotent** (won't insert a
  second open row while one is unresolved), so a stray duplicate event can't stack
  outages either. Tests: `integration/test_single_instance`.

- **Uplink state is logged as a stable token, not the alert title.** `_send_owner(...,
  payload=...)` records `"UPLINK_DOWN"` / `"UPLINK_RESTORED"` in `alert_log.payload`; the
  rehydration query (`build_engine`) and the dashboard banner (`system_summary`) both key
  off `"UPLINK_DOWN" in payload`. Don't fold the human title back into the payload or a
  wording change silently breaks uplink detection. (There is still no dedicated uplink-state
  column — the `alert_log` token is the source of truth.)
- **Canary freeze is logged.** When `result.canary_down` (canary 100% loss + `canary_freeze`),
  `run_cycle` logs a WARNING — the freeze skips ALL local detection that cycle, so a flaky
  internet canary silently stalls detection; the log is how you tell that apart from "nothing
  is actually down". Consider a LAN-reachable canary or `WISP_CANARY_FREEZE=0` for LAN gear.
- **The daemon never dies on one bad cycle.** `apps/daemon/main.py:run_forever` wraps both
  the device-set reload and `run_cycle` in try/except that **logs and continues** — a DB lock,
  a probe library blowing up, or a bug skips that cycle, it does not kill the monitor. Keep any
  new per-cycle work inside that guard. (`_gather_pings` separately swallows per-probe errors.)
- **A page must not vanish to a blip.** `NtfyNotifier.send` retries via the pure
  `send_with_retry(attempt, attempts, backoff, sleep)` helper: network/timeout/5xx are
  **retryable** (exponential backoff), a **4xx fails fast** (bad topic/config won't self-heal).
  Tunable via `WISP_NTFY_RETRIES` / `WISP_NTFY_RETRY_BACKOFF_S`. Test the helper directly with a
  fake `attempt`/`sleep` (no httpx) — don't reintroduce a network-touching notifier test.
- **Dead-monitor watchdog (`server/watchdog.py`).** The **dashboard process** watches the
  **daemon**: `MonitorWatchdog.check(now)` pages the owner (`MONITOR_STALE`) when the newest
  `poll_results` is older than `cfg.stale_threshold_s()` (auto `max(180, 3×poll)`, override
  `WISP_MONITOR_STALE_S`), and once more (`MONITOR_OK`) when polling resumes. It is **restart-safe**
  (rehydrates `_alarm_active` from the last *sent* `MONITOR_*` row in `alert_log`, so a dashboard
  restart never re-pages) and **conservative** (never alarms before the first poll or with no
  active devices — a fresh install is not a dead monitor). A failed page is logged `failed` and
  retried next tick, so it doesn't strand the alarm. `start_watchdog_thread` runs it on a daemon
  thread from `apps/dashboard/main.py`. `services.system_summary` exposes the same `monitor_stale`
  flag (+ `stale_after_s`) so the dashboard banner, the summary, and the watchdog all agree —
  don't reintroduce a client-side staleness threshold in `app.js`.

## Config + device-set reload (don't break)

- **Config is env-var only.** Every tunable is a field on the frozen `Config` dataclass
  (`config.py`), read once from a `WISP_*` env var (or its default) at process start. There is
  **no DB settings layer** — no `SettingSpec`, no `SETTING_SCHEMA`, no `load_runtime_config`,
  no `get/update_settings`, no `/api/settings`. Change a tunable by exporting the env var and
  restarting. Add a new tunable = add one `Config` field + its `_env_*` default.
- The DB `settings` table still exists, but **only** `server/auth.py` uses it (the salted PIN
  hash + salt). It is not a general config store anymore.
- **Device-set hot reload (in-process, no `os.execv`):** the daemon builds engine/prober/
  dispatcher **once at startup**, then at the top of each cycle re-reads `load_device_meta` and,
  if the active device set changed (UI add/remove), rebuilds the engine + dispatcher **in-process**
  (`apps/daemon/main.py:run_forever`). `build_engine` rehydrates each FSM from the last
  `poll_results` row, so a rebuild never re-pages an open outage. Config changes are **not**
  hot-reloaded — they need a daemon restart. (Finite `--cycles` runs skip the reload check.)
- **Auth (`server/auth.py`):** one shared PIN (salted SHA-256 in `settings`) + signed-cookie sessions
  (HMAC, secret in `data/session_secret`, 0600). `routes._guard_api` gates every `/api/*` except the
  login flow; **static assets are unauthed** (the SPA renders its own PIN gate on 401). Verb handlers
  **must drain the request body** (`_consume_body` at the top) before any early 401/429 reply, or the
  unread body corrupts the next keep-alive request.
- **Role-based channels (routing):** alerts route to **three fixed ntfy topics**, one per role —
  `cfg.ntfy_topic_{owner,operator,tech}` (defaults `hansa-*`, env `WISP_NTFY_TOPIC_*`). A person
  subscribes to the topic for their role; there is **no per-person routing key**. `notifiers.role_topic`
  maps role→topic; `AlertDispatcher._publish(role, …)` sends to that topic **plus a copy to the operator
  topic** (operators get full visibility), while `AlertDispatcher._broadcast(…)` sends to **all three**
  topics once each. Mapping: a fresh device DOWN pages **owner + operator** (`_publish("owner", …)`, which
  is owner-topic + the operator copy) so the down page is as consistent as the restore (the owner used to
  only hear about a DOWN an hour later via the escalation — the "DOWN less consistent than UP" report); the
  recurring hourly escalation + the restore notice **broadcast to all three** (so the tech channel is only
  looped in once it's been down a while); uplink down/restored →
  `owner`. (See "Escalation model" below.) The old per-device `technician_phone` routing, `resolve_owner`,
  and `services.technicians()` are **gone** (the `devices.technician_phone` / `workers.phone` /
  `workers.ntfy_topic` columns still exist but are unused by routing). Workers are now just identity +
  role (`name/role/region/is_active/notes`); still can't remove the last active owner (`LastOwnerError`
  → 409). `services.test_channel(role)` fires a test push to a role's topic. The SPA learns the topic
  names from `/api/auth/status` (`channels`) and shows them on the Team + Settings pages.
- **Operator attendance (daily present-toggle; migration 0010 `attendance`).** A roster of who
  showed up, by **UTC calendar day** (same day convention as the heatmap, via `services._today`).
  One row per `(worker_id, day)` = present that day; **absence is the absence of a row** (the toggle
  DELETEs to mark not-present), `UNIQUE(worker_id, day)` makes marking idempotent. **Operators only**
  — `set_attendance` 422s a non-operator worker; `attendance_overview`/`list_operators` filter
  `role='operator' AND is_active=1`. API: `GET /api/attendance` (roster: `{today, days, operators:[…]}`
  for the Team-page board) + `POST /api/attendance {worker_id, present, day?}` (toggle, day defaults
  to today). The Team page renders the toggle chips + a recent-days grid; **triage cards carry
  `on_duty`** — operators present on the outage's **start** day — so "who was around when it broke"
  shows on the card (`onDutyLine` in app.js). Attendance writes do **not** bump `_data_version`
  (they don't touch poll_results/outages/alert_log), so triage `on_duty` refreshes on the next poll /
  15s fallback, not instantly — fine, it's not time-critical. **`attendance` REFERENCES `workers(id)`,
  so `delete_worker` must clear a worker's attendance rows before the worker row** (same `foreign_keys=ON`
  rule as the devices-FK tables). Tests: `integration/test_api` `AttendanceTest`.
- **Secrets:** the only secret is the dashboard `pin_hash` (salted SHA-256 in the `settings`
  table), handled entirely by `server/auth.py` (set/verify via the PIN endpoints). No other
  config flows through the DB — everything else is env-var `Config` (see "Config" above).

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2,
  recovery = 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override applied
  in `MonitorEngine.process_cycle` after `feed()`. Don't regress these counts. (Fast-confirm —
  see Scaling — gathers DOWN's 3 samples in seconds via rapid re-probe, but the *count* is the
  same; it changes when the samples arrive, not how many.)
- **Topology order:** devices are processed parent-before-child (`_topological_order`) so a
  parent's new state is known when evaluating its children.
- **No automatic cause inference.** The engine does **not** guess why a device is down. The old
  power-vs-link heuristic (`power_ref_ip`, `inferred_cause`, per-device `criticality`) was
  removed (migration `0005`) — it was never settable from the UI. Cause is now only the
  operator-entered post-mortem (`root_cause` / `resolution_notes`) at resolution. Don't
  reintroduce an inferred cause or a `criticality` field.
- **Escalation model (the alarm ladder):** a fresh DOWN pages **owner + operator**, immediately
  (`_on_open` → `_publish("owner", …)` → owner topic + the operator copy; the tech channel is held back
  to the hourly escalation). Dedupe is **per-outage** (`_already_paged`: was there
  already a `sent` row for this `outage_id`?), NOT a time window — a device that recovers and fails
  again is a new outage and pages again. (The old `alert_dedupe_min` window also counted restore
  notices as "recently alerted", so any flapping device went silent after the first page — the
  "no DOWN notification" bug. `alert_dedupe_min` is now unused.) It also queues **one** `escalations`
  row of kind `"hourly"` due at `now + cfg.escalate_every_min` (default 60, env
  `WISP_ESCALATE_EVERY_MIN`). Each `sweep` that finds it due while the outage is **still open** fires
  `_fire_hourly` — an **all-hands broadcast** (owner + operator + tech) stating the running duration
  and who acked it (if anyone) — then **reschedules the same row** to `now + interval` (it does *not*
  mark it executed). **Acknowledgement does NOT stop this loop; only recovery does** — `_on_resolved`
  marks the row executed (and broadcasts the restore). So don't reintroduce ack-cancels-escalation or
  the old two-step `realert`/`escalate_to_owner` kinds.
- **Escalations are DB-derived** (`escalations.due_at` + sweeper), not in-memory timers, so
  restarts don't drop them. `UNIQUE(outage_id, kind)` keeps them idempotent (one `hourly` row per
  outage, rescheduled in place rather than re-inserted).
- **Restart safety:** `build_engine` rehydrates each FSM from the last `poll_results` row;
  breaking that re-pages everyone on restart.
- **Timestamps:** poll/outage stamps are ISO8601 `+00:00`; SQLite `datetime('now')` (acks) is
  space-separated naive. `core/analytics._parse` normalises both to naive UTC — reuse it.
- **Schema changes:** add `migrations/000N_*.sql` (idempotent, `IF NOT EXISTS`); the runner
  tracks applied versions in `schema_migrations`. Never edit `0001_init.sql` in place. If you
  add a `devices` column, update both `DeviceMeta` and the SELECT in `load_device_meta`.
- **Dashboard layering:** `server/services.py` mirrors `core/analytics.py` but returns
  dicts/lists; `server/routes.py` is HTTP-only (runnable entry is `apps/dashboard/main.py`).
  Triage buckets: open+unacked = `unassigned`, open+acked = `in_progress`,
  recovered+undocumented = `pending_postmortem`; UNREACHABLE is excluded (never paged).
  **Blast radius:** an UNREACHABLE child is topology-suppressed so it gets no card of its
  own — `triage_outages` instead attributes each UNREACHABLE node to its nearest **DOWN**
  ancestor (walk parents while they're UNREACHABLE; the first DOWN is the culprit) and
  returns those names as `affected_children` on the **open** parent card (rendered by
  `affectedLine` in app.js). Without this a downed parent hides who else is dark. The
  confirmed cause is captured by the post-mortem dropdown at resolution (there is no automatic
  cause guess anymore). A `pending_postmortem` card can
  instead be **dismissed** (DELETE `/api/outages/{id}` → `services.dismiss_outage`): it stamps a
  sentinel `resolution_notes` (`DISMISSED_NOTE`) so the row leaves triage but **stays in downtime
  history** — it's a soft clear, not a hard delete, so analytics are untouched.
- **Device CRUD** (`services.create/update/delete_device`, validated via `DeviceError`→422):
  PUT is a full replace (the form submits every field); DELETE hard-deletes the node + its
  poll/outage/alert history in one txn but is **blocked (409) if it still has child nodes**.
  **`delete_device` must delete from *every* table that `REFERENCES devices(id)` before the
  device row** — currently `escalations`, `alert_log`, `outages`, `poll_results`,
  `poll_rollups`, `device_perf`, `device_links` (**both** `child_id` and `parent_id`),
  `device_redundancy`, `switch_ports` (**both** `device_id` and `feeds_device_id`) — or
  `foreign_keys=ON` rejects the final DELETE with "FOREIGN KEY constraint failed". Adding
  a new devices-FK table? Add its delete here too.
  Added/removed nodes start/stop being monitored automatically: the daemon snapshots the device
  set at start and rebuilds its engine in-process when it changes — within one poll cycle (see
  "Config + device-set reload").
- **Maintenance mode (`devices.maintenance`, migration 0009; `services.set_maintenance`, POST
  `/api/devices/{id}/maintenance`).** Fully pauses one node's monitoring: `load_device_meta` filters
  `maintenance=1` out of the active set, so the device-set reload **drops it from the engine in-process**
  — the daemon stops pinging it and pages no one for it (any pending `escalations` row no-ops via
  `_fire_hourly`'s `dev is None` guard). The node **stays in the inventory and on the Nodes dashboard**,
  badged "maintenance" (`nodes_list`/`list_devices` expose the flag), so a paused node isn't mistaken for
  a healthy live one — its shown `state` is its last reading (stale). A normal device edit (`update_device`,
  `_DEVICE_FIELDS`) does **not** touch the flag; it's toggled only via `set_maintenance`. Caveat: putting an
  already-DOWN node into maintenance leaves its open outage lingering in triage until you resume it (then
  `build_engine` rehydrates + `_confirm_up` resolves it) or dismiss it.
- **Live UI is push, not poll (SSE).** `GET /api/events` (`routes._serve_events`) is a
  Server-Sent Events stream: a per-connection 1s loop emits a `changed` event whenever
  `_data_version` (MAX(id) of `poll_results`/`outages`/`alert_log` + MAX(`updated_at`) of
  `switch_ports` so an SNMP walk's port/bandwidth changes — which UPSERT in place, no new id —
  also push the map/faceplate + Low-Bandwidth card live) moves — i.e. every cycle and
  instantly on a between-cycle DOWN/recovery. The body has no Content-Length (delimited by
  `Connection: close`); it's gated by `_guard_api` like any `/api/*` (EventSource sends the session
  cookie). The SPA subscribes once (`startLive`/`liveReload` in app.js) and re-renders the live
  views on each event; a 15s poll remains only as a **fallback when the stream is down** (`_liveOk`).
  Don't reintroduce unconditional 15s polling. Tested over the real server in `integration/test_auth`.
  Because EventSource + keep-alive clients drop sockets constantly (navigate away, refresh, reconnect),
  the server is `routes._DashboardServer` (a `ThreadingHTTPServer` subclass) whose `handle_error`
  **swallows benign connection-teardown errors** (`ConnectionReset`/`BrokenPipe`/`ConnectionAborted`)
  instead of dumping a full traceback per disconnect — that noise otherwise buries real errors. It
  still surfaces every other exception. Don't revert to the bare `ThreadingHTTPServer`.
- **Web assets are vendored** under `apps/dashboard/static/` (no CDN, no build step); `routes.py`
  serves `index.html` from `templates/` and everything else from `static/`. The app is plain
  vanilla JS — Tailwind's Play-CDN runtime JITs classes off the live DOM, so dynamically-built
  class strings work. Phase-7: swap the Play-CDN runtime for a precompiled stylesheet.

## Tests

Run `python -m unittest discover -s tests` after any logic change (161 tests). They mirror the
layers: `unit/test_state_machine` (FSM + overrides + `probe_plan` gentle-infra + the subset
confirmation pass + adaptive cadence), `integration/test_notifiers` (dispatch/escalation/ack +
the `send_with_retry` policy, temp DB + controlled time), `integration/test_analytics`
(outage-window/downtime math), `integration/test_api` (services + device CRUD + `AttendanceTest`:
operator present-toggle, roster window, operators-only guard, triage `on_duty`, FK-safe delete),
`integration/test_watchdog` (dead-monitor alarm: stale→page, recover, restart no-repage,
failed-send retry), `integration/test_daemon` (poll-gather error policy, concurrency-bound
semaphore / per-IP count map, **+ fast-confirm: rapid DOWN confirm and blip-clears-without-paging**,
**+ fast-recovery (`_confirm_up`): rapid UP confirm + flap-doesn't-recover**,
**+ between-cycle watch: mid-gap failure pages, mid-gap recovery resolves, mid-gap blip doesn't page, canary freeze suppresses**),
`integration/test_rollup` (hourly fold math, in-progress-hour skip, idempotency, `device_trend`),
`unit/test_baseline` (pure perf-deviation detector: enter/hold/recover, floors, jitter,
DOWN-excluded baseline), `integration/test_perf` (`perf_sweep`: operator-only edge page,
badge in `device_perf`, DOWN-clears-silently, alerts gate, restart no-repage),
`integration/test_single_instance` (the OS lock refuses a second daemon + idempotent
`OutageOpened`), `integration/test_auth` (also covers the SSE `/api/events` stream: auth-gated
+ emits a `changed` event).
Add cases there — time-based paths (escalation, dedupe, staleness, rollup buckets)
need the temp-DB + controlled-clock setup to surface. Tests inject a recording notifier double
(no real ntfy/network); don't reintroduce a production mock channel.
