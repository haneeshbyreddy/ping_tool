import hashlib
import hmac
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
from wisp.central.store import CentralStore
from wisp.central.server import make_server

VERIFY = "verify-token-abc123"
SECRET = "app-secret-xyz789"


class WhatsappWebhookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            central_db=Path(self.tmp.name) / "central.db",
            central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_setting("whatsapp_verify_token", VERIFY)
        self.store.set_setting("whatsapp_app_secret", SECRET)

        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def _post(self, raw: bytes, sig: str | None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if sig is not None:
            headers["X-Hub-Signature-256"] = sig
        conn.request("POST", "/whatsapp/webhook", body=raw, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body


    def test_verify_echoes_challenge_on_match(self):
        status, body = self._get(
            "/whatsapp/webhook?hub.mode=subscribe"
            "&hub.challenge=CHALLENGE_42&hub.verify_token=" + VERIFY)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"CHALLENGE_42")

    def test_verify_rejects_wrong_token(self):
        status, body = self._get(
            "/whatsapp/webhook?hub.mode=subscribe"
            "&hub.challenge=CHALLENGE_42&hub.verify_token=WRONG")
        self.assertEqual(status, 403)
        self.assertNotIn(b"CHALLENGE_42", body)

    def test_verify_rejects_wrong_mode(self):
        status, _ = self._get(
            "/whatsapp/webhook?hub.mode=unsubscribe"
            "&hub.challenge=X&hub.verify_token=" + VERIFY)
        self.assertEqual(status, 403)


    def _sign(self, raw: bytes) -> str:
        return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    def test_signed_post_is_acked(self):
        raw = json.dumps({"object": "whatsapp_business_account",
                          "entry": []}).encode()
        status, body = self._post(raw, self._sign(raw))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_bad_signature_is_rejected(self):
        raw = json.dumps({"object": "whatsapp_business_account"}).encode()
        status, _ = self._post(raw, "sha256=deadbeef")
        self.assertEqual(status, 403)

    def test_missing_signature_is_rejected_when_secret_set(self):
        raw = b'{"object":"x"}'
        status, _ = self._post(raw, None)
        self.assertEqual(status, 403)


class WhatsappWebhookNoSecretTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_setting("whatsapp_verify_token", VERIFY)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def test_unsigned_post_rejected_when_no_secret(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/whatsapp/webhook", body=b'{"object":"x"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
