from __future__ import annotations

from wisp.central.store_util import _now_iso

PROXY_AUDIT_KEEP_DAYS = 14


class ProxyStoreMixin:

    def create_proxy_session(self, sid: str, org_id: str, device_id: int,
                             node_id: str, created_by: int | None,
                             expires_at: str) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO proxy_sessions (sid, org_id, device_id, node_id,"
                " created_by, created_at, expires_at, status, last_active_at)"
                " VALUES (?,?,?,?,?,?,?, 'open', ?)",
                (sid, org_id, device_id, node_id, created_by, now, expires_at, now))
            conn.commit()

    def prune_proxy_audit(self, cutoff_iso: str) -> int:
        # Rides the history maintenance loop, not session creation: an org that
        # stops opening tunnels used to stop pruning while its rows stayed.
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM proxy_audit WHERE ts < ?", (cutoff_iso,))
            conn.commit()
            return cur.rowcount

    def touch_proxy_session(self, sid: str, expires_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE proxy_sessions SET expires_at=?, last_active_at=?"
                " WHERE sid=? AND status='open'",
                (expires_at, _now_iso(), sid))
            conn.commit()

    def close_node_proxy_sessions(self, org_id: str, node_id: str) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE proxy_sessions SET status='closed'"
                " WHERE org_id=? AND node_id=? AND status='open'",
                (org_id, node_id))
            conn.commit()
            return cur.rowcount

    def close_proxy_session(self, sid: str, status: str = "closed") -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE proxy_sessions SET status=? WHERE sid=? AND status='open'",
                (status, sid))
            conn.commit()
            return cur.rowcount > 0

    def proxy_session_row(self, sid: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM proxy_sessions WHERE sid=?",
                               (sid,)).fetchone()
        return dict(row) if row else None

    def list_proxy_sessions(self, org_id: str, limit: int = 50) -> list[dict]:
        now = _now_iso()
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT s.*, d.name AS device_name FROM proxy_sessions s"
                " LEFT JOIN org_devices d ON d.id = s.device_id"
                " WHERE s.org_id=? ORDER BY s.created_at DESC LIMIT ?",
                (org_id, max(1, int(limit))))]
        for r in rows:
            if r["status"] == "open" and r["expires_at"] < now:
                r["status"] = "expired"
        return rows

    def record_proxy_audit(self, sid: str, org_id: str, device_id: int,
                           user_id: int | None, method: str, path: str,
                           status: int | None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO proxy_audit (sid, org_id, device_id, user_id,"
                " method, path, status, ts) VALUES (?,?,?,?,?,?,?,?)",
                (sid, org_id, device_id, user_id, method[:16], path[:512],
                 status, _now_iso()))
            conn.commit()

    def list_proxy_audit(self, org_id: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT a.*, d.name AS device_name FROM proxy_audit a"
                " LEFT JOIN org_devices d ON d.id = a.device_id"
                " WHERE a.org_id=? ORDER BY a.id DESC LIMIT ?",
                (org_id, max(1, int(limit))))]


    def org_web_proxy(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT web_proxy FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return bool(row["web_proxy"]) if row else False

    def set_org_web_proxy(self, org_id: str, on: bool) -> None:
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute("UPDATE orgs SET web_proxy=? WHERE org_id=?",
                         (1 if on else 0, org_id))
            conn.commit()
