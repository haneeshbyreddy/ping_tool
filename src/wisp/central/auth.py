from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.central.auth")

SESSION_COOKIE = "wisp_central_session"
MIN_PASSWORD_LEN = 8
# Two roles since 2026-07-21: the org has owners (full write) and field workers
# (triage — ack/post-mortem — via api/common.can_triage, routed to the stripped
# worker view). The read-only `operator`/`tech` roles were removed; existing
# accounts holding one were migrated to `worker` (store._collapse_roles).
ROLES = ("owner", "worker")

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

# --- password hashing --------------------------------------------------------
# Passwords are stored scrypt-hashed (memory-hard: a leaked central.db can't be
# cracked at GPU speed the way single-round SHA-256 could). The stored string is
# SELF-DESCRIBING — "scrypt$N$r$p$salt_hex$digest_hex" — so the cost parameters
# can be tuned later without a schema flag day; the separate `pw_salt` column is
# legacy and unused by scrypt (new rows store ""). Accounts created before the
# migration hold a legacy salted-SHA-256 hash (64 hex chars, no "$") and are
# transparently UPGRADED to scrypt on their next correct login — see verify_login.
_SCRYPT_N = 2 ** 14          # 16384 — ~70 ms/hash on the prod box
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024   # 64 MiB ceiling (these params need ~16 MiB)

def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt((password or "").encode("utf-8"), salt=salt, n=n, r=r,
                          p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, 32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"

def _legacy_hash(password: str, salt: str) -> str:
    """The pre-migration scheme: one round of salted SHA-256. Kept ONLY to verify
    (and then upgrade) accounts that predate scrypt."""
    return hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()

# Old name, retained so any external caller keeps working; it is the legacy
# scheme by definition and must never be used to write a NEW password.
hash_pw = _legacy_hash

def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        tag, n, r, p, salt_hex, dig_hex = stored.split("$")
        if tag != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dig_hex)
        got = _scrypt(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)

def verify_password(password: str, pw_hash: str, pw_salt: str) -> tuple[bool, bool]:
    """Returns (ok, needs_upgrade). A correct login against a legacy SHA-256 hash
    asks to be re-hashed with scrypt; a scrypt hash never needs upgrading."""
    if pw_hash and pw_hash.startswith("scrypt$"):
        return _verify_scrypt(password, pw_hash), False
    ok = hmac.compare_digest(pw_hash or "", _legacy_hash(password, pw_salt or ""))
    return ok, ok

# A fixed dummy hash so a login for a NON-existent (or deactivated) account still
# spends a full scrypt verification — otherwise "unknown user" returns instantly
# while "known user, wrong password" costs ~70 ms, a timing oracle for which
# usernames exist. Computed once at import.
_DUMMY_HASH = hash_password(secrets.token_hex(16))

def _validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return password

def create_user(store, org_id: str | None, username: str, password: str,
                role: str = "worker") -> int:
    username = (username or "").strip()
    if not username:
        raise AuthError("username required")
    if org_id is None:
        # A SUPERADMIN's role column is meaningless (identity is org_id IS NULL)
        # — but it must not be left on the org default, which is 'worker' and
        # would make the SPA serve the platform admin the field-worker view.
        role = "owner"
    elif role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    _validate_password(password)
    if store.get_user_by_username(username):
        raise AuthError(f"username {username!r} already exists")
    # pw_salt is legacy — scrypt embeds its own salt in the hash string.
    return store.add_user(org_id, username, hash_password(password), "", role)

def set_password(store, user_id: int, password: str) -> None:
    _validate_password(password)
    store.set_user_password(user_id, hash_password(password), "")

def verify_login(store, username: str, password: str) -> dict | None:
    user = store.get_user_by_username((username or "").strip())
    if not user or not user["is_active"]:
        # Equalise timing with the found-user path so the response time doesn't
        # betray whether the account exists.
        verify_password(password or "", _DUMMY_HASH, "")
        return None
    ok, upgrade = verify_password(password or "", user["pw_hash"], user["pw_salt"])
    if not ok:
        return None
    if upgrade:
        # Re-hash the now-verified password with scrypt. Best-effort: a hiccup
        # here must never fail an otherwise-valid login (it retries next login).
        try:
            store.set_user_password(user["id"], hash_password(password or ""), "")
        except Exception:
            log.warning("scrypt upgrade failed for user %s", user["id"],
                        exc_info=True)
    return user

# --- sessions ----------------------------------------------------------------
# Stateless signed cookie. The token carries FIVE facts so verification needs no
# server state beyond the account's session_epoch:
#   user_id — who
#   hard    — absolute expiry (epoch seconds); the session dies here no matter what
#   seen    — last-activity time; slid forward on use (this is the IDLE clock)
#   idle    — idle window in seconds (0 = disabled, for "remember this device")
#   epoch   — the account's session generation. A newer login (or a logout) bumps
#             users.session_epoch, which instantly invalidates every older cookie
#             — that is what enforces ONE active session and makes logout real.

@dataclass(frozen=True)
class Session:
    user_id: int
    hard: int
    seen: int
    idle: int
    epoch: int

def _sign(secret: bytes, msg: str) -> str:
    return hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()

def _encode(s: Session, cfg: Config) -> str:
    msg = f"{s.user_id}.{s.hard}.{s.seen}.{s.idle}.{s.epoch}"
    return f"{msg}.{_sign(get_session_secret(cfg), msg)}"

def _decode(token: str | None, *, cfg: Config = CONFIG,
            now: float | None = None) -> Session | None:
    if not token or token.count(".") != 5:
        return None
    uid, hard, seen, idle, epoch, sig = token.split(".")
    msg = f"{uid}.{hard}.{seen}.{idle}.{epoch}"
    if not hmac.compare_digest(sig, _sign(get_session_secret(cfg), msg)):
        return None
    try:
        s = Session(int(uid), int(hard), int(seen), int(idle), int(epoch))
    except ValueError:
        return None
    t = time.time() if now is None else now
    if t > s.hard:                        # absolute cap
        return None
    if s.idle > 0 and t > s.seen + s.idle:   # idle cap
        return None
    return s

def issue_session(user_id: int, cfg: Config = CONFIG, *, remember: bool = False,
                  epoch: int = 0, now: float | None = None) -> str:
    t = int(time.time() if now is None else now)
    if remember:
        # Trusted device: long absolute life, and NO idle logout (idle=0).
        hard = t + cfg.session_remember_days * 86400
        idle = 0
    else:
        hard = t + cfg.session_timeout_h * 3600
        idle = cfg.session_idle_minutes * 60
    return _encode(Session(int(user_id), hard, t, idle, int(epoch)), cfg)

def verify_session(token: str | None, *, cfg: Config = CONFIG,
                   now: float | None = None) -> int | None:
    s = _decode(token, cfg=cfg, now=now)
    return s.user_id if s else None

def session_cookie_max_age(cfg: Config = CONFIG, *, remember: bool = False) -> int:
    """Browser retention hint for a freshly issued cookie. Set to the ABSOLUTE
    cap (not the idle window) so an active session's cookie survives across the
    idle window while the token's own seen+idle enforces idle expiry server-side."""
    if remember:
        return cfg.session_remember_days * 86400
    return cfg.session_timeout_h * 3600

def slide_session(token: str | None, cfg: Config = CONFIG, *,
                  now: float | None = None, min_interval: int = 60
                  ) -> tuple[str, int] | None:
    """Advance a still-valid, idle-limited session's activity clock. Returns
    (new_token, cookie_max_age) when it should be re-issued (idle enabled AND at
    least `min_interval` seconds since the last slide, and not past the absolute
    cap), else None. Remember-me sessions (idle==0) never slide."""
    s = _decode(token, cfg=cfg, now=now)
    if s is None or s.idle <= 0:
        return None
    t = int(time.time() if now is None else now)
    if t - s.seen < min_interval or t >= s.hard:
        return None
    fresh = Session(s.user_id, s.hard, t, s.idle, s.epoch)
    return _encode(fresh, cfg), max(1, s.hard - t)

def session_cookie(token: str, *, max_age: int, secure: bool = False) -> str:
    sec = " Secure;" if secure else ""
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly;{sec} "
            f"SameSite=Lax; Max-Age={max_age}")

def clear_cookie(*, secure: bool = False) -> str:
    sec = " Secure;" if secure else ""
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly;{sec} SameSite=Lax; Max-Age=0"

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
    s = _decode(token, cfg=cfg)
    if s is None:
        return None
    user = store.get_user(s.user_id)
    if not user or not user["is_active"]:
        return None
    # Single active session: a cookie whose epoch trails the account's current
    # session_epoch was superseded by a newer login (or killed by a logout).
    if int(user.get("session_epoch") or 0) != s.epoch:
        return None
    user = dict(user)
    # Strip secrets before the row travels anywhere: password material, the
    # session generation, and the TOTP secret/replay-cursor/recovery hashes.
    # totp_enabled STAYS — public_user surfaces it and it isn't sensitive.
    for k in ("pw_hash", "pw_salt", "session_epoch", "totp_secret",
              "totp_last_step", "totp_recovery"):
        user.pop(k, None)
    user["is_superadmin"] = user["org_id"] is None
    return user

class LoginThrottle:
    """Generic keyed exponential backoff. The login path feeds it BOTH the client
    IP and a ``user:<name>`` key, so a guess-storm is slowed whether it comes from
    one box against many accounts (IP key) or many boxes against one account (user
    key). Counters DECAY after ``window`` idle seconds, so a burst self-heals and
    the per-account key can't be weaponised to lock a victim out indefinitely —
    the worst it can do is impose the capped delay while the attack is sustained."""

    def __init__(self, lock_after: int = 5, base_delay: float = 2.0,
                 cap: float = 300.0, window: float = 900.0) -> None:
        self.lock_after = lock_after
        self.base_delay = base_delay
        self.cap = cap
        self.window = window
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _count(self, key: str, now: float) -> tuple[int, float]:
        n, last = self._fails.get(key, (0, 0.0))
        if n and (now - last) > self.window:
            return 0, 0.0
        return n, last

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        t = time.time() if now is None else now
        with self._lock:
            n, last = self._count(key, t)
        if n < self.lock_after:
            return 0.0
        delay = min(self.cap, self.base_delay * (2 ** (n - self.lock_after)))
        return max(0.0, (last + delay) - t)

    def fail(self, key: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else now
        with self._lock:
            n, _ = self._count(key, t)
            self._fails[key] = (n + 1, t)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
