"""Central store — the multi-org aggregation store (Phase 10 Part B).

Its OWN SQLite (cfg.central_db), wholly separate from any edge DB. Many edges write here,
so every write goes through a single process-wide lock — the plan's "serialized ingest
writer" in miniature (WAL won't save you from many concurrent writers; Postgres behind the
same method surface is the documented upgrade when org count grows — *the edge stays
SQLite+stdlib forever*). Reads use WAL and don't take the lock.

Identity & id mapping (decision #6). The durable edge identity is `(org_id, node_id)`;
an edge's autoincrement `device_id` is per-SQLite and **cannot be merged across nodes**. So
central keeps its OWN global id space: the `devices` table maps each
`(org_id, node_id, edge_local_id)` to a central `id` (the global device id) and carries the
latest denormalized name/ip/region. Every org/node/device row is scoped by `org_id`, and
**every read takes an optional `org_id` filter** — central is multi-org, so nothing is
cross-org by default. Orgs are auto-provisioned on first contact (an edge showing up
registers its org); naming/per-org alert routing can be set later (Part C).

Ingest stays idempotent on the edge's outbox row id (`UNIQUE(org_id, node_id, edge_id)` +
INSERT OR IGNORE): a re-delivered batch after a lost ack stores nothing twice — at-least-once
delivery + idempotent storage = effectively-once.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,   -- the central GLOBAL device id
    org_id     TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    edge_local_id INTEGER NOT NULL,                    -- the edge's per-SQLite devices.id
    name          TEXT,
    ip            TEXT,
    region        TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (org_id, node_id, edge_local_id)
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    edge_id       INTEGER NOT NULL,        -- the edge's outbox row id (idempotency key)
    type          TEXT,
    device_id     INTEGER,                 -- edge-local id (joins devices -> global id)
    device_name   TEXT,
    device_ip     TEXT,
    device_region TEXT,
    state         TEXT,
    occurred_at   TEXT,
    payload       TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    UNIQUE (org_id, node_id, edge_id)
);
CREATE TABLE IF NOT EXISTS rollups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    edge_id     INTEGER NOT NULL,
    device_id   INTEGER,
    bucket      TEXT,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL,
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
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_devices_org ON org_devices(org_id, is_active);
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
    -- Operator-set (never touched by a walk): bw_threshold_mbps/bw_direction. Walk-
    -- refreshed: the raw octet counters (TEXT — Counter64 can exceed SQLite's signed-64
    -- INTEGER range) + the last computed rates. Flap-suppressed like the port-down path,
    -- its own streak because traffic is burstier than link state.
    bw_threshold_mbps REAL,
    bw_direction      TEXT,
    in_octets         TEXT,
    out_octets        TEXT,
    counters_at       TEXT,
    in_bps            REAL,
    out_bps           REAL,
    bw_low_streak     INTEGER NOT NULL DEFAULT 0,
    bw_alarm          INTEGER NOT NULL DEFAULT 0,
    bw_alarm_since    TEXT,
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
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _recent_days(today: str, n: int) -> list[str]:
    """The last `n` UTC calendar days ending at `today`, oldest first."""
    from datetime import timedelta
    base = datetime.strptime(today, "%Y-%m-%d")
    return [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, n) - 1, -1, -1)]


class CentralStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            # Must run BEFORE executescript: _SCHEMA's `CREATE INDEX IF NOT EXISTS`
            # statements reference `org_id`, which doesn't exist yet on a pre-rename
            # table until this migration renames it (a no-op on a brand-new DB, since
            # PRAGMA table_info on a not-yet-created table returns nothing).
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
                ("bw_alarm", "INTEGER NOT NULL DEFAULT 0"), ("bw_alarm_since", "TEXT")))
            self._ensure_columns(conn, "org_devices", (
                ("assigned_node_id", "TEXT"),))
            conn.commit()

    @staticmethod
    def _ensure_columns(conn, table: str, coldefs: tuple[tuple[str, str], ...]) -> None:
        """Add any of `coldefs` missing from `table` (name, SQL type) — a DB created before
        a column existed doesn't get it from CREATE TABLE IF NOT EXISTS."""
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, sqltype in coldefs:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")

    # One-time rename of the pre-existing `tenant_id` column to `org_id` on every table
    # that still has it — a DB created before the rename keeps its old column name
    # forever otherwise, since CREATE TABLE IF NOT EXISTS is a no-op on an existing
    # table. SQLite's RENAME COLUMN updates dependent indexes/UNIQUE constraints in
    # place, so no index rebuild is needed alongside this.
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

    # --- ingest (writers — all serialized through _write_lock) ---
    def touch_node(self, org_id: str, node_id: str, now: str | None = None) -> None:
        """Record edge liveness for the fleet watchdog + Edge Nodes dashboard's
        last-seen. Called from `/report` on every report (full AND recheck) — the ONLY
        heartbeat a central-brain edge ever sends. `/heartbeat`/`/ingest` (which also
        touch `nodes`) are legacy pre-central-brain routes no current edge calls, so
        without this a central-brain node's `last_seen` never updates and both the
        dashboard and `CentralWatchdog` stay permanently blind to it."""
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

    def ingest(self, org_id: str, node_id: str, records: list[dict],
               now: str | None = None) -> list[int]:
        """Persist a batch idempotently. Returns the edge_ids central now durably holds
        (newly inserted OR already present) — the edge deletes exactly those from its outbox.
        An ingest also registers the org + node (so they appear in the fleet view before the
        first heartbeat) and maps every event's device into the global device registry."""
        now = now or _now_iso()
        accepted: list[int] = []
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            self._touch_node(conn, org_id, node_id, now)
            for rec in records:
                edge_id = rec.get("id")
                if edge_id is None:
                    continue
                kind = rec.get("kind")
                body = rec.get("body", {})
                if kind == "event":
                    self._insert_event(conn, org_id, node_id, int(edge_id), body, now)
                elif kind == "rollup":
                    self._insert_rollup(conn, org_id, node_id, int(edge_id), body, now)
                else:
                    continue  # unknown kind — ack it so the edge doesn't wedge on it
                accepted.append(int(edge_id))
            conn.commit()
        return accepted

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

    @staticmethod
    def _resolve_device(conn, org_id, node_id, edge_local_id, body, now) -> int | None:
        """Map (org, node, edge-local id) -> the central global device id, assigning one
        on first sight and refreshing the denormalized name/ip/region. None when the event
        carries no device (e.g. UplinkDown). This IS the id mapping of decision #6."""
        if edge_local_id is None:
            return None
        conn.execute(
            "INSERT INTO devices (org_id, node_id, edge_local_id, name, ip, region,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(org_id, node_id, edge_local_id) DO UPDATE SET"
            "   name=COALESCE(excluded.name, devices.name),"
            "   ip=COALESCE(excluded.ip, devices.ip),"
            "   region=COALESCE(excluded.region, devices.region),"
            "   last_seen=excluded.last_seen",
            (org_id, node_id, edge_local_id, body.get("device_name"),
             body.get("device_ip"), body.get("device_region"), now, now),
        )
        row = conn.execute(
            "SELECT id FROM devices WHERE org_id=? AND node_id=? AND edge_local_id=?",
            (org_id, node_id, edge_local_id)).fetchone()
        return row["id"] if row else None

    def _insert_event(self, conn, org_id, node_id, edge_id, body, now) -> None:
        self._resolve_device(conn, org_id, node_id, body.get("device_id"), body, now)
        conn.execute(
            "INSERT OR IGNORE INTO events (org_id, node_id, edge_id, type, device_id,"
            " device_name, device_ip, device_region, state, occurred_at, payload,"
            " received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, node_id, edge_id, body.get("type"), body.get("device_id"),
             body.get("device_name"), body.get("device_ip"), body.get("device_region"),
             body.get("state"), body.get("at"),
             json.dumps(body, separators=(",", ":")), now),
        )

    def _insert_rollup(self, conn, org_id, node_id, edge_id, body, now) -> None:
        self._resolve_device(conn, org_id, node_id, body.get("device_id"), body, now)
        conn.execute(
            "INSERT OR IGNORE INTO rollups (org_id, node_id, edge_id, device_id, bucket,"
            " payload, received_at) VALUES (?,?,?,?,?,?,?)",
            (org_id, node_id, edge_id, body.get("device_id"), body.get("bucket"),
             json.dumps(body, separators=(",", ":")), now),
        )

    # --- org admin (Part C will surface this; available now for provisioning) ---
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
        """The org's ntfy topic for one alert role (owner/operator/tech) — Phase B's
        AlertDispatcher will route pages through this; Phase A's Settings page + test-alert
        use it now."""
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

    def org_exists(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        return row is not None

    # --- read views (every one is org-scopeable) ---
    def _scope(self, org_id, prefix="") -> tuple[str, tuple]:
        """('' , ()) or (' AND <p>org_id=?', (org_id,)) — the multi-org filter."""
        if not org_id:
            return "", ()
        return f" AND {prefix}org_id = ?", (org_id,)

    def fleet(self, org_id: str | None = None, recent_events: int = 50) -> dict:
        nscope, nargs = self._scope(org_id)
        escope, eargs = self._scope(org_id)
        with self._connect() as conn:
            nodes = [dict(r) for r in conn.execute(
                "SELECT org_id, node_id, version, last_poll_ts, fleet_size, open_outages,"
                " last_seen FROM nodes WHERE 1=1" + nscope
                + " ORDER BY org_id, node_id", nargs)]
            events = [dict(r) for r in conn.execute(
                "SELECT org_id, node_id, type, device_id, device_name, device_ip,"
                " state, occurred_at, received_at FROM events WHERE 1=1" + escope
                + " ORDER BY id DESC LIMIT ?", (*eargs, max(0, recent_events)))]
        return {"nodes": nodes, "recent_events": events}

    def low_bandwidth_alarms(self, org_id: str) -> list[dict]:
        """Monitored ports currently below their bandwidth threshold, across every
        active device this org owns — feeds the dashboard's Low Bandwidth card and
        header chip (mirrors the old single-box `system_summary`'s low_bandwidth list,
        just org-scoped and read straight off `switch_ports` rather than a summary
        helper, since central has no per-org summary function of its own)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sp.id AS port_id, sp.device_id, d.name AS switch_name,"
                " sp.if_index, sp.if_name, sp.if_alias, sp.in_bps, sp.out_bps,"
                " sp.bw_threshold_mbps, sp.bw_direction, sp.bw_alarm_since"
                " FROM switch_ports sp JOIN org_devices d ON d.id = sp.device_id"
                " WHERE sp.org_id=? AND sp.monitored=1 AND sp.bw_alarm=1"
                " AND d.is_active=1 ORDER BY sp.bw_alarm_since", (org_id,)).fetchall()
        out = []
        for r in rows:
            base = r["if_name"] or f"if{r['if_index']}"
            label = f"{base} ({r['if_alias']})" if r["if_alias"] else base
            out.append({
                "port_id": r["port_id"], "device_id": r["device_id"],
                "switch_name": r["switch_name"], "label": label,
                "in_mbps": round(r["in_bps"] / 1e6, 2) if r["in_bps"] is not None else None,
                "out_mbps": round(r["out_bps"] / 1e6, 2) if r["out_bps"] is not None else None,
                "threshold_mbps": r["bw_threshold_mbps"],
                "direction": r["bw_direction"] or "either",
                "since": r["bw_alarm_since"],
            })
        return out

    def data_version(self, org_id: str | None = None) -> str:
        """A cheap monotonic fingerprint of the data the dashboard's live views render,
        for the `/api/events` SSE stream — bumps whenever a new event/outage row lands,
        or a switch_ports write (SNMP walk: port status + live bandwidth, which UPSERT
        in place with no new id) refreshes, so a client knows to re-fetch. Mirrors the
        old single-box server's `_data_version` one-for-one, scoped to one org (or
        every org when `org_id` is None, for a superadmin's all-orgs view)."""
        escope, eargs = self._scope(org_id, prefix="e.")
        oscope, oargs = self._scope(org_id, prefix="o.")
        sscope, sargs = self._scope(org_id, prefix="sp.")
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
        return f"{e}.{o}.{s}"

    # --- self-service node enrollment (dashboard-issued ingest credentials) ---
    @staticmethod
    def _hash_node_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get_node_token_status(self, org_id: str, node_id: str) -> dict | None:
        """None if this identity has never been registered. Else {'created_at',
        'revoked_at'} — never the token or its hash."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at, revoked_at FROM node_tokens"
                " WHERE org_id=? AND node_id=?", (org_id, node_id)).fetchone()
        return dict(row) if row else None

    def issue_node_token(self, org_id: str, node_id: str, *,
                         created_by: int | None = None) -> str:
        """(Re)issues this node's credential: a fresh plaintext token (returned once,
        never stored) and its hash written in place of any prior one, un-revoking it if
        it was revoked. Used for both first-time registration and rotation — the API
        layer (central/server.py) decides which one a request means by checking
        `get_node_token_status` first, so this method itself stays a plain upsert."""
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
        """True if a live credential was revoked; False if there was none to revoke
        (never registered, or already revoked) — the caller's cue for a 404 vs 200."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE node_tokens SET revoked_at=? WHERE org_id=? AND node_id=?"
                " AND revoked_at IS NULL", (_now_iso(), org_id, node_id))
            conn.commit()
        return cur.rowcount > 0

    def delete_node_token(self, org_id: str, node_id: str) -> bool:
        """Permanently forgets this wisp client's identity — unlike `revoke_node_token`
        (which keeps the row so a rotated credential can resume under the same
        identity), this is for a client that's never coming back. Any device
        explicitly assigned to it (`org_devices.assigned_node_id`) is unassigned
        (falls back to NULL = every node covers it) rather than silently orphaned —
        a hard-deleted node_id can never satisfy `node_expected_ips` again, so leaving
        the assignment in place would quietly stop that device from being monitored by
        anyone. Returns True if a row existed to delete."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE org_devices SET assigned_node_id=NULL"
                " WHERE org_id=? AND assigned_node_id=?", (org_id, node_id))
            cur = conn.execute(
                "DELETE FROM node_tokens WHERE org_id=? AND node_id=?",
                (org_id, node_id))
            conn.commit()
        return cur.rowcount > 0

    def list_node_tokens(self, org_id: str) -> list[dict]:
        """This org's registered node identities, left-joined with the `nodes`
        heartbeat table so the dashboard can show "never connected" vs. live/version/
        last-seen in one list without a second round trip. Never includes the token
        or its hash."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT nt.node_id, nt.created_at, nt.revoked_at, n.version,"
                " n.last_seen, n.fleet_size, n.open_outages FROM node_tokens nt"
                " LEFT JOIN nodes n ON n.org_id=nt.org_id AND n.node_id=nt.node_id"
                " WHERE nt.org_id=? ORDER BY nt.node_id", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_node_token(self, presented_token: str) -> tuple[str, str] | None:
        """The `(org_id, node_id)` a presented bearer token authenticates as, or None
        if it's blank or doesn't match any live (non-revoked) registered credential.
        `central/server.py`'s ingest auth calls this the same way it derives identity
        from a verified mTLS cert — never trust the envelope's claimed org/node
        alone, derive it from the credential and compare."""
        if not presented_token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, node_id FROM node_tokens"
                " WHERE token_hash=? AND revoked_at IS NULL",
                (self._hash_node_token(presented_token),)).fetchone()
        return (row["org_id"], row["node_id"]) if row else None

    def node_token_registered(self, org_id: str, node_id: str) -> bool:
        """True if this specific node has its own live registered credential — if so,
        ingest for it is gated on a matching credential even when the deployment has
        neither a global token nor mTLS configured (self-service registration has to
        mean something on its own, not just "if nothing else is set up either")."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM node_tokens WHERE org_id=? AND node_id=?"
                " AND revoked_at IS NULL", (org_id, node_id)).fetchone()
        return row is not None

    def devices(self, org_id: str | None = None) -> list[dict]:
        """The global device registry + each device's latest reported state (the unified
        cross-fleet device view; edge-local ids are hidden behind the global `id`)."""
        scope, args = self._scope(org_id, prefix="d.")
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT d.id, d.org_id, d.node_id, d.edge_local_id, d.name, d.ip,"
                " d.region, d.last_seen,"
                " (SELECT e.state FROM events e WHERE e.org_id=d.org_id"
                "   AND e.node_id=d.node_id AND e.device_id=d.edge_local_id"
                "   AND e.state IS NOT NULL ORDER BY e.id DESC LIMIT 1) AS last_state,"
                " (SELECT e.type FROM events e WHERE e.org_id=d.org_id"
                "   AND e.node_id=d.node_id AND e.device_id=d.edge_local_id"
                "   ORDER BY e.id DESC LIMIT 1) AS last_event"
                " FROM devices d WHERE 1=1" + scope
                + " ORDER BY d.org_id, d.node_id, d.edge_local_id", args)]

    def counts(self) -> dict:
        with self._connect() as conn:
            return {
                "orgs": conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "devices": conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "rollups": conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0],
            }

    # --- cross-edge watchdog support ---
    def node_liveness(self) -> list[dict]:
        """Every node's (org_id, node_id, last_seen) — the watchdog derives staleness."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT org_id, node_id, last_seen FROM nodes")]

    def last_node_alarm(self, org_id: str, node_id: str) -> bool:
        """Restart safety: was the last *delivered* watchdog page for this node a STALE one?
        Only 'sent' rows count, so a failed page doesn't strand the node in 'alarmed'."""
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

    # --- users / login accounts (Part C; crypto in central/auth.py) ---
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

    # --- org team roster + attendance (Part C — org-wide, was per-edge) ---
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
            # FK: clear the worker's attendance rows before the worker row.
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

    # --- Phase A: ISP-managed device topology (org_devices) ---------------------
    def list_org_devices(self, org_id: str) -> list[dict]:
        """Every active device an org has configured, plus each node's child count (for
        the Nodes-page tree + the parent-node dropdown) and its BACKUP parent ids
        (CLAUDE.md item 3's graph topology — the PRIMARY parent is still just
        `parent_device_id` above; `backup_parents` is the extra redundancy edge set).

        Also LEFT JOINs `device_states` (state/latency_ms/packet_loss/jitter_ms/
        state_updated_at) so the dashboard's topology view can color a device without a
        second round trip per device — null until that device's first report lands.
        This is a read-only convenience join; `device_states` itself is still owned by
        `central/engine.py`'s commit path, not written here."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.device_type, d.region,"
                " d.parent_device_id, d.assigned_node_id, d.maintenance, d.snmp_enabled,"
                " d.snmp_version, d.snmp_community, d.snmp_port,"
                " (SELECT COUNT(*) FROM org_devices c"
                "  WHERE c.parent_device_id = d.id AND c.is_active = 1) AS child_count,"
                " s.state AS state, s.latency_ms AS latency_ms, s.packet_loss AS packet_loss,"
                " s.jitter_ms AS jitter_ms, s.updated_at AS state_updated_at"
                " FROM org_devices d LEFT JOIN device_states s ON s.device_id = d.id"
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
        """The owning org of an org_devices row (so a write can be authorized against
        the right org before it's known which org's device this id belongs to)."""
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM org_devices WHERE id=?",
                               (device_id,)).fetchone()
        return row["org_id"] if row else None

    def registered_node_ids(self, org_id: str) -> set[str]:
        """This org's registered edge-node (wisp client) ids, for validating an
        `assigned_node_id` on a device payload — includes revoked ones too (revoking a
        credential is a separate concern from what a device is tagged with)."""
        with self._connect() as conn:
            return {r["node_id"] for r in conn.execute(
                "SELECT node_id FROM node_tokens WHERE org_id=?", (org_id,))}

    def node_expected_ips(self, org_id: str, node_id: str) -> set[str]:
        """IPs this specific edge node is responsible for: devices explicitly assigned
        to it, plus every UNASSIGNED device (default — every node covers it, identical
        to pre-assignment behavior). Feeds `MonitorEngine.process_cycle`'s
        `expected_ips` via `central/engine.py:run_cycle` so a device assigned to a
        DIFFERENT node is skipped by this report rather than scored 100% loss for not
        showing up in it. Same active/not-in-maintenance filter as
        `org_device_topology`, since a paused or deleted device isn't anyone's concern."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ip_address FROM org_devices WHERE org_id=? AND is_active=1"
                " AND maintenance=0 AND (assigned_node_id IS NULL OR assigned_node_id=?)",
                (org_id, node_id)).fetchall()
        return {r["ip_address"] for r in rows}

    def org_device_parent_map(self, org_id: str) -> dict[int, int | None]:
        """id -> parent_device_id over one org's active devices — the cycle-check input
        for `central/inventory.py` (never crosses orgs: an org can't loop through
        another org's topology)."""
        with self._connect() as conn:
            return {r["id"]: r["parent_device_id"] for r in conn.execute(
                "SELECT id, parent_device_id FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,))}

    def create_org_device(self, org_id: str, clean: dict) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type, region,"
                " parent_device_id, assigned_node_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["ip_address"], clean["device_type"],
                 clean["region"], clean["parent_device_id"], clean.get("assigned_node_id"),
                 _now_iso()))
            conn.commit()
            return int(cur.lastrowid)

    def update_org_device(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET name=?, ip_address=?, device_type=?, region=?,"
                " parent_device_id=?, assigned_node_id=? WHERE id=? AND org_id=?"
                " AND is_active=1",
                (clean["name"], clean["ip_address"], clean["device_type"], clean["region"],
                 clean["parent_device_id"], clean.get("assigned_node_id"),
                 device_id, org_id))
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
        """Hard-delete a configured device. Blocked (like the edge) if it still has child
        nodes, so topology never dangles. Returns {ok, reason}.

        Genuinely permanent (the dashboard's own confirm dialog says so), so this
        cascades every OTHER table with a live FK on `org_devices(id)` first —
        `device_states`, `outages` (+ their `alert_log`/`escalations` rows, which
        reference `outage_id` without an enforced FK, so they'd otherwise survive as
        silent orphans), `device_rollups`, `switch_ports` (both its own device_id AND
        any OTHER port's `feeds_device_id` pointing here), `org_device_links` (either
        side), `device_redundancy`, `device_perf_samples`, `device_perf`. Skipping any
        of these previously left the plain `DELETE FROM org_devices` tripping sqlite's
        FK constraint (`IntegrityError`) for literally any device that had ever been
        probed even once — i.e. every real device."""
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
            conn.execute("DELETE FROM org_devices WHERE id=? AND org_id=?",
                         (device_id, org_id))
            conn.commit()
        return {"ok": True}

    # --- Phase B: central runs the brain (per-org engine + live state) -------
    def org_device_topology(self, org_id: str) -> list[dict]:
        """The device set `central/engine.py` builds a MonitorEngine from: active,
        NOT in maintenance (mirrors the edge's `load_device_meta` filter exactly — a
        paused node drops out of detection). Unlike `list_org_devices` (the Nodes-page
        listing, which must still SHOW a maintenance device with its badge), this feeds
        the FSM, so maintenance rows are excluded here, not just flagged. Also carries
        each device's SNMP config (`GET /edge/devices`'s payload) — the edge has no
        local DB, so central hands over the community/port/version it needs to walk a
        switch's IF-MIB itself, the same way it hands over `canary_ip`."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, ip_address, region, parent_device_id, assigned_node_id,"
                " snmp_enabled, snmp_version, snmp_community, snmp_port FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND maintenance=0 ORDER BY id",
                (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def device_states(self, org_id: str) -> dict[int, dict]:
        """device_id -> its last committed FSM row, for engine rehydration after a
        central restart (mirrors the edge's `poll_results` last-row lookup — but this
        table already holds only the current state, so it's a direct read)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, state, latency_ms, packet_loss, jitter_ms FROM"
                " device_states WHERE org_id=?", (org_id,)).fetchall()
        return {r["device_id"]: dict(r) for r in rows}

    def write_device_states(self, org_id: str, rows: list[tuple], ts: str) -> None:
        """Bulk-upsert this cycle's committed FSM state (one row per device):
        `rows` is [(device_id, state, latency_ms, packet_loss, jitter_ms), ...]."""
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
        """Restart-safe rehydration of the per-org uplink/canary flag: was the last
        UPLINK_* log entry a DOWN? Mirrors the edge's `build_engine` alert_log read."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM alert_log WHERE org_id=? AND"
                " (payload LIKE '%UPLINK%' OR payload LIKE '%Uplink%')"
                " ORDER BY id DESC LIMIT 1", (org_id,)).fetchone()
        return bool(row and "UPLINK_DOWN" in (row["payload"] or ""))

    # -- outages (mirrors core/state_machine.apply_events, org-scoped) --
    def open_outage_id(self, org_id: str, device_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["id"] if row else None

    def open_outage_if_absent(self, org_id: str, device_id: int, ts: str,
                              state: str) -> None:
        """Idempotent open: never stack a second open row for a device that already has
        one unresolved (same invariant as the edge's `apply_events`)."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at, final_state)"
                " SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM outages"
                " WHERE org_id=? AND device_id=? AND resolved_at IS NULL)",
                (org_id, device_id, ts, state, org_id, device_id))
            conn.commit()

    def recategorize_outage(self, org_id: str, device_id: int, state: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET final_state=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (state, org_id, device_id))
            conn.commit()

    def stamp_outage_cause(self, org_id: str, outage_id: int, cause: str) -> None:
        """Enrich a still-open outage with a physical cause (e.g. a folded-in SNMP
        port-down) — COALESCE so this never clobbers an operator's own post-mortem
        `root_cause`, and only applies while the outage is still open."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET root_cause = COALESCE(root_cause, ?)"
                " WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (cause, outage_id, org_id))
            conn.commit()

    def resolve_outage(self, org_id: str, device_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET resolved_at=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (ts, org_id, device_id))
            conn.commit()

    def outages_in_window(self, org_id: str, since: str, until: str) -> list[dict]:
        """Every outage overlapping [since, until] for one org (open or resolved) —
        the input to `central/analytics.py`'s downtime/SLA math. An open outage
        (`resolved_at IS NULL`) always overlaps, since it's still ongoing."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND (o.resolved_at IS NULL OR o.resolved_at >= ?)"
                " AND o.started_at <= ? ORDER BY o.started_at",
                (org_id, since, until)).fetchall()
        return [dict(r) for r in rows]

    # -- hourly latency/loss rollups (CLAUDE.md item 2, second slice) --
    def fold_device_rollups(self, entries: list[tuple]) -> None:
        """`entries`: (org_id, device_id, bucket, latency_ms, loss_pct, down) tuples,
        one per device sampled this cycle — accumulated into that hour's running row
        (`col = col + excluded.col` on conflict), so folding N cycles into one bucket is
        just N upserts, never a read-modify-write of a growing list."""
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
        """Hourly buckets in [since, until] for one device, with averages computed at
        read time from the stored running sums. `avg_latency_ms` is None for a bucket
        where every sample was 100% loss (no latency reading ever landed)."""
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
        """The `final_state` of the most recently resolved outage — used to tell a
        genuine recovery from an UNREACHABLE outage we never paged about."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["final_state"] if row else None

    def acknowledge_outage(self, org_id: str, outage_id: int, by: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE outages SET acknowledged_at=COALESCE(acknowledged_at, ?),"
                " acknowledged_by=? WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (_now_iso(), by, outage_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def outage_org(self, outage_id: int) -> str | None:
        """The owning org of an outages row — same re-derive-from-the-row discipline
        as `device_org`/`switch_port_org`, so an ack/post-mortem write is
        authorized against the right org before the request body is trusted."""
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM outages WHERE id=?",
                               (outage_id,)).fetchone()
        return row["org_id"] if row else None

    def triage_outages(self, org_id: str, postmortem_days: int = 30) -> list[dict]:
        """The dashboard's Outages triage queue: every still-open outage, plus any
        resolved in the last `postmortem_days` still missing an operator post-mortem
        `root_cause`. `status` is derived, never stored: 'unassigned' (open,
        unacknowledged), 'in_progress' (open, acknowledged), or 'pending_postmortem'
        (resolved, no root_cause yet). Recovery itself is never operator-driven — the
        FSM resolves outages on its own (CLAUDE.md: cause is operator-confirmed, never
        inferred) — this only surfaces outages that need acknowledgement or a cause."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=postmortem_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name AS device_name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND (o.resolved_at IS NULL"
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
        """Operator-entered post-mortem on an already-RESOLVED outage (CLAUDE.md: cause
        is operator-confirmed, never auto-inferred) — overwrites whatever
        `stamp_outage_cause` may have COALESCEd in from an SNMP fold, since a human call
        supersedes a leading indicator. Refuses a still-open outage: a cause is only
        ever entered at resolution, not while the picture can still change."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE outages SET root_cause=?, resolution_notes=?"
                " WHERE id=? AND org_id=? AND resolved_at IS NOT NULL",
                (root_cause, resolution_notes, outage_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    def list_events(self, org_id: str, limit: int = 100,
                    before_id: int | None = None) -> list[dict]:
        """Newest-first event log for the dashboard's Logs page, cursor-paginated on
        `id` (before_id = strictly older than this id). Unlike `fleet()`'s
        `recent_events` (capped at 50, meant for the overview card), this is the
        org's full history, paged for a proper log view."""
        scope, args = self._scope(org_id)
        cursor = ""
        if before_id is not None:
            cursor = " AND id < ?"
            args = (*args, before_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, org_id, node_id, type, device_id, device_name, device_ip,"
                " device_region, state, occurred_at, received_at FROM events"
                " WHERE 1=1" + scope + cursor + " ORDER BY id DESC LIMIT ?",
                (*args, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]

    # -- alert log + escalation ladder (mirrors egress/notifiers.AlertDispatcher) --
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
        """Cancel any pending escalation rows tied to this device's now-resolved
        outage(s) — recovery stops the hourly ladder (ack alone does not)."""
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

    # --- SNMP port status (central-side, CLAUDE.md item 1) ------------------------
    def list_switch_ports(self, org_id: str, device_id: int) -> list[dict]:
        """Every discovered port on one switch — `central/ports.py`'s per-cycle read
        of prior state (streak/alarm/monitored/feeds) before folding this walk's
        readings, and the Nodes-page port panel's data source."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM switch_ports WHERE org_id=? AND device_id=?"
                " ORDER BY if_index", (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def switch_port_org(self, port_id: int) -> str | None:
        """The owning org of a switch_ports row — same re-derive-from-the-row
        discipline as `device_org`, so a monitored/feeds write is authorized
        against the right org before the request body is trusted."""
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM switch_ports WHERE id=?",
                               (port_id,)).fetchone()
        return row["org_id"] if row else None

    def upsert_switch_port(self, org_id: str, device_id: int, if_index: int,
                           if_name: str | None, if_alias: str | None, admin_status: str,
                           oper_status: str, last_change: str | None, down_streak: int,
                           alarm: bool, alarm_since: str | None, ts: str, *,
                           bw: tuple | None = None) -> None:
        """Discover/refresh one port's live reading + flap-suppressed alarm state.
        Operator-set fields (`monitored`, `feeds_device_id`, `bw_threshold_mbps`,
        `bw_direction`) are NOT touched here — only the dedicated setters do.

        `bw`, when given, is `(in_octets, out_octets, counters_at, in_bps, out_bps,
        bw_low_streak, bw_alarm, bw_alarm_since)` — the throughput half of the same
        walk (CLAUDE.md item 3), written in the SAME upsert so a port's status and
        bandwidth reading never disagree about which walk they came from."""
        in_octets = out_octets = counters_at = in_bps = out_bps = None
        bw_low_streak, bw_alarm, bw_alarm_since = 0, False, None
        if bw is not None:
            (in_octets, out_octets, counters_at, in_bps, out_bps,
             bw_low_streak, bw_alarm, bw_alarm_since) = bw
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO switch_ports (org_id, device_id, if_index, if_name,"
                " if_alias, admin_status, oper_status, last_change, down_streak, alarm,"
                " alarm_since, updated_at, in_octets, out_octets, counters_at, in_bps,"
                " out_bps, bw_low_streak, bw_alarm, bw_alarm_since)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, if_index) DO UPDATE SET"
                " if_name=excluded.if_name, if_alias=excluded.if_alias,"
                " admin_status=excluded.admin_status, oper_status=excluded.oper_status,"
                " last_change=excluded.last_change, down_streak=excluded.down_streak,"
                " alarm=excluded.alarm, alarm_since=excluded.alarm_since,"
                " updated_at=excluded.updated_at, in_octets=excluded.in_octets,"
                " out_octets=excluded.out_octets, counters_at=excluded.counters_at,"
                " in_bps=excluded.in_bps, out_bps=excluded.out_bps,"
                " bw_low_streak=excluded.bw_low_streak, bw_alarm=excluded.bw_alarm,"
                " bw_alarm_since=excluded.bw_alarm_since",
                (org_id, device_id, if_index, if_name, if_alias, admin_status,
                 oper_status, last_change, down_streak, 1 if alarm else 0, alarm_since, ts,
                 str(in_octets) if in_octets is not None else None,
                 str(out_octets) if out_octets is not None else None,
                 counters_at, in_bps, out_bps, bw_low_streak, 1 if bw_alarm else 0,
                 bw_alarm_since))
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
                                  threshold_mbps: float | None, direction: str) -> bool:
        """Operator config for the low-bandwidth alarm: `threshold_mbps=None` disables
        it for that port (badge-only, never alarms — mirrors `snmp_enabled=0`'s "no
        alarm without explicit opt-in" pattern)."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET bw_threshold_mbps=?, bw_direction=?"
                " WHERE id=? AND org_id=?",
                (threshold_mbps, direction, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0

    # --- graph topology: backup edges (CLAUDE.md item 3) --------------------------
    def org_device_backup_map(self, org_id: str) -> dict[int, set[int]]:
        """child_id -> set of active BACKUP parent ids, scoped to one org — the
        cycle-check input for `central/inventory.py:clean_backup_link` (never crosses
        orgs, same discipline as `org_device_parent_map`)."""
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
        """Every active backup edge for one org — `central/engine.py:load_device_meta`
        builds each device's `DeviceMeta.parents` from this."""
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

    # --- on-backup redundancy badge (CLAUDE.md item 3) -----------------------------
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

    # --- per-link performance baseline (CLAUDE.md item 3) --------------------------
    def record_perf_sample(self, org_id: str, device_id: int, ts: str,
                           latency_ms: float | None, packet_loss: float | None,
                           jitter_ms: float | None, state: str, keep: int) -> None:
        """Append one sample and trim to the newest `keep` rows for this device — a
        bounded ring buffer in SQL rather than an ever-growing history table."""
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
        """Oldest-first trailing samples for one device — `evaluate_perf`'s input."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT latency_ms, packet_loss, jitter_ms, state FROM"
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

    # --- releases / staged rollout (Part D version authority) ---
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
            return [{"version": r["version"], "channel": r["channel"],
                     "created_at": r["created_at"]}
                    for r in conn.execute(
                        "SELECT version, channel, created_at FROM releases ORDER BY created_at DESC")]

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
        """Every node's (node_id, version, last_seen) — the rollout evaluator's input."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT node_id, version, last_seen FROM nodes WHERE org_id=?",
                (org_id,))]
