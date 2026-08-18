"""The HTTP surface accepts a gzipped edge report — and still accepts a plain one.

Central must ALWAYS take both. That is the whole deployment argument: no
handshake and no version dance, so central ships whenever and edges start
saving as they roll. Both directions are pinned here, plus the two refusals
(corrupt, and a body that inflates past the ceiling) which must land on the
same 400 a malformed JSON body has always landed on.
"""

import gzip
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth, server as central_server
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.runtime.central_client import build_central_client


class _RecordingNotifier:
    """A double, so nothing here can reach a real network."""

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="default", **kw):
        self.sent.append((title, body))
        return True


class GzipReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.9", "device_type": "OLT",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.notifier = _RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ wire

    def _raw_post(self, path, body: bytes, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", "Authorization": "Bearer tok"}
        h.update(headers or {})
        conn.request("POST", path, body=body, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _report(self, n_ports=40):
        return {"v": 1, "org_id": "ispA", "node_id": "probe1", "mode": "full",
                "ts": "2026-08-18T09:15:03+00:00",
                "pings": {"10.0.0.9": {"loss_pct": 0.0, "latency_ms": 4.2}},
                "ports": {str(self.olt): [
                    {"if_index": 100 + i, "if_name": f"EPON0/1:{i}",
                     "if_alias": f"cust drop {i}", "admin_status": "up",
                     "oper_status": "up", "speed_bps": 1000000000,
                     "in_octets": 10 ** 10 + i, "out_octets": 10 ** 9 + i}
                    for i in range(n_ports)]}}

    def _stored_ports(self):
        return self.store.list_switch_ports("ispA", self.olt)

    # ------------------------------------------------------------------ both

    def test_a_gzipped_report_is_accepted_and_actually_lands(self):
        env = self._report()
        body = gzip.compress(json.dumps(env).encode(), 1, mtime=0)
        status, reply = self._raw_post("/report", body,
                                       {"Content-Encoding": "gzip"})
        self.assertEqual(status, 200, reply)
        # 200 alone would also be what a silently-empty body earns, so prove
        # the rows actually crossed.
        rows = self._stored_ports()
        self.assertEqual(len(rows), 40)
        self.assertEqual({r["if_name"] for r in rows if r["if_index"] == 100},
                         {"EPON0/1:0"})

    def test_an_uncompressed_report_still_works(self):
        # The half that must never regress: old edges keep reporting while the
        # fleet rolls, so central ships before any of them move.
        status, reply = self._raw_post(
            "/report", json.dumps(self._report()).encode())
        self.assertEqual(status, 200, reply)
        self.assertEqual(len(self._stored_ports()), 40)

    def test_a_gzipped_body_with_no_encoding_header_still_parses(self):
        # The magic outranks the header: a middlebox that strips the header
        # must not cost the fleet its ingest.
        body = gzip.compress(json.dumps(self._report()).encode(), 1, mtime=0)
        status, reply = self._raw_post("/report", body)
        self.assertEqual(status, 200, reply)
        self.assertEqual(len(self._stored_ports()), 40)

    def test_a_plain_body_labelled_gzip_still_parses(self):
        # ...and the other direction: a middlebox that inflates the body and
        # leaves its own header behind.
        status, reply = self._raw_post(
            "/report", json.dumps(self._report()).encode(),
            {"Content-Encoding": "gzip"})
        self.assertEqual(status, 200, reply)
        self.assertEqual(len(self._stored_ports()), 40)

    # -------------------------------------------------------------- refusals

    def test_a_corrupt_gzip_body_is_a_400_and_never_a_500(self):
        status, reply = self._raw_post("/report", b"\x1f\x8b" + b"garbage" * 40,
                                       {"Content-Encoding": "gzip"})
        self.assertEqual(status, 400, reply)
        self.assertIn("JSON", reply.get("error", ""))
        self.assertEqual(self._stored_ports(), [])

    def test_a_truncated_gzip_body_is_a_400(self):
        whole = gzip.compress(json.dumps(self._report()).encode(), 1, mtime=0)
        status, reply = self._raw_post("/report", whole[:-8],
                                       {"Content-Encoding": "gzip"})
        self.assertEqual(status, 400, reply)
        self.assertEqual(self._stored_ports(), [])

    def test_the_DECOMPRESSED_bound_bites_on_a_body_Content_Length_waves_through(self):
        # A gzip bomb sails past the 16 MB Content-Length guard: this one is a
        # few KB on the wire and 17 MB in RAM. Without a second ceiling the
        # existing guard would have stopped meaning anything.
        bomb = gzip.compress(b"\0" * (17 * 1024 * 1024), 1, mtime=0)
        self.assertLess(len(bomb), 128 * 1024)
        status, reply = self._raw_post("/report", bomb,
                                       {"Content-Encoding": "gzip"})
        self.assertEqual(status, 400, reply)
        self.assertEqual(self._stored_ports(), [])

    # (The uncompressed 16 MB Content-Length guard is deliberately not driven
    # over a socket: central refuses it without draining the body, so the
    # client gets a broken pipe rather than the 400. That path is unchanged by
    # gzip; what gzip changed is the ceiling above, which is why that one is
    # pinned here.)

    # ----------------------------------------------------------- end to end

    def test_the_real_edge_client_gzips_the_wire_and_the_report_lands(self):
        edge = build_central_client(Config(
            central_url=f"http://127.0.0.1:{self.port}", central_token="tok",
            org_id="ispA", node_id="probe1"))
        self.addCleanup(edge.close)

        seen = []
        real = central_server.decode_body

        def recording(raw, encoding, *a, **kw):
            seen.append((raw[:2], encoding))
            return real(raw, encoding, *a, **kw)

        env = self._report(n_ports=400)
        with mock.patch.object(central_server, "decode_body", recording):
            reply = edge.report(env["pings"], env["ts"], ports=env["ports"])

        self.assertEqual(reply.get("ok"), True)
        self.assertEqual(seen, [(b"\x1f\x8b", "gzip")],
                         "the bytes on the wire were not gzip")
        self.assertEqual(len(self._stored_ports()), 400)

    def test_a_small_report_from_the_real_edge_client_goes_plain(self):
        edge = build_central_client(Config(
            central_url=f"http://127.0.0.1:{self.port}", central_token="tok",
            org_id="ispA", node_id="probe1"))
        self.addCleanup(edge.close)

        seen = []
        real = central_server.decode_body

        def recording(raw, encoding, *a, **kw):
            seen.append((raw[:2], encoding))
            return real(raw, encoding, *a, **kw)

        with mock.patch.object(central_server, "decode_body", recording):
            reply = edge.report({"10.0.0.9": {"loss_pct": 0.0, "latency_ms": 4.2}},
                                "2026-08-18T09:15:03+00:00")
        self.assertEqual(reply.get("ok"), True)
        self.assertEqual(seen, [(b'{"', "")])


if __name__ == "__main__":
    unittest.main()
