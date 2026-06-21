"""Phase 8.2 — dashboard access control: shared-PIN gate + signed-cookie sessions.

Pure stdlib (`hmac`/`hashlib`/`secrets`), consistent with the "no deps, runs on a
bare laptop" rule. No user accounts — one shared PIN gates the whole UI; worker
*roles* are a routing concept, not an authentication one (plan §8.2).

Split of concerns, mirroring the config bootstrap-vs-operational split:
  * the **session secret** is bootstrap class — a file under `data/` (0600), never
    the DB (it's needed to verify the cookie that authorizes DB-changing requests);
  * the **PIN** (salted SHA-256) and the **session timeout** live in `settings`,
    so the operator can change them from the browser.

Security posture (documented, accepted for the office-LAN target): plain HTTP, secret
+ PIN hash protected by filesystem perms, not TLS/encryption. See plan §8.2.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

from wisp.config import CONFIG, Config

SESSION_COOKIE = "wisp_session"
_PIN_HASH_KEY = "pin_hash"
_PIN_SALT_KEY = "pin_salt"
MIN_PIN_LEN = 4


class PinError(ValueError):
    """A bad PIN value (too short / non-numeric), surfaced to the UI as a 422."""


# --- session secret (bootstrap: file under data/, cached per path) ----------
_secret_lock = threading.Lock()
_secret_cache: dict[str, bytes] = {}


def session_secret_path(cfg: Config = CONFIG) -> Path:
    return cfg.db_path.parent / "session_secret"


def get_session_secret(cfg: Config = CONFIG) -> bytes:
    """Read (or generate-once) the 32-byte HMAC key used to sign session cookies.
    Stored 0600 next to the DB; cached per path so repeated requests don't re-read."""
    path = session_secret_path(cfg)
    key = str(path)
    with _secret_lock:
        cached = _secret_cache.get(key)
        if cached is not None:
            return cached
        if path.exists():
            secret = path.read_bytes()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_bytes(32)
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, secret)
                finally:
                    os.close(fd)
            except FileExistsError:  # raced with another process — use theirs
                secret = path.read_bytes()
        _secret_cache[key] = secret
        return secret


# --- PIN (salted SHA-256, stored in settings) -------------------------------
def hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()


def pin_is_set(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?", (_PIN_HASH_KEY,)).fetchone()
    except Exception:
        return False
    return row is not None


def set_pin(conn, pin: str, by: str = "operator") -> None:
    """Validate and store a salted PIN hash. Raises PinError on a weak PIN. Caller
    owns the transaction boundary (this commits via the connection)."""
    pin = (pin or "").strip()
    if not (pin.isdigit() and len(pin) >= MIN_PIN_LEN):
        raise PinError(f"PIN must be at least {MIN_PIN_LEN} digits")
    salt = secrets.token_hex(16)
    _put(conn, _PIN_SALT_KEY, salt, by)
    _put(conn, _PIN_HASH_KEY, hash_pin(pin, salt), by)
    conn.commit()


def verify_pin(conn, pin: str) -> bool:
    rows = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?)",
        (_PIN_HASH_KEY, _PIN_SALT_KEY))}
    stored = rows.get(_PIN_HASH_KEY)
    salt = rows.get(_PIN_SALT_KEY)
    if not stored or salt is None:
        return False
    return hmac.compare_digest(stored, hash_pin((pin or "").strip(), salt))


def _put(conn, key: str, value: str, by: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_by) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_by=excluded.updated_by, updated_at=datetime('now')",
        (key, value, by),
    )


# --- session tokens (issued_at + HMAC, verified on every request) -----------
def _sign(secret: bytes, msg: str) -> str:
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(cfg: Config = CONFIG, *, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    return f"{issued}.{_sign(get_session_secret(cfg), issued)}"


def verify_session(token: str | None, *, cfg: Config = CONFIG, timeout_h: int,
                   now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    issued, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(get_session_secret(cfg), issued)):
        return False
    try:
        issued_i = int(issued)
    except ValueError:
        return False
    elapsed = (time.time() if now is None else now) - issued_i
    return 0 <= elapsed <= timeout_h * 3600


# --- cookies ----------------------------------------------------------------
def session_cookie(token: str, *, max_age: int) -> str:
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax;"
            f" Max-Age={max_age}")


def clear_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def cookie_token(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


# --- brute-force throttle (a 4-digit PIN is only 10^4 combinations) ----------
class LoginThrottle:
    """In-memory per-IP failed-login limiter with exponential backoff. A single-
    process ThreadingHTTPServer shares one instance, so a dict + lock suffices."""

    def __init__(self, lock_after: int = 5, base_delay: float = 2.0,
                 cap: float = 300.0) -> None:
        self.lock_after = lock_after
        self.base_delay = base_delay
        self.cap = cap
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, ip: str, *, now: float | None = None) -> float:
        """Seconds the client must wait before another attempt (0.0 if allowed)."""
        t = time.time() if now is None else now
        with self._lock:
            n, last = self._fails.get(ip, (0, 0.0))
        if n < self.lock_after:
            return 0.0
        delay = min(self.cap, self.base_delay * (2 ** (n - self.lock_after)))
        return max(0.0, (last + delay) - t)

    def fail(self, ip: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else now
        with self._lock:
            n, _ = self._fails.get(ip, (0, 0.0))
            self._fails[ip] = (n + 1, t)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


# Module singleton used by the HTTP layer.
THROTTLE = LoginThrottle()
