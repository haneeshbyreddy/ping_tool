"""Device inventory: org_devices, regions, locations, link routes, topology, backup links and redundancy state.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import json

from wisp.central.inventory import PASSIVE_TYPES as _PASSIVE_TYPES
from wisp.central.store_util import _now_iso


class DeviceStoreMixin:

    # ----- regions -----------------------------------------------------------

    def list_regions(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            declared = {r["name"] for r in conn.execute(
                "SELECT name FROM org_regions WHERE org_id=?", (org_id,))}
            dev_counts = {r["region"]: r["n"] for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND region IS NOT NULL AND region!=''"
                " GROUP BY region", (org_id,))}
            worker_counts = {r["region"]: r["n"] for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM org_workers"
                " WHERE org_id=? AND is_active=1 AND region IS NOT NULL AND region!=''"
                " GROUP BY region", (org_id,))}
        names = sorted(declared | set(dev_counts) | set(worker_counts), key=str.lower)
        return [{
            "name": n,
            "declared": n in declared,
            "device_count": dev_counts.get(n, 0),
            "worker_count": worker_counts.get(n, 0),
        } for n in names]


    def add_region(self, org_id: str, name: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, name, _now_iso()))
            conn.commit()
            return cur.rowcount > 0


    def rename_region(self, org_id: str, old: str, new: str) -> None:
        # Cascades to devices and workers so a rename can't fragment the org's
        # region set; the new name lands declared even if `old` never was.
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, old))
            conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, new, _now_iso()))
            conn.execute("UPDATE org_devices SET region=? WHERE org_id=? AND region=?",
                         (new, org_id, old))
            conn.execute("UPDATE org_workers SET region=? WHERE org_id=? AND region=?",
                         (new, org_id, old))
            conn.commit()


    def delete_region(self, org_id: str, name: str) -> dict:
        with self._write_lock, self._connect() as conn:
            in_use = conn.execute(
                "SELECT (SELECT COUNT(*) FROM org_devices"
                "        WHERE org_id=? AND region=? AND is_active=1)"
                "     + (SELECT COUNT(*) FROM org_workers"
                "        WHERE org_id=? AND region=? AND is_active=1)",
                (org_id, name, org_id, name)).fetchone()[0]
            if in_use:
                return {"ok": False,
                        "reason": f"region is used by {in_use} device(s)/member(s)"}
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, name))
            conn.commit()
            return {"ok": True}


    def list_org_devices(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.device_type, d.region,"
                " d.parent_device_id, d.assigned_node_id, d.maintenance, d.snmp_enabled,"
                " d.snmp_version, d.snmp_community, d.snmp_port, d.gpon_vendor,"
                " d.lat, d.lng, d.pon_port, d.onu_pon_limit,"
                " (SELECT COUNT(*) FROM org_devices c"
                "  WHERE c.parent_device_id = d.id AND c.is_active = 1) AS child_count,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.alarm = 1) AS ports_down,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.bw_alarm = 1) AS ports_bw_low,"
                " (SELECT COUNT(*) FROM switch_ports p WHERE p.device_id = d.id"
                "  AND p.monitored = 1 AND p.bw_high_alarm = 1) AS ports_bw_high,"
                " g.onus_total AS onus_total, g.onus_online AS onus_online,"
                " g.warn_count AS onus_warn, g.crit_count AS onus_crit,"
                " g.updated_at AS optics_updated_at,"
                " (SELECT MAX(p.updated_at) FROM switch_ports p"
                "  WHERE p.device_id = d.id) AS ports_updated_at,"
                " (SELECT MAX(o.started_at) FROM outages o WHERE o.device_id = d.id"
                "  AND o.resolved_at IS NULL) AS outage_started_at,"
                " s.state AS state, s.latency_ms AS latency_ms, s.packet_loss AS packet_loss,"
                " s.jitter_ms AS jitter_ms, s.updated_at AS state_updated_at,"
                " h.cpu_pct AS health_cpu_pct, h.mem_pct AS health_mem_pct,"
                " h.mem_used_bytes AS health_mem_used_bytes,"
                " h.mem_total_bytes AS health_mem_total_bytes,"
                " h.temp_c AS health_temp_c, h.updated_at AS health_updated_at"
                " FROM org_devices d LEFT JOIN device_states s ON s.device_id = d.id"
                " LEFT JOIN olt_optics g ON g.device_id = d.id"
                " LEFT JOIN device_health h ON h.device_id = d.id"
                " WHERE d.org_id=? AND d.is_active=1 ORDER BY d.id",
                (org_id,)).fetchall()
            links = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'",
                (org_id,)).fetchall()
        backups: dict[int, list[int]] = {}
        for link in links:
            backups.setdefault(link["child_id"], []).append(link["parent_id"])
        out = [dict(r) for r in rows]
        for d in out:
            d["backup_parents"] = backups.get(d["id"], [])
        return out


    def get_org_device(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
        return dict(row) if row else None


    def device_org(self, device_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM org_devices WHERE id=?",
                               (device_id,)).fetchone()
        return row["org_id"] if row else None


    def org_device_parent_map(self, org_id: str) -> dict[int, int | None]:
        with self._connect() as conn:
            return {r["id"]: r["parent_device_id"] for r in conn.execute(
                "SELECT id, parent_device_id FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,))}


    def create_org_device(self, org_id: str, clean: dict) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type, region,"
                " parent_device_id, assigned_node_id, gpon_vendor, pon_port, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["ip_address"], clean["device_type"],
                 clean["region"], clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), clean.get("pon_port"), _now_iso()))
            conn.commit()
            return int(cur.lastrowid)


    def update_org_device(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET name=?, ip_address=?, device_type=?, region=?,"
                " parent_device_id=?, assigned_node_id=?, gpon_vendor=?, pon_port=?"
                " WHERE id=? AND org_id=? AND is_active=1",
                (clean["name"], clean["ip_address"], clean["device_type"], clean["region"],
                 clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), clean.get("pon_port"), device_id, org_id))
            if cur.rowcount > 0 and not clean.get("assigned_node_id"):
                conn.execute("DELETE FROM device_states WHERE org_id=? AND device_id=?",
                             (org_id, device_id))
                open_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM outages WHERE org_id=? AND device_id=?"
                    " AND resolved_at IS NULL", (org_id, device_id))]
                if open_ids:
                    conn.execute(
                        "UPDATE outages SET resolved_at=? WHERE org_id=? AND device_id=?"
                        " AND resolved_at IS NULL", (_now_iso(), org_id, device_id))
                    conn.executemany("DELETE FROM escalations WHERE outage_id=?",
                                     [(oid,) for oid in open_ids])
            conn.commit()
            return cur.rowcount > 0


    def set_org_device_location(self, org_id: str, device_id: int,
                                lat: float | None, lng: float | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET lat=?, lng=? WHERE id=? AND org_id=? AND is_active=1",
                (lat, lng, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_link_route(self, org_id: str, child_id: int, parent_id: int,
                       waypoints: list[list[float]], updated_by: str | None) -> None:
        """Upsert the drawn cable path for one link; an empty list clears it."""
        with self._write_lock, self._connect() as conn:
            if not waypoints:
                conn.execute(
                    "DELETE FROM link_routes WHERE org_id=? AND child_id=? AND parent_id=?",
                    (org_id, child_id, parent_id))
            else:
                conn.execute(
                    "INSERT INTO link_routes (org_id, child_id, parent_id, waypoints,"
                    " updated_at, updated_by) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(org_id, child_id, parent_id) DO UPDATE SET"
                    " waypoints=excluded.waypoints, updated_at=excluded.updated_at,"
                    " updated_by=excluded.updated_by",
                    (org_id, child_id, parent_id, json.dumps(waypoints), _now_iso(),
                     updated_by))
            conn.commit()


    def list_link_routes(self, org_id: str) -> list[dict]:
        # Only routes whose link still exists: a re-parented child leaves its old
        # route row dangling — invisible here, overwritten or deleted later.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.child_id, r.parent_id, r.waypoints, r.updated_at, r.updated_by"
                " FROM link_routes r JOIN org_devices c ON c.id = r.child_id"
                " WHERE r.org_id=? AND c.org_id=? AND c.is_active=1"
                " AND (c.parent_device_id = r.parent_id OR EXISTS ("
                "   SELECT 1 FROM org_device_links l WHERE l.org_id = r.org_id"
                "   AND l.child_id = r.child_id AND l.parent_id = r.parent_id"
                "   AND l.is_active = 1))",
                (org_id, org_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["waypoints"] = json.loads(d["waypoints"])
            except (TypeError, ValueError):
                d["waypoints"] = []
            out.append(d)
        return out


    def set_org_device_maintenance(self, org_id: str, device_id: int, on: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET maintenance=? WHERE id=? AND org_id=? AND is_active=1",
                (1 if on else 0, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_org_device_snmp(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET snmp_enabled=?, snmp_version=?, snmp_community=?,"
                " snmp_port=? WHERE id=? AND org_id=? AND is_active=1",
                (clean["snmp_enabled"], clean["snmp_version"], clean["snmp_community"],
                 clean["snmp_port"], device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def delete_org_device(self, org_id: str, device_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
            if not row:
                return {"ok": False, "reason": "device not found"}
            children = conn.execute(
                "SELECT COUNT(*) FROM org_devices"
                " WHERE parent_device_id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()[0]
        if children:
            return {"ok": False,
                    "reason": f"node has {children} child node(s); reassign them first"}
        with self._write_lock, self._connect() as conn:
            outage_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=?",
                (org_id, device_id))]
            for oid in outage_ids:
                conn.execute("DELETE FROM alert_log WHERE outage_id=?", (oid,))
                conn.execute("DELETE FROM escalations WHERE outage_id=?", (oid,))
            conn.execute("DELETE FROM outages WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM device_states WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM device_rollups WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute(
                "UPDATE switch_ports SET feeds_device_id=NULL"
                " WHERE org_id=? AND feeds_device_id=?", (org_id, device_id))
            conn.execute("DELETE FROM switch_ports WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute(
                "DELETE FROM org_device_links"
                " WHERE org_id=? AND (child_id=? OR parent_id=?)",
                (org_id, device_id, device_id))
            conn.execute(
                "DELETE FROM link_routes"
                " WHERE org_id=? AND (child_id=? OR parent_id=?)",
                (org_id, device_id, device_id))
            conn.execute("DELETE FROM device_redundancy WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM device_perf_samples WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM device_perf WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM onu_optics WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM olt_optics WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM pon_fault_state WHERE org_id=? AND device_id=?",
                         (org_id, device_id))
            conn.execute("DELETE FROM device_snmp_status WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM device_capability WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM org_devices WHERE id=? AND org_id=?",
                         (device_id, org_id))
            conn.commit()
        return {"ok": True}


    def org_device_topology(self, org_id: str) -> list[dict]:
        # Passive plant (splitter/fdb/closure) is filtered HERE, the single choke
        # point: the engine never builds an FSM for it (and the topology
        # fingerprint doesn't move when plant is added — no rebuild, no re-page)
        # and /edge/devices never ships an empty IP for a probe to ping.
        placeholders = ",".join("?" for _ in _PASSIVE_TYPES)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, ip_address, region, parent_device_id, assigned_node_id,"
                " snmp_enabled, snmp_version, snmp_community, snmp_port, device_type,"
                " gpon_vendor FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND maintenance=0"
                f" AND (device_type IS NULL OR device_type NOT IN ({placeholders}))"
                " ORDER BY id",
                (org_id, *_PASSIVE_TYPES)).fetchall()
        return [dict(r) for r in rows]


    def org_passive_ids(self, org_id: str) -> set[int]:
        placeholders = ",".join("?" for _ in _PASSIVE_TYPES)
        with self._connect() as conn:
            return {r["id"] for r in conn.execute(
                "SELECT id FROM org_devices WHERE org_id=? AND is_active=1"
                f" AND device_type IN ({placeholders})",
                (org_id, *_PASSIVE_TYPES))}


    def org_device_backup_map(self, org_id: str) -> dict[int, set[int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'",
                (org_id,)).fetchall()
        out: dict[int, set[int]] = {}
        for r in rows:
            out.setdefault(r["child_id"], set()).add(r["parent_id"])
        return out


    def org_device_backup_edges(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='backup'", (org_id,))]


    def create_backup_link(self, org_id: str, child_id: int, parent_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO org_device_links (org_id, child_id, parent_id,"
                " kind) VALUES (?,?,?,'backup')", (org_id, child_id, parent_id))
            conn.commit()


    def delete_backup_link(self, org_id: str, child_id: int, parent_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM org_device_links WHERE org_id=? AND child_id=?"
                " AND parent_id=? AND kind='backup'", (org_id, child_id, parent_id))
            conn.commit()
            return cur.rowcount > 0


    def device_redundancy_state(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT on_backup, primary_down_since FROM device_redundancy"
                " WHERE org_id=? AND device_id=?", (org_id, device_id)).fetchone()
        return dict(row) if row else None


    def write_device_redundancy(self, org_id: str, device_id: int, on_backup: bool,
                                since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_redundancy (device_id, org_id, on_backup,"
                " primary_down_since, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET on_backup=excluded.on_backup,"
                " primary_down_since=excluded.primary_down_since,"
                " updated_at=excluded.updated_at",
                (device_id, org_id, 1 if on_backup else 0, since, ts))
            conn.commit()
