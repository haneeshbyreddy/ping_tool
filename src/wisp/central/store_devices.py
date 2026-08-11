from __future__ import annotations

import json

from wisp.central import cablepath, fiber
from wisp.central.inventory import PASSIVE_TYPES as _PASSIVE_TYPES
from wisp.central.store_util import _now_iso


class DeviceStoreMixin:


    def list_regions(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            declared = {r["name"] for r in conn.execute(
                "SELECT name FROM org_regions WHERE org_id=?", (org_id,))}
            dev_counts = {r["region"]: r["n"] for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND region IS NOT NULL AND region!=''"
                " GROUP BY region", (org_id,))}
        names = sorted(declared | set(dev_counts), key=str.lower)
        return [{
            "name": n,
            "declared": n in declared,
            "device_count": dev_counts.get(n, 0),
        } for n in names]


    def add_region(self, org_id: str, name: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, name, _now_iso()))
            conn.commit()
            return cur.rowcount > 0


    def rename_region(self, org_id: str, old: str, new: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, old))
            conn.execute(
                "INSERT OR IGNORE INTO org_regions (org_id, name, created_at)"
                " VALUES (?,?,?)", (org_id, new, _now_iso()))
            conn.execute("UPDATE org_devices SET region=? WHERE org_id=? AND region=?",
                         (new, org_id, old))
            conn.commit()


    def delete_region(self, org_id: str, name: str) -> dict:
        with self._write_lock, self._connect() as conn:
            in_use = conn.execute(
                "SELECT COUNT(*) FROM org_devices"
                " WHERE org_id=? AND region=? AND is_active=1",
                (org_id, name)).fetchone()[0]
            if in_use:
                return {"ok": False,
                        "reason": f"region is used by {in_use} device(s)"}
            conn.execute("DELETE FROM org_regions WHERE org_id=? AND name=?",
                         (org_id, name))
            conn.commit()
            return {"ok": True}


    def list_org_devices(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.device_type, d.region,"
                " d.tags,"
                " d.parent_device_id, d.assigned_node_id, d.maintenance, d.snmp_enabled,"
                " d.snmp_version, d.snmp_community, d.snmp_port, d.gpon_vendor,"
                " d.lat, d.lng, d.pon_port, d.split_ratio, d.split_inputs, d.onu_pon_limit,"
                " d.accuracy_m, d.place_source, d.placed_by, d.placed_at,"
                " d.tree_detached,"
                " d.web_ip, d.web_port, d.web_scheme,"
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
                " (SELECT COUNT(*) FROM onu_optics r WHERE r.device_id = d.id"
                "  AND r.rx_dbm IS NOT NULL) AS onus_rx,"
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
            peers = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='peer'",
                (org_id,)).fetchall()
        backups: dict[int, list[int]] = {}
        for link in links:
            backups.setdefault(link["child_id"], []).append(link["parent_id"])
        peer_ids: dict[int, list[int]] = {}
        for link in peers:
            peer_ids.setdefault(link["child_id"], []).append(link["parent_id"])
            peer_ids.setdefault(link["parent_id"], []).append(link["child_id"])
        assignees = self.device_assignee_ids(org_id)
        feeds = self.org_plant_feed_map(org_id)
        fibre_pons = self.org_fibre_pon_map(org_id, feeds)
        out = [dict(r) for r in rows]
        for d in out:
            d["backup_parents"] = backups.get(d["id"], [])
            d["peer_ids"] = sorted(peer_ids.get(d["id"], []))
            d["assignee_ids"] = assignees.get(d["id"], [])
            d["feed_device_id"] = feeds.get(d["id"])
            d["fibre_pon"] = fibre_pons.get(d["id"])
            d["tags"] = [t for t in (d["tags"] or "").split(",") if t]
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


    def org_plant_feed_map(self, org_id: str) -> dict[int, int | None]:


        parents = self.org_device_parent_map(org_id)
        out = dict(parents)
        for point, feed in self._plant_feed_points(org_id).items():
            if point[0] != "device" or feed[0] != "device":
                continue
            if parents.get(point[1]) is None:
                out[point[1]] = feed[1]
        return out


    def create_org_device(self, org_id: str, clean: dict) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type, region,"
                " tags, parent_device_id, assigned_node_id, gpon_vendor, pon_port,"
                " split_ratio, split_inputs, onu_pon_limit, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["ip_address"], clean["device_type"],
                 clean["region"], clean.get("tags"),
                 clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), clean.get("pon_port"),
                 clean.get("split_ratio"), clean.get("split_inputs"),
                 clean.get("onu_pon_limit"), _now_iso()))
            conn.commit()
            return int(cur.lastrowid)


    def update_org_device(self, org_id: str, device_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET name=?, ip_address=?, device_type=?, region=?,"
                " tags=?, parent_device_id=?, assigned_node_id=?, gpon_vendor=?,"
                " pon_port=?, split_ratio=?, split_inputs=?, onu_pon_limit=?,"
                " tree_detached=CASE WHEN ? IS NULL THEN 0 ELSE tree_detached END"
                " WHERE id=? AND org_id=? AND is_active=1",
                (clean["name"], clean["ip_address"], clean["device_type"], clean["region"],
                 clean.get("tags"),
                 clean["parent_device_id"], clean.get("assigned_node_id"),
                 clean.get("gpon_vendor"), clean.get("pon_port"),
                 clean.get("split_ratio"), clean.get("split_inputs"),
                 clean.get("onu_pon_limit"),
                 clean["parent_device_id"], device_id, org_id))
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
                "UPDATE org_devices SET lat=?, lng=?,"
                " accuracy_m=NULL, place_source=NULL, placed_by=NULL, placed_at=NULL"
                " WHERE id=? AND org_id=? AND is_active=1",
                (lat, lng, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def place_org_device(self, org_id: str, device_id: int, lat: float, lng: float,
                         *, accuracy_m: float | None, source: str,
                         placed_by: str) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET lat=?, lng=?, accuracy_m=?, place_source=?,"
                " placed_by=?, placed_at=datetime('now')"
                " WHERE id=? AND org_id=? AND is_active=1",
                (lat, lng, accuracy_m, source, placed_by, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    @staticmethod
    def _prune_link_route(conn, org_id: str, child_id: int, parent_id: int) -> None:


        conn.execute(
            "DELETE FROM link_routes WHERE org_id=? AND child_id=? AND parent_id=?"
            " AND waypoints IN ('', '[]') AND label_pos IS NULL",
            (org_id, child_id, parent_id))


    def set_link_route(self, org_id: str, child_id: int, parent_id: int,
                       waypoints: list[list[float]], updated_by: str | None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO link_routes (org_id, child_id, parent_id, waypoints,"
                " updated_at, updated_by) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(org_id, child_id, parent_id) DO UPDATE SET"
                " waypoints=excluded.waypoints, updated_at=excluded.updated_at,"
                " updated_by=excluded.updated_by",
                (org_id, child_id, parent_id, json.dumps(waypoints), _now_iso(),
                 updated_by))
            self._prune_link_route(conn, org_id, child_id, parent_id)
            conn.commit()


    _LINK_STYLE_COLS = ("label_pos",)

    def set_link_style(self, org_id: str, child_id: int, parent_id: int,
                       fields: dict, updated_by: str | None) -> None:

        sets, vals = [], []
        for col in self._LINK_STYLE_COLS:
            if col in fields:
                sets.append(f"{col}=?")
                vals.append(fields[col])
        if not sets:
            return
        cols = ", ".join(self._LINK_STYLE_COLS)
        marks = ",".join("?" * len(self._LINK_STYLE_COLS))
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO link_routes (org_id, child_id, parent_id, waypoints,"
                f" {cols}, updated_at, updated_by) VALUES (?,?,?,'[]',{marks},?,?)"
                " ON CONFLICT(org_id, child_id, parent_id) DO UPDATE SET "
                + ", ".join(sets) + ", updated_at=?, updated_by=?",
                (org_id, child_id, parent_id,
                 *(fields.get(c) for c in self._LINK_STYLE_COLS),
                 _now_iso(), updated_by,
                 *vals, _now_iso(), updated_by))
            self._prune_link_route(conn, org_id, child_id, parent_id)
            conn.commit()


    def list_link_routes(self, org_id: str) -> list[dict]:


        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.child_id, r.parent_id, r.waypoints, r.label_pos,"
                " r.updated_at, r.updated_by"
                " FROM link_routes r JOIN org_devices c ON c.id = r.child_id"
                " JOIN org_devices p ON p.id = r.parent_id"
                " WHERE r.org_id=? AND c.org_id=? AND c.is_active=1"
                " AND (c.parent_device_id = r.parent_id OR EXISTS ("
                "   SELECT 1 FROM org_device_links l WHERE l.org_id = r.org_id"
                "   AND l.is_active = 1 AND ("
                "     (l.child_id = r.child_id AND l.parent_id = r.parent_id)"
                "     OR (l.kind = 'peer' AND l.child_id = r.parent_id"
                "         AND l.parent_id = r.child_id))))",
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


    def onu_places_exist(self, org_id: str, macs) -> bool:

        macs = {m for m in macs if m}
        if not macs:
            return True
        with self._connect() as conn:
            found = conn.execute(
                "SELECT COUNT(DISTINCT mac) AS n FROM onu_places"
                " WHERE org_id=? AND mac IN (%s)" % ",".join("?" * len(macs)),
                (org_id, *macs)).fetchone()["n"]
        return found == len(macs)


    @staticmethod
    def _pkey(device_id: int | None, mac: str | None):
        return ("device", device_id) if device_id is not None else ("onu", mac)

    @staticmethod
    def _pdict(device_id: int | None, mac: str | None) -> dict:
        return {"kind": "device" if device_id is not None else "onu",
                "device_id": device_id, "mac": mac}


    def _point_names(self, org_id: str) -> dict:


        from wisp.central import onuroster
        out: dict = {}
        with self._with_norm_mac(self._connect()) as conn:
            for r in conn.execute(
                    "SELECT id, name, device_type, lat, lng FROM org_devices"
                    " WHERE org_id=? AND is_active=1", (org_id,)):
                out[("device", r["id"])] = {
                    "kind": "device", "device_id": r["id"], "mac": None,
                    "name": r["name"], "device_type": r["device_type"],
                    "lat": r["lat"], "lng": r["lng"]}
            for r in conn.execute(
                    "SELECT p.mac, p.label, p.lat, p.lng,"
                    " (SELECT o.name FROM onu_optics o"
                    "   WHERE o.org_id=p.org_id AND wisp_norm_mac(o.serial)=p.mac"
                    "   LIMIT 1) AS name"
                    " FROM onu_places p WHERE p.org_id=?", (org_id,)):
                out[("onu", r["mac"])] = {
                    "kind": "onu", "device_id": None, "mac": r["mac"],
                    "name": onuroster.display_name(dict(r)),
                    "device_type": None, "lat": r["lat"], "lng": r["lng"]}
        return out


    def cable_org(self, cable_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM org_cables WHERE id=?",
                               (cable_id,)).fetchone()
        return row["org_id"] if row else None


    def _raw_cables(self, conn, org_id: str) -> list[dict]:
        out = []
        for r in conn.execute(
                "SELECT * FROM org_cables WHERE org_id=? ORDER BY name, id",
                (org_id,)):
            d = dict(r)
            try:
                d["path"] = json.loads(d["path"]) if d["path"] else []
            except (TypeError, ValueError):
                d["path"] = []
            d["a_point"] = self._pkey(d["a_device_id"], d["a_mac"])
            d["b_point"] = self._pkey(d["b_device_id"], d["b_mac"])
            out.append(d)
        return out


    def _raw_joints(self, conn, org_id: str) -> list[dict]:
        out = []
        for r in conn.execute(
                "SELECT * FROM org_fibre_joints WHERE org_id=? ORDER BY id",
                (org_id,)):
            d = dict(r)
            d["point"] = self._pkey(d["device_id"], d["mac"])
            out.append(d)
        return out


    def list_org_cables(self, org_id: str) -> list[dict]:


        with self._connect() as conn:
            cables = self._raw_cables(conn, org_id)
            joints = self._raw_joints(conn, org_id)
            labels: dict[int, dict[str, str]] = {}
            for r in conn.execute(
                    "SELECT cable_id, core_no, label FROM org_cable_cores"
                    " WHERE org_id=? ORDER BY core_no", (org_id,)):
                labels.setdefault(r["cable_id"], {})[str(r["core_no"])] = r["label"]
        points = self._point_names(org_id)
        names = {c["id"]: c["name"] for c in cables}

        plans: dict[int, dict[str, dict]] = {c["id"]: {} for c in cables}
        by_id = {c["id"]: c for c in cables}

        def place(cable_id, core_no, point, entry):
            cable = by_id.get(cable_id)
            if cable is None or core_no is None:
                return
            side = "a" if point == cable["a_point"] else \
                   "b" if point == cable["b_point"] else None
            if side is None:
                return
            plans[cable_id].setdefault(str(core_no), {})[side] = entry

        for j in joints:
            if j["b_cable_id"] is None:
                where = points.get(j["point"], {})
                place(j["a_cable_id"], j["a_core_no"], j["point"],
                      {"terminates": True, "point": where.get("name")})
                continue
            place(j["a_cable_id"], j["a_core_no"], j["point"],
                  {"cable_id": j["b_cable_id"], "cable_name": names.get(j["b_cable_id"]),
                   "core_no": j["b_core_no"]})
            place(j["b_cable_id"], j["b_core_no"], j["point"],
                  {"cable_id": j["a_cable_id"], "cable_name": names.get(j["a_cable_id"]),
                   "core_no": j["a_core_no"]})

        out = []
        for c in cables:
            plan = plans[c["id"]]
            core_labels = labels.get(c["id"], {})
            out.append({
                "id": c["id"], "name": c["name"], "cores": c["cores"],
                "path": c["path"], "notes": c["notes"],
                "length_m": cablepath.length_m(c["path"]),
                "a": {**self._pdict(c["a_device_id"], c["a_mac"]),
                      **{k: v for k, v in (points.get(c["a_point"]) or {}).items()
                         if k in ("name", "device_type", "lat", "lng")}},
                "b": {**self._pdict(c["b_device_id"], c["b_mac"]),
                      **{k: v for k, v in (points.get(c["b_point"]) or {}).items()
                         if k in ("name", "device_type", "lat", "lng")}},
                "plan": plan,
                "labels": core_labels,
                "cores_recorded": len(set(plan) | set(core_labels)),
                "updated_at": c["updated_at"], "updated_by": c["updated_by"],
            })
        return out


    def set_cable_core_label(self, org_id: str, cable_id: int, core_no: int,
                             label: str | None, updated_by: str | None) -> None:

        text = (label or "").strip()
        with self._write_lock, self._connect() as conn:
            if not text:
                conn.execute("DELETE FROM org_cable_cores"
                             " WHERE org_id=? AND cable_id=? AND core_no=?",
                             (org_id, cable_id, core_no))
            else:
                conn.execute(
                    "INSERT INTO org_cable_cores (org_id, cable_id, core_no, label,"
                    " updated_at, updated_by) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(cable_id, core_no) DO UPDATE SET"
                    " label=excluded.label, updated_at=excluded.updated_at,"
                    " updated_by=excluded.updated_by",
                    (org_id, cable_id, core_no, text[:200], _now_iso(), updated_by))
            conn.commit()


    def set_org_cable(self, org_id: str, cable_id: int | None, *, name: str,
                      cores: int | None, notes: str | None,
                      a: dict | None, b: dict | None,
                      updated_by: str | None) -> int:


        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            if cable_id is None:
                cur = conn.execute(
                    "INSERT INTO org_cables (org_id, name, cores, notes,"
                    " a_device_id, a_mac, b_device_id, b_mac,"
                    " created_at, updated_at, updated_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (org_id, name, cores, notes,
                     a["device_id"], a["mac"], b["device_id"], b["mac"],
                     now, now, updated_by))
                conn.commit()
                return int(cur.lastrowid)

            row = conn.execute(
                "SELECT * FROM org_cables WHERE id=? AND org_id=?",
                (cable_id, org_id)).fetchone()
            if row is None:
                raise ValueError("cable not found")
            if cores is None:
                conn.execute("DELETE FROM org_fibre_joints"
                             " WHERE org_id=? AND (a_cable_id=? OR b_cable_id=?)",
                             (org_id, cable_id, cable_id))
                conn.execute("DELETE FROM org_cable_cores"
                             " WHERE org_id=? AND cable_id=?", (org_id, cable_id))
            else:
                over = conn.execute(
                    "SELECT MAX(n) AS n FROM ("
                    "  SELECT a_core_no AS n FROM org_fibre_joints"
                    "   WHERE org_id=? AND a_cable_id=?"
                    "  UNION ALL"
                    "  SELECT b_core_no FROM org_fibre_joints"
                    "   WHERE org_id=? AND b_cable_id=?)",
                    (org_id, cable_id, org_id, cable_id)).fetchone()
                if over and over["n"] is not None and over["n"] > cores:
                    raise ValueError(
                        f"core {over['n']} is in use — clear it before making this"
                        f" cable a {cores}F")
            if a is not None and b is not None:
                for side, end in (("a", a), ("b", b)):
                    was = self._pkey(row[f"{side}_device_id"], row[f"{side}_mac"])
                    if was != self._pkey(end["device_id"], end["mac"]):
                        conn.execute(
                            "DELETE FROM org_fibre_joints WHERE org_id=?"
                            " AND ((device_id IS ? AND mac IS ?))"
                            " AND (a_cable_id=? OR b_cable_id=?)",
                            (org_id, was[1] if was[0] == "device" else None,
                             was[1] if was[0] == "onu" else None,
                             cable_id, cable_id))
                conn.execute(
                    "UPDATE org_cables SET a_device_id=?, a_mac=?, b_device_id=?,"
                    " b_mac=? WHERE id=? AND org_id=?",
                    (a["device_id"], a["mac"], b["device_id"], b["mac"],
                     cable_id, org_id))
            conn.execute(
                "UPDATE org_cables SET name=?, cores=?, notes=?, updated_at=?,"
                " updated_by=? WHERE id=? AND org_id=?",
                (name, cores, notes, now, updated_by, cable_id, org_id))
            conn.commit()
            return cable_id


    def set_cable_path(self, org_id: str, cable_id: int,
                       path: list[list[float]], updated_by: str | None) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_cables SET path=?, updated_at=?, updated_by=?"
                " WHERE id=? AND org_id=?",
                (json.dumps(path) if path else None, _now_iso(), updated_by,
                 cable_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def split_org_cable(self, org_id: str, cable_id: int, *, lat: float, lng: float,
                        name: str | None, updated_by: str | None) -> dict:


        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM org_cables WHERE id=? AND org_id=?",
                               (cable_id, org_id)).fetchone()
            if row is None:
                raise ValueError("cable not found")
            try:
                path = json.loads(row["path"]) if row["path"] else []
            except (TypeError, ValueError):
                path = []

            points = self._point_names(org_id)
            a_key = self._pkey(row["a_device_id"], row["a_mac"])
            b_key = self._pkey(row["b_device_id"], row["b_mac"])

            def pin(key):
                p = points.get(key) or {}
                return (p["lat"], p["lng"]) if p.get("lat") is not None else None

            traced = len(path) >= cablepath.MIN_PATH_POINTS
            if not traced:
                path = [list(p) for p in (pin(a_key), pin(b_key)) if p is not None]
                if len(path) < cablepath.MIN_PATH_POINTS:
                    raise ValueError(
                        "place both ends of this cable, or trace it, before"
                        " opening a closure in it")
            halves = cablepath.split(path, lat, lng)
            if halves is None:
                raise ValueError("that point is at the end of the cable, not along it")
            head, tail = halves

            a_first = cablepath.orient(path, pin(a_key), pin(b_key))
            head_side, tail_side = ("a", "b") if a_first else ("b", "a")

            cut = head[-1]
            if not traced:
                head = tail = []
            now = _now_iso()
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type,"
                " region, parent_device_id, lat, lng, created_at)"
                " VALUES (?,?,'','closure',NULL,NULL,?,?,?)",
                (org_id, name or self._next_closure_name(conn, org_id),
                 cut[0], cut[1], now))
            closure_id = int(cur.lastrowid)

            conn.execute(
                f"UPDATE org_cables SET path=?, {tail_side}_device_id=?,"
                f" {tail_side}_mac=NULL, updated_at=?, updated_by=?"
                " WHERE id=? AND org_id=?",
                (json.dumps(head), closure_id, now, updated_by, cable_id, org_id))
            far = conn.execute(
                "INSERT INTO org_cables (org_id, name, cores, path, notes,"
                " a_device_id, a_mac, b_device_id, b_mac,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, row["name"], row["cores"], json.dumps(tail), row["notes"],
                 closure_id, None,
                 row[f"{tail_side}_device_id"], row[f"{tail_side}_mac"],
                 now, now, updated_by))
            far_id = int(far.lastrowid)

            tail_key = b_key if a_first else a_key
            for col in ("a", "b"):
                conn.execute(
                    f"UPDATE org_fibre_joints SET {col}_cable_id=?"
                    f" WHERE org_id=? AND {col}_cable_id=?"
                    "  AND device_id IS ? AND mac IS ?",
                    (far_id, org_id, cable_id,
                     tail_key[1] if tail_key[0] == "device" else None,
                     tail_key[1] if tail_key[0] == "onu" else None))
            spliced = 0
            if row["cores"]:
                for core in range(1, int(row["cores"]) + 1):
                    conn.execute(
                        "INSERT INTO org_fibre_joints (org_id, device_id, mac,"
                        " a_cable_id, a_core_no, b_cable_id, b_core_no,"
                        " created_at, updated_at, updated_by)"
                        " VALUES (?,?,NULL,?,?,?,?,?,?,?)",
                        (org_id, closure_id, *self._canon(
                            (cable_id, core), (far_id, core)), now, now, updated_by))
                    spliced += 1
            conn.commit()
        return {"cable_id": cable_id, "new_cable_id": far_id,
                "closure_id": closure_id, "spliced": spliced}


    @staticmethod
    def _next_closure_name(conn, org_id: str) -> str:

        n = conn.execute(
            "SELECT COUNT(*) AS n FROM org_devices WHERE org_id=? AND is_active=1"
            " AND device_type='closure'", (org_id,)).fetchone()["n"]
        return f"JC-{n + 1}"


    @staticmethod
    def _canon(a: tuple, b: tuple) -> tuple:
        lo, hi = (a, b) if a <= b else (b, a)
        return (lo[0], lo[1], hi[0], hi[1])


    def delete_org_cable(self, org_id: str, cable_id: int) -> bool:

        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM org_fibre_joints"
                         " WHERE org_id=? AND (a_cable_id=? OR b_cable_id=?)",
                         (org_id, cable_id, cable_id))
            conn.execute("DELETE FROM org_cable_cores"
                         " WHERE org_id=? AND cable_id=?", (org_id, cable_id))
            cur = conn.execute("DELETE FROM org_cables WHERE id=? AND org_id=?",
                               (cable_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_fibre_joint(self, org_id: str, clean: dict,
                        updated_by: str | None) -> dict:

        point = self._pkey(clean["device_id"], clean["mac"])
        a = (clean["a_cable_id"], clean["a_core_no"])
        b = ((clean["b_cable_id"], clean["b_core_no"])
             if clean["b_cable_id"] is not None else None)
        port = ((clean["port_kind"], clean.get("port_no"))
                if clean.get("port_kind") else None)
        with self._write_lock, self._connect() as conn:
            cables = {c["id"]: c for c in self._raw_cables(conn, org_id)}
            joints = self._raw_joints(conn, org_id)
            why = fiber.joint_refusal(a, b, point, cables,
                                      fiber.taken_at(joints, point), port,
                                      fiber.ports_taken_at(joints, point))
            if why:
                return {"ok": False, "refused": why,
                        "reason": fiber.JOINT_REFUSAL_TEXT[why]}
            now = _now_iso()
            cols = self._canon(a, b) if b is not None else (a[0], a[1], None, None)
            cur = conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, port_kind, port_no,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["device_id"], clean["mac"], *cols,
                 clean.get("port_kind"), clean.get("port_no"), now, now,
                 updated_by))
            conn.commit()
            return {"ok": True, "id": int(cur.lastrowid)}


    def take_core_to_box(self, org_id: str, clean: dict,
                         updated_by: str | None) -> dict:


        point = self._pkey(clean["device_id"], clean["mac"])
        far = self._pkey(clean["to"]["device_id"], clean["to"]["mac"])
        a = (clean["a_cable_id"], clean["a_core_no"])
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cables = {c["id"]: c for c in self._raw_cables(conn, org_id)}
            joints = self._raw_joints(conn, org_id)
            taken = fiber.taken_at(joints, point)
            why = fiber.joint_refusal(a, None, point, cables, taken)
            if why:
                return {"ok": False, "refused": why,
                        "reason": fiber.JOINT_REFUSAL_TEXT[why]}
            if not self._point_exists(conn, org_id, clean["to"]):
                return {"ok": False, "refused": "missing_point",
                        "reason": "That box is not in this network any more."}
            far_port = ((clean["port_kind"], clean.get("port_no"))
                        if clean.get("port_kind") else None)
            if far_port and far_port in fiber.ports_taken_at(joints, far):
                return {"ok": False, "refused": "port_taken",
                        "reason": fiber.JOINT_REFUSAL_TEXT["port_taken"]}

            label = (clean["name"] or "")[:fiber.CABLE_NAME_MAX]
            cur = conn.execute(
                "INSERT INTO org_cables (org_id, name, cores, notes,"
                " a_device_id, a_mac, b_device_id, b_mac,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,1,NULL,?,?,?,?,?,?,?)",
                (org_id, label,
                 clean["device_id"], clean["mac"],
                 clean["to"]["device_id"], clean["to"]["mac"], now, now, updated_by))
            tail_id = int(cur.lastrowid)

            conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, created_at, updated_at,"
                " updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["device_id"], clean["mac"],
                 *self._canon(a, (tail_id, 1)), now, now, updated_by))
            conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, port_kind, port_no,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,?,?,1,NULL,NULL,?,?,?,?,?)",
                (org_id, clean["to"]["device_id"], clean["to"]["mac"], tail_id,
                 clean.get("port_kind"), clean.get("port_no"),
                 now, now, updated_by))
            conn.commit()
        return {"ok": True, "cable_id": tail_id, "name": label or None}


    def connect_points(self, org_id: str, clean: dict,
                       updated_by: str | None) -> dict:


        point = self._pkey(clean["device_id"], clean["mac"])
        far = self._pkey(clean["to"]["device_id"], clean["to"]["mac"])
        if point == far:
            return {"ok": False, "refused": "self",
                    "reason": "A cable runs between two points, not from a box"
                              " back to itself."}
        port = ((clean["port_kind"], clean.get("port_no"))
                if clean.get("port_kind") else None)
        stated = ((clean["to_port_kind"], clean.get("to_port_no"))
                  if clean.get("to_port_kind") else None)
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            if not self._point_exists(conn, org_id, clean["to"]):
                return {"ok": False, "refused": "missing_point",
                        "reason": "That box is not in this network any more."}
            joints = self._raw_joints(conn, org_id)
            if port and port in fiber.ports_taken_at(joints, point):
                return {"ok": False, "refused": "port_taken",
                        "reason": fiber.JOINT_REFUSAL_TEXT["port_taken"]}
            taken_far = fiber.ports_taken_at(joints, far)
            if stated and stated in taken_far:
                return {"ok": False, "refused": "port_taken",
                        "reason": fiber.JOINT_REFUSAL_TEXT["port_taken"]}
            far_port = stated or self._sole_input(conn, org_id, clean["to"])
            if far_port and far_port in taken_far:
                far_port = None
            label = (clean.get("name") or "")[:fiber.CABLE_NAME_MAX]
            cur = conn.execute(
                "INSERT INTO org_cables (org_id, name, cores, notes,"
                " a_device_id, a_mac, b_device_id, b_mac,"
                " created_at, updated_at, updated_by) VALUES (?,?,1,NULL,?,?,?,?,?,?,?)",
                (org_id, label,
                 clean["device_id"], clean["mac"],
                 clean["to"]["device_id"], clean["to"]["mac"], now, now, updated_by))
            cable_id = int(cur.lastrowid)
            for end, p in ((clean, port),
                           (clean["to"], far_port)):
                conn.execute(
                    "INSERT INTO org_fibre_joints (org_id, device_id, mac,"
                    " a_cable_id, a_core_no, b_cable_id, b_core_no, port_kind,"
                    " port_no, created_at, updated_at, updated_by)"
                    " VALUES (?,?,?,?,1,NULL,NULL,?,?,?,?,?)",
                    (org_id, end["device_id"], end["mac"], cable_id,
                     p[0] if p else None, p[1] if p else None,
                     now, now, updated_by))
            conn.commit()
        return {"ok": True, "cable_id": cable_id, "name": label or None,
                "far_port": fiber.port_label(*far_port) if far_port else None}


    def _sole_input(self, conn, org_id: str, end: dict):

        if end.get("device_id") is None:
            return None
        row = conn.execute(
            "SELECT device_type, split_inputs FROM org_devices"
            " WHERE id=? AND org_id=?", (end["device_id"], org_id)).fetchone()
        if row is None or row["device_type"] != "splitter":
            return None
        return ("in", 1) if (row["split_inputs"] or 1) == 1 else None


    def _point_exists(self, conn, org_id: str, end: dict) -> bool:

        if end["device_id"] is not None:
            return conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (end["device_id"], org_id)).fetchone() is not None
        return conn.execute(
            "SELECT 1 FROM onu_places WHERE org_id=? AND mac=?",
            (org_id, end["mac"])).fetchone() is not None


    def splice_through(self, org_id: str, clean: dict,
                       updated_by: str | None) -> dict:


        point = self._pkey(clean["device_id"], clean["mac"])
        with self._write_lock, self._connect() as conn:
            cables = {c["id"]: c for c in self._raw_cables(conn, org_id)}
            a_cable = cables.get(clean["a_cable_id"])
            b_cable = cables.get(clean["b_cable_id"])
            if not a_cable or not b_cable:
                raise ValueError("cable not found")
            if not a_cable["cores"] or not b_cable["cores"]:
                return {"ok": True, "spliced": 0, "skipped": 0,
                        "reason": "record a fibre count on both cables first"}
            joints = self._raw_joints(conn, org_id)
            taken = fiber.taken_at(joints, point)
            now, spliced, skipped = _now_iso(), 0, 0
            for core in range(1, min(int(a_cable["cores"]), int(b_cable["cores"])) + 1):
                a, b = (a_cable["id"], core), (b_cable["id"], core)
                why = fiber.joint_refusal(a, b, point, cables, taken)
                if why:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO org_fibre_joints (org_id, device_id, mac,"
                    " a_cable_id, a_core_no, b_cable_id, b_core_no, created_at,"
                    " updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (org_id, clean["device_id"], clean["mac"], *self._canon(a, b),
                     now, now, updated_by))
                taken[a] = taken[b] = {"pending": True}
                spliced += 1
            conn.commit()
        return {"ok": True, "spliced": spliced, "skipped": skipped}


    def clear_fibre_joint(self, org_id: str, clean: dict) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM org_fibre_joints WHERE org_id=?"
                " AND device_id IS ? AND mac IS ?"
                " AND ((a_cable_id=? AND a_core_no=?)"
                "   OR (b_cable_id=? AND b_core_no=?))",
                (org_id, clean["device_id"], clean["mac"],
                 clean["cable_id"], clean["core_no"],
                 clean["cable_id"], clean["core_no"]))
            conn.commit()
            return cur.rowcount > 0


    def device_pon_ports(self, conn, org_id: str, device_id: int) -> list[int]:


        return fiber.pon_ports(
            roster=[r["pon_port"] for r in conn.execute(
                "SELECT DISTINCT pon_port FROM onu_optics WHERE device_id=?",
                (device_id,))],
            interfaces=[r["if_name"] for r in conn.execute(
                "SELECT DISTINCT if_name FROM switch_ports WHERE device_id=?",
                (device_id,))],
            recorded=[r["port_no"] for r in conn.execute(
                "SELECT DISTINCT port_no FROM org_fibre_joints"
                " WHERE org_id=? AND device_id=? AND port_kind='pon'"
                " AND port_no IS NOT NULL", (org_id, device_id))])


    def device_ports(self, conn, org_id: str, device_id: int) -> tuple[list[int],
                                                                      dict[int, str]]:


        names: dict[int, str] = {}
        for r in conn.execute(
                "SELECT if_name FROM switch_ports WHERE device_id=?"
                " ORDER BY if_index", (device_id,)):
            n = fiber.if_port_no(r["if_name"])
            if n is not None:
                names.setdefault(n, r["if_name"])
        recorded = {int(r["port_no"]) for r in conn.execute(
            "SELECT DISTINCT port_no FROM org_fibre_joints"
            " WHERE org_id=? AND device_id=? AND port_kind='port'"
            " AND port_no IS NOT NULL", (org_id, device_id))}
        return sorted(set(names) | recorded), names


    def org_device_ports(self, org_id: str) -> dict[int, list[dict]]:


        pons: dict[int, list[str]] = {}
        ifs: dict[int, list[str]] = {}
        rec: dict[int, list[tuple[str, int]]] = {}
        with self._connect() as conn:
            for r in conn.execute(
                    "SELECT DISTINCT device_id, pon_port FROM onu_optics"
                    " WHERE org_id=? AND pon_port IS NOT NULL", (org_id,)):
                pons.setdefault(r["device_id"], []).append(r["pon_port"])
            for r in conn.execute(
                    "SELECT device_id, if_name FROM switch_ports"
                    " WHERE org_id=? AND if_name IS NOT NULL ORDER BY if_index",
                    (org_id,)):
                ifs.setdefault(r["device_id"], []).append(r["if_name"])
            for r in conn.execute(
                    "SELECT DISTINCT device_id, port_kind, port_no"
                    " FROM org_fibre_joints WHERE org_id=? AND device_id IS NOT NULL"
                    " AND port_kind IS NOT NULL AND port_no IS NOT NULL", (org_id,)):
                rec.setdefault(r["device_id"], []).append(
                    (r["port_kind"], r["port_no"]))
            boxes = conn.execute(
                "SELECT id, device_type, split_ratio, split_inputs FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,)).fetchall()

        out: dict[int, list[dict]] = {}
        for b in boxes:
            dtype, did = b["device_type"], b["id"]
            names: dict[int, str] = {}
            numbered: list[int] = []
            if dtype == "OLT":
                slots = fiber.port_slots(dtype, pons=fiber.pon_ports(
                    roster=pons.get(did, []), interfaces=ifs.get(did, []),
                    recorded=[n for k, n in rec.get(did, []) if k == "pon"]))
            elif dtype == "splitter" or dtype in fiber.ENCLOSURE_TYPES:
                slots = fiber.port_slots(dtype, split_ratio=b["split_ratio"],
                                         split_inputs=b["split_inputs"])
            else:
                for name in ifs.get(did, []):
                    n = fiber.if_port_no(name)
                    if n is not None:
                        names.setdefault(n, name)
                numbered = sorted(set(names) | {n for k, n in rec.get(did, [])
                                                if k == "port"})
                slots = fiber.port_slots(dtype, ports=numbered)
            if not slots:
                continue
            out[did] = [{"kind": k, "no": n, "label": fiber.port_label(k, n),
                         "device_label": names.get(n) if k == "port" else None}
                        for k, n in slots]
        return out


    def point_fibre(self, org_id: str, *, device_id: int | None = None,
                    mac: str | None = None) -> dict:


        point = self._pkey(device_id, mac)
        with self._connect() as conn:
            cables = self._raw_cables(conn, org_id)
            joints = [j for j in self._raw_joints(conn, org_id)
                      if j["point"] == point]
            labels: dict[int, dict[str, str]] = {}
            for r in conn.execute(
                    "SELECT cable_id, core_no, label FROM org_cable_cores"
                    " WHERE org_id=?", (org_id,)):
                labels.setdefault(r["cable_id"], {})[str(r["core_no"])] = r["label"]
            box = conn.execute(
                "SELECT device_type, split_ratio, split_inputs FROM org_devices"
                " WHERE id=? AND org_id=?", (device_id, org_id)).fetchone() \
                if device_id is not None else None
            dtype = box["device_type"] if box else None
            port_names: dict[int, str] = {}
            if box and dtype not in fiber.ENCLOSURE_TYPES and dtype != "splitter" \
                    and dtype != "OLT":
                numbered, port_names = self.device_ports(conn, org_id, device_id)
            else:
                numbered = []
            slots = fiber.port_slots(
                dtype,
                split_ratio=box["split_ratio"] if box else None,
                split_inputs=box["split_inputs"] if box else None,
                pons=(self.device_pon_ports(conn, org_id, device_id)
                      if box and dtype == "OLT" else None),
                ports=numbered,
            ) if box else []
            drops = {}
            if box and box["device_type"] == "splitter":
                for r in conn.execute(
                        "SELECT d.mac, d.leg_no, p.label FROM onu_drops d"
                        " LEFT JOIN onu_places p ON p.org_id=d.org_id AND p.mac=d.mac"
                        " WHERE d.org_id=? AND d.passive_id=?", (org_id, device_id)):
                    drops.setdefault(r["leg_no"], []).append(
                        {"mac": r["mac"], "name": r["label"]})
        points = self._point_names(org_id)
        feed = self._plant_feed_points(org_id, cables)
        here = points.get(point) or self._pdict(device_id, mac)

        landing = []
        for c in cables:
            if point not in (c["a_point"], c["b_point"]):
                continue
            far = fiber.other_end(c, point)
            landing.append({
                "cable_id": c["id"], "name": c["name"], "cores": c["cores"],
                "end": "a" if point == c["a_point"] else "b",
                "far": points.get(far) or {},
                "labels": labels.get(c["id"], {}),
                "plumbing": fiber.is_plumbing(c),
                "side": "feed" if feed.get(point) == far else "onward",
            })
        landing.sort(key=lambda c: (c["side"] != "feed", c["name"] or "",
                                    c["cable_id"]))
        unplaced_drops = drops.pop(None, []) if drops else []
        return {
            "point": here,
            "cables": landing,
            "ports": [{"kind": kind, "no": no,
                       "label": fiber.port_label(kind, no),
                       "device_label": port_names.get(no) if kind == "port" else None,
                       "drops": drops.get(no, []) if kind == "leg" else []}
                      for kind, no in slots],
            "port_add": (None if (dtype == "splitter" and box["split_ratio"])
                         else fiber.port_kind_for(dtype)) if box else None,
            "undrawn": self._undrawn_here(org_id, point, cables, points),
            "unplaced_drops": unplaced_drops,
            "joints": [{"id": j["id"], "a_cable_id": j["a_cable_id"],
                        "a_core_no": j["a_core_no"], "b_cable_id": j["b_cable_id"],
                        "b_core_no": j["b_core_no"],
                        "port_kind": j["port_kind"], "port_no": j["port_no"]}
                       for j in joints],
        }


    def _undrawn_here(self, org_id: str, point, cables: list[dict],
                      points: dict) -> list[dict]:


        if point is None or (point[0] if isinstance(point, tuple) else None) != "device":
            return []
        device_id = point[1]
        declared: list[tuple] = []
        with self._connect() as conn:
            for r in conn.execute(
                    "SELECT id, name, device_type FROM org_devices"
                    " WHERE org_id=? AND parent_device_id=? AND is_active=1"
                    " ORDER BY name", (org_id, device_id)):
                declared.append(((("device", device_id)), ("device", r["id"]),
                                 "feeds"))
            up = conn.execute(
                "SELECT parent_device_id FROM org_devices WHERE id=? AND org_id=?",
                (device_id, org_id)).fetchone()
            if up and up["parent_device_id"]:
                declared.append((("device", device_id),
                                 ("device", up["parent_device_id"]), "fed by"))
            hops = {("device", r["id"]) for r in conn.execute(
                "SELECT id FROM org_devices WHERE org_id=? AND device_type IN"
                f" ({','.join('?' * len(fiber.ENCLOSURE_TYPES))})",
                (org_id, *fiber.ENCLOSURE_TYPES))}
        missing = fiber.undrawn([(a, b) for a, b, _ in declared], cables, hops)
        want = {frozenset(p) for p in missing}
        out = []
        for a, b, rel in declared:
            if frozenset((a, b)) not in want:
                continue
            want.discard(frozenset((a, b)))
            far = points.get(b) or {}
            out.append({"far": far, "relation": rel})
        return out


    def org_fibre_pon_map(self, org_id: str,
                          feeds: dict[int, int | None] | None = None) -> dict[int, dict]:


        with self._connect() as conn:
            cables = self._raw_cables(conn, org_id)
            joints = self._raw_joints(conn, org_id)
        reached = fiber.pon_of_points(cables, joints)
        out: dict[int, dict] = {}
        for point, src in reached.items():
            if point[0] != "device":
                continue
            if src is None:
                out[point[1]] = {"olt_id": None, "pon_no": None,
                                 "source": "fibre", "ambiguous": True}
            elif src[0][0] == "device":
                out[point[1]] = {"olt_id": src[0][1], "pon_no": src[1],
                                 "source": "fibre", "ambiguous": False}
        feeds = self.org_plant_feed_map(org_id) if feeds is None else feeds
        for device_id in list(feeds):
            if device_id in out:
                continue
            seen, cur = {device_id}, feeds.get(device_id)
            while cur is not None and cur not in seen:
                if cur in out:
                    out[device_id] = {**out[cur], "source": "inherited",
                                      "via_device_id": cur}
                    break
                seen.add(cur)
                cur = feeds.get(cur)
        return out


    def trace_fibre(self, org_id: str, cable_id: int, core_no: int) -> dict:

        with self._connect() as conn:
            cables = self._raw_cables(conn, org_id)
            joints = self._raw_joints(conn, org_id)
        result = fiber.trace(cables, joints, cable_id, core_no)
        points = self._point_names(org_id)
        names = {c["id"]: c for c in cables}

        def at(key):
            p = points.get(key) or {}
            return {"kind": key[0], "device_id": key[1] if key[0] == "device" else None,
                    "mac": key[1] if key[0] == "onu" else None,
                    "name": p.get("name"), "lat": p.get("lat"), "lng": p.get("lng")}

        for hop in result["hops"]:
            cable = names.get(hop["cable_id"], {})
            hop["cable_name"] = cable.get("name")
            hop["cores"] = cable.get("cores")
            hop["from"] = at(hop.pop("from_point"))
            hop["to"] = at(hop.pop("to_point"))
        result["points"] = [at(p) for p in result["points"]]
        result["fault_at"] = at(result["fault_at"]) if result["fault_at"] else None
        result["ends"] = [
            {"point": at(self._pkey(e["device_id"], e["mac"]))} if e else None
            for e in result["ends"]]
        return result


    def _plant_feed_points(self, org_id: str, cables: list[dict] | None = None) -> dict:

        if cables is None:
            with self._connect() as conn:
                cables = self._raw_cables(conn, org_id)
        parents = self.org_device_parent_map(org_id)
        with self._connect() as conn:
            passive = {r["id"] for r in conn.execute(
                "SELECT id FROM org_devices WHERE org_id=? AND is_active=1"
                " AND device_type IN (%s)" % ",".join("?" * len(_PASSIVE_TYPES)),
                (org_id, *_PASSIVE_TYPES))}
        roots = {("device", d) for d in parents
                 if d not in passive or parents.get(d) is not None}
        return fiber.feed_map([(c["a_point"], c["b_point"]) for c in cables], roots)


    def device_names(self, org_id: str) -> dict[int, str]:
        with self._connect() as conn:
            return {r["id"]: r["name"] for r in conn.execute(
                "SELECT id, name FROM org_devices WHERE org_id=? AND is_active=1",
                (org_id,))}


    def set_org_device_maintenance(self, org_id: str, device_id: int, on: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET maintenance=? WHERE id=? AND org_id=? AND is_active=1",
                (1 if on else 0, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_org_device_tree_detached(self, org_id: str, device_id: int, on: bool) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET tree_detached=? WHERE id=? AND org_id=? AND is_active=1",
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


    def set_org_device_web_access(self, org_id: str, device_id: int, *,
                                  web_ip: str | None, web_port: int | None,
                                  web_scheme: str | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET web_ip=?, web_port=?, web_scheme=?"
                " WHERE id=? AND org_id=? AND is_active=1",
                (web_ip or None, web_port, web_scheme or None, device_id, org_id))
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
            conn.execute(
                "UPDATE switch_ports SET uplink_device_id=NULL"
                " WHERE org_id=? AND uplink_device_id=?", (org_id, device_id))
            conn.execute("DELETE FROM switch_ports WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute(
                "DELETE FROM org_device_links"
                " WHERE org_id=? AND (child_id=? OR parent_id=?)",
                (org_id, device_id, device_id))
            conn.execute(
                "DELETE FROM org_fibre_joints WHERE org_id=? AND (device_id=?"
                " OR a_cable_id IN (SELECT id FROM org_cables WHERE org_id=?"
                "   AND (a_device_id=? OR b_device_id=?))"
                " OR b_cable_id IN (SELECT id FROM org_cables WHERE org_id=?"
                "   AND (a_device_id=? OR b_device_id=?)))",
                (org_id, device_id, org_id, device_id, device_id,
                 org_id, device_id, device_id))
            conn.execute(
                "DELETE FROM org_cable_cores WHERE org_id=? AND cable_id IN ("
                " SELECT id FROM org_cables WHERE org_id=?"
                "  AND (a_device_id=? OR b_device_id=?))",
                (org_id, org_id, device_id, device_id))
            conn.execute(
                "DELETE FROM org_cables"
                " WHERE org_id=? AND (a_device_id=? OR b_device_id=?)",
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
            conn.execute("DELETE FROM onu_web_optics WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM web_optics_status WHERE org_id=? AND device_id=?",
                        (org_id, device_id))
            conn.execute("DELETE FROM olt_optics WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM onu_drops WHERE org_id=? AND passive_id=?",
                         (org_id, device_id))
            conn.execute("DELETE FROM pon_fault_state WHERE org_id=? AND device_id=?",
                         (org_id, device_id))
            conn.execute("DELETE FROM device_snmp_status WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM device_capability WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM device_webui_credentials WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM device_health WHERE device_id=?", (device_id,))
            conn.execute("DELETE FROM pon_capacity_state WHERE org_id=? AND device_id=?",
                         (org_id, device_id))
            conn.execute("DELETE FROM snmp_walks WHERE org_id=? AND device_id=?",
                         (org_id, device_id))
            conn.execute("DELETE FROM org_device_workers WHERE device_id=?",
                         (device_id,))
            conn.execute("DELETE FROM org_devices WHERE id=? AND org_id=?",
                         (device_id, org_id))
            conn.commit()
        return {"ok": True}


    def org_device_topology(self, org_id: str) -> list[dict]:
        placeholders = ",".join("?" for _ in _PASSIVE_TYPES)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, ip_address, region, parent_device_id, assigned_node_id,"
                " snmp_enabled, snmp_version, snmp_community, snmp_port, device_type,"
                " gpon_vendor, web_ip, web_port, web_scheme FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND maintenance=0"
                f" AND (device_type IS NULL OR device_type NOT IN ({placeholders}))"
                " ORDER BY id",
                (org_id, *_PASSIVE_TYPES)).fetchall()
        return [dict(r) for r in rows]


    def get_device_webui_credentials(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT device_id, username, password_enc, auth_mode, updated_by,"
                " updated_at FROM device_webui_credentials"
                " WHERE device_id=? AND org_id=?",
                (device_id, org_id)).fetchone()
        return dict(row) if row else None

    def set_device_webui_credentials(self, org_id: str, device_id: int, *,
                                     username: str, password_enc: str | None,
                                     set_password: bool, auth_mode: str,
                                     updated_by: str) -> bool:
        with self._write_lock, self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
            if not owned:
                return False
            if set_password:
                pw = password_enc or None
            else:
                existing = conn.execute(
                    "SELECT password_enc FROM device_webui_credentials WHERE device_id=?",
                    (device_id,)).fetchone()
                pw = existing["password_enc"] if existing else None
            conn.execute(
                "INSERT INTO device_webui_credentials"
                " (device_id, org_id, username, password_enc, auth_mode, updated_by,"
                "  updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET"
                "   org_id=excluded.org_id, username=excluded.username,"
                "   password_enc=excluded.password_enc, auth_mode=excluded.auth_mode,"
                "   updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (device_id, org_id, username, pw, auth_mode, updated_by, _now_iso()))
            conn.commit()
        return True

    def clear_device_webui_credentials(self, org_id: str, device_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM device_webui_credentials WHERE device_id=? AND org_id=?",
                (device_id, org_id))
            conn.commit()
        return cur.rowcount > 0


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


    @staticmethod
    def _peer_pair(a_id: int, b_id: int) -> tuple[int, int]:
        return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


    def org_device_peer_map(self, org_id: str) -> dict[int, set[int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT child_id, parent_id FROM org_device_links"
                " WHERE org_id=? AND is_active=1 AND kind='peer'", (org_id,)).fetchall()
        out: dict[int, set[int]] = {}
        for r in rows:
            out.setdefault(r["child_id"], set()).add(r["parent_id"])
            out.setdefault(r["parent_id"], set()).add(r["child_id"])
        return out


    def create_peer_link(self, org_id: str, a_id: int, b_id: int) -> None:
        lo, hi = self._peer_pair(a_id, b_id)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO org_device_links (org_id, child_id, parent_id,"
                " kind) VALUES (?,?,?,'peer')", (org_id, lo, hi))
            conn.commit()


    def delete_peer_link(self, org_id: str, a_id: int, b_id: int) -> bool:
        lo, hi = self._peer_pair(a_id, b_id)
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM org_device_links WHERE org_id=? AND child_id=?"
                " AND parent_id=? AND kind='peer'", (org_id, lo, hi))
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
