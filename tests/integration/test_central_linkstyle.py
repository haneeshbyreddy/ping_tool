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


class LinkStyleApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "hand", "handpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.core = self.store.create_org_device("ispA", {
            "name": "CORE-SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.edge = self.store.create_org_device("ispA", {
            "name": "EDGE-SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": None, "parent_device_id": self.core})
        self.far = self.store.create_org_device("ispA", {
            "name": "FAR-SW", "ip_address": "10.0.0.3", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

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

    def _routes(self, cookie):
        status, body, _ = self._req("GET", "/api/inventory/routes", cookie=cookie)
        self.assertEqual(status, 200, body)
        return {(r["child_id"], r["parent_id"]): r for r in body["routes"]}

    def _cable(self, cookie, name="trunk", cores=12):
        status, body, _ = self._req(
            "POST", "/api/inventory/cable",
            {"org_id": "ispA", "name": name, "cores": cores}, cookie=cookie)
        self.assertEqual(status, 200, body)
        return body["id"]


    def test_a_chip_position_survives_the_route_being_straightened(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "label_pos": 0.25},
                  cookie=cookie)
        self._req("POST", "/api/inventory/route",
                  {"child_id": self.edge, "parent_id": self.core,
                   "waypoints": [[17.4, 78.4]]}, cookie=cookie)
        status, body, _ = self._req("POST", "/api/inventory/route",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "waypoints": []}, cookie=cookie)
        self.assertEqual(status, 200, body)
        row = self._routes(cookie)[(self.edge, self.core)]
        self.assertAlmostEqual(row["label_pos"], 0.25)
        self.assertEqual(row["waypoints"], [])

    def test_a_write_naming_a_CABLE_is_refused_here(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "cable_id": 1, "core_no": 3}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_label_pos_stays_on_the_line(self):
        cookie = self._login()
        for bad in (-0.1, 1.5, "half"):
            status, body, _ = self._req("POST", "/api/inventory/link-style",
                                        {"child_id": self.edge, "parent_id": self.core,
                                         "label_pos": bad}, cookie=cookie)
            self.assertEqual(status, 422, f"{bad} was accepted: {body}")


    def test_a_cross_link_can_be_styled_and_routed(self):
        cookie = self._login()
        lo, hi = min(self.core, self.far), max(self.core, self.far)
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.core, "b_id": self.far}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": hi, "parent_id": lo,
                                     "label_pos": 0.4}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/route",
                                    {"child_id": hi, "parent_id": lo,
                                     "waypoints": [[17.5, 78.5]]}, cookie=cookie)
        self.assertEqual(status, 200, body)
        row = self._routes(cookie)[(hi, lo)]
        self.assertAlmostEqual(row["label_pos"], 0.4)
        self.assertEqual(row["waypoints"], [[17.5, 78.5]])

    def test_unlinked_devices_cannot_be_styled(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.far, "parent_id": self.edge,
                                     "label_pos": 0.5}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_styling_is_an_owner_write(self):
        cookie = self._login("hand", "handpassword")
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "label_pos": 0.5}, cookie=cookie)
        self.assertEqual(status, 403, body)

    def test_another_orgs_owner_is_refused(self):
        cookie = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "label_pos": 0.5}, cookie=cookie)
        self.assertIn(status, (403, 404), body)
        self.assertEqual(self._routes(self._login()), {})


if __name__ == "__main__":
    unittest.main()
