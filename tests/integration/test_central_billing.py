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

import hashlib
import hmac as hmac_mod

from wisp.config import Config
from wisp.central import auth, billing
from wisp.central.razorpay import RazorpayGateway
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from support import RecordingNotifier

THIS_MONTH = billing.month_key(datetime.now(timezone.utc))
NEXT_MONTH = billing.next_month(THIS_MONTH)


class FakeRazorpay(RazorpayGateway):
    """Real key/signature plumbing off the store; only the network call is
    canned. Orders get deterministic ids so tests can verify against them."""

    def __init__(self, store):
        super().__init__(store)
        self.orders = []

    def _post(self, path, payload):
        order = {"id": f"order_test{len(self.orders) + 1}", **payload,
                 "status": "created"}
        self.orders.append(order)
        return order


def _sign(order_id, payment_id, secret="sekrit"):
    return hmac_mod.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()


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


class RazorpayCheckoutTest(unittest.TestCase):
    """Self-serve checkout end-to-end with a canned gateway: order → signed
    verify → months marked + plan applied + unlock, and every rejection path
    (bad signature, wrong org, replay, unconfigured keys)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "otherowner", "otherpassword", "owner")
        self.store.set_org("ispA", name="Acme")
        self.store.set_org("ispB", name="Beta")
        self.store.set_setting("razorpay_key_id", "rzp_test_x")
        self.store.set_setting("razorpay_key_secret", "sekrit")
        self.notifier = RecordingNotifier()
        self.gateway = FakeRazorpay(self.store)
        self.server = make_server(self.cfg, self.store, notifier=self.notifier,
                                  payments=self.gateway)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    tearDown = CentralBillingHttpTest.tearDown
    _req = CentralBillingHttpTest._req
    _login = CentralBillingHttpTest._login

    def test_billing_read_carries_key_id_only_when_configured(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/billing", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["razorpay_key_id"], "rzp_test_x")
        self.store.set_setting("razorpay_key_secret", None)
        status, body, _ = self._req("GET", "/api/billing", cookie=cookie)
        self.assertIsNone(body["razorpay_key_id"])

    def test_locked_org_pays_its_way_out(self):
        self.store.set_org_plan("ispA", "pro")  # current month unpaid → locked
        cookie = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 402)
        # the checkout routes stay reachable through the lock
        status, order, _ = self._req("POST", "/api/billing/order",
                                     {"months": 2}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(order["amount"], 2 * 2000 * 100)
        self.assertEqual(order["months"], [THIS_MONTH, NEXT_MONTH])
        self.assertEqual(order["key_id"], "rzp_test_x")
        status, body, _ = self._req(
            "POST", "/api/billing/verify",
            {"razorpay_order_id": order["order_id"],
             "razorpay_payment_id": "pay_1",
             "razorpay_signature": _sign(order["order_id"], "pay_1")},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(body["locked"])
        self.assertIn(THIS_MONTH, body["paid_months"])
        self.assertIn(NEXT_MONTH, body["paid_months"])
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 200)
        # the admin heads-up rode the central channel
        self.assertTrue(any("paid" in n["title"] for n in self.notifier.sent))

    def test_free_org_upgrades_by_paying(self):
        cookie = self._login("owner", "ownerpassword")
        status, order, _ = self._req("POST", "/api/billing/order",
                                     {"plan": "vip", "months": 1}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(order["amount"], 3000 * 100)
        status, body, _ = self._req(
            "POST", "/api/billing/verify",
            {"razorpay_order_id": order["order_id"],
             "razorpay_payment_id": "pay_9",
             "razorpay_signature": _sign(order["order_id"], "pay_9")},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], "vip")
        self.assertFalse(body["locked"])
        self.assertEqual(self.store.org_plan("ispA"), "vip")

    def test_bad_signature_marks_nothing(self):
        self.store.set_org_plan("ispA", "pro")
        cookie = self._login("owner", "ownerpassword")
        _, order, _ = self._req("POST", "/api/billing/order", {}, cookie=cookie)
        status, _, _ = self._req(
            "POST", "/api/billing/verify",
            {"razorpay_order_id": order["order_id"],
             "razorpay_payment_id": "pay_1",
             "razorpay_signature": _sign(order["order_id"], "pay_1", "wrong")},
            cookie=cookie)
        self.assertEqual(status, 422)
        self.assertEqual(self.store.paid_months("ispA"), set())
        # a good signature still settles the same order afterwards
        status, body, _ = self._req(
            "POST", "/api/billing/verify",
            {"razorpay_order_id": order["order_id"],
             "razorpay_payment_id": "pay_1",
             "razorpay_signature": _sign(order["order_id"], "pay_1")},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(THIS_MONTH, body["paid_months"])

    def test_verify_is_idempotent_and_org_scoped(self):
        self.store.set_org_plan("ispA", "pro")
        cookie = self._login("owner", "ownerpassword")
        _, order, _ = self._req("POST", "/api/billing/order", {}, cookie=cookie)
        payload = {"razorpay_order_id": order["order_id"],
                   "razorpay_payment_id": "pay_1",
                   "razorpay_signature": _sign(order["order_id"], "pay_1")}
        # another org's owner can't settle (or even see) this order
        other = self._login("otherowner", "otherpassword")
        self.assertEqual(self._req("POST", "/api/billing/verify", payload,
                                   cookie=other)[0], 404)
        self.assertEqual(self._req("POST", "/api/billing/verify", payload,
                                   cookie=cookie)[0], 200)
        # replaying the settled order is a no-op 200, months stay marked once
        status, body, _ = self._req("POST", "/api/billing/verify", payload,
                                    cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(THIS_MONTH, body["paid_months"])

    def test_order_validation_and_owner_only(self):
        cookie = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("POST", "/api/billing/order",
                                   {"plan": "free"}, cookie=cookie)[0], 422)
        self.assertEqual(self._req("POST", "/api/billing/order",
                                   {"plan": "pro", "months": 0},
                                   cookie=cookie)[0], 422)
        self.assertEqual(self._req("POST", "/api/billing/order",
                                   {"plan": "pro", "months": 13},
                                   cookie=cookie)[0], 422)
        # non-owner roles can't start a checkout
        auth.create_user(self.store, "ispA", "tech1", "techpassword", "tech")
        vcookie = self._login("tech1", "techpassword")
        self.assertEqual(self._req("POST", "/api/billing/order",
                                   {"plan": "pro"}, cookie=vcookie)[0], 403)

    def test_locked_org_can_escape_to_free(self):
        self.store.set_org_plan("ispA", "vip")  # unpaid month → locked
        cookie = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 402)
        # /api/billing/plan stays reachable through the lock
        status, body, _ = self._req("POST", "/api/billing/plan",
                                    {"plan": "free"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], "free")
        self.assertFalse(body["locked"])
        self.assertEqual(self.store.org_plan("ispA"), "free")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0], 200)
        # the churn heads-up rode the central channel
        self.assertTrue(any("Free" in n["title"] for n in self.notifier.sent))

    def test_plan_route_never_grants_a_paid_plan(self):
        cookie = self._login("owner", "ownerpassword")
        for plan in ("pro", "vip", "gold"):
            status, _, _ = self._req("POST", "/api/billing/plan",
                                     {"plan": plan}, cookie=cookie)
            self.assertEqual(status, 422, plan)
        self.assertEqual(self.store.org_plan("ispA"), "free")
        # owner-only, like every org write
        auth.create_user(self.store, "ispA", "tech2", "techpassword", "tech")
        vcookie = self._login("tech2", "techpassword")
        self.assertEqual(self._req("POST", "/api/billing/plan",
                                   {"plan": "free"}, cookie=vcookie)[0], 403)

    def test_paid_plan_switch_goes_through_checkout(self):
        # a pro org moves to vip by paying the vip price; plan flips on verify
        self.store.set_org_plan("ispA", "pro")
        self.store.set_billing_month("ispA", THIS_MONTH, True)
        cookie = self._login("owner", "ownerpassword")
        status, order, _ = self._req("POST", "/api/billing/order",
                                     {"plan": "vip"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(order["amount"], 3000 * 100)
        # vip's runway starts at the first month vip hasn't covered: next month
        self.assertEqual(order["months"], [NEXT_MONTH])
        status, body, _ = self._req(
            "POST", "/api/billing/verify",
            {"razorpay_order_id": order["order_id"],
             "razorpay_payment_id": "pay_up",
             "razorpay_signature": _sign(order["order_id"], "pay_up")},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], "vip")
        self.assertEqual(self.store.org_plan("ispA"), "vip")

    def test_unconfigured_gateway_rejects_orders(self):
        self.store.set_setting("razorpay_key_id", None)
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/billing/order",
                                    {"plan": "pro"}, cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("not configured", body["error"])


if __name__ == "__main__":
    unittest.main()
