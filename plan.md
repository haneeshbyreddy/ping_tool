# Village WISP Monitor — Plan

A small, reliable tool to watch the **shared network infrastructure** of a rural WiFi
broadband operator (several villages) and instantly tell the operator
and field technicians **what is down and where** — over free push
channels (ntfy). Runs on one always-on box, no cloud, no per-message costs.

> **What's here:** the design *rationale* (the why), the remaining **Phase 7** go-live
> work, and the open questions. The per-feature build detail now lives in the code,
> `README.md` (what/how/layout), and `CLAUDE.md` (invariants & gotchas) — this file is
> deliberately not a duplicate of those.

## Status

Phases 1–6 (engine, FSM, alerting, BI, dashboard) **and Phase 8** (team directory, PIN
gate, monitor lifecycle) are **done** — 80 tests. The build now targets a **real
environment**: the mock notifier and simulated prober (and the demo seeder) have been
removed, so the daemon polls with `IcmpProber` and alerts via `NtfyNotifier`. Config is
**env-var only** (a frozen `Config` read at startup — no DB settings layer). The build also
carries the **fleet-scale** work: bounded probe fan-out, gentle probing of aggregation gear,
optional adaptive cadence, and an hourly rollup tier (see "Scaling" below). The dashboard +
tests remain pure stdlib; the daemon needs the venv (`icmplib`/`httpx`) and the kernel ping
group enabled. See "Going live" below.

---

## Decisions locked for v1

| Topic | Decision |
|---|---|
| **What we monitor** | Shared infrastructure only — towers, relays, backhaul, core. Not the end-user routers (yet). |
| **Primary goal** | Operator & technician awareness. No mass end-user messaging in v1. |
| **Gear assumption** | Mixed / unknown — every device is just an **IP to ICMP-ping**. No SNMP/API yet. |
| **Alert channels** | **ntfy** only (free, no approvals, instant on phones). |
| **Where it runs** | On-prem, single always-on machine (office PC / mini-PC / Pi). SQLite + WAL. |
| **Realism** | Probers & notifiers sit behind small interfaces (`build_prober`/`build_notifier`); the real ICMP prober + ntfy notifier are the only impls. Tests inject a recording notifier double instead of hitting the network. |

### Re-scoped away from the original brief (and why)
- **WhatsApp / SMS / Twilio / IVR → dropped from v1.** The goal is operator awareness, not
  end-user comms; we don't build it.
- **Automatic cause inference → dropped.** An earlier version tried to guess power-vs-link
  from device co-location; it was never wired to the UI and has been removed. Cause is now an
  operator-entered post-mortem fact, not a guess.

---

## The model: what "shared infrastructure" looks like

```
            Internet
               │
        [ Core / Gateway ]
               │
        [ Main Tower A ]  ── backhaul ──  [ Relay Tower B ]
            │     │                            │
        [Relay] [Sector AP]               [Sector AP]
```

Each node is a `devices` row with a `parent_device_id`, so when a parent dies we mark its
children **UNREACHABLE** instead of paging about all of them.

---

## Design rationale (the "why" behind the engine)

The exact thresholds/counts and code-level invariants are in `CLAUDE.md`; this is the
reasoning they encode.

- **Flap suppression / hysteresis.** A wireless link blips constantly; paging on a single
  bad poll would train everyone to ignore alerts. So DOWN needs 3 consecutive 100%-loss
  polls, DEGRADED needs 2, recovery needs 2 healthy. That confirmation is a deliberate trade:
  never cry wolf. But the *3 samples* needn't be *3 minutes* — see "fast-confirm" below: those
  three samples are gathered in seconds via rapid re-probe of the suspect alone, so we keep the
  hysteresis and still detect in ~4s. The poll interval is then about steady-state probe load,
  not detection latency.
- **Uplink canary.** If our own office internet is down, every tower looks down. Pinging a
  canary first lets us send ONE `UPLINK_DOWN` and freeze transitions, instead of a storm —
  and means "our internet is down" never masquerades as "the towers are down."
- **Topology suppression.** One "Tower A down" is actionable; forty "sector down under
  Tower A" alerts are noise. A child of a down parent becomes UNREACHABLE and is never paged.
- **Cause is operator-confirmed, not guessed.** An earlier version auto-tagged each DOWN as
  "Likely Power Outage" vs "Link/Equipment Fault" from a `power_ref_ip` / co-location
  heuristic, with a per-device `criticality`. Both were **removed** (migration `0005`): neither
  was ever settable from the UI, so the guess was always inert. Cause is now captured by the
  operator at resolution via the post-mortem (`root_cause` + `resolution_notes`) — a confirmed
  fact, not an inference.
- **Durable, restart-safe memory.** Outages, alerts, and escalation timers live in the DB
  (not in-memory timers), and the FSM rehydrates from the last poll on startup — a crash or a
  deliberate restart never drops an escalation or re-pages everyone.

These three — flap suppression, canary, topology — are the heart of the tool; everything else
(BI, dashboard, env-var config) is built around keeping them trustworthy.

### Scaling (so the alarm stays trustworthy at fleet size)
The same "never lie" principle drives the fleet-scale work; the code-level invariants are in
`CLAUDE.md` §"Scaling invariants".
- **Bounded probe fan-out.** Probes run under an `asyncio.Semaphore` (`WISP_MAX_INFLIGHT`,
  default 256). An unbounded fan-out opens one ICMP socket per device per tick; past the
  process FD limit the kernel refuses sockets and every excess probe reads as 100% loss — a
  *fake mass outage exactly at peak fleet size*. Bounding it keeps a 10k-device fleet within
  the poll window on a few hundred FDs.
- **Gentle on aggregation gear.** A tower/switch/AP that backhauls hundreds of customers
  rate-limits ICMP to its own control plane. Any device that is a *parent* of another is probed
  with fewer echoes per poll (`WISP_PINGS_PER_POLL_INFRA`, default 2) so we don't read that
  rate-limiting as phantom loss on the very box that matters most.
- **Raw polls are scratch; rollups are the trend record.** Nothing reads the historical body of
  `poll_results` (only the latest state per device + a forensic window), so the daemon folds it
  hourly into compact `poll_rollups` (one row per device per hour). Trend charts read hours, not
  a billion raw rows, and raw retention can be cut short without losing history. `outages` stays
  the source of truth for incidents.
- **Fast-confirm — detection in seconds, not minutes.** Detection used to be
  `down_consecutive × poll_interval` (~3 min). Now, the instant a poll reads 100% loss, the
  daemon re-probes *only that device* back-to-back every `WISP_RETRY_INTERVAL_S` (default 2s)
  until it gathers the 3 all-lost samples (→ DOWN) or it comes back reachable (→ a blip, cleared,
  never paged). Detection ≈ 4s, the healthy fleet keeps its gentle cadence, and the 3-sample
  hysteresis is unchanged — this is the soft-state/hard-state model (cf. Nagios `retry_interval`),
  just confirmation decoupled from the steady-state poll. The next rung, for gear we control, is
  event-driven ingress (SNMP traps / controller webhooks / BFD) for sub-second — the prober/notifier
  interfaces leave room for it without reworking the engine.

---

## Going live

The real ICMP prober + ntfy notifier are now the only adapters; bringing it up on the
always-on box is the remaining setup. The dashboard is the device/team control plane (Nodes +
Team, with the daemon self-reloading the device set in-process); tunables are `WISP_*` env vars
on the systemd units (no DB config layer). Backup is built in.

1. **Dependencies:** `python3 -m venv .venv && . .venv/bin/activate && pip install -r
   requirements.txt` (`icmplib`/`httpx`). Never install globally (system Python is
   PEP 668-locked).
2. **ICMP permission:** the prober uses unprivileged ping sockets, so just enable the kernel
   ping group once — `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"` (persist it in
   `/etc/sysctl.d/`). No root, no `cap_net_raw`.
3. **Inventory:** enter the real devices + parent→child topology from the dashboard **Nodes**
   page.
4. **Channels:** the notifier is ntfy. Set `WISP_NTFY_URL` and the three role topics
   (`WISP_NTFY_TOPIC_*`) on the systemd units, add workers (owner + region techs) on the
   **Team** page so each subscribes to their role's topic, then use **Settings ▸ Send test
   alert** to confirm routing *before* a real outage depends on it.
5. **Run under systemd** (`deploy/wisp-*.service`) for auto-start and crash-restart. Node edits
   apply on their own (the daemon self-reloads the device set); `WISP_*` tunable changes need a
   daemon restart. Keep the dashboard on the office LAN (plain HTTP + PIN).
6. **Tune thresholds/cadence** (`WISP_POLL_INTERVAL_S`, `WISP_LOSS_DEGRADED`, … — or
   `WISP_POLL_INTERVAL_ADAPTIVE=1` for faster detection on a small fleet) against how the real
   links actually blip, then restart the daemon.

### What ping-only can't show (future SNMP/controller layer)
Throughput/bandwidth, signal strength (RSSI/SNR), per-link usage, CPU/temperature. When
the gear is known, **signal strength + throughput** are the two highest-value additions —
they enable failure *prediction*, not just outage *detection*. The interfaces are designed
so this layers on without reworking the engine.

---

## Open questions (for the real deployment)

- **Device inventory:** the real towers/relays/backhaul nodes and their parent→child topology
  (even a hand-sketch) to enter on the Nodes page.
- **Static IPs / management reachability:** are the towers reachable by stable IPs from where
  the monitor sits (management VLAN, or over the radios themselves)? This decides whether a
  "down" reading is the device or just the path to it.
- **Canary target:** `1.1.1.1`, or the actual upstream provider gateway/BNG (a better signal
  of *your* uplink specifically)?
- **Owner & techs:** the real ntfy topic names for the three roles (`WISP_NTFY_TOPIC_*`), so the
  team subscribes to the right channels and escalation routing is live. Routing is role→topic;
  there is no per-person key.
- **Later — end-user comms:** if/when wanted, which is realistic locally — SMS (DLT-registered)
  or WhatsApp — and do end users expect per-outage messages or a status page they check?
