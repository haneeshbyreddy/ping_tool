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
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from support import RecordingNotifier


def _device(name, ip):
    return {"name": name, "ip_address": ip, "device_type": "switch", "region": None,
            "tags": None, "parent_device_id": None, "assigned_node_id": None,
            "gpon_vendor": None, "pon_port": None}


class OrgDeleteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "aowner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "ownerpassword", "owner")

        self.dev_a = self.store.create_org_device("ispA", _device("swA", "10.0.0.1"))
        self.store.open_outage_if_absent("ispA", self.dev_a, "2026-07-21T00:00:00+00:00", "DOWN")
        self.store.touch_node("ispA", "edge-a")
        self.store.issue_node_token("ispA", "edge-a")
        self.store.set_billing_month("ispA", "2026-07", True)
        self.store.set_org(orgA := "ispA", name="ISP A")
        self.assertTrue(orgA)

        self.dev_b = self.store.create_org_device("ispB", _device("swB", "10.0.1.1"))
        self.store.touch_node("ispB", "edge-b")

        self.profile = self.store.create_snmp_profile(None, {
            "name": "global-vendor", "match_sysobjectid": "1.3.6.1.4.1.9",
            "metrics": {}, "enabled": True})

        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
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
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

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

    def _delete(self, org, cookie, confirm=None):
        return self._req("POST", "/api/orgs/delete",
                         {"org_id": org, "confirm": org if confirm is None else confirm},
                         cookie=cookie)

    def test_superadmin_delete_sweeps_every_org_scoped_table(self):
        cookie = self._login("root", "rootpassword")
        status, body = self._delete("ispA", cookie)
        self.assertEqual(status, 200)
        self.assertFalse(self.store.org_exists("ispA"))
        with self.store._connect() as conn:
            for table in ("org_devices", "outages", "nodes", "node_tokens",
                          "users", "org_billing_months"):
                left = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE org_id='ispA'").fetchone()[0]
                self.assertEqual(left, 0, f"{table} still holds ispA rows")
        self.assertIn("org_devices", body["deleted"])

    def test_bystander_org_and_global_rows_survive(self):
        cookie = self._login("root", "rootpassword")
        self._delete("ispA", cookie)
        self.assertTrue(self.store.org_exists("ispB"))
        self.assertEqual(len(self.store.list_org_devices("ispB")), 1)
        self.assertIsNotNone(self.store.get_user_by_username("root"))
        names = [p["name"] for p in self.store.list_snmp_profiles(None)]
        self.assertIn("global-vendor", names)

    def test_confirm_must_echo_the_org_id(self):
        cookie = self._login("root", "rootpassword")
        status, body = self._delete("ispA", cookie, confirm="")
        self.assertEqual(status, 422)
        status, _ = self._delete("ispA", cookie, confirm="ispB")
        self.assertEqual(status, 422)
        self.assertTrue(self.store.org_exists("ispA"))

    def test_org_owner_cannot_delete_any_org(self):
        cookie = self._login("aowner", "ownerpassword")
        status, _ = self._delete("ispA", cookie)
        self.assertEqual(status, 403)
        status, _ = self._delete("ispB", cookie)
        self.assertEqual(status, 403)
        self.assertTrue(self.store.org_exists("ispA"))
        self.assertTrue(self.store.org_exists("ispB"))

    def test_anonymous_is_401(self):
        status, _ = self._delete("ispA", None)
        self.assertEqual(status, 401)
        self.assertTrue(self.store.org_exists("ispA"))

    def test_unknown_org_is_404(self):
        cookie = self._login("root", "rootpassword")
        status, _ = self._delete("nosuchorg", cookie)
        self.assertEqual(status, 404)

    def test_registry_forgets_the_deleted_org(self):
        registry = self.server.registry if hasattr(self.server, "registry") else None
        from wisp.central.engine import EngineRegistry
        reg = registry or EngineRegistry(self.store, self.cfg)
        reg.get("ispA")
        self.assertIn("ispA", reg._engines)
        reg.forget("ispA")
        self.assertNotIn("ispA", reg._engines)
        self.assertNotIn("ispA", reg._fingerprints)


if __name__ == "__main__":
    unittest.main()
