# Village WISP Monitor — Plan

A small, reliable tool to watch the **shared network infrastructure** of a rural WiFi
broadband operator (several villages, ~1,000 customers) and instantly tell the operator
and field technicians **what is down, where, and the likely cause** — over free push
channels (ntfy / Telegram). Built to run on one always-on box, no cloud, no per-message
costs.

---

## Decisions locked for v1

| Topic | Decision |
|---|---|
| **What we monitor** | Shared infrastructure only — towers, relays, backhaul links, core gear. Not the ~1k customer routers (yet). |
| **Primary goal** | Operator & technician awareness. No mass customer messaging in v1. |
| **Gear assumption** | Mixed / unknown — every device is just an **IP to ICMP-ping**. No SNMP/API yet. |
| **Alert channels** | **ntfy** and **Telegram** only (free, no approvals, instant on phones). |
| **Where it runs** | On-prem, single always-on machine (office PC / mini-PC / Pi). SQLite + WAL. |
| **Realism** | Probers & notifiers behind interfaces. Mock/simulated impls so the whole thing runs and is testable on a laptop today; real ICMP + real ntfy/Telegram are thin adapters. |

### Re-scoped away from the original Gemini prompt (and why)
- **WhatsApp / SMS / Twilio / IVR voice calls → dropped from v1.** Goal is operator
  awareness, not customer comms. The schema stays ready for it; we don't build it.
- **"Potential Fiber Cut" → "Link / Equipment fault".** This is a *wireless* network;
  the meaningful split is **power loss vs. wireless-link/equipment fault**, which is the
  single biggest cause of rural downtime.
- **160-char SMS budget / 12h SMS cap → deferred** (no SMS in v1). A lighter per-channel
  anti-spam cap still applies to staff alerts so phones don't get hammered.

---

## What "shared infrastructure" looks like (model)

```
            Internet
               │
        [ Core / Gateway ]              criticality 5
               │
        [ Main Tower A ]  ── backhaul ──  [ Relay Tower B ]   criticality 4
            │     │                            │
        [Relay] [Sector AP]               [Sector AP]         criticality 2-3
```

Each node is a row in `devices` with a `parent_device_id`, so when a parent dies we
mark its children **UNREACHABLE** instead of screaming about all of them (§4).

---

## Layers (v1 emphasis)

1. **Monitoring Core** — async ICMP poll of every active infra device every 60s, N echos
   each, persisted to `poll_results`.
2. **Pattern Recognition** — state machine + flap suppression, uplink canary, topology
   suppression, and **power-vs-link cause inference**. This is the heart of the tool.
3. **Operational Memory** — durable `outages`, `alert_log`, technician acknowledgements;
   survives restarts; source of truth for escalation timing.
4. **Operator/Tech Alerting** — ntfy + Telegram to the region's technician and to your
   father; escalates if no acknowledgement.
5. **(Deferred) BI + Customer comms** — analytics queries are light in v1; customer
   notification tables exist but are unused until a later phase.

---

## Database (SQLite + WAL)

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE devices (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,           -- 'Tower A - Main', human friendly
    ip_address    TEXT NOT NULL,
    device_type   TEXT,                    -- 'core'|'tower'|'relay'|'sector'|'backhaul'
    criticality   INTEGER DEFAULT 3,       -- 1..5; 5 = core gateway
    region        TEXT,                    -- village / area name
    is_active     INTEGER DEFAULT 1,
    parent_device_id     INTEGER REFERENCES devices(id),
    power_ref_ip         TEXT,             -- a node known to share this site's MAINS power
                                           --   (see §5 power-vs-link). NULL if none.
    technician_phone     TEXT,             -- region tech (used as ntfy/Telegram routing key)
    customer_count       INTEGER DEFAULT 0,-- customers behind this site; drives blast radius
    base_revenue_impact  REAL DEFAULT 0    -- est. ₹/hour while down (≈ customer_count × rate)
);

CREATE TABLE poll_results (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER REFERENCES devices(id),
    timestamp   TEXT NOT NULL,             -- ISO8601 UTC
    latency_ms  REAL,                      -- NULL on 100% loss
    packet_loss REAL NOT NULL,             -- 0..100
    state       TEXT NOT NULL              -- state AFTER this poll
);
CREATE INDEX idx_poll_device_ts ON poll_results(device_id, timestamp);

CREATE TABLE outages (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER REFERENCES devices(id),
    started_at      TEXT NOT NULL,
    resolved_at     TEXT,                  -- NULL = ongoing
    final_state     TEXT,                  -- 'DOWN' | 'UNREACHABLE'
    inferred_cause  TEXT,                  -- 'Likely Power Outage'|'Link/Equipment Fault'|NULL
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX idx_outage_device_start ON outages(device_id, started_at);

-- Every alert we send (audit + anti-spam + restart safety)
CREATE TABLE alert_log (
    id         INTEGER PRIMARY KEY,
    outage_id  INTEGER REFERENCES outages(id),
    device_id  INTEGER REFERENCES devices(id),
    channel    TEXT NOT NULL,             -- 'ntfy'|'telegram'|'mock'
    recipient  TEXT NOT NULL,             -- tech phone/topic/chat-id
    sent_at    TEXT NOT NULL,
    status     TEXT NOT NULL,             -- 'sent'|'failed'|'suppressed'
    payload    TEXT
);
CREATE INDEX idx_alert_recipient ON alert_log(recipient, device_id, sent_at);

-- DB-derived escalation timers so a restart never drops an escalation
CREATE TABLE escalations (
    id          INTEGER PRIMARY KEY,
    outage_id   INTEGER REFERENCES outages(id),
    kind        TEXT NOT NULL,            -- 'realert' | 'escalate_to_owner'
    due_at      TEXT NOT NULL,
    executed_at TEXT,                     -- NULL = pending
    UNIQUE(outage_id, kind)
);

-- Present but UNUSED in v1 — reserved so the customer-comms layer needs no migration later
CREATE TABLE customer_mappings (
    id             INTEGER PRIMARY KEY,
    customer_phone TEXT NOT NULL,
    device_id      INTEGER REFERENCES devices(id),
    region         TEXT NOT NULL
);
```

---

## State machine (precise)

Each poll sends `N=5` echos (configurable). `packet_loss` = % lost; `latency_ms` = avg of
replies (NULL if all lost).

**States:** `UP`, `DEGRADED`, `DOWN`, `UNREACHABLE`.

**Entry (evaluate top-down, first match wins):**
1. **DOWN** — `packet_loss == 100%` for **3 consecutive** polls (flap suppression: a
   single dropped poll never pages anyone).
2. **DEGRADED** — `latency_ms > 150` OR `5% ≤ packet_loss < 100%`, for **2 consecutive** polls.
3. **UP** — `latency_ms < 150` AND `packet_loss < 5%`.

**Recovery (hysteresis — the prompt omitted this):**
- `DOWN`/`UNREACHABLE` → `UP`: **2 consecutive** healthy polls → close the outage.
- `DEGRADED` → `UP`: **2 consecutive** healthy polls.
- Any counter resets the moment its condition breaks.

**Runtime:** an in-memory `DeviceFSM` per device holds the current state + consecutive
counters. On startup it **rehydrates** the current state from the latest `poll_results`
row, so a restart doesn't reset everyone to UP and re-page.

> Note the deliberate trade-off: 60s × 3 polls ≈ **3 min to declare DOWN**. That's the
> price of not crying wolf on brief wireless blips. Tunable per criticality later.

### Decision flow before committing a DOWN
```
candidate = evaluate(device)
if candidate == DOWN:
    if canary_failed():                 # §3  our own uplink is the problem
        freeze_transitions()
        system_alert('UPLINK_DOWN')     # ONE alert, not one per tower
        return
    if parent_is_down(device):          # §4  topology
        candidate = UNREACHABLE         # child of a dead parent: no separate alarm
    else:
        cause = infer_power_vs_link(device)   # §5
commit(device, candidate)               # opens/updates outage, schedules alerts
```

### §3 Uplink canary
Before *anyone* goes DOWN, ping the canary (default `1.1.1.1`, configurable to the actual
upstream gateway/BNG). If the canary is unreachable, our site's own internet is down — so
**freeze all transitions and send a single `UPLINK_DOWN` alert** instead of a storm. Result
cached for the cycle so we ping it once, not once per device.

### §4 Topology suppression
If `parent_device_id` is currently DOWN/UNREACHABLE, the child becomes **UNREACHABLE** and
generates no separate page. One "Tower A down" alert, not forty.

### §5 Power vs link (the most valuable signal for rural)
On a genuine DOWN, decide the likely cause with two cheap heuristics:
- **Co-location heuristic (primary):** if *all* monitored devices that share this site go
  DOWN within the same cycle → `Likely Power Outage`. If some siblings at the site are
  still UP → `Link/Equipment Fault`.
- **Power-ref ping (if `power_ref_ip` set):** ping a node known to be on the same mains.
  Unreachable → reinforces `Likely Power Outage`; alive → `Link/Equipment Fault`.

This tells a tech whether to grab a battery/genset or climb the tower for the radio.

---

## Alerting (ntfy + Telegram)

On a confirmed, non-suppressed outage:

| When | Action |
|---|---|
| **T+0** | Push to the region's technician (`technician_phone` → ntfy topic / Telegram chat) with device name, region, state, and inferred cause. Priority scales with `criticality`. |
| **T+10 min** | If still DOWN and `acknowledged_at IS NULL` → **re-alert** (higher priority). |
| **T+20 min** | If still unacknowledged → **escalate to the owner** (your father's Telegram). |
| **On recovery** | Send a "✅ restored, down for Xm" follow-up so the loop is closed. |

- Timers are **DB-derived** (`escalations.due_at` + a sweeper job), so a restart replays
  pending escalations instead of silently dropping them.
- **Anti-spam:** a recipient won't get the same device's alert more than once per
  configurable window (default 10 min) except for the explicit escalation steps — enforced
  in `notifiers.py`, logged in `alert_log`.
- **Acknowledge** for v1 = a Telegram bot button / `/ack <outage_id>` command (or ntfy
  action), which stamps `outages.acknowledged_at`. This is what stops the escalation.

---

## What we present (the v1 information set)

Two audiences, opposite needs: your **father (owner)** wants impact, money, and trends;
a **field tech** wants location, likely cause, and what to do *now*. v1 delivers four
surfaces. (Maps, per-tech scorecards, predictive flap detection, and photo-logs are
deliberately deferred — they layer on later without rework.)

### 4.1 The rich alert (most-read thing in the system)
Pushed to the region's tech on a confirmed, non-suppressed outage. Every field earns its
place; the **cause** and **blast radius** are what make it actionable.

```
🔴 DOWN — Rampur Main Tower (Rampur)
⚡ Likely cause: POWER (whole site dark)
👥 ~140 customers affected · Rampur, Sohna
💰 ≈ ₹350/hr · 🔺 Criticality 4
⏱ Down 3m · ⚠️ 3rd outage this week
👷 Routed to: Suresh
[ Acknowledge ]  [ On my way ]  [ Resolved ]
```
Fields: plain name + village, state (🔴🟠🟡✅), power-vs-link cause, blast radius
(customers + villages), revenue/hr, criticality, time-down, repeat-offender flag, who it's
routed to, and ack buttons. On recovery: `✅ Restored — Rampur Main Tower, down 47m (power)`.

### 4.2 Current-status board (text, on demand)
A `/status` Telegram command (and an `analytics.py` print) answering "how is it *right now*":
- Headline: `🟢 38 / 41 sites up`.
- **Active outages**, sorted by impact (customers × criticality), each with cause + duration.
- **Degraded watch-list** — flapping / high-latency sites *before* they fail (early warning).
- Unacknowledged count, and the uplink/canary status line.

### 4.3 Daily digest to your father (8:00 AM Telegram)
Where ping data becomes business intelligence:
```
📊 Network — Yesterday (18 Jun)
Uptime: 99.2% overall
Outages: 6  (⚡ 4 power · 🔧 2 equipment)
Total downtime: 3h 40m
Worst site: Sohna Relay — 2h 10m (power)
Customers impacted: ~310
Est. revenue lost: ≈ ₹1,850
Repeat offenders: Sohna Relay (4×), Bhondsi AP (3×)
Slowest tech response: 22 min (Rampur)
```
The **power-vs-equipment split** is the headline rural stat — it tells your father whether
to invest in batteries/inverters or replace radios, and gives hard numbers for an
electricity-board complaint. Weekly/monthly roll-ups (per-village availability, trend line,
reliability ranking) reuse the same queries.

### 4.4 Monitor heartbeat (dead-man's switch)
If the monitoring box loses power or internet, silence looks identical to "all good." So a
tiny morning `✅ Monitor healthy — 41 sites watched, uplink OK` makes silence trustworthy,
and the canary status is always surfaced so "our office internet is down" never masquerades
as "the towers are down."

### Data this depends on (inputs needed)
- `devices.customer_count` — even rough, per site. Without it: no blast radius, no revenue.
- `base_revenue_impact` (₹/hour) — turns downtime into money. Derivable from
  `customer_count × ₹/customer/hour` if a flat rate is easier than per-site numbers.

### What ping-only can't show (future SNMP/controller layer)
Throughput/bandwidth, signal strength (RSSI/SNR), per-customer usage, CPU/temperature.
When the gear is known, **signal strength + throughput** are the two highest-value
additions — they enable real failure *prediction*, not just outage *detection*.

---

## Module layout (single project)

```
ping_tool/
├── plan.md
├── config.py            # poll interval, N, thresholds, canary IP, channel + creds (env)
├── db.py                # WAL+FK connection factory, migration runner, write-retry helper
├── migrations/0001_init.sql
├── probers.py           # Prober interface; SimulatedProber (dev) + IcmpProber (real, icmplib)
├── state_machine.py     # DeviceFSM, evaluate/commit, canary/topology/cause hooks
├── polling_daemon.py    # APScheduler 60s loop + escalation sweeper; orchestrates everything
├── notifiers.py         # Notifier interface; NtfyNotifier, TelegramNotifier, MockNotifier
│                        #   + anti-spam + escalation logic
├── analytics.py         # light BI: uptime %, MTTR, current-outages board, top offenders
├── seed.py              # demo towers/relays + scripted SimulatedProber outage scenarios
└── tests/               # state-machine truth table, topology suppression, anti-spam, restart
```

### Interfaces (so mock ↔ real swaps cleanly, chosen by env)
```python
class Prober(Protocol):
    async def ping(self, ip: str, count: int) -> PingResult: ...   # latency_ms, packet_loss

class Notifier(Protocol):
    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult: ...
```
- `SimulatedProber` plays scripted scenarios (Tower A loses power → all children DOWN →
  `Likely Power Outage`; a single relay link flaps → DEGRADED then recovers; canary fails →
  `UPLINK_DOWN`) so every branch is demonstrable with zero hardware.
- `PROBER=simulated|icmp` and `NOTIFY=mock|ntfy|telegram` select implementations.
- `IcmpProber` uses `icmplib` (needs root/raw sockets or `cap_net_raw` — documented).

---

## Reliability
- **DB locks:** WAL + `busy_timeout`; all writes through a retry-with-backoff helper.
- **Socket errors in ICMP:** caught in the prober, reported as `packet_loss=100`, never
  crash the loop. Each device polls as its own task — one bad device can't stall the cycle.
- **Notifier failures:** httpx timeout + retry; final failure logged `status='failed'`,
  never blocks polling or corrupts state.
- **Restart safety:** FSM rehydrates from last poll; pending escalations replay from DB;
  escalations are idempotent (`UNIQUE(outage_id, kind)`).

---

## Build phases (each phase runs & is testable)
1. **✅ DONE — Scaffold + DB** — `db.py`, migration, `config.py`, `seed.py`. WAL + indexes verified.
2. **✅ DONE — Prober + poll loop** — `SimulatedProber`, asyncio loop → `poll_results`
   (asyncio used instead of APScheduler; system Python is PEP 668-locked).
3. **✅ DONE — State machine** — precedence + hysteresis; 9 truth-table tests pass.
4. **✅ DONE — Layer-2 brains** — canary freeze, topology suppression, power-vs-link;
   open/close outages; FSM rehydration on restart verified.
5. **✅ DONE — Alerting** — Mock/Ntfy/Telegram notifiers; AlertDispatcher with
   routing, anti-spam dedupe, UNREACHABLE suppression, DB-derived T+10/T+20
   escalation ladder + sweeper; `ack.py` CLI. 6 dispatcher tests pass (15 total).
6. **✅ DONE — Light BI** — `analytics.py`: live status board, daily digest (uptime %,
   power-vs-equipment split, revenue lost, worst site, repeat offenders), per-device
   uptime. 5 math tests pass (20 total).
7. **Go-live adapters** — wire `IcmpProber` against real device IPs on the on-prem box;
   real ntfy topic + Telegram bot token from env. Tune thresholds against real blip behavior.

Phases 1–6 are **DONE** and run entirely on your laptop (20 tests passing). Phase 7 is the
only step that needs the real network, credentials, and the always-on box.

---

## Interface (PROPOSAL — not built yet)

Today there are two "interfaces": the CLI reports (`analytics.py`) and the push alerts
(`notifiers.py`). No graphical UI exists. The audience is phones, in villages, on patchy
data — so the recommended UI is **not** a heavy web app first.

### A. Telegram bot as the interface (recommended first) — `telegram_bot.py` (~1 file)
Make the alerts the techs already receive *interactive*. Zero hosting, no app install,
works on any cheap phone, and reuses code that already exists.
- **Inline buttons under every alert:** `[✓ Acknowledge] [On my way] [Resolved]` →
  calls the existing `acknowledge_outage()` and stops the escalation ladder.
- **Commands:** `/status` → `analytics.status_board`; `/digest` → `compute_digest`;
  `/outages`; `/ack <id>`.
- **Role-aware:** techs see their region; owner chat gets escalations + the 8 AM digest.
- **Effort:** small — a long-poll `getUpdates` loop over `analytics.py` (content) +
  `acknowledge_outage` (action) + `TelegramNotifier` (send). Needs `httpx` + a bot token
  (Phase 7), but buildable now against a free test bot.

### B. Lightweight local web dashboard (later, for the office) — read-only
A screen for the office wall: headline (38/41 up), tower list/map color-coded, active
outages, uptime trends.
- **Stack:** stdlib `http.server` serving one HTML page that calls `/api/status` (JSON
  from `analytics.py`). No framework, no build step. Maps/charts later.
- **Effort:** medium. Worth it once there's a fixed office machine; less so for field use.

### Recommendation
Telegram bot first (covers father + techs on the devices they carry, no hosting), web
dashboard later as an office upgrade. Both need real credentials/host → align with Phase 7.

---

## Open questions (won't block phases 1–6)
- **Device inventory:** rough count of towers/relays/backhaul nodes, and their parent→child
  topology? Even a hand-sketched tree lets me build a realistic `seed.py`.
- **Static IPs / management reachability:** are the towers/relays reachable by stable IPs
  from where the monitor will sit (same LAN/management VLAN, or over the radios themselves)?
  This affects whether a "down" reading is the device or just the path to it.
- **Canary target:** is `1.1.1.1` fine, or should it be your actual upstream provider's
  gateway (better signal of *your* uplink specifically)?
- **Who acknowledges + who's "owner":** Telegram handles/chat IDs for the tech(s) and your
  father, so escalation routing is real.
- **Power-ref nodes:** at sites with backup, is there a device we can ping that's on mains
  only (no UPS/genset)? That makes the power-vs-link call much sharper.
- **Later:** when you do want customer notifications, which is realistic in your area —
  SMS (DLT-registered) or WhatsApp — and do customers expect per-outage messages or just
  a status they can check?
```