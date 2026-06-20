# CLAUDE.md

Working notes for Claude Code in this repo — **only** the conventions, invariants, and
gotchas that aren't obvious from the code. For everything else, don't duplicate it here,
read it: `README.md` (what it is, how to run it, the directory layout, the module/layer
map, config, behaviors) and `plan.md` (full design rationale).

## Status

Wireframe/dev build: Phases 1–6 done (incl. the operator dashboard); Phase 7 (real
hardware adapters) pending. NOT deployed yet — it ships to the operator's site later, so
keep everything runnable on a bare laptop: **pure stdlib, no hardware/credentials/root**.
`requirements.txt` (`icmplib`/`httpx`/`APScheduler`) is Phase-7 only — install into a
`.venv`, **never globally** (system Python is PEP 668-locked).

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
- Prober/Notifier are swapped by env (`WISP_PROBER`, `WISP_NOTIFIER`) behind interfaces, with
  mock impls as the default. Keep new providers behind those interfaces.

## Conventions & gotchas

- **States:** `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`.
  Constants live in `core/state_machine.py` — import them, don't hardcode strings.
- **Flap suppression / hysteresis:** DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2,
  recovery = 2 healthy. The FSM never emits `UNREACHABLE` — that's a topology override applied
  in `MonitorEngine.process_cycle` after `feed()`. Don't regress these counts.
- **Topology order:** devices are processed parent-before-child (`_topological_order`) so a
  parent's new state is known when evaluating its children.
- **Power-vs-link:** the co-location heuristic needs **2+ devices** sharing a `power_ref_ip`
  (a lone device down = link fault, not power). A test enforces this — don't regress it.
- **Escalations are DB-derived** (`escalations.due_at` + sweeper), not in-memory timers, so
  restarts don't drop them. `UNIQUE(outage_id, kind)` keeps them idempotent.
- **Restart safety:** `build_engine` rehydrates each FSM from the last `poll_results` row;
  breaking that re-pages everyone on restart.
- **Timestamps:** poll/outage stamps are ISO8601 `+00:00`; SQLite `datetime('now')` (acks) is
  space-separated naive. `core/analytics._parse` normalises both to naive UTC — reuse it.
- **Schema changes:** add `migrations/000N_*.sql` (idempotent, `IF NOT EXISTS`); the runner
  tracks applied versions in `schema_migrations`. Never edit `0001_init.sql` in place. If you
  add a `devices` column, update both `DeviceMeta` and the SELECT in `load_device_meta`.
- **Demo IPs** are `192.0.2.0/24` (TEST-NET-1, never routed) so a real ICMP prober can't hit
  anything live.
- **Dashboard layering:** `server/services.py` mirrors `core/analytics.py` but returns
  dicts/lists; `server/routes.py` is HTTP-only (runnable entry is `apps/dashboard/main.py`).
  Triage buckets: open+unacked = `unassigned`, open+acked = `in_progress`,
  recovered+undocumented = `pending_postmortem`; UNREACHABLE is excluded (never paged). Active
  cards deliberately **don't** show the inferred power/link cause (it's a guess) — the confirmed
  cause is captured by the post-mortem dropdown at resolution.
- **Device CRUD** (`services.create/update/delete_device`, validated via `DeviceError`→422):
  PUT is a full replace (the form submits every field); DELETE hard-deletes the node + its
  poll/outage/alert history in one txn but is **blocked (409) if it still has child nodes**.
  Added/removed nodes are only *monitored* after a daemon restart (`build_engine` snapshots the
  device set at start).
- **Web assets are vendored** under `apps/dashboard/static/` (no CDN, no build step); `routes.py`
  serves `index.html` from `templates/` and everything else from `static/`. The app is plain
  vanilla JS — Tailwind's Play-CDN runtime JITs classes off the live DOM, so dynamically-built
  class strings work. Phase-7: swap the Play-CDN runtime for a precompiled stylesheet.

## Tests

Run `python -m unittest discover -s tests` after any logic change (30 tests). They mirror the
layers: `unit/test_state_machine` (FSM + overrides), `integration/test_notifiers`
(dispatch/escalation/ack, temp DB + controlled time), `integration/test_analytics` (digest
math), `integration/test_api` (services + device CRUD). Add cases there rather than relying on
the live demo — time-based paths (escalation, dedupe) won't surface in a sub-second run.
