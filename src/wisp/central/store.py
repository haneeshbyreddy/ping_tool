"""Central store — the multi-edge mirror (Phase 10 Part A skeleton).

Its OWN SQLite (cfg.central_db), wholly separate from any edge DB. Many edges write here,
so writes go through a single process-wide lock — the miniature of the plan's "serialized
ingest writer" (WAL won't save you from many concurrent writers; Part B is where this may
become a real queue / Postgres). Reads use WAL and don't take the lock.

Every row is scoped by `(tenant_id, node_id)` — the durable edge identity (decision #6).
Edge-local `device_id`s ride along only as a per-node correlation id; central does NOT try
to merge them into a global id yet (that mapping is Part B). Ingest is idempotent on the
edge's outbox row id (`UNIQUE(tenant_id, node_id, edge_id)` + INSERT OR IGNORE), so a
re-delivered batch after a lost ack stores nothing twice — at-least-once + idempotent =
effectively-once.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    tenant_id    TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    version      TEXT,
    last_poll_ts TEXT,
    fleet_size   INTEGER,
    open_outages INTEGER,
    health       TEXT,                 -- the raw heartbeat body (JSON)
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_id)
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    edge_id       INTEGER NOT NULL,    -- the edge's outbox row id (idempotency key)
    type          TEXT,
    device_id     INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_events_node ON events(tenant_id, node_id, id);
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

    # --- ingest (writers — serialized) ---
    def record_heartbeat(self, tenant_id: str, node_id: str, body: dict,
                         now: str | None = None) -> None:
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
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
        (newly inserted OR already present) — the edge deletes exactly those from its
        outbox. A node touched by ingest is also registered so it appears in the fleet view
        even before its first heartbeat lands."""
        now = now or _now_iso()
        accepted: list[int] = []
        with self._write_lock, self._connect() as conn:
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
    def _touch_node(conn, tenant_id, node_id, now) -> None:
        conn.execute(
            "INSERT INTO nodes (tenant_id, node_id, first_seen, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(tenant_id, node_id)"
            " DO UPDATE SET last_seen=excluded.last_seen",
            (tenant_id, node_id, now, now),
        )

    @staticmethod
    def _insert_event(conn, tenant_id, node_id, edge_id, body, now) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO events (tenant_id, node_id, edge_id, type, device_id,"
            " device_name, device_ip, device_region, state, occurred_at, payload,"
            " received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, node_id, edge_id, body.get("type"), body.get("device_id"),
             body.get("device_name"), body.get("device_ip"), body.get("device_region"),
             body.get("state"), body.get("at"),
             json.dumps(body, separators=(",", ":")), now),
        )

    @staticmethod
    def _insert_rollup(conn, tenant_id, node_id, edge_id, body, now) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO rollups (tenant_id, node_id, edge_id, device_id, bucket,"
            " payload, received_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, node_id, edge_id, body.get("device_id"), body.get("bucket"),
             json.dumps(body, separators=(",", ":")), now),
        )

    # --- read view (the Part A value: a fleet-wide picture) ---
    def fleet(self, recent_events: int = 50) -> dict:
        with self._connect() as conn:
            nodes = [dict(r) for r in conn.execute(
                "SELECT tenant_id, node_id, version, last_poll_ts, fleet_size,"
                " open_outages, last_seen FROM nodes ORDER BY tenant_id, node_id")]
            events = [dict(r) for r in conn.execute(
                "SELECT tenant_id, node_id, type, device_id, device_name, device_ip,"
                " state, occurred_at, received_at FROM events"
                " ORDER BY id DESC LIMIT ?", (max(0, recent_events),))]
        return {"nodes": nodes, "recent_events": events}

    def counts(self) -> dict:
        with self._connect() as conn:
            return {
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "rollups": conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0],
            }
