from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wisp.version import version_tuple

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
    gpon_vendor      TEXT,                 -- OLT only: which GponProfile the edge walks
                                            -- (ingress/gpon.py); NULL = fall back to the
                                            -- edge's WISP_GPON_VENDOR env, then huawei
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
-- CLAUDE.md item 2, second slice: hourly latency/packet-loss trend (30-day retention,
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
CREATE INDEX IF NOT EXISTS idx_events_node ON events(org_id, node_id, id);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(org_id, node_id, device_id, id);
CREATE INDEX IF NOT EXISTS idx_node_alerts ON node_alerts(org_id, node_id, id);
-- Phase C follow-up — SNMP port status, central-side (CLAUDE.md item 1). One row per
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
    -- CLAUDE.md item 3: per-port throughput (bandwidth), orthogonal to oper/admin status.
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
-- CLAUDE.md item 3: graph topology backup edges, central-side. Mirrors the old single-box
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
-- CLAUDE.md item 3: per-link performance baseline, central-side (core/baseline.py's pure
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
"""

# Diagnostic walk results kept per device (newest first) — older ones are pruned at
# create time so a chatty operator can't grow the DB unbounded.
SNMP_WALKS_KEEP = 10

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _recent_days(today: str, n: int) -> list[str]:
    from datetime import timedelta
    base = datetime.strptime(today, "%Y-%m-%d")
    return [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, n) - 1, -1, -1)]

class CentralStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            self._migrate_tenant_to_org(conn)
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn, "orgs", (
                ("ntfy_topic_owner", "TEXT"), ("ntfy_topic_operator", "TEXT"),
                ("ntfy_topic_tech", "TEXT")))
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
                ("gpon_vendor", "TEXT")))
            conn.commit()

    @staticmethod
    def _ensure_columns(conn, table: str, coldefs: tuple[tuple[str, str], ...]) -> None:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, sqltype in coldefs:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")

    _TENANT_TABLES = (
        "orgs", "nodes", "node_tokens", "devices", "events", "rollups", "node_alerts",
        "users", "org_workers", "org_attendance", "org_devices", "device_states",
        "outages", "device_rollups", "alert_log", "escalations", "rollouts",
        "switch_ports", "org_device_links", "device_redundancy", "device_perf_samples",
        "device_perf",
    )

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

    def touch_node(self, org_id: str, node_id: str, now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            self._touch_node(conn, org_id, node_id, now)
            conn.commit()

    def record_heartbeat(self, org_id: str, node_id: str, body: dict,
                         now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            conn.execute(
                """
                INSERT INTO nodes (org_id, node_id, version, last_poll_ts, fleet_size,
                                   open_outages, health, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(org_id, node_id) DO UPDATE SET
                    version=excluded.version, last_poll_ts=excluded.last_poll_ts,
                    fleet_size=excluded.fleet_size, open_outages=excluded.open_outages,
                    health=excluded.health, last_seen=excluded.last_seen
                """,
                (org_id, node_id, body.get("version"), body.get("last_poll_ts"),
                 body.get("fleet_size"), body.get("open_outages"),
                 json.dumps(body, separators=(",", ":")), now, now),
            )
            conn.commit()

    @staticmethod
    def _ensure_org(conn, org_id, now) -> None:
        conn.execute("INSERT OR IGNORE INTO orgs (org_id, created_at) VALUES (?,?)",
                     (org_id, now))

    @staticmethod
    def _touch_node(conn, org_id, node_id, now) -> None:
        conn.execute(
            "INSERT INTO nodes (org_id, node_id, first_seen, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(org_id, node_id)"
            " DO UPDATE SET last_seen=excluded.last_seen",
            (org_id, node_id, now, now),
        )

    def set_org(self, org_id: str, name: str | None = None,
                ntfy_topic: str | None = None, ntfy_topic_owner: str | None = None,
                ntfy_topic_operator: str | None = None, ntfy_topic_tech: str | None = None
                ) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            conn.execute(
                "UPDATE orgs SET name=COALESCE(?, name), ntfy_topic=COALESCE(?, ntfy_topic),"
                " ntfy_topic_owner=COALESCE(?, ntfy_topic_owner),"
                " ntfy_topic_operator=COALESCE(?, ntfy_topic_operator),"
                " ntfy_topic_tech=COALESCE(?, ntfy_topic_tech)"
                " WHERE org_id=?",
                (name, ntfy_topic, ntfy_topic_owner, ntfy_topic_operator, ntfy_topic_tech,
                 org_id))
            conn.commit()

    def org_topic(self, org_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT ntfy_topic FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row["ntfy_topic"] if row else None

    def org_name(self, org_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT name FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row["name"] if row else None

    def org_role_topic(self, org_id: str, role: str) -> str | None:
        col = {"owner": "ntfy_topic_owner", "operator": "ntfy_topic_operator",
               "tech": "ntfy_topic_tech"}.get(role)
        if not col:
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT {col} FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row[col] if row else None

    def orgs(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT o.org_id, o.name, o.ntfy_topic, o.ntfy_topic_owner,"
                " o.ntfy_topic_operator, o.ntfy_topic_tech,"
                " (SELECT COUNT(*) FROM nodes n WHERE n.org_id=o.org_id) AS node_count"
                " FROM orgs o ORDER BY o.org_id")]

    def showcase_stats(self, limit: int = 40) -> dict:
        """Public social-proof numbers for the marketing landing ticker.

        `count` is orgs with at least one probe node (real deployments, not
        empty/test orgs); `names` are the named subset (a customer opts out of
        the scroll simply by leaving its display name blank), oldest first,
        capped at `limit` so a huge fleet doesn't bloat the injected payload.
        """
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM orgs o"
                " WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.org_id=o.org_id)"
            ).fetchone()[0]
            names = [r[0] for r in conn.execute(
                "SELECT o.name FROM orgs o"
                " WHERE o.name IS NOT NULL AND TRIM(o.name) <> ''"
                "   AND EXISTS (SELECT 1 FROM nodes n WHERE n.org_id=o.org_id)"
                " ORDER BY o.created_at ASC LIMIT ?", (limit,))]
        return {"count": count, "names": names}

    def org_exists(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        return row is not None

    def _scope(self, org_id, prefix="") -> tuple[str, tuple]:
        if not org_id:
            return "", ()
        return f" AND {prefix}org_id = ?", (org_id,)

    def _bandwidth_alarms(self, org_id: str, *, flag_col: str, limit_col: str,
                          limit_key: str, since_col: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT sp.id AS port_id, sp.device_id, d.name AS switch_name,"
                f" sp.if_index, sp.if_name, sp.if_alias, sp.in_bps, sp.out_bps,"
                f" sp.{limit_col}, sp.bw_direction, sp.{since_col}"
                f" FROM switch_ports sp JOIN org_devices d ON d.id = sp.device_id"
                f" WHERE sp.org_id=? AND sp.monitored=1 AND sp.{flag_col}=1"
                f" AND d.is_active=1 ORDER BY sp.{since_col}", (org_id,)).fetchall()
        out = []
        for r in rows:
            base = r["if_name"] or f"if{r['if_index']}"
            label = f"{base} ({r['if_alias']})" if r["if_alias"] else base
            out.append({
                "port_id": r["port_id"], "device_id": r["device_id"],
                "switch_name": r["switch_name"], "label": label,
                "in_mbps": round(r["in_bps"] / 1e6, 2) if r["in_bps"] is not None else None,
                "out_mbps": round(r["out_bps"] / 1e6, 2) if r["out_bps"] is not None else None,
                limit_key: r[limit_col],
                "direction": r["bw_direction"] or "either",
                "since": r[since_col],
            })
        return out

    def low_bandwidth_alarms(self, org_id: str) -> list[dict]:
        return self._bandwidth_alarms(org_id, flag_col="bw_alarm",
                                      limit_col="bw_threshold_mbps",
                                      limit_key="threshold_mbps",
                                      since_col="bw_alarm_since")

    def high_bandwidth_alarms(self, org_id: str) -> list[dict]:
        return self._bandwidth_alarms(org_id, flag_col="bw_high_alarm",
                                      limit_col="bw_max_mbps", limit_key="max_mbps",
                                      since_col="bw_high_alarm_since")

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

    @staticmethod
    def _hash_node_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get_node_token_status(self, org_id: str, node_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at, revoked_at FROM node_tokens"
                " WHERE org_id=? AND node_id=?", (org_id, node_id)).fetchone()
        return dict(row) if row else None

    def issue_node_token(self, org_id: str, node_id: str, *,
                         created_by: int | None = None) -> str:
        token = secrets.token_urlsafe(32)
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO node_tokens (org_id, node_id, token_hash, created_at, created_by)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id, node_id) DO UPDATE SET"
                " token_hash=excluded.token_hash, created_at=excluded.created_at,"
                " created_by=excluded.created_by, revoked_at=NULL",
                (org_id, node_id, self._hash_node_token(token), now, created_by))
            conn.commit()
        return token

    def revoke_node_token(self, org_id: str, node_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE node_tokens SET revoked_at=? WHERE org_id=? AND node_id=?"
                " AND revoked_at IS NULL", (_now_iso(), org_id, node_id))
            conn.commit()
        return cur.rowcount > 0

    def delete_node_token(self, org_id: str, node_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE org_devices SET assigned_node_id=NULL"
                " WHERE org_id=? AND assigned_node_id=?", (org_id, node_id))
            tok = conn.execute(
                "DELETE FROM node_tokens WHERE org_id=? AND node_id=?",
                (org_id, node_id))
            hb = conn.execute("DELETE FROM nodes WHERE org_id=? AND node_id=?",
                              (org_id, node_id))
            conn.commit()
        return tok.rowcount > 0 or hb.rowcount > 0

    def list_node_tokens(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT nt.node_id, nt.created_at, nt.revoked_at, 1 AS registered,"
                " n.version, n.last_seen, n.fleet_size, n.open_outages, n.health"
                " FROM node_tokens nt"
                " LEFT JOIN nodes n ON n.org_id=nt.org_id AND n.node_id=nt.node_id"
                " WHERE nt.org_id=?"
                " UNION ALL"
                " SELECT n.node_id, NULL AS created_at, NULL AS revoked_at, 0 AS registered,"
                " n.version, n.last_seen, n.fleet_size, n.open_outages, n.health"
                " FROM nodes n"
                " WHERE n.org_id=? AND NOT EXISTS ("
                "   SELECT 1 FROM node_tokens nt"
                "   WHERE nt.org_id=n.org_id AND nt.node_id=n.node_id)"
                " ORDER BY registered DESC, node_id", (org_id, org_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["registered"] = bool(d["registered"])
            raw = d.pop("health", None)
            try:
                hb = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                hb = {}
            for key in ("rss_bytes", "mem_total_bytes", "mem_available_bytes"):
                d[key] = hb.get(key)
            out.append(d)
        return out

    def resolve_node_token(self, presented_token: str) -> tuple[str, str] | None:
        if not presented_token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, node_id FROM node_tokens"
                " WHERE token_hash=? AND revoked_at IS NULL",
                (self._hash_node_token(presented_token),)).fetchone()
        return (row["org_id"], row["node_id"]) if row else None

    def node_token_registered(self, org_id: str, node_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM node_tokens WHERE org_id=? AND node_id=?"
                " AND revoked_at IS NULL", (org_id, node_id)).fetchone()
        return row is not None

    def counts(self) -> dict:
        with self._connect() as conn:
            return {
                "orgs": conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "devices": conn.execute("SELECT COUNT(*) FROM org_devices"
                                        " WHERE is_active=1").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }

    def node_liveness(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT n.org_id, n.node_id, n.last_seen FROM nodes n"
                " WHERE NOT EXISTS (SELECT 1 FROM node_tokens nt"
                "                   WHERE nt.org_id=n.org_id)"
                "    OR EXISTS (SELECT 1 FROM node_tokens nt"
                "               WHERE nt.org_id=n.org_id AND nt.node_id=n.node_id"
                "                 AND nt.revoked_at IS NULL)")]

    def last_node_alarm(self, org_id: str, node_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kind FROM node_alerts WHERE org_id=? AND node_id=?"
                " AND status='sent' AND kind IN ('NODE_STALE','NODE_OK')"
                " ORDER BY id DESC LIMIT 1", (org_id, node_id)).fetchone()
        return bool(row and row["kind"] == "NODE_STALE")

    def record_node_alert(self, org_id: str, node_id: str, kind: str,
                          status: str, detail: str = "", now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO node_alerts (org_id, node_id, kind, status, detail,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (org_id, node_id, kind, status, detail, now))
            conn.commit()

    def add_user(self, org_id: str | None, username: str, pw_hash: str,
                 pw_salt: str, role: str = "operator") -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (org_id, username, pw_hash, pw_salt, role,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (org_id, username, pw_hash, pw_salt, role, _now_iso()))
            conn.commit()
            return int(cur.lastrowid)

    def get_user_by_username(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, org_id: str | None = None) -> list[dict]:
        scope, args = self._scope(org_id)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, org_id, username, role, is_active, created_at FROM users"
                " WHERE 1=1" + scope + " ORDER BY org_id IS NOT NULL, org_id, username",
                args)]

    def set_user_password(self, user_id: int, pw_hash: str, pw_salt: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?",
                         (pw_hash, pw_salt, user_id))
            conn.commit()

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET is_active=? WHERE id=?",
                         (1 if active else 0, user_id))
            conn.commit()

    def delete_user(self, user_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()

    def add_worker(self, org_id: str, name: str, role: str = "operator",
                   region: str | None = None, notes: str | None = None) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO org_workers (org_id, name, role, region, notes, created_at)"
                " VALUES (?,?,?,?,?,?)", (org_id, name, role, region, notes, _now_iso()))
            conn.commit()
            return int(cur.lastrowid)

    def list_workers(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, org_id, name, role, region, is_active, notes FROM org_workers"
                " WHERE org_id=? ORDER BY role, name", (org_id,))]

    def update_worker(self, worker_id: int, **fields) -> None:
        allowed = ("name", "role", "region", "is_active", "notes")
        sets = {k: fields[k] for k in allowed if k in fields}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._write_lock, self._connect() as conn:
            conn.execute(f"UPDATE org_workers SET {cols} WHERE id=?",
                         (*sets.values(), worker_id))
            conn.commit()

    def delete_worker(self, worker_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM org_attendance WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM org_workers WHERE id=?", (worker_id,))
            conn.commit()

    def set_attendance(self, org_id: str, worker_id: int, present: bool,
                       day: str | None = None) -> None:
        day = day or _today()
        with self._write_lock, self._connect() as conn:
            if present:
                conn.execute(
                    "INSERT OR IGNORE INTO org_attendance (org_id, worker_id, day)"
                    " VALUES (?,?,?)", (org_id, worker_id, day))
            else:
                conn.execute("DELETE FROM org_attendance WHERE worker_id=? AND day=?",
                             (worker_id, day))
            conn.commit()

    def attendance_overview(self, org_id: str, days: int = 7,
                            today: str | None = None) -> dict:
        today = today or _today()
        with self._connect() as conn:
            ops = [dict(r) for r in conn.execute(
                "SELECT id, name, role, region FROM org_workers"
                " WHERE org_id=? AND is_active=1 AND role='operator' ORDER BY name",
                (org_id,))]
            present = {(r["worker_id"], r["day"]) for r in conn.execute(
                "SELECT worker_id, day FROM org_attendance WHERE org_id=?", (org_id,))}
        day_list = _recent_days(today, days)
        for op in ops:
            op["present_today"] = (op["id"], today) in present
            op["days"] = {d: ((op["id"], d) in present) for d in day_list}
        return {"today": today, "days": day_list, "operators": ops}

    # ----- regions -----------------------------------------------------------

    def list_regions(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            declared = {r["name"] for r in conn.execute(
                "SELECT name FROM org_regions WHERE org_id=?", (org_id,))}
            dev_counts = {r["region"]: r["n"] for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND region IS NOT NULL AND region!=''"
                " GROUP BY region", (org_id,))}
            worker_counts = {r["region"]: r["n"] for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM org_workers"
                " WHERE org_id=? AND is_active=1 AND region IS NOT NULL AND region!=''"
                " GROUP BY region", (org_id,))}
        names = sorted(declared | set(dev_counts) | set(worker_counts), key=str.lower)
        return [{
            "name": n,
            "declared": n in declared,
            "device_count": dev_counts.get(n, 0),
            "worker_count": worker_counts.get(n, 0),
        } for n in names]

    def add_region(self, org_id: str, name: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, name, _now_iso()))
            conn.commit()
            return cur.rowcount > 0

    def rename_region(self, org_id: str, old: str, new: str) -> None:
        # Cascades to devices and workers so a rename can't fragment the org's
        # region set; the new name lands declared even if `old` never was.
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, old))
            conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, new, _now_iso()))
            conn.execute("UPDATE org_devices SET region=? WHERE org_id=? AND region=?",
                         (new, org_id, old))
            conn.execute("UPDATE org_workers SET region=? WHERE org_id=? AND region=?",
                         (new, org_id, old))
            conn.commit()

    def delete_region(self, org_id: str, name: str) -> dict:
        with self._write_lock, self._connect() as conn:
            in_use = conn.execute(
                "SELECT (SELECT COUNT(*) FROM org_devices"
                "        WHERE org_id=? AND region=? AND is_active=1)"
                "     + (SELECT COUNT(*) FROM org_workers"
                "        WHERE org_id=? AND region=? AND is_active=1)",
                (org_id, name, org_id, name)).fetchone()[0]
            if in_use:
                return {"ok": False,
                        "reason": f"region is used by {in_use} device(s)/member(s)"}
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, name))
            conn.commit()
            return {"ok": True}

    def list_org_devices(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.device_type, d.region,"
                " d.parent_device_id, d.assigned_node_id, d.maintenance, d.snmp_enabled,"
                " d.snmp_version, d.snmp_community, d.snmp_port, d.gpon_vendor,"
                " (SELECT COUNT(*) FROM org_devices c"
                "  WHERE c.parent_device_id = d.id AND c.is_active = 1) AS child_count,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.alarm = 1) AS ports_down,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.bw_alarm = 1) AS ports_bw_low,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.bw_high_alarm = 1) AS ports_bw_high,"
                " g.onus_total AS onus_total, g.onus_online AS onus_online,"
                " g.warn_count AS onus_warn, g.crit_count AS onus_crit,"
                " s.state AS state, s.latency_ms AS latency_ms, s.packet_loss AS packet_loss,"
                " s.jitter_ms AS jitter_ms, s.updated_at AS state_updated_at,"
                " h.cpu_pct AS health_cpu_pct, h.mem_pct AS health_mem_pct,"
                " h.mem_used_bytes AS health_mem_used_bytes,"
                " h.mem_total_bytes AS health_mem_total_bytes,"
                " h.temp_c AS health_temp_c, h.updated_at AS health_updated_at"
                " FROM org_devices d LEFT JOIN device_states s ON s.device_id = d.id"
                " LEFT JOIN olt_optics g ON g.device_id = d.id"
                " LEFT JOIN device_health h ON h.device_id = d.id"
                " WHERE d.org_id=? AND d.is_active=1 ORDER BY d.id",
                (org_id,)).fetchall()
            links = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'",
                (org_id,)).fetchall()
        backups: dict[int, list[int]] = {}
        for link in links:
            backups.setdefault(link["child_id"], []).append(link["parent_id"])
        out = [dict(r) for r in rows]
        for d in out:
            d["backup_parents"] = backups.get(d["id"], [])
        return out

    def get_org_device(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
        return dict(row) if row else None

    def device_org(self, device_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM org_devices WHERE id=?",
                               (device_id,)).fetchone()
        return row["org_id"] if row else None

    def registered_node_ids(self, org_id: str) -> set[str]:
        with self._connect() as conn:
            return {r["node_id"] for r in conn.execute(
                "SELECT node_id FROM node_tokens WHERE org_id=?", (org_id,))}

    def node_expected_ips(self, org_id: str, node_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ip_address FROM org_devices WHERE org_id=? AND is_active=1"
                " AND maintenance=0 AND assigned_node_id=?",
                (org_id, node_id)).fetchall()
        return {r["ip_address"] for r in rows}

    def org_device_parent_map(self, org_id: str) -> dict[int, int | None]:
        with self._connect() as conn:
            return {r["id"]: r["parent_device_id"] for r in conn.execute(
                "SELECT id, parent_device_id FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,))}

    def create_org_device(self, org_id: str, clean: dict) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type, region,"
                " parent_device_id, assigned_node_id, gpon_vendor, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["ip_address"], clean["device_type"],
                 clean["region"], clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), _now_iso()))
            conn.commit()
            return int(cur.lastrowid)

    def update_org_device(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET name=?, ip_address=?, device_type=?, region=?,"
                " parent_device_id=?, assigned_node_id=?, gpon_vendor=? WHERE id=? AND org_id=?"
                " AND is_active=1",
                (clean["name"], clean["ip_address"], clean["device_type"], clean["region"],
                 clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), device_id, org_id))
            if cur.rowcount > 0 and not clean.get("assigned_node_id"):
                conn.execute("DELETE FROM device_states WHERE org_id=? AND device_id=?",
                             (org_id, device_id))
                open_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM outages WHERE org_id=? AND device_id=?"
                    " AND resolved_at IS NULL", (org_id, device_id))]
                if open_ids:
                    conn.execute(
                        "UPDATE outages SET resolved_at=? WHERE org_id=? AND device_id=?"
                        " AND resolved_at IS NULL", (_now_iso(), org_id, device_id))
                    conn.executemany("DELETE FROM escalations WHERE outage_id=?",
                                     [(oid,) for oid in open_ids])
            conn.commit()
            return cur.rowcount > 0

    def set_org_device_maintenance(self, org_id: str, device_id: int, on: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET maintenance=? WHERE id=? AND org_id=? AND is_active=1",
                (1 if on else 0, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def set_org_device_snmp(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET snmp_enabled=?, snmp_version=?, snmp_community=?,"
                " snmp_port=? WHERE id=? AND org_id=? AND is_active=1",
                (clean["snmp_enabled"], clean["snmp_version"], clean["snmp_community"],
                 clean["snmp_port"], device_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_org_device(self, org_id: str, device_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
            if not row:
                return {"ok": False, "reason": "device not found"}
            children = conn.execute(
                "SELECT COUNT(*) FROM org_devices"
                " WHERE parent_device_id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()[0]
        if children:
            return {"ok": False,
                    "reason": f"node has {children} child node(s); reassign them first"}
        with self._write_lock, self._connect() as conn:
            outage_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=?",
                (org_id, device_id))]
            for oid in outage_ids:
                conn.execute("DELETE FROM alert_log WHERE outage_id=?", (oid,))
                conn.execute("DELETE FROM escalations WHERE outage_id=?", (oid,))
            conn.execute("DELETE FROM outages WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM device_states WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM device_rollups WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute(
                "UPDATE switch_ports SET feeds_device_id=NULL"
                " WHERE org_id=? AND feeds_device_id=?", (org_id, device_id))
            conn.execute("DELETE FROM switch_ports WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute(
                "DELETE FROM org_device_links"
                " WHERE org_id=? AND (child_id=? OR parent_id=?)",
                (org_id, device_id, device_id))
            conn.execute("DELETE FROM device_redundancy WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM device_perf_samples WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM device_perf WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM onu_optics WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM olt_optics WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM org_devices WHERE id=? AND org_id=?",
                         (device_id, org_id))
            conn.commit()
        return {"ok": True}

    def org_device_topology(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, ip_address, region, parent_device_id, assigned_node_id,"
                " snmp_enabled, snmp_version, snmp_community, snmp_port, device_type,"
                " gpon_vendor FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND maintenance=0 ORDER BY id",
                (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def device_states(self, org_id: str) -> dict[int, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, state, latency_ms, packet_loss, jitter_ms FROM"
                " device_states WHERE org_id=?", (org_id,)).fetchall()
        return {r["device_id"]: dict(r) for r in rows}

    def write_device_states(self, org_id: str, rows: list[tuple], ts: str) -> None:
        if not rows:
            return
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO device_states (device_id, org_id, state, latency_ms,"
                " packet_loss, jitter_ms, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,"
                " latency_ms=excluded.latency_ms, packet_loss=excluded.packet_loss,"
                " jitter_ms=excluded.jitter_ms, updated_at=excluded.updated_at",
                [(did, org_id, state, lat, loss, jit, ts)
                 for did, state, lat, loss, jit in rows])
            conn.commit()

    def uplink_active(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM alert_log WHERE org_id=? AND"
                " (payload LIKE '%UPLINK%' OR payload LIKE '%Uplink%')"
                " ORDER BY id DESC LIMIT 1", (org_id,)).fetchone()
        return bool(row and "UPLINK_DOWN" in (row["payload"] or ""))

    _CENTRAL_NODE = "central"

    def _insert_org_event(self, conn, org_id: str, device_id: int | None,
                          device_name: str | None, region: str | None, type_: str,
                          state: str | None, occurred_at: str, payload: dict) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(edge_id), 0) + 1 FROM events WHERE org_id=? AND node_id=?",
            (org_id, self._CENTRAL_NODE)).fetchone()
        conn.execute(
            "INSERT INTO events (org_id, node_id, edge_id, type, device_id, device_name,"
            " device_ip, device_region, state, occurred_at, payload, received_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, self._CENTRAL_NODE, row[0], type_, device_id, device_name, None,
             region, state, occurred_at, json.dumps(payload, separators=(",", ":")),
             _now_iso()))

    def open_outage_id(self, org_id: str, device_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["id"] if row else None

    def open_outage_if_absent(self, org_id: str, device_id: int, ts: str,
                              state: str) -> None:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at, final_state)"
                " SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM outages"
                " WHERE org_id=? AND device_id=? AND resolved_at IS NULL)",
                (org_id, device_id, ts, state, org_id, device_id))
            if cur.rowcount > 0:
                dev = conn.execute("SELECT name, region FROM org_devices WHERE id=?",
                                   (device_id,)).fetchone()
                self._insert_org_event(conn, org_id, device_id,
                    dev["name"] if dev else None, dev["region"] if dev else None,
                    "OUTAGE_OPENED", state, ts, {"started_at": ts})
            conn.commit()

    def recategorize_outage(self, org_id: str, device_id: int, state: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET final_state=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (state, org_id, device_id))
            conn.commit()

    def stamp_outage_cause(self, org_id: str, outage_id: int, cause: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET root_cause = COALESCE(root_cause, ?)"
                " WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (cause, outage_id, org_id))
            conn.commit()

    def resolve_outage(self, org_id: str, device_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (org_id, device_id)).fetchone()
            cur = conn.execute(
                "UPDATE outages SET resolved_at=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (ts, org_id, device_id))
            if cur.rowcount > 0:
                dev = conn.execute("SELECT name, region FROM org_devices WHERE id=?",
                                   (device_id,)).fetchone()
                self._insert_org_event(conn, org_id, device_id,
                    dev["name"] if dev else None, dev["region"] if dev else None,
                    "OUTAGE_RESOLVED", row["final_state"] if row else None, ts,
                    {"resolved_at": ts})
            conn.commit()

    def outages_in_window(self, org_id: str, since: str, until: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND (o.resolved_at IS NULL OR o.resolved_at >= ?)"
                " AND o.started_at <= ? ORDER BY o.started_at",
                (org_id, since, until)).fetchall()
        return [dict(r) for r in rows]

    def fold_device_rollups(self, entries: list[tuple]) -> None:
        if not entries:
            return
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO device_rollups (org_id, device_id, bucket, samples,"
                " latency_sum, latency_count, loss_sum, down_samples)"
                " VALUES (?,?,?,1,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, bucket) DO UPDATE SET"
                " samples = samples + 1,"
                " latency_sum = latency_sum + excluded.latency_sum,"
                " latency_count = latency_count + excluded.latency_count,"
                " loss_sum = loss_sum + excluded.loss_sum,"
                " down_samples = down_samples + excluded.down_samples",
                [(org_id, device_id, bucket, latency_ms or 0.0,
                  1 if latency_ms is not None else 0, loss_pct if loss_pct is not None else 0.0,
                  down)
                 for org_id, device_id, bucket, latency_ms, loss_pct, down in entries])
            conn.commit()

    def device_rollup_series(self, org_id: str, device_id: int, since: str,
                             until: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bucket, samples, latency_sum, latency_count, loss_sum,"
                " down_samples FROM device_rollups WHERE org_id=? AND device_id=?"
                " AND bucket >= ? AND bucket <= ? ORDER BY bucket",
                (org_id, device_id, since, until)).fetchall()
        out = []
        for r in rows:
            avg_latency = (r["latency_sum"] / r["latency_count"]) if r["latency_count"] else None
            avg_loss = (r["loss_sum"] / r["samples"]) if r["samples"] else None
            down_pct = (100.0 * r["down_samples"] / r["samples"]) if r["samples"] else None
            out.append({
                "bucket": r["bucket"], "samples": r["samples"],
                "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
                "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
                "down_pct": round(down_pct, 2) if down_pct is not None else None,
            })
        return out

    def prune_rollups_older_than(self, cutoff: str) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM device_rollups WHERE bucket < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    def last_resolved_state(self, org_id: str, device_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["final_state"] if row else None

    def acknowledge_outage(self, org_id: str, outage_id: int, by: str) -> bool:
        with self._write_lock, self._connect() as conn:
            now = _now_iso()
            cur = conn.execute(
                "UPDATE outages SET acknowledged_at=COALESCE(acknowledged_at, ?),"
                " acknowledged_by=? WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (now, by, outage_id, org_id))
            if cur.rowcount > 0:
                row = conn.execute(
                    "SELECT o.device_id, o.final_state, d.name, d.region FROM outages o"
                    " JOIN org_devices d ON d.id = o.device_id WHERE o.id=?",
                    (outage_id,)).fetchone()
                if row:
                    self._insert_org_event(conn, org_id, row["device_id"], row["name"],
                        row["region"], "OUTAGE_ACKNOWLEDGED", row["final_state"], now,
                        {"by": by})
            conn.commit()
            return cur.rowcount > 0

    def outage_org(self, outage_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM outages WHERE id=?",
                               (outage_id,)).fetchone()
        return row["org_id"] if row else None

    def triage_outages(self, org_id: str, postmortem_days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=postmortem_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name AS device_name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND d.assigned_node_id IS NOT NULL"
                " AND (o.resolved_at IS NULL"
                " OR (o.root_cause IS NULL AND o.resolved_at >= ?))"
                " ORDER BY o.started_at DESC", (org_id, cutoff)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["resolved_at"] is None:
                d["status"] = "in_progress" if d["acknowledged_at"] else "unassigned"
            else:
                d["status"] = "pending_postmortem"
            out.append(d)
        return out

    def set_outage_postmortem(self, org_id: str, outage_id: int, root_cause: str,
                              resolution_notes: str | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE outages SET root_cause=?, resolution_notes=?"
                " WHERE id=? AND org_id=? AND resolved_at IS NOT NULL",
                (root_cause, resolution_notes, outage_id, org_id))
            if cur.rowcount > 0:
                row = conn.execute(
                    "SELECT o.device_id, d.name, d.region FROM outages o"
                    " JOIN org_devices d ON d.id = o.device_id WHERE o.id=?",
                    (outage_id,)).fetchone()
                if row:
                    self._insert_org_event(conn, org_id, row["device_id"], row["name"],
                        row["region"], "OUTAGE_POSTMORTEM", None, _now_iso(),
                        {"root_cause": root_cause, "resolution_notes": resolution_notes})
            conn.commit()
            return cur.rowcount > 0

    def clear_pending_postmortems(self, org_id: str, root_cause: str,
                                  resolution_notes: str | None = None) -> int:
        with self._write_lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT o.id, o.device_id, d.name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND o.resolved_at IS NOT NULL AND o.root_cause IS NULL",
                (org_id,)).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE outages SET root_cause=?, resolution_notes=? WHERE id=?",
                    (root_cause, resolution_notes, r["id"]))
                self._insert_org_event(conn, org_id, r["device_id"], r["name"],
                    r["region"], "OUTAGE_POSTMORTEM", None, _now_iso(),
                    {"root_cause": root_cause, "resolution_notes": resolution_notes})
            conn.commit()
            return len(rows)

    def list_events(self, org_id: str, limit: int = 100,
                    before_id: int | None = None) -> list[dict]:
        scope, args = self._scope(org_id)
        cursor = ""
        if before_id is not None:
            cursor = " AND id < ?"
            args = (*args, before_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, org_id, node_id, type, device_id, device_name, device_ip,"
                " device_region, state, occurred_at, received_at, payload FROM events"
                " WHERE 1=1" + scope + cursor + " ORDER BY id DESC LIMIT ?",
                (*args, max(1, min(limit, 500)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            payload = d.pop("payload", None)
            try:
                d["payload"] = json.loads(payload) if payload else None
            except (TypeError, ValueError):
                d["payload"] = None
            out.append(d)
        return out

    def already_paged(self, outage_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alert_log WHERE outage_id=? AND status='sent' LIMIT 1",
                (outage_id,)).fetchone()
        return row is not None

    def log_alert(self, org_id: str, outage_id: int | None, device_id: int | None,
                  channel: str, recipient: str | None, status: str, payload: str,
                  ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO alert_log (org_id, outage_id, device_id, channel,"
                " recipient, sent_at, status, payload) VALUES (?,?,?,?,?,?,?,?)",
                (org_id, outage_id, device_id, channel, recipient, ts, status, payload))
            conn.commit()

    def schedule_escalation(self, org_id: str, outage_id: int, kind: str,
                            due_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO escalations (org_id, outage_id, kind, due_at)"
                " VALUES (?,?,?,?)", (org_id, outage_id, kind, due_at))
            conn.commit()

    def due_escalations(self, org_id: str, now_ts: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.kind, o.id AS outage_id, o.device_id, o.started_at,"
                " o.acknowledged_by, o.resolved_at FROM escalations e"
                " JOIN outages o ON o.id = e.outage_id"
                " WHERE e.org_id=? AND e.executed_at IS NULL AND e.due_at <= ?",
                (org_id, now_ts)).fetchall()
        return [dict(r) for r in rows]

    def cancel_pending_escalations(self, org_id: str, device_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE escalations SET executed_at=? WHERE org_id=?"
                " AND executed_at IS NULL AND outage_id IN (SELECT id FROM outages"
                " WHERE org_id=? AND device_id=? AND resolved_at IS NOT NULL)",
                (ts, org_id, org_id, device_id))
            conn.commit()

    def mark_escalation_executed(self, esc_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE escalations SET executed_at=? WHERE id=?", (ts, esc_id))
            conn.commit()

    def reschedule_escalation(self, esc_id: int, due_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE escalations SET due_at=? WHERE id=?", (due_at, esc_id))
            conn.commit()

    def list_switch_ports(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM switch_ports WHERE org_id=? AND device_id=?"
                " ORDER BY if_index", (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def switch_port_org(self, port_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM switch_ports WHERE id=?",
                               (port_id,)).fetchone()
        return row["org_id"] if row else None

    def upsert_switch_port(self, org_id: str, device_id: int, if_index: int,
                           if_name: str | None, if_alias: str | None, admin_status: str,
                           oper_status: str, last_change: str | None, down_streak: int,
                           alarm: bool, alarm_since: str | None, ts: str, *,
                           bw: tuple | None = None) -> None:
        in_octets = out_octets = counters_at = in_bps = out_bps = None
        bw_low_streak, bw_alarm, bw_alarm_since = 0, False, None
        bw_high_streak, bw_high_alarm, bw_high_alarm_since = 0, False, None
        if bw is not None:
            (in_octets, out_octets, counters_at, in_bps, out_bps,
             bw_low_streak, bw_alarm, bw_alarm_since,
             bw_high_streak, bw_high_alarm, bw_high_alarm_since) = bw
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO switch_ports (org_id, device_id, if_index, if_name,"
                " if_alias, admin_status, oper_status, last_change, down_streak, alarm,"
                " alarm_since, updated_at, in_octets, out_octets, counters_at, in_bps,"
                " out_bps, bw_low_streak, bw_alarm, bw_alarm_since, bw_high_streak,"
                " bw_high_alarm, bw_high_alarm_since)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, if_index) DO UPDATE SET"
                " if_name=excluded.if_name, if_alias=excluded.if_alias,"
                " admin_status=excluded.admin_status, oper_status=excluded.oper_status,"
                " last_change=excluded.last_change, down_streak=excluded.down_streak,"
                " alarm=excluded.alarm, alarm_since=excluded.alarm_since,"
                " updated_at=excluded.updated_at, in_octets=excluded.in_octets,"
                " out_octets=excluded.out_octets, counters_at=excluded.counters_at,"
                " in_bps=excluded.in_bps, out_bps=excluded.out_bps,"
                " bw_low_streak=excluded.bw_low_streak, bw_alarm=excluded.bw_alarm,"
                " bw_alarm_since=excluded.bw_alarm_since,"
                " bw_high_streak=excluded.bw_high_streak,"
                " bw_high_alarm=excluded.bw_high_alarm,"
                " bw_high_alarm_since=excluded.bw_high_alarm_since",
                (org_id, device_id, if_index, if_name, if_alias, admin_status,
                 oper_status, last_change, down_streak, 1 if alarm else 0, alarm_since, ts,
                 str(in_octets) if in_octets is not None else None,
                 str(out_octets) if out_octets is not None else None,
                 counters_at, in_bps, out_bps, bw_low_streak, 1 if bw_alarm else 0,
                 bw_alarm_since, bw_high_streak, 1 if bw_high_alarm else 0,
                 bw_high_alarm_since))
            conn.commit()

    def set_port_monitored(self, org_id: str, port_id: int, on: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET monitored=? WHERE id=? AND org_id=?",
                (1 if on else 0, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def set_port_feeds(self, org_id: str, port_id: int,
                       feeds_device_id: int | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET feeds_device_id=? WHERE id=? AND org_id=?",
                (feeds_device_id, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def set_port_bandwidth_config(self, org_id: str, port_id: int,
                                  threshold_mbps: float | None, direction: str,
                                  max_mbps: float | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET bw_threshold_mbps=?, bw_direction=?,"
                " bw_max_mbps=? WHERE id=? AND org_id=?",
                (threshold_mbps, direction, max_mbps, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def list_onu_optics(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM onu_optics WHERE org_id=? AND device_id=?"
                " ORDER BY rx_dbm IS NULL, rx_dbm ASC, onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def upsert_onu_optics(self, org_id: str, device_id: int, onu_key: str, *,
                          pon_port: str | None, onu_id: int | None, name: str | None,
                          serial: str | None, state: str | None, rx_dbm: float | None,
                          tx_dbm: float | None, olt_rx_dbm: float | None,
                          distance_m: int | None, rx_ref_dbm: float | None,
                          rx_ref_at: str | None, severity: str, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_optics (org_id, device_id, onu_key, pon_port, onu_id,"
                " name, serial, state, rx_dbm, tx_dbm, olt_rx_dbm, distance_m,"
                " rx_ref_dbm, rx_ref_at, severity, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, onu_key) DO UPDATE SET"
                " pon_port=excluded.pon_port, onu_id=excluded.onu_id, name=excluded.name,"
                " serial=excluded.serial, state=excluded.state, rx_dbm=excluded.rx_dbm,"
                " tx_dbm=excluded.tx_dbm, olt_rx_dbm=excluded.olt_rx_dbm,"
                " distance_m=excluded.distance_m, rx_ref_dbm=excluded.rx_ref_dbm,"
                " rx_ref_at=excluded.rx_ref_at, severity=excluded.severity,"
                " updated_at=excluded.updated_at",
                (org_id, device_id, onu_key, pon_port, onu_id, name, serial, state,
                 rx_dbm, tx_dbm, olt_rx_dbm, distance_m, rx_ref_dbm, rx_ref_at,
                 severity, ts))
            conn.commit()

    def get_olt_optics(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM olt_optics WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None

    def upsert_olt_optics(self, org_id: str, device_id: int, *, onus_total: int,
                          onus_online: int, warn_count: int, crit_count: int,
                          alarm: bool, alarm_since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO olt_optics (device_id, org_id, onus_total, onus_online,"
                " warn_count, crit_count, alarm, alarm_since, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET onus_total=excluded.onus_total,"
                " onus_online=excluded.onus_online, warn_count=excluded.warn_count,"
                " crit_count=excluded.crit_count, alarm=excluded.alarm,"
                " alarm_since=excluded.alarm_since, updated_at=excluded.updated_at",
                (device_id, org_id, onus_total, onus_online, warn_count, crit_count,
                 1 if alarm else 0, alarm_since, ts))
            conn.commit()

    def onu_optics_org(self, onu_row_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM onu_optics WHERE id=?",
                               (onu_row_id,)).fetchone()
        return row["org_id"] if row else None

    def set_onu_ack(self, org_id: str, onu_row_id: int, until: str | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_optics SET ack_until=? WHERE id=? AND org_id=?",
                (until, onu_row_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def set_olt_optical_thresholds(self, org_id: str, device_id: int,
                                   warn_dbm: float | None, crit_dbm: float | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET optical_warn_dbm=?, optical_crit_dbm=?"
                " WHERE id=? AND org_id=? AND is_active=1",
                (warn_dbm, crit_dbm, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def org_device_backup_map(self, org_id: str) -> dict[int, set[int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'",
                (org_id,)).fetchall()
        out: dict[int, set[int]] = {}
        for r in rows:
            out.setdefault(r["child_id"], set()).add(r["parent_id"])
        return out

    def org_device_backup_edges(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'", (org_id,))]

    def create_backup_link(self, org_id: str, child_id: int, parent_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO org_device_links (org_id, child_id, parent_id,"
                " kind) VALUES (?,?,?,'backup')", (org_id, child_id, parent_id))
            conn.commit()

    def delete_backup_link(self, org_id: str, child_id: int, parent_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM org_device_links WHERE org_id=? AND child_id=?"
                " AND parent_id=? AND kind='backup'", (org_id, child_id, parent_id))
            conn.commit()
            return cur.rowcount > 0

    def device_redundancy_state(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT on_backup, primary_down_since FROM device_redundancy"
                " WHERE org_id=? AND device_id=?", (org_id, device_id)).fetchone()
        return dict(row) if row else None

    def write_device_redundancy(self, org_id: str, device_id: int, on_backup: bool,
                                since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_redundancy (device_id, org_id, on_backup,"
                " primary_down_since, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET on_backup=excluded.on_backup,"
                " primary_down_since=excluded.primary_down_since,"
                " updated_at=excluded.updated_at",
                (device_id, org_id, 1 if on_backup else 0, since, ts))
            conn.commit()

    def upsert_device_health(self, org_id: str, device_id: int, health: dict,
                             ts: str) -> None:
        def _f(key):
            v = health.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _i(key):
            v = _f(key)
            return int(v) if v is not None else None

        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_health (device_id, org_id, cpu_pct, mem_used_bytes,"
                " mem_total_bytes, mem_pct, temp_c, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET cpu_pct=excluded.cpu_pct,"
                " mem_used_bytes=excluded.mem_used_bytes,"
                " mem_total_bytes=excluded.mem_total_bytes, mem_pct=excluded.mem_pct,"
                " temp_c=excluded.temp_c, updated_at=excluded.updated_at",
                (device_id, org_id, _f("cpu_pct"), _i("mem_used_bytes"),
                 _i("mem_total_bytes"), _f("mem_pct"), _f("temp_c"), ts))
            conn.commit()

    def create_snmp_walk(self, org_id: str, device_id: int, node_id: str,
                         root_oid: str, max_varbinds: int,
                         requested_by: str | None = None) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            # One pending walk per device — a re-request supersedes the stale one
            # instead of queueing behind it.
            conn.execute(
                "UPDATE snmp_walks SET status='error', error='superseded',"
                " completed_at=? WHERE org_id=? AND device_id=? AND status='pending'",
                (now, org_id, device_id))
            cur = conn.execute(
                "INSERT INTO snmp_walks (org_id, device_id, node_id, root_oid,"
                " max_varbinds, requested_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (org_id, device_id, node_id, root_oid, max_varbinds, requested_by, now))
            conn.execute(
                "DELETE FROM snmp_walks WHERE org_id=? AND device_id=? AND id NOT IN"
                " (SELECT id FROM snmp_walks WHERE org_id=? AND device_id=?"
                "  ORDER BY id DESC LIMIT ?)",
                (org_id, device_id, org_id, device_id, SNMP_WALKS_KEEP))
            conn.commit()
            return int(cur.lastrowid)

    def pending_snmp_walks(self, org_id: str, node_id: str) -> list[dict]:
        # Target coordinates come from org_devices at DELIVERY time (not queue time)
        # so a community/port edit between queue and pickup is honored.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.id, w.root_oid, w.max_varbinds, d.ip_address,"
                " d.snmp_community, d.snmp_port, d.snmp_version"
                " FROM snmp_walks w JOIN org_devices d"
                "  ON d.id=w.device_id AND d.org_id=w.org_id"
                " WHERE w.org_id=? AND w.node_id=? AND w.status='pending'"
                " AND d.is_active=1 AND d.snmp_enabled=1 ORDER BY w.id",
                (org_id, node_id)).fetchall()
        return [dict(r) for r in rows]

    def complete_snmp_walk(self, org_id: str, node_id: str, walk_id: int, *,
                           varbinds: list | None = None,
                           error: str | None = None) -> bool:
        status = "error" if error else "done"
        result = (json.dumps(varbinds, separators=(",", ":"))
                  if varbinds is not None and not error else None)
        count = len(varbinds) if varbinds is not None and not error else None
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE snmp_walks SET status=?, error=?, result=?, varbind_count=?,"
                " completed_at=? WHERE id=? AND org_id=? AND node_id=?"
                " AND status='pending'",
                (status, error, result, count, _now_iso(), walk_id, org_id, node_id))
            conn.commit()
            return cur.rowcount > 0

    def list_snmp_walks(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, node_id, root_oid, max_varbinds, status, requested_by,"
                " error, varbind_count, created_at, completed_at FROM snmp_walks"
                " WHERE org_id=? AND device_id=? ORDER BY id DESC",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def get_snmp_walk(self, org_id: str, walk_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM snmp_walks WHERE id=? AND org_id=?",
                (walk_id, org_id)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["result"] = json.loads(out["result"]) if out["result"] else None
        except (TypeError, ValueError):
            out["result"] = None
        return out

    def snmp_walk_org(self, walk_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM snmp_walks WHERE id=?",
                               (walk_id,)).fetchone()
        return row["org_id"] if row else None

    def list_snmp_profiles(self, org_id: str | None) -> list[dict]:
        # An org sees global profiles + its own; superadmin scope (None) sees all.
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM snmp_profiles ORDER BY org_id IS NOT NULL, name")
            else:
                rows = conn.execute(
                    "SELECT * FROM snmp_profiles WHERE org_id IS NULL OR org_id=?"
                    " ORDER BY org_id IS NOT NULL, name", (org_id,))
            out = [dict(r) for r in rows.fetchall()]
        for p in out:
            try:
                p["metrics"] = json.loads(p["metrics"])
            except (TypeError, ValueError):
                p["metrics"] = {}
            p["enabled"] = bool(p["enabled"])
        return out

    def snmp_profiles_for_edge(self, org_id: str) -> list[dict]:
        return [{"name": p["name"], "match_sysobjectid": p["match_sysobjectid"],
                 "metrics": p["metrics"]}
                for p in self.list_snmp_profiles(org_id) if p["enabled"]]

    def create_snmp_profile(self, org_id: str | None, clean: dict) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO snmp_profiles (org_id, name, match_sysobjectid, metrics,"
                " enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["metrics"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, now, now))
            conn.commit()
            return int(cur.lastrowid)

    def update_snmp_profile(self, profile_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE snmp_profiles SET name=?, match_sysobjectid=?, metrics=?,"
                " enabled=?, updated_at=? WHERE id=?",
                (clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["metrics"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, _now_iso(), profile_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_snmp_profile(self, profile_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM snmp_profiles WHERE id=?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_snmp_profile(self, profile_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM snmp_profiles WHERE id=?",
                               (profile_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["metrics"] = json.loads(out["metrics"])
        except (TypeError, ValueError):
            out["metrics"] = {}
        out["enabled"] = bool(out["enabled"])
        return out

    def record_perf_sample(self, org_id: str, device_id: int, ts: str,
                           latency_ms: float | None, packet_loss: float | None,
                           jitter_ms: float | None, state: str, keep: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_perf_samples (org_id, device_id, ts, latency_ms,"
                " packet_loss, jitter_ms, state) VALUES (?,?,?,?,?,?,?)",
                (org_id, device_id, ts, latency_ms, packet_loss, jitter_ms, state))
            conn.execute(
                "DELETE FROM device_perf_samples WHERE org_id=? AND device_id=? AND id"
                " NOT IN (SELECT id FROM device_perf_samples WHERE org_id=? AND"
                " device_id=? ORDER BY id DESC LIMIT ?)",
                (org_id, device_id, org_id, device_id, keep))
            conn.commit()

    def perf_sample_window(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, latency_ms, packet_loss, jitter_ms, state FROM"
                " device_perf_samples WHERE org_id=? AND device_id=? ORDER BY id",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def device_perf_state(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT degraded, metric, baseline_ms, current_ms, since FROM"
                " device_perf WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None

    def write_device_perf(self, org_id: str, device_id: int, degraded: bool,
                          metric: str | None, baseline_ms: float | None,
                          current_ms: float | None, since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_perf (device_id, org_id, degraded, metric,"
                " baseline_ms, current_ms, since, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET degraded=excluded.degraded,"
                " metric=excluded.metric, baseline_ms=excluded.baseline_ms,"
                " current_ms=excluded.current_ms, since=excluded.since,"
                " updated_at=excluded.updated_at",
                (device_id, org_id, 1 if degraded else 0, metric, baseline_ms,
                 current_ms, since, ts))
            conn.commit()

    def set_release(self, version: str, artifacts: dict, channel: str = "stable") -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO releases (version, channel, artifacts, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(version) DO UPDATE SET"
                " channel=excluded.channel, artifacts=excluded.artifacts",
                (version, channel, json.dumps(artifacts, separators=(",", ":")), _now_iso()))
            conn.commit()

    def get_release(self, version: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["artifacts"] = json.loads(out["artifacts"])
        return out

    def list_releases(self) -> list[dict]:
        with self._connect() as conn:
            rows = [{"version": r["version"], "channel": r["channel"],
                     "created_at": r["created_at"]}
                    for r in conn.execute(
                        "SELECT version, channel, created_at FROM releases")]
        rows.sort(key=lambda r: (version_tuple(r["version"]), r["created_at"]), reverse=True)
        return rows

    def set_rollout(self, org_id: str, target_version: str, canary: list,
                    state: str = "canary", note: str | None = None,
                    now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO rollouts (org_id, target_version, canary, state, started_at,"
                " updated_at, note) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id) DO UPDATE SET target_version=excluded.target_version,"
                " canary=excluded.canary, state=excluded.state, started_at=excluded.started_at,"
                " updated_at=excluded.updated_at, note=excluded.note",
                (org_id, target_version, json.dumps(canary), state, now, now, note))
            conn.commit()

    def update_rollout_state(self, org_id: str, state: str, now: str | None = None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE rollouts SET state=?, updated_at=? WHERE org_id=?",
                         (state, now or _now_iso(), org_id))
            conn.commit()

    def get_rollout(self, org_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rollouts WHERE org_id=?",
                               (org_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["canary"] = json.loads(out["canary"])
        return out

    def node_versions(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT node_id, version, last_seen FROM nodes WHERE org_id=?",
                (org_id,))]
