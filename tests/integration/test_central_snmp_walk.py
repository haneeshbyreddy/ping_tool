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
from wisp.central.store import CentralStore, SNMP_WALKS_KEEP
from wisp.central.weboptics_profiles import BUILTIN_SPECS


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
        self.assertFalse(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid, varbinds=[["1.3.6.1.2.1.1.5.0", "other"]]))
        walk = self.store.get_snmp_walk("ispA", wid)
        self.assertEqual(walk["status"], "done")
        self.assertEqual(walk["varbind_count"], 1)
        self.assertEqual(walk["result"], [["1.3.6.1.2.1.1.5.0", "sw1"]])

    def test_truncation_survives_the_round_trip_and_defaults_off(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.assertTrue(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid, varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"]],
            truncated=True))
        self.assertEqual(self.store.get_snmp_walk("ispA", wid)["truncated"], 1)
        self.assertEqual(
            self.store.list_snmp_walks("ispA", self.dev)[0]["truncated"], 1)

        plain = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.complete_snmp_walk("ispA", "edge-1", plain, varbinds=[])
        self.assertEqual(self.store.get_snmp_walk("ispA", plain)["truncated"], 0)

        bad = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.complete_snmp_walk("ispA", "edge-1", bad, error="timeout",
                                      truncated=True)
        self.assertEqual(self.store.get_snmp_walk("ispA", bad)["truncated"], 0)

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
        # Walking is a PLATFORM-ADMIN tool, so the walk driver here is the
        # superadmin; the owners stay for the refusal cases.
        auth.create_user(self.store, None, "root", "rootpassword")
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
        cookie = self._login("root", "rootpassword")
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
        self.assertEqual(walks[0]["root_oid"], "1.3.6.1")

        status, reply = self._report(mode="recheck")
        self.assertNotIn("snmp_walks", reply)

        status, resp, _ = self._req("POST", "/edge/snmp-walk", {
            "v": 1, "org_id": "ispA", "node_id": "edge-1", "walk_id": wid,
            "varbinds": [["1.3.6.1.2.1.1.5.0", "sw1"], ["1.3.6.1.2.1.1.2.0",
                         "1.3.6.1.4.1.5651.1"]], "truncated": True}, token="tok")
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])
        status, reply = self._report()
        self.assertNotIn("snmp_walks", reply)

        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-walks?device_id={self.dev}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["walks"][0]["status"], "done")
        self.assertEqual(body["walks"][0]["varbind_count"], 2)
        self.assertEqual(body["walks"][0]["truncated"], 1)
        self.assertNotIn("result", body["walks"][0])
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-walk/result?id={wid}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["walk"]["result"]), 2)

    def test_queue_validations(self):
        cookie = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": self.dev, "root_oid": "not.an.oid"},
                                    cookie=cookie)
        self.assertEqual(status, 422)
        bare = self.store.create_org_device("ispA", {
            "name": "bare", "ip_address": "10.0.0.10", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": bare}, cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("SNMP", body["error"])
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
        self.assertEqual(walk["varbind_count"], 2)
        self.assertEqual(len(walk["result"][0][1]), 1024)

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

    def test_superadmin_creates_global_and_org_scoped_rows(self):
        # Authoring is superadmin-only now; an org-scoped row is still written,
        # by NAMING the org in the body rather than by being that org's owner.
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/snmp-profiles",
                                    self._payload(), cookie=root)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/snmp-profiles",
                                    self._payload(name="local", org_id="ispA"),
                                    cookie=root)
        self.assertEqual(status, 200, body)

        status, body, _ = self._req("GET", "/api/snmp-profiles?org=ispA",
                                    cookie=root)
        self.assertEqual({p["name"] for p in body["profiles"]},
                         {"fiberhome", "local"})
        status, body, _ = self._req("GET", "/api/snmp-profiles?org=ispB",
                                    cookie=root)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"fiberhome"})
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

    def test_superadmin_creates_global_and_org_scoped_rows(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/gpon-profiles",
                                    self._payload(), cookie=root)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/gpon-profiles",
                                    self._payload(name="local", org_id="ispA"),
                                    cookie=root)
        self.assertEqual(status, 200, body)
        # The LIST stays owner-readable — it is the device form's vendor
        # dropdown, and a Select with no item for its value renders blank and
        # unstamps the vendor on save.
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/gpon-profiles", cookie=owner)
        self.assertEqual(status, 200, body)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"vsol", "local"})
        bowner = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("GET", "/api/gpon-profiles", cookie=bowner)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"vsol"})
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
        from wisp.ingress.gpon import gpon_profile_from_dict
        p = gpon_profile_from_dict(body["gpon_profiles"][0])
        self.assertIsNotNone(p)
        self.assertEqual(p.decode_state("1"), "online")
        self.assertEqual(p.format_pon_label("2"), "EPON0/2")

    def test_an_olt_can_be_saved_on_a_profile_vendor(self):
        # The owner no longer authors the profile, but must still be able to
        # SAVE an OLT on one: `gpon_vendor` validates against the profile rows,
        # so the owner's read of that list is load-bearing.
        root = self._login("root", "rootpassword")
        owner = self._login("owner", "ownerpassword")
        self._req("POST", "/api/gpon-profiles",
                  self._payload(name="syrotech_gpon", org_id="ispA"), cookie=root)
        status, body, _ = self._req("POST", "/api/inventory", {
            "org_id": "ispA", "name": "Gpon_08", "ip_address": "10.0.0.7",
            "device_type": "OLT", "gpon_vendor": "syrotech_gpon"}, cookie=owner)
        self.assertEqual(status, 200, body)
        did = body["id"]

        status, body, _ = self._req("POST", "/api/inventory/update", {
            "id": did, "name": "Gpon_08", "ip_address": "10.0.0.7",
            "device_type": "OLT", "gpon_vendor": "syrotech_gpon",
            "onu_pon_limit": 128}, cookie=owner)
        self.assertEqual(status, 200, body)
        row = self.store.get_org_device("ispA", did)
        self.assertEqual(row["gpon_vendor"], "syrotech_gpon")
        self.assertEqual(row["onu_pon_limit"], 128)

        profiles = self.store.list_gpon_profiles("ispA")
        pid = next(p["id"] for p in profiles if p["name"] == "syrotech_gpon")
        payload = self._payload(name="syrotech_gpon", enabled=False)
        payload["id"] = pid
        status, body, _ = self._req("POST", "/api/gpon-profiles/update", payload,
                                    cookie=root)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/update", {
            "id": did, "name": "Gpon_08 (renamed)", "ip_address": "10.0.0.7",
            "device_type": "OLT", "gpon_vendor": "syrotech_gpon"}, cookie=owner)
        self.assertEqual(status, 200, body)

        status, _, _ = self._req("POST", "/api/inventory/update", {
            "id": did, "name": "Gpon_08", "ip_address": "10.0.0.7",
            "device_type": "OLT", "gpon_vendor": "syrotek"}, cookie=owner)
        self.assertEqual(status, 422)


class PollIntervalTest(unittest.TestCase):
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
        for bad in (5, 300, "soon"):
            status, _, _ = self._req("POST", "/api/org",
                                     {"org_id": "ispA", "poll_interval_s": bad},
                                     cookie=owner)
            self.assertEqual(status, 422, bad)
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


class PlatformAdminOnlyTest(unittest.TestCase):
    """Raw SNMP walking and recipe AUTHORING are PLATFORM tools.

    Operator decision 2026-08-18: an ISP owner no longer walks OIDs off gear or
    authors a decoding recipe the whole fleet's readings ride on. The walk
    dialog and the profile wizard were DELETED from the SPA for every role and
    vendor onboarding became a CLI/ops job, so this gate is not a second layer
    behind a hidden button — it is the only thing in front of these routes.

    What an owner KEEPS is the other half of the invariant, pinned below:
    reading every recipe list, because picking the vendor for your own box is
    the ISP's job and a 403 there renders the dropdown blank and unstamps the
    device on the next save. And the "Test SNMP" button, on its own route pair
    (`/api/inventory/snmp-test`) where the SERVER pins the walk root and
    answers with a verdict instead of a dump.
    """

    _SNMP_METRICS = {"cpu_pct": {"oid": "1.3.6.1.4.1.5651.3.901.2.0",
                                 "decode": "as_is", "select": "first"}}
    _GPON_SPEC = {"oids": {"ident_key": "1.3.6.1.4.1.999.1.6",
                           "ident_state": "1.3.6.1.4.1.999.1.5"},
                  "scales": {"rx": 0.1},
                  "state_map": {"1": "online", "0": "offline"},
                  "state_default": "offline", "pon_index": "first_segment",
                  "pon_label": "EPON0/{pon}"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "field", "fieldpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.dev = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org_device_snmp("ispA", self.dev, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        self.wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1",
                                               "1.3.6.1", 100)
        self.snmp_pid = self.store.create_snmp_profile("ispA", {
            "name": "a-local", "match_sysobjectid": "1.3.6.1.4.1.5651",
            "metrics": self._SNMP_METRICS, "enabled": True})
        self.gpon_pid = self.store.create_gpon_profile("ispA", {
            "name": "vsol", "match_sysobjectid": "1.3.6.1.4.1.999",
            "spec": self._GPON_SPEC, "enabled": True})
        self.webopt_pid = self.store.create_web_optics_profile("ispA", {
            "name": "dbc-local", "spec": dict(BUILTIN_SPECS["dbc"]),
            "enabled": True})
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    tearDown = SnmpWalkHttpTest.tearDown
    _req = SnmpWalkHttpTest._req
    _login = SnmpWalkHttpTest._login

    def _snmp_payload(self, **over):
        p = {"name": "authored", "match_sysobjectid": "1.3.6.1.4.1.5651",
             "metrics": self._SNMP_METRICS, "enabled": True}
        p.update(over)
        return p

    def _gpon_payload(self, **over):
        p = dict(self._GPON_SPEC, name="authored",
                 match_sysobjectid="1.3.6.1.4.1.999", enabled=True)
        p.update(over)
        return p

    def _webopt_payload(self, **over):
        p = dict(BUILTIN_SPECS["dbc"], name="authored", enabled=True)
        p.update(over)
        return p

    def _writes(self):
        # Every recipe table's create/update/delete, plus the walk queue.
        return [
            ("/api/inventory/snmp-walk", {"device_id": self.dev}),
            ("/api/snmp-profiles", self._snmp_payload()),
            ("/api/snmp-profiles/update", self._snmp_payload(id=self.snmp_pid)),
            ("/api/snmp-profiles/delete", {"id": self.snmp_pid}),
            ("/api/gpon-profiles", self._gpon_payload()),
            ("/api/gpon-profiles/update", self._gpon_payload(id=self.gpon_pid)),
            ("/api/gpon-profiles/delete", {"id": self.gpon_pid}),
            ("/api/web-optics-profiles", self._webopt_payload()),
            ("/api/web-optics-profiles/update",
             self._webopt_payload(id=self.webopt_pid)),
            ("/api/web-optics-profiles/delete", {"id": self.webopt_pid}),
        ]

    def _walk_reads(self):
        # The walk surface only: a raw varbind dump off a customer's gear.
        return [f"/api/inventory/snmp-walks?device_id={self.dev}",
                f"/api/inventory/snmp-walk/result?id={self.wid}"]

    _RECIPE_LISTS = ("/api/snmp-profiles", "/api/gpon-profiles",
                     "/api/web-optics-profiles", "/api/nvr-profiles")

    def test_an_owner_is_refused_every_walk_and_profile_route(self):
        owner = self._login("owner", "ownerpassword")
        for path, body in self._writes():
            status, resp, _ = self._req("POST", path, body, cookie=owner)
            self.assertEqual(status, 403, f"POST {path} answered {status}: {resp}")
            self.assertEqual(resp.get("error"), "forbidden", path)
        for path in self._walk_reads():
            status, resp, _ = self._req("GET", path, cookie=owner)
            self.assertEqual(status, 403, f"GET {path} answered {status}: {resp}")

        # A refusal wrote NOTHING: no queued walk, no edited or deleted recipe.
        self.assertEqual([w["id"] for w in self.store.list_snmp_walks(
            "ispA", self.dev)], [self.wid])
        self.assertEqual(self.store.get_snmp_profile(self.snmp_pid)["name"],
                         "a-local")
        self.assertEqual(self.store.get_gpon_profile(self.gpon_pid)["name"], "vsol")

    def test_the_superadmin_still_walks_and_authors(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/inventory/snmp-walk",
                                    {"device_id": self.dev}, cookie=root)
        self.assertEqual(status, 200, body)
        fresh = body["id"]
        for path in (f"/api/inventory/snmp-walks?device_id={self.dev}",
                     f"/api/inventory/snmp-walk/result?id={fresh}",
                     "/api/snmp-profiles?org=ispA"):
            status, body, _ = self._req("GET", path, cookie=root)
            self.assertEqual(status, 200, f"GET {path} answered {status}: {body}")
        for path, payload in [
            ("/api/snmp-profiles", self._snmp_payload(name="s-new")),
            ("/api/snmp-profiles/update", self._snmp_payload(id=self.snmp_pid,
                                                             name="s-edited")),
            ("/api/gpon-profiles", self._gpon_payload(name="g-new")),
            ("/api/gpon-profiles/update", self._gpon_payload(id=self.gpon_pid,
                                                             name="g-edited")),
            ("/api/web-optics-profiles", self._webopt_payload(name="w-new")),
            ("/api/web-optics-profiles/update",
             self._webopt_payload(id=self.webopt_pid, name="w-edited")),
            ("/api/snmp-profiles/delete", {"id": self.snmp_pid}),
            ("/api/gpon-profiles/delete", {"id": self.gpon_pid}),
            ("/api/web-optics-profiles/delete", {"id": self.webopt_pid}),
        ]:
            status, body, _ = self._req("POST", path, payload, cookie=root)
            self.assertEqual(status, 200, f"POST {path} answered {status}: {body}")

    def test_an_owner_reads_every_recipe_list_but_writes_none(self):
        # THE PAIR IS THE INVARIANT. Authoring a recipe is internal work;
        # choosing which recipe applies to your own box stays the ISP's, and it
        # is the whole payoff of the recipes going global — an owner picks the
        # vendor instead of waiting for a per-org copy. A 403 on a list route
        # would not be a cosmetic refusal: the device form's vendor Select
        # renders BLANK with no item for its value, and the next save unstamps
        # a correctly-vendored device.
        owner = self._login("owner", "ownerpassword")
        for path in self._RECIPE_LISTS:
            status, body, _ = self._req("GET", path, cookie=owner)
            self.assertEqual(status, 200, f"GET {path} answered {status}: {body}")
            # ... and reads only global rows plus its own: no other tenant's
            # identity rides along in the shape we widened.
            for row in body.get("profiles") or []:
                self.assertIn(row.get("org_id"), (None, "ispA"), f"{path}: {row}")
        for path, payload in self._writes():
            status, resp, _ = self._req("POST", path, payload, cookie=owner)
            self.assertEqual(status, 403, f"POST {path} answered {status}: {resp}")

    def test_the_owners_diagnosis_path_survives(self):
        # The SNMP and Rx explanations an owner reads are composed from status
        # facts the probe/scrape reported, never from a profile row.
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-status?device_id={self.dev}", cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req(
            "GET", f"/api/inventory/rx-status?device_id={self.dev}", cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/gpon-profiles", cookie=owner)
        self.assertEqual(status, 200, body)
        self.assertEqual({p["name"] for p in body["profiles"]}, {"vsol"})

    # ---- the owner's "Test SNMP" button, the half the lock must not take ----
    #
    # Every fault it names is the ISP's own to fix: a wrong community string, a
    # source-IP ACL, UDP 161 not forwarded through NAT. Hiding the button was
    # the wrong cure, so the fix is a route pair where the SERVER pins what is
    # walked and answers with a verdict instead of a dump.

    def _queue_test(self, cookie, device_id=None, **extra):
        body = {"device_id": self.dev if device_id is None else device_id}
        body.update(extra)
        return self._req("POST", "/api/inventory/snmp-test", body, cookie=cookie)

    def _verdict(self, cookie, wid):
        return self._req("GET", f"/api/inventory/snmp-test/result?id={wid}",
                         cookie=cookie)

    def test_an_owner_queues_a_test_and_reads_its_verdict(self):
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._queue_test(owner)
        self.assertEqual(status, 200, body)
        wid = body["id"]

        status, body, _ = self._verdict(owner, wid)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["test"]["status"], "pending")
        self.assertFalse(body["test"]["answered"])

        # The probe answers. It rode the ordinary walk queue, so the ordinary
        # completion path fills it in.
        self.assertTrue(self.store.complete_snmp_walk(
            "ispA", "edge-1", wid,
            varbinds=[["1.3.6.1.2.1.1.1.0", "  C-Data  FD1104S\n v2.1 "],
                      ["1.3.6.1.2.1.1.5.0", "HILL-OLT-1"]]))
        status, body, _ = self._verdict(owner, wid)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["test"]["status"], "done")
        self.assertTrue(body["test"]["answered"])
        self.assertEqual(body["test"]["sys_descr"], "C-Data FD1104S v2.1")
        self.assertIsNone(body["test"]["error"])

    def test_an_agent_that_answers_with_nothing_is_not_an_answer(self):
        # "Nothing is wrong" and "nothing is measured" must not render alike:
        # the empty walk drives the SPA's own toast, so the flag has to be here.
        owner = self._login("owner", "ownerpassword")
        _, body, _ = self._queue_test(owner)
        self.store.complete_snmp_walk("ispA", "edge-1", body["id"], varbinds=[])
        _, body, _ = self._verdict(owner, body["id"])
        self.assertEqual(body["test"]["status"], "done")
        self.assertFalse(body["test"]["answered"])
        self.assertIsNone(body["test"]["sys_descr"])

        _, body, _ = self._queue_test(owner)
        self.store.complete_snmp_walk("ispA", "edge-1", body["id"],
                                      error="No SNMP response before timeout")
        _, body, _ = self._verdict(owner, body["id"])
        self.assertEqual(body["test"]["status"], "error")
        self.assertFalse(body["test"]["answered"])
        self.assertIn("timeout", body["test"]["error"])

    def test_a_body_supplied_root_oid_is_IGNORED(self):
        # THE WHOLE POINT. A permission that depends on a body field the client
        # chooses is not a permission, so the root is pinned server-side and
        # nothing the caller sends can move it or come back echoed.
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._queue_test(
            owner, root_oid="1.3.6.1.4.1", max_varbinds=20000)
        self.assertEqual(status, 200, body)
        walk = self.store.get_snmp_walk("ispA", body["id"])
        self.assertEqual(walk["root_oid"], inventory.SNMP_TEST_ROOT_OID)
        self.assertEqual(walk["max_varbinds"], inventory.SNMP_TEST_MAX_VARBINDS)
        self.assertNotIn("root_oid", body)
        _, verdict, _ = self._verdict(owner, body["id"])
        self.assertNotIn("root_oid", verdict["test"])

    def test_the_verdict_carries_NO_raw_varbinds(self):
        owner = self._login("owner", "ownerpassword")
        _, body, _ = self._queue_test(owner)
        wid = body["id"]
        self.store.complete_snmp_walk(
            "ispA", "edge-1", wid,
            varbinds=[["1.3.6.1.2.1.1.1.0", "sysDescr"],
                      ["1.3.6.1.2.1.1.6.0", "hill top"]])
        _, body, _ = self._verdict(owner, wid)
        self.assertEqual(set(body["test"]),
                         {"id", "status", "answered", "sys_descr", "error"})
        self.assertNotIn("hill top", json.dumps(body))

    def test_the_verdict_route_is_no_keyhole_onto_a_raw_walk(self):
        # `self.wid` is a raw walk on 1.3.6.1, the kind the CLI queues. It is
        # this org's own row, so only the pinned root can refuse it.
        owner = self._login("owner", "ownerpassword")
        self.store.complete_snmp_walk(
            "ispA", "edge-1", self.wid,
            varbinds=[["1.3.6.1.4.1.5651.1.2.3", "vendor secret"]])
        status, body, _ = self._verdict(owner, self.wid)
        self.assertEqual(status, 404, body)
        self.assertNotIn("vendor secret", json.dumps(body))

    def test_an_owner_cannot_test_another_orgs_device(self):
        bowner = self._login("bowner", "bownerpassword")
        status, body, _ = self._queue_test(bowner)
        self.assertEqual(status, 403, body)
        self.assertEqual(self.store.list_snmp_walks("ispA", self.dev)[0]["id"],
                         self.wid)
        owner = self._login("owner", "ownerpassword")
        _, body, _ = self._queue_test(owner)
        status, resp, _ = self._verdict(bowner, body["id"])
        self.assertEqual(status, 403, resp)

    def test_a_worker_reaches_neither_test_route(self):
        # Workers do not manage devices, so the new routes stay out of
        # `_WORKER_ROUTES` and the default refusal is the gate.
        worker = self._login("field", "fieldpassword")
        status, body, _ = self._queue_test(worker)
        self.assertEqual(status, 403, body)
        owner = self._login("owner", "ownerpassword")
        _, body, _ = self._queue_test(owner)
        status, resp, _ = self._verdict(worker, body["id"])
        self.assertEqual(status, 403, resp)

    def test_the_test_route_refuses_a_device_it_cannot_walk_BY_NAME(self):
        owner = self._login("owner", "ownerpassword")
        bare = self.store.create_org_device("ispA", {
            "name": "bare", "ip_address": "10.0.0.10", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, body, _ = self._queue_test(owner, device_id=bare)
        self.assertEqual(status, 422, body)
        self.assertIn("SNMP", body["error"])
        orphan = self.store.create_org_device("ispA", {
            "name": "orphan", "ip_address": "10.0.0.11", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": None})
        self.store.set_org_device_snmp("ispA", orphan, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        status, body, _ = self._queue_test(owner, device_id=orphan)
        self.assertEqual(status, 422, body)
        self.assertIn("assign", body["error"])

    def test_the_test_queue_is_the_SAME_queue(self):
        # Retention and supersede-pending are the store's, not the route's:
        # the button must not become a second write path into snmp_walks.
        owner = self._login("owner", "ownerpassword")
        _, first, _ = self._queue_test(owner)
        _, second, _ = self._queue_test(owner)
        self.assertEqual([w["id"] for w in
                          self.store.pending_snmp_walks("ispA", "edge-1")],
                         [second["id"]])
        self.assertEqual(self.store.get_snmp_walk("ispA", first["id"])["error"],
                         "superseded")
        for _ in range(SNMP_WALKS_KEEP + 3):
            self._queue_test(owner)
        self.assertEqual(len(self.store.list_snmp_walks("ispA", self.dev)),
                         SNMP_WALKS_KEEP)

    def test_the_credential_and_account_surfaces_are_untouched(self):
        # HARD BOUNDARY. Recipes became platform-owned; ACCOUNTS and CREDENTIALS
        # did not and must not. A profile may never carry a host — the account
        # does — and an ISP legitimately owns its own device passwords. This
        # pins that the recipe lock never creeps onto them.
        owner = self._login("owner", "ownerpassword")
        status, body, _ = self._req(
            "GET", f"/api/inventory/credentials?device_id={self.dev}", cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/inventory/radius", cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/snmp", {
            "id": self.dev, "snmp_enabled": True, "snmp_version": "2c",
            "snmp_community": "private", "snmp_port": 161}, cookie=owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/credentials", {
            "device_id": self.dev, "username": "admin", "password": "hunter2"},
            cookie=owner)
        self.assertEqual(status, 200, body)


class EdgeProfileOrderTest(unittest.TestCase):
    """An org override must beat a GLOBAL profile carrying the SAME prefix.

    Both edge matchers keep the longest matching sysObjectID prefix, so an
    equal-length tie is settled by LIST ORDER — and the two settle it in
    opposite directions (health takes the first, gpon the last). Profiles are
    mostly global now, so the tie is the ordinary case; central composes both
    lists so the org row wins either way, with no edge change and no rollout.
    """

    _PREFIX = "1.3.6.1.4.1.999"

    setUp = SnmpProfileTest.setUp
    tearDown = SnmpProfileTest.tearDown
    _req = SnmpWalkHttpTest._req
    _login = SnmpWalkHttpTest._login

    def _seed_snmp(self):
        # Names chosen so alphabetical order and scope order DISAGREE: the org
        # row sorts last by name, so a passing test can only be the scope sort.
        self.store.create_snmp_profile(None, {
            "name": "aa-global", "match_sysobjectid": self._PREFIX,
            "metrics": {"cpu_pct": {"oid": f"{self._PREFIX}.1.1",
                                    "decode": "as_is", "select": "first"}},
            "enabled": True})
        self.store.create_snmp_profile("ispA", {
            "name": "zz-override", "match_sysobjectid": self._PREFIX,
            "metrics": {"cpu_pct": {"oid": f"{self._PREFIX}.2.2",
                                    "decode": "as_is", "select": "first"}},
            "enabled": True})

    def _seed_gpon(self):
        spec = {"oids": {"ident_key": f"{self._PREFIX}.1.6",
                         "ident_state": f"{self._PREFIX}.1.5"},
                "scales": {"rx": 0.1},
                "state_map": {"1": "online", "0": "offline"},
                "state_default": "offline", "pon_index": "first_segment",
                "pon_label": "EPON0/{pon}"}
        # Mirror image of the SNMP seed: here the org row sorts FIRST by name,
        # so alphabetical order and the required scope order disagree again.
        self.store.create_gpon_profile(None, {
            "name": "zz-global", "match_sysobjectid": self._PREFIX,
            "spec": spec, "enabled": True})
        self.store.create_gpon_profile("ispA", {
            "name": "aa-override", "match_sysobjectid": self._PREFIX,
            "spec": spec, "enabled": True})

    def _edge(self, org="ispA"):
        status, body, _ = self._req("GET", f"/edge/devices?org_id={org}",
                                    token="tok")
        self.assertEqual(status, 200, body)
        return body

    def test_an_org_snmp_override_ships_before_the_global_row(self):
        from wisp.ingress.health import match_profile
        self._seed_snmp()
        shipped = self._edge()["snmp_profiles"]
        self.assertEqual([p["name"] for p in shipped], ["zz-override", "aa-global"],
                         "org-scoped rows must ship FIRST: the edge's health"
                         " matcher takes the FIRST row on an equal-length tie")
        picked = match_profile(shipped, f"{self._PREFIX}.1.2.3")
        self.assertEqual(picked["name"], "zz-override")
        # An org with no override of its own still gets the global one.
        picked = match_profile(self._edge("ispB")["snmp_profiles"],
                               f"{self._PREFIX}.1.2.3")
        self.assertEqual(picked["name"], "aa-global")

    def test_an_org_gpon_override_ships_after_the_global_row(self):
        from wisp.ingress.gpon import gpon_profile_from_dict, match_gpon_profile
        self._seed_gpon()
        shipped = self._edge()["gpon_profiles"]
        self.assertEqual([p["name"] for p in shipped], ["zz-global", "aa-override"],
                         "org-scoped rows must ship LAST here: the edge's gpon"
                         " matcher takes the LATER row on an equal-length tie")
        # Exactly what GponPollerPool.set_profiles builds, in wire order.
        extra = {}
        for d in shipped:
            parsed = gpon_profile_from_dict(d)
            self.assertIsNotNone(parsed, d)
            extra[parsed.name] = parsed
        picked = match_gpon_profile(f"{self._PREFIX}.1", extra=extra)
        self.assertEqual(picked.name, "aa-override")
        picked = match_gpon_profile(
            f"{self._PREFIX}.1",
            extra={p.name: p for p in [gpon_profile_from_dict(d)
                                       for d in self._edge("ispB")["gpon_profiles"]]})
        self.assertEqual(picked.name, "zz-global")

    def test_the_edge_wire_shape_is_unchanged(self):
        # Ordering is the ONLY thing that moved: same rows, same keys, same
        # enabled-only filter. A probe reads these two lists and nothing else.
        self._seed_snmp()
        self._seed_gpon()
        self.store.create_snmp_profile("ispA", {
            "name": "off", "match_sysobjectid": "1.3.6.1.4.1.4242",
            "metrics": {"cpu_pct": {"oid": "1.3.6.1.4.1.4242.1",
                                    "decode": "as_is", "select": "first"}},
            "enabled": False})
        body = self._edge()
        self.assertEqual({p["name"] for p in body["snmp_profiles"]},
                         {"zz-override", "aa-global"})
        for p in body["snmp_profiles"]:
            self.assertEqual(set(p), {"name", "match_sysobjectid", "metrics"})
        for p in body["gpon_profiles"]:
            self.assertIn("match_sysobjectid", p)
            self.assertIn("oids", p)


class AdminCliWalkTest(unittest.TestCase):
    """The CLI is the ONLY caller left for the walk queue.

    The dashboard's walk dialog was deleted, so without these two subcommands
    the documented vendor-onboarding workflow needs a hand-written curl with a
    superadmin cookie. `admin.py` has no other test in this suite — this covers
    the two commands added with the lock, nothing more.
    """

    def setUp(self):
        from wisp.central import admin
        self.admin = admin
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "central.db"
        self.store = CentralStore(self.db)
        self.dev = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org_device_snmp("ispA", self.dev, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        self.bare = self.store.create_org_device("ispA", {
            "name": "bare", "ip_address": "10.0.0.10", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        import contextlib
        import io
        from unittest import mock
        from wisp.config import Config
        out, err = io.StringIO(), io.StringIO()
        cfg = Config(central_db=self.db)
        with mock.patch.object(self.admin, "CONFIG", cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.admin.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_queueing_a_walk_names_the_wait_it_costs(self):
        code, out, _ = self._run("snmp-walk", "--device", str(self.dev),
                                 "--root-oid", "1.3.6.1.4.1")
        self.assertEqual(code, 0, out)
        pending = self.store.pending_snmp_walks("ispA", "edge-1")
        self.assertEqual(len(pending), 1)
        wid = pending[0]["id"]
        self.assertEqual(pending[0]["root_oid"], "1.3.6.1.4.1")
        self.assertIn(f"id={wid}", out)
        # The wait is the thing an operator gets wrong: the edge only ever
        # polls, so a walk is not instant.
        self.assertIn("/report", out)
        self.assertIn(f"snmp-walk-result --id {wid}", out)

    def test_a_second_walk_supersedes_the_pending_one(self):
        # Straight through the store method, so retention and supersede still
        # apply — the CLI adds no second path to the table.
        self._run("snmp-walk", "--device", str(self.dev))
        self._run("snmp-walk", "--device", str(self.dev))
        self.assertEqual(len(self.store.pending_snmp_walks("ispA", "edge-1")), 1)

    def test_a_device_that_cannot_be_walked_is_refused_by_name(self):
        code, _, err = self._run("snmp-walk", "--device", str(self.bare))
        self.assertEqual(code, 1)
        self.assertIn("SNMP", err)
        code, _, err = self._run("snmp-walk", "--device", "99999")
        self.assertEqual(code, 1)
        self.assertIn("no device", err)
        code, _, err = self._run("snmp-walk", "--device", str(self.dev),
                                 "--root-oid", "not.an.oid")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_a_truncated_dump_says_so_loudly(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.complete_snmp_walk(
            "ispA", "edge-1", wid,
            varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"],
                      ["1.3.6.1.2.1.1.2.0", "1.3.6.1.4.1.5651.1"]],
            truncated=True)
        code, out, _ = self._run("snmp-walk-result", "--id", str(wid))
        self.assertEqual(code, 0, out)
        self.assertIn("1.3.6.1.2.1.1.5.0\tsw1", out)
        # Announced before AND after the dump: a long dump is read from its
        # tail as often as its head, and "that OID holds nothing" read off a
        # partial walk is the false negative this flag exists to prevent.
        self.assertEqual(out.count("TRUNCATED"), 2)

    def test_a_complete_dump_carries_no_truncation_warning(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.complete_snmp_walk("ispA", "edge-1", wid,
                                      varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"]])
        code, out, _ = self._run("snmp-walk-result", "--id", str(wid))
        self.assertEqual(code, 0, out)
        self.assertNotIn("TRUNCATED", out)

    def test_the_dump_can_go_to_a_file(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        self.store.complete_snmp_walk(
            "ispA", "edge-1", wid,
            varbinds=[["1.3.6.1.2.1.1.5.0", "sw1"], ["1.3.6.1.2.1.1.6.0", "hill"]])
        dump = Path(self.tmp.name) / "walk.tsv"
        code, out, _ = self._run("snmp-walk-result", "--id", str(wid),
                                 "--out", str(dump))
        self.assertEqual(code, 0, out)
        self.assertEqual(dump.read_text().splitlines(),
                         ["1.3.6.1.2.1.1.5.0\tsw1", "1.3.6.1.2.1.1.6.0\thill"])

    def test_a_pending_or_failed_walk_reads_honestly(self):
        wid = self.store.create_snmp_walk("ispA", self.dev, "edge-1", "1.3.6.1", 100)
        code, out, _ = self._run("snmp-walk-result", "--id", str(wid))
        self.assertEqual(code, 0, out)
        self.assertIn("still queued", out)
        self.store.complete_snmp_walk("ispA", "edge-1", wid, error="timeout")
        code, _, err = self._run("snmp-walk-result", "--id", str(wid))
        self.assertEqual(code, 1)
        self.assertIn("timeout", err)
        code, _, err = self._run("snmp-walk-result", "--id", "99999")
        self.assertEqual(code, 1)
        self.assertIn("no walk", err)


if __name__ == "__main__":
    unittest.main()
