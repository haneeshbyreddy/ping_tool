"""Worker location tracking: tracker credentials, shifts and fixes.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).

The credential half is the NODE-TOKEN pattern verbatim (``store_fleet.py``): only
a SHA-256 hash is kept, the plaintext is shown once, and the only way back is a
rotation. That is what lets the tracker's server URL stay identical for every
worker — the token rides Traccar's ``id`` field, so identity is per-person while
the string an owner reads down a phone line is one string.
"""
from __future__ import annotations

import hashlib
import secrets

from wisp.central.store_util import _now_iso

# Ceiling on the points ONE worker's trail may carry in a reply. At the designed
# 90 s cadence a 12-hour day is ~480 fixes, so this is a bound on a runaway
# client rather than a truncation of a real day — and a trail is a shape, not a
# record: past a few hundred points the polyline is pixel-identical and the reply
# is just bigger. Newest are kept (see the query) because the recent end is the
# half anybody is looking at.
TRAIL_MAX_POINTS = 600


class FieldStoreMixin:

    # ----- tracker credentials ------------------------------------------------

    @staticmethod
    def _hash_field_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


    def issue_field_token(self, org_id: str, user_id: int, *,
                          created_by: int | None = None) -> str:
        """Mint (or ROTATE) a worker's tracker token; returns the plaintext ONCE.

        Rotating un-revokes, exactly like ``issue_node_token``: the owner's way
        back from a lost handset is to issue a new string, and leaving the row
        revoked would mean the fresh token could not authenticate."""
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
        """(org_id, user_id) for a live token, else None.

        Identity comes FROM the credential — the request carries no other claim
        about who is reporting, which is the same rule edge ingest follows. A
        DEACTIVATED account resolves to nothing: switching an account off has to
        stop its phone reporting, or "deactivated" would mean less than it says.
        """
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
        """Every ACTIVE account in the org with its tracker-credential state.

        Driven off `users`, not off `field_tokens`: the owner-facing panel has to
        list the people who could be tracked and haven't been set up yet — a
        roster of only the issued ones can never show you who is missing. Ships
        `issued_at`/`revoked_at` and nothing resembling the token, which is not
        recoverable anyway."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT u.id AS user_id, u.username, u.role,"
                " t.created_at AS issued_at, t.revoked_at"
                " FROM users u"
                " LEFT JOIN field_tokens t ON t.org_id=u.org_id AND t.user_id=u.id"
                " WHERE u.org_id=? AND u.is_active=1"
                " ORDER BY (u.role='worker') DESC, u.username", (org_id,)).fetchall()
        return [dict(r) for r in rows]


    # ----- shifts -------------------------------------------------------------

    def open_shift(self, org_id: str, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, started_at FROM worker_shifts"
                " WHERE org_id=? AND user_id=? AND ended_at IS NULL"
                " ORDER BY id DESC LIMIT 1", (org_id, user_id)).fetchone()
        return dict(row) if row else None


    def start_shift(self, org_id: str, user_id: int, now: str | None = None) -> dict:
        """Mark on-shift. IDEMPOTENT — an already-open shift is returned as-is.

        A second press must not open a second row: the dashboard button and a
        stale tab press the same thing, and two overlapping shifts would make
        "when did he start" unanswerable."""
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
        """Close the open shift. False when there wasn't one — idempotent for the
        same reason `start_shift` is."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE worker_shifts SET ended_at=?"
                " WHERE org_id=? AND user_id=? AND ended_at IS NULL",
                (now or _now_iso(), org_id, user_id))
            conn.commit()
        return cur.rowcount > 0


    def last_shift(self, org_id: str, user_id: int) -> dict | None:
        """The most recent shift, open or closed — what "went home at 6" is read
        from once the shift is over."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, started_at, ended_at FROM worker_shifts"
                " WHERE org_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (org_id, user_id)).fetchone()
        return dict(row) if row else None


    # ----- fixes --------------------------------------------------------------

    def record_worker_fix(self, org_id: str, user_id: int, fix: dict) -> bool:
        """Store one position. False when it was a REPLAY of one already held.

        INSERT OR IGNORE against UNIQUE(org_id, user_id, ts): Traccar re-sends a
        fix it never got a 200 for, so the same position legitimately arrives
        twice and must land once."""
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
        """Every active account in the org: its latest fix, its trail since
        `trail_since`, and its shift state.

        EVERY account, not only the ones that have reported — "set up but never
        worked" and "on shift and gone quiet" are two of the four states the map
        must tell apart, and neither can be rendered from a list that only
        contains people who sent something. The caller (`central/field.py`)
        classifies; this returns facts.
        """
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
                # Newest-first with a cap, then reversed: a runaway client must
                # not be able to push the recent end of a trail out of the reply.
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
        """Drop fixes stamped before `cutoff`. The retention policy IS the
        feature — see the table comment."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM worker_locations WHERE ts < ?", (cutoff,))
            conn.commit()
        return cur.rowcount
