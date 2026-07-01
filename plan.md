# Village WISP Monitor — Plan

A multi-tenant network monitoring + alerting platform for rural WiFi broadband operators
(ISPs). Central runs the brain — the FSM, topology-aware suppression, fast-confirm
detection, and the ntfy alerting ladder — for every ISP it serves. Each ISP's edge box is
a thin probe: real ICMP (and, later, SNMP) against its own network, reporting raw results
to central. ISPs log into the central dashboard with their own account and manage
everything — topology, team, alert routing, outage history — from the browser; nothing to
run locally beyond the probe.

> **What's here:** the design rationale (the why), what's done, and what's next. The
> per-feature build detail lives in the code, `README.md` (what/how/layout/config), and
> `CLAUDE.md` (invariants & gotchas) — this file stays the rationale, not a duplicate.

## Status

**Done — this is the whole platform now, not an add-on to a single-box tool.**

- **Central management plane** — ISPs manage device topology, team, and per-org alert
  settings from the central dashboard, independent of any edge. Accounts are
  central-provisioned: a superadmin onboards each ISP; org users are scoped to their own
  tenant with a role (owner/operator/tech).
- **Central runs the brain** — `MonitorEngine` (the FSM) and the alerting ladder run on
  central, fed by raw per-IP samples the edge probes report over `POST /report`. This
  includes the fast-confirm round trip (central names a suspect IP in its reply; the edge
  re-probes just that IP every couple seconds until confirmed or cleared) and the canary/
  uplink freeze — both tested end to end over a real socket.
- **The edge is a thin probe, full stop.** No local database, dashboard, PIN, or FSM on
  the edge box. It fetches its topology from central, probes with real ICMP under a
  bounded-concurrency fan-out, and reports back. One daemon mode; nothing else.
- **Fleet deploy + self-update — core logic done.** Central is the version authority with
  a staged, health-gated, auto-rollback rollout; a small edge supervisor owns
  verify→atomic-swap→restart→health-gate→rollback. Publishing/rollout is exercised
  end-to-end in tests. What's *not* exercised outside CI: the actual multi-arch
  PyInstaller build, code-signing, and the Windows installer on real hardware.

206 tests, `python -m unittest discover -s tests`.

## The model: what changed from a single-box tool

This platform grew out of a single-box appliance (one daemon + one local dashboard, one
ISP, one SQLite file) — that history is still visible in git log if you want the
archaeology, but it is **not** how the system runs today and this file no longer
describes it as current. The edge kept its detection *speed* characteristics (bounded
probe fan-out, gentle probing of aggregation gear, the fast-confirm mechanism) but lost
everything that made it a standalone product — the FSM, the alerting, the local UI, and
the local database all now live on central, multiplied across tenants instead of hardcoded
to one operator.

```
  ISP "A"                                        Central (multi-tenant)
  ┌──────────────────────────┐                  ┌───────────────────────────┐
  │ edge-a1 (thin probe)      │  POST /report    │ POST /report, GET         │
  │  IcmpProber only ─────────┼─── raw pings ───►│  /edge/devices  (per-org  │
  │  no local FSM/DB/alerting │◄── recheck hint ─┤  topology)                │
  │  + supervisor (updates) ◄─┼── version/url ───┤      │                    │
  └──────────────────────────┘   in heartbeat    │      ▼                    │
  ┌──────────────────────────┐   reply           │ MonitorEngine (per tenant,│
  │ edge-a2 ...               │                  │ in-memory, restart-safe) │
  └──────────────────────────┘                   │      │                    │
  ISP "B": edge-b1, edge-b2 ─────────────────────►│      ▼                    │
                                                  │ CentralAlertDispatcher ──┼──► ntfy
                                                  │ multi-tenant dashboard   │  (per-org
                                                  │ + fleet watchdog         │   topics)
                                                  │ + version authority      │
                                                  └───────────────────────────┘
```

## Design rationale (the "why" behind the engine — unchanged by where it runs)

The exact thresholds/counts and code-level invariants are in `CLAUDE.md`; this is the
reasoning they encode. None of this changed when the FSM moved from the edge to central —
it's the same `MonitorEngine`, reused verbatim.

- **Flap suppression / hysteresis.** A wireless link blips constantly; paging on a single
  bad poll would train everyone to ignore alerts. So DOWN needs 3 consecutive 100%-loss
  samples, DEGRADED needs 2, recovery needs 2 healthy. That confirmation is a deliberate
  trade: never cry wolf. But the *3 samples* needn't be *3 minutes* — the fast-confirm
  round trip gathers them in seconds by having central name the suspect and the edge
  re-probe just that IP, so the poll interval is mostly about steady-state probe load, not
  detection latency.
- **Uplink canary.** If an ISP's own internet is down, every device behind it looks down.
  Pinging a canary first lets central send ONE `UPLINK_DOWN` and freeze that tenant's
  transitions for the cycle, instead of a storm — "our internet is down" never
  masquerades as "the towers are down."
- **Topology suppression.** One "Tower A down" is actionable; forty "sector down under
  Tower A" alerts are noise. A child of a down parent becomes UNREACHABLE and is never
  paged separately — unless it has a live backup path, in which case it's a genuine fault
  and still pages (that "any parent alive → real fault" rule generalizes past a single
  parent for free).
- **Cause is operator-confirmed, not guessed.** The engine never infers *why* a device is
  down. Cause is captured by the operator at resolution via the post-mortem — a confirmed
  fact, not an inference. (An early single-box version tried automatic power-vs-link
  inference from device co-location; it was never wired to any UI and was removed.)
- **Durable, restart-safe memory.** Outages, alerts, and escalation timers live in the DB
  (not in-memory timers), and the FSM rehydrates from the last known state on startup — a
  central restart never drops an escalation or re-pages everyone. `EngineRegistry` keeps
  one live engine per tenant so a device's flap-suppression streak survives across an
  edge's successive reports (an HTTP request is stateless; the FSM's counters are not).

These — flap suppression, canary, topology, restart-safety — are the heart of the tool;
everything else (multi-tenancy, the dashboard, fleet updates) is built around keeping
them trustworthy at scale, across many ISPs, without any of them stepping on each other.

### Scaling (so the alarm stays trustworthy at fleet size)
- **Bounded probe fan-out.** Probes run under an `asyncio.Semaphore` (`WISP_MAX_INFLIGHT`,
  default 256) on the edge. An unbounded fan-out opens one ICMP socket per device per
  tick; past the process FD limit the kernel refuses sockets and every excess probe reads
  as 100% loss — a *fake mass outage exactly at peak fleet size*.
- **Gentle on aggregation gear.** A tower/switch/AP that backhauls hundreds of customers
  rate-limits ICMP to its own control plane. Any device that is a *parent* of another is
  probed with fewer echoes per poll (`WISP_PINGS_PER_POLL_INFRA`, default 2) so the edge
  doesn't read that rate-limiting as phantom loss on the very box that matters most.
- **Fast-confirm — detection in seconds, not minutes.** The instant central's engine sees
  a suspect sample (100% loss on a device not yet DOWN, or a reachable sample on a device
  still DOWN), its reply to `POST /report` names that IP; the edge re-probes *only that
  device* every `WISP_RETRY_INTERVAL_S` (default 2s) and reports back until the FSM
  confirms or clears it. Detection ≈ a few seconds, the healthy fleet keeps its gentle
  cadence, and the 3-sample hysteresis is unchanged.
- **One engine per tenant, not per request.** Central's ingest is stateless HTTP, but flap
  suppression needs streaks to accumulate across requests. `EngineRegistry` solves this
  with one in-memory `MonitorEngine` per tenant, rebuilt only when that tenant's topology
  actually changes.

## Locked decisions (do not relitigate without a real reason to)

| Topic | Decision |
|---|---|
| **Where the brain runs** | Central, for every tenant. The edge never runs an FSM, never persists an outage, never alerts on its own. |
| **What we monitor** | Shared infrastructure — towers, relays, backhaul, core, switches. Not end-user routers (yet). |
| **Alert channels** | **ntfy** only. Three fixed topics per org (owner/operator/tech); a fresh DOWN pages owner+operator, the hourly escalation broadcasts to all three until recovery. |
| **Multi-tenancy** | Non-negotiable. An org user must never see another org's data; every central read is tenant-scoped. |
| **Where the edge runs** | On-prem at the ISP, one probe process, no local DB/dashboard. Frozen-binary (fleet, PyInstaller + supervisor) or venv (single box, systemd) — both talk the same wire protocol to central. |
| **Transport** | The edge dials central; central never connects in. Bearer-token auth for now (`WISP_CENTRAL_TOKEN`); the envelope is versioned so mTLS can slot in later without a wire change. |
| **Updates** | Pull-based over the existing heartbeat/report channel (no inbound holes). Central is the version authority; rollouts are staged + health-gated + auto-rollback. |
| **Realism** | Probers & notifiers sit behind small interfaces (`build_prober`/`build_notifier`); the real ICMP prober + ntfy notifier are the only impls. Tests inject recording doubles instead of hitting the network. |

## What's next

The platform is feature-complete for its core job (detect, suppress, page, multi-tenant
dashboard) but has real gaps versus the single-box tool it replaced, plus production
groundwork that hasn't been done yet. In rough priority order:

1. ~~**SNMP port monitoring on central.**~~ **Done.** The single-box tool could watch switch
   uplink ports (IF-MIB oper/admin status) and fold a port-down into the device outage it
   feeds; that's now wired end to end on the current platform. `POST /report` carries an
   optional `ports` key ({device_id: [port dict, ...]}) on the edge's own slow SNMP cadence
   (`WISP_SNMP_INTERVAL_S`, independent of the ICMP poll interval — ports don't flap like
   radio links), and `central/ports.py:CentralPortMonitor` is the central-side consumer,
   mirroring the old edge design one-for-one: `monitored` ports only, admin-down silent
   (reuses `ingress/snmp.py`'s `PortStatus.is_down()`), fold into an open outage
   (`stamp_outage_cause`) rather than a competing alarm, and a leading-indicator heads-up
   when there's no open outage yet. SNMP *bandwidth* (`ifHCIn/OutOctets`/`ifHighSpeed` —
   `ingress/snmp.py` already parses these) is still not carried on the wire or stored
   centrally; that remains a follow-up alongside item 3 below.
2. **Central-side historical rollups / trend analytics.** Central has live `device_states`
   but nothing like the old `poll_rollups` table — no uptime chart, no latency trend, no
   SLA reporting. An ISP asking "how reliable was Tower A last month" has no answer today.
3. **Per-link performance baseline + on-backup redundancy signal, on central.** Both
   existed on the old edge as soft-signal tiers (a link slow/jittery vs its own baseline;
   a node running on a backup path with primary uplink down) and were genuinely useful
   heads-up signals, not just outages. Central would need its own trailing-sample storage
   to reintroduce either — don't bolt them onto `device_states` (a single current-state
   row) without designing that storage properly.
4. **Actually deploying central for production.** Everything to date has been local dev
   (`run.sh`) or a single test process. Central is meant to run somewhere always-available
   (a small always-on VM is enough to start) with a TLS terminator in front of it (it
   speaks plain HTTP itself, by design, to stay dependency-free) — none of that
   provisioning exists in this repo yet.
5. **Fleet update system hardening.** The rollout/supervisor *logic* is tested; the
   PyInstaller multi-arch build, code-signing (Authenticode/minisign), and the Windows
   installer have never run outside this dev sandbox. Needs real CI runners + real signing
   keys to validate.
6. **mTLS enrollment**, replacing the static bearer-token stopgap for edge↔central auth —
   deferred from the original design on purpose; revisit once there's more than a handful
   of edges to make cert issuance/rotation worth building.

None of these block the platform from running today — `WISP_CENTRAL_BRAIN=1` +
`WISP_CENTRAL_URL` is a complete, working loop. They're the gap between "works" and
"as capable as the tool it replaced, running in production for real ISPs."

## Open questions (answer as you build each item above, don't block on them)

- ~~**SNMP wire format**~~ — decided: extended the existing `POST /report` envelope with a
  `ports` key rather than a sibling endpoint, since it rides the same auth/tenant/idempotency
  machinery for free; it runs on its own slower cadence (`WISP_SNMP_INTERVAL_S`) within that
  same envelope rather than every cycle, since ports don't flap like radio links.
- **Rollup ownership:** does central fold `device_states` history itself (a new sweep,
  central's own cron-equivalent), or does the edge ship periodic snapshots for central to
  fold? Given central already owns all detection, folding locally seems right — but check
  the DB-lock discipline (`CentralStore`'s single writer lock) before assuming it's free.
- **Central hosting:** which provider/region, and does one box serve every tenant or does
  scale eventually demand sharding? Not urgent until tenant count says otherwise.
- **Rollout policy:** fully automatic per org, or operator-approved? Canary size? These
  are product calls for whoever's running the platform, not code-shape choices.
- **Data residency / retention** across tenants once rollups exist — one shared retention
  policy, or per-org?
