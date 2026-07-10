# Village WISP Monitor

Multi-org network monitoring + alerting for rural WiFi ISPs. **Central runs the
brain** — FSM, topology-aware suppression, fast-confirm detection, the ntfy alerting
ladder, multi-org dashboard. Each ISP's **edge box is a thin probe**: real ICMP +
SNMP, reports raw samples to central, no local DB/dashboard/FSM. Invariants, gotchas,
and design decisions: `CLAUDE.md`.

## Quick start (local dev)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"   # unprivileged ICMP

./run.sh   # central (127.0.0.1:8080) + one edge probe pointed at it
PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you --password ...
# `/` is the marketing landing page; the dashboard is at /app

python -m unittest discover -s tests
```

`src/wisp` is a src layout, not installed — `apps/*` add `src/` to `sys.path`; the
admin CLI needs `PYTHONPATH=src`. The dashboard is a pre-built React SPA committed in
`central/static/` — no Node needed to run. To change it: `cd web && npm install &&
npm run build` (Node 20+, build-time only).

## Layout

```
src/wisp/
├── config.py, version.py
├── core/        # state_machine.py (pure FSM), analytics.py, baseline.py
├── ingress/     # probers.py (ICMP), snmp.py, health.py, gpon.py, walker.py
├── egress/      # notifiers.py (ntfy)
├── central/     # the brain: engine, dispatch, store, server, auth, inventory,
│                #   ports, redundancy, perf, analytics, rollup, rollout, pki,
│                #   watchdog, releasesync, admin CLI, static/ (built SPA)
└── runtime/     # central_client, single_instance, supervisor, edge_status
apps/            # daemon (edge probe), central, supervisor, tray (Windows)
web/             # dashboard SPA source → builds into central/static/
deploy/          # systemd units, build-deb.sh, wisp-edge-setup.iss, PyInstaller spec
data/            # central.db + session_secret + pki/ — git-ignored
tests/{unit,integration}/
```

## Key behaviors (central)

- **Flap suppression** — DOWN after 3 consecutive 100%-loss polls, DEGRADED after 2,
  recovery after 2 healthy. A single blip never pages.
- **Fast-confirm** — central names suspect IPs in its `/report` reply; the edge
  re-probes just those every `WISP_RETRY_INTERVAL_S` — detection in seconds.
- **Uplink canary** — edge's own internet down → org cycle freezes, one
  `UPLINK_DOWN` instead of a storm.
- **Topology suppression** — a child is silent (`UNREACHABLE`) only when every
  parent incl. backup is down; any live parent means a real fault, still pages.
- **On-backup redundancy** — reachable only via backup parent = one operator
  heads-up, never an outage.
- **Per-link perf baseline** — pages on sustained latency/jitter deviation from the
  link's own median+MAD baseline, even within FSM thresholds.
- **SNMP** — port up/down + bandwidth floor/ceiling per port, device health
  (CPU/mem/temp) via declarative vendor profiles, per-ONU GPON optics with vendor
  auto-detect, dashboard-queued remote diagnostic walks. All on a slow background
  cadence, never blocking the ICMP path.
- **Analytics** — `GET /api/analytics?days=` (per-device uptime/outages) and
  `/api/analytics/trend` (hourly latency/loss, 30-day retention).
- **Escalation is restart-safe** — DB-backed: fresh DOWN pages owner+operator;
  all-hands re-page every `WISP_ESCALATE_EVERY_MIN` until recovery. Ack doesn't
  stop it.
- **Multi-org, always** — every read/write is org-scoped.

## Running central + an edge

```bash
WISP_CENTRAL_TOKEN=s3cret python apps/central/main.py --host 0.0.0.0 --port 8443
PYTHONPATH=src python -m wisp.central.admin create-user --org ispA --username asha --role owner

WISP_CENTRAL_URL=https://central.example.net WISP_CENTRAL_TOKEN=s3cret \
WISP_ORG_ID=ispA WISP_NODE_ID=edge-a1 python apps/daemon/main.py
```

Put a TLS terminator (nginx/Caddy) in front of central, or set
`WISP_CENTRAL_TLS_CERT`/`_KEY` (+ `WISP_CENTRAL_CLIENT_CA` for mTLS). Ingest auth is
any one of: global token, self-service per-node token (dashboard: Network → Probes),
or mTLS (`admin init-ca` / `enroll-edge`).

## Deploying an edge node

Native install, not Docker (a network monitor wants the host's real stack). Register
the probe in the dashboard (Network → Probes → Register) and follow the per-OS
instructions it shows, or by hand:

```bash
sudo dpkg -i wisp-edge-linux-amd64.deb        # Linux (amd64/arm64)
sudo vi /etc/wisp/edge.env                    # central URL + token + org
sudo systemctl enable --now wisp-edge
```

Windows: `wisp-edge-setup-win-amd64.exe` (wizard, or `/VERYSILENT /Central=…
/Token=… /Org=… /Node=…`). Both packages install agent + supervisor; the supervisor
owns all subsequent agent self-updates (staged, canary-first, health-gated,
auto-rollback), driven from central:

```bash
PYTHONPATH=src python -m wisp.central.admin sync-releases
PYTHONPATH=src python -m wisp.central.admin start-rollout --org ispA --version X.Y.Z --canary edge-a1
PYTHONPATH=src python -m wisp.central.admin rollout-status --org ispA
```

Edge health lives on disk: `status.json` + `logs/edge.log` next to the install
(`/var/lib/wisp` / `C:\ProgramData\WISP`). Windows also gets `wisp-tray.exe` in the
notification area (status, logs, restart, uninstall).

## Configuration

Env-var only, read once at startup into the frozen `Config` — full list + defaults
in `src/wisp/config.py`. The ones worth knowing:

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | seconds between polls |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm re-probe interval (`0` = off) |
| `WISP_PINGS_PER_POLL` / `_INFRA` | `5` / `2` | echoes per poll: leaf / aggregation gear |
| `WISP_MAX_INFLIGHT` | `256` | concurrent probe cap |
| `WISP_SNMP_INTERVAL_S` | `90` | SNMP sweep cadence (`0` = off) |
| `WISP_SNMP_WALK_TIMEOUT_S` | `20` | per-walk cap (health) |
| `WISP_PORT_WALK_TIMEOUT_S` | `60` | per-walk cap for the ifTable port walk (big OLTs) |
| `WISP_GPON_WALK_TIMEOUT_S` | `75` | per-walk cap for the ONU optics roster walk |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages |
| `WISP_CENTRAL_URL` / `_TOKEN` | — | central address + ingest auth |
| `WISP_ORG_ID` / `WISP_NODE_ID` | `default` / hostname | edge identity |
| `WISP_CENTRAL_DB` / `_BIND` / `_PORT` | `data/central.db` / `0.0.0.0` / `8443` | central store + listen |

Device topology, team, and alert routing are live in the dashboard, not env vars.
