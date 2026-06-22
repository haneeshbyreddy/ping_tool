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

Phases 1–6 (engine, FSM, alerting, BI, dashboard) **and Phase 8** (in-UI config, team
directory, PIN gate, monitor lifecycle) are **done** — 58 tests. The build now targets a
**real environment**: the mock notifier and simulated prober (and the demo seeder) have
been removed, so the daemon polls with `IcmpProber` and alerts via `NtfyNotifier`. The
dashboard + tests remain pure stdlib; the daemon needs the venv (`icmplib`/`httpx`) and
the kernel ping group enabled. See "Going live" below.

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
  polls, DEGRADED needs 2, recovery needs 2 healthy — ~3 min to declare DOWN. That delay is
  a deliberate trade: never cry wolf.
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
(BI, dashboard, in-UI config) is built around keeping them trustworthy.

---

## Going live

The real ICMP prober + ntfy notifier are now the only adapters; bringing it up on the
always-on box is the remaining setup. The control plane is already built (Settings/Team/
Channels in the dashboard, DB-backed config with daemon self-reload, systemd units, backup).

1. **Dependencies:** `python3 -m venv .venv && . .venv/bin/activate && pip install -r
   requirements.txt` (`icmplib`/`httpx`). Never install globally (system Python is
   PEP 668-locked).
2. **ICMP permission:** the prober uses unprivileged ping sockets, so just enable the kernel
   ping group once — `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"` (persist it in
   `/etc/sysctl.d/`). No root, no `cap_net_raw`.
3. **Inventory:** enter the real devices + parent→child topology from the dashboard **Nodes**
   page.
4. **Channels:** the notifier is ntfy. Set the ntfy base URL in **Settings ▸ Channels**, add
   workers (owner + region techs) with their ntfy topic on the **Team** page, then use **Send
   test alert** to confirm routing *before* a real outage depends on it.
5. **Run under systemd** (`deploy/wisp-*.service`) for auto-start and crash-restart. Settings
   and node edits apply on their own (the daemon self-reloads). Keep the dashboard on the
   office LAN (plain HTTP + PIN).
6. **Tune thresholds** (Settings ▸ Detection) against how the real links actually blip.

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
- **Owner & techs:** real ntfy topics (and phone routing keys) per worker, so escalation routing
  is live (owner gets escalations + the daily digest).
- **Later — end-user comms:** if/when wanted, which is realistic locally — SMS (DLT-registered)
  or WhatsApp — and do end users expect per-outage messages or a status page they check?
