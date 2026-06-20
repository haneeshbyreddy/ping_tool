# Village WISP Monitor

A network monitoring + alerting tool for a rural WiFi broadband operator. It pings the
shared infrastructure (towers, relays, backhaul, core), figures out **what is down, where,
and the likely cause** — power vs link/equipment — and pushes that to the operator and the
right field technician, escalating if nobody responds.

Built mock-first: it runs end-to-end on a laptop with **no hardware, no credentials, no
root** (simulated pings + a mock notifier), then swaps to real ICMP + ntfy/Telegram by
flipping two env vars. See `plan.md` for the full design and `broadband-monitor-idea-doc`
notes inside it.

## Quick start (no dependencies)

**Fastest path:** `./run.sh --demo` — migrates, seeds a demo network with live
outages, and runs both the worker and the dashboard on http://127.0.0.1:8000
(Ctrl-C stops both). The rest of this section shows the pieces it orchestrates.

The `wisp` package lives under `src/` (a *src layout*) but nothing is installed —
the two runtimes (`apps/daemon`, `apps/dashboard`) put `src/` on the path
themselves; the admin CLIs use `PYTHONPATH=src python -m …`.

```bash
PYTHONPATH=src python -m wisp.database.client        # create DB (WAL) + run migrations
PYTHONPATH=src python -m wisp.database.seed --reset  # load demo network (8 devices, real topology)

# watch the worker run — simulated outages, flap suppression, alerts, recovery:
python apps/daemon/main.py --interval 1 --cycles 13

# operator views:
PYTHONPATH=src python -m wisp.core.analytics status     # live board: who's up/down right now
PYTHONPATH=src python -m wisp.core.analytics digest     # daily summary (uptime, power-vs-equipment, ₹)
PYTHONPATH=src python -m wisp.core.analytics devices    # per-device uptime
PYTHONPATH=src python -m wisp.core.analytics offenders  # repeat-offender ranking

PYTHONPATH=src python -m wisp.egress.ack                # list open outages
PYTHONPATH=src python -m wisp.egress.ack <id> "Your Name"   # acknowledge (stops escalation)

# operator dashboard (browser UI over the same live DB):
python apps/dashboard/main.py                        # http://127.0.0.1:8000  (Ctrl-C to stop)

python -m unittest discover -s tests                 # 30 tests
```

The dashboard is **fully self-contained** — Tailwind and the Material icons are
vendored under `apps/dashboard/static/`, so it works on a site with no internet,
no build step, and no third-party Python deps. The daemon and the web server are
**decoupled runtimes** that run side by side (WAL SQLite lets the writer and the
dashboard reader coexist); all local state lives under `data/` (git-ignored).

From the **Nodes** page you can add / edit / delete devices (the whole inventory —
IP, type, criticality, parent, power-ref, technician, customers, revenue) from the
UI. Newly added or removed nodes start/stop being *monitored* after the next
daemon restart (the engine snapshots the device set at startup).

## Layout

A *src layout*: the engine is an importable `wisp` package; the two runtimes that
drive it live under `apps/`. Nothing is installed (see "no dependencies" above).

```
src/wisp/                 # the engine package (import as `wisp.*`)
├── config.py             # frozen Config from env; CONFIG singleton
├── core/                 # business logic — state_machine.py, analytics.py
├── database/             # client.py (WAL conn + migration runner), seed.py (demo data)
├── ingress/              # probers.py (ping; simulated | icmp)
├── egress/               # notifiers.py (alert dispatch), ack.py
└── server/               # services.py (JSON data + device CRUD), routes.py (HTTP layer)
apps/
├── daemon/main.py        # worker runtime — the 60s polling loop
└── dashboard/            # web runtime — main.py + templates/ + static/{app.js,icons.js,vendor/}
data/                     # wisp.db (+ wal/shm) — git-ignored
migrations/               # 000N_*.sql, applied in order, tracked in schema_migrations
tests/{unit,integration}/ # unittest — `python -m unittest discover -s tests`
docs/  assets/            # incident post-mortem template; original design mockup
run.sh                    # one-shot setup + run for both runtimes
```

## How it works (the layers)

| Module | Layer | Does |
|---|---|---|
| `wisp.ingress.probers` | 1 Monitoring | pings devices (`SimulatedProber` now / `IcmpProber` later) |
| `apps.daemon.main` | 1 | 60s async poll loop; orchestrates everything |
| `wisp.core.state_machine` | 2 Pattern | FSM + flap suppression, canary freeze, topology suppression, power-vs-link |
| `wisp.egress.notifiers` / `wisp.egress.ack` | 4/5 Alerting | routing, anti-spam, T+10/T+20 escalation ladder, ack |
| `wisp.core.analytics` | 3 BI | status board, daily digest, uptime, offenders |
| `wisp.server.{services,routes}` + `apps.dashboard` | 6 Dashboard | JSON views + stdlib HTTP server for the self-contained UI |
| `wisp.database.client` / `migrations/` | 5 Memory | WAL SQLite, durable outages/alerts/escalations |

## Key behaviors

- **Flap suppression** — DOWN only after 3 straight 100%-loss polls; DEGRADED after 2.
  Recovery needs 2 healthy polls (hysteresis). A single blip never pages anyone.
- **Uplink canary** — if our own internet is down, freeze everything and send ONE
  `UPLINK_DOWN` instead of a storm of per-tower alerts.
- **Topology suppression** — a child of a down parent is `UNREACHABLE` (one alert, not forty).
- **Power vs link** — a real DOWN is tagged "Likely Power Outage" or "Link/Equipment Fault"
  so a tech brings the right gear.
- **Escalation is restart-safe** — timers live in the DB, not memory; a crash can't drop them.

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `WISP_PROBER` | `simulated` | `simulated` or `icmp` (real, needs root) |
| `WISP_NOTIFIER` | `mock` | `mock`, `ntfy`, or `telegram` |
| `WISP_POLL_INTERVAL_S` | `60` | seconds between polls |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_REALERT_MIN` / `WISP_ESCALATE_MIN` | `10` / `20` | escalation timing |
| `WISP_NTFY_URL` / `WISP_TG_TOKEN` / `WISP_OWNER_CHAT` | — | channel credentials |

Full list in `config.py`.

## Going live (Phase 7, needs the real network)

1. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
2. Enter the real device inventory + topology from the dashboard **Nodes** page
   (or replace the demo rows in `src/wisp/database/seed.py`).
3. `WISP_PROBER=icmp` (run with `sudo`/`cap_net_raw`), `WISP_NOTIFIER=ntfy` (or `telegram`).
4. Tune thresholds against how the real links actually blip.
