# Architecture — design rationale, roadmap, and removed code

This doc holds the *why* behind the invariants in `CLAUDE.md` and the history of what
was deliberately deleted. Split out of `CLAUDE.md` to keep the per-session working
notes lean — read this when you need the backstory, not on every task.

## Design rationale & roadmap

The platform grew out of a single-box appliance (one daemon + one local dashboard, one
ISP, one SQLite file) — visible in git history, not how the system runs today. The edge
kept its detection-speed characteristics (bounded fan-out, gentle infra probing,
fast-confirm) but lost everything that made it a standalone product; that now all lives
on central, multiplied across orgs.

```
  ISP "A"                                        Central (multi-org)
  ┌──────────────────────────┐                  ┌───────────────────────────┐
  │ edge-a1 (thin probe)      │  POST /report    │ POST /report, GET         │
  │  ICMP + SNMP only ────────┼─── raw samples ─►│  /edge/devices (topology) │
  │  no local FSM/DB/alerting │◄── recheck hint ─┤      │                    │
  │  + supervisor (updates) ◄─┼── version/url ───┤      ▼                    │
  └──────────────────────────┘   in heartbeat    │ MonitorEngine (per org,│
  ISP "B": edge-b1, edge-b2 ────────────────────►│  in-memory, restart-safe) │
                                                  │      │                    │
                                                  │      ▼                    │
                                                  │ CentralAlertDispatcher ──┼──► ntfy
                                                  │ + dashboard + watchdog   │
                                                  │ + version authority      │
                                                  └───────────────────────────┘
```

**Why the invariants above are shaped this way:**
- Flap suppression exists because a wireless link blips constantly; paging on one bad
  poll trains people to ignore alerts. Fast-confirm collapses the *wall clock* for those
  samples to seconds without touching the count.
- The uplink canary exists because if an ISP's own internet is down, every device
  behind it looks down — one `UPLINK_DOWN` beats a storm.
- Topology suppression exists because one "Tower A down" is actionable, forty child
  alerts are noise — a child stays silent unless it has a live backup path too.
- Cause is operator-confirmed, never inferred, because an earlier auto-inference
  attempt (power-vs-link heuristics from device co-location) was never wired to any UI
  and was removed rather than finished.
- Durable DB-backed memory (not in-memory timers) plus `EngineRegistry`'s
  one-engine-per-org exists because a central restart must never drop an escalation
  or re-page everyone, and flap-suppression streaks must survive across an edge's
  stateless HTTP reports.

**Locked decisions (don't relitigate without a real reason):**

| Topic | Decision |
|---|---|
| Where the brain runs | Central, for every org. The edge never runs an FSM or alerts on its own. |
| What we monitor | Shared infrastructure (towers, backhaul, switches), not end-user routers (yet). |
| Alert channels | ntfy only; 3 topics per org (owner/operator/tech); fresh DOWN pages owner+operator, hourly escalation broadcasts to all three until recovery. |
| Multi-tenancy | Non-negotiable — every central read/write is org-scoped. |
| Where the edge runs | On-prem, one process per node, no local DB/dashboard. Every node deploys the same way (frozen binary + supervisor); no separate "simple" mode. |
| Transport | Edge dials central, never the reverse. Ingest auth: global token, self-service node token, or mTLS — any one satisfies it. |
| Updates | Pull-based over the existing report channel. Central is the version authority; rollouts are staged + health-gated + auto-rollback. |
| Realism | Probers/notifiers sit behind small interfaces; tests inject recording doubles, never hit the real network. |

**Roadmap (item numbers are referenced by comments across the codebase — keep them
stable):**

1. ~~SNMP port monitoring on central~~ — **done**, including bandwidth (item 3).
2. ~~Central-side historical rollups / trend analytics~~ — **done** (outage-derived SLA
   + hourly latency/loss trend).
3. ~~Per-link performance baseline + on-backup redundancy + SNMP bandwidth~~ — **done**.
4. **Deploy central for production** — everything to date is local dev (`run.sh`) or
   test processes; needs an always-on host + TLS terminator, and the operator's actual
   provider/region/domain choice. Not started.
5. **Fleet update signing, last mile** — CI signing logic is real and exercised
   (unsigned) on every push. Still needed: a real minisign keypair +
   `WINDOWS_CODESIGN_PFX` cert held by the platform operator, a genuine signed `v*`
   release, and running the signed installers on real Linux/Windows hardware — not
   fabricable in a coding session.
6. ~~mTLS enrollment~~ — **done**, replacing the bearer-token-only stopgap.
7. ~~Self-service node registration from the dashboard~~ — **done**.

Nothing is deferred at the detection-tier level (every soft-signal tier the old
single-box edge had now exists on central) — items 4–5 are production/ops groundwork,
not missing features.

**Open questions (answer as you build, don't block on them):** central hosting
provider/region and whether one box serves every org or scale eventually demands
sharding; rollout policy (fully automatic per org vs. operator-approved, canary size) —
product calls for the platform operator, not code-shape questions; data residency/
retention policy — one shared policy across orgs, or per-org.

## Removed — don't go looking for these

An earlier single-org version (one daemon + one local dashboard, no central server)
was fully retired; code deleted wholesale, not deprecated. Detail is in git history, not
here. Gone:

- `apps/dashboard/` (edge's local web UI), `src/wisp/server/` (routes/services/auth/
  watchdog — `LoginThrottle` now lives in `central/auth.py`).
- `egress/shipper.py` + `database/outbox.py` (store-and-forward, moot once the edge
  ships raw samples), `egress/ports.py` + `egress/ack.py`, `core/rollup.py`.
- `AlertDispatcher` + `role_topic`/`acknowledge_outage` out of `egress/notifiers.py` —
  that file now holds only the ntfy channel (`NtfyNotifier`, `send_with_retry`,
  `build_notifier`). The DB-coupled alerting policy is `central/dispatch.py`'s
  `CentralAlertDispatcher`.
- `apps/daemon/main.py`'s old local-`MonitorEngine` drivers: `run_forever`, `run_cycle`,
  `_confirm_down`/`_confirm_up`, `_between_cycle_watch`, `_persist`, `prune_old_polls`,
  `snmp_cycle`. What's left is the probe loop: `_gather_pings`, `_gentle_probe_plan`,
  `run_cycle_central_brain`, `run_forever_central_brain`, `_follow_recheck`.
- The legacy per-edge SQLite layer: `src/wisp/database/` and `migrations/*.sql`, plus
  `core/state_machine.py`'s old DB-glue (`load_device_meta`/`build_engine`/
  `apply_events` — distinct from central's own same-named functions in
  `central/engine.py`, which are what's actually called) and `ingress/snmp.py`'s
  `load_snmp_targets` (zero callers). `core/state_machine.py`, `core/analytics.py`
  (now just `_parse`/`_now`), and `core/baseline.py` are still alive — central imports
  them directly. Grep before deleting anything in `core/`.
- The single-box install path: `deploy/install.sh`, `install.ps1`,
  `wisp-monitor.service` — and later (2026-07) the curl-script fleet installers
  `install-edge.sh`/`.ps1` too. Every edge node now installs through the release
  packages (`deploy/build-deb.sh` .deb on Linux, `deploy/wisp-edge-setup.iss` setup
  exe on Windows) + `wisp-edge.service`. No separate "simple" mode. Deleted alongside
  the scripts: the legacy edge-ingest plane (`POST /ingest`, the `devices`/`rollups`
  tables, `GET /api/devices`, `GET /api/fleet`) — the central-brain wire format
  (`/report` + `/heartbeat`) replaced it wholesale.
- The old hand-rolled vanilla-JS dashboard: `central/static/app.js`, `icons.js`,
  `vendor/tailwind.js`, and `index.html`'s gray Material color config — replaced
  wholesale by the React/Tailwind/shadcn SPA in `web/` (see `CLAUDE.md`), not
  deprecated alongside it. Git history has the old one if you need to
  compare behavior; don't resurrect its files.

`runtime/supervisor.py`, `apps/supervisor/main.py`, and staged-rollout/self-update are
untouched by any of the above.
