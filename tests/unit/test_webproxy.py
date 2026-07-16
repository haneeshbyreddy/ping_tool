"""Web-UI proxy tunnel — hub parking desk + edge worker, in isolation (no network).

The hub is the cross-thread parking desk (browser thread blocks, edge thread hands
off); the edge worker is the allow-list gate + device fetch. Full HTTP round-trip
lives in integration/test_central_proxy.py.
"""
import asyncio
import base64
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.proxy import (
    ProxyHub, ProxySession, parse_ports, rewrite_body, rewrite_headers,
)
from wisp.ingress.webproxy import ProxyTunnel


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class ParsePortsTest(unittest.TestCase):
    def test_parses_and_drops_junk(self):
        self.assertEqual(parse_ports("80,443"), frozenset({80, 443}))
        self.assertEqual(parse_ports(" 80 , x , 8080 "), frozenset({80, 8080}))
        self.assertEqual(parse_ports(""), frozenset())
        self.assertEqual(parse_ports("70000"), frozenset())


class ProxyDefaultOnTest(unittest.TestCase):
    """Activation is central-driven (v0.15.8): a fresh edge with no env var
    must build the tunnel — the per-edge WISP_PROXY_ENABLED requirement was
    the field trap (missing flag read as a 504 on every session). =0 stays
    the explicit kill switch."""

    def test_default_on_env_zero_kills(self):
        old = os.environ.pop("WISP_PROXY_ENABLED", None)
        try:
            self.assertTrue(Config().proxy_enabled)
            os.environ["WISP_PROXY_ENABLED"] = "0"
            self.assertFalse(Config().proxy_enabled)
        finally:
            if old is None:
                os.environ.pop("WISP_PROXY_ENABLED", None)
            else:
                os.environ["WISP_PROXY_ENABLED"] = old


class ProxyHubTest(unittest.TestCase):
    def setUp(self):
        self.hub = ProxyHub()

    def _open(self, ttl_s=60.0):
        return self.hub.open_session(
            org_id="o", device_id=1, node_id="n", device_ip="1.2.3.4",
            device_port=80, scheme="http", created_by=7, ttl_s=ttl_s)

    def test_session_lookup_and_ttl_expiry(self):
        sess = self._open(ttl_s=60)
        self.assertIs(self.hub.get_session(sess.sid), sess)
        expired = self._open(ttl_s=-1)  # already in the past
        self.assertIsNone(self.hub.get_session(expired.sid))

    def test_round_trip_parks_and_delivers(self):
        sess = self._open()
        result = {}

        def browser():
            result["r"] = self.hub.submit(
                sess, method="GET", path="/x?a=1", headers={}, body=b"", timeout=5)

        t = threading.Thread(target=browser)
        t.start()
        payload = self.hub.next_request("o", "n", 2.0)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["path"], "/x?a=1")
        self.assertEqual(payload["device_ip"], "1.2.3.4")
        ok = self.hub.deliver(payload["req_id"], "o", "n",
                              {"status": 200, "headers": {}, "body_b64": _b64(b"hi")})
        self.assertTrue(ok)
        t.join(timeout=5)
        self.assertEqual(result["r"]["status"], 200)
        self.assertEqual(base64.b64decode(result["r"]["body_b64"]), b"hi")

    def test_body_is_carried_to_the_edge(self):
        sess = self._open()
        t = threading.Thread(target=lambda: self.hub.submit(
            sess, method="POST", path="/", headers={}, body=b"payload", timeout=5))
        t.start()
        payload = self.hub.next_request("o", "n", 2.0)
        self.assertEqual(base64.b64decode(payload["body_b64"]), b"payload")
        self.hub.deliver(payload["req_id"], "o", "n",
                         {"status": 204, "headers": {}, "body_b64": ""})
        t.join(timeout=5)

    def test_deliver_rejects_foreign_node(self):
        sess = self._open()
        result = {}
        t = threading.Thread(target=lambda: result.__setitem__(
            "r", self.hub.submit(sess, method="GET", path="/", headers={},
                                 body=b"", timeout=0.6)))
        t.start()
        payload = self.hub.next_request("o", "n", 2.0)
        # A credential for a different node must not answer this req_id.
        self.assertFalse(self.hub.deliver(payload["req_id"], "o", "other",
                                          {"status": 200, "headers": {}, "body_b64": ""}))
        t.join(timeout=5)
        self.assertIsNone(result["r"])  # browser timed out, unanswered

    def test_next_request_times_out_empty(self):
        self.assertIsNone(self.hub.next_request("o", "n", 0.1))

    def test_submit_times_out_without_edge(self):
        sess = self._open()
        self.assertIsNone(self.hub.submit(
            sess, method="GET", path="/", headers={}, body=b"", timeout=0.2))

    def test_extend_session_slides_expiry_forward_only(self):
        sess = self._open(ttl_s=600)
        before = sess.expires_at
        self.assertGreaterEqual(self.hub.extend_session(sess, 600), before)
        # a shorter ttl must never PULL the expiry closer
        far = self.hub.extend_session(sess, 3600)
        self.assertEqual(self.hub.extend_session(sess, 1), far)

    def test_active_sessions_for_reports_relative_ttl(self):
        sess = self._open(ttl_s=120)
        self._open(ttl_s=-1)  # expired — must not be carried
        other = self.hub.open_session(
            org_id="o", device_id=2, node_id="OTHER", device_ip="1.2.3.5",
            device_port=80, scheme="http", created_by=7, ttl_s=120)
        carried = self.hub.active_sessions_for("o", "n")
        self.assertEqual([s["sid"] for s in carried], [sess.sid])
        self.assertTrue(0 < carried[0]["ttl_s"] <= 120)
        self.assertEqual(
            [s["sid"] for s in self.hub.active_sessions_for("o", "OTHER")],
            [other.sid])

    def test_inflight_counts_parked_requests_per_session(self):
        sess = self._open()
        self.assertEqual(self.hub.inflight(sess.sid), 0)
        t = threading.Thread(target=lambda: self.hub.submit(
            sess, method="GET", path="/", headers={}, body=b"", timeout=2))
        t.start()
        payload = self.hub.next_request("o", "n", 2.0)
        self.assertEqual(self.hub.inflight(sess.sid), 1)
        self.assertEqual(self.hub.inflight("other-sid"), 0)
        self.hub.deliver(payload["req_id"], "o", "n",
                         {"status": 200, "headers": {}, "body_b64": ""})
        t.join(timeout=5)
        self.assertEqual(self.hub.inflight(sess.sid), 0)


def _sess(sid="SID", ip="10.0.0.2", port=80, scheme="http") -> ProxySession:
    return ProxySession(sid=sid, org_id="o", device_id=1, node_id="n",
                        device_ip=ip, device_port=port, scheme=scheme,
                        created_by=1, created_at=0.0, expires_at=1e12)


class RewriteHeadersTest(unittest.TestCase):
    def test_root_absolute_location_prefixed(self):
        out = rewrite_headers("SID", _sess(), [("Location", "/login?next=1")])
        self.assertEqual(out, [("Location", "/api/proxy/SID/login?next=1")])

    def test_device_origin_location_prefixed(self):
        out = rewrite_headers("SID", _sess(), [("Location", "http://10.0.0.2/x")])
        self.assertEqual(out[0][1], "/api/proxy/SID/x")
        out = rewrite_headers("SID", _sess(), [("Location", "http://10.0.0.2:80/y")])
        self.assertEqual(out[0][1], "/api/proxy/SID/y")
        out = rewrite_headers("SID", _sess(), [("Location", "http://10.0.0.2")])
        self.assertEqual(out[0][1], "/api/proxy/SID/")

    def test_external_location_untouched(self):
        loc = "https://vendor.example.com/firmware"
        out = rewrite_headers("SID", _sess(), [("Location", loc)])
        self.assertEqual(out[0][1], loc)

    def test_set_cookie_path_rescoped_and_duplicates_survive(self):
        pairs = [("Set-Cookie", "A=1; Path=/; HttpOnly"),
                 ("Set-Cookie", "B=2; path=/admin")]
        out = rewrite_headers("SID", _sess(), pairs)
        self.assertEqual(out[0][1], "A=1; Path=/api/proxy/SID/; HttpOnly")
        self.assertEqual(out[1][1], "B=2; path=/api/proxy/SID/admin")
        self.assertEqual(len(out), 2)

    def test_other_headers_pass_through(self):
        pairs = [("Content-Type", "text/html"), ("X-Frame-Options", "DENY")]
        self.assertEqual(rewrite_headers("SID", _sess(), pairs), pairs)


class RewriteBodyTest(unittest.TestCase):
    def test_html_root_absolute_attrs_prefixed(self):
        body = b'<a href="/a">x</a><img src=\'/i.png\'><form action="/save">'
        out = rewrite_body("SID", "text/html; charset=utf-8", body)
        self.assertIn(b'href="/api/proxy/SID/a"', out)
        self.assertIn(b"src='/api/proxy/SID/i.png'", out)
        self.assertIn(b'action="/api/proxy/SID/save"', out)

    def test_relative_and_protocol_relative_untouched(self):
        body = b'<a href="page.html">r</a><img src="//cdn.example.com/x.png">'
        self.assertEqual(rewrite_body("SID", "text/html", body), body)

    def test_css_url_prefixed_in_css_and_html(self):
        css = b'body { background: url(/bg.png) } .x{background:url("/y.png")}'
        out = rewrite_body("SID", "text/css", css)
        self.assertIn(b"url(/api/proxy/SID/bg.png)", out)
        self.assertIn(b'url("/api/proxy/SID/y.png")', out)
        self.assertIn(b"url(/api/proxy/SID/", rewrite_body(
            "SID", "text/html", b"<style>a{background:url(/z.png)}</style>"))

    def test_non_text_types_bit_identical(self):
        blob = b'\x89PNG href="/x" url(/y)'
        self.assertEqual(rewrite_body("SID", "image/png", blob), blob)
        self.assertEqual(rewrite_body("SID", "application/json",
                                      b'{"href": "/x"}'), b'{"href": "/x"}')


class _FakeClient:
    """Stands in for HttpCentralClient: yields queued requests, records replies."""

    def __init__(self, requests):
        self._requests = list(requests)
        self.replies = []

    def proxy_next(self, hold_s):
        return self._requests.pop(0) if self._requests else None

    def proxy_reply(self, sid, req_id, status, headers, body_b64, error=None):
        self.replies.append({"sid": sid, "req_id": req_id, "status": status,
                             "headers": headers, "body_b64": body_b64, "error": error})
        return {"ok": True}


def _req(**over):
    base = {"req_id": 1, "sid": "s", "method": "GET", "path": "/", "headers": {},
            "body_b64": None, "device_ip": "127.0.0.1", "device_port": 80,
            "scheme": "http"}
    base.update(over)
    return base


class ProxyTunnelTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(proxy_enabled=True, proxy_mgmt_ports="80",
                          proxy_poll_hold_s=0.2, proxy_workers=1,
                          proxy_request_timeout_s=2.0,
                          proxy_max_body_bytes=1_000_000)
        self.fetched = []

    def _fetcher(self, status=200, headers=None, body=b"BODY"):
        async def fetch(req, cfg):
            self.fetched.append(req)
            return status, (headers or {"Content-Type": "text/plain"}), body
        return fetch

    def _run(self, client, devices, fetcher):
        tunnel = ProxyTunnel(client, self.cfg, devices_provider=lambda: devices,
                             fetcher=fetcher)
        return asyncio.run(tunnel.serve_once())

    def test_serves_allowed_device(self):
        client = _FakeClient([_req(path="/status")])
        served = self._run(client, [{"ip_address": "127.0.0.1"}], self._fetcher())
        self.assertTrue(served)
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(client.replies[0]["status"], 200)
        self.assertEqual(base64.b64decode(client.replies[0]["body_b64"]), b"BODY")
        self.assertIsNone(client.replies[0]["error"])

    def test_refuses_ip_not_in_device_list(self):
        client = _FakeClient([_req(device_ip="10.9.9.9")])
        self._run(client, [{"ip_address": "127.0.0.1"}], self._fetcher())
        self.assertEqual(len(self.fetched), 0)  # never touched the device
        self.assertEqual(client.replies[0]["status"], 502)
        self.assertIn("not a device this node probes", client.replies[0]["error"])

    def test_refuses_port_outside_mgmt_set(self):
        client = _FakeClient([_req(device_port=8291)])
        self._run(client, [{"ip_address": "127.0.0.1"}], self._fetcher())
        self.assertEqual(len(self.fetched), 0)
        self.assertEqual(client.replies[0]["status"], 502)
        self.assertIn("not permitted", client.replies[0]["error"])

    def test_fetch_failure_reports_error_not_crash(self):
        async def boom(req, cfg):
            raise RuntimeError("connection refused")
        client = _FakeClient([_req()])
        self._run(client, [{"ip_address": "127.0.0.1"}], boom)
        self.assertEqual(client.replies[0]["status"], 502)
        self.assertIn("connection refused", client.replies[0]["error"])

    def test_oversize_response_refused(self):
        self.cfg = Config(proxy_enabled=True, proxy_mgmt_ports="80",
                          proxy_poll_hold_s=0.2, proxy_max_body_bytes=8)
        client = _FakeClient([_req()])
        self._run(client, [{"ip_address": "127.0.0.1"}],
                  self._fetcher(body=b"way-too-large-body"))
        self.assertEqual(client.replies[0]["status"], 502)
        self.assertIn("proxy_max_body_bytes", client.replies[0]["error"])

    def test_empty_poll_serves_nothing(self):
        client = _FakeClient([])
        self.assertFalse(self._run(client, [], self._fetcher()))


class TunnelDormancyTest(unittest.TestCase):
    """M1 activation model: zero long-polls while no session is live."""

    def test_workers_spin_up_on_sessions_and_stand_down(self):
        cfg = Config(proxy_enabled=True, proxy_mgmt_ports="80",
                     proxy_poll_hold_s=0.02, proxy_workers=2,
                     proxy_request_timeout_s=1.0, proxy_max_body_bytes=1000)
        client = _FakeClient([])

        async def run():
            tunnel = ProxyTunnel(client, cfg, devices_provider=lambda: [])
            tunnel._GRACE_S = 0.0  # test-only: don't wait the real 30s grace
            self.assertEqual(tunnel._tasks, [])   # dormant at construction
            tunnel.notify_sessions(None)          # idle reply: still dormant
            tunnel.notify_sessions([])
            self.assertEqual(tunnel._tasks, [])
            tunnel.notify_sessions([{"sid": "s1", "ttl_s": 0.3},
                                    {"sid": "junk", "ttl_s": "x"}])
            self.assertTrue(any(not t.done() for t in tunnel._tasks))
            await asyncio.sleep(0.6)              # deadline passed, no refresh
            self.assertTrue(all(t.done() for t in tunnel._tasks))
            tunnel.notify_sessions([{"sid": "s1", "ttl_s": 0.3}])  # re-arms
            self.assertTrue(any(not t.done() for t in tunnel._tasks))
            await tunnel.aclose()

        asyncio.run(run())

    def test_expired_ttls_do_not_wake_the_tunnel(self):
        cfg = Config(proxy_enabled=True, proxy_mgmt_ports="80",
                     proxy_poll_hold_s=0.02, proxy_workers=1,
                     proxy_request_timeout_s=1.0, proxy_max_body_bytes=1000)

        async def run():
            tunnel = ProxyTunnel(_FakeClient([]), cfg, devices_provider=lambda: [])
            tunnel.notify_sessions([{"sid": "s1", "ttl_s": 0},
                                    {"sid": "s2", "ttl_s": -5}])
            self.assertEqual(tunnel._tasks, [])
            await tunnel.aclose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
