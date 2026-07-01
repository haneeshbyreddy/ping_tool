"""Tests for the internal mTLS CA (CLAUDE.md item 6, central/pki.py). Real `openssl`
subprocess calls against a tmp dir (no network, no server) — skipped if `openssl` isn't
on PATH, mirroring how the rest of the suite skips real-network/real-hardware paths.

Run:  python -m unittest discover -s tests   (from the project root)
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import pki

_HAS_OPENSSL = shutil.which("openssl") is not None


@unittest.skipUnless(_HAS_OPENSSL, "openssl not on PATH")
class EnsureCaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pki_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_ca_once(self):
        key, cert = pki.ensure_ca(self.pki_dir)
        self.assertTrue(key.exists())
        self.assertTrue(cert.exists())
        self.assertEqual(oct(key.stat().st_mode)[-3:], "600")

    def test_idempotent(self):
        key1, cert1 = pki.ensure_ca(self.pki_dir)
        before = cert1.read_bytes()
        key2, cert2 = pki.ensure_ca(self.pki_dir)
        self.assertEqual(key1, key2)
        self.assertEqual(before, cert2.read_bytes())  # not regenerated


@unittest.skipUnless(_HAS_OPENSSL, "openssl not on PATH")
class IssueCertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pki_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _subject_cn(self, cert_path: Path) -> str:
        import subprocess
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-subject"],
            capture_output=True, text=True, check=True).stdout
        return out.strip().split("CN")[-1].lstrip(" =")

    def test_issues_edge_cert_with_expected_cn(self):
        cn = pki.edge_common_name("ispA", "edge-1")
        key, cert = self.pki_dir / "edge-1.key", self.pki_dir / "edge-1.crt"
        pki.issue_cert(self.pki_dir, cn, key, cert)
        self.assertTrue(key.exists())
        self.assertTrue(cert.exists())
        self.assertEqual(self._subject_cn(cert), "ispA:edge-1")

    def test_no_csr_left_behind(self):
        key, cert = self.pki_dir / "edge-1.key", self.pki_dir / "edge-1.crt"
        pki.issue_cert(self.pki_dir, "ispA:edge-1", key, cert)
        self.assertFalse(key.with_suffix(".csr").exists())

    def test_reuses_same_ca_across_issuances(self):
        key1, cert1 = self.pki_dir / "edge-1.key", self.pki_dir / "edge-1.crt"
        key2, cert2 = self.pki_dir / "edge-2.key", self.pki_dir / "edge-2.crt"
        pki.issue_cert(self.pki_dir, "ispA:edge-1", key1, cert1)
        pki.issue_cert(self.pki_dir, "ispA:edge-2", key2, cert2)
        # both issued certs verify against the SAME ca.crt
        import subprocess
        for cert in (cert1, cert2):
            subprocess.run(
                ["openssl", "verify", "-CAfile", str(self.pki_dir / "ca.crt"), str(cert)],
                capture_output=True, text=True, check=True)

    def test_server_cert_carries_san(self):
        key, cert = self.pki_dir / "central.key", self.pki_dir / "central.crt"
        pki.issue_cert(self.pki_dir, "central", key, cert, san=["DNS:localhost", "IP:127.0.0.1"])
        import subprocess
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("127.0.0.1", out)
        self.assertIn("localhost", out)


class PeerIdentityTest(unittest.TestCase):
    """Pure — no openssl needed, just decodes a getpeercert()-shaped dict."""

    def test_none_cert_is_no_identity(self):
        self.assertIsNone(pki.peer_identity(None))
        self.assertIsNone(pki.peer_identity({}))

    def test_decodes_tenant_node_cn(self):
        cert = {"subject": ((("commonName", "ispA:edge-1"),),)}
        self.assertEqual(pki.peer_identity(cert), ("ispA", "edge-1"))

    def test_non_conforming_cn_is_ignored(self):
        cert = {"subject": ((("commonName", "not-our-shape"),),)}
        self.assertIsNone(pki.peer_identity(cert))

    def test_missing_common_name(self):
        cert = {"subject": ((("organizationName", "Example Co"),),)}
        self.assertIsNone(pki.peer_identity(cert))


class OpensslMissingTest(unittest.TestCase):
    def test_clear_error_when_openssl_absent(self):
        real_which = shutil.which

        def fake_which(name):
            return None if name == "openssl" else real_which(name)

        import wisp.central.pki as pki_mod
        original = pki_mod.shutil.which
        pki_mod.shutil.which = fake_which
        try:
            with self.assertRaises(pki.PkiError):
                pki_mod._openssl(["version"])
        finally:
            pki_mod.shutil.which = original


if __name__ == "__main__":
    unittest.main()
