"""Org rows, role topics, server-wide settings, showcase/global stats.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

from wisp.central.store_util import _now_iso


class OrgStoreMixin:

    #: Org-scoped tables that are REFERENCED by another org-scoped table and so
    #: cannot wait for their turn in `delete_org`'s alphabetical sweep. Both
    #: entries point at `org_cables`, which sorts between them — `org_cable_cores`
    #: happens to be safe today and is listed anyway, because relying on where a
    #: table's NAME falls in the alphabet is not a rule anyone can maintain. The
    #: list is the mechanism; the alternative is discovering the next one as a
    #: bare IntegrityError on somebody's real delete.
    _DELETE_FIRST = ("org_fibre_joints", "org_cable_cores")

    def set_org(self, org_id: str, name: str | None = None,
                ntfy_topic: str | None = None, ntfy_topic_owner: str | None = None,
                ntfy_topic_worker: str | None = None,
                map_region: str | None = None) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            conn.execute(
                "UPDATE orgs SET name=COALESCE(?, name), ntfy_topic=COALESCE(?, ntfy_topic),"
                " ntfy_topic_owner=COALESCE(?, ntfy_topic_owner),"
                " ntfy_topic_worker=COALESCE(?, ntfy_topic_worker),"
                " map_region=COALESCE(?, map_region)"
                " WHERE org_id=?",
                (name, ntfy_topic, ntfy_topic_owner, ntfy_topic_worker,
                 map_region, org_id))
            conn.commit()


    def org_colors(self, org_id: str, kind: str) -> dict[str, str]:
        """Operator colour-coding for one kind ('tag' | 'node'), keyed by name.

        Sparse by design: an uncoloured tag/probe simply has no row, so the
        default costs nothing and clearing a colour is a DELETE, not a sentinel.
        """
        with self._connect() as conn:
            return {r["key"]: r["color"] for r in conn.execute(
                "SELECT key, color FROM org_colors WHERE org_id=? AND kind=?",
                (org_id, kind))}


    def set_org_color(self, org_id: str, kind: str, key: str,
                      color: str | None) -> None:
        with self._write_lock, self._connect() as conn:
            if color is None:
                conn.execute(
                    "DELETE FROM org_colors WHERE org_id=? AND kind=? AND key=?",
                    (org_id, kind, key))
            else:
                conn.execute(
                    "INSERT INTO org_colors (org_id, kind, key, color) VALUES (?,?,?,?)"
                    " ON CONFLICT(org_id, kind, key) DO UPDATE SET color=excluded.color",
                    (org_id, kind, key, color))
            conn.commit()


    @staticmethod
    def _ensure_org(conn, org_id, now) -> None:
        conn.execute("INSERT OR IGNORE INTO orgs (org_id, created_at) VALUES (?,?)",
                     (org_id, now))


    def get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?",
                               (key,)).fetchone()
        return row["value"] if row else None


    def set_setting(self, key: str, value: str | None) -> None:
        # None/"" deletes — an absent row IS the "not configured" state
        with self._write_lock, self._connect() as conn:
            if value:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            else:
                conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
            conn.commit()


    def whatsapp_settings(self) -> dict:
        """The superadmin's live WhatsApp config as a bare {suffix: value} dict
        (keys `enabled`/`token`/`phone_id`/`template`/`lang`/`api_version`),
        read fresh by WhatsAppNotifier on each send so a dashboard change applies
        with no restart. Only present keys are returned — the notifier fills the
        rest from the Config env-var fallbacks."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE 'whatsapp_%'"
            ).fetchall()
        return {r["key"][len("whatsapp_"):]: r["value"] for r in rows}


    def set_org_poll_interval(self, org_id: str, seconds: int | None) -> None:
        # Not folded into set_org: its COALESCE pattern can't write NULL, and
        # NULL ("automatic") is a legitimate target state here.
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute("UPDATE orgs SET poll_interval_s=? WHERE org_id=?",
                         (seconds, org_id))
            conn.commit()


    def org_poll_interval(self, org_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT poll_interval_s FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row["poll_interval_s"] if row else None


    def set_org_auto_update(self, org_id: str, enabled: bool) -> None:
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute("UPDATE orgs SET auto_update=? WHERE org_id=?",
                         (1 if enabled else 0, org_id))
            conn.commit()


    def org_auto_update(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT auto_update FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return bool(row["auto_update"]) if row else False


    # ----- paywall (central/billing.py owns the math) -----------------------

    def org_plan(self, org_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT plan FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return (row["plan"] if row and row["plan"] else "free")


    def set_org_plan(self, org_id: str, plan: str) -> None:
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute("UPDATE orgs SET plan=? WHERE org_id=?", (plan, org_id))
            conn.commit()


    def paid_months(self, org_id: str) -> set[str]:
        with self._connect() as conn:
            return {r["month"] for r in conn.execute(
                "SELECT month FROM org_billing_months WHERE org_id=?", (org_id,))}


    def set_billing_month(self, org_id: str, month: str, paid: bool,
                          marked_by: str | None = None) -> None:
        with self._write_lock, self._connect() as conn:
            if paid:
                conn.execute(
                    "INSERT INTO org_billing_months (org_id, month, marked_by, marked_at)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(org_id, month) DO UPDATE SET"
                    " marked_by=excluded.marked_by, marked_at=excluded.marked_at",
                    (org_id, month, marked_by, _now_iso()))
            else:
                conn.execute("DELETE FROM org_billing_months WHERE org_id=? AND month=?",
                             (org_id, month))
            conn.commit()


    def billing_orgs(self) -> list[dict]:
        """Paid-plan orgs + their page targets — the billing sweeper's input."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT org_id, name, plan, ntfy_topic, ntfy_topic_owner FROM orgs"
                " WHERE plan IN ('pro','vip') ORDER BY org_id")]


    def billing_notice(self, org_id: str, month: str, kind: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM billing_notices WHERE org_id=? AND month=? AND kind=?",
                (org_id, month, kind)).fetchone()
        return row["status"] if row else None


    def record_billing_notice(self, org_id: str, month: str, kind: str,
                              status: str, sent_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO billing_notices (org_id, month, kind, status, sent_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id, month, kind) DO UPDATE SET"
                " status=excluded.status, sent_at=excluded.sent_at",
                (org_id, month, kind, status, sent_at))
            conn.commit()


    def org_monitored_device_count(self, org_id: str,
                                   passive_types: tuple[str, ...] = ()) -> int:
        """Active probed devices — passive plant (splitters/FDBs) never counts
        toward the paywall device cap."""
        ph = ",".join("?" for _ in passive_types)
        extra = f" AND (device_type IS NULL OR device_type NOT IN ({ph}))" if ph else ""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM org_devices WHERE org_id=? AND is_active=1" + extra,
                (org_id, *passive_types)).fetchone()[0]


    def org_topic(self, org_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT ntfy_topic FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row["ntfy_topic"] if row else None


    def org_name(self, org_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT name FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row["name"] if row else None


    def org_role_topic(self, org_id: str, role: str) -> str | None:
        col = {"owner": "ntfy_topic_owner", "worker": "ntfy_topic_worker"}.get(role)
        if not col:
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT {col} FROM orgs WHERE org_id=?",
                               (org_id,)).fetchone()
        return row[col] if row else None


    def orgs(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT o.org_id, o.name, o.ntfy_topic, o.ntfy_topic_owner,"
                " o.ntfy_topic_worker, o.map_region,"
                " o.poll_interval_s, o.plan, o.web_proxy,"
                " (SELECT COUNT(*) FROM nodes n WHERE n.org_id=o.org_id) AS node_count,"
                # what a delete would take with it — the confirmation states it
                " (SELECT COUNT(*) FROM org_devices d"
                "   WHERE d.org_id=o.org_id AND d.is_active=1) AS device_count,"
                " (SELECT COUNT(*) FROM users u WHERE u.org_id=o.org_id) AS user_count"
                " FROM orgs o ORDER BY o.org_id")]


    def showcase_stats(self, limit: int = 40) -> dict:
        """Public social-proof numbers for the marketing landing ticker.

        `count` is orgs with at least one probe node (real deployments, not
        empty/test orgs); `names` are the named subset (a customer opts out of
        the scroll simply by leaving its display name blank), oldest first,
        capped at `limit` so a huge fleet doesn't bloat the injected payload.
        """
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM orgs o"
                " WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.org_id=o.org_id)"
            ).fetchone()[0]
            names = [r[0] for r in conn.execute(
                "SELECT o.name FROM orgs o"
                " WHERE o.name IS NOT NULL AND TRIM(o.name) <> ''"
                "   AND EXISTS (SELECT 1 FROM nodes n WHERE n.org_id=o.org_id)"
                " ORDER BY o.created_at ASC LIMIT ?", (limit,))]
        return {"count": count, "names": names}


    def org_exists(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        return row is not None


    def org_summary(self, org_id: str) -> dict:
        """What a delete would destroy — shown on the confirmation, never guessed."""
        with self._connect() as conn:
            def one(sql: str) -> int:
                return conn.execute(sql, (org_id,)).fetchone()[0]
            return {
                "devices": one("SELECT COUNT(*) FROM org_devices"
                               " WHERE org_id=? AND is_active=1"),
                "nodes": one("SELECT COUNT(*) FROM nodes WHERE org_id=?"),
                "users": one("SELECT COUNT(*) FROM users WHERE org_id=?"),
                "outages": one("SELECT COUNT(*) FROM outages WHERE org_id=?"),
            }


    def delete_org(self, org_id: str) -> dict:
        """Erase an org and everything scoped to it. Irreversible.

        The org-scoped tables are DISCOVERED (any table carrying an ``org_id``
        column) rather than listed: this schema grows a table most months, and a
        hardcoded list silently orphans rows in whatever was added last — an
        org id is reusable, so those rows would surface inside a LATER org of
        the same name. Rows with ``org_id IS NULL`` are global by construction
        (superadmins in `users`, the built-in `snmp_profiles`/`gpon_profiles`)
        and an equality match already spares them.

        ORDER MATTERS: `_connect` runs with `PRAGMA foreign_keys=ON` and nearly
        every FK in the schema points at ``org_devices(id)``, so the device table
        goes LAST — sweeping it alphabetically (before `switch_ports`,
        `snmp_walks`, …) leaves those rows dangling at statement end and SQLite
        aborts the whole delete. Constraints stay IMMEDIATE rather than deferred
        on purpose: a violation surviving this ordering means a row references
        another org's device, which should fail loudly, not be swept away.

        ``org_cables(id)`` is the referenced table this rule exists for, and the
        alphabet does NOT satisfy it: `org_fibre_joints` sorts AFTER it, so a
        plain alphabetical sweep would take the cables out from under live joint
        rows and abort the whole delete. Hence `_DELETE_FIRST` — the explicit
        head this docstring predicted would eventually be needed. A new
        table that is REFERENCED by another org-scoped one belongs in it.
        """
        deleted: dict[str, int] = {}
        with self._write_lock, self._connect() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            scoped = []
            for table in tables:
                if table in ("orgs", "org_devices"):
                    continue
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if "org_id" in cols:
                    scoped.append(table)
            head = [t for t in self._DELETE_FIRST if t in scoped]
            scoped = head + [t for t in scoped if t not in head]
            for table in (*scoped, "org_devices", "orgs"):
                cur = conn.execute(f"DELETE FROM {table} WHERE org_id=?", (org_id,))
                if cur.rowcount:
                    deleted[table] = cur.rowcount
            conn.commit()
        return deleted


    def counts(self) -> dict:
        with self._connect() as conn:
            return {
                "orgs": conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "devices": conn.execute("SELECT COUNT(*) FROM org_devices"
                                        " WHERE is_active=1").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
