"""Web-UI proxy tunnel — hub parking desk + edge worker, in isolation (no network).

The hub is the cross-thread parking desk (browser thread blocks, edge thread hands
off); the edge worker is the allow-list gate + device fetch. Full HTTP round-trip
lives in integration/test_central_proxy.py.
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.proxy import (
    AssetCache, ProxyHub, ProxySession, cache_key, cache_refusal, cacheable_path,
    cacheable_reply, is_connect_failure, parse_ports, rewrite_body,
    rewrite_headers,
)
from wisp.ingress.webproxy import (
    DeviceFetchError, ProxyTunnel, _ClientPool, _DeviceGate,
    _friendly_fetch_error, _is_connect_failure, _web_endpoints,
    make_pooled_fetch,
)


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

    def test_an_idle_window_narrows_active_to_sessions_in_USE(self):
        """"Is a session open" and "is a human driving it" are different
        questions, and the web-optics sweeper needs the second one.

        Nothing tells central a browser tab was closed, so an abandoned session
        stays open for the rest of its TTL — and the sweeper's browse gate is
        per-NODE, so answering the first question let one forgotten tab suppress
        the optical read of every OLT behind that probe. The edge path still
        asks the first (its tunnel should stay up for the whole TTL).
        """
        idle = self._open(ttl_s=600)
        idle.last_used_at = time.time() - 300
        busy = self.hub.open_session(
            org_id="o", device_id=2, node_id="n", device_ip="1.2.3.5",
            device_port=80, scheme="http", created_by=7, ttl_s=600)
        self.assertEqual(
            {s["sid"] for s in self.hub.active_sessions_for("o", "n")},
            {idle.sid, busy.sid})
        self.assertEqual(
            [s["sid"] for s in self.hub.active_sessions_for("o", "n", idle_s=180)],
            [busy.sid])

    def test_activity_marks_the_session_used(self):
        sess = self._open(ttl_s=600)
        sess.last_used_at = time.time() - 300
        self.hub.extend_session(sess, 600)
        self.assertEqual(
            [s["sid"] for s in self.hub.active_sessions_for("o", "n", idle_s=180)],
            [sess.sid])

    def test_has_session_is_expiry_aware(self):
        # The dashboard's "live" badge and its pulsing globe read this. A plain
        # membership test kept both claiming a session was open long after it
        # had timed out — sessions are dropped lazily, and the one thing that
        # would look this one up (its browser) is what has gone away.
        live, dead = self._open(ttl_s=60), self._open(ttl_s=-1)
        self.assertTrue(self.hub.has_session(live.sid))
        self.assertFalse(self.hub.has_session(dead.sid))

    def test_reap_expired_returns_the_sids_it_dropped(self):
        live, dead = self._open(ttl_s=60), self._open(ttl_s=-1)
        self.assertEqual(self.hub.reap_expired(), [dead.sid])
        self.assertEqual(self.hub.reap_expired(), [])   # nothing left to retire
        self.assertTrue(self.hub.has_session(live.sid))

    def test_submit_extra_merges_but_keeps_base_fields(self):
        # the preflight rides extra=; an old edge must still see a plain fetch
        sess = self._open()
        t = threading.Thread(target=lambda: self.hub.submit(
            sess, method="GET", path="/", headers={}, body=b"", timeout=2,
            extra={"kind": "preflight", "candidates": [["1.2.3.4", 443, "https"]]}))
        t.start()
        payload = self.hub.next_request("o", "n", 2.0)
        self.assertEqual(payload["kind"], "preflight")
        self.assertEqual(payload["candidates"], [["1.2.3.4", 443, "https"]])
        self.assertEqual(payload["device_ip"], "1.2.3.4")   # base fields survive
        self.assertEqual(payload["scheme"], "http")
        self.hub.deliver(payload["req_id"], "o", "n",
                         {"status": 200, "headers": {}, "body_b64": ""})
        t.join(timeout=5)

    def test_polled_recently_tracks_next_request(self):
        self.assertFalse(self.hub.polled_recently("o", "n", 30.0))
        self.hub.next_request("o", "n", 0.05)
        self.assertTrue(self.hub.polled_recently("o", "n", 30.0))
        self.assertFalse(self.hub.polled_recently("o", "OTHER", 30.0))

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

    def test_serves_owner_declared_web_endpoint(self):
        # OLT web UI is port-forwarded to a DIFFERENT IP on a NON-mgmt port; the
        # (web_ip, web_port) pair the owner declared is allowed even though the IP
        # isn't the probe IP and 8080 isn't in proxy_mgmt_ports.
        client = _FakeClient([_req(device_ip="203.0.113.9", device_port=8080)])
        devices = [{"ip_address": "10.0.0.5", "web_ip": "203.0.113.9",
                    "web_port": 8080, "web_scheme": "http"}]
        self._run(client, devices, self._fetcher())
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(client.replies[0]["status"], 200)

    def test_web_override_pair_must_match_exactly(self):
        # Same declared IP but a port the owner never declared is still refused —
        # the override is a pair, not a blanket IP allow.
        client = _FakeClient([_req(device_ip="203.0.113.9", device_port=9999)])
        devices = [{"ip_address": "10.0.0.5", "web_ip": "203.0.113.9",
                    "web_port": 8080, "web_scheme": "http"}]
        self._run(client, devices, self._fetcher())
        self.assertEqual(len(self.fetched), 0)
        self.assertEqual(client.replies[0]["status"], 502)


class WebEndpointsTest(unittest.TestCase):
    """The edge's owner-declared-endpoint resolution mirrors central's."""

    def test_no_override_contributes_nothing(self):
        self.assertEqual(_web_endpoints([{"ip_address": "10.0.0.5"}]), frozenset())

    def test_ip_and_port_pair(self):
        self.assertEqual(
            _web_endpoints([{"ip_address": "10.0.0.5", "web_ip": "203.0.113.9",
                             "web_port": 8080}]),
            frozenset({("203.0.113.9", 8080)}))

    def test_port_only_uses_probe_ip(self):
        # a custom mgmt port on the same box (no separate IP)
        self.assertEqual(
            _web_endpoints([{"ip_address": "10.0.0.5", "web_port": 8443}]),
            frozenset({("10.0.0.5", 8443)}))

    def test_scheme_only_picks_default_port(self):
        self.assertEqual(
            _web_endpoints([{"ip_address": "10.0.0.5", "web_scheme": "https"}]),
            frozenset({("10.0.0.5", 443)}))
        self.assertEqual(
            _web_endpoints([{"ip_address": "10.0.0.5", "web_ip": "203.0.113.9",
                             "web_scheme": "http"}]),
            frozenset({("203.0.113.9", 80)}))


class PreflightTest(unittest.TestCase):
    """kind="preflight": connect-probe candidates through the SAME allow-list
    gate as fetches, never touch the device's HTTP layer."""

    def setUp(self):
        self.cfg = Config(proxy_enabled=True, proxy_mgmt_ports="80,443",
                          proxy_poll_hold_s=0.2, proxy_workers=1,
                          proxy_request_timeout_s=2.0,
                          proxy_max_body_bytes=1_000_000)
        self.probed = []

    def _prober(self, ok_for=frozenset()):
        async def probe(ip, port, scheme, timeout_s):
            self.probed.append((ip, port, scheme))
            return None if (ip, port, scheme) in ok_for else "connect timeout"
        return probe

    async def _no_fetch(self, req, cfg):
        raise AssertionError("preflight must never reach the fetch path")

    def _run(self, client, devices, prober):
        tunnel = ProxyTunnel(client, self.cfg, devices_provider=lambda: devices,
                             fetcher=self._no_fetch, prober=prober)
        return asyncio.run(tunnel.serve_once())

    @staticmethod
    def _results(client):
        doc = json.loads(base64.b64decode(client.replies[0]["body_b64"]))
        return doc

    def test_probes_candidates_and_reports_shape(self):
        client = _FakeClient([_req(kind="preflight",
                                   candidates=[["127.0.0.1", 443, "https"],
                                               ["127.0.0.1", 80, "http"]])])
        self._run(client, [{"ip_address": "127.0.0.1"}],
                  self._prober(ok_for={("127.0.0.1", 80, "http")}))
        doc = self._results(client)
        self.assertTrue(doc["preflight"])
        by_key = {(r[0], r[1], r[2]): r for r in doc["results"]}
        self.assertFalse(by_key[("127.0.0.1", 443, "https")][3])
        self.assertTrue(by_key[("127.0.0.1", 80, "http")][3])
        self.assertEqual(client.replies[0]["status"], 200)
        self.assertIsNone(client.replies[0]["error"])

    def test_disallowed_candidate_never_probed(self):
        client = _FakeClient([_req(kind="preflight",
                                   candidates=[["10.9.9.9", 8080, "http"],
                                               ["127.0.0.1", 80, "http"]])])
        self._run(client, [{"ip_address": "127.0.0.1"}],
                  self._prober(ok_for={("127.0.0.1", 80, "http")}))
        self.assertNotIn(("10.9.9.9", 8080, "http"), self.probed)
        doc = self._results(client)
        row = next(r for r in doc["results"] if r[0] == "10.9.9.9")
        self.assertFalse(row[3])
        self.assertEqual(row[4], "not permitted")

    def test_owner_declared_endpoint_probeable(self):
        client = _FakeClient([_req(kind="preflight",
                                   candidates=[["203.0.113.9", 8080, "https"]])])
        devices = [{"ip_address": "10.0.0.5", "web_ip": "203.0.113.9",
                    "web_port": 8080}]
        self._run(client, devices,
                  self._prober(ok_for={("203.0.113.9", 8080, "https")}))
        doc = self._results(client)
        self.assertTrue(doc["results"][0][3])


class FriendlyFetchErrorTest(unittest.TestCase):
    """Fast-failure copy: the 502 string must name the fix, not the httpx class."""

    def setUp(self):
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.skipTest("httpx not installed (central-only environment)")

    def test_connect_timeout_names_dead_target(self):
        import httpx
        from wisp.ingress.webproxy import _friendly_fetch_error
        msg = _friendly_fetch_error(httpx.ConnectTimeout("x"), "10.0.0.5", 443, "https")
        self.assertIn("connect timeout", msg)
        self.assertIn("10.0.0.5:443", msg)

    def test_tls_failure_suggests_plain_http(self):
        import httpx
        from wisp.ingress.webproxy import _friendly_fetch_error
        exc = httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
        msg = _friendly_fetch_error(exc, "10.0.0.5", 8080, "https")
        self.assertIn("TLS", msg)
        self.assertIn("http", msg)

    def test_connection_refused_suggests_port(self):
        import httpx
        from wisp.ingress.webproxy import _friendly_fetch_error
        exc = httpx.ConnectError("All connection attempts failed: connection refused")
        msg = _friendly_fetch_error(exc, "10.0.0.5", 80, "http")
        self.assertIn("refused", msg)

    def test_protocol_garbage_suggests_other_scheme(self):
        import httpx
        from wisp.ingress.webproxy import _friendly_fetch_error
        msg = _friendly_fetch_error(httpx.RemoteProtocolError("bad"), "10.0.0.5", 443, "http")
        self.assertIn("try https", msg)

    def test_unknown_exception_passes_through(self):
        from wisp.ingress.webproxy import _friendly_fetch_error
        self.assertEqual(
            _friendly_fetch_error(ValueError("odd thing"), "10.0.0.5", 80, "http"),
            "odd thing")


class TunnelStandbyTest(unittest.TestCase):
    """First-connect fix (2026-07-20): ``proxy_standby`` holds exactly ONE
    long-poll while idle; a live session scales it to the full pool and back."""

    def _cfg(self, workers=3):
        return Config(proxy_enabled=True, proxy_mgmt_ports="80",
                      proxy_poll_hold_s=0.02, proxy_workers=workers,
                      proxy_request_timeout_s=1.0, proxy_max_body_bytes=1000)

    @staticmethod
    def _live(tunnel):
        return sum(1 for t in tunnel._tasks if not t.done())

    def test_standby_holds_exactly_one_worker(self):
        async def run():
            tunnel = ProxyTunnel(_FakeClient([]), self._cfg(),
                                 devices_provider=lambda: [])
            tunnel.notify_standby(False)      # org without the proxy: dormant
            self.assertEqual(tunnel._tasks, [])
            tunnel.notify_standby(True)
            self.assertEqual(self._live(tunnel), 1)
            tunnel.notify_standby(True)       # refresh must not add workers
            self.assertEqual(self._live(tunnel), 1)
            await tunnel.aclose()

        asyncio.run(run())

    def test_standby_lapses_without_refresh(self):
        async def run():
            tunnel = ProxyTunnel(_FakeClient([]), self._cfg(),
                                 devices_provider=lambda: [])
            tunnel._STANDBY_TTL_S = 0.15
            tunnel.notify_standby(True)
            self.assertEqual(self._live(tunnel), 1)
            await asyncio.sleep(0.5)          # central stopped sending the key
            self.assertEqual(self._live(tunnel), 0)
            await tunnel.aclose()

        asyncio.run(run())

    def test_session_scales_standby_up_then_back_to_one(self):
        async def run():
            tunnel = ProxyTunnel(_FakeClient([]), self._cfg(workers=3),
                                 devices_provider=lambda: [])
            tunnel._GRACE_S = 0.0
            tunnel.notify_standby(True)
            self.assertEqual(self._live(tunnel), 1)
            tunnel.notify_sessions([{"sid": "s1", "ttl_s": 0.3}])
            self.assertEqual(self._live(tunnel), 3)
            await asyncio.sleep(0.7)          # session lapsed, standby still armed
            self.assertEqual(self._live(tunnel), 1)
            await tunnel.aclose()

        asyncio.run(run())


class TunnelDormancyTest(unittest.TestCase):
    """Activation model: zero long-polls while no session is live AND the org
    hasn't enabled the web proxy (no ``proxy_standby`` refresh)."""

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


class AssetCacheTest(unittest.TestCase):
    """The per-session memo that took 44% of the tunnel's traffic off the wire.
    What it REFUSES to remember is the whole safety story."""

    def test_cacheable_path_is_a_closed_extension_list(self):
        self.assertTrue(cacheable_path("GET", "/js/jquery-1.7.1.min.js"))
        self.assertTrue(cacheable_path("GET", "/i18N/main_en_US.properties"))
        self.assertTrue(cacheable_path("get", "/images/logo.png?v=2"))
        # this vendor's DYNAMIC pages are .html — never inferred as static
        self.assertFalse(cacheable_path("GET", "/action/onuauthinfo.html"))
        self.assertFalse(cacheable_path("GET", "/action/main.html"))
        self.assertFalse(cacheable_path("GET", "/status"))
        # a POST changes something on the box, by definition
        self.assertFalse(cacheable_path("POST", "/js/app.js"))

    def test_a_reply_carrying_state_is_refused(self):
        ok = [("Content-Type", "application/javascript")]
        self.assertTrue(cacheable_reply(200, ok))
        self.assertFalse(cacheable_reply(404, ok))
        self.assertFalse(cacheable_reply(302, ok))
        self.assertFalse(cacheable_reply(200, [("Set-Cookie", "S=1; Path=/")]))
        self.assertFalse(cacheable_reply(200, [("Cache-Control", "no-store")]))
        self.assertFalse(cacheable_reply(200, [("Vary", "Cookie")]))
        # max-age alone is a device saying "yes, cache me"
        self.assertTrue(cacheable_reply(200, [("Cache-Control", "max-age=600")]))
        # the one Vary we can honour: the edge hands central decoded bytes
        self.assertTrue(cacheable_reply(200, [("Vary", "Accept-Encoding")]))

    def test_no_cache_and_private_are_deliberately_defied(self):
        """`private` addresses SHARED caches and this one is per-session by
        construction; `no-cache` means store-then-revalidate and this firmware
        ships no validator to revalidate with. Honouring either literally means
        the cache never works on the whole fleet — which is how a 2011 jQuery
        got fetched 553 times in one session. The vendor's own `?rand=` busting
        is what keeps the genuinely volatile files missing."""
        self.assertTrue(cacheable_reply(200, [("Cache-Control", "no-cache")]))
        self.assertTrue(cacheable_reply(200, [("Pragma", "no-cache")]))
        self.assertTrue(cacheable_reply(200, [("Cache-Control", "private, max-age=0")]))
        # but the one that means "do not write this down" still means it
        self.assertFalse(cacheable_reply(200, [("Cache-Control", "no-cache, no-store")]))

    def test_no_store_matches_a_token_not_a_substring(self):
        self.assertTrue(cacheable_reply(200, [("Cache-Control", "max-age=60")]))
        self.assertTrue(cacheable_reply(
            200, [("Content-Disposition", "inline; filename=no-store.js")]))

    def test_the_refusal_reason_is_reportable(self):
        """An empty cache and a working one look identical from outside — the
        reason has to be reachable without a debugger in production."""
        self.assertIsNone(cache_refusal(200, [("Content-Type", "text/css")]))
        self.assertEqual(cache_refusal(404, []), "status 404")
        self.assertEqual(cache_refusal(200, [("Set-Cookie", "a=b")]),
                         "carries Set-Cookie")
        self.assertIn("no-store", cache_refusal(200, [("Cache-Control", "no-store")]))
        self.assertIn("Cookie", cache_refusal(200, [("Vary", "Cookie")]))

    def test_jquery_own_cache_buster_is_stripped_but_the_vendors_is_not(self):
        """`_=<ts>` is what `$.ajax({cache:false})` appends to everything — a
        CLIENT LIBRARY statement about the browser cache, not a vendor statement
        about the resource. 20% of this tunnel's traffic is a static .properties
        translation table wearing one. `rand=` is written by the firmware's own
        HTML per script tag and stays keyed."""
        self.assertEqual(cache_key("/i18N/error_en_US.properties?_=1785323171532"),
                         "/i18N/error_en_US.properties")
        self.assertEqual(cache_key("/js/misc.js?rand=15959"), "/js/misc.js?rand=15959")
        self.assertEqual(cache_key("/js/app.js"), "/js/app.js")
        # a `_` alongside real parameters loses only itself
        self.assertEqual(cache_key("/a.js?lang=en&_=99&v=2"), "/a.js?lang=en&v=2")
        # and a parameter that merely STARTS with _ is somebody else's
        self.assertEqual(cache_key("/a.js?_token=x"), "/a.js?_token=x")

    def test_round_trip_and_ttl_expiry(self):
        c = AssetCache(ttl_s=60.0)
        self.assertIsNone(c.get("/a.js"))
        c.put("/a.js", 200, [("Content-Type", "text/javascript")], b"x=1")
        self.assertEqual(c.get("/a.js"), (200, [("Content-Type", "text/javascript")], b"x=1"))
        expired = AssetCache(ttl_s=-1.0)
        expired.put("/a.js", 200, [], b"x=1")
        self.assertIsNone(expired.get("/a.js"))

    def test_a_returned_header_list_cannot_be_mutated_back_into_the_cache(self):
        """The caller filters and rewrites the pairs in place; handing out the
        stored list would let one request's rewrite leak into the next."""
        c = AssetCache()
        c.put("/a.js", 200, [("Content-Type", "text/javascript")], b"x")
        _, pairs, _ = c.get("/a.js")
        pairs.append(("X-Injected", "1"))
        _, again, _ = c.get("/a.js")
        self.assertEqual(again, [("Content-Type", "text/javascript")])

    def test_bounded_by_entries_and_bytes(self):
        c = AssetCache(max_entries=2, max_bytes=10_000)
        for name in ("a", "b", "c"):
            c.put(f"/{name}.js", 200, [], b"x")
        self.assertEqual(c.stats()["entries"], 2)
        self.assertIsNone(c.get("/a.js"), "oldest should have been evicted")
        self.assertIsNotNone(c.get("/c.js"))

        big = AssetCache(max_entries=100, max_bytes=100)
        big.put("/one.js", 200, [], b"y" * 60)
        big.put("/two.js", 200, [], b"y" * 60)
        self.assertEqual(big.stats()["entries"], 1)
        self.assertLessEqual(big.stats()["bytes"], 100)

    def test_an_entry_larger_than_the_whole_budget_is_simply_not_stored(self):
        c = AssetCache(max_entries=10, max_bytes=100)
        c.put("/huge.js", 200, [], b"z" * 500)
        self.assertIsNone(c.get("/huge.js"))
        self.assertEqual(c.stats()["entries"], 0)


class CentralDeviceThrottleTest(unittest.TestCase):
    """The fix for the PROVEN cause: SRPL-OLT's every failure logged `connect
    timeout` — the TCP connect never completed, which is a box dropping SYNs
    because its accept queue is full. Central caps what one DEVICE is asked to
    accept at once, and needs no fleet rollout to start doing it."""

    def _hub(self, top=4):
        return ProxyHub(device_max_inflight=top)

    def _sess(self, hub, device_id=1):
        return hub.open_session(
            org_id="o", device_id=device_id, node_id="n", device_ip="1.2.3.4",
            device_port=443, scheme="https", created_by=1, ttl_s=60)

    def test_it_bounds_concurrent_submits_for_one_device(self):
        hub = self._hub(top=2)
        sess = self._sess(hub)
        started, blocked = threading.Semaphore(0), threading.Event()
        done = threading.Event()

        def hold():
            started.release()
            hub.submit(sess, method="GET", path="/a.js", headers={}, body=b"",
                       timeout=3.0)

        holders = [threading.Thread(target=hold, daemon=True) for _ in range(2)]
        for t in holders:
            t.start()
        for _ in holders:
            started.acquire()
        # both slots are taken by requests nobody will answer; a third must not
        # reach the device at all — it waits, then reads as a timeout
        def third():
            blocked.set()
            self.assertIsNone(
                hub.submit(sess, method="GET", path="/b.js", headers={},
                           body=b"", timeout=0.4))
            done.set()

        t3 = threading.Thread(target=third, daemon=True)
        t3.start()
        self.assertTrue(blocked.wait(2))
        self.assertTrue(done.wait(3), "the third request was not gated")
        # the gated one never got parked, so the edge — and the device — never
        # saw it: exactly the connection that was being dropped before
        parked = []
        while True:
            req = hub.next_request("o", "n", 0.05)
            if req is None:
                break
            parked.append(req["path"])
        self.assertEqual(parked, ["/a.js", "/a.js"])
        self.assertNotIn("/b.js", parked)

    def test_a_slot_is_released_even_when_the_request_times_out(self):
        hub = self._hub(top=1)
        sess = self._sess(hub)
        self.assertIsNone(hub.submit(sess, method="GET", path="/a.js",
                                     headers={}, body=b"", timeout=0.2))
        # the failed request must not have leaked its slot
        self.assertIsNone(hub.submit(sess, method="GET", path="/b.js",
                                     headers={}, body=b"", timeout=0.2))
        self.assertEqual(hub.device_limit("o", 1), 1)

    def test_it_narrows_only_on_a_connect_failure(self):
        hub = self._hub(top=4)
        self.assertEqual(hub.device_limit("o", 1), 4)
        # a bad answer over a good connection says nothing about capacity
        self.assertIsNone(hub.note_failure("o", 1, "404 not found"))
        self.assertIsNone(hub.note_failure(
            "o", 1, "TLS handshake failed on 1.2.3.4:443 — "
                    "the device likely speaks plain http there"))
        self.assertEqual(hub.device_limit("o", 1), 4)
        self.assertEqual(
            hub.note_failure("o", 1,
                             "connect timeout to 1.2.3.4:443 — nothing answering there"),
            2)
        self.assertEqual(hub.device_limit("o", 1), 2)
        # 2 is the FLOOR, measured: at 1 the tunnel stops overlapping the WAN
        # legs and every asset pays for that dead air (median gap 1.50s at 1,
        # 0.00s at 2). Curing the failures by serialising costs more than the
        # failures did.
        self.assertIsNone(hub.note_failure("o", 1, "could not connect to 1.2.3.4:443: x"))
        self.assertEqual(hub.device_limit("o", 1), 2)

    def test_the_ladder_never_serialises_a_device(self):
        hub = self._hub(top=4)
        for _ in range(10):
            hub.note_failure("o", 1, "connect timeout to 1.2.3.4:443")
        self.assertEqual(hub.device_limit("o", 1), 2)

    def test_narrowing_one_device_leaves_its_neighbours_alone(self):
        hub = self._hub(top=4)
        hub.note_failure("o", 1, "connect timeout to 1.2.3.4:443")
        self.assertEqual(hub.device_limit("o", 1), 2)
        self.assertEqual(hub.device_limit("o", 2), 4)
        self.assertEqual(hub.device_limit("other-org", 1), 4)

    def test_the_rung_outlives_the_session(self):
        """Keyed on the DEVICE: reopening a tab must inherit what we already
        learned about the box, not restart the ladder."""
        hub = self._hub(top=4)
        first = self._sess(hub)
        hub.note_failure("o", first.device_id, "connect timeout to 1.2.3.4:443")
        hub.close_session(first.sid)
        self.assertEqual(hub.device_limit("o", first.device_id), 2)

    def test_a_narrowed_device_re_probes_one_rung_faster_later(self):
        hub = self._hub(top=4)
        hub.note_failure("o", 1, "connect timeout to 1.2.3.4:443")
        self.assertEqual(hub.device_limit("o", 1), 2)
        hub._throttle("o", 1)._promote_at = time.monotonic() - 1
        sess = self._sess(hub)
        hub.submit(sess, method="GET", path="/a.js", headers={}, body=b"",
                   timeout=0.1)   # an acquire is what re-probes
        self.assertEqual(hub.device_limit("o", 1), 4)


class ConnectFailureWordingTest(unittest.TestCase):
    """Central classifies the EDGE's failure prose, because the fleet cannot be
    updated in the same breath as central. That is a coupling across two
    modules, so it gets pinned to the real function rather than to a copy of
    its wording."""

    def _sentence(self, exc):
        return _friendly_fetch_error(exc, "1.2.3.4", 443, "https")

    def test_every_connect_family_sentence_is_recognised(self):
        import httpx
        for exc in (httpx.ConnectTimeout("t"),
                    httpx.ConnectError("connection refused"),
                    httpx.ConnectError("something else entirely"),
                    httpx.ReadTimeout("slow")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(is_connect_failure(self._sentence(exc)),
                                self._sentence(exc))

    def test_a_configuration_error_is_not_a_capacity_signal(self):
        import httpx
        for exc in (httpx.ConnectError("SSL: WRONG_VERSION_NUMBER"),
                    httpx.RemoteProtocolError("not http")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(is_connect_failure(self._sentence(exc)),
                                 self._sentence(exc))

    def test_an_absent_or_unknown_error_never_narrows(self):
        self.assertFalse(is_connect_failure(None))
        self.assertFalse(is_connect_failure(""))
        self.assertFalse(is_connect_failure("device response exceeds proxy_max_body_bytes"))
        self.assertFalse(is_connect_failure("target is not a device this node probes"))


class _RecordingClient:
    """Stands in for an httpx.AsyncClient: counts requests and can be told to
    fail the first one the way a device closing a pooled connection does."""

    def __init__(self, fail_first: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_first = fail_first
        self.closed = False

    async def request(self, method, url, content=None, headers=None):
        self.calls.append((method, url))
        if self.fail_first is not None and len(self.calls) == 1:
            raise self.fail_first
        return _FakeResponse()

    async def aclose(self):
        self.closed = True


class _FakeResponse:
    status_code = 200
    content = b"body"

    class _H:
        @staticmethod
        def multi_items():
            return [("Content-Type", "text/plain")]

    headers = _H()


class ClientPoolTest(unittest.TestCase):
    """Connection reuse — the root cause of 'laggy through the tunnel, instant
    on the LAN'. One handshake per asset, on a box with no crypto acceleration,
    IS the page load."""

    def _cfg(self):
        return Config(proxy_enabled=True, proxy_mgmt_ports="80",
                      proxy_request_timeout_s=5.0, proxy_keepalive_idle_s=90.0,
                      proxy_device_max_inflight=4)

    def test_one_client_per_endpoint_is_reused(self):
        cfg = self._cfg()

        async def run():
            pool = _ClientPool(cfg)
            made = []
            pool._build = lambda s, i, p: made.append(_RecordingClient()) or made[-1]
            a = await pool.get("http", "1.2.3.4", 80)
            b = await pool.get("http", "1.2.3.4", 80)
            c = await pool.get("https", "1.2.3.4", 443)
            self.assertIs(a, b, "a second asset re-handshaked the device")
            self.assertIsNot(a, c, "different endpoints must not share a client")
            await pool.aclose()
            self.assertTrue(all(m.closed for m in made))

        asyncio.run(run())

    def test_a_stale_pooled_connection_costs_one_silent_retry_on_a_GET(self):
        """Embedded servers reap idle sockets aggressively; reusing one they
        just closed is normal and must never surface as a 502 to the tech."""
        import httpx
        cfg = self._cfg()

        async def run():
            pool = _ClientPool(cfg)
            clients = []

            def build(s, i, p):
                exc = httpx.RemoteProtocolError("Server disconnected") \
                    if not clients else None
                clients.append(_RecordingClient(fail_first=exc))
                return clients[-1]

            pool._build = build
            fetch = make_pooled_fetch(pool)
            status, _, body = await fetch(
                {"method": "GET", "device_ip": "1.2.3.4", "device_port": 80,
                 "scheme": "http", "path": "/js/app.js"}, cfg)
            self.assertEqual((status, body), (200, b"body"))
            self.assertEqual(len(clients), 2, "the dead client was not replaced")
            await pool.aclose()

        asyncio.run(run())

    def test_a_POST_is_never_silently_replayed(self):
        """A write that died without a reply may still have been applied.
        Re-submitting a config change is worse than the 502."""
        import httpx
        cfg = self._cfg()

        async def run():
            pool = _ClientPool(cfg)
            clients = []

            def build(s, i, p):
                clients.append(_RecordingClient(
                    fail_first=httpx.RemoteProtocolError("Server disconnected")))
                return clients[-1]

            pool._build = build
            fetch = make_pooled_fetch(pool)
            with self.assertRaises(RuntimeError):
                await fetch({"method": "POST", "device_ip": "1.2.3.4",
                             "device_port": 80, "scheme": "http",
                             "path": "/action/save.html"}, cfg)
            self.assertEqual(len(clients), 1, "the POST was retried")
            await pool.aclose()

        asyncio.run(run())


class DeviceGateTest(unittest.TestCase):
    """Two of twenty devices on this fleet answer ~1 request at a time and
    refused 4-5% of what we sent, while peers on the SAME probe took 8-9 in
    parallel and refused 0.1%. No vendor hardcode: walk down, heal up."""

    def _gate(self, top=4):
        return _DeviceGate(Config(proxy_device_max_inflight=top))

    def test_starts_wide_and_narrows_only_on_a_connect_failure(self):
        g = self._gate()
        self.assertEqual(g.limit("1.2.3.4", 443), 4)
        self.assertTrue(g.demote("1.2.3.4", 443))
        self.assertEqual(g.limit("1.2.3.4", 443), 2)
        self.assertTrue(g.demote("1.2.3.4", 443))
        self.assertEqual(g.limit("1.2.3.4", 443), 1)
        # floor: there is no such thing as half a connection
        self.assertFalse(g.demote("1.2.3.4", 443))
        self.assertEqual(g.limit("1.2.3.4", 443), 1)

    def test_narrowing_one_box_leaves_its_neighbours_alone(self):
        g = self._gate()
        g.demote("1.2.3.4", 443)
        g.demote("1.2.3.4", 443)
        self.assertEqual(g.limit("1.2.3.4", 443), 1)
        self.assertEqual(g.limit("5.6.7.8", 443), 4)

    def test_a_narrowed_box_re_probes_one_rung_faster_later(self):
        """A firmware fix or a reboot must heal without anyone noticing it
        happened — the same instinct as PysnmpPoller's ladder."""
        g = self._gate()
        g.demote("1.2.3.4", 443)
        g.demote("1.2.3.4", 443)
        self.assertEqual(g.limit("1.2.3.4", 443), 1)
        g._promote_at[("1.2.3.4", 443)] = time.monotonic() - 1
        self.assertEqual(g.limit("1.2.3.4", 443), 1)  # limit() does not promote
        g.semaphore("1.2.3.4", 443)
        self.assertEqual(g.limit("1.2.3.4", 443), 2)

    def test_semaphore_actually_bounds_concurrency(self):
        g = self._gate(top=2)

        async def run():
            sem = g.semaphore("1.2.3.4", 443)
            await sem.acquire()
            await sem.acquire()
            self.assertTrue(sem.locked())
            sem.release()
            sem.release()

        asyncio.run(run())

    def test_a_fleet_configured_down_to_one_still_has_a_valid_ladder(self):
        g = self._gate(top=1)
        self.assertEqual(g.limit("1.2.3.4", 443), 1)
        self.assertFalse(g.demote("1.2.3.4", 443))


class ConnectFailureClassificationTest(unittest.TestCase):
    """Only a failure to GET A CONNECTION says anything about how many the box
    can take. A 404 or a slow page proves nothing about capacity."""

    def test_classification(self):
        import httpx
        self.assertTrue(_is_connect_failure(httpx.ConnectError("refused")))
        self.assertTrue(_is_connect_failure(httpx.ConnectTimeout("timeout")))
        self.assertTrue(_is_connect_failure(httpx.PoolTimeout("pool")))
        self.assertFalse(_is_connect_failure(httpx.ReadTimeout("slow page")))
        self.assertFalse(_is_connect_failure(httpx.RemoteProtocolError("stale")))

    def test_the_error_carries_its_classification_to_the_caller(self):
        err = DeviceFetchError("connect timeout to x", connect_failure=True)
        self.assertIsInstance(err, RuntimeError)   # existing handlers untouched
        self.assertTrue(err.connect_failure)
        self.assertEqual(str(err), "connect timeout to x")
        self.assertFalse(DeviceFetchError("404").connect_failure)


if __name__ == "__main__":
    unittest.main()
