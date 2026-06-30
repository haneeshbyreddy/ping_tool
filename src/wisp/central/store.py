"""Central store — the multi-tenant aggregation store (Phase 10 Part B).

Its OWN SQLite (cfg.central_db), wholly separate from any edge DB. Many edges write here,
so every write goes through a single process-wide lock — the plan's "serialized ingest
writer" in miniature (WAL won't save you from many concurrent writers; Postgres behind the
same method surface is the documented upgrade when tenant count grows — *the edge stays
SQLite+stdlib forever*). Reads use WAL and don't take the lock.

Identity & id mapping (decision #6). The durable edge identity is `(tenant_id, node_id)`;
an edge's autoincrement `device_id` is per-SQLite and **cannot be merged across nodes**. So
central keeps its OWN global id space: the `devices` table maps each
`(tenant_id, node_id, edge_local_id)` to a central `id` (the global device id) and carries the
latest denormalized name/ip/region. Every org/node/device row is scoped by `tenant_id`, and
**every read takes an optional `tenant_id` filter** — central is multi-tenant, so nothing is
cross-tenant by default. Orgs are auto-provisioned on first contact (an edge showing up
registers its org); naming/per-org alert routing can be set later (Part C).

Ingest stays idempotent on the edge's outbox row id (`UNIQUE(tenant_id, node_id, edge_id)` +
INSERT OR IGNORE): a re-delivered batch after a lost ack stores nothing twice — at-least-once
delivery + idempotent storage = effectively-once.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT,
    ntfy_topic TEXT,                       -- per-org page target for the fleet watchdog
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    tenant_id    TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    version      TEXT,
    last_poll_ts TEXT,
    fleet_size   INTEGER,
    open_outages INTEGER,
    health       TEXT,                     -- the raw heartbeat body (JSON)
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id)
);
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,   -- the central GLOBAL device id
    tenant_id     TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    edge_local_id INTEGER NOT NULL,                    -- the edge's per-SQLite devices.id
    name          TEXT,
    ip            TEXT,
    region        TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (tenant_id, node_id, edge_local_id)
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
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
    UNIQUE (tenant_id, node_id, edge_id)
);
CREATE TABLE IF NOT EXISTS rollups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    edge_id     INTEGER NOT NULL,
    device_id   INTEGER,
    bucket      TEXT,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (tenant_id, node_id, edge_id)
);
-- The cross-edge watchdog's restart-safe state: the last STALE/OK page per node (only
-- 'sent' rows count when rehydrating, so a failed page is retried, not stranded).
CREATE TABLE IF NOT EXISTS node_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- 'NODE_STALE' | 'NODE_OK'
    status     TEXT NOT NULL,              -- 'sent' | 'failed'
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(tenant_id, node_id, id);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(tenant_id, node_id, device_id, id);
CREATE INDEX IF NOT EXISTS idx_node_alerts ON node_alerts(tenant_id, node_id, id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CentralStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    # --- ingest (writers — all serialized through _write_lock) ---
    def record_heartbeat(self, tenant_id: str, node_id: str, body: dict,
                         now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, tenant_id, now)
            conn.execute(
                """
                INSERT INTO nodes (tenant_id, node_id, version, last_poll_ts, fleet_size,
                                   open_outages, health, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, node_id) DO UPDATE SET
                    version=excluded.version, last_poll_ts=excluded.last_poll_ts,
                    fleet_size=excluded.fleet_size, open_outages=excluded.open_outages,
                    health=excluded.health, last_seen=excluded.last_seen
                """,
                (tenant_id, node_id, body.get("version"), body.get("last_poll_ts"),
                 body.get("fleet_size"), body.get("open_outages"),
                 json.dumps(body, separators=(",", ":")), now, now),
            )
            conn.commit()

    def ingest(self, tenant_id: str, node_id: str, records: list[dict],
               now: str | None = None) -> list[int]:
        """Persist a batch idempotently. Returns the edge_ids central now durably holds
        (newly inserted OR already present) — the edge deletes exactly those from its outbox.
        An ingest also registers the org + node (so they appear in the fleet view before the
        first heartbeat) and maps every event's device into the global device registry."""
        now = now or _now_iso()
        accepted: list[int] = []
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, tenant_id, now)
            self._touch_node(conn, tenant_id, node_id, now)
            for rec in records:
                edge_id = rec.get("id")
                if edge_id is None:
                    continue
                kind = rec.get("kind")
                body = rec.get("body", {})
                if kind == "event":
                    self._insert_event(conn, tenant_id, node_id, int(edge_id), body, now)
                elif kind == "rollup":
                    self._insert_rollup(conn, tenant_id, node_id, int(edge_id), body, now)
                else:
                    continue  # unknown kind — ack it so the edge doesn't wedge on it
                accepted.append(int(edge_id))
            conn.commit()
        return accepted

    @staticmethod
    def _ensure_org(conn, tenant_id, now) -> None:
        conn.execute("INSERT OR IGNORE INTO orgs (tenant_id, created_at) VALUES (?,?)",
                     (tenant_id, now))

    @staticmethod
    def _touch_node(conn, tenant_id, node_id, now) -> None:
        conn.execute(
            "INSERT INTO nodes (tenant_id, node_id, first_seen, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(tenant_id, node_id)"
            " DO UPDATE SET last_seen=excluded.last_seen",
            (tenant_id, node_id, now, now),
        )

    @staticmethod
    def _resolve_device(conn, tenant_id, node_id, edge_local_id, body, now) -> int | None:
        """Map (tenant, node, edge-local id) -> the central global device id, assigning one
        on first sight and refreshing the denormalized name/ip/region. None when the event
        carries no device (e.g. UplinkDown). This IS the id mapping of decision #6."""
        if edge_local_id is None:
            return None
        conn.execute(
            "INSERT INTO devices (tenant_id, node_id, edge_local_id, name, ip, region,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, node_id, edge_local_id) DO UPDATE SET"
            "   name=COALESCE(excluded.name, devices.name),"
            "   ip=COALESCE(excluded.ip, devices.ip),"
            "   region=COALESCE(excluded.region, devices.region),"
            "   last_seen=excluded.last_seen",
            (tenant_id, node_id, edge_local_id, body.get("device_name"),
             body.get("device_ip"), body.get("device_region"), now, now),
        )
        row = conn.execute(
            "SELECT id FROM devices WHERE tenant_id=? AND node_id=? AND edge_local_id=?",
            (tenant_id, node_id, edge_local_id)).fetchone()
        return row["id"] if row else None

    def _insert_event(self, conn, tenant_id, node_id, edge_id, body, now) -> None:
        self._resolve_device(conn, tenant_id, node_id, body.get("device_id"), body, now)
        conn.execute(
            "INSERT OR IGNORE INTO events (tenant_id, node_id, edge_id, type, device_id,"
            " device_name, device_ip, device_region, state, occurred_at, payload,"
            " received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, node_id, edge_id, body.get("type"), body.get("device_id"),
             body.get("device_name"), body.get("device_ip"), body.get("device_region"),
             body.get("state"), body.get("at"),
             json.dumps(body, separators=(",", ":")), now),
        )

    def _insert_rollup(self, conn, tenant_id, node_id, edge_id, body, now) -> None:
        self._resolve_device(conn, tenant_id, node_id, body.get("device_id"), body, now)
        conn.execute(
            "INSERT OR IGNORE INTO rollups (tenant_id, node_id, edge_id, device_id, bucket,"
            " payload, received_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, node_id, edge_id, body.get("device_id"), body.get("bucket"),
             json.dumps(body, separators=(",", ":")), now),
        )

    # --- org admin (Part C will surface this; available now for provisioning) ---
    def set_org(self, tenant_id: str, name: str | None = None,
                ntfy_topic: str | None = None) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, tenant_id, now)
            conn.execute(
                "UPDATE orgs SET name=COALESCE(?, name), ntfy_topic=COALESCE(?, ntfy_topic)"
                " WHERE tenant_id=?", (name, ntfy_topic, tenant_id))
            conn.commit()

    def org_topic(self, tenant_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT ntfy_topic FROM orgs WHERE tenant_id=?",
                               (tenant_id,)).fetchone()
        return row["ntfy_topic"] if row else None

    def orgs(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT o.tenant_id, o.name, o.ntfy_topic,"
                " (SELECT COUNT(*) FROM nodes n WHERE n.tenant_id=o.tenant_id) AS node_count"
                " FROM orgs o ORDER BY o.tenant_id")]

    # --- read views (every one is tenant-scopeable) ---
    def _scope(self, tenant_id, prefix="") -> tuple[str, tuple]:
        """('' , ()) or (' AND <p>tenant_id=?', (tenant_id,)) — the multi-tenant filter."""
        if not tenant_id:
            return "", ()
        return f" AND {prefix}tenant_id = ?", (tenant_id,)

    def fleet(self, tenant_id: str | None = None, recent_events: int = 50) -> dict:
        nscope, nargs = self._scope(tenant_id)
        escope, eargs = self._scope(tenant_id)
        with self._connect() as conn:
            nodes = [dict(r) for r in conn.execute(
                "SELECT tenant_id, node_id, version, last_poll_ts, fleet_size, open_outages,"
                " last_seen FROM nodes WHERE 1=1" + nscope
                + " ORDER BY tenant_id, node_id", nargs)]
            events = [dict(r) for r in conn.execute(
                "SELECT tenant_id, node_id, type, device_id, device_name, device_ip,"
                " state, occurred_at, received_at FROM events WHERE 1=1" + escope
                + " ORDER BY id DESC LIMIT ?", (*eargs, max(0, recent_events)))]
        return {"nodes": nodes, "recent_events": events}

    def devices(self, tenant_id: str | None = None) -> list[dict]:
        """The global device registry + each device's latest reported state (the unified
        cross-fleet device view; edge-local ids are hidden behind the global `id`)."""
        scope, args = self._scope(tenant_id, prefix="d.")
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT d.id, d.tenant_id, d.node_id, d.edge_local_id, d.name, d.ip,"
                " d.region, d.last_seen,"
                " (SELECT e.state FROM events e WHERE e.tenant_id=d.tenant_id"
                "   AND e.node_id=d.node_id AND e.device_id=d.edge_local_id"
                "   AND e.state IS NOT NULL ORDER BY e.id DESC LIMIT 1) AS last_state,"
                " (SELECT e.type FROM events e WHERE e.tenant_id=d.tenant_id"
                "   AND e.node_id=d.node_id AND e.device_id=d.edge_local_id"
                "   ORDER BY e.id DESC LIMIT 1) AS last_event"
                " FROM devices d WHERE 1=1" + scope
                + " ORDER BY d.tenant_id, d.node_id, d.edge_local_id", args)]

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
        """Every node's (tenant_id, node_id, last_seen) — the watchdog derives staleness."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT tenant_id, node_id, last_seen FROM nodes")]

    def last_node_alarm(self, tenant_id: str, node_id: str) -> bool:
        """Restart safety: was the last *delivered* watchdog page for this node a STALE one?
        Only 'sent' rows count, so a failed page doesn't strand the node in 'alarmed'."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kind FROM node_alerts WHERE tenant_id=? AND node_id=?"
                " AND status='sent' AND kind IN ('NODE_STALE','NODE_OK')"
                " ORDER BY id DESC LIMIT 1", (tenant_id, node_id)).fetchone()
        return bool(row and row["kind"] == "NODE_STALE")

    def record_node_alert(self, tenant_id: str, node_id: str, kind: str,
                          status: str, detail: str = "", now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO node_alerts (tenant_id, node_id, kind, status, detail,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (tenant_id, node_id, kind, status, detail, now))
            conn.commit()
