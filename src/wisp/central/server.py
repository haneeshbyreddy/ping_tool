from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import shutil
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import api, auth, inventory, pki
from wisp.central import rollup as central_rollup
from wisp.central.api.common import public_user
from wisp.central.auth import LoginThrottle
from wisp.central.engine import EngineRegistry
from wisp.central.store import CentralStore
from wisp.egress.notifiers import build_notifier
from wisp.runtime.central_client import WIRE_V

log = logging.getLogger("wisp.central")

MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024
_STATIC = Path(__file__).resolve().parent / "static"

def _make_handler(cfg: Config, store: CentralStore, throttle: LoginThrottle, notifier=None,
                  engine_registry: EngineRegistry | None = None):
    token = cfg.central_token
    client_ca = cfg.central_client_ca
    notifier = notifier or build_notifier(cfg)
    registry = engine_registry or EngineRegistry(store, cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "wisp-central"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _reply(self, code: int, body: dict, *, cookie: str | None = None) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return None
            if length <= 0 or length > _MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                return None

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

        def _user(self) -> dict | None:
            tok = auth.cookie_token(self.headers.get("Cookie"))
            return auth.resolve_session(store, tok, cfg=cfg)

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
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            last: str | None = None
            idle = 0
            while True:
                try:
                    version = store.data_version(org)
                except Exception:
                    version = last
                try:
                    if version != last:
                        last = version
                        self.wfile.write(f"event: changed\ndata: {version}\n\n".encode())
                        idle = 0
                    else:
                        idle += 1
                        if idle % 15 == 0:
                            self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(1.0)

        def _serve_static(self, route: str) -> bool:
            # `/` is the public marketing landing page; the dashboard SPA lives
            # under `/app` (HashRouter, so `/app#/home` etc.). Anything else is a
            # real static file (assets, favicon, install scripts).
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
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return True

        def _inject_showcase(self, html: bytes) -> bytes:
            # The landing page is an opaque pre-bundled artifact that rebuilds its
            # whole DOM client-side (documentElement.replaceWith), so we don't edit
            # the bundle: we inject the live DB numbers + a small self-healing
            # overlay script (showcase.js re-mounts after the swap). Best-effort —
            # a store hiccup must never 500 the marketing page.
            try:
                stats = store.showcase_stats()
            except Exception:
                logging.exception("showcase stats failed")
                return html
            payload = json.dumps({"enabled": True, **stats})
            # Guard the JSON against breaking out of the <script> element.
            payload = payload.replace("</", "<\\/")
            snippet = (
                "<script>window.__WISP_SHOWCASE__=" + payload + ";</script>"
                '<script src="/showcase.js"></script>'
            ).encode("utf-8")
            marker = b"</body>"
            i = html.rfind(marker)
            return html[:i] + snippet + html[i:] if i != -1 else html + snippet

        def _serve_release(self, route: str) -> bool:
            # /download/<version>/<name> or /download/latest/<name> — the mirrored
            # GitHub release assets (installers + agent binaries). PUBLIC by design:
            # these are compiled artifacts, not secrets (the source repo is what's
            # private), and edges self-update from here with no dashboard session.
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
            handler = api.GET.get(route)
            if handler is not None:
                handler(self, qs)
                return
            if route.startswith("/download/"):
                if not self._serve_release(route):
                    self._reply(404, {"error": "not found"})
                return
            if self._serve_static(route):
                return
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route in ("/heartbeat", "/report", "/edge/snmp-walk"):
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
                self._reply(200, {"ok": True}, cookie=auth.clear_cookie())
                return
            user = self._user()
            if not user:
                self._reply(401, {"error": "unauthorized"})
                return
            handler = api.POST.get(route)
            if handler is None:
                self._reply(404, {"error": "not found"})
                return
            try:
                handler(self, user, body)
            except (auth.AuthError, inventory.InventoryError) as exc:
                self._reply(422, {"error": str(exc)})
            except Exception:
                log.exception("dashboard write failed: %s", route)
                self._reply(500, {"error": "internal error"})

        def _login(self):
            ip = self.client_address[0]
            wait = throttle.retry_after(ip)
            body = self._read_body() or {}
            if wait > 0:
                self._reply(429, {"error": f"too many attempts; retry in {int(wait)+1}s"})
                return
            user = auth.verify_login(store, body.get("username", ""), body.get("password", ""))
            if not user:
                throttle.fail(ip)
                self._reply(401, {"error": "invalid credentials"})
                return
            throttle.reset(ip)
            tok = auth.issue_session(user["id"], cfg)
            cookie = auth.session_cookie(tok, max_age=cfg.session_timeout_h * 3600)
            self._reply(200, {"user": public_user(user, store)}, cookie=cookie)

    # Route handlers in wisp.central.api receive the live handler instance;
    # the request services ride on it as class attributes.
    Handler.cfg = cfg
    Handler.store = store
    Handler.notifier = notifier
    Handler.registry = registry
    return Handler

class _TLSThreadingHTTPServer(ThreadingHTTPServer):

    def __init__(self, addr, handler, ssl_context: ssl.SSLContext) -> None:
        super().__init__(addr, handler)
        self._ssl_context = ssl_context

    def finish_request(self, request, client_address) -> None:
        request = self._ssl_context.wrap_socket(request, server_side=True)
        self.RequestHandlerClass(request, client_address, self)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            log.debug("TLS handshake with %s failed: %s", client_address, exc)
            return
        super().handle_error(request, client_address)

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
                notifier=None, engine_registry: EngineRegistry | None = None
                ) -> ThreadingHTTPServer:
    store = store or CentralStore(cfg.central_db)
    handler = _make_handler(cfg, store, LoginThrottle(), notifier, engine_registry)
    tls_context = _build_tls_context(cfg)
    if tls_context is not None:
        httpd = _TLSThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler, tls_context)
    else:
        httpd = ThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler)
    httpd.store = store
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
