"""Remote diagnostic SNMP walks + declarative vendor profiles, central side.

The walk channel is poll-only: the dashboard queues a walk, central delivers it in
the next full /report reply to the device's assigned node, the edge posts the dump
back to /edge/snmp-walk. Profiles ride the GET /edge/devices reply.
"""

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
from wisp.central import auth, inventory
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class SnmpWalkStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.dev = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org_device_snmp("ispA", self.dev, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_walk_delivers_live_device_coordinates(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1",
                                          "1.3.6.1.4.1", 2000, requested_by="alice")
        pending = self.store.pending_snmp_walks("ispA", "edge-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], wid)
        self.assertEqual(pending[0]["ip_address"], "10.0.0.9")
        self.assertEqual(pending[0]["snmp_community"], "public")
        self.assertEqual(pending[0]["root_oid"], "1.3.6.1.4.1")
        # Scoped to the walk's node — another node in the org sees nothing.
        self.assertEqual(self.store.pending_snmp_walks("ispA", "edge-2"), [])

    def test_new_walk_supersedes_the_pending_one(self):
        first = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        second = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        pending = self.store.pending_snmp_walks("ispA", "edge-1")
        self.assertEqual([w["id"] for w in pending], [second])
        stale = self.store.get_snmp_walk("ispA", first)
        self.assertEqual(stale["status"], "error")
        self.assertEqual(stale["error"], "superseded")

    def test_complete_requires_matching_node_and_pending_status(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.assertFalse(self.store.complete_snmp_walk(
            "ispA", "edge-2", wid, varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"]]))
        self.assertTrue(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid, varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"]]))
        # A second completion (duplicate upload) is a no-op.
        self.assertFalse(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid, varbinds=[["1.3.6.1.2.1.1.5.0", "other"]]))
        walk = self.store.get_snmp_walk("ispA", wid)
        self.assertEqual(walk["status"], "done")
        self.assertEqual(walk["varbind_count"], 1)
        self.assertEqual(walk["result"], [["1.3.6.1.2.1.1.5.0", "sw1"]])

    def test_error_completion_stores_no_result(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.assertTrue(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid, error="No SNMP response received before timeout"))
        walk = self.store.get_snmp_walk("ispA", wid)
        self.assertEqual(walk["status"], "error")
        self.assertIsNone(walk["result"])

    def test_retention_keeps_newest_per_device(self):
        from wisp.central.store import SNMP_WALKS_KEEP
        for _ in range(SNMP_WALKS_KEEP + 5):
            self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.assertEqual(len(self.store.list_snmp_walks("ispA", self.dev)),
                         SNMP_WALKS_KEEP)

    def test_disabled_snmp_or_inactive_device_stops_delivery(self):
        self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.set_org_device_snmp("ispA", self.dev, {
            "snmp_enabled": 0, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        self.assertEqual(self.store.pending_snmp_walks("ispA", "edge-1"), [])

    def test_data_version_bumps_on_queue_and_completion(self):
        v0 = self.store.data_version("ispA")
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        v1 = self.store.data_version("ispA")
        self.assertNotEqual(v0, v1)
        self.store.complete_snmp_walk("ispA", "edge-1", wid, varbinds=[])
        self.assertNotEqual(v1, self.store.data_version("ispA"))

    def test_org_isolation(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.assertIsNone(self.store.get_snmp_walk("ispB", wid))
        self.assertEqual(self.store.snmp_walk_org(wid), "ispA")
        self.assertFalse(self.store.complete_snmp_walk("ispB", "edge-1", wid,
                                                       varbinds=[]))


class SnmpWalkHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.dev = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org_device_snmp("ispA", self.dev, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _report(self, node="edge-1", mode="full"):
        body = {"v": 1, "org_id": "ispA", "node_id": node, "mode": mode,
                "pings": {"10.0.0.9": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        status, resp, _ = self._req("POST", "/report", body, token="tok")
        return status, resp

    def test_queue_walk_and_deliver_in_report_reply(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": self.dev}, cookie=cookie)
        self.assertEqual(status, 200, body)
        wid = body["id"]

        status, reply = self._report()
        self.assertEqual(status, 200)
        walks = reply.get("snmp_walks")
        self.assertEqual(len(walks), 1)
        self.assertEqual(walks[0]["id"], wid)
        self.assertEqual(walks[0]["ip_address"], "10.0.0.9")
        self.assertEqual(walks[0]["root_oid"], "1.3.6.1")  # default root

        # A recheck report never carries walks.
        status, reply = self._report(mode="recheck")
        self.assertNotIn("snmp_walks", reply)

        # Edge posts the result; the walk stops being delivered.
        status, resp, _ = self._req("POST", "/edge/snmp-walk", {
            "v": 1, "org_id": "ispA", "node_id": "edge-1", "walk_id": wid,
            "varbinds": [["1.3.6.1.2.1.1.5.0", "sw1"], ["1.3.6.1.2.1.1.2.0",
                         "1.3.6.1.4.1.5651.1"]]}, token="tok")
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])
        status, reply = self._report()
        self.assertNotIn("snmp_walks", reply)

        # Dashboard reads the list and the full dump.
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-walks?device_id={self.dev}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["walks"][0]["status"], "done")
        self.assertEqual(body["walks"][0]["varbind_count"], 2)
        self.assertNotIn("result", body["walks"][0])  # list is metadata-only
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-walk/result?id={wid}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["walk"]["result"]), 2)

    def test_queue_validations(self):
        cookie = self._login("owner", "ownerpassword")
        # Bad OID.
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": self.dev, "root_oid": "not.an.oid"},
                                    cookie=cookie)
        self.assertEqual(status, 422)
        # SNMP disabled.
        bare = self.store.create_org_device("ispA", {
            "name": "bare", "ip_address": "10.0.0.10", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": bare}, cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("SNMP", body["error"])
        # No assigned node.
        orphan = self.store.create_org_device("ispA", {
            "name": "orphan", "ip_address": "10.0.0.11", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": None})
        self.store.set_org_device_snmp("ispA", orphan, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": orphan}, cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("assign", body["error"])
        # max_varbinds is capped server-side.
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": self.dev, "max_varbinds": 10**9},
                                    cookie=cookie)
        self.assertEqual(status, 200)
        walk = self.store.get_snmp_walk("ispA", body["id"])
        self.assertEqual(walk["max_varbinds"], inventory.WALK_CAP_MAX_VARBINDS)

    def test_cross_org_walk_access_forbidden(self):
        cookie_b = self._login("bowner", "bownerpassword")
        status, _, _ = self._req("POST", "/api/inventory/snmp-walk",
                                 {"device_id": self.dev}, cookie=cookie_b)
        self.assertEqual(status, 403)
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        status, _, _ = self._req(
            "GET", f"/api/inventory/snmp-walks?device_id={self.dev}", cookie=cookie_b)
        self.assertEqual(status, 403)
        status, _, _ = self._req(
            "GET", f"/api/inventory/snmp-walk/result?id={wid}", cookie=cookie_b)
        self.assertEqual(status, 403)

    def test_edge_result_upload_is_bounded_and_sanitised(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        status, resp, _ = self._req("POST", "/edge/snmp-walk", {
            "v": 1, "org_id": "ispA", "node_id": "edge-1", "walk_id": wid,
            "varbinds": [["1.3.6.1.2.1.1.1.0", "x" * 5000], ["bad-shape"],
                         ["1.3.6.1.2.1.1.5.0", "sw1"]]}, token="tok")
        self.assertEqual(status, 200)
        walk = self.store.get_snmp_walk("ispA", wid)
        self.assertEqual(walk["varbind_count"], 2)  # malformed pair dropped
        self.assertEqual(len(walk["result"][0][1]), 1024)  # value length capped

    def test_edge_result_requires_ingest_auth(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        status, _, _ = self._req("POST", "/edge/snmp-walk", {
            "v": 1, "org_id": "ispA", "node_id": "edge-1", "walk_id": wid,
            "varbinds": []})
        self.assertEqual(status, 401)


class SnmpProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    _req = SnmpWalkHttpTest._req
    _login = SnmpWalkHttpTest._login

    @staticmethod
    def _payload(name="fiberhome", org_id=None, enabled=True):
        p = {"name": name, "match_sysobjectid": "1.3.6.1.4.1.5651",
             "metrics": {"cpu_pct": {"oid": "1.3.6.1.4.1.5651.3.901.2.0",
                                     "decode": "as_is"}},
             "enabled": enabled}
        if org_id is not None:
            p["org_id"] = org_id
        return p

    def test_superadmin_creates_global_owner_creates_org_local(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/snmp-profiles",
                                    self._payload(), cookie=root)
        self.assertEqual(status, 200, body)

        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/snmp-profiles",
                                    self._payload(name="local"), cookie=owner)
        self.assertEqual(status, 200, body)

        # ispA sees global + its own; ispB sees only global.
        status, body, _ = self._req("GET", "/api/snmp-profiles", cookie=owner)
        self.assertEqual({p["name"] for p in body["profiles"]},
                         {"fiberhome", "local"})
        bowner = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("GET", "/api/snmp-profiles", cookie=bowner)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"fiberhome"})
        # The vocabulary rides the reply for the profile editor UI.
        self.assertIn("as_is", body["decodes"])

    def test_owner_cannot_touch_a_global_profile(self):
        pid = self.store.create_snmp_profile(None, {
            "name": "global", "match_sysobjectid": "1.3.6.1.4.1.14988",
            "metrics": {"temp_c": {"oid": "1.3.6.1.4.1.14988.1.1.3.10",
                                   "decode": "div10", "select": "max"}},
            "enabled": True})
        owner = self._login("owner", "ownerpassword")
        payload = self._payload(name="hijack")
        payload["id"] = pid
        status, _, _ = self._req("POST", "/api/snmp-profiles/update", payload,
                                 cookie=owner)
        self.assertEqual(status, 403)
        status, _, _ = self._req("POST", "/api/snmp-profiles/delete", {"id": pid},
                                 cookie=owner)
        self.assertEqual(status, 403)
        root = self._login("root", "rootpassword")
        status, _, _ = self._req("POST", "/api/snmp-profiles/delete", {"id": pid},
                                 cookie=root)
        self.assertEqual(status, 200)

    def test_validation_rejects_unknown_metric_and_decode(self):
        root = self._login("root", "rootpassword")
        bad = self._payload()
        bad["metrics"] = {"fan_rpm": {"oid": "1.3.6.1.4.1.5651.1"}}
        status, body, _ = self._req("POST", "/api/snmp-profiles", bad, cookie=root)
        self.assertEqual(status, 422)
        bad = self._payload()
        bad["metrics"] = {"cpu_pct": {"oid": "1.3.6.1.4.1.5651.1",
                                      "decode": "times9000"}}
        status, body, _ = self._req("POST", "/api/snmp-profiles", bad, cookie=root)
        self.assertEqual(status, 422)

    def test_edge_devices_reply_carries_enabled_profiles(self):
        gid = self.store.create_snmp_profile(None, {
            "name": "global", "match_sysobjectid": "1.3.6.1.4.1.14988",
            "metrics": {"temp_c": {"oid": "1.3.6.1.4.1.14988.1.1.3.10",
                                   "decode": "div10", "select": "max"}},
            "enabled": True})
        self.store.create_snmp_profile("ispA", {
            "name": "a-local", "match_sysobjectid": "1.3.6.1.4.1.5651",
            "metrics": {"cpu_pct": {"oid": "1.3.6.1.4.1.5651.3.901.2.0",
                                    "decode": "as_is", "select": "first"}},
            "enabled": True})
        self.store.create_snmp_profile("ispA", {
            "name": "a-off", "match_sysobjectid": "1.3.6.1.4.1.9999",
            "metrics": {"cpu_pct": {"oid": "1.3.6.1.4.1.9999.1",
                                    "decode": "as_is", "select": "first"}},
            "enabled": False})
        status, body, _ = self._req("GET", "/edge/devices?org_id=ispA", token="tok")
        self.assertEqual(status, 200)
        names = {p["name"] for p in body["snmp_profiles"]}
        self.assertEqual(names, {"global", "a-local"})
        status, body, _ = self._req("GET", "/edge/devices?org_id=ispB", token="tok")
        self.assertEqual({p["name"] for p in body["snmp_profiles"]}, {"global"})


class GponProfileTest(unittest.TestCase):
    """GPON vendor profiles as data — same auth shape as SNMP health profiles."""

    setUp = SnmpProfileTest.setUp
    tearDown = SnmpProfileTest.tearDown
    _req = SnmpWalkHttpTest._req
    _login = SnmpWalkHttpTest._login

    @staticmethod
    def _payload(name="vsol", org_id=None, enabled=True, **over):
        p = {"name": name, "match_sysobjectid": "1.3.6.1.4.1.999",
             "oids": {"ident_key": "1.3.6.1.4.1.999.1.6",
                      "ident_state": "1.3.6.1.4.1.999.1.5"},
             "scales": {"rx": 0.1},
             "state_map": {"1": "online", "0": "offline"},
             "state_default": "offline", "pon_index": "first_segment",
             "pon_label": "EPON0/{pon}", "enabled": enabled}
        if org_id is not None:
            p["org_id"] = org_id
        p.update(over)
        return p

    def test_superadmin_creates_global_owner_creates_org_local(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/gpon-profiles",
                                    self._payload(), cookie=root)
        self.assertEqual(status, 200, body)
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/gpon-profiles",
                                    self._payload(name="local"), cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/gpon-profiles", cookie=owner)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"vsol", "local"})
        bowner = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("GET", "/api/gpon-profiles", cookie=bowner)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"vsol"})
        # The closed vocabulary rides the reply for the profile editor UI.
        self.assertIn("first_segment", body["pon_index_strategies"])
        self.assertIn("dying_gasp", body["states"])

    def test_owner_cannot_touch_a_global_profile(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/gpon-profiles",
                                    self._payload(), cookie=root)
        pid = body["id"]
        owner = self._login("owner", "ownerpassword")
        payload = self._payload(name="hijack")
        payload["id"] = pid
        status, _, _ = self._req("POST", "/api/gpon-profiles/update", payload,
                                 cookie=owner)
        self.assertEqual(status, 403)
        status, _, _ = self._req("POST", "/api/gpon-profiles/delete", {"id": pid},
                                 cookie=owner)
        self.assertEqual(status, 403)

    def test_validation_rejects_outside_the_vocabulary(self):
        root = self._login("root", "rootpassword")
        for bad in (self._payload(oids={"rx": "not-an-oid"}),
                    self._payload(oids={"fan": "1.2.3"}),
                    self._payload(oids={}),
                    self._payload(state_map={"1": "sleeping"}),
                    self._payload(pon_index="regex"),
                    self._payload(pon_label="EPON0/1")):
            status, body, _ = self._req("POST", "/api/gpon-profiles", bad,
                                        cookie=root)
            self.assertEqual(status, 422, body)

    def test_edge_devices_reply_carries_the_spec_the_edge_parses(self):
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/gpon-profiles", self._payload(), cookie=root)
        self._req("POST", "/api/gpon-profiles",
                  self._payload(name="off", enabled=False), cookie=root)
        status, body, _ = self._req("GET", "/edge/devices?org_id=ispA", token="tok")
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["gpon_profiles"]], ["vsol"])
        # The wire shape must round-trip through the edge's validator.
        from wisp.ingress.gpon import gpon_profile_from_dict
        p = gpon_profile_from_dict(body["gpon_profiles"][0])
        self.assertIsNotNone(p)
        self.assertEqual(p.decode_state("1"), "online")
        self.assertEqual(p.format_pon_label("2"), "EPON0/2")


class PollIntervalTest(unittest.TestCase):
    """Dashboard-set probe cadence, delivered over the topology channel."""

    setUp = SnmpProfileTest.setUp
    tearDown = SnmpProfileTest.tearDown
    _req = SnmpWalkHttpTest._req
    _login = SnmpWalkHttpTest._login

    def test_set_clamp_and_clear(self):
        owner = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/org",
                                 {"org_id": "ispA", "poll_interval_s": 30},
                                 cookie=owner)
        self.assertEqual(status, 200)
        status, body, _ = self._req("GET", "/edge/devices?org_id=ispA", token="tok")
        self.assertEqual(body["poll_interval_s"], 30)
        # Out of the 10-120s window: the fleet watchdog pages NODE_STALE at 180s,
        # so a slower cadence must be refused, not stored.
        for bad in (5, 300, "soon"):
            status, _, _ = self._req("POST", "/api/org",
                                     {"org_id": "ispA", "poll_interval_s": bad},
                                     cookie=owner)
            self.assertEqual(status, 422, bad)
        # null clears back to automatic.
        status, _, _ = self._req("POST", "/api/org",
                                 {"org_id": "ispA", "poll_interval_s": None},
                                 cookie=owner)
        self.assertEqual(status, 200)
        status, body, _ = self._req("GET", "/edge/devices?org_id=ispA", token="tok")
        self.assertIsNone(body["poll_interval_s"])

    def test_owner_cannot_set_another_orgs_interval(self):
        owner = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/org",
                                 {"org_id": "ispB", "poll_interval_s": 30},
                                 cookie=owner)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
