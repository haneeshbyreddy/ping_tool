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

SESSION_COOKIE = "wisp_central_session"
MIN_PASSWORD_LEN = 8
ROLES = ("owner", "operator", "tech")

class AuthError(ValueError):
    pass

_secret_lock = threading.Lock()
_secret_cache: dict[str, bytes] = {}

def session_secret_path(cfg: Config = CONFIG) -> Path:
    return cfg.central_db.parent / "central_session_secret"

def get_session_secret(cfg: Config = CONFIG) -> bytes:
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
            except FileExistsError:
                secret = path.read_bytes()
        _secret_cache[key] = secret
        return secret

def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def _validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return password

def create_user(store, org_id: str | None, username: str, password: str,
                role: str = "operator") -> int:
    username = (username or "").strip()
    if not username:
        raise AuthError("username required")
    if org_id is not None and role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    _validate_password(password)
    if store.get_user_by_username(username):
        raise AuthError(f"username {username!r} already exists")
    salt = secrets.token_hex(16)
    return store.add_user(org_id, username, hash_pw(password, salt), salt, role)

def set_password(store, user_id: int, password: str) -> None:
    _validate_password(password)
    salt = secrets.token_hex(16)
    store.set_user_password(user_id, hash_pw(password, salt), salt)

def verify_login(store, username: str, password: str) -> dict | None:
    user = store.get_user_by_username((username or "").strip())
    if not user or not user["is_active"]:
        return None
    expected = user["pw_hash"]
    got = hash_pw(password or "", user["pw_salt"])
    return user if hmac.compare_digest(expected, got) else None

def _sign(secret: bytes, msg: str) -> str:
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()

def session_ttl_s(cfg: Config = CONFIG, *, remember: bool = False) -> int:
    """How long a freshly issued session lives. A trusted ('remember this device')
    login gets the long window; everyone else the short default."""
    if remember:
        return cfg.session_remember_days * 86400
    return cfg.session_timeout_h * 3600

def issue_session(user_id: int, cfg: Config = CONFIG, *, remember: bool = False,
                  now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    # The TTL is signed into the token so a trusted session verifies against its
    # own lifetime — verify_session no longer needs to be told the timeout.
    ttl = str(session_ttl_s(cfg, remember=remember))
    msg = f"{user_id}.{issued}.{ttl}"
    return f"{msg}.{_sign(get_session_secret(cfg), msg)}"

def verify_session(token: str | None, *, cfg: Config = CONFIG,
                   now: float | None = None) -> int | None:
    if not token or token.count(".") != 3:
        return None
    user_part, issued, ttl, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(get_session_secret(cfg), f"{user_part}.{issued}.{ttl}")):
        return None
    try:
        user_id = int(user_part)
        issued_i = int(issued)
        ttl_i = int(ttl)
    except ValueError:
        return None
    elapsed = (time.time() if now is None else now) - issued_i
    return user_id if 0 <= elapsed <= ttl_i else None

def session_cookie(token: str, *, max_age: int) -> str:
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")

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

def resolve_session(store, token: str | None, *, cfg: Config = CONFIG) -> dict | None:
    user_id = verify_session(token, cfg=cfg)
    if user_id is None:
        return None
    user = store.get_user(user_id)
    if not user or not user["is_active"]:
        return None
    user = dict(user)
    user.pop("pw_hash", None)
    user.pop("pw_salt", None)
    user["is_superadmin"] = user["org_id"] is None
    return user

class LoginThrottle:

    def __init__(self, lock_after: int = 5, base_delay: float = 2.0,
                 cap: float = 300.0) -> None:
        self.lock_after = lock_after
        self.base_delay = base_delay
        self.cap = cap
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, ip: str, *, now: float | None = None) -> float:
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
