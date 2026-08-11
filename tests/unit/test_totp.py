import base64
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))

from wisp.central import totp


class TotpTest(unittest.TestCase):
    RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

    def test_rfc6238_known_answers(self):
        self.assertEqual(totp.verify(self.RFC_SECRET, "287082", now=59, window=0), 1)
        self.assertEqual(
            totp.verify(self.RFC_SECRET, "081804", now=1111111109, window=0), 37037036)

    def test_wrong_and_malformed_codes_rejected(self):
        self.assertIsNone(totp.verify(self.RFC_SECRET, "000000", now=59))
        self.assertIsNone(totp.verify(self.RFC_SECRET, "28708", now=59))
        self.assertIsNone(totp.verify(self.RFC_SECRET, "abcdef", now=59))
        self.assertIsNone(totp.verify(self.RFC_SECRET, "", now=59))

    def test_skew_window(self):
        secret = totp.new_secret()
        key = totp._decode_secret(secret)
        counter = 1000
        now = counter * 30 + 5
        for c in (counter - 1, counter, counter + 1):
            self.assertIsNotNone(totp.verify(secret, totp._hotp(key, c), now=now))
        self.assertIsNone(totp.verify(secret, totp._hotp(key, counter + 2), now=now))

    def test_replay_guard(self):
        secret = totp.new_secret()
        key = totp._decode_secret(secret)
        now = 1000.0
        step = int(now // 30)
        code = totp._hotp(key, step)
        self.assertEqual(totp.verify(secret, code, now=now), step)
        self.assertIsNone(totp.verify(secret, code, now=now, after_step=step))
        self.assertIsNone(totp.verify(secret, code, now=now, after_step=step + 1))

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri("ABC234", "alice", "WISP Central")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=ABC234", uri)
        self.assertIn("issuer=WISP%20Central", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)

    def test_recovery_codes_are_unique_and_normalized(self):
        codes = totp.new_recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        self.assertRegex(codes[0], r"^[a-z2-7]{5}-[a-z2-7]{5}$")
        self.assertEqual(totp.recovery_hash("ABcde-FGHIJ"),
                         totp.recovery_hash("abcde fghij"))
        self.assertNotEqual(totp.recovery_hash("abcde-fghij"),
                            totp.recovery_hash("abcde-fghik"))


if __name__ == "__main__":
    unittest.main()
