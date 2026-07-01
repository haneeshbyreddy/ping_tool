"""Self-service node enrollment end to end: an ISP owner/operator registers a new edge
node from the dashboard (`POST /api/nodes`) instead of a platform superadmin running
`central.admin enroll-edge`, and the token that comes back actually authenticates
ingest — over a real socket, mirroring tests/integration/test_central_auth.py's and
test_central_mtls.py's style. central/pki.py's mTLS path is untouched and still works;
this is the third, simpler option alongside it.
"""
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
from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class NodeEnrollmentHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0, central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "oper", "operpassword", "operator")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
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
        status, body, setcookie = self._req("POST", "/api/login",
                                            {"username": username, "password": password})
        return status, (setcookie.split(";")[0] if setcookie else None)

    def _report(self, tenant, node, token=None, loss=0.0):
        body = {"v": 1, "tenant_id": tenant, "node_id": node,
                "pings": {"10.0.0.1": {"loss_pct": loss, "latency_ms": None if loss else 5.0}}}
        status, resp_body, _ = self._req("POST", "/report", body, token=token)
        return status, resp_body

    # --- write gating ---
    def test_register_requires_owner_or_superadmin(self):
        _, cookie = self._login("oper", "operpassword")
        status, body, _ = self._req("POST", "/api/nodes",
                                    {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        self.assertEqual(status, 403)

    def test_register_requires_login(self):
        status, _, _ = self._req("POST", "/api/nodes", {"tenant_id": "ispA", "node_id": "edge-a1"})
        self.assertEqual(status, 401)

    # --- registration flow ---
    def test_owner_registers_and_gets_a_token_once(self):
        _, cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/nodes",
                                    {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["node_id"], "edge-a1")
        self.assertTrue(len(body["token"]) > 20)

    def test_duplicate_register_is_rejected(self):
        _, cookie = self._login("owner", "ownerpassword")
        self._req("POST", "/api/nodes", {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, body, _ = self._req("POST", "/api/nodes",
                                    {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        self.assertEqual(status, 422)

    def test_bad_node_id_rejected(self):
        _, cookie = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/nodes",
                                 {"tenant_id": "ispA", "node_id": "not a valid id!"}, cookie=cookie)
        self.assertEqual(status, 422)

    def test_rotate_requires_existing_registration(self):
        _, cookie = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/nodes/rotate",
                                 {"tenant_id": "ispA", "node_id": "ghost"}, cookie=cookie)
        self.assertEqual(status, 422)

    def test_rotate_replaces_the_token(self):
        _, cookie = self._login("owner", "ownerpassword")
        _, first, _ = self._req("POST", "/api/nodes",
                                {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, second, _ = self._req("POST", "/api/nodes/rotate",
                                      {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotEqual(first["token"], second["token"])
        # the old token no longer authenticates ingest
        status, _ = self._report("ispA", "edge-a1", token=first["token"])
        self.assertEqual(status, 401)
        status, _ = self._report("ispA", "edge-a1", token=second["token"])
        self.assertEqual(status, 200)

    def test_revoke_unregistered_is_404(self):
        _, cookie = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/nodes/revoke",
                                 {"tenant_id": "ispA", "node_id": "ghost"}, cookie=cookie)
        self.assertEqual(status, 404)

    def test_list_is_tenant_scoped(self):
        _, cookie_a = self._login("owner", "ownerpassword")
        _, cookie_b = self._login("bowner", "bownerpassword")
        self._req("POST", "/api/nodes", {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie_a)
        self._req("POST", "/api/nodes", {"tenant_id": "ispB", "node_id": "edge-b1"}, cookie=cookie_b)
        _, body, _ = self._req("GET", "/api/nodes", cookie=cookie_a)
        self.assertEqual([n["node_id"] for n in body["nodes"]], ["edge-a1"])
        _, body, _ = self._req("GET", "/api/nodes", cookie=cookie_b)
        self.assertEqual([n["node_id"] for n in body["nodes"]], ["edge-b1"])

    # --- the issued token actually authenticates ingest ---
    def test_issued_token_authenticates_report_and_edge_devices(self):
        _, cookie = self._login("owner", "ownerpassword")
        _, reg, _ = self._req("POST", "/api/nodes",
                              {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, body, _ = self._req("GET", f"/edge/devices?tenant_id=ispA", token=reg["token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"][0]["ip_address"], "10.0.0.1")
        status, _ = self._report("ispA", "edge-a1", token=reg["token"])
        self.assertEqual(status, 200)

    def test_token_does_not_authenticate_a_different_tenant(self):
        _, cookie = self._login("owner", "ownerpassword")
        _, reg, _ = self._req("POST", "/api/nodes",
                              {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, _ = self._report("ispB", "edge-a1", token=reg["token"])
        self.assertEqual(status, 401)

    def test_token_does_not_authenticate_a_different_node_same_tenant(self):
        _, cookie = self._login("owner", "ownerpassword")
        _, reg, _ = self._req("POST", "/api/nodes",
                              {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, _ = self._report("ispA", "edge-a2", token=reg["token"])
        self.assertEqual(status, 401)

    def test_revoked_token_stops_authenticating(self):
        _, cookie = self._login("owner", "ownerpassword")
        _, reg, _ = self._req("POST", "/api/nodes",
                              {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        self._req("POST", "/api/nodes/revoke",
                 {"tenant_id": "ispA", "node_id": "edge-a1"}, cookie=cookie)
        status, _ = self._report("ispA", "edge-a1", token=reg["token"])
        self.assertEqual(status, 401)

    def test_global_token_still_works_alongside_self_service_tokens(self):
        # the shared WISP_CENTRAL_TOKEN ("tok" in this test's Config) still authenticates
        # any tenant/node -- self-service tokens are additive, not a replacement.
        status, _ = self._report("ispA", "some-other-node", token="tok")
        self.assertEqual(status, 200)


class NodeEnrollmentRequiredWhenRegisteredTest(unittest.TestCase):
    """A registered node's own credential is required even on a deployment with NEITHER
    a global bearer token NOR mTLS configured — self-service registration has to mean
    something on its own, not just "if nothing else is set up either"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0, central_token="")
        self.store = CentralStore(self.cfg.central_db)
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
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

    def _req(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def test_unregistered_node_stays_open_by_default(self):
        body = {"v": 1, "tenant_id": "ispA", "node_id": "edge-anything",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        status, _ = self._req("POST", "/report", body)
        self.assertEqual(status, 200)

    def test_registered_node_requires_its_own_token_even_with_nothing_else_configured(self):
        token = self.store.issue_node_token("ispA", "edge-a1")
        body = {"v": 1, "tenant_id": "ispA", "node_id": "edge-a1",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        status, _ = self._req("POST", "/report", body)               # no credential presented
        self.assertEqual(status, 401)
        status, _ = self._req("POST", "/report", body, token=token)  # its own token works
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
