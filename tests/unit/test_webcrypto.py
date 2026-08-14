"""AES-CBC and the CryptoJS envelope, in stdlib, for panels that encrypt the
sign-in in the browser.

Central is pure stdlib and has no AES anywhere else, so this is the one place a
cipher is implemented rather than called. It is therefore pinned to the
PUBLISHED vectors (FIPS-197 and NIST SP 800-38A) rather than to itself: a
round-trip test would pass just as happily against a wrong cipher, and the panel
this exists for would answer 500 with nothing to say why.

It protects nothing of ours -- TLS carries the request and the passphrase is the
panel's own one-time nonce -- so it is written for clarity, not for constant
time.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))

from wisp.central import webcrypto  # noqa: E402


class BlockCipherTest(unittest.TestCase):
    """FIPS-197 appendix C known-answer vectors."""

    def _block(self, key_hex, plain_hex):
        words, rounds = webcrypto._expand_key(bytes.fromhex(key_hex))
        return webcrypto._encrypt_block(
            bytes.fromhex(plain_hex), words, rounds).hex()

    def test_aes_128(self):
        self.assertEqual(
            self._block("000102030405060708090a0b0c0d0e0f",
                        "00112233445566778899aabbccddeeff"),
            "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_aes_192(self):
        self.assertEqual(
            self._block("000102030405060708090a0b0c0d0e0f1011121314151617",
                        "00112233445566778899aabbccddeeff"),
            "dda97ca4864cdfe06eaf70a0ec0d7191")

    def test_aes_256(self):
        self.assertEqual(
            self._block("000102030405060708090a0b0c0d0e0f"
                        "101112131415161718191a1b1c1d1e1f",
                        "00112233445566778899aabbccddeeff"),
            "8ea2b7ca516745bfeafc49904b496089")


class CbcModeTest(unittest.TestCase):
    """NIST SP 800-38A F.2.5, CBC-AES256.Encrypt."""

    KEY = bytes.fromhex("603deb1015ca71be2b73aef0857d7781"
                        "1f352c073b6108d72d9810a30914dff4")
    IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")

    def test_the_published_cbc_vector(self):
        plain = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52ef"
            "f69f2445df4f9b17ad2b417be66c3710")
        out = webcrypto.aes_cbc_encrypt(self.KEY, self.IV, plain)
        self.assertEqual(out[:32].hex(),
                         "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
                         "9cfc4e967edb808d679f777bc6702c7d")
        self.assertEqual(out[32:64].hex(),
                         "39f23369a9d9bacfa530e26304231461"
                         "b2eb05e2c39be9fcda6c19078c6a9d1b")

    def test_a_whole_block_of_padding_is_added_when_it_already_fits(self):
        out = webcrypto.aes_cbc_encrypt(self.KEY, self.IV, b"0123456789abcdef")
        self.assertEqual(len(out), 32)

    def test_a_short_key_or_iv_is_refused(self):
        with self.assertRaises(ValueError):
            webcrypto.aes_cbc_encrypt(b"short", self.IV, b"x")
        with self.assertRaises(ValueError):
            webcrypto.aes_cbc_encrypt(self.KEY, b"short", b"x")


class EnvelopeTest(unittest.TestCase):
    """The shape Encryption.js expects back, which is what the panel decrypts."""

    def test_the_envelope_carries_what_the_panel_needs_to_decrypt(self):
        out = webcrypto.cryptojs_encrypt("secret", "nonce")
        env = json.loads(base64.b64decode(out))
        self.assertEqual(sorted(env), ["ciphertext", "iterations", "iv", "salt"])
        self.assertEqual(env["iterations"], webcrypto.CRYPTOJS_ITERATIONS)
        self.assertEqual(len(bytes.fromhex(env["iv"])), 16)
        self.assertEqual(len(bytes.fromhex(env["salt"])),
                         webcrypto.CRYPTOJS_SALT_LEN)

    def test_the_derivation_is_PINNED(self):
        # A golden envelope for a fixed iv/salt. The iteration count, the SHA-512
        # PBKDF2, the 32-byte key and the PKCS7 padding all feed this string, so
        # any drift in the chain shows up here rather than as a 500 from a panel.
        out = webcrypto.cryptojs_encrypt(
            "MANIspurini", "noncenoncenonce1",
            iv=bytes(range(16)), salt=bytes(range(256)))
        env = json.loads(base64.b64decode(out))
        self.assertEqual(env["ciphertext"], "yOZY70QePQc2CzXI/jiS3g==")

    def test_two_calls_differ_because_the_iv_and_salt_are_fresh(self):
        a = webcrypto.cryptojs_encrypt("secret", "nonce")
        b = webcrypto.cryptojs_encrypt("secret", "nonce")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
