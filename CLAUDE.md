# CLAUDE.md

Invariants and gotchas that aren't obvious from the code. What/how/layout lives in
`README.md`. Each rule carries the reason it exists — most were paid for by a field
incident, and the reason is what stops the next session undoing the fix. Pinning tests are
named. Verify claims against code — docs drift.

## Architecture

- Central runs the brain for every org: FSM, topology suppression, fast-confirm, alerting,
  multi-org dashboard, fleet rollout state. The edge is a thin probe (`WISP_CENTRAL_URL`
  mandatory): fetch topology, probe ICMP under bounded fan-out, report raw per-IP samples,
  heartbeat. No local DB, dashboard, or FSM on the edge.
- Central is pure stdlib. The dashboard is a build-time React/TS/Tailwind SPA (`web/` →
  `central/static/`; the committed build is what deploys, Node is dev-only). Edge needs a
  `.venv` (icmplib/httpx) + `sysctl net.ipv4.ping_group_range="0 2147483647"`.
- **Locked decisions (don't relitigate):** brain on central; monitor shared infra, not
  end-user routers; WhatsApp (Meta Cloud API) is the SOLE notification channel, recipients
  are per-account E.164 numbers; every read/write org-scoped; edge dials central, never the
  reverse; updates pull-based, staged, health-gated; probers/notifiers behind interfaces,
  tests inject doubles.

## Imports & paths

Absolute imports under `wisp.*`; src layout, nothing installed. `apps/*/main.py` prepend
`<repo>/src` to sys.path; the admin CLI needs `PYTHONPATH=src`; tests bootstrap their own
path. `config.PROJECT_ROOT` = repo root; `central_db` defaults to `data/central.db`.

## Engine invariants

- `core/state_machine.py:MonitorEngine` is **pure** — results + ts in, states + events out,
  no I/O. Central owns build/rehydrate/persist (`central/engine.py`).
- `process_cycle(subset=None)`: None = full pass (keep byte-identical); a set[int] =
  confirmation pass (those FSMs only, topo order, skips canary/uplink).
- `probe_plan()` is a reference the edge approximates, not something central calls. Known
  gap: it counts a BACKUP parent as infra; the edge can't (`/edge/devices` carries only
  `parent_device_id`).
- `dispatch.py` sends OUTSIDE any DB transaction — a slow API call must never hold a write
  lock.
- **Windows probes via `SingleSocketIcmpProber`, never icmplib** (`WISP_PROBER` forces one):
  Windows raw sockets are promiscuous (N sockets each see every reply, O(N²)) and asyncio
  stamps arrival late. One shared raw socket + one receiver thread stamping perf_counter,
  matched by id+seq+source. Linux keeps icmplib's unprivileged datagram sockets (raw needs
  root and breaks the ping-group invariant). `unit/test_probers`.

## Scaling invariants

- Probe fan-out bounded by `Semaphore(probe_max_inflight)` (256) — unbounded gather past
  `ulimit -n` reads as a fake mass outage (socket refusals masked as 100% loss).
- Aggregation gear probed gently: parents get `pings_per_poll_infra` (2), leaves 5 — or
  ICMP rate-limiters read as phantom loss.
- Fast-confirm is central-driven: `compute_recheck` names suspect IPs in the `/report`
  reply; the edge re-probes just those (`mode="recheck"`). A frozen cycle (canary down)
  yields no hint.
- Adaptive cadence (`Config.effective_interval`) computed at startup, EXCEPT the org
  override `orgs.poll_interval_s`, which rides `/edge/devices` per cycle. Precedence: CLI
  > org > env/adaptive. **Clamped 10–120 s both sides** — past 120 s a healthy probe
  outlasts the watchdog's 180 s NODE_STALE threshold and pages as dead.

## SNMP

- **Background asyncio task, never inline in the probe cycle** (inline walks made the edge
  report every 4 minutes). Ports attach to full reports only, never recheck.
- **Three walk CAPS** — health 20 s, ports 60 s, gpon 75 s — because one cap leaves the big
  table permanently stale while small walks stay fresh. **Three sweep CLOCKS** too
  (`snmp_interval_s` is health AND the master gate, ≤0 = all off; all default 300 s; don't
  raise past ~600 s without revisiting the 900 s staleness gates). One clock once made a
  slow roster walk starve the ifTable walk and re-fire immediately — the polling caused
  the failure.
- **One `_SnmpAirtime` gate spans every SNMP subsystem**: fleet-wide `Semaphore(4)` PLUS a
  per-device lock, device lock acquired BEFORE the semaphore (waiting on a busy box must
  not pin a fleet slot). `SharedAirtimeGateTest`.
- **Per-request patience**: `snmp_request_timeout_s` 5 s × 3 retries, not 2 × 1 — weak
  agents answer whoever retries longest (the strict window got zero responses for 26 h
  fleet-wide). Don't "optimize" the retries down.
- **One `SnmpEngine` per poller, NEVER per walk** — a per-walk engine leaks ~1 MiB + one FD
  forever; FD exhaustion reads as a fake mass outage. Concurrent walks are safe
  (request-id demux). `EngineReuseTest` in `unit/test_snmp` + `unit/test_gpon`.
- **These agents QUIT a GETBULK mid-table and pysnmp reports a clean finish**: every early
  stop lands on an exact multiple of the 25 max-repetitions, every genuine end does not.
  The budget is per-RESPONSE, so no cap value makes a broad walk safe — `walker.py`
  RESUMES from the last OID. **`lexicographicMode` MUST be True to resume** (False bounds
  the walk to the start-OID's subtree, so resuming returns nothing — which reads as proof
  there was no more data); the subtree bound is our own prefix test. `unit/test_walker`.
- **An ABSENT state cell is `unknown`, never `state_default`** (`gpon._metric_state`) — a
  truncated STATE column once rendered live subscribers dark and handed ponfault a
  fabricated fibre cut. An absent COLUMN is a firmware fact (gets the default); a missing
  VALUE in a column that exists is a row fact.
- **The DDM rail guard (`sane_rx`/`sane_tx`) lives in `optics.py`**, imported BY weboptics
  — ONE definition on the one path every reading crosses. Keyed on PHYSICS: 0.0 dBm
  RECEIVED is impossible, 0.0 dBm TRANSMITTED is ordinary — the asymmetry is deliberate;
  `sane_tx` is weak on purpose (the +8.16 high rail needs the voltage column no SNMP
  profile maps). `DdmRailOnTheSnmpPathTest`.
- **pysnmp 7 walk commands take exactly ONE varbind** — multiple positional columns is a
  TypeError swallowed as "walk failed" (froze every fleet port table 30 h). The combined
  walk is `MultiColumnWalk` over raw `bulk_cmd`; ANY failure falls back to per-column —
  keep the fallback. `CombinedWalkDriverTest`.
- **`PysnmpPoller` is ADAPTIVE and vendor-agnostic**: per-device ladder — 0 combined
  (15 s time-box), 1 per-column/25, 2 per-column/4 — persisted, re-probed one rung faster
  every 6 h. The per-column net is TOLERANT (a dropped column is skipped, never fatal;
  status columns first); a dead agent still re-raises; no ifTable returns `[]`. NO vendor
  hardcode. `AdaptivePortWalkTest`.
- **The port walk is SPLIT: counters every sweep, identity hourly.** Per-sweep cost is
  rows × columns and on C-Data EPON every ONU is an ifTable row, so the full walk died
  before the counter columns every sweep while filing `ok`. Hot walk = admin, oper, HCIn,
  HCOut on `port_interval_s`; full 10-column walk on `port_identity_interval_s` (3600 s) +
  process start + any never-seen if_index. A deadline-cut column KEEPS fetched rows and is
  named in `missing_columns`. Raising the timeout is the wrong fix — rows grow with
  subscribers. `ScopedWalkTest`, `PortScopePlannerTest`.
- **The wire is SPARSE and central PRESERVES on absence**: an absent row key never arrived;
  a present key, even None, is authoritative (present-None clears a deleted alias).
  **Counters are ATOMIC per row** — both octets or neither (`counters_at` is ONE stamp;
  taking one side inflates the next delta). A counter-less sweep preserves the stored
  baseline (the old unconditional upsert is why a rate never computed on HILL-OLT-1); held
  bps expires past 900 s, the baseline never; the historian records NO rate for a held
  sweep; bw eligibility rides oper status, which IS current. **DEPLOY ORDER: central
  before any edge rollout** — old central + new edge wipes port names every hot sweep.
  `PartialWalkTest`.
- **A counter reading BACKWARDS is a GLITCH and may never become the baseline**
  (`_counter_regression`) — a regressed baseline once published a port's whole lifetime as
  one interval (121.85 Gb/s on a 1 Gb/s PON; an ISP disputed the number). A regression is
  a counter-less sweep; EITHER direction condemns the pair. **One-sided ON PURPOSE**: a
  high reading is still adopted outright (that's the two-sweep self-heal); the `STALE_S`
  reboot hatch bounds the hold (~4 rate-less sweeps after a real reboot, accepted). Test
  fixtures move the delta, never walk the absolute counter down. `CounterRegressionTest`.
- **A walk that dropped columns reports `partial`, NAMING them — never `ok`.** `partial`
  had to be ADDED to `store_util.SNMP_STATUS_STATES` (closed vocabulary; unknown states
  silently dropped). `last_ok_at` stamps on `ok` only. The ports panel chips "last walk
  incomplete" when rows exist.
- **The next lever is Stage 2** (targeted GETs for the ~25 real ports + sysUpTime reboot
  guard; triggers: hot walk > 40 s or an OLT past ~400 ONUs), never a bigger timeout. ONU
  pseudo-rows can never be dropped — they're the only per-subscriber rate source
  (`onu_if_token`).
- Unit tests inject fake pollers, so a bad HLAPI call only surfaces on a REAL walk —
  verify `device_snmp_status` after any edge SNMP rollout.
- **Remote diag walks**: dashboard-queued, delivered in the next full `/report` reply (the
  edge NEVER accepts inbound), sequential runner, refuses target IPs outside the node's
  device list. `truncated` rides to the dashboard row — a partial dump that looks complete
  turns "that OID holds nothing" into a costly false negative. A narrower root is the fix,
  never a bigger cap.
- **Vendor health profiles are DATA** (`snmp_profiles`, org NULL = global, longest
  sysObjectID prefix, closed decode vocabulary). Onboarding a vendor = a profile row,
  never a rollout.

## Central runs the brain

- `central/engine.py` is the only DB glue; `central/dispatch.py` is the alerting policy
  (dedupe per outage, owner+worker on open, both on resolve, ack never stops it — only
  recovery does).
- **`EngineRegistry`: one live engine per org** (flap streaks accumulate across stateless
  `/report` calls). Rebuilds only when the fingerprint `(id, ip_address, parent_device_id,
  parents)` changes; rehydrates from `device_states` (breaking rehydration re-pages
  everyone on restart). **`ip_address` IS in the fingerprint** — a shape-only fingerprint
  once froze an IP-edited device silently at UP for 3.5 h (the tell: SNMP fresh, ICMP
  frozen — SNMP reads the device row each sweep, the engine caches it). Anything the
  engine caches from a device row belongs in the fingerprint.
  `test_registry_rebuilds_when_a_device_ip_changes`,
  `test_a_device_keeps_being_monitored_after_its_ip_changes`.
- Wire format is IP-keyed (`POST /report`); the edge never sees device ids.
- Escalation sweeping rides the report cadence, scoped to that org.
- **The heartbeat is the self-update channel, not liveness** (liveness = `touch_node` off
  `/report`). Update directives written atomically; DELIVERY clears the restart flag — a
  lost directive means the operator clicks again, never a loop.
- Auto-update is org-opt-in; a HALTED rollout for the same target is NEVER auto-retried —
  a build that failed its health gate re-arms only via a human's Retry.

### The notification governor

`central/notify_policy.py`. Every paging shell routes send + status + log through
`AlertRouter.emit(kind, …)`.

- **Only `_ACTIVE_KINDS` pages** — currently PORT_DOWN/RESTORED, PORT_BW_HIGH/NORMAL/
  LOW/OK, CAMERA_DOWN/RESTORED. Device/uplink go through `dispatch.py` and probe up/down
  through the watchdog (both bypass the governor). Everything else (optics/PON/ONU/perf/
  backup/hourly-escalation) is suppressed: logged, state still written — the dashboard
  stays fully live. Re-enabling a kind is one line in `_ACTIVE_KINDS`.
- The PUSH/DIGEST two-tier machinery is intact but dormant; cooldown backstop per
  `(device, kind)` (ports pass 0 — already per-if_index gated).
- Why it exists: an area power cut → dozens of false "fiber cut" pages → rate-limited
  channel dropped REAL pages. Tests: `unit/test_notify_policy`, `unit/test_whatsapp`,
  `integration/test_central_whatsapp`.

### WhatsApp is the SOLE channel

- `build_notifier(cfg, store)` returns a bare `WhatsAppNotifier`; `alert_log.channel` is
  always `whatsapp`. `send(title, body, priority, *, whatsapp, facts)` — audience is a
  list of E.164 numbers; `WhatsAppFacts` fills the one approved template `wisp_alert1`
  (Device/Status/Detail/Time Logged).
- **A send can never crash the report cycle** — nothing raises; the result drives the
  logged status. `send_with_retry`: network/5xx retry, 4xx fail fast. Sends run on a
  bounded `SendPool` (the watchdog deliberately stays inline — its retry-next-sweep
  semantics need the sync result).
- **ONE audience, no role routing**: `org_alert_recipients(org)` = owner + worker numbers,
  de-duped, one send. **The superadmin ops number is NOT in the org audience** — it
  carries only topic-less pings (org "I've paid", self-downgrade churn, release-sync
  failing) via `orgs._admin_whatsapp`. Don't re-add it.
- Central-only by construction: the edge builds a store-less notifier, which is inert.
- Config lives in `app_settings` (superadmin, read FRESH each send, no restart); token is
  write-only; per-account numbers are self-service.
- **"Time Logged" renders in the operator's zone at ONE choke point**
  (`notifiers._wa_time`, `WISP_DISPLAY_TZ` default Asia/Kolkata) — a page is the only
  place a stored UTC stamp reaches a human raw. Degrades, never raises. Display only.
  `DisplayTimeTest`.
- Dead ntfy plumbing (`orgs.ntfy_topic*`, `org_role_topic`) stays unused — don't wire it
  back or clean it up into a migration.

### Assignment: paging AND visibility

`central/assignment.py` + `store_assign.py`; `org_device_workers`.

- **It is a visibility rule too, and UNASSIGNED means NOBODY** (2026-08-12, a deliberate
  reversal — the old "unassigned pages everyone" delivered every page to all nine workers
  of an org that had assigned 1 of 9 devices). A worker sees and is paged for exactly
  `scope_of(user)` = assigned devices + everything below (ancestors NOT visible). An
  unassigned device reaches no worker. **Owners and superadmin are untouched — that is
  the whole safety argument** (`_compose` always prepends owners; `visible_device_ids`
  returns None = no filter for non-workers). An org can blind its field team by assigning
  nothing; it cannot go dark.
- An event with NO device (uplink) reaches owners only; a probe resolves via `for_node`
  with NO org-wide fallback (that fallback was the largest source of "everyone got it").
- Read side is ONE helper, `api/common.visible_device_ids`, gated inside
  `device_read_scope`/`survey_write_org`/`triage_outage_org` — new routes on those choke
  points are scoped by default; list endpoints filter explicitly. **A count and its list
  narrow TOGETHER**; `/api/nodes` is deliberately unfiltered (probe-reported numbers).
- A worker with nothing assigned is TOLD (`format.ts:NO_ASSIGNED_DEVICES`, one string),
  never shown "No devices yet. Add one above."
- Known cost, accepted: a field-created passive is unassigned and vanishes from its
  creator's view; auto-assign was deliberately not built without asking.
- **Responsibility flows DOWN the tree and UNIONS, never overrides** (nearest-ancestor-
  wins rejected: a narrow assignment must not silently drop a wide one). Live parent
  chain, PRIMARY parents only, cycle-guarded.
- A deactivated assignee doesn't count as "somebody is responsible"; an assignee with no
  WhatsApp number is REPORTED (`unreachable`), never widened around.
- `emit` resolves the audience AFTER the allowlist gate (suppressed kinds must not cost
  three queries per emit); the watchdog builds a fresh audience per page.
- UI: both assignment surfaces still say "paged" — they now govern SEEING too; say so.
  Tests: `unit/test_assignment`, `integration/test_central_assign` (incl. `VisibilityTest`).

### Alerting subsystems (state always written; paging per the allowlist)

- **Ports** (`central/ports.py`): monitored-only, admin-down silent; a port-down folds
  into the open outage via `stamp_outage_cause` COALESCE (never clobbers a post-mortem);
  SNMP never opens an outage. Bandwidth has floor AND ceiling, N consecutive walks, never
  judged on a down port.
- **Perf** (`central/perf.py`): median+MAD over a per-device ring buffer (NOT the hourly
  rollup — averages smear the slowdown); clears on hard-DOWN.
- **Redundancy**: `org_device_links` kind='backup' (cycle-checked); pages enter/leave,
  never opens an outage.
- **Rollups**: `analytics.device_reliability` pure outage math (UNREACHABLE excluded);
  `rollup.py` hourly buckets, 30 d.
- **Incidents** (`central/incidents.py`): outage waves × independent branches (roots
  judged against the FULL down set) × geography. ≥2 branches in 3 km = power; 1 =
  upstream; scattered = SILENT (no verdict beats a wrong one). ANNOTATION only — never
  mutes or reroutes a page.

### PON / ONU

- **Mass-drop verdicts** (`ponfault.py` math, `ponalert.py` shell): `last_online_at`
  freezes off-online, so "≥3 ONUs on one PON dark with recent last_online" IS the event.
  Gasp majority = POWER (recorded, never pages); LOS majority = fiber, cut bracketed in
  ranging metres (always a stretch). An OLT > 15 min stale is skipped (the ICMP outage
  owns that page). Transition-only. Hardware gap: C-Data EPON reports only
  online/offline, so power can't fire there and area cuts page as "fiber" (simultaneous
  multi-PON drops are the tell). `unit/test_ponfault`, `integration/test_central_ponalert`.
- **Roster hygiene** (`onuroster.py`/`onualert.py`): per-PON cap (`onu_pon_limit` per-OLT
  override set as "PON type" on the device form; **UNSET means the global cap, never 64**;
  any new `update_org_device` caller must carry the key or a GPON box silently drops to
  the EPON cap). Redundant MAC pages only when ≥2 are ONLINE (these OLTs keep every slot
  an ONU ever occupied; dead dups are history). Both read `current_roster` (freshest walk
  per OLT, 900 s) because `onu_optics` never deletes. **A stale OLT FREEZES its alert
  states, never clears them** — clearing on staleness re-paged 178 MACs per stall.
  `unit/test_onuroster`, `integration/test_central_onualert`.
- **ONU search**: `search_key` (registered SQL fn `wisp_search_key`) is SEARCH-only and
  must never replace `_norm_mac` — identity stays separator-exact or punctuation variants
  collapse into fabricated dup-MAC pages. Searches the CURRENT roster; 3-char floor.
  `integration/test_central_onusearch`.
- **GPON vendor auto-detects from sysObjectID; unmatched = optics OFF, never guess** (a
  fabricated dBm is the trap). Precedence: device `gpon_vendor` override >
  `WISP_GPON_VENDOR` > longest prefix > None. `unit/test_gpon`.
- **An ONU's identity is its SLOT, never its serial — on BOTH parse paths.** These OLTs
  never drop a vacated registration, so a serial key collapses rows and writes live ONUs
  dark. The serial is a fact to report about a row. Mapping a serial column is safe only
  once every probe runs a slot-keying build (`tools/gpon_enable_serial.py` version-gates
  it). `test_one_serial_on_two_slots_stays_two_rows`.
- **GPON profiles are DATA** (`gpon_profiles`, served in `/edge/devices`; closed
  vocabulary, whole profile rejected on anything outside it; same-name shadows a
  built-in). `set_profiles` MUST stay a fingerprint-gated no-op on an unchanged payload
  (rebuilding pollers churns SnmpEngines — the leak invariant).
  **`org_devices.gpon_vendor` validates against PROFILES, not the built-in list**
  (profiles are the vocabulary, built-ins its floor; a built-ins-only check 422'd every
  edit of a profile-vendored OLT). DISABLED rows count (a tombstone); the SPA keeps the
  device's current vendor as a dropdown item (a Select with no item for its value renders
  blank and saving unstamps it).
- **`packed_ifindex`**: a PON can live in the OID INDEX and nowhere else (Stelfiber
  STGP08X, PEN 50224: `chassis<<24|slot<<16|pon<<8|onu`). The strategy supplies BOTH
  halves (PON from the packed index, onu id unpacked). Its state column maps 0 → unknown,
  never offline (unregistered auth entries must not feed ponfault a fabricated cohort).
  Its per-ONU optics are indexed `<ifindex>.0.0` vs the roster's `<ifindex>` — joining
  them is a parser change, not a profile edit. `CentralProfileTest`,
  `tools/gpon_add_stgp08x.py`.

### Reference ONUs: witnesses replace dying-gasp

`onu_places`, `ponfault._witness_verdict`, `map/refonu.ts`. The operator marks the
subscribers known to run on UPS/solar/tower power; those witness the PON verdict. Exists
because the C-Data fleet reports neither dying_gasp nor los.

- **The power claim is an EXPLICIT TOGGLE, not placement** (2026-08-04; the original
  "placing is the claim" broke when a fleet surveyed its drops — every located customer
  became a witness). Placing/locating writes location only: `clean_onu_place_payload` has
  NO `witness` key (unsayable), the claim lives on owner-only
  `POST /api/inventory/onu-witness`, `set_onu_place(witness=)` is a required kwarg. The
  contract copy lives on the toggle and may not be softened. Lesson: an optional-but-
  preserved key was rejected — a route that CAN carry the claim gets wired back in.
- **Keyed on the MAC (`_norm_mac`), never `(device, onu_key)`** — slots rot (re-registered
  ONUs move). Normalized at exactly ONE write path. An RMA'd box orphans the row and is
  REPORTED (`matched:false`), never hidden.
- **Rules** (`WitnessTest`): a witness dark silently → `fiber` (evidence); every witness
  online AND reaching past the dark set → `power`, no crew. **A witness reporting
  dying_gasp counts in NEITHER tally** — hardware beats paperwork. `_reaches_past`
  compares ORDER only, never the unit (safe on time-quanta distances).
- `PonFault.evidence` is `witness | dying_gasp | silence` and the three must never render
  alike. Every ponfault caller passes `witness_macs` (count-agreement rule).
- **Map layer**: opt-in, subordinate by SHAPE, TONE and STACKING — never by size (drawn
  "smallest mark" twice, unreadable both times; marks match a device dot by AREA). The
  drop line is DOTTED (a logical association, not surveyed plant — crews quote drum off
  lines that look traced); the DASH carries the ranking, so dash periods scale with
  stroke width. Solid only under the cursor (`REF_HOVER_BOOST`, bounded, one line,
  narrated by the hover card) — the resting map may never look surveyed. Line tone
  follows the optical roster (`isRefDark`), not `port_state` (pin and line must agree).
- **The rate on a drop line is the ONU's OWN ifTable row, NEVER the PON aggregate**
  (`onu_if_token`, keyed on `if_name`'s first token — the alias mutates). A miss degrades
  to "no reading". The chip draws only when a rate exists (`refHasChip`); the collision
  budget reads the same predicate as the render. "No rate", "0 Mb/s" and "this firmware
  has no per-ONU row" are different sentences; `refHasRate` gates on port freshness.
- **Dark emphasis is for WITNESSES** (`isRefEvidence = witness && dark`) — the ONE
  predicate gating line tone/weight, name ink, zoom-floor exemption, chip priority and
  z-lift. An ordinary offline customer gets the red dot and nothing else (thousands go
  dark every evening; a wall of red is unactionable). `isRefDark` still answers counts.
- NOT a registry: ONUs are deliberately not `org_devices` rows (tree, list, fingerprint).
  Tests: `unit/test_ponfault:WitnessTest`, `integration/test_central_onuplaces`.

### Splitters & drops: the distribution network

`onu_drops` + `org_devices.split_ratio`, `central/drops.py` (pure math), `map/drops.ts`,
`splitter-panel.tsx`. Reality: OLT PON → feeder → splitter(s) → drop → ONU; a customer
hangs off the NEAREST splitter.

- The splitter chain already existed (passives are `org_devices` rows with parents,
  `pon_port`, routes) — this is one table (passive → MAC) + one column (ratio). Don't
  build a second topology.
- **Keyed on the MAC**, normalized at ONE write path (`clean_onu_drops_payload`). The PON
  is deliberately NOT stored (comes from the roster; a copy could disagree with the walk).
- **"Recorded" is NEVER "occupied"** — six drops on a 1:8 doesn't make two legs free;
  unknown is not spare. The one capacity claim an incomplete record survives is
  OVER-subscription. Same instinct as "nothing wrong" vs "nothing measured".
- `SPLIT_RATIOS` is CLOSED (2,4,8,16 — what the ISPs stock); `cumulativeSplit` returns
  null if ANY box in the chain lacks a ratio (a partial product UNDERSTATES the split).
  **`split_inputs` (1|2) is the OTHER axis, not a second ratio** — a 2:16 still splits 16
  ways; nothing multiplies it. NULL means ONE (the one absence-takes-a-default in this
  schema — every pre-existing splitter was already drawn 1:N); it's how the box was
  MANUFACTURED, never how many feeds are connected (that's `org_device_links`; PEERs
  excluded). A second input needs a ratio first; clearing the ratio clears it.
- **The ratio string is built in ONE place** (`drops.ts:ratioLabel`) — it was hand-written
  at eight render sites; `SplitRatioField` is ONE control across all four forms.
- **A branch fault names a SPAN, not a distance**: all recorded subscribers below one
  passive dark while a sibling branch stays lit ⇒ the break is the ONE span feeding it —
  beats the ranging bracket outright on this fleet. Self-limiting by construction (no
  "deepest wins" rule to get backwards). `MIN_BRANCH_DARK` = 2.
- **Detects and renders; never pages, never touches a ponfault verdict** — `drops.py` is
  imported by no alerting shell (structurally incapable). Keeps ponfault's refusals: a
  stale/down OLT is SKIPPED; a gasp majority reads power; a dark witness in the branch
  outranks it; a GASPING witness counts in neither tally.
- Unrecorded subscribers are never assumed lit or dark; every string says "recorded" and
  the layers menu states coverage (`N of M mapped`).
- **Rx is compared against SIBLINGS, never a modelled budget** (`OUTLIER_DB` 3.0) — ONUs
  on one splitter differ only by drop length; an absolute budget would be a guess wearing
  a decimal point. A uniformly low splitter is NOT a box of outliers — that's the feeder.
- Recording is BULK, one dialog per splitter ("which customers are on this box", asked
  once, standing at it). An ONU recorded elsewhere shows WHICH box — ticking MOVES it.
- Drops hang only off `PASSIVE_TYPES`; deleting a passive DELETES its drops (the
  subscribers live in the roster, untouched).
  `test_delete_cascade_handles_every_fk_table`.
- A passive stays QUIET until its subscribers aren't; the alarm is the FILL only — no
  halo, no pulse, no size change (a splitter is never down; its subscribers are). The pin
  runs both colour axes: quiet/ok on the plant identity hue, weak/dark take
  warning/destructive outright. Its plate carries the SPLIT RATIO, not its name (a box
  with no ratio keeps the name — the plate is one of only two channels telling plant from
  a customer).
- Drop lines are dotted and tighter than every other dash — **unless TRACED
  (`onu_drops.waypoints`), then SOLID**: tracing earns the surveyed stroke, and the two
  states may not look alike in either direction. ONE editor for link|drop spans
  (`RouteEdit`). Re-homing a drop DISCARDS its route (guarded on the passive actually
  changing, so the bulk dialog re-saves idempotently). Waypoints run splitter → ONU. A
  traced drop may be MEASURED (along-cable metres); an untraced one prints none.
- Tests: `unit/test_drops`, `integration/test_central_drops`,
  `integration/test_central_cableplant:DropRouteTest`.

### Field survey

`/survey`, `use-gps-fix.ts`, `field_location`/`field_passive`/`field_onu` routes,
`can_survey`. The worker geo-tags plant; coordinates only, the owner wires topology later.

- The worker role gains exactly two POST routes, safe by what they CANNOT do:
  `field-location` can't clear a pin, `field-passive` can't set a parent/IP/probe.
  Separate functions from the owner's route (whose contract includes both-null = delete).
  `can_survey` is its own predicate so it can't drift into `_can_write`.
- **A field-created passive reaches NO engine** (passives are excluded from
  `org_device_topology`, the single choke point) — recording one can never re-page a
  fleet. `test_a_field_passive_never_touches_the_engine_fingerprint`.
- **Provenance is the feature** (`accuracy_m`/`place_source`/`placed_by`/`placed_at`): a
  phone's first fix is a 30–80 m estimate, so a field capture and a surveyed point are
  different claims. `use-gps-fix` WATCHES and keeps the TIGHTEST fix (settles ≤8 m, gives
  up at 12 s, stops on unmount). A `gps` claim with no accuracy downgrades to `manual`. A
  desktop drag WIPES the stamp. A dragged pin records `manual` with NULL accuracy
  (`accuracy_m` means "radius this measurement is good to"; a hand-placed point has none).
- The adjust map is one marker, one job, satellite by default, folded (springs open on
  reopen); **any second Leaflet map MUST keep `attributionControl` ON** — `GoogleLayer`
  writes through `map.attributionControl`, which Leaflet only creates when the option is
  true; it fails ONLY for orgs with a working Google key, so local tests pass and prod
  breaks.
- **A poor fix is never a hard refusal** — past 25 m the button demotes to "Save anyway";
  only the absurd (>10 km) rejects. "Same spot as…" BORROWS exact coordinates (racked
  boxes are one point; independent fixes scatter them) as `manual`/NULL.
- Live write + audit trail, deliberately no approval queue (200 approval clicks kill a
  survey); the owner reviews in bulk off `placed_by`.
- **A worker on a phone gets `/survey` and nothing else** (`FieldShell`; after the
  billing-lock check). `use-mobile.ts` reads the viewport synchronously or the desktop
  chrome flashes first.
- **Locating is NOT witnessing**: subscribers land in `onu_places` via `onu-search`; the
  field route can neither create nor destroy a witness claim
  (`clean_field_onu_payload` has no witness key; `field_onu` preserves the flag).
  The MAC must be in the roster (404). The subscriber's NAME goes to `onu_places.label`,
  NEVER `onu_optics.name` (the walk rewrites that every sweep). Display order is ONE
  function — `onuroster.display_name` / `format.ts:onuName` — never a rule per screen (a
  name visible only where it was captured is indistinguishable from one never saved); the
  row CARRIES the label via `_LABEL_JOIN` (`_norm_mac` registered as a SQL fn so SQL and
  Python identity can't drift), and BOTH search halves match it. Labels stored UPPERCASE
  at one write helper.
- **Renaming is its own route** (`field-onu-name`) — re-placing would restamp a real GPS
  fix as hand-placed. NAME + NUMBER + LOCATION are captured together or not at all
  (server-enforced; the phone rule is looser than the WhatsApp E.164 rule — it's a number
  a human dials). The desktop reference dialog keeps both optional (its write means the
  power claim) and never erases field-captured values (COALESCE).
- A placed subscriber must be REACHABLE: the Layers entry is "Subscribers" with a count;
  `/map?onu=<MAC>` (a query param — survives reload) enables the layer, flies past the
  zoom floor, selects the pin. Zoom floors are SPLIT: marks 14, lines+chips 16, names 17
  — and all are superadmin **Map detail** settings (`central/mapdetail.py` +
  `map/detail.ts`): one config for the install, riding `/api/orgs`; defaults mirrored in
  Python and TS (`SpaAgreementTest`); per-field coercion; **nothing draws at a zoom where
  its own mark doesn't** (dependent floors repaired on write AND read). A density ask is
  a dashboard control, not a code edit.
- **Once a fleet surveys in bulk, an OLT/PON FOCUS scopes the layer** (`onuScope`):
  a SET of PONs (empty = all), separate from the layer toggle, bypasses both zoom floors,
  **never touches the zoom** (a filter thins what's drawn, it doesn't re-frame; pans at
  current zoom only if the focus reveals nothing on screen), announced in the status
  strip (z-1002, above the floating cards). PON chips come from PLACED subscribers, never
  the OLT's PON list. The focus narrows PASSIVE PLANT with the drops (`plantInScope`):
  keep a box a drawn subscriber hangs off, else the feed chain from the scoped OLT; a box
  with blank `pon_port` STAYS under a PON pick (nobody wrote it down ≠ another PON); gear
  is never narrowed.
- Map search finds subscribers (placed set client-side + roster debounced); an unplaced
  hit names its OLT and STOPS — placement writes happen where the contract is stated,
  never as a search side effect.
- `onu_places.witness` ships as a real BOOLEAN (SQLite 0/1 breaks `w === true` and
  renders literal "0"s). Coverage is per-OLT with the roster as denominator; the opened
  OLT lists the located half first (one roster pass, done rows carry label/phone/pin so a
  tap defaults to RENAME, done rows are their own block).
- Tests: `integration/test_central_survey`.

### Worker location tracking

Workers run **Traccar Client** (OsmAnd protocol) → public `GET|POST /field/track`;
`central/field.py`, `map/workers.ts`. No APK work — off-the-shelf beats ours
(Doze/OEM tuning, iOS free). Don't re-propose a tracking APK.

- **On-shift only**; the tracker's own switch is the real toggle plus an explicit
  Start/End shift button — when somebody is on-shift and no fixes arrive, that gap IS the
  "OEM battery manager killed the service" alarm. Nothing may infer a shift from fixes.
- Auth: per-worker token in Traccar's `id` field (hash-only, shown once, rotatable) — one
  server URL for everyone. Revoked token / deactivated account resolves to nothing.
- **A refusal that is OURS answers 200, not 4xx** — Traccar re-sends in order, so a 4xx
  on a fix we'll never accept WEDGES the offline buffer. Too vague / too old → 200
  `{stored:false}`; malformed 400s; bad token 401 (right to wedge); rate cap 429s. The
  rate cap is a token BUCKET (a minimum gap would discard the buffered burst);
  `UNIQUE(org,user,ts)` + INSERT OR IGNORE makes replay idempotent.
- Speed arrives in KNOTS, converted once at ingest. 7-day retention, pruned daily (the
  window is the answer to "what does this keep about staff"). Ungated by billing; not an
  `/api/*` route; nothing logs the request line (token in the query string).
- **Four states, never collapsed** (`workerState`): live / quiet (THE alarm) / off /
  never (a count, not a mark). Classified in the SPA (freshness ticks with the clock);
  threshold server-owned; the panel reads the SAME function as the map.
- Layer discipline: opt-in + remembered, out of clustering, below every device pin;
  rounded square with INITIALS (text is what stops a person reading as plant); no status
  tones except `quiet`. States differ by HUE (survives satellite tiles); the trail is
  SOLID (the one measured line) with the standard dark casing.
- "Today" is the operator's day (`WISP_DISPLAY_TZ`), not UTC midnight.
- `/api/field/shift` is the only worker-writable route this adds (idempotent both ways);
  workers/tokens routes are owner-only. Setup panel = steps an owner reads out + the OEM
  autostart warning (the single most likely "it looks broken"). Before fleet rollout: one
  full day on a Xiaomi/Realme handset. Tests: `integration/test_central_field`.

### NVR / CCTV

Cameras : NVR :: ONUs : OLT — a roster (`nvr_channels`), never `org_devices` rows, read
off the NVR's own HTTP API through the web-proxy tunnel (no edge code). `nvr_profiles` is
the fifth recipe table (same rules); built-ins `cpplus`/`dahua` = Dahua CGI + digest auth.

- Own 300 s clock (`WISP_NVR_INTERVAL_S`, ≤0 disables) on the SAME WebOpticsSweeper
  instance (shared per-device locks + browse/tunnel gates). Targets = device_type='nvr' +
  declared `nvr_vendor` (NVRs walk no SNMP, so no detection channel) + creds + tunnel.
- **States come over RPC2** (`state_format: "rpc2-camerastate"` — the same source the
  NVR's own UI draws from, so it cannot disagree with it). **VideoLoss/VideoBlind event
  queries are a TRAP**: unarmed detection answers "No Events" with cameras dead. This
  build's dead-camera word is `Unconnect`; an unmapped state word is NAMED in
  `nvr_status.detail` so the next firmware self-describes. Channel numbering: 0-based in
  config, 1-based in `snapshot.cgi`.
- Identity is the CHANNEL slot; IP is a fact about the row; absent state = `unknown`
  never offline; roster PRUNED on a complete read (config, not a learned table — the one
  deliberate divergence from `onu_optics`).
- **Paging is ON** (CAMERA_DOWN/RESTORED), storm-defused: transitions batch into ONE page
  per NVR per sweep; unknown pages in neither direction; a failed read pages nobody
  (paging keys on transitions between successful reads). Per-camera `monitored` toggle is
  an operator column the sweep never writes; defaults ON (the mute, not the enable); an
  unwatched dark camera renders muted with a chip, drops out of `cameras_down`.
- `camera_down` is an `/issues` KIND; `store.dark_cameras` mirrors `down_ports`' gates so
  the Home tile equals the list length by construction. Tree chip + map ring count
  WATCHED dark only.
- Snapshots: one JPEG through the tunnel, `no-store`, never persisted; fast-5xx retries
  ×3, a TIMEOUT is never retried; some camera models simply don't serve ONVIF snapshots
  (stated in the dialog, not our bug). NVR web click-through hits the documented
  prefix-tunnel limit (root-absolute module graph) — don't retry cosmetic fixes.
- Tests: `unit/test_nvr`, `integration/test_central_nvr`.

### C-Data / DBC hardware truths (don't re-derive)

- **Per-ONU Rx exists NOWHERE in that EPON firmware's SNMP** (proven by warm capture +
  exhaustive re-sweep; the one optical column is the OLT's burst-receiver level). The web
  OPM-Diag page is the only source. Tool for the next vendor: `oidhunt.py`.
- **`distance_m` on the `dbc` profile is RTT in EPON time quanta**, not metres (true
  metres = `1.6393 × TQ − 157`) — every printed cut bracket is ~39% short and crews quote
  drum off it. `scales.distance = 1.6393` is the zero-rollout approximation; do the scale
  fix BEFORE merging the scrape's exact metres (metres-for-survivors + quanta-for-dark
  INVERTS the ponfault bracket).
- **The Syrotech GPON build publishes NO ranging distance at all** (the whole ONU MIB is
  mapped; the zeros are unpopulated counters — mapping one prints "0 m" = unranged).
  Same class of verdict: a hardware fact, not a gap to code around. Its roster/Rx/serial
  ARE in SNMP at `37950.1.1.6` — the opposite verdict to EPON; never carry either across.

### Per-ONU Rx from the OLT's web UI (weboptics)

`central/weboptics.py` + `weboptics_sweep.py`. Central-only: rides the web-proxy tunnel —
no edge code, no rollout.

- **The scrape is an INPUT to the optics fold, never a second pipeline**: readings land in
  `onu_web_optics` (own table, own clock) and `_merge_web_optics` folds them in BEFORE
  `sync_device` — severity/badge/ponfault stay ONE path that never learns the source.
- **Merge by MAC, ONLINE slots only, ambiguity DROPPED** (a reading pinned to the wrong
  drop sends a tech to the wrong house). Matching is punctuation-blind (`_match_key`, a
  THIRD normalizer beside `_norm_mac` and `search_key`, deliberately). SNMP stays
  authoritative for the roster: a scrape can never add an ONU or blank a walked value.
  Freshness judged on CENTRAL's clock; past 1 h readings drop whole.
- `distance_m` is stored but NOT merged (unit mix inverts ponfault brackets).
- **The sweeper's restraint is the feature**: 900 s clock, sequential, per-OLT lock, skips
  dormant tunnels and any OLT being browsed (one web session slot — scraping mid-browse
  logs the operator out), handed NO notifier. The browse gate keys on ACTIVELY browsing
  (`idle_s` + `reap_expired` + tab-watch), not "has a session" — an abandoned tab once
  suppressed a whole node's optics indefinitely.
- `POST /api/inventory/rx-refresh` (owner-only) drives `scrape_one` on the SAME sweeper
  (lock shared); eligibility has ONE source (`WebOpticsSweeper.target()`), answers at once
  and scrapes on a thread; a second click 409s.
- **Eligibility is the VENDOR PROFILE, not the dropdown**: `web_optics_targets` accepts an
  explicit vendor OR the edge's reported sysObjectID detection (the stronger signal; a
  hand-typed gate once kept the subsystem to 3 of 13 eligible OLTs). `sysobjectid`
  non-empty is load-bearing (an env override could launder itself into a "detection").
  **PON count comes from the ROSTER** (`pon_indices`; junk labels dropped, never guessed)
  — the same firmware ships 3–8 PONs with gaps, and a fixed (1,2,3,4) skips half the
  fleet while logging success.
- **A sensor rail is not a reading** (`_sane_optics`): dead DDM prints the raw register on
  every field at once (0xFFFF → +8.16 dBm grades `ok`; 0x0000 → −40 grades `crit`).
  Supply VOLTAGE is the discriminator (3.3 V by design, known a priori); outside 2–5 V
  the whole optical block blanks. Rejects RAILS, never merely-bad optics.
  `expect_voltage` comes from whether the profile maps a voltage column. `DdmRailTest`.
- **The login-page GET is a GATE, not a preamble** — a non-OK reply aborts with
  "credentials NOT sent"; a 404 on the optics path is "this firmware has no OPM page",
  not a retryable fault. `web_optics_device_budget_s` bounds one OLT's whole scrape.
- **The vendor recipe is DATA** (`web_optics_profiles`, third profile table, same rules).
  Columns map BY HEADING (position-mapping reports Tx as Rx, a confident lie); the
  session is a FLOW (`rotating-key`|`cookie`); charset per-vendor; **a profile may never
  carry a host** — path-only is what stops the tunnel being a lateral-movement primitive.
- **Outcomes are PERSISTED** (`web_optics_status`, closed vocabulary ok|partial|skipped|
  no_profile|no_credentials|unreachable|login|error; `last_ok_at` survives failures) —
  "no Rx", "no password stored" and "failing for a day" render identically as a blank
  column but take opposite actions. The SPA composes the sentence from facts
  (`rx-diagnosis.tsx`).
- Next vendor page discovery: `proxy_audit` records every path a human opens — have the
  operator click the optics menu once; never guess a page (a guessed page that parses is
  how a fabricated dBm ships).
- Tests: `unit/test_weboptics*`, `integration/test_central_weboptics`,
  `integration/test_central_rxstatus`.

### The USER MAC (stage 1 of RADIUS)

`central/webmacs.py` + `webmacs_profiles.py`, same sweep shell. **Not the ONU's MAC** —
the subscriber's own router's address, which RADIUS is keyed on.

- **1 GET per OLT, not one per subscriber** (the audit showed 739 hand-clicks on the
  per-ONU page; per-ONU fetches are the load shape that overran these boxes).
- **`Port ID` is the join and it is the SLOT** (`EPON0/8:38` / `PON2:ONU36` → `onu_key`);
  measured 100% join on both builds. `port_shape` is a CLOSED vocabulary — a permissive
  regex attributes one customer's router to another.
- **The recipe is its OWN table** (`web_mac_profiles`), not a field on the optics profile
  — the Syrotech build serves this page and provably has no optics page. Login goes
  through the ONE `weboptics.login` implementation.
- One sweep, one login (the OLT holds one session slot) — the address read is a second
  page inside `scrape_device`; every gate taken once. A mac-profile-only device still
  reaches the read.
- **The truncation guard is the OLT's own count** where it exists (`macCount`); the build
  with no total gets `complete = None`, never True — "we cannot tell" is a third answer.
  A short read records `partial` naming the shortfall.
- Uplink rows (GE/CPU/PON aggregates) are DISCARDED, not attributed. One slot may carry
  several MACs and ALL are kept (picking one is a guess); the VLAN rides along.
- **Rows are never deleted, only re-stamped** — it's a LEARNED table that ages out; the
  stale row is the one reading MORE useful when the customer is down (it's what they type
  into RADIUS). So the panel row sits OUTSIDE the frozen block, each row dated.
- Tests: `unit/test_webmacs`, `integration/test_central_usermacs`.

### RADIUS: the billing book joined to the network

`central/radius.py` + `radius_profiles.py` + `radius_sync.py`; `radius_accounts/
customers/links/status` tables; the fourth recipe table (closed vocabulary, heading-
mapped columns, org NULL = global, disabled = tombstone).

- **The join is the feature and it was measured first**: MAC outranks name (observed
  traffic beats a provisioning string); the name compare is punctuation-blind
  (`search_key`), the MAC goes through **`webmacs.normalise_mac`, the same function that
  stored the other side**. **An ambiguous match links nothing** (wrong-house failure).
  Resolved once at sync into `radius_links` (auditable `match_by`), never joined on the
  fly — `list_org_devices` is the hottest query and stays untouched.
  `unit/test_radius:LinkTest`.
- Expired customers being ~9% MAC-reachable is CORRECT (aged out of a learned table) —
  don't widen the match.
- **Identity ranks: label > radius_username > radius_name > walked name > serial > key**
  — mirrored in `format.ts:onuName`. **The USERNAME is the identity, the customer name is
  extra info** (the ISPs' own call; usernames are unique and universal, names are often
  one lowercase word). A username renders MONO and VERBATIM (a key you retype, never
  case-folded); `onuSubName` never repeats the headline (search-key equality). The row
  CARRIES both columns via one grouped CTE pass — the correlated sub-select version cost
  721 ms on the map's places read; the CTE is ~32 ms. Ambiguous MAC yields NULL, never a
  pick. `PlaceIdentityTest`.
- The panel's lat/long columns are JUNK (two distinct points for a whole ISP) — nothing
  here writes `onu_places`; the survey is not replaceable.
- Expiry/balance stored as the panel's own STRINGS; parsed only under a profile-declared
  `date_format` (blank = stays unparsed; an impossible date parses to nothing).
- **A profile may never carry a host — the ACCOUNT does** (`base_url`, bare server only).
  Central talks to the panel DIRECTLY (public internet, pure stdlib urllib + cookie jar);
  dormant until an account row exists.
- **The login-page GET is a gate**; login success judged by whether the EXPORT came back
  as CSV. **`forbidden` is its own state** (signed in, export 302s to a not-allowed page
  — reporting it as `login` sent an ISP to change a correct password). Outcomes persisted
  (`radius_status`, closed vocabulary + `partial` naming missing columns).
- A customer missing from one export is KEPT, never deleted; links are REPLACED wholesale
  each sync (a link is a claim about now).
- **Many panels per org**: accounts are SOURCES, order (= id) is priority.
  **The link pass is org-wide** (`relink_org`) — per-panel linking would delete the
  sibling's links every sweep. **Linking reads only each panel's latest read**
  (`seen_seq`, a COUNTER not a timestamp — two syncs in one second tie). Two panels
  claiming one MAC settle by order (same person, same slot — refusing would drop a real
  subscriber); two customers on one MAC inside one panel still link nothing.
- **A new login flow is part of the recipe** (`login_flow` form|encrypted-nonce; OneRadius
  mints a one-time enckey and AES-encrypts credentials — `central/webcrypto.py` is the
  ONLY cipher in the codebase, pure-stdlib AES-CBC + CryptoJS envelope, pinned to
  published FIPS/NIST vectors, written for clarity not constant time; `secretbox.py`
  encrypts what WE store — don't confuse them).
- **A MAC cell is not always one MAC** (`mac_field`: split, normalise, one distinct MAC or
  none — unconditional, not a profile knob). **An aggregate port is not a subscriber**
  (`MAX_SLOT_MACS` 128; one slot with 21k MACs is a trunk, measured 3 orders above the
  largest legitimate slot).
- Onboarding the next ISP = a profile row + an account row + MEASURING the join first.
- Settings → Monitoring → Billing/RADIUS panel card (owner, org-scoped): brand dropdown
  from the server's profile list; password write-only; status chip composes the sentence
  from `radius_status`.
- Tests: `unit/test_radius`, `unit/test_webcrypto`, `integration/test_central_radius`.

### The customers page

`central/customers.py` (compose-only) + `/api/inventory/customers` + `customers-page.tsx`.
Directory + fault triage, NOT a mirror of the billing panel.

- **Owner-only on BOTH layers** (route + `_can_write` + nav `ownerOnly`) — the full book
  with phone numbers is the largest PII surface; workers keep the per-subscriber panel.
- `net` is a closed vocabulary (online|dark|frozen|stale|unlinked) and keeps the frozen
  rule: a customer behind a down/stale OLT reads `frozen`, never `dark` ("6 of 6 dark"
  behind a down OLT is the OLT's outage restated).
- An unmatched customer carries a PROVABLE reason only (`no_mac`/`mac_unseen`/
  `mac_unresolved`/`gone`); a row outside its panel's latest read gets `gone` and NEVER a
  MAC reason (check `in_last_read` first — 340 rows once blamed MACs that pinned fine).
- **One book's absence is evidence; a sibling's is not**: a username CURRENT in another
  enabled panel folds the stale twin away (displayed rule only; row kept; fold COUNTED —
  the one count the SPA cannot recount; current-in-both stays two rows). The unmatched
  chip is ACTIVE-only (expired customers CANNOT match by design; counting them buried 57
  actionable rows under 1024).
- Every chip count is a recount of the rows it filters to (the /issues rule). Sort orders
  by the PRINTED number; expired sinks below live.
- The subscriber panel composes ONE identity (`IdentityRows`: per-fact provenance; two
  differing phones BOTH render tagged field/billing) — `BillingSection` keeps only what
  billing alone can know.
- Test-fixture traps, both paid for: stamp fixtures at CALL time, never import time
  (discovery imports everything up front → everything reads frozen); one
  `upsert_radius_customers` call is ONE read (writing one-at-a-time marks all but the
  last "gone").
- Tests: `integration/test_central_customers`.

### Topology extras

- **Passive plant lives in org_devices** (`PASSIVE_TYPES` = splitter/closure/fdb;
  `coupler` lingers unused because removing a type promotes surviving rows to gear that
  can page). Containment is `org_device_topology` — the single choke point (engine, the
  rebuild fingerprint, `/edge/devices`) — so adding a splitter can never re-page.
  Validation rejects an IP/probe on a passive and a passive PARENT for a monitored device.
- **Peer links are a KIND, not a second graph** (`org_device_links` kind='peer',
  canonicalized (min,max)). Invisible to the engine by construction (every dependency
  read filters kind='backup') — declaring cabling can NEVER re-page (`test_central_
  peerlinks`). Deliberately no cycle check (a ring IS a cycle). Descriptive only; real
  failover is a declared backup uplink.
- **Port-level links live ON switch_ports** (`feeds_device_id` parent side,
  `uplink_device_id` child) — one registry so bandwidth labels and outage folding can't
  disagree. Operator columns; the walk upsert deliberately omits them. The Uplinks picker
  is collapsed by default (cabling is reference, and nothing alarm-shaped hides in it).
  `integration/test_central_linkports`.

## Device web-UI proxy (reverse tunnel through the edge)

Browser → central → edge → device. The edge stays DORMANT until a `/report` reply carries
a live session. Activation is per-org `orgs.web_proxy` (superadmin); `WISP_PROXY_ENABLED`
is only the kill switch.

- Diagnosing: **504 = the edge never picked up; 502 = the edge fetched and failed.** Many
  OLTs refuse :80 — create the session with `port: 443`.
- Request headers forward on an allow-list: device cookies travel, central's session
  cookie stripped, Referer/Origin rewritten to the device origin, Authorization only for
  Basic.
- **The autofill bootstrap must never be spliced into JavaScript** (`_injection_point`):
  insertion point is the last `</body>` OUTSIDE any `<script>`, and the payload must
  start with `<` (old firmware serves JS with no Content-Type and the doc sniff matched
  it). The failure is invisible in `proxy_audit` — every request still 200s.
  `InjectionPointTest`.
- Escape rescue: an unknown root-absolute URL whose Referer names a live session 307s
  back inside the prefix. Wildcard-host is the real fix if this keeps biting.
- One tunnel per probe (session_create replaces); owner-only, org-flag gated, every path
  audited to `proxy_audit` (which doubles as vendor page discovery).
- Known limit: long-polls consume central worker threads — dormant-until-requested is
  what keeps this bounded; don't make it always-on.

### Tunnel latency (don't undo these)

The tunnel once opened a fresh TCP connection per asset and weak boxes couldn't accept
them that fast (proven: every 502 was a connect timeout = overrun accept queue; the 1.00 s
median was one SYN retransmit). **The lever is concurrent connections per device**, not
timeouts.

- **Central keeps a per-session static-asset cache** (`AssetCache` on the ProxySession —
  dies with the credential, no cross-session key). Stores the device's RAW reply (one
  rewriting path, `_finish`); query string part of the key (the vendor's own `rand=`
  busting keeps working); `_CACHEABLE_EXT` is a CLOSED list (this vendor's dynamic pages
  are `.html`); `cache_refusal` refuses anything carrying state. Checked BEFORE the
  in-flight ceiling; hits still audited. `no-cache`/`private` deliberately DEFIED
  (per-session in-process cache; no validators exist), `no-store` honored; jQuery's own
  `_=` buster is stripped from the key, the vendor's `rand=` is not. Refusals logged once
  per (session, reason).
- The edge reuses ONE httpx client per (scheme, ip, port) (`_ClientPool`); a device
  closing a pooled connection costs one silent retry — **GET/HEAD only** (a dead POST may
  have been applied).
- **Per-device concurrency is an adaptive ladder on BOTH sides** (4 → 2 → 1 rungs;
  central's ships without a rollout, which is why it exists there). Drop a rung only on a
  CONNECT failure; re-probe faster every 3 h. **The ladder FLOORS AT 2, measured**: 1 was
  the worst of the three (the tunnel is a pipeline; serialising pays dead air per asset).
  Central's gate is inside `ProxyHub.submit` (every caller bounded; a gate a caller can
  forget is not a gate), a Condition not a Semaphore (capacity moves). Central classifies
  the edge's failure PROSE (`is_connect_failure` — cross-module string coupling pinned by
  `ConnectFailureWordingTest`); TLS mismatch / non-HTTP replies are NOT capacity signals.
- Every slow asset logs WHERE the time went (`queued` = tunnel cost, `edge` = device
  fetch) — the two cures are opposite and guessing wrong made it worse once. Failed
  fetches log one line on central.
- Tests: `unit/test_webproxy`, `integration/test_central_proxy`.

## Central management plane

- **`org_devices` is THE device table** — don't reintroduce a second registry. `events`
  survives (central-originated log lines only).
- `central/inventory.py` is pure validation. Every `org_devices` write re-derives org
  from the DB row (`store.device_org(id)`; body org trusted only on create).
- New columns on existing tables need `_ensure_columns`; new tables need nothing.
- **Schema edits on this box**: the release-sync timer used to migrate the live DB within
  15 minutes of SAVING store.py; that footgun is defused (`sync-releases` opens with
  `migrate=False`) — but the running process still holds old code, so **restart central
  in the same breath as any schema change**, and back up first.
- `make_server` takes an injectable notifier — tests inject a recording double.
- **Routes live in `central/api/` route tables**, not server.py if/elif chains. Adding an
  endpoint = a function + a table row. `api/common.DENIED` is the "403 already sent"
  sentinel (don't test scope helpers with `if not org` — superadmin yields org=None).
  **A duplicate key in the route-dict literal loses SILENTLY** — `unit/test_routes`
  parses the SOURCE and fails on any repeat (it happened; the new route answered
  somebody else's handler through review and typecheck).
- **Every JSON API reply is `Cache-Control: no-store`** — with no header a browser can
  serve a stale 200 body without asking (an ONU search once pinned an empty reply to its
  query string). Freshness is react-query's job, never the HTTP cache's.
- **Deleting an org sweeps DISCOVERED tables (introspected for `org_id`), `org_devices`
  LAST** (FKs point at it; org ids are reusable, so a hardcoded list orphans rows into a
  later org of the same name). Superadmin-only, gated on an ECHOED org id server-side.
  Deliberately no tombstone (`_ensure_org` IS probe bootstrap; the dialog says uninstall
  the probe). `integration/test_central_orgdelete`.
- Superadmin Overview = coverage, not alarms; never pages.

### Roles: owner and worker (only)

- `auth.ROLES`. Worker scope is ONE choke point (`_WORKER_ROUTES`): a new `/api/*` route
  is worker-blocked by default. That's the ROUTE layer; `visible_device_ids` is the DATA
  layer under it. Both apply.
- **Every worker check gates on IDENTITY before role, server AND SPA** — a superadmin is
  `org_id IS NULL` and its role is meaningless; the role collapse once flipped the
  superadmin to worker and locked the platform admin out. `create_user` forces owner for
  org-less accounts; `_collapse_roles` spares and repairs superadmins. Pinned by three
  tests (`test_superadmin_is_never_worker_blocked` etc.).
- Ack/post-mortem rights = `can_triage` (its own predicate — write rights stay
  owner-only). Workers get the full shell; `/issues` + its exports are worker-readable.

### Billing / payments

- `central/billing.py`: plans free/pro/vip; paid months in `org_billing_months`
  (pre-marking future months IS the "no reminder" switch). Unpaid current month 402s on
  every `/api/*` except `_BILLING_EXEMPT`; **edge ingest, monitoring and paging are NEVER
  gated** — a lapsed bill must not silence an alarm. Device caps enforce on CREATE only;
  passives never count. Free never locks.
- **Payments are manual** — GPay number/QR + "I've paid" ping to the admin number; the
  admin marks the month by hand. No ledger, no gateway (two were built and removed).
  `POST /api/billing/plan` accepts only 'free'. Tests: `unit/test_billing`,
  `integration/test_central_billing`.

## Dashboard (web/ → central/static/)

Built output is committed; `./run.sh` needs no Node.

- `/` = marketing landing (source `web/public/landing.html` — edit there, never the built
  copy; its page HTML is a JSON string in a script tag, see the escaping trap in git
  history), SPA at `/app`. **HashRouter** (the server 404s non-file paths).
- No frontend test suite — verify via `tsc -b`, `npm run build`, manual Playwright.
- Mockup-only fakes — don't "finish" them: Clients online, manual Resolve, Docker
  install, Notification history.
- **The SPA keeps itself fresh**: a window-focus `/api/me` probe (react-query v5 misses
  window-behind-window), a dying SSE stream probes auth (an EventSource can't see WHY it
  died), and `event: build` announces the served bundle (re-read off disk, mtime-gated —
  `npm run build` deploys with no restart, so a startup latch would announce the old
  build forever); the client compares its own script tag and reloads WHEN SAFE (one
  auto-reload per build id; `vite:preloadError` guards deleted lazy chunks).
  `test_central.py` build-cache tests.

### Theme & palette

- Minimal-gray, dark default, warm slate; surface steps + borders, never shadows;
  desaturated accents so STATUS COLORS STAY LOUDEST. Type scale is rem-only (13 px floor
  — operator's call; **Inter Variable, never Geist** for UI text). A resolved outage
  pending post-mortem renders NEUTRAL, never green.
- **Colors are operator-settable** (Settings → Platform → Appearance →
  `app_settings.theme_overrides`; `central/theme.py` validates, injected as a `<style>`
  at the end of head). **Check whether an ask is just this panel before touching
  index.css.** Storage is a SPARSE DIFF, never a snapshot ({} clears; absent key = leave
  alone). The UI edits ~7 seeds; `readableInk` picks foregrounds by contrast measurement.
  `theme.py:_TOKENS` ↔ SPA `ALL_TOKENS` are pinned by `test_allowlist_matches_spa`; the
  allowlist + value regex are a security boundary (values land in a style block),
  re-applied on READ.
- **Mode scoping broke dark mode twice**: preview must be CSS in a `<style>` (inline root
  styles outrank everything and carry no mode), and injected selectors must be
  `:root:not(.dark)` / `:root.dark` (the plain pair ties at (0,1,0) and source order
  lets light win in dark mode). Pinned by two tests. Verify palette changes in a REAL
  browser in BOTH modes — both regressions passed static review.
- Load-bearing measurements: `--primary-foreground` is DARK ink (white on the steel blue
  fails AA); borders are ALPHA not hex (one token holds on every surface + raster
  tiles); `--muted` ≠ `--background`; a further "darker" ask raises card with the canvas.
- **Surface ladder**: `--muted` recesses, `--popover` is THE raised surface, `--accent`
  is the interaction fill; row hover = `hover:bg-foreground/5`. The ≥3 ΔL* floor is
  measured on ADJACENCY (what shares an edge), not a sorted token list. Selection is a
  STATE of the row, not another layer: no outline, no inset edge, a CHROMATIC lift
  (`--selected` carries the accent hue at similar lightness) — a brightness lift reads
  as a new surface. The accent has exactly three jobs: focus ring, selected row, active
  nav.

### The two colour axes

- **Axis A — STATUS** (red/amber/green/info): reserved, supreme. **Axis B — IDENTITY**:
  five measurement planes (`lib/planes.ts`, `--plane-*`: optical 200° · traffic 247° ·
  vitals 273° · plant 299° · fleet 325°) — constant, never alarming. The schema already
  models them (four freshness stamps, three walk clocks, the panel tabs).
- Reachability gets NO hue — it IS Axis A (a plane earns a hue only for facts that
  survive health).
- Fenced mechanically: hue inside 200–330°, never 20–140°, ≥18° off `--primary`; chroma
  ≤55% of the QUIETEST status tone; contrast in [3.0, 80% of that tone]. **An identity
  hue is a MARK colour, never a TEXT colour** (light mode's ceiling lands below AA for
  text): a status chip is coloured text, an identity chip is neutral text beside a
  coloured dot.
- NOT operator-settable (an encoding solved against a budget); `--chart-1..5` MIRROR the
  planes and ARE settable. `toneFamily` no longer takes a chart key — charts must never
  draw series in alarm colours again.
- Identity never goes on the left rail (that rail carries the operator's tag/probe colour;
  two meanings on one rail is the documented Datadog failure).

### Instrument grammar

- **`<Reading>` is ONE form for every number that can be uncertain** — five states, each
  with a non-colour channel: current · stale (dotted underline) · frozen (desaturated +
  pause + reason) · absent (the DEAD ZONE — a dash on a hairline track holding its
  column: "we looked and this instrument cannot answer", the OTDR concept) · suppressed
  (struck bell — a suppressed ALERT must never look like a suppressed FACT). `at` derives
  the state and does not print; frozen is the only state that surrenders its status tone.
- `RxScale` is a POWER METER, not a progress bar: domain is the DECISION BOUNDARY
  ([crit−3, warn+3]), healthy PEGS, `ok` gets no band, thresholds threaded per-OLT, no
  upper bound (this product models none — a scale that disagrees with what pages is
  worse than incomplete).
- `OnuBar` = the PON heat strip at aggregate resolution (proportional segments; offline
  takes the MUTED step, never destructive).
- Status tone is ONE formula (`status-badge.tsx`: tone text, 13% fill, 30% edge). `info`
  is a real tone (an ACKED outage is a human owning a live incident).
- **Operator colours are ONE closed vocabulary** (`lib/palette.ts` ↔ `inventory.PALETTE`,
  shared by map links, TAGS, PROBES; a free hex lets an operator fake an alarm —
  `test_central_colors`). Sparse storage keyed on TEXT; first-tag-wins then probe; renders
  as a left rail; status always wins. Palette members are STROKE colours (`.wisp-tag`
  mixes toward black on light). New colour-coded thing = a new `kind`, never a new palette.
- Five `wisp-*` primitives (`.wisp-panel/-head`, `.wisp-row`, `.wisp-eyebrow`,
  `.wisp-thead`, `.wisp-frozen`) — don't re-copy their Tailwind strings per page. Page
  measure is TWO widths (`.wisp-page` 105rem, `--narrow` 66rem) — don't add a third.
- **No prose em-dashes in user-visible copy** (the loudest "AI copy" tell) — periods,
  colons, `·`. Code comments and CLAUDE.md keep theirs. The bare `—` as an empty data
  cell STAYS (a null marker, load-bearing).

### Honesty rules

- **A DOWN device's SNMP readings are FROZEN and must look it** (`isDownState`, not the
  900 s rule — unreachable is proof the data is stale before staleness notices). Three
  layers: the panel GRAYS (`.wisp-frozen` on the subtree, always with a live reason
  outside it), the tree row SUPPRESSES chips (a chip is a claim about NOW), the summary
  EXCLUDES (`_bandwidth_alarms` drops DOWN/UNREACHABLE). Alarm STATE and PAGING untouched
  at every layer. `test_bandwidth_summary_hides_alarms_on_an_unreachable_device`.
- **"Nothing is wrong" and "nothing is measured" must never render alike** — a C-Data OLT
  walks a complete roster with every rx NULL and used to render like a healthy fleet.
  One number (`onus_rx`) drives the em dash + "no OLT reports dBm" on Home, the coverage
  Gauge icon (green only when measured AND fresh, never severity-tinted), and the
  Optical tab's explanation.
- **"This OLT doesn't report Rx" was a guess stated as a hardware fact and is GONE** —
  `rx-diagnosis.tsx` composes the real reason from `/api/inventory/rx-status` (the
  server ships facts, the SPA writes the sentence). It must never trigger a scrape.
- A dBm on screen carries no date — `RxFreshness` gives the web read its own stamp
  (read / failing-since / never, never collapsed); the refresh button draws off the
  server's `can_refresh`.
- Optical drill-down degrades, never dead-ends (roster in slot order; 0 m renders "—" =
  unranged). `OnuRow` is one column per fact — no cell stands in for another.
- The Network tree's shape is a VIEW (`tree_detached`) — read only by `list_org_devices`,
  never topology, so lifting a row can never re-page (`test_central_treedetach`). A
  lifted row renders its parent's name; the chevron gates on children actually emitted.

### The issue plane (`/issues`)

- ONE ROW PER PROBLEM (port, ONU, PON, probe, camera) — a switch with four dark ports is
  one tree row and four jobs. Read-side only; writes nothing, pages nobody.
- **The tile's count and the list's length MUST be the same number** — `issues.collect`
  re-derives NOTHING; it composes the same store reads and gates the tiles use.
- `KINDS` is CLOSED, one kind per tile (pon_fiber ≠ pon_power — one merged kind makes the
  chip exceed the tile). An unknown `?kind=` shows the WHOLE list, never an empty one.
  A port-down on an unreachable switch is KEPT but demoted to info + "reading frozen"
  (count-agreement vs honesty, both).
- **The PDF is server-rendered pure stdlib** (`central/pdf.py`); traps it survives:
  escaped `)`, xref offsets captured as written, **cp1252 not latin-1** (the fonts are
  WinAnsi; an em dash once titled a report "Open issues ?"), mono measured with its own
  widths. Column widths are MEASURED from content (`_solve_widths`, water-fill) — never a
  share of the page (proportional sizing truncated the MAC the report exists to carry).
- **Excel export is a REAL .xlsx** hand-written with zipfile (`central/xlsx.py`) — not a
  CSV wearing the name. Traps: `autoFilter` AFTER `sheetData`; style counts must agree;
  the two default fills are mandatory; epoch 1899-12-30; XML-forbidden control chars
  really arrive in aliases; `since` is a real date cell so sorting orders by time.
- Timestamps go through ONE zone conversion (`notifiers._wa_local`) for WhatsApp, PDF and
  xlsx alike — possible only because every stamp lives in `since`, none interpolated into
  detail strings.
- Filter chips are a SINGLE choice; build the next URLSearchParams fresh (mutating the
  memoized instance moves the value without moving the reference). Workers CAN read
  issues + exports (read-only, org-pinned). Tests: `unit/test_pdf`, `unit/test_xlsx`,
  `integration/test_central_issues`.

### Historian & charts

- Ten `hist_*` tables + `onu_events`/`hist_onu_hour`/`hist_onu_day`
  (`central/history.py`; `store_history/replay/capacity` mixins; sampling hooks in
  `optics.sync_device`/`ports.py`/`radius_sync` — zero new report-path writes; 6 h
  fold+prune maintenance thread; retention via `WISP_HIST_*`). Read API under
  `/api/history/*`. hist_* schema (WITHOUT ROWID, PK = read path) + age-ladder retention
  is verified good — prune-path indexes only.
- Chart kit `web/src/chart/` (d3-scale/shape/array). **ONE plane per plot** — the five
  identity planes fail as a categorical palette (deliberately quiet); multi-series
  separates structurally, never by pairing plane hues. Status hues appear only for
  failure claims. Gaps break lines; windows clamp to the org's first row and HOUR-FLOOR
  it (or the partial first bucket vanishes).
- Surfaces: Home cockpit band + Reliability + Busy-hour (graded amber past 70%/red past
  90%, one ladder shared with the per-port drill), `/triage` queue with the two-tier nav
  badge, device History fold (every panel leads with HEALTH — an explicit take-back,
  don't restore optics-first), Logs paging ledger, map time machine (replay closes the
  device panel and stands down live-verdict layers, "on · not in replay"), Marey bottom
  dock, Outage-time layer, subscriber Rx/state-timeline chart (no-Rx fleets get the
  state timeline, honestly).

### Layout & navigation

- Settings is SECTIONED behind a `SECTIONS` table whose `visible(ctx)` predicate must
  model the same conditions its cards gate on (a role-only predicate once showed a blank
  tab). Account-scoped config lives in the account menu, not primary nav.
- Viewport breakpoints are wrong inside the 380 px device panel — use CONTAINER queries.
- `HourStrip` floors on EPOCH hours (half-hour zones shift every cell). Sort by
  `occurred_at ?? received_at`, never insert id.
- Home is a NOC overview, never empty when healthy; outage status is derived, never
  stored; recovery is FSM-automatic — no manual resolve, ever.
- Live updates: one SSE per org scope invalidating react-query keys off
  `store.data_version`; `list_org_devices()` LEFT JOINs states + port aggregates so rows
  color without round trips.

### Map (`/map`)

Leaflet renders; Google is ONLY the tile source (Map Tiles API — the sanctioned
third-party-renderer API). Swapping to the Google SDK would rewrite every overlay,
re-bill per load, and lose the keyless CARTO fallback (the map is never blank). Helpers
live in `web/src/map/*`; new map logic goes in the matching module, not map-page.tsx.

- Server-wide key in `app_settings` (superadmin, referrer-restricted, ships to browsers
  by design; central makes NO Google calls). dpr>1 sessions request 512 px tiles (plain
  256 rasters are why it "looked blurry"). Session tokens cached per variant; **the cache
  key carries a HASH of the style array** (a token bakes its style in for ~2 weeks — a
  palette edit would silently serve old tiles); `variantOf` never returns "" for roadmap.
- **The basemap must rank UNDER the status tones.** Two ceilings: geometry ≤ `--border`
  (a road is at most a panel edge), labels ≤ ~2.05:1 dark / 1.75:1 light (a legibility
  floor — quieter than that, use the "Google labels" switch, don't chase with colour).
  Ladders re-solved by scaling in LINEAR RGB (preserves hue, moves level); road geometry
  dulled against the GROUND, never its own casing (a dissolved network reads broken);
  dulling runs the OPPOSITE direction per mode. These are SOLVED SETS — re-run the whole
  ratio table, never nudge one value. `ICONS_OFF` strips Google's POI discs + yellow
  highway shields unconditionally on roadmap (they share the grammar of device pins and
  `--warning`); geometry untouched. `styles` is roadmap-only; an oversized array is
  dropped SILENTLY (check a real tile after editing).
- Rate chips are kilobit-floored and collapse when idle (`IDLE_BPS`/`fmtShort`, ONE copy
  shared with refonu). **ONE screen-space budget for every chip family**
  (`chipShown`, greedy, ranked by `bwRank`; a claim carries its own half-width — one
  box for all families let wide chips visibly overlap). A new chip family JOINS this
  budget. The budget must read the same predicate as the render (a chip nobody draws may
  not reserve pixels).
- **Stroke weight scales with zoom** (`map/stroke.ts`: linear in zoom level, floor 1.0 at
  z≤13, ONE multiplier for every line — per-kind curves would re-rank the tuned weights).
  **The dash MUST scale with the weight** (`strokeAt`/`casingAt` take both together): SVG
  dash lengths are absolute px, so widening a dotted line without opening its gaps closes
  it into a solid one — i.e. traced fibre a crew quotes drum off. Screen-space quantities
  (chip boxes, CLUSTER_PX) are deliberately NOT scaled.
- **A traced route is drawn unless BOTH ends folded onto one point**
  (`foldedTogether`). Three distance-threshold rules in a row were wrong — a fold nudges
  one segment by a cluster radius and self-heals; a chord replaces surveyed geometry with
  a line indistinguishable from a real one. `drawnLinks` no longer depends on zoom (a map
  that draws cable at z16 and a chord at z17 teaches distrust).
- **Passive plant is OUT of the clustering pass** (a site badge is a claim about GEAR — a
  count/status ring over boxes with FSMs; a splitter has none). Each passive returns as
  its own single-member cluster (dropping one orphans its drop lines). Cost accepted:
  dense plant overlaps at low zoom.
- **Mark size is a LADDER, not a number** (`index.css` THE MARK SCALE): a mark picks a
  RANK (evidence > gear > plant > drop) and a SHAPE FACTOR, never a px. Ranks are stated
  as INK (equivalent-circle diameter; a diamond's bounding box carries half a square's
  ink — how two "9px" marks differed 2× on screen). Gear is the calibration (the OLT).
  Steps between lower ranks are SMALL on purpose: **ink encodes ORDER, never EMPHASIS**
  — emphasis is tone, stacking, clustering and zoom floors; **shrinking is not a
  subordination channel** (tried three times, unreadable each time). Radii/holes/tips are
  PROPORTIONS.
- **Quieting a status colour means CHROMA at fixed hue, never a mix toward the grey**
  (this palette's greys lean cool, so a mix drags hue — the "green" pin went cyan and
  ended up less colourful than the dotted line beside it). The tokens are
  `oklch(from var(--success) …)` — derived, so recolouring carries through; the
  `@supports` fallback is load-bearing (an unparsable custom property makes the mark
  VANISH).
- Hover affordance is pure CSS on `transform` (icons are cached by html string;
  `useNow()` ticks every second, so React-routed hover would restart the down-pulse).
  A mount animation may only go on a mark whose html string is stable. `zoomSnap=0.25`.
- **Hovering a box opens the same card a subscriber does** (`map/hovercard.tsx` — one
  frame, two models): gear toned by `pinTone`, plant by `dropTone`, CALLED not
  re-derived (a card may never grade a box differently from its pin). Every frozen rule
  carries over (a splitter whose OLT is down reads "state unknown", and `dropTone`
  stands down with it). Rows are content-measured in a browser. The link-distance
  readout stands down near a pin (`PIN_KEEPOUT_PX` — the ring centres ~12 px below the
  visible dot) and while any card is open.
- Chrome-over-tiles: outline buttons need `dark:bg-popover/95` too.
  **`.wisp-map-wrap { isolation: isolate }` is load-bearing** (Leaflet panes at z 400-1000
  otherwise beat every Radix portal — don't fix by bumping portal z-indexes). Floating
  chrome belongs inside the control column, not at hardcoded offsets.
- Leaflet traps: `pathOptions.className` is silently dropped (pass `className` top-level
  + tone in the key); **topology polylines stay `interactive={false}`** or they swallow
  placement clicks.
- Placement: lat/lng only via `POST /api/inventory/location` (dashboard-side; the edge
  never sees coordinates). divIcons CACHED by html string. Role shapes via
  border-radius/rotation/::before only (clip-path clips the selection shadow).
- Viewport locked to `orgs.map_region`; all view logic in ONE `useMap()` child; fit runs
  before `setMinZoom` (an animated setZoom otherwise lands after and overrides).
- **Site clusters** (the spider-fan was REMOVED — it scattered pins onto fake coordinates
  and read as geography): screen-space fold into a count badge; co-located members
  resolve in UI space (site card anchored by member id). Placement snaps to a site's
  exact coords; a drag within 24 px snaps to a neighbor. `pinPos` is raw-or-centroid
  ONLY — **no display positions that aren't true locations**. Same rule killed the
  per-ONU spoke fan: the map shows only true locations; ONU severity lives in the pin
  ring + Optical tab.
- **Cable routes**: `link_routes` keyed (org, child, parent); waypoints INTERMEDIATE
  only (endpoints implicit so pins rubber-band). Cross-links keyed child=higher so
  waypoints always run parent→child (until fixed, every peer route save 400'd).
  Straight-chord fallback when an endpoint folds. The device panel shows "cable" AND
  "straight-line", labeled honestly — crews quote drum off this. A span's own record
  (cable id, core, label_pos) rides `link_routes`; `set_link_style` is sparse;
  straightening a path must not forget which cable it is in. THE PER-LINK COLOUR IS GONE
  (`LINK_COLORS` deleted — `org_cables` now says "one physical cable" properly).

### The fibre plant

`central/fiber.py` ↔ `lib/fiber.ts`, `central/cablepath.py`; `org_cables` +
`org_fibre_joints` + `org_cable_cores`; tray in `coupler-tray.tsx`. The ISPs corrected
the model: *fibre runs between two closures; at a closure you join cable to cable or
take a core out to a device; any core may carry anything; a customer point is a closure
too.*

- **A cable is a SEGMENT with two recorded ends.** Everything else fell out as deletions:
  runs, taps, the double-booking checker and the implicit-continuity rule are GONE —
  their states are unrepresentable now. `SegmentModelTest` (in test_fiber AND
  test_cablepath) asserts the names STAY deleted.
- A fibre POINT is a device OR a subscriber (nullable pair `a_device_id`/`a_mac`) — not a
  third registry. A cable ends on WHATEVER it is dropped on. **`parent_device_id` is
  untouched** — nothing here writes org_devices or links, so a recorded splice can never
  re-page. `test_recording_fibre_NEVER_reaches_the_engine`.
- The plant chain comes from the fibre when nothing is declared (`org_plant_feed_map`);
  DECLARED wins. **The feed flood runs down the GEAR TREE** (`feed_map(rank=)`, ranks =
  declared depth): shallower gear floods first and passes THROUGH deeper gear — the
  blind nearest-flood once named an OLT as feeding its own uplink. `FeedMapTest`.
- **A fibre lands on a PORT** (`PORT_KINDS` = pon|leg|in|port; two nullable columns, NOT
  a table — a splitter's ports derive from its ratio, an OLT's from its PONs, a switch's
  from walked interfaces via `if_port_no`, the number off the END of the name, NEVER the
  ifIndex; `_VIRTUAL_IF` refuses VLAN/loopback/bridge). Never a `switch_ports` row (that
  table pages). Enclosures have NO ports — every fibre in one is a splice. `leg`/`in`
  bounded by the ratio; `pon`/`port` deliberately UNBOUNDED (nothing tells us the count;
  refusing PON 9 refuses the operator's own sight). Naming a port is PROMPTED, never
  required.
- **The PON a box is on is WALKED, not typed** (`pon_of_points` → `fibre_pon`), inherited
  down the plant chain; never overwrites the typed field (both shown when they differ —
  a finding, not a conflict); two PONs = `ambiguous`, never a pick. A PON is stored by
  INDEX and printed by the box's OWN name (`pon_names`/`port_display`, resolved
  server-side at one call — half a menu in our words reads as the wrong box; a bare
  number is not a name; the walk wins over the roster).
- Roster labels and interface names parse by DIFFERENT rules (`pon_index` vs
  `pon_index_of_interface`) — read permissively, `GE016` becomes a PON. The list is a
  SET, never a range (real fleets have gaps).
- **Connect a port straight to a box** (`/api/inventory/fibre/connect`) — the gesture:
  open the box, click the port, pick the box (was 18 interactions across three panels).
  **A MACRO that writes only rows the manual path could** (a 1F cable named for the
  connection + a termination at each end) — the moment it records something the tray
  cannot, there are two models again. The far end lands on the only port it could
  (`_sole_input`); a 2-input splitter gets a choice. A leg connects straight to a
  customer through `onu_drops.leg_no` (sparse; the bulk dialog sends no leg, so re-saving
  never wipes one; a re-home clears it).
- **THE RECORD STARTS FULL** (`fiber.undrawn`): the panel opens with the connections the
  declared topology claims and has no fibre for — confirming one is two clicks. (The
  ISPs had already built the network once in `parent_device_id`; asking them to build it
  again in glass, in our vocabulary, is why four cables were laid and abandoned in 36
  minutes.) **A DRAFT, NEVER A CLAIM** — nothing stored until confirmed;
  `parent_device_id` is read, never written. Glass recorded THROUGH a closure counts
  (`connected_points`; the hop set is ENCLOSURES, never gear — plain reachability would
  suppress every genuine last hop). The picker orders by declared topology, then
  distance. `test_the_DRAFT_IS_NEVER_A_CLAIM`.
- **A cable nobody laid is not a cable** (`is_plumbing` = unnamed AND ≤1 core AND
  untraced): never listed, labelled, offered or counted — the connection reports as the
  box at the far end. It stops being plumbing the instant somebody names/widens/traces
  it. `_connect_name`/`_tail_name` are DELETED and must not come back (machine names read
  our bookkeeping aloud). Mind `path` is None in Python and `[]` in the SPA (`![]` is
  false — the server ships the `plumbing` flag, no surface re-derives it).
- **The five gestures**: lay a cable (snap has TWO budgets, px AND metres, larger wins —
  a px budget shrinks in ground terms as you zoom in, and ends measured from the pin
  ANCHOR missed the dot the operator aims at; the banner names what each end caught; a
  near miss within 25 m is ASKED about, never guessed); open a closure mid-span
  (`cablepath.split` — every core spliced straight through, both halves keep the drum's
  name; offered on the map right-click; an untraced cable can be opened too, both halves
  stay untraced); splice (below); take a core to a box (a joint with `b_cable_id` NULL is
  the termination; the tail macro writes 1F + splice + termination in ONE transaction,
  fibre checked before the cable is laid; the tail is 1F and untraced, named for source
  cable + core); trace (walks both directions; a fork or loop STOPS the walk and returns
  `fault_at` — a confident line past a fork sends a splicer to the wrong closure).
- **The tray is a SPLICE SCHEDULE — one row per fibre, destination ON the row** (the
  facing-pages tray could ENTER arrangements it could not DISPLAY: cores fanning to
  three boxes drew as empty cells, hiding the work just done — do not re-add facing
  columns). Straight-through and UNRECORDED runs collapse (never crossing a buffer
  tube; a crossing NEVER collapses); a collapsed free run stays actionable naming its
  core; a fully-joined cable shows DISABLED, not hidden. A tail reports as the BOX it
  reaches. `Splice all through` skips what's already joined. **A cut drum is ONE
  schedule** (`cutPairs` pairs the two same-named segments; view-level only — storage
  stays two segments; spare glass on the other side reads `spare · toward <far>`).
- **One fibre joins exactly one fibre**, enforced on the WRITE (`joint_refusal` →
  absent/self/taken), answered 200 with a NAMED refusal (a bare 400 on a splice tray
  reads as a broken button). Same core number across two different cables is legal (a
  12F spliced through is twelve of those); a U-turn is legal.
- **Recorded is never occupied** (`cores_recorded` counts joints at either end or a
  label; no `cores_free`/`spare` key exists — pinned). What a core CARRIES is typed free
  text (`org_cable_cores`), rendered apart from joints.
- Shrinking the count under a core IN USE is refused; MOVING an end discards the joints
  at that end (guarded on the end actually changing, so a rename is idempotent).
- **Orientation is MEASURED, never stored** (`cablepath.orient`, decided on the TOTAL of
  both end stubs — either alone draws the cable crossing itself). LENGTH is walked
  segment-by-segment (Mercator stretches; crews order drum by the metre); an untraced
  cable has NO length, not zero.
- Rendering: `--map-plant` at full chroma, no status tone (a cable has no state);
  untraced = dashed chord (`CABLE_DASH`); tracing lights by EMPHASIS, never hue. **The
  cable says its own name on the map** (name leads, count follows; the CHIP is the click
  target — the polyline stays non-interactive; a third chip family in the shared budget,
  which forced per-family half-widths; `cableLines` resolves geometry ONCE for render
  and budget).
- **Recorded glass wins: a dependency chord STANDS DOWN once fibre joins its pair**
  (`connected_spans` → `cabled_pairs`, through closures — the same walk `undrawn` reads,
  so map and draft can't disagree). The rate chip moves onto the biggest sheath on the
  run and KEEPS ITS RANK in the budget; only a REAL reading moves. The sheath inherits
  the CHORD's visibility, not plant's (a stood-down pair must never revert to a dotted
  line at any zoom); the exemption covers the WHOLE run or the line dies at a closure.
  The hover distance probe reads what the map DRAWS (stood-down chords excluded; drawn
  cables included, naming the cable's own ends; `HoverLink.straight` marks chord
  measurements).
- The Layers switch is "Dependency links" — what stays dashed IS the to-do list.
- **A strand colour may never be a line's stroke or text** (TIA-598 contains the alarm
  hues — core 7 painted red is a fabricated outage): a DOT in a neutral chip, a SWATCH
  in a panel. Past 12 fibres the sequence restarts inside a buffer TUBE
  (`locate()`/`strandAt`; twelve to a row so each row IS a tube). `TubeTest`.
- `FIBER_COUNTS` is CLOSED (1/2/4/6/8/12/24/48/96) and mirrored in TS
  (`SpaAgreementTest`, including every hex). **1F is the single-fibre tail** — leaving it
  out once made a closure feeding an OLT unrecordable.
- A topology link carries NO plant record (`link_routes` is cartography); the old
  `/cable/run|tap|splice` routes answer "recorded on the cable itself now — reload"
  rather than 404 (the SPA deploys instantly, central needs a restart).
- The old-model WIPE ran guarded by an app_settings marker; children before parents
  (`PRAGMA foreign_keys=ON`), rehearsed on a copy. `onu_drops` SURVIVES — a drop says
  which LEG (optical split), a cable says which GLASS. Different facts.
- Vocabulary: keep the words a splicer says (closure, splice, core, tube, 24F, leg,
  drop); a port row's empty state is the verb "Connect…", never "not recorded". The
  joint box is a **closure** — `coupler` was OUR word wrongly recorded as theirs (a
  comment claiming a word is theirs is not evidence); the type stays in `PASSIVE_TYPES`
  forever so a restored-backup row stays silent plant.
- Still to do: **SHARED RISK** — N cables through one duct as a common-cause hypothesis
  for `incidents.py`. Deliberately scoped out.
- Tests: `unit/test_fiber`, `unit/test_cablepath`,
  `integration/test_central_cableplant`.

## Config

Every tunable is a field on the frozen `Config` dataclass, read once from `WISP_*` env.
No DB settings layer (topology/team/credentials live in the dashboard). **`Config` is
shared between edge and central** — grep both `apps/daemon/` and `src/wisp/central/`
before renaming a field. `db_path` is just where the lock file lives.

## Ingest auth & enrollment

- Any ONE of: global bearer, per-node token, mTLS. None configured = open (trusted
  network) — fail-closed is staged, pending the operator's go-ahead.
- Node tokens: hash-only, shown once, rotatable. A node that HAS a credential is gated on
  presenting it; identity comes FROM the credential. `clean_node_id` validates (it
  becomes a systemd identity and a path segment).
- mTLS: `central/pki.py` shells to openssl; CN encodes `org:node` and must match. The
  handshake runs in the request's worker thread so one slow handshake can't stall the
  listener. No CRL — revoking means rotating the CA.

## Reliability ("trust the alarm")

- One probe per org/node via an OS advisory lock; central's per-outage dedupe is
  idempotent anyway.
- A page must not vanish to a blip: `send_with_retry` (network/5xx retry, 4xx fast).
- The probe loop never dies on one bad cycle (per-cycle try/except — keep new per-cycle
  work inside it); `_gather_pings` swallows per-probe errors but re-raises
  config/permission RuntimeErrors loudly.
- Fleet watchdog is central's, transition-only, restart-safe. Input is
  `store.node_liveness()`, NOT `SELECT * FROM nodes` (that table remembers every
  identity ever seen); `delete_node_token` purges the heartbeat row too, or a deleted
  probe pages NODE_STALE forever.

## Fleet packaging & self-update

- Two FIRST-INSTALL-only artifacts (.deb, Windows setup exe) run the **supervisor**,
  which owns all self-updates (download → sha256 → swap → health-gate → rollback). The
  manifest builder skips installers.
- The supervisor STOPS the agent before `os.replace` (Windows delete-locks a running
  image); any mid-apply exception discards the directive (retry rides the poll cadence).
  The health gate needs `stable_polls` CONSECUTIVE healthy polls or rollback is
  unreachable for a crash-looper. Supervisors and the tray update only via installer
  re-run.
- The tray's ONLY status truth is status.json (a SYSTEM task is unreadable non-elevated,
  indistinguishable from "not installed"). The Windows installer upgrades in place
  (PrepareToInstall kills the running fleet first) and WAITS for a fresh status.json.
- **Central is the release mirror; edges never touch GitHub.** `releasesync.py` pulls
  unauthenticated by default (a PAT is only for private repos — an expired one silently
  blocked a rollout), verifies sha256, serves `/download/<ver|latest>/<name>`. GitHub
  asset 302s to S3 which REJECTS an Authorization header — re-fetch clean.
- Install-artifact names are VERSION-LESS and load-bearing (the install card links
  `/download/latest/<asset>`). The field-app APK mirror is store-less: **never
  `set_release` an app version** (the release table drives edge self-update — an app tag
  would roll the fleet).
- CI signing (Authenticode + minisign) no-ops while secrets are unset; nothing has run
  against real keys yet. `.spec` Analysis paths resolve against SPECPATH, not cwd.

## Conventions & gotchas

- States: UP/DEGRADED/DOWN/UNREACHABLE; `DOWN_FAMILY`. Import from state_machine, don't
  hardcode. Hysteresis: DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2, recovery = 2.
  The FSM never emits UNREACHABLE (topology override after feed()).
- No automatic cause inference — cause is operator-entered at resolution.
- Escalation: one `escalations` row per outage re-broadcasts while open; ack doesn't stop
  it, recovery does.
- **Assignment is triage**: owner-only (`_can_write`), an ASK that does NOT stamp the ack
  — status stays DOWN ("awaiting response") until an assignee ACCEPTS (the one claim a
  NOC screen must not make falsely is "in progress" on an untouched outage). It pages
  EXACTLY the assignees (`named_whatsapp`), never the org audience; empty list refused;
  usernames re-resolved against active accounts.
- **Accepting** (`can_triage`, store refuses anyone not NAMED) is the only thing that
  moves it to in_progress; first acceptance also stamps the ack (COALESCE). Re-assigning
  keeps acceptances of anyone still named. The page carries an interactive
  [✅ I'm on it] button (falls back to the template outside Meta's 24 h window);
  `whatsapp_bot.py` handles `acc:<id>` on the same store method; both paths tell the
  assigner. Tests: `integration/test_central_outage_assign`.
- Timestamps: ISO8601 `+00:00`; SQLite `datetime('now')` is space-separated naive —
  `core/analytics._parse` normalizes both, reuse it.
- Schema: `store.py`'s `_SCHEMA` + `_ensure_columns` is the only schema.
  **`CentralStore` is split into domain mixins** (`store_*.py`) composed in store.py —
  new methods go in the matching mixin; import `CentralStore` from `wisp.central.store`.

## Tests

**`.venv/bin/python -m unittest discover -s tests`** — the interpreter matters: system
python3 has no httpx, which ERRORS ~12 proxy/edge tests and looks like a standing
failure (it never was). Never pipe the run to `tail` (masks the exit code); redirect and
grep `^(OK|FAILED|Ran )`. Tests inject recording doubles — no real network.

## Removed — don't go looking for these

- **The single-box era** is deleted wholesale and git history was truncated to the newest
  10 commits (2026-07-09, no backup) — don't offer to restore it. `core/state_machine`,
  `core/analytics`, `core/baseline` are ALIVE (central imports them).
- **Both payment gateways** (Razorpay, UPIGateway) — the operator wanted manual GPay/QR.
  Dead settings rows and an orphaned `billing_payments` table may linger; don't "clean
  them up" into a migration (house rule for all dead columns: ntfy topics, org_workers/
  org_attendance, operator/tech role columns).
- **The Team page / roster model** — who works for an org is who has a login account.
- ntfy, the read-only operator/tech roles, the spider-fan, per-ONU map spokes, the
  per-link colour, `webplan.md`/`whatsapp-notifier-plan.md` (folded into this file).
- Tags: `v0.13.0`–`v0.15.1` survive; **`v0.14.0` is the rollback floor** — no artifact
  below it, older edges can only roll forward.
