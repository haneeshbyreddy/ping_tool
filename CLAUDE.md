# CLAUDE.md

Conventions, invariants, and gotchas that aren't obvious from the code. For
what/how/layout/config, read `README.md`. For design rationale, the roadmap, and the
list of deliberately-deleted single-box-era code (`apps/dashboard/`, `src/wisp/server/`,
the old vanilla-JS dashboard — gone, not deprecated), read `docs/ARCHITECTURE.md`.

Verify claims about what's done against the code, not this file — stale docs drift.

## Architecture at a glance

Central runs the brain for every org. The edge is a thin probe — one daemon mode
(pointed at central by `WISP_CENTRAL_URL`, mandatory): fetches topology from central,
probes real ICMP under bounded fan-out, reports raw per-IP samples, heartbeats its
build version (the self-update channel). No local DB, dashboard, PIN, or FSM on the
edge.

Central owns the FSM, topology-aware suppression, fast-confirm detection, the alerting
ladder, the multi-org dashboard, and fleet version/rollout state. ISPs log in per-org,
manage topology/team/alert routing, and self-register the nodes they run (one or many,
each with its own enrollment credential).

Central's server (`central/server.py`/`store.py`/`auth.py`/…) is pure stdlib. Its
dashboard is a build-time SPA (React/TS/Tailwind v4/shadcn, source in `web/`, built into
`central/static/`) — Node is dev-only, the deployed artifact is static assets the stdlib
server hands out unchanged. Edge needs a `.venv` (`requirements.txt`: `icmplib`/`httpx`;
never install globally, system Python is PEP 668-locked) + the kernel ping group
(`sysctl net.ipv4.ping_group_range="0 2147483647"`).

## Imports & paths (the main trap)

Src layout, zero-install:

- Imports are absolute under `wisp.*`. Don't reintroduce flat top-level imports.
- Nothing is installed. `apps/daemon/main.py`/`apps/central/main.py` prepend `<repo>/src`
  to `sys.path`; the admin CLI needs `PYTHONPATH=src`; tests bootstrap their own path.
- `config.PROJECT_ROOT` = repo root (`parents[2]` of `config.py`); `central_db` defaults
  to `data/central.db`; `central/server.py` resolves the SPA from `central/static/`.

## Engine invariants (don't break)

- `core/state_machine.py`'s `MonitorEngine` is **pure** — takes `{ip: PingResult}` + ts,
  returns committed states + `Event`s, no I/O. Central owns building/rehydrating/
  persisting (`central/engine.py`). Don't put DB/network calls in the engine.
- **`process_cycle(results, ts, subset=None)` has two modes.** `None` = normal full pass
  (every device + canary/uplink + freeze). A `set[int]` = confirmation pass: advances
  only those FSMs one sample (topo order preserved), skips canary/uplink, returns the
  subset's states. Central's fast-confirm uses this. Keep the full-pass path
  byte-identical.
- **`probe_plan()` is a reference the edge approximates, not something central calls.**
  The edge computes its own per-cycle ping counts (`apps/daemon/main.py:_gentle_probe_plan`).
  Known gap: `probe_plan()` counts a BACKUP parent as infra (`effective_parents()`);
  `_gentle_probe_plan` can't yet — `GET /edge/devices` only carries `parent_device_id`.
- `central/dispatch.py`'s `CentralAlertDispatcher` does network sends OUTSIDE any DB
  transaction — a slow API call never holds a write lock.
- Prober/Notifier live behind interfaces (`ingress/probers.py`, `egress/notifiers.py`):
  `IcmpProber` (icmplib), `NtfyNotifier` (ntfy). `build_prober`/`build_notifier` are the
  swap point; keep new providers behind them.
- **Windows probes via `SingleSocketIcmpProber`, never icmplib** (`build_prober` picks by
  `sys.platform`; `WISP_PROBER=singlesock|icmplib` forces one for A/B on a box). Windows
  raw sockets are promiscuous — N icmplib sockets each see every inbound reply (O(N²))
  and asyncio stamps arrival when the coroutine finally reads the socket, so RTT measured
  event-loop queue-wait (~150ms floor, jitter ≈ latency, while ping.exe read <1ms). The
  fix: ONE shared raw socket + ONE receiver thread whose first act after `recvfrom` is
  `time.perf_counter()`, matching by ICMP id (pid-derived, filters other pingers) + seq +
  reply source IP. Linux keeps icmplib's unprivileged datagram sockets (kernel demuxes
  per socket, the fan-in never existed there; a raw socket would need root and break the
  ping-group invariant). Loss semantics and `RuntimeError`-on-missing-privilege are
  unchanged. Tests: `unit/test_probers` (fake socket via `sock_factory`, no network).

## Scaling invariants (don't regress at fleet size)

- **Probe fan-out is bounded** by `asyncio.Semaphore(cfg.probe_max_inflight)`
  (`WISP_MAX_INFLIGHT`, 256). Unbounded `gather` opens one ICMP socket per device per
  tick — past `ulimit -n` the kernel refuses sockets and the generic-`Exception` guard
  masks it as 100% loss = a fake mass outage at peak fleet size. Don't reintroduce it.
- **Aggregation gear is probed gently.** `_gentle_probe_plan` mirrors `probe_plan()`: a
  parent gets `cfg.pings_per_poll_infra` (2), leaves + canary get `pings_per_poll` (5) —
  fewer echoes so the box's ICMP rate-limiter doesn't read as phantom loss.
- **Fast-confirm is central-driven.** `central/engine.py:compute_recheck` names suspect
  IPs (streak started but unconfirmed) in the `/report` reply; the edge's
  `_follow_recheck` re-probes just those every `WISP_RETRY_INTERVAL_S`, reports
  `mode="recheck"` until the hint is empty. A frozen cycle (canary down) yields no hint.
- **Adaptive cadence is fleet-size-derived, opt-in.** `Config.effective_interval
  (device_count)` returns `poll_interval_small_s` (30) while fleet `<= small_fleet_max`
  (1000) and `poll_interval_adaptive` is on, else `poll_interval_s` (60). Off by default;
  computed once at startup, not retuned mid-run. Latency floor = `interval ×
  down_consecutive`, though fast-confirm usually beats it.

## Central runs the brain (the only edge mode)

- **`MonitorEngine` is reused UNCHANGED — only its DB glue is central-native.**
  `central/engine.py`'s `load_device_meta`/`build_engine`/`apply_events` build/rehydrate/
  persist against `org_devices`/`device_states`/`outages`. `central/dispatch.py` is the
  alerting policy (dedupe-per-outage, owner+operator on open, all-three on escalation/
  resolve, ack-doesn't-stop-only-recovery-does).
- **`EngineRegistry` holds one live engine per org** because HTTP handling is stateless
  per-request but flap-suppression counters are not — a `down_streak` accumulates across
  successive `/report` calls. Rebuilds an org's engine only when its topology fingerprint
  `(id, parent_device_id, d.parents)` changes (`d.parents` covers backup links too);
  rehydrates from `device_states` (restart-safe). Breaking rehydration re-pages everyone
  on restart. One registry per process, threaded into `_make_handler` with `notifier`.
- **Wire format is IP-keyed.** `POST /report`: `{"v":1,"org_id":…,"node_id":…,"ts":…,
  "mode":"full"|"recheck","pings":{"<ip>":{"loss_pct":…,"latency_ms":…,"jitter_ms":…}}}`
  — the edge never needs central's device ids. A `"recheck"` report carries only the
  suspect IPs from a prior reply.
- **Escalation sweeping rides the report cadence, not a timer.** `sweep(ts)` runs once
  per full `/report` (not recheck), scoped to that org's due `escalations`. Tradeoff:
  escalations stall if an edge goes fully stale — the fleet watchdog pages for that
  separately.
- **The daemon has exactly one mode.** `main()` runs `run_forever_central_brain` behind a
  `SingleInstance` lock. Fetches topology every cycle (a hiccup keeps last-known),
  probes, reports, follows recheck hints, then heartbeats. `central/engine.py` +
  `dispatch.py` do 100% of detecting/paging.
- **The heartbeat is the self-update channel, not the liveness signal.**
  `_send_heartbeat` (per full cycle, skipped on finite `--cycles` runs) POSTs version/
  platform; the reply may carry an `update` directive (`central/rollout.py:
  directive_for`), which the daemon writes ATOMICALLY as `update_request.json` next to
  the lock file — the supervisor consumes it and owns download→verify→swap→health-gate→
  rollback. Liveness stays `touch_node` off `/report`. A failed heartbeat is a warning,
  never a crashed cycle.
- **SNMP port folding, end to end.** Edge walks snmp-enabled `org_devices` on its own
  slow cadence (`cfg.snmp_interval_s`, 90s) via `_gather_snmp_ports`, attached to the
  full `POST /report` under `ports` (never recheck). **The sweep is a BACKGROUND asyncio
  task, never inline in the probe cycle** — inline re-serializes walks and a few
  SNMP-deaf switches burn ~10 columns × `snmp_timeout_s` inside the ICMP cycle (the "edge
  reports every 4 minutes" bug). Walks run `cfg.snmp_max_inflight` (4) at a time, each
  capped at `cfg.snmp_walk_timeout_s` (20s); keep the ICMP report path free of any await
  on SNMP. **One `SnmpEngine` per poller instance, NEVER one per walk** — an engine
  registers its UDP transport with the event loop, so a per-walk engine is never GC'd:
  ~1 MiB RSS + one socket FD leaked per walk, forever (the "edge RAM keeps climbing"
  bug, 2026-07; FD exhaustion then reads as a fake mass outage). Both `PysnmpPoller`
  and `PysnmpGponPoller` lazily create and reuse `self._engine`; concurrent walks on
  the shared engine are safe (request-id demux). `EngineReuseTest` in `unit/test_snmp`
  + `unit/test_gpon` pins this (skipped without pysnmp). `central/ports.py:CentralPortMonitor` writes `switch_ports`: monitored-only,
  admin-down silent (`PortStatus.is_down()`), one alarm not two (a port-down folds into
  the open outage via `stamp_outage_cause` COALESCE, never clobbers a post-mortem; no
  open outage = leading-indicator heads-up; SNMP never opens an outage). Operator-only,
  gated by `cfg.snmp_alerts`; state always written. **Bandwidth, floor AND ceiling:**
  octet counters diffed by `throughput_bps`; a monitored port below its floor
  (`bw_threshold_mbps`) for `cfg.snmp_bw_consecutive` walks alarms (`bw_alarm`),
  independently above its ceiling (`bw_max_mbps`) alarms (`bw_high_alarm`). Both optional
  per port, same `/api/inventory/ports/bandwidth` payload (`max_mbps > threshold_mbps`
  when both set); never judged on a down port, clear silently if the port goes down.
  Gated by `cfg.snmp_bw_alerts`.
- **Historical rollups, two slices.** `central/analytics.py:device_reliability`
  (`GET /api/analytics?days=`) — pure outage-history math, no new storage, reports every
  active device (clean = 100%), UNREACHABLE excluded. `central/rollup.py`
  (`GET /api/analytics/trend?device_id=&days=`) — hourly buckets, 30-day retention;
  `record_cycle` folds per-device samples off every full report as running sums.
  `start_central_rollup_prune_thread` prunes daily.
- **Per-link perf baseline.** `central/perf.py` reuses `core/baseline.py`'s median+MAD.
  `device_perf_samples` is a bounded per-(org,device) ring buffer (NOT the hourly rollup
  — an hourly average smears the intra-hour slowdown this catches). `record_and_evaluate`
  runs per full cycle, persists the badge (`device_perf`, restart-safe, clears on
  hard-DOWN). Operator-only, gated by `cfg.perf_alerts`. Exposed read-only at
  `GET /api/inventory/perf/samples?device_id=` for the Network row's sparkline.
- **On-backup redundancy needed ZERO engine changes** — `MonitorEngine` already computes
  `CycleResult.redundancy` via `DeviceMeta.effective_parents()`. Wiring: `org_device_links`
  (`kind='backup'`), `clean_backup_link` (full-edge-set cycle check), `load_device_meta`
  populating `DeviceMeta.parents`. `central/redundancy.py:sweep` persists the badge per
  cycle, pages on enter/leave, never opens an outage, clears on hard-DOWN. Gated by
  `cfg.backup_alerts`.
- **Tests:** `integration/test_central_brain.py`, `test_daemon_central_brain.py`,
  `test_central_ports.py`, `test_central_redundancy.py`, `test_central_perf.py`.

## Central management plane — inventory, team, settings

- **`org_devices` is THE device table** — the ISP-managed topology (what the engine runs
  against): `GET/POST /api/inventory*`, the Network page's *Devices* section (its
  *Probes* section is node enrollment). The single-box era's `devices`/`rollups` legacy
  ingest tables and `POST /ingest` are DELETED (2026-07), not dormant — don't
  reintroduce a second device registry. `events` survives them: central-originated log
  lines only (`_insert_org_event`, `node_id='central'`).
- **`central/inventory.py` is pure validation, no storage.** `clean_device_payload`'s
  `parents` map is pre-scoped to one org by the caller (`org_device_parent_map`), so a
  cross-org id just looks like "parent does not exist". `clean_backup_link`'s cycle check
  walks the FULL edge set.
- **Every `org_devices` write re-derives org from the DB row, not the body**, via
  `store.device_org(id)` (body `org_id` trusted only on *create*); `_can_write(user,
  org)` gates on it. `switch_ports` → `store.switch_port_org(id)`; `feeds` also checks
  same-org; `/api/inventory/links` derives from `child_id`, checks `parent_id` same org.
- **`/api/orgs` must stay org-filtered** — same `_scope_org` (pinned for org users,
  optional `?org=` for superadmin).
- **`orgs.ntfy_topic_owner/operator/tech`** (per-role outage routing, customer-set) are
  separate from **`orgs.ntfy_topic`** (fleet-watchdog `NODE_STALE`/`NODE_OK`) — don't merge.
- **New `orgs`/`org_devices`/`switch_ports` columns need the in-code migration** —
  `CentralStore.__init__` runs `_ensure_columns(conn, table, coldefs)` after
  `executescript`; add there or an existing `central.db` keeps the old schema. A brand-new
  TABLE needs no migration.
- **Dashboard writes send real pushes** (`/api/test-alert`), so `make_server`/
  `_make_handler` take an injectable `notifier` (default `build_notifier(cfg)`); tests
  inject a recording double. Follow this for anything central sends.
- **Tests:** `unit/test_central_inventory`, `integration/test_central.OrgDevicesTest`,
  `test_central_auth`, `test_central_analytics`.

## Central dashboard (React + Tailwind + shadcn/ui)

- **Source in `web/`, builds into `central/static/`.** `vite.config.ts`'s `build.outDir`
  = `../src/wisp/central/static` + `emptyOutDir` — `cd web && npm install && npm run
  build` regenerates it. Built output is committed to git (only `web/node_modules`
  ignored) so `./run.sh` works with zero Node dependency. `server.py`'s `_STATIC`/
  `_serve_static` hand out `central/static/`.
- **`/` is the public marketing landing page, the SPA lives under `/app`.**
  `_serve_static` maps `/`→`landing.html` and `/app`(`/`)→`index.html`; everything else
  is a literal static file. `landing.html` is a self-contained pre-bundled artifact (its
  own JS runtime + embedded woff2 fonts, no `/assets` deps) — the SOURCE is
  `web/public/landing.html`, which vite copies into `static/` on build (like `favicon.svg`
  and the install scripts), so `emptyOutDir` doesn't wipe it. Its two "Sign in" links
  point at `/app`. Edit the copy in `web/public/`, not the built one in `static/`.
- **The landing's marketing overlays are SERVER-INJECTED, never edited into the bundle.**
  The bundle rebuilds its whole DOM once (`documentElement.replaceWith`), so anything in
  the initial markup is wiped. `_serve_static._inject_showcase` (gated `cfg.showcase_enabled`
  / `WISP_SHOWCASE`) splices `window.__WISP_SHOWCASE__={…}` (live `store.showcase_stats()`
  — orgs with ≥1 node; named subset scrolls, blank name = opted out) + `showcase.js` before
  `</body>`. `web/public/showcase.js` (a normal `public/` file, ALSO committed to `static/`)
  is a self-healing overlay: a `MutationObserver` on `document` re-mounts the early-access
  offer bar + "Trusted by N ISPs" ticker after the bundle's swap. Offer copy lives in
  `showcase.js`; the numbers come from the DB. Tests: `test_central` (`showcase_stats`,
  `landing_injects_showcase`).
- **Routing is `HashRouter`, not `BrowserRouter`.** The SPA at `/app` uses hash routes
  (`/app#/home`); `server.py`'s static handler 404s on any non-file path (no SPA
  fallback, deliberately). `BrowserRouter` would need that fallback; `HashRouter` needs no
  server cooperation. Don't switch without adding the fallback first.
- **Theme (`web/src/index.css`) is minimal-gray, dark default** — the canvas sits
  near-black (`#09090b`) so cards/popovers step visibly UP from it, with borders one
  notch above the surface they sit on (`#2b2b33`) — surface steps + borders, never
  shadows, are what make elements read; desaturated accents so status colors stay loudest. shadcn
  CSS-var convention + `--success`/`--warning`/`-soft` for status; `dark` class on
  `<html>`, persisted (`lib/theme.ts`), applied before first paint in `main.tsx`.
  Density: single-line topology rows, health readout worst-news-first, device name wins
  over metric on narrow screens; row actions behind a kebab; mono only for machine ids;
  h1 = `text-lg font-semibold tracking-tight`; status chips 12px semibold
  (`text-[0.75rem]`). A resolved outage pending post-mortem renders NEUTRAL, never
  green — a to-do, not a win.
- **Spacing rides an 8px-ish grid, GCP-console loose, not cramped.** List rows are
  `h-11`/`py-2.5` with `px-4`–`px-5`, panel headers `px-5 py-3`, cards
  `--card-spacing:--spacing(5)`, header `h-14`. Density comes from filling the width
  with information, not from shaving inner padding — don't tighten these to fit more.
- **Type scale is bumped one notch and fully rem-based — never hardcode px font sizes.**
  `index.css` overrides `--text-xs`=13px and `--text-sm`=15px (readability for a 40+ NOC
  audience; the two smaller steps are `text-[0.75rem]`/`text-[0.6875rem]`), and the root
  font-size scales at wide viewports (17px ≥1600px, 18px ≥1920px) so the whole rem-based
  UI grows to fill big monitors instead of leaving dead margins. A `text-[12px]`-style
  literal opts out of that scaling — use the tokens or rem literals.
- **Auth rides the same session-cookie plane** — `central/auth.py` untouched.
  `hooks/use-auth.tsx` wraps `/api/me`/`login`/`logout` over same-origin fetch; a 401
  dispatches a `wisp:unauthorized` window event. Org scoping (`scopeOrg`): superadmin
  picks via header switcher (persisted, `GET /api/orgs`), org user pinned to own org —
  mirrors `server.py`'s `_scope_org`.
- **Live updates via `GET /api/events` SSE** — `hooks/use-event-stream.ts` opens one
  `EventSource` per org scope, invalidates react-query keys (`summary`/`outages`/
  `inventory`/`logs`/`team`/`attendance`/`nodes`) on each `changed` event. Fed by
  `store.data_version` (org-scoped fingerprint; includes `MAX(nodes.last_seen)` so a
  bare heartbeat fires it — probe staleness is client-derived from `last_seen`, and
  without that bump a resumed probe stayed "stale" until a hard refresh) +
  `store.low_bandwidth_alarms`/
  `high_bandwidth_alarms` behind `GET /api/summary` (the header's bandwidth chips).
  `uplink_down` stays in the summary response for other tooling but the SPA surfaces it
  nowhere; `central/dispatch.py` still pages `UPLINK_DOWN` via ntfy independently.
- **`store.list_org_devices()` LEFT JOINs `device_states`** so the tree/home color a
  device without a per-device round trip — nullable `state`/`latency_ms`/`packet_loss`/
  `jitter_ms`/`state_updated_at`, null until first report. Read-only join, no migration.
  Same for `switch_ports` aggregates (`ports_down`/`ports_bw_low`/`ports_bw_high`,
  monitored only) → clickable chips on a switch row.
- **The expanded device row is a facts-first health summary, not a chart gallery**
  (`DevicePerfPanel`). Top→bottom: latest sample + perf verdict vs the link's OWN
  baseline (`GET /api/inventory/perf`), a 24h hour strip (`HourStrip`, from
  `/api/analytics/trend?days=1` — cells floored on EPOCH hours to match `bucket_of`,
  never local hours, or half-hour timezones like IST shift every cell), the live
  sparkline with a dashed normal line, a 7-day uptime footer (`/api/analytics?days=7`).
  A query error there renders as an error, never the "no samples yet" empty state (a
  stale build 404ing the samples route once masqueraded as "graph never fills in").
- **Logs page groups by day of `occurred_at` (falling back to `received_at`), sorted by
  that stamp, NOT insert id** — acks/post-mortems insert long after the outage, so id
  order interleaves days. Group keys include the first row's event id: day labels repeat,
  and duplicate React keys leave stale rows.
- **Home is a NOC overview, never an empty page when healthy** (`routes/home-page.tsx`):
  stat tiles, triage (collapses to a one-line all-clear strip when nothing needs eyes),
  then a worst-first device panel + probes + recent-activity panels fed entirely by
  existing endpoints (inventory join, `/api/analytics?days=7`, `/api/logs`, nodes). The
  activity panel sorts by `occurred_at ?? received_at`, NOT insert id (same trap as the
  Logs page); event labels/tones are shared via `lib/events.ts`.
- **Outages triage** (`store.triage_outages` + `GET /api/outages`,
  `POST /api/outages/acknowledge`/`postmortem`) is folded into the Home page
  (`routes/home-page.tsx`, `components/outage-card.tsx`), no standalone route. Status
  (`unassigned`/`in_progress`/`pending_postmortem`) is derived, never stored, from
  `acknowledged_at`/`resolved_at`/`root_cause`; a resolved outage without a `root_cause`
  stays for `postmortem_days` (30). Recovery is FSM-automatic — triage offers
  acknowledge/postmortem only, never a manual resolve. Writes re-derive org via
  `store.outage_org`, gated owner/superadmin.
- **Mockup-only fakes with no backend — don't try to "finish" them:** "Clients online"
  (infra-only monitoring), a manual "Resolve" (recovery is FSM-automatic), Docker install
  (only Linux/Windows), a Topology "Map" (no coordinates in schema), "Notification
  history" (`alert_log` is internal-only). No frontend test suite; verified via `tsc
  --noEmit`, `npm run build`, manual Playwright.

## Config (env-var only)

- **Every tunable is a field on the frozen `Config` dataclass** (`config.py`), read once
  from a `WISP_*` env var at start. No DB settings layer.
- **`Config` is shared between edge and central** — grep both `apps/daemon/` and
  `src/wisp/central/` before deleting/renaming a field (`escalate_every_min`,
  `session_timeout_h`, `canary_ip`, `retry_interval_s` look edge-only but central reads them).
- **Topology, team, alert routing, node credentials live in the dashboard, not env vars.**
  Only process-level tunables are `WISP_*`.
- **`db_path` (`WISP_DB`) is not a database** — just where the lock file and the
  supervisor's transient files live.
- **Edge→central ingest auth is any ONE of three:** global bearer (`WISP_CENTRAL_TOKEN`),
  a self-service per-node token, or mTLS. Plus central's own dashboard session secret
  (`central/auth.py`, a `data/` file, 0600). No PIN.

## Self-service node enrollment

- **Registered from the Network page's "Probes" section** (`/#/nodes` redirects there).
  `POST /api/nodes` issues a credential shown once, `/rotate` replaces, `/revoke`
  deactivates. Third option alongside `central.admin enroll-edge` (mTLS) and
  `WISP_CENTRAL_TOKEN`.
- **Only a SHA-256 hash is stored** (`node_tokens`) — plaintext shown once, rotatable
  only. Fast hash is fine (`secrets.token_urlsafe(32)` is ~256 bits already).
- **The token rides the same `Authorization: Bearer` header** — zero client changes.
  `_ingest_ok(org, node)` tries global token, then self-service (`_node_token_identity`
  → `resolve_node_token`, identity FROM the credential not the envelope's claim), then
  mTLS. Any one satisfies it.
- **A node that HAS registered a credential is gated on presenting it** even with no
  global token or mTLS (`store.node_token_registered`); an UNREGISTERED node gets the
  open trusted-network default.
- **`clean_node_id`** validates the id (1–64 chars, starts letter/digit, then
  letters/digits/`.`/`_`/`-`) — it becomes a systemd identity, an `/etc/wisp` path
  segment, and a bare wire value.
- **Tests:** `integration/test_central.NodeTokenTest`, `test_central_node_enrollment.py`.

## mTLS enrollment

- **`central/pki.py` shells out to `openssl`** (issuance is a one-time admin-CLI op, not
  per-request) rather than adding `cryptography`; request-time verification uses stdlib
  `ssl` only. `openssl` must be on the admin box's PATH.
- **Identity is CN-encoded** — client cert CN is `org_id:node_id`
  (`pki.edge_common_name`/`peer_identity`), decoded off the verified socket, so
  `/report`'s JSON is unchanged. `_peer_identity()`'s CN must match the CLAIMED org (and
  node, where the route has one). None of the three configured = ingest stays open.
- **Central terminates TLS when configured** — stdlib `ssl`. `make_server` wraps the
  listener only when `WISP_CENTRAL_TLS_CERT`/`_KEY` are both set; `WISP_CENTRAL_CLIENT_CA`
  independently turns on `CERT_OPTIONAL` (dashboard browsers still connect certless). A
  terminator in front is also valid.
- **The handshake runs inside each request's worker thread** — `_TLSThreadingHTTPServer`
  overrides `finish_request` (not `get_request`), so one slow handshake can't stall new
  connections. `handle_error` logs `ssl.SSLError` quietly, lets others fall through.
- **`central.admin init-ca --host` / `enroll-edge --org --node`** create the CA + server
  cert / issue one client cert; both print the env vars to set.
- **No CRL/rotation yet** — revoking means rotating the CA. Future work.
- **Tests:** `unit/test_central_pki` (skipped without `openssl`),
  `integration/test_central_mtls`.

## Reliability invariants (the "trust the alarm" set)

- **One logical probe per org/node** via an OS advisory lock
  (`runtime/single_instance.py`), exits (code 3) if another holds it. Double-reporting is
  harmless anyway — central's per-outage dedupe (`open_outage_if_absent`, `WHERE NOT
  EXISTS`) is idempotent.
- **A page must not vanish to a blip.** `NtfyNotifier.send` retries via
  `send_with_retry`: network/timeout/5xx retryable (exp backoff), 4xx fails fast. Tunable
  `WISP_NTFY_RETRIES`/`_RETRY_BACKOFF_S`. Test with a fake attempt/sleep, no httpx.
- **The probe loop never dies on one bad cycle** — `run_forever_central_brain` wraps each
  cycle in try/except that logs and continues; keep new per-cycle work inside it.
  `_gather_pings` swallows per-probe errors but re-raises a config/permission
  `RuntimeError` loudly.
- **Cross-edge fleet watchdog is central's** — `central/watchdog.py:check(now)` pages an
  org when a node's heartbeat is stale, restart-safe and transition-only. No edge-side
  watchdog. Its input is `store.node_liveness()`, NOT `SELECT * FROM nodes` — the
  `nodes` heartbeat table remembers every identity that ever reported (one-off diag
  runs included), so per org: registered-credential orgs only watch live-credential
  identities, credential-less (global-token/mTLS) orgs watch every reporter. And
  `delete_node_token` purges the heartbeat row too, or a deleted probe pages
  NODE_STALE forever.

## Fleet update signing

- **Two install packages, one per OS** — a `.deb` (`deploy/build-deb.sh`, amd64+arm64)
  and a Windows setup exe (`deploy/wisp-edge-setup.iss` + `deploy/windows-task.ps1`,
  Inno Setup), both built by CI's `build` job. They are FIRST-INSTALL artifacts only:
  binaries in `/opt/wisp/bin` (`Program Files\WISP\bin`), config in `/etc/wisp`
  (`ProgramData\WISP`, conffile/never overwritten), service runs the supervisor
  (`runtime/supervisor.py`), which owns all subsequent agent self-updates by swapping
  the BARE binary — `release.yml`'s manifest.json deliberately excludes the .deb/setup
  exe (`wisp-edge-setup*` skip) so a rollout never ships an installer as an "agent".
  The old `install-edge.sh`/`.ps1` curl-scripts are deleted; `-src` variants remain for
  dev checkouts.
- **The supervisor STOPS the agent before `os.replace`, and its loop survives a failed
  update.** Windows delete-locks a running image — the ≤0.10.3 supervisor replaced the
  binary live, so the first real rollout (v0.11.0, 2026-07-04) crashed every Windows
  supervisor at the swap and orphaned the old agent (still heartbeating, version never
  flipping; central's rollout auto-HALTED exactly as designed). Any mid-apply exception
  now yields outcome `FAILED` and discards `update_request.json` — the next heartbeat
  re-drops it, so retry rides the poll cadence, never a tight re-download loop. The
  health gate needs `stable_polls` (3) CONSECUTIVE healthy polls — a lone
  `alive()`-right-after-spawn check passes a crash-looping build and makes rollback
  unreachable. Supervisors are NOT in the self-update channel: a supervisor fix reaches
  installed fleets only via an installer re-run on the box.
- **CI signing (`release.yml`) is real, needs real secrets.** Authenticode signs Windows
  `.exe`s per-binary in `build` (`WINDOWS_CODESIGN_PFX`/`_PASSWORD`); minisign signs the
  assembled `SHA256SUMS` **once** in `release` (`MINISIGN_KEY`, generated password-less
  via `minisign -G -W`). One signature over the manifest covers every artifact — no
  per-artifact `.minisig`. Both steps are `if: env.<SECRET> != ''` no-ops when unset.
- **The minisign PUBLIC key is not a secret — commit `deploy/minisign.pub`** once a real
  keypair exists; don't fabricate a placeholder. With the curl-scripts gone, minisign
  is for operators verifying a downloaded release by hand (`minisign -V -p
  deploy/minisign.pub -m SHA256SUMS`); the self-update path verifies each artifact's
  sha256 from the rollout directive central signed off on (`runtime/supervisor.py:
  verify_sha256`), and Windows additionally gets Authenticode on the binaries + setup
  exe. Signing self-activates; nothing hard-requires it — same pattern as mTLS and node
  tokens.
- **The edge's health is readable from disk: `status.json` + `logs/edge.log`.** The
  daemon drops `status.json` (atomic tmp+replace, best-effort — a full disk must never
  kill the probe loop) next to the lock file at startup, after EVERY full cycle, and on
  fatal startup errors (`runtime/edge_status.py`); before it existed, a probe with a bad
  URL/token crash-looped invisibly under the supervisor and the install looked "done".
  `windows-task.ps1` transcribes itself to `ProgramData\WISP\install.log`, makes the
  launcher `.cmd` append supervisor+agent output to `logs\edge.log` (rotated at task
  start when >5MB — crash-restarts re-run the launcher, so a crash-loop can't grow it
  unbounded), and after `Start-ScheduledTask` WAITS for a fresh `status.json` (exit 10 =
  no confirmation; the installer runs registration from `[Code]` `Exec` and surfaces
  that — never move it back to a fire-and-forget `[Run]` entry). Re-running the
  installer WITH a `-Central` value rewrites `edge.env.ps1`; the old write-once rule
  turned one bad first install into a permanently dead probe. Scheme-less URLs are
  normalized to `https://` in both the wizard and the script (silent installs skip the
  wizard). Tests: `unit/test_edge_status`, `integration/test_daemon_central_brain`'s
  status-file cases.
- **`wisp-tray.exe` is per-user UI, not part of the probe.** `apps/tray/main.py` +
  `runtime/win32tray.py` — pure-ctypes Shell_NotifyIcon (no pystray/Pillow; keep it
  dependency-free), HKLM `Run`-key autostart, reads `status.json`/`edge.env.ps1` from
  ProgramData as a standard user. All control goes through ELEVATED `schtasks` /
  `unins000.exe` (one UAC prompt per action); never parse `schtasks` output — its Status
  column is localized, only exit codes are safe. Product decision: tray "Exit" STOPS the
  probe (confirm dialog first); the probe still auto-starts at next boot. The tray is
  NOT in the self-update channel (the supervisor swaps the agent binary only) — it
  updates with the installer, and uninstall taskkills it before unregistering the task.
- **Release assets can't be hidden on GitHub** — `release.yml` writes install-first
  notes (`RELEASE_NOTES.md` → `body_path`, kept OUTSIDE `release/` so it isn't uploaded
  as an asset): an install table pointing at the setup exe/.debs plus an explicit
  "everything else is the self-update channel" line.
- **The GitHub repo is PRIVATE; central is the release mirror — nothing else touches
  GitHub.** `central/releasesync.py:sync_release` (run by `wisp-release-sync.timer` →
  `admin sync-releases`) pulls the latest release via the GitHub REST API using
  `WISP_GITHUB_TOKEN` (fine-grained PAT, Contents:read — the ONLY GitHub credential in
  the system, lives in `deploy/central.env`), downloads `manifest.json` + every agent
  binary it lists + the installers into `cfg.release_cache_dir` (`data/releases/<ver>/`),
  **verifies each agent binary against the manifest's sha256 before caching**, then
  rewrites the artifact URLs to central-relative `/download/<ver>/<name>` (sha256
  unchanged) and records the release. Private-repo asset downloads are a two-hop dance:
  the asset API 302s to a signed S3 URL that REJECTS an `Authorization` header, so
  `GithubReleases.download` captures the `Location` and re-fetches it clean (don't let
  urllib auto-follow with the token attached — `_NoRedirect`). `server.py`'s public
  `/download/<ver|latest>/<name>` route serves the cache (no auth — compiled artifacts
  aren't secrets, the SOURCE is what's private; edges self-update from here with no
  session). The supervisor resolves a leading-`/` directive URL against its own
  `WISP_CENTRAL_URL` (`apps/supervisor/main.py:_download`). Tests: `unit/test_releasesync`
  (FakeGh + the redirect/auth-drop case), `integration/test_central.DownloadRouteTest`.
- **Install-artifact asset names are VERSION-LESS and load-bearing**
  (`wisp-edge-setup-win-amd64.exe`, `wisp-edge-linux-<arch>.deb` — see `build-deb.sh`):
  the dashboard's Probes install card (`web/src/lib/install.ts`) links them same-origin
  via `${origin}/download/latest/<asset>` (central's mirror, NOT GitHub), which breaks
  the moment a filename embeds the version. The manifest builder skips
  `.deb`s/`wisp-edge-setup*` explicitly — a `.deb` matching the `wisp-edge-*` glob must
  never become an "agent artifact"; the mirror caches those installers separately for
  the install card.
- **`deploy/wisp-edge.spec`'s `Analysis` paths use `os.path.dirname(SPECPATH)`, not bare
  relative strings — don't revert.** A loaded `.spec` resolves relative paths against its
  own dir (`deploy/`), not cwd. The inline supervisor + tray builds in `release.yml` (no
  `.spec`) resolve against cwd = repo root, so they're fine.
- **None of this has run against a real key or hardware** — needs the operator's real
  minisign keypair + code-signing cert and a genuine `v*` tag release. The unsigned
  multi-arch build validates for real on every push.

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import, don't hardcode.
- **Hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2, recovery = 2
  healthy. The FSM never emits `UNREACHABLE` — that's a topology override in
  `process_cycle` after `feed()`. Don't regress these counts (fast-confirm changes when
  samples arrive, not how many).
- **Topology order:** parent-before-child (`_topological_order`) so a parent's new state
  is known when evaluating children.
- **No automatic cause inference** — cause is only an operator-entered post-mortem at
  resolution.
- **Escalation model:** a fresh DOWN pages owner+operator immediately (`_on_open`).
  Dedupe is **per-outage** (a `sent` row for this outage id?), not time-windowed. Queues
  one `escalations` row (`kind="hourly"`, due `now + cfg.escalate_every_min`, 60); each
  `sweep` that finds it due while open fires an all-hands broadcast and reschedules the
  same row. Ack does NOT stop this; only recovery does. Escalations are DB-derived
  (`escalations.due_at` + sweeper), `UNIQUE(outage_id, kind)` keeps them idempotent.
- **Restart safety:** `EngineRegistry` rehydrates each FSM from `device_states` — breaking
  it re-pages everyone on restart.
- **Timestamps:** poll/outage stamps are ISO8601 `+00:00`; SQLite `datetime('now')`
  (acks) is space-separated naive. `core/analytics._parse` normalises both to naive UTC —
  reuse it (also used by `rollout.py`/`watchdog.py`).
- **Schema:** `central/store.py`'s `_SCHEMA` + `_ensure_columns` is the only schema now.

## Tests

`python -m unittest discover -s tests` (393 tests) after any logic change. Tests inject a
recording notifier/client double — no real ntfy/central network. Per-area files are named
next to their invariant above; the rest: `unit/test_state_machine` (FSM + overrides +
`probe_plan` + subset confirmation + adaptive cadence), `unit/test_baseline`,
`unit/test_snmp` (parser/throughput + `is_down()`), `unit/test_supervisor`,
`integration/test_daemon` (`_gather_pings`), `integration/test_notifiers`
(`send_with_retry`), `integration/test_single_instance`.
