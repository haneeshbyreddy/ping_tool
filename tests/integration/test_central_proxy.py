"""Web-UI proxy — full round trip: browser -> central -> edge -> stub device -> back.

This is the M0 spike proof (webplan.md). A real central server, the real edge
ProxyTunnel worker, and a stub HTTP "device" — a browser GET to
/api/proxy/<sid>/... comes back carrying the device's own bytes, proving the
reverse tunnel end to end. The on-site test against a real switch/OLT stays the
operator's; here the device is local so the mechanism is exercised honestly.
"""
import asyncio
import base64
import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.ingress.webproxy import ProxyTunnel
from wisp.runtime.central_client import build_central_client


class _StubDevice(BaseHTTPRequestHandler):
    """Stands in for a switch/OLT web UI: echoes method + path so the test can
    prove the request reached it faithfully."""

    # How many times each path has actually been fetched. The asset-cache tests
    # assert on this rather than on the reply: "the browser got the bytes" is
    # true either way, and the whole point is that the DEVICE was not asked.
    hits: dict = {}

    def _serve(self, body: bytes, ctype: str, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _emit(self, note: str):
        body = f"STUB-DEVICE {self.command} {self.path} {note}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = self.path.split("?")[0]
        _StubDevice.hits[base] = _StubDevice.hits.get(base, 0) + 1
        if base == "/app.js":
            # the ordinary case: a static script with no cache headers at all,
            # which is exactly what the C-Data firmware serves
            self._serve(b"var wisp=1;/* /app.js */", "application/javascript")
            return
        if base == "/session.js":
            self._serve(b"var s=1;", "application/javascript",
                        [("Set-Cookie", "DEVSID=abc; Path=/")])
            return
        if base == "/nostore.js":
            self._serve(b"var n=1;", "application/javascript",
                        [("Cache-Control", "no-store")])
            return
        if base == "/vary.js":
            self._serve(b"var v=1;", "application/javascript",
                        [("Vary", "Cookie")])
            return
        if base == "/missing.js":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if base == "/linked.css":
            self._serve(b'@import url(/deep/other.css);', "text/css")
            return
        if self.path == "/headers":
            # echo the request headers the login-flow fix must forward
            lines = [f"{k}: {self.headers.get(k)}"
                     for k in ("Cookie", "Referer", "Origin", "Content-Type")]
            body = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/redirect":
            # old-firmware style: root-absolute redirect + root-scoped cookie
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "DEVSID=abc; Path=/; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/page.html":
            body = b'<html><a href="/style.css">x</a> <img src=logo.png></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._emit("ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._emit(f"got{length}")

    def log_message(self, *a):
        pass


class ProxyRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

        # 1) stub device on an ephemeral port
        self.device = ThreadingHTTPServer(("127.0.0.1", 0), _StubDevice)
        self.dev_port = self.device.server_address[1]
        self.dev_thread = threading.Thread(target=self.device.serve_forever, daemon=True)
        self.dev_thread.start()

        # 2) central — proxy on, device's port whitelisted as a mgmt port
        self.cfg = Config(
            central_db=Path(self.tmp.name) / "central.db",
            central_bind="127.0.0.1", central_port=0, central_token="s3cret",
            proxy_enabled=True, proxy_mgmt_ports=str(self.dev_port),
            proxy_poll_hold_s=5.0, proxy_request_timeout_s=10.0,
            proxy_max_body_bytes=1_000_000)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        self.device_id = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "127.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        # M1: the proxy is opt-in per org (superadmin-set capability flag)
        self.store.set_org_web_proxy("ispA", True)

        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.srv_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.srv_thread.start()
        self.cookie = self._login("owner", "ownerpassword")

        # 3) the real edge tunnel, pointed back at central
        self.edge_cfg = Config(
            central_url=f"http://127.0.0.1:{self.port}", central_token="s3cret",
            org_id="ispA", node_id="edge-1",
            proxy_enabled=True, proxy_mgmt_ports=str(self.dev_port),
            proxy_poll_hold_s=5.0, proxy_request_timeout_s=10.0,
            proxy_max_body_bytes=1_000_000)
        self.edge_client = build_central_client(self.edge_cfg)

    def tearDown(self):
        try:
            self.edge_client.close()
        except Exception:
            pass
        self.server.shutdown()
        self.srv_thread.join(timeout=2)
        self.server.server_close()
        self.device.shutdown()
        self.dev_thread.join(timeout=2)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def _login(self, username, password) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        self.assertIsNotNone(cookie, "login did not set a session cookie")
        return cookie.split(";")[0]

    def _open_session(self, device_id=None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/proxy/session",
                     body=json.dumps({"device_id": device_id or self.device_id,
                                      "port": self.dev_port}),
                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        resp = conn.getresponse()
        body = json.loads(resp.read() or "{}")
        conn.close()
        return resp.status, body

    def _browser(self, method, path, out, body=None, cookie=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Cookie": cookie or self.cookie, **(extra or {})}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        out["status"] = resp.status
        out["headers"] = resp.getheaders()
        out["body"] = resp.read()
        conn.close()

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

    def _round_trip(self, method, path, devices, body=None, extra=None):
        """Fire the (blocking) browser request in a thread, serve one request from
        the edge, return the browser's result."""
        out = {}
        t = threading.Thread(target=self._browser, args=(method, path, out),
                             kwargs={"body": body, "extra": extra})
        t.start()
        tunnel = ProxyTunnel(self.edge_client, self.edge_cfg,
                             devices_provider=lambda: devices)
        served = asyncio.run(tunnel.serve_once())
        t.join(timeout=12)
        return served, out

    # -- tests -----------------------------------------------------------------

    def test_session_requires_login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/proxy/session",
                     body=json.dumps({"device_id": self.device_id, "port": self.dev_port}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)

    def test_get_round_trip_carries_device_bytes(self):
        status, sess = self._open_session()
        self.assertEqual(status, 200, sess)
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/status?vlan=7",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(out["status"], 200)
        self.assertIn(b"STUB-DEVICE GET", out["body"])
        self.assertIn(b"/status?vlan=7", out["body"])  # path + query forwarded

    def test_post_body_reaches_device(self):
        _, sess = self._open_session()
        served, out = self._round_trip(
            "POST", f"/api/proxy/{sess['sid']}/save", devices=[{"ip_address": "127.0.0.1"}],
            body=b"config=1")
        self.assertTrue(served)
        self.assertEqual(out["status"], 200)
        self.assertIn(b"STUB-DEVICE POST", out["body"])
        self.assertIn(b"got8", out["body"])  # device saw the 8-byte body

    def test_edge_refuses_device_not_in_its_list(self):
        _, sess = self._open_session()
        # The edge's live device list does NOT contain 127.0.0.1 -> refuse.
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/status",
            devices=[{"ip_address": "10.0.0.250"}])
        self.assertTrue(served)
        self.assertEqual(out["status"], 502)
        self.assertIn(b"not a device this node probes", out["body"])

    def test_unknown_session_404(self):
        out = {}
        self._browser("GET", "/api/proxy/deadbeef/status", out)
        self.assertEqual(out["status"], 404)

    def test_session_rejects_port_outside_mgmt_set(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/proxy/session",
                     body=json.dumps({"device_id": self.device_id, "port": 8291}),
                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        resp = conn.getresponse()
        body = json.loads(resp.read() or "{}")
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("proxy_mgmt_ports", body.get("error", ""))

    def test_web_access_override_endpoint_used(self):
        # The device's admin page is declared at a specific endpoint (here the stub
        # device). The browser sends NO port, so the classic path would default to
        # 80 and be rejected (80 isn't a mgmt port here) — a working round-trip
        # proves the override endpoint won and drove the tunnel.
        self.store.set_org_device_web_access(
            "ispA", self.device_id, web_ip="127.0.0.1", web_port=self.dev_port,
            web_scheme="http")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/proxy/session",
                     body=json.dumps({"device_id": self.device_id}),  # no port
                     headers={"Content-Type": "application/json", "Cookie": self.cookie})
        resp = conn.getresponse()
        sess = json.loads(resp.read() or "{}")
        conn.close()
        self.assertEqual(resp.status, 200, sess)
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/status",
            devices=[{"ip_address": "127.0.0.1", "web_ip": "127.0.0.1",
                      "web_port": self.dev_port, "web_scheme": "http"}])
        self.assertTrue(served)
        self.assertEqual(out["status"], 200)
        self.assertIn(b"STUB-DEVICE GET", out["body"])

    # -- M1: sessions + security -------------------------------------------------

    def test_session_requires_org_capability_flag(self):
        self.store.set_org_web_proxy("ispA", False)
        status, body = self._open_session()
        self.assertEqual(status, 403)
        self.assertIn("not enabled", body.get("error", ""))

    def test_worker_role_cannot_open_session(self):
        # The tunnel stays owner-only. Since roles collapsed to owner+worker
        # (2026-07-21) `worker` is the only non-owner org role, and it is now
        # refused TWICE over: server.py's _WORKER_ROUTES whitelist answers
        # first (a proxy route isn't on it), with _PROXY_ROLES behind it.
        auth.create_user(self.store, "ispA", "w1", "workerpassword1", "worker")
        cookie = self._login("w1", "workerpassword1")
        status, _ = self._api("POST", "/api/proxy/session",
                              {"device_id": self.device_id, "port": self.dev_port},
                              cookie=cookie)
        self.assertEqual(status, 403)

    def test_worker_cannot_drive_owner_session(self):
        # even a session an owner left live must not be browsable by a worker
        # who can see its sid.
        _, sess = self._open_session()
        auth.create_user(self.store, "ispA", "w1", "workerpassword1", "worker")
        cookie = self._login("w1", "workerpassword1")
        out = {}
        self._browser("GET", f"/api/proxy/{sess['sid']}/status", out, cookie=cookie)
        self.assertEqual(out["status"], 403)

    def test_capability_revoked_mid_session_kills_it(self):
        _, sess = self._open_session()
        self.store.set_org_web_proxy("ispA", False)
        out = {}
        self._browser("GET", f"/api/proxy/{sess['sid']}/status", out)
        self.assertEqual(out["status"], 403)
        row = self.store.proxy_session_row(sess["sid"])
        self.assertEqual(row["status"], "closed")

    def test_audit_row_written_per_request(self):
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/status?vlan=7",
                         devices=[{"ip_address": "127.0.0.1"}])
        rows = self.store.list_proxy_audit("ispA")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sid"], sess["sid"])
        self.assertEqual(rows[0]["method"], "GET")
        self.assertEqual(rows[0]["path"], "/status?vlan=7")
        self.assertEqual(rows[0]["status"], 200)
        self.assertEqual(rows[0]["device_id"], self.device_id)

    # -- per-session static-asset cache (2026-07-29) ----------------------------
    #
    # 44% of every request this tunnel had ever carried was a re-fetch of an
    # unchanging asset inside ONE session, and on a weak embedded HTTPS stack
    # each of those cost a full TCP+TLS handshake — 1.00s per asset, a
    # 7-second page. These tests pin what may and may not be remembered.

    def _cached_get(self, sid, path):
        """A browser request with NO edge serving it — so a 200 here proves the
        answer came from central's memo and the device was never asked."""
        out = {}
        self._browser("GET", f"/api/proxy/{sid}{path}", out)
        return out

    def test_static_asset_is_served_from_the_session_cache(self):
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        served, first = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/app.js",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(first["status"], 200)
        second = self._cached_get(sess["sid"], "/app.js")
        self.assertEqual(second["status"], 200)
        # byte-identical to the miss: a hit replays the SAME rewriting path
        self.assertEqual(second["body"], first["body"])
        self.assertEqual(_StubDevice.hits["/app.js"], 1,
                         "the device was fetched twice — the cache did nothing")

    def test_a_cache_hit_is_still_audited(self):
        """The audit answers 'who opened what'. Dropping cached requests would
        make it stop reflecting what a human actually browsed."""
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/app.js",
                         devices=[{"ip_address": "127.0.0.1"}])
        self._cached_get(sess["sid"], "/app.js")
        rows = [r for r in self.store.list_proxy_audit("ispA")
                if r["path"] == "/app.js"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {200})

    def test_a_dynamic_page_is_never_cached(self):
        """This vendor's DYNAMIC pages are .html (/action/onuauthinfo.html) —
        the exact class a looser rule would start serving stale."""
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/page.html",
                         devices=[{"ip_address": "127.0.0.1"}])
        served, again = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/page.html",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served, "second .html was answered without the device")
        self.assertEqual(again["status"], 200)
        self.assertEqual(_StubDevice.hits["/page.html"], 2)

    def test_a_reply_carrying_session_state_is_never_cached(self):
        """A static extension is a hint about the URL, never a promise about
        the response: Set-Cookie, no-store and Vary each disqualify one."""
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        for path in ("/session.js", "/nostore.js", "/vary.js"):
            with self.subTest(path=path):
                self._round_trip("GET", f"/api/proxy/{sess['sid']}{path}",
                                 devices=[{"ip_address": "127.0.0.1"}])
                served, _ = self._round_trip(
                    "GET", f"/api/proxy/{sess['sid']}{path}",
                    devices=[{"ip_address": "127.0.0.1"}])
                self.assertTrue(served, f"{path} was cached")
                self.assertEqual(_StubDevice.hits[path], 2)

    def test_a_failed_fetch_is_never_cached(self):
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/missing.js",
                         devices=[{"ip_address": "127.0.0.1"}])
        served, again = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/missing.js",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(again["status"], 404)
        self.assertEqual(_StubDevice.hits["/missing.js"], 2)

    def test_the_firmwares_own_cache_busting_still_reaches_the_device(self):
        """`/js/misc.js?rand=52258` carries a fresh number per page load. The
        query is part of the key, so the bust keeps working — second-guessing a
        deliberate one is how a cache starts serving a page nobody can explain."""
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/app.js?rand=1",
                         devices=[{"ip_address": "127.0.0.1"}])
        served, _ = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/app.js?rand=2",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(_StubDevice.hits["/app.js"], 2)

    def test_jquerys_own_cache_buster_does_not_defeat_the_cache(self):
        """`_=<ts>` is appended by `$.ajax({cache:false})` to everything — the
        client library talking about the browser cache, not the vendor talking
        about the resource. 20% of this tunnel's traffic is a static .properties
        table wearing one, so keying it literally is a permanent miss."""
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/app.js?_=111",
                         devices=[{"ip_address": "127.0.0.1"}])
        out = self._cached_get(sess["sid"], "/app.js?_=222")
        self.assertEqual(out["status"], 200)
        self.assertEqual(_StubDevice.hits["/app.js"], 1)

    def test_the_cache_does_not_survive_into_a_new_session(self):
        """It lives ON the session, so it dies with the credential that opened
        it — there is no cross-session (let alone cross-org) key to get wrong."""
        _StubDevice.hits.clear()
        _, first = self._open_session()
        self._round_trip("GET", f"/api/proxy/{first['sid']}/app.js",
                         devices=[{"ip_address": "127.0.0.1"}])
        # the edge has polled by now, so the second open runs a real preflight
        tunnel = ProxyTunnel(self.edge_client, self.edge_cfg,
                             devices_provider=lambda: [{"ip_address": "127.0.0.1"}])
        _, second = self._open_with_tunnel(tunnel)
        self.assertNotEqual(first["sid"], second["sid"])
        served, _ = self._round_trip(
            "GET", f"/api/proxy/{second['sid']}/app.js",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(_StubDevice.hits["/app.js"], 2)

    def test_a_cached_body_is_still_rewritten_into_the_session_prefix(self):
        """The cache stores the DEVICE's raw reply, so a hit runs the identical
        rewrite pipeline. Caching post-rewrite bytes would work today and break
        the moment anything downstream became session-dependent."""
        _StubDevice.hits.clear()
        _, sess = self._open_session()
        _, first = self._round_trip("GET", f"/api/proxy/{sess['sid']}/linked.css",
                                    devices=[{"ip_address": "127.0.0.1"}])
        prefix = f"/api/proxy/{sess['sid']}/deep/other.css".encode()
        self.assertIn(prefix, first["body"])
        second = self._cached_get(sess["sid"], "/linked.css")
        self.assertEqual(second["body"], first["body"])
        self.assertIn(prefix, second["body"])
        self.assertEqual(_StubDevice.hits["/linked.css"], 1)

    def test_edge_refusal_audits_as_502(self):
        _, sess = self._open_session()
        self._round_trip("GET", f"/api/proxy/{sess['sid']}/status",
                         devices=[{"ip_address": "10.0.0.250"}])
        rows = self.store.list_proxy_audit("ispA")
        self.assertEqual(rows[0]["status"], 502)

    def test_session_record_persisted_and_listed(self):
        _, sess = self._open_session()
        status, doc = self._api("GET", "/api/proxy/sessions")
        self.assertEqual(status, 200)
        rows = [s for s in doc["sessions"] if s["sid"] == sess["sid"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "open")
        self.assertTrue(rows[0]["live"])
        self.assertEqual(rows[0]["device_name"], "SW")

    def test_close_session_stops_browsing(self):
        _, sess = self._open_session()
        status, doc = self._api("POST", "/api/proxy/close", {"sid": sess["sid"]})
        self.assertEqual(status, 200)
        self.assertTrue(doc["was_open"])
        out = {}
        self._browser("GET", f"/api/proxy/{sess['sid']}/status", out)
        self.assertEqual(out["status"], 404)
        self.assertEqual(self.store.proxy_session_row(sess["sid"])["status"], "closed")

    def test_audit_view_is_owner_only(self):
        auth.create_user(self.store, "ispA", "w1", "workerpassword1", "worker")
        cookie = self._login("w1", "workerpassword1")
        status, _ = self._api("GET", "/api/proxy/audit", cookie=cookie)
        self.assertEqual(status, 403)
        status, doc = self._api("GET", "/api/proxy/audit")  # owner
        self.assertEqual(status, 200)
        self.assertIn("audit", doc)

    def test_report_reply_carries_live_sessions(self):
        _, sess = self._open_session()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/report",
                     body=json.dumps({"v": 1, "org_id": "ispA", "node_id": "edge-1",
                                      "ts": "2026-07-16T00:00:00+00:00",
                                      "mode": "full", "pings": {}}),
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer s3cret"})
        resp = conn.getresponse()
        reply = json.loads(resp.read() or "{}")
        conn.close()
        self.assertEqual(resp.status, 200)
        carried = reply.get("proxy_sessions") or []
        self.assertEqual([s["sid"] for s in carried], [sess["sid"]])
        self.assertGreater(carried[0]["ttl_s"], 0)

    def _report(self) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/report",
                     body=json.dumps({"v": 1, "org_id": "ispA", "node_id": "edge-1",
                                      "ts": "2026-07-20T00:00:00+00:00",
                                      "mode": "full", "pings": {}}),
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer s3cret"})
        resp = conn.getresponse()
        reply = json.loads(resp.read() or "{}")
        conn.close()
        self.assertEqual(resp.status, 200)
        return reply

    def test_report_reply_carries_standby_flag_for_proxy_org(self):
        # No live session: the flag alone keeps one edge long-poll warm so the
        # FIRST browser connect doesn't wait a report cycle (2026-07-20 fix).
        reply = self._report()
        self.assertTrue(reply.get("proxy_standby"))
        self.assertNotIn("proxy_sessions", reply)
        self.store.set_org_web_proxy("ispA", False)
        reply = self._report()
        self.assertNotIn("proxy_standby", reply)

    def test_redirect_and_cookie_rewritten_into_prefix(self):
        _, sess = self._open_session()
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/redirect",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertEqual(out["status"], 302)
        headers = {k.lower(): v for k, v in out["headers"]}
        self.assertEqual(headers["location"], f"/api/proxy/{sess['sid']}/login")
        self.assertIn(f"Path=/api/proxy/{sess['sid']}/", headers["set-cookie"])

    def test_html_body_absolute_refs_rewritten(self):
        _, sess = self._open_session()
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sess['sid']}/page.html",
            devices=[{"ip_address": "127.0.0.1"}])
        self.assertTrue(served)
        self.assertIn(f'href="/api/proxy/{sess["sid"]}/style.css"'.encode(),
                      out["body"])
        # plain relative refs must pass through untouched
        self.assertIn(b"src=logo.png", out["body"])

    def test_browser_headers_forwarded_but_dashboard_cookie_stripped(self):
        # The device login flow depends on ITS cookie coming back on every
        # request; central's own session cookie must never leave the house, and
        # Referer is rewritten to the device origin (firmwares CSRF-check it).
        _, sess = self._open_session()
        sid = sess["sid"]
        served, out = self._round_trip(
            "GET", f"/api/proxy/{sid}/headers",
            devices=[{"ip_address": "127.0.0.1"}],
            extra={"Referer": f"http://127.0.0.1:{self.port}/api/proxy/{sid}/page.html",
                   "Cookie": f"{self.cookie}; DEVSID=abc"})
        self.assertTrue(served)
        text = out["body"].decode()
        self.assertIn("Cookie: DEVSID=abc", text)
        self.assertNotIn("wisp_central_session", text)
        self.assertIn(f"Referer: http://127.0.0.1:{self.dev_port}/page.html", text)

    def test_escaped_root_absolute_url_rescued_via_referer(self):
        # Device JS builds "/js/x.js" — it escapes the sid prefix and lands on
        # central as an unknown route. With a live session in the Referer the
        # server must bounce it back inside the tunnel, method preserved.
        _, sess = self._open_session()
        sid = sess["sid"]
        ref = {"Referer": f"http://127.0.0.1:{self.port}/api/proxy/{sid}/index.html"}
        out = {}
        self._browser("GET", "/js/app.js?v=3", out, extra=ref)
        self.assertEqual(out["status"], 307)
        headers = {k.lower(): v for k, v in out["headers"]}
        self.assertEqual(headers["location"], f"/api/proxy/{sid}/js/app.js?v=3")
        out = {}
        self._browser("POST", "/action/login.html", out, body=b"u=admin", extra=ref)
        self.assertEqual(out["status"], 307)
        headers = {k.lower(): v for k, v in out["headers"]}
        self.assertEqual(headers["location"], f"/api/proxy/{sid}/action/login.html")

    # -- session-open preflight ---------------------------------------------------

    def _open_with_tunnel(self, tunnel, device_id=None):
        """Prime polled_recently, then open a session while the tunnel serves
        the resulting preflight probe."""
        self.edge_client.proxy_next(0.05)  # stamps last_poll on the hub
        result = {}
        t = threading.Thread(target=lambda: result.update(
            dict(zip(("status", "body"), self._open_session(device_id)))))
        t.start()
        asyncio.run(tunnel.serve_once())
        t.join(timeout=12)
        return result.get("status"), result.get("body")

    def test_preflight_adopts_answering_scheme(self):
        # Owner declared the endpoint without pinning a scheme; the heuristic
        # says http (non-443 port) but only https answers — the session must
        # adopt https, proven by the scheme the edge is later told to fetch.
        self.store.set_org_device_web_access(
            "ispA", self.device_id, web_ip="127.0.0.1", web_port=self.dev_port,
            web_scheme=None)
        probed = []

        async def prober(ip, port, scheme, timeout_s):
            probed.append((ip, port, scheme))
            return None if scheme == "https" else "connection refused"

        fetched = []

        async def fetcher(req, cfg):
            fetched.append(req)
            return 200, {"Content-Type": "text/plain"}, b"ok"

        tunnel = ProxyTunnel(self.edge_client, self.edge_cfg,
                             devices_provider=lambda: [
                                 {"ip_address": "127.0.0.1",
                                  "web_ip": "127.0.0.1",
                                  "web_port": self.dev_port}],
                             fetcher=fetcher, prober=prober)
        status, sess = self._open_with_tunnel(tunnel)
        self.assertEqual(status, 200, sess)
        self.assertIn(("127.0.0.1", self.dev_port, "https"), probed)
        out = {}
        t = threading.Thread(target=self._browser,
                             args=("GET", f"/api/proxy/{sess['sid']}/x", out))
        t.start()
        asyncio.run(tunnel.serve_once())
        t.join(timeout=12)
        self.assertEqual(out["status"], 200)
        self.assertEqual(fetched[0]["scheme"], "https")
        self.assertEqual(fetched[0]["device_port"], self.dev_port)

    def test_preflight_nothing_listening_fails_fast_and_clear(self):
        async def prober(ip, port, scheme, timeout_s):
            return "connect timeout"

        tunnel = ProxyTunnel(self.edge_client, self.edge_cfg,
                             devices_provider=lambda: [{"ip_address": "127.0.0.1"}],
                             prober=prober)
        status, body = self._open_with_tunnel(tunnel)
        self.assertEqual(status, 502)
        self.assertIn("unreachable", body.get("error", ""))
        # the failed open must not leave a live session behind
        _, doc = self._api("GET", "/api/proxy/sessions")
        self.assertEqual([s for s in doc["sessions"] if s["status"] == "open"], [])

    def test_no_recent_poll_skips_preflight(self):
        # Dormant tunnel / pre-standby edge: the open must not stall — the
        # heuristic target is kept and the session opens immediately.
        before = time.monotonic()
        status, sess = self._open_session()
        self.assertEqual(status, 200, sess)
        self.assertLess(time.monotonic() - before, 3.0)

    def test_old_edge_fetch_reply_keeps_heuristic(self):
        # An old edge doesn't know kind="preflight" and answers with a normal
        # page fetch — central must keep the heuristic target and still open.
        from wisp.central.api import proxy as proxy_api
        self.edge_client.proxy_next(0.05)
        result = {}
        t = threading.Thread(target=lambda: result.update(
            dict(zip(("status", "body"), self._open_session()))))
        t.start()
        req = self.edge_client.proxy_next(5.0)   # old edge: plain fetch reply
        self.assertEqual(req.get("kind"), "preflight")
        self.edge_client.proxy_reply(
            req["sid"], req["req_id"], 200, {},
            base64.b64encode(b"<html>a real page, not a probe report</html>").decode())
        t.join(timeout=proxy_api._PREFLIGHT_TIMEOUT_S + 4)
        self.assertEqual(result.get("status"), 200, result.get("body"))

    def test_one_session_per_node_newest_wins(self):
        # Opening a second session on the same probe replaces the first: the
        # old sid stops browsing (404) and its record reads closed, not open.
        _, first = self._open_session()
        status, second = self._open_session()
        self.assertEqual(status, 200, second)
        out = {}
        self._browser("GET", f"/api/proxy/{first['sid']}/status", out)
        self.assertEqual(out["status"], 404)
        _, doc = self._api("GET", "/api/proxy/sessions")
        by_sid = {s["sid"]: s for s in doc["sessions"]}
        self.assertEqual(by_sid[first["sid"]]["status"], "closed")
        self.assertEqual(by_sid[second["sid"]]["status"], "open")
        self.assertTrue(by_sid[second["sid"]]["live"])

    def test_escape_rescue_needs_a_live_session(self):
        bogus = {"Referer":
                 f"http://127.0.0.1:{self.port}/api/proxy/AAAAAAAAAAAAAAAAAAAAAAAA/x.html"}
        out = {}
        self._browser("GET", "/js/app.js", out, extra=bogus)
        self.assertEqual(out["status"], 404)


if __name__ == "__main__":
    unittest.main()
