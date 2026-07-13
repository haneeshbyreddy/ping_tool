"""Dashboard users, field workers, attendance.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations


from wisp.central.store_util import _now_iso, _today, _recent_days


class UserStoreMixin:

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
