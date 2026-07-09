import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.server import make_server

def _hb(org="ispA", node="edge-1", **body):
    return {"v": 1, "org_id": org, "node_id": node, "body": body}

class CentralStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _device(self, org="ispA", name="Tower", ip="10.0.0.1", assigned_node_id="edge-1"):
        return self.store.create_org_device(org, {
            "name": name, "ip_address": ip, "device_type": None,
            "region": None, "parent_device_id": None,
            "assigned_node_id": assigned_node_id})

    def test_open_outage_if_absent_does_not_stack(self):
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:34:54+00:00", "DOWN")
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:34:57+00:00", "DOWN")
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))
        with self.store._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE org_id='ispA' AND device_id=?"
                " AND resolved_at IS NULL", (dev,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_showcase_stats_counts_active_named_orgs(self):
        # Org with a node + display name shows in the ticker; a blank-name org
        # still counts but stays anonymous; a named org with no node is excluded.
        self.store.touch_node("ispA", "edge-1")
        self.store.set_org("ispA", name="SkyLink Broadband")
        self.store.touch_node("ispB", "edge-2")       # active, no display name
        self.store.set_org("ispC", name="Ghost Net")  # named but never reported
        stats = self.store.showcase_stats()
        self.assertEqual(stats["count"], 2)            # ispA + ispB
        self.assertEqual(stats["names"], ["SkyLink Broadband"])

    def test_open_outage_reopens_after_resolve(self):
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        self.store.resolve_outage("ispA", dev, "2026-06-23T08:05:00+00:00")
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T09:00:00+00:00", "DOWN")
        with self.store._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE org_id='ispA' AND device_id=?",
                (dev,)).fetchone()[0]
        self.assertEqual(total, 2)
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))

    def test_triage_outages_status_derivation(self):
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispA", dev)
        statuses = {o["id"]: o["status"] for o in self.store.triage_outages("ispA")}
        self.assertEqual(statuses[oid], "unassigned")

        self.store.acknowledge_outage("ispA", oid, "alice")
        statuses = {o["id"]: o["status"] for o in self.store.triage_outages("ispA")}
        self.assertEqual(statuses[oid], "in_progress")

        self.store.resolve_outage("ispA", dev, "2026-06-23T08:05:00+00:00")
        statuses = {o["id"]: o["status"] for o in self.store.triage_outages("ispA")}
        self.assertEqual(statuses[oid], "pending_postmortem")

        self.assertTrue(self.store.set_outage_postmortem("ispA", oid, "fiber cut", "spliced"))
        self.assertNotIn(oid, {o["id"] for o in self.store.triage_outages("ispA")})

    def test_clear_pending_postmortems_empties_the_queue(self):
        caused, open_dev, res1, res2 = (
            self._device(name="d0", ip="10.0.0.10"),
            self._device(name="d1", ip="10.0.0.11"),
            self._device(name="d2", ip="10.0.0.12"),
            self._device(name="d3", ip="10.0.0.13"))
        for i, dev in enumerate((caused, res1, res2)):
            self.store.open_outage_if_absent("ispA", dev, f"2026-06-23T08:0{i}:00+00:00", "DOWN")
            self.store.resolve_outage("ispA", dev, f"2026-06-23T08:0{i}:30+00:00")
        self.store.open_outage_if_absent("ispA", open_dev, "2026-06-23T09:00:00+00:00", "DOWN")
        self.store.set_outage_postmortem("ispA", self._resolved_oid("ispA", caused), "known cause", None)

        cleared = self.store.clear_pending_postmortems("ispA", "no post-mortem recorded")
        self.assertEqual(cleared, 2)
        remaining = {o["status"] for o in self.store.triage_outages("ispA")}
        self.assertNotIn("pending_postmortem", remaining)
        self.assertIn("unassigned", remaining)
        self.assertEqual(self.store.clear_pending_postmortems("ispA", "again"), 0)

    def _resolved_oid(self, org, dev):
        with self.store._connect() as conn:
            return conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=? ORDER BY id DESC LIMIT 1",
                (org, dev)).fetchone()[0]

    def test_triage_excludes_probeless_devices(self):
        orphan = self._device(name="orphan", ip="10.0.0.9", assigned_node_id=None)
        watched = self._device(name="watched", ip="10.0.0.8")
        self.store.open_outage_if_absent("ispA", orphan, "2026-06-23T08:00:00+00:00", "DOWN")
        self.store.open_outage_if_absent("ispA", watched, "2026-06-23T08:00:00+00:00", "DOWN")
        names = {o["device_name"] for o in self.store.triage_outages("ispA")}
        self.assertEqual(names, {"watched"})
        self.store.update_org_device("ispA", watched, {
            "name": "watched", "ip_address": "10.0.0.8", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": None})
        self.assertEqual(self.store.triage_outages("ispA"), [])

    def test_postmortem_refuses_still_open_outage(self):
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispA", dev)
        self.assertFalse(self.store.set_outage_postmortem("ispA", oid, "guess", None))

    def test_outage_org_is_none_for_unknown_id(self):
        self.assertIsNone(self.store.outage_org(999))

    def test_list_events_pagination_newest_first(self):
        for i in range(1, 6):
            dev = self._device(name=f"d{i}", ip=f"10.0.0.{i}")
            self.store.open_outage_if_absent("ispA", dev,
                                             f"2026-06-23T08:00:0{i}+00:00", "DOWN")
        page1 = self.store.list_events("ispA", limit=2)
        self.assertEqual(len(page1), 2)
        self.assertTrue(page1[0]["id"] > page1[1]["id"])
        page2 = self.store.list_events("ispA", limit=2, before_id=page1[-1]["id"])
        self.assertEqual(len(page2), 2)
        self.assertTrue(page2[0]["id"] < page1[-1]["id"])

    def test_heartbeat_upserts_node(self):
        self.store.record_heartbeat("ispA", "edge-1",
                                    {"version": "0.10.0", "fleet_size": 7, "open_outages": 1,
                                     "last_poll_ts": "2026-06-30T12:00:00+00:00"})
        self.store.record_heartbeat("ispA", "edge-1",
                                    {"version": "0.10.1", "fleet_size": 9, "open_outages": 0})
        nodes = self.store.node_versions("ispA")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["version"], "0.10.1")
        with self.store._connect() as conn:
            row = conn.execute("SELECT fleet_size FROM nodes WHERE org_id='ispA'"
                               " AND node_id='edge-1'").fetchone()
        self.assertEqual(row["fleet_size"], 9)

    def test_org_auto_provisioned_on_first_contact(self):
        self.store.touch_node("ispA", "edge-1")
        self.store.record_heartbeat("ispB", "edge-9", {"fleet_size": 1})
        orgs = {o["org_id"] for o in self.store.orgs()}
        self.assertEqual(orgs, {"ispA", "ispB"})

    def test_org_scoping_filters_event_reads(self):
        a = self._device(org="ispA")
        b = self._device(org="ispB", ip="10.0.1.1")
        self.store.open_outage_if_absent("ispA", a, "2026-06-23T08:00:00+00:00", "DOWN")
        self.store.open_outage_if_absent("ispB", b, "2026-06-23T08:00:00+00:00", "DOWN")
        self.assertEqual(len(self.store.list_events("ispA")), 1)
        self.assertEqual({e["org_id"] for e in self.store.list_events(None)},
                         {"ispA", "ispB"})

    def test_set_org_topic(self):
        self.store.touch_node("ispA", "edge-1")
        self.store.set_org("ispA", name="ISP A", ntfy_topic="ispA-ops")
        self.assertEqual(self.store.org_topic("ispA"), "ispA-ops")
        org = next(o for o in self.store.orgs() if o["org_id"] == "ispA")
        self.assertEqual(org["name"], "ISP A")
        self.assertEqual(org["node_count"], 1)

    def test_set_org_role_topics(self):
        self.store.set_org("ispA", ntfy_topic_owner="isp-a-owner",
                           ntfy_topic_operator="isp-a-op", ntfy_topic_tech="isp-a-tech")
        self.assertEqual(self.store.org_role_topic("ispA", "owner"), "isp-a-owner")
        self.assertEqual(self.store.org_role_topic("ispA", "operator"), "isp-a-op")
        self.assertEqual(self.store.org_role_topic("ispA", "tech"), "isp-a-tech")
        self.assertIsNone(self.store.org_role_topic("ispA", "bogus"))
        self.store.set_org("ispA", ntfy_topic_owner="isp-a-owner-2")
        self.assertEqual(self.store.org_role_topic("ispA", "owner"), "isp-a-owner-2")
        self.assertEqual(self.store.org_role_topic("ispA", "operator"), "isp-a-op")

class NodeTokenTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_never_registered_has_no_status(self):
        self.assertIsNone(self.store.get_node_token_status("ispA", "edge-1"))
        self.assertFalse(self.store.node_token_registered("ispA", "edge-1"))

    def test_issue_then_resolve(self):
        token = self.store.issue_node_token("ispA", "edge-1")
        self.assertEqual(self.store.resolve_node_token(token), ("ispA", "edge-1"))
        self.assertTrue(self.store.node_token_registered("ispA", "edge-1"))
        status = self.store.get_node_token_status("ispA", "edge-1")
        self.assertIsNotNone(status["created_at"])
        self.assertIsNone(status["revoked_at"])

    def test_wrong_token_does_not_resolve(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.assertIsNone(self.store.resolve_node_token("not-the-token"))
        self.assertIsNone(self.store.resolve_node_token(""))

    def test_reissue_invalidates_the_old_token(self):
        old = self.store.issue_node_token("ispA", "edge-1")
        new = self.store.issue_node_token("ispA", "edge-1")
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.resolve_node_token(old))
        self.assertEqual(self.store.resolve_node_token(new), ("ispA", "edge-1"))

    def test_revoke_stops_resolving_but_keeps_the_row(self):
        token = self.store.issue_node_token("ispA", "edge-1")
        self.assertTrue(self.store.revoke_node_token("ispA", "edge-1"))
        self.assertIsNone(self.store.resolve_node_token(token))
        self.assertFalse(self.store.node_token_registered("ispA", "edge-1"))
        status = self.store.get_node_token_status("ispA", "edge-1")
        self.assertIsNotNone(status)
        self.assertIsNotNone(status["revoked_at"])

    def test_revoke_twice_is_a_no_op(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.assertTrue(self.store.revoke_node_token("ispA", "edge-1"))
        self.assertFalse(self.store.revoke_node_token("ispA", "edge-1"))

    def test_revoke_of_never_registered_node_is_false(self):
        self.assertFalse(self.store.revoke_node_token("ispA", "ghost"))

    def test_reissue_after_revoke_reactivates(self):
        token1 = self.store.issue_node_token("ispA", "edge-1")
        self.store.revoke_node_token("ispA", "edge-1")
        token2 = self.store.issue_node_token("ispA", "edge-1")
        self.assertIsNone(self.store.resolve_node_token(token1))
        self.assertEqual(self.store.resolve_node_token(token2), ("ispA", "edge-1"))
        self.assertIsNone(self.store.get_node_token_status("ispA", "edge-1")["revoked_at"])

    def test_org_isolation(self):
        a = self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispB", "edge-1")
        self.assertEqual(self.store.resolve_node_token(a), ("ispA", "edge-1"))
        self.assertEqual(len(self.store.list_node_tokens("ispA")), 1)
        self.assertEqual(len(self.store.list_node_tokens("ispB")), 1)

    def test_delete_removes_the_row_entirely(self):
        token = self.store.issue_node_token("ispA", "edge-1")
        self.assertTrue(self.store.delete_node_token("ispA", "edge-1"))
        self.assertIsNone(self.store.resolve_node_token(token))
        self.assertIsNone(self.store.get_node_token_status("ispA", "edge-1"))

    def test_delete_of_never_registered_node_is_false(self):
        self.assertFalse(self.store.delete_node_token("ispA", "ghost"))

    def test_delete_unassigns_devices_pointed_at_it(self):
        did = self.store.create_org_device("ispA", {
            "name": "CPE", "ip_address": "10.0.0.5", "device_type": None, "region": None,
            "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.issue_node_token("ispA", "edge-1")
        self.store.delete_node_token("ispA", "edge-1")
        dev = next(d for d in self.store.list_org_devices("ispA") if d["id"] == did)
        self.assertIsNone(dev["assigned_node_id"])

    def test_delete_does_not_touch_other_orgs_or_nodes(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispA", "edge-2")
        self.store.issue_node_token("ispB", "edge-1")
        self.store.delete_node_token("ispA", "edge-1")
        self.assertEqual({r["node_id"] for r in self.store.list_node_tokens("ispA")}, {"edge-2"})
        self.assertEqual({r["node_id"] for r in self.store.list_node_tokens("ispB")}, {"edge-1"})

    def test_list_joins_heartbeat_info(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispA", "edge-2")
        self.store.record_heartbeat("ispA", "edge-1", {"version": "0.11.0"})
        rows = {r["node_id"]: r for r in self.store.list_node_tokens("ispA")}
        self.assertEqual(rows["edge-1"]["version"], "0.11.0")
        self.assertIsNotNone(rows["edge-1"]["last_seen"])
        self.assertIsNone(rows["edge-2"]["version"])

    def test_delete_purges_the_heartbeat_row_too(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.record_heartbeat("ispA", "edge-1", {})
        self.store.delete_node_token("ispA", "edge-1")
        self.assertEqual([r for r in self.store.node_liveness()
                          if (r["org_id"], r["node_id"]) == ("ispA", "edge-1")], [])

    def test_node_liveness_skips_uncredentialed_when_org_uses_tokens(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.record_heartbeat("ispA", "edge-1", {})
        self.store.record_heartbeat("ispA", "edge-1-diag", {})
        self.store.record_heartbeat("ispB", "edge-9", {})
        watched = {(r["org_id"], r["node_id"]) for r in self.store.node_liveness()}
        self.assertEqual(watched, {("ispA", "edge-1"), ("ispB", "edge-9")})

    def test_node_liveness_skips_revoked_credentials(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispA", "edge-2")
        self.store.record_heartbeat("ispA", "edge-1", {})
        self.store.record_heartbeat("ispA", "edge-2", {})
        self.store.revoke_node_token("ispA", "edge-2")
        watched = {r["node_id"] for r in self.store.node_liveness()}
        self.assertEqual(watched, {"edge-1"})

    def test_data_version_moves_on_heartbeat(self):
        before = self.store.data_version("ispA")
        self.store.record_heartbeat("ispA", "edge-1", {},
                                    now="2099-01-01T00:00:00+00:00")
        self.assertNotEqual(self.store.data_version("ispA"), before)
        other = self.store.data_version("ispB")
        self.store.record_heartbeat("ispA", "edge-1", {},
                                    now="2099-01-01T00:01:00+00:00")
        self.assertEqual(self.store.data_version("ispB"), other)

class OrgDevicesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_list_update_delete_round_trip(self):
        root = self.store.create_org_device("ispA", {
            "name": "Core Router", "ip_address": "10.0.0.1", "device_type": "core",
            "region": "north", "parent_device_id": None})
        child = self.store.create_org_device("ispA", {
            "name": "Tower 1", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "north", "parent_device_id": root})
        devs = self.store.list_org_devices("ispA")
        self.assertEqual(len(devs), 2)
        root_row = next(d for d in devs if d["id"] == root)
        self.assertEqual(root_row["child_count"], 1)

        ok = self.store.update_org_device("ispA", child, {
            "name": "Tower 1 (renamed)", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "north", "parent_device_id": root})
        self.assertTrue(ok)
        self.assertEqual(self.store.get_org_device("ispA", child)["name"], "Tower 1 (renamed)")

        result = self.store.delete_org_device("ispA", root)
        self.assertFalse(result["ok"])
        self.assertIn("child", result["reason"])
        self.store.delete_org_device("ispA", child)
        result = self.store.delete_org_device("ispA", root)
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.list_org_devices("ispA"), [])

    def test_gpon_vendor_round_trips_and_rides_topology(self):
        olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.7", "device_type": "OLT",
            "region": None, "parent_device_id": None, "gpon_vendor": "huawei"})
        row = next(d for d in self.store.list_org_devices("ispA") if d["id"] == olt)
        self.assertEqual(row["gpon_vendor"], "huawei")

        topo = {d["id"]: d for d in self.store.org_device_topology("ispA")}
        self.assertEqual(topo[olt]["gpon_vendor"], "huawei")
        self.assertEqual(topo[olt]["device_type"], "OLT")

        self.store.update_org_device("ispA", olt, {
            "name": "OLT-1", "ip_address": "10.0.0.7", "device_type": "OLT",
            "region": None, "parent_device_id": None, "gpon_vendor": None})
        self.assertIsNone(self.store.get_org_device("ispA", olt)["gpon_vendor"])

    def test_list_carries_monitored_port_alarm_counts(self):
        sw = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        ts = "2026-01-01T00:00:00+00:00"
        bw_low = (100, 200, ts, 1e6, 1e6, 3, True, ts, 0, False, None)
        self.store.upsert_switch_port("ispA", sw, 1, "Gi0/1", None, "up", "down",
                                      None, 2, True, ts, ts)
        self.store.upsert_switch_port("ispA", sw, 2, "Gi0/2", None, "up", "up",
                                      None, 0, False, None, ts, bw=bw_low)
        self.store.upsert_switch_port("ispA", sw, 3, "Gi0/3", None, "up", "down",
                                      None, 2, True, ts, ts)
        for if_index in (1, 2):
            port = next(p for p in self.store.list_switch_ports("ispA", sw)
                        if p["if_index"] == if_index)
            self.store.set_port_monitored("ispA", port["id"], True)

        row = next(d for d in self.store.list_org_devices("ispA") if d["id"] == sw)
        self.assertEqual(row["ports_down"], 1)
        self.assertEqual(row["ports_bw_low"], 1)
        self.assertEqual(row["ports_bw_high"], 0)

    def test_org_isolation(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.1.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.assertEqual(len(self.store.list_org_devices("ispA")), 1)
        self.assertEqual(len(self.store.list_org_devices("ispB")), 1)
        self.assertIsNone(self.store.get_org_device("ispB", a))
        self.assertFalse(self.store.update_org_device("ispB", a, {
            "name": "hijacked", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None}))
        self.assertEqual(self.store.device_org(a), "ispA")

    def test_parent_map_is_org_scoped(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.1.1", "device_type": None,
            "region": None, "parent_device_id": None})
        pmap = self.store.org_device_parent_map("ispA")
        self.assertEqual(set(pmap.keys()), {a})

    def test_delete_cascades_every_dependent_table(self):
        switch = self.store.create_org_device("ispA", {
            "name": "Switch", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        d = self.store.create_org_device("ispA", {
            "name": "CPE", "ip_address": "10.0.0.10", "device_type": None,
            "region": None, "parent_device_id": None})
        backup = self.store.create_org_device("ispA", {
            "name": "Backup parent", "ip_address": "10.0.0.11", "device_type": None,
            "region": None, "parent_device_id": None})

        self.store.write_device_states("ispA", [(d, "DOWN", None, 100.0, None)], "t1")
        self.store.open_outage_if_absent("ispA", d, "t1", "DOWN")
        self.store.fold_device_rollups([("ispA", d, "2026-01-01T00", None, 100.0, 1)])
        self.store.record_perf_sample("ispA", d, "t1", 20.0, 0.0, 1.0, "UP", 20)
        self.store.write_device_perf("ispA", d, True, "latency", 20.0, 400.0, "t1", "t1")
        self.store.write_device_redundancy("ispA", d, True, "t1", "t1")
        self.store.create_backup_link("ispA", d, backup)
        self.store.upsert_switch_port("ispA", switch, 1, "eth1", None, "up", "up",
                                      None, 0, False, None, "t1")
        self.store.set_port_feeds("ispA", self.store.list_switch_ports("ispA", switch)[0]["id"], d)

        result = self.store.delete_org_device("ispA", d)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.store.get_org_device("ispA", d))
        ports = self.store.list_switch_ports("ispA", switch)
        self.assertEqual(len(ports), 1)
        self.assertIsNone(ports[0]["feeds_device_id"])
        self.assertTrue(self.store.delete_org_device("ispA", backup)["ok"])

    def test_maintenance_and_snmp_toggle(self):
        d = self.store.create_org_device("ispA", {
            "name": "Sw1", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.assertTrue(self.store.set_org_device_maintenance("ispA", d, True))
        self.assertTrue(self.store.get_org_device("ispA", d)["maintenance"])
        self.assertTrue(self.store.set_org_device_snmp("ispA", d, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161}))
        row = self.store.get_org_device("ispA", d)
        self.assertTrue(row["snmp_enabled"])
        self.assertEqual(row["snmp_community"], "public")

class RegionsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_is_union_of_declared_and_in_use(self):
        self.store.add_region("ispA", "north")
        self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": "south", "parent_device_id": None})
        self.store.add_worker("ispA", "Ravi", region="east")

        regions = {r["name"]: r for r in self.store.list_regions("ispA")}
        self.assertEqual(set(regions), {"north", "south", "east"})
        self.assertTrue(regions["north"]["declared"])
        self.assertFalse(regions["south"]["declared"])
        self.assertEqual(regions["south"]["device_count"], 1)
        self.assertEqual(regions["east"]["worker_count"], 1)
        self.assertEqual(regions["north"]["device_count"], 0)

    def test_add_is_idempotent(self):
        self.assertTrue(self.store.add_region("ispA", "north"))
        self.assertFalse(self.store.add_region("ispA", "north"))
        self.assertEqual(len(self.store.list_regions("ispA")), 1)

    def test_rename_cascades_devices_and_workers(self):
        d = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": "north-dc", "parent_device_id": None})
        w = self.store.add_worker("ispA", "Ravi", region="north-dc")
        # a same-name region in another org must not be touched
        other = self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.1.1", "device_type": None,
            "region": "north-dc", "parent_device_id": None})

        self.store.rename_region("ispA", "north-dc", "north")
        self.assertEqual(self.store.get_org_device("ispA", d)["region"], "north")
        worker = next(x for x in self.store.list_workers("ispA") if x["id"] == w)
        self.assertEqual(worker["region"], "north")
        self.assertEqual(self.store.get_org_device("ispB", other)["region"], "north-dc")
        regions = {r["name"]: r for r in self.store.list_regions("ispA")}
        self.assertIn("north", regions)
        self.assertTrue(regions["north"]["declared"])
        self.assertNotIn("north-dc", regions)

    def test_delete_blocked_while_in_use(self):
        self.store.add_region("ispA", "north")
        d = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": "north", "parent_device_id": None})
        result = self.store.delete_region("ispA", "north")
        self.assertFalse(result["ok"])
        self.assertIn("used by", result["reason"])

        self.store.delete_org_device("ispA", d)
        self.assertTrue(self.store.delete_region("ispA", "north")["ok"])
        self.assertEqual(self.store.list_regions("ispA"), [])

    def test_org_isolation(self):
        self.store.add_region("ispA", "north")
        self.assertEqual(self.store.list_regions("ispB"), [])
        self.assertTrue(self.store.delete_region("ispB", "north")["ok"])
        self.assertEqual(len(self.store.list_regions("ispA")), 1)

class CentralServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0, central_token="s3cret")
        self.store = CentralStore(self.cfg.central_db)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def test_healthz_unauthed(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_heartbeat_requires_token(self):
        status, _ = self._req("POST", "/heartbeat", _hb())
        self.assertEqual(status, 401)
        status, _ = self._req("POST", "/heartbeat", _hb(), token="wrong")
        self.assertEqual(status, 401)

    def test_heartbeat_persists(self):
        status, body = self._req("POST", "/heartbeat",
                                 _hb(version="0.10.0", fleet_size=3), token="s3cret")
        self.assertEqual(status, 200)
        nodes = self.store.node_versions("ispA")
        self.assertEqual(nodes[0]["version"], "0.10.0")

    def test_heartbeat_reply_carries_update_directive(self):
        self.store.set_release("9.9.9", {"linux-amd64": {
            "url": "https://dl.example/wisp-edge-linux-amd64", "sha256": "ab" * 32}})
        self.store.set_rollout("ispA", "9.9.9", ["edge-1"], state="canary")
        status, body = self._req("POST", "/heartbeat",
                                 _hb(version="0.10.0", platform="linux-amd64"),
                                 token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["update"]["target_version"], "9.9.9")
        self.assertEqual(body["update"]["sha256"], "ab" * 32)
        status, body = self._req("POST", "/heartbeat",
                                 _hb(version="9.9.9", platform="linux-amd64"),
                                 token="s3cret")
        self.assertNotIn("update", body)

    def test_unsupported_version_rejected(self):
        env = _hb()
        env["v"] = 999
        status, body = self._req("POST", "/heartbeat", env, token="s3cret")
        self.assertEqual(status, 400)

    def test_missing_org_rejected(self):
        env = {"v": 1, "node_id": "edge-1", "body": {}}
        status, _ = self._req("POST", "/heartbeat", env, token="s3cret")
        self.assertEqual(status, 400)

    def test_bad_json_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/heartbeat", body="{not json",
                     headers={"Authorization": "Bearer s3cret",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_orgs_endpoint(self):
        self._req("POST", "/heartbeat", _hb(), token="s3cret")
        status, body = self._req("GET", "/api/orgs", token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["orgs"][0]["org_id"], "ispA")
        self.assertEqual(self._req("GET", "/api/orgs")[0], 401)

    def _get_raw(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def test_landing_injects_showcase(self):
        # A live, named org should surface in the server-injected payload on `/`.
        self.store.touch_node("ispA", "edge-1")
        self.store.set_org("ispA", name="SkyLink Broadband")
        status, raw = self._get_raw("/")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("window.__WISP_SHOWCASE__=", text)
        self.assertIn('"enabled": true', text)
        self.assertIn("SkyLink Broadband", text)
        self.assertIn('src="/showcase.js"', text)
        # And the overlay script itself is served.
        js_status, js_raw = self._get_raw("/showcase.js")
        self.assertEqual(js_status, 200)
        self.assertIn(b"__WISP_SHOWCASE__", js_raw)

    def test_summary_requires_org_and_reports_low_bandwidth(self):
        status, _ = self._req("GET", "/api/summary")
        self.assertEqual(status, 401)
        status, body = self._req("GET", "/api/summary", token="s3cret")
        self.assertEqual(status, 400)
        switch = self.store.create_org_device("ispA", {
            "name": "Core Switch", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.store.upsert_switch_port("ispA", switch, 3, "Gi0/3", None, "up", "up",
                                      None, 0, False, None, "2026-01-01T00:00:00+00:00",
                                      bw=("0", "0", "2026-01-01T00:00:00+00:00",
                                          5_000_000.0, 5_000_000.0, 2, True,
                                          "2026-01-01T00:00:20+00:00", 0, False, None))
        port_id = self.store.list_switch_ports("ispA", switch)[0]["id"]
        self.store.set_port_monitored("ispA", port_id, True)
        self.store.set_port_bandwidth_config("ispA", port_id, 10, "either")
        status, body = self._req("GET", "/api/summary?org=ispA", token="s3cret")
        self.assertEqual(status, 200)
        self.assertFalse(body["uplink_down"])
        self.assertEqual(len(body["low_bandwidth"]), 1)
        self.assertEqual(body["low_bandwidth"][0]["switch_name"], "Core Switch")
        self.assertEqual(body["high_bandwidth"], [])

    def test_events_stream_emits_changed_on_new_data(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Authorization": "Bearer s3cret"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.assertTrue(resp.readline().startswith(b"retry:"))
        resp.readline()
        self.assertEqual(resp.readline(), b"event: changed\n")
        version_before = resp.readline()
        resp.readline()
        self._req("POST", "/heartbeat", _hb(), token="s3cret")
        self.assertEqual(resp.readline(), b"event: changed\n")
        version_after = resp.readline()
        self.assertNotEqual(version_before, version_after)
        conn.close()

class AdminOverviewTest(unittest.TestCase):
    """GET /api/admin/overview — superadmin fleet coverage rollup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="s3cret")
        self.store = CentralStore(self.cfg.central_db)
        from wisp.central import auth
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _dev(self, name, ip, dtype, snmp=True):
        did = self.store.create_org_device("ispA", {
            "name": name, "ip_address": ip, "device_type": dtype,
            "region": None, "parent_device_id": None})
        if snmp:
            self.store.set_org_device_snmp("ispA", did, {
                "snmp_enabled": 1, "snmp_version": "2c",
                "snmp_community": "public", "snmp_port": 161})
        return did

    def test_requires_superadmin(self):
        self.assertEqual(self._req("GET", "/api/admin/overview")[0], 401)
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": "owner", "password": "ownerpassword"})
        cookie = setcookie.split(";")[0]
        status, _, _ = self._req("GET", "/api/admin/overview", cookie=cookie)
        self.assertEqual(status, 403)

    def test_coverage_counts_and_problems(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fresh = now.isoformat(timespec="seconds")
        stale = (now - timedelta(hours=2)).isoformat(timespec="seconds")

        # Fresh switch: health + one monitored fresh port -> fully working.
        sw = self._dev("sw1", "10.0.0.1", "Switch")
        self.store.upsert_device_health("ispA", sw, {"cpu_pct": 12.0}, fresh)
        self.store.upsert_switch_port("ispA", sw, 1, "eth1", None, "up", "up",
                                      None, 0, False, None, fresh)
        pid = self.store.list_switch_ports("ispA", sw)[0]["id"]
        self.store.set_port_monitored("ispA", pid, True)
        # OLT with fresh health but no optics ever -> optics "never" problem.
        olt = self._dev("olt1", "10.0.0.2", "OLT")
        self.store.upsert_device_health("ispA", olt, {"cpu_pct": 20.0}, fresh)
        # SNMP-enabled but silent -> snmp "never" problem.
        self._dev("dead1", "10.0.0.3", "Switch")
        # SNMP data stopped -> snmp "stale" problem.
        gone = self._dev("stale1", "10.0.0.4", "Router")
        self.store.upsert_device_health("ispA", gone, {"cpu_pct": 30.0}, stale)
        # No SNMP at all -> counted in devices only, never a problem.
        self._dev("plain", "10.0.0.5", "Router", snmp=False)

        status, body, _ = self._req("GET", "/api/admin/overview", token="s3cret")
        self.assertEqual(status, 200)
        org = next(o for o in body["orgs"] if o["org_id"] == "ispA")
        self.assertEqual(org["devices"], 5)
        self.assertEqual(org["snmp"], {"enabled": 4, "working": 2})
        self.assertEqual(org["optics"]["olts"], 1)
        self.assertEqual(org["optics"]["working"], 0)
        self.assertEqual(org["ports"]["monitored"], 1)
        self.assertEqual(org["ports"]["working"], 1)
        problems = {(p["name"], p["area"], p["reason"]) for p in org["problems"]}
        self.assertEqual(problems, {("dead1", "snmp", "never"),
                                    ("olt1", "optics", "never"),
                                    ("stale1", "snmp", "stale")})
        self.assertEqual(body["problems_total"], 3)
        self.assertEqual(body["totals"]["snmp"], {"enabled": 4, "working": 2})

class DownloadRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "releases"
        (self.cache / "0.13.0").mkdir(parents=True)
        (self.cache / "0.13.0" / "wisp-edge-linux-amd64.deb").write_bytes(b"DEB-BYTES")
        (self.cache / "0.13.0" / "wisp-edge-linux-amd64").write_bytes(b"AGENT-BYTES")
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          release_cache_dir=self.cache)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_release("0.13.0", {"linux-amd64": {
            "url": "/download/0.13.0/wisp-edge-linux-amd64", "sha256": "ab" * 32}})
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _raw(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_download_by_version_no_auth(self):
        status, data = self._raw("/download/0.13.0/wisp-edge-linux-amd64.deb")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"DEB-BYTES")

    def test_download_latest_resolves_newest(self):
        status, data = self._raw("/download/latest/wisp-edge-linux-amd64.deb")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"DEB-BYTES")

    def test_download_agent_binary(self):
        status, data = self._raw("/download/latest/wisp-edge-linux-amd64")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"AGENT-BYTES")

    def test_unknown_asset_404(self):
        self.assertEqual(self._raw("/download/0.13.0/nope")[0], 404)
        self.assertEqual(self._raw("/download/9.9.9/wisp-edge-linux-amd64.deb")[0], 404)

    def test_path_traversal_blocked(self):
        status, _ = self._raw("/download/0.13.0/..%2f..%2fcentral.db")
        self.assertEqual(status, 404)
        status, _ = self._raw("/download/..%2f..%2f0.13.0/wisp-edge-linux-amd64.deb")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
