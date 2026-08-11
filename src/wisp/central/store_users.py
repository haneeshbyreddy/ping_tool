from __future__ import annotations

import re

from wisp.central.store_util import _now_iso


class UserStoreMixin:

    def add_user(self, org_id: str | None, username: str, pw_hash: str,
                 pw_salt: str, role: str = "worker") -> int:
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
                "SELECT id, org_id, username, role, is_active, whatsapp_number,"
                " created_at FROM users"
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


    def set_user_whatsapp(self, user_id: int, number: str | None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET whatsapp_number=? WHERE id=?",
                         (number or None, user_id))
            conn.commit()


    def org_role_whatsapp(self, org_id: str, role: str) -> list[str]:
        if role not in ("owner", "worker"):
            return []
        with self._connect() as conn:
            return [r["whatsapp_number"] for r in conn.execute(
                "SELECT whatsapp_number FROM users"
                " WHERE org_id=? AND role=? AND is_active=1"
                "   AND whatsapp_number IS NOT NULL AND TRIM(whatsapp_number) <> ''"
                " ORDER BY username", (org_id, role))]

    def org_alert_recipients(self, org_id: str) -> list[str]:

        nums: list[str] = []
        for role in ("owner", "worker"):
            nums.extend(self.org_role_whatsapp(org_id, role))
        return list(dict.fromkeys(nums))

    def named_whatsapp(self, org_id: str, usernames: list[str]) -> list[str]:

        names = [str(u) for u in (usernames or []) if str(u or "").strip()]
        if not names:
            return []
        marks = ",".join("?" for _ in names)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT whatsapp_number FROM users"
                f" WHERE org_id=? AND is_active=1 AND username IN ({marks})"
                f"   AND whatsapp_number IS NOT NULL AND TRIM(whatsapp_number) <> ''"
                f" ORDER BY username", (org_id, *names)).fetchall()
        return list(dict.fromkeys(r["whatsapp_number"] for r in rows))

    def whatsapp_user(self, number: str) -> dict | None:

        digits = re.sub(r"\D", "", str(number or ""))
        if len(digits) < 8:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, org_id, role, whatsapp_number FROM users"
                " WHERE is_active=1 AND org_id IS NOT NULL"
                "   AND whatsapp_number IS NOT NULL AND TRIM(whatsapp_number) <> ''"
            ).fetchall()
        hits = [{"id": r["id"], "username": r["username"], "org_id": r["org_id"],
                 "role": r["role"]}
                for r in rows
                if re.sub(r"\D", "", r["whatsapp_number"] or "") == digits]
        return hits[0] if len(hits) == 1 else None


    def set_totp_pending(self, user_id: int, secret_enc: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=?, totp_enabled=0,"
                " totp_last_step=NULL, totp_recovery=NULL WHERE id=?",
                (secret_enc, user_id))
            conn.commit()

    def activate_totp(self, user_id: int, recovery_json: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_enabled=1, totp_recovery=? WHERE id=?",
                (recovery_json, user_id))
            conn.commit()

    def disable_totp(self, user_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=NULL, totp_enabled=0,"
                " totp_last_step=NULL, totp_recovery=NULL WHERE id=?", (user_id,))
            conn.commit()

    def set_totp_recovery(self, user_id: int, recovery_json: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET totp_recovery=? WHERE id=?",
                         (recovery_json, user_id))
            conn.commit()

    def claim_totp_step(self, user_id: int, step: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET totp_last_step=? WHERE id=?"
                " AND (totp_last_step IS NULL OR totp_last_step < ?)",
                (step, user_id, step))
            conn.commit()
            return cur.rowcount == 1

    def consume_recovery_code(self, user_id: int, code_hash: str) -> bool:
        import json
        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT totp_recovery FROM users WHERE id=?",
                               (user_id,)).fetchone()
            if not row or not row["totp_recovery"]:
                return False
            try:
                hashes = json.loads(row["totp_recovery"])
            except (ValueError, TypeError):
                return False
            if code_hash not in hashes:
                return False
            hashes = [h for h in hashes if h != code_hash]
            conn.execute("UPDATE users SET totp_recovery=? WHERE id=?",
                         (json.dumps(hashes), user_id))
            conn.commit()
            return True

    def bump_session_epoch(self, user_id: int) -> int:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET session_epoch = COALESCE(session_epoch, 0) + 1"
                " WHERE id=?", (user_id,))
            row = conn.execute(
                "SELECT session_epoch FROM users WHERE id=?", (user_id,)).fetchone()
            conn.commit()
        return int(row["session_epoch"]) if row else 0

    def delete_user(self, user_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
