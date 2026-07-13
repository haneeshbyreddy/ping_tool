"""Org rows, role topics, server-wide settings, showcase/global stats.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations


from wisp.central.store_util import _now_iso


class OrgStoreMixin:

    def set_org(self, org_id: str, name: str | None = None,
                ntfy_topic: str | None = None, ntfy_topic_owner: str | None = None,
                ntfy_topic_operator: str | None = None, ntfy_topic_tech: str | None = None,
                map_region: str | None = None) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, now)
            conn.execute(
                "UPDATE orgs SET name=COALESCE(?, name), ntfy_topic=COALESCE(?, ntfy_topic),"
                " ntfy_topic_owner=COALESCE(?, ntfy_topic_owner),"
                " ntfy_topic_operator=COALESCE(?, ntfy_topic_operator),"
                " ntfy_topic_tech=COALESCE(?, ntfy_topic_tech),"
                " map_region=COALESCE(?, map_region)"
                " WHERE org_id=?",
                (name, ntfy_topic, ntfy_topic_owner, ntfy_topic_operator, ntfy_topic_tech,
                 map_region, org_id))
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
        col = {"owner": "ntfy_topic_owner", "operator": "ntfy_topic_operator",
               "tech": "ntfy_topic_tech"}.get(role)
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
                " o.ntfy_topic_operator, o.ntfy_topic_tech, o.map_region,"
                " (SELECT COUNT(*) FROM nodes n WHERE n.org_id=o.org_id) AS node_count"
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


    def counts(self) -> dict:
        with self._connect() as conn:
            return {
                "orgs": conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0],
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "devices": conn.execute("SELECT COUNT(*) FROM org_devices"
                                        " WHERE is_active=1").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
