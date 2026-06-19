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

```bash
python db.py                 # create the database (WAL) + run migrations
python seed.py --reset       # load a demo network (8 devices, real topology)

# watch it run — simulated outages, flap suppression, alerts, recovery:
python polling_daemon.py --interval 1 --cycles 13

# operator views:
python analytics.py status            # live board: who's up/down right now
python analytics.py digest            # daily summary (uptime, power-vs-equipment, ₹ lost)
python analytics.py devices           # per-device uptime
python analytics.py offenders         # repeat-offender ranking

python ack.py                         # list open outages
python ack.py <id> "Your Name"        # acknowledge one (stops escalation)

python -m unittest discover -s tests  # 20 tests
```

## How it works (the 5 layers)

| File | Layer | Does |
|---|---|---|
| `probers.py` | 1 Monitoring | pings devices (`SimulatedProber` now / `IcmpProber` later) |
| `polling_daemon.py` | 1 | 60s async poll loop; orchestrates everything |
| `state_machine.py` | 2 Pattern | FSM + flap suppression, canary freeze, topology suppression, power-vs-link |
| `notifiers.py` | 4/5 Alerting | routing, anti-spam, T+10/T+20 escalation ladder, ack |
| `analytics.py` | 3 BI | status board, daily digest, uptime, offenders |
| `db.py` / `migrations/` | 5 Memory | WAL SQLite, durable outages/alerts/escalations |

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
2. Replace the demo rows in `seed.py` with the real device inventory + topology.
3. `WISP_PROBER=icmp` (run with `sudo`/`cap_net_raw`), `WISP_NOTIFIER=ntfy` (or `telegram`).
4. Tune thresholds against how the real links actually blip.
