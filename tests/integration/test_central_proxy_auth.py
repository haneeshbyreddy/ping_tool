"""Web-UI login injection through the proxy (Phase 2 + 2b):

  * auth_mode='basic' -> central adds `Authorization: Basic <user:pass>` to the
    device fetch; the password never touches the browser.
  * auth_mode='form'  -> central injects an autofill script into the login HTML so
    the page pre-fills (the password DOES reach the DOM — inherent to form login).

The edge is SIMULATED with plain http.client against /edge/proxy/next and
/edge/proxy/reply (the real ProxyTunnel needs httpx, an edge-only dep), so this
runs in the central-only test env while exercising the real central path:
session open -> resolve login -> _forward_headers / inject_autofill -> browser.
"""
import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth, secretbox
from wisp.central.api import proxy as api_proxy
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class _ProxyAuthBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            central_db=Path(self.tmp.name) / "central.db",
            central_bind="127.0.0.1", central_port=0,
            proxy_enabled=True, proxy_mgmt_ports="80,443",
            proxy_poll_hold_s=5.0, proxy_request_timeout_s=10.0,
            proxy_max_body_bytes=1_000_000)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        self.store.set_org_web_proxy("ispA", True)
        self.device_id = self.store.create_org_device("ispA", {
            "name": "OLT", "ip_address": "127.0.0.1", "device_type": "olt",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")

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
        return cookie.split(";")[0]

    def _set_creds(self, **body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inventory/credentials",
                     body=json.dumps({"device_id": self.device_id, **body}),
                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        conn.getresponse().read()
        conn.close()

    def _open_session(self) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/proxy/session",
                     body=json.dumps({"device_id": self.device_id, "port": 80}),
                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        doc = json.loads(conn.getresponse().read())
        conn.close()
        return doc["sid"]

    def _round_trip(self, sid, *, reply_headers=None, reply_body=b"",
                    browser_headers=None):
        """Fire a browser request (it blocks on the hub), pick the parked request
        up as the edge would, answer it, and return (forwarded_headers to device,
        body the browser received)."""
        holder = {}
        def browser():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=12)
            c.request("GET", f"/api/proxy/{sid}/login",
                      headers={"Cookie": self.cookie, **(browser_headers or {})})
            r = c.getresponse()
            holder["body"] = r.read()
            c.close()
        t = threading.Thread(target=browser)
        t.start()

        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        c.request("GET", "/edge/proxy/next?org_id=ispA&node_id=edge-1")
        payload = json.loads(c.getresponse().read())["request"]
        c.close()
        self.assertIsNotNone(payload, "no request was parked for the edge")

        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/edge/proxy/reply", body=json.dumps({
            "v": 1, "org_id": "ispA", "node_id": "edge-1",
            "req_id": payload["req_id"], "status": 200,
            "headers": reply_headers or {},
            "body_b64": base64.b64encode(reply_body).decode()}),
            headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.close()
        t.join(timeout=10)
        return payload["headers"], holder.get("body", b"")


class BasicAuthInjectionTest(_ProxyAuthBase):
    def _headers(self, sid, browser_headers=None):
        fwd, _ = self._round_trip(sid, browser_headers=browser_headers)
        return fwd

    def test_basic_login_injected_into_device_fetch(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="basic")
        headers = self._headers(self._open_session())
        expect = "Basic " + base64.b64encode(b"admin:sravani@1987").decode()
        self.assertEqual(headers.get("Authorization"), expect)

    def test_browser_never_sends_the_password(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="basic")
        wrong = "Basic " + base64.b64encode(b"someoneelse:guess").decode()
        headers = self._headers(self._open_session(), {"Authorization": wrong})
        expect = "Basic " + base64.b64encode(b"admin:sravani@1987").decode()
        self.assertEqual(headers.get("Authorization"), expect)

    def test_form_mode_not_injected(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="form")
        headers = self._headers(self._open_session())
        self.assertNotIn("Authorization", headers)

    def test_no_stored_login_no_injection(self):
        headers = self._headers(self._open_session())
        self.assertNotIn("Authorization", headers)

    def test_undecryptable_password_skipped_not_fatal(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="basic")
        with self.store._connect() as conn:
            conn.execute("UPDATE device_webui_credentials SET password_enc='garbage'"
                         " WHERE device_id=?", (self.device_id,))
            conn.commit()
        headers = self._headers(self._open_session())  # must still open + round-trip
        self.assertNotIn("Authorization", headers)


_LOGIN_HTML = (b"<html><body><form>"
               b"<input name='username'><input type='password'></form></body></html>")


class FormAutofillTest(_ProxyAuthBase):
    def _autofill_endpoint(self, sid, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/proxy/{sid}/{api_proxy.proxy_mod.AUTOFILL_PATH}",
                     headers={"Cookie": cookie or self.cookie})
        resp = conn.getresponse()
        doc = json.loads(resp.read() or "{}")
        conn.close()
        return resp.status, doc

    def test_form_page_gets_bootstrap_without_embedding_creds(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="form")
        fwd, body = self._round_trip(
            self._open_session(),
            reply_headers={"Content-Type": "text/html"}, reply_body=_LOGIN_HTML)
        self.assertIn(b"/* wisp-autofill */", body)
        # the credential-free bootstrap: neither password nor username in the page
        self.assertNotIn(b"sravani@1987", body)
        self.assertNotIn(b'"u":', body)
        self.assertIn(api_proxy.proxy_mod.AUTOFILL_PATH.encode(), body)
        self.assertNotIn("Authorization", fwd)  # form mode never injects a header

    def test_creds_endpoint_returns_login_for_form_session(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="form")
        status, doc = self._autofill_endpoint(self._open_session())
        self.assertEqual(status, 200)
        self.assertEqual(doc, {"u": "admin", "p": "sravani@1987"})

    def test_creds_endpoint_404_for_basic_session(self):
        # basic mode arms the header, NOT autofill — the reserved path must not
        # hand out the login
        self._set_creds(username="admin", password="sravani@1987", auth_mode="basic")
        status, _ = self._autofill_endpoint(self._open_session())
        self.assertEqual(status, 404)

    def test_basic_mode_gets_no_autofill_script(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="basic")
        fwd, body = self._round_trip(
            self._open_session(),
            reply_headers={"Content-Type": "text/html"}, reply_body=_LOGIN_HTML)
        self.assertNotIn(b"wisp-autofill", body)
        self.assertIn("Authorization", fwd)

    def test_csp_header_stripped_when_autofill_armed(self):
        self._set_creds(username="admin", password="sravani@1987", auth_mode="form")
        # capture the browser's response headers via a raw round-trip
        sid = self._open_session()
        holder = {}
        def browser():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=12)
            c.request("GET", f"/api/proxy/{sid}/login", headers={"Cookie": self.cookie})
            r = c.getresponse()
            holder["headers"] = {k.lower(): v for k, v in r.getheaders()}
            r.read()
            c.close()
        t = threading.Thread(target=browser)
        t.start()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        c.request("GET", "/edge/proxy/next?org_id=ispA&node_id=edge-1")
        payload = json.loads(c.getresponse().read())["request"]
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/edge/proxy/reply", body=json.dumps({
            "v": 1, "org_id": "ispA", "node_id": "edge-1", "req_id": payload["req_id"],
            "status": 200,
            "headers": {"Content-Type": "text/html",
                        "Content-Security-Policy": "default-src 'self'"},
            "body_b64": base64.b64encode(_LOGIN_HTML).decode()}),
            headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.close()
        t.join(timeout=10)
        self.assertNotIn("content-security-policy", holder["headers"])


class ResolveLoginUnitTest(unittest.TestCase):
    """The resolvers in isolation, with a real store + real SecretBox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        self.device_id = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "e1"})
        self.box = secretbox.from_config(self.cfg)
        self.h = types.SimpleNamespace(store=self.store, secretbox=self.box)

    def tearDown(self):
        self.tmp.cleanup()

    def _store_creds(self, *, password, auth_mode="basic", username="admin"):
        enc = self.box.encrypt(password) if password is not None else None
        self.store.set_device_webui_credentials(
            "ispA", self.device_id, username=username, password_enc=enc,
            set_password=True, auth_mode=auth_mode, updated_by="owner")

    def test_basic_builds_header(self):
        self._store_creds(password="p@ss")
        got = api_proxy._resolve_injected_auth(self.h, "ispA", self.device_id)
        self.assertEqual(got, "Basic " + base64.b64encode(b"admin:p@ss").decode())
        self.assertIsNone(api_proxy._resolve_autofill(self.h, "ispA", self.device_id))

    def test_form_builds_autofill(self):
        self._store_creds(password="p@ss", auth_mode="form")
        self.assertIsNone(api_proxy._resolve_injected_auth(self.h, "ispA", self.device_id))
        self.assertEqual(
            api_proxy._resolve_autofill(self.h, "ispA", self.device_id), ("admin", "p@ss"))

    def test_no_password_both_none(self):
        self._store_creds(password=None, auth_mode="form")
        self.assertIsNone(api_proxy._resolve_autofill(self.h, "ispA", self.device_id))
        self.assertIsNone(api_proxy._resolve_injected_auth(self.h, "ispA", self.device_id))

    def test_no_row_both_none(self):
        self.assertIsNone(api_proxy._resolve_autofill(self.h, "ispA", self.device_id))
        self.assertIsNone(api_proxy._resolve_injected_auth(self.h, "ispA", self.device_id))

    def test_empty_username_still_builds(self):
        self._store_creds(password="p@ss", username="")
        got = api_proxy._resolve_injected_auth(self.h, "ispA", self.device_id)
        self.assertEqual(got, "Basic " + base64.b64encode(b":p@ss").decode())


if __name__ == "__main__":
    unittest.main()
