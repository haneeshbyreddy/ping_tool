# CLAUDE.md

Working notes for Claude Code in this repo — conventions, invariants, and gotchas that
aren't obvious from the code. For what/how/layout/config, read `README.md` instead of
duplicating it here. For the design rationale behind these invariants, the roadmap, and
a list of deliberately-deleted code, read `docs/ARCHITECTURE.md` — split out to keep
this file's per-session load small; only open it when you need the backstory.

## Architecture at a glance

Central runs the brain, for every org. The edge is a thin probe, full stop — one
daemon mode (`WISP_CENTRAL_BRAIN=1` + `WISP_CENTRAL_URL`, effectively mandatory). It
fetches topology from central, probes with real ICMP under bounded-concurrency fan-out,
reports raw per-IP samples back. No local database, dashboard, PIN, or FSM on the edge.

Central owns the FSM, topology-aware suppression, fast-confirm detection, the alerting
ladder, the multi-org dashboard, and fleet version/rollout state. ISPs log into
central's dashboard with a per-org account, manage topology/team/alert routing there,
and self-service-register the nodes they run — one ISP can run one or many nodes, each
with its own enrollment credential.

335 tests, `python -m unittest discover -s tests`. Verify claims about what's done
against the code, not just this file — stale docs drift.

Central's own server process (`central/server.py`/`store.py`/`auth.py`/…) is pure
stdlib. Its dashboard is a separate build-time SPA — React + TypeScript + Tailwind +
shadcn/ui, source in `web/`, compiled straight into `central/static/` (see "Central
dashboard" below) — Node is a dev/build dependency only, never a runtime one; the
deployed artifact is static assets a stdlib server hands out unchanged, same as before.
The edge needs a small venv
(`requirements.txt`: `icmplib`/`httpx`) — install into `.venv`, never globally
(system Python is PEP 668-locked) — and the kernel ping group for unprivileged ICMP
(`sysctl net.ipv4.ping_group_range="0 2147483647"`). Not yet done: real production
hosting (needs the operator's provider/region/domain) and the last mile of fleet-update
signing (real minisign keypair + Windows code-signing cert as CI secrets, plus a genuine
tagged release tested on real hardware) — see "Roadmap" below.

See `docs/ARCHITECTURE.md` for: the design rationale behind the invariants below, the
locked-decisions table, the numbered roadmap (item numbers are referenced by comments
across the codebase — keep them stable), open questions, and the full list of
deliberately-removed single-box-era code (don't go looking for `apps/dashboard/`,
`src/wisp/server/`, the old vanilla-JS dashboard, etc. — it's all gone, not deprecated).

## Fleet update signing

- **Two install scripts, one per OS.** `deploy/install-edge.sh` (Linux) /
  `install-edge.ps1` (Windows): frozen binary + supervisor (`runtime/supervisor.py`)
  owns self-update. Both are exercised for real on every push via CI's `build` job
  (unsigned).
- **CI signing (`.github/workflows/release.yml`) is real, needs real secrets.**
  Authenticode signs Windows `.exe`s directly (per-binary, in `build`, needs
  `WINDOWS_CODESIGN_PFX`/`_PASSWORD`); minisign signs the assembled `SHA256SUMS`
  **once** in `release` (needs `MINISIGN_KEY`, generated **without** a password —
  `minisign -G -W` — since CI can't answer a passphrase prompt). One signature over the
  checksums manifest covers every artifact transitively — don't add a per-artifact
  `.minisig`. Both steps are `if: env.<SECRET> != ''` no-ops when unset.
- **The minisign PUBLIC key is not a secret — commit it to `deploy/minisign.pub`** once
  a real keypair exists; don't fabricate a placeholder (an invalid pubkey makes both
  installers hard-fail verification even on a legitimately-unsigned release, since the
  file's presence alone triggers the check).
- **Both fleet installers are self-activating on signing, not hard-required.** No
  pubkey/signature (Linux) or Authenticode `NotSigned` (Windows) is a warning +
  sha256-only fallback; a signature that IS present and doesn't verify is a hard
  err/Die. Same pattern as mTLS enrollment and self-service node tokens.
- **`deploy/wisp-edge.spec`'s `Analysis` paths are built off
  `os.path.dirname(SPECPATH)`, not bare relative strings — don't revert.** PyInstaller
  resolves a *loaded* `.spec`'s relative paths against the spec's own directory
  (`deploy/`), not the invoking cwd — bare strings would look in `deploy/apps/...` /
  `deploy/src` instead of the repo root. The inline supervisor build in `release.yml`
  (no `.spec` file) doesn't have this problem — CLI-invoked PyInstaller without a spec
  resolves relative to cwd, which IS the repo root there.
- **None of this has run against a real signing key or real hardware** — needs the
  operator's actual minisign keypair + code-signing cert (not fabricated in a coding
  session) and a genuine `v*` tag release. The unsigned multi-arch build DOES validate
  for real on every push via `build`.

## Imports & paths (the main trap)

Src layout, zero-install:

- Imports are absolute under `wisp.*`. Don't reintroduce flat top-level imports.
- Nothing is installed. `apps/daemon/main.py`/`apps/central/main.py` prepend
  `<repo>/src` to `sys.path` themselves; the admin CLI needs `PYTHONPATH=src`; tests
  bootstrap their own path.
- `config.PROJECT_ROOT` is the repo root (`parents[2]` of `config.py`); `central_db`
  defaults to `data/central.db`; `central/server.py` resolves the dashboard SPA from
  `central/static/`.
- **The dashboard is a React/Tailwind/shadcn SPA (`web/`), not hand-rolled vanilla JS
  anymore** — see "Central dashboard" below for the full picture. `store.low_bandwidth_alarms`
  + `GET /api/summary` (org-scoped: uplink flag + fleet-wide bw-alarmed ports) and
  `store.data_version` + `GET /api/events` (SSE, org-scoped fingerprint) exist only to
  feed that look — the header's low-bandwidth chip and live refresh-on-change.
  `uplink_down` is still in `GET /api/summary`'s response (other tooling may read it) but
  the SPA no longer surfaces it anywhere — an operator viewing the dashboard at all is
  already evidence their own uplink isn't fully down, so a dedicated "uplink status"
  indicator was judged low-value clutter and dropped (`central/dispatch.py` still pages
  `UPLINK_DOWN` via ntfy the moment it happens, independent of anything in the SPA).
  The Outages triage queue (`store.triage_outages` + `GET /api/outages`,
  `POST /api/outages/acknowledge`/`postmortem`) and the Logs page
  (`store.list_events` + `GET /api/logs`, cursor-paginated on `id`) were added as a
  follow-up — status (`unassigned`/`in_progress`/`pending_postmortem`) is derived, never
  stored, from `acknowledged_at`/`resolved_at`/`root_cause`; a resolved outage without a
  `root_cause` stays in the queue for `postmortem_days` (default 30) so an operator can
  still log one. Recovery itself is never operator-driven — the FSM resolves outages on
  its own — so triage only ever offers acknowledge/postmortem, never a manual resolve.
  Ack/postmortem writes re-derive org from the outage row (`store.outage_org`),
  same discipline as `device_org`/`switch_port_org`, and are gated the same as
  every other dashboard write (owner or superadmin only). **The triage queue itself was
  later folded into the Home page** (`routes/home-page.tsx`, using the shared
  `components/outage-card.tsx`) rather than kept as its own `/outages` route — it's a
  small, bounded, action-oriented list (not a full history), so a dedicated page was one
  click of indirection with no payoff; there's no standalone Triage nav entry anymore.

## Engine invariants (don't break)

- `core/state_machine.py`'s `MonitorEngine` is **pure** — takes `{ip: PingResult}` + ts,
  returns committed states + `Event`s, no I/O. Central owns building/rehydrating it and
  persisting events (`central/engine.py`) — that separation is what makes the FSM
  unit-testable with no database. Don't put DB/network calls in the engine.
- **`process_cycle(results, ts, subset=None)` has two modes.** `subset=None` is the
  normal full pass (every device + canary/uplink edge + freeze). A `set[int]` runs a
  **confirmation pass**: advances only those FSMs one more sample (topological order
  preserved), skips canary/uplink logic, returns committed states for the subset only.
  Central's fast-confirm recheck uses this (`central/engine.py:run_cycle`'s `subset`
  param). Keep the full-pass path byte-identical.
- **`probe_plan()` is a reference the edge approximates, not something central calls.**
  The edge computes its own per-cycle ping counts client-side
  (`apps/daemon/main.py:_gentle_probe_plan`) since it has no local engine.
  `probe_plan()` is the "correct" behavior it mirrors — including counting a BACKUP
  parent edge as infra too (`effective_parents()`), which `_gentle_probe_plan` can't do
  yet since `GET /edge/devices`'s topology reply only carries `parent_device_id`, not
  backup edges. Known, low-priority gap.
- `central/dispatch.py`'s `CentralAlertDispatcher` does network sends OUTSIDE any DB
  transaction, then logs — a slow API call never holds a write lock.
- Prober/Notifier live behind small interfaces (`ingress/probers.py`,
  `egress/notifiers.py`) — `IcmpProber` (unprivileged ICMP via icmplib) and
  `NtfyNotifier` (ntfy push). `build_prober`/`build_notifier` are the swap point; keep
  new providers behind those interfaces.

## Scaling invariants (don't regress at fleet size)

- **Probe fan-out is bounded.** `_gather_pings` runs under
  `asyncio.Semaphore(cfg.probe_max_inflight)` (`WISP_MAX_INFLIGHT`, default 256). An
  unbounded `gather` opens one ICMP socket per device per tick — past `ulimit -n` the
  kernel refuses sockets and the generic-`Exception` guard masks it as 100% loss, i.e. a
  **fake mass outage exactly at peak fleet size**. Don't reintroduce unbounded fan-out.
- **Aggregation gear is probed gently.** `_gentle_probe_plan` mirrors
  `MonitorEngine.probe_plan()`: any device that's a parent gets `cfg.pings_per_poll_infra`
  (default 2), leaves + canary get `pings_per_poll` (5) — fewer echoes so the box's ICMP
  rate-limiter doesn't read as phantom loss.
- **Fast-confirm is central-driven, not edge-timed.** `central/engine.py`'s
  `compute_recheck` names suspect IPs (down streak started but unconfirmed, or recovery
  streak started but unconfirmed) in the `/report` reply; the edge's `_follow_recheck`
  re-probes just those IPs every `WISP_RETRY_INTERVAL_S` and reports back
  (`mode="recheck"`) until the hint is empty — self-terminates the moment a streak
  commits or resets. A frozen cycle (canary down) never yields a hint.
- **Adaptive cadence is fleet-size-derived, opt-in.** `Config.effective_interval
  (device_count)` returns `poll_interval_small_s` (30) while fleet `<= small_fleet_max`
  (1000) and `poll_interval_adaptive` is on (`WISP_POLL_INTERVAL_ADAPTIVE`), else
  `poll_interval_s` (60). Off by default; `run_forever_central_brain` computes it once
  at startup and doesn't retune mid-run. Detection latency floor is `interval ×
  down_consecutive`, though fast-confirm usually beats that.

## Central dashboard (React + Tailwind + shadcn/ui)

- **Source lives in `web/` (Vite + React 18 + TypeScript + Tailwind v4 + shadcn/ui,
  react-router, @tanstack/react-query), builds straight into `central/static/`.**
  `web/vite.config.ts`'s `build.outDir` points at `../src/wisp/central/static` with
  `emptyOutDir: true` — `cd web && npm install && npm run build` regenerates it after any
  frontend change. The built output is committed to git like any other static asset (not
  gitignored) so `./run.sh` keeps working with zero Node dependency for anyone not
  touching the frontend; only `web/node_modules` is ignored. `server.py`'s
  `_STATIC`/`_serve_static` are completely unchanged — they just hand out whatever's in
  `central/static/`, same as when it was hand-rolled vanilla JS.
- **Routing is `HashRouter` (`/#/topology`, `/#/nodes`, …), not `BrowserRouter`.**
  `server.py`'s static handler 404s on any path that isn't a literal file — there's no
  SPA-fallback route, deliberately, to keep the stdlib server dumb. `BrowserRouter` needs
  the server to serve `index.html` for unknown paths (deep-link/refresh would 404);
  `HashRouter` needs no server cooperation at all since the fragment never reaches the
  server. Don't switch back without adding that fallback to `server.py` first.
- **Theme (`web/src/index.css`) is a hex-for-hex port of the "WISP Central Mobile"
  design mockup's `themeTokens()`** (dark default — `bg #181614`/`accent #da7756` — plus
  a light variant), not the old dashboard's gray Material palette. CSS variables follow
  shadcn's convention (`--background`/`--card`/`--primary`/`--border`/…) with
  `--success`/`--warning`/their `-soft` variants layered on top for status coloring;
  toggled via a `dark` class on `<html>`, persisted to `localStorage`
  (`web/src/lib/theme.ts`), applied before first paint in `main.tsx` to avoid a flash.
- **Auth rides the exact same session-cookie plane as before** — `central/auth.py` is
  untouched. `web/src/hooks/use-auth.tsx`'s `AuthProvider` wraps `GET /api/me`/
  `POST /api/login`/`POST /api/logout` over same-origin `fetch` (cookie sent
  automatically); a 401 anywhere dispatches a `wisp:unauthorized` window event the
  provider listens for. Org scoping (`scopeOrg`) mirrors the old dashboard exactly:
  a superadmin picks an org via the header switcher (persisted to `localStorage`, GET
  `/api/orgs`); an org user is always pinned to their own org and never shown a
  picker, matching `server.py`'s `_scope_org`.
- **Live updates reuse the same `GET /api/events` SSE stream** —
  `web/src/hooks/use-event-stream.ts` opens one `EventSource` per org scope and
  invalidates react-query cache keys (`summary`/`outages`/`inventory`/`logs`/`team`/
  `attendance`) on each `changed` event, instead of the old dashboard's manual
  `refresh()`.
- **One backend change this needed:** `store.list_org_devices()` now LEFT JOINs
  `device_states` so the topology tree and home page can color a device without a
  second round trip per device — nullable `state`/`latency_ms`/`packet_loss`/
  `jitter_ms`/`state_updated_at` on each row, null until that device's first report
  lands. Read-only convenience join, not a new stored column — no `_ensure_columns`
  migration needed.
- **The mockup this was built from was a fully mocked design-doc, not a spec** — some of
  its fake features have no backend equivalent and don't exist in the real app: "Clients
  online" (platform only monitors infra, not end-user devices), a manual "Resolve"
  action (recovery is FSM-automatic, never operator-driven — only acknowledge +
  post-resolve postmortem are real), a Docker install option (only Linux/Windows
  installers exist), a Topology "Map" view (no device coordinates in the schema), and
  "Notification history" (`alert_log` is internal-only, no dashboard route reads it).
  Don't go looking for them or try to "finish" them without a real backend underneath.
- **No frontend test suite yet.** Verified during development via `tsc --noEmit`, a
  production `npm run build`, and manual Playwright screenshots/interaction against a
  live central instance (not committed — was throwaway verification tooling). The
  Python suite (`tests/`) only needed a one-line extension for the `list_org_devices`
  change above and is otherwise unaffected by any of this.

## Central management plane — device inventory, team, settings

- **`org_devices` and `devices` are TWO DIFFERENT TABLES.** `devices` (legacy naming) is
  the edge-ingest global id map: rows exist only when an edge has reported an event for
  them, keyed `(org_id, node_id, edge_local_id)`. `org_devices` is the ISP-managed
  topology an org builds by hand — exists before/independent of any edge, own id space,
  what central-brain's engine runs against. `GET /api/devices` = legacy registry
  (read-only, no CRUD, stays empty in practice since central-brain is the only mode).
  `GET/POST /api/inventory*` = the org-managed topology, what the dashboard's **Nodes**
  page uses (a DIFFERENT "Nodes" than **Edge Nodes** — device topology vs. physical
  probe enrollment). Adding a field to the wrong one is the classic mistake.
- **`central/inventory.py` is pure validation, no storage.** `clean_device_payload`'s
  `parents` map must already be scoped to one org by the caller
  (`CentralStore.org_device_parent_map`), so a cross-org id just looks like "parent
  does not exist" rather than needing an explicit check. `clean_backup_link`'s cycle
  check walks the FULL edge set (primary + existing backups).
- **Every `org_devices` write re-derives the org from the DB row, not the request
  body**, via `store.device_org(id)` — a body's `org_id` is only trusted for
  *create*. `_can_write(user, org)` gates on the derived org.
  `switch_ports` writes follow the same pattern via `store.switch_port_org(id)`;
  `feeds` also checks the target device is in the SAME org. `/api/inventory/links`
  derives org from `child_id` and checks `parent_id` is in that same org.
- **`/api/orgs` must stay org-filtered** — same `_scope_org` every other route
  uses (pinned for org users, optional `?org=` for superadmin).
- **`orgs.ntfy_topic_owner/operator/tech`** (per-role outage routing, customer-set) are
  separate from **`orgs.ntfy_topic`** (fleet-watchdog's `NODE_STALE`/`NODE_OK`,
  platform-operational) — don't merge them.
- **New `orgs`/`org_devices`/`switch_ports` columns need the in-code migration**, not
  just `CREATE TABLE IF NOT EXISTS`. `CentralStore.__init__` runs
  `_ensure_columns(conn, table, coldefs)` right after `executescript` — add new columns
  there or an existing `central.db` keeps the old schema. A brand-new TABLE needs no
  such migration.
- **`central/server.py`'s dashboard writes send real pushes (`/api/test-alert`), so
  `make_server`/`_make_handler` take an injectable `notifier`** (defaults to
  `build_notifier(cfg)`) — tests inject a recording double, no real network. Follow this
  pattern for anything else central sends.
- **`GET /api/analytics?days=`** is outage-history math, no new storage —
  `central/analytics.py:device_reliability` reads `store.outages_in_window`,
  org-scoped. Reports every ACTIVE configured device, not just ones with an outage
  (a clean device shows 100% uptime). UNREACHABLE outages excluded from downtime — a
  topology-suppressed child isn't "unreliable" on its own account.
- **Tests:** `unit/test_central_inventory`, `integration/test_central.OrgDevicesTest`,
  `integration/test_central_auth`, `integration/test_central_analytics`.

## Central runs the brain (the only edge mode)

- **`core/state_machine.MonitorEngine` is reused UNCHANGED — only its DB glue is
  central-native.** `central/engine.py`'s `load_device_meta`/`build_engine`/
  `apply_events` build/rehydrate/persist against `org_devices`/`device_states`/
  `outages`. `central/dispatch.py`'s `CentralAlertDispatcher` is the alerting policy
  (dedupe-per-outage, owner+operator on open, all-three on hourly escalation and
  resolve, ack-doesn't-stop-only-recovery-does).
- **`EngineRegistry` exists because central's HTTP handling is stateless per-request but
  flap-suppression counters are NOT.** One HTTP request feeds the engine ONE sample; a
  `down_streak` must accumulate across an edge's successive `/report` calls.
  `EngineRegistry` holds one live `MonitorEngine` per org in memory, rebuilding a
  org's engine only when its topology fingerprint changes (`(id,
  parent_device_id, d.parents)` — `d.parents` covers backup-link add/remove too). A
  fresh/rebuilt engine rehydrates from `device_states` (restart-safe). One registry per
  server process, threaded into `_make_handler` alongside `notifier`.
- **The wire format is IP-keyed, not device-id-keyed.** `POST /report` body:
  `{"v":1,"org_id":…,"node_id":…,"ts":…,"mode":"full"|"recheck","pings":{"<ip>":
  {"loss_pct":…,"latency_ms":…,"jitter_ms":…}}}` — the edge never needs central's device
  ids, only which IPs to probe (from `GET /edge/devices`). A `"recheck"` report carries
  only the suspect IPs named in a prior reply; `_follow_recheck` keeps following until
  empty (round cap is a safety net, not the normal termination path).
- **Escalation sweeping rides the edge's report cadence, not a background timer.**
  `CentralAlertDispatcher.sweep(ts)` runs once per full `/report` (not recheck — timing
  is due-at-gated and idempotent, no need to re-check that often), scoped to that
  org's due `escalations`. Accepted tradeoff: escalations for an org stop advancing
  if its edge goes fully stale — the fleet watchdog (`central/watchdog.py`) separately
  pages for that, a different alarm.
- **The daemon has exactly one mode.** `main()` unconditionally runs
  `run_forever_central_brain` behind a `SingleInstance` lock
  (`<db_path>.central-brain.lock` — central-brain makes zero local DB writes; `db_path`
  is just a per-node data-directory anchor now). Fetches topology every cycle (a fetch
  hiccup keeps the last-known set), probes via `_gather_pings`/`build_prober`
  (incl. `_gentle_probe_plan`), `POST /report`s raw results, follows any `recheck` hint.
  Central's `central/engine.py` + `central/dispatch.py` do 100% of detecting/paging.
- **SNMP port folding is wired end to end.** The edge walks snmp-enabled `org_devices`
  (config travels on `GET /edge/devices`'s reply) on its own slow cadence
  (`cfg.snmp_interval_s`, default 90s, independent of `poll_interval_s`), via
  `_gather_snmp_ports`, attached to the SAME full `POST /report` under a `ports` key
  (never a recheck report). A dead switch is isolated per-device, never sinks the ICMP
  cycle. `central/ports.py:CentralPortMonitor` (run after the ICMP cycle commits) writes
  `switch_ports`: monitored-only, admin-down silent (reuses `PortStatus.is_down()`
  verbatim), one alarm not two (a monitored port-down folds into the open outage via
  `stamp_outage_cause` — COALESCE, never clobbers a post-mortem — instead of a
  competing alarm; no open outage = leading-indicator heads-up; SNMP never opens an
  outage itself). A device id in `ports` not in the reporting org's `eng.meta` is
  silently ignored. Operator-only page, gated by `cfg.snmp_alerts`; state always
  written. **Bandwidth is wired too:** 64-bit octet counters diffed by
  `throughput_bps` into a live rate; a MONITORED port below its per-port threshold
  (`bw_threshold_mbps`/`bw_direction`) for `cfg.snmp_bw_consecutive` walks alarms its own
  streak (`bw_low_streak`/`bw_alarm`, separate from port-down). Never judged on a
  down/admin-down port; clears silently if the port goes down (no confusing chaser).
  Gated by `cfg.snmp_bw_alerts`.
- **Historical rollups/trend, two slices.** `central/analytics.py:device_reliability`
  (`GET /api/analytics?days=`) — pure outage-history math, no new storage.
  `central/rollup.py` (`GET /api/analytics/trend?device_id=&days=`) — hourly buckets,
  30-day retention (both platform-wide, not per-org). `record_cycle` folds off
  `_report`'s per-device samples on every full report (never recheck — would skew an
  hour's average) as running sums, averaged at read time.
  `start_central_rollup_prune_thread` runs a daily sweep alongside the fleet watchdog.
- **Per-link performance baseline.** `central/perf.py` reuses `core/baseline.py`'s pure
  median+MAD math verbatim. `device_perf_samples` is a bounded per-(org,device) ring
  buffer (insert then trim to `cfg.perf_window`), deliberately NOT the hourly rollup
  storage — an hourly average would smear the intra-hour slowdown this tier exists to
  catch. `record_and_evaluate` runs once per full-report cycle, persists the badge
  (`device_perf`, restart-safe, clears silently on hard-DOWN). Operator-only page on
  enter/leave edge, gated by `cfg.perf_alerts`.
- **On-backup redundancy needed ZERO engine changes.** `MonitorEngine` already computed
  `CycleResult.redundancy` generically (`DeviceMeta.effective_parents()`). The work was
  wiring the extra edge: `org_device_links` (org-scoped, `kind='backup'`),
  `clean_backup_link` (full-edge-set loop check), `load_device_meta` populating
  `DeviceMeta.parents` from backup edges. **`EngineRegistry`'s fingerprint includes
  `d.parents`, not just `parent_device_id`** — a backup add/remove is a topology change
  like any other. `central/redundancy.py:sweep` persists the badge every full cycle,
  pages the operator once on enter/leave, never opens an outage; a hard-DOWN node clears
  its badge silently. Gated by `cfg.backup_alerts`.
- **Every soft-signal tier this platform's edge ever had now exists on central** — SNMP
  status+bandwidth, SLA reporting, latency/loss trend, perf baseline, redundancy.
  Nothing deferred at the detection-tier level; see `docs/ARCHITECTURE.md`'s "Roadmap"
  for remaining groundwork.
- **Tests:** `integration/test_central_brain.py` (`CentralEngineTest`,
  `CentralAlertDispatcherTest`, `ReportEndpointTest` — full `/report`+`/edge/devices`
  over a real socket, recheck round trip, SNMP folding, trend rollup, redundancy),
  `integration/test_daemon_central_brain.py` (edge probe loop end to end), plus
  `test_central_ports.py`/`test_central_redundancy.py`/`test_central_perf.py` for their
  respective sweeps.

## Reliability invariants (the "trust the alarm" set — don't regress)

- **One logical probe per org/node.** Each daemon holds its own OS advisory lock
  (`runtime/single_instance.py`) and exits (code 3) if another holds it. Two probes
  would double-report, wasteful but harmless since central's per-outage dedupe
  (`store.open_outage_if_absent`, `WHERE NOT EXISTS`) is idempotent.
- **A page must not vanish to a blip.** `NtfyNotifier.send` retries via
  `send_with_retry(attempt, attempts, backoff, sleep)`: network/timeout/5xx are
  retryable (exponential backoff), 4xx fails fast. Tunable via `WISP_NTFY_RETRIES` /
  `WISP_NTFY_RETRY_BACKOFF_S`. Test the helper with a fake attempt/sleep, no httpx.
- **The probe loop never dies on one bad cycle.** `run_forever_central_brain` wraps
  each cycle's `run_cycle_central_brain` in try/except that logs and continues. Keep
  new per-cycle work inside that guard. `_gather_pings` swallows per-probe errors but
  re-raises a `RuntimeError` config/permission failure loudly.
- **Cross-edge fleet watchdog is central's job.** `central/watchdog.py`'s
  `CentralWatchdog.check(now)` pages a node's org when its heartbeat is stale, restart-
  safe and transition-only. No edge-side dead-monitor watchdog.

## Config (env-var only)

- **Every tunable is a field on the frozen `Config` dataclass** (`config.py`), read once
  from a `WISP_*` env var at process start. No DB settings layer either side.
- **`Config` is shared between edge and central processes** — grep both `apps/daemon/`
  and `src/wisp/central/` before deleting/renaming a field (`escalate_every_min`,
  `session_timeout_h`, `canary_ip`, `retry_interval_s` all look edge-only in isolation
  but are read by central too).
- **Topology, team, alert routing, node enrollment credentials are live in central's
  dashboard, not env vars.** Only process-level tunables are `WISP_*` — see `README.md`.
- **`db_path` (`WISP_DB`) is not a database anymore** — just where the single-instance
  lock file and the supervisor's transient download/update files live.
- **Edge→central ingest auth is any ONE of three:** the global bearer token
  (`WISP_CENTRAL_TOKEN`), a self-service per-node token (see below), or mTLS (below).
  Plus central's own dashboard session secret (`central/auth.py`, a file under `data/`,
  0600). No PIN — central uses per-user accounts.

## Self-service node enrollment

- **An ISP owner/operator registers a node from the dashboard's "Edge Nodes" tab** (not
  "Nodes" — that's device topology). `POST /api/nodes` issues a credential shown once,
  `/api/nodes/rotate` replaces it, `/api/nodes/revoke` deactivates it. Third option
  alongside `central.admin enroll-edge` (mTLS) or the shared `WISP_CENTRAL_TOKEN`.
- **Only a SHA-256 hash of the token is ever stored** (`node_tokens` table) — plaintext
  shown once at issue time, never retrievable, only rotatable. A fast hash is fine since
  the token is already ~256 bits of generated entropy (`secrets.token_urlsafe(32)`).
- **The token rides the EXACT SAME `Authorization: Bearer` header** the edge already
  sends — zero client changes. `central/server.py`'s `_ingest_ok(org, node)` tries
  the global token, then a self-service token (`_node_token_identity` →
  `store.resolve_node_token`, deriving identity FROM the credential, never trusting the
  envelope's claim alone), then a verified mTLS cert. Any one satisfies it.
- **A node that HAS registered its own credential is gated on presenting it**, even with
  neither the global token nor mTLS configured — `store.node_token_registered(org,
  node)` is a hard "credential required" gate before falling back to the open
  trusted-network default. An UNREGISTERED node still gets that open default.
- **`clean_node_id`** validates the id (1–64 chars, starts letter/digit, then
  letters/digits/`.`/`_`/`-`) — deliberately boring since it becomes a systemd identity,
  a path segment under `/etc/wisp`, and a bare wire value.
- **Tests:** `integration/test_central.NodeTokenTest`,
  `integration/test_central_node_enrollment.py`.

## mTLS enrollment (replaces the bearer-token-only stopgap)

- **`central/pki.py` shells out to `openssl`** rather than adding `cryptography` as a
  dependency — cert issuance is a one-time admin-CLI operation, not per-request, so it
  doesn't need to live in central's pure-stdlib request path (verification at request
  time uses only stdlib `ssl`). `openssl` needs to be on the admin CLI box's PATH;
  `PkiError` gives a clear message if missing.
- **Identity is CN-encoded, not a new wire field.** An edge's client cert CommonName is
  `org_id:node_id` (`pki.edge_common_name`/`pki.peer_identity`) — central decodes it
  off the verified socket, so `/report`'s JSON is unchanged.
- **The bearer token, a self-service token, or a verified matching cert — any one
  satisfies ingest auth**, none required. `_peer_identity()`'s cert CN must match the
  CLAIMED org (and node, on routes that have one — `/edge/devices` is org-only).
  If none of the three is configured, ingest stays fully open (unchanged default).
- **Central terminates TLS itself when configured — stdlib `ssl`, no new dependency.**
  `make_server` wraps the listener only when `WISP_CENTRAL_TLS_CERT`/`_KEY` are BOTH
  set; otherwise plain HTTP as always. `WISP_CENTRAL_CLIENT_CA` independently turns on
  `CERT_OPTIONAL` verification (requested, not required — dashboard browsers still
  connect fine with no cert) once TLS is on. A terminator (nginx/Caddy) in front is
  still valid too.
- **The TLS handshake happens inside each request's own worker thread**, not the shared
  accept loop — `_TLSThreadingHTTPServer` overrides `finish_request` (not
  `get_request`), so one client's slow/failed handshake can't stall new connections.
  `handle_error` logs `ssl.SSLError` quietly (routine scanner/stale-client noise on an
  internet-facing ingest port) but lets any other exception fall through loudly.
- **`central.admin init-ca --host <name-or-ip>`** creates the CA (idempotent) + central's
  own server cert, `--host` (repeatable) becomes the SAN. **`enroll-edge --org
  --node`** issues one client cert per edge off that CA. Both print the exact env vars
  to set on each side.
- **No CRL or cert rotation tooling yet.** Revoking a compromised edge cert means
  rotating the CA. Acceptable at today's fleet size; real CRL/rotation is future work.
- **Tests:** `unit/test_central_pki` (skipped if `openssl` not on PATH),
  `integration/test_central_mtls` (real TLS socket via `make_server`).

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2,
  recovery = 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override
  applied in `MonitorEngine.process_cycle` after `feed()`. Don't regress these counts
  (fast-confirm changes when the samples arrive, not how many).
- **Topology order:** devices process parent-before-child (`_topological_order`) so a
  parent's new state is known when evaluating children.
- **No automatic cause inference.** Cause is only ever an operator-entered post-mortem
  at resolution — don't reintroduce an inferred cause.
- **Escalation model:** a fresh DOWN pages owner+operator immediately
  (`CentralAlertDispatcher._on_open`). Dedupe is **per-outage** (was there already a
  `sent` row for this outage id?), not a time window. Also queues one `escalations` row
  (kind `"hourly"`, due at `now + cfg.escalate_every_min`, default 60). Each `sweep`
  that finds it due while still open fires an all-hands broadcast (owner+operator+tech)
  and **reschedules the same row**. Acknowledgement does NOT stop this; only recovery
  does.
- **Escalations are DB-derived** (`escalations.due_at` + sweeper), not in-memory timers.
  `UNIQUE(outage_id, kind)` keeps them idempotent.
- **Restart safety:** `EngineRegistry` rehydrates each FSM from `device_states`;
  breaking that re-pages everyone on restart.
- **Timestamps:** poll/outage stamps are ISO8601 `+00:00`; SQLite `datetime('now')`
  (acks) is space-separated naive. `core/analytics._parse` normalises both to naive
  UTC — reuse it (also used by `central/rollout.py`/`central/watchdog.py`).
- **Schema changes:** central's own schema (`central/store.py`'s `_SCHEMA` +
  `_ensure_columns`) is the only schema now — see "Central management plane" for the
  column-migration convention.

## Tests

`python -m unittest discover -s tests` (310 tests) after any logic change. Tests inject
a recording notifier/client double where a real network call would otherwise be
needed — no real ntfy/central network in the suite. Per-area test files are named
throughout this doc next to the invariant they cover; the rest: `unit/test_state_machine`
(FSM + overrides + `probe_plan` + subset confirmation + adaptive cadence),
`unit/test_baseline` (perf-deviation math), `unit/test_snmp` (SNMP parser/throughput,
incl. `is_down()`), `unit/test_supervisor` (self-update logic),
`integration/test_daemon` (`_gather_pings` concurrency/error policy),
`integration/test_notifiers` (`send_with_retry`), `integration/test_single_instance`
(the OS lock).
