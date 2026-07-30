"""Device→field-account responsibility: storage for the paging rule.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the schema,
``__init__`` and connection plumbing. The rules this feeds (inheritance down the
tree, unassigned-pages-everyone) live in ``central/assignment.py``; this file is
only reads and writes.

Its own domain rather than a few methods bolted onto ``store_devices``/
``store_users`` because it is exactly the crossing of the two, and because
everything here exists to answer ONE question at paging time — keeping it
together is what makes "does an alarm still reach somebody" auditable in one
file.
"""
from __future__ import annotations

from wisp.central.store_util import _now_iso


class AssignmentStoreMixin:

    # ----- reads used at paging time ---------------------------------------

    def device_parent_map(self, org_id: str) -> dict[int, int | None]:
        """``{device_id: parent_device_id}`` for every ACTIVE device in the org,
        passives included — responsibility covers plant, and a splitter sitting
        between an OLT and an ONU must not break the chain the walk climbs.

        Only the PRIMARY parent: backup edges are a failover path and peer links
        are cabling, neither a chain of command (see assignment.py)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, parent_device_id FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,)).fetchall()
        return {int(r["id"]): (int(r["parent_device_id"])
                               if r["parent_device_id"] is not None else None)
                for r in rows}

    def device_assignment_map(self, org_id: str) -> dict[int, set[int]]:
        """``{device_id: {user_id, …}}`` — the rows as stored, no inheritance
        applied. Joined against `users` so a deactivated account contributes
        nothing: it can't be paged, and letting it count as "somebody is
        responsible" would narrow a device's audience down to nobody."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.device_id, w.user_id FROM org_device_workers w"
                " JOIN users u ON u.id = w.user_id"
                " WHERE w.org_id=? AND u.is_active=1 AND u.org_id=?",
                (org_id, org_id)).fetchall()
        out: dict[int, set[int]] = {}
        for r in rows:
            out.setdefault(int(r["device_id"]), set()).add(int(r["user_id"]))
        return out

    def org_paging_numbers(self, org_id: str) -> dict:
        """``{"owners": [number, …], "workers": {user_id: number}}`` — one read
        of everything an audience can be composed from.

        Owners come back as a bare list because they are never narrowed (they
        page for the whole org, as they always have); workers are keyed by id
        because that is what an assignment names. Accounts without a number are
        absent from both, exactly like ``org_role_whatsapp``."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, role, whatsapp_number FROM users"
                " WHERE org_id=? AND is_active=1 AND role IN ('owner','worker')"
                "   AND whatsapp_number IS NOT NULL AND TRIM(whatsapp_number) <> ''"
                " ORDER BY username", (org_id,)).fetchall()
        owners = [r["whatsapp_number"] for r in rows if r["role"] == "owner"]
        workers = {int(r["id"]): r["whatsapp_number"]
                   for r in rows if r["role"] == "worker"}
        return {"owners": list(dict.fromkeys(owners)), "workers": workers}

    def node_device_ids(self, org_id: str, node_id: str) -> list[int]:
        """Active devices probed by one node — what a probe-down page is about."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM org_devices"
                " WHERE org_id=? AND assigned_node_id=? AND is_active=1",
                (org_id, node_id)).fetchall()
        return [int(r["id"]) for r in rows]

    # ----- reads for the dashboard ----------------------------------------

    def list_device_assignments(self, org_id: str) -> list[dict]:
        """Every assignment row with the account's name attached, for the
        assignment screen. Deactivated accounts are included here (unlike
        ``device_assignment_map``) and flagged, so an operator can see and clear
        a row that has stopped paging anybody."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.device_id, w.user_id, w.assigned_by, w.assigned_at,"
                " u.username, u.role, u.is_active,"
                " (u.whatsapp_number IS NOT NULL AND TRIM(u.whatsapp_number) <> '')"
                "   AS has_whatsapp"
                " FROM org_device_workers w JOIN users u ON u.id = w.user_id"
                " WHERE w.org_id=? ORDER BY u.username, w.device_id",
                (org_id,)).fetchall()
        return [{"device_id": int(r["device_id"]), "user_id": int(r["user_id"]),
                 "username": r["username"], "role": r["role"],
                 "is_active": bool(r["is_active"]),
                 "has_whatsapp": bool(r["has_whatsapp"]),
                 "assigned_by": r["assigned_by"], "assigned_at": r["assigned_at"]}
                for r in rows]

    def device_assignee_ids(self, org_id: str) -> dict[int, list[int]]:
        """``{device_id: [user_id, …]}`` for merging into ``list_org_devices`` —
        EXPLICIT rows only. The device list carries what was clicked, and the
        reader derives the inherited set from the parent chain it already has;
        shipping the inherited set instead would leave the UI unable to tell
        "assigned here" from "covered from above", which is the one thing an
        operator editing an assignment needs to know."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.device_id, w.user_id FROM org_device_workers w"
                " JOIN users u ON u.id = w.user_id"
                " WHERE w.org_id=? AND u.is_active=1 ORDER BY w.user_id",
                (org_id,)).fetchall()
        out: dict[int, list[int]] = {}
        for r in rows:
            out.setdefault(int(r["device_id"]), []).append(int(r["user_id"]))
        return out

    # ----- writes ----------------------------------------------------------

    def assignable_user_ids(self, org_id: str) -> set[int]:
        """Ids that may legally appear in an assignment: ACTIVE accounts of this
        org. The resolution point for "never trust the body's ids" — a worker
        from another org is simply not in the set."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM users WHERE org_id=? AND is_active=1"
                "   AND role IN ('owner','worker')", (org_id,)).fetchall()
        return {int(r["id"]) for r in rows}

    def set_device_assignees(self, org_id: str, device_id: int,
                             user_ids: list[int], by: str) -> bool:
        """REPLACE the assignee set of one device. An empty list clears it, which
        is a real state (back to paging every worker) rather than an ambiguity —
        the opposite of outage assignment, where "assigned to nobody" has no
        meaning and is refused.

        Rows already present are left ALONE, so ``assigned_at``/``assigned_by``
        keep recording when responsibility actually changed hands instead of
        being restamped every time the dialog is saved.
        """
        wanted = {int(u) for u in user_ids}
        with self._write_lock, self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
            if not owned:
                return False
            live = {int(r["id"]) for r in conn.execute(
                "SELECT id FROM users WHERE org_id=? AND is_active=1"
                "   AND role IN ('owner','worker')", (org_id,))}
            wanted &= live
            current = {int(r["user_id"]) for r in conn.execute(
                "SELECT user_id FROM org_device_workers WHERE device_id=?",
                (device_id,))}
            now = _now_iso()
            for uid in sorted(wanted - current):
                conn.execute(
                    "INSERT OR IGNORE INTO org_device_workers"
                    " (org_id, device_id, user_id, assigned_by, assigned_at)"
                    " VALUES (?,?,?,?,?)", (org_id, device_id, uid, by, now))
            for uid in sorted(current - wanted):
                conn.execute(
                    "DELETE FROM org_device_workers WHERE device_id=? AND user_id=?",
                    (device_id, uid))
            conn.commit()
        return True

    def bulk_assign_devices(self, org_id: str, device_ids: list[int],
                            user_ids: list[int], by: str, *,
                            remove: bool = False) -> int:
        """ADD (or REMOVE) one set of accounts across many devices, leaving every
        other assignee on those devices untouched. Returns how many device rows
        changed.

        Additive rather than replacing because the bulk path is "give Ravi the
        Hansa region": doing that as a replace would quietly strip whoever else
        was responsible for those devices, and a bulk action that removes an
        audience it was never asked about is how a page goes missing.
        """
        targets = [int(d) for d in device_ids]
        users = [int(u) for u in user_ids]
        if not targets or not users:
            return 0
        changed = 0
        with self._write_lock, self._connect() as conn:
            marks = ",".join("?" for _ in targets)
            owned = {int(r["id"]) for r in conn.execute(
                f"SELECT id FROM org_devices WHERE org_id=? AND is_active=1"
                f"   AND id IN ({marks})", (org_id, *targets))}
            live = {int(r["id"]) for r in conn.execute(
                "SELECT id FROM users WHERE org_id=? AND is_active=1"
                "   AND role IN ('owner','worker')", (org_id,))}
            users = [u for u in users if u in live]
            now = _now_iso()
            for did in sorted(owned):
                touched = False
                for uid in users:
                    if remove:
                        cur = conn.execute(
                            "DELETE FROM org_device_workers"
                            " WHERE device_id=? AND user_id=?", (did, uid))
                    else:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO org_device_workers"
                            " (org_id, device_id, user_id, assigned_by, assigned_at)"
                            " VALUES (?,?,?,?,?)", (org_id, did, uid, by, now))
                    touched = touched or cur.rowcount > 0
                changed += 1 if touched else 0
            conn.commit()
        return changed
