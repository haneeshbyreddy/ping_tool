"""Paywall end-to-end: the 402 lock gate, the device cap, and the superadmin
billing controls — against a live central server, no clock mocking (lock state
derives from the REAL current month, so tests mark/unmark that month)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import auth, billing
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from support import RecordingNotifier

THIS_MONTH = billing.month_key(datetime.now(timezone.utc))


class CentralBillingHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.store.set_org("ispA", name="Acme")
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

    def _req(self, method, path, body=None, cookie=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
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
        status, _, setcookie = self._req("POST", "/api/login",
                                         {"username": username, "password": password})
        self.assertEqual(status, 200)
        return setcookie.split(";")[0]

    # ----- reads -------------------------------------------------------------

    def test_billing_read_free_org(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/billing", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], "free")
        self.assertEqual(body["status"], "free")
        self.assertEqual(body["device_cap"], 5)
        self.assertEqual(body["gpay_number"], billing.DEFAULT_GPAY_NUMBER)
        self.assertIn("pro", body["plans"])

    # ----- the 402 lock gate -------------------------------------------------

    def test_locked_org_gets_402_except_me_and_billing(self):
        self.store.set_org_plan("ispA", "pro")  # no months paid → locked now
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/inventory", cookie=cookie)
        self.assertEqual(status, 402)
        self.assertTrue(body["locked"])
        self.assertEqual(self._req("POST", "/api/inventory",
                                   {"name": "r1", "ip_address": "10.0.0.1"},
                                   cookie=cookie)[0], 402)
        # the lock screen's lifelines stay open
        self.assertEqual(self._req("GET", "/api/me", cookie=cookie)[0], 200)
        status, body, _ = self._req("GET", "/api/billing", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "locked")
        self.assertEqual(self._req("POST", "/api/logout", cookie=cookie)[0], 200)

    def test_superadmin_never_locked_and_edge_ingest_unaffected(self):
        self.store.set_org_plan("ispA", "pro")
        cookie = self._login("root", "rootpassword")
        self.assertEqual(self._req("GET", "/api/inventory?org=ispA",
                                   cookie=cookie)[0], 200)
        # the edge keeps fetching topology with the global token — monitoring
        # survives a lapsed bill by design
        self.assertEqual(self._req("GET", "/edge/devices?org_id=ispA",
                                   token="tok")[0], 200)

    def test_marking_the_month_paid_unlocks(self):
        self.store.set_org_plan("ispA", "pro")
        cookie = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 402)
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/admin/billing",
                                    {"org_id": "ispA", "month": THIS_MONTH, "paid": True},
                                    cookie=root)
        self.assertEqual(status, 200)
        self.assertIn(THIS_MONTH, body["paid_months"])
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 200)

    # ----- admin billing writes ----------------------------------------------

    def test_admin_billing_is_superadmin_only(self):
        cookie = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/admin/billing",
                                 {"org_id": "ispA", "plan": "vip"}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.org_plan("ispA"), "free")

    def test_admin_sets_plan_and_validates_input(self):
        root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/admin/billing",
                                    {"org_id": "ispA", "plan": "vip"}, cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], "vip")
        self.assertEqual(self._req("POST", "/api/admin/billing",
                                   {"org_id": "ispA", "plan": "gold"},
                                   cookie=root)[0], 422)
        self.assertEqual(self._req("POST", "/api/admin/billing",
                                   {"org_id": "ispA", "month": "2026-13", "paid": True},
                                   cookie=root)[0], 422)
        self.assertEqual(self._req("POST", "/api/admin/billing",
                                   {"org_id": "nope", "plan": "pro"},
                                   cookie=root)[0], 404)

    def test_admin_can_unmark_a_month(self):
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "plan": "pro"}, cookie=root)
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "month": THIS_MONTH, "paid": True}, cookie=root)
        status, body, _ = self._req("POST", "/api/admin/billing",
                                    {"org_id": "ispA", "month": THIS_MONTH, "paid": False},
                                    cookie=root)
        self.assertEqual(status, 200)
        self.assertNotIn(THIS_MONTH, body["paid_months"])
        self.assertTrue(body["locked"])

    # ----- probe cap ----------------------------------------------------------

    def test_free_plan_caps_at_one_probe(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/nodes",
                                    {"org_id": "ispA", "node_id": "edge-1"},
                                    cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("token", body)
        status, body, _ = self._req("POST", "/api/nodes",
                                    {"org_id": "ispA", "node_id": "edge-2"},
                                    cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("1 edge probe", body["error"])
        # rotating the LIVE probe stays free (not a new slot)
        self.assertEqual(self._req("POST", "/api/nodes/rotate",
                                   {"org_id": "ispA", "node_id": "edge-1"},
                                   cookie=cookie)[0], 200)
        # revoking frees the slot; re-activating it via rotate re-occupies it
        self.assertEqual(self._req("POST", "/api/nodes/revoke",
                                   {"org_id": "ispA", "node_id": "edge-1"},
                                   cookie=cookie)[0], 200)
        self.assertEqual(self._req("POST", "/api/nodes",
                                   {"org_id": "ispA", "node_id": "edge-2"},
                                   cookie=cookie)[0], 200)
        status, body, _ = self._req("POST", "/api/nodes/rotate",
                                    {"org_id": "ispA", "node_id": "edge-1"},
                                    cookie=cookie)
        self.assertEqual(status, 422)
        # upgrading lifts the cap (month marked so pro doesn't lock)
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "plan": "pro"}, cookie=root)
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "month": THIS_MONTH, "paid": True}, cookie=root)
        self.assertEqual(self._req("POST", "/api/nodes",
                                   {"org_id": "ispA", "node_id": "edge-3"},
                                   cookie=cookie)[0], 200)

    # ----- device cap ---------------------------------------------------------

    def test_free_plan_caps_at_5_devices_passives_exempt(self):
        cookie = self._login("owner", "ownerpassword")
        for i in range(5):
            status, _, _ = self._req("POST", "/api/inventory",
                                     {"name": f"d{i}", "ip_address": f"10.0.{i // 250}.{i % 250 + 1}"},
                                     cookie=cookie)
            self.assertEqual(status, 200, f"device {i}")
        status, body, _ = self._req("POST", "/api/inventory",
                                    {"name": "d5", "ip_address": "10.0.1.1"},
                                    cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("5", body["error"])
        # passive plant is documentation, never metered
        status, _, _ = self._req("POST", "/api/inventory",
                                 {"name": "spl-1", "device_type": "splitter"},
                                 cookie=cookie)
        self.assertEqual(status, 200)
        # upgrading lifts the cap (pro month marked so the org isn't locked)
        root = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "plan": "pro"}, cookie=root)
        self._req("POST", "/api/admin/billing",
                  {"org_id": "ispA", "month": THIS_MONTH, "paid": True}, cookie=root)
        status, _, _ = self._req("POST", "/api/inventory",
                                 {"name": "d5", "ip_address": "10.0.1.1"},
                                 cookie=cookie)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
