from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from wisp.central import fiber
from wisp.central.store_assign import AssignmentStoreMixin
from wisp.central.store_orgs import OrgStoreMixin
from wisp.central.store_users import UserStoreMixin
from wisp.central.store_fleet import FleetStoreMixin
from wisp.central.store_devices import DeviceStoreMixin
from wisp.central.store_field import FieldStoreMixin
from wisp.central.store_history import HistoryStoreMixin
from wisp.central.store_outages import OutageStoreMixin
from wisp.central.store_proxy import ProxyStoreMixin
from wisp.central.store_radius import RadiusStoreMixin
from wisp.central.store_snmp import SnmpStoreMixin
from wisp.central.store_util import (  # noqa: F401
    SNMP_STATUS_STATES, SNMP_SUBSYSTEMS, SNMP_WALKS_KEEP,
    _now_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    org_id        TEXT PRIMARY KEY,
    name             TEXT,
    ntfy_topic       TEXT,                 -- per-org page target for the fleet watchdog
    -- Per-role outage routing. TWO channels since 2026-07-21 (roles collapsed to
    -- owner+worker): the owner gets outage opens, the worker channel carries the
    -- SNMP-derived stream + escalation. An older DB still has the dead
    -- ntfy_topic_operator/ntfy_topic_tech columns — operator's VALUE was copied
    -- into ntfy_topic_worker at migration so no subscribed phone went quiet.
    ntfy_topic_owner    TEXT,
    ntfy_topic_worker   TEXT,
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
    role       TEXT NOT NULL DEFAULT 'worker',  -- owner|worker within the org
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- (The credential-less org_workers roster + org_attendance were REMOVED 2026-07-21 —
-- see the "Removed" note in CLAUDE.md. The tables may linger in an older DB, unread.)
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
    tags             TEXT,               -- comma-separated free-form labels
                                          -- (dashboard filtering); cosmetic only,
                                          -- never in the engine topology fingerprint
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
-- `org_devices.region` stays plain text; list_regions returns
-- the UNION of declared + in-use names, so pre-table free-text regions surface
-- without any backfill.
-- Operator colour-coding, presentation ONLY (central/inventory.py:PALETTE).
-- A row per coloured tag or probe; absent = uncoloured, which is the default and
-- the "clear" state. Keyed by TEXT rather than a foreign key on purpose: a tag
-- is only ever text inside org_devices.tags, and a probe may exist as a
-- node_tokens row, a nodes row, or both. Nothing here reaches the engine.
CREATE TABLE IF NOT EXISTS org_colors (
    org_id  TEXT NOT NULL,
    kind    TEXT NOT NULL,            -- 'tag' | 'node'
    key     TEXT NOT NULL,            -- the tag text / node_id
    color   TEXT NOT NULL,            -- a PALETTE name, never free hex
    PRIMARY KEY (org_id, kind, key)
);
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
    payload    TEXT,
    kind       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_log_outage ON alert_log(outage_id);
-- the cooldown index rides `kind`, added by _ensure_columns for older DBs, so
-- it's built in __init__ AFTER that runs, not here.
-- DIGEST-tier notifications wait here until the hourly flush composes them into
-- one summary (central/notify_policy.py). `sent_at` NULL = still pending; the
-- flush anchors its interval on the OLDEST pending row so no per-org clock is
-- needed. State rows for these alerts still live in their own tables — this is
-- only the notification queue.
CREATE TABLE IF NOT EXISTS alert_digest (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    device_id  INTEGER,
    kind       TEXT NOT NULL,
    title      TEXT,
    body       TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_digest_pending
    ON alert_digest(org_id, sent_at, created_at);
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
    -- Operator-declared physical cabling, the child-side mirror of feeds_device_id:
    -- THIS port on the child faces THAT parent (primary or backup). Together the two
    -- columns name both ends of a link — switch_ports stays the ONLY port registry,
    -- so the map's bandwidth labels and ports.py's outage folding can never disagree
    -- about which port carries a link. Never touched by a walk (upsert omits it).
    uplink_device_id INTEGER REFERENCES org_devices(id),
    UNIQUE(org_id, device_id, if_index)
);
CREATE INDEX IF NOT EXISTS idx_switch_ports_device ON switch_ports(org_id, device_id);
CREATE INDEX IF NOT EXISTS idx_switch_ports_feeds ON switch_ports(org_id, feeds_device_id);
-- WHO GETS PAGED for a device — a NOTIFICATION rule, nothing else. A field
-- account named here is responsible for that device and everything BELOW it
-- (central/assignment.py walks the parent chain), so one row on a region head
-- covers the region. Deliberately NOT read by any view, list, KPI or export:
-- every account still sees the whole fleet (operator choice 2026-07-26), which
-- is why this is not a permission table and carries no read semantics at all.
--
-- An UNASSIGNED device keeps paging every worker (assignment.py's fallback), so
-- turning this on can never silence an alarm — the same instinct as the notify
-- governor writing state rows regardless of the allowlist.
--
-- user_id, not username: an account rename must not silently re-point a page,
-- and ON DELETE CASCADE means deleting an account can't leave a row that
-- resolves to nobody (which would read as "assigned" while paging no one).
CREATE TABLE IF NOT EXISTS org_device_workers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT NOT NULL,
    device_id   INTEGER NOT NULL REFERENCES org_devices(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by TEXT,
    assigned_at TEXT,
    UNIQUE(device_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_org_device_workers_org ON org_device_workers(org_id);
CREATE INDEX IF NOT EXISTS idx_org_device_workers_user ON org_device_workers(org_id, user_id);
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
-- Per-link MAP PRESENTATION for one link, geometry and styling both. Keyed by the
-- (child, parent) pair so it covers the implicit primary link
-- (org_devices.parent_device_id), the backup rows above and cross-links alike.
-- Waypoints are the INTERMEDIATE vertices only, ordered parent→child — endpoints
-- stay implicit (the device pins), so moving a pin rubber-bands the route instead
-- of orphaning it. Dashboard-side only; the edge never sees geometry.
--
-- `cable_id`/`core_no`/`cores` are ALL DEAD, and so is `color` (2026-08-08). They
-- are the fossil record of three earlier answers to "which glass is this span": a
-- decorative tint operators used to group spans by drum, then a fibre count on the
-- span itself, then a membership pointing at a cable. Fibre is its own graph now
-- (see _FIBRE_SCHEMA) and none of them is read. Left in place rather than migrated
-- away, the ntfy-topics convention — but note that a dead column with a REFERENCES
-- clause is NOT inert: `cable_id` pinned cables it named and made them undeletable
-- until the rebuild cleared it. What survives here is CARTOGRAPHY: `label_pos`, an
-- operator's 0..1 fraction along the rendered path, and the waypoints above.

CREATE TABLE IF NOT EXISTS link_routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    child_id   INTEGER NOT NULL REFERENCES org_devices(id),
    parent_id  INTEGER NOT NULL REFERENCES org_devices(id),
    waypoints  TEXT NOT NULL,            -- JSON [[lat,lng],...]
    color      TEXT,                     -- DEAD since 2026-08-08; see above
    label_pos  REAL,                     -- 0..1 along the path; NULL = midpoint
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
-- Per-device web-UI login, so a tech never retypes a switch/OLT admin password.
-- The password is ENCRYPTED at rest (central/secretbox.py) under a key kept
-- outside the DB; username is plaintext (not a secret). A SEPARATE table on
-- purpose: the ciphertext must never ride list_org_devices() into the browser.
CREATE TABLE IF NOT EXISTS device_webui_credentials (
    device_id    INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id       TEXT NOT NULL,
    username     TEXT,
    password_enc TEXT,                    -- secretbox token; NULL = no password stored
    auth_mode    TEXT NOT NULL DEFAULT 'form',   -- 'form' = login-form device (default;
                                                  -- inject the autofill bootstrap); 'basic' =
                                                  -- HTTP Basic popup (inject Authorization header)
    updated_by   TEXT,
    updated_at   TEXT NOT NULL
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
-- Per-ONU optics SCRAPED from the OLT's own web UI (central/weboptics.py), the
-- only source of Rx on C-Data/DBC EPON. Deliberately its own table rather than
-- more columns on onu_optics: the scrape rides a slow independent clock while
-- onu_optics is rewritten by every SNMP walk, so one table would make "which
-- half of this row is fresh?" unanswerable. These rows are an INPUT that the
-- optics fold merges by MAC (weboptics.merge_scraped) — onu_optics stays the
-- one place severity, the badge and PON-fault read from.
CREATE TABLE IF NOT EXISTS onu_web_optics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT NOT NULL,
    device_id   INTEGER NOT NULL REFERENCES org_devices(id),   -- the OLT
    onu_key     TEXT NOT NULL,   -- "<pon>.<onu_id>", same shape the roster uses
    serial      TEXT,            -- MAC as the page prints it — the merge key
    rx_dbm      REAL,
    tx_dbm      REAL,
    -- REAL METRES, unlike the dbc SNMP profile's distance (EPON time quanta);
    -- this page is where an honest fibre-cut bracket comes from.
    distance_m  INTEGER,
    temp_c      REAL,
    voltage_v   REAL,
    tx_bias_ma  REAL,
    scraped_at  TEXT NOT NULL,
    UNIQUE(org_id, device_id, onu_key)
);
CREATE INDEX IF NOT EXISTS idx_onu_web_optics_device
    ON onu_web_optics(org_id, device_id);
-- The MAC a SUBSCRIBER'S OWN equipment presents behind an ONU, read off the
-- OLT's address table (central/webmacs.py). Not the ONU's MAC: on the GPON
-- fleet the ONU has no MAC at all (its identity is a GPON serial like
-- ZTEGcbd796ed), so this is the only address that customer has, and it is what
-- the ISPs' RADIUS app is keyed on. Before this they read it by opening the
-- OLT's per-ONU page one customer at a time.
--
-- Its own table, for the reason onu_web_optics is: the address table rides its
-- own clock and one table would make "which half of this row is fresh?"
-- unanswerable. Joined to the roster on (device_id, onu_key) — the SLOT, never
-- the serial, the same identity rule parse_onu_table keeps.
--
-- ONE SLOT MAY CARRY SEVERAL, so the MAC is part of the key: a customer router
-- plus what is bridged behind it, or one device presenting on several service
-- VLANs (1-5 observed per slot in the field). Picking one would be a guess
-- about which is "the" customer.
--
-- ROWS ARE NEVER DELETED, only re-stamped. This is a LEARNED forwarding table
-- and it ages out, so an ONU that is offline or merely idle drops off the page
-- while still being the same customer with the same router. Keeping the last
-- known address (with its date, so the UI can grade it) is what makes this
-- useful for exactly the customer who has gone down — and a MAC that has aged
-- out is stale, not absent. A subscriber who changes router accumulates a
-- second row, which is honest and is also what RADIUS will still be holding.
CREATE TABLE IF NOT EXISTS onu_user_macs (
    org_id        TEXT NOT NULL,
    device_id     INTEGER NOT NULL REFERENCES org_devices(id),   -- the OLT
    onu_key       TEXT NOT NULL,   -- "<pon>.<onu_id>", the roster's own key
    mac           TEXT NOT NULL,   -- as the page prints it, upper, colon-separated
    vlan          TEXT,            -- service VLAN, straight off the same row
    kind          TEXT,            -- Dynamic | Static, the OLT's own word
    port_label    TEXT,            -- e.g. "EPON0/8:38" / "PON2:ONU36", as printed
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, onu_key, mac)
);
CREATE INDEX IF NOT EXISTS idx_onu_user_macs_device
    ON onu_user_macs(org_id, device_id);
CREATE INDEX IF NOT EXISTS idx_onu_user_macs_mac
    ON onu_user_macs(org_id, mac);
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
-- THE OPERATOR'S RECORD OF A SUBSCRIBER — everything about a drop that no walk
-- can tell us: who lives there, the number to ring, where the box is, and
-- whether its power can be relied on. Sparse by construction: no row means
-- nobody has written anything down, so a stock org carries none.
--
-- It began (2026-07-28) as a pure REFERENCE-POINT table — a coordinate and a
-- power claim — and the field survey then hung `label` and `phone` on it, which
-- is how the customer record ended up as a passenger on a map pin. That cost two
-- things until 2026-08-03: a name and a number could not be recorded WITHOUT a
-- coordinate (both write paths demanded lat/lng), so an ISP with 2,156
-- subscribers and a handful of pins had nowhere to put the other 2,150 names;
-- and "Remove" on the map card — an eye-off icon that reads as "hide this pin" —
-- ran a DELETE and took the name and phone number with it, silently.
--
-- So: LAT/LNG ARE NULLABLE, still both-or-nothing. Clearing a pin clears the
-- COORDINATES and leaves the record standing (`clear_onu_place_coords`); the row
-- is deleted only when it holds nothing at all — no coordinates, no name, no
-- number, no notes, and not a witness (`_prune_onu_place`, the rule
-- `_prune_link_route` already follows for link styling). A reader that means
-- "has a PIN" must therefore say `lat IS NOT NULL` and not merely "has a row" —
-- `onu_place_macs(located_only=True)` is that question, and the survey's
-- coverage count is its one caller.
--
-- PLACING IS THE WITNESS CLAIM, so UNPLACING RETRACTS IT (`witness=0` on the
-- same UPDATE). The ISP picks the subscribers it knows run on a UPS, solar or a
-- tower supply; nothing infers that and there is no power column, so the act of
-- vouching is the whole signal — which is why the UI states the contract at the
-- click, and why clearing the pin has to be read as taking it back. A witness
-- that outlived its pin would be a live input to a fibre-cut verdict with
-- nothing left on the one screen that lists witnesses: the exact inverse of "a
-- pin that quietly stopped witnessing is the one failure this list must not
-- conceal". It also keeps alerting byte-identical across this change — a bare
-- reference point, vouched for but never named, still prunes away completely.
-- Only what an operator TYPED survives an unpin.
--
-- Keyed on the MAC/serial (onuroster._norm_mac form), NOT on (device, onu_key):
-- onu_optics never deletes a removed ONU's row and a re-registered ONU changes
-- slot, so a slot key would rot. The MAC is the sticker on the box in the
-- customer's house, so re-homing a drop to another PON — or another OLT — moves
-- the record with it and needs no click. An RMA'd ONU orphans its row, which is
-- correct: the box really did change.
CREATE TABLE IF NOT EXISTS onu_places (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    mac        TEXT NOT NULL,      -- onuroster._norm_mac of the serial
    lat        REAL,               -- both-or-nothing, and NULL = no pin yet (or
    lng        REAL,               -- cleared). The record itself survives.
    label      TEXT,               -- operator's name for the subscriber
    notes      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(org_id, mac)
);
CREATE INDEX IF NOT EXISTS idx_onu_places_org ON onu_places(org_id);
-- Which passive box a subscriber's drop actually comes off (`central/drops.py`).
--
-- An access network is OLT PON -> feeder -> splitter (1:2/1:4/1:8) -> possibly a
-- SECOND splitter -> drop -> ONU, and an ISP hangs a customer off whichever
-- splitter is nearest. Until this table the map drew a subscriber straight to its
-- OLT, which skips the entire distribution network the field crew works on. The
-- splitter chain itself already exists (passives are org_devices rows with parent
-- chains and link_routes geometry) — this is the missing LAST hop.
--
-- Keyed on the MAC (onuroster._norm_mac form), NOT (device, onu_key), for the
-- same reason onu_places is: onu_optics never deletes a vacated slot and a
-- re-registered ONU moves, so a slot key rots. Re-homing a drop keeps its
-- splitter; an RMA'd box orphans the row, which is reported, not hidden.
--
-- The PON is deliberately NOT stored here — it comes from the ONU's roster row,
-- which SNMP owns. A second copy could disagree with the walk about which PON a
-- subscriber is on, and then two screens would tell different stories.
--
-- `passive_id` is any PASSIVE_TYPES row (a splitter, or the FDB/closure housing
-- one), never powered gear: what is recorded is the box the drop comes out of.
CREATE TABLE IF NOT EXISTS onu_drops (
    org_id     TEXT NOT NULL,
    mac        TEXT NOT NULL,      -- onuroster._norm_mac of the serial
    passive_id INTEGER NOT NULL REFERENCES org_devices(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, mac)
);
CREATE INDEX IF NOT EXISTS idx_onu_drops_passive ON onu_drops(org_id, passive_id);
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
    -- The edge stopped at its varbind cap or time budget, so this subtree is
    -- only PARTIALLY dumped. Load-bearing for vendor onboarding: a truncated
    -- walk and a complete one are indistinguishable from the row alone, and
    -- "that OID holds nothing" read off a partial walk is a false negative.
    truncated     INTEGER NOT NULL DEFAULT 0,
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
-- Web-UI optics recipes as data (central/weboptics_profiles.py) — the third
-- profile table, for the vendors whose per-ONU Rx exists in NO SNMP OID and can
-- only be read off the OLT's own page (C-Data/DBC EPON, proven exhaustively).
-- `name` is deliberately the SAME token as gpon_profiles.name /
-- org_devices.gpon_vendor: that is already how a device is bound to a vendor,
-- and a second web-only notion of "which vendor is this" could disagree with it
-- about the same OLT. spec is the whole closed-vocabulary JSON
-- (clean_web_optics_profile_payload): paths, login/optics form shape, session
-- strategy, charset, columns BY HEADING. Built-in 'dbc' stays in code as the
-- fallback; a same-named row shadows it, a disabled row switches it OFF.
CREATE TABLE IF NOT EXISTS web_optics_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT,                    -- NULL => global
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,           -- JSON, closed vocabulary (see above)
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_web_optics_profiles_scope
    ON web_optics_profiles(IFNULL(org_id, ''), name);
-- Outcome of the last web-optics scrape, per OLT (central/weboptics_sweep.py).
-- The sweep used to leave its verdict in the log only, which meant a blank dBm
-- column on the dashboard had no explanation anywhere a user could reach: "this
-- vendor has no Rx", "nobody has typed the OLT's password" and "the scrape has
-- been failing for a day" all render as the same empty column. Same job
-- device_snmp_status does for a blank Ports/Optical panel, and the same closed
-- vocabulary discipline: state is one of ok | partial | skipped | no_profile |
-- no_credentials | unreachable | login | error. last_ok_at survives a failure so
-- the panel can say "was working until <ts>".
CREATE TABLE IF NOT EXISTS web_optics_status (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id      TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    detail      TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    last_ok_at  TEXT
);
-- Vendor recipe for the OLT's ADDRESS TABLE page, the same shape and the same
-- rules as web_optics_profiles (closed vocabulary, whole profile rejected on
-- anything outside it, org_id NULL = global, a same-named row shadows a
-- built-in, a disabled row is a tombstone that switches it off).
--
-- SEPARATE from web_optics_profiles on purpose, and the reason is a hardware
-- fact: syrotech_gpon serves this page and provably has NO optical page (the
-- OPM path 404s on that build), so a MAC recipe folded into the optics one
-- could never be configured for the very fleet where it matters most — a GPON
-- ONU has no MAC of its own. The reverse holds too: a vendor may publish
-- optics and no address table. The two pages fail, and are onboarded,
-- independently. The LOGIN half is duplicated in the spec but not in the code:
-- both go through weboptics.login, so there is one implementation of "send the
-- credential only after the login page answered".
CREATE TABLE IF NOT EXISTS web_mac_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT,                    -- NULL => global
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,           -- JSON, closed vocabulary
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_web_mac_profiles_scope
    ON web_mac_profiles(IFNULL(org_id, ''), name);
-- Outcome of the last address-table read, per OLT — same job and the same
-- closed vocabulary as web_optics_status, because a blank MAC column has the
-- same several meanings: this vendor has no recipe, nobody stored the OLT's
-- password, the read has been failing for a day, or the page was TRUNCATED.
-- 'partial' is the truncation case and it is not cosmetic: a short read makes
-- customers who do have an address render exactly like customers who do not.
CREATE TABLE IF NOT EXISTS web_mac_status (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id      TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    detail      TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    declared    INTEGER,                 -- the OLT's own total, where it prints one
    updated_at  TEXT NOT NULL,
    last_ok_at  TEXT
);
-- CCTV: an NVR's cameras, read off the NVR's own web API through the tunnel
-- (central/nvr.py + the weboptics sweeper). Cameras are a ROSTER, never
-- org_devices rows — the ONU rule: the tree, billing and the engine fingerprint
-- must not know a camera exists. Identity is the CHANNEL (the slot), never the
-- camera's IP — an IP is a fact ABOUT the row. last_online_at FREEZES when a
-- channel leaves 'online' (the onu_optics rule), so "dark since Tuesday" needs
-- no history table. Unlike onu_optics this table IS pruned on a complete read:
-- the source is deliberate config, not a learned table, so a channel the
-- operator removed from the NVR disappearing here is the honest reading.
-- state is a closed vocabulary: online | offline | unknown — an absent or
-- unrecognised state cell reads 'unknown', never 'offline' (the gpon lesson:
-- a decode gap must not render live cameras dark or page a fabricated drop).
CREATE TABLE IF NOT EXISTS nvr_channels (
    org_id         TEXT NOT NULL,
    device_id      INTEGER NOT NULL REFERENCES org_devices(id),
    channel_no     INTEGER NOT NULL,     -- the NVR's own 0-based slot
    name           TEXT,
    ip_address     TEXT,
    port           INTEGER,
    camera_kind    TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    monitored      INTEGER NOT NULL DEFAULT 1,  -- operator column, sweep never writes it; unlike ports it defaults ON (8 deliberate cameras, not 28 walked interfaces)
    state          TEXT NOT NULL DEFAULT 'unknown',
    last_online_at TEXT,
    first_seen_at  TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (device_id, channel_no)
);
CREATE INDEX IF NOT EXISTS idx_nvr_channels_org
    ON nvr_channels(org_id, device_id);
-- Outcome of the last channel read, per NVR — the web_mac_status shape and the
-- same reason: an empty Cameras tab has several meanings (no brand set, no
-- stored login, the read failing for a day, a build with no channel table) and
-- they take opposite actions. last_ok_at survives a failure.
CREATE TABLE IF NOT EXISTS nvr_status (
    device_id   INTEGER PRIMARY KEY REFERENCES org_devices(id),
    org_id      TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,           -- ok|partial|skipped|no_profile|no_credentials|unreachable|login|error
    detail      TEXT,
    channels    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    last_ok_at  TEXT
);
-- NVR vendor recipes — the fifth profile table, same rules as gpon/web-optics/
-- web-mac/radius: closed vocabulary, whole profile rejected on anything outside
-- it, org_id NULL = global, a same-named row shadows the built-in, a disabled
-- row is a tombstone. Paths only, never a host (the tunnel boundary).
CREATE TABLE IF NOT EXISTS nvr_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT,                    -- NULL => global
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,           -- JSON, closed vocabulary
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nvr_profiles_scope
    ON nvr_profiles(IFNULL(org_id, ''), name);
-- The org's RADIUS/billing panel: WHERE it is and WHO we sign in as. The recipe
-- (which pages, which columns) is radius_profiles; this row is the one thing that
-- cannot be shared, and it is deliberately the ONLY place a host is stored. The
-- profile carries paths alone, exactly as web_optics_profiles does, so a recipe
-- can never point the credential at a server nobody authorised.
-- MANY rows per org, not one (2026-08-13). An ISP can run several billing
-- panels at once -- Hansa asked for a second the week the first went live, and
-- two brands or two franchise areas on two platforms is the ordinary shape, not
-- an edge case. So an account is the SOURCE, identified by its own id, and
-- org_id is a plain indexed column.
-- ORDER IS PRIORITY, and it is `id` rather than an operator-sorted column: the
-- panel connected FIRST wins a cross-panel tie, which is explicable without a UI
-- for it and is right by default (the established panel outranks the new one).
CREATE TABLE IF NOT EXISTS radius_accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',  -- the operator's name for this panel
    profile      TEXT NOT NULL,
    base_url     TEXT NOT NULL,           -- scheme://host[:port], no path
    username     TEXT,
    password_enc TEXT,                    -- secretbox token; NULL = nothing stored
    enabled      INTEGER NOT NULL DEFAULT 1,
    updated_by   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radius_accounts_org
    ON radius_accounts(org_id, id);
-- The vendor recipe for a billing panel, DATA like gpon_profiles /
-- web_optics_profiles / web_mac_profiles: org_id NULL = global, a same-named row
-- SHADOWS the built-in, a disabled row is a TOMBSTONE (not an absence) so the
-- toggle does not lie on the orgs that shipped with one. Whole profile rejected
-- on anything outside the closed vocabulary — never a best-effort partial, or a
-- column read by position reports one customer's details against another's line.
CREATE TABLE IF NOT EXISTS radius_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT,                    -- NULL => global
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,           -- JSON, closed vocabulary
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_radius_profiles_scope
    ON radius_profiles(IFNULL(org_id, ''), name);
-- The customer as the BILLING SYSTEM holds them, keyed on the RADIUS username
-- because that is the only identifier that panel guarantees. Everything here is
-- the panel's own text and none of it is ours to correct: expiry and balance are
-- stored as the STRINGS they arrived as, deliberately unparsed — "06/01/2024" is
-- day-first or month-first depending on a setting we cannot see, and a date we
-- guessed wrong is worse than a date we simply repeat.
-- Rows are re-stamped and never deleted while the account survives, the same rule
-- onu_user_macs keeps: a customer missing from one export is far more likely a
-- filtered read than a cancelled subscriber.
-- BECAUSE nothing is deleted, LINKING reads only the rows from each account's
-- MOST RECENT read -- `current_roster`'s freshest-walk rule, needed here for the
-- identical reason: a customer who has since given up a router would otherwise go
-- on claiming its MAC forever and make the live customer on that MAC look
-- ambiguous, which links nothing and silently drops a real subscriber.
-- The marker is `seen_seq`, a COUNTER and deliberately not the timestamp: two
-- syncs inside one second stamp the same `_now_iso()`, and then both books read
-- as current. Every row one sync saw carries that sync's number.
-- Stale rows are kept for history and for every read that is not the join.
-- Keyed on the ACCOUNT as well as the org: two panels may both hold a customer
-- called "1001" and they are different people, so a username is only unique
-- inside the book it came from.
CREATE TABLE IF NOT EXISTS radius_customers (
    org_id        TEXT NOT NULL,
    account_id    INTEGER NOT NULL DEFAULT 0,
    username      TEXT NOT NULL,
    name          TEXT,
    mac           TEXT,                  -- upper, colon-separated, via webmacs.normalise_mac
    mobile        TEXT,
    alt_mobile    TEXT,
    acno          TEXT,
    status        TEXT NOT NULL DEFAULT 'unknown',  -- active|expired|inactive|unknown
    expiry        TEXT,                  -- the panel's own string, unparsed
    package       TEXT,
    branch        TEXT,
    area          TEXT,
    address       TEXT,
    balance       TEXT,                  -- the panel's own string, unparsed
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    seen_seq      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, account_id, username)
);
CREATE INDEX IF NOT EXISTS idx_radius_customers_mac
    ON radius_customers(org_id, mac);
-- Which ONU a customer sits behind, RESOLVED ONCE at sync rather than joined on
-- the fly. Two reasons: the join is two hops (slot -> learned MAC -> customer)
-- with a punctuation-blind name fallback, which is not something to re-derive
-- inside list_org_devices, the hottest query in the app; and storing it makes the
-- match AUDITABLE — match_by says which evidence tied this customer to this slot.
-- An AMBIGUOUS match is absent, never a guess: a MAC on two slots, or two
-- customers claiming one MAC, links nothing at all. A name pinned to the wrong
-- drop sends a tech to the wrong house, which is the same failure the web-optics
-- merge refuses for the same reason.
-- account_id says WHICH panel this claim came from, so "who is behind this ONU"
-- stays answerable when an org runs several. The PK is still the SLOT: an ONU
-- has one customer whatever the number of books, and a slot claimed by two
-- panels is settled by account order (see radius_accounts) rather than refused
-- -- two panels naming one MAC is not the LOCATION ambiguity that must link
-- nothing, it is two books describing one person, and the drop is the same drop.
CREATE TABLE IF NOT EXISTS radius_links (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    onu_key    TEXT NOT NULL,
    account_id INTEGER NOT NULL DEFAULT 0,
    username   TEXT NOT NULL,
    match_by   TEXT NOT NULL,            -- mac | name
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, device_id, onu_key)
);
CREATE INDEX IF NOT EXISTS idx_radius_links_user
    ON radius_links(org_id, username);
-- Outcome of the last roster read, same job and the same instinct as
-- web_optics_status: an ONU with no customer name has several meanings — nobody
-- configured a panel, the password is wrong, the sync has been failing for a day,
-- or this subscriber genuinely is not in billing — and they take opposite actions.
-- last_ok_at survives a failure so a panel can still say "was working until <ts>".
-- Per ACCOUNT, because with several panels "the sync is failing" is only a
-- useful sentence once it names which one: one panel refusing the sign-in while
-- another reads fine is the ordinary state of an org mid-onboarding.
-- `forbidden` is its own state and was paid for by Badri Fiber Net, whose login
-- succeeds and whose export answers a permission page: reported as `login` it
-- sends an ISP to change a password that was never wrong.
CREATE TABLE IF NOT EXISTS radius_status (
    account_id  INTEGER PRIMARY KEY,
    org_id      TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,           -- ok|partial|skipped|no_profile|no_credentials|unreachable|login|forbidden|error
    detail      TEXT,
    customers   INTEGER NOT NULL DEFAULT 0,
    linked      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    last_ok_at  TEXT
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
-- Worker location tracking (central/field.py). Workers run the off-the-shelf
-- Traccar Client; it POSTs OsmAnd fixes to the public /field/track ingest.
--
-- The credential is the NODE-TOKEN pattern, deliberately: only a SHA-256 hash is
-- stored, the plaintext is shown once and is rotatable but never recoverable.
-- It rides Traccar's `id` field, which is what keeps the server URL IDENTICAL
-- for every worker — one string to put on screen, in a QR, and to read down a
-- phone line — while identity stays per-person.
CREATE TABLE IF NOT EXISTS field_tokens (
    org_id     TEXT NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    revoked_at TEXT,
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_field_tokens_hash ON field_tokens(token_hash);
-- On-shift periods, declared in the web app. The tracker app's OWN on/off switch
-- is the real toggle — when it is off the phone transmits nothing, which is a far
-- better promise than receiving a worker's evening and choosing not to store it.
-- This is the SECOND, explicit declaration, and the two-tap cost is deliberate:
-- when somebody marks on-shift and no fixes arrive, that DISCREPANCY is the
-- "the OEM battery manager killed the service" alarm. It is a feature, not
-- redundancy, so nothing here may be inferred from the fixes.
CREATE TABLE IF NOT EXISTS worker_shifts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_shifts_open
    ON worker_shifts(org_id, user_id, ended_at);
-- Append-only fixes, PRUNED TO cfg.field_track_retention_days (7) daily, the way
-- rollup.py prunes hourly buckets. Do not ship a change here without the prune:
-- `data/releases/` is the standing example of a directory nothing prunes, and
-- the retention window is the whole privacy argument for the feature.
--
-- UNIQUE(org_id, user_id, ts) makes a replay idempotent. Traccar retries a fix
-- it did not get a 200 for, so without it a flaky link writes the same position
-- several times and inflates a trail into a stutter.
CREATE TABLE IF NOT EXISTS worker_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    accuracy_m  REAL,
    speed_mps   REAL,
    heading     REAL,
    battery_pct INTEGER,
    UNIQUE(org_id, user_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_worker_locations_ts
    ON worker_locations(org_id, user_id, ts);
"""

_FIBRE_SCHEMA = """
-- A CABLE: one sheath SEGMENT, with two ENDS THAT ARE RECORDED.
--
-- That is the whole change. A cable knows where it starts and stops, so core N of it
-- runs end to end BY DEFINITION and there is nothing left to write down about the
-- run. Opening a sheath at a new closure SPLITS it into two cables and splices every
-- core straight through (`cablepath.split`), which is what the crew physically does
-- — and it is what keeps segment-per-span from being a tax on tapping a street.
--
-- AN END IS A FIBRE POINT: a device (an OLT, a switch, a splitter, or one of the
-- passive joint boxes) OR a subscriber. Deliberately not a third registry of places:
-- passive plant already lives in `org_devices` and subscribers in `onu_places`, and
-- a customer had to become a possible end because these operators daisy-chain a lane
-- — core 1 into this house, cores 2-4 onward to the next three. Hence a nullable
-- PAIR per end with exactly one side set: the device FK is what makes deleting a box
-- take its cable with it, and a MAC is what a subscriber is keyed on everywhere else
-- (`onu_places` is keyed (org, mac) and has no stable id to point at).
--
-- Deliberately NOT a device: no state, no FSM, no outage, absent from
-- org_device_topology, read by no alerting shell. Recording fibre can never re-page
-- a fleet — the standing a splitter's split ratio has, and the reason this whole
-- surface is safe to hand to an operator mid-survey.
--
-- `path` IS THE GLASS ON THE GROUND and it is COMPLETE, first vertex to last —
-- unlike `link_routes.waypoints`, which omits its ends because those are two device
-- pins and the line must rubber-band when one is dragged. A cable ends wherever the
-- glass does. Which end of `path` belongs to `a` is NOT stored: `cablepath.orient`
-- measures it, so a cable drawn backwards still draws correctly and a retrace cannot
-- silently flip it.
CREATE TABLE IF NOT EXISTS org_cables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT NOT NULL,
    name        TEXT NOT NULL,       -- required: a sheath nobody can name is not an object
    cores       INTEGER,             -- fiber.FIBER_COUNTS; NULL = unsurveyed
    path        TEXT,                -- JSON [[lat,lng],...] COMPLETE; NULL = untraced
    a_device_id INTEGER REFERENCES org_devices(id),
    a_mac       TEXT,                -- exactly one of a_device_id / a_mac
    b_device_id INTEGER REFERENCES org_devices(id),
    b_mac       TEXT,                -- …and the two ends may not be the same point
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_cables_org ON org_cables(org_id);
CREATE INDEX IF NOT EXISTS idx_org_cables_a ON org_cables(org_id, a_device_id);
CREATE INDEX IF NOT EXISTS idx_org_cables_b ON org_cables(org_id, b_device_id);

-- WHAT EACH FIBRE CARRIES. Sparse: a row exists only for a core somebody has written
-- something against, because the whole point of this schema is that an unrecorded
-- core stays unrecorded rather than being claimed as spare.
--
-- Free text on purpose. A real core register reads "BSNL leased line", "village A
-- tower", "reserved for the new OLT" — a mixture of destinations, customers and
-- intentions no closed vocabulary survives contact with. Where a core GOES is
-- derived (`fiber.trace`) and never typed here: what a core carries is the operator's
-- claim, where it runs is the record's.
CREATE TABLE IF NOT EXISTS org_cable_cores (
    org_id     TEXT NOT NULL,
    cable_id   INTEGER NOT NULL REFERENCES org_cables(id),
    core_no    INTEGER NOT NULL,
    label      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    PRIMARY KEY (cable_id, core_no)
);
CREATE INDEX IF NOT EXISTS idx_org_cable_cores_org ON org_cable_cores(org_id);

-- A JOINT: at this point, this fibre is joined to that one — or taken out to the
-- equipment standing here.
--
-- ONE TABLE FOR BOTH because they are the same kind of statement (this fibre ends
-- here, in this way) and because they consume a fibre end identically: one fibre
-- joins exactly one fibre, whether the other side is another strand or an OLT's PON
-- port. `b_cable_id IS NULL` is the termination, and it is the ONLY way a core is
-- attached to a box — which is why a device connection needs no table of its own.
--
-- The POINT is the same nullable pair the cable's ends are, and it must be one of
-- BOTH cables' own ends (`fiber.joint_refusal` → `absent`): a strand passing a
-- closure it was never cut at is not available to be joined there.
--
-- Canonical (a_cable, a_core) < (b_cable, b_core) so one splice is one row whichever
-- fibre the operator picked up first — the rule cross-links and the old runs kept.
CREATE TABLE IF NOT EXISTS org_fibre_joints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    device_id  INTEGER REFERENCES org_devices(id),   -- the point: exactly one of
    mac        TEXT,                                 -- device_id / mac is set
    a_cable_id INTEGER NOT NULL REFERENCES org_cables(id),
    a_core_no  INTEGER NOT NULL,
    b_cable_id INTEGER REFERENCES org_cables(id),    -- NULL = terminates into the
    b_core_no  INTEGER,                              -- equipment at this point
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_fibre_joints_org ON org_fibre_joints(org_id);
CREATE INDEX IF NOT EXISTS idx_org_fibre_joints_device ON org_fibre_joints(org_id, device_id);
CREATE INDEX IF NOT EXISTS idx_org_fibre_joints_mac ON org_fibre_joints(org_id, mac);
CREATE INDEX IF NOT EXISTS idx_org_fibre_joints_a ON org_fibre_joints(a_cable_id);
CREATE INDEX IF NOT EXISTS idx_org_fibre_joints_b ON org_fibre_joints(b_cable_id);
"""

_HIST_SCHEMA = """
-- THE HISTORIAN — see notes/viz-plan.md Stage 1. Rules that shaped every table:
--   * ANSWERS, never evidence: derived counts/rates/percentiles a named chart
--     reads — no raw payloads, ever (those stay in snmp_walks/proxy_audit).
--   * Numbers only, wide rows. Time columns are INTEGER epoch seconds (UTC) —
--     a deliberate deviation from the ISO-TEXT convention: smaller rows, exact
--     bucket arithmetic, no parse ambiguity; the API converts at the edge.
--     Buckets floor on epoch hours / epoch days (UTC).
--   * A missed sweep writes NOTHING; a frozen/stale reading is never sampled.
--     Rows only exist when a walk actually arrived, so a gap IS the record.
--     `samples` on the hour/day tiers counts coverage against the expected
--     cadence — the probe-honesty channel every chart renders under its axis.
--   * WITHOUT ROWID, PK = the read path, no secondary indexes (device_rollups
--     pays for a redundant one; not repeating that). org_id TEXT on every
--     table so the org-delete introspection sweeps them (pinned by a test).
--   * Retention ladder + hard row caps live in central/history.py; the nightly
--     maintenance thread prunes by age AND enforces the caps, so unbounded
--     growth is impossible even if a clock runs wild.
--
-- One row per OLT optics walk: the folded truth AFTER every gate (rail guard,
-- sane_rx, web-optics merge) — the same numbers olt_optics' badge writes.
-- rx percentiles are over ONLINE ONUs whose walk carried a usable Rx;
-- `measured` is that population's size (the "N of M measured" honesty figure).
CREATE TABLE IF NOT EXISTS hist_olt_sweep (
    org_id    TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES org_devices(id),
    ts        INTEGER NOT NULL,
    onus      INTEGER NOT NULL,
    online    INTEGER NOT NULL,
    warn      INTEGER NOT NULL,
    crit      INTEGER NOT NULL,
    measured  INTEGER NOT NULL,
    rx_med    REAL,
    rx_p10    REAL,
    rx_min    REAL,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;
-- Hourly digest, upserted as running sums/extremes at walk time (the
-- device_rollups fold pattern — no fold job for this tier). crit_max is the
-- honest hourly answer to "was there a spike"; mean-of-sweep-medians =
-- rx_med_sum / rx_med_n, labeled as such wherever it renders.
CREATE TABLE IF NOT EXISTS hist_olt_hour (
    org_id       TEXT NOT NULL,
    device_id    INTEGER NOT NULL REFERENCES org_devices(id),
    bucket       INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    onus_max     INTEGER NOT NULL,
    online_min   INTEGER NOT NULL,
    warn_max     INTEGER NOT NULL,
    crit_max     INTEGER NOT NULL,
    measured_min INTEGER NOT NULL,
    rx_med_sum   REAL NOT NULL,
    rx_med_n     INTEGER NOT NULL,
    rx_min       REAL,
    PRIMARY KEY (device_id, bucket)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hist_olt_day (
    org_id       TEXT NOT NULL,
    device_id    INTEGER NOT NULL REFERENCES org_devices(id),
    day          INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    onus_max     INTEGER NOT NULL,
    online_min   INTEGER NOT NULL,
    warn_max     INTEGER NOT NULL,
    crit_max     INTEGER NOT NULL,
    measured_min INTEGER NOT NULL,
    rx_med_sum   REAL NOT NULL,
    rx_med_n     INTEGER NOT NULL,
    rx_min       REAL,
    PRIMARY KEY (device_id, day)
) WITHOUT ROWID;
-- Per-PON grain — where a splice lives. pon_port is the roster's own label (a
-- key, not a sample), so charts join current_roster/fibre_pon untranslated.
-- ONUs whose walk carries no pon_port are counted in the OLT tables and
-- skipped here (a PON that can't be named can't be charted). Hourly tier only
-- at this cardinality (163 PONs today); week-over-week reads the day tier.
CREATE TABLE IF NOT EXISTS hist_pon_hour (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    pon_port   TEXT NOT NULL,
    bucket     INTEGER NOT NULL,
    samples    INTEGER NOT NULL,
    onus_max   INTEGER NOT NULL,
    online_min INTEGER NOT NULL,
    crit_max   INTEGER NOT NULL,
    rx_med_sum REAL NOT NULL,
    rx_med_n   INTEGER NOT NULL,
    rx_min     REAL,
    PRIMARY KEY (device_id, pon_port, bucket)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hist_pon_day (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    pon_port   TEXT NOT NULL,
    day        INTEGER NOT NULL,
    samples    INTEGER NOT NULL,
    onus_max   INTEGER NOT NULL,
    online_min INTEGER NOT NULL,
    crit_max   INTEGER NOT NULL,
    rx_med_sum REAL NOT NULL,
    rx_med_n   INTEGER NOT NULL,
    rx_min     REAL,
    PRIMARY KEY (device_id, pon_port, day)
) WITHOUT ROWID;
-- ELIGIBLE ports only (monitored, or carrying a declared link, or carrying a
-- bandwidth threshold — 177 of 6,400 walked ports today): sampling every
-- walked access port would be ~14M rows/90d for readings nobody asked about.
-- Rates are the POST-throughput_bps values, so a counter reset/reboot arrives
-- as NULL in a row that still exists — "we walked the port, no rate
-- computable" — never a negative spike and never a zero.
CREATE TABLE IF NOT EXISTS hist_port_sweep (
    org_id    TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES org_devices(id),
    if_index  INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    in_bps    REAL,
    out_bps   REAL,
    oper_up   INTEGER NOT NULL,
    PRIMARY KEY (device_id, if_index, ts)
) WITHOUT ROWID;
-- rate_n counts sweeps where BOTH rates were computable, and it is the mean's
-- denominator (in_sum/rate_n) — `samples` alone would let a reboot hour
-- understate the average.
CREATE TABLE IF NOT EXISTS hist_port_hour (
    org_id     TEXT NOT NULL,
    device_id  INTEGER NOT NULL REFERENCES org_devices(id),
    if_index   INTEGER NOT NULL,
    bucket     INTEGER NOT NULL,
    samples    INTEGER NOT NULL,
    rate_n     INTEGER NOT NULL,
    in_sum     REAL NOT NULL,
    in_max     REAL,
    out_sum    REAL NOT NULL,
    out_max    REAL,
    up_samples INTEGER NOT NULL,
    PRIMARY KEY (device_id, if_index, bucket)
) WITHOUT ROWID;
-- Folded NIGHTLY from hist_port_hour (not upserted at walk time) because the
-- busy_* columns need the day's hourly means complete: busy_in_bps is the max
-- HOURLY MEAN and busy_in_hour which UTC hour it fell in — the evening-peak
-- question at day grain for a year, two columns instead of twenty-four.
CREATE TABLE IF NOT EXISTS hist_port_day (
    org_id       TEXT NOT NULL,
    device_id    INTEGER NOT NULL REFERENCES org_devices(id),
    if_index     INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    rate_n       INTEGER NOT NULL,
    in_sum       REAL NOT NULL,
    in_max       REAL,
    out_sum      REAL NOT NULL,
    out_max      REAL,
    up_samples   INTEGER NOT NULL,
    busy_in_bps  REAL,
    busy_in_hour INTEGER,
    busy_out_bps REAL,
    busy_out_hour INTEGER,
    PRIMARY KEY (device_id, if_index, day)
) WITHOUT ROWID;
-- Folded nightly from device_rollups BEFORE its 30-day prune discards the
-- hours — the month-scale latency/loss/downtime record. Sums only (rollups
-- carry no percentiles); DEGRADED is deliberately not counted (rollups don't
-- carry it; perf episodes live in alert_log's PERF_* rows if ever charted).
CREATE TABLE IF NOT EXISTS hist_device_day (
    org_id       TEXT NOT NULL,
    device_id    INTEGER NOT NULL REFERENCES org_devices(id),
    day          INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    down_samples INTEGER NOT NULL,
    latency_sum  REAL NOT NULL,
    latency_n    INTEGER NOT NULL,
    loss_sum     REAL NOT NULL,
    PRIMARY KEY (device_id, day)
) WITHOUT ROWID;
-- The billing runway, one row per org per UTC day, written only when EVERY
-- enabled panel's latest read was fully 'ok' (a partial read may be missing
-- the status/expiry columns themselves — counting it would trend garbage; the
-- gap is the record). Counts reuse the customers page's own derivation
-- (current-book rows, parse_expiry under the profile's date_format, days_left
-- against today in WISP_DISPLAY_TZ) so the chart and the page cannot disagree.
CREATE TABLE IF NOT EXISTS hist_radius_day (
    org_id    TEXT NOT NULL,
    day       INTEGER NOT NULL,
    customers INTEGER NOT NULL,
    active    INTEGER NOT NULL,
    expired   INTEGER NOT NULL,
    expiring7 INTEGER NOT NULL,
    linked    INTEGER NOT NULL,
    PRIMARY KEY (org_id, day)
) WITHOUT ROWID;
"""


class CentralStore(
    OrgStoreMixin,
    UserStoreMixin,
    FleetStoreMixin,
    DeviceStoreMixin,
    OutageStoreMixin,
    SnmpStoreMixin,
    ProxyStoreMixin,
    RadiusStoreMixin,
    AssignmentStoreMixin,
    FieldStoreMixin,
    HistoryStoreMixin,
):

    _TENANT_TABLES = (
        "orgs", "nodes", "node_tokens", "devices", "events", "rollups", "node_alerts",
        "users", "org_devices", "device_states",
        "outages", "device_rollups", "alert_log", "alert_digest", "escalations", "rollouts",
        "switch_ports", "org_device_links", "device_redundancy", "device_perf_samples",
        "device_perf",
    )


    _CENTRAL_NODE = "central"

    def __init__(self, db_path: Path | str, *, migrate: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        if not migrate:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"{self.db_path} does not exist — a migrate=False open never creates "
                    f"or alters the schema; start central once to build it")
            return
        with self._connect() as conn:
            self._migrate_tenant_to_org(conn)
            self._radius_panels_are_many(conn)
            conn.executescript(_SCHEMA)
            self._rebuild_fibre_plant(conn)
            conn.executescript(_FIBRE_SCHEMA)
            conn.executescript(_HIST_SCHEMA)
            self._ensure_columns(conn, "org_fibre_joints", (
                ("port_kind", "TEXT"), ("port_no", "INTEGER"),
                ("port_ref", "TEXT")))
            self._port_refs_are_text(conn)
            self._unname_plumbing(conn)
            self._couplers_are_closures(conn)
            self._ensure_columns(conn, "orgs", (
                ("ntfy_topic_owner", "TEXT"), ("ntfy_topic_worker", "TEXT"),
                ("map_region", "TEXT"),
                ("poll_interval_s", "INTEGER"),
                ("plan", "TEXT NOT NULL DEFAULT 'free'"),
                ("web_proxy", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_update", "INTEGER NOT NULL DEFAULT 0")))
            self._ensure_columns(conn, "nodes", (
                ("restart_pending", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "users", (
                ("whatsapp_number", "TEXT"),
                ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
                ("totp_secret", "TEXT"),
                ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("totp_last_step", "INTEGER"),
                ("totp_recovery", "TEXT"),))
            self._ensure_columns(conn, "snmp_walks", (
                ("truncated", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "outages", (
                ("assigned_to", "TEXT"), ("assigned_at", "TEXT"),
                ("assigned_by", "TEXT"), ("accepted_by", "TEXT"),
                ("accepted_at", "TEXT")))
            self._ensure_columns(conn, "onu_dup_mac_state", (
                ("online_members", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "radius_customers", (
                ("seen_seq", "INTEGER NOT NULL DEFAULT 0"),))
            self._ensure_columns(conn, "alert_log", (("kind", "TEXT"),))
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_log_cooldown"
                " ON alert_log(org_id, device_id, kind, status)")
            self._ensure_columns(conn, "switch_ports", (
                ("bw_threshold_mbps", "REAL"), ("bw_direction", "TEXT"),
                ("in_octets", "TEXT"), ("out_octets", "TEXT"), ("counters_at", "TEXT"),
                ("in_bps", "REAL"), ("out_bps", "REAL"),
                ("bw_low_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_alarm", "INTEGER NOT NULL DEFAULT 0"), ("bw_alarm_since", "TEXT"),
                ("bw_max_mbps", "REAL"),
                ("bw_high_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_high_alarm", "INTEGER NOT NULL DEFAULT 0"),
                ("bw_high_alarm_since", "TEXT"),
                ("uplink_device_id", "INTEGER")))
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_switch_ports_uplink"
                " ON switch_ports(org_id, uplink_device_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_switch_ports_updated"
                " ON switch_ports(org_id, updated_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_onu_optics_updated"
                " ON onu_optics(org_id, updated_at)")
            self._ensure_columns(conn, "org_devices", (
                ("assigned_node_id", "TEXT"),
                ("optical_warn_dbm", "REAL"), ("optical_crit_dbm", "REAL"),
                ("gpon_vendor", "TEXT"),
                ("nvr_vendor", "TEXT"),
                ("lat", "REAL"), ("lng", "REAL"),
                ("pon_port", "TEXT"),
                ("split_ratio", "INTEGER"),
                ("split_inputs", "INTEGER"),
                ("onu_pon_limit", "INTEGER"),
                ("web_ip", "TEXT"),
                ("web_port", "INTEGER"),
                ("web_scheme", "TEXT"),
                ("tags", "TEXT"),
                ("tree_detached", "INTEGER NOT NULL DEFAULT 0"),
                ("accuracy_m", "REAL"),
                ("place_source", "TEXT"),
                ("placed_by", "TEXT"),
                ("placed_at", "TEXT")))
            self._ensure_columns(conn, "onu_optics", (
                ("last_online_at", "TEXT"),))
            self._ensure_columns(conn, "device_webui_credentials", (
                ("auth_mode", "TEXT NOT NULL DEFAULT 'form'"),))
            self._ensure_columns(conn, "nvr_channels", (
                ("monitored", "INTEGER NOT NULL DEFAULT 1"),))
            self._ensure_columns(conn, "link_routes", (
                ("color", "TEXT"), ("label_pos", "REAL")))
            self._ensure_columns(conn, "onu_drops", (
                ("waypoints", "TEXT NOT NULL DEFAULT '[]'"),
                ("leg_no", "INTEGER")))
            self._ensure_columns(conn, "onu_places", (
                ("witness", "INTEGER NOT NULL DEFAULT 1"),
                ("accuracy_m", "REAL"),
                ("place_source", "TEXT"),
                ("placed_by", "TEXT"),
                ("placed_at", "TEXT"),
                ("phone", "TEXT")))
            self._relax_onu_place_coords(conn)
            self._seed_google_key(conn)
            self._collapse_roles(conn)
            self._upper_onu_labels(conn)
            self._stamp_history_since(conn)
            conn.commit()


    @staticmethod
    def _relax_onu_place_coords(conn) -> None:


        info = list(conn.execute("PRAGMA table_info(onu_places)"))
        if not any(r["name"] == "lat" and r["notnull"] for r in info):
            return
        cols = [r["name"] for r in info if r["name"] != "id"]
        names = ", ".join(cols)
        conn.execute("""
            CREATE TABLE onu_places_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id       TEXT NOT NULL,
                mac          TEXT NOT NULL,
                lat          REAL,
                lng          REAL,
                label        TEXT,
                notes        TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                witness      INTEGER NOT NULL DEFAULT 1,
                accuracy_m   REAL,
                place_source TEXT,
                placed_by    TEXT,
                placed_at    TEXT,
                phone        TEXT,
                UNIQUE(org_id, mac)
            )""")
        conn.execute(f"INSERT INTO onu_places_new ({names})"
                     f" SELECT {names} FROM onu_places")
        conn.execute("DROP TABLE onu_places")
        conn.execute("ALTER TABLE onu_places_new RENAME TO onu_places")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onu_places_org"
                     " ON onu_places(org_id)")


    @staticmethod
    def _upper_onu_labels(conn) -> None:


        conn.execute("UPDATE onu_places SET label = UPPER(label)"
                     " WHERE label IS NOT NULL AND label <> UPPER(label)")

    @staticmethod
    def _collapse_roles(conn) -> None:


        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orgs)")}
        if "ntfy_topic_operator" in cols:
            conn.execute(
                "UPDATE orgs SET ntfy_topic_worker=ntfy_topic_operator"
                " WHERE ntfy_topic_worker IS NULL AND ntfy_topic_operator IS NOT NULL")
        conn.execute(
            "UPDATE users SET role='worker'"
            " WHERE org_id IS NOT NULL AND role IN ('operator','tech')")
        conn.execute(
            "UPDATE users SET role='owner' WHERE org_id IS NULL AND role!='owner'")


    @staticmethod
    def _couplers_are_closures(conn) -> None:


        try:
            conn.execute("UPDATE org_devices SET device_type='closure'"
                         " WHERE device_type='coupler'")
        except sqlite3.OperationalError:
            pass


    @staticmethod
    def _radius_panels_are_many(conn) -> None:

        # An org used to have exactly one billing panel, so `radius_accounts` was
        # keyed on org_id and customers/links/status all hung off the org alone.
        # Carrying that across is a REBUILD rather than an ALTER because the
        # primary keys themselves move. Every existing row belongs to the org's
        # one account, so the mapping is unambiguous and nothing is guessed.
        # Runs BEFORE `_SCHEMA`: its `CREATE TABLE IF NOT EXISTS` would leave the
        # old shape in place, and the new index names a column it has not got.
        def columns(table: str) -> list[str]:
            try:
                return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            except sqlite3.OperationalError:
                return []

        def key_of(table: str) -> set[str]:
            try:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                        if r[5]}
            except sqlite3.OperationalError:
                return set()

        cols = columns("radius_accounts")
        if cols and "id" not in cols:
            conn.execute(
                "CREATE TABLE radius_accounts_new ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,"
                " label TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL,"
                " base_url TEXT NOT NULL, username TEXT, password_enc TEXT,"
                " enabled INTEGER NOT NULL DEFAULT 1, updated_by TEXT,"
                " updated_at TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO radius_accounts_new (org_id, label, profile, base_url,"
                " username, password_enc, enabled, updated_by, updated_at)"
                " SELECT org_id, '', profile, base_url, username, password_enc,"
                " enabled, updated_by, updated_at FROM radius_accounts"
                " ORDER BY org_id")
            conn.execute("DROP TABLE radius_accounts")
            conn.execute("ALTER TABLE radius_accounts_new RENAME TO radius_accounts")

        if not columns("radius_accounts"):
            return
        owner = {r["org_id"]: r["id"] for r in conn.execute(
            "SELECT id, org_id FROM radius_accounts GROUP BY org_id")}

        for table in ("radius_customers", "radius_links", "radius_status"):
            have = columns(table)
            if not have or "account_id" in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN"
                         " account_id INTEGER NOT NULL DEFAULT 0")
            for org_id, account_id in owner.items():
                conn.execute(f"UPDATE {table} SET account_id=? WHERE org_id=?",
                             (account_id, org_id))

        # radius_status was keyed on org_id; its PK has to become the account.
        # Rebuilt rather than altered for the same reason as the accounts table.
        status_cols = columns("radius_status")
        if status_cols and "account_id" not in key_of("radius_status"):
            conn.execute(
                "CREATE TABLE radius_status_new (account_id INTEGER PRIMARY KEY,"
                " org_id TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '',"
                " state TEXT NOT NULL, detail TEXT,"
                " customers INTEGER NOT NULL DEFAULT 0,"
                " linked INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,"
                " last_ok_at TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO radius_status_new (account_id, org_id,"
                " profile, state, detail, customers, linked, updated_at, last_ok_at)"
                " SELECT account_id, org_id, profile, state, detail, customers,"
                " linked, updated_at, last_ok_at FROM radius_status"
                " WHERE account_id != 0")
            conn.execute("DROP TABLE radius_status")
            conn.execute("ALTER TABLE radius_status_new RENAME TO radius_status")

        # The old customers PK was (org_id, username); it is now scoped by the
        # account too. SQLite cannot alter a PK, so this is the same rebuild.
        cust_cols = columns("radius_customers")
        if cust_cols and "account_id" not in key_of("radius_customers"):
            names = [c for c in cust_cols if c != "account_id"]
            cols_sql = ", ".join(names)
            conn.execute(
                "CREATE TABLE radius_customers_new ("
                " org_id TEXT NOT NULL, account_id INTEGER NOT NULL DEFAULT 0,"
                " username TEXT NOT NULL, name TEXT, mac TEXT, mobile TEXT,"
                " alt_mobile TEXT, acno TEXT,"
                " status TEXT NOT NULL DEFAULT 'unknown', expiry TEXT,"
                " package TEXT, branch TEXT, area TEXT, address TEXT,"
                " balance TEXT, first_seen_at TEXT NOT NULL,"
                " last_seen_at TEXT NOT NULL,"
                " PRIMARY KEY (org_id, account_id, username))")
            conn.execute(
                f"INSERT OR REPLACE INTO radius_customers_new (account_id, {cols_sql})"
                f" SELECT account_id, {cols_sql} FROM radius_customers")
            conn.execute("DROP TABLE radius_customers")
            conn.execute(
                "ALTER TABLE radius_customers_new RENAME TO radius_customers")


    @staticmethod
    def _port_refs_are_text(conn) -> None:

        # A port's identity is the box's own STRING (`fiber.port_ref`). The old
        # `port_no` derived a number from an interface name, which collapsed
        # `GigaEthernet0/5` with `TGigaEthernet0/5` — two sockets, one of them the
        # SFP+ the trunk lands on. Carry any number already recorded across as its own
        # text; for a numbered kind that is the same fact spelled the same way, and for
        # `port` it is the best we can say about a row written under the old rule.
        # `port_no` itself is LEFT IN PLACE unread, the convention this schema keeps
        # for the ntfy topics — dropping a column is irreversible and nothing reads it.
        try:
            conn.execute(
                "UPDATE org_fibre_joints SET port_ref = CAST(port_no AS TEXT)"
                " WHERE port_ref IS NULL AND port_no IS NOT NULL")
        except sqlite3.OperationalError:
            pass


    @staticmethod
    def _unname_plumbing(conn) -> None:


        try:
            rows = conn.execute(
                "SELECT c.id, c.name, c.a_device_id, c.b_device_id,"
                "       j.port_kind, j.port_ref"
                "  FROM org_cables c"
                "  LEFT JOIN org_fibre_joints j"
                "         ON j.a_cable_id=c.id AND j.b_cable_id IS NULL"
                "        AND j.device_id=c.a_device_id"
                " WHERE c.cores=1 AND c.path IS NULL AND c.name<>''").fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return
        names = {r["id"]: r["name"] for r in conn.execute(
            "SELECT id, name FROM org_devices")}
        clear: list[int] = []
        for r in rows:
            here = names.get(r["a_device_id"]) or "box"
            there = names.get(r["b_device_id"]) or "box"
            # `port_label`, NOT `port_display` — this rebuilds the string the deleted
            # `_connect_name` ACTUALLY WROTE, and it wrote the canonical form. Naming a
            # PON the box's way here would stop matching the very rows it must clear.
            label = fiber.port_label(r["port_kind"], r["port_ref"])
            built = {f"{here} → {there}"}
            if label:
                built.add(f"{here} {label} → {there}")
            m = re.fullmatch(r"(?:(.+) )?core (\d+) → (.+)", r["name"])
            if m and m.group(3) == there:
                built.add(r["name"])
            if r["name"] in built:
                clear.append(r["id"])
        if clear:
            conn.executemany("UPDATE org_cables SET name='' WHERE id=?",
                             [(i,) for i in clear])


    @staticmethod
    def _rebuild_fibre_plant(conn) -> None:


        done = conn.execute(
            "SELECT 1 FROM app_settings WHERE key='fibre_plant_rebuilt'").fetchone()
        if done:
            return
        conn.execute("INSERT INTO app_settings (key, value)"
                     " VALUES ('fibre_plant_rebuilt', ?)", (_now_iso(),))
        have = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("org_splices", "org_cable_runs", "org_cable_taps",
                      "org_cable_cores", "org_fibre_joints"):
            if table in have:
                conn.execute(f"DELETE FROM {table}")
        for table in ("org_splices", "org_cable_runs", "org_cable_taps"):
            if table in have:
                conn.execute(f"DROP TABLE {table}")
        if "link_routes" in have:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(link_routes)")}
            if "cable_id" in cols:
                conn.execute("UPDATE link_routes SET cable_id=NULL")
            conn.execute("UPDATE link_routes SET waypoints='[]'")
        if "org_cables" in have:
            conn.execute("DROP TABLE org_cables")
        conn.executescript(_FIBRE_SCHEMA)


    @staticmethod
    def _seed_google_key(conn) -> None:
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
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-16000;")
        self._local.conn = conn
        return conn


    def _scope(self, org_id, prefix="") -> tuple[str, tuple]:
        if not org_id:
            return "", ()
        return f" AND {prefix}org_id = ?", (org_id,)


    def data_version(self, org_id: str | None = None) -> str:
        escope, eargs = self._scope(org_id, prefix="e.")
        oscope, oargs = self._scope(org_id, prefix="o.")
        sscope, sargs = self._scope(org_id, prefix="sp.")
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
            g = conn.execute(
                "SELECT COALESCE(MAX(g.updated_at),'') FROM onu_optics g"
                " WHERE 1=1" + gscope, gargs).fetchone()[0]
            wscope, wargs = self._scope(org_id, prefix="w.")
            w = conn.execute(
                "SELECT COALESCE(MAX(w.id),0) || ':' || COALESCE(MAX(w.completed_at),'')"
                " FROM snmp_walks w WHERE 1=1" + wscope, wargs).fetchone()[0]
        return f"{e}.{o}.{s}.{g}.{w}"
