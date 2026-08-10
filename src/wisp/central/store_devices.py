"""Device inventory: org_devices, regions, locations, link routes, topology, backup links and redundancy state.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import json

from wisp.central import cablepath, fiber
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
        # Cascades to devices so a rename can't fragment the org's region set;
        # the new name lands declared even if `old` never was.
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
                # How many roster slots carry a real per-ONU Rx figure. The
                # optics badge alone can't answer "is dBm working here": a
                # C-Data/DBC OLT walks a full roster with EVERY rx_dbm NULL, so
                # the optics icon goes green on a box that reports no optical
                # power at all. Counted off the live table rather than stored on
                # olt_optics because the web scrape (central/weboptics.py) folds
                # Rx in on its OWN clock — a count stamped by the SNMP sweep
                # would read zero for up to 15 minutes after a scrape landed.
                # Counted over the raw table (zombie slots included, since
                # onu_optics never deletes) — this is a CAPABILITY signal, "does
                # a dBm figure exist for this OLT at all", and the reader pairs
                # it with optics_updated_at for the freshness half.
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
        # ONE canonical row per cross-link (lo, hi) expands SYMMETRICALLY here, so
        # each end lists the other regardless of which device it was declared from
        peer_ids: dict[int, list[int]] = {}
        for link in peers:
            peer_ids.setdefault(link["child_id"], []).append(link["parent_id"])
            peer_ids.setdefault(link["parent_id"], []).append(link["child_id"])
        # Who is EXPLICITLY on the hook for paging about this device (never the
        # inherited set — see store_assign.device_assignee_ids). Rides the device
        # list because the panel that edits it already has the row, and because a
        # separate fetch would let the two disagree about who is responsible.
        # PAGING only: nothing reads this to decide what a session may see.
        assignees = self.device_assignee_ids(org_id)
        # WHAT FEEDS THIS BOX PHYSICALLY, which since 2026-08-09 is no longer the
        # same question as what its `parent_device_id` says. Placing a box stopped
        # asking what feeds it — you record the fibre afterwards, which is the
        # order the work happens in — so the plant chain has to be able to come
        # from a run. Shipped on the row rather than derived in the browser so the
        # cumulative split, the PON a splitter sits on and central's own
        # branch-fault verdict all walk ONE chain; two derivations of "what feeds
        # this" is how a panel and a page end up naming different boxes.
        #
        # It NEVER shadows `parent_device_id`, which rides alongside unchanged:
        # that column is the declared topology, it is what the engine and cycle
        # validation read, and a splice must not be able to move it.
        feeds = self.org_plant_feed_map(org_id)
        out = [dict(r) for r in rows]
        for d in out:
            d["backup_parents"] = backups.get(d["id"], [])
            d["peer_ids"] = sorted(peer_ids.get(d["id"], []))
            d["assignee_ids"] = assignees.get(d["id"], [])
            d["feed_device_id"] = feeds.get(d["id"])
            # stored comma-joined; the wire carries a real list
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
        """What feeds each box PHYSICALLY: the declared parent, else the fibre.

        Deliberately NOT `org_device_parent_map`, although it answers a question
        that sounds like the same one. That map is the DECLARED topology and it
        is what cycle validation and the engine are checked against — it must
        keep meaning exactly "what somebody typed", or a recorded splice could
        start deciding what pages.

        This one is the PLANT chain: the thing a cumulative split, a PON
        inheritance and a branch-fault verdict walk. Since 2026-08-09 a box can
        be recorded with no parent at all — placing one stopped asking what feeds
        it, because the honest answer arrives later, when a core is pulled into
        it — so the chain has to be able to come from the glass.

        DECLARED WINS. Every splitter on the live fleet was entered under the old
        flow and carries a parent; reading the fibre first would let a
        half-finished cable record quietly re-route a chain that was already
        right. The fibre only fills gaps.

        A feed that arrives THROUGH a customer is dropped rather than reported.
        Daisy-chaining a lane is real and the walk follows it correctly, but this
        map is device→device — it is what a cumulative split and a PON
        inheritance walk — and there is no device id to name a subscriber with.
        No feed is the honest answer there, and it is the answer that map already
        has for anything the walk never reached.
        """
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
                # tree_detached only means something under a parent, and the row
                # menu offering it hides once there isn't one — so clearing it
                # here is what keeps the flag reachable (an operator who parents
                # the device again gets the plain nested row, not a stale lift)
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
        """The DESKTOP placement path: a click on the map, a pin drag, or a clear.

        Provenance is WIPED, not preserved. A hand-placed pin carries no accuracy
        and was not taken anywhere near the device, so keeping a field capture's
        `accuracy_m` here would leave the map claiming a 9 m GPS fix for a point
        somebody dragged across a village. Losing the stamp is correct: the owner
        moving a pin IS the newer claim, and "unknown provenance" is the honest
        reading of it."""
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
        """The FIELD placement path: somebody standing at the device.

        Deliberately cannot clear a pin — lat/lng are non-optional here, so the
        worker-facing route has no way to reach the both-null delete branch that
        `clean_location_payload` allows. Provenance is stamped on every write
        because that is the entire reason this method exists separately."""
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
        """Drop a row that no longer carries anything.

        "Empty" is every column being empty — clearing the waypoints off a link
        whose chip somebody dragged must not take that position with it. Every
        column added here has to join this list or the row survives as an
        invisible ghost.

        The dead plant columns are NOT tested any more, and could not be: a DB
        created since the fibre rebuild has never had them. They are cleared by
        that rebuild in the DBs that do."""
        conn.execute(
            "DELETE FROM link_routes WHERE org_id=? AND child_id=? AND parent_id=?"
            " AND waypoints IN ('', '[]') AND label_pos IS NULL",
            (org_id, child_id, parent_id))


    def set_link_route(self, org_id: str, child_id: int, parent_id: int,
                       waypoints: list[list[float]], updated_by: str | None) -> None:
        """Upsert the drawn cable path for one link; an empty list clears it."""
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


    #: everything on a link_routes row that is not its geometry — the cable
    #: record (what the span physically IS) and the cartography (how it draws).
    #: One list, used for both halves of the sparse upsert below, so a column
    #: added to the INSERT can't be forgotten in the UPDATE and silently refuse
    #: to save on a link that already had a row.
    #: The cable ROW went first (2026-08-08 — the sheath is `org_cables` and the
    #: fibre count lives there), and the MEMBERSHIP followed it on 2026-08-09:
    #: fibre is its own graph now, with its own ends, and it needs no topology
    #: link at all. What is left here is pure cartography — where the operator
    #: dragged the chip — which really is a property of the drawn line and of
    #: nothing else. `color`, `cores`, `cable_id` and `core_no` survive as dead
    #: columns in upgraded DBs, the ntfy-topics convention.
    _LINK_STYLE_COLS = ("label_pos",)

    def set_link_style(self, org_id: str, child_id: int, parent_id: int,
                       fields: dict, updated_by: str | None) -> None:
        """Upsert a link's map styling.

        A SPARSE update, like the theme overrides: only the keys present in
        `fields` are written, so moving a label can't straighten a route.
        Creates the row on a link with no drawn geometry and prunes it back out
        when nothing is left.
        """
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
        """A link's CARTOGRAPHY: where its chip was dragged, and any legacy path.

        Since 2026-08-09 a topology link carries NO plant record. It is the
        monitoring dependency — what suppression and paging read — and it is not a
        cable; whether glass runs between those two boxes is `org_cables`, which
        needs no link to exist and draws itself. A link with no cable recorded
        therefore draws as a dotted chord, which is the honest state and doubles
        as the to-do list while a fleet re-lays its plant.

        Only routes whose link still exists: a re-parented child leaves its old
        route row dangling — invisible here, overwritten or deleted later.

        The `link_routes` key runs parent_id → child_id because that is the
        WAYPOINT ORDER, and a cross-link has no real parent: the map draws a peer
        from the lower device id, so its geometry is keyed (child=higher,
        parent=lower) — the opposite of `org_device_links`' (min, max)
        canonicalization. Hence the either-order match on peer rows. Keeping
        "waypoints run parent→child" literally true for every link kind is worth
        more than key-order agreement between the two tables.
        """
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


    # ----- fibre plant -------------------------------------------------------
    #
    # A cable is one sheath SEGMENT between two FIBRE POINTS, and a joint is what
    # one of its cores is attached to at one of those points. Neither is a device:
    # no state, no FSM, no outage, absent from org_device_topology, read by no
    # alerting shell — the same standing a splitter's split ratio has. Everything
    # below is therefore structurally incapable of paging a fleet.
    #
    # A POINT is a device OR a subscriber, carried as a nullable pair and handled
    # here as an opaque key. `fiber.py` never looks inside one, which is what lets
    # its walks be tested against plain tuples.

    def onu_places_exist(self, org_id: str, macs) -> bool:
        """Every one of these subscribers has a record in this org.

        The scoping check for the subscriber half of a fibre point, and the exact
        counterpart of `device_org` for the other half: a MAC in a request body is
        just a string, so this is what stops one org's cable claiming to land on
        another org's customer. A MISSING record is refused rather than created —
        a scrape can never add a subscriber and neither can this, and a cable
        landing on a typo'd sticker would draw to a point with nothing behind it.
        """
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
        """Every point in one org: what it is called and where it is.

        ONE read for both kinds, because every surface here needs both and a
        caller that had to remember to look up subscribers separately would
        eventually forget on one screen — which is exactly how a typed customer
        name once reached the DB and rendered nowhere.

        A subscriber's name goes through `onuroster.display_name`, never off
        `onu_places.label` alone, so a cable end and the Optical tab call the same
        customer the same thing — and the walked name is joined in through the
        registered `wisp_norm_mac`, the one normalizer, so SQL identity cannot
        drift from Python identity and silently stop matching a sticker.
        """
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
        """The org a cable belongs to, re-derived from the row like every other
        `org_devices` write scope — a body's `org_id` is never trusted."""
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
        """Every cable, with its two ends resolved and its core plan worked out.

        THE CORE PLAN IS THE POINT OF THIS READ. A cable panel's job is to answer
        "what is on each fibre", and that is per-core AND per-END: core 3 might be
        spliced onward at one end and land on a splitter at the other. Composing
        it here rather than in the browser is the same rule the geometry follows —
        the panel, the tray and the map must not each derive their own version of
        one sentence.

        `cores_recorded` counts a core that has ANYTHING against it: a joint at
        either end, or a label. Counting only labels once printed "0 of 12 cores
        recorded" directly above two cores that were plainly recorded, which is
        the count-agreement rule broken inside a single card. It stays RECORDED
        and never "used": the rest are not spare, nobody wrote them down.
        """
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

        # Which end of each cable a joint sits at, so the plan can be built in one
        # pass. A joint's point is one of the cable's own two ends by construction
        # (`fiber.joint_refusal` → absent), so anything else here is a legacy row
        # and is simply not placed.
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
        """What one fibre CARRIES — free text, and sparse: blank deletes the row.

        Deliberately separate from where the core RUNS, which is derived from the
        joints and can never be typed. One is the operator's claim about purpose
        ("BSNL leased line"), the other is the record's statement about geometry,
        and a panel that let them be entered the same way would make a note look
        like a fact.
        """
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
        """Create or update a cable. Returns its id.

        TWO REFUSALS, both about not silently invalidating something already
        recorded:

        SHRINKING THE COUNT is refused while a joint uses a strand above the new
        one. A 24F re-recorded as a 12F with core 19 spliced somewhere would leave
        a joint naming a fibre the cable no longer has — and it would render, with
        a tube and a colour, in full confidence. Clearing the count entirely is a
        different statement and clears every joint and label with it, in the same
        write.

        MOVING AN END discards the joints made at the end that moved. A splice is
        a fact about a particular closure; the fibres that were joined at the old
        one are not joined at the new one, and carrying them across would invent a
        splice nobody made. Guarded on the end actually CHANGING, so re-saving a
        cable to rename it is idempotent — the same shape as `set_onu_drops`
        discarding a traced route only on a real re-home.
        """
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
        """Where the glass runs. An empty list clears the route.

        Nothing else has to move with it. Under the model this replaced, retracing
        a street re-snapped every tap on it and could re-route a dozen spans that
        had no geometry of their own; now the cable IS the line, so a retrace is
        one column and the joints at its ends do not care where it went in
        between.
        """
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
        """Open the sheath here: cut the cable in two and splice every core through.

        THIS IS WHAT KEEPS SEGMENT-PER-SPAN FROM BEING A TAX. A cable's ends are
        recorded now, so a box tapped halfway down a street has to become a real
        end — and asking an operator to redraw the street to achieve that is how a
        plant record stops being kept. One click does what the crew does: open the
        closure, put a coupler in it, and fusion-splice the tray straight through.

        EVERY CORE IS SPLICED, not just the ones with something recorded against
        them, and that is a deliberate reading of what the gesture means. Splitting
        a sheath must not change what any fibre does — a trace that ran down this
        street before must still run down it after — and a core left discontinuous
        would silently change the answer for every fibre nobody had got round to
        recording. It is the physical default and it is editable: the tray shows
        all of them and any one can be cleared. With no fibre count recorded there
        is nothing to enumerate, so nothing is spliced and the caller is told.

        The new coupler is an ordinary passive `org_devices` row, so it inherits
        pins, the tree, regions and every other piece of shared machinery — and,
        being passive, it is excluded from `org_device_topology` and cannot reach
        the engine.
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM org_cables WHERE id=? AND org_id=?",
                               (cable_id, org_id)).fetchone()
            if row is None:
                raise ValueError("cable not found")
            try:
                path = json.loads(row["path"]) if row["path"] else []
            except (TypeError, ValueError):
                path = []
            if len(path) < cablepath.MIN_PATH_POINTS:
                raise ValueError("trace this cable before opening a coupler on it")
            halves = cablepath.split(path, lat, lng)
            if halves is None:
                raise ValueError("that point is at the end of the cable, not along it")
            head, tail = halves

            points = self._point_names(org_id)
            a_key = self._pkey(row["a_device_id"], row["a_mac"])
            b_key = self._pkey(row["b_device_id"], row["b_mac"])

            def pin(key):
                p = points.get(key) or {}
                return (p["lat"], p["lng"]) if p.get("lat") is not None else None

            # Which recorded end the HEAD belongs to. Measured, never stored — a
            # cable's vertices are in the order somebody drew them.
            a_first = cablepath.orient(path, pin(a_key), pin(b_key))
            head_side, tail_side = ("a", "b") if a_first else ("b", "a")

            cut = head[-1]
            now = _now_iso()
            cur = conn.execute(
                "INSERT INTO org_devices (org_id, name, ip_address, device_type,"
                " region, parent_device_id, lat, lng, created_at)"
                " VALUES (?,?,'','coupler',NULL,NULL,?,?,?)",
                (org_id, name or self._next_coupler_name(conn, org_id),
                 cut[0], cut[1], now))
            coupler_id = int(cur.lastrowid)

            # The original keeps its head-side end and its id, so anything already
            # pointing at this cable still resolves; the far half becomes a new row.
            conn.execute(
                f"UPDATE org_cables SET path=?, {tail_side}_device_id=?,"
                f" {tail_side}_mac=NULL, updated_at=?, updated_by=?"
                " WHERE id=? AND org_id=?",
                (json.dumps(head), coupler_id, now, updated_by, cable_id, org_id))
            far = conn.execute(
                "INSERT INTO org_cables (org_id, name, cores, path, notes,"
                " a_device_id, a_mac, b_device_id, b_mac,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, row["name"], row["cores"], json.dumps(tail), row["notes"],
                 coupler_id, None,
                 row[f"{tail_side}_device_id"], row[f"{tail_side}_mac"],
                 now, now, updated_by))
            far_id = int(far.lastrowid)

            # Joints made at the far end were made on THAT closure and belong to
            # the half that still reaches it.
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
                        (org_id, coupler_id, *self._canon(
                            (cable_id, core), (far_id, core)), now, now, updated_by))
                    spliced += 1
            conn.commit()
        return {"cable_id": cable_id, "new_cable_id": far_id,
                "coupler_id": coupler_id, "spliced": spliced}


    @staticmethod
    def _next_coupler_name(conn, org_id: str) -> str:
        """`JC-n`, counting the couplers that exist rather than the ones ever made.

        A survey names dozens of these and none of the names mean anything — what
        an operator wants is for the box to already have one so they can carry on
        placing the next. Renaming is one field away in the same sheet.
        """
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM org_devices WHERE org_id=? AND is_active=1"
            " AND device_type='coupler'", (org_id,)).fetchone()["n"]
        return f"JC-{n + 1}"


    @staticmethod
    def _canon(a: tuple, b: tuple) -> tuple:
        """One splice is one row whichever fibre the operator picked up first."""
        lo, hi = (a, b) if a <= b else (b, a)
        return (lo[0], lo[1], hi[0], hi[1])


    def delete_org_cable(self, org_id: str, cable_id: int) -> bool:
        """Delete a cable and everything that was only true because of it.

        The joints go because a splice names two fibres and one of them has just
        stopped existing; the core labels go because a core is a position inside a
        particular sheath. Nothing else in the schema points here — a cable is not
        a device and never enters the topology — so this cannot orphan monitoring
        state or change what pages.
        """
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


    # ----- joints ------------------------------------------------------------

    def set_fibre_joint(self, org_id: str, clean: dict,
                        updated_by: str | None) -> dict:
        """Join two fibres at a point, or take one out to the equipment there.

        The physics lives in `fiber.joint_refusal`, read ONCE here, because it
        needs the cables and the joints already made — and putting half of it in
        the payload validator is how two callers end up enforcing two different
        rules. What comes back names the refusal so the tray can say which one it
        is; a bare 400 on a splice tray is indistinguishable from a broken button.
        """
        point = self._pkey(clean["device_id"], clean["mac"])
        a = (clean["a_cable_id"], clean["a_core_no"])
        b = ((clean["b_cable_id"], clean["b_core_no"])
             if clean["b_cable_id"] is not None else None)
        with self._write_lock, self._connect() as conn:
            cables = {c["id"]: c for c in self._raw_cables(conn, org_id)}
            joints = self._raw_joints(conn, org_id)
            why = fiber.joint_refusal(a, b, point, cables,
                                      fiber.taken_at(joints, point))
            if why:
                return {"ok": False, "refused": why,
                        "reason": fiber.JOINT_REFUSAL_TEXT[why]}
            now = _now_iso()
            cols = self._canon(a, b) if b is not None else (a[0], a[1], None, None)
            cur = conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, created_at, updated_at,"
                " updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["device_id"], clean["mac"], *cols, now, now,
                 updated_by))
            conn.commit()
            return {"ok": True, "id": int(cur.lastrowid)}


    def take_core_to_box(self, org_id: str, clean: dict,
                         updated_by: str | None) -> dict:
        """Take one core out of a cable to a box standing somewhere ELSE.

        Lays a single-fibre tail between the two points and joins it at both ends,
        in ONE transaction: a half-written tail is a cable running to a box with
        nothing in it, which reads on the map as real plant and in the tray as a
        free core somebody could splice again.

        IT IS A MACRO OVER THE EXISTING WRITES AND MUST STAY ONE. Every row it
        makes is a row the tray can already make by hand, so nothing downstream —
        `trace`, `split_org_cable`, the delete cascade, the tray's own refusals —
        needs to know this shortcut exists. The moment it writes something the
        manual path cannot, there are two models of a tail again.

        THE SOURCE FIBRE IS CHECKED BEFORE THE CABLE IS LAID, or a refused splice
        leaves a tail cable behind as litter — and the operator, seeing a new line
        on the map, would reasonably believe the connection was made.

        The tail is deliberately 1F and deliberately UNTRACED. One core out is one
        strand, and nobody surveys the two metres from a closure to the rack beside
        it: an empty path draws the dashed chord, which is this map's own word for
        "recorded, not walked". An operator who really has an 8F tail lays it
        themselves and splices in the tray, which has always worked — this exists
        for the single strand that had no route through the record at all.
        """
        point = self._pkey(clean["device_id"], clean["mac"])
        far = self._pkey(clean["to"]["device_id"], clean["to"]["mac"])
        a = (clean["a_cable_id"], clean["a_core_no"])
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cables = {c["id"]: c for c in self._raw_cables(conn, org_id)}
            joints = self._raw_joints(conn, org_id)
            taken = fiber.taken_at(joints, point)
            # `b=None` here asks exactly the right question of the shared rule:
            # is THIS fibre open at THIS point and still free? The tail's own core
            # cannot be taken — the cable does not exist yet.
            why = fiber.joint_refusal(a, None, point, cables, taken)
            if why:
                return {"ok": False, "refused": why,
                        "reason": fiber.JOINT_REFUSAL_TEXT[why]}
            if not self._point_exists(conn, org_id, clean["to"]):
                return {"ok": False, "refused": "missing_point",
                        "reason": "That box is not in this network any more."}

            label = clean["name"] or self._tail_name(
                org_id, far, cables.get(a[0]), clean["a_core_no"])
            cur = conn.execute(
                "INSERT INTO org_cables (org_id, name, cores, notes,"
                " a_device_id, a_mac, b_device_id, b_mac,"
                " created_at, updated_at, updated_by)"
                " VALUES (?,?,1,NULL,?,?,?,?,?,?,?)",
                (org_id, label[:fiber.CABLE_NAME_MAX],
                 clean["device_id"], clean["mac"],
                 clean["to"]["device_id"], clean["to"]["mac"], now, now, updated_by))
            tail_id = int(cur.lastrowid)

            # The splice at THIS end, canonicalised like every other joint so one
            # splice is one row whichever side the caller named first.
            conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, created_at, updated_at,"
                " updated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (org_id, clean["device_id"], clean["mac"],
                 *self._canon(a, (tail_id, 1)), now, now, updated_by))
            # …and the termination at the far one: the fibre goes INTO that box.
            conn.execute(
                "INSERT INTO org_fibre_joints (org_id, device_id, mac, a_cable_id,"
                " a_core_no, b_cable_id, b_core_no, created_at, updated_at,"
                " updated_by) VALUES (?,?,?,?,1,NULL,NULL,?,?,?)",
                (org_id, clean["to"]["device_id"], clean["to"]["mac"], tail_id,
                 now, now, updated_by))
            conn.commit()
        return {"ok": True, "cable_id": tail_id,
                "name": label[:fiber.CABLE_NAME_MAX]}


    def _tail_name(self, org_id: str, far, source: dict | None, core_no: int) -> str:
        """What to call a tail nobody will ever name themselves.

        THE SOURCE CORE IS IN THE NAME, and it is not decoration. An 8-PON OLT fed
        off one closure gets eight tails between the same two points, and named
        for those points alone all eight are the same string — so the OLT's own
        tray offers eight identical rows in its picker and the operator cannot say
        which is which. The core is what tells them apart, it is what a splicer
        would say out loud, and it is stable: a fibre can be tailed only once, so
        two tails can never claim one core.

        IT NAMES THE SOURCE CABLE, NEVER THE SOURCE POINT. The first cut wrote
        `JC-1 core 3 → OLT`, and JC-1 is a CLOSURE — so "core 3" had no cable to
        be a core of, which is the exact half-fact `clean_core_no` refuses to
        store. `a1 core 3 → OLT` says which strand of which sheath, and it is
        shorter, which matters because this string has to survive a picker.

        The SOURCE CABLE is what gets dropped when the name will not fit: the far
        box and the core carry the information, and the sheath is named again by
        the cable's own recorded end.
        """
        dst = (self._point_names(org_id).get(far, {}).get("name")) or "box"
        src = (source or {}).get("name")
        full = f"{src} core {core_no} → {dst}" if src else f"core {core_no} → {dst}"
        if len(full) <= fiber.CABLE_NAME_MAX:
            return full
        return f"core {core_no} → {dst}"[:fiber.CABLE_NAME_MAX]


    def _point_exists(self, conn, org_id: str, end: dict) -> bool:
        """Is this still a real place in this org?

        The API's own scoping answers a deleted device with a 404 before this is
        reached, so on that path it is belt and braces. It is here anyway because
        the guard belongs to whoever LAYS THE CABLE: this method is reachable from
        the admin CLI and from any route added later, a device end carries a real
        foreign key but a subscriber end is a bare MAC with nothing to catch it,
        and the failure it prevents is a sheath recorded as running to a place
        that does not exist.
        """
        if end["device_id"] is not None:
            return conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (end["device_id"], org_id)).fetchone() is not None
        return conn.execute(
            "SELECT 1 FROM onu_places WHERE org_id=? AND mac=?",
            (org_id, end["mac"])).fetchone() is not None


    def splice_through(self, org_id: str, clean: dict,
                       updated_by: str | None) -> dict:
        """Splice every FREE core of one cable straight through to another, 1:1.

        Nine closures in ten are exactly this, and doing it as N separate gestures
        is the difference between a plant record that gets written and one that
        does not — the same argument the bulk drops dialog is built on.

        A core already joined here is SKIPPED, never overwritten. So pressing the
        button twice is safe, and pressing it after some hand-work leaves the
        hand-work alone: the operator who deliberately crossed core 3 to core 7
        does not lose it to a convenience.

        1:1 runs to the SMALLER of the two counts, because there is no honest
        answer for core 13 of a 12F. A cable with no count recorded splices
        nothing — enumerating cores of a sheath nobody has measured would be
        inventing the very fact this schema keeps refusing to invent.
        """
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
        """Undo whatever this fibre is joined to at this point.

        Named by the FIBRE, not by the joint's id, because either side of a splice
        must be able to undo it — and because what the operator is looking at is a
        row in a tray, not a database row.
        """
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


    def point_fibre(self, org_id: str, *, device_id: int | None = None,
                    mac: str | None = None) -> dict:
        """THE TRAY: every cable end landing at one point, and the joints between.

        One read for both columns and the connectors, so they cannot disagree
        about what is on the tray — the same reason the optical panel takes its
        counts from one roster pass.

        `side` is DERIVED and advisory: which way a splicer would hold the tray,
        with the feed on the left. It decides nothing — any two fibres open here
        may be joined — and a point the feed walk never reached simply has
        everything on one side, which is honest rather than broken.
        """
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
                # The feed side is the cable this point was reached BY.
                "side": "feed" if feed.get(point) == far else "onward",
            })
        landing.sort(key=lambda c: (c["side"] != "feed", c["name"], c["cable_id"]))
        return {
            "point": here,
            "cables": landing,
            "joints": [{"id": j["id"], "a_cable_id": j["a_cable_id"],
                        "a_core_no": j["a_core_no"], "b_cable_id": j["b_cable_id"],
                        "b_core_no": j["b_core_no"]} for j in joints],
        }


    def trace_fibre(self, org_id: str, cable_id: int, core_no: int) -> dict:
        """The whole optical path this fibre makes, across sheaths and joints.

        Answered on the SERVER rather than mirrored into the browser: the walk is
        an algorithm, not a vocabulary, and this codebase mirrors constants (the
        strand colours, the fibre counts, the map-detail defaults) precisely
        because two copies of an algorithm drift where two copies of a list are
        pinned by a test. The map lights whatever this returns.
        """
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
        """The plant feed over POINTS — the shared half of the feed question.

        Roots are the boxes a feed can only ever come FROM: gear (whose upstream
        is its own declared parent) and any passive somebody has already parented.
        Feeding out from those is what gives an undirected cable its direction.
        """
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
        """Network-tree presentation only — the parent link is NOT touched.

        An aggregation switch with a large subtree buries a device an operator
        reads often; detaching lifts that row (and its own subtree) to the top
        level of the tree WITHOUT lying about the plant: parent_device_id stays,
        so suppression, the map and paging are unchanged.
        """
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
        """Set (or clear) a device's web-UI proxy address override. Each field is
        independent: NULL ip = proxy the probe IP, NULL port = the scheme default,
        NULL scheme = infer from port. All three NULL clears the override entirely."""
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
            # A CABLE NAMES TWO POINTS AND ONE OF THEM IS GOING. The glass may
            # well still be in the ground, but "this sheath runs to a box that no
            # longer exists" is not a record anybody can act on, and leaving it
            # dangles an FK. Same call `link_routes` makes one line down.
            #
            # JOINTS FIRST, and by CABLE as well as by point: a fibre landing here
            # may be joined at some OTHER closure, so sweeping only this device's
            # own joints would leave rows pointing at cables about to go. Then the
            # core register, for the same reason the cable delete takes it — a
            # core is a position inside a particular sheath.
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
            # Removing a splitter un-records the drops that came off it, rather
            # than dangling them: the box is gone, so "which passive feeds this
            # subscriber" genuinely has no answer any more. The subscribers
            # themselves are untouched (they live in the SNMP roster) and simply
            # go back to reading "splitter not recorded" — the honest state, and
            # the one the operator has to correct anyway once the plant moved.
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
            # Paging responsibility for a device that no longer exists. The
            # schema says ON DELETE CASCADE, but this is swept explicitly like
            # every other FK table above: a deferred cascade would leave the
            # deletion order dependent on a PRAGMA, and the guardrail test
            # (test_delete_cascade_handles_every_fk_table) reads this source.
            conn.execute("DELETE FROM org_device_workers WHERE device_id=?",
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
                " gpon_vendor, web_ip, web_port, web_scheme FROM org_devices"
                " WHERE org_id=? AND is_active=1 AND maintenance=0"
                f" AND (device_type IS NULL OR device_type NOT IN ({placeholders}))"
                " ORDER BY id",
                (org_id, *_PASSIVE_TYPES)).fetchall()
        return [dict(r) for r in rows]


    # ----- device web-UI credentials -----------------------------------------

    def get_device_webui_credentials(self, org_id: str, device_id: int) -> dict | None:
        """Raw credential row for a device, INCLUDING the encrypted password blob
        (``password_enc``). Callers returning data to the browser must drop that
        field — decode it through the SecretBox instead."""
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
        """Upsert a device's web-UI login. ``username`` is stored verbatim
        (``''`` clears it). Password handling is explicit so a username-only edit
        never wipes a stored password: ``set_password=False`` leaves the existing
        ciphertext untouched; ``True`` writes ``password_enc`` (``None`` clears
        it). Returns False if the device isn't an active member of this org."""
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


    # ----- peer (cross) links -------------------------------------------------
    # Switch-to-switch cabling between boxes at the same level. Rides the SAME
    # table as backup edges under kind='peer', which is what keeps it invisible to
    # the engine: every dependency read path (org_device_backup_edges/_map, and so
    # load_device_meta and the rebuild fingerprint) filters kind='backup'. A peer
    # link therefore CANNOT rebuild an engine or re-page anyone — the property the
    # whole design rests on, pinned by test_central_peerlinks.
    #
    # A peer edge is UNDIRECTED, so the directional (child_id, parent_id) columns
    # are canonicalized to (lo, hi) = (min, max) of the pair. One cable = one row
    # no matter which end the operator declared it from; the UNIQUE index then
    # makes a duplicate declaration a no-op instead of a second line on the map.

    @staticmethod
    def _peer_pair(a_id: int, b_id: int) -> tuple[int, int]:
        return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


    def org_device_peer_map(self, org_id: str) -> dict[int, set[int]]:
        """Symmetric adjacency: every device → the peers it cross-links to."""
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
