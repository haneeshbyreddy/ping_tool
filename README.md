# Village WISP Monitor

Multi-org network monitoring + alerting for rural WiFi ISPs. **Central runs the
brain** — FSM, topology-aware suppression, fast-confirm detection, the ntfy alerting
ladder, multi-org dashboard — for every org it serves. Each ISP's **edge box is a
thin probe**: real ICMP, reports raw results to central, no local DB/dashboard/FSM of
its own. See `CLAUDE.md` for design rationale, status, and invariants.

## Quick start (local dev)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"   # unprivileged ICMP

./run.sh   # starts central (127.0.0.1:8080) + one edge probe pointed at it
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you --password ...
# log in, add your first device from the dashboard's Nodes page

python -m unittest discover -s tests   # 335 tests, pure stdlib
```

`src/wisp` is a *src layout*, not installed — `apps/daemon` and `apps/central` add
`src/` to `sys.path` themselves; the admin CLI needs `PYTHONPATH=src`.

The dashboard itself (`central/static/`) is a built React SPA — `./run.sh` serves the
already-committed build, so the quick start above needs no Node. Only touch the frontend
source if you're changing the dashboard: `cd web && npm install && npm run build`
regenerates `central/static/` (Node 20+; build-time only, never a runtime dependency).

## Central dashboard

React + Tailwind + shadcn/ui (source in `web/`, see `CLAUDE.md`'s "Central dashboard"
section for the architecture). Pages:

- **Home** — fleet-wide stat tiles, uplink status, low-bandwidth alert, recent events.
- **Triage** — the outage queue: acknowledge an open outage, log a post-mortem once
  it's auto-resolved. Recovery itself is never operator-driven.
- **Topology** — device tree (name, IP, type, region, parent/backup, live state),
  SNMP port watch/bandwidth. Changes apply within one poll cycle, no edge restart.
- **Probes** — physical probe enrollment (distinct from **Topology**). Owner/operator
  self-registers a node, gets a one-time token; rotate/revoke/delete from the same page.
- **Team** — workers as identity + role (owner/operator/tech); alerts route to one ntfy
  topic per role, set in **Settings**. Also carries daily operator attendance.
- **Settings** — org name, the three role ntfy topics + a test-alert button, and login
  account provisioning (owner/superadmin only).
- **Logs** — the full org event history, cursor-paginated.
- **Accounts** — central-provisioned only: a superadmin onboards each ISP
  (`central.admin create-superadmin`/`create-user`); org users see only their org.

## Layout

```
src/wisp/
├── config.py, version.py
├── core/        # state_machine.py (FSM, shared), analytics.py, baseline.py
├── ingress/     # probers.py (ICMP), snmp.py (IF-MIB walk)
├── egress/      # notifiers.py (ntfy channel)
├── central/     # the brain: engine, dispatch, ports, redundancy, perf, analytics,
│                #   rollup, pki, store, server, watchdog, auth, admin CLI, rollout,
│                #   inventory, static/ (built dashboard SPA — generated, see web/)
└── runtime/     # central_client.py, single_instance.py, supervisor.py
apps/
├── daemon/main.py      # edge: probe loop only
├── central/main.py     # central: ingest + dashboard + watchdog
└── supervisor/main.py  # edge supervisor (self-update)
web/         # dashboard SPA source (React/TS/Tailwind/shadcn) — `npm run build`
             #   compiles into src/wisp/central/static/; Node is build-time only
data/        # central.db + session_secret + pki/ — git-ignored
deploy/      # wisp-edge.service, install-edge.sh/.ps1, PyInstaller spec
tests/{unit,integration}/
assets/      # original design mockup
run.sh
```

## Key behaviors (central)

- **Flap suppression** — DOWN after 3 consecutive 100%-loss samples, DEGRADED after 2,
  recovery after 2 healthy. A single blip never pages.
- **Fast-confirm** — central names a suspect IP in its `/report` reply; the edge
  re-probes just that IP every `WISP_RETRY_INTERVAL_S` until confirmed/cleared —
  detection in seconds, not `poll_interval × down_consecutive`.
- **Uplink canary** — edge's own internet down → central freezes that org's cycle,
  sends one `UPLINK_DOWN` instead of a storm.
- **Topology suppression** — a child is `UNREACHABLE` (silent) only when every parent
  (primary + backup) is down; any live parent means a real fault, still pages.
- **On-backup redundancy** — a device reachable only via its backup parent gets one
  operator heads-up, never an outage; clears silently if it goes hard DOWN.
- **Per-link performance baseline** — pages once on sustained deviation from a rolling
  median+MAD latency/jitter baseline, even within FSM thresholds.
- **SNMP port + bandwidth** — edge walks snmp-enabled switches on its own slow cadence;
  central folds a monitored port-down into the open outage (or a heads-up if none), and
  alarms sustained low throughput against a per-port threshold. Admin-down/unmonitored
  stays silent. Full rules: `CLAUDE.md`.
- **Outage-derived SLA + trend** — `GET /api/analytics?days=` (per-device uptime/outage
  count, UNREACHABLE excluded) and `GET /api/analytics/trend?device_id=&days=` (hourly
  latency/loss buckets, 30-day retention) — both computed off data central already keeps.
- **Escalation is restart-safe** — DB-backed timers. Fresh DOWN pages owner+operator;
  all-hands (owner+operator+tech) re-page every `WISP_ESCALATE_EVERY_MIN` while open.
  Ack doesn't stop it, only recovery does.
- **Scales without lying** — bounded probe fan-out (`WISP_MAX_INFLIGHT`) and gentle
  infra probing (`WISP_PINGS_PER_POLL_INFRA`) keep a large fleet from faking a mass
  outage or reading rate-limiting as loss.
- **Multi-org, always** — every read/write is org-scoped; a superadmin can narrow
  with `?org=`.

## Central + edge setup

```bash
WISP_CENTRAL_TOKEN=s3cret python apps/central/main.py --host 0.0.0.0 --port 8443
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you
PYTHONPATH=src python -m wisp.central.admin create-user --org ispA --username asha --role owner

WISP_CENTRAL_BRAIN=1 WISP_CENTRAL_URL=https://central.example.net WISP_CENTRAL_TOKEN=s3cret \
WISP_ORG_ID=ispA WISP_NODE_ID=edge-a1 python apps/daemon/main.py
```

Put a TLS terminator (nginx/Caddy) in front of central, or let it terminate TLS itself
via `WISP_CENTRAL_TLS_CERT`/`_KEY` (+ `WISP_CENTRAL_CLIENT_CA` for mTLS). Plain HTTP by
default to stay dependency-free.

**mTLS enrollment** (alternative to the bearer token; either or both can be active):
```bash
PYTHONPATH=src python -m wisp.central.admin init-ca --host central.example.net
PYTHONPATH=src python -m wisp.central.admin enroll-edge --org ispA --node edge-a1
```
Each command prints the env vars to set on its respective side.

**Fleet deploy + self-update.** Every edge ships as a frozen binary (PyInstaller); a
small supervisor owns `download → verify(sha256[+minisign]) → atomic-swap → restart →
health-gate → rollback`. Central is the version authority with a staged, canary-first,
health-gated rollout:
```bash
PYTHONPATH=src python -m wisp.central.admin publish-release --version 0.11.0 \
    --artifact linux-amd64 https://.../wisp-edge-linux-amd64 <sha256>
PYTHONPATH=src python -m wisp.central.admin start-rollout --org ispA --version 0.11.0 --canary edge-a1
PYTHONPATH=src python -m wisp.central.admin rollout-status --org ispA
```
Install: `curl … | sudo sh -s -- --central … --token … --org … --node …`
(`deploy/install-edge.sh`) or `deploy/install-edge.ps1` on Windows — see "Going live"
below. Both are the only supported install path (no separate single-box mode). CI
(`.github/workflows/release.yml`) builds+tests every push/PR; on a `v*` tag it also
signs (Authenticode on Windows binaries, minisign over `SHA256SUMS`) — no-ops until
`WINDOWS_CODESIGN_PFX`/`MINISIGN_KEY` secrets are set, so unsigned forks/PRs still build.

## Configuration (env vars, all optional)

Full list + defaults: `src/wisp/config.py`. The ones worth knowing up front:

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | steady-state seconds between polls |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm re-probe interval (`0` = off) |
| `WISP_PINGS_PER_POLL` / `_INFRA` | `5` / `2` | echoes per poll: leaf CPE / aggregation gear |
| `WISP_MAX_INFLIGHT` | `256` | concurrent probe cap (`0` = unbounded) |
| `WISP_SNMP_INTERVAL_S` | `90` | seconds between SNMP port walks (`0` = off) |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages |
| `WISP_CENTRAL_BRAIN` / `_URL` / `_TOKEN` | `0` / — / — | edge mode switch + central address + ingest auth |
| `WISP_CENTRAL_CLIENT_CERT`/`_KEY`/`_CA_CERT` | — | edge's mTLS identity (from `enroll-edge`) |
| `WISP_ORG_ID` / `WISP_NODE_ID` | `default` / hostname | edge identity |
| `WISP_CENTRAL_DB` / `_BIND` / `_PORT` | `data/central.db` / `0.0.0.0` / `8443` | central store + listen address |
| `WISP_CENTRAL_TLS_CERT`/`_KEY`/`_CLIENT_CA` | — | central-terminated TLS + mTLS verification |
| `WISP_CENTRAL_NODE_STALE_S` | `180` | fleet-watchdog staleness threshold |

Config is env-var only, read once at startup into the frozen `Config` — no in-UI
settings page. Device topology, team, and per-org alert routing *are* live in the
central dashboard.

## Going live (deploying an edge node)

A **native install, not Docker** — this is a network monitor that wants the host's real
network stack; a container would need `network_mode: host` plus the ping-group sysctl
just to ping correctly, buying no real isolation for an extra moving part.

1. On the edge box, run the fleet installer with your enrollment token:
   ```bash
   curl -fsSL https://YOUR-CENTRAL/install-edge.sh | sudo sh -s -- \
       --central https://central.example.net --token <ENROLL> --org ispA --node edge-a1
   ```
   (Windows: `deploy/install-edge.ps1 -Central … -Token … -Org … -Node …`, run
   elevated.) This detects arch, verifies sha256 (+ minisign/Authenticode if published),
   installs the agent + supervisor under `/opt/wisp` (`Program Files` on Windows),
   writes identity/config to `/etc/wisp` (untouched by future updates), enables
   unprivileged ICMP, and starts the service — `wisp-edge.service` on Linux runs the
   **supervisor**, not the agent directly, so it can self-update. On Windows there's no
   unprivileged-ICMP path (no ping-group, no datagram sockets), so icmplib needs raw
   sockets — that's why the Scheduled Task runs as SYSTEM. Not verified on real Windows
   hardware from this repo; smoke-test before trusting it.
2. In central's dashboard: enter real topology (Nodes), team (Team), role ntfy topics
   (Settings), confirm with **Send test alert**.
3. Tune cadence/thresholds in `/etc/wisp/edge.env` against real link behavior, then
   restart the service (`systemctl restart wisp-edge`). Agent version bumps instead flow
   through the supervisor's self-update — no manual restart needed for those. Device
   topology changes apply within one poll cycle with no edge restart at all.
4. Additional nodes for the same ISP repeat step 1 with a different `--node` — central
   tells each one what to probe.

## Incident post-mortem (template)

Use one write-up per significant outage — the dashboard already captures root
cause/resolution notes on its *Pending post-mortem* card; this is for the longer human
narrative on repeat offenders or multi-site events. Copy this list per incident:

- **Summary** — incident ID, date/time (UTC), duration (down → restored), site(s),
  severity, who acknowledged.
- **What happened** — short narrative: what was seen, when the alert fired, who
  responded.
- **Detection** — how it was found (dashboard/ntfy); did the FSM classify it correctly
  (the engine never guesses cause — that's operator-entered at resolution only).
- **Timeline** — first 100%-loss poll → DOWN confirmed/alert sent → acknowledged →
  root cause found → restored.
- **Root cause** — what actually broke (power / fiber-backhaul / hardware /
  weather-RF / other) and why; note if topology suppression helped or misled.
- **Resolution** — what the technician did to restore service.
- **Follow-ups** — preventive action; threshold/topology tuning if the FSM
  mis-classified; inventory fix in the dashboard if metadata was wrong.
