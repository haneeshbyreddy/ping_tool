from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import struct
from pathlib import Path

log = logging.getLogger("wisp.secretbox")

_VERSION = b"\x01"
_NONCE_LEN = 16
_TAG_LEN = 32
_KEY_LEN = 32
_KEY_FILE_NAME = "secret.key"


class DecryptError(Exception):
    pass


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) < _KEY_LEN:
            raise ValueError("key must be at least 32 bytes")
        self._enc = hmac.new(key, b"wisp-secretbox-enc-v1", hashlib.sha256).digest()
        self._mac = hmac.new(key, b"wisp-secretbox-mac-v1", hashlib.sha256).digest()

    def _keystream(self, nonce: bytes, n: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < n:
            out.extend(hmac.new(self._enc, nonce + struct.pack(">I", counter),
                                hashlib.sha256).digest())
            counter += 1
        return bytes(out[:n])

    def encrypt(self, plaintext: str) -> str:
        data = plaintext.encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = bytes(a ^ b for a, b in zip(data, self._keystream(nonce, len(data))))
        tag = hmac.new(self._mac, _VERSION + nonce + ct, hashlib.sha256).digest()
        return base64.b64encode(_VERSION + nonce + ct + tag).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            raw = base64.b64decode(token.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as e:
            raise DecryptError("not valid base64") from e
        if len(raw) < 1 + _NONCE_LEN + _TAG_LEN or raw[:1] != _VERSION:
            raise DecryptError("bad header")
        nonce = raw[1:1 + _NONCE_LEN]
        ct = raw[1 + _NONCE_LEN:-_TAG_LEN]
        tag = raw[-_TAG_LEN:]
        expect = hmac.new(self._mac, _VERSION + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expect):
            raise DecryptError("authentication failed")
        pt = bytes(a ^ b for a, b in zip(ct, self._keystream(nonce, len(ct))))
        try:
            return pt.decode("utf-8")
        except UnicodeDecodeError as e:
            raise DecryptError("plaintext not utf-8") from e


def _coerce_key(material: str | bytes) -> bytes:
    if isinstance(material, str):
        material = material.strip().encode("utf-8")
    else:
        material = material.strip()
    try:
        decoded = base64.b64decode(material, validate=True)
        if len(decoded) >= _KEY_LEN:
            return decoded[:_KEY_LEN]
    except (binascii.Error, ValueError):
        pass
    return hashlib.sha256(material).digest()


def load_key(key_file: Path, env_value: str = "") -> bytes:
    if env_value and env_value.strip():
        return _coerce_key(env_value)
    try:
        existing = key_file.read_bytes()
    except OSError:
        existing = b""
    if existing.strip():
        return _coerce_key(existing)
    key = secrets.token_bytes(_KEY_LEN)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.b64encode(key))
    except OSError as e:
        log.warning("could not persist device-secret key at %s (%s); using an "
                    "ephemeral key — set WISP_SECRET_KEY so stored device "
                    "passwords survive a restart", key_file, e)
    return key


def from_config(cfg) -> SecretBox:
    key_file = Path(cfg.central_db).parent / _KEY_FILE_NAME
    return SecretBox(load_key(key_file, getattr(cfg, "secret_key", "")))
