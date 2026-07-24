"""Dashboard login accounts.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).

The credential-less worker roster + attendance that used to live here went with
the Team page (2026-07-21): who works for the org is now just who has a login.
"""
from __future__ import annotations


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
        # None/"" clears — an absent number IS the "no WhatsApp for this account"
        # state, so org_role_whatsapp simply skips the row.
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET whatsapp_number=? WHERE id=?",
                         (number or None, user_id))
            conn.commit()


    def org_role_whatsapp(self, org_id: str, role: str) -> list[str]:
        """WhatsApp numbers to page for one org+role — the per-account analog of
        org_role_topic. Every ACTIVE login of that role in the org that has set a
        number contributes one; an account without a number is simply absent.
        Deactivated accounts never page (same as a revoked ntfy subscription)."""
        if role not in ("owner", "worker"):
            return []
        with self._connect() as conn:
            return [r["whatsapp_number"] for r in conn.execute(
                "SELECT whatsapp_number FROM users"
                " WHERE org_id=? AND role=? AND is_active=1"
                "   AND whatsapp_number IS NOT NULL AND TRIM(whatsapp_number) <> ''"
                " ORDER BY username", (org_id, role))]


    # --- TOTP second factor -------------------------------------------------
    # get_user()'s SELECT * already returns the totp_* columns for reads; these
    # are the writes. See central/totp.py for the crypto and api/users.py / the
    # login handler for the flow.

    def set_totp_pending(self, user_id: int, secret_enc: str) -> None:
        """Store an ENCRYPTED secret and reset the account to a fresh, NOT-yet-
        enforced enrollment (enabled=0). Restarting enrollment overwrites cleanly
        and drops any stale recovery codes / replay cursor."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=?, totp_enabled=0,"
                " totp_last_step=NULL, totp_recovery=NULL WHERE id=?",
                (secret_enc, user_id))
            conn.commit()

    def activate_totp(self, user_id: int, recovery_json: str) -> None:
        """Flip a confirmed enrollment on and store its recovery-code hashes."""
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
        """Atomically advance the replay cursor to ``step``, but ONLY if it is
        newer than the stored one. Returns True if this call claimed it — the
        login proceeds only on True, so two requests presenting the same fresh
        code can never both succeed (single-use, race-free)."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET totp_last_step=? WHERE id=?"
                " AND (totp_last_step IS NULL OR totp_last_step < ?)",
                (step, user_id, step))
            conn.commit()
            return cur.rowcount == 1

    def consume_recovery_code(self, user_id: int, code_hash: str) -> bool:
        """Remove one recovery-code hash if present (single-use). Read-modify-
        write under the write lock so two requests can't spend the same code."""
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
        """Advance an account's session generation and return the new value. A
        freshly issued cookie signs in this number; every older cookie (a lower
        epoch) then fails auth.resolve_session — so a new login, or a logout, ends
        any OTHER active session for the account. This is the whole of the
        single-active-session mechanism; there is no session table to sweep."""
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
