"""The edge -> central body is gzipped, and central accepts BOTH shapes.

Two halves, pinned separately: `server.gunzip_bounded`/`decode_body` (what
central will read) and `HttpCentralClient._encode` (what the edge will write).
The pair is exercised end to end over a real socket in
`integration/test_central_gzip`.
"""

import gzip
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.server import decode_body, gunzip_bounded
from wisp.runtime.central_client import HttpCentralClient


def _gz(data: bytes, level: int = 1) -> bytes:
    return gzip.compress(data, level, mtime=0)


class GunzipBoundedTest(unittest.TestCase):
    def test_round_trip(self):
        body = json.dumps({"ports": [{"if_index": i} for i in range(500)]}).encode()
        self.assertEqual(gunzip_bounded(_gz(body)), body)

    def test_the_decompressed_bound_bites(self):
        # The point of the whole guard: Content-Length bounds what a client may
        # SEND, and once bodies may be compressed that stops bounding what
        # central ALLOCATES. 4 MB of zeros ships in a few KB.
        bomb = _gz(b"\0" * (4 * 1024 * 1024))
        self.assertLess(len(bomb), 64 * 1024, "the bomb must sail past Content-Length")
        self.assertIsNone(gunzip_bounded(bomb, limit=64 * 1024))
        # ...and the same bytes are fine under a ceiling that fits them.
        self.assertEqual(len(gunzip_bounded(bomb, limit=8 * 1024 * 1024) or b""),
                         4 * 1024 * 1024)

    def test_a_body_exactly_at_the_ceiling_is_still_accepted(self):
        body = b"x" * 4096
        self.assertEqual(gunzip_bounded(_gz(body), limit=4096), body)

    def test_corrupt_returns_none_and_never_raises(self):
        self.assertIsNone(gunzip_bounded(b"\x1f\x8bnot really gzip at all"))
        self.assertIsNone(gunzip_bounded(b"\x1f\x8b"))
        self.assertIsNone(gunzip_bounded(b""))

    def test_a_TRUNCATED_stream_is_refused_even_though_it_would_parse(self):
        # The bytes that arrived happen to be complete JSON, but the gzip
        # trailer says we did not get the whole member. Half a port table that
        # files as a complete walk is the failure this codebase keeps paying
        # for, so an unterminated stream is refused, not salvaged.
        whole = _gz(b'{"ports":{"1":[]}}')
        self.assertIsNone(gunzip_bounded(whole[:-4]))


class DecodeBodyTest(unittest.TestCase):
    def test_plain_body_passes_through_untouched(self):
        raw = b'{"v":1}'
        self.assertEqual(decode_body(raw, ""), raw)

    def test_gzip_body_with_the_header_inflates(self):
        raw = b'{"v":1,"mode":"full"}'
        self.assertEqual(decode_body(_gz(raw), "gzip"), raw)

    def test_the_MAGIC_outranks_the_header_in_both_directions(self):
        # A middlebox in front of central may rewrite the header. The bytes it
        # cannot fake: a gzip member always opens 1f 8b and JSON text never can.
        raw = b'{"v":1}'
        # header stripped, bytes still compressed
        self.assertEqual(decode_body(_gz(raw), ""), raw)
        # header left behind, bytes already inflated
        self.assertEqual(decode_body(raw, "gzip"), raw)

    def test_an_undecodable_gzip_body_returns_None_for_the_caller_to_400(self):
        self.assertIsNone(decode_body(b"\x1f\x8bgarbage", "gzip"))

    def test_an_unknown_encoding_is_left_alone_and_fails_at_the_json_parse(self):
        # Nothing we send is brotli; central refuses it the way it refuses any
        # unparsable body rather than pretending to understand it.
        self.assertEqual(decode_body(b"\x1b\x2egarbage", "br"), b"\x1b\x2egarbage")


class EncodeBodyTest(unittest.TestCase):
    def _client(self, **kw):
        return HttpCentralClient(Config(central_url="http://c", org_id="o",
                                        node_id="n", **kw))

    def _env(self, n_ports: int) -> dict:
        return {"v": 1, "org_id": "o", "node_id": "n", "mode": "full",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 3.0}},
                "ports": {"1": [{"if_index": 100 + i, "admin_status": "up",
                                 "oper_status": "up", "in_octets": 10 ** 9 + i,
                                 "out_octets": 10 ** 8 + i} for i in range(n_ports)]}}

    def test_a_small_body_is_NOT_compressed_and_carries_no_encoding_header(self):
        # Below the floor the saving is noise and the CPU is real on a small
        # probe box. A ping-only report is ~0.4 KB.
        body, headers = self._client()._encode({"v": 1, "pings": {}})
        self.assertEqual(headers, {})
        self.assertEqual(json.loads(body)["v"], 1)

    def test_a_big_body_is_gzipped_and_declares_it(self):
        client = self._client()
        env = self._env(2000)
        body, headers = client._encode(env)
        self.assertEqual(headers, {"Content-Encoding": "gzip"})
        self.assertEqual(body[:2], b"\x1f\x8b")
        self.assertEqual(json.loads(gunzip_bounded(body) or b"{}"), env)
        plain = json.dumps(env, separators=(",", ":")).encode()
        self.assertLess(len(body), len(plain) / 2)

    def test_a_plain_request_never_inherits_a_stale_encoding_header(self):
        # `_headers()` puts Content-Type on the httpx CLIENT once; the encoding
        # is per-request, so a small report following a big one must not still
        # be claiming gzip.
        client = self._client()
        self.assertEqual(client._encode(self._env(2000))[1],
                         {"Content-Encoding": "gzip"})
        self.assertEqual(client._encode({"v": 1, "pings": {}})[1], {})

    def test_the_threshold_is_the_escape_hatch(self):
        env = self._env(2000)
        off = self._client(ship_gzip_min_bytes=0)
        body, headers = off._encode(env)
        self.assertEqual(headers, {})
        self.assertEqual(json.loads(body), env)
        # and it really is a threshold, not a boolean
        low = self._client(ship_gzip_min_bytes=1)
        self.assertEqual(low._encode({"v": 1, "pings": {}})[1],
                         {"Content-Encoding": "gzip"})

    def test_the_env_var_reaches_the_field(self):
        old = os.environ.pop("WISP_SHIP_GZIP_MIN_BYTES", None)
        try:
            self.assertEqual(Config().ship_gzip_min_bytes, 4096)
            os.environ["WISP_SHIP_GZIP_MIN_BYTES"] = "0"
            self.assertEqual(Config().ship_gzip_min_bytes, 0)
        finally:
            if old is None:
                os.environ.pop("WISP_SHIP_GZIP_MIN_BYTES", None)
            else:
                os.environ["WISP_SHIP_GZIP_MIN_BYTES"] = old

    def test_a_failing_compressor_ships_uncompressed_and_never_raises(self):
        # The probe loop never dies on one bad cycle, and central accepts both
        # shapes — so the plain bytes are a complete answer, not a degraded one.
        client = self._client()
        env = self._env(2000)
        with mock.patch("wisp.runtime.central_client.gzip.compress",
                        side_effect=RuntimeError("zlib exploded")), \
                self.assertLogs("wisp.edge.central", "WARNING") as logs:
            body, headers = client._encode(env)
        self.assertEqual(headers, {})
        self.assertEqual(json.loads(body), env)
        self.assertIn("uncompressed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
