# CLAUDE.md

Invariants and gotchas that aren't obvious from the code. What/how/layout/config lives in
`README.md`. Verify claims about what's done against the code — stale docs drift.

Each bullet is a rule plus the reason it exists. **The reason is load-bearing:** most were
paid for by a field incident, and without the why the next session redoes the thing that
broke. Where a test pins an invariant it's named — that's the cheap way to check you haven't
undone it.

## Architecture

Central runs the brain for every org: FSM, topology-aware suppression, fast-confirm, the
alerting ladder, the multi-org dashboard, fleet version/rollout state. The edge is a thin
probe with exactly one mode (`WISP_CENTRAL_URL` mandatory): fetch topology, probe ICMP under
bounded fan-out, report raw per-IP samples, heartbeat its version. No local DB, dashboard, or
FSM on the edge.

Central (`central/server.py`/`store.py`/`auth.py`/…) is pure stdlib. Its dashboard is a
build-time React/TS/Tailwind/shadcn SPA (`web/` → built into `central/static/`; Node is
dev-only, the committed build is what deploys). Edge needs a `.venv` (`icmplib`/`httpx`;
system Python is PEP 668-locked) + `sysctl net.ipv4.ping_group_range="0 2147483647"`.

**Locked decisions (don't relitigate):** brain on central, always; monitor shared infra, not
end-user routers; ntfy is the primary channel, 2 role topics/org; every read/write
org-scoped; edge dials central, never the reverse; updates pull-based over the report
channel, staged + health-gated; probers/notifiers behind interfaces, tests inject doubles.

## Imports & paths (the main trap)

Absolute imports under `wisp.*`; src layout, nothing installed. `apps/daemon/main.py` and
`apps/central/main.py` prepend `<repo>/src` to `sys.path`; the admin CLI needs
`PYTHONPATH=src`; tests bootstrap their own path. `config.PROJECT_ROOT` = repo root
(`parents[2]` of `config.py`); `central_db` defaults to `data/central.db`; the SPA resolves
from `central/static/`.

## Engine invariants

- `core/state_machine.py:MonitorEngine` is **pure** — `{ip: PingResult}` + ts in, states +
  `Event`s out, no I/O. Central owns build/rehydrate/persist (`central/engine.py`).
- `process_cycle(results, ts, subset=None)`: `None` = full pass (all devices + canary/uplink
  + freeze); a `set[int]` = confirmation pass (those FSMs only, topo order, skips
  canary/uplink). Keep the full-pass path byte-identical.
- `probe_plan()` is a reference the edge approximates (`_gentle_probe_plan`), not something
  central calls. Known gap: it counts a BACKUP parent as infra; the edge can't
  (`GET /edge/devices` carries only `parent_device_id`).
- `central/dispatch.py:CentralAlertDispatcher` sends OUTSIDE any DB transaction — a slow API
  call must never hold a write lock.
- Prober/Notifier live behind `build_prober`/`build_notifier`; new providers go behind them.
- **Windows probes via `SingleSocketIcmpProber`, never icmplib** (picked by `sys.platform`;
  `WISP_PROBER` forces one). Windows raw sockets are promiscuous — N sockets each see every
  reply (O(N²)) and asyncio stamps arrival at coroutine-read time (~150ms floor). Fix: ONE
  shared raw socket + ONE receiver thread stamping `perf_counter()` right after `recvfrom`,
  matched by ICMP id (pid-derived) + seq + reply source IP. Linux keeps icmplib's
  unprivileged datagram sockets (raw would need root and break the ping-group invariant).
  Tests: `unit/test_probers`.

## Scaling invariants

- **Probe fan-out is bounded** by `Semaphore(cfg.probe_max_inflight)` (256). Unbounded gather
  past `ulimit -n` reads as a fake mass outage (socket refusals masked as 100% loss).
- **Aggregation gear is probed gently**: parents get `pings_per_poll_infra` (2), leaves +
  canary `pings_per_poll` (5) — or ICMP rate-limiters read as phantom loss.
- **Fast-confirm is central-driven**: `engine.compute_recheck` names suspect IPs in the
  `/report` reply; the edge re-probes just those every `WISP_RETRY_INTERVAL_S`
  (`mode="recheck"`) until the hint is empty. A frozen cycle (canary down) yields no hint.
- **Adaptive cadence** (`Config.effective_interval`): 30s while fleet ≤ 1000 and
  `poll_interval_adaptive` on (off by default), else 60s — computed once at startup. EXCEPT
  the org override `orgs.poll_interval_s`, which rides the `/edge/devices` reply and
  re-applies every cycle, no restart. Precedence: CLI `--interval` > org value > env/adaptive.
  **Clamped 10–120s on BOTH sides** — past 120s a healthy probe outlasts the watchdog's 180s
  NODE_STALE threshold and pages as dead. NULL = automatic.

## SNMP: background, three clocks, one airtime gate

- **Background asyncio task, never inline in the probe cycle** (inline walks once made the
  edge report every 4 minutes). Ports attach to full reports only, never recheck.
- **Three separate walk CAPS, not one**: health `snmp_walk_timeout_s` (20s),
  `port_walk_timeout_s` (60s, ifTable), `gpon_walk_timeout_s` (75s, ONU roster). A
  200+-interface OLT can't finish in 20s, and timing out leaves that table permanently stale
  while the smaller walks stay fresh on the same box. Field-diagnosed 2026-07-09; don't
  collapse them.
- **Three separate sweep CLOCKS too** (2026-07-17): `snmp_interval_s` is health AND the
  master gate (`<=0` = all SNMP off); `port_interval_s`/`gpon_interval_s` ride their own. All
  default 300s — don't raise past ~600s without revisiting the 900s staleness gates that
  freeze roster/port alert state. Until the split, one 90s clock fired all three and the next
  sweep waited on ALL THREE: a slow roster walk starved the ifTable walk and (next_snmp
  stamped at sweep START) re-fired immediately, walking that agent back-to-back all day.
  HILL-OLT-1/PYLON sat at 0% port-walk success while optics stayed fresh — **the polling
  caused the failure.**
- **One `_SnmpAirtime` gate spans every SNMP subsystem** (ports/health/optics +
  `_DiagWalkRunner`): a fleet-wide `Semaphore(snmp_max_inflight)` (4, global not
  per-subsystem) PLUS a per-device lock, because equal 300s periods make the clocks fire on
  the same tick every sweep. Device lock acquires BEFORE the semaphore (waiting on a busy box
  must not pin a fleet slot). `SharedAirtimeGateTest`.
- **Per-request patience matches gpon's**: ports/health/diag use `snmp_request_timeout_s`
  (5s) × `snmp_request_retries` (3), not the bare 2s × 1. Weak agents answer whoever retries
  longest; the strict window got ZERO responses for 26h fleet-wide while optics (already
  patient) stayed fresh on the same boxes. Don't "optimize" the retries back down.
- **One `SnmpEngine` per poller instance, NEVER per walk** — a per-walk engine leaks ~1 MiB +
  one FD forever (transport stays registered with the loop), and FD exhaustion reads as a
  fake mass outage. `PysnmpPoller`/`PysnmpGponPoller` lazily reuse `self._engine` (concurrent
  walks are safe — request-id demux). `EngineReuseTest` in `unit/test_snmp` + `unit/test_gpon`.
- **pysnmp 7 walk commands take exactly ONE varbind** — passing ten ifTable columns to
  `bulk_walk_cmd` positionally is a TypeError swallowed as "walk failed" (froze every fleet
  port table for 30h in v0.15.3; only `bulk_cmd` takes `*varBinds`). The combined walk is
  `MultiColumnWalk` over raw `bulk_cmd`, and ANY failure falls back to per-column
  `bulk_walk_cmd` — keep the fallback; port coverage must never ride on one optimization.
  `CombinedWalkDriverTest`.
- **`PysnmpPoller` is ADAPTIVE and vendor-agnostic** (2026-07-19): weak C-Data OLTs serve
  health+optics but drop the heavy combined ifTable GETBULK. A per-device ladder walked from
  the gentlest level that last WORKED — 0 combined (time-boxed to `_FAST_PATH_MAX_S`=15s so a
  big box hands the rest of the budget to the net), 1 per-column/25, 2 per-column/4 —
  with `_device_level`/`_promote_at` persisting across sweeps and re-probing one rung faster
  every 6h so a firmware change self-heals. NO vendor hardcode. **The per-column net is
  TOLERANT**: a dropped column is skipped, never fatal (the old raise-on-first-drop is why
  small OLTs no_responsed though the same box's health walk succeeded); status columns first
  so a budget-bounded partial still yields port up/down. A genuinely dead agent still
  RE-RAISES (keeps the no_response classification); an agent with no ifTable returns `[]`.
  `AdaptivePortWalkTest`.
- **Unit tests inject fake pollers, so a bad HLAPI call only surfaces on a REAL walk** —
  verify `device_snmp_status` after any edge SNMP rollout.
- **Remote walks: the edge is central's hands, poll-only.** Queued from the dashboard,
  delivered in the next FULL `/report` reply under `snmp_walks` (the edge NEVER accepts
  inbound), run by a sequential `_DiagWalkRunner`, result POSTs to `/edge/snmp-walk`. Pending
  walks re-deliver until a result lands; one pending per device; newest 10 kept. **The runner
  refuses any target IP not in the node's device list** — no lateral-movement primitive.
  Server double-bounds upload size. A walk stopped at the varbind cap or 60s budget carries
  `truncated` to the dashboard row: a partial dump that looks complete turns "that OID holds
  nothing" into a false negative costing a vendor-onboarding session. A narrower root is the
  fix, never a bigger cap.
- **Vendor health profiles are DATA, not edge code** (`snmp_profiles`; org_id NULL = global).
  Metric → OID + decode from a CLOSED vocabulary (`as_is/div10/div100/signed_div100`, select
  `first/avg/max/sum`). `ingress/health.py` walks `sysObjectID`, matches by LONGEST prefix,
  walks profile OIDs BEFORE standard MIBs, fills only fields still None; hardcoded
  MikroTik/Fiberhome fallbacks stay for fleets on an older central. Onboarding a vendor = a
  profile row, never a rollout. Keep the vocabulary tiny.

## Central runs the brain

- `central/engine.py` is the only DB glue around the unchanged `MonitorEngine`;
  `central/dispatch.py` is the alerting policy (dedupe per outage, owner+worker on open, both
  on resolve, ack never stops it — only recovery does).
- **`EngineRegistry`: one live engine per org** (flap streaks accumulate across stateless
  `/report` calls). Rebuilds only when the fingerprint `(id, parent_device_id, d.parents)`
  changes; rehydrates from `device_states`. Breaking rehydration re-pages everyone on restart.
- **Wire format is IP-keyed**: `POST /report` `{"v":1,"org_id","node_id","ts","mode":"full"|
  "recheck","pings":{ip:{loss_pct,latency_ms,jitter_ms}}}`. The edge never sees device ids.
- **Escalation sweeping rides the report cadence** — `sweep(ts)` once per full `/report`,
  scoped to that org. Stalls if an edge goes stale; the watchdog pages for that separately.
- **The heartbeat is the self-update channel, not liveness.** Reply may carry an `update`
  directive (`central/rollout.py`) written ATOMICALLY as `update_request.json`, or
  `restart: true` (one-shot `nodes.restart_pending`). Liveness is `touch_node` off `/report`;
  a failed heartbeat is a warning, never a crashed cycle. DELIVERY clears the restart flag: a
  directive lost in flight means the operator clicks again, never a loop.
- **Auto-update is org-opt-in** (`orgs.auto_update`): `maybe_auto_rollout` arms the SAME
  staged canary rollout the manual button uses. A HALTED rollout for the same target is NEVER
  auto-retried — a build that failed its health gate re-arms only via a human's Retry; `done`
  re-arms freely.

### The notification governor (the choke point)

`central/notify_policy.py`. Every paging shell (dispatch / ponalert / onualert / ports / perf
/ redundancy / optics) routes send + status + log through `AlertRouter.emit(kind, …)` rather
than calling the notifier inline. Two tiers on a clean `kind` token:

- **PUSH** (buzz the phone): ICMP device/uplink/port down **and their recoveries**; port
  bandwidth floor/ceiling crossings + clears (`PORT_BW_*` — a saturated/dark uplink can't
  wait for the roll-up); `OPTICAL_CRIT`/`OPTICAL_RECOVERED` (an ONU under the floor is a
  subscriber about to lose sync — burying it should be an explicit operator decision, not a
  refactor's side effect).
- **DIGEST** (`_DIGEST_KINDS`): the rest of the SNMP-derived stream (PON_FAULT, ONU_LIMIT,
  ONU_DUP_MAC, PERF_*, ON_BACKUP/BACKUP_CLEARED) **plus the hourly escalation**, queued to
  `alert_digest`, one summary per org every `cfg.digest_interval_min` (60).
- **Unknown kind ⇒ PUSH** — a new alert type must never be silently buried.

`flush_digests` rides the full `/report` sweep, anchors on the OLDEST pending row (no per-org
clock), marks-sent only on success. Escalation is OPERATOR-topic only. PUSH cooldown backstop
= `cfg.alert_cooldown_min` per `(device,kind)` (ports pass 0 — per-if_index, already gated).
State rows are written by the shells regardless of tier — this governs only the notification.

**Why:** a DBC area power cut darkened many PONs → dozens of false "fiber cut" pages → ntfy
429s that dropped REAL pages (~497→~76 phone pages/day). Tests: `unit/test_notify_policy`.

### WhatsApp: an ADDITIVE second channel behind the governor

`build_notifier(cfg, store)` returns a `MultiNotifier` fanning every page to ntfy AND WhatsApp
(Meta Cloud API). Experimental, 2026-07-23.

- **ntfy stays byte-identical** — `send(recipient=topic_str, …)` unchanged; WhatsApp rides a
  companion `whatsapp=` kwarg plus `WhatsAppFacts(subject,status,detail,timestamp)`. That
  kwarg is deliberately how "widen the recipient to a value object" was realised, so the
  `str` recipient a dozen tests assert on survives.
- **WhatsApp can NEVER break a page** — never primary, fully exception-wrapped; a bad
  token/timeout/4xx is logged only. `MultiNotifier.send` returns the ntfy result and
  `.channel`/`alert_log.channel` stay `ntfy`.
- **CENTRAL-ONLY by construction**: built only when a `store` is passed (it reads live config
  from `app_settings`); the edge passes none → a bare `NtfyNotifier`.
- **Recipients are PER-LOGIN-ACCOUNT** (`users.whatsapp_number`, E.164) via
  `store.org_role_whatsapp(org, role)` — the analog of `org_role_topic` — so WhatsApp rides
  wherever that role's ntfy topic rides. `emit` resolves WORKER numbers itself on the PUSH
  path, so the six paging shells needed no change.
- **Config is the SUPERADMIN's, not env**: toggle + token + phone-id + template/lang/version
  in `app_settings` (Settings → Platform), read FRESH each send so no restart is needed;
  `WISP_*` are fallback defaults only. Token is WRITE-ONLY in the API (`token_set` boolean;
  blank leaves the stored one). Numbers are set per account in Accounts
  (`/api/users/whatsapp`, self-service like the password route, so worker-reachable).
  Tests: `unit/test_whatsapp`, `integration/test_central_whatsapp`.

### Alerting subsystems

- **Port alarms** (`central/ports.py`): monitored-only, admin-down silent, one alarm not two —
  a port-down folds into the open outage via `stamp_outage_cause` COALESCE (never clobbers a
  post-mortem); no open outage = heads-up; SNMP never opens an outage. Gated
  `cfg.snmp_alerts`, state always written. **Bandwidth has floor AND ceiling**
  (`bw_threshold_mbps`/`bw_max_mbps`, `snmp_bw_consecutive` walks to alarm); never judged on a
  down port; gated `cfg.snmp_bw_alerts`.
- **Perf baseline** (`central/perf.py`): median+MAD over a bounded per-device ring buffer
  (`device_perf_samples` — NOT the hourly rollup; an hourly average smears the slowdown).
  Badge persisted, clears on hard-DOWN, operator-only, gated `cfg.perf_alerts`.
- **On-backup redundancy**: the engine already computes it via `effective_parents()`.
  `org_device_links` (`kind='backup'`, cycle-checked over the FULL edge set);
  `redundancy.sweep` pages enter/leave, never opens an outage, gated `cfg.backup_alerts`.
- **Rollups**: `analytics.device_reliability` (`/api/analytics?days=`) is pure outage math,
  every active device, UNREACHABLE excluded. `rollup.py` (`/api/analytics/trend`) is hourly
  buckets, 30-day retention, pruned daily.
- **Incident shape** (`central/incidents.py`): open-outage waves (15-min start gaps) ×
  independent branches (a root = a down device whose parent is NOT down, judged against the
  FULL down set so victims of an older outage never count) × geography. ≥2 branches inside
  3 km = "power"; 1 branch = "upstream"; scattered multi-branch = SILENT (no verdict beats a
  wrong one). ANNOTATION ONLY — never mutes, reroutes, or replaces a page.

### PON / ONU

- **PON mass-drop verdicts** (`ponfault.py` pure math, `ponalert.py` paging shell):
  `onu_optics.last_online_at` FREEZES when an ONU leaves `online`, so "≥3 ONUs on one PON dark
  with a recent last_online_at" IS the event — no history table. Dying-gasp majority = POWER
  (recorded, NEVER pages — the whole point is not rolling a splicing crew for the DISCOM); LOS
  majority = fiber, cut bracketed (max-online-short-of-dark, min-dark] in RANGING metres
  (optical path with slack — always a stretch, never a point). An OLT whose walk is >15 min
  stale is skipped (the ICMP outage owns that page). Transition-only via `pon_fault_state`,
  gated `cfg.pon_fault_alerts`, state written even when gated off. Suspect naming = the
  deepest passive whose route distance (link_routes geometry, chord where undrawn, ABORTS on
  any unplaced hop — never fabricate) lands in the interval. **Hardware gap:** the C-Data/DBC
  EPON fleet reports only online/offline — never dying_gasp/LOS — so POWER can't fire there
  and area power cuts page as "fiber" (simultaneous multi-PON/multi-OLT drops are the tell).
  Tests: `unit/test_ponfault`, `integration/test_central_ponalert`.
- **ONU-roster hygiene** (`onuroster.py` math, `onualert.py` shell), both transition-only on
  the same optics fold:
  - **Per-PON cap** — EPON tops out at 1:64, so a PON at its limit pages "at capacity"
    (`cfg.onu_pon_limit` 64; per-OLT `org_devices.onu_pon_limit` override so a 1:128 GPON box
    doesn't false-page). `list_org_devices` MUST carry that column or the override silently
    no-ops in paging.
  - **Redundant MAC** — a serial/MAC on ≥2 slots org-wide is a duplicate, but it PAGES only
    when ≥2 are ONLINE at once: C-Data reg tables keep every slot an ONU ever occupied, so the
    byreddy fleet had 178 "duplicates" of which 2 were live clones. Dead-member dups are
    history — state-only, never ntfy.
  - Both read `onuroster.current_roster` — per OLT, rows from the FRESHEST walk (identical
    `updated_at`), skipping an OLT staler than 900s — because `onu_optics` NEVER deletes
    removed-ONU rows, so raw counts over-count the cap and fake duplicates off zombies.
  - **A stale OLT FREEZES its alert states, never clears them** (dup absence checked against
    the staleness-blind `duplicate_macs(stale_s=None)` shadow; capacity clears need the device
    in `fresh_device_ids`) — clearing on staleness re-paged 178 MACs every time a slow walk
    stalled. Tests: `unit/test_onuroster`, `integration/test_central_onualert`.
- **ONU lookup by MAC/name** (`/api/inventory/onu-search`): a tech holds the sticker MAC or
  the provisioned name, neither of which reached anything before — the roster was visible only
  once you knew which OLT to open, the thing being looked up. Hits deep-link into that OLT's
  Optical tab via `focusOnuId`; the OLT stays in the tree (`filterWithAncestors`' `extraIds`).
  **`search_key` (alphanumeric, upper) is SEARCH-only and must never replace `_norm_mac`** —
  identity stays separator-exact, or differently-punctuated strings collapse into fabricated
  dup-MAC pages. It's a registered SQLite function (`wisp_search_key`), not a REPLACE chain:
  the chain knew only the four MAC separators and silently missed the `_` in a real name like
  `hc_kiran`, and ONE normalizer is what guarantees a typed `%`/`_` can't reach LIKE. Searches
  the CURRENT roster, not raw `onu_optics` — a zombie slot would deep-link to a tab that
  doesn't list it. 3-char floor, 50-row cap. Tests: `integration/test_central_onusearch`.
- **GPON vendor auto-detects from sysObjectID; unmatched = optics OFF, never guess** (a
  fabricated dBm is the DBC placeholder trap). `GponPollerPool.resolve` precedence: device
  `gpon_vendor` override > `WISP_GPON_VENDOR` > sysObjectID longest-prefix > None. Detection
  cached per device (1h ok / 15min on silence — catches a hardware swap), inside the SNMP
  semaphore, one lazy engine. Tests: `unit/test_gpon`.
- **GPON profiles are DATA** (`gpon_profiles`), served in the `/edge/devices` reply. Built-in
  callables travel as a CLOSED vocabulary (`state_map`+`state_default`, `pon_index`
  `as_is|first_segment`, `pon_label` template); `gpon_profile_from_dict` rejects the WHOLE
  profile on anything outside it — never a best-effort partial. A same-named row shadows a
  built-in (huawei/dbc stay in edge code as fallbacks for older fleets). `set_profiles` runs
  every cycle and MUST stay a fingerprint-gated no-op on an unchanged payload — rebuilding
  pollers churns SnmpEngines (the leak invariant).

### C-Data / DBC: two hardware truths (don't re-derive)

- **Per-ONU Rx exists NOWHERE in that firmware's SNMP.** FINAL — warm-capture against web-UI
  truth values, then the whole vendor ONU area swept (`…5.12.1.27`–`.33`): the only optical
  column is the OLT's burst-receiver level, uncorrelated with the ONUs it indexes, and it
  covered *exactly* the one PON that had been opened in a browser. The OLT queries each ONU
  live over EPON-OAM when you open OPM Diag and stores nothing. Blank Rx on the `dbc` profile
  is CORRECT; the web page is the only source. Tool for the next vendor: `oidhunt.py`
  (correlates a walk dump against a pasted web table by MAC join).
- **`distance_m` on the `dbc` profile is RTT in EPON time quanta, NOT metres.** The OLT's own
  ONU-list page heads that column `RTT(TQ)`, matching our stored values. True metres =
  `1.6393 × TQ − 157` — so **every fibre-cut bracket we have printed is ~39% short**, and
  splicing crews quote drum off those. The OLT publishes real metres itself in field 14 of the
  `.31.1.3` CSV. Parsing a packed CSV is outside the profile vocabulary, so the
  exact fix needs edge code; `scales.distance = 1.6393` is the zero-rollout approximation
  (157 m high). Don't "fix" this by widening the scale vocabulary alone.

### Per-ONU Rx from the OLT's web UI

`central/weboptics.py` (transport+parse+merge) + `weboptics_sweep.py` (shell). CENTRAL-ONLY by
construction: it rides the web-proxy tunnel the edge already serves — no edge code, no rollout.

- **The scrape is an INPUT to the optics fold, never a second pipeline.** Readings land in
  `onu_web_optics` (its own table — the two run on independent clocks, and one row carrying both
  would make "which half is fresh?" unanswerable), and `_merge_web_optics` folds them in BEFORE
  `CentralOpticsMonitor.sync_device`. Severity, the OLT badge, PON-fault and the Optical tab
  stay ONE path that never learns where a number came from. Don't teach it a second source.
- **Merge is by MAC, ONLINE slots only, ambiguity DROPPED.** MAC survives the two firmware
  subsystems disagreeing about slot numbering; the online restriction disposes of the zombie
  problem for free. A still-ambiguous MAC gets NOTHING — a reading pinned to the wrong drop
  sends a tech to the wrong house. Matching is punctuation-blind (`_match_key`), a THIRD
  normalizer beside `_norm_mac` (identity) and `search_key` (search), deliberately: exact
  matching across two views would merge nothing while looking healthy, and a blank Rx column
  reads as "this vendor has no Rx" — the exact false negative this feature exists to kill.
- **`distance_m` is stored but NOT merged, on purpose.** The page carries metres, the dbc
  profile time quanta, and the page returns only ONLINE ONUs — so `onu_optics.distance_m` would
  go metres-for-survivors / quanta-for-dark, and `ponfault` brackets a cut between exactly those
  two groups. A mixed-unit interval INVERTS; a uniformly wrong one stays monotonic. Fix the UNIT
  first. `_MERGED_FIELDS` is the knob.
- **SNMP stays authoritative for the roster.** A scrape can never add an ONU and a blank scraped
  column never erases a walked value — which is what makes a partial scrape safe (the OLT holds
  ONE session slot, so ending early is normal). UPSERT, never delete-then-insert. Freshness is
  judged on CENTRAL's clock, NOT the report's `ts` (that rides in from the edge, and a probe
  with a slow clock would discard seconds-old readings). Past `web_optics_max_age_s` (1h)
  readings are dropped whole rather than aged in — a badge is a claim about now.
- **The sweeper's restraint is the feature.** Own slow clock (`web_optics_interval_s`, 900s — Rx
  drifts over days), strictly sequential, per-OLT lock. SKIPS a node whose tunnel isn't
  long-polling, and an OLT someone is actively browsing (that firmware keeps no cookie and holds
  one session slot, so scraping mid-browse logs the operator out of the box they're working on).
  Handed NO notifier, so a failed scrape can page nobody by construction.
- **The browse gate keys on ACTIVELY browsing, not "has a session"**: nothing tells central a tab
  closed (the tunnel is request/response), so an abandoned session lived its full
  `proxy_session_ttl_s` (600s) — and the gate is per-NODE, so one forgotten tab suppressed the
  optical read of EVERY OLT behind that probe, indefinitely against a self-refreshing device UI.
  Three parts, the second the one that always holds: `last_used_at` + an `idle_s` arg to
  `active_sessions_for` that the sweeper passes (`web_optics_browse_idle_s`, 180s) and the EDGE
  path deliberately does not; `ProxyHub.reap_expired` at the top of each sweep (sessions were
  only ever dropped when something looked one up, and the browser is what went away); and the
  dashboard closing a session when its tab vanishes (`watchSessionTab`) — a nicety, deliberately
  NOT hooked to `pagehide`, which fires on a reload and would kill a session in use elsewhere.
  `has_session` is expiry-aware for the same reason: it fed the "live" badge and pulsing globe.
- **The 15-minute clock is right for the VALUE and wrong for the MOMENT.** Rx drifts over days so
  the sweep shouldn't hurry — but a tech at a pole who just reseated a connector needs the answer
  now, and "wait 15 minutes" is how a diagnosis becomes a second site visit.
  `POST /api/inventory/rx-refresh` (owner-only — it spends the OLT's stored admin login) drives
  `scrape_one` on the SAME sweeper instance so the per-OLT lock covers both. It widens WHO may
  ask and WHEN, never what a read may do: every gate applies unchanged, and eligibility has
  exactly ONE source — `WebOpticsSweeper.target()`, which the route gates on AND `rx_status`
  answers `can_refresh` from, because the button, the route and the sweep must agree about what
  is readable. An ineligible device is refused 400 and records NOTHING (a `web_optics_status` row
  reports an ATTEMPT; writing "you can't read this" would erase what really happened last). It
  answers at once and scrapes on a thread — one OLT costs up to `web_optics_device_budget_s`
  (120s), and a request held that long is a browser timeout. A second click 409s rather than
  racing the lock and overwriting a good verdict.
- **ELIGIBILITY IS THE VENDOR PROFILE, NOT THE DROPDOWN** — this took the subsystem off PYLON and
  onto the fleet (2 targets → 12, ~1,030 more ONUs with a real dBm). `web_optics_targets` accepts
  an explicit `gpon_vendor='dbc'` OR the edge's own sysObjectID match, which arrives on every
  report as `device_snmp_status(subsystem='optics').profile` + `.sysobjectid`. The original gate
  was justified by "auto-detection lives on the edge and is never reported to central" — simply
  WRONG, and it cost this feature a fleet: 13 of 19 OLTs were already stamped `dbc` off the
  C-Data PEN arc `1.3.6.1.4.1.37950` while only 3 carried the hand-typed vendor. Detection is the
  STRONGER signal — the box's maker ID versus a human's recollection. Requiring `sysobjectid`
  non-empty is load-bearing: `profile` is echoed for an override too, so a fleet-wide
  `WISP_GPON_VENDOR` could otherwise launder itself into a "detection".
- **A roster is required, and PON COUNT COMES FROM IT.** `DEFAULT_PONS = (1,2,3,4)` was PYLON's
  port count read as the fleet's; the same firmware ships 3–8 PONs with GAPS (HILL-OLT-1 runs
  1,3,4,5,6,7,8), so widening the vendor gate alone would have skipped over half the fleet's
  online ONUs **while logging success** — and blank Rx meaning "we never asked" is
  indistinguishable on screen from "this vendor has none". `pon_indices` parses the roster's
  distinct `pon_port` labels (`EPON0/3` → 3; junk like a bare `60` or an empty label DROPPED,
  never guessed); `DEFAULT_PONS` survives only as the fallback for an unrecognised shape.
  Requiring a roster follows from the merge: readings land ONTO walked slots and can never create
  one, so an OLT with no roster has nothing a scrape could surface.
- **A SENSOR RAIL IS NOT A READING** (`_sane_optics`). An ONU whose diagnostics are dead prints
  the raw register on every DDM field at once: 0xFFFF reads 6.55 V / 131.07 mA / **+8.16 dBm**,
  0x0000 reads 0.0 V / 0.22 mA / **−40.0 dBm**. Both were live on HILL-OLT-1 and they fail
  OPPOSITE ways, which is why a range check on Rx alone won't do: +8.16 grades comfortably `ok`
  (dead optics rendering as the healthiest drop on the PON) while −40.0 grades `crit` and joins
  an OPTICAL_CRIT page. **Supply voltage is the discriminator, not any optical threshold** — an
  ONU's rail is 3.3 V by design on every ONU ever built, the one field whose correct value is
  known a priori. Outside 2.0–5.0 V the WHOLE optical block is blanked, because these fields rail
  together and trusting a Tx from the same dead register just moves the lie one column over.
  Identity survives; only readings go. The guard rejects RAILS, never merely-bad optics (PYLON's
  −28.24 crit and −2.87 over-driven ONU both stay). `DdmRailTest`.
- **The login-page GET is a GATE, not a preamble.** Its reply used to be discarded, so the admin
  credential went out no matter what answered — tolerable for one hand-verified OLT, not once
  eligibility is inferred fleet-wide. A non-OK reply aborts with "credentials NOT sent", and a
  404 on `OPM_PATH` is reported as "this firmware has no OPM Diag page" rather than a retryable
  fault. `web_optics_device_budget_s` bounds one OLT's whole scrape; the per-request timeout only
  ever bounded a hop.
- **THE VENDOR RECIPE IS DATA** (`web_optics_profiles`) — the third profile table, done like
  `gpon_profiles`: CLOSED vocabulary, whole profile REJECTED on anything outside it, org_id NULL
  = global, a same-named row SHADOWS the built-in, a DISABLED row switches it off (a tombstone,
  not an absence, or the toggle would lie on exactly the OLTs that shipped with a built-in).
  `weboptics.py` keeps every vendor name as the DBC built-in's default argument, so the one
  field-verified path stays byte-identical while the module is profile-driven. `name` is
  deliberately the SAME token as `gpon_profiles.name` / `org_devices.gpon_vendor` — a second
  web-only notion of "which vendor is this" could disagree with the first about the same OLT.
  Four vocabulary choices, each learned the hard way: (1) columns map BY HEADING, with
  `column_order` only as the positional fallback for a header-less table — and a profile that
  declares headings, meets a page with none, and has no order reads NOTHING rather than guessing
  (`ColumnMappingTest`), because position-mapping is how transmit power gets reported as received
  power, a confident lie; (2) the session is a FLOW (`rotating-key` | `cookie`), not a regex;
  (3) charset is per-vendor (`gb2312` here) and getting it wrong looks like "no optics"; (4) a
  profile may NEVER carry a host — `_clean_path` refuses a URL outright rather than stripping it,
  because path-only is what stops the tunnel being a lateral-movement primitive. Keep the
  live-browse skip unconditional even for vendors allowing concurrent sessions. `_sane_optics`
  takes `expect_voltage` from whether the PROFILE maps a voltage column — the rail guard keys on
  a rail every ONU has by design but not every page prints, and running it blind would blank
  every reading such a vendor produced. An ABSENT column is a fact about the firmware; a MISSING
  value in a column that exists is a fact about the ONU, and only the second is a dead sensor.
- **A SCRAPE'S OUTCOME IS PERSISTED, NOT JUST LOGGED** (`web_optics_status`). It used to live
  only in the log, so a blank dBm column had no explanation a user could reach — and "this vendor
  has no Rx", "nobody typed the OLT's password" and "the scrape has been failing for a day"
  render identically as an empty column while taking OPPOSITE actions. Every outcome INCLUDING
  skips is recorded under a closed vocabulary (ok | partial | skipped | no_profile |
  no_credentials | unreachable | login | error); `last_ok_at` survives a failure so a panel can
  say "was working until <ts>". The SPA composes the sentence from facts rather than the server
  shipping a verdict.
- **Next profile to write is C-Data's own GPON build; `proxy_audit` is how to find its page.**
  Gpon_04/Gpon_08 are the SAME vendor and SAME `/action/` UI (login flow and key rotation carry
  over) but `/action/onuopmdiag.html` 404s — that build's menu is GPON vocabulary and its optical
  page is elsewhere under another name. DO NOT guess it; a guessed page that parses is how a
  fabricated dBm ships. `proxy_audit` records every path a human opens through the tunnel, so an
  operator clicking the optics menu once writes the answer into the DB — the same table that
  proved the EPON path live on NLK-OLT/NDN-OLT. Those boxes also have no SNMP roster, so they're
  ineligible on that ground too until their walk works.
- Tests: `unit/test_weboptics`, `unit/test_weboptics_profiles`, `unit/test_weboptics_sweep`,
  `integration/test_central_weboptics`, `integration/test_central_rxstatus`.

### Topology extras

- **Passive plant lives in org_devices** (`inventory.PASSIVE_TYPES` = splitter/fdb/closure),
  NOT a second registry — parent chains, map pins and routes all come free. `ip_address` stays
  `''`; validation rejects an IP/probe on a passive and a PASSIVE PARENT for a monitored device
  (passive-under-passive is fine — plant hangs below gear). Containment is
  `org_device_topology` (the single choke point: engine FSMs, the rebuild fingerprint — adding
  a splitter must NOT re-page — and `/edge/devices`) plus a `device_reliability` skip.
  `org_devices.pon_port` (passives only) binds a splitter to its PON for suspect naming.
- **Peer links are a KIND, not a second graph**: real plant isn't a tree — aggregation switches
  cross-link. Those ride `org_device_links` under `kind='peer'`, canonicalized to
  `(min, max)` so one cable is ONE row whichever end declared it; `list_org_devices` expands it
  symmetrically into `peer_ids`. **Invisible to the engine by construction** — every dependency
  read path already filters `kind='backup'`, so declaring cabling can NEVER rebuild an engine
  or re-page a fleet (`test_central_peerlinks` pins both directions). Deliberately **no cycle
  check** — a ring of cross-links IS a cycle, which is the whole reason they can't be backup
  edges. A pair already joined by any edge is refused, so port bindings are never ambiguous.
  Peers are **descriptive only**: whether traffic reroutes over one depends on STP/routing
  state central can't see, so an operator wanting real failover declares a backup uplink in the
  direction traffic actually reroutes. Port binding uses `uplink_device_id` on BOTH ends, never
  `feeds_device_id` — a cross-link dropping is not the peer's outage cause.
- **Port-level links live ON switch_ports, never a second table**: the parent side is
  `feeds_device_id` (the SAME column ports.py folds a port-down through), the child side its
  mirror `uplink_device_id` — one registry, so the map's bandwidth labels and outage folding
  can't disagree about which port carries a link. Both are operator columns and the walk upsert
  deliberately omits them (a sweep must not clobber declared cabling). The picker is the device
  panel's Uplinks section, also the ONLY backup-link ("ring") management UI — **collapsed by
  default**, since cabling is reference material, not status, and nothing alarm-shaped hides in
  the fold (a down uplink port is already a tree-row chip and a Ports-tab row). Tests:
  `integration/test_central_linkports`.

## Device web-UI proxy (reverse tunnel through the edge)

Drive a switch/OLT's native web UI from the dashboard: **browser → central → edge → device**.
The edge is already central's hands on the LAN for SNMP; this makes it central's hands for
HTTP. Shipped 2026-07-16, field-proven; `weboptics` rides it.

- **Activation is the per-org `orgs.web_proxy` flag** (default off, superadmin-set).
  `WISP_PROXY_ENABLED` defaults ON since v0.15.8 and is only the emergency kill switch — the
  original double-dark shipped every fresh edge with the tunnel off, surfacing as an
  undiagnosable 504. The edge stays DORMANT until a `/report` reply carries a live session, so
  default-on costs zero idle long-polls.
- **Diagnosing a failure: 504 = the edge never picked up; 502 = the edge fetched and failed.**
  Many OLTs refuse port 80 — create the session with `port: 443`.
- **Request headers forward browser→device** (`_forward_headers`) on an allow-list: device
  cookies travel, central's `wisp_central_session` is stripped; Referer/Origin rewritten to the
  device origin (firmware CSRF checks); Authorization forwarded only for `Basic` (a bearer
  would be central's own token). Without this, logins bounce.
- **Escape rescue** (`server.py:_proxy_rescue`): a JS-built root-absolute URL lands on central
  as an unknown route; if the Referer names a LIVE session it 307s back inside the prefix
  (method+body preserved). Path-prefix rewriting will never fully tame JS-built absolute URLs —
  wildcard-host is the real fix if this keeps biting.
- **One tunnel per probe**: `session_create` replaces whatever was open on that node, so the
  operator never hunts a forgotten session.
- **Owner-only** (`_PROXY_ROLES`, also behind the worker whitelist), org-flag gated, every path
  audited to `proxy_audit` — which doubles as the cheapest way to discover a vendor's page
  names.
- **Known limits, honest:** long-polls consume central worker threads while a session is live —
  fine for a handful of techs, not hundreds of always-open tunnels; the dormant-until-requested
  model is what keeps this bounded, so don't make it always-on. Cold start is up to one report
  interval. This is a genuine threat-model change for the edge: the allow-list + audit +
  opt-in-session + capability-flag controls are the price of admission, and shipping any subset
  is not acceptable.
- Tests: `unit/test_webproxy`, `integration/test_central_proxy`.

## Central management plane

- **`org_devices` is THE device table.** The single-box `devices`/`rollups` tables and
  `POST /ingest` are DELETED — don't reintroduce a second registry. `events` survives:
  central-originated log lines only.
- `central/inventory.py` is pure validation, no storage; `clean_device_payload`'s `parents` map
  is pre-scoped to one org by the caller.
- **Every `org_devices` write re-derives org from the DB row** via `store.device_org(id)` (body
  `org_id` trusted only on create); same for `switch_ports`, feeds, links.
- `orgs.ntfy_topic_owner/worker` (outage routing) are separate from `orgs.ntfy_topic`
  (fleet-watchdog NODE_STALE/OK) — don't merge.
- **New columns on existing tables need `_ensure_columns`** in `CentralStore.__init__` or an
  existing `central.db` keeps the old schema. New tables need nothing.
- `make_server`/`_make_handler` take an injectable `notifier` — tests inject a recording double;
  follow this for anything central sends.
- **Routes live in `central/api/` route tables**, not `server.py` if/elif chains:
  `api/__init__.py` maps exact paths to handlers in `api/{edge,orgs,users,fleet,devices,
  outages}.py`. GET handlers are `fn(h, qs)`, dashboard POSTs `fn(h, user, body)`; `h` carries
  `cfg/store/notifier/registry` plus `_reply`/`_reader`/`_scope_org`/`_can_write`. `server.py`
  keeps only transport, auth helpers, static/SSE/download serving, login, and edge-ingest
  special cases. Adding an endpoint = a function + a table row. `api/common.py:DENIED` is the
  "403 already sent" sentinel — don't test scope helpers with `if not org` (superadmin
  legitimately yields org=None).
- **Every JSON API reply is `Cache-Control: no-store`** (`server.py:_reply`). With no header a
  200 GET is heuristically cacheable and `_reply` sends no validator, so a browser can serve a
  stale body without ever asking. Surfaced on ONU search, whose URL carries the needle: a
  `?q=BSNL` answered while search was still serial-only pinned its EMPTY reply to that string,
  so "BSNL" stayed blank while "BSN" worked. Freshness is react-query's job in memory, never
  the HTTP cache's.
- **Deleting an org sweeps DISCOVERED tables, and `org_devices` goes LAST**
  (`store_orgs.delete_org`). Org-scoped tables are found by introspecting for an `org_id`
  column rather than listed: this schema grows a table most months, a hardcoded list silently
  orphans rows in whatever was added last, and org ids are REUSABLE — those rows would surface
  inside a later org of the same name. `org_id IS NULL` is global by construction and the
  equality match spares it. Order matters because `_connect` runs `PRAGMA foreign_keys=ON` and
  every FK points at `org_devices(id)`: sweeping it alphabetically dangles those rows and
  SQLite aborts. Constraints stay IMMEDIATE — a violation surviving the ordering means a
  cross-org reference, which should fail loudly. Superadmin-only AND gated on an ECHOED org id,
  enforced server-side so a SPA refactor can't drop it; `EngineRegistry.forget` evicts the
  in-memory engine. Deliberately NO tombstone: `_ensure_org` on the ingest path IS how a probe
  bootstraps its org, so an edge still pointed at a deleted org re-creates an empty shell — the
  dialog says to uninstall the probe rather than break self-enrollment for everyone. Tests:
  `integration/test_central_orgdelete`.
- **Superadmin Overview = coverage, not alarms** (`/api/admin/overview`): per-org
  SNMP/optics/ports enabled-vs-working (working = a reading fresher than 900s); never-reported
  vs gone-stale are distinct reasons; optics/ports problems suppressed when SNMP is dead
  outright. Pure read-side, never pages.

### Roles: owner and worker (only)

`auth.ROLES`. The read-only `operator`/`tech` roles were removed — an org has people who run it
and people in the field, nothing in between. `store._collapse_roles` migrates existing accounts
to `worker` on open; a row on a role outside `ROLES` fails every check and reads as a broken
login.

- **The operator ntfy TOPIC became the worker topic, value carried across**
  (`ntfy_topic_operator` → `ntfy_topic_worker`) — that column is where nearly every page
  actually lands, so dropping it would have silenced a live fleet until every handset
  re-subscribed. The dead `ntfy_topic_operator`/`_tech` columns linger unread in upgraded DBs;
  don't "clean them up" into a migration.
- **Worker scope is ONE choke point**, `server.py:_WORKER_ROUTES`: a worker reaches
  me/outages/SSE/ack/post-mortem/own-password and 403s on every other `/api/*`, so a NEW route
  is worker-blocked by default — widen the set deliberately.
- **Every worker check gates on IDENTITY before role — server AND SPA.** A superadmin is
  `org_id IS NULL` and its `role` is meaningless, so it must be exempted first, in BOTH
  `_worker_blocked` and `require-auth.tsx`. When the collapse made the org default `worker` the
  superadmin row flipped too: the server 403'd it off every `/api/*` and the SPA served the
  platform admin the stripped field view with no way back. Belt AND braces: `create_user`
  forces `owner` for `org_id IS NULL`, and `_collapse_roles` both SPARES superadmins and
  REPAIRS any already flipped. Pinned by `test_superadmin_is_never_worker_blocked`,
  `test_superadmin_provisioned_bare_is_not_a_worker`,
  `test_role_collapse_spares_and_repairs_superadmins`.
- Ack/post-mortem rights = `api/common.py:can_triage` (every role now, but it stays its own
  predicate because WRITE rights are still owner-only: a worker triages, it doesn't
  reconfigure). The SPA routes worker sessions to `routes/worker-page.tsx` for EVERY path.
  Since workers never reach the shell, every visitor to the dashboard has `canWrite`.

### Billing / payments

- **Paywall** (`central/billing.py`): plans free/pro/vip on `orgs.plan`, paid months in
  `org_billing_months` ('YYYY-MM' UTC; pre-marking future months IS the "no reminder" switch).
  A pro/vip org whose CURRENT month is unpaid 402s on every `/api/*` except `_BILLING_EXEMPT`
  (me, billing, login/logout, paid-ping + free-plan — what the lock screen needs); **edge
  ingest, monitoring and outage paging are NEVER gated** — a lapsed bill must not silence an
  alarm. Device caps (5/500/∞) enforce on CREATE only (a downgrade never breaks existing
  monitoring); passives never count. Reminders are transition-only (`billing_notices`): owner
  topic at ≤3 days runway and on lock; failed sends retry next sweep, 'skipped' (no topic)
  doesn't. Free never locks.
- **Payments are manual — GPay number or QR, NO gateway.** The superadmin sets a number and
  optionally a QR image (`billing_gpay_number`/`billing_qr_image`, a `data:image/…` URI capped
  ~512 KB); both ride the `/api/billing` reply. An org pays out-of-band then taps **"I've
  paid"** → `POST /api/billing/paid` (billing-exempt, ANY signed-in member — everyone sees the
  lock screen) pushing to the dedicated payments channel (`billing_paid_topic`, blank falls
  back to `cfg.central_ntfy_topic`; the ONE choke point is `orgs._admin_payments_topic`). The
  admin then marks the month by hand. NO ledger, NO settlement callback — the admin IS the
  confirmation; the button only sends a heads-up (`notified:false` when unconfigured, never an
  error). `POST /api/billing/plan` accepts ONLY 'free' (the no-payment escape hatch).
- Tests: `unit/test_billing`, `integration/test_central_billing`.

## Dashboard (web/ → central/static/)

Built output is committed (`cd web && npm install && npm run build`); `./run.sh` needs no Node.

- **`/` = marketing landing, SPA at `/app`.** `landing.html`'s SOURCE is
  `web/public/landing.html` (vite copies it) — edit there, never the built copy. Marketing
  overlays are SERVER-INJECTED (`_inject_showcase`, gated `WISP_SHOWCASE`) because the bundle
  rebuilds its whole DOM; `showcase.js` re-mounts via MutationObserver.
- **`HashRouter`, not `BrowserRouter`** — the server 404s non-file paths, no SPA fallback.
- **No frontend test suite** — verify via `tsc --noEmit`, `npm run build`, manual Playwright
  (in `web/node_modules`, run from inside `web/`).
- **Mockup-only fakes — don't "finish" them**: Clients online, manual Resolve, Docker install,
  Notification history.

### Theme & palette

- **Minimal-gray, dark default, WARM-SLATE**: canvas `#0c0e12`, card `#1c1f24`, raised
  `#24272e`; surface steps + borders, never shadows; desaturated accents so status colors stay
  loudest. Spacing is 8px-grid GCP-loose (`h-11` rows, `px-4`–`px-5`) — density comes from
  filling width, not shaving padding. **Type scale is rem-only** (`--text-2xs` 12px,
  `--text-xs` 13px, `--text-sm` 15px, root scales up ≥1600px) — a `text-[12px]` literal opts
  out. A resolved outage pending post-mortem renders NEUTRAL, never green.
- **Colors are OPERATOR-SETTABLE — a palette ask is a dashboard task, not a code edit.**
  Settings → Platform → Appearance (superadmin, server-wide) writes
  `app_settings.theme_overrides`; `central/theme.py` validates, `server.py:_inject_theme`
  renders it into a `<style id="wisp-theme">` at the END of `<head>`. **Check whether the ask
  is just this panel before touching index.css** — that was the whole point. index.css stays
  the DEFAULT and the design record.
  - **Storage is a SPARSE DIFF, never a snapshot** — an untouched seed emits nothing, so a
    stock install is byte-identical to the shipped CSS and future palette work still reaches
    every token an operator hasn't taken over. Writing a full palette on first save would
    freeze each install on that day's colors. `{}` CLEARS the row (the reset); the key absent
    from the POST means "leave colors alone" (so saving the Maps key can't wipe them).
  - The UI edits ~7 SEEDS, not 44 raw tokens: `lib/theme-tokens.ts` holds the OKLab math and
    re-derives each family, so the surface ladder's measured steps survive an operator drag —
    and `readableInk` picks every `*-foreground` by CONTRAST MEASUREMENT, which keeps
    "`--primary-foreground` is dark ink, not white" true for colors nobody has picked yet.
    `ADVANCED_TOKENS` is the raw escape hatch.
  - **`theme.py:_TOKENS` and the SPA's `ALL_TOKENS` are two hand-kept lists** — a token added
    on one side only is silently dropped on save (presents as "the color didn't stick");
    `test_allowlist_matches_spa` reads the TS source to pin them, which is why tone tokens are
    spelled out as literals rather than built from a prefix. The allowlist + value regex are a
    SECURITY boundary (values land in a `<style>` block), re-applied on READ so a hand-edited
    DB row can't reach the page. Injected rather than fetched so there's no flash of the
    default palette and the pre-session login/lock screens are themed.
  - **Mode scoping broke dark mode TWICE — get both halves right.** (1) The live preview must
    be CSS in a `<style>`, never inline styles on `<html>`: inline root styles outrank EVERY
    stylesheet rule including `.dark{}` and carry no mode, so a light preview survived a toggle
    to dark. (2) Selectors must be `:root:not(.dark)` / `:root.dark`, NOT index.css's
    `:root`/`.dark` pair — those have EQUAL specificity (0,1,0), so an injected `:root{}` of
    light beats the bundle's `.dark{}` on source order and applies IN DARK MODE. The symptom is
    nasty because it's partial: customised tokens flip to light while untouched ones stay dark
    → light text on white. The `:not()`/`.dark` pair is (0,2,0) and mutually exclusive, so
    placement stops mattering. index.css keeps the simple pair because it controls its own
    source order; an injected layer can't assume that. Pinned by
    `test_mode_selectors_are_mutually_exclusive_and_outrank_the_bundle` and
    `test_preview_never_writes_inline_root_styles`. **Verify a palette change in a REAL browser
    in BOTH modes** — both regressions passed static review and unit tests.
  - Tests: `unit/test_theme`, `integration/test_central_theme`.
- **Two things about the warm-dark palette are load-bearing.** (1) `--primary-foreground` is
  DARK ink (`#0b1920`), not white — the steel blue sits at L*≈68, so white on a filled primary
  button measures 2.5:1 and fails AA; a later "restore white for contrast" would invert the
  measurement. (2) The border alphas did NOT change — `rgba(255,255,255,.10)` resolves to
  exactly `#31353d` on the card, which is the argument for alpha borders rather than hex, so
  don't re-hex them to the swatch. Also: **`--muted` no longer equals `--background`** (at the
  ~8.3 ΔL* gap a canvas-colored well inside a card reads as a black band, not a recess), and
  the canvas sits within ~0.4 ΔL* of the near-black the halation argument rejects, so **a
  further "darker" ask should raise card with it rather than widen the gap again.** Promoting
  the hover wash (`#26292d`) to `--card` was tried and REVERTED within the hour ("too bright"):
  a hover tone is a transient highlight, not a resting surface. Don't redo it.
- **Two choices from the NOC design file were deliberately NOT taken** and must not be
  re-imported: **the font** (it specifies Geist; Geist was tried and pulled 2026-07-09 because
  operators found it hard to read — UI text stays **Inter Variable**; Geist MONO is fine, the
  mono was never the complaint), and **the type scale** (it runs 13px body / 11px labels; the
  app keeps its raised floor, and there is deliberately no 3xs step). Both were the operator's
  explicit call. Borders are **alpha, not hex**, so one token holds a constant relationship on
  canvas, card, popover AND raster map tiles; `--border-subtle` is the INTERNAL rule inside an
  already-bordered object, and keeping it quieter than `--border` is most of why a panel reads
  as one clean object. `--ghost-foreground` is the 4th text step below `--faint-foreground`.
- **Surface ladder**: `--muted` sits BELOW its surface (wells recess), `--popover` is THE
  raised/focus surface (drill-in, map chrome, menus), `--accent` is the interaction fill on
  raised surfaces — so a fill meaning "hover/selected/skeleton" must use accent, never muted.
  Row hover is `hover:bg-foreground/5` (works on every surface); selection is `.wisp-drillin`
  (popover bg + `--border-strong` outline; NO colored rail — tried, rejected). Hover ≠
  selected; keep adjacent surfaces ≥ ~3 ΔL* (they were 1.017:1 once). Faint text =
  `text-faint-foreground`, not opacity hacks; maint/stale chips render neutral, never amber.
- **Status tone is ONE formula** (`components/status-badge.tsx`): tone color for text, same hue
  at 13% fill, 30% edge — shared by `Chip`, `StateBadge`/`TonePill` and `StatusDot`. `info` is
  a real tone: an ACKED outage means a human owns a still-live incident, which muted hid. On
  Home a stat tile is tinted ONLY when something is wrong, so scanning is a search for color
  rather than a read of eight numbers.
- **Operator colours are ONE closed vocabulary** (`lib/palette.ts` ↔ `inventory.PALETTE`):
  violet/magenta/teal/lime/indigo/chalk, shared by the map's per-link palette, TAGS and PROBES.
  One set on purpose (a second would drift the first time either grew) and closed on purpose: a
  free hex lets an operator paint a healthy thing the same red as a broken one, faking an alarm
  on the screens that exist to show them (`test_central_colors` pins the refusal). Nothing here
  is a status tone. Storage is `org_colors (org_id, kind, key, color)`, sparse — no row IS
  uncoloured, clearing is a DELETE — keyed on TEXT, not a foreign key, because a tag exists
  only inside `org_devices.tags` and a probe may live in `node_tokens`, `nodes`, or both. A
  device takes the colour of the FIRST of its tags that has one, falling back to its probe's —
  most-specific-wins, with the operator's own tag order as tie-break because any other rule
  reshuffles colours as tags are added elsewhere. It renders as a left RAIL outside the tree
  indent guides; the status dot, alarm chips and map stroke all still win, so a colour can never
  hide an outage. **Tag names are NOT chipped on the device row**: every other chip in
  `DeviceChips` is a claim about state, and organisational labels beside them are noise. **The
  palette members are STROKE colours** — on light mode's near-white card they measure ~2:1 as
  chip text, so `.wisp-tag` mixes them toward black there; that's why a coloured chip is a class
  taking `--tag` rather than an inline style. Adding another colour-coded thing = a new `kind`,
  never a new palette.
- **Five `wisp-*` primitives carry the repeated patterns** (index.css — don't re-copy their
  Tailwind strings per page; that drift is what makes screens stop looking like one product):
  `.wisp-panel`(+`-head`), `.wisp-row` (the rule under the header is dropped via
  `.wisp-panel-head + .wisp-row`, an adjacent-sibling selector ON PURPOSE because
  `:first-of-type` matches per element type and silently fails when both are `<div>`s — they
  are), `.wisp-eyebrow`, `.wisp-thead` (a muted WELL, so it frames rather than competing with
  row one), `.wisp-frozen`.
- **Page measure is TWO widths, not one per route**: `.wisp-page` (105rem) for anything
  scannable and `.wisp-page--narrow` (66rem) for form-shaped pages where line length hurts. The
  app had six measures (48–105rem) and visibly changed shape as you navigated. Don't add a third.

### Honesty rules (what the UI may and may not claim)

- **A DOWN device's SNMP readings are FROZEN, and must look it.** Ports/vitals/optics rows
  PERSIST — nothing deletes them when a box drops — so a down OLT went on rendering green "up"
  ports, live-looking bit rates and a healthy CPU meter. `format.ts:isDownState` is the trigger,
  NOT the 900s `isFresh` rule: unreachable is proof the data is stale up to 15 minutes before
  staleness would notice. Three layers, answering different questions:
  1. **the panel GRAYS** — `.wisp-frozen` on the readings container in `device-detail.tsx` and
     `optical-panel.tsx`, applied to the SUBTREE rather than per-tone conditionals so a reading
     added later can't opt out by forgetting to check. Always pair it with a live reason OUTSIDE
     the frozen block; gray with no explanation reads as a broken panel.
  2. **the tree row SUPPRESSES** — `DeviceChips` gates every SNMP chip on
     `liveSnmp = !isDown && !isStale(...)`. A chip is a claim about NOW: "port down" on an
     unreachable switch is the outage reported twice.
  3. **the summary EXCLUDES** — `store_snmp._bandwidth_alarms` drops DOWN/UNREACHABLE so the
     top-bar chip and Home tile stop pointing at a dead switch. Display-only.
  **Alarm STATE and PAGING are untouched at every layer** — same discipline as the governor (a
  suppressed chip must never mean a suppressed page). The ICMP verdict itself stays at FULL
  strength — that's live truth, not a stale reading. Pinned by
  `test_bandwidth_summary_hides_alarms_on_an_unreachable_device`.
- **"Nothing is wrong" and "nothing is measured" must never render alike.** A C-Data/DBC OLT
  walks a COMPLETE roster with every `rx_dbm` NULL, so the optics badge goes green and
  crit/warn sit at 0 on a box measuring no light at all — byte-identical to a healthy fleet.
  Three places separate them, all off ONE number (`list_org_devices.onus_rx` per OLT,
  `pon_summary.onus_rx`/`olts_rx` org-wide): Home tiles render an em dash + "no OLT reports
  dBm" rather than a green 0, and print `N of M ONUs measured` when coverage is partial;
  `DeviceCapabilityIcons` carries a `Gauge` icon green ONLY when `onus_rx > 0` AND the walk is
  fresh — deliberately NOT tinted by severity, because a missing measurement is a coverage gap,
  not an alarm; and the Optical tab explains the gap. `onus_rx` counts the RAW `onu_optics`
  table, zombies included, because it's a CAPABILITY signal paired with `optics_updated_at` for
  freshness — and because the scrape folds Rx in on its own clock, so a count stamped by the
  SNMP sweep would read zero for up to 15 min after a scrape landed.
- **"This OLT doesn't report Rx" was a GUESS stated as a hardware fact and is GONE.** The same
  blank column is produced by a vendor with no recipe, an OLT nobody stored a password for, and
  a scrape failing for a day — and on the fleet that started this the honest answer was "we
  never asked". `components/rx-diagnosis.tsx` composes the real reason from
  `/api/inventory/rx-status` (vendor + how it resolved, recipe coverage, credentials, proxy
  grant, last scrape outcome) — the same split `SnmpDiagnosis` runs for a blank Ports panel:
  the server ships FACTS, the SPA writes the sentence. It must never trigger a scrape — the
  sweeper's slow clock is the feature (`refetchInterval` 120s for the same reason).
- **A dBm ON SCREEN CARRIES NO DATE.** The Optical panel's freshness stamp dated the SNMP
  roster only, so on a scrape-fed vendor the walk could be seconds old while the figures beside
  it were from yesterday — and "last week's numbers" looked exactly like "measured four minutes
  ago" while a tech decided whether a splice had helped. `RxFreshness` gives the web read its
  own stamp with three states never collapsed: read / failing-since (`last_ok_at` survives, so
  "was working until <ts>" stays sayable) / never. `rx-status` carries `can_refresh` +
  `refreshing` so the button is drawn off the server's own eligibility answer; re-deriving it in
  the SPA would drift into a button promising a reading nothing will take.
- **Optical drill-down degrades, never dead-ends**: a PON whose ONUs all have NULL Rx renders a
  roster ordered by ONU id (state, ranging distance — 0 m renders "—", meaning unranged,
  time-dark from `last_online_at`), a stable slot order the tech reads down rather than one
  shuffled by which ONUs are up; the worst-PON/threshold header hides when the whole OLT has no
  readings. Keep the empty-card branch unreachable.
- **`OnuRow` is ONE COLUMN PER FACT — no cell stands in for another.** The Rx cell used to FALL
  BACK to ranging distance or time-dark so the 380px panel kept one useful number, but the
  dedicated distance column renders at `@2xl`, so a no-Rx online ONU printed the same "2.78 km"
  twice in one row. Both are now their own columns at EVERY width and the Rx column is dropped
  entirely when the PON is `rosterOnly`. The Rx-drift-vs-baseline column is GONE for good — it
  reads `rx_dbm - rx_ref_dbm`, so on a no-Rx vendor it could only render "—", contradicting the
  header note two lines above. `rx_ref_dbm` still rides the API and the optics baseline.
- **The Network tree's shape is a VIEW, not the topology** (`org_devices.tree_detached`): a
  device buried under a huge subtree can be lifted to the top level by the row menu.
  Presentation ONLY — like `tags` it's read solely by `list_org_devices`, never by
  `org_device_topology`, so the parent link, suppression, the map, `/edge/devices` and paging
  are untouched and toggling it can NEVER re-page a fleet (`test_central_treedetach`). Two
  consequences: a lifted row RENDERS its parent's name beside a corner-up icon (the tree
  stopping short of saying where a box hangs is the one thing this could get dishonestly
  wrong), and `treeOrder`'s chevron gates on children it actually emitted, not the server's
  `child_count`, or a parent whose only children were lifted offers an expander over nothing.
  `update_org_device` clears the flag when the parent goes NULL.

### Layout & navigation

- **Settings is SECTIONED, and a section that would render empty is not offered**
  (`routes/settings-page.tsx`): five addressable sections (`/settings/:section`) behind a
  `SECTIONS` table whose `visible(ctx)` predicate decides whether it appears at all. That
  predicate must model the SAME conditions its cards gate on, including data-dependent ones —
  `WebProxyCard` returns null unless `orgs.web_proxy` is granted, so the page runs the
  `["orgs", org]` query (deduped with the card's) and feeds `hasWebProxy` in. A role-only
  predicate showed a read-only operator a Monitoring tab that rendered blank. Adding a card =
  putting it in a section, not appending to a list.
- **Account menu, not a Settings nav item** (`layout/account-menu.tsx`): primary nav lists
  PLACES IN THE NETWORK, so account-scoped config lives in a dropdown on the sidebar's foot
  identity row instead of spending a permanent nav slot. `NavItem.account` marks such entries;
  the sidebar filters them out and the MOBILE "More" sheet keeps them, because below `md` there
  is no sidebar and so no account menu. The menu advertises `⌘,`, bound in `app-shell.tsx` and
  ignored while a field has focus; if that binding goes, the hint goes with it.
- **Viewport breakpoints are wrong inside the device panel** — it's a fixed 380px on a wide
  screen, so `sm:`/`md:` guards all pass and columns overflow (the ONU heat-strip once
  collapsed to one cell wide). Width-conditional columns there use CONTAINER queries.
- **Epoch-hour trap**: `HourStrip` cells floor on EPOCH hours to match `bucket_of`, never local
  hours (half-hour zones like IST shift every cell). A query error in the device panel renders
  as an error, never the empty state.
- **Sort by `occurred_at ?? received_at`, NOT insert id** (Logs day-grouping, Home activity) —
  acks/post-mortems insert long after the outage. Log group keys include the first row's event
  id (day labels repeat).
- Home is a NOC overview, never empty when healthy; outage triage folds into it (status derived
  from `acknowledged_at`/`resolved_at`/`root_cause`, never stored; recovery is FSM-automatic —
  no manual resolve, ever).
- Auth rides the session cookie; 401 dispatches a `wisp:unauthorized` window event; org scoping
  mirrors `_scope_org`. **Live updates**: one SSE `EventSource` per org scope (`/api/events`)
  invalidating react-query keys off `store.data_version` (which includes `MAX(nodes.last_seen)`
  so a bare heartbeat un-stales a probe without a refresh). `list_org_devices()` LEFT JOINs
  `device_states` (+ `switch_ports` aggregates) so rows color without per-device round trips.

### Map (`/map`)

Leaflet + raster tiles fetched by the BROWSER (central needs no egress). Helpers live in
`web/src/map/` — `pins.ts`, `clusters.ts`, `cut.ts`, `geometry.ts`, `basemaps.tsx`, `view.tsx`,
`search.tsx` — with page composition in `routes/map-page.tsx`. New map logic goes in the
matching module, not back into map-page.tsx.

- **Basemaps are Google / Google Satellite ONLY** (the CARTO/Esri/Dim entries were removed at
  the operator's request the same day they shipped) via the Map Tiles API — the sanctioned
  third-party-renderer API, NOT the SDK-only Maps tiles. **The key is SERVER-WIDE,
  superadmin-pasted once** (`app_settings`); org owners never see a key field, and the
  `/api/orgs` reply injects it into every org row (referrer-restricted, ships to browsers by
  design, central still makes NO Google calls; no key = no Layers button). CARTO Voyager is the
  KEYLESS FALLBACK, never a menu option: it renders for no-key orgs, under a still-creating
  session, and after a Google failure — the map is never blank.
- `lib/google-tiles.ts`: session token (~2wk) cached in localStorage per mapType; **dpr>1
  sessions request `scaleFactor2x`+`highDpi`** (512px tiles at 256 CSS px — plain 256 rasters
  are why Google "looked blurry" on scaled displays; the cache key carries the scale).
- **Dark basemaps follow the app theme**: `createSession` takes a `styles` array and we send
  Google's night array with GEOMETRY intact and **LABEL TEXT dimmed**. Stock shipped first and
  came back "too contrasty" within the hour — it put road labels **louder than every status
  tone**, so the basemap's road names outshouted a device being down. Ours ranks under all
  three tones and is neutral grey rather than stock's tan/gold (which sat right on
  `--warning`). Re-measure before touching those hexes — ranking BELOW the alarms
  is the requirement; matching Google's sample is not. Three constraints: **(1) `styles` is
  roadmap-ONLY** (the API ignores styling on satellite, so there is no dark satellite and the
  Layers menu must not imply one); **(2) an oversized style array is dropped SILENTLY** — no
  error, tiles just come back light, and Google publishes no limit (ours is ~1.5 KB; check a
  real tile after editing); **(3) there is no `mapId`/cloud styling for 2D tiles**, inline JSON
  only. **The session cache key carries a HASH of the style array**, not just `:night` — a token
  bakes in the style it was minted with and lives ~2 weeks, so a palette edit would otherwise
  keep serving OLD tiles to every browser that already had one, invisibly to whoever made the
  edit. Derived, not hand-bumped, because nobody remembers to bump it. Theme is read reactively
  from the `.dark` class via `hooks/use-dark-mode.ts` — there is no theme provider and THREE
  components each keep their own copy, so the root class is the only signal they all agree on.
  The keyless CARTO fallback switches with it (style in the TileLayer key — a TileLayer won't
  re-fetch on a bare url change).
- ToS needs the per-viewport copyright in the attribution control + a Google wordmark overlay.
  Failure ladder: tile-error BURST (3 in 5s, once per token — a stray rural-z20 404 must not
  nuke the basemap) → recreate session once → toast + fallback tiles, WITHOUT overwriting the
  user's saved pick.
- **Chrome-over-tiles trap**: shadcn outline Buttons carry `dark:bg-input/30`, which BEATS a
  plain `bg-popover/95` override in dark mode — map chrome needs
  `bg-popover/95 dark:bg-popover/95`.
- **`.wisp-map-wrap { isolation: isolate }` is load-bearing**: Leaflet stacks its own panes at
  z-index 400–1000, so with the wrapper at `z-index:auto` those land in the ROOT stacking
  context and BEAT every Radix portal (dialogs, dropdowns, selects, the command palette are all
  `z-50` on `<body>`) — anything opened over the map rendered behind the tiles, and Leaflet
  painted over the app header. Isolating confines the ladder to the map; its own chrome is
  INSIDE the wrapper so `z-[1000]` still wins. Don't "fix" a future instance by bumping portal
  z-indexes — that fights the whole shadcn layer. Floating map chrome belongs INSIDE the control
  column, not at a hardcoded `top-[Nrem]`: the stack's height depends on which controls render,
  and the edit-mode hint's fixed offset landed on the very button that leaves edit mode.
- **Leaflet trap**: `pathOptions.className` is silently DROPPED (setStyle ignores it) — pass
  `className` as a top-level react-leaflet prop and include the tone in the key so a state
  change remounts the path. **Topology polylines MUST stay `interactive={false}`** or they
  swallow placement clicks.
- **Placement & pins**: `lat/lng` write only via `POST /api/inventory/location`
  (paired-or-both-null; dashboard-side only — the edge never sees coordinates). The
  click-through panel is the same `device-detail.tsx` the tree rows use (extracted, not forked —
  keep it shared). **Map divIcons are CACHED by html string** — `useNow()` re-renders every
  second, and an uncached icon swaps every marker's DOM node per tick, restarting the
  down-pulse. **Role shapes**: `wisp-pin--t-<device_type>` picks the SILHOUETTE (fill stays
  health, ::after ring stays optics) via border-radius / rotation / ::before ONLY — clip-path
  would clip the selection box-shadow and the down-pulse. Passives render small + always-muted.
- **The viewport is LOCKED to `orgs.map_region`** (bounds in `lib/map-regions.ts`, unknown key →
  all-India, `world` = no lock). All view logic lives in ONE `useMap()` child
  (`ViewController`) — a ref on `MapContainer` isn't set yet when a query resolves — and the fit
  MUST run before `setMinZoom`: min-zooming a zoomed-out map fires an ANIMATED setZoom that
  lands after and overrides an `animate:false` fitBounds. Control buttons shift left of the open
  device panel on desktop or it covers them.
- Map search = device match (instant) + OSM Nominatim geocoding (browser-side, debounced 450ms
  + 3-char floor — stay a polite keyless client; results boxed to the org's map area). Picking
  an unplaced device starts placement.
- **Site clusters** (the spider-fan was REMOVED): pins that would overlap on screen fold into a
  count badge with a conic composition ring — SCREEN-SPACE and zoom-dependent, greedy in Web
  Mercator px. The fan scattered pins onto real coordinates and read as geography (the ONU-spoke
  lesson again), so co-located members resolve in UI SPACE now: badge click = fitBounds when
  genuinely spread, else a SITE CARD listing members, anchored by member DEVICE id so it
  survives zoom reshuffles. Placing devices at the same coords IS the "rack", no schema — and
  co-location is deliberate: in placement mode a badge/pin click snaps to that site's EXACT
  coords, and an edit-pins drag dropped within 24 px of a neighbor snaps to it (near-stacks were
  the fan's original sin). `pinPos` is raw-or-centroid ONLY; a folded selection highlights the
  badge and auto-opens the card, so search never lands on a hidden pin. **Don't reintroduce
  display positions that aren't true locations.**
- **PON on the map**: OLT pins ring amber/red off `onus_warn/crit` (suppressed when
  maint/down/stale-optics). The per-ONU spoke fan was REMOVED at the operator's request — EPON
  ranging gives distance but no bearing, so spoke angles were fabricated, and on a map
  everything reads as geography. **The map shows only true locations**; ONU severity lives in
  the pin ring + the Optical tab. ONUs return only if they ever get real coordinates
  (`focusOnuId` threading survives for that).
- **Cable routes**: `link_routes` keyed (org, child, parent) covers primary AND backup links;
  waypoints are INTERMEDIATE vertices only, parent→child — endpoints stay implicit so moving a
  pin rubber-bands the route. `list_link_routes` joins against the live link, so a re-parent
  hides stale geometry instead of drawing a lie. `/api/inventory/routes` is map-only,
  deliberately NOT folded into `list_org_devices()`. The renderer falls back to the straight
  chord whenever an endpoint folds into a cluster (a route snaking into a centroid reads as an
  error), and the editor is ~100 lines of plain react-leaflet — no leaflet-geoman. The device
  panel shows "cable" (route length) AND "straight-line" (chord), labeled honestly, because
  splicing crews quote drum metres off this.
- **Cross-links are keyed (child=higher, parent=lower) in `link_routes`** — the OPPOSITE of
  `org_device_links`' (min, max) — so waypoints run parent→child for every kind and the map
  needs no per-kind reversal. `list_link_routes` and `_link_write_scope` match a peer in either
  order. Until this was fixed, **every peer route save 400'd** although the map offered the
  editor.
- **Per-link cartography rides `link_routes`, not a second table**: that row is keyed exactly
  "one link", so `color` and `label_pos` live there beside the geometry. Consequences:
  `set_link_route` no longer DELETEs on an empty waypoint list (`_prune_link_route` drops a row
  only when waypoints AND colour AND label_pos are all empty, or clearing a drawn path would
  silently repaint the line), and `set_link_style` is SPARSE so the colour picker and a label
  drag can't clobber each other. **Colour is a CLOSED palette of NAMES** (the product-wide
  vocabulary; values in index.css as `--map-line-*`), never a free hex.
  `paintedLineColor` refuses to paint a line whose tone is destructive/warning, and selection
  emphasis still overrides — status and "which path did I click" both outrank decoration. The
  `--map-line-*` tokens are ONE set for both themes (unlike every other colour here): the
  backdrop is raster tiles, equally bright under either app theme, and the dark casing does the
  contrast work. Deliberately NOT in `theme.py:_TOKENS` — cartography, not brand theming.
- **The bandwidth chip's position is a FRACTION along the path** (`label_pos`, 0..1), snapped to
  the line every frame. Never a lat/lng: the line rubber-bands when either pin moves and a saved
  coordinate would drift off the cable it names. The chip borrows a coloured line's hue on its
  border and a left rail, never the rate text (a port alarm's tone owns the whole chip and wins).
- **Link hover distance is a MAP-level mousemove, never a polyline mouseover**: hovering a cable
  reads out ground distance to each end (`map/linkhover.tsx`), but polylines must stay
  non-interactive, so the probe projects the cursor and walks pre-projected geometry. Distances
  are walked segment-by-segment in METRES (`geometry.alongKm`), not a projected fraction —
  Mercator stretches with latitude and a splicing crew orders drum off this; the readout labels
  itself "along cable" vs "straight-line" for the same reason. Two perf invariants: the readout
  icon must NOT go through `cachedDivIcon` (its text changes per pixel, and a cache overflow
  `clear()`s the pin icons, restarting every down-pulse), and the probe calls `setHover` only
  when the rounded readout CHANGES (mousemove fires per pixel and the common case is "nowhere
  near a cable").
- **Cut overlay**: walks the OLT→passive-chain path (drawn routes where traced, chords where
  not; the first hop must name the PON, deeper plant may leave `pon_port` blank), clamps the
  RANGING interval into the geometry, paints the stretch + a pulsing ✕ opening the OLT's Optical
  tab. No placed splitter chain = no overlay (the fault card still carries the distance).
  Power-pattern waves render a dashed warning hull + banner.

## Config

Every tunable is a field on the frozen `Config` dataclass, read once from `WISP_*` env vars. No
DB settings layer; topology/team/routing/credentials live in the dashboard, not env vars.
**`Config` is shared between edge and central** — grep both `apps/daemon/` and
`src/wisp/central/` before deleting/renaming a field. `db_path` (`WISP_DB`) is not a database —
just where the lock file and supervisor transient files live.

## Ingest auth & enrollment

- Any ONE of three: global bearer (`WISP_CENTRAL_TOKEN`), a self-service per-node token, or
  mTLS. None configured = ingest stays open (trusted network).
- **Node tokens**: registered from Network → Probes; only a SHA-256 hash stored, plaintext shown
  once, rotatable only. A node that HAS a credential is gated on presenting it; identity comes
  FROM the credential, not the envelope. `clean_node_id` validates (it becomes a systemd
  identity and a path segment).
- **mTLS**: `central/pki.py` shells out to `openssl` (admin-CLI one-time op); identity is
  CN-encoded `org_id:node_id` and must match the claimed org/node. Central terminates TLS when
  `WISP_CENTRAL_TLS_CERT/_KEY` are set; `WISP_CENTRAL_CLIENT_CA` turns on CERT_OPTIONAL
  (browsers stay certless). The handshake runs in the request's worker thread (`finish_request`
  override) so one slow handshake can't stall the listener. No CRL — revoking means rotating
  the CA.

## Reliability ("trust the alarm")

- One probe per org/node via an OS advisory lock (exit 3); central's per-outage dedupe
  (`open_outage_if_absent`) is idempotent anyway.
- **A page must not vanish to a blip**: `send_with_retry` — network/timeout/5xx retry with
  backoff, 4xx fails fast.
- The probe loop never dies on one bad cycle (per-cycle try/except; keep new per-cycle work
  inside it). `_gather_pings` swallows per-probe errors but re-raises config/permission
  `RuntimeError` loudly.
- **Fleet watchdog is central's** (`central/watchdog.py`), transition-only, restart-safe. Input
  is `store.node_liveness()`, NOT `SELECT * FROM nodes` (that table remembers every identity
  ever seen); `delete_node_token` purges the heartbeat row too, or a deleted probe pages
  NODE_STALE forever.

## Fleet packaging & self-update

- Two FIRST-INSTALL-only artifacts: `.deb` and the Windows setup exe. Both run the
  **supervisor** (`runtime/supervisor.py`), which owns all agent self-updates (download → verify
  sha256 → swap → health-gate → rollback). The manifest builder skips them — an installer must
  never become an "agent artifact".
- **The supervisor STOPS the agent before `os.replace`** (Windows delete-locks a running image —
  the v0.11.0 live-swap crash), and any mid-apply exception yields FAILED + discards
  `update_request.json` (retry rides the poll cadence, never a tight loop). The health gate needs
  `stable_polls` (3) CONSECUTIVE healthy polls or rollback is unreachable for a crash-looping
  build. Supervisors are NOT in the self-update channel — only an installer re-run updates them.
  Ditto `wisp-tray.exe` (per-user pure-ctypes tray; keep it dependency-free; control via elevated
  `schtasks`, never parse its localized output).
- **The tray's ONLY status truth is status.json** — never gate its menu on `schtasks /Query`: the
  SYSTEM task is unreadable from a non-elevated session and that failure is indistinguishable
  from "not installed" (it kept healthy probes reading "task not installed" for days). It shows
  the AGENT's version from status.json, since its own compiled stamp goes stale the first time
  the agent self-updates.
- **The Windows installer upgrades in place**: `PrepareToInstall` ends the task and taskkills
  tray/supervisor/agent before file copy — a running fleet delete-locks its images and reinstalls
  used to dead-end on "old files exist". Don't remove it.
- **Edge health is on disk**: `status.json` (atomic, best-effort — a full disk must never kill
  the probe loop) + `logs/edge.log` (rotated at task start >5MB). Headless boxes read it via
  `wisp-edge status` (exit 0 healthy / 1 starting-degraded / 2 stale-error). The Windows
  installer WAITS for a fresh `status.json` (exit 10 = unconfirmed) — never move that back to
  fire-and-forget. Re-running with `-Central` rewrites `edge.env.ps1` (write-once made one bad
  install permanently dead). Scheme-less URLs normalize to `https://`.
- **Central is the release mirror; edges never touch GitHub.** `central/releasesync.py` pulls the
  latest release **unauthenticated by default** (`WISP_GITHUB_TOKEN` only for a private repo /
  rate limits — an expired PAT once silently blocked a rollout), verifies each binary's sha256
  against the manifest, caches under `data/releases/<ver>/`, and rewrites URLs to central-relative
  `/download/<ver|latest>/<name>`. GitHub asset downloads 302 to S3 which REJECTS an
  Authorization header — capture Location and re-fetch clean (`_NoRedirect`). **Nothing prunes
  `data/releases/`** — it grows ~174 MB per version forever; prune by hand, keeping the rollback
  floor + current + previous. A `releases` row whose cached dir is gone simply can't be served.
- **Install-artifact names are VERSION-LESS and load-bearing**
  (`wisp-edge-setup-win-amd64.exe`, `wisp-edge-linux-<arch>.deb`) — the install card links
  `${origin}/download/latest/<asset>`.
- **The field-app APK mirror is store-less BY DESIGN** (`sync_app_release`): the separate PUBLIC
  `wisp-field-app` repo's latest `.apk` lands in the FIXED `release_cache_dir/app/` and serves at
  `/download/app/wisp-field.apk` (any non-`latest` dir serves without a store lookup). **Never
  `set_release` an app version** — the store's release table drives edge self-update "latest",
  and an app tag there would roll the fleet. Rides the release-sync timer best-effort; fetched
  UNAUTHENTICATED (the fine-grained release-sync PAT would 403 on the app repo). Gated
  `WISP_APP_RELEASES_REPO`.
- CI signing (`release.yml`): Authenticode per Windows binary, minisign once over `SHA256SUMS`;
  both no-op while secrets are unset. Commit `deploy/minisign.pub` only once a real keypair
  exists. Nothing has run against real keys yet.
- `deploy/wisp-edge.spec` `Analysis` paths use `os.path.dirname(SPECPATH)`, not bare relative
  strings — a loaded `.spec` resolves against its own dir, not cwd.

## Conventions & gotchas

- States: `UP`/`DEGRADED`/`DOWN`/`UNREACHABLE`; `DOWN_FAMILY = {DOWN, UNREACHABLE}`. Import from
  `core/state_machine.py`, don't hardcode.
- Hysteresis: DOWN = 3 consecutive 100%-loss polls, DEGRADED = 2, recovery = 2 healthy. The FSM
  never emits `UNREACHABLE` — that's a topology override after `feed()`. Fast-confirm changes
  when samples arrive, not how many.
- Topology order: parent-before-child (`_topological_order`).
- No automatic cause inference — cause is operator-entered at resolution only.
- Escalation: fresh DOWN pages owner+worker; one `escalations` row (`kind="hourly"`,
  `UNIQUE(outage_id, kind)`) re-broadcasts all-hands every `cfg.escalate_every_min` while open.
  Ack doesn't stop it; recovery does.
- Timestamps: poll/outage are ISO8601 `+00:00`; SQLite `datetime('now')` is space-separated
  naive. `core/analytics._parse` normalizes both — reuse it.
- Schema: `central/store.py`'s `_SCHEMA` + `_ensure_columns` is the only schema.
- **`CentralStore` is split into domain mixins** (`store_orgs/users/fleet/devices/outages/
  snmp.py`, helpers in `store_util.py`) composed in `store.py`, which keeps `_SCHEMA`,
  `__init__`, `_connect`/`_scope` and the class attrs. New store methods go in the matching mixin
  file; import `CentralStore` from `wisp.central.store` as before — the mixins are not a public
  API.

## Tests

**`.venv/bin/python -m unittest discover -s tests`** after any logic change — the interpreter
matters. There is no bare `python` on this box, and the system `python3` has no `httpx`, so
running it that way ERRORS the ~12 proxy/edge tests that import it. That looked like a standing
"pre-existing failure" for several sessions; it never was.

Tests inject recording doubles — no real ntfy/central network. Key files:
`unit/test_state_machine`, `test_probers`, `test_snmp`, `test_gpon`, `test_health`,
`test_supervisor`, `test_releasesync`, `test_central_inventory`, `test_central_pki`,
`test_edge_status`; `integration/test_central*`, `test_daemon*`, `test_notifiers`,
`test_single_instance`.

## Removed — don't go looking for these

**The single-box era** (one daemon + local dashboard, one SQLite per edge) is deleted wholesale,
and **git history no longer has it**: history was truncated to the newest 10 commits (2026-07-09,
force-pushed, no backup), so don't offer to restore it and don't cite `git log` before `5a532a7`.
Gone: `apps/dashboard/`, `src/wisp/server/`, `egress/shipper.py`, `src/wisp/database/` +
`migrations/`, the old local-engine drivers in `apps/daemon/main.py`, `POST /ingest` +
`devices`/`rollups`, the curl-script installers, the vanilla-JS dashboard. `core/state_machine.py`,
`core/analytics.py`, `core/baseline.py` are ALIVE — central imports them; grep before deleting
anything in `core/`.

**BOTH self-serve payment gateways.** Razorpay (2026-07-16 — `central/razorpay.py`,
`lib/razorpay.ts`, the HMAC verify path, `razorpay_key_*`; the account was refused, individual/no
GST), then UPIGateway (2026-07-21 — `central/upigateway.py`, `POST /api/billing/order`/`/verify`/
`/upi-return`, the `billing_payments` ledger, `upigateway_key`, `billing.months_to_pay`, the
`make_server(upi=…)` injection, `PayOnlineButton`): the operator wanted a plain GPay-number/QR
flow with a manual "I've paid" ping. Dead `razorpay_*`/`upigateway_key` rows and an orphaned
`billing_payments` table may linger — harmless, don't "clean them up" into a migration.

**The Team page and the whole roster/attendance model** (2026-07-21): `routes/team-page.tsx` and
its nav entry, `teamApi`, the `Worker`/`Attendance*` types, `/api/team*` + `/api/attendance`, the
`org_workers`/`org_attendance` tables and their store methods, `api/common.py:worker_org`,
`store_util._today`/`_recent_days`. It was a credential-less second list of people that nothing
else read — **who works for an org is now just who has a login account**. Regions lost
`worker_count` with it. The two tables are LEFT IN PLACE in upgraded DBs — dropping a table is
irreversible and nothing reads them. Removed with it: the read-only `operator`/`tech` roles, the
`ntfy_topic_tech` channel, and the `hasWebProxy` predicate probe that existed only so a read-only
operator wasn't offered a blank Monitoring section.

**Doc files folded into this one** (2026-07-24): `webplan.md` (web-proxy design — the feature
shipped; its invariants and caveats are the "Device web-UI proxy" section) and
`whatsapp-notifier-plan.md` (a next-session prompt for work now BUILT; the outcome, which deviated
from the plan on two points, is the WhatsApp section). `webplan.md` is in git history; the
WhatsApp plan was untracked and is gone.

**Tags:** five survive (`v0.13.0`–`v0.15.1`); only `v0.14.0`/`v0.15.0`/`v0.15.1` carry a GitHub
Release. `v0.1.x`–`v0.12.1` went with the history they pointed at. **`v0.14.0` is the rollback
floor** — there is no artifact below it, so an edge on an older build can only roll forward.
