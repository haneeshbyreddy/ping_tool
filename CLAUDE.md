# CLAUDE.md

Invariants and gotchas that aren't obvious from the code. What/how/layout/config lives
in `README.md`. Verify claims about what's done against the code — stale docs drift.

## Architecture

Central runs the brain for every org: FSM, topology-aware suppression, fast-confirm,
the alerting ladder, the multi-org dashboard, fleet version/rollout state. The edge is
a thin probe with exactly one mode (`WISP_CENTRAL_URL` mandatory): fetch topology,
probe ICMP under bounded fan-out, report raw per-IP samples, heartbeat its version.
No local DB, dashboard, or FSM on the edge.

Central (`central/server.py`/`store.py`/`auth.py`/…) is pure stdlib. Its dashboard is
a build-time React/TS/Tailwind/shadcn SPA (`web/` → built into `central/static/`, Node
is dev-only; the committed build is what deploys). Edge needs a `.venv`
(`requirements.txt`: `icmplib`/`httpx`; system Python is PEP 668-locked) + `sysctl
net.ipv4.ping_group_range="0 2147483647"`.

**Locked decisions (don't relitigate):** brain on central, always; monitor shared
infra, not end-user routers; ntfy only, 3 role topics/org; every read/write org-scoped;
edge dials central, never the reverse; updates pull-based over the report channel,
staged + health-gated; probers/notifiers behind interfaces, tests inject doubles.

## Imports & paths (the main trap)

- Absolute imports under `wisp.*`; src layout, nothing installed. `apps/daemon/main.py`
  and `apps/central/main.py` prepend `<repo>/src` to `sys.path`; the admin CLI needs
  `PYTHONPATH=src`; tests bootstrap their own path.
- `config.PROJECT_ROOT` = repo root (`parents[2]` of `config.py`); `central_db`
  defaults to `data/central.db`; the SPA resolves from `central/static/`.

## Engine invariants

- `core/state_machine.py:MonitorEngine` is **pure** — `{ip: PingResult}` + ts in,
  states + `Event`s out, no I/O. Central owns build/rehydrate/persist
  (`central/engine.py`). No DB/network calls in the engine.
- `process_cycle(results, ts, subset=None)`: `None` = full pass (all devices +
  canary/uplink + freeze); a `set[int]` = confirmation pass (only those FSMs, topo
  order, skips canary/uplink). Keep the full-pass path byte-identical.
- `probe_plan()` is a reference the edge approximates (`_gentle_probe_plan`), not
  something central calls. Known gap: it counts a BACKUP parent as infra; the edge
  can't yet (`GET /edge/devices` only carries `parent_device_id`).
- `central/dispatch.py:CentralAlertDispatcher` sends OUTSIDE any DB transaction — a
  slow API call must never hold a write lock.
- Prober/Notifier live behind `build_prober`/`build_notifier`
  (`ingress/probers.py`, `egress/notifiers.py`); new providers go behind them.
- **Windows probes via `SingleSocketIcmpProber`, never icmplib** (picked by
  `sys.platform`; `WISP_PROBER=singlesock|icmplib` forces one). Windows raw sockets
  are promiscuous — N sockets each see every reply (O(N²)) and asyncio stamps arrival
  at coroutine-read time (~150ms floor). Fix: ONE shared raw socket + ONE receiver
  thread stamping `perf_counter()` right after `recvfrom`, matched by ICMP id
  (pid-derived) + seq + reply source IP. Linux keeps icmplib's unprivileged datagram
  sockets (kernel demuxes; raw would need root and break the ping-group invariant).
  Tests: `unit/test_probers` (fake socket via `sock_factory`).

## Scaling invariants

- **Probe fan-out is bounded** by `asyncio.Semaphore(cfg.probe_max_inflight)`
  (`WISP_MAX_INFLIGHT`, 256). Unbounded gather past `ulimit -n` reads as a fake mass
  outage (socket refusals masked as 100% loss). Don't reintroduce it.
- **Aggregation gear is probed gently**: parents get `pings_per_poll_infra` (2),
  leaves + canary `pings_per_poll` (5) — or ICMP rate-limiters read as phantom loss.
- **Fast-confirm is central-driven**: `central/engine.py:compute_recheck` names
  suspect IPs in the `/report` reply; the edge re-probes just those every
  `WISP_RETRY_INTERVAL_S` (`mode="recheck"`) until the hint is empty. A frozen cycle
  (canary down) yields no hint.
- **Adaptive cadence** (`Config.effective_interval`): 30s while fleet ≤ 1000 and
  `poll_interval_adaptive` on (off by default), else 60s. Computed once at startup.

## Central runs the brain

- `central/engine.py` (`load_device_meta`/`build_engine`/`apply_events`) is the only
  DB glue around the unchanged `MonitorEngine`; `central/dispatch.py` is the alerting
  policy (dedupe per outage, owner+operator on open, all-three on escalation/resolve,
  ack never stops it — only recovery does).
- **`EngineRegistry`: one live engine per org** (flap streaks accumulate across
  stateless `/report` calls). Rebuilds only when the topology fingerprint
  `(id, parent_device_id, d.parents)` changes; rehydrates from `device_states`.
  Breaking rehydration re-pages everyone on restart.
- **Wire format is IP-keyed**: `POST /report`
  `{"v":1,"org_id","node_id","ts","mode":"full"|"recheck","pings":{ip:{loss_pct,latency_ms,jitter_ms}}}`.
  The edge never sees central's device ids.
- **Escalation sweeping rides the report cadence** — `sweep(ts)` once per full
  `/report`, scoped to that org. Stalls if an edge goes stale; the fleet watchdog
  pages for that separately.
- **The heartbeat is the self-update channel, not liveness.** Reply may carry an
  `update` directive (`central/rollout.py`), written ATOMICALLY as
  `update_request.json` for the supervisor. Liveness is `touch_node` off `/report`.
  A failed heartbeat is a warning, never a crashed cycle.
- **SNMP is a BACKGROUND asyncio task, never inline in the probe cycle** (inline
  walks once made the edge report every 4 minutes). `snmp_max_inflight` (4)
  concurrent walks; no await on SNMP in the ICMP report path. Ports attach to full
  reports only, never recheck. **Three separate walk caps, not one** — health rides
  `snmp_walk_timeout_s` (20s), but the two big walks got their own larger budgets
  because a 200+-interface OLT / hundreds-of-ONU EPON agent can't finish inside 20s
  and timing out leaves that table permanently stale while the smaller walks stay
  fresh (same box): `port_walk_timeout_s` (60s, ifTable) and `gpon_walk_timeout_s`
  (75s, ONU roster). Both field-diagnosed 2026-07-09. Don't collapse them back.
- **One `SnmpEngine` per poller instance, NEVER one per walk** — a per-walk engine
  leaks ~1 MiB + one FD per walk forever (transport stays registered with the loop);
  FD exhaustion then reads as a fake mass outage. `PysnmpPoller`/`PysnmpGponPoller`
  lazily reuse `self._engine` (concurrent walks are safe — request-id demux).
  `EngineReuseTest` in `unit/test_snmp` + `unit/test_gpon` pins this.
- **Port alarms** (`central/ports.py`): monitored-only, admin-down silent, one alarm
  not two — a port-down folds into the open outage via `stamp_outage_cause` COALESCE
  (never clobbers a post-mortem); no open outage = heads-up; SNMP never opens an
  outage. Gated `cfg.snmp_alerts`; state always written. **Bandwidth has floor AND
  ceiling** (`bw_threshold_mbps`/`bw_max_mbps`, both optional per port,
  `snmp_bw_consecutive` walks to alarm); never judged on a down port; gated
  `cfg.snmp_bw_alerts`.
- **Rollups**: `central/analytics.py:device_reliability` (`/api/analytics?days=`) is
  pure outage math, every active device, UNREACHABLE excluded. `central/rollup.py`
  (`/api/analytics/trend`) is hourly buckets, 30-day retention, pruned daily.
- **Perf baseline** (`central/perf.py`): median+MAD over a bounded per-device ring
  buffer (`device_perf_samples` — NOT the hourly rollup; an hourly average smears the
  slowdown). Badge persisted (`device_perf`), clears on hard-DOWN, operator-only,
  gated `cfg.perf_alerts`. Samples at `/api/inventory/perf/samples`.
- **On-backup redundancy**: engine already computes it via `effective_parents()`.
  `org_device_links` (`kind='backup'`, cycle-checked over the FULL edge set),
  `central/redundancy.py:sweep` pages enter/leave, never opens an outage, gated
  `cfg.backup_alerts`.
- **Remote SNMP walks — the edge is central's hands, poll-only.** Queued from the
  dashboard, delivered in the next FULL `/report` reply under `snmp_walks` (the edge
  NEVER accepts inbound); run by a sequential background runner (`_DiagWalkRunner`)
  via `ingress/walker.py` (shared engine, bounded); result POSTs to
  `/edge/snmp-walk`. Pending walks re-deliver every report until a result lands; one
  pending per device; newest 10 kept. **The runner refuses any target IP not in the
  node's device list** — no lateral-movement primitive. Server double-bounds the
  upload size.
- **Vendor SNMP health profiles are DATA, not edge code** (`snmp_profiles`; org_id
  NULL = global). Metric → OID + decode from a CLOSED vocabulary
  (`as_is/div10/div100/signed_div100`, select `first/avg/max/sum`). The edge
  interpreter (`ingress/health.py`) walks `sysObjectID`, matches by LONGEST prefix,
  walks profile OIDs BEFORE standard MIBs, fills only fields still None; hardcoded
  MikroTik/Fiberhome fallbacks stay for fleets on an older central. Onboarding a
  vendor = a profile row, never a rollout. Keep the vocabulary tiny.
- **GPON vendor auto-detects from sysObjectID; unmatched = optics OFF, never guess**
  (a fabricated dBm is the DBC placeholder trap). Precedence in
  `GponPollerPool.resolve`: device `gpon_vendor` (now an override) >
  `WISP_GPON_VENDOR` (default empty = auto) > sysObjectID longest-prefix match >
  None (no optics). Detection cached per device (1h ok / 15min on silence — catches
  a hardware swap), runs inside the SNMP semaphore, reuses one lazy engine. Tests:
  `unit/test_gpon`.

## Central management plane

- **`org_devices` is THE device table.** The single-box `devices`/`rollups` tables
  and `POST /ingest` are DELETED (2026-07) — don't reintroduce a second registry.
  `events` survives: central-originated log lines only.
- `central/inventory.py` is pure validation, no storage; `clean_device_payload`'s
  `parents` map is pre-scoped to one org by the caller.
- **Every `org_devices` write re-derives org from the DB row** via
  `store.device_org(id)` (body `org_id` trusted only on create); same pattern for
  `switch_ports`, feeds, links. `/api/orgs` stays org-filtered (`_scope_org`).
- `orgs.ntfy_topic_owner/operator/tech` (outage routing) are separate from
  `orgs.ntfy_topic` (fleet-watchdog NODE_STALE/OK) — don't merge.
- **New columns on existing tables need `_ensure_columns`** in
  `CentralStore.__init__` or an existing `central.db` keeps the old schema. New
  tables need nothing.
- `make_server`/`_make_handler` take an injectable `notifier` — tests inject a
  recording double; follow this for anything central sends.
- **Superadmin Overview = coverage, not alarms** (`/api/admin/overview`): per-org
  SNMP/optics/ports enabled-vs-working (working = any reading fresher than 900s);
  never-reported vs gone-stale are distinct reasons; optics/ports problems
  suppressed when the device's SNMP is dead outright. Pure read-side, never pages.

## Dashboard (web/ → central/static/)

- Built output is committed (`cd web && npm install && npm run build`); `./run.sh`
  needs no Node.
- **`/` = marketing landing, SPA at `/app`.** `landing.html` is self-contained; its
  SOURCE is `web/public/landing.html` (vite copies it on build) — edit there, never
  the built copy. Marketing overlays are SERVER-INJECTED (`_inject_showcase`, gated
  `WISP_SHOWCASE`) because the bundle rebuilds its whole DOM — `showcase.js` is a
  self-healing overlay (MutationObserver re-mounts after the swap).
- **`HashRouter`, not `BrowserRouter`** — the server 404s non-file paths, no SPA
  fallback. Don't switch without adding the fallback first.
- **Theme**: minimal-gray, dark default, near-black canvas; surface steps + borders,
  never shadows; desaturated accents so status colors stay loudest. Spacing is
  8px-grid GCP-loose (`h-11` rows, `px-4`–`px-5`) — density comes from filling
  width, not shaving padding. **Type scale is rem-only** (`--text-2xs`=12px,
  `--text-xs`=13px, `--text-sm`=15px, root scales up ≥1600px) — a `text-[12px]`
  literal opts out of the scaling; use tokens or rem. A resolved outage pending
  post-mortem renders NEUTRAL, never green.
- **Surface ladder (2026-07-10 elevation pass)**: `--muted` sits BELOW its surface
  (wells recess), `--popover` is THE raised/focus surface (drill-in block, map
  chrome, menus), `--accent` is the interaction fill on raised surfaces — so a
  fill that means "hover/selected/skeleton" must use accent, never muted (muted
  now darkens). Row hover is `hover:bg-foreground/5` (a wash that works on every
  surface), selection is the `.wisp-drillin` block (index.css: popover bg +
  `--border-strong` outline; NO colored rail — tried, rejected) — hover ≠
  selected, keep the steps
  perceptible (adjacent surfaces ≥ ~3 ΔL*; they were 1.017:1 once). Faint text =
  `text-faint-foreground`, not muted-foreground/70-style opacities; maint/stale
  chips render neutral, never amber; device-panel tabs are `variant="line"`.
- Auth rides the session cookie (`central/auth.py` untouched); 401 dispatches a
  `wisp:unauthorized` window event; org scoping mirrors `_scope_org`.
- **Live updates**: one SSE `EventSource` per org scope (`/api/events`), invalidates
  react-query keys off `store.data_version` (includes `MAX(nodes.last_seen)` so a
  bare heartbeat un-stales a probe without a refresh).
- `list_org_devices()` LEFT JOINs `device_states` (+ `switch_ports` aggregates) so
  rows color without per-device round trips.
- **Epoch-hour trap**: `HourStrip` cells floor on EPOCH hours to match `bucket_of`,
  never local hours (half-hour timezones like IST shift every cell). A query error
  in the device panel renders as an error, never the empty state.
- **Viewport breakpoints are wrong inside the device panel** — it's a fixed 380px
  on a wide desktop screen, so `sm:`/`md:` guards all pass and columns overflow
  (the Optical tab's ONU heat-strip once collapsed to a one-cell-wide column, one
  wrapped row per ONU). Width-conditional columns in panel content use CONTAINER
  queries (`@container` on the panel block, `@md:`/`@xl:` on the columns).
- **Sort by `occurred_at ?? received_at`, NOT insert id** (Logs day-grouping and the
  Home activity panel) — acks/post-mortems insert long after the outage. Log group
  keys include the first row's event id (day labels repeat).
- Home is a NOC overview, never empty when healthy; outage triage is folded into it
  (status derived from `acknowledged_at`/`resolved_at`/`root_cause`, never stored;
  recovery is FSM-automatic — no manual resolve, ever).
- **Map view (`/map`) is real** (2026-07-10, was a mockup-fake): Leaflet + raster
  tiles fetched by the BROWSER (central needs no egress). Basemaps are
  **Google / Google Satellite ONLY** (2026-07-11; the CARTO/Esri/Dim menu
  entries were removed at the operator's request the same day they shipped) via
  the Map Tiles API — the sanctioned third-party-renderer API, NOT the SDK-only
  Maps tiles. **The key is SERVER-WIDE, superadmin-pasted once** (`app_settings`
  table, Settings → "Google Maps (all organizations)", `/api/admin/settings`) —
  org owners never see a key field; the GET `/api/orgs` reply injects it into
  every org row so each org's map lights up (referrer-restricted, ships to
  browsers by design, central still makes NO Google calls; no key = no Layers
  button). A dead `orgs.google_maps_key` column may linger in older DBs from
  the few hours it was per-org. CARTO Voyager survives as the KEYLESS FALLBACK,
  never a menu option: it renders for no-key orgs, under a
  still-creating session, and after a Google failure — the map is never blank.
  `lib/google-tiles.ts`: session token (~2wk) cached in localStorage per
  mapType; **dpr>1 sessions request `scaleFactor2x`+`highDpi`** (512px tiles at
  256 CSS px — plain 256 rasters are why Google "looked blurry" on scaled
  displays; cache key carries the scale). ToS needs the per-viewport copyright
  in the attribution control + a Google wordmark overlay. Failure ladder:
  tile-error BURST (3 in 5s, once per token — a stray rural-z20 404 must not
  nuke the basemap) → recreate session once → toast + fallback tiles, WITHOUT
  overwriting the user's saved pick. **Chrome-over-tiles trap**: shadcn outline Buttons carry
  `dark:bg-input/30`, which BEATS a plain `bg-popover/95` override in dark mode —
  invisible over dark tiles, washed-out over bright ones; map chrome needs
  `bg-popover/95 dark:bg-popover/95`.
  `org_devices.lat/lng` write only via `POST /api/inventory/location`
  (paired-or-both-null; dashboard-side only — the edge never sees coordinates).
  Pins are divIcons styled off theme tokens; the click-through panel is the same
  `components/device-detail.tsx` the Network tree rows use (extracted, not forked —
  keep it shared). Topology polylines MUST stay `interactive={false}` or they
  swallow placement clicks. The viewport is LOCKED to `orgs.map_region` (Settings →
  Map area; keys + bounds in `web/src/lib/map-regions.ts`, unknown key → all-India,
  `world` = no lock). All view logic lives in ONE `useMap()` child
  (`ViewController`) — a ref on `MapContainer` isn't set yet when a query resolves,
  and the fit MUST run before `setMinZoom`: min-zooming a zoomed-out map fires an
  ANIMATED setZoom that lands after and overrides an `animate:false` fitBounds.
  Map search = device match (instant) + OSM Nominatim geocoding (browser-side,
  debounced 450ms + 3-char floor — stay a polite keyless client; results boxed to
  the org's map area). Picking an unplaced device from search starts placement.
  Map divIcons are CACHED by html string (`_iconCache`) — `useNow()` re-renders
  every second and an uncached icon swaps every marker's DOM node per tick,
  restarting the down-pulse. Control buttons shift left of the open device panel
  on desktop (`md:right-[calc(380px+1.5rem)]`) or the panel covers them.
  **Site clusters** (2026-07-11): pins that would overlap on screen fold into a
  count badge (worst member tone on the border) — SCREEN-SPACE and zoom-dependent
  (`buildClusters`, greedy in Web Mercator px; cluster key = member ids, so
  zooming folds an open fan on purpose). Click = fitBounds when members are
  genuinely spread, spider-fan (pixel-sized radius, dashed legs) when they share
  a spot — placing devices at the same coords IS the "rack", no schema. `pinPos`
  is each device's DISPLAY position (centroid folded / fan open); links + PON
  spokes read it, and selecting a device force-opens its cluster so search and
  trouble-cycle never land on a hidden pin.
- **PON on the map** (2026-07-10): OLT pins ring amber/red off `onus_warn/crit`
  (suppressed when maint/down/stale-optics). The per-ONU spoke fan (Phase 2) was
  REMOVED 2026-07-11 at the operator's request — EPON ranging gives distance but
  no bearing, so spoke angles were fabricated, and on a map everything reads as
  geography. **The map shows only true locations**; ONU severity lives in the pin
  ring + the Optical tab. Don't rebuild pseudo-geographic overlays — ONUs return
  to the map only if/when they get real coordinates (`focusOnuId` threading
  through DeviceDetail → OpticalPanel survives for that).
  **Leaflet trap**: `pathOptions.className` is silently DROPPED (setStyle
  ignores className) — pass `className` as a top-level react-leaflet prop and
  include the tone in the key so a state change remounts the path.
- **Mockup-only fakes — don't "finish" them**: Clients online, manual Resolve,
  Docker install, Notification history. No frontend test suite;
  verify via `tsc --noEmit`, `npm run build`, manual Playwright.

## Config

- Every tunable is a field on the frozen `Config` dataclass (`config.py`), read once
  from `WISP_*` env vars. No DB settings layer. Topology/team/routing/credentials
  live in the dashboard, not env vars.
- **`Config` is shared between edge and central** — grep both `apps/daemon/` and
  `src/wisp/central/` before deleting/renaming a field.
- `db_path` (`WISP_DB`) is not a database — just where the lock file and supervisor
  transient files live.

## Ingest auth & enrollment

- Any ONE of three: global bearer (`WISP_CENTRAL_TOKEN`), a self-service per-node
  token, or mTLS. None configured = ingest stays open (trusted network).
- **Node tokens**: registered from Network → Probes; only a SHA-256 hash stored,
  plaintext shown once, rotatable only. Same `Authorization: Bearer` header. A node
  that HAS a credential is gated on presenting it; identity comes FROM the
  credential, not the envelope. `clean_node_id` validates (it becomes a systemd
  identity + path segment).
- **mTLS**: `central/pki.py` shells out to `openssl` (admin-CLI one-time op);
  identity is CN-encoded `org_id:node_id`, must match the claimed org/node. Central
  terminates TLS when `WISP_CENTRAL_TLS_CERT/_KEY` set; `WISP_CENTRAL_CLIENT_CA`
  turns on CERT_OPTIONAL (browsers stay certless). The handshake runs in the
  request's worker thread (`finish_request` override) so one slow handshake can't
  stall the listener. No CRL — revoking means rotating the CA.

## Reliability ("trust the alarm")

- One probe per org/node via an OS advisory lock (exit 3); central's per-outage
  dedupe (`open_outage_if_absent`) is idempotent anyway.
- **A page must not vanish to a blip**: `send_with_retry` — network/timeout/5xx
  retry with backoff, 4xx fails fast (`WISP_NTFY_RETRIES`/`_RETRY_BACKOFF_S`).
- The probe loop never dies on one bad cycle (per-cycle try/except; keep new
  per-cycle work inside it). `_gather_pings` swallows per-probe errors but re-raises
  config/permission `RuntimeError` loudly.
- **Fleet watchdog is central's** (`central/watchdog.py`), transition-only,
  restart-safe. Input is `store.node_liveness()`, NOT `SELECT * FROM nodes` (the
  heartbeat table remembers every identity ever seen); `delete_node_token` purges
  the heartbeat row too, or a deleted probe pages NODE_STALE forever.

## Fleet packaging & self-update

- Two FIRST-INSTALL-only artifacts: `.deb` (`deploy/build-deb.sh`) and Windows setup
  exe (`deploy/wisp-edge-setup.iss`). Both run the **supervisor**
  (`runtime/supervisor.py`), which owns all agent self-updates (download → verify
  sha256 → swap → health-gate → rollback). The manifest builder skips
  `.deb`s/`wisp-edge-setup*` — an installer must never become an "agent artifact".
- **The supervisor STOPS the agent before `os.replace`** (Windows delete-locks a
  running image — the v0.11.0 live-swap crash), and any mid-apply exception yields
  FAILED + discards `update_request.json` (retry rides the poll cadence, never a
  tight loop). The health gate needs `stable_polls` (3) CONSECUTIVE healthy polls or
  rollback is unreachable for a crash-looping build. Supervisors are NOT in the
  self-update channel — only an installer re-run updates them. Ditto
  `wisp-tray.exe` (per-user pure-ctypes tray; keep it dependency-free; control via
  elevated `schtasks`, never parse its localized output; tray "Exit" stops the
  probe, it still auto-starts at boot).
- **Edge health is on disk**: `status.json` (atomic, best-effort — a full disk must
  never kill the probe loop; written at startup, every full cycle, and on fatal
  startup errors) + `logs/edge.log` (rotated at task start >5MB). The Windows
  installer WAITS for a fresh `status.json` (exit 10 = unconfirmed) — never move
  that back to fire-and-forget. Re-running the installer with `-Central` rewrites
  `edge.env.ps1` (write-once made one bad install permanently dead). Scheme-less
  URLs normalize to `https://`.
- **Central is the release mirror; edges never touch GitHub.**
  `central/releasesync.py` (via `wisp-release-sync.timer`) pulls the latest release
  **unauthenticated by default** (`WISP_GITHUB_TOKEN` only for a private repo / rate
  limits — an expired PAT once silently blocked a rollout), verifies each agent
  binary's sha256 against the manifest, caches under `data/releases/<ver>/`, and
  rewrites URLs to central-relative `/download/<ver|latest>/<name>` (public route,
  no auth). GitHub asset downloads 302 to S3 which REJECTS an Authorization header —
  capture Location and re-fetch clean (`_NoRedirect`). The supervisor resolves
  leading-`/` URLs against its own `WISP_CENTRAL_URL`.
- **Install-artifact names are VERSION-LESS and load-bearing**
  (`wisp-edge-setup-win-amd64.exe`, `wisp-edge-linux-<arch>.deb`) — the dashboard's
  install card links `${origin}/download/latest/<asset>`.
- CI signing (`release.yml`): Authenticode per Windows binary, minisign once over
  `SHA256SUMS`; both no-op while secrets are unset. Commit `deploy/minisign.pub`
  only once a real keypair exists. Nothing has run against real keys yet.
- `deploy/wisp-edge.spec` `Analysis` paths use `os.path.dirname(SPECPATH)`, not
  bare relative strings — a loaded `.spec` resolves against its own dir, not cwd.

## Conventions & gotchas

- States: `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Import from `core/state_machine.py`, don't hardcode.
- Hysteresis: DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2, recovery = 2
  healthy. The FSM never emits `UNREACHABLE` — that's a topology override after
  `feed()`. Fast-confirm changes when samples arrive, not how many.
- Topology order: parent-before-child (`_topological_order`).
- No automatic cause inference — cause is operator-entered at resolution only.
- Escalation: fresh DOWN pages owner+operator; one `escalations` row
  (`kind="hourly"`, `UNIQUE(outage_id, kind)`) re-broadcasts all-hands every
  `cfg.escalate_every_min` while open. Ack doesn't stop it; recovery does.
- Timestamps: poll/outage are ISO8601 `+00:00`; SQLite `datetime('now')` is
  space-separated naive. `core/analytics._parse` normalizes both — reuse it.
- Schema: `central/store.py`'s `_SCHEMA` + `_ensure_columns` is the only schema.

## Removed — don't go looking for these

The single-box era (one daemon + local dashboard, one SQLite per edge) is deleted
wholesale, not deprecated — and **git history no longer has it**. History was
truncated to the newest 10 commits (2026-07-09, force-pushed, no backup kept), so
that code is gone for good: don't offer to restore it, and don't cite `git log` for
anything before `5a532a7`. Gone: `apps/dashboard/`, `src/wisp/server/`,
`egress/shipper.py`, `src/wisp/database/` + `migrations/`, the old local-engine
drivers in `apps/daemon/main.py`, `POST /ingest` + `devices`/`rollups` tables, the
curl-script installers, and the vanilla-JS dashboard.
`core/state_machine.py`, `core/analytics.py`, `core/baseline.py` are alive — central
imports them; grep before deleting anything in `core/`.

Five tags survive (`v0.13.0`–`v0.15.1`); only `v0.14.0`/`v0.15.0`/`v0.15.1` carry a
GitHub Release, and those are the artifacts the fleet self-updates from. `v0.1.x`–
`v0.12.1` went with the history they pointed at. **`v0.14.0` is the rollback floor** —
there is no artifact below it, so an edge still on an older build (Edge_1, `0.12.1`)
can only roll forward.

## Tests

`python -m unittest discover -s tests` after any logic change. Tests inject recording
doubles — no real ntfy/central network. Key files: `unit/test_state_machine`,
`test_probers`, `test_snmp`, `test_gpon`, `test_health`, `test_supervisor`,
`test_releasesync`, `test_central_inventory`, `test_central_pki`, `test_edge_status`;
`integration/test_central*`, `test_daemon*`, `test_notifiers`,
`test_single_instance`.
