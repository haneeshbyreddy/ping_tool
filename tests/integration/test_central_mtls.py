"""mTLS enrollment end-to-end over a REAL TLS socket (plan.md item 6) — mirrors
tests/integration/test_central_brain.py's ReportEndpointTest style (real
`make_server` + `http.client`, no mocking of the auth path), but with the listener
actually wrapped in TLS via `central/pki.py`'s issued certs. Skipped if `openssl`
isn't on PATH (needed to mint the throwaway CA/certs, not by the server itself —
`central/server.py` only ever uses stdlib `ssl` at request time).
"""
import http.client
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import pki
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config

_HAS_OPENSSL = shutil.which("openssl") is not None


@unittest.skipUnless(_HAS_OPENSSL, "openssl not on PATH")
class MtlsIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pki_dir = root / "pki"

        server_key, server_cert = self.pki_dir / "central.key", self.pki_dir / "central.crt"
        pki.issue_cert(self.pki_dir, "central", server_key, server_cert,
                       san=["IP:127.0.0.1", "DNS:localhost"])
        _, self.ca_cert = pki.ensure_ca(self.pki_dir)

        self.a_key, self.a_cert = self._issue("ispA", "edge-a1")
        self.b_key, self.b_cert = self._issue("ispB", "edge-b1")

        self.cfg = Config(
            central_db=root / "central.db",
            central_bind="127.0.0.1", central_port=0,
            central_token="",  # cert-only for most tests here — see the coexistence test
            central_tls_cert=str(server_cert), central_tls_key=str(server_key),
            central_client_ca=str(self.ca_cert),
            down_consecutive=3, recover_consecutive=2,
        )
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

    def _issue(self, tenant, node):
        cn = pki.edge_common_name(tenant, node)
        key, cert = self.pki_dir / f"{node}.key", self.pki_dir / f"{node}.crt"
        pki.issue_cert(self.pki_dir, cn, key, cert)
        return key, cert

    def _client_ctx(self, cert=None, key=None, *, verify=True):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if verify:
            ctx.load_verify_locations(cafile=str(self.ca_cert))
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if cert and key:
            ctx.load_cert_chain(str(cert), str(key))
        return ctx

    def _get(self, path, ctx, *, headers=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _post(self, path, body, ctx, *, headers=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        conn.request("POST", path, body=json.dumps(body), headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def test_listener_is_https(self):
        # a plain (non-TLS) client talking to a TLS port can't complete the exchange —
        # either the server resets the connection on the bad handshake, or (if it
        # somehow got a reply) that reply is NOT a plain-HTTP 200. Either way confirms
        # make_server actually wrapped the socket, not just accepted the TLS config.
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                reply = sock.recv(1024)
            except (ConnectionResetError, ConnectionAbortedError):
                return
        self.assertNotIn(b"HTTP/1.1 200", reply)

    def test_valid_client_cert_authenticates_ingest(self):
        ctx = self._client_ctx(self.a_cert, self.a_key)
        status, body = self._get("/edge/devices?tenant_id=ispA", ctx)
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"][0]["ip_address"], "10.0.0.1")

    def test_no_cert_and_no_token_is_unauthorized(self):
        ctx = self._client_ctx()  # no client cert presented
        status, _ = self._get("/edge/devices?tenant_id=ispA", ctx)
        self.assertEqual(status, 401)

    def test_cert_for_wrong_tenant_is_rejected(self):
        # b_cert is valid (signed by the real CA) but claims ispB, not ispA.
        ctx = self._client_ctx(self.b_cert, self.b_key)
        status, _ = self._get("/edge/devices?tenant_id=ispA", ctx)
        self.assertEqual(status, 401)

    def test_report_over_mtls_end_to_end(self):
        ctx = self._client_ctx(self.a_cert, self.a_key)
        body = {"v": 1, "tenant_id": "ispA", "node_id": "edge-a1",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        status, resp = self._post("/report", body, ctx)
        self.assertEqual(status, 200)

    def test_report_cert_node_mismatch_rejected(self):
        # a_cert is CN ispA:edge-a1 — claiming a DIFFERENT node in the same tenant
        # must not be accepted just because the tenant half matches.
        ctx = self._client_ctx(self.a_cert, self.a_key)
        body = {"v": 1, "tenant_id": "ispA", "node_id": "edge-a2",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        status, _ = self._post("/report", body, ctx)
        self.assertEqual(status, 401)

    def test_healthz_reachable_without_any_client_cert(self):
        # the dashboard/health surface must keep working over HTTPS for clients that
        # never present a cert at all (browsers, curl -k, monitoring probes).
        ctx = self._client_ctx()
        status, body = self._get("/healthz", ctx)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


@unittest.skipUnless(_HAS_OPENSSL, "openssl not on PATH")
class TokenAndMtlsCoexistTest(unittest.TestCase):
    """Bearer token and mTLS satisfy ingest auth independently — enabling one is not a
    hard cutover off the other (see central/server.py's `_ingest_ok`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pki_dir = root / "pki"
        server_key, server_cert = self.pki_dir / "central.key", self.pki_dir / "central.crt"
        pki.issue_cert(self.pki_dir, "central", server_key, server_cert,
                       san=["IP:127.0.0.1"])
        _, self.ca_cert = pki.ensure_ca(self.pki_dir)
        self.cfg = Config(
            central_db=root / "central.db",
            central_bind="127.0.0.1", central_port=0,
            central_token="tok",
            central_tls_cert=str(server_cert), central_tls_key=str(server_key),
            central_client_ca=str(self.ca_cert),
        )
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

    def test_bearer_token_alone_still_works_when_mtls_also_configured(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(self.ca_cert))
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        conn.request("GET", "/edge/devices?tenant_id=ispA",
                     headers={"Authorization": "Bearer tok"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        conn.close()


if __name__ == "__main__":
    unittest.main()
