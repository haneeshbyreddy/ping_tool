# Sub-5-minute port and ONU freshness — mechanism, decision rule, and cost

Workstream D. Design and costing only; no production code is changed by this document.
Written 2026-08-18 against the working tree at `9947a68` plus three in-flight workstreams
(edge walk instrumentation, report-body gzip, and whatever else is mid-edit in
`apps/daemon/main.py`, `src/wisp/central/api/edge.py`, `store.py`, `store_snmp.py`,
`server.py`, `runtime/central_client.py`, `config.py`).

---

## 0. NO INTERVAL VALUES ARE CHOSEN HERE, DELIBERATELY

**Every interval in this document is written as a symbol, never a number.** The clock is
not the limit — walk duration is — and *no walk duration has ever been measured in
production.* The instrumentation that will measure it
(`apps/daemon/main.py:104` `_walk_elapsed`, `device_snmp_status.elapsed_s` at
`src/wisp/central/store.py:1087`, cleaned at `src/wisp/central/store_snmp.py:1267-1282`,
rendered at `web/src/components/snmp-diagnosis.tsx:262`) is written but has **not run
against the fleet**: the live database at `data/central.db` still has no `elapsed_s`
column, because central has not been restarted since the migration was added
(`store.py:1794-1795`).

So this document defines the **mechanism**, the **decision rule**, and **what number
would justify what setting**. When the data lands, picking the intervals is arithmetic.
Section 9 is the query to run and the thresholds to compare it against.

Anyone who fills in a number before section 9 has been run is guessing, and this
codebase has an expensive history of confident numbers nobody measured
(`CLAUDE.md`: the strict SNMP window that got zero responses for 26 hours; the
`distance_m` unit that made every printed cut bracket 39% short).

---

## 1. What actually limits freshness today

### 1.1 The four serial terms

End-to-end age of a port or ONU fact on the dashboard is the sum of four things, not one:

```
age  =  launch_wait   +   walk_duration   +   harvest_wait   +   report
        (≤ clock)         (unmeasured)       (≤ probe interval)
```

**`launch_wait` — the subsystem clock.** `apps/daemon/main.py:594-610` fires each
subsystem when `started >= next_X`, then sets `next_X = started + cfg.X_interval_s`.
Three clocks, all defaulting to 300 s (`src/wisp/config.py:99-101`): `snmp_interval_s`
(health, and the master gate — `≤0` disables all three, `main.py:553-568`),
`port_interval_s`, `gpon_interval_s`.

**`walk_duration` — the unmeasured term.** Bounded by three caps
(`config.py:102-115`): health 20 s, ports 60 s, gpon 75 s. The cap is a *ceiling*, not a
measurement — a walk that takes 58 s files `ok` and looks identical to one that took 2 s.
That is precisely the blind spot `elapsed_s` closes.

**`harvest_wait` — and this term is routinely forgotten.** The launch block
(`main.py:592-610`) runs immediately before the harvest block (`main.py:613-636`) with no
`await` between them, so `snmp_task.done()` is always `False` for a task created this
cycle. **A walk launched in cycle N is harvested no earlier than cycle N+1.** The cycle
period is the probe interval, clamped to 10–120 s server-side and client-side
(`main.py:533-542`, `float(min(120, max(10, central_s)))`); every live org has
`orgs.poll_interval_s` NULL, so all six run at `cfg.poll_interval_s` = 60 s.

**Consequence, and it is a hard floor:** a fast subsystem clock is *quantised to the
probe interval*. Setting a 30 s ONU clock on a 60 s probe loop yields a 60 s effective
clock. Sub-60-second ONU freshness is not reachable by changing `gpon_interval_s` alone —
it requires either dropping the probe interval (which costs ICMP fan-out and is bounded
below at 10 s) or giving the fast walk its own harvest-and-ship path. **This is an open
question for the operator (§12, Q1).**

### 1.2 The real denominator: one `Semaphore(4)` across all three subsystems

`apps/daemon/main.py:87-97`:

```python
class _SnmpAirtime:
    def __init__(self, limit): self._sem = asyncio.Semaphore(max(1, limit)); self._locks = {}
    @contextlib.asynccontextmanager
    async def slot(self, ip):
        async with self._locks.setdefault(ip, asyncio.Lock()):   # device lock FIRST
            async with self._sem:                                 # then the fleet slot
                yield
```

CLAUDE.md's description matches the code exactly: fleet-wide `Semaphore(snmp_max_inflight)`
(default 4, `config.py:118-119`), per-device lock acquired *before* the semaphore so
waiting on a busy box cannot pin a fleet slot. Pinned by `SharedAirtimeGateTest`
(`tests/integration/test_daemon_central_brain.py:368`).

**One instance is shared by ports, optics, health and diagnostic walks**
(`main.py:553`, passed at `:598`, `:604`, `:609`, `:567`). Each `_gather_*` launches
*every* eligible device at once (`main.py:199`, `:247`, `:294`) and lets the semaphore
serialise them. So:

```
sweep wall-clock (per node)  ≈  Σ(every walk duration, all subsystems) / snmp_max_inflight
```

That is the number that has to fit inside the clock, and it is **not** what `elapsed_s`
measures — `elapsed_s` is deliberately per-walk and starts *after* the gates
(`main.py:99-105`, and correctly so: "queueing behind a busy box is contention, not walk
cost"). §9 gives the query that converts per-walk seconds into sweep wall-clock.

### 1.3 Measured fleet shape (read-only, from `data/central.db`, 2026-08-18)

These are counts, not durations. Durations still do not exist.

| Fact | Value |
|---|---|
| `switch_ports` rows | **6,925** across **57** devices |
| of which non-ONU rows (`if_name NOT LIKE '%ONU%'`) | **1,749 (25%)** |
| `switch_ports` with `monitored=1` | **177** |
| with `feeds_device_id` or `uplink_device_id` | **12** |
| with a bandwidth floor or ceiling | **16** |
| `onu_optics` rows | **5,638** across **33** OLTs |
| `onu_places` rows / witnesses / located | **608 / 2 / 594** |
| Largest node by SNMP devices | `rapidnetworks/Edge_1` — **19** SNMP devices, 8 OLTs |
| Next | `byreddy/EDGE_SAGAR` 14 (5 OLTs), `MS-Telecom/edge1` 10 (8 OLTs) |
| GPON profile spread (active OLTs) | `dbc` **29**, `(none)` 5, `syrotech_gpon` 2, `cdata_54824` 1 |

**The "~25 real ports" figure in CLAUDE.md is confirmed as measured, not folklore.**
Every C-Data EPON OLT lands on 25–26 non-ONU rows: HLY-OLT-2 25/340, HILL-OLT-1 25/264,
HLY-OLT-1 25/201, HILL-OLT-2 25/174, Epon_8 26/274, NDN-OLT 26/234, syro8_mainoffice_up
25/170, uni4_office2 25/151. Sixteen `GE0/x` + eight `EPON0/x` + one stray is the shape.

**But the name rule does not generalise, and this kills the obvious implementation.**
Three live devices have *zero* rows matching `%ONU%`: `MAIN_OLT5` (258/258 "real"),
`TECHNEXT_OFFICE` (119/119), and `MAIN_OLT2` reports 744 rows of which the pseudo-rows
are named `EPON0ONU12` but 32 carry an **empty** `if_name`. A row selector that keys on
the interface name would select the entire table on those boxes, save nothing, and log
success. See §2.2.

Worked example for the biggest node, `Edge_1` (19 SNMP devices, 8 OLTs), all clocks at
300 s: 19 port walks + 8 optics walks + 19 health walks = **46 walks per 300 s window**,
serialised 4-wide = **11.5 slot-generations**. Fitting inside 300 s needs a mean walk of
≤ 26 s. Fitting inside 120 s needs ≤ 10.4 s. Fitting inside 60 s needs ≤ 5.2 s. Those
three numbers are the go/no-go thresholds, and today nobody knows which side of them the
fleet is on.

---

## 2. Option A — split the port walk by ROW

### 2.1 What already exists (verify before extending)

The column split is real and matches CLAUDE.md:

- `src/wisp/ingress/snmp.py:188-193` — `_FULL_ORDER` (10 columns, status first), `_HOT_ORDER`
  (`ifAdminStatus`, `ifOperStatus`, `ifHCInOctets`, `ifHCOutOctets`), `_SCOPE_COLUMNS`.
- `apps/daemon/main.py:130-161` — `_PortScopePlanner`: full walk on first sight, on
  `port_identity_interval_s` (3600 s, `config.py:116-117`), and on **any never-seen
  `if_index`** (`:160-161` — a hot result containing an unknown index sets `_refresh`).
  Pinned by `PortScopePlannerTest` (`tests/integration/test_daemon_central_brain.py:328`)
  and `ScopedWalkTest` (`tests/unit/test_snmp.py:372`).
- The wire is **sparse and preserve-on-absence**: `main.py:116-128` deletes absent keys
  from the row; `src/wisp/central/ports.py:31-49` `_to_port_status.held()` falls back to
  the stored value for an absent key and honours a present-`None`. Pinned by
  `PartialWalkTest` (`tests/integration/test_central_ports.py:387`).

Everything Option A needs on the wire is therefore **already built**. The new work is
entirely in choosing a row set and in the honesty consequences of a partial row set.

### 2.2 Choosing the row set — three candidate rules

The selector must survive ports appearing and disappearing, must never drop an ONU
pseudo-row from the *record*, and must not be defeated by firmware that names things
differently.

| Rule | How it fails |
|---|---|
| **By name** (`if_name NOT LIKE '%ONU%'`) | Measured: selects 258/258 on MAIN_OLT5 and 119/119 on TECHNEXT_OFFICE. Saves nothing on ~4 of 57 devices and logs `ok`. **Reject.** |
| **By `if_index` range / low indices** | Vendor-defined and unstable across firmware. No evidence in the tree that low indices are the uplinks. **Reject.** |
| **By central's own operator columns** — the set that `history.port_eligible` already uses | Small (177 monitored + 12 linked + 16 bw = ≤ 205 rows fleet-wide), stable, operator-owned, and *already* the definition of "a port anyone cares about". **Recommend.** |

The third rule has a decisive property: **it is the same predicate the historian and every
alarm already use.** `src/wisp/central/history.py:183-193`:

```python
def port_eligible(prior) -> bool:
    return bool(prior["monitored"] or prior["feeds_device_id"] is not None
                or prior["uplink_device_id"] is not None
                or prior["bw_threshold_mbps"] is not None
                or prior["bw_max_mbps"] is not None)
```

A port that is not eligible writes no history sample, opens no outage, and pages nobody
(`ports.py:187-207` — `down`, `bw_eligible` and `high_eligible` all require
`monitored`). **Walking it fast buys literally nothing.** Conversely a port that *is*
eligible is exactly what "the ~25 real ports that actually page" means, and on the C-Data
fleet the monitored set is exactly `GE0/x` uplink + `EPON0/x` PON aggregates (measured:
HILL-OLT-1's nine monitored names are `GE0/5, EPON0/1, EPON0/3…EPON0/8`).

### 2.3 Where the selection has to live — and it is *not* the edge

**The operator columns live on central and the edge has never seen them.**
`switch_ports.monitored` / `feeds_device_id` / `uplink_device_id` /
`bw_threshold_mbps` / `bw_max_mbps` are central-only (`store.py:294-338`, and the walk
upsert deliberately omits them — `store_snmp.py:92-131`). `/edge/devices`
(`src/wisp/central/api/edge.py:22-54`) ships devices, canary, two profile lists and a poll
interval. There is no port channel.

Two shapes, and the choice is a real trade:

**(a) Central declares the fast set, per device, on `/edge/devices`.**
A new key alongside `snmp_profiles`/`gpon_profiles`, e.g.
`"fast_if_indexes": {device_id: [if_index, …]}`, derived from `port_eligible`.
*Pros:* one definition of "a port that matters", already maintained by the operator, and
it shrinks the fast walk to a handful of GETs rather than a bounded table walk.
*Cons:* a new key on the topology contract; a rollout-ordering rule (central first — the
same DEPLOY ORDER rule the column split already carries); and a cold-start hole — a device
central has never walked has no eligible ports, so the fast set is empty until the first
full walk lands. That hole is benign (nothing pages on those ports either) but must be
stated, not discovered.

**(b) The edge infers the set from what it walked last.**
No contract change; but the edge cannot see `monitored`, so it would have to re-derive
"real port" from the interface name — which §2.2 measured as broken. **Reject.**

**Recommend (a).** It makes this "Stage 2" in CLAUDE.md's sense — *targeted GETs for the
~25 real ports* — arriving as a cadence split rather than as a replacement walk, which is
what the pre-approval covers.

### 2.4 Keeping it correct as ports appear and disappear

The planner already solves the appear case for scopes; the row split needs the same
discipline plus two more rules.

1. **A never-seen `if_index` forces a full-scope, full-row walk.** Reuse
   `_PortScopePlanner._refresh` (`main.py:160-161`) verbatim — a fast-row result carrying
   an index outside the known set schedules a full pass.
2. **The fast row set is advisory; the full walk is authoritative.** The full walk runs
   the whole table on `port_interval_s`, so a port promoted to `monitored` between full
   walks is picked up within one full period at worst. It is not "missing", it is
   "arriving on the normal clock".
3. **A row not in the fast set must not be aged out.** `ports.sync_device`
   (`ports.py:169-302`) only touches rows present in `raw_ports`, and nothing deletes
   `switch_ports` rows. So ONU pseudo-rows simply keep their last full-walk values —
   which is exactly the current behaviour for a column the walk dropped.

### 2.5 `onu_if_token` survives — verified

The per-subscriber rate join is `src/wisp/central/onuroster.py:63-71`:
`onu_if_token(pon_port, onu_id)` → `"EPON01ONU7"`, matched against the *first token* of
`switch_ports.if_name` in `store_snmp.py:840-856` (`onu_interfaces`), consumed at
`api/devices.py:928-933` (the map's `places` payload) and `:1028-1034` (the subscriber
panel's `rate`). **Nothing in Option A deletes a row or changes `if_name`**; the ONU
pseudo-rows keep being written by the full walk on `port_interval_s`, unchanged. The
token join is untouched.

What *does* change is the **age** of the per-subscriber rate: it stays at
`port_interval_s`, which is what it is today. If the fast clock is introduced *and*
`port_interval_s` is left at 300 s, per-subscriber rates are exactly as fresh as now.
That is the correct outcome — a rate chip is not an alarm.

### 2.6 What Option A breaks, and the honesty fix

**`ports_updated_at` is a `MAX`, and the SPA reads it as "the ports panel is fresh".**
`src/wisp/central/store_devices.py:118-120`:

```sql
(SELECT MAX(p.updated_at) FROM switch_ports p WHERE …) AS ports_updated_at
```

consumed by `web/src/components/device-detail.tsx:138` (`portsStale = !isFresh(lastWalk)`),
`web/src/routes/topology-page.tsx:906`, `web/src/map/devhover.tsx:84`,
`web/src/map/sitehover.tsx:81` — all against `SNMP_FRESH_AFTER_S = 900`
(`web/src/lib/format.ts:87-91`).

With a fast row set, `MAX` is refreshed by 25 rows while 700 rows sit at the last full
walk. **The panel would read "fresh" over a table that is mostly an hour old.** That is
the "nothing is wrong vs nothing is measured" rule from CLAUDE.md, violated by
construction.

The fix is cheap and must ship *with* the split, not after it: alongside `ports_updated_at`
(the MAX), expose **`ports_full_at` = `MIN(p.updated_at)`** for the device, and let the
panel state the two ages separately — "status 40 s · identity 12 min". The per-row
consumers are already honest, because they read the row's own stamp: `refHasRate`
(`web/src/map/refonu.ts:81`) gates on `p.port_updated_at`, which comes from
`onu_interfaces`' per-row `updated_at` (`api/devices.py:955`), and the subscriber panel
does the same (`api/devices.py:1034`, checked at
`web/src/components/subscriber-detail.tsx:635`). Those need no change.

### 2.7 Option A cost

Assume `F` = fast clock, `P` = `port_interval_s` (full-row), `R` = eligible rows on the
device (measured max 10, fleet total ~205), `T` = total rows (measured max 744).

- **SNMP requests/device/hour.** The hot walk is a per-column bounded walk at
  max-repetitions 25 (`snmp.py:181`, level 1) or a combined `bulk_cmd` at 8
  (`snmp.py:174`, level 0), so cost ≈ `columns × ceil(rows / reps)`. A *targeted GET* set
  for 25 rows × 4 hot columns = 100 varbinds, which is 1–2 requests at any sane
  max-varbind size, versus `4 × ceil(744/25) = 120` requests for a hot table walk.
  **This is where the win is: 2 requests instead of 120, per fast tick.** At `F` = 60 s
  that is 120 requests/hour against today's 4 × ceil(744/25) × 12 = 1,440.
  *Net: Option A can go 5× faster and still cut request count by ~90% on the big OLTs.*
- **Rows on the wire/hour.** `3600/F × R` instead of `3600/P × T`. At `F`=60, `R`=10,
  `P`=300, `T`=744: 600 rows/hour vs 8,928 — a **93% reduction**, before gzip.
- **Central ingest writes/hour.** `ports.sync_device` does one `executemany` upsert per
  device per report (`store_snmp.py:133-140`) plus **two SQL statements per eligible port**
  in `record_port_sweeps` (`store_history.py:205-236`, a per-row loop, not `executemany`).
  Historian volume scales with `R`, not `T`, so 5× the cadence is 5× the historian writes:
  fleet-wide 177 eligible ports × 60/hr = 10,620 `hist_port_sweep` rows/hour vs 2,124
  today. Over `hist_raw_hours` = 48 (`config.py:156-157`) that is ~510 k rows instead of
  ~102 k. Bounded and pruned, but it is the largest single write increase in this batch.
- **Risk: LOW.** No new parse path, no new decode vocabulary, no new failure mode on the
  device. The two real risks are the freshness-display lie (§2.6, fixable) and the
  cold-start empty set (§2.3, benign).

---

## 3. Option B — split the ONU roster

### 3.1 Exactly which columns the mass-drop verdict depends on — proved

`ponfault.evaluate_org` → `evaluate_olt` reads its rows from
`store_snmp.org_onu_rows` (`store_snmp.py:755-771`), which returns
`device_id, onu_key, pon_port, onu_id, name, serial, state, distance_m, last_online_at,
updated_at, rx_dbm, severity, label, radius_*, device_name`.

Every field the verdict actually touches:

| Field | Where | What it decides | Needed on **every fast walk**? |
|---|---|---|---|
| `state` | `ponfault.py:163` (`DARK_STATES`), `:168` (gasp count), `:141`,`:145`,`:182` (`"online"`) | The cohort, power-vs-fiber, survivors | **YES** — this is the whole point |
| `last_online_at` | `ponfault.py:164-165` (30-min horizon), `:187` (`since`) | Whether a dark ONU is *recently* dark | **Derived by central from `state`**, not walked — `store_snmp.py:1142-1143`: `last_online_at = CASE WHEN excluded.state='online' THEN excluded.updated_at ELSE onu_optics.last_online_at END`. A state-only walk feeds it correctly. |
| `pon_port` | `ponfault.py:158` | The grouping key — a fault is *per PON* | **YES, but see §3.2** — on 30 of 32 profiled live OLTs it is a walked *column*, not the OID index |
| `onu_key` | `ponfault.py:138-140` (witness cohort membership), and the upsert PK | Row identity | **YES** — and on `dbc`/`cdata_54824` it is built from two columns (`gpon.py:367`) |
| `updated_at` | `ponfault.py:216-218` (`stale_s` = 900) | Whether the OLT is fresh enough to judge at all | Written by the ingest, free |
| `device_id`, `device_name` | `ponfault.py:188-191` | Labelling only | Free (joined) |
| `serial` | `ponfault.py:135` via `_norm_mac` | **Witness matching only** | **NO** for ponfault — but see §3.3, it is load-bearing elsewhere |
| `distance_m` | `ponfault.py:123-124` (`_reaches_past`), `:178-185` (cut bracket), `:113-117` (`_bind_suspect`) | The ranging bracket and the suspect passive | **NO** — the last known ranging distance of a now-dark ONU is exactly the number the bracket wants; preserving it is *more* correct than re-reading it, since a dark ONU has no live ranging |
| `rx_dbm` | **not read by `ponfault` at all** | — | NO for ponfault; **YES for severity, see §3.4** |
| `name` | not read by `ponfault` | — | NO |

**So the honest answer to "which columns does the mass-drop verdict genuinely depend
on": `state`, plus whatever the profile needs to construct `pon_port` and `onu_key`.**
`serial` and `distance_m` are needed by the *witness* and *bracket* refinements, and both
can be preserved from the stored row rather than re-walked — with the staleness caveat in
§3.3.

**What a state-only walk could NOT feed:** live Rx (and therefore optical severity, the
OLT badge, `OPTICAL_CRIT`/`OPTICAL_RECOVERED`, `RxScale`, the historian's Rx percentiles),
a newly-registered ONU's serial/name, a *changed* ranging distance, and the web-optics
merge (§3.4). None of those are inputs to the mass-drop verdict; all of them are outputs
somebody looks at.

### 3.2 The saving is much smaller than "1 column instead of 8" — measured

The brief's premise ("1 column instead of 8") is true only for **index-keyed** profiles.
`PysnmpGponPoller.walk` (`gpon.py:391-429`) walks whichever of twelve profile OIDs are
non-empty (`:401-405`), one `bulk_walk_cmd` per column at max-repetitions 25 (`:415-419`).
The live profiles:

| Profile | Live OLTs | Columns walked today | Minimum for a state-only walk | Saving |
|---|---|---|---|---|
| **`dbc` (built-in, `gpon.py:96-109`)** | **29** | 6: `ident_key`, `ident_pon`, `ident_onu`, `ident_state`, `ident_distance`, `ident_name` | **3**: `ident_pon`, `ident_onu`, `ident_state` — because `onu_key = f"{pon}.{onu_id}"` (`gpon.py:367`) and `pon_port = format_pon_label(pon)` (`:364`) are both *column*-derived | **50%** |
| `cdata_54824` (data profile, org NULL) | 1 | 6, same shape, `state_default: unknown` | 3 | 50% |
| `syrotech_gpon` (data profile) | 2 | 4: `rx`, `tx`, `state`, `serial`; `pon_index: first_segment` so pon and key come from the **OID index** | **1**: `state` | **75%** |
| `huawei` (built-in) | 0 live | 6 | 1 (index-keyed, `gpon.py:72-73`) | 83% |

Walk cost is roughly `columns × ceil(ONUs / 25)` requests. On MAIN_OLT2 (773 ONUs) that
is 6 × 31 = **186 requests today**, dropping to 3 × 31 = **93** — a real halving, but not
the order of magnitude the brief assumes. **On the fleet that matters (29 of 32 profiled
OLTs are `dbc`), a state-only ONU walk is a 2× speedup, not an 8×.** That materially
changes Option B's expected value and is the single most important correction in this
document.

Whether a 93-request walk fits inside a 60–120 s clock is unmeasured. §9.

### 3.3 Four blockers that must be solved before a state-only walk can ship

These are not polish. Each one, shipped unsolved, produces a wrong number on a screen or
a wrong number on a bill.

**Blocker 1 — the ONU wire is NOT sparse, and the upsert is unconditional.**
Unlike ports, there is no `carried` set on `OnuOptic.to_wire()` (`gpon.py:35-41` emits all
ten keys always) and no `held()` fallback in `optics.sync_device`
(`optics.py:124-132` builds `pending` unconditionally). The SQL is unconditional too
(`store_snmp.py:1130-1143`): `serial=excluded.serial, rx_dbm=excluded.rx_dbm,
tx_dbm=…, distance_m=…, name=…, severity=…`.

**A state-only walk fed through this path blanks `serial`, `name`, `rx_dbm`, `tx_dbm`,
`distance_m` and `severity` on every ONU in the fleet, on the first fast tick.**

Required: port the port-side sparse-wire discipline onto the ONU wire — an ONU
equivalent of `main.py:116-128` `_WIRE_OPTIONAL`/`_wire_port`, an ONU equivalent of
`ports.py:31-49` `held()`, and a preserve-on-absence upsert. The port side's rule carries
over verbatim and should be stated the same way: *an absent key never arrived; a present
key, even `None`, is authoritative.*

**Blocker 2 — blanking `serial` moves the bill.**
`src/wisp/central/store_billing.py:24-29`:

```sql
SELECT COUNT(DISTINCT wisp_norm_mac(o.serial)) FROM onu_optics o …
 WHERE … AND COALESCE(o.serial,'') <> '' AND o.last_online_at >= ?
```

With `serial` blanked, that count is **zero**. `SOURCE_LADDER` is `("onu", "none")`
(`central/metering.py:48`) with nothing underneath but the device floor, so every org
would fall to the floor and stamp `downgraded`. CLAUDE.md's own words: *"A broken walk
must never silently move a bill."* This is the most severe consequence in the batch and
it is caused purely by a cadence change. Blocker 1's fix is what prevents it — but it
must be **pinned by a test**, not assumed. Suggested name, in the house style:
`test_a_state_only_walk_does_not_move_the_bill`.

**Blocker 3 — the web-optics merge reads the WIRE, not the stored row.**
`api/edge.py:189-190` merges *before* `sync_device`, and `weboptics.merge_scraped`
(`weboptics.py:531-551`) keys on `_match_key(onu.get("serial"))` **of the incoming wire
row**. Preserve-on-absence at the database layer therefore cannot rescue it: a state-only
wire with no `serial` matches nothing and merges zero readings.

The right answer is not to carry `serial` on the fast wire — it is to **skip
`_merge_web_optics` on a state-only sweep entirely.** The scrape has its own 900 s clock
(`config.py:133-134`) and its readings are already in `onu_web_optics`; a fast sweep that
skips the merge loses nothing, whereas a fast sweep that *runs* the merge with no serials
would re-run the whole matcher 60 times an hour to merge nothing.

**Blocker 4 — `dbc`'s ident path defaults a missing state to ONLINE. This is the
alarm-muting direction.**
`gpon.py:292-298` `_metric_state` is the guard CLAUDE.md describes ("an ABSENT state cell
is `unknown`, never `state_default`"), and it is correct — but **it is only applied on the
metric path** (`gpon.py:308`). The ident path decodes raw:

```python
# gpon.py:373
state=profile.decode_state(cells.get("state", "")),
```

and the built-in `dbc` decoder (`gpon.py:88-94`) ends `return STATE_ONLINE` for anything
it does not recognise — including `""`. So on the **29 OLTs using the built-in `dbc`
profile**, a walk where `ident_key`/`ident_pon`/`ident_onu` arrive but `ident_state` is
cut short reports **every affected ONU as online**. A real fibre cut would be silent.
Today this needs a mid-walk truncation to bite; a state-only walk makes the state column
the *last* thing in a much tighter budget, so it makes the exposure worse.

Two required fixes, both cheap:
- Apply the `_metric_state` guard on the ident path too (`if profile.oid_ident_state and
  raw is None: return STATE_UNKNOWN`).
- Make the state-only walk **atomic on the state column**: if the state column came back
  short, ship **nothing** for that OLT and file the sweep as `partial` naming the column.
  A half-covered state walk is worse than no walk, because `ponfault` and the roster
  cannot tell "not walked" from "still online".

(Note `cdata_54824` and `syrotech_gpon`, being *data* profiles, go through
`_state_decoder` (`gpon.py:146-148`) with declared defaults `unknown` and `offline`
respectively — the `unknown` one is safe; `syrotech_gpon`'s `offline` default is
protected by the metric-path guard because it maps `state`, and it should stay on the
metric path.)

### 3.4 Two more consequences of a state-only sweep on central

**Optical severity and the OLT badge.** `optics.sync_device` recomputes
`sev = _severity(rx, state, …)` (`optics.py:105`) and writes it (`:132`), then
`_update_badge` (`optics.py:142-163`) counts `crit_unacked` and fires `OPTICAL_CRIT` /
`OPTICAL_RECOVERED`. With `rx` absent, `_severity` returns `ok` for every ONU
(`optics.py:42-43`), the badge clears, and the sweep emits `OPTICAL_RECOVERED`.
Those kinds are outside `_ACTIVE_KINDS` (`notify_policy.py:16-21`) so nobody is *paged* —
but the badge, the tree chip and the Home tile would flap between the fast and full
sweeps, which is exactly the "a chip is a claim about NOW" failure. **A state-only sweep
must not run the badge update, and must preserve `severity`.**

**The historian.** `history.OpticsAccumulator.add` (`history.py:83`) and
`OnuAccumulator.add` (`history.py:148`) are fed inside the same loop
(`optics.py:110`, `:116`). A state-only sweep would push `measured = 0` and null Rx into
`hist_olt_sweep` / `hist_olt_hour` (`store_history.py:113-143`), which is exactly the
"nothing measured rendered as nothing wrong" trap. `record_onu_sweep`
(`store_history.py:181-203`) is hour-bucketed and safe on volume, but `onu_events`
(transitions) is per-transition and *would* grow, which is the point — a faster clock
catches more flaps. **Recommend: a state-only sweep records the ONU state tier and skips
the optics tier entirely.**

### 3.5 The roster-snapshot rule forbids a row-scoped ONU walk

`onuroster.current_roster` (`onuroster.py:98-115`) keeps only rows whose `updated_at`
**equals** the device's newest stamp:

```python
out.extend(r for r in onus if (t := _ts(r.get("updated_at"))) is not None and t == newest)
```

So a walk that refreshes a *subset* of ONUs makes the roster *become* that subset.
Downstream: `capacity_faults` (`onuroster.py:130-148`) would see a PON's count collapse
and `OnuRosterAlerter._sweep_capacity`'s clearing branch (`onualert.py:62-73`) would fire
"✅ PON below capacity"; `duplicate_macs` (`onuroster.py:151-177`) would lose members and
`_sweep_dup_mac` (`onualert.py:105-117`) would fire "✅ Duplicate MAC cleared" — and the
`shadow` guard at `onualert.py:80` does **not** protect against this, because
`stale_s=None` only skips the staleness test, not the `t == newest` test.

**Therefore: the ONU split may be by COLUMN only, never by row.** Every fast sweep must
carry every ONU the OLT reports. Stated as an invariant so a future session cannot
"optimise" it away.

(Observation, offered as a question rather than a diagnosis: several live OLTs already
have far fewer rows at the newest stamp than in total — MAIN_OLT2 341/773, `Ndcl OLT`
134/380, MAIN_OLT5 66/246, HLY-OLT-2 216/328. On HLY-OLT-2 the stragglers are tiny
cohorts weeks old, which reads as ordinary "`onu_optics` never deletes" residue. On
MAIN_OLT2 they are three cohorts of 71/190/119 stamped within ten minutes on 2026-08-08,
which does not. Worth answering before adding a second clock to the same table. §12, Q4.)

### 3.6 Option B cost

`G` = the fast state clock, `Gf` = the full optics clock, `N` = ONUs on the OLT.

- **SNMP requests/device/hour.** Fast: `3600/G × 3 × ceil(N/25)` on `dbc`, or
  `3600/G × 1 × ceil(N/25)` on index-keyed profiles. For MAIN_OLT2 (`N`=773) at `G`=60:
  `60 × 93` = **5,580 requests/hour**, against today's `12 × 186` = 2,232. That is a
  **2.5× increase in request volume** for a 5× freshness gain on `dbc` — the trade is real
  but it is not free, and it lands on boxes CLAUDE.md already records as fragile.
- **Rows on the wire/hour.** `3600/G × N` rows, each much narrower (3–4 keys instead of
  ten). At `G`=60 and the measured fleet (5,638 ONUs): **338 k rows/hour** vs 68 k today.
  This is the case gzip was built for: `runtime/central_client.py:14-19` measured level 1
  at **87% saving in 4 ms** on a real hot port sweep, threshold
  `ship_gzip_min_bytes` = 4096 (`config.py:260-267`). So **wire cost rises ~5× in rows and
  well under 1× in bytes relative to today's uncompressed traffic** — request count and
  wire bytes genuinely move in opposite directions here, and only the request count is a
  risk to the OLT.
- **Central ingest writes/hour.** `upsert_onu_optics_many` is one `executemany` per OLT
  per sweep (`store_snmp.py:1145-1157`) — 5× the executions, same shape. Plus
  `record_onu_sweep`'s two `executemany`s (hour-bucketed, so 5× the upserts against the
  same row count). Plus `PonFaultAlerter.sweep` **and** `OnuRosterAlerter.sweep`, which
  run on *every* report carrying optics (`api/edge.py:193-200`) and each begin with a full
  `org_onu_rows` read (`ponalert.py:27`, `onualert.py:30`) — on chandana-network that is
  ~2,100 rows joined through `_LABEL_JOIN` and `_RADIUS_COLS`
  (`store_snmp.py:220-230`), **5× per hour**. This is the sleeper cost of Option B and it
  is on the ingest thread.
- **Risk: HIGH until the four blockers in §3.3 are closed; MEDIUM after.** The residual
  risk is the `dbc` request-volume increase against boxes that are already documented as
  answering only whoever retries longest.

---

## 4. Fast clocks must TRY-ACQUIRE and skip, never queue

### 4.1 What the code does today

Two separate mechanisms, and neither is a try-acquire.

**At the sweep level**, `main.py:594-599`:

```python
if snmp_poller is not None and snmp_task is None and started >= next_ports:
    ...
    snmp_task = asyncio.create_task(...)
    next_ports = started + cfg.port_interval_s
```

The `snmp_task is None` guard prevents *overlap*. It does **not** prevent immediate
re-fire: `next_ports` is stamped at **launch**, so a sweep that overruns its interval is
already past due when it finishes and the next cycle launches it again with zero gap.
That is exactly the failure CLAUDE.md records — *"One clock once made a slow roster walk
starve the ifTable walk and re-fire immediately — the polling caused the failure"* — and
it is still reachable today at the sweep level.

**At the device level**, `_SnmpAirtime.slot` (`main.py:93-97`) takes
`async with self._locks.setdefault(ip, asyncio.Lock())` — an **unbounded wait**. A fast
tick arriving while a slow full walk holds the device lock does not skip; it queues, and
it holds an `asyncio` task and (once it gets past the lock) a fleet slot, behind data that
is by then stale anyway.

### 4.2 The design

Three rules, all cheap:

1. **`try_slot(ip)` — a non-blocking variant of `slot`.** `asyncio.Lock` exposes
   `locked()`, so a `try_slot` that yields `False` immediately when the device lock is
   held (and when `Semaphore._value == 0`, or equivalently via
   `asyncio.wait_for(sem.acquire(), 0)`) is a few lines. The **fast** paths use
   `try_slot`; the **full** paths keep `slot` unchanged, so the slow, authoritative walk
   is never starved by the fast one. Extend `SharedAirtimeGateTest`
   (`tests/integration/test_daemon_central_brain.py:368`) with a
   `test_a_fast_tick_skips_rather_than_queues_behind_a_slow_walk`.
2. **Stamp the next tick at COMPLETION, not at launch**, for fast clocks:
   `next_fast = loop.time() + F` set when the task resolves, with an explicit floor so a
   walk longer than `F` yields a gap rather than a back-to-back re-fire. This is the one
   line that closes the sweep-level re-fire above.
3. **A skipped tick is a fact, not a silence.**

### 4.3 What a skipped tick must record

A fast clock that is *permanently* skipping looks, from every existing surface, exactly
like a fast clock that is working: `device_snmp_status` keeps its last `ok`, `last_ok_at`
keeps advancing on the full walks, and the panel says "fine". That is the failure this
whole batch exists to avoid.

`SNMP_STATUS_STATES` (`store_util.py:9-10`) is a **closed vocabulary** and unknown states
are silently dropped by `upsert_snmp_statuses` (`store_snmp.py:1260`) — the same trap
that required `partial` to be added when the column split shipped. So:

- **Add `skipped` to `SNMP_STATUS_STATES`** — and to the SPA's mirror — before anything
  can report it.
- A skipped fast tick writes `{"state": "skipped", "detail": "<why>", "elapsed_s": null}`
  under a **new subsystem key** (`SNMP_SUBSYSTEMS` is also closed —
  `store_util.py:7`), e.g. `"ports_fast"` and `"optics_state"`. A separate subsystem row
  is right because the fast and full walks have genuinely different states and one row
  cannot hold both; it also means `last_ok_at` on the fast row answers "when did the fast
  clock last actually run", which is the question.
- `elapsed_s` stays **NULL** on a skip. The column's own comment already states the rule
  (`store.py:1084-1086`): *"NULL is 'the probe reported none' … it must render as nothing,
  never as an instant walk."* A skipped tick that recorded `0.0` would show up in §9's
  query as the fastest walk in the fleet.
- **Nothing pages on it.** Consistent with `elapsed_s` ("nothing alarms on it"). It
  surfaces in the device's SNMP diagnosis panel and in the §9 query. If the operator later
  wants an alarm, the honest one is *"the fast clock has not completed a tick in N
  minutes"*, not *"a tick was skipped"* — skipping is the designed behaviour.

Also record a **skip counter or a last-run stamp per (device, fast subsystem)**, because
one skipped tick is normal and a hundred is a broken deployment, and `state` alone cannot
tell them apart.

---

## 5. Do NOT shorten `snmp_request_timeout_s`

Stated for the record so a future reader does not re-derive it from the arithmetic.

`config.py:110-113`: `snmp_request_timeout_s` 5.0 s, `snmp_request_retries` 3 — consumed
at `snmp.py:239-241` and passed to `UdpTransportTarget.create(…, timeout, retries)`
(`snmp.py:274-275`). The GPON side has its own pair (`config.py:106-109`, used at
`gpon.py:387-388`), also 5 s × 3.

CLAUDE.md: *"weak agents answer whoever retries longest (the strict window got zero
responses for 26 h fleet-wide). Don't 'optimize' the retries down."*

A tempting piece of false reasoning is available here and should be named so it can be
refused: *"a 60 s clock cannot contain a 15 s worst-case request, therefore shorten the
timeout."* It cannot, and the correct conclusion is the opposite one — **if the fast walk
does not fit inside the fast clock at 5 s × 3, the fast clock is wrong, not the timeout.**
Options: raise the fast clock, shrink the fast row/column set further, or drop the option.

Note the asymmetry, which is not a bug but is worth knowing: ports use
`max(1, retries)` (`snmp.py:241`) and GPON uses `max(0, retries)` (`gpon.py:388`), so the
GPON side can be configured to zero retries and the port side cannot.

---

## 6. The 900 s gates and the counter-regression self-heal under a faster sweep

### 6.1 The correction the brief needs

> *"The counter-regression rule … costs roughly 4 rate-less sweeps after a real reboot
> (`STALE_S` hatch) — at a faster cadence that is the same number of sweeps but much less
> wall-clock."*

**This is backwards, and the code says so.** `_baseline_stale` (`ports.py:74-85`) compares
the **stored** `counters_at` — which is *preserved* through the hold (`ports.py:248-250`)
— against the current report `ts`:

```python
return _dt_seconds(at, ts) > STALE_S      # ports.py:85, STALE_S = 900 (ports.py:11)
```

`counters_at` is pinned at the last good read for the whole hold, so the elapsed time
grows monotonically with **wall clock, independent of cadence**. At 300 s the hatch opens
on the 4th post-reboot sweep (t+1200); at 60 s it opens on the 16th (t+960). **Same
≈15-minute recovery; four times as many rate-less sweeps.**

So: the *operator-visible* cost is unchanged, and the *sample-count* cost rises 4–5×. The
accepted cost stays acceptable in the dimension the operator experiences it. Nothing here
blocks a faster clock, and no constant needs re-deriving. Pinned by
`CounterRegressionTest` (`tests/integration/test_central_ports.py:525`) — note that its
fixtures move the delta and never walk the absolute counter down, which is the rule to
preserve when adding cadence cases.

### 6.2 What genuinely breaks, what merely shifts

| Gate | Where | Wall-clock or sweeps? | Verdict at a faster clock |
|---|---|---|---|
| `_baseline_stale` reboot hatch | `ports.py:74-85`, 900 s | **Wall clock** | **Shifts only.** Same recovery time, 4–5× the rate-less sweeps. No change needed. |
| Held-bps expiry (`still_now`) | `ports.py:253-257`, 900 s | **Wall clock** | **Shifts only.** A held rate is a claim about now and expires at the same age. More consecutive sweeps show the same held number; the historian correctly records `None` for each (`ports.py:281-283`). |
| `ponfault.evaluate_org` OLT staleness | `ponfault.py:206`,`:218`, 900 s | **Wall clock** | **Shifts favourably.** With a fast state clock the OLT is fresh far more often, so fewer PONs are skipped. But note the *ratio*: at 300 s the gate tolerates 3 missed sweeps; at 60 s it tolerates 15. **Re-derive as a multiple of the fast clock, not as 900.** §12, Q3. |
| `onuroster.current_roster` / `fresh_device_ids` | `onuroster.py:9`,`:111`,`:127`, 900 s | **Wall clock** | Same as above, same re-derivation question. And the `t == newest` rule forbids row-scoped ONU walks outright (§3.5). |
| `ponfault` `WINDOW_MIN` = 30 | `ponfault.py:20`,`:155` | Wall clock, in *minutes* | **Shifts favourably and is the real prize.** A dark ONU must have `last_online_at` inside 30 minutes to join the cohort. At a 300 s clock the *earliest* the cohort can form is one sweep after the drop; at 60 s it is five times sooner. The 30-minute window itself does not need changing. |
| SPA `SNMP_FRESH_AFTER_S` = 900 | `web/src/lib/format.ts:87` | Wall clock | **Breaks the honesty contract under Option A** — see §2.6. `MAX(updated_at)` would read fresh over a mostly-stale table. Needs the `ports_full_at` companion. |
| `web_optics_max_age_s` = 3600 | `config.py:135-136`, used `api/edge.py:211` | Wall clock | **Shifts.** Unchanged semantics, but the merge would run 5× as often over the same 900 s-clock scrape data. §3.3 Blocker 3 says skip it on fast sweeps. |
| `snmp_down_consecutive` = 2, `snmp_bw_consecutive` = 3 | `config.py:120-124`, `ports.py:189`,`:228` | **Sweeps** | **These are the ones that need re-deriving.** A port-down alarm today needs 2 × 300 s ≈ 10 min of confirmed down; at a 60 s fast clock it becomes 2 min. That is a *faster page*, which is the goal — but it is also 5× less flap suppression on link-flapping uplinks, and `PORT_DOWN` is one of the few kinds that actually pages (`notify_policy.py:16-21`). **Decide deliberately; do not let it change as a side effect of the clock.** §12, Q2. |

### 6.3 One genuine accuracy problem a faster clock makes much worse

`dt` for the throughput calculation is the spacing between **report** timestamps, not
between **walks**. `ports.py:217` uses `_dt_seconds(prior["counters_at"], ts)` and
`counters_at = ts` (`:218`), where `ts` is the report envelope stamp
(`api/edge.py:104`, set at `main.py:433` at the *start of the report cycle*). The octet
delta, however, spans the interval between the two **walks**, which completed some time
earlier — and the walk-to-report lag varies by up to a full probe interval, because the
result is harvested on a later cycle (§1.1).

At a 300 s cadence a ±60 s lag jitter is a **±20%** rate error. At a 60 s cadence it is
**±100%**. This is a live inaccuracy today, hidden by the long interval; a fast clock
exposes it, and the busy-hour panel and the bandwidth ceiling both read these rates.

**The fix is nearly free and it is already half-built:** the same workstream adding
`elapsed_s` is already timing the walk (`main.py:99-105`). Carrying a **walk-completed
timestamp** on the ports payload and using *that* as `counters_at` makes `dt` the interval
the counters actually span. **This should be treated as a prerequisite for any port-clock
change below ~180 s, not as a follow-up.**

---

## 7. Cost summary and ranking

Per device per hour, `F`/`G` = fast clocks, against today's 300 s baseline.

| | Option A — port rows | Option B — ONU state column |
|---|---|---|
| SNMP requests | **↓ ~90%** even at 5× cadence (targeted GETs: ~2 requests/tick vs ~120 for a table walk) | **↑ ~2.5×** at 5× cadence on `dbc` (3 columns × `ceil(N/25)`) |
| Rows on the wire | **↓ ~93%** (`R`≈10 vs `T`≈744) | **↑ ~5×** in rows, near-flat in *bytes* after gzip level 1 (87% measured, `central_client.py:14-19`) |
| Central ingest writes | ↑ 5× on `hist_port_sweep` (a per-row two-statement loop, `store_history.py:205-236`); ~102 k → ~510 k rows at 48 h retention | ↑ 5× on the ONU upsert and the hour-bucket upserts, **plus 5× `PonFaultAlerter` + `OnuRosterAlerter` full-`org_onu_rows` reads on the ingest thread** (~2,100 joined rows/sweep on the largest org) |
| New failure modes | Freshness display lies unless `ports_full_at` ships with it; cold-start empty fast set | Blanked serial moves the **bill**; blanked Rx flaps the optical badge; `dbc` ident-path defaults a missing state to **online**; the roster snapshot rule forbids row scoping |
| Prerequisites | A `/edge/devices` key + central-before-edge deploy order; the walk-timestamp fix (§6.3) if the port clock drops below ~180 s | Sparse ONU wire + preserve-on-absence upsert + skip-merge + skip-badge + ident-path state guard + atomic state column |
| Risk | **LOW** | **HIGH until the four blockers close; MEDIUM after** |
| Operational value | Uplink and PON-aggregate down detection: ~10 min → ~2 min | Fibre-cut detection: ~10 min → ~2–3 min (see below) |

### Ranking, by operational value per unit of risk

1. **Option A (port rows).** Highest value/risk by a wide margin. It uses machinery that
   already exists and is already pinned by tests, it *reduces* load on the devices, and
   `PORT_DOWN` is one of the few kinds that actually pages
   (`notify_policy.py:16-21`) — so the freshness gain converts directly into a faster real
   page. Its one honesty hazard (§2.6) is a `MIN()` and a label.
2. **§4 (try-acquire and skip-visibility) — do this FIRST regardless.** It is a
   prerequisite for either option and it retires a live latent fault (the sweep-level
   immediate re-fire, §4.1). Low cost, no new data path.
3. **§6.3 (walk-completed timestamp).** Small, closes a real existing inaccuracy, and is a
   hard prerequisite for a fast port clock.
4. **Option B (ONU state column).** Highest raw operational value — fibre-cut detection is
   the thing an ISP calls about — but the measured saving is **50%, not 87%** on 30 of 32
   profiled live OLTs (§3.2), the request volume *rises*, and four of the five ways it can
   go wrong are silent-and-wrong rather than loud-and-broken (§3.3). **Do it after A, with
   every blocker closed and pinned, and stage it to one org.**
5. **A wholesale reduction of `gpon_interval_s` with no column split.** Listed only to
   rank it: it is the cheapest thing to type and the worst thing to do — full 6-column
   walks 5× as often against boxes CLAUDE.md documents as quitting GETBULK mid-table.
   **No.**

**One honest deflation of the headline.** The brief says fibre-cut detection goes "from 5
minutes blind to about 1 minute". Given §1.1's four serial terms, at `G` = 60 s on a 60 s
probe loop the *worst case* is (60 s clock) + (walk) + (≤60 s harvest) + report ≈ **2–3
minutes**, not 1. Getting to ~1 minute needs either a shorter probe interval or a
dedicated ship path for the fast walk (§12, Q1). Two to three minutes against ten is still
the largest operational win in this batch — it just should not be promised as one.

---

## 8. What to look at first when the numbers land

Run this **after central has been restarted** (the `elapsed_s` migration at
`store.py:1794-1795` needs a restart — CLAUDE.md's "restart central in the same breath as
any schema change") **and after at least one full sweep cycle has reported**.

### 8.1 Per-walk duration, the raw picture

```sql
SELECT d.org_id, d.assigned_node_id AS node, d.name, s.subsystem, s.state,
       s.item_count, s.elapsed_s, s.updated_at, s.last_ok_at
  FROM device_snmp_status s
  JOIN org_devices d ON d.id = s.device_id
 WHERE d.is_active = 1
 ORDER BY s.elapsed_s DESC NULLS LAST;
```

`elapsed_s IS NULL` means an older agent build, **not** a fast walk (`store.py:1084-1086`).
Count the NULLs first; if they are the majority, the rollout is incomplete and every
conclusion below is drawn from a minority of the fleet.

### 8.2 The number that actually decides everything: sweep wall-clock per node

`elapsed_s` is per-walk; the clock has to contain the *sweep*, which is
`Σ(walks)/snmp_max_inflight` (§1.2):

```sql
SELECT d.org_id, d.assigned_node_id AS node,
       COUNT(*)                              AS walks,
       ROUND(SUM(s.elapsed_s), 1)            AS serial_seconds,
       ROUND(SUM(s.elapsed_s) / 4.0, 1)      AS sweep_s_at_inflight_4,
       ROUND(MAX(s.elapsed_s), 1)            AS worst_single_walk
  FROM device_snmp_status s
  JOIN org_devices d ON d.id = s.device_id
 WHERE d.is_active = 1 AND s.elapsed_s IS NOT NULL
 GROUP BY d.org_id, d.assigned_node_id
 ORDER BY serial_seconds DESC;
```

Substitute the node's real `WISP_SNMP_MAX_INFLIGHT` for the `4.0` if it has been
overridden.

### 8.3 The go / no-go thresholds

Read `sweep_s_at_inflight_4` for the **worst** node (expected: `rapidnetworks/Edge_1`,
19 SNMP devices) and `worst_single_walk` for the **worst** device.

**Option A — port row split.** The relevant number is the port subsystem's
`worst_single_walk` on the biggest table (MAIN_OLT2, 744 rows).

| Observation | Verdict |
|---|---|
| Port walks mostly < 5 s and the sweep total fits in 300 s with room | **Row split is NOT NEEDED for capacity.** It still buys a faster page, so run it as a cadence choice, not a rescue: set `F` ≈ 3× `worst_single_walk`, floored at the probe interval. |
| Port walks 5–20 s, sweep total 100–250 s | **GO.** Targeted GETs collapse the fast tick to ~1 request. Set `F` ≥ probe interval and ≥ 4× the measured *fast* walk (measure it in staging on one device before fleet rollout). |
| Any port walk > 40 s, or any device filing `timeout` | **GO, and it is the stated Stage-2 trigger** (CLAUDE.md: "hot walk > 40 s or an OLT past ~400 ONUs"). Ship the row split before touching any clock; do **not** raise `port_walk_timeout_s`. |
| Sweep total already > 300 s on any node | **Neither option may raise a clock until this is fixed.** The node is already overrunning its interval and the sweep-level re-fire in §4.1 is live. Fix §4 first. |

**Option B — ONU state column.** The relevant number is the optics subsystem's
`elapsed_s`, and the projection is **`elapsed_s × 0.5`** for `dbc`/`cdata_54824` and
**`elapsed_s × 0.25`** for `syrotech_gpon` (§3.2's column ratios — a projection, not a
measurement; confirm it on one OLT in staging before believing it).

| Observation | Verdict |
|---|---|
| Optics walk `elapsed_s` < 20 s on the biggest OLT | **GO** at `G` = probe interval, once §3.3's blockers are closed. Projected state-only walk ≈ 10 s, comfortably inside a 60 s tick. |
| 20–60 s | **CONDITIONAL GO.** Projected ≈ 10–30 s. Set `G` ≥ 120 s and re-measure with the fast walk's own `elapsed_s` before going lower. Freshness ~2 min → ~4 min: still a win, half the headline. |
| > 60 s, or approaching the 75 s `gpon_walk_timeout_s` cap | **NO-GO on cadence.** A halved walk is still ~35 s+ and will not sit inside any clock worth the risk. The lever here is the walk, not the clock: fewer columns still, or Stage-2-style targeted reads. |
| Any OLT filing `timeout` on optics | **NO-GO for that OLT.** Exclude it explicitly rather than letting a fast clock hammer a box that is already failing its 75 s cap. |

**A third check that gates both:** compare `item_count` against the stored row counts
(`SELECT device_id, COUNT(*) FROM onu_optics GROUP BY device_id`;
`… FROM switch_ports …`). A walk filing `ok` with an `item_count` well below the stored
row count is a **silently short walk**, and no cadence change is safe on that device until
that is understood. Two devices already file `ports/partial` today.

---

## 9. Out of scope, noted for later: SNMP traps as a HINT

**Not now. Recorded so the idea arrives with its guardrails already attached.**

The grammar already exists in this codebase and it is `compute_recheck`
(`api/edge.py:144-146`, followed at `main.py:368-390`): central names *suspect* targets in
a report reply, the edge re-probes exactly those, and the FSM decides. The trap version
would be the same shape from the other direction — a linkDown or an ONU-offline trap is a
**hint that triggers an immediate targeted walk**, and the walk's answer is the truth.

The rules that must survive from day one:

- **A trap is never a state.** Nothing writes `switch_ports.oper_status` or
  `onu_optics.state` from a trap. It only schedules a walk. (Traps are unauthenticated
  UDP; a state written from one is a state an attacker can write.)
- **The edge still never accepts inbound from central.** A trap listener is the edge
  listening to *devices on its own LAN*, which is a different thing from central dialling
  the edge, and the distinction must be stated in the design or it will be argued about.
- **It rides the same airtime gate**, with `try_slot` (§4) — a trap storm during an area
  power cut must degrade to "one walk", not to a walk per trap.
- **Rate-limit and coalesce at the source.** The failure this prevents is the one CLAUDE.md
  already paid for once: *"an area power cut → dozens of false 'fiber cut' pages"*.
- **It is a latency optimisation, not a coverage one.** The polled walk must remain
  correct with the trap listener switched off, so it can always be switched off.

Value: sub-10-second reaction on gear that actually emits traps. Cost: a listener, a
port, a filter, an auth story, and a new class of "the trap said X but the walk says Y".
Deliberately scoped out of this batch.

---

## 10. Where CLAUDE.md and the code disagree

Checked against the tree, in the spirit of the file's own warning that docs drift.

1. **"~4 rate-less sweeps after a real reboot"** — *misleading, not wrong.* The hold is
   bounded by **900 s of wall clock** (`ports.py:74-85`), and "4 sweeps" is that bound
   expressed at today's 300 s cadence. At any other cadence the sweep count changes and
   the wall clock does not. Suggested rewording if the clock ever moves: *"~900 s without
   a rate after a real reboot (4 sweeps at 300 s), accepted."*
2. **"An ABSENT state cell is `unknown`, never `state_default` (`gpon._metric_state`)"** —
   *true on the metric path, FALSE on the ident path.* `gpon.py:373` calls
   `profile.decode_state(cells.get("state", ""))` with no guard, and the built-in `dbc`
   decoder returns `STATE_ONLINE` for anything unrecognised (`gpon.py:88-94`). That is
   the ident path used by **29 of 34 live OLTs**, and the default is in the alarm-muting
   direction. This is a real gap, not a documentation nit (§3.3, Blocker 4).
3. **"`lexicographicMode` MUST be True to resume"** — *correct, and correctly scoped.* It
   is True only in the diagnostic walker (`ingress/walker.py:60`,
   `lexicographicMode=(start != scope)`); the production port and GPON walks pass `False`
   (`snmp.py:369`, `gpon.py:418`, `health.py:353`) because they do not resume. No drift.
4. **"targeted GETs for the ~25 real ports"** — *the 25 is confirmed as measured* on every
   C-Data EPON OLT (§1.3), but it does **not** hold fleet-wide: three live devices have no
   distinguishable ONU pseudo-rows at all. Worth a clause when this is written up.
5. Everything else checked matched: the three caps (20/60/75 s), the three clocks (300 s
   each, `snmp_interval_s` as master gate), the airtime gate's lock-before-semaphore
   order, the hot column set, `port_identity_interval_s` = 3600 s, the never-seen-index
   refresh, the sparse port wire and its atomic-counters rule, `last_online_at` freezing
   off-online, and the closed `SNMP_STATUS_STATES` vocabulary.

---

## 11. Open questions that need the operator's decision

**Q1 — Is sub-60-second ONU freshness actually wanted, given what it costs?**
The probe loop quantises every subsystem clock to the probe interval (§1.1), so `G` < 60 s
requires either dropping `poll_interval_s` fleet-wide (more ICMP, and it is clamped at
10 s) or giving the fast walk its own harvest-and-ship path (a second POST per cycle, i.e.
a real change to the report contract). If ~2–3 minutes is good enough — and against
today's ~10 it is a large win — none of that is needed.

**Q2 — Should `snmp_down_consecutive` (2) and `snmp_bw_consecutive` (3) stay counts, or
become durations?** Today they mean "10 minutes" and "15 minutes" only because the clock
is 300 s. At a 60 s fast clock they silently become 2 and 3 minutes. Faster pages are the
goal; 5× less flap suppression on a flapping uplink is not. This must be decided, not
inherited.

**Q3 — Should the 900 s staleness gates become multiples of the clock?** At 300 s, 900 s
tolerates 3 missed sweeps. At 60 s it tolerates 15, which is a much weaker claim about
"this OLT is answering". Recommend `max(900, 3 × fast_clock)` — but the operator owns
whether "fresh" means an age or a number of missed sweeps.

**Q4 — What are the stale ONU cohorts on MAIN_OLT2?** 341 of 773 rows carry the newest
stamp; 380 more sit in three cohorts stamped within ten minutes on 2026-08-08 (§3.5).
On HLY-OLT-2 the same shape is clearly ordinary never-deleted residue; on MAIN_OLT2 it is
not obviously anything. `current_roster`'s exact-stamp rule means those 380 rows are
already invisible to capacity and duplicate-MAC checks. Worth answering before a second
clock writes to that table.

**Q5 — Staging order and blast radius.** Recommend: §4 (try-acquire + skip visibility)
fleet-wide first; then Option A on one org with a small node (`badri_fiber/MAIN_1`, 3
devices) before `rapidnetworks/Edge_1`; then Option B on one org only, with the bill
re-checked against `onu_conn_count` before and after the first fast tick. Note the
existing DEPLOY ORDER rule applies to both: **central before any edge rollout.**

---

## Appendix — primary sources read

Edge: `apps/daemon/main.py` (87-105 airtime + elapsed, 116-128 sparse wire, 130-161 scope
planner, 163-201 port gather, 251-296 optics gather, 533-545 interval clamp, 592-655 the
loop) · `src/wisp/ingress/snmp.py` (174-193 reps/scopes, 237-319 the ladder, 354-408
per-column walk) · `src/wisp/ingress/gpon.py` (75-109 built-in profiles, 146-220 data
profiles, 292-381 parse, 391-429 the walk) · `src/wisp/runtime/central_client.py` (14-19
gzip) · `src/wisp/config.py` (98-137 SNMP knobs, 260-267 gzip threshold).

Central: `api/edge.py` (103-256 ingest order) · `ports.py` (11-101 gates, 169-302 sync) ·
`optics.py` (25-48 rails/severity, 87-163 sync + badge) · `ponfault.py` (17-24 constants,
109-225 the verdict) · `onuroster.py` (9-177) · `ponalert.py` · `onualert.py` ·
`store_snmp.py` (92-160 port upsert, 755-771 roster read, 840-856 ONU interfaces,
1130-1170 ONU upsert, 1252-1300 status upsert) · `store_devices.py` (100-145 device row) ·
`store_billing.py` (24-29 the ONU count) · `metering.py` (46-55 the ladder) ·
`history.py` (65-205) · `store_history.py` (113-236) · `weboptics.py` (523-563 merge) ·
`notify_policy.py` (16-30) · `store.py` (294-339 switch_ports, 1073-1091
device_snmp_status) · `store_util.py` (7-10 closed vocabularies).

SPA: `web/src/lib/format.ts` (75-91) · `web/src/map/refonu.ts` (54, 78-82) ·
`web/src/components/device-detail.tsx` (138) · `web/src/components/snmp-diagnosis.tsx`
(262) · `web/src/lib/types.ts` (1332).

Tests consulted: `tests/unit/test_snmp.py` (ScopedWalkTest 372, AdaptivePortWalkTest 236,
CombinedWalkDriverTest 152) · `tests/integration/test_daemon_central_brain.py`
(PortScopePlannerTest 328, SharedAirtimeGateTest 368) ·
`tests/integration/test_central_ports.py` (PartialWalkTest 387, CounterRegressionTest 525)
· `tests/unit/test_ponfault.py` (WitnessTest 96).

Fleet counts in §1.3 and §3.2 are read-only queries against `data/central.db`
(`mode=ro`) on 2026-08-18. They are **counts**. No walk duration exists yet.
