# CLAUDE.md

Working notes for Claude Code in this repo — **only** the conventions, invariants, and
gotchas that aren't obvious from the code. For everything else, don't duplicate it here,
read it: `README.md` (what it is, how to run it, the directory layout, the module/layer
map, config, behaviors) and `plan.md` (design rationale, the remaining Phase 7 go-live
work, and open questions).

## Status

Production build: Phases 1–6 (engine, FSM, alerting, BI, dashboard) **and Phase 8**
(team directory, PIN gate, monitor lifecycle) — 75 tests. Config is env-var only (no
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

## Reliability invariants (the "trust the alarm" set — don't regress)

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
  topics once each. Mapping: a fresh device DOWN pages **operator only** (`_publish("operator", …)`); the
  recurring hourly escalation + the restore notice **broadcast to all three**; uplink down/restored →
  `owner`. (See "Escalation model" below.) The old per-device `technician_phone` routing, `resolve_owner`,
  and `services.technicians()` are **gone** (the `devices.technician_phone` / `workers.phone` /
  `workers.ntfy_topic` columns still exist but are unused by routing). Workers are now just identity +
  role (`name/role/region/is_active/notes`); still can't remove the last active owner (`LastOwnerError`
  → 409). `services.test_channel(role)` fires a test push to a role's topic. The SPA learns the topic
  names from `/api/auth/status` (`channels`) and shows them on the Team + Settings pages.
- **Secrets:** the only secret is the dashboard `pin_hash` (salted SHA-256 in the `settings`
  table), handled entirely by `server/auth.py` (set/verify via the PIN endpoints). No other
  config flows through the DB — everything else is env-var `Config` (see "Config" above).

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2,
  recovery = 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override applied
  in `MonitorEngine.process_cycle` after `feed()`. Don't regress these counts.
- **Topology order:** devices are processed parent-before-child (`_topological_order`) so a
  parent's new state is known when evaluating its children.
- **No automatic cause inference.** The engine does **not** guess why a device is down. The old
  power-vs-link heuristic (`power_ref_ip`, `inferred_cause`, per-device `criticality`) was
  removed (migration `0005`) — it was never settable from the UI. Cause is now only the
  operator-entered post-mortem (`root_cause` / `resolution_notes`) at resolution. Don't
  reintroduce an inferred cause or a `criticality` field.
- **Escalation model (the alarm ladder):** a fresh DOWN pages the **operator only**, immediately
  (`_on_open` → `_publish("operator", …)`, anti-spam deduped). It also queues **one** `escalations`
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
  recovered+undocumented = `pending_postmortem`; UNREACHABLE is excluded (never paged). The
  confirmed cause is captured by the post-mortem dropdown at resolution (there is no automatic
  cause guess anymore). A `pending_postmortem` card can
  instead be **dismissed** (DELETE `/api/outages/{id}` → `services.dismiss_outage`): it stamps a
  sentinel `resolution_notes` (`DISMISSED_NOTE`) so the row leaves triage but **stays in downtime
  history** — it's a soft clear, not a hard delete, so analytics are untouched.
- **Device CRUD** (`services.create/update/delete_device`, validated via `DeviceError`→422):
  PUT is a full replace (the form submits every field); DELETE hard-deletes the node + its
  poll/outage/alert history in one txn but is **blocked (409) if it still has child nodes**.
  Added/removed nodes start/stop being monitored automatically: the daemon snapshots the device
  set at start and rebuilds its engine in-process when it changes — within one poll cycle (see
  "Config + device-set reload").
- **Web assets are vendored** under `apps/dashboard/static/` (no CDN, no build step); `routes.py`
  serves `index.html` from `templates/` and everything else from `static/`. The app is plain
  vanilla JS — Tailwind's Play-CDN runtime JITs classes off the live DOM, so dynamically-built
  class strings work. Phase-7: swap the Play-CDN runtime for a precompiled stylesheet.

## Tests

Run `python -m unittest discover -s tests` after any logic change (75 tests). They mirror the
layers: `unit/test_state_machine` (FSM + overrides + `probe_plan` gentle-infra),
`integration/test_notifiers` (dispatch/escalation/ack + the `send_with_retry` policy, temp DB +
controlled time), `integration/test_analytics` (outage-window/downtime math),
`integration/test_api` (services + device CRUD), `integration/test_watchdog` (dead-monitor alarm:
stale→page, recover, restart no-repage, failed-send retry), `integration/test_daemon`
(poll-gather error policy **+ the concurrency-bound semaphore / per-IP count map**),
`integration/test_rollup` (hourly fold math, in-progress-hour skip, idempotency, `device_trend`).
Add cases there — time-based paths (escalation, dedupe, staleness, rollup buckets)
need the temp-DB + controlled-clock setup to surface. Tests inject a recording notifier double
(no real ntfy/network); don't reintroduce a production mock channel.
