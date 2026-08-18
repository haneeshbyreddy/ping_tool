from __future__ import annotations

import hmac
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import ssl
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import api, auth, billing, field, inventory, pki, secretbox, theme, totp
from wisp.central import rollup as central_rollup
from wisp.central.api.common import public_user
from wisp.central.auth import LoginThrottle
from wisp.central.engine import EngineRegistry
from wisp.central.liveping import LivePingHub
from wisp.central.proxy import ProxyHub
from wisp.central.store import CentralStore
from wisp.central.whatsapp_bot import WhatsAppBot
from wisp.egress.notifiers import build_notifier
from wisp.runtime.central_client import WIRE_V

log = logging.getLogger("wisp.central")

MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024

# gzip container (not raw deflate), and the two bytes every gzip member opens
# with. `_MAX_BODY` bounds what a client may SEND; the decompressed side needs
# its OWN ceiling or a few KB of zeros expands into gigabytes of RAM.
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_GZIP_MAGIC = b"\x1f\x8b"

_PROXY_EXACT = frozenset({
    "/api/proxy/session", "/api/proxy/sessions",
    "/api/proxy/close", "/api/proxy/audit",
})
_PROXY_SID_RE = re.compile(r"/api/proxy/([A-Za-z0-9_-]{16,})/")
_STATIC = Path(__file__).resolve().parent / "static"

# Routes a LOCKED org may still reach. Every billing and payment route is in
# here on purpose: gating the pay screen behind the paywall it exists to clear
# is the one unforgivable own-goal, and a locked owner must be able to see the
# amount, download the invoice and pay it without reading anything twice.
# `/payments/webhook` needs no entry (the gate only guards `/api/*`).
_BILLING_EXEMPT = {"/api/me", "/api/login", "/api/logout", "/healthz",
                   "/api/billing", "/api/billing/invoice", "/api/billing/pay",
                   "/api/billing/return", "/api/billing/plan"}

_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

_WORKER_GET = {
    # `/api/billing` is deliberately ABSENT: workers never see billing, the
    # customers-page rule (owner-only on the route layer AND in the nav).
    "/api/me", "/api/outages", "/api/events", "/api/summary",
    "/api/orgs", "/api/nodes", "/api/regions",
    "/api/inventory", "/api/inventory/routes", "/api/inventory/ports",
    "/api/inventory/link-ports", "/api/inventory/optics",
    "/api/inventory/onu-search", "/api/inventory/onu-places",
    "/api/inventory/subscriber",
    "/api/inventory/onu-coverage", "/api/inventory/snmp-status",
    "/api/inventory/rx-status", "/api/inventory/nvr-channels",
    "/api/inventory/nvr-snapshot",
    "/api/inventory/perf",
    "/api/inventory/perf/samples", "/api/pon/faults", "/api/pon/summary",
    "/api/incident/shape", "/api/analytics", "/api/analytics/trend",
    "/api/history/reliability",
    "/api/history/onu", "/api/history/replay", "/api/history/port",
    "/api/logs",
    "/api/issues", "/api/issues/pdf", "/api/issues/xlsx",
    "/api/field/shift",
    # Live ping. A deliberate grant, not a default: the user story IS the
    # worker ("a technician standing at the device"), and the route is safe by
    # what it cannot do — it writes nothing, pages nobody, and cannot reach the
    # FSM (see `central/liveping.py`). The data layer still applies underneath:
    # the handler resolves its target through `visible_device_ids`, so a worker
    # can only watch a device assigned to them.
    "/api/liveping",
}
_WORKER_POST = {
    "/api/outages/acknowledge", "/api/outages/accept", "/api/outages/postmortem",
    # Both halves of live ping, or the button 403s the person it exists for.
    "/api/liveping/start", "/api/liveping/stop",
    # No billing route belongs here. Workers never see billing, and a stale
    # entry in a permission allowlist silently grants the path if it is ever
    # reused (v1's "/api/billing/paid" sat here after its route was deleted).
    "/api/users/password", "/api/users/whatsapp",
    "/api/inventory/field-location", "/api/inventory/field-passive",
    "/api/inventory/field-onu", "/api/inventory/field-onu-name",
    "/api/field/shift",
}

class _VersionCache:
    def __init__(self, store: CentralStore, tick: float = 3.0) -> None:
        self.store = store
        self.tick = tick
        self._cond = threading.Condition()
        self._versions: dict = {}
        self._started = False

    def _compute(self) -> dict:
        vers = {org: self.store.data_version(org)
                for org in self.store.org_ids()}
        vers[None] = "|".join(f"{o}={v}" for o, v in sorted(vers.items()))
        return vers

    def ensure_started(self) -> None:
        with self._cond:
            if self._started:
                return
            self._started = True
            try:
                self._versions = self._compute()
            except Exception:
                self._versions = {}
        threading.Thread(target=self._loop, name="sse-versions",
                         daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(self.tick)
            try:
                vers = self._compute()
            except Exception:
                log.debug("sse version tick failed", exc_info=True)
                continue
            with self._cond:
                if vers != self._versions:
                    self._versions = vers
                    self._cond.notify_all()

    def wait_change(self, org: str | None, last: str | None,
                    timeout: float) -> str | None:
        with self._cond:
            cur = self._versions.get(org)
            if cur is not None and cur != last:
                return cur
            self._cond.wait(timeout)
            return self._versions.get(org)


_ENTRY_RE = re.compile(rb"/assets/index-([A-Za-z0-9_-]+)\.js")


class _BuildCache:
    """The served SPA's build id: the entry chunk's hash off index.html on disk.

    `npm run build` deploys the SPA with no restart, so this must re-read the
    file rather than latch at startup — cached by mtime behind a short TTL so
    every SSE iteration can afford the check. The id is the same string the
    browser can read off its own <script> tag, which is what lets the client
    compare without a second versioning scheme.
    """

    def __init__(self, path: Path, ttl: float = 5.0) -> None:
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._checked = 0.0
        self._mtime: float | None = None
        self._id: str | None = None

    def current(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            if now - self._checked < self.ttl:
                return self._id
            self._checked = now
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = self._id = None
                return None
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    m = _ENTRY_RE.search(self.path.read_bytes())
                except OSError:
                    m = None
                self._id = m.group(1).decode("ascii") if m else None
            return self._id


_build_cache = _BuildCache(_STATIC / "index.html")


def gunzip_bounded(raw: bytes, limit: int = _MAX_BODY) -> bytes | None:
    """Inflate a gzip body, refusing anything that expands past `limit`.

    Content-Length bounds what a client may SEND, and once bodies may be
    compressed that stops bounding what central ALLOCATES: 90 KB of zeros
    inflates to a gigabyte. `max_length` makes zlib stop at the ceiling
    instead of allocating past it, and a non-empty `unconsumed_tail` is the
    proof it wanted to keep going.

    Returns None — never raises — on a bomb, on corruption, and on a
    TRUNCATED stream (`eof` false). Truncation is refused even though the
    bytes so far may parse: half a port table that files as a complete walk
    is the failure this codebase keeps paying for.
    """
    try:
        dec = zlib.decompressobj(_GZIP_WBITS)
        out = dec.decompress(raw, max(1, limit))
        if dec.unconsumed_tail or not dec.eof:
            return None
        return out
    except Exception:
        return None


def decode_body(raw: bytes, encoding: str, limit: int = _MAX_BODY) -> bytes | None:
    """Undo `Content-Encoding` on a request body. None = undecodable.

    Central ALWAYS accepts both compressed and uncompressed — that is the
    whole deployment argument for the edge half: no handshake and no version
    dance, so central ships whenever and edges start saving as they roll.

    The MAGIC decides, not the header, and it decides in BOTH directions. A
    gzip member always opens \\x1f\\x8b and JSON text never can, so the bytes
    are a stronger signal than a header a middlebox in front of central may
    rewrite: a proxy that inflates the body and leaves `Content-Encoding:
    gzip` behind, and one that strips the header and forwards the bytes, both
    still parse. The header is kept only to log the disagreement, because a
    fleet-wide 400 storm the day gzip rolls out wants a breadcrumb naming
    which side of the tunnel mangled it.
    """
    gzipped = raw[:2] == _GZIP_MAGIC
    declared = "gzip" in encoding
    if gzipped != declared:
        log.debug("body encoding disagrees with the bytes: header=%r gzip_magic=%s",
                  encoding, gzipped)
    if gzipped:
        return gunzip_bounded(raw, limit)
    return raw


def _make_handler(cfg: Config, store: CentralStore, throttle: LoginThrottle, notifier=None,
                  engine_registry: EngineRegistry | None = None,
                  secret_box=None):
    token = cfg.central_token
    client_ca = cfg.central_client_ca
    notifier = notifier or build_notifier(cfg, store)
    registry = engine_registry or EngineRegistry(store, cfg)
    secret_box = secret_box or secretbox.from_config(cfg)
    versions = _VersionCache(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "wisp-central"
        timeout = 30
        _showcase_cache: tuple[float, dict] | None = None

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "same-origin")
            if cfg.session_cookie_secure:
                self.send_header("Strict-Transport-Security",
                                 "max-age=31536000; includeSubDomains")

        def _reply(self, code: int, body: dict, *, cookie: str | None = None) -> None:
            try:
                raw = json.dumps(body, allow_nan=False).encode()
            except ValueError:
                log.warning("non-finite float in a JSON reply — nulled (%s)",
                            self.path)
                raw = json.dumps(_json_safe(body), allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self._security_headers()
            send_cookie = cookie if cookie is not None else getattr(
                self, "_refresh_cookie", None)
            if send_cookie:
                self.send_header("Set-Cookie", send_cookie)
            self.end_headers()
            self.wfile.write(raw)

        def _raw_reply(self, code: int, headers, body: bytes) -> None:
            self.send_response(code)
            items = headers.items() if isinstance(headers, dict) else headers
            for k, v in items:
                try:
                    self.send_header(str(k), str(v))
                except Exception:
                    continue
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _proxy_forward(self, method: str, route: str, query: str) -> None:
            sid, _, rest = route[len("/api/proxy/"):].partition("/")
            body = b""
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    length = 0
                if length > _MAX_BODY:
                    self._reply(413, {"error": "request too large"})
                    return
                if length > 0:
                    body = self.rfile.read(length)
            api.proxy.browser_request(self, method, sid, rest, query, body)

        def _proxy_rescue(self, parsed) -> bool:
            if not cfg.proxy_enabled:
                return False
            m = _PROXY_SID_RE.search(self.headers.get("Referer") or "")
            if not m or not self.proxy.has_session(m.group(1)):
                return False
            loc = f"/api/proxy/{m.group(1)}{parsed.path}"
            if parsed.query:
                loc += "?" + parsed.query
            self.send_response(307)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def _read_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return None
            if length <= 0 or length > _MAX_BODY:
                return None
            raw = decode_body(self.rfile.read(length),
                              (self.headers.get("Content-Encoding") or "").strip().lower())
            if raw is None:
                # A body that will not inflate is refused exactly the way a
                # body that will not parse has always been refused: None here,
                # 400 at the caller. Never an exception into the handler.
                return None
            try:
                return json.loads(raw)
            except Exception:
                return None

        # `_read_raw` deliberately does NOT decode Content-Encoding: its whole
        # contract is "the bytes as they arrived". Two of its three callers HMAC
        # what it returns (the payments webhook and the WhatsApp webhook sign the
        # wire bytes), so inflating here would compute the digest over something
        # the sender never signed and no signature would ever verify again. The
        # third is Traccar's form POST. None of the three is our code, none of
        # them negotiates an encoding with us, and none of them carries an SNMP
        # table — so there is nothing to save here and a signature to lose.
        def _read_raw(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return b""
            if length <= 0 or length > _MAX_BODY:
                return b""
            return self.rfile.read(length)

        def _send_binary(self, code: int, ctype: str, body: bytes, *,
                         filename: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, code: int, text: str) -> None:
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _whatsapp_verify(self, qs) -> None:
            want = (store.whatsapp_settings().get("verify_token")
                    or cfg.whatsapp_verify_token or "").strip()
            mode = (qs.get("hub.mode") or [""])[0]
            got = (qs.get("hub.verify_token") or [""])[0]
            challenge = (qs.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and want and hmac.compare_digest(got, want):
                self._send_text(200, challenge)
            else:
                log.warning("whatsapp webhook verify rejected (mode=%r token_set=%s)",
                            mode, bool(want))
                self._send_text(403, "verification failed")

        def _whatsapp_sig_ok(self, raw: bytes) -> bool:
            secret = (store.whatsapp_settings().get("app_secret")
                      or cfg.whatsapp_app_secret or "").strip()
            if not secret:
                log.warning("whatsapp webhook: no app_secret set — REJECTING "
                            "unsigned webhook (set the app secret in "
                            "Settings -> Platform)")
                return False
            sent = self.headers.get("X-Hub-Signature-256", "")
            if not sent.startswith("sha256="):
                return False
            mac = hmac.new(secret.encode(), raw, "sha256").hexdigest()
            return hmac.compare_digest(sent[len("sha256="):], mac)

        def _whatsapp_inbound(self) -> None:
            raw = self._read_raw()
            if not self._whatsapp_sig_ok(raw):
                self._send_text(403, "bad signature")
                return
            self._reply(200, {"ok": True})
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                log.warning("whatsapp webhook: body is not JSON (%d bytes)", len(raw or b""))
                return
            try:
                msgs = statuses = 0
                fields = []
                for e in (payload.get("entry") or []):
                    for ch in (e.get("changes") or []):
                        fields.append(ch.get("field"))
                        v = ch.get("value") or {}
                        msgs += len(v.get("messages") or [])
                        statuses += len(v.get("statuses") or [])
                log.info("whatsapp webhook POST: fields=%s messages=%d statuses=%d",
                         ",".join(f or "?" for f in fields) or "-", msgs, statuses)
            except Exception:
                pass
            base = f"https://{self.headers.get('Host', '')}".rstrip("/")
            bot = WhatsAppBot(store, self.notifier,
                              getattr(self, "weboptics", None), base_url=base)
            threading.Thread(target=bot.handle, args=(payload,),
                             name="wisp-wa-bot", daemon=True).start()

        def _field_track(self, parsed) -> None:


            params = parse_qs(parsed.query)
            if self.command == "POST":
                raw = self._read_raw()
                if raw:
                    try:
                        form = parse_qs(raw.decode("utf-8", "replace"))
                    except Exception:
                        form = {}
                    for k, v in form.items():
                        params.setdefault(k, v)
            if not self.field_ip_rate.allow(f"ip:{self._client_ip()}"):
                self._send_text(429, "too many requests")
                return
            identity = store.resolve_field_token(
                field.param(params, "id", "deviceid", "device_id"))
            if identity is None:
                self._send_text(401, "unauthorized")
                return
            org, user_id = identity
            if not self.field_rate.allow(f"{org}:{user_id}"):
                self._send_text(429, "too many fixes")
                return
            try:
                fix = field.clean_fix(params, cfg)
            except field.TrackDropped as exc:
                self._reply(200, {"ok": True, "stored": False, "reason": str(exc)})
                return
            except field.TrackError as exc:
                self._reply(400, {"error": str(exc)})
                return
            stored = store.record_worker_fix(org, user_id, fix)
            self._reply(200, {"ok": True, "stored": stored})

        def _presented_bearer(self) -> str:
            got = self.headers.get("Authorization", "")
            return got[7:] if got.startswith("Bearer ") else ""

        def _token_ok(self) -> bool:
            if not token:
                return False
            return hmac.compare_digest(self._presented_bearer(), token)

        def _bearer_ok(self) -> bool:
            if not token:
                return True
            return self._token_ok()

        def _peer_identity(self) -> tuple[str, str] | None:
            getpeercert = getattr(self.connection, "getpeercert", None)
            if getpeercert is None:
                return None
            return pki.peer_identity(getpeercert())

        def _node_token_identity(self) -> tuple[str, str] | None:
            presented = self._presented_bearer()
            return store.resolve_node_token(presented) if presented else None

        def _ingest_ok(self, org: str, node: str | None = None) -> bool:
            if self._token_ok():
                return True
            node_identity = self._node_token_identity()
            if (node_identity is not None and node_identity[0] == org
                    and (node is None or node_identity[1] == node)):
                return True
            cert_identity = self._peer_identity()
            if (cert_identity is not None and cert_identity[0] == org
                    and (node is None or cert_identity[1] == node)):
                return True
            if node is not None and store.node_token_registered(org, node):
                return False
            return not token and not client_ca

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            if cfg.trust_forwarded_for and peer in _LOOPBACK:
                xff = self.headers.get("X-Forwarded-For")
                if xff:
                    return xff.rsplit(",", 1)[-1].strip() or peer
            return peer

        def _user(self) -> dict | None:
            tok = auth.cookie_token(self.headers.get("Cookie"))
            user = auth.resolve_session(store, tok, cfg=cfg)
            if user is not None:
                fresh = auth.slide_session(tok, cfg=cfg)
                if fresh:
                    self._refresh_cookie = auth.session_cookie(
                        fresh[0], max_age=fresh[1],
                        secure=cfg.session_cookie_secure)
            return user

        def _reader(self) -> dict | None:
            user = self._user()
            if user:
                return user
            if token and self._bearer_ok():
                return {"id": 0, "username": "token", "org_id": None,
                        "role": "superadmin", "is_superadmin": True}
            return None

        def _scope_org(self, user: dict, qs: dict) -> str | None:
            if not user["is_superadmin"]:
                return user["org_id"]
            return (qs.get("org") or [None])[0]

        @staticmethod
        def _can_write(user: dict, org: str | None) -> bool:
            if user["is_superadmin"]:
                return True
            return user["role"] == "owner" and user["org_id"] == org

        def _worker_blocked(self, route: str, user: dict | None = None,
                            method: str = "GET") -> bool:
            if not route.startswith("/api/"):
                return False
            user = user or self._user()
            if not user:
                return False
            if user.get("is_superadmin") or user.get("org_id") is None:
                return False
            if user.get("role") != "worker":
                return False
            allowed = _WORKER_GET if method == "GET" else _WORKER_POST
            if route in allowed:
                return False
            self._reply(403, {"error": "forbidden"})
            return True

        def _billing_blocked(self, route: str, user: dict | None = None) -> bool:
            if not route.startswith("/api/") or route in _BILLING_EXEMPT:
                return False
            user = user or self._user()
            if not user or user["is_superadmin"] or not user["org_id"]:
                return False
            if not billing.org_locked(store, user["org_id"]):
                return False
            self._reply(402, {"error": "payment required, account locked",
                              "locked": True})
            return True

        def _envelope(self, body: dict) -> dict | None:
            v = body.get("v")
            if not isinstance(v, int) or v > MAX_WIRE_V:
                self._reply(400, {"error": f"unsupported envelope version {v!r}"})
                return None
            if not body.get("org_id") or not body.get("node_id"):
                self._reply(400, {"error": "missing org_id/node_id"})
                return None
            return body

        def _serve_events(self, org: str | None) -> None:
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self._security_headers()
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                build = _build_cache.current()
                if build:
                    self.wfile.write(f"event: build\ndata: {build}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            versions.ensure_started()
            tok = auth.cookie_token(self.headers.get("Cookie"))
            by_session = tok and auth.resolve_session(store, tok,
                                                      cfg=cfg) is not None
            last_check = time.monotonic()
            last: str | None = None
            while True:
                version = versions.wait_change(org, last, timeout=15.0)
                if by_session and time.monotonic() - last_check >= 60.0:
                    last_check = time.monotonic()
                    if auth.resolve_session(store, tok, cfg=cfg) is None:
                        return
                cur_build = _build_cache.current()
                try:
                    if cur_build and cur_build != build:
                        build = cur_build
                        self.wfile.write(f"event: build\ndata: {build}\n\n".encode())
                    if version is not None and version != last:
                        last = version
                        self.wfile.write(f"event: changed\ndata: {version}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        def _serve_static(self, route: str) -> bool:
            if route in ("/", ""):
                rel = "landing.html"
            elif route in ("/app", "/app/"):
                rel = "index.html"
            else:
                rel = route.lstrip("/")
            path = (_STATIC / rel).resolve()
            if not str(path).startswith(str(_STATIC)) or not path.is_file():
                return False
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            data = path.read_bytes()
            if rel == "landing.html" and cfg.showcase_enabled:
                data = self._inject_showcase(data)
            if rel in ("index.html", "landing.html"):
                data = self._inject_theme(data)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if rel.startswith("assets/"):
                self.send_header("Cache-Control",
                                 "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)
            return True

        def _inject_theme(self, html: bytes) -> bytes:


            try:
                css = theme.render_css(theme.load(store))
            except Exception:
                logging.exception("theme overrides failed")
                return html
            if not css:
                return html
            snippet = ('<style id="wisp-theme">'
                       + css.replace("<", "\\3c ")
                       + "</style>").encode("utf-8")
            marker = b"</head>"
            i = html.find(marker)
            return html[:i] + snippet + html[i:] if i != -1 else html + snippet

        def _inject_showcase(self, html: bytes) -> bytes:
            try:
                cached = Handler._showcase_cache
                if cached is not None and time.monotonic() - cached[0] < 60.0:
                    stats = cached[1]
                else:
                    stats = store.showcase_stats()
                    Handler._showcase_cache = (time.monotonic(), stats)
            except Exception:
                logging.exception("showcase stats failed")
                return html
            payload = json.dumps({"enabled": True, **stats})
            payload = payload.replace("</", "<\\/")
            snippet = (
                "<script>window.__WISP_SHOWCASE__=" + payload + ";</script>"
                '<script src="/showcase.js"></script>'
            ).encode("utf-8")
            marker = b"</body>"
            i = html.rfind(marker)
            return html[:i] + snippet + html[i:] if i != -1 else html + snippet

        def _serve_release(self, route: str) -> bool:
            rest = route[len("/download/"):]
            parts = [p for p in rest.split("/") if p]
            if len(parts) != 2:
                return False
            ver, name = parts
            if "/" in name or name in ("", ".", ".."):
                return False
            if ver == "latest":
                rels = store.list_releases()
                if not rels:
                    return False
                ver = rels[0]["version"]
            base = cfg.release_cache_dir.resolve()
            path = (base / ver / name).resolve()
            if not str(path).startswith(str(base) + os.sep) or not path.is_file():
                return False
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with open(path, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return True

        def do_GET(self):
            parsed = urlparse(self.path)
            route, qs = parsed.path, parse_qs(parsed.query)
            if route == "/whatsapp/webhook":
                self._whatsapp_verify(qs)
                return
            if route == "/field/track":
                self._field_track(parsed)
                return
            if route == "/edge/proxy/next":
                api.proxy.edge_next(self, qs)
                return
            if route.startswith("/api/proxy/") and route not in _PROXY_EXACT:
                self._proxy_forward("GET", route, parsed.query)
                return
            handler = api.GET.get(route)
            if handler is not None:
                if self._billing_blocked(route) or self._worker_blocked(route):
                    return
                handler(self, qs)
                return
            if route.startswith("/download/"):
                if not self._serve_release(route):
                    self._reply(404, {"error": "not found"})
                return
            if self._serve_static(route):
                return
            if self._proxy_rescue(parsed):
                return
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/whatsapp/webhook":
                self._whatsapp_inbound()
                return
            # Carries no session and must skip both gates, so it is not an
            # `/api/*` route. Signature-verified inside; replay-safe in the
            # store. The gateway is the only thing that ever calls it.
            if route == "/payments/webhook":
                try:
                    api.billing.webhook(self)
                except Exception:
                    log.exception("payment webhook failed")
                    self._reply(500, {"error": "internal error"})
                return
            if route == "/field/track":
                self._field_track(parsed)
                return
            if route.startswith("/api/proxy/") and route not in _PROXY_EXACT:
                self._proxy_forward("POST", route, parsed.query)
                return
            # `/edge/liveping` is its OWN edge route and is deliberately not a
            # mode on `/report`. `/report` is the FSM's ingest path — it routes
            # `mode="recheck"` straight into `central_engine.run_cycle` — so a
            # live-ping mode on it would put a packet stream one `if` away from
            # the state machine, and an operator merely WATCHING a device could
            # move its flap counters and page somebody. A separate route means
            # there is no such `if` to get wrong.
            if route in ("/heartbeat", "/report", "/edge/snmp-walk",
                         "/edge/proxy/reply", "/edge/liveping"):
                body = self._read_body()
                if body is None or not isinstance(body, dict):
                    self._reply(400, {"error": "bad or missing JSON body"})
                    return
                if not self._ingest_ok(body.get("org_id"), body.get("node_id")):
                    self._reply(401, {"error": "unauthorized"})
                    return
                env = self._envelope(body)
                if env is None:
                    return
                org, node = env["org_id"], env["node_id"]
                try:
                    if route == "/heartbeat":
                        hb = env.get("body", {})
                        store.record_heartbeat(org, node, hb)
                        self._reply(200, api.edge.heartbeat_reply(self, org, node, hb))
                    elif route == "/edge/snmp-walk":
                        api.edge.walk_result(self, org, node, env)
                    elif route == "/edge/proxy/reply":
                        api.proxy.edge_reply(self, org, node, env)
                    elif route == "/edge/liveping":
                        api.liveping.edge_exchange(self, org, node, env)
                    else:
                        self._reply(200, api.edge.report(self, org, env))
                except Exception:
                    log.exception("ingest failed for %s/%s", org, node)
                    self._reply(500, {"error": "internal error"})
                return
            if route == "/api/login":
                self._login()
                return
            body = self._read_body() or {}
            if route == "/api/logout":
                u = self._user()
                if u:
                    try:
                        store.bump_session_epoch(u["id"])
                    except Exception:
                        log.debug("logout epoch bump failed", exc_info=True)
                self._reply(200, {"ok": True},
                            cookie=auth.clear_cookie(secure=cfg.session_cookie_secure))
                return
            user = self._user()
            if not user:
                self._reply(401, {"error": "unauthorized"})
                return
            handler = api.POST.get(route)
            if handler is None:
                if self._proxy_rescue(parsed):
                    return
                self._reply(404, {"error": "not found"})
                return
            if self._billing_blocked(route, user) or self._worker_blocked(route, user, "POST"):
                return
            try:
                handler(self, user, body)
            except (auth.AuthError, inventory.InventoryError) as exc:
                self._reply(422, {"error": str(exc)})
            except Exception:
                log.exception("dashboard write failed: %s", route)
                self._reply(500, {"error": "internal error"})

        def _login(self):
            ip = self._client_ip()
            body = self._read_body() or {}
            username = (body.get("username") or "").strip()
            ukey = f"user:{username.lower()}"
            wait = throttle.retry_after(ip)
            if username:
                wait = max(wait, throttle.retry_after(ukey))
            if wait > 0:
                self._reply(429, {"error": f"too many attempts; retry in {int(wait)+1}s"})
                return
            user = auth.verify_login(store, username, body.get("password", ""))
            if not user:
                throttle.fail(ip)
                if username:
                    throttle.fail(ukey)
                self._reply(401, {"error": "invalid credentials"})
                return
            if user.get("totp_enabled"):
                verdict = self._check_second_factor(user, body)
                if verdict != "ok":
                    if verdict == "required":
                        self._reply(401, {"error": "enter your authenticator code",
                                          "totp_required": True})
                    else:
                        throttle.fail(ip)
                        if username:
                            throttle.fail(ukey)
                        self._reply(401, {"error": "that code didn't match",
                                          "totp_required": True})
                    return
            throttle.reset(ip)
            throttle.reset(ukey)
            remember = bool(body.get("remember"))
            trusted_admin = False
            if user["org_id"] is None or user["role"] == "owner":
                trusted_admin = remember
                remember = False
            epoch = store.bump_session_epoch(user["id"])
            tok = auth.issue_session(user["id"], cfg, remember=remember,
                                     trusted_admin=trusted_admin, epoch=epoch)
            cookie = auth.session_cookie(
                tok, max_age=auth.session_cookie_max_age(
                    cfg, remember=remember, trusted_admin=trusted_admin),
                secure=cfg.session_cookie_secure)
            self._reply(200, {"user": public_user(user, store)}, cookie=cookie)

        def _check_second_factor(self, user, body):
            code = (body.get("totp") or body.get("code") or "").strip()
            recovery = (body.get("recovery") or "").strip()
            if not code and not recovery:
                return "required"
            if recovery:
                return ("ok" if store.consume_recovery_code(
                    user["id"], totp.recovery_hash(recovery)) else "bad")
            try:
                secret = self.secretbox.decrypt(user["totp_secret"] or "")
            except Exception:
                return "bad"
            step = totp.verify(secret, code, after_step=user.get("totp_last_step"))
            if step is None:
                return "bad"
            return "ok" if store.claim_totp_step(user["id"], step) else "bad"

    Handler.cfg = cfg
    Handler.store = store
    Handler.notifier = notifier
    Handler.registry = registry
    Handler.secretbox = secret_box
    Handler.proxy = ProxyHub(device_max_inflight=cfg.proxy_device_max_inflight)
    # In-memory, TTL'd, dies with the process — a live ping is not history.
    # Constructed with no store and no engine registry on purpose: see the
    # FSM-isolation argument at the top of `central/liveping.py`.
    Handler.liveping = LivePingHub(
        max_s=cfg.liveping_max_s, interval_ms=cfg.liveping_interval_ms,
        infra_interval_ms=cfg.liveping_infra_interval_ms,
        max_per_org=cfg.liveping_max_per_org)
    Handler.field_rate = field.TrackRate(cfg.field_track_rate_per_min)
    Handler.field_ip_rate = field.TrackRate(cfg.field_track_rate_per_min * 20)
    from wisp.central.weboptics_sweep import build_sweeper
    Handler.weboptics = build_sweeper(cfg, store, Handler.proxy, secret_box,
                                      notifier)
    return Handler

class _CentralHTTPServer(ThreadingHTTPServer):
    request_queue_size = 512
    max_workers = 512

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._worker_slots = threading.BoundedSemaphore(self.max_workers)

    def process_request(self, request, client_address) -> None:
        if not self._worker_slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class _TLSThreadingHTTPServer(_CentralHTTPServer):

    def __init__(self, addr, handler, ssl_context: ssl.SSLContext) -> None:
        super().__init__(addr, handler)
        self._ssl_context = ssl_context

    def finish_request(self, request, client_address) -> None:
        request.settimeout(15.0)
        request = self._ssl_context.wrap_socket(request, server_side=True)
        self.RequestHandlerClass(request, client_address, self)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            log.debug("TLS handshake with %s failed: %s", client_address, exc)
            return
        super().handle_error(request, client_address)

def _json_safe(obj):

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _build_tls_context(cfg: Config) -> ssl.SSLContext | None:
    if not (cfg.central_tls_cert and cfg.central_tls_key):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cfg.central_tls_cert, cfg.central_tls_key)
    if cfg.central_client_ca:
        ctx.verify_mode = ssl.CERT_OPTIONAL
        ctx.load_verify_locations(cafile=cfg.central_client_ca)
    return ctx

def make_server(cfg: Config = CONFIG, store: CentralStore | None = None,
                notifier=None, engine_registry: EngineRegistry | None = None,
                secret_box=None) -> ThreadingHTTPServer:
    store = store or CentralStore(cfg.central_db)
    handler = _make_handler(cfg, store, LoginThrottle(), notifier,
                            engine_registry, secret_box)
    tls_context = _build_tls_context(cfg)
    if tls_context is not None:
        httpd = _TLSThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler, tls_context)
    else:
        httpd = _CentralHTTPServer((cfg.central_bind, cfg.central_port), handler)
    httpd.store = store
    httpd.proxy = handler.proxy
    httpd.liveping = handler.liveping
    httpd.secretbox = handler.secretbox
    httpd.weboptics = handler.weboptics
    return httpd

def serve(cfg: Config = CONFIG) -> None:
    if not cfg.central_token and not cfg.central_client_ca:
        log.warning("neither WISP_CENTRAL_TOKEN nor WISP_CENTRAL_CLIENT_CA is set — ingest is "
                    "UNAUTHENTICATED. Set a token and/or enroll edges with mTLS "
                    "(central.admin init-ca / enroll-edge) before exposing central beyond a "
                    "trusted network.")
    httpd = make_server(cfg)
    from wisp.central.watchdog import start_central_watchdog_thread
    start_central_watchdog_thread(cfg, httpd.store)
    central_rollup.start_central_rollup_prune_thread(cfg, httpd.store)
    field.start_field_prune_thread(cfg, httpd.store)
    from wisp.central.history import start_history_thread
    start_history_thread(cfg, httpd.store)
    billing.start_central_billing_thread(cfg, httpd.store)
    from wisp.central.weboptics_sweep import start_nvr_thread, start_web_optics_thread
    start_web_optics_thread(cfg, httpd.store, httpd.proxy, httpd.secretbox,
                            sweeper=httpd.weboptics)
    start_nvr_thread(cfg, httpd.store, httpd.proxy, httpd.secretbox,
                     sweeper=httpd.weboptics)
    from wisp.central.radius_sync import start_radius_thread
    start_radius_thread(cfg, httpd.store, httpd.secretbox)
    if not httpd.store.list_users():
        log.warning("no central accounts yet — bootstrap one: "
                    "PYTHONPATH=src python -m wisp.central.admin create-superadmin --username ...")
    scheme = "https" if isinstance(httpd, _TLSThreadingHTTPServer) else "http"
    log.info("central listening on %s://%s:%d (db=%s)",
             scheme, cfg.central_bind, cfg.central_port, cfg.central_db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
