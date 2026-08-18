"""The payment-gateway seam: provider_settings / get_provider / RazorpayProvider.

No real network: create_order talks to a local http.server double via the
adapter's base_url instance attribute. Signature tests compute their expected
HMACs with the stdlib in the test itself, never from copied constants.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import payments
from wisp.central.payments import (PaymentError, RazorpayProvider, get_provider,
                                   provider_settings)
from wisp.central.secretbox import SecretBox

KEY_SECRET = "rzp_live_s3cret"
WEBHOOK_SECRET = "whsec_wisp"


class FakeStore:
    """app_settings as a dict; the only method the seam reads is get_setting."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key):
        return self.settings.get(key)


def _box() -> SecretBox:
    return SecretBox(b"k" * 32)


def _provider(webhook_secret=WEBHOOK_SECRET) -> RazorpayProvider:
    return RazorpayProvider(key_id="rzp_test_key", key_secret=KEY_SECRET,
                            webhook_secret=webhook_secret)


def _return_sig(secret: str, order_id: str, payment_id: str) -> str:
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()


def _webhook_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class ReturnVerifyTest(unittest.TestCase):
    def setUp(self):
        self.p = _provider()
        self.params = {
            "razorpay_order_id": "order_MNO123",
            "razorpay_payment_id": "pay_ABC789",
            "razorpay_signature": _return_sig(KEY_SECRET, "order_MNO123",
                                              "pay_ABC789"),
        }

    def test_a_valid_signature_verifies(self):
        self.assertTrue(self.p.verify_return(self.params))

    def test_a_tampered_signature_fails(self):
        sig = self.params["razorpay_signature"]
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        self.params["razorpay_signature"] = flipped
        self.assertFalse(self.p.verify_return(self.params))

    def test_a_signature_for_another_payment_fails(self):
        self.params["razorpay_payment_id"] = "pay_OTHER"
        self.assertFalse(self.p.verify_return(self.params))

    def test_each_missing_param_fails(self):
        for key in list(self.params):
            with self.subTest(missing=key):
                short = dict(self.params)
                del short[key]
                self.assertFalse(self.p.verify_return(short))

    def test_empty_params_fail(self):
        self.assertFalse(self.p.verify_return({}))


def _event(kind="payment.captured", entity="default"):
    if entity == "default":
        entity = {"id": "pay_ABC789", "order_id": "order_MNO123",
                  "amount": 50000, "notes": {"org_id": "hansanet"}}
    payload = {} if entity is None else {"payment": {"entity": entity}}
    return {"event": kind, "payload": payload}


class WebhookVerifyTest(unittest.TestCase):
    def setUp(self):
        self.p = _provider()

    def _deliver(self, event, secret=WEBHOOK_SECRET, header="X-Razorpay-Signature"):
        body = json.dumps(event).encode()
        return self.p.verify_webhook({header: _webhook_sig(secret, body)}, body)

    def test_captured_normalizes_fully(self):
        self.assertEqual(self._deliver(_event()), {
            "org_id": "hansanet", "payment_id": "pay_ABC789",
            "order_id": "order_MNO123", "paise": 50000, "status": "captured"})

    def test_header_lookup_is_case_insensitive(self):
        got = self._deliver(_event(), header="X-RAZORPAY-SIGNATURE")
        self.assertEqual(got["status"], "captured")

    def test_failed_event_maps_to_failed(self):
        self.assertEqual(self._deliver(_event(kind="payment.failed"))["status"],
                         "failed")

    def test_unknown_event_maps_to_other(self):
        got = self._deliver(_event(kind="refund.created", entity=None))
        self.assertEqual(got, {"org_id": None, "payment_id": "", "order_id": None,
                               "paise": 0, "status": "other"})

    def test_missing_org_note_yields_none(self):
        entity = {"id": "pay_1", "order_id": "order_1", "amount": 100, "notes": {}}
        got = self._deliver(_event(entity=entity))
        self.assertIsNone(got["org_id"])
        self.assertEqual(got["paise"], 100)

    def test_a_tampered_body_yields_none(self):
        body = json.dumps(_event()).encode()
        sig = _webhook_sig(WEBHOOK_SECRET, body + b" ")
        self.assertIsNone(self.p.verify_webhook({"X-Razorpay-Signature": sig}, body))

    def test_a_signature_from_the_wrong_secret_yields_none(self):
        self.assertIsNone(self._deliver(_event(), secret="whsec_other"))

    def test_a_missing_header_yields_none(self):
        body = json.dumps(_event()).encode()
        self.assertIsNone(self.p.verify_webhook({}, body))
        self.assertIsNone(self.p.verify_webhook({"X-Other": "x"}, body))

    def test_no_webhook_secret_configured_yields_none_always(self):
        p = _provider(webhook_secret=None)
        body = json.dumps(_event()).encode()
        headers = {"X-Razorpay-Signature": _webhook_sig(WEBHOOK_SECRET, body)}
        self.assertIsNone(p.verify_webhook(headers, body))

    def test_a_signed_but_unparseable_body_yields_none(self):
        body = b"not json at all"
        headers = {"X-Razorpay-Signature": _webhook_sig(WEBHOOK_SECRET, body)}
        self.assertIsNone(self.p.verify_webhook(headers, body))


class _ApiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        self.server.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw.decode("utf-8")),
        })
        status, payload = self.server.reply
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class CreateOrderTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _ApiHandler)
        self.server.requests = []
        self.server.reply = (200, {"id": "order_LIVE1"})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.p = _provider()
        self.p.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_the_happy_path_sends_auth_paise_and_the_org_note(self):
        got = self.p.create_order("hansanet", 50000, "wisp-2026-08")
        self.assertEqual(got, {"provider": "razorpay", "key_id": "rzp_test_key",
                               "order_id": "order_LIVE1", "amount": 50000,
                               "currency": "INR"})
        req = self.server.requests[0]
        self.assertEqual(req["path"], "/v1/orders")
        expect_auth = "Basic " + base64.b64encode(
            f"rzp_test_key:{KEY_SECRET}".encode()).decode("ascii")
        self.assertEqual(req["auth"], expect_auth)
        # Amounts are already paise: 50000 travels verbatim, never x100.
        self.assertEqual(req["body"], {"amount": 50000, "currency": "INR",
                                       "receipt": "wisp-2026-08",
                                       "notes": {"org_id": "hansanet"}})

    def test_an_api_failure_surfaces_the_gateway_sentence(self):
        self.server.reply = (401, {"error": {"description": "Authentication failed"}})
        with self.assertRaises(PaymentError) as ctx:
            self.p.create_order("hansanet", 50000, "r1")
        msg = str(ctx.exception)
        self.assertIn("Could not create the payment order", msg)
        self.assertIn("Authentication failed", msg)

    def test_a_reply_without_an_order_id_is_an_error(self):
        self.server.reply = (200, {"amount": 50000})
        with self.assertRaises(PaymentError):
            self.p.create_order("hansanet", 50000, "r1")

    def test_an_unreachable_gateway_is_a_payment_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.p.base_url = "http://127.0.0.1:%d" % dead_port
        with self.assertRaises(PaymentError) as ctx:
            self.p.create_order("hansanet", 50000, "r1")
        self.assertIn("Could not create the payment order", str(ctx.exception))

    def test_a_non_positive_amount_is_refused_locally(self):
        with self.assertRaises(PaymentError):
            self.p.create_order("hansanet", 0, "r1")
        self.assertEqual(self.server.requests, [])


def _configured_store(box, provider="razorpay", key_id="rzp_test_key",
                      key_secret=KEY_SECRET, webhook_secret=WEBHOOK_SECRET):
    settings = {}
    if provider is not None:
        settings[payments.PROVIDER_KEY] = provider
    if key_id is not None:
        settings[payments.KEY_ID_KEY] = key_id
    if key_secret is not None:
        settings[payments.KEY_SECRET_KEY] = box.encrypt(key_secret)
    if webhook_secret is not None:
        settings[payments.WEBHOOK_SECRET_KEY] = box.encrypt(webhook_secret)
    return FakeStore(settings)


class ProviderSettingsTest(unittest.TestCase):
    def setUp(self):
        self.box = _box()

    def test_all_configured_decrypts_both_secrets(self):
        store = _configured_store(self.box)
        self.assertEqual(provider_settings(store, self.box), {
            "provider": "razorpay", "key_id": "rzp_test_key",
            "key_secret": KEY_SECRET, "webhook_secret": WEBHOOK_SECRET})

    def test_an_empty_store_is_all_none(self):
        self.assertEqual(provider_settings(FakeStore(), self.box), {
            "provider": None, "key_id": None,
            "key_secret": None, "webhook_secret": None})

    def test_an_undecryptable_token_degrades_that_field_only(self):
        store = _configured_store(self.box)
        store.settings[payments.KEY_SECRET_KEY] = "garbage-not-a-token"
        got = provider_settings(store, self.box)
        self.assertIsNone(got["key_secret"])
        self.assertEqual(got["webhook_secret"], WEBHOOK_SECRET)
        self.assertEqual(got["provider"], "razorpay")

    def test_a_token_from_another_key_degrades_too(self):
        other = SecretBox(b"x" * 32)
        store = _configured_store(self.box)
        store.settings[payments.WEBHOOK_SECRET_KEY] = other.encrypt(WEBHOOK_SECRET)
        got = provider_settings(store, self.box)
        self.assertIsNone(got["webhook_secret"])
        self.assertEqual(got["key_secret"], KEY_SECRET)

    def test_a_blank_provider_reads_as_none(self):
        store = _configured_store(self.box, provider="  ")
        self.assertIsNone(provider_settings(store, self.box)["provider"])


class GetProviderTest(unittest.TestCase):
    def setUp(self):
        self.box = _box()
        payments._warned_unknown.clear()

    def test_dormant_until_fully_configured(self):
        cases = {
            "empty": FakeStore(),
            "provider only": _configured_store(self.box, key_id=None,
                                               key_secret=None, webhook_secret=None),
            "no key secret": _configured_store(self.box, key_secret=None,
                                               webhook_secret=None),
            "no key id": _configured_store(self.box, key_id=None),
        }
        for label, store in cases.items():
            with self.subTest(label):
                self.assertIsNone(get_provider(store, self.box))

    def test_an_undecryptable_key_secret_stays_dormant(self):
        store = _configured_store(self.box)
        store.settings[payments.KEY_SECRET_KEY] = "garbage"
        self.assertIsNone(get_provider(store, self.box))

    def test_fully_configured_yields_a_working_razorpay_adapter(self):
        p = get_provider(_configured_store(self.box), self.box)
        self.assertIsInstance(p, RazorpayProvider)
        self.assertEqual(p.name, "razorpay")
        self.assertEqual(p.key_id, "rzp_test_key")
        # Behavior, not attributes: the decrypted secrets actually verify.
        self.assertTrue(p.verify_return({
            "razorpay_order_id": "o1", "razorpay_payment_id": "p1",
            "razorpay_signature": _return_sig(KEY_SECRET, "o1", "p1")}))
        body = json.dumps(_event()).encode()
        got = p.verify_webhook(
            {"X-Razorpay-Signature": _webhook_sig(WEBHOOK_SECRET, body)}, body)
        self.assertEqual(got["status"], "captured")

    def test_no_webhook_secret_still_enables_checkout_but_not_webhooks(self):
        store = _configured_store(self.box, webhook_secret=None)
        p = get_provider(store, self.box)
        self.assertIsInstance(p, RazorpayProvider)
        body = json.dumps(_event()).encode()
        headers = {"X-Razorpay-Signature": _webhook_sig(WEBHOOK_SECRET, body)}
        self.assertIsNone(p.verify_webhook(headers, body))

    def test_an_unknown_provider_is_none_and_logs_once(self):
        store = _configured_store(self.box, provider="payu")
        with self.assertLogs("wisp.payments", level="WARNING") as logs:
            self.assertIsNone(get_provider(store, self.box))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("payu", logs.output[0])
        with self.assertNoLogs("wisp.payments", level="WARNING"):
            self.assertIsNone(get_provider(store, self.box))


if __name__ == "__main__":
    unittest.main()
