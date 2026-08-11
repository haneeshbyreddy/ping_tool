from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_STEP = 30
_DIGITS = 6
_WINDOW = 1
_SECRET_BYTES = 20
_RECOVERY_COUNT = 10
_RECOVERY_BYTES = 7


def new_secret() -> str:
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
    label = quote(f"{issuer}:{account}")
    query = (f"secret={secret_b32}&issuer={quote(issuer)}"
             f"&algorithm=SHA1&digits={_DIGITS}&period={_STEP}")
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes(n: int = _RECOVERY_COUNT) -> list[str]:
    out = []
    for _ in range(n):
        raw = base64.b32encode(secrets.token_bytes(_RECOVERY_BYTES)
                               ).decode("ascii").rstrip("=").lower()[:10]
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def recovery_hash(code: str) -> str:
    norm = (code or "").strip().lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
