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

---

# Phase 9 — Graph topology (backup lines) + SNMP port status

> **For the next session that implements this.** This is a design brief, not a spec to
> follow blindly. The four **product** decisions in "Locked" are the operator's call —
> honour them. Everything else is my **recommendation**; where you see *(your call)* you are
> expected to read the code and decide what's actually best, even if it diverges from here.
> Before you touch anything, internalise the invariants in `CLAUDE.md` — engine purity, the
> FK-delete discipline in `delete_device`, idempotent forward-only migrations, and
> restart-safety. When you're done, **update `CLAUDE.md` and `README.md`** so they describe
> the new reality (this repo keeps its docs honest — that's a hard rule here).

## The lens (why these two features belong together)

This tool **never routes through the topology**. It ICMP-pings every device's management IP
directly; `parent_device_id` is used for exactly two things — *alert suppression*
(`MonitorEngine.process_cycle`, the override around `state_machine.py:256`) and *blast-radius
attribution* (`triage_outages._culprit`, `services.py:171`). Probing is flat; topology is pure
inference layered on top.

That frames both features:

- **Graph nodes** change the *inference rules*. Today a child is declared `UNREACHABLE` the
  instant its one parent is down. With a backup line that's a lie — if the primary path is dead
  but the backup carries traffic, the child is genuinely reachable, and *"running on backup" is
  itself the most valuable alert the tool can't currently raise* ("you are one failure from an
  outage").
- **SNMP ports** add a *new, more specific ingress signal*. Instead of inferring "Tower B is
  down" from ping timeouts, the switch states directly "port Gi0/2 — the Tower B backhaul — is
  down," sooner and with the physical cause attached.

End state: the graph models the redundant *paths*; SNMP reports the live state of the physical
*links* those paths are built from. Build them in sequence (Part A then Part B), but design Part
A's edge table knowing Part B will hang a port off an edge.

## Locked decisions (operator's call — do not relitigate)

1. **On-backup is NOT louder.** Primary down + backup carrying traffic = a quiet dashboard badge
   plus a single operator page on the edge. It does **not** enter the outage/escalation ladder.
   Clone the perf-tier pattern (`device_perf` + `AlertDispatcher.perf_sweep`, `notifiers.py:397`).
2. **SNMP is the simple version.** SNMP **v2c** with a community string. Don't build v3
   auth/priv now (leave the column/enum room for it, but no implementation).
3. **A monitored port-down folds into the device outage it feeds** — it is *not* a separate,
   competing alarm. A port with a downstream device enriches that device's outage narrative; it
   does not page on its own track.
4. **Library: `pysnmp`** (lazy-imported in the daemon venv, exactly like `icmplib`). Keeps the
   dashboard + the test suite pure-stdlib. (This was left to me; that's the call.)

---

## Part A — Graph topology (backup lines)

### Model — edge table, primary kept denormalized *(recommended)*

Add an explicit edges table and **keep `devices.parent_device_id` as the denormalized primary
parent**, backfilled from it:

```sql
-- migration 0011_device_links.sql (sketch — names/columns your call)
CREATE TABLE IF NOT EXISTS device_links (
    id         INTEGER PRIMARY KEY,
    child_id   INTEGER NOT NULL REFERENCES devices(id),
    parent_id  INTEGER NOT NULL REFERENCES devices(id),
    kind       TEXT NOT NULL DEFAULT 'primary',   -- 'primary' | 'backup'
    is_active  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(child_id, parent_id)
);
-- backfill: every existing parent_device_id becomes a 'primary' edge
INSERT OR IGNORE INTO device_links (child_id, parent_id, kind)
SELECT id, parent_device_id, 'primary' FROM devices WHERE parent_device_id IS NOT NULL;
```

Why this and not the alternatives:
- A single `backup_parent_device_id` column is too weak — it can't generalise to N paths or
  carry per-link metadata (and Part B wants to hang a port off a specific edge).
- A full DAG that *drops* `parent_device_id` is the cleanest model but rewrites every query that
  touches the parent column (tree render, `nodes_list`, `list_devices`, `_topological_order`,
  `_culprit`, the cycle check, the delete-FK dance) for little extra capability. **Not worth it
  for v1.** Keeping `parent_device_id` as "the primary" means every existing tree query keeps
  working unchanged; the edge table is consulted *only* for the redundancy questions.

If, reading the code, you conclude the full-DAG cut is actually cleaner than maintaining two
sources of truth — *(your call)*, but then you own migrating all of the above and the test churn.

### Semantics — the actual engine change

The suppression override (`state_machine.py:256`) changes from **single-parent** to
**all-parents-down**:

> child FSM is `DOWN` **and** *every* one of its parents ∈ `DOWN_FAMILY` → relabel `UNREACHABLE`
> (suppress the page). If **any** parent is alive, the child stays genuinely `DOWN` and pages —
> the backup path works yet the child still won't answer, so it's a real fault, not a topology
> artifact.

**Backward-compat (state this in the test):** with exactly one parent, "all parents down" ≡
"the parent is down", so every existing single-parent test and behaviour is preserved byte-for-
byte. That's the safety net that lets you change the override without moving the full-pass path.

A new orthogonal signal falls out for free:

> **on-backup** = a node's *primary* parent ∈ `DOWN_FAMILY` but at least one *backup* parent is
> alive. The node itself pings fine, so this must **not** open an outage. Per decision #1: badge +
> a single operator page on the enter/leave edge, perf-tier style.

**Compute it inside the engine, not as a DB sweep** *(recommended)*: on-backup is a pure function
of the committed parent states (already in hand inside `process_cycle`) plus each edge's `kind`.
Return it in `CycleResult` (e.g. a `redundancy: dict[int,str]` field) and let the daemon persist
the badge + the dispatcher fire the edge page. This keeps the engine pure and avoids a second
per-cycle DB pass. (The perf tier is a sweep only because it needs trailing history; redundancy
needs only this cycle's states, so the engine is the natural home.) If you'd rather mirror
`perf_sweep` exactly for consistency — *(your call)*.

**One-level direct-parent check is sufficient — don't enumerate paths.** Because every node is
pinged directly *and* `process_cycle` evaluates parents-before-children in topological order, a
node whose entire upstream is dead reads 100% loss itself and is caught by the all-parents-down
test at its own level — the cascade handles multi-hop. Full path-to-root reachability would be
more code for no extra truth (ping can't see whether an "up" parent is actually forwarding
anyway). Keep it one-level.

**Canary freeze:** because `process_cycle` returns early under freeze (`state_machine.py:241`),
computing redundancy inside it is automatically frozen too — good. If you make it a sweep instead,
guard it on `result.canary_down` like the daemon already guards fast-confirm.

### Blast radius — the honest file-by-file list

- **Migration** `device_links` + backfill (above).
- **`DeviceMeta` / `load_device_meta`** (`state_machine.py:34`, `:283`): carry each device's
  parent edges (with `kind`). Remember the CLAUDE rule: if you add a `devices` column, update
  **both** `DeviceMeta` and the SELECT. (Loading edges is a second query/join — fine.)
- **`MonitorEngine._topological_order`** (`state_machine.py:162`): generalise the single-parent
  queue to **Kahn's by in-degree** (a node after *all* parents). Bonus: Kahn's detects cycles
  cleanly and can retire the ad-hoc `guard` loop.
- **`process_cycle`** (`:208`): all-parents-down suppression + emit redundancy state.
- **`probe_plan`** (`:188`): "is a parent" becomes "appears as any `parent_id` in the edge set",
  so gentle-infra probing stays correct for a node that is only a *backup* parent.
- **`triage_outages._culprit`** (`services.py:171`): walk **all** parents; attribution gets more
  accurate (a child is blamed on a down ancestor only if *every* path runs through down
  ancestors). `affected_children` improves for free.
- **Cycle check** in `_clean_device_payload` (`services.py:838`): generalise to the full edge set
  (DAG cycle detection over `device_links`, not the single `parent_device_id` chain).
- **`delete_device`** (`services.py:917`): add `device_links` to the FK-clear list — **both
  directions** (rows where the device is `child_id` *or* `parent_id`) — before the device row, or
  `foreign_keys=ON` rejects the delete. This is the exact discipline CLAUDE.md already calls out.
- **Edge CRUD + API + UI**: a way to add/remove backup edges (Nodes page). Tree stays
  primary-parent-based; render backups as secondary edges and an **"on backup"** badge on the
  node. `services.list_devices` / `nodes_list` grow the edge data. *(UI shape: your call.)*
- **New sidecar for the badge** *(recommended)*: a `device_redundancy` table mirroring
  `device_perf` (PK `device_id`, `on_backup`, `primary_down_since`, `updated_at`) — or fold it
  onto an existing per-device state row if you find that cleaner. *(your call.)*

### Tests (Part A)
Mirror the existing layout (`tests/unit/test_state_machine`, `tests/integration/test_api`):
- multi-parent topological order (node after *both* parents; cycle handled).
- suppression: all-parents-down → `UNREACHABLE`; one-parent-up + child lost → genuine `DOWN`
  (pages); single-parent case unchanged (the back-compat anchor).
- on-backup enter/leave edge → badge written + single operator page, no outage opened, no
  escalation row.
- `_culprit` / `affected_children` over a diamond (two paths) — only suppressed when both dead.
- `delete_device` clears `device_links` both directions (FK-safe).

---

## Part B — SNMP port status

### Scope — IF-MIB oper/admin only (no resource monitoring)
Per the operator: **port up/down state, nothing else** — no CPU/mem/temp. That is purely:

| OID | What | Use |
|---|---|---|
| `ifOperStatus` `.1.3.6.1.2.1.2.2.1.8` | up(1)/down(2)/lowerLayerDown(7)/… | the actual signal |
| `ifAdminStatus` `.1.3.6.1.2.1.2.2.1.7` | up(1)/down(2) | never alarm on an admin-shut port |
| `ifName` `.1.3.6.1.2.1.31.1.1.1.1` (fallback `ifDescr` `.1.3.6.1.2.1.2.2.1.2`) | port id | display |
| `ifAlias` `.1.3.6.1.2.1.31.1.1.1.18` | operator's label ("→ Rampur backhaul") | **map port → what it feeds** |
| `ifLastChange` `.1.3.6.1.2.1.2.2.1.9` | last flap (sysUpTime ticks) | cheap flap detection |

`ifName`/`ifAlias` live in the **ifXTable** (`ifMIB`), so a single bulk-walk per switch pulls
oper+admin+name+alias+lastchange in a couple of GETBULK rounds.

### Architecture — a sibling ingress, NOT a Prober impl
Port status doesn't fit `Prober.ping(ip) -> PingResult` (one reading per IP; a switch has N
ports). So build a **parallel poller**, mirroring `IcmpProber` exactly:

- `ingress/snmp.py`: an `SnmpPoller` behind a tiny interface + `build_snmp_poller(cfg)`,
  **lazy-importing `pysnmp`** the way `IcmpProber` lazy-imports `icmplib` (`probers.py:82`). The
  dashboard + the 122 tests stay pure-stdlib; only the daemon venv gains `pysnmp` (add it to
  `requirements.txt`).
- Runs **in the same daemon** (one process already owns the single-instance lock + asyncio loop)
  on its **own, slower cadence** (`WISP_SNMP_INTERVAL_S`, default ~60–120s — ports don't flap like
  radio links, and one bulk-walk per switch is cheap). Slot it into `run_forever` as a separate
  timed task next to the prune/rollup guards, each wrapped in its own try/except — **a broken
  SNMP walk must never sink the ICMP cycle.** *(Exact loop wiring: your call.)*
- pysnmp has both sync and asyncio HLAPI, and the v6/v7 APIs differ. Pick whichever integrates
  cleanly with the existing `asyncio` loop and pin it in `requirements.txt`. *(your call.)*

### Data model *(sketch — your call on exact shape)*
```sql
-- on devices: SNMP is per-device config. NOTE: snmp_community is the FIRST per-device
-- credential in the DB (until now the only DB secret is the PIN hash). Low sensitivity,
-- but a conscious change — call it out in CLAUDE.md.
ALTER TABLE devices ADD COLUMN snmp_enabled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN snmp_version   TEXT;     -- '2c' for now (room for '3')
ALTER TABLE devices ADD COLUMN snmp_community TEXT;
ALTER TABLE devices ADD COLUMN snmp_port      INTEGER NOT NULL DEFAULT 161;

CREATE TABLE IF NOT EXISTS switch_ports (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES devices(id),
    if_index        INTEGER NOT NULL,
    if_name         TEXT,
    if_alias        TEXT,
    admin_status    TEXT,
    oper_status     TEXT,
    last_change     TEXT,
    monitored       INTEGER NOT NULL DEFAULT 0,     -- only alarm on flagged uplink/infra ports
    feeds_device_id INTEGER REFERENCES devices(id), -- the bridge to the graph (Part A edge)
    updated_at      TEXT,
    UNIQUE(device_id, if_index)
);
```
- **`monitored`** is essential: you do **not** want to alarm on every access port where a laptop
  comes and goes — only operator-flagged uplink/infra ports. Discovery = walk the ifTable once,
  list the ports (with `ifAlias`), let the operator tick which to monitor.
- **`feeds_device_id`** is the bridge: it ties a physical port to the downstream node it feeds
  (and, later, to a specific `device_links` edge — consider a `link_id` FK instead/as-well so a
  port maps to a *path*, not just a node). *(your call.)*
- Port history: probably **don't** need a per-sample table like `poll_results` — changes are
  rare. Logging transitions (down/up) to `alert_log`, or a tiny `port_events` table, is enough.
  *(your call.)*

### Detection + "fold into the device outage" (decision #3)
- Apply the same **flap-suppression discipline** you already trust — a port that bounces once
  shouldn't fire. Either consecutive-sample confirmation or trust `ifLastChange`. *(your call.)*
- A monitored port `oper=down` while `admin=up` is the alarm condition (admin-down = intentional,
  stay silent).
- **Folding:** a monitored port that has a `feeds_device_id` does not raise its own alert track.
  Instead it **enriches that device's outage** — e.g. when the downstream device is ICMP-down, the
  port-down becomes part of that outage's story (a more specific cause: "Gi0/2 → Tower B is
  down" instead of a bare "no ping response"). Reuse the existing `AlertDispatcher` + role topics;
  do not invent a parallel escalation ladder. *(Exact mechanism — annotate the outage row's
  cause, add to the alert body, or attach a port-state line — your call. The principle is: one
  incident per failure, with the physical port attached, not two competing alarms.)*
- **Decide explicitly** whether a critical-uplink port-down may *accelerate* outage detection
  (a port-down on a tower's sole uplink is instant proof it's cut, faster than 3 ICMP samples).
  For a first cut I'd keep ICMP as the outage owner and let SNMP *confirm/enrich* only — but if
  you see a clean way to let a critical port-down open the outage immediately, *(your call)*, just
  keep flap-suppression honest.

### Tests (Part B)
- `SnmpPoller` parsing: ifTable rows → port records (mock the pysnmp layer; **no real SNMP in
  tests**, same way the suite injects a recording notifier double and never hits ntfy).
- monitored vs unmonitored port-down (only monitored alarms).
- admin-down port stays silent.
- a port with `feeds_device_id` folds into that device's outage rather than firing standalone.
- flap suppression: a single down→up bounce doesn't alarm.

---

## Sequencing

1. **Part A — graph topology.** Self-contained to the engine/services/UI you already know;
   independently shippable and the foundation Part B leans on.
2. **Part B — SNMP ports** as a standalone ingress + dashboard panel.
3. **Bind them** — map a port to a `device_links` edge (`feeds_device_id`/`link_id`), so an SNMP
   port-down on a *backup* path warns you redundancy is gone *before* anything pings down, and an
   ICMP outage gets attributed to a physical port.

Each part is independently testable. Do **not** land it all in one swing.

## You decide (don't wait to be asked)

The operator handed the implementation judgment to you. Make and document these as you go —
pick what the code makes cleanest, not what's written above if you find better:

- Exact table/column names, migration numbering, and whether redundancy state is a `device_perf`
  clone or folded onto an existing row.
- Whether on-backup is computed in the engine (my rec) or as a sweep.
- Kahn's vs an incremental fix to `_topological_order`.
- pysnmp sync-vs-asyncio API and version pin.
- Port flap detection: consecutive samples vs `ifLastChange`.
- Whether a port maps to a node (`feeds_device_id`) or a path (`link_id`) or both.
- UI rendering of backup edges and the on-backup badge.
- Whether a critical-uplink SNMP port-down may accelerate ICMP outage detection.

When in genuine doubt about a **product** behaviour (not a code-shape choice), leave a short note
in the PR/commit rather than guessing silently.

## Invariants you must not break (see `CLAUDE.md` for the full set)

- **Engine stays pure** — no DB/network in `MonitorEngine`; keep the DB glue at the bottom of
  `state_machine.py`.
- **Migrations** are idempotent, forward-only, `IF NOT EXISTS` (or a bare one-shot `ALTER` per the
  0007/0009 pattern); never edit `0001_init.sql` in place.
- **`delete_device`** must clear every table that `REFERENCES devices(id)` before the device row —
  now including `device_links` (both directions) and `switch_ports`.
- **Restart-safety** — the FSM rehydrates from the last `poll_results` row; don't add state that
  re-pages an open outage on restart (read the redundancy/port badges back like `perf_sweep`
  rehydrates `was_degraded`).
- **A bad cycle never kills the daemon** — every new per-cycle task (SNMP walk included) lives
  inside its own try/except that logs and continues.
- **Tests inject doubles; no real ICMP/SNMP/ntfy in the suite.** Add to the existing `tests/`
  layers; run `python -m unittest discover -s tests` (currently 122 — keep them green and add
  cases for every new path above).

## Open questions for the operator (surface before/while building)

- **Which switches speak SNMP**, at what community string, and is read-only v2c acceptable on the
  management VLAN? (If any site needs v3, flag it — we scoped v3 out, so it'd be a follow-up.)
- **Which ports actually matter** (the uplink/backhaul/infra ports to flag `monitored`) — ideally
  from their `ifAlias` labels, so discovery can pre-suggest them.
- **The real redundant paths**: which towers/relays have a backup line, primary vs backup, and
  does the backup ride a switch port we can watch (so Part B can confirm Part A)?

---

# Phase 10 — Edge nodes + central server (distributed, multi-tenant)

> **For the next session that implements this.** A design brief, not a spec. The calls in
> "Locked decisions" are made — honour them. Everything else is a recommendation; where you see
> *(your call)* read the code and decide. The hard rule still stands: when you ship a part,
> update `CLAUDE.md` + `README.md` so they describe the new reality, and keep the suite green
> (`python -m unittest discover -s tests`) with new cases for every new path.

> **Status — Parts A + B shipped.** Part A: the edge shipper + `outbox` (migration 0015) + heartbeat +
> a skeleton central ingest server (`apps/central`, `src/wisp/central`, `src/wisp/egress/shipper.py`,
> `src/wisp/database/outbox.py`); `WISP_CENTRAL_URL` empty keeps every existing deployment byte-for-byte
> standalone. Part B: the multi-tenant central store (orgs auto-provisioned, every read tenant-scoped),
> the **global device-id mapping** per decision #6 (`devices` table maps `(tenant,node,edge-local id)`→a
> central id), and the **cross-edge fleet watchdog** (`central/watchdog.py` — pages an org when a node's
> heartbeat goes stale, restart-safe like the edge watchdog). The "serialized ingest writer" is a
> process-wide lock in `CentralStore` (Postgres behind the same surface is the documented upgrade).
> Part C: the central **multi-tenant dashboard + per-org accounts** (`central/auth.py`,
> `central/admin.py`, `central/static/`). Two auth planes — ingest stays the machine bearer token,
> the dashboard uses identity-carrying signed-cookie sessions; accounts are central-provisioned
> (superadmin onboards each ISP; org users scoped to their tenant with a role); every read is
> tenant-scoped. **Team + attendance became org-wide central concepts** (decision honoured), but the
> live per-outage paging ladder **stays on the edge** (decision #2 resilience — central owns the
> picture, the edge owns the page).
>
> Part D — **the testable core is shipped**: central is the **version authority** with a
> **staged, health-gated, auto-rollback rollout** (`central/rollout.py` — canary→promoted→done|halted),
> the **heartbeat reply is the update channel** (carries `{target_version, url, sha256}`), and the edge
> **supervisor** (`runtime/supervisor.py`) owns verify→atomic-swap→health-gate→rollback. All unit-tested
> and validated end-to-end (publish → canary rollout → directive → supervisor apply → auto-promote →
> done). The **deploy/CI scaffolding** is written — PyInstaller spec (`deploy/wisp-edge.spec`), fleet
> systemd unit, `curl|sh` Linux installer (`deploy/install-edge.sh`), the supervisor entrypoint
> (`apps/supervisor/main.py`), and the GitHub Actions release pipeline (`.github/workflows/release.yml`).
> **What still needs real CI + hosts to exercise** (not runnable in this dev sandbox): the actual
> PyInstaller multi-arch build, code-signing (Authenticode/minisign), the Windows Inno Setup installer,
> and mTLS enrollment/cert-rotation (still on the static-bearer-token stopgap). Code-level invariants
> live in `CLAUDE.md` §"Central reporting"; this brief stays the *why*, per the repo's docs rule.

## The lens (what actually changes, and what doesn't)

Today: **one daemon + one dashboard, one SQLite, one site, one shared PIN.** The target is
**many edge nodes** (each is *today's daemon*, almost unchanged) across **many ISPs/tenants**,
all reporting to **one central server** (aggregation + a multi-tenant dashboard + fleet updates).

The pivot that makes this tractable: **the edge IS today's appliance plus a shipper; the central
server is a NEW, separate plane.** We are not rewriting `MonitorEngine`, the FSM, fast-confirm,
the SNMP ingress, or local ntfy alerting — those stay on the edge, where the prober is. Central
is additive. The back-compat anchor (state it in tests, like single-parent in Phase 9): with
`WISP_CENTRAL_URL` unset, an edge is **byte-for-byte today's standalone monitor** — the entire
distributed layer is dormant.

```
  ISP "A" site                                  Central (multi-tenant)
  ┌──────────────────────────┐                  ┌───────────────────────────┐
  │ edge-a1  (today's daemon) │                  │ ingest API  (outbox sink) │
  │  IcmpProber + FSM + SNMP  │── pages ntfy     │      │                    │
  │  + NtfyNotifier (LOCAL) ──┼──► (operators)   │      ▼                    │
  │  + OUTBOX shipper ────────┼── mTLS, edge ───►│ central store (per-tenant)│
  │  local SQLite + outbox    │   initiated      │      │                    │
  │  + SUPERVISOR (updates) ◄─┼── version/url ───┤      ▼                    │
  └──────────────────────────┘   in heartbeat   │ multi-tenant dashboard    │
  ┌──────────────────────────┐   reply          │ + cross-edge watchdog     │
  │ edge-a2 ...               │                  │ + version authority       │
  └──────────────────────────┘                  │   (staged rollout)        │
  ISP "B": edge-b1, edge-b2 ──────────────────► └───────────────────────────┘
```

## Locked decisions (made — do not relitigate)

1. **Smart edges, aggregating centre.** Detection (FSM, fast-confirm, between-cycle watch),
   topology suppression, the canary, SNMP, and **immediate alerting all stay on the edge.** The
   detection loop is a tight prober↔FSM loop; crossing the WAN for it would destroy the
   seconds-level detection. Topology is per-site. Central never runs an FSM.
2. **The edge pages its own devices over ntfy immediately; central owns the org-wide view.**
   Resilience first: the WAN is most likely to break *during* an ISP's outage, and the alarm must
   survive the thing it alarms about. Central owns cross-edge correlation, the multi-tenant
   dashboard, the fleet/heartbeat watchdog, and the analytics rollup. **No double-paging** — the
   edge owns the page, central owns the picture. (The org-wide escalation/attendance question is
   deferred to Part C; for the first cut escalation stays on the edge too.)
3. **Transport: HTTPS + mTLS, edge-initiated outbound ONLY.** The edge dials central; central
   never connects in. Zero inbound firewall holes at any site (edges live behind ISP NAT/CGNAT).
   The client cert *is* the node's identity *is* its enrollment — one mechanism for encryption +
   authn. (WireGuard+HTTP was the alternative; rejected — more ops, identity still separate.)
4. **Store-and-forward outbox on the edge (SQLite).** The daemon writes records to a local
   `outbox` transactionally; a shipper drains it and deletes on ack. A WAN blip just grows the
   outbox. We never lose an outage record to a dropped socket (same durability discipline as the
   DB-derived escalations).
5. **Ship events + hourly rollups + heartbeat — NOT raw polls.** `poll_results` is local scratch
   (already true per "Scaling"); `poll_rollups` is the trend record; `Event`s are the real-time
   truth. Central pulls a raw window on demand only when an operator drills into an incident.
6. **Edge identity is `(tenant_id, node_id)`; central assigns global ids.** Edge autoincrement
   `devices.id` are per-SQLite and **cannot be merged.** Central keeps a mapping
   `(tenant_id, node_id, edge_local_id) → central_global_id`. Settle this *before* writing ingest.
7. **Deploy the edge as a frozen single binary** (PyInstaller, per platform/arch) — no Python,
   no venv, no PEP668 on the edge box. Windows = a service installer; Linux = `curl|sh` + systemd.
   The existing `deploy/install.{sh,ps1}` (venv + git-pull) **stays** for the on-prem single-box
   build; the frozen binary is the *fleet* path. Two deploy modes, both supported.
8. **Updates are pull-based over the existing mTLS channel; central is the version authority;
   rollouts are staged + health-gated + auto-rollback.** A small stable **supervisor** owns
   swapping the **agent** binary (the updater is not the thing being updated).

## Part A — edge shipper + outbox (additive, standalone-safe; ship FIRST)

- **`WISP_CENTRAL_URL` empty ⇒ today's behaviour, nothing runs.** This is the safety switch and
  the test anchor.
- **`outbox` table** (new migration, idempotent/forward-only): `id, kind, payload(JSON TEXT),
  created_at, attempts, sent_at`. The daemon, inside the *same* transaction that writes
  `poll_results`/applies events, also enqueues the shippable records (events; hourly, the rollup
  rows). One writer, no new lock contention.
- **Shipper**: a background thread (mirror `start_watchdog_thread`) that drains the outbox to
  central over mTLS, deletes on 2xx ack, exponential-backs-off on failure, and **caps** the outbox
  (oldest-rollup eviction past a high-water mark — never evict unsent `Event`s; an outage record
  is sacred). Isolated try/except — a shipper hiccup never touches the poll loop.
- **Heartbeat**: every cycle (or every N s) POST `{node_id, version, last_poll_ts, fleet_size,
  health}`. This doubles as the **liveness signal** (Part B watchdog) and the **update channel**
  (Part D — the reply carries the target version). 
- **Wire protocol**: a **versioned envelope** (`v`, `tenant_id`, `node_id`, `kind`, `body`). Ingest
  must accept old+new `v` during a rollout (version skew is normal in a fleet). Keep it JSON for v1
  *(your call on msgpack/protobuf later if volume demands)*.
- **Skeleton central ingest** to receive + persist into a central store (Part B). At this stage
  central is just a mirror — value (a fleet-wide read view) before any auth/alerting change.

## Part B — central ingest + multi-tenant store + fleet watchdog

- **Tenant/org + node entities.** `org` (the ISP), `node` (an edge, belongs to an org), every
  device/outage/rollup row carries `tenant_id` + `node_id`. Scope **every** central query by tenant
  — this is the bulk of the work and it's all central-side; the edge barely changes.
- **Serialized ingest writer.** Many edges → one central store. Start SQLite behind a single
  writer thread/queue (WAL won't save you from many concurrent writers). **Postgres is the
  expected central upgrade** when tenant count grows — design the data layer so central can swap
  the backend; *the edge stays SQLite+stdlib forever.* *(your call on when to make the cut.)*
- **Id mapping** per decision #6.
- **Cross-edge watchdog = `MonitorWatchdog` one level up.** Today the dashboard pages when
  `poll_results` goes stale; central pages (per-org) when a **node's heartbeat** goes stale — box
  dead *or* WAN cut. Reuse the restart-safe/conservative logic. Nearly free, high value.

## Part C — central multi-tenant dashboard + auth

- **The single shared PIN does not survive.** Need per-org accounts (and within an org, the role
  model you already have). `server/auth.py` is the seam; this is a real authn change.
- **Reuse the existing dashboard**, scoped by tenant, against the aggregated store. Edge-local
  dashboards can stay (LAN debugging) or be disabled per policy.
- Decide here whether **escalation/attendance/team** become org-wide central concepts or stay
  per-edge *(product call — recommend central, since "who's on duty" is an org fact)*.

## Part D — Deployment & updates (the operator's explicit ask)

### Edge artifact — frozen single binary
- **PyInstaller**, one artifact per platform/arch: `win-amd64`, `linux-amd64`, **`linux-arm64`**
  (Raspberry Pi / ARM mini-PCs — confirm in scope). Bundle `icmplib` + `pysnmp` (force-include the
  lazy imports). No venv, no system Python, no PEP668 fight on a box you don't control.
- **Config + identity live OUTSIDE the binary** in a stable dir that **survives updates**:
  `/etc/wisp/` (Linux) / `%ProgramData%\Wisp\` (Windows), `0600` — holds the mTLS client cert/key,
  `node_id`, `central_url`, and `WISP_*` overrides. An update swaps the binary, **never** the
  config/cert/DB.

### Windows — service installer
- An **Inno Setup `.exe`** (or MSI) that: drops the agent + supervisor, registers a service that
  is **auto-start + restart-on-failure**, running as **SYSTEM** (Windows has no unprivileged ICMP —
  icmplib forces raw sockets needing admin; this is already documented in `deploy/install.ps1`, so
  reuse that rationale). Scheduled-Task-as-SYSTEM (today's approach) is an acceptable fallback that
  needs no third-party service wrapper *(your call: native service vs Scheduled Task vs bundled
  NSSM — the Scheduled Task path is already proven here)*.
- **Code signing (Authenticode)** to avoid SmartScreen warnings on a fleet install — flag whether
  a signing cert exists; unsigned is a deployment-friction landmine.

### Linux — `curl | sh`
- `curl -fsSL https://central/install.sh | sh -s -- --token <ENROLL> --central https://...`:
  detect arch → download the matching binary → **verify sha256 (+ signature)** → drop a systemd
  unit (model on the existing `deploy/wisp-monitor.service`) → set `ping_group_range` sysctl →
  enroll with the token → `systemctl enable --now`.
- **Supply-chain honesty:** `curl|sh` is a real risk surface. Serve over HTTPS, publish + verify a
  sha256, and **sign the binary** (minisign/GPG) with the public key pinned in the script. Don't
  ship an unverified pipe-to-shell.

### Enrollment baked into install (ties to decision #3)
- One-shot: the install token is exchanged at first contact for the **mTLS client cert** bound to
  `(org, node)`. Central issues + can revoke; plan **cert rotation** (the agent renews over the
  channel before expiry). Until Part C's enrollment exists, Part A can run on a static
  per-node key/`WISP_*` env as a stopgap.

### Updates — pull-based, staged, health-gated (the part they care about most)
- **Two-part install: a stable `supervisor` + the `agent` it manages.** The OS service runs the
  *supervisor*; the supervisor launches/monitors the agent and owns
  **download → verify → atomic-swap → restart → rollback**. This solves "how does a binary update
  itself while running" — the updater isn't the thing being updated. The supervisor changes rarely;
  agent updates are the common path.
- **Pull model over the existing channel (no inbound access):** the **heartbeat reply** carries
  `{target_version, signed_url, sha256}`. If it differs from the running version, the supervisor
  downloads to a temp path, **verifies signature + checksum**, atomically renames into place,
  restarts the agent, and **gates on the agent's existing `preflight()` + first successful
  heartbeat** within N minutes. On failure → **roll back to last-known-good** (keep the previous
  binary) and report the failure up. This is exactly how Tailscale-class agents self-update behind
  NAT.
- **Central is the version authority, with control:** per-org / per-node version **pinning** and
  **staged rollout** — canary a few nodes first, watch their post-update heartbeats, and **halt the
  rollout automatically if updated nodes fail to come back healthy.** "Update every node" must
  never mean "brick every node at once." *(your call on the rollout policy knobs.)*
- **Version skew is normal**: central ingest accepts old + new envelope versions throughout a
  rollout (decision #5/Part A wire protocol).

### CI/CD & release — GitHub Actions builds the artifacts (the "factory")
The pipeline is the *other half* of the update story, not an alternative to it: **CI builds +
signs + packages + publishes; central dispatches (staged rollout); the supervisor installs.** They
compose — CI feeds central feeds the edges. Edges **never** pull GitHub "latest" directly (that
would bypass the staged/health-gated/rollback control in "Updates" above); central stays the
version authority and hands out the (GitHub-hosted) signed URL in the heartbeat reply.

- **Build matrix on native runners**, one job per platform/arch:
  | Runner | Artifact |
  |---|---|
  | `windows-latest` | signed `.exe` (PyInstaller) + Inno Setup installer |
  | `ubuntu-latest` | `linux-amd64` binary + `.deb` |
  | `ubuntu-24.04-arm` (GitHub hosted arm64 Linux runner) | `linux-arm64` binary + `.deb` (Pi) |
  Plus `sha256SUMS` + detached signatures + a **version manifest** (`version → {url, sha256}`),
  all attached to a **GitHub Release**. Use **nfpm** for the `.deb` (config-driven, no fpm/Ruby;
  drops the binary + systemd unit + a postinst that enables the service; gives `.rpm` for free).
- **Versioning = the git tag (semver), single source of truth.** CI stamps it into the binary at
  build (`_version.py` from `git describe`), so every artifact maps to exactly one commit. The edge
  **reports this version in its heartbeat**; central compares it to the target it's rolling out; the
  supervisor pulls only on a mismatch. One string ties commit → artifact → running version →
  rollout decision.
- **Two triggers, deliberately split** (the cardinal rule — never auto-ship every commit to a live
  fleet):
  - **push / PR** → build + run the test suite + produce an artifact (catches build breaks early,
    gives you something to smoke-test). Not eligible to ship.
  - **git tag `v*` (or manual `workflow_dispatch`)** → sign, package, publish a Release. Only
    tagged versions are eligible for central to promote.
- **Signing happens in CI** — Authenticode (Windows) + minisign/GPG (Linux) keys live in **Actions
  secrets**, so artifacts are signed the instant they're built and the edge verifies before any
  swap. This is also what makes the `curl|sh` install trustworthy.
- **Secondary native path:** publish the `.deb` to a hosted **apt repo** (central or GitHub Pages)
  so initial install / standalone boxes can use `apt install wisp-edge` + `apt upgrade`. Keep the
  **agent self-update (central-mediated) as PRIMARY** for the managed fleet; the apt path is for
  bootstrap + manual boxes — **don't run two competing auto-updaters against the same node.**

## Sequencing (each step independently shippable)

1. **Part A** — edge outbox + shipper + heartbeat + skeleton central ingest. Value (fleet read
   view) with **no** auth/alerting change; `WISP_CENTRAL_URL` empty keeps every existing
   deployment identical.
2. **Part D packaging** — frozen binary + Linux `curl|sh` + Windows installer, so Part A edges can
   actually be rolled out at sites. (Enrollment can be a stopgap static key here.)
3. **Part B** — multi-tenant central store + id mapping + cross-edge watchdog.
4. **Part C** — per-org auth + multi-tenant dashboard (+ decide org-wide escalation).
5. **Part D updates** — supervisor + pull-based self-update + staged/health-gated rollout +
   cert rotation. Transport scale (a queue/NATS) only if edge volume demands it.

## Invariants you must not break (plus the existing set in `CLAUDE.md`)

- **`WISP_CENTRAL_URL` unset ⇒ byte-identical standalone behaviour.** The distributed layer is
  strictly additive and off by default. This is the new back-compat anchor.
- **The edge stays SQLite + frozen-stdlib; only the *central* store may graduate to Postgres.**
- **Updating the agent binary never touches the DB, config, or mTLS cert** (separate dirs).
- **A shipper/heartbeat hiccup never sinks the poll cycle** — same try/except discipline as every
  other per-cycle task; the monitor is the hardest thing in the system to kill.
- **Outbox eviction never drops an unsent `Event`** (an outage record is the source of truth).
- **Tests inject doubles — no real network to central in the suite** (recording-shipper double,
  exactly like the recording notifier). Mirror the `tests/` layers.

## Open questions for the operator (surface before building Part B+)

- **Account model:** does central provision orgs (you onboard each ISP), or self-serve signup?
- **Where does central live** (your VPS/cloud), its domain, and the **CA** that issues edge mTLS
  certs (self-managed CA vs a public one)?
- **Code signing:** do you have (or want) an Authenticode cert for Windows + a signing key for the
  Linux binary? Affects install friction and the `curl|sh` trust story.
- **Arch list:** is Raspberry Pi / ARM64 in scope (it changes the build matrix)?
- **Update policy:** fully automatic per org, or operator-approved rollouts? Canary size?
- **Data residency / retention** at central across tenants (one big store vs per-tenant DBs).
