"""Per-device web-UI credentials: the owner stores a switch/OLT login, it lands
ENCRYPTED at rest, the plaintext never rides back to the browser, and non-owners
are locked out. Exercises the real central HTTP API end to end."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth, secretbox
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        self.store.set_org("ispB")
        self.device_id = self.store.create_org_device("ispA", {
            "name": "OLT", "ip_address": "172.168.107.244", "device_type": "olt",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "op1", "operatorpassword", "operator")
        auth.create_user(self.store, "ispB", "ownerB", "ownerBpassword", "owner")

        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.srv_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.srv_thread.start()
        self.cookie = self._login("owner", "ownerpassword")

    def tearDown(self):
        self.server.shutdown()
        self.srv_thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _login(self, username, password) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        self.assertIsNotNone(cookie)
        return cookie.split(";")[0]

    def _api(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json",
                              "Cookie": cookie or self.cookie})
        resp = conn.getresponse()
        doc = json.loads(resp.read() or "{}")
        conn.close()
        return resp.status, doc

    def _get_creds(self, cookie=None):
        return self._api("GET",
                         f"/api/inventory/credentials?device_id={self.device_id}",
                         cookie=cookie)

    # -- tests -----------------------------------------------------------------

    def test_empty_before_set(self):
        status, doc = self._get_creds()
        self.assertEqual(status, 200)
        self.assertEqual(doc["credentials"],
                         {"username": "", "has_password": False,
                          "auth_mode": "form", "updated_by": None,
                          "updated_at": None})

    def test_set_stores_encrypted_and_hides_password(self):
        status, doc = self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin",
            "password": "sravani@1987"})
        self.assertEqual((status, doc), (200, {"ok": True}))

        # GET never reveals the plaintext, only that one is set
        status, doc = self._get_creds()
        self.assertEqual(doc["credentials"]["username"], "admin")
        self.assertTrue(doc["credentials"]["has_password"])
        self.assertEqual(doc["credentials"]["updated_by"], "owner")
        self.assertNotIn("password", doc["credentials"])
        self.assertNotIn("password_enc", doc["credentials"])

        # at rest: the stored blob is ciphertext, not the plaintext, and it
        # decrypts back with central's own key file
        row = self.store.get_device_webui_credentials("ispA", self.device_id)
        self.assertNotIn("sravani@1987", row["password_enc"])
        box = secretbox.from_config(self.cfg)
        self.assertEqual(box.decrypt(row["password_enc"]), "sravani@1987")

    def test_username_only_edit_keeps_password(self):
        self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin",
            "password": "sravani@1987"})
        # password key omitted -> leave it untouched, just rename the user
        status, doc = self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "root"})
        self.assertEqual(status, 200)
        _, doc = self._get_creds()
        self.assertEqual(doc["credentials"]["username"], "root")
        self.assertTrue(doc["credentials"]["has_password"])

    def test_empty_password_clears_it(self):
        self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin",
            "password": "sravani@1987"})
        status, _ = self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin", "password": ""})
        self.assertEqual(status, 200)
        _, doc = self._get_creds()
        self.assertEqual(doc["credentials"]["username"], "admin")
        self.assertFalse(doc["credentials"]["has_password"])

    def test_clear_removes_row(self):
        self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin",
            "password": "sravani@1987"})
        status, doc = self._api("POST", "/api/inventory/credentials/clear",
                                {"device_id": self.device_id})
        self.assertEqual((status, doc), (200, {"ok": True}))
        self.assertIsNone(
            self.store.get_device_webui_credentials("ispA", self.device_id))

    def test_operator_cannot_read_or_write(self):
        op = self._login("op1", "operatorpassword")
        status, _ = self._get_creds(cookie=op)
        self.assertEqual(status, 403)
        status, _ = self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "x", "password": "y"},
            cookie=op)
        self.assertEqual(status, 403)

    def test_other_org_owner_denied(self):
        other = self._login("ownerB", "ownerBpassword")
        status, _ = self._get_creds(cookie=other)
        self.assertEqual(status, 403)
        status, _ = self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "x", "password": "y"},
            cookie=other)
        self.assertEqual(status, 403)

    def test_deleting_device_purges_credentials(self):
        self._api("POST", "/api/inventory/credentials", {
            "device_id": self.device_id, "username": "admin",
            "password": "sravani@1987"})
        # hard-delete the device; the FK-referenced credential row must go too
        result = self.store.delete_org_device("ispA", self.device_id)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(
            self.store.get_device_webui_credentials("ispA", self.device_id))


if __name__ == "__main__":
    unittest.main()
