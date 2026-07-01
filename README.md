# Village WISP Monitor

A multi-tenant network monitoring + alerting platform for rural WiFi broadband
operators (ISPs). Central runs the brain: the FSM, topology-aware suppression
(a dead parent's children don't page separately), fast-confirm detection, and
the ntfy alerting ladder (owner+operator immediately, then owner+operator+tech
every hour the outage stays open) all run on a central server you operate. Each
ISP's edge box is a **thin probe** — it pings its network with real ICMP and
reports raw results to central; it carries no local database, dashboard, or
alerting of its own.

ISPs log into the central dashboard with their own account, add their device
topology and team, and see live outages/history there — nothing to manage on
the edge box beyond keeping the probe running. See `plan.md` for the design
rationale, what's done, and what's next.

## Quick start (local dev — central + one edge probe)

```bash
# one-time: deps for the edge probe (ICMP + central HTTP)
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
# let the probe send ICMP as a normal user (unprivileged ping sockets):
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
echo 'net.ipv4.ping_group_range=0 2147483647' | sudo tee /etc/sysctl.d/99-wisp-ping.conf
```

**Fastest path:** `./run.sh` — starts a local central server on
`http://127.0.0.1:8080` and, alongside it, an edge probe in central-brain mode
pointed at it. Central starts with no orgs/devices; create a superadmin, log
in, and add your first device from the dashboard's **Nodes** page:

```bash
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you --password ...
```

The `wisp` package lives under `src/` (a *src layout*) but nothing is
installed — the two runtimes (`apps/daemon`, `apps/central`) put `src/` on the
path themselves; the admin CLI uses `PYTHONPATH=src python -m …`.

```bash
# run central and an edge probe separately (what run.sh does under the hood):
python apps/central/main.py --host 0.0.0.0 --port 8443

WISP_CENTRAL_BRAIN=1 WISP_CENTRAL_URL=http://127.0.0.1:8443 \
WISP_TENANT_ID=ispA WISP_NODE_ID=edge-a1 python apps/daemon/main.py

python -m unittest discover -s tests                 # pure stdlib
```

The central dashboard is **fully self-contained** — pure stdlib, no build
step, no third-party Python deps for the dashboard itself (the edge probe
needs the small venv above). All central state lives under `data/central.db`
(git-ignored).

## Central dashboard (control plane)

An ISP owner/operator runs and reconfigures everything from the central
browser dashboard — nothing on the edge box needs touching after install.

- **Nodes** — the device inventory + topology (name, IP, type, region,
  parent), full CRUD from the UI. A newly added/removed/reparented device
  applies to the live probe within one poll cycle (the edge re-fetches its
  topology from central every cycle — no edge restart needed).
- **Team** — workers as identity + role (owner / operator / tech), *not*
  per-person routing. Alerts route to **three per-org ntfy topics, one per
  role**, set from **Settings**; a person subscribes to the topic for their
  role. Team also carries org-wide **Attendance** (a daily present-toggle for
  operators).
- **Settings** — each org's three role ntfy topics + a **Send test alert**
  button.
- **Accounts** — central-provisioned (no public signup): a *superadmin* (the
  platform operator, `wisp.central.admin create-superadmin`) onboards each ISP
  and seeds its accounts (`create-user --tenant … --role owner|operator|tech`);
  org users only ever see their own tenant's data.

## Layout

A *src layout*: the engine is an importable `wisp` package; the two runtimes
that drive it live under `apps/`. Central is pure stdlib; the edge probe uses
the venv (see Quick start).

```
src/wisp/                 # the engine package (import as `wisp.*`)
├── config.py             # frozen Config from env; CONFIG singleton
├── version.py            # the running build version (reported in the heartbeat)
├── core/                 # state_machine.py (FSM, reused by central), analytics.py, baseline.py
├── database/             # client.py (WAL conn + migration runner; used by state_machine's DB glue)
├── ingress/               # probers.py (real ICMP via icmplib), snmp.py (IF-MIB port walk;
│                          #   walked by the edge daemon, folded/alerted by central/ports.py)
├── egress/               # notifiers.py — the ntfy channel (NtfyNotifier/send_with_retry),
│                          #   shared by the edge probe's error paths and central's dispatcher
├── central/              # THE BRAIN: engine.py + dispatch.py (FSM/alerting), ports.py (SNMP
│                          #   port folding), analytics.py (outage-derived downtime/SLA),
│                          #   rollup.py (hourly latency/loss trend, 30d retention),
│                          #   store (multi-tenant SQLite), server.py (ingest + dashboard
│                          #   API), watchdog, auth, admin CLI, rollout, inventory,
│                          #   static/ (the dashboard SPA)
└── runtime/               # central_client.py (edge's central HTTP client), single_instance.py,
                           #   supervisor.py (agent self-update logic)
apps/
├── daemon/main.py        # the edge runtime — thin probe loop only (probe, report, follow
│                          #   fast-confirm hints); no local FSM, DB, or dashboard
├── central/main.py       # central server runtime — ingest + multi-tenant dashboard + watchdog
└── supervisor/main.py    # edge supervisor — runs + self-updates the frozen agent (fleet path)
data/                     # central.db (+ wal/shm) + session_secret — git-ignored
migrations/               # 000N_*.sql for central's underlying wisp.database.client schema (see
                           #   core/state_machine.py's DB-glue tests) — central's own store.py
                           #   schema is separate (executescript in central/store.py)
deploy/                   # systemd units + install scripts + PyInstaller spec (single-box + fleet)
.github/workflows/        # release.yml — build/test on push/PR; sign+publish on a v* tag
tests/{unit,integration}/ # unittest — `python -m unittest discover -s tests`
docs/  assets/            # incident post-mortem template; original design mockup
run.sh                    # local dev: central + one edge probe together
```

## How it works (the layers)

| Module | Layer | Does |
|---|---|---|
| `wisp.ingress.probers` | 1 Monitoring | pings devices (`IcmpProber`, real ICMP via icmplib) |
| `apps.daemon.main` | 1 | the edge's thin probe loop — probe, `POST /report`, follow fast-confirm hints |
| `wisp.core.state_machine` | 2 Pattern | FSM + flap suppression, canary freeze, topology suppression (reused verbatim by central) |
| `wisp.central.engine` | 2 | central-native DB glue over the FSM: per-tenant `EngineRegistry`, `compute_recheck` |
| `wisp.central.dispatch` | 4/5 Alerting | routing, anti-spam, hourly all-hands re-page until recovery — central's `AlertDispatcher` |
| `wisp.egress.notifiers` | 4 | the ntfy channel itself (`NtfyNotifier`, retry policy) — used by central's dispatcher |
| `wisp.central.store` | 5 Memory | central's own multi-tenant SQLite: org topology, team, live device state, outages |
| `wisp.central.server` / `apps.central` | 6 Dashboard + Ingest | `GET/POST /api/*` for the SPA, `POST /report` + `GET /edge/devices` for edges |
| `wisp.runtime.supervisor` / `apps.supervisor` | 8 Update | (optional) supervisor self-updates the frozen agent: verify → swap → health-gate → rollback |

## Key behaviors (running on central)

- **Flap suppression** — DOWN only after 3 straight 100%-loss samples; DEGRADED after 2.
  Recovery needs 2 healthy samples (hysteresis). A single blip never pages anyone.
- **Fast-confirm round trip** — when a device looks suspect, central's reply to
  `POST /report` carries a `recheck` hint naming just that IP; the edge re-probes it
  every `WISP_RETRY_INTERVAL_S` and reports back until the FSM confirms or clears it —
  so detection collapses from `poll_interval × down_consecutive` to a few seconds
  without touching the healthy fleet's cadence.
- **Uplink canary** — if an edge's own internet is down, central freezes that tenant's
  detection for the cycle and sends ONE `UPLINK_DOWN` instead of a storm of per-site alerts.
- **Topology suppression** — a child is `UNREACHABLE` (one alert, not forty) only when
  every monitored parent is down; a genuinely-down device with any live parent still
  pages. (Backup/redundant uplinks are an edge-only soft-signal tier from before this
  migration — not yet ported to central-brain mode; see `CLAUDE.md`.)
- **SNMP port folding** — the edge walks its snmp-enabled switches on its own slow
  cadence (`WISP_SNMP_INTERVAL_S`, independent of the ICMP poll interval) and reports
  port readings alongside its pings; central folds a monitored port-down into the open
  outage it feeds (stamping the physical cause) or, with no open outage yet, sends a
  one-shot operator heads-up — never a second, competing alarm. Admin-down ports and
  unmonitored (undiscovered) ports stay silent. See `CLAUDE.md`'s "Central runs the
  brain" for the full rule set.
- **Outage-derived SLA reporting** — `GET /api/analytics?days=` answers "how reliable
  was Tower A over the last N days" straight off the outage history central already
  keeps: per-device downtime seconds, uptime %, and outage count (UNREACHABLE outages
  don't count against a device's own uptime — that's a topology-suppressed artifact of
  a dead parent, not this device's fault). No new storage; a device with zero outages
  in the window still reports 100% up.
- **Latency/loss trend** — `GET /api/analytics/trend?device_id=&days=` returns hourly
  average-latency/loss/down-percentage buckets (30-day retention, pruned by a daily
  background sweep), folded incrementally from each "full" report cycle's samples —
  never a recheck, which would skew an hour's average with its rapid re-probe of just
  the suspect subset.
- **Escalation is restart-safe** — timers live in central's DB, not memory; a crash
  can't drop them. A fresh DOWN pages owner+operator immediately; while it stays open,
  an all-hands page (owner+operator+tech) fires every `WISP_ESCALATE_EVERY_MIN` with the
  running duration. Acknowledgement doesn't stop the clock — only recovery does.
- **Scales without lying** — probes fan out under a concurrency cap
  (`WISP_MAX_INFLIGHT`) so a large fleet never exhausts file descriptors and fakes a mass
  outage; aggregation gear (towers/switches/APs) is probed *gently*
  (`WISP_PINGS_PER_POLL_INFRA`) so its control-plane ICMP rate-limiter doesn't read as
  phantom loss.
- **Multi-tenant, always.** Central is keyed by tenant; an org user only ever sees their
  own org's devices/team/outages. A superadmin sees all and can narrow with `?tenant=`.

## Central server + edge setup

Run the central server (its own SQLite at `WISP_CENTRAL_DB`), bootstrap accounts, then
point an edge at it:
```bash
WISP_CENTRAL_TOKEN=s3cret python apps/central/main.py --host 0.0.0.0 --port 8443
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you   # then log in at /
PYTHONPATH=src python -m wisp.central.admin create-user --tenant ispA --username asha --role owner
# the read API also accepts the bearer token (curl / automation), treated as a cross-tenant reader:
curl -H 'Authorization: Bearer s3cret' 'http://HOST:8443/api/devices?tenant=ispA'
```
```bash
# on the edge box's systemd unit (deploy/wisp-monitor.service):
WISP_CENTRAL_BRAIN=1 WISP_CENTRAL_URL=https://central.example.net WISP_CENTRAL_TOKEN=s3cret \
WISP_TENANT_ID=ispA WISP_NODE_ID=edge-a1 python apps/daemon/main.py
```
Put the central server behind a TLS terminator (nginx/Caddy) in production — it speaks
plain HTTP itself to stay dependency-free.

**Fleet deploy + self-update.** Edges can instead ship as a **frozen single binary**
(PyInstaller — no Python/venv on the box); a small stable **supervisor** runs the agent
and owns `download → verify(sha256) → atomic-swap → restart → health-gate → rollback`.
Central is the **version authority** with a staged, health-gated rollout — a canary
subset updates first and the rollout auto-promotes only once the canaries come back
healthy on the target, else it auto-halts:
```bash
PYTHONPATH=src python -m wisp.central.admin publish-release --version 0.11.0 \
    --artifact linux-amd64 https://.../wisp-edge-linux-amd64 <sha256>
PYTHONPATH=src python -m wisp.central.admin start-rollout --tenant ispA --version 0.11.0 --canary edge-a1
PYTHONPATH=src python -m wisp.central.admin rollout-status --tenant ispA
```
Linux install is `curl … | sudo sh -s -- --central … --token … --tenant … --node …`
(`deploy/install-edge.sh`: arch-detect → download → **verify sha256** → systemd unit →
ICMP sysctl). CI (`.github/workflows/release.yml`) builds + tests on every push/PR and,
**only on a `v*` tag**, signs/packages/publishes a Release with a version manifest
central ingests. The single-box venv path (`deploy/install.sh`) still exists for a
simpler systemd-managed probe; the frozen binary + supervisor is the *fleet* path.

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | seconds between polls (steady-state cadence; see fast-confirm below) |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm: re-probe a named suspect every Ns until central confirms/clears it (0 = off) |
| `WISP_POLL_INTERVAL_ADAPTIVE` | `0` | `1` = poll faster on a small fleet (see below) |
| `WISP_POLL_INTERVAL_SMALL_S` | `30` | cadence used while the fleet ≤ `WISP_SMALL_FLEET_MAX` (adaptive on) |
| `WISP_SMALL_FLEET_MAX` | `1000` | fleet size at/below which the small cadence applies |
| `WISP_PINGS_PER_POLL` | `5` | echoes per poll for leaf devices (CPEs) |
| `WISP_PINGS_PER_POLL_INFRA` | `2` | echoes per poll for aggregation gear (any device that is a parent) |
| `WISP_MAX_INFLIGHT` | `256` | max concurrent probes in flight (0 = unbounded); caps FD use at scale |
| `WISP_SNMP_INTERVAL_S` | `90` | seconds between SNMP port walks (0 = off); independent of the ICMP poll cadence |
| `WISP_SNMP_DOWN_CONSECUTIVE` | `2` | consecutive down walks a *monitored* port needs before central alarms it |
| `WISP_SNMP_ALERTS` | `1` | `0` = mute the operator port-down page (the `switch_ports` state is still written) |
| `WISP_SNMP_TIMEOUT_S` | `2.0` | per-switch SNMP request timeout (a dead switch must never block the ICMP cycle) |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages while an outage stays open |
| `WISP_NTFY_URL` | `https://ntfy.sh` | ntfy base URL |
| `WISP_CENTRAL_BRAIN` | `0` | `1` = run the edge as a thin probe (needs `WISP_CENTRAL_URL` too — this is the only mode) |
| `WISP_CENTRAL_URL` | — | central base URL the edge probes report to |
| `WISP_CENTRAL_TOKEN` | — | bearer token the edge presents / central requires for ingest |
| `WISP_TENANT_ID` / `WISP_NODE_ID` | `default` / hostname | edge identity central keys records by |
| `WISP_CENTRAL_DB` / `WISP_CENTRAL_BIND` / `WISP_CENTRAL_PORT` | `data/central.db` / `0.0.0.0` / `8443` | central server store + listen address |
| `WISP_CENTRAL_NODE_STALE_S` | `180` | central pages an org when a node's heartbeat is older than this (box dead / WAN cut) |
| `WISP_CENTRAL_NTFY_TOPIC` | `wisp-central` | fallback fleet-watchdog topic when an org has set none |
| `WISP_ROLLOUT_HEALTH_WINDOW_S` | `600` | how long a canary has to come back healthy on the target before the rollout auto-halts |
| `WISP_AGENT_HEALTH_DEADLINE_S` | `300` | how long a freshly-swapped agent has to prove healthy before the supervisor rolls back |
| `WISP_VERSION` | — | override the reported build version (CI stamps it from `git describe`) |

**Config is env-var only** — every tunable is read once at startup into the frozen
`Config` (`config.py` has the full list + defaults). There is no in-UI settings page and
no DB config layer: change a value by exporting the env var and restarting the edge
probe. Per-org alert topics, device topology, and team *are* live in the central
dashboard; process-level tunables above are not.

**How fast we detect DOWN.** DOWN still needs 3 consecutive 100%-loss samples (flap
suppression), but those samples don't wait a full poll interval each: central's
fast-confirm reply names the suspect IP and the edge re-probes it every
`WISP_RETRY_INTERVAL_S` until confirmed or cleared — detection in seconds, not
`down_consecutive × poll_interval`. Set `WISP_RETRY_INTERVAL_S=0` to disable it.

## Going live (edge box)

1. `sudo deploy/install.sh` on the edge box — installs deps, the venv, the unprivileged
   ICMP sysctl, and the `wisp-monitor` systemd unit (see the script for what it does).
2. `sudo systemctl edit --full wisp-monitor` and set `WISP_CENTRAL_URL` /
   `WISP_CENTRAL_TOKEN` / `WISP_TENANT_ID` (and optionally `WISP_NODE_ID`) to the values
   your central operator gave you, then `sudo systemctl enable --now wisp-monitor`.
3. From the **central** dashboard: enter the real device inventory + topology (Nodes),
   add your team (Team), set the three role ntfy topics (Settings) and confirm routing
   with **Send test alert**.
4. Tune thresholds/cadence (`WISP_POLL_INTERVAL_S`, etc.) against how the real links
   actually blip, then restart `wisp-monitor`.

For a fleet of many edges instead of one box at a time, see "Fleet deploy + self-update"
above.
