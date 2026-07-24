"""Port-level link bindings: which physical port carries each parent→child link.

The parent side reuses switch_ports.feeds_device_id (the same column ports.py
folds a port-down into the child's outage through); the child side is the new
uplink_device_id mirror. `/api/inventory/link-ports` serves every bound port
org-wide in one query — the map hangs a live bandwidth label per link off it.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore

TS = "2026-07-20T00:00:00+00:00"


class LinkPortsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.sw = self.store.create_org_device("ispA", {
            "name": "CORE-SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.ap = self.store.create_org_device("ispA", {
            "name": "HILL-AP", "ip_address": "10.0.0.2", "device_type": "ap",
            "region": None, "parent_device_id": self.sw})
        self.other = self.store.create_org_device("ispB", {
            "name": "B-SW", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self._port(self.sw, 5, "ge5")
        self._port(self.ap, 1, "wan")
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _port(self, device_id, if_index, name, org="ispA"):
        self.store.upsert_switch_port(org, device_id, if_index, name, None,
                                      "up", "up", None, 0, False, None, TS)

    def _port_id(self, device_id, if_index, org="ispA"):
        return next(p["id"] for p in self.store.list_switch_ports(org, device_id)
                    if p["if_index"] == if_index)

    def _req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username="owner", password="ownerpassword"):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    # --- binding both ends of a link ----------------------------------------

    def test_bind_both_sides_then_list_serves_the_link(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/ports/uplink",
                                    {"id": self._port_id(self.ap, 1),
                                     "uplink_device_id": self.sw}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/ports/feeds",
                                    {"id": self._port_id(self.sw, 5),
                                     "feeds_device_id": self.ap}, cookie=cookie)
        self.assertEqual(status, 200, body)

        status, body, _ = self._req("GET", "/api/inventory/link-ports", cookie=cookie)
        self.assertEqual(status, 200, body)
        rows = body["ports"]
        self.assertEqual(len(rows), 2)
        by_dev = {r["device_id"]: r for r in rows}
        self.assertEqual(by_dev[self.ap]["uplink_device_id"], self.sw)
        self.assertEqual(by_dev[self.ap]["if_name"], "wan")
        self.assertEqual(by_dev[self.sw]["feeds_device_id"], self.ap)

    def test_uplink_clears_with_null(self):
        cookie = self._login()
        pid = self._port_id(self.ap, 1)
        self._req("POST", "/api/inventory/ports/uplink",
                  {"id": pid, "uplink_device_id": self.sw}, cookie=cookie)
        status, body, _ = self._req("POST", "/api/inventory/ports/uplink",
                                    {"id": pid, "uplink_device_id": None}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/inventory/link-ports", cookie=cookie)
        self.assertEqual(body["ports"], [])

    def test_uplink_target_must_share_the_org(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/ports/uplink",
                                    {"id": self._port_id(self.ap, 1),
                                     "uplink_device_id": self.other}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_another_orgs_owner_cannot_touch_the_port(self):
        cookie = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/ports/uplink",
                                    {"id": self._port_id(self.ap, 1),
                                     "uplink_device_id": self.sw}, cookie=cookie)
        self.assertEqual(status, 403, body)

    def test_link_ports_list_is_org_scoped(self):
        # ispA binds a port; ispB's list stays empty
        self.store.set_port_uplink("ispA", self._port_id(self.ap, 1), self.sw)
        cookie = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("GET", "/api/inventory/link-ports", cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["ports"], [])

    def test_walk_refresh_keeps_the_binding(self):
        # the SNMP sweep upserts the same row every pass; the operator's cabling
        # declaration must survive it (upsert deliberately omits the column)
        self.store.set_port_uplink("ispA", self._port_id(self.ap, 1), self.sw)
        self._port(self.ap, 1, "wan")
        row = self.store.list_switch_ports("ispA", self.ap)[0]
        self.assertEqual(row["uplink_device_id"], self.sw)

    def test_deleting_the_parent_clears_child_bindings(self):
        leaf = self.store.create_org_device("ispA", {
            "name": "LEAF", "ip_address": "10.0.0.3", "device_type": "ap",
            "region": None, "parent_device_id": None})
        self._port(leaf, 2, "eth2")
        self.store.set_port_uplink("ispA", self._port_id(leaf, 2), self.sw)
        # re-home the AP so CORE-SW has no children, then delete it
        self.store.update_org_device("ispA", self.ap, {
            "name": "HILL-AP", "ip_address": "10.0.0.2", "device_type": "ap",
            "region": None, "parent_device_id": None})
        self.store.delete_org_device("ispA", self.sw)
        row = next(p for p in self.store.list_switch_ports("ispA", leaf)
                   if p["if_index"] == 2)
        self.assertIsNone(row["uplink_device_id"])
        self.assertEqual(self.store.list_link_ports("ispA"), [])


if __name__ == "__main__":
    unittest.main()
