"""Edge nodes: heartbeats/liveness, enrollment tokens, releases and rollouts.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import hashlib
import json
import secrets

from wisp.version import version_tuple
from wisp.central.store_util import _now_iso


class FleetStoreMixin:

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
    def _touch_node(conn, org_id, node_id, now) -> None:
        conn.execute(
            "INSERT INTO nodes (org_id, node_id, first_seen, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(org_id, node_id)"
            " DO UPDATE SET last_seen=excluded.last_seen",
            (org_id, node_id, now, now),
        )


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


    def set_release(self, version: str, artifacts: dict, channel: str = "stable") -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO releases (version, channel, artifacts, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(version) DO UPDATE SET"
                " channel=excluded.channel, artifacts=excluded.artifacts",
                (version, channel, json.dumps(artifacts, separators=(",", ":")), _now_iso()))
            conn.commit()


    def set_release_sync_status(self, ok: bool, detail: str,
                                now: str | None = None) -> dict | None:
        """Record the latest release-sync outcome; returns the PREVIOUS status.

        The previous status is what makes transition-only paging possible — the
        sync timer fires every 15 min and a broken mirror must page once, not 96x/day.
        """
        prev = self.release_sync_status()
        doc = {"ok": bool(ok), "detail": detail, "at": now or _now_iso()}
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('release_sync', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(doc, separators=(",", ":")),))
            conn.commit()
        return prev


    def release_sync_status(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='release_sync'").fetchone()
        return json.loads(row["value"]) if row else None


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
