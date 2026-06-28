"""Dashboard data-layer tests (api.py): the JSON views the web UI renders.

Uses a temp DB with hand-placed device/outage/poll rows and timestamps anchored
to real `now` (the api functions window off the wall clock), so the lifecycle
buckets, uptime maths, heatmap, and the two write actions are all exercised
without a server or network.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.server import services as api
from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.core.state_machine import DOWN, UNREACHABLE, UP


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with connect(self.cfg) as c:
            # Two co-located devices in Rampur + one in Sohna.
            for did, name, ip, region in [
                (1, "Rampur Tower", "d1", "Rampur"),
                (2, "Rampur Sector", "d2", "Rampur"),
                (3, "Sohna Relay", "d3", "Sohna"),
            ]:
                c.execute(
                    "INSERT INTO devices (id,name,ip_address,region,"
                    "technician_phone)"
                    " VALUES (?,?,?,?,?)",
                    (did, name, ip, region, "+91TECH"),
                )
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers --
    def _poll(self, device_id, when, state=UP, loss=0.0):
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO poll_results (device_id,timestamp,latency_ms,packet_loss,state)"
                " VALUES (?,?,?,?,?)",
                (device_id, _iso(when), 10.0, loss, state),
            )
            c.commit()

    def _outage(self, device_id, started, resolved=None, state=DOWN,
                acked_by=None, notes=None):
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO outages (device_id,started_at,resolved_at,final_state,"
                "acknowledged_by,acknowledged_at,resolution_notes)"
                " VALUES (?,?,?,?,?,?,?)",
                (device_id, _iso(started), _iso(resolved) if resolved else None,
                 state, acked_by, _iso(started) if acked_by else None, notes),
            )
            c.execute("SELECT last_insert_rowid() AS id")
            return c.execute("SELECT MAX(id) AS id FROM outages").fetchone()["id"]

    # -- summary --
    def test_summary_counts_and_health(self):
        # device 1 currently down, others up
        self._poll(1, self.now, state=DOWN, loss=100.0)
        self._poll(2, self.now, state=UP)
        self._poll(3, self.now, state=UP)
        self._outage(1, self.now - timedelta(hours=1))  # open, ongoing
        s = api.system_summary(self.cfg)
        self.assertEqual(s["total_nodes"], 3)
        self.assertEqual(s["active_nodes"], 2)      # only 2 reporting UP
        self.assertEqual(s["outages"], 1)
        self.assertLess(s["system_health_pct"], 100.0)
        self.assertGreater(s["system_health_pct"], 90.0)

    # -- triage lifecycle buckets --
    def test_triage_buckets_and_order(self):
        self._outage(2, self.now - timedelta(minutes=30))                       # unassigned (small)
        self._outage(1, self.now - timedelta(minutes=20), acked_by="Sarah K.")  # in_progress (big)
        self._outage(3, self.now - timedelta(hours=2),
                     resolved=self.now - timedelta(hours=1), notes=None)         # pending_postmortem
        items = api.triage_outages(self.cfg)
        statuses = [i["status"] for i in items]
        self.assertEqual(statuses, ["unassigned", "in_progress", "pending_postmortem"])
        # in_progress carries the assignee through
        ip = next(i for i in items if i["status"] == "in_progress")
        self.assertEqual(ip["assigned_to"], "Sarah K.")

    def test_triage_excludes_unreachable_and_documented_and_stale(self):
        self._outage(1, self.now - timedelta(minutes=10), state=UNREACHABLE)     # suppressed
        self._outage(2, self.now - timedelta(hours=2),
                     resolved=self.now - timedelta(hours=1), notes="fixed")      # documented
        self._outage(3, self.now - timedelta(days=3),
                     resolved=self.now - timedelta(days=3))                       # stale post-mortem
        self.assertEqual(api.triage_outages(self.cfg), [])

    # -- nodes uptime --
    def test_nodes_uptime_and_state(self):
        self._poll(1, self.now, state=DOWN, loss=100.0)
        # 3h outage on device 1 over the 24h window -> ~87.5% uptime
        self._outage(1, self.now - timedelta(hours=3), resolved=self.now)
        nodes = {n["id"]: n for n in api.nodes_list(self.cfg)}
        self.assertEqual(nodes[1]["state"], DOWN)
        self.assertLess(nodes[1]["uptime_pct"], 90.0)
        self.assertEqual(nodes[2]["uptime_pct"], 100.0)

    def test_nodes_list_carries_topology(self):
        # device 2 hangs off device 1; the tree UI needs parent + child counts.
        with connect(self.cfg) as c:
            c.execute("UPDATE devices SET parent_device_id=1 WHERE id=2")
            c.commit()
        nodes = {n["id"]: n for n in api.nodes_list(self.cfg)}
        self.assertEqual(nodes[2]["parent_device_id"], 1)
        self.assertIsNone(nodes[1]["parent_device_id"])
        self.assertEqual(nodes[1]["child_count"], 1)  # has device 2 beneath it
        self.assertEqual(nodes[2]["child_count"], 0)
        self.assertEqual(nodes[3]["child_count"], 0)

    def test_maintenance_pauses_polling_and_badges(self):
        from wisp.core.state_machine import load_device_meta
        # all three devices are polled to start
        self.assertEqual({d.id for d in load_device_meta(self.cfg)}, {1, 2, 3})
        # put device 2 in maintenance -> dropped from the polled set...
        self.assertTrue(api.set_maintenance(2, True, self.cfg))
        self.assertEqual({d.id for d in load_device_meta(self.cfg)}, {1, 3})
        # ...still in the inventory + on the dashboard, badged
        node2 = {n["id"]: n for n in api.nodes_list(self.cfg)}[2]
        self.assertTrue(node2["maintenance"])
        inv2 = {d["id"]: d for d in api.list_devices(self.cfg)}[2]
        self.assertEqual(inv2["maintenance"], 1)
        # resuming brings it back into the polled set
        self.assertTrue(api.set_maintenance(2, False, self.cfg))
        self.assertEqual({d.id for d in load_device_meta(self.cfg)}, {1, 2, 3})
        self.assertFalse(api.set_maintenance(999, True, self.cfg))  # unknown id

    # -- heatmap --
    def test_heatmap_states(self):
        self._poll(1, self.now)  # first data point is today
        self._outage(1, self.now - timedelta(hours=2), resolved=self.now)
        cells = api.network_heatmap(self.cfg, days=30)
        self.assertEqual(len(cells), 30)
        self.assertEqual(cells[-1]["state"], "outage")   # today had a DOWN
        self.assertEqual(cells[0]["state"], "nodata")    # 30 days ago, no polls yet

    # -- heatmap day drill-down --
    def test_nodes_down_on_day(self):
        day = (self.now - timedelta(days=2))
        ds = day.date().isoformat()
        # device 1 down for ~2h that day; device 3 down a different day
        self._outage(1, day.replace(hour=1), resolved=day.replace(hour=3))
        self._outage(3, self.now - timedelta(days=5),
                     resolved=self.now - timedelta(days=5) + timedelta(hours=1))
        down = api.nodes_down_on_day(self.cfg, ds)
        self.assertEqual([n["id"] for n in down], [1])
        self.assertGreater(down[0]["down_s"], 0)
        # a clean day returns nothing
        clean = (self.now - timedelta(days=1)).date().isoformat()
        self.assertEqual(api.nodes_down_on_day(self.cfg, clean), [])

    # -- logs search + pagination --
    def test_logs_search_and_paging(self):
        for k in range(5):
            self._outage(1, self.now - timedelta(hours=k + 2),
                         resolved=self.now - timedelta(hours=k + 1))
        self._outage(3, self.now - timedelta(hours=1), resolved=self.now)
        all_logs = api.logs(self.cfg, limit=100)
        self.assertEqual(all_logs["total"], 6)
        rampur = api.logs(self.cfg, query="rampur")
        self.assertEqual(rampur["total"], 5)
        page = api.logs(self.cfg, limit=2, offset=2)
        self.assertEqual(len(page["entries"]), 2)
        self.assertEqual(page["total"], 6)

    # -- write actions --
    def test_assign_ack_then_postmortem(self):
        oid = self._outage(1, self.now - timedelta(minutes=5))  # open
        self.assertTrue(api.assign_and_ack(oid, "Marcus J.", self.cfg))
        with connect(self.cfg) as c:
            row = c.execute("SELECT acknowledged_by FROM outages WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["acknowledged_by"], "Marcus J.")
        # post-mortem on an OPEN outage is rejected
        self.assertFalse(api.submit_postmortem(oid, "Power Failure", "n", self.cfg))
        # resolve it, then post-mortem succeeds
        with connect(self.cfg) as c:
            c.execute("UPDATE outages SET resolved_at=? WHERE id=?", (_iso(self.now), oid))
            c.commit()
        self.assertTrue(api.submit_postmortem(oid, "Power Failure", "Genset refuel", self.cfg))
        with connect(self.cfg) as c:
            row = c.execute("SELECT root_cause,resolution_notes FROM outages WHERE id=?",
                            (oid,)).fetchone()
        self.assertEqual(row["root_cause"], "Power Failure")
        self.assertEqual(row["resolution_notes"], "Genset refuel")

    def test_dismiss_outage_clears_triage_keeps_history(self):
        # open outage cannot be dismissed (nothing to discard yet)
        oid = self._outage(1, self.now - timedelta(minutes=5))
        self.assertFalse(api.dismiss_outage(oid, self.cfg))
        # resolved + undocumented -> shows as pending_postmortem
        rid = self._outage(2, self.now - timedelta(hours=2),
                           resolved=self.now - timedelta(minutes=30))
        self.assertIn("pending_postmortem", [i["status"] for i in api.triage_outages(self.cfg)])
        # dismiss drops it off triage but the row (and its downtime) survive
        self.assertTrue(api.dismiss_outage(rid, self.cfg))
        self.assertNotIn(rid, [i["id"] for i in api.triage_outages(self.cfg)])
        with connect(self.cfg) as c:
            row = c.execute("SELECT resolved_at,resolution_notes FROM outages WHERE id=?",
                            (rid,)).fetchone()
        self.assertIsNotNone(row["resolved_at"])           # history intact
        self.assertEqual(row["resolution_notes"], api.DISMISSED_NOTE)
        # already-documented outage is not re-dismissed
        self.assertFalse(api.dismiss_outage(rid, self.cfg))

    def test_postmortem_without_notes_clears_triage(self):
        # logging a confirmed cause with an empty notes box must still drop the card
        # off triage — "documented" hinges on the (required) root cause, not notes.
        rid = self._outage(2, self.now - timedelta(hours=2),
                           resolved=self.now - timedelta(minutes=10))
        self.assertIn(rid, [i["id"] for i in api.triage_outages(self.cfg)])
        self.assertTrue(api.submit_postmortem(rid, "Hardware Fault", "", self.cfg))
        self.assertNotIn(rid, [i["id"] for i in api.triage_outages(self.cfg)])

    # -- add-node reachability probe --
    def test_check_reachable(self):
        # malformed IP is rejected the same way create_device would (422)
        with self.assertRaises(api.DeviceError):
            api.check_reachable("not-an-ip", self.cfg)
        # loopback should answer (or None if the box has no ping binary); never a
        # hard failure, and the IP is echoed back for the UI.
        res = api.check_reachable("127.0.0.1", self.cfg)
        self.assertEqual(res["ip"], "127.0.0.1")
        self.assertIn(res["reachable"], (True, None))

    # -- device inventory CRUD --
    def test_create_validate_update_delete(self):
        # create
        nid = api.create_device({
            "name": "New AP", "ip_address": "192.0.2.99", "device_type": "sector",
            "region": "Testville", "parent_device_id": "1",
        }, self.cfg)
        rows = {d["id"]: d for d in api.list_devices(self.cfg)}
        self.assertIn(nid, rows)
        self.assertEqual(rows[nid]["parent_device_id"], 1)

        # validation
        for bad in ({"ip_address": "x"}, {"name": "n"},
                    {"name": "n", "ip_address": "i", "device_type": "bogus"}):
            with self.assertRaises(api.DeviceError):
                api.create_device(bad, self.cfg)

        # no self-parent / no cycle
        with self.assertRaises(api.DeviceError):
            api.update_device(nid, {"name": "x", "ip_address": "i",
                                    "parent_device_id": str(nid)}, self.cfg)

        # update (full replace, as the UI submits)
        self.assertTrue(api.update_device(nid, {
            "name": "Renamed", "ip_address": "192.0.2.99"}, self.cfg))
        self.assertEqual({d["id"]: d for d in api.list_devices(self.cfg)}[nid]["name"], "Renamed")

        # delete
        self.assertEqual(api.delete_device(nid, self.cfg), {"ok": True})
        self.assertNotIn(nid, {d["id"] for d in api.list_devices(self.cfg)})

    def test_delete_blocked_by_children_and_purges_history(self):
        # make device 2 a child of device 1 -> deleting the parent is blocked
        with connect(self.cfg) as c:
            c.execute("UPDATE devices SET parent_device_id=1 WHERE id=2")
            c.commit()
        res = api.delete_device(1, self.cfg)
        self.assertFalse(res["ok"])
        self.assertIn("child", res["reason"])

        # a leaf with outage history deletes cleanly and purges the outage rows
        self._outage(2, self.now - timedelta(hours=1), resolved=self.now)
        self.assertEqual(api.delete_device(2, self.cfg), {"ok": True})
        with connect(self.cfg) as c:
            left = c.execute("SELECT COUNT(*) FROM outages WHERE device_id=2").fetchone()[0]
        self.assertEqual(left, 0)

    def test_triage_names_suppressed_children(self):
        # Topology: Rampur Sector (2) hangs off Rampur Tower (1). Tower goes DOWN,
        # Sector is dragged UNREACHABLE (topology-suppressed -> no card of its own),
        # so its name must surface as blast radius on the tower's triage card.
        with connect(self.cfg) as c:
            c.execute("UPDATE devices SET parent_device_id=1 WHERE id=2")
            c.commit()
        self._poll(1, self.now, state=DOWN, loss=100.0)
        self._poll(2, self.now, state=UNREACHABLE, loss=100.0)
        self._outage(1, self.now - timedelta(minutes=5))  # open DOWN on the tower

        items = api.triage_outages(self.cfg)
        tower = next(i for i in items if i["device_id"] == 1)
        self.assertEqual(tower["affected_children"], ["Rampur Sector"])

    def test_delete_purges_rollups_and_perf(self):
        # Regression for the FK-constraint delete bug: a node with rollup + perf rows
        # (both tables REFERENCE devices(id)) must delete cleanly.
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO poll_rollups (device_id,bucket,samples,loss_avg,"
                "down_polls,degraded_polls,up_polls) VALUES (3,?,?,?,?,?,?)",
                (_iso(self.now), 5, 0.0, 0, 0, 5))
            c.execute("INSERT INTO device_perf (device_id,degraded,updated_at)"
                      " VALUES (3,1,?)", (_iso(self.now),))
            c.commit()
        self.assertEqual(api.delete_device(3, self.cfg), {"ok": True})
        with connect(self.cfg) as c:
            for t in ("poll_rollups", "device_perf", "devices"):
                col = "id" if t == "devices" else "device_id"
                n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=3").fetchone()[0]
                self.assertEqual(n, 0, f"{t} not purged")


class AttendanceTest(unittest.TestCase):
    """Daily operator attendance: the toggle, the roster view, the FK-safe delete,
    and the triage 'who was on duty' cross-link."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        self.today = datetime.now(timezone.utc).date().isoformat()
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region)"
                      " VALUES (1,'Rampur Tower','d1','Rampur')")
            for wid, name, role in [
                (1, "Ravi", "operator"), (2, "Meena", "operator"),
                (3, "Arjun", "tech"), (4, "Hansa", "owner"),
            ]:
                c.execute("INSERT INTO workers (id,name,role) VALUES (?,?,?)",
                          (wid, name, role))
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_toggle_present_and_absent(self):
        # mark present (default = today), idempotent, then clear it.
        self.assertTrue(api.set_attendance(1, True, "", self.cfg)["ok"])
        self.assertTrue(api.set_attendance(1, True, "", self.cfg)["ok"])  # idempotent
        ov = api.attendance_overview(self.cfg)
        ravi = next(o for o in ov["operators"] if o["id"] == 1)
        self.assertTrue(ravi["present_today"])
        self.assertEqual(ov["today"], self.today)
        # the toggle off removes the row
        api.set_attendance(1, False, "", self.cfg)
        ov = api.attendance_overview(self.cfg)
        self.assertFalse(next(o for o in ov["operators"] if o["id"] == 1)["present_today"])

    def test_overview_lists_only_active_operators(self):
        ov = api.attendance_overview(self.cfg)
        names = {o["name"] for o in ov["operators"]}
        self.assertEqual(names, {"Ravi", "Meena"})  # tech + owner excluded
        self.assertEqual(len(ov["days"]), 14)

    def test_non_operator_rejected(self):
        with self.assertRaises(api.WorkerError):
            api.set_attendance(3, True, "", self.cfg)   # Arjun is a tech
        self.assertEqual(api.set_attendance(999, True, "", self.cfg)["ok"], False)

    def test_bad_day_rejected(self):
        with self.assertRaises(api.WorkerError):
            api.set_attendance(1, True, "24-06-2026", self.cfg)

    def test_present_days_window(self):
        old = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
        api.set_attendance(1, True, old, self.cfg)
        api.set_attendance(1, True, "", self.cfg)
        ravi = next(o for o in api.attendance_overview(self.cfg)["operators"]
                    if o["id"] == 1)
        self.assertEqual(ravi["present_days"], sorted([old, self.today]))

    def test_triage_shows_on_duty(self):
        api.set_attendance(1, True, "", self.cfg)  # Ravi present today
        with connect(self.cfg) as c:
            c.execute("INSERT INTO outages (device_id,started_at,final_state)"
                      " VALUES (1,?, 'DOWN')",
                      (datetime.now(timezone.utc).replace(microsecond=0).isoformat(),))
            c.commit()
        item = api.triage_outages(self.cfg)[0]
        self.assertEqual(item["on_duty"], ["Ravi"])

    def test_delete_worker_purges_attendance(self):
        api.set_attendance(1, True, "", self.cfg)
        self.assertEqual(api.delete_worker(1, self.cfg), {"ok": True})
        with connect(self.cfg) as c:
            n = c.execute("SELECT COUNT(*) FROM attendance WHERE worker_id=1").fetchone()[0]
        self.assertEqual(n, 0)


class BackupLinkTest(unittest.TestCase):
    """Phase 9 Part A — backup parent edges (device_links): CRUD + validation, the
    full-edge cycle check, FK-safe delete (both directions), multi-parent blast-radius
    attribution, and the on-backup node badge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with connect(self.cfg) as c:
            # core(1) <- Tower A(2), Tower B(3); Relay(4) primary under Tower A.
            for did, name, parent in [
                (1, "Core", None), (2, "Tower A", 1), (3, "Tower B", 1), (4, "Relay", 2),
            ]:
                c.execute(
                    "INSERT INTO devices (id,name,ip_address,region,parent_device_id)"
                    " VALUES (?,?,?,?,?)", (did, name, f"d{did}", "Rampur", parent))
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _poll(self, device_id, state):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO poll_results (device_id,timestamp,latency_ms,"
                      "packet_loss,state) VALUES (?,?,?,?,?)",
                      (device_id, _iso(self.now), 10.0, 0.0, state))
            c.commit()

    def _open_outage(self, device_id):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO outages (device_id,started_at,final_state)"
                      " VALUES (?,?, 'DOWN')",
                      (device_id, _iso(self.now - timedelta(minutes=5))))
            c.commit()

    def test_add_remove_backup_link_and_list(self):
        self.assertEqual(api.add_backup_link(4, 3, self.cfg), {"ok": True})
        devs = {d["id"]: d for d in api.list_devices(self.cfg)}
        self.assertEqual([b["id"] for b in devs[4]["backup_parents"]], [3])
        # duplicate rejected
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(4, 3, self.cfg)
        # remove (idempotent: second remove reports not-found)
        self.assertEqual(api.remove_backup_link(4, 3, self.cfg), {"ok": True})
        self.assertFalse(api.remove_backup_link(4, 3, self.cfg)["ok"])

    def test_backup_validation(self):
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(4, 4, self.cfg)      # self
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(4, 2, self.cfg)      # 2 is already the primary parent
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(4, 999, self.cfg)    # parent doesn't exist
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(999, 3, self.cfg)    # child doesn't exist

    def test_backup_link_cycle_rejected(self):
        # Tower A(2) is the primary parent of Relay(4); a backup edge 2<-4 closes a loop.
        with self.assertRaises(api.DeviceError):
            api.add_backup_link(2, 4, self.cfg)

    def test_delete_clears_links_both_directions(self):
        api.add_backup_link(4, 3, self.cfg)   # Tower B(3) is a backup parent of Relay(4)
        api.add_backup_link(3, 2, self.cfg)   # Tower A(2) is a backup parent of Tower B(3)
        # Tower B(3) has no PRIMARY children, so the delete isn't blocked; it appears in
        # device_links as both a child (of 2) and a parent (of 4).
        self.assertEqual(api.delete_device(3, self.cfg), {"ok": True})
        with connect(self.cfg) as c:
            n = c.execute("SELECT COUNT(*) FROM device_links WHERE child_id=3 OR parent_id=3"
                          ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_culprit_attributes_to_all_down_ancestors(self):
        # Diamond: Relay(4) primary Tower A(2), backup Tower B(3). Both towers DOWN ->
        # Relay is UNREACHABLE and named on BOTH tower triage cards.
        api.add_backup_link(4, 3, self.cfg)
        self._poll(2, DOWN)
        self._poll(3, DOWN)
        self._poll(4, UNREACHABLE)
        self._open_outage(2)
        self._open_outage(3)
        items = {i["device_id"]: i for i in api.triage_outages(self.cfg)}
        self.assertEqual(items[2]["affected_children"], ["Relay"])
        self.assertEqual(items[3]["affected_children"], ["Relay"])

    def test_nodes_list_on_backup_badge(self):
        self._poll(4, UP)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO device_redundancy (device_id,on_backup,"
                      "primary_down_since,updated_at) VALUES (4,1,?,?)",
                      (_iso(self.now), _iso(self.now)))
            c.commit()
        node = {n["id"]: n for n in api.nodes_list(self.cfg)}[4]
        self.assertTrue(node["on_backup"])
        # a hard-DOWN node never shows the on-backup badge (the outage owns it)
        self._poll(4, DOWN)
        node = {n["id"]: n for n in api.nodes_list(self.cfg)}[4]
        self.assertFalse(node["on_backup"])


class SnmpPortApiTest(unittest.TestCase):
    """Phase 9 Part B — SNMP services: device SNMP config, port discovery list, the
    monitor + feeds toggles, and FK-safe delete of switch_ports (both directions)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (1,'Switch','d1','R')")
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (2,'Tower','d2','R')")
            # a discovered port on the switch
            c.execute("INSERT INTO switch_ports (device_id,if_index,if_name,admin_status,"
                      "oper_status,updated_at) VALUES (1,2,'Gi0/2','up','up',?)",
                      (_iso(self.now),))
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _port_id(self):
        with connect(self.cfg) as c:
            return c.execute("SELECT id FROM switch_ports WHERE device_id=1").fetchone()["id"]

    def test_snmp_config_validation_and_save(self):
        self.assertTrue(api.set_snmp_config(
            1, {"snmp_enabled": 1, "snmp_community": "public", "snmp_port": 161}, self.cfg))
        dev = {d["id"]: d for d in api.list_devices(self.cfg)}[1]
        self.assertEqual(dev["snmp_enabled"], 1)
        self.assertEqual(dev["snmp_community"], "public")
        # enabling without a community is rejected
        with self.assertRaises(api.DeviceError):
            api.set_snmp_config(1, {"snmp_enabled": 1, "snmp_community": ""}, self.cfg)
        # bad port / version rejected
        with self.assertRaises(api.DeviceError):
            api.set_snmp_config(1, {"snmp_enabled": 0, "snmp_port": 99999}, self.cfg)
        with self.assertRaises(api.DeviceError):
            api.set_snmp_config(1, {"snmp_enabled": 0, "snmp_version": "3"}, self.cfg)

    def test_load_snmp_targets_only_enabled(self):
        from wisp.ingress.snmp import load_snmp_targets
        self.assertEqual(load_snmp_targets(self.cfg), [])     # none enabled yet
        api.set_snmp_config(1, {"snmp_enabled": 1, "snmp_community": "public"}, self.cfg)
        targets = load_snmp_targets(self.cfg)
        self.assertEqual([t[0] for t in targets], [1])
        self.assertEqual(targets[0][1].community, "public")

    def test_port_monitor_and_feeds_toggles(self):
        pid = self._port_id()
        self.assertTrue(api.set_port_monitored(pid, True, self.cfg))
        self.assertTrue(api.set_port_feeds(pid, 2, self.cfg))
        port = api.list_switch_ports(1, self.cfg)[0]
        self.assertTrue(port["monitored"])
        self.assertEqual(port["feeds_device_id"], 2)
        self.assertEqual(port["feeds_name"], "Tower")
        # a port can't feed its own switch, and the target must exist
        with self.assertRaises(api.DeviceError):
            api.set_port_feeds(pid, 1, self.cfg)
        with self.assertRaises(api.DeviceError):
            api.set_port_feeds(pid, 999, self.cfg)
        # clearing the mapping
        self.assertTrue(api.set_port_feeds(pid, None, self.cfg))
        self.assertIsNone(api.list_switch_ports(1, self.cfg)[0]["feeds_device_id"])

    def test_delete_clears_switch_ports_both_directions(self):
        pid = self._port_id()
        api.set_port_feeds(pid, 2, self.cfg)   # switch(1) port feeds Tower(2)
        # deleting the FED device (2) must clear the port's feeds reference cleanly
        self.assertEqual(api.delete_device(2, self.cfg), {"ok": True})
        # deleting the switch (1) removes its ports
        self.assertEqual(api.delete_device(1, self.cfg), {"ok": True})
        with connect(self.cfg) as c:
            n = c.execute("SELECT COUNT(*) FROM switch_ports").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
