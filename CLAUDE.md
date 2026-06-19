# CLAUDE.md

Guidance for Claude Code working in this repo. Read `plan.md` for the full design
rationale and `README.md` for the user-facing quickstart.

## What this is

A network monitoring + alerting tool for a rural WiFi broadband operator (~1k customers
across several villages). It pings shared infrastructure (towers/relays/backhaul/core),
runs a state machine to detect outages with low false-alarm noise, infers **power vs
link/equipment** cause, and pushes alerts to the operator + field technician with an
escalation ladder.

**Status:** wireframe/dev build. Phases 1–6 done; Phase 7 (real hardware adapters) pending.
This is NOT yet deployed — it ships to the operator's site later. Keep it runnable on a
laptop with no hardware/credentials/root.

## Commands

```bash
python db.py                          # migrate (idempotent); prints WAL + indexes
python seed.py --reset                # load demo network (8 devices)
python polling_daemon.py --interval 1 --cycles 13   # demo run (fast)
python polling_daemon.py              # real 60s cadence, runs forever
python analytics.py status|digest|devices|offenders
python ack.py [<outage_id> "Name"]
python -m unittest discover -s tests  # 20 tests — run after any logic change
```

No third-party deps are installed (system Python is PEP 668-locked). Everything above
runs on **pure stdlib**. `requirements.txt` deps (`icmplib`/`httpx`/`APScheduler`) are
Phase 7 only — install into a `.venv`, never globally.

## Architecture (data flow)

```
probers.py ──► polling_daemon.py ──► state_machine.py ──► notifiers.py
 (ping)         (60s asyncio loop)     (MonitorEngine)      (AlertDispatcher)
                       │                      │                   │
                       └──────────► db.py (WAL SQLite) ◄──────────┘
                                          ▲
                                  analytics.py (read-only)
```

- `config.py` — single frozen `Config` dataclass, all tunables from env. `CONFIG` singleton.
- `state_machine.py` — `MonitorEngine` is **pure** (no I/O): takes `{ip: PingResult}` + ts,
  returns committed states + `Event`s. DB glue (`build_engine`, `apply_events`) is separate.
  This is why it's unit-testable. Keep it that way — don't put DB/network calls in the engine.
- `notifiers.py` — `AlertDispatcher` does network sends OUTSIDE any DB transaction, then logs.
- Provider selection via env: `WISP_PROBER=simulated|icmp`, `WISP_NOTIFIER=mock|ntfy|telegram`.
  Both sides are behind interfaces (`Prober`, `Notifier`) with mock impls as default.

## Conventions & gotchas

- **States:** `UP` / `DEGRADED` / `DOWN` / `UNREACHABLE`. `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `state_machine.py`; import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN needs 3 consecutive 100%-loss polls, DEGRADED needs
  2, recovery needs 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override
  applied in `MonitorEngine.process_cycle` after `feed()`.
- **Topology:** devices are processed parent-before-child (`_topological_order`) so a parent's
  new state is known when evaluating children.
- **Power-vs-link:** co-location heuristic requires **2+ devices** sharing a `power_ref_ip`
  (a lone device down = link fault, not power). A test enforces this — don't regress it.
- **Escalations are DB-derived** (`escalations.due_at` + sweeper), NOT in-memory timers — so
  restarts don't drop them. `UNIQUE(outage_id, kind)` keeps them idempotent.
- **Restart safety:** `build_engine` rehydrates each FSM from the last `poll_results` row;
  don't break that or restarts will re-page everyone.
- **Timestamps:** poll/outage stamps are ISO8601 with `+00:00`; SQLite `datetime('now')`
  (used by acks) is space-separated naive. `analytics._parse` normalises both to naive UTC.
- **Schema changes:** add a new `migrations/000N_*.sql` (idempotent, `IF NOT EXISTS`); the
  runner tracks applied versions in `schema_migrations`. Don't edit `0001_init.sql` in place.
  If you add a `devices` column, update both `DeviceMeta` and the SELECT in `load_device_meta`.
- **Demo IPs** are `192.0.2.0/24` (TEST-NET-1, never routed) so a real ICMP prober can't hit
  anything live.

## When changing logic

Run the test suite. The three test files mirror the layers: `test_state_machine.py` (FSM +
overrides), `test_notifiers.py` (dispatch/escalation/ack, temp DB + controlled time),
`test_analytics.py` (digest math). Add cases there rather than relying on the live demo —
time-based paths (escalation, dedupe) won't surface in a sub-second demo run.
