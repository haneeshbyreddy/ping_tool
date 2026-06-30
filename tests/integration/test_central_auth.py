"""Central dashboard auth + multi-tenant API tests (Phase 10 Part C): password/account
crypto, identity-carrying sessions, the login flow, tenant scoping (org user pinned,
superadmin sees all), write authorization (owner/superadmin only), and the org-wide team /
attendance — all over a real socket with http.client. No network beyond loopback."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth
from wisp.central.store import CentralStore
from wisp.central.server import make_server


class CentralAuthUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db")
        self.store = CentralStore(self.cfg.central_db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_user_validations(self):
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "a", "short", "owner")     # weak password
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "a", "longenough", "boss")  # bad role
        auth.create_user(self.store, "ispA", "alice", "longenough", "owner")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "alice", "longenough", "owner")  # dup username

    def test_verify_login(self):
        auth.create_user(self.store, "ispA", "alice", "correcthorse", "owner")
        self.assertIsNone(auth.verify_login(self.store, "alice", "wrong"))
        self.assertIsNotNone(auth.verify_login(self.store, "alice", "correcthorse"))
        # deactivated account never authenticates
        uid = self.store.get_user_by_username("alice")["id"]
        self.store.set_user_active(uid, False)
        self.assertIsNone(auth.verify_login(self.store, "alice", "correcthorse"))

    def test_session_round_trip_and_tamper(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")  # superadmin
        tok = auth.issue_session(uid, self.cfg)
        self.assertEqual(auth.verify_session(tok, cfg=self.cfg, timeout_h=12), uid)
        self.assertIsNone(auth.verify_session(tok + "x", cfg=self.cfg, timeout_h=12))  # bad sig
        # expired
        old = auth.issue_session(uid, self.cfg, now=time.time() - 13 * 3600)
        self.assertIsNone(auth.verify_session(old, cfg=self.cfg, timeout_h=12, now=time.time()))
        # resolve strips secrets + flags superadmin
        user = auth.resolve_session(self.store, tok, cfg=self.cfg)
        self.assertTrue(user["is_superadmin"])
        self.assertNotIn("pw_hash", user)


class CentralAuthHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0, central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        # one superadmin, one org owner, one org operator
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "oper", "operpassword", "operator")
        # seed some data
        self.store.ingest("ispA", "edge-1", [{"id": 1, "kind": "event",
            "body": {"type": "OutageOpened", "device_id": 5, "device_name": "Tower", "state": "DOWN"}}])
        self.store.ingest("ispB", "edge-1", [{"id": 1, "kind": "event",
            "body": {"type": "OutageOpened", "device_id": 9, "device_name": "Relay", "state": "DOWN"}}])
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
        if status != 200:
            return status, None
        return status, setcookie.split(";")[0]

    # --- login flow ---
    def test_login_sets_cookie_and_me(self):
        status, cookie = self._login("owner", "ownerpassword")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("wisp_central_session="))
        status, body, _ = self._req("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["tenant_id"], "ispA")
        self.assertEqual(body["user"]["role"], "owner")

    def test_bad_login_401_and_me_requires_session(self):
        self.assertEqual(self._login("owner", "nope")[0], 401)
        self.assertEqual(self._req("GET", "/api/me")[0], 401)

    def test_login_throttle(self):
        for _ in range(5):
            self._login("owner", "wrong")
        self.assertEqual(self._login("owner", "wrong")[0], 429)  # locked out after 5 fails

    # --- tenant scoping ---
    def test_org_user_is_pinned_to_their_tenant(self):
        _, cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/fleet", cookie=cookie)
        self.assertEqual({n["tenant_id"] for n in body["nodes"]}, {"ispA"})
        # even if they try to peek at another org, the server ignores ?tenant for org users
        _, body, _ = self._req("GET", "/api/fleet?tenant=ispB", cookie=cookie)
        self.assertEqual({n["tenant_id"] for n in body["nodes"]}, {"ispA"})

    def test_superadmin_sees_all_and_can_narrow(self):
        _, cookie = self._login("root", "rootpassword")
        _, body, _ = self._req("GET", "/api/fleet", cookie=cookie)
        self.assertEqual({n["tenant_id"] for n in body["nodes"]}, {"ispA", "ispB"})
        _, body, _ = self._req("GET", "/api/fleet?tenant=ispB", cookie=cookie)
        self.assertEqual({n["tenant_id"] for n in body["nodes"]}, {"ispB"})

    def test_bearer_token_reads_as_machine_superadmin(self):
        status, body, _ = self._req("GET", "/api/devices", token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["devices"]), 2)        # cross-tenant

    # --- write authorization ---
    def test_operator_cannot_write_team_owner_can(self):
        _, op = self._login("oper", "operpassword")
        status, _, _ = self._req("POST", "/api/team",
                                 {"tenant_id": "ispA", "name": "Bob"}, cookie=op)
        self.assertEqual(status, 403)
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/team",
                                    {"tenant_id": "ispA", "name": "Bob", "role": "operator"}, cookie=own)
        self.assertEqual(status, 200)
        # owner cannot write into a DIFFERENT org
        status, _, _ = self._req("POST", "/api/team",
                                 {"tenant_id": "ispB", "name": "X"}, cookie=own)
        self.assertEqual(status, 403)

    def test_team_and_attendance_round_trip(self):
        _, own = self._login("owner", "ownerpassword")
        self._req("POST", "/api/team", {"tenant_id": "ispA", "name": "Asha", "role": "operator"}, cookie=own)
        _, team, _ = self._req("GET", "/api/team", cookie=own)
        self.assertEqual(team["team"][0]["name"], "Asha")
        wid = team["team"][0]["id"]
        self._req("POST", "/api/attendance", {"worker_id": wid, "present": True}, cookie=own)
        _, att, _ = self._req("GET", "/api/attendance", cookie=own)
        op = next(o for o in att["operators"] if o["id"] == wid)
        self.assertTrue(op["present_today"])

    def test_superadmin_provisions_org_user(self):
        _, root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/users",
            {"tenant_id": "ispB", "username": "bowner", "password": "bpassword12", "role": "owner"},
            cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("bowner", "bpassword12")[0], 200)

    def test_ingest_uses_bearer_not_session(self):
        _, op = self._login("oper", "operpassword")
        # a session cookie does NOT authorize ingest (that plane is bearer-only)
        env = {"v": 1, "tenant_id": "ispA", "node_id": "edge-2", "kind": "heartbeat",
               "body": {"fleet_size": 1}}
        self.assertEqual(self._req("POST", "/heartbeat", env, cookie=op)[0], 401)
        self.assertEqual(self._req("POST", "/heartbeat", env, token="tok")[0], 200)

    # --- static (unauthed) ---
    def test_static_index_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn(b"WISP Central", resp.read())
        conn.close()


if __name__ == "__main__":
    unittest.main()
