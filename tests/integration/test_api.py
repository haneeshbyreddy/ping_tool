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


if __name__ == "__main__":
    unittest.main()
