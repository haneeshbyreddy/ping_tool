# Village WISP Monitor

A network monitoring + alerting tool for a rural WiFi broadband operator. It pings the
shared infrastructure (towers, relays, backhaul, core), figures out **what is down and
where** (topology-aware, so a dead parent suppresses its children), and pages the operator
immediately — then re-pages the whole team (owner + operator + tech) every hour the outage
stays open, with the running duration, until it recovers.

It polls with real ICMP and alerts over ntfy push. The dashboard + admin CLIs are pure
stdlib; the **daemon** needs a small venv (`icmplib`/`httpx`, plus `pysnmp` for the SNMP port
ingress) and the kernel ping group enabled.
See `plan.md` for the full design and `broadband-monitor-idea-doc` notes inside it.

## Quick start

**Fastest path:** `./run.sh` — migrates the DB and runs both the worker and the dashboard
on http://127.0.0.1:8000 (Ctrl-C stops both). The DB starts empty; add your real devices
and team from the dashboard. The daemon needs the venv below to actually ping.

```bash
# one-time: deps for the polling daemon (ICMP + ntfy HTTP)
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
# let the daemon send ICMP as a normal user (unprivileged ping sockets):
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
echo 'net.ipv4.ping_group_range=0 2147483647' | sudo tee /etc/sysctl.d/99-wisp-ping.conf
```

The `wisp` package lives under `src/` (a *src layout*) but nothing is installed —
the two runtimes (`apps/daemon`, `apps/dashboard`) put `src/` on the path
themselves; the admin CLIs use `PYTHONPATH=src python -m …`.

```bash
PYTHONPATH=src python -m wisp.database.client        # create DB (WAL) + run migrations

# the polling worker (needs the venv active):
python apps/daemon/main.py                           # real 60s cadence, forever

# operator actions (CLI; the dashboard covers the live views):
PYTHONPATH=src python -m wisp.egress.ack                # list open outages
PYTHONPATH=src python -m wisp.egress.ack <id> "Your Name"   # acknowledge (named in the hourly re-page; doesn't stop it)

# operator dashboard (browser UI over the same live DB; pure stdlib):
python apps/dashboard/main.py                        # http://127.0.0.1:8000  (Ctrl-C to stop)

python -m unittest discover -s tests                 # 244 tests (pure stdlib)
```

**First visit** sets a dashboard **PIN** (shared, gates the whole UI); after that, the
PIN unlocks it (12h signed-cookie sessions). Seed a PIN non-interactively with
`WISP_DASHBOARD_PIN=1234`. Everything an operator needs is now in the browser — see
**Operator self-service** below.

The dashboard is **fully self-contained** — Tailwind and the Material icons are
vendored under `apps/dashboard/static/`, so it works on a site with no internet,
no build step, and no third-party Python deps. The daemon and the web server are
**decoupled runtimes** that run side by side (WAL SQLite lets the writer and the
dashboard reader coexist); all local state lives under `data/` (git-ignored).

From the **Nodes** page you can add / edit / delete devices (the whole inventory —
name, IP, type, region, parent) from the
UI. Newly added or removed nodes start/stop being *monitored* automatically — the
daemon re-reads the device set each cycle and rebuilds its engine in-process when it
changes, within one poll cycle.

## Operator self-service (Phase 8 — no shell needed)

The dashboard is the control plane: an operator runs and reconfigures everything from
the browser.

- **Settings** — the in-UI essentials: a **Send test alert** button (channel check),
  **Change PIN**, and **Download backup**. Detection thresholds, escalation timing, the
  ntfy base URL, and org/timezone are configured via `WISP_*` environment variables and
  applied on restart (see Config below).
- **Team** — workers as first-class entities — identity + role (owner / operator / tech),
  *not* per-person routing. Alerts go to **three fixed ntfy topics, one per role**
  (`WISP_NTFY_TOPIC_{OWNER,OPERATOR,TECH}`); a person subscribes to the topic for their
  role. You can't remove the last active owner. The Team page also carries an
  **Attendance** board — a daily present-toggle for operators (who showed up, by date)
  plus a recent-days grid. The day's on-duty operators are surfaced on each active
  outage's triage card, so you can see who was around when it broke.
- **Device-set hot reload** — the daemon re-reads the active device set each cycle and
  rebuilds its engine in-process when you add/remove a node (state rehydrates from the DB,
  so nobody is re-paged). No restart needed for inventory changes. Config tunable changes
  do need a daemon restart.
- **Download backup** (Settings) — a consistent `VACUUM INTO` copy of the DB
  (PIN + team + history), so a lost `wisp.db` isn't a re-onboarding.

**Config:** every tunable is a `WISP_*` environment variable read once at startup into the
frozen `Config` (see `src/wisp/config.py` for the full list + defaults); change one by
exporting it and restarting. The session-secret file lives under `data/` (0600); the
dashboard PIN is a salted hash in the DB `settings` table, managed by `server/auth.py`.

## Layout

A *src layout*: the engine is an importable `wisp` package; the two runtimes that
drive it live under `apps/`. Nothing is installed; the dashboard + CLIs are stdlib,
the daemon uses the venv (see Quick start).

```
src/wisp/                 # the engine package (import as `wisp.*`)
├── config.py             # frozen Config from env; CONFIG singleton
├── version.py            # the running build version (reported in the heartbeat)
├── core/                 # business logic — state_machine.py, analytics.py, rollup.py
├── database/             # client.py (WAL conn + migration runner), outbox.py (ship queue glue)
├── ingress/              # probers.py (real ICMP via icmplib), snmp.py (IF-MIB port walk)
├── egress/               # notifiers.py (alert dispatch), ports.py, ack.py,
│                         #   shipper.py (drains the outbox to central + heartbeats)
├── central/             # the aggregation plane (Phase 10): store + server + watchdog + auth + admin CLI + static/
└── server/               # services.py (JSON data + device/worker CRUD, rollup trends),
                          #   routes.py (HTTP + auth gate), auth.py (PIN + sessions)
apps/
├── daemon/main.py        # worker runtime — the polling loop (+ the central shipper thread)
├── dashboard/            # web runtime — main.py + templates/ + static/{app.js,icons.js,vendor/}
└── central/main.py       # central server runtime — multi-edge ingest + fleet read view
data/                     # wisp.db / central.db (+ wal/shm) + session_secret — git-ignored
migrations/               # 000N_*.sql, applied in order, tracked in schema_migrations
deploy/                   # systemd units (wisp-monitor.service, wisp-dashboard.service)
tests/{unit,integration}/ # unittest — `python -m unittest discover -s tests`
docs/  assets/            # incident post-mortem template; original design mockup
run.sh                    # one-shot setup + run for both runtimes
```

## How it works (the layers)

| Module | Layer | Does |
|---|---|---|
| `wisp.ingress.probers` | 1 Monitoring | pings devices (`IcmpProber`, real ICMP via icmplib) |
| `apps.daemon.main` | 1 | 60s async poll loop; orchestrates everything |
| `wisp.core.state_machine` | 2 Pattern | FSM + flap suppression, canary freeze, topology suppression |
| `wisp.egress.notifiers` / `wisp.egress.ack` | 4/5 Alerting | routing, anti-spam, hourly all-hands re-page until recovery, ack |
| `wisp.core.analytics` | 3 BI | shared outage-window / uptime / offender query helpers for the dashboard |
| `wisp.server.{services,routes}` + `apps.dashboard` | 6 Dashboard | JSON views + stdlib HTTP server for the self-contained UI |
| `wisp.database.client` / `migrations/` | 5 Memory | WAL SQLite, durable outages/alerts/escalations |
| `wisp.egress.shipper` / `wisp.central` / `apps.central` | 7 Fleet | (optional) edge→central outbox shipper + heartbeat; central ingest + fleet view |

## Key behaviors

- **Flap suppression** — DOWN only after 3 straight 100%-loss polls; DEGRADED after 2.
  Recovery needs 2 healthy polls (hysteresis). A single blip never pages anyone.
- **Uplink canary** — if our own internet is down, freeze everything and send ONE
  `UPLINK_DOWN` instead of a storm of per-tower alerts.
- **Topology suppression** — a child is `UNREACHABLE` (one alert, not forty) only when
  **every** parent is down. With a **backup line** (a second `device_links` edge), if the
  primary path dies but a backup carries traffic the node is still genuinely reachable —
  so it isn't suppressed, and "running on backup" is surfaced as its own soft signal.
- **On-backup signal** — when a node's primary uplink fails but a backup parent keeps it
  up, the dashboard badges it "on backup" and the operator gets a single heads-up page
  ("redundancy is gone — one more failure is an outage"). It never enters the outage /
  escalation ladder (`WISP_BACKUP_ALERTS=0` keeps the badge, mutes the page).
- **SNMP port status** — for switches that speak SNMP (v2c), the daemon walks the IF-MIB
  on its own slow cadence and watches the **operator-flagged uplink/infra ports**. A
  monitored port going `oper=down` (while `admin=up`) is flap-suppressed, then — if that
  port `feeds` a device with an open outage — **folds into that outage** as the physical
  cause ("Port Gi0/2 → Tower B is down") instead of raising a competing alarm. ICMP stays
  the outage owner; SNMP confirms/enriches. Enable per device + flag ports on the Nodes page.
- **Per-port bandwidth + low-throughput alarm** — the same SNMP walk reads the 64-bit IF-MIB byte
  counters and the daemon diffs them into a live throughput rate (in/out Mbps, shown on the ports
  panel, the node row, and the topology map). Assign a per-port **minimum** threshold and the
  **direction** that matters for that link (in / out / either / total); a monitored port whose rate
  stays below it pages the operator (flap-suppressed, operator-only, `WISP_SNMP_BW_ALERTS=0` mutes
  the page). It's a soft "this uplink went quiet" signal — a port still up but no longer carrying
  traffic — and never opens an outage (a hard port-down is the down alarm's job).
- **Topology map + live port health** — the Nodes page has a **Tree ⇄ Map** toggle. The map
  is an interactive node-link graph (pan/zoom, click a node for a live detail card) that draws
  all three relationship layers at once: the primary ping parent, **backup uplinks** (`device_links`),
  and the physical **SNMP port-feed** links (`switch_ports.feeds_device_id`), colour-coded by state.
  A switch's port trouble is now surfaced **live** on its row/node (a "2/8 ports down" badge), not
  buried in the edit modal. Served by `GET /api/topology`; the per-switch port summary rides on
  `GET /api/nodes`.
- **Post-mortem cause** — at resolution the operator records the confirmed root cause + notes
  (there is no automatic power-vs-link guess).
- **Escalation is restart-safe** — timers live in the DB, not memory; a crash can't drop them.
- **Scales without lying** — probes are fanned out under a concurrency cap
  (`WISP_MAX_INFLIGHT`) so a large fleet never exhausts file descriptors and fakes a mass
  outage; aggregation gear (towers/switches/APs) is probed *gently* (`WISP_PINGS_PER_POLL_INFRA`)
  so its control-plane ICMP rate-limiter doesn't read as phantom loss.
- **Hourly rollups** — raw polls are hot scratch; the daemon folds them into compact
  per-device/hour rows (`poll_rollups`) once an hour, so trend charts read hours, not a
  billion raw samples (`services.device_trend`). Incidents still live in `outages`.
- **Central reporting (optional, off by default)** — set `WISP_CENTRAL_URL` and an edge
  *also* reports its outage events + hourly rollups + a liveness heartbeat to a central
  server, over a store-and-forward outbox (a WAN blip just grows the queue; an outage record
  is never lost). The edge keeps detecting + paging **locally** — central only aggregates a
  fleet-wide picture; it never runs an FSM and never pages. With `WISP_CENTRAL_URL` **unset**
  the whole layer is dormant and the edge is byte-for-byte the standalone monitor. See below.

## Central reporting (Phase 10 Parts A–C — distributed, multi-tenant; optional)

By default this is **one box** — one daemon + one dashboard, standalone. Phase 10 lets many
**edge** nodes (each is today's daemon, unchanged) across many ISPs/tenants report up to **one
central** server with its **own multi-tenant dashboard**. It is strictly additive and **off
unless `WISP_CENTRAL_URL` is set** — that empty-URL case is the hard back-compat anchor (no
outbox writes, no shipper thread, identical behaviour).

What ships, and how:
- **The edge owns the page; central owns the picture.** Detection (FSM, fast-confirm,
  between-cycle watch), topology suppression, the canary, SNMP, and **local ntfy alerting all
  stay on the edge** — the WAN is most likely to break *during* an outage, so the alarm must
  survive the thing it alarms about. Central never pages (no double-paging).
- **Store-and-forward outbox.** The daemon enqueues outage **events** (in the *same
  transaction* as the `poll_results`/`outages` write, so a record is never half-written) and
  the hourly **rollups** into a local `outbox` table. A background **shipper thread** drains
  it over HTTPS, deletes each row only on a central ack, exponentially backs off on failure,
  and past a high-water mark evicts the oldest **rollups** only — an unsent **event** is an
  outage record and is never dropped.
- **Heartbeat.** Every `WISP_HEARTBEAT_INTERVAL_S` the shipper POSTs a live liveness +
  health beat (version, last-poll time, fleet size, open-outage count, outbox backlog). It is
  sent *direct* (not queued — a stale "I'm alive" is worthless) and is the signal a future
  cross-edge watchdog keys off (box dead **or** WAN cut).
- **Wire protocol.** A versioned JSON envelope (`v`, `tenant_id`, `node_id`, `kind`, …); the
  edge identity is `(tenant_id, node_id)`. Auth is a bearer token (`WISP_CENTRAL_TOKEN`) for
  now — mTLS enrollment is a later part; the envelope is shaped so it slots in without a wire
  change. Delivery is at-least-once + central storage is idempotent on the edge's outbox id
  (a lost ack just re-ships rows central already holds), i.e. effectively-once.

**Multi-tenant central (Part B).** Central is keyed by `(tenant_id, node_id)`: orgs (ISPs)
are auto-provisioned on first contact, every node belongs to an org, and **central assigns its
own global device ids** — an edge's per-SQLite `device_id` can't be merged across nodes, so the
central `devices` table maps each `(tenant, node, edge-local id)` to a global id (the same
edge-local id from two nodes gets two global ids, never collides). The read API is **tenant-
scoped** (`?tenant=` narrows any read; absent = whole fleet). A **cross-edge fleet watchdog**
pages a node's org when its heartbeat goes stale (box dead or WAN cut) — the dead-monitor
watchdog one level up, restart-safe and conservative, routing to the org's ntfy topic.

**Per-org dashboard + accounts (Part C).** Central has its **own dashboard** (a separate,
pure-stdlib SPA) with **per-org login accounts** — the edge's single shared PIN doesn't survive
multi-tenancy. Two auth planes, deliberately separate: **ingest** stays a machine **bearer
token** (`WISP_CENTRAL_TOKEN`), while the **dashboard** uses per-user, identity-carrying
signed-cookie sessions. Accounts are **central-provisioned** (no public signup): a *superadmin*
(the platform operator) onboards each ISP and seeds its accounts; org users are scoped to their
tenant with a role (owner/operator/tech), and every dashboard read is auto-scoped to the
caller's org (a superadmin sees all and can narrow with `?tenant=`). **Team + attendance are now
org-wide central concepts** ("who's on duty" is an org fact); the live per-outage paging ladder
stays **on the edge** (resilience — the edge owns the page, central owns the picture).

Run the central server (a **separate** process, its own SQLite at `WISP_CENTRAL_DB`), then
bootstrap the first account:
```bash
WISP_CENTRAL_TOKEN=s3cret python apps/central/main.py                  # ingest + dashboard on :8443
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you   # then log in at /
PYTHONPATH=src python -m wisp.central.admin create-user --tenant ispA --username asha --role owner
# the read API also accepts the bearer token (curl / automation), treated as a cross-tenant reader:
curl -H 'Authorization: Bearer s3cret' 'http://HOST:8443/api/devices?tenant=ispA'   # global device view
```
Then point an edge at it (on the daemon's systemd unit):
```bash
WISP_CENTRAL_URL=https://central.example.net WISP_CENTRAL_TOKEN=s3cret \
WISP_TENANT_ID=ispA WISP_NODE_ID=edge-a1 python apps/daemon/main.py
```
Put the central server behind a TLS terminator (nginx/Caddy) in production — it speaks plain
HTTP itself to stay dependency-free. The frozen-binary fleet **rollout/self-update + CI/CD**
(Part D) is the remaining Phase 10 part (`plan.md`).

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | seconds between polls (steady-state cadence; see fast-confirm below) |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm: re-probe a lossy device every Ns until DOWN is confirmed (0 = off) |
| `WISP_POLL_INTERVAL_ADAPTIVE` | `0` | `1` = poll faster on a small fleet (see below) |
| `WISP_POLL_INTERVAL_SMALL_S` | `30` | cadence used while the fleet ≤ `WISP_SMALL_FLEET_MAX` (adaptive on) |
| `WISP_SMALL_FLEET_MAX` | `1000` | fleet size at/below which the small cadence applies |
| `WISP_PINGS_PER_POLL` | `5` | echoes per poll for leaf devices (CPEs) |
| `WISP_PINGS_PER_POLL_INFRA` | `2` | echoes per poll for aggregation gear (any device that is a parent) |
| `WISP_MAX_INFLIGHT` | `256` | max concurrent probes in flight (0 = unbounded); caps FD use at scale |
| `WISP_POLL_RETENTION_DAYS` | `7` | days of raw poll samples kept (scratch; hourly rollups + `outages` are the durable record) |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages while an outage stays open |
| `WISP_BACKUP_ALERTS` | `1` | `0` = keep the on-backup badge but mute the operator page |
| `WISP_SNMP_INTERVAL_S` | `30` | seconds between SNMP walks — port status + live bandwidth (0 = SNMP ingress off); raise to ease switch load, lower for fresher rates |
| `WISP_SNMP_DOWN_CONSECUTIVE` | `2` | consecutive down walks before a monitored port alarms |
| `WISP_SNMP_ALERTS` | `1` | `0` = keep port state/badges but mute the operator page |
| `WISP_SNMP_BW_CONSECUTIVE` | `3` | consecutive below-threshold walks before a monitored port's low-bandwidth alarm |
| `WISP_SNMP_BW_ALERTS` | `1` | `0` = keep the low-bandwidth state/badge but mute the operator page |
| `WISP_NTFY_URL` | `https://ntfy.sh` | ntfy base URL |
| `WISP_NTFY_TOPIC_{OWNER,OPERATOR,TECH}` | `hansa-*` | the three role topics alerts route to |
| `WISP_DASHBOARD_PIN` | — | seed the dashboard PIN on first run (else set it in the UI) |
| `WISP_CENTRAL_URL` | — | central ingest base URL; **empty = standalone** (no reporting) |
| `WISP_CENTRAL_TOKEN` | — | bearer token the edge presents / central requires |
| `WISP_TENANT_ID` / `WISP_NODE_ID` | `default` / hostname | edge identity central keys records by |
| `WISP_HEARTBEAT_INTERVAL_S` | `60` | seconds between liveness heartbeats |
| `WISP_SHIP_INTERVAL_S` / `WISP_SHIP_BATCH` | `5` / `200` | shipper drain cadence + max records per POST |
| `WISP_OUTBOX_MAX_ROWS` | `100000` | outbox high-water mark (evicts oldest rollups; never events; 0 = unbounded) |
| `WISP_CENTRAL_DB` / `WISP_CENTRAL_BIND` / `WISP_CENTRAL_PORT` | `data/central.db` / `0.0.0.0` / `8443` | central server store + listen address |
| `WISP_CENTRAL_NODE_STALE_S` | `180` | central pages an org when a node's heartbeat is older than this (box dead / WAN cut) |
| `WISP_CENTRAL_NTFY_TOPIC` | `wisp-central` | fallback fleet-watchdog topic when an org has set none |

**Config is env-var only** — every tunable is read once at startup into the frozen `Config`
(`config.py` has the full list + defaults). There is no in-UI settings page and no DB config
layer: change a value by exporting the env var and restarting the daemon. (Device/team edits
*are* live in the UI; *tunables* are not.) The dashboard's **Settings** page is just the test
alert, PIN change, and DB backup.

**How fast we detect DOWN.** DOWN still needs 3 consecutive 100%-loss samples (flap
suppression), but those samples no longer wait a full poll interval each. **Fast-confirm**
(`WISP_RETRY_INTERVAL_S`, default 2s, on by default) re-probes *only* the device that just read
100% loss, back-to-back, until it either confirms DOWN or comes back reachable — so detection is
**~4 seconds**, not `3 × poll_interval`, and the healthy fleet is never re-probed. A reachable
retry clears the suspicion, so a blip never pages. Set `WISP_RETRY_INTERVAL_S=0` to disable it
(detection falls back to `down_consecutive × poll_interval`).

`WISP_POLL_INTERVAL_S` is therefore now mostly the **steady-state probe load** dial, not the
detection-latency dial. For load control on a small fleet you can still set
`WISP_POLL_INTERVAL_ADAPTIVE=1` (poll every 30s while ≤1k devices, auto-back-off above that,
re-evaluated on device-set reload).

## Going live (on the always-on box)

1. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`,
   then enable unprivileged ICMP: `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"`
   (persist it in `/etc/sysctl.d/`) so the daemon can ping without root.
2. Enter the real device inventory + topology from the dashboard **Nodes** page.
3. Set the ntfy base URL and the three role topics (`WISP_NTFY_URL`,
   `WISP_NTFY_TOPIC_*`) in the systemd units, add your team (owner + techs) on the **Team**
   page so each person knows which role topic to subscribe to, then use **Settings ▸ Send
   test alert** to confirm routing.
4. Tune thresholds/cadence (`WISP_POLL_INTERVAL_S`, `WISP_LOSS_DEGRADED`, etc.) against how the
   real links actually blip, then restart the daemon.
5. Run both processes under systemd for auto-start and crash-restart:
   ```bash
   sudo cp deploy/wisp-*.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now wisp-monitor wisp-dashboard
   ```
   (Adjust `WorkingDirectory`/`User` in the units; the dashboard is plain HTTP + PIN —
   keep it on the office LAN, not the public internet. See plan §8.2.)
