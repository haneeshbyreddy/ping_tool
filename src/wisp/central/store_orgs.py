from __future__ import annotations

from wisp.central.store_util import _now_iso


class OrgStoreMixin:

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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE 'whatsapp_%'"
            ).fetchall()
        return {r["key"][len("whatsapp_"):]: r["value"] for r in rows}


    def set_org_poll_interval(self, org_id: str, seconds: int | None) -> None:
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
                " (SELECT COUNT(*) FROM org_devices d"
                "   WHERE d.org_id=o.org_id AND d.is_active=1) AS device_count,"
                " (SELECT COUNT(*) FROM users u WHERE u.org_id=o.org_id) AS user_count"
                " FROM orgs o ORDER BY o.org_id")]


    def showcase_stats(self, limit: int = 40) -> dict:

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
