"""Central server (Phase 10 Parts A–C) — ingest + a multi-tenant dashboard, pure stdlib.

Two auth planes, deliberately separate:
  * **Ingest** (`POST /ingest`, `/heartbeat`) — machine-to-machine, bearer token
    (`WISP_CENTRAL_TOKEN`). This is how edges report; unchanged from Part A/B.
  * **Dashboard** (`/api/*` reads + writes, the SPA) — humans, per-org login accounts with
    identity-carrying signed-cookie sessions (`central/auth.py`). Every dashboard read is
    **scoped to the caller's tenant**; a superadmin sees all orgs and may pass `?tenant=`.

Writes (team/attendance/users/org) require an owner or a superadmin. Static assets are unauthed
(the SPA renders its own login gate on a 401), exactly like the edge dashboard. Run behind a TLS
terminator in production; the server itself speaks plain HTTP to stay dependency-free.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import auth
from wisp.central.store import CentralStore
from wisp.egress.shipper import WIRE_V
from wisp.server.auth import LoginThrottle

log = logging.getLogger("wisp.central")

MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024
_STATIC = Path(__file__).resolve().parent / "static"


def _make_handler(cfg: Config, store: CentralStore, throttle: LoginThrottle):
    token = cfg.central_token

    class Handler(BaseHTTPRequestHandler):
        server_version = "wisp-central"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # --- io helpers ---
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

        # --- auth: ingest (bearer) vs dashboard (session) ---
        def _bearer_ok(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            presented = got[7:] if got.startswith("Bearer ") else ""
            return hmac.compare_digest(presented, token)

        def _user(self) -> dict | None:
            tok = auth.cookie_token(self.headers.get("Cookie"))
            return auth.resolve_session(store, tok, cfg=cfg)

        def _reader(self) -> dict | None:
            """The principal allowed to READ: a logged-in human, OR — for curl/automation —
            the configured bearer token, treated as a cross-tenant machine superadmin. Writes
            never accept the token (they go through real accounts); the token reads only."""
            user = self._user()
            if user:
                return user
            if token and self._bearer_ok():
                return {"id": 0, "username": "token", "tenant_id": None,
                        "role": "superadmin", "is_superadmin": True}
            return None

        def _scope_tenant(self, user: dict, qs: dict) -> str | None:
            """The tenant a request is allowed to read: an org user is pinned to their own
            tenant; a superadmin sees all (None) or narrows with ?tenant=."""
            if not user["is_superadmin"]:
                return user["tenant_id"]
            return (qs.get("tenant") or [None])[0]

        @staticmethod
        def _can_write(user: dict, tenant: str | None) -> bool:
            if user["is_superadmin"]:
                return True
            return user["role"] == "owner" and user["tenant_id"] == tenant

        def _envelope(self) -> dict | None:
            env = self._read_body()
            if env is None or not isinstance(env, dict):
                self._reply(400, {"error": "bad or missing JSON body"})
                return None
            v = env.get("v")
            if not isinstance(v, int) or v > MAX_WIRE_V:
                self._reply(400, {"error": f"unsupported envelope version {v!r}"})
                return None
            if not env.get("tenant_id") or not env.get("node_id"):
                self._reply(400, {"error": "missing tenant_id/node_id"})
                return None
            return env

        # --- static (unauthed; SPA shows its own login gate on 401) ---
        def _serve_static(self, route: str) -> bool:
            rel = "index.html" if route in ("/", "") else route.lstrip("/")
            path = (_STATIC / rel).resolve()
            if not str(path).startswith(str(_STATIC)) or not path.is_file():
                return False
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        # --- routing ---
        def do_GET(self):
            parsed = urlparse(self.path)
            route, qs = parsed.path, parse_qs(parsed.query)
            if route == "/healthz":
                self._reply(200, {"ok": True, "counts": store.counts()})
                return
            if route == "/api/me":
                user = self._user()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                self._reply(200, {"user": _public_user(user),
                                  "channels": {"central": cfg.central_ntfy_topic}})
                return
            if route in ("/api/fleet", "/api/orgs", "/api/devices",
                         "/api/team", "/api/attendance", "/api/users"):
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                tenant = self._scope_tenant(user, qs)
                if route == "/api/fleet":
                    self._reply(200, store.fleet(tenant_id=tenant))
                elif route == "/api/orgs":
                    self._reply(200, {"orgs": store.orgs()})
                elif route == "/api/devices":
                    self._reply(200, {"devices": store.devices(tenant_id=tenant)})
                elif route == "/api/users":
                    if not user["is_superadmin"] and user["role"] != "owner":
                        self._reply(403, {"error": "forbidden"})
                        return
                    self._reply(200, {"users": store.list_users(tenant_id=tenant)})
                elif route == "/api/team":
                    if not tenant:
                        self._reply(400, {"error": "tenant required"})
                        return
                    self._reply(200, {"team": store.list_workers(tenant)})
                else:  # /api/attendance
                    if not tenant:
                        self._reply(400, {"error": "tenant required"})
                        return
                    self._reply(200, store.attendance_overview(tenant))
                return
            if self._serve_static(route):
                return
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            route = parsed.path
            # Ingest plane (bearer token).
            if route in ("/ingest", "/heartbeat"):
                if not self._bearer_ok():
                    self._read_body()
                    self._reply(401, {"error": "unauthorized"})
                    return
                env = self._envelope()
                if env is None:
                    return
                tenant, node = env["tenant_id"], env["node_id"]
                try:
                    if route == "/ingest":
                        self._reply(200, {"accepted": store.ingest(tenant, node,
                                                                   env.get("records", []))})
                    else:
                        store.record_heartbeat(tenant, node, env.get("body", {}))
                        self._reply(200, {"ok": True})
                except Exception:
                    log.exception("ingest failed for %s/%s", tenant, node)
                    self._reply(500, {"error": "internal error"})
                return
            # Dashboard plane (session).
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
            try:
                self._dashboard_write(route, user, body)
            except auth.AuthError as exc:
                self._reply(422, {"error": str(exc)})
            except Exception:
                log.exception("dashboard write failed: %s", route)
                self._reply(500, {"error": "internal error"})

        # --- login ---
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
            self._reply(200, {"user": _public_user(user)}, cookie=cookie)

        # --- dashboard writes (owner / superadmin) ---
        def _dashboard_write(self, route: str, user: dict, body: dict):
            # team
            if route == "/api/team":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                wid = store.add_worker(tenant, body["name"], body.get("role", "operator"),
                                       body.get("region"), body.get("notes"))
                self._reply(200, {"id": wid})
                return
            if route == "/api/team/delete":
                w = _worker_tenant(store, body.get("id"))
                if not self._can_write(user, w):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.delete_worker(int(body["id"]))
                self._reply(200, {"ok": True})
                return
            if route == "/api/attendance":
                w = _worker_tenant(store, body.get("worker_id"))
                if not self._can_write(user, w):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.set_attendance(w, int(body["worker_id"]), bool(body.get("present")),
                                     body.get("day"))
                self._reply(200, {"ok": True})
                return
            # org rename / topic (owner of that org, or superadmin)
            if route == "/api/org":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.set_org(tenant, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"))
                self._reply(200, {"ok": True})
                return
            # user provisioning: superadmin anywhere; an owner within their own org
            if route == "/api/users":
                tenant = body.get("tenant_id") if user["is_superadmin"] else user["tenant_id"]
                if not (user["is_superadmin"] or user["role"] == "owner"):
                    self._reply(403, {"error": "forbidden"})
                    return
                uid = auth.create_user(store, tenant, body.get("username", ""),
                                       body.get("password", ""), body.get("role", "operator"))
                self._reply(200, {"id": uid})
                return
            if route == "/api/users/deactivate":
                if not (user["is_superadmin"] or user["role"] == "owner"):
                    self._reply(403, {"error": "forbidden"})
                    return
                target = store.get_user(int(body["id"]))
                if target and (user["is_superadmin"] or target["tenant_id"] == user["tenant_id"]):
                    store.set_user_active(int(body["id"]), bool(body.get("active", False)))
                    self._reply(200, {"ok": True})
                else:
                    self._reply(403, {"error": "forbidden"})
                return
            self._reply(404, {"error": "not found"})

    return Handler


def _public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "tenant_id": user["tenant_id"],
            "role": user["role"], "is_superadmin": user["tenant_id"] is None}


def _worker_tenant(store: CentralStore, worker_id) -> str | None:
    """The tenant a worker belongs to (so a write is authorized against the right org)."""
    if worker_id is None:
        return None
    with store._connect() as conn:  # read-only; the write that follows takes the lock
        row = conn.execute("SELECT tenant_id FROM org_workers WHERE id=?",
                           (int(worker_id),)).fetchone()
    return row["tenant_id"] if row else None


def make_server(cfg: Config = CONFIG, store: CentralStore | None = None) -> ThreadingHTTPServer:
    store = store or CentralStore(cfg.central_db)
    handler = _make_handler(cfg, store, LoginThrottle())
    httpd = ThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler)
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def serve(cfg: Config = CONFIG) -> None:
    if not cfg.central_token:
        log.warning("WISP_CENTRAL_TOKEN is empty — ingest is UNAUTHENTICATED. Set a token "
                    "before exposing central beyond a trusted network.")
    httpd = make_server(cfg)
    from wisp.central.watchdog import start_central_watchdog_thread
    start_central_watchdog_thread(cfg, httpd.store)  # type: ignore[attr-defined]
    if not httpd.store.list_users():  # type: ignore[attr-defined]
        log.warning("no central accounts yet — bootstrap one: "
                    "PYTHONPATH=src python -m wisp.central.admin create-superadmin --username ...")
    log.info("central listening on %s:%d (db=%s)",
             cfg.central_bind, cfg.central_port, cfg.central_db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
