# New Architecture — Design Brief

## What we're building

A multi-tenant ISP monitoring platform. Each ISP installs a lightweight edge probe on
their always-on box; all intelligence lives on a central server we operate. ISPs log
into the central server with their own credentials and get the full monitoring experience
— outage panels, device management, SNMP ports, team, alerts — without needing to manage
any software beyond the probe.

The central server is on GCP and treated as always-available. Edge probes send raw ping
and SNMP results to central; central runs the brain.

---

## The three phases

### Phase A — Central gets the full dashboard UI

ISPs should be able to log in, add their network topology, and manage their team entirely
from the central server. No live monitoring data yet — just the management plane.

The rich dashboard already exists in `apps/dashboard/`. The job is making it work on
central (`apps/central/`) as a multi-tenant app where each ISP sees only their own data.
The per-org login system is already built (`central/auth.py`).

What needs to work at the end of Phase A:
- ISP owner logs in, creates their devices with topology (parent/child tree)
- Nodes page with tree view (map view optional for now)
- Team page — add workers, set roles, toggle attendance
- Settings — set ntfy topics per role, send test alert
- Outage panels exist but show no live data yet (Phase B fills them)
- Superadmin sees all orgs; org user sees only their own

How to get there is your call. The edge services (`server/services.py`) and the SPA
(`apps/dashboard/static/app.js`) are the main references. Multi-tenant scoping already
has a clear pattern in `central/server.py` — follow it.

---

### Phase B — Central runs the brain

The FSM, outage detection, and ntfy alerting move from the edge to central.

Each poll cycle, the edge sends raw results:
- Per-device ICMP: loss %, avg latency, jitter
- Per-switch SNMP: port oper/admin status, byte counters

Central receives this, runs `MonitorEngine.process_cycle()` per tenant, fires
`AlertDispatcher` for new events, and persists states and outages into the central store.

The FSM and alerting code already exist in `core/state_machine.py` and
`egress/notifiers.py`. The job is running them on central against incoming data instead
of locally on the edge. The per-link fast-confirm loop (re-probe a suspect device every
2s until confirmed or cleared) goes round-trip: central tells the edge to recheck a
device in the POST reply; edge re-probes and ships the result back.

Alert routing: each org stores its three ntfy topics (owner/operator/tech). Extend the
`orgs` table for these; org owner sets them from the central dashboard.

Design the new wire format for raw results however makes sense given the central store
schema. The existing outbox/shipper format (finished events) becomes obsolete — the edge
will ship raw data instead.

---

### Phase C — Edge becomes a thin probe

Once Phase B is solid, strip the edge down to its job: probe and ship.

Keep:
- ICMP prober (`ingress/probers.py`)
- SNMP poller (`ingress/snmp.py`)
- Async probe loop with semaphore (the fan-out discipline matters at scale)
- HTTP POST to central
- Heartbeat

Remove:
- FSM, alerting, local dashboard, local DB for outages/alerts
- Everything under `server/` and `apps/dashboard/` from the edge runtime

The edge needs a local device list (pulled from central on start, cached so a brief
central hiccup doesn't stop probing) and a small SNMP counter cache (so bandwidth rates
survive a restart without a phantom spike).

How slim is slim enough is your call — don't over-engineer, but don't leave dead weight.

---

## Locked decisions

**Central is the only place ISPs interact with the system.** No local dashboard on the
edge. The edge box is a headless probe.

**The flap suppression counts don't change.** DOWN = 3 consecutive 100%-loss samples,
recovery = 2 healthy. These are the "never cry wolf" contract. Fast-confirm keeps this
count while collapsing the wall time to ~4 seconds.

**Topology suppression stays.** A child whose every parent is DOWN becomes UNREACHABLE
and is never paged separately. If any backup parent is alive, the child stays genuinely
DOWN. This logic moves to central unchanged.

**ntfy is the only alert channel.** Three fixed topics per org (owner/operator/tech).
Fresh DOWN pages owner + operator immediately; hourly escalation broadcasts to all three
until recovery.

**Multi-tenancy is non-negotiable.** An org user must never see another org's data. The
scoping pattern is already in `central/server.py` — honour it throughout.

**Tests stay green.** Run `python -m unittest discover -s tests` after every meaningful
change. Add new tests for new paths; don't remove existing ones until the code they test
is genuinely gone.

---

## What's already built — don't rebuild it

- FSM: `core/state_machine.py` — pure, no I/O, just move where it's called from
- Alerting: `egress/notifiers.py` — same, relocate the call site
- SNMP ingress + port monitoring: `ingress/snmp.py` + `egress/ports.py`
- Analytics: `core/analytics.py`, `core/rollup.py`
- Rich dashboard UI: `apps/dashboard/templates/` + `static/`
- Central auth + accounts: `central/auth.py`, `central/admin.py`
- Central multi-tenant store: `central/store.py`
- Central HTTP server: `central/server.py`
- Fleet watchdog: `central/watchdog.py`

Read before writing. Most of Phase A and B is moving and adapting existing code,
not writing new logic.

---

## Open questions (answer them as you build, don't wait)

- Where does in-flight fast-confirm state live on central between two successive POSTs
  from the same edge? (Per-tenant in-memory dict is probably fine.)

- SNMP counter cache on the edge: in-memory (lost on restart, one phantom spike) or a
  small local file? Pick what's simpler.

- Does Phase A need the topology map view, or is the tree list enough to unblock ISPs?
  Your call — don't block Phase A on it.

- How does central store and serve per-device poll state for the dashboard (the UP/DOWN
  badges, latency numbers)? The central store currently has events and rollups but not
  live per-device state. You'll need a `device_states` table or similar — design it to
  fit the FSM output naturally.
