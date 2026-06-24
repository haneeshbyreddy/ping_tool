# Village WISP Monitor

A network monitoring + alerting tool for a rural WiFi broadband operator. It pings the
shared infrastructure (towers, relays, backhaul, core), figures out **what is down and
where** (topology-aware, so a dead parent suppresses its children), and pages the operator
immediately — then re-pages the whole team (owner + operator + tech) every hour the outage
stays open, with the running duration, until it recovers.

It polls with real ICMP and alerts over ntfy push. The dashboard + admin CLIs are pure
stdlib; the **daemon** needs a small venv (`icmplib`/`httpx`) and the kernel ping group enabled.
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

python -m unittest discover -s tests                 # 58 tests (pure stdlib)
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
- **Team** — workers as first-class entities (owner / operator / tech) with phone +
  ntfy routing. The `owner` receives escalations; you can't remove
  the last active owner.
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
├── core/                 # business logic — state_machine.py, analytics.py
├── database/             # client.py (WAL conn + migration runner)
├── ingress/              # probers.py (real ICMP ping via icmplib)
├── egress/               # notifiers.py (alert dispatch), ack.py
└── server/               # services.py (JSON data + device/worker/settings CRUD),
                          #   routes.py (HTTP + auth gate), auth.py (PIN + sessions)
apps/
├── daemon/main.py        # worker runtime — the 60s polling loop
└── dashboard/            # web runtime — main.py + templates/ + static/{app.js,icons.js,vendor/}
data/                     # wisp.db (+ wal/shm) + session_secret — git-ignored
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

## Key behaviors

- **Flap suppression** — DOWN only after 3 straight 100%-loss polls; DEGRADED after 2.
  Recovery needs 2 healthy polls (hysteresis). A single blip never pages anyone.
- **Uplink canary** — if our own internet is down, freeze everything and send ONE
  `UPLINK_DOWN` instead of a storm of per-tower alerts.
- **Topology suppression** — a child of a down parent is `UNREACHABLE` (one alert, not forty).
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

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | seconds between polls |
| `WISP_PINGS_PER_POLL` | `5` | echoes per poll for leaf devices (CPEs) |
| `WISP_PINGS_PER_POLL_INFRA` | `2` | echoes per poll for aggregation gear (any device that is a parent) |
| `WISP_MAX_INFLIGHT` | `256` | max concurrent probes in flight (0 = unbounded); caps FD use at scale |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages while an outage stays open |
| `WISP_NTFY_URL` | `https://ntfy.sh` | ntfy base URL |
| `WISP_DASHBOARD_PIN` | — | seed the dashboard PIN on first run (else set it in the UI) |

Full list in `config.py`. Most of these are now editable from **Settings** in the UI and
the DB value overrides the env var (the env is bootstrap / a deploy-time override).

## Going live (on the always-on box)

1. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`,
   then enable unprivileged ICMP: `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"`
   (persist it in `/etc/sysctl.d/`) so the daemon can ping without root.
2. Enter the real device inventory + topology from the dashboard **Nodes** page.
3. Set the ntfy base URL in **Settings ▸ Channels**, add your team (owner + techs with
   their ntfy topics) on the **Team** page, then use **Send test alert** to confirm routing.
4. Tune thresholds against how the real links actually blip (Settings ▸ Detection).
5. Run both processes under systemd for auto-start and crash-restart:
   ```bash
   sudo cp deploy/wisp-*.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now wisp-monitor wisp-dashboard
   ```
   (Adjust `WorkingDirectory`/`User` in the units; the dashboard is plain HTTP + PIN —
   keep it on the office LAN, not the public internet. See plan §8.2.)
