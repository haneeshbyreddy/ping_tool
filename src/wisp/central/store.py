from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from wisp.central.store_orgs import OrgStoreMixin
from wisp.central.store_users import UserStoreMixin
from wisp.central.store_fleet import FleetStoreMixin
from wisp.central.store_devices import DeviceStoreMixin
from wisp.central.store_outages import OutageStoreMixin
from wisp.central.store_proxy import ProxyStoreMixin
from wisp.central.store_snmp import SnmpStoreMixin
from wisp.central.store_util import (  # noqa: F401 — re-exported
    SNMP_STATUS_STATES, SNMP_SUBSYSTEMS, SNMP_WALKS_KEEP,
    _now_iso, _recent_days, _today,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    org_id        TEXT PRIMARY KEY,
    name             TEXT,
    ntfy_topic       TEXT,                 -- per-org page target for the fleet watchdog
    ntfy_topic_owner    TEXT,              -- Phase A: per-role outage routing (Phase B pages these)
    ntfy_topic_operator TEXT,
    ntfy_topic_tech     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    org_id    TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    version      TEXT,
    last_poll_ts TEXT,
    fleet_size   INTEGER,
    open_outages INTEGER,
    health       TEXT,                     -- the raw heartbeat body (JSON)
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    restart_pending INTEGER NOT NULL DEFAULT 0,  -- one-shot; consumed by heartbeat reply
    PRIMARY KEY (org_id, node_id)
);
-- Self-service edge enrollment: an ISP owner/operator issues one of these per node from
-- the dashboard, then presents it as the ingest bearer token. Independent of `nodes`
-- above (a row here can exist before that node has ever connected) and of the global
-- WISP_CENTRAL_TOKEN/mTLS (either of those still also works) — see central/server.py's
-- `_ingest_ok`. Only the hash is ever stored; the plaintext is shown once, at issue time.
CREATE TABLE IF NOT EXISTS node_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    created_by  INTEGER,                    -- users.id of whoever issued it
    revoked_at  TEXT,
    UNIQUE (org_id, node_id)
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    edge_id       INTEGER NOT NULL,        -- per-org counter (see _insert_org_event)
    type          TEXT,
    device_id     INTEGER,                 -- org_devices.id
    device_name   TEXT,
    device_ip     TEXT,
    device_region TEXT,
    state         TEXT,
    occurred_at   TEXT,
    payload       TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    UNIQUE (org_id, node_id, edge_id)
);
-- The cross-edge watchdog's restart-safe state: the last STALE/OK page per node (only
-- 'sent' rows count when rehydrating, so a failed page is retried, not stranded).
CREATE TABLE IF NOT EXISTS node_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id  TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- 'NODE_STALE' | 'NODE_OK'
    status     TEXT NOT NULL,              -- 'sent' | 'failed'
    detail     TEXT,
    created_at TEXT NOT NULL
);
-- Part C — dashboard login accounts. org_id NULL = a SUPERADMIN (the platform
-- operator who onboards ISPs + provisions org accounts); else the account is scoped to
-- one org. Passwords are salted SHA-256 (crypto in central/auth.py, like the edge PIN).
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id  TEXT,                       -- NULL => superadmin (cross-org)
    username   TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'operator',  -- owner|operator|tech within the org
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- Part C — the org-wide team roster + attendance ("who's on duty" is an org fact, so it
-- lives centrally now, not per-edge). Mirrors the edge workers/attendance model.
CREATE TABLE IF NOT EXISTS org_workers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'operator',
    region     TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,
    notes      TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_attendance (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL,
    worker_id INTEGER NOT NULL REFERENCES org_workers(id),
    day       TEXT NOT NULL,               -- UTC calendar day; presence = a row exists
    UNIQUE (worker_id, day)
);
-- Phase A — the ISP-managed device topology (the management plane an org builds from the
-- central dashboard, independent of any edge). NOT the same table as `devices` above: that
-- one is the edge-ingest global id map (Phase B/C will populate live state onto it via
-- edge_local_id); this one is what the ISP configures by hand before any edge ever reports.
CREATE TABLE IF NOT EXISTS org_devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        TEXT NOT NULL,
    name             TEXT NOT NULL,
    ip_address       TEXT NOT NULL,
    device_type      TEXT,
    region           TEXT,
    parent_device_id INTEGER REFERENCES org_devices(id),
    assigned_node_id TEXT,             -- which registered edge node probes this device;
                                        -- NULL = every node for this org covers it
                                        -- (default, pre-assignment behavior)
    maintenance      INTEGER NOT NULL DEFAULT 0,
    snmp_enabled     INTEGER NOT NULL DEFAULT 0,
    snmp_version     TEXT NOT NULL DEFAULT '2c',
    snmp_community   TEXT,
    snmp_port        INTEGER NOT NULL DEFAULT 161,
    gpon_vendor      TEXT,                 -- OLT only: manual override; NULL = the edge
                                           -- auto-detects the GponProfile via sysObjectID
                                            -- (ingress/gpon.py); NULL = fall back to the
                                            -- edge's WISP_GPON_VENDOR env, then huawei
    lat              REAL,                  -- map pin (WGS84); both set or both NULL
    lng              REAL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_devices_org ON org_devices(org_id, is_active);
-- Declared region names per org — feeds the dashboard's region dropdowns.
-- `org_devices.region`/`org_workers.region` stay plain text; list_regions returns
-- the UNION of declared + in-use names, so pre-table free-text regions surface
-- without any backfill.
CREATE TABLE IF NOT EXISTS org_regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (org_id, name)
);
-- Phase B — central runs the brain. One MonitorEngine per org (central/engine.py)
-- feeds off org_devices topology and commits here every report; this is the FSM output
-- store the edge's `poll_results`/`devices.state` played on the standalone box.
CREATE TABLE IF NOT EXISTS device_states (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id   TEXT NOT NULL,
    state       TEXT NOT NULL,          -- UP | DEGRADED | DOWN | UNREACHABLE
    latency_ms  REAL,
    packet_loss REAL,
    jitter_ms   REAL,
    updated_at  TEXT NOT NULL
);
-- Mirrors the edge's outages/alert_log/escalations one-for-one (same lifecycle, same
-- escalation ladder in central/dispatch.py) but org-scoped, since central is the
-- multi-org aggregation point now running detection for every org at once.
CREATE TABLE IF NOT EXISTS outages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        TEXT NOT NULL,
    device_id        INTEGER NOT NULL REFERENCES org_devices(id),
    started_at       TEXT NOT NULL,
    resolved_at      TEXT,
    final_state      TEXT NOT NULL,
    acknowledged_by  TEXT,
    acknowledged_at  TEXT,
    root_cause       TEXT,
    resolution_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_outages_open ON outages(org_id, device_id, resolved_at);
-- Hourly latency/packet-loss trend (30-day retention,
-- hourly buckets — both decided; see CLAUDE.md). Folded incrementally at each "full"
-- report cycle (never a recheck — see central/rollup.py), so no raw per-poll history
-- needs to live here, just running sums per (org, device, hour). Averages are
-- computed at READ time (`CentralStore.device_rollup_series`), not stored, so the
-- write path stays a single upsert regardless of how many samples land in an hour.
CREATE TABLE IF NOT EXISTS device_rollups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    device_id     INTEGER NOT NULL REFERENCES org_devices(id),
    bucket        TEXT NOT NULL,           -- hour-bucket start, ISO8601 naive UTC
    samples       INTEGER NOT NULL DEFAULT 0,
    latency_sum   REAL NOT NULL DEFAULT 0,
    latency_count INTEGER NOT NULL DEFAULT 0,  -- latency can be NULL (100% loss) -> tracked apart from samples
    loss_sum      REAL NOT NULL DEFAULT 0,
    down_samples  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(org_id, device_id, bucket)
);
CREATE INDEX IF NOT EXISTS idx_device_rollups_lookup ON device_rollups(org_id, device_id, bucket);
CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id  TEXT NOT NULL,
    outage_id  INTEGER,
    device_id  INTEGER,
    channel    TEXT,
    recipient  TEXT,
    sent_at    TEXT,
    status     TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_log_outage ON alert_log(outage_id);
CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id   TEXT NOT NULL,
    outage_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    executed_at TEXT,
    UNIQUE (outage_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_escalations_due ON escalations(executed_at, due_at);
-- Part D — the version authority. A published release + its per-platform signed artifacts,
-- and one active staged rollout per org (canary subset first, promoted fleet-wide only after
-- the canaries come back healthy on the target; auto-halts otherwise).
CREATE TABLE IF NOT EXISTS releases (
    version    TEXT PRIMARY KEY,
    channel    TEXT NOT NULL DEFAULT 'stable',
    artifacts  TEXT NOT NULL,             -- JSON {platform: {url, sha256}}
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollouts (
    org_id      TEXT PRIMARY KEY,
    target_version TEXT NOT NULL,
    canary         TEXT NOT NULL,         -- JSON list of node_ids (the first wave)
    state          TEXT NOT NULL,         -- 'canary' | 'promoted' | 'done' | 'halted'
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    note           TEXT
);
-- Tiny central-wide KV (not org-scoped): release-sync health lives here so a dead
-- mirror is visible/pageable instead of rotting silently (the 2026-07 expired-PAT
-- incident stalled a rollout for days with zero signal).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(org_id, node_id, id);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(org_id, node_id, device_id, id);
CREATE INDEX IF NOT EXISTS idx_node_alerts ON node_alerts(org_id, node_id, id);
-- SNMP port status, central-side. One row per
-- discovered switch port, mirrors the old single-box `switch_ports` table one-for-one
-- but org-scoped: `device_id`/`feeds_device_id` are `org_devices` ids. Discovery
-- (every walked port) lands `monitored=0`; the operator ticks which ports to watch —
-- you do NOT want to alarm on every access port a laptop comes and goes on. A
-- monitored port that drops folds into the outage of the device it `feeds_device_id`
-- (central/ports.py), it never raises a competing alarm. `down_streak`/`alarm`/
-- `alarm_since` carry the flap-suppressed detection state in-row so it survives a
-- central restart (no in-memory port FSM to lose, same discipline as `device_states`).
CREATE TABLE IF NOT EXISTS switch_ports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       TEXT NOT NULL,
    device_id       INTEGER NOT NULL REFERENCES org_devices(id),
    if_index        INTEGER NOT NULL,
    if_name         TEXT,
    if_alias        TEXT,
    admin_status    TEXT,
    oper_status     TEXT,
    last_change     TEXT,
    monitored       INTEGER NOT NULL DEFAULT 0,
    feeds_device_id INTEGER REFERENCES org_devices(id),
    down_streak     INTEGER NOT NULL DEFAULT 0,
    alarm           INTEGER NOT NULL DEFAULT 0,
    alarm_since     TEXT,
    updated_at      TEXT,
    -- Per-port throughput (bandwidth), orthogonal to oper/admin status.
    -- Operator-set (never touched by a walk): bw_threshold_mbps/bw_max_mbps/bw_direction
    -- — a floor and a ceiling on the SAME rate stream, either independently optional.
    -- Walk-refreshed: the raw octet counters (TEXT — Counter64 can exceed SQLite's
    -- signed-64 INTEGER range) + the last computed rates. Flap-suppressed like the
    -- port-down path, each bound its own streak because traffic is burstier than link
    -- state (and a port can be simultaneously fine on one bound, tripped on the other).
    bw_threshold_mbps REAL,
    bw_max_mbps       REAL,
    bw_direction      TEXT,
    in_octets         TEXT,
    out_octets        TEXT,
    counters_at       TEXT,
    in_bps            REAL,
    out_bps           REAL,
    bw_low_streak     INTEGER NOT NULL DEFAULT 0,
    bw_alarm          INTEGER NOT NULL DEFAULT 0,
    bw_alarm_since    TEXT,
    bw_high_streak    INTEGER NOT NULL DEFAULT 0,
    bw_high_alarm     INTEGER NOT NULL DEFAULT 0,
    bw_high_alarm_since TEXT,
    UNIQUE(org_id, device_id, if_index)
);
CREATE INDEX IF NOT EXISTS idx_switch_ports_device ON switch_ports(org_id, device_id);
CREATE INDEX IF NOT EXISTS idx_switch_ports_feeds ON switch_ports(org_id, feeds_device_id);
-- Graph topology backup edges, central-side. Mirrors the old single-box
-- `device_links` one-for-one, org-scoped: the PRIMARY parent stays the single source
-- of truth on `org_devices.parent_device_id` (every existing tree/topo query keeps
-- working unchanged); this table carries only the EXTRA redundancy edges
-- (kind='backup'). `core/state_machine.py`'s `DeviceMeta.effective_parents()` combines
-- the two — the engine itself needed ZERO changes to support this (see central/engine.py).
CREATE TABLE IF NOT EXISTS org_device_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id  TEXT NOT NULL,
    child_id   INTEGER NOT NULL REFERENCES org_devices(id),
    parent_id  INTEGER NOT NULL REFERENCES org_devices(id),
    kind       TEXT NOT NULL DEFAULT 'backup',
    is_active  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(org_id, child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_org_device_links_child ON org_device_links(org_id, child_id);
CREATE INDEX IF NOT EXISTS idx_org_device_links_parent ON org_device_links(org_id, parent_id);
-- Drawn cable path for one link (map view). Keyed by the (child, parent) pair so it
-- covers both the implicit primary link (org_devices.parent_device_id) and backup
-- rows above. Waypoints are the INTERMEDIATE vertices only, ordered parent→child —
-- endpoints stay implicit (the device pins), so moving a pin rubber-bands the route
-- instead of orphaning it. Dashboard-side only; the edge never sees geometry.
CREATE TABLE IF NOT EXISTS link_routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    child_id   INTEGER NOT NULL REFERENCES org_devices(id),
    parent_id  INTEGER NOT NULL REFERENCES org_devices(id),
    waypoints  TEXT NOT NULL,            -- JSON [[lat,lng],...]
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    UNIQUE(org_id, child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_link_routes_org ON link_routes(org_id);
-- The on-backup badge (one row per redundancy-capable device) — central/redundancy.py
-- writes it every full report cycle, restart-safe (a restart mid-failover reads `was`
-- back from here rather than re-paging). Never part of the outage/escalation ladder.
CREATE TABLE IF NOT EXISTS device_redundancy (
    device_id          INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id          TEXT NOT NULL,
    on_backup          INTEGER NOT NULL DEFAULT 0,
    primary_down_since TEXT,
    updated_at         TEXT NOT NULL
);
-- Per-link performance baseline, central-side (core/baseline.py's pure
-- median+MAD deviation math, unchanged — central's job is just the trailing-sample
-- window + badge). device_perf_samples is a BOUNDED per-device ring buffer (trimmed to
-- the newest cfg.perf_window rows after every insert — central/perf.py), not a full
-- history: this is deliberately much finer-grained than device_rollups' hourly buckets
-- (the whole point is catching an intra-hour slowdown an hourly average would smear
-- out), so it is NOT the same storage as the trend rollup — don't conflate the two.
CREATE TABLE IF NOT EXISTS device_perf_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id    TEXT NOT NULL,
    device_id    INTEGER NOT NULL REFERENCES org_devices(id),
    ts           TEXT NOT NULL,
    latency_ms   REAL,
    packet_loss  REAL,
    jitter_ms    REAL,
    state        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_perf_samples_lookup
    ON device_perf_samples(org_id, device_id, id);
-- The slow-link badge (one row per device), restart-safe like device_redundancy — a
-- central restart resumes from the last verdict even though the raw sample window
-- itself resets (see device_perf_samples' docstring above).
CREATE TABLE IF NOT EXISTS device_perf (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id   TEXT NOT NULL,
    degraded    INTEGER NOT NULL DEFAULT 0,
    metric      TEXT,
    baseline_ms REAL,
    current_ms  REAL,
    since       TEXT,
    updated_at  TEXT NOT NULL
);
-- GPON per-ONU optical reading, one row per ONU under an OLT `org_devices` row. The
-- edge walks the OLT's vendor GPON MIB on its slow SNMP cadence (ingress/gpon.py),
-- ships every ONU's Rx power under `POST /report`'s `optics` key, and central/optics.py
-- upserts here. `onu_key` is the vendor-stable per-ONU identity (serial, or a
-- PON/onu-id composite) so a re-walk UPSERTs in place rather than duplicating. Like
-- switch_ports this is a LEADING INDICATOR store: optical NEVER opens an outage (the
-- ICMP FSM owns those), the badge just colors the OLT's expanded Optical tab and feeds
-- the per-OLT crit page. `rx_ref_dbm`/`rx_ref_at` carry a rolling ~7-day reference so
-- the dashboard can show signal DRIFT (this ONU is 2.1 dB weaker than a week ago)
-- without a full history table — refreshed by central when the reference ages out.
-- `ack_until` is the operator's per-ONU acknowledgement (suppress this ONU from the
-- crit count until it recovers or the stamp passes), mirroring outage acknowledge.
CREATE TABLE IF NOT EXISTS onu_optics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        TEXT NOT NULL,
    device_id     INTEGER NOT NULL REFERENCES org_devices(id),   -- the OLT
    onu_key       TEXT NOT NULL,
    pon_port      TEXT,            -- e.g. "0/6"
    onu_id        INTEGER,         -- ONU index within its PON
    name          TEXT,            -- subscriber / description
    serial        TEXT,
    state         TEXT,            -- online | offline | dying_gasp | los | unknown
    rx_dbm        REAL,            -- ONU-side received optical power (the headline metric)
    tx_dbm        REAL,            -- ONU-side transmit power (optional)
    olt_rx_dbm    REAL,            -- OLT-side received-from-this-ONU power (optional)
    distance_m    INTEGER,         -- ranging distance
    rx_ref_dbm    REAL,            -- rolling reference for drift
    rx_ref_at     TEXT,
    severity      TEXT,            -- ok | warn | crit (evaluated vs the OLT's thresholds)
    ack_until     TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE(org_id, device_id, onu_key)
);
CREATE INDEX IF NOT EXISTS idx_onu_optics_device ON onu_optics(org_id, device_id);
-- Per-OLT optical badge — one row per OLT, restart-safe like device_redundancy/
-- device_perf. Carries the summary counts the OLT row/header render and the
-- transition-only paging state (page when crit_count crosses 0 -> >0, recover at 0),
-- so a re-walk that leaves the crit set unchanged never re-pages.
CREATE TABLE IF NOT EXISTS olt_optics (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id      TEXT NOT NULL,
    onus_total  INTEGER NOT NULL DEFAULT 0,
    onus_online INTEGER NOT NULL DEFAULT 0,
    warn_count  INTEGER NOT NULL DEFAULT 0,
    crit_count  INTEGER NOT NULL DEFAULT 0,
    alarm       INTEGER NOT NULL DEFAULT 0,
    alarm_since TEXT,
    updated_at  TEXT NOT NULL
);
-- PON fault ladder state (central/ponalert.py) — one row per (OLT, PON port),
-- transition-only paging like device_redundancy/olt_optics: a re-walk that
-- leaves the fault standing must not re-page. State written even when the
-- alert gate is off. Never opens an outage — SNMP-derived facts don't.
CREATE TABLE IF NOT EXISTS pon_fault_state (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    pon_port   TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- power | fiber
    dark       INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 0,
    since      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, device_id, pon_port)
);
-- ONU-roster hygiene ladder state (central/onualert.py) — transition-only paging
-- like pon_fault_state: a re-walk that leaves the condition standing must not
-- re-page. State written even when the alert gate is off. Never opens an outage.
-- Per-PON ONU cap: one row per (OLT, PON) that reached its ONU limit.
CREATE TABLE IF NOT EXISTS pon_capacity_state (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    pon_port   TEXT NOT NULL,
    onus       INTEGER NOT NULL DEFAULT 0,   -- roster count at the transition
    active     INTEGER NOT NULL DEFAULT 0,
    since      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, device_id, pon_port)
);
-- Redundant MAC: one row per duplicated ONU MAC (serial), org-wide across OLTs.
CREATE TABLE IF NOT EXISTS onu_dup_mac_state (
    org_id     TEXT NOT NULL,
    mac        TEXT NOT NULL,                -- normalized (.strip().upper())
    members    INTEGER NOT NULL DEFAULT 0,   -- distinct slots sharing the MAC
    -- slots ONLINE at once; >=2 = live clone/loop and the only case that pages
    -- (C-Data reg tables keep every slot an ONU ever occupied, so dead-member
    -- duplicates are history, not faults — state only, no ntfy)
    online_members INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 0,
    since      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, mac)
);
-- Device health over SNMP (CPU %, RAM, temperature) — one row per device, written
-- off the full /report's `health` key on the edge's SNMP cadence (ingress/health.py).
-- DISPLAY-ONLY: never opens an outage, never pages — the ICMP FSM owns alarms; this
-- just explains them (a router at 98% CPU is why latency looks bad). Latest reading
-- only, no history — the hourly rollup / perf ring stay ICMP-focused.
CREATE TABLE IF NOT EXISTS device_health (
    device_id       INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id          TEXT NOT NULL,
    cpu_pct         REAL,
    mem_used_bytes  INTEGER,
    mem_total_bytes INTEGER,
    mem_pct         REAL,
    temp_c          REAL,
    updated_at      TEXT NOT NULL
);
-- Remote diagnostic SNMP walks — the dashboard queues one against a device, central
-- delivers it to that device's assigned node inside the next full /report reply
-- (like recheck hints and update directives, the edge only ever POLLS — no inbound
-- connection to a probe), and the edge posts the varbind dump to /edge/snmp-walk.
-- status: pending -> done | error. A walk stays 'pending' (re-delivered every report)
-- until a result lands, so an edge restart mid-walk just re-runs it — idempotent.
-- Results are bounded (max_varbinds, server-capped) and retained newest-N per device.
CREATE TABLE IF NOT EXISTS snmp_walks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        TEXT NOT NULL,
    device_id     INTEGER NOT NULL REFERENCES org_devices(id),
    node_id       TEXT NOT NULL,
    root_oid      TEXT NOT NULL,
    max_varbinds  INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    requested_by  TEXT,
    error         TEXT,
    result        TEXT,               -- JSON [[oid, value], ...]
    varbind_count INTEGER,
    created_at    TEXT NOT NULL,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_snmp_walks_pending ON snmp_walks(org_id, node_id, status);
CREATE INDEX IF NOT EXISTS idx_snmp_walks_device ON snmp_walks(org_id, device_id, id);
-- Declarative vendor SNMP health profiles — vendor knowledge as DATA, not edge code.
-- Each row maps health metrics (cpu_pct/mem_pct/mem bytes/temp_c) to vendor OIDs plus
-- a decode rule; the EDGE matches a profile to a device by sysObjectID prefix during
-- its health sweep (ingress/health.py). org_id NULL = global (superadmin-managed,
-- served to every org); else org-local. Delivered in the GET /edge/devices reply, so
-- onboarding a new vendor is a profile row, never an edge code change or rollout.
CREATE TABLE IF NOT EXISTS snmp_profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id            TEXT,              -- NULL => global
    name              TEXT NOT NULL,
    match_sysobjectid TEXT NOT NULL,     -- OID prefix, e.g. 1.3.6.1.4.1.5651
    metrics           TEXT NOT NULL,     -- JSON {metric: {oid, decode, select}}
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- GPON/EPON vendor profiles as data — the optics counterpart of snmp_profiles.
-- spec is the whole closed-vocabulary JSON the edge's gpon_profile_from_dict
-- (ingress/gpon.py) validates: oids{rx,tx,state,distance,serial,name,ident_*},
-- scales, state_map, state_default, pon_index, pon_label. Delivered in the
-- GET /edge/devices reply; built-in huawei/dbc profiles stay in edge code as
-- fallbacks (a same-named row here shadows them), so validating a new vendor's
-- OIDs is a dashboard row, never an edge rollout.
CREATE TABLE IF NOT EXISTS gpon_profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id            TEXT,              -- NULL => global
    name              TEXT NOT NULL,
    match_sysobjectid TEXT NOT NULL DEFAULT '',
    spec              TEXT NOT NULL,     -- JSON, closed vocabulary (see above)
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- Per-device, per-subsystem SNMP sweep diagnosis, reported by the edge on every
-- SNMP cadence ("snmp_status" on the full report). This is what lets the dashboard
-- say WHY a panel is blank (agent silent vs subtree empty vs walk timeout vs no
-- vendor profile) instead of showing "no data" — the guided-troubleshooting flow
-- reads it. state is the edge's closed vocabulary: ok | empty | no_response |
-- timeout | no_profile | error. last_ok_at survives non-ok states so the UI can
-- say "was working until <ts>".
CREATE TABLE IF NOT EXISTS device_snmp_status (
    device_id   INTEGER NOT NULL REFERENCES org_devices(id),
    org_id      TEXT NOT NULL,
    subsystem   TEXT NOT NULL,           -- health | ports | optics
    state       TEXT NOT NULL,
    detail      TEXT,
    sysobjectid TEXT,
    profile     TEXT,                    -- matched vendor profile, if any
    item_count  INTEGER,
    updated_at  TEXT NOT NULL,
    last_ok_at  TEXT,
    PRIMARY KEY (device_id, subsystem)
);
-- Paywall: which calendar months ('YYYY-MM', UTC) an org has paid for. The
-- superadmin marks these from the Organizations page — as far ahead as he
-- likes (pre-marked months get no reminder). A pro/vip org whose CURRENT
-- month has no row here is locked out of the dashboard (server.py's 402
-- gate); edge ingest and outage paging are deliberately never gated. Free
-- plan ignores this table entirely.
CREATE TABLE IF NOT EXISTS org_billing_months (
    org_id    TEXT NOT NULL,
    month     TEXT NOT NULL,
    marked_by TEXT,
    marked_at TEXT NOT NULL,
    PRIMARY KEY (org_id, month)
);
-- Razorpay checkout ledger (central/razorpay.py): one row per order created
-- from the dashboard's Pay button. `months` is the comma-joined 'YYYY-MM'
-- list the order buys; verification flips status created→paid exactly once
-- (settle_billing_payment's WHERE status='created' guard) and only then are
-- the months marked in org_billing_months (marked_by 'razorpay:<payment_id>').
CREATE TABLE IF NOT EXISTS billing_payments (
    order_id     TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    plan         TEXT NOT NULL,
    months       TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    payment_id   TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    paid_at      TEXT
);
-- Transition-only billing reminders (central/billing.py, watchdog pattern):
-- kind = 'due_soon' | 'locked', one row per (org, month, kind). Only
-- status 'sent'/'skipped' suppress a retry — a failed ntfy send is retried
-- on the next sweep instead of stranding the reminder.
CREATE TABLE IF NOT EXISTS billing_notices (
    org_id  TEXT NOT NULL,
    month   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    status  TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (org_id, month, kind)
);
-- Server-wide dashboard settings the SUPERADMIN manages once for every org
-- (e.g. google_maps_key: pasted once, served to all orgs' browsers). NOT the
-- Config env-var layer — those stay frozen WISP_* tunables; this is for
-- dashboard-entered credentials/state, same split as topology and routing.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Operator verdicts on what a device's hardware can and cannot do. supported=0
-- means "proven absent — stop flagging it" (e.g. a switch with no temperature
-- sensor, an OLT whose firmware only refreshes optics from its web UI). The
-- admin coverage overview and the device panel both suppress nagging for
-- unsupported subsystems; the edge keeps probing regardless (cheap, and a
-- firmware upgrade that adds the OID starts working with zero reconfiguration).
CREATE TABLE IF NOT EXISTS device_capability (
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    org_id     TEXT NOT NULL,
    subsystem  TEXT NOT NULL,            -- health | ports | optics
    supported  INTEGER NOT NULL DEFAULT 1,
    note       TEXT,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (device_id, subsystem)
);
-- Web-UI proxy (webplan.md M1). The RECORD of a tunnel session — the live
-- tunnel itself is process memory in central/proxy.py and dies with the
-- process; these rows are the who-opened-what-against-which-device trail.
CREATE TABLE IF NOT EXISTS proxy_sessions (
    sid            TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL,
    device_id      INTEGER NOT NULL,
    node_id        TEXT NOT NULL,
    created_by     INTEGER,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',  -- open | closed | expired
    last_active_at TEXT
);
-- One row per proxied request (non-negotiable — webplan.md §6.3). Pruned to
-- PROXY_AUDIT_KEEP_DAYS lazily on session create.
CREATE TABLE IF NOT EXISTS proxy_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sid       TEXT NOT NULL,
    org_id    TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    user_id   INTEGER,
    method    TEXT NOT NULL,
    path      TEXT NOT NULL,
    status    INTEGER,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxy_audit_org ON proxy_audit(org_id, id);
"""

class CentralStore(
    OrgStoreMixin,
    UserStoreMixin,
    FleetStoreMixin,
    DeviceStoreMixin,
    OutageStoreMixin,
    SnmpStoreMixin,
    ProxyStoreMixin,
):

    _TENANT_TABLES = (
        "orgs", "nodes", "node_tokens", "devices", "events", "rollups", "node_alerts",
        "users", "org_workers", "org_attendance", "org_devices", "device_states",
        "outages", "device_rollups", "alert_log", "escalations", "rollouts",
        "switch_ports", "org_device_links", "device_redundancy", "device_perf_samples",
        "device_perf",
    )


    _CENTRAL_NODE = "central"

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            self._migrate_tenant_to_org(conn)
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn, "orgs", (
                ("ntfy_topic_owner", "TEXT"), ("ntfy_topic_operator", "TEXT"),
                ("ntfy_topic_tech", "TEXT"),
                # Map view viewport lock; a key from the dashboard's region list
                # (web/src/lib/map-regions.ts), e.g. "telangana". NULL = all-India.
                # (google_maps_key briefly lived here too — moved to app_settings
                # 2026-07-11, one superadmin key for every org; the org column may
                # linger in older DBs, dead.)
                ("map_region", "TEXT"),
                # Dashboard-set probe cadence for this org's edges, seconds.
                # NULL = automatic (edge env/adaptive default). API clamps to
                # 10–120s: past 120s the fleet watchdog's 180s stale threshold
                # would page NODE_STALE for a healthy probe.
                ("poll_interval_s", "INTEGER"),
                # Paywall tier: free | pro | vip (central/billing.py PLANS).
                # Superadmin-set only; drives the device cap and the monthly
                # payment lock (org_billing_months).
                ("plan", "TEXT NOT NULL DEFAULT 'free'"),
                # Web-UI proxy capability (webplan.md §6.7): opt-in per org,
                # superadmin-set — THE activation gate since v0.15.8
                # (cfg.proxy_enabled defaults on; =0 is the emergency kill
                # switch, per side).
                ("web_proxy", "INTEGER NOT NULL DEFAULT 0"),
                # Fleet auto-update: when a newer release lands in the mirror,
                # central starts the (staged, health-gated) rollout itself —
                # first stale heartbeat becomes the canary (central/rollout.py:
                # maybe_auto_rollout). Off = updates stay dashboard-clicked.
                ("auto_update", "INTEGER NOT NULL DEFAULT 0")))
            self._ensure_columns(conn, "nodes", (
                ("restart_pending", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "onu_dup_mac_state", (
                ("online_members", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "switch_ports", (
                ("bw_threshold_mbps", "REAL"), ("bw_direction", "TEXT"),
                ("in_octets", "TEXT"), ("out_octets", "TEXT"), ("counters_at", "TEXT"),
                ("in_bps", "REAL"), ("out_bps", "REAL"),
                ("bw_low_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_alarm", "INTEGER NOT NULL DEFAULT 0"), ("bw_alarm_since", "TEXT"),
                ("bw_max_mbps", "REAL"),
                ("bw_high_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_high_alarm", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_high_alarm_since", "TEXT")))
            self._ensure_columns(conn, "org_devices", (
                ("assigned_node_id", "TEXT"),
                ("optical_warn_dbm", "REAL"), ("optical_crit_dbm", "REAL"),
                ("gpon_vendor", "TEXT"),
                ("lat", "REAL"), ("lng", "REAL"),
                # passive plant only (splitter/fdb/closure): which PON it serves
                ("pon_port", "TEXT"),
                # OLT only: per-PON ONU cap override (NULL = cfg.onu_pon_limit, the
                # EPON 1:64 default); a 1:128 GPON box raises it so it never
                # false-pages "at capacity" (central/onualert.py)
                ("onu_pon_limit", "INTEGER")))
            # when this ONU was last seen online — central/ponfault.py reads it to
            # spot a mass drop ("N ONUs dark within one walk") without a history table
            self._ensure_columns(conn, "onu_optics", (
                ("last_online_at", "TEXT"),))
            self._seed_google_key(conn)
            conn.commit()


    @staticmethod
    def _seed_google_key(conn) -> None:
        # The Google Maps key moved from the per-org orgs.google_maps_key column
        # to the server-wide app_settings table (2026-07-11). A DB that set the
        # key BEFORE that move still carries it only in the now-dead column, and
        # app_settings is empty — so the map silently drops to the CARTO
        # fallback. Promote the lingering key ONCE so Google stays the default
        # across the upgrade. Superadmin Settings still overrides it any time.
        has = conn.execute(
            "SELECT 1 FROM app_settings WHERE key='google_maps_key'").fetchone()
        if has:
            return
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orgs)")}
        if "google_maps_key" not in cols:
            return
        row = conn.execute(
            "SELECT google_maps_key AS k FROM orgs"
            " WHERE google_maps_key IS NOT NULL AND TRIM(google_maps_key) <> ''"
            " LIMIT 1").fetchone()
        if row and row["k"]:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('google_maps_key', ?)",
                (row["k"].strip()[:128],))


    @staticmethod
    def _ensure_columns(conn, table: str, coldefs: tuple[tuple[str, str], ...]) -> None:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, sqltype in coldefs:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")


    @classmethod
    def _migrate_tenant_to_org(cls, conn) -> None:
        for table in cls._TENANT_TABLES:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "tenant_id" in cols and "org_id" not in cols:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN tenant_id TO org_id")


    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn


    def _scope(self, org_id, prefix="") -> tuple[str, tuple]:
        if not org_id:
            return "", ()
        return f" AND {prefix}org_id = ?", (org_id,)


    def data_version(self, org_id: str | None = None) -> str:
        escope, eargs = self._scope(org_id, prefix="e.")
        oscope, oargs = self._scope(org_id, prefix="o.")
        sscope, sargs = self._scope(org_id, prefix="sp.")
        nscope, nargs = self._scope(org_id, prefix="n.")
        gscope, gargs = self._scope(org_id, prefix="g.")
        with self._connect() as conn:
            e = conn.execute(
                "SELECT COALESCE(MAX(e.id),0) FROM events e WHERE 1=1" + escope,
                eargs).fetchone()[0]
            o = conn.execute(
                "SELECT COALESCE(MAX(o.id),0) FROM outages o WHERE 1=1" + oscope,
                oargs).fetchone()[0]
            s = conn.execute(
                "SELECT COALESCE(MAX(sp.updated_at),'') FROM switch_ports sp"
                " WHERE 1=1" + sscope, sargs).fetchone()[0]
            n = conn.execute(
                "SELECT COALESCE(MAX(n.last_seen),'') FROM nodes n"
                " WHERE 1=1" + nscope, nargs).fetchone()[0]
            g = conn.execute(
                "SELECT COALESCE(MAX(g.updated_at),'') FROM onu_optics g"
                " WHERE 1=1" + gscope, gargs).fetchone()[0]
            wscope, wargs = self._scope(org_id, prefix="w.")
            # MAX(id) moves on queue, MAX(completed_at) on a result landing — both
            # must bump the fingerprint or the walk dialog needs a hard refresh.
            w = conn.execute(
                "SELECT COALESCE(MAX(w.id),0) || ':' || COALESCE(MAX(w.completed_at),'')"
                " FROM snmp_walks w WHERE 1=1" + wscope, wargs).fetchone()[0]
        return f"{e}.{o}.{s}.{n}.{g}.{w}"
