"""RFC 6238 TOTP + single-use recovery codes — pure, stdlib only.

The second factor for owner/superadmin logins (2026-07-23). Kept deliberately
separate from ``auth.py``: this module is pure math (no store, no I/O), the same
shape as ``ponfault.py`` vs ``ponalert.py`` — the store/API glue lives in
``store_users.py`` / ``api/users.py`` / the login handler.

TOTP is HMAC-SHA1 over a 30s counter, 6 digits (what Google Authenticator, Authy,
etc. generate by default — do not change these without breaking every enrolled
device). Verification allows ±1 step for clock skew, and takes an ``after_step``
so the caller can enforce single-use: a code is accepted only if its step is
strictly newer than the last one that logged the account in (replay guard).

The shared TOTP secret is sensitive — a leaked one lets an attacker mint codes —
so it is stored secretbox-encrypted (like device passwords), NEVER in the clear.
Recovery codes are high-entropy random, so a single SHA-256 of each is enough at
rest (nothing to brute-force); only the hashes are stored, the plaintext is shown
to the user exactly once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_STEP = 30           # seconds per code
_DIGITS = 6
_WINDOW = 1          # accept the code one step either side (clock skew)
_SECRET_BYTES = 20   # 160-bit shared secret (base32 → 32 chars)
_RECOVERY_COUNT = 10
_RECOVERY_BYTES = 7  # ~56 bits per code


def new_secret() -> str:
    """A fresh base32 TOTP secret (no padding), ready for an otpauth:// URI."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    s = (secret_b32 or "").strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8), casefold=True)


def _hotp(key: bytes, counter: int, digits: int = _DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    bincode = (((mac[off] & 0x7F) << 24) | (mac[off + 1] << 16)
               | (mac[off + 2] << 8) | mac[off + 3])
    return str(bincode % (10 ** digits)).zfill(digits)


def verify(secret_b32: str, code: str, *, now: float | None = None,
           window: int = _WINDOW, after_step: int | None = None) -> int | None:
    """Return the matched counter step if ``code`` is a valid TOTP for the secret
    AND its step is strictly newer than ``after_step`` (the replay guard), else
    None. The caller stores the returned step as the new ``after_step``."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != _DIGITS or not code.isdigit():
        return None
    try:
        key = _decode_secret(secret_b32)
    except Exception:
        return None
    counter = int((time.time() if now is None else now) // _STEP)
    floor = -1 if after_step is None else after_step
    for w in range(-window, window + 1):
        c = counter + w
        if c < 0 or c <= floor:
            continue
        if hmac.compare_digest(_hotp(key, c), code):
            return c
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """The otpauth:// URI an authenticator app reads from the enrollment QR."""
    label = quote(f"{issuer}:{account}")
    query = (f"secret={secret_b32}&issuer={quote(issuer)}"
             f"&algorithm=SHA1&digits={_DIGITS}&period={_STEP}")
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes(n: int = _RECOVERY_COUNT) -> list[str]:
    """``n`` human-typeable single-use codes (``abcde-fghij``). Shown once."""
    out = []
    for _ in range(n):
        raw = base64.b32encode(secrets.token_bytes(_RECOVERY_BYTES)
                               ).decode("ascii").rstrip("=").lower()[:10]
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def recovery_hash(code: str) -> str:
    """Separator/case-insensitive SHA-256 of a recovery code — what's stored."""
    norm = (code or "").strip().lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
