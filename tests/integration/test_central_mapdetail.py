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
from wisp.central import auth, mapdetail
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from support import RecordingNotifier


class CentralMapDetailHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "field", "fieldpassword", "worker")
        self.store.set_org("ispA", name="Acme")
        self.server = make_server(self.cfg, self.store, notifier=RecordingNotifier())
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
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, (json.loads(raw) if raw else {})
        except ValueError:
            return resp.status, raw

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        conn.close()
        return cookie


    def test_admin_settings_reports_the_defaults_on_a_fresh_install(self):
        root = self._login("root", "rootpassword")
        status, body = self._req("GET", "/api/admin/settings", cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(body["map_detail"], mapdetail.DEFAULTS)

    def test_superadmin_can_save_and_read_it_back(self):
        root = self._login("root", "rootpassword")
        sent = {"labels": 10, "passives": 11, "subscribers": 12,
                "subscriber_names": 16, "drop_lines": 15}
        status, _ = self._req("POST", "/api/admin/settings",
                              {"map_detail": sent}, cookie=root)
        self.assertEqual(status, 200)
        _, body = self._req("GET", "/api/admin/settings", cookie=root)
        self.assertEqual(body["map_detail"], sent)

    def test_an_absent_key_leaves_map_detail_alone(self):
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"map_detail": {"labels": 9, "subscribers": 9, "drop_lines": 9}},
                  cookie=root)
        self._req("POST", "/api/admin/settings",
                  {"google_maps_key": "AIzaTEST"}, cookie=root)
        _, body = self._req("GET", "/api/admin/settings", cookie=root)
        self.assertEqual(body["map_detail"]["labels"], 9)

    def test_the_invariant_is_repaired_server_side(self):
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"map_detail": {"labels": 12, "subscribers": 16,
                                  "subscriber_names": 9, "drop_lines": 11}},
                  cookie=root)
        _, body = self._req("GET", "/api/admin/settings", cookie=root)
        self.assertEqual(body["map_detail"]["drop_lines"], 16)
        self.assertEqual(body["map_detail"]["subscriber_names"], 16)

    def test_a_drop_line_can_never_outlive_the_SPLITTER_it_runs_to(self):
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"map_detail": {"passives": 17, "subscribers": 11,
                                  "drop_lines": 12}},
                  cookie=root)
        _, body = self._req("GET", "/api/admin/settings", cookie=root)
        self.assertEqual(body["map_detail"]["drop_lines"], 17)


    def test_it_rides_every_org_row_so_the_map_needs_no_extra_fetch(self):
        root = self._login("root", "rootpassword")
        sent = {"labels": 11, "passives": 12, "subscribers": 13,
                "subscriber_names": 18, "drop_lines": 14}
        self._req("POST", "/api/admin/settings",
                  {"map_detail": sent}, cookie=root)
        owner = self._login("owner", "ownerpassword")
        status, body = self._req("GET", "/api/orgs", cookie=owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["orgs"][0]["map_detail"], sent)

    def test_a_worker_reads_it_too(self):
        worker = self._login("field", "fieldpassword")
        status, body = self._req("GET", "/api/orgs", cookie=worker)
        self.assertEqual(status, 200)
        self.assertEqual(body["orgs"][0]["map_detail"], mapdetail.DEFAULTS)

    def test_an_org_owner_CANNOT_change_it(self):
        owner = self._login("owner", "ownerpassword")
        status, _ = self._req("POST", "/api/admin/settings",
                              {"map_detail": {"labels": 4, "subscribers": 4,
                                              "drop_lines": 4}}, cookie=owner)
        self.assertEqual(status, 403)
        self.assertEqual(mapdetail.load(self.store), mapdetail.DEFAULTS)

    def test_an_org_owner_cannot_READ_the_platform_form(self):
        owner = self._login("owner", "ownerpassword")
        status, _ = self._req("GET", "/api/admin/settings", cookie=owner)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
