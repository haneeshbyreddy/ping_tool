"""Phase 8.2 — auth gate (PIN + signed-cookie sessions) over the real HTTP server.

Boots the actual dashboard server on an ephemeral port against a temp DB (routes
uses a module-level CONFIG, monkeypatched here), then drives it with http.client:
no cookie ⇒ 401, first-run set-PIN, login/logout, throttle lockout. Session expiry
is checked directly against auth.verify_session with an injected clock.
"""
import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import migrate
from wisp.server import auth, routes


class AuthHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        os.environ.pop("WISP_DASHBOARD_PIN", None)
        migrate(self.cfg)
        self._saved_cfg = routes.CONFIG
        routes.CONFIG = self.cfg               # point the handler at the temp DB
        auth.THROTTLE.reset("127.0.0.1")
        self.server = routes.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        routes.CONFIG = self._saved_cfg
        auth.THROTTLE.reset("127.0.0.1")
        self.tmp.cleanup()

    # -- HTTP helper ---------------------------------------------------------
    def _req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            import json
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        import json
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        return resp.status, data, set_cookie

    @staticmethod
    def _cookie_value(set_cookie):
        # "wisp_session=TOKEN; Path=/; ..." → "wisp_session=TOKEN"
        return set_cookie.split(";", 1)[0] if set_cookie else None

    # -- cases ---------------------------------------------------------------
    def test_status_before_pin(self):
        status, data, _ = self._req("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["pin_set"])
        self.assertFalse(data["authed"])

    def test_protected_without_cookie_is_401(self):
        status, data, _ = self._req("GET", "/api/summary")
        self.assertEqual(status, 401)
        self.assertIn("error", data)

    def test_first_run_setup_then_access(self):
        status, data, set_cookie = self._req("POST", "/api/auth/setup", {"pin": "1234"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        cookie = self._cookie_value(set_cookie)
        self.assertIsNotNone(cookie)
        # The cookie now unlocks protected endpoints.
        status, _, _ = self._req("GET", "/api/summary", cookie=cookie)
        self.assertEqual(status, 200)
        # And status reflects authed=True with that cookie.
        status, data, _ = self._req("GET", "/api/auth/status", cookie=cookie)
        self.assertTrue(data["pin_set"])
        self.assertTrue(data["authed"])

    def test_setup_twice_conflicts(self):
        self._req("POST", "/api/auth/setup", {"pin": "1234"})
        status, _, _ = self._req("POST", "/api/auth/setup", {"pin": "9999"})
        self.assertEqual(status, 409)

    def test_weak_pin_rejected(self):
        status, _, _ = self._req("POST", "/api/auth/setup", {"pin": "12"})
        self.assertEqual(status, 422)

    def test_login_wrong_then_right(self):
        self._req("POST", "/api/auth/setup", {"pin": "4321"})
        self._req("POST", "/api/logout")  # drop the setup session
        status, _, _ = self._req("POST", "/api/login", {"pin": "0000"})
        self.assertEqual(status, 401)
        status, _, set_cookie = self._req("POST", "/api/login", {"pin": "4321"})
        self.assertEqual(status, 200)
        cookie = self._cookie_value(set_cookie)
        status, _, _ = self._req("GET", "/api/nodes", cookie=cookie)
        self.assertEqual(status, 200)

    def test_logout_clears_cookie(self):
        _, _, set_cookie = self._req("POST", "/api/auth/setup", {"pin": "1234"})
        cookie = self._cookie_value(set_cookie)
        status, _, clear = self._req("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNotNone(clear)
        self.assertIn("Max-Age=0", clear or "")

    def test_throttle_locks_out(self):
        self._req("POST", "/api/auth/setup", {"pin": "1234"})
        self._req("POST", "/api/logout")
        # 5 wrong attempts trip the lockout; the 6th is refused with 429.
        for _ in range(5):
            self._req("POST", "/api/login", {"pin": "0000"})
        status, _, _ = self._req("POST", "/api/login", {"pin": "1234"})
        self.assertEqual(status, 429)

    # -- session expiry (direct, with injected clock) ------------------------
    def test_session_expires(self):
        token = auth.issue_session(self.cfg, now=1000.0)
        # within window
        self.assertTrue(auth.verify_session(
            token, cfg=self.cfg, timeout_h=12, now=1000.0 + 11 * 3600))
        # past window
        self.assertFalse(auth.verify_session(
            token, cfg=self.cfg, timeout_h=12, now=1000.0 + 13 * 3600))

    def test_tampered_token_rejected(self):
        token = auth.issue_session(self.cfg, now=1000.0)
        bad = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertFalse(auth.verify_session(bad, cfg=self.cfg, timeout_h=12, now=1000.0))


if __name__ == "__main__":
    unittest.main()
