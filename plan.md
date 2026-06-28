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
