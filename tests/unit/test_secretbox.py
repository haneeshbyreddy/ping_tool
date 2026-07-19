import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import secretbox
from wisp.central.secretbox import DecryptError, SecretBox


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.box = SecretBox(b"k" * 32)

    def test_roundtrip_variety(self):
        for pt in ["", "admin", "sravani@1987", "hüntér2 🔐", "x" * 500,
                   "line1\nline2\t\x00end"]:
            self.assertEqual(self.box.decrypt(self.box.encrypt(pt)), pt)

    def test_nonce_is_random(self):
        # same plaintext, different ciphertext each time (no nonce reuse)
        a = self.box.encrypt("same")
        b = self.box.encrypt("same")
        self.assertNotEqual(a, b)
        self.assertEqual(self.box.decrypt(a), self.box.decrypt(b))

    def test_wrong_key_fails(self):
        token = self.box.encrypt("secret")
        with self.assertRaises(DecryptError):
            SecretBox(b"j" * 32).decrypt(token)

    def test_tamper_detected(self):
        import base64
        raw = bytearray(base64.b64decode(self.box.encrypt("secret")))
        raw[-1] ^= 0x01  # flip a tag bit
        with self.assertRaises(DecryptError):
            self.box.decrypt(base64.b64encode(bytes(raw)).decode())

    def test_ciphertext_body_tamper_detected(self):
        import base64
        raw = bytearray(base64.b64decode(self.box.encrypt("secretpw")))
        raw[20] ^= 0x01  # flip a ciphertext bit (past version+nonce)
        with self.assertRaises(DecryptError):
            self.box.decrypt(base64.b64encode(bytes(raw)).decode())

    def test_garbage_token(self):
        for junk in ["", "not base64!!", "AAAA"]:
            with self.assertRaises(DecryptError):
                self.box.decrypt(junk)

    def test_short_key_rejected(self):
        with self.assertRaises(ValueError):
            SecretBox(b"short")


class KeyLoadTest(unittest.TestCase):
    def test_env_base64_key(self):
        import base64
        raw = bytes(range(32))
        key = secretbox.load_key(Path("/nonexistent/x"),
                                 base64.b64encode(raw).decode())
        self.assertEqual(key, raw)

    def test_env_passphrase_stretched(self):
        key = secretbox.load_key(Path("/nonexistent/x"), "a short passphrase")
        self.assertEqual(len(key), 32)
        # deterministic: same passphrase -> same key -> round-trips across boxes
        key2 = secretbox.load_key(Path("/nonexistent/y"), "a short passphrase")
        self.assertEqual(key, key2)
        token = SecretBox(key).encrypt("pw")
        self.assertEqual(SecretBox(key2).decrypt(token), "pw")

    def test_file_generated_and_reused(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "secret.key"
            k1 = secretbox.load_key(kf, "")
            self.assertTrue(kf.exists())
            self.assertEqual(len(k1), 32)
            # 0600 perms
            self.assertEqual(kf.stat().st_mode & 0o777, 0o600)
            # a second load reuses the same key (secrets survive restart)
            k2 = secretbox.load_key(kf, "")
            self.assertEqual(k1, k2)

    def test_file_key_roundtrips_after_reload(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "secret.key"
            token = SecretBox(secretbox.load_key(kf, "")).encrypt("sravani@1987")
            # simulate a restart: rebuild the box from the persisted key file
            reloaded = SecretBox(secretbox.load_key(kf, ""))
            self.assertEqual(reloaded.decrypt(token), "sravani@1987")


if __name__ == "__main__":
    unittest.main()
