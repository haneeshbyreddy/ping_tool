"""Central dashboard auth (Phase 10 Part C) — per-org login accounts.

The edge's single shared PIN does not survive multi-tenancy: central serves many ISPs, so
each operator logs in with their OWN account, scoped to their org. This is the real authn
change the plan flags. It reuses the edge's proven, pure-stdlib crypto (salted SHA-256
passwords + HMAC-signed cookies, `server/auth.py`) — but the session now carries the user's
**identity** (so the server resolves their tenant scope + role per request, and a deactivated
account loses access immediately), and there are real user records instead of one PIN.

Account model (decision: central-provisioned). A SUPERADMIN (the platform operator, a `users`
row with `tenant_id IS NULL`) onboards each ISP and provisions its accounts; org users are
scoped to one `tenant_id` with a role (owner/operator/tech). No public signup. Provision the
first superadmin with the `central/admin.py` CLI; everything else can be done from the console.

Same documented posture as the edge: plain HTTP behind a TLS terminator, secrets protected by
filesystem perms. The ingest channel keeps its own bearer-token auth — this is humans only.
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

SESSION_COOKIE = "wisp_central_session"
MIN_PASSWORD_LEN = 8
ROLES = ("owner", "operator", "tech")


class AuthError(ValueError):
    """A bad credential/account value, surfaced to the UI as a 4xx."""


# --- session secret (a file next to the central DB, 0600; cached per path) ---
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
            except FileExistsError:  # raced with another process — use theirs
                secret = path.read_bytes()
        _secret_cache[key] = secret
        return secret


# --- passwords (salted SHA-256, same scheme as the edge PIN) -----------------
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return password


def create_user(store, tenant_id: str | None, username: str, password: str,
                role: str = "operator") -> int:
    """Provision an account. tenant_id None = superadmin. Raises AuthError on a weak
    password, a bad role, or a duplicate username."""
    username = (username or "").strip()
    if not username:
        raise AuthError("username required")
    if tenant_id is not None and role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    _validate_password(password)
    if store.get_user_by_username(username):
        raise AuthError(f"username {username!r} already exists")
    salt = secrets.token_hex(16)
    return store.add_user(tenant_id, username, hash_pw(password, salt), salt, role)


def set_password(store, user_id: int, password: str) -> None:
    _validate_password(password)
    salt = secrets.token_hex(16)
    store.set_user_password(user_id, hash_pw(password, salt), salt)


def verify_login(store, username: str, password: str) -> dict | None:
    """Return the active user dict on a correct password, else None (constant-time compare).
    A deactivated account never authenticates."""
    user = store.get_user_by_username((username or "").strip())
    if not user or not user["is_active"]:
        return None
    expected = user["pw_hash"]
    got = hash_pw(password or "", user["pw_salt"])
    return user if hmac.compare_digest(expected, got) else None


# --- sessions (identity-carrying: user_id + issued-at, HMAC-signed) ----------
def _sign(secret: bytes, msg: str) -> str:
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(user_id: int, cfg: Config = CONFIG, *, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    msg = f"{user_id}.{issued}"
    return f"{msg}.{_sign(get_session_secret(cfg), msg)}"


def verify_session(token: str | None, *, cfg: Config = CONFIG, timeout_h: int,
                   now: float | None = None) -> int | None:
    """Return the user_id of a valid, unexpired session, else None. The CALLER then looks
    the user up (so a role change / deactivation takes effect on the very next request)."""
    if not token or token.count(".") != 2:
        return None
    user_part, issued, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(get_session_secret(cfg), f"{user_part}.{issued}")):
        return None
    try:
        user_id = int(user_part)
        issued_i = int(issued)
    except ValueError:
        return None
    elapsed = (time.time() if now is None else now) - issued_i
    return user_id if 0 <= elapsed <= timeout_h * 3600 else None


# --- cookies (mirror the edge helpers) ---------------------------------------
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
    """token -> the active user dict (with a derived `is_superadmin`), or None. The single
    seam the server calls to authorize a dashboard request + learn its tenant scope."""
    user_id = verify_session(token, cfg=cfg, timeout_h=cfg.session_timeout_h)
    if user_id is None:
        return None
    user = store.get_user(user_id)
    if not user or not user["is_active"]:
        return None
    user = dict(user)
    user.pop("pw_hash", None)
    user.pop("pw_salt", None)
    user["is_superadmin"] = user["tenant_id"] is None
    return user
