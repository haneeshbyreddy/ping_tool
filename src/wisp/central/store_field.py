from __future__ import annotations

import hashlib
import secrets

from wisp.central.store_util import _now_iso

TRAIL_MAX_POINTS = 600


class FieldStoreMixin:


    @staticmethod
    def _hash_field_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


    def issue_field_token(self, org_id: str, user_id: int, *,
                          created_by: int | None = None) -> str:

        token = secrets.token_urlsafe(24)
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO field_tokens (org_id, user_id, token_hash, created_at,"
                " created_by) VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id, user_id) DO UPDATE SET"
                " token_hash=excluded.token_hash, created_at=excluded.created_at,"
                " created_by=excluded.created_by, revoked_at=NULL",
                (org_id, user_id, self._hash_field_token(token), now, created_by))
            conn.commit()
        return token


    def revoke_field_token(self, org_id: str, user_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE field_tokens SET revoked_at=? WHERE org_id=? AND user_id=?"
                " AND revoked_at IS NULL", (_now_iso(), org_id, user_id))
            conn.commit()
        return cur.rowcount > 0


    def resolve_field_token(self, presented: str) -> tuple[str, int] | None:

        if not presented:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.org_id, t.user_id FROM field_tokens t"
                " JOIN users u ON u.id = t.user_id"
                " WHERE t.token_hash=? AND t.revoked_at IS NULL AND u.is_active=1",
                (self._hash_field_token(presented),)).fetchone()
        return (row["org_id"], row["user_id"]) if row else None


    def list_field_tokens(self, org_id: str) -> list[dict]:

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT u.id AS user_id, u.username, u.role,"
                " t.created_at AS issued_at, t.revoked_at"
                " FROM users u"
                " LEFT JOIN field_tokens t ON t.org_id=u.org_id AND t.user_id=u.id"
                " WHERE u.org_id=? AND u.is_active=1"
                " ORDER BY (u.role='worker') DESC, u.username", (org_id,)).fetchall()
        return [dict(r) for r in rows]


    def open_shift(self, org_id: str, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, started_at FROM worker_shifts"
                " WHERE org_id=? AND user_id=? AND ended_at IS NULL"
                " ORDER BY id DESC LIMIT 1", (org_id, user_id)).fetchone()
        return dict(row) if row else None


    def start_shift(self, org_id: str, user_id: int, now: str | None = None) -> dict:

        existing = self.open_shift(org_id, user_id)
        if existing:
            return {**existing, "already": True}
        now = now or _now_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO worker_shifts (org_id, user_id, started_at)"
                " VALUES (?,?,?)", (org_id, user_id, now))
            conn.commit()
        return {"id": cur.lastrowid, "started_at": now, "already": False}


    def end_shift(self, org_id: str, user_id: int, now: str | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE worker_shifts SET ended_at=?"
                " WHERE org_id=? AND user_id=? AND ended_at IS NULL",
                (now or _now_iso(), org_id, user_id))
            conn.commit()
        return cur.rowcount > 0


    def last_shift(self, org_id: str, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, started_at, ended_at FROM worker_shifts"
                " WHERE org_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (org_id, user_id)).fetchone()
        return dict(row) if row else None


    def record_worker_fix(self, org_id: str, user_id: int, fix: dict) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO worker_locations"
                " (org_id, user_id, ts, lat, lng, accuracy_m, speed_mps, heading,"
                "  battery_pct) VALUES (?,?,?,?,?,?,?,?,?)",
                (org_id, user_id, fix["ts"], fix["lat"], fix["lng"],
                 fix.get("accuracy_m"), fix.get("speed_mps"), fix.get("heading"),
                 fix.get("battery_pct")))
            conn.commit()
        return cur.rowcount > 0


    def worker_tracking(self, org_id: str, *, trail_since: str) -> list[dict]:

        with self._connect() as conn:
            people = conn.execute(
                "SELECT id AS user_id, username, role FROM users"
                " WHERE org_id=? AND is_active=1 ORDER BY username",
                (org_id,)).fetchall()
            out = []
            for p in people:
                uid = p["user_id"]
                last = conn.execute(
                    "SELECT ts, lat, lng, accuracy_m, speed_mps, heading, battery_pct"
                    " FROM worker_locations WHERE org_id=? AND user_id=?"
                    " ORDER BY ts DESC LIMIT 1", (org_id, uid)).fetchone()
                trail = conn.execute(
                    "SELECT lat, lng FROM worker_locations"
                    " WHERE org_id=? AND user_id=? AND ts>=?"
                    " ORDER BY ts DESC LIMIT ?",
                    (org_id, uid, trail_since, TRAIL_MAX_POINTS)).fetchall()
                shift = conn.execute(
                    "SELECT started_at, ended_at FROM worker_shifts"
                    " WHERE org_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                    (org_id, uid)).fetchone()
                has_token = conn.execute(
                    "SELECT 1 FROM field_tokens WHERE org_id=? AND user_id=?"
                    " AND revoked_at IS NULL", (org_id, uid)).fetchone()
                out.append({
                    "user_id": uid,
                    "username": p["username"],
                    "role": p["role"],
                    "has_token": has_token is not None,
                    "last_fix": dict(last) if last else None,
                    "trail": [[r["lat"], r["lng"]] for r in reversed(trail)],
                    "shift_started_at": shift["started_at"] if shift else None,
                    "shift_ended_at": shift["ended_at"] if shift else None,
                })
        return out


    def prune_worker_locations(self, cutoff: str) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM worker_locations WHERE ts < ?", (cutoff,))
            conn.commit()
        return cur.rowcount
