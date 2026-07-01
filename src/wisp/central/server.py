"""Central server (Phase 10 Parts A–C; Phase B raw-report ingest) — ingest + a
multi-tenant dashboard, pure stdlib.

Two auth planes, deliberately separate:
  * **Ingest** (`POST /ingest`, `/heartbeat`, `/report`, `GET /edge/devices`) —
    machine-to-machine, any ONE of three satisfies it: the global bearer token
    (`WISP_CENTRAL_TOKEN`), a self-service per-node token an ISP owner/operator issues
    from the dashboard itself (`POST /api/nodes`, presented the same way — as
    `Authorization: Bearer <token>` — so no edge-side config shape changes; see
    `node_tokens` in `central/store.py`), or a verified mTLS client cert
    (`WISP_CENTRAL_CLIENT_CA` + an edge cert from `central.admin enroll-edge`, see
    `central/pki.py`). If NONE of the three is configured/registered for a given node,
    ingest stays open (trusted-network default, unchanged from before any of this
    existed) — but a node that HAS a self-service credential of its own is gated on it
    regardless of the other two, so registering one actually means something.
  * **Dashboard** (`/api/*` reads + writes, the SPA) — humans, per-org login accounts with
    identity-carrying signed-cookie sessions (`central/auth.py`). Every dashboard read is
    **scoped to the caller's tenant**; a superadmin sees all orgs and may pass `?tenant=`.

Writes (team/attendance/users/org) require an owner or a superadmin. Static assets are unauthed
(the SPA renders its own login gate on a 401), exactly like the edge dashboard. The server
speaks plain HTTP unless `WISP_CENTRAL_TLS_CERT`/`_KEY` are set, in which case it terminates
TLS itself (stdlib `ssl` — no new dependency) so it can do the client-cert handshake mTLS
needs; a terminator (nginx/Caddy) in front is still fine for the dashboard-only case, or if
you'd rather not manage the internal CA at all.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import ssl
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import auth, inventory, pki
from wisp.central import analytics as central_analytics
from wisp.central import engine as central_engine
from wisp.central import perf as central_perf
from wisp.central import redundancy as central_redundancy
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.engine import EngineRegistry
from wisp.central.ports import CentralPortMonitor
from wisp.central import rollup as central_rollup
from wisp.central.store import CentralStore
from wisp.egress.notifiers import build_notifier
from wisp.ingress.probers import PingResult
from wisp.runtime.central_client import WIRE_V
from wisp.central.auth import LoginThrottle

log = logging.getLogger("wisp.central")

MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024
_STATIC = Path(__file__).resolve().parent / "static"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_handler(cfg: Config, store: CentralStore, throttle: LoginThrottle, notifier=None,
                  engine_registry: EngineRegistry | None = None):
    token = cfg.central_token
    client_ca = cfg.central_client_ca
    notifier = notifier or build_notifier(cfg)
    # ONE registry per server (not per-request): the FSM's flap-suppression counters must
    # survive across an edge's successive POST /report calls (see central/engine.py).
    registry = engine_registry or EngineRegistry(store, cfg)

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

        # --- auth: ingest (global bearer, and/or a self-service per-node token, and/or
        # mTLS) vs dashboard (session) ---
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
            """The (tenant_id, node_id) a verified mTLS client cert claims, or None if
            this connection is plain HTTP / presented no cert / the cert's CN isn't in
            our `tenant:node` shape. `self.connection` is only an `ssl.SSLSocket` (with
            `getpeercert`) when `make_server` wrapped the listener in TLS."""
            getpeercert = getattr(self.connection, "getpeercert", None)
            if getpeercert is None:
                return None
            return pki.peer_identity(getpeercert())

        def _node_token_identity(self) -> tuple[str, str] | None:
            """The (tenant_id, node_id) a presented bearer authenticates as via a
            dashboard-issued self-service credential (`POST /api/nodes`, `central/
            store.py`'s `node_tokens`) — same derive-identity-from-the-credential
            discipline as `_peer_identity()`, never trust the envelope's claimed
            tenant/node alone."""
            presented = self._presented_bearer()
            return store.resolve_node_token(presented) if presented else None

        def _ingest_ok(self, tenant: str, node: str | None = None) -> bool:
            """Ingest auth: the global bearer token, OR a self-service per-node token
            claiming this tenant (and node, when known), OR a verified mTLS client cert
            claiming the same (GET /edge/devices has no node in its query, so that check
            is tenant-only there) — any one of the three satisfies it. If NONE of the
            three is configured/registered at all, ingest stays open (today's trusted-
            network default). But a node that HAS its own registered self-service
            credential is gated on presenting it regardless of whether the global token
            or mTLS are configured — otherwise self-service registration would be
            security theatre on a deployment that never set either of those up."""
            if self._token_ok():
                return True
            node_identity = self._node_token_identity()
            if (node_identity is not None and node_identity[0] == tenant
                    and (node is None or node_identity[1] == node)):
                return True
            cert_identity = self._peer_identity()
            if (cert_identity is not None and cert_identity[0] == tenant
                    and (node is None or cert_identity[1] == node)):
                return True
            if node is not None and store.node_token_registered(tenant, node):
                return False
            return not token and not client_ca

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

        def _envelope(self, body: dict) -> dict | None:
            """Shape-validate an already-read ingest body (see do_POST — auth needs
            `tenant_id`/`node_id` out of the body first, so reading happens before this)."""
            v = body.get("v")
            if not isinstance(v, int) or v > MAX_WIRE_V:
                self._reply(400, {"error": f"unsupported envelope version {v!r}"})
                return None
            if not body.get("tenant_id") or not body.get("node_id"):
                self._reply(400, {"error": "missing tenant_id/node_id"})
                return None
            return body

        # --- live push (Server-Sent Events) ---
        def _serve_events(self, tenant: str | None) -> None:
            """SSE stream: emit a `changed` event whenever `store.data_version(tenant)`
            moves, so the dashboard updates the instant an edge reports or an SNMP walk
            lands — no client-side polling. Mirrors the old single-box dashboard's
            `_serve_events` one-for-one; scoped to the caller's tenant (or every tenant,
            for a superadmin viewing "all orgs"). No Content-Length — the connection
            stays open and the browser's EventSource auto-reconnects."""
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
                    version = store.data_version(tenant)
                except Exception:
                    version = last  # a transient DB hiccup must not kill the stream
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
            if route == "/api/summary":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                tenant = self._scope_tenant(user, qs)
                if not tenant:
                    self._reply(400, {"error": "tenant required"})
                    return
                self._reply(200, {"uplink_down": store.uplink_active(tenant),
                                  "low_bandwidth": store.low_bandwidth_alarms(tenant)})
                return
            if route == "/api/events":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                tenant = self._scope_tenant(user, qs)
                self._serve_events(tenant)
                return
            # Ingest plane (bearer token): what should this edge probe? Phase B — the
            # edge's device list now comes from the ISP-managed org_devices topology
            # (Phase A), not a local dashboard.
            if route == "/edge/devices":
                tenant = (qs.get("tenant_id") or [None])[0]
                if not tenant:
                    self._reply(400, {"error": "tenant_id required"})
                    return
                if not self._ingest_ok(tenant):
                    self._reply(401, {"error": "unauthorized"})
                    return
                self._reply(200, {"devices": store.org_device_topology(tenant),
                                  "canary_ip": cfg.canary_ip})
                return
            if route == "/api/inventory/ports":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                try:
                    did = int((qs.get("device_id") or [None])[0])
                except (TypeError, ValueError):
                    self._reply(400, {"error": "device_id required"})
                    return
                tenant = store.device_tenant(did)
                if tenant is None or not (user["is_superadmin"] or user["tenant_id"] == tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"ports": store.list_switch_ports(tenant, did)})
                return
            if route == "/api/inventory/redundancy":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                try:
                    did = int((qs.get("device_id") or [None])[0])
                except (TypeError, ValueError):
                    self._reply(400, {"error": "device_id required"})
                    return
                tenant = store.device_tenant(did)
                if tenant is None or not (user["is_superadmin"] or user["tenant_id"] == tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"redundancy": store.device_redundancy_state(tenant, did)})
                return
            if route == "/api/inventory/perf":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                try:
                    did = int((qs.get("device_id") or [None])[0])
                except (TypeError, ValueError):
                    self._reply(400, {"error": "device_id required"})
                    return
                tenant = store.device_tenant(did)
                if tenant is None or not (user["is_superadmin"] or user["tenant_id"] == tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"perf": store.device_perf_state(tenant, did)})
                return
            if route == "/api/analytics":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                tenant = self._scope_tenant(user, qs)
                if not tenant:
                    self._reply(400, {"error": "tenant required"})
                    return
                try:
                    days = int((qs.get("days") or [30])[0])
                except (TypeError, ValueError):
                    days = 30
                since, until = central_analytics.window(days)
                self._reply(200, {"since": since, "until": until,
                                  "devices": central_analytics.device_reliability(
                                      store, tenant, since, until)})
                return
            if route == "/api/analytics/trend":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                try:
                    did = int((qs.get("device_id") or [None])[0])
                except (TypeError, ValueError):
                    self._reply(400, {"error": "device_id required"})
                    return
                tenant = store.device_tenant(did)
                if tenant is None or not (user["is_superadmin"] or user["tenant_id"] == tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                try:
                    days = int((qs.get("days") or [7])[0])
                except (TypeError, ValueError):
                    days = 7
                days = min(days, central_rollup.RETENTION_DAYS)   # nothing older survives
                since, until = central_analytics.window(days)
                self._reply(200, {"since": since, "until": until,
                                  "buckets": store.device_rollup_series(tenant, did, since, until)})
                return
            if route in ("/api/fleet", "/api/orgs", "/api/devices", "/api/inventory",
                         "/api/team", "/api/attendance", "/api/users", "/api/nodes"):
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                tenant = self._scope_tenant(user, qs)
                if route == "/api/fleet":
                    self._reply(200, store.fleet(tenant_id=tenant))
                elif route == "/api/orgs":
                    # store.orgs() is cross-tenant by nature (it's the org directory) — an
                    # org user must only ever see their OWN org's row (name/topics included),
                    # never another tenant's. `tenant` here is already pinned for org users
                    # (_scope_tenant) and only None for a superadmin with no ?tenant=.
                    orgs = store.orgs()
                    if tenant:
                        orgs = [o for o in orgs if o["tenant_id"] == tenant]
                    self._reply(200, {"orgs": orgs})
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
                elif route == "/api/inventory":
                    if not tenant:
                        self._reply(400, {"error": "tenant required"})
                        return
                    self._reply(200, {"devices": store.list_org_devices(tenant)})
                elif route == "/api/nodes":
                    if not tenant:
                        self._reply(400, {"error": "tenant required"})
                        return
                    self._reply(200, {"nodes": store.list_node_tokens(tenant)})
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
            # Ingest plane (bearer token and/or mTLS client cert).
            if route in ("/ingest", "/heartbeat", "/report"):
                body = self._read_body()
                if body is None or not isinstance(body, dict):
                    self._reply(400, {"error": "bad or missing JSON body"})
                    return
                # Auth needs the CLAIMED tenant/node to check a presented cert against —
                # read before full shape validation so a missing field still 401s (not
                # 400s) when auth is what actually failed, same precedence as before mTLS.
                if not self._ingest_ok(body.get("tenant_id"), body.get("node_id")):
                    self._reply(401, {"error": "unauthorized"})
                    return
                env = self._envelope(body)
                if env is None:
                    return
                tenant, node = env["tenant_id"], env["node_id"]
                try:
                    if route == "/ingest":
                        self._reply(200, {"accepted": store.ingest(tenant, node,
                                                                   env.get("records", []))})
                    elif route == "/heartbeat":
                        body = env.get("body", {})
                        store.record_heartbeat(tenant, node, body)
                        self._reply(200, self._heartbeat_reply(tenant, node, body))
                    else:
                        self._reply(200, self._report(tenant, env))
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
            except (auth.AuthError, inventory.InventoryError) as exc:
                self._reply(422, {"error": str(exc)})
            except Exception:
                log.exception("dashboard write failed: %s", route)
                self._reply(500, {"error": "internal error"})

        def _heartbeat_reply(self, tenant: str, node: str, body: dict) -> dict:
            """The heartbeat reply doubles as the update channel (Part D): advance the org's
            rollout and, if this node is due a newer version, hand it the signed directive."""
            reply: dict = {"ok": True}
            try:
                from wisp.central import rollout
                rollout.evaluate(store, tenant, cfg=cfg)
                directive = rollout.directive_for(store, tenant, node, body.get("version"),
                                                  body.get("platform"))
                if directive:
                    reply["update"] = directive
            except Exception:
                log.exception("rollout directive failed for %s/%s", tenant, node)
            return reply

        def _report(self, tenant: str, env: dict) -> dict:
            """Phase B — one raw-ping report from an edge: run that tenant's
            MonitorEngine one cycle, persist outages + live device_states, and page
            through the org's role topics. `pings` is {ip: {loss_pct, latency_ms,
            jitter_ms}} — the SAME ip-keyed shape `MonitorEngine.process_cycle` already
            expects (a device's IP resolves to its org_devices row inside the engine), so
            no device-id translation is needed on the wire going IN.

            `mode` (default "full") is the fast-confirm round trip: a "full" report
            advances every device and, if anything looks like a fresh DOWN/recovery in
            progress, the reply carries a `recheck` hint (down_ips/up_ips/interval_s —
            see central/engine.py:compute_recheck). A "recheck" report carries samples for
            ONLY those suspect IPs; central resolves them back to device ids (via the
            SAME cached engine — `eng.meta`, so FSM state stays consistent with the full
            pass that flagged them) and advances just that subset, mirroring the edge's
            own confirmation-pass mode. Either way the reply may carry ANOTHER `recheck`
            hint — the edge just keeps following it until the reply omits one, which
            happens automatically once every suspect has either confirmed or cleared (see
            compute_recheck's docstring for why that's guaranteed to terminate)."""
            ts = env.get("ts") or _now_iso()
            pings = env.get("pings") or {}
            results = {
                ip: PingResult(ip, v.get("latency_ms"),
                              float(v.get("loss_pct", 100.0)), v.get("jitter_ms"))
                for ip, v in pings.items()
            }
            eng = registry.get(tenant)
            mode = env.get("mode") or "full"
            if mode == "recheck":
                ip_to_id = {d.ip_address: d.id for d in eng.meta.values()}
                subset = {ip_to_id[ip] for ip in results if ip in ip_to_id}
                cycle = central_engine.run_cycle(store, tenant, eng, results, ts,
                                                 subset=subset)
            else:
                cycle = central_engine.run_cycle(store, tenant, eng, results, ts)

            disp = CentralAlertDispatcher(store, tenant, eng, notifier, cfg)
            disp.dispatch(cycle.events, ts)
            if mode != "recheck":
                # Escalation sweeping is time-gated (due_at) and idempotent, but a recheck
                # burst can fire several rounds a second — no need to re-check on every one.
                disp.sweep(ts)
                # SNMP port folding (CLAUDE.md item 1): only a "full" report carries a
                # `ports` key (the edge's own slow SNMP cadence, independent of ICMP's
                # poll_interval_s — see apps/daemon/main.py's _gather_snmp_ports), run
                # AFTER the ICMP cycle commits so open_outage_id reflects this cycle's
                # outages, not last cycle's.
                self._ingest_ports(tenant, eng, env.get("ports"), ts)
                # Hourly latency/loss trend rollup (CLAUDE.md item 2, second slice) — full
                # reports only, so a recheck's rapid re-probe of a suspect subset never
                # skews an hour's average.
                central_rollup.record_cycle(store, tenant, eng, cycle, results, ts)
                # Per-link performance baseline (CLAUDE.md item 3) — same full-report-only
                # gating; a recheck's suspect subset isn't a meaningful perf sample.
                central_perf.record_and_evaluate(store, tenant, eng, cycle, results, ts,
                                                 notifier, cfg)
                # On-backup redundancy signal (CLAUDE.md item 3) — cycle.redundancy is
                # only ever populated on a full pass (see MonitorEngine.process_cycle),
                # so this is a no-op on a recheck even without the mode gate above.
                central_redundancy.sweep(store, tenant, eng, cycle.redundancy,
                                         cycle.states, notifier, ts, cfg)

            reply: dict = {"ok": True}
            recheck = central_engine.compute_recheck(eng, cycle, results, cfg)
            if recheck:
                reply["recheck"] = recheck
            return reply

        def _ingest_ports(self, tenant: str, eng, ports_by_device, ts: str) -> None:
            """Fold each reported switch's port readings. `ports_by_device` is
            {"<device_id>": [port dict, ...]} (JSON object keys are always strings on
            the wire). A device id not in THIS tenant's engine meta is ignored rather
            than trusted from the body — the same re-derive-tenant-from-what-we-already-
            know discipline `org_devices` writes use, so tenant A can't attribute a port
            reading to tenant B's device id."""
            if not ports_by_device:
                return
            monitor = CentralPortMonitor(store, tenant, notifier, cfg)
            for raw_id, ports in ports_by_device.items():
                try:
                    device_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if device_id not in eng.meta or not isinstance(ports, list):
                    continue
                try:
                    monitor.sync_device(device_id, ports, ts)
                except Exception:
                    log.exception("SNMP port fold failed for %s/device=%d", tenant, device_id)

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
            # self-service node (edge) enrollment
            if route == "/api/nodes":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                if store.get_node_token_status(tenant, node_id):
                    raise inventory.InventoryError(
                        f"node {node_id!r} is already registered for {tenant!r} — "
                        "use rotate instead of registering it again")
                node_token = store.issue_node_token(tenant, node_id, created_by=user["id"])
                self._reply(200, {"node_id": node_id, "token": node_token})
                return
            if route == "/api/nodes/rotate":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                if not store.get_node_token_status(tenant, node_id):
                    raise inventory.InventoryError(
                        f"node {node_id!r} isn't registered for {tenant!r} yet")
                node_token = store.issue_node_token(tenant, node_id, created_by=user["id"])
                self._reply(200, {"node_id": node_id, "token": node_token})
                return
            if route == "/api/nodes/revoke":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                ok = store.revoke_node_token(tenant, node_id)
                self._reply(200 if ok else 404, {"ok": ok})
                return
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
            # org rename / topics (owner of that org, or superadmin)
            if route == "/api/org":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.set_org(tenant, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                              ntfy_topic_owner=body.get("ntfy_topic_owner"),
                              ntfy_topic_operator=body.get("ntfy_topic_operator"),
                              ntfy_topic_tech=body.get("ntfy_topic_tech"))
                self._reply(200, {"ok": True})
                return
            # send a test push to one of an org's three role channels (Settings go-live check)
            if route == "/api/test-alert":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                role = str(body.get("role") or "").strip().lower()
                if role not in ("owner", "operator", "tech"):
                    self._reply(422, {"error": "role must be one of: owner, operator, tech"})
                    return
                topic = store.org_role_topic(tenant, role)
                if not topic:
                    self._reply(422, {"error": f"no {role} channel configured — set it in "
                                                 "Settings first"})
                    return
                res = notifier.send(topic, "✅ WISP Central test alert",
                                    f"This is a test alert for {tenant}'s {role} channel.", 3)
                self._reply(200, {"ok": res.ok, "detail": res.detail, "channel": notifier.channel,
                                  "recipient": topic, "role": role})
                return
            # device inventory (the org's topology; owner of that org, or superadmin)
            if route == "/api/inventory":
                tenant = body.get("tenant_id") or user["tenant_id"]
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_device_payload(
                    body, parents=store.org_device_parent_map(tenant), device_id=None)
                did = store.create_org_device(tenant, clean)
                self._reply(200, {"id": did})
                return
            if route == "/api/inventory/update":
                did = int(body.get("id") or 0)
                tenant = store.device_tenant(did)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                parents = store.org_device_parent_map(tenant)
                clean = inventory.clean_device_payload(body, parents=parents, device_id=did)
                ok = store.update_org_device(tenant, did, clean)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/delete":
                did = int(body.get("id") or 0)
                tenant = store.device_tenant(did)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                result = store.delete_org_device(tenant, did)
                self._reply(200 if result["ok"] else 409, result)
                return
            if route == "/api/inventory/maintenance":
                did = int(body.get("id") or 0)
                tenant = store.device_tenant(did)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.set_org_device_maintenance(tenant, did, bool(body.get("on")))
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/snmp":
                did = int(body.get("id") or 0)
                tenant = store.device_tenant(did)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_snmp_payload(body)
                ok = store.set_org_device_snmp(tenant, did, clean)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # SNMP port folding config (central/ports.py): which discovered ports
            # actually alarm, and which downstream device a monitored port feeds.
            if route == "/api/inventory/ports/monitored":
                pid = int(body.get("id") or 0)
                tenant = store.switch_port_tenant(pid)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.set_port_monitored(tenant, pid, bool(body.get("on")))
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/ports/feeds":
                pid = int(body.get("id") or 0)
                tenant = store.switch_port_tenant(pid)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                feeds_raw = body.get("feeds_device_id")
                feeds = None
                if feeds_raw not in (None, "", "null"):
                    try:
                        feeds = int(feeds_raw)
                    except (TypeError, ValueError):
                        self._reply(422, {"error": "feeds_device_id must be a number"})
                        return
                    if store.device_tenant(feeds) != tenant:
                        self._reply(422, {"error": "feeds device must belong to the same org"})
                        return
                ok = store.set_port_feeds(tenant, pid, feeds)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/ports/bandwidth":
                pid = int(body.get("id") or 0)
                tenant = store.switch_port_tenant(pid)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_port_bandwidth_payload(body)
                ok = store.set_port_bandwidth_config(
                    tenant, pid, clean["threshold_mbps"], clean["direction"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # graph topology: backup (redundancy) parent edges (CLAUDE.md item 3)
            if route == "/api/inventory/links":
                child_id = int(body.get("child_id") or 0)
                parent_id = int(body.get("parent_id") or 0)
                tenant = store.device_tenant(child_id)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                if store.device_tenant(parent_id) != tenant:
                    self._reply(422, {"error": "backup parent must belong to the same org"})
                    return
                parents = store.org_device_parent_map(tenant)
                backups = store.org_device_backup_map(tenant)
                inventory.clean_backup_link(child_id, parent_id, parents=parents,
                                            backups=backups)
                store.create_backup_link(tenant, child_id, parent_id)
                self._reply(200, {"ok": True})
                return
            if route == "/api/inventory/links/delete":
                child_id = int(body.get("child_id") or 0)
                parent_id = int(body.get("parent_id") or 0)
                tenant = store.device_tenant(child_id)
                if not self._can_write(user, tenant):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.delete_backup_link(tenant, child_id, parent_id)
                self._reply(200 if ok else 404, {"ok": ok})
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


class _TLSThreadingHTTPServer(ThreadingHTTPServer):
    """Wraps each accepted socket in TLS inside its own worker thread rather than the
    shared accept loop — `ThreadingMixIn.process_request_thread` calls `finish_request`
    (this override) already off-thread, so one client's slow/failed handshake can't
    stall new connections. A handshake failure raises here and is caught by
    `ThreadingMixIn`'s own per-request try/except (`handle_error` + `shutdown_request`),
    same as any other request exception — it never takes the server down."""

    def __init__(self, addr, handler, ssl_context: ssl.SSLContext) -> None:
        super().__init__(addr, handler)
        self._ssl_context = ssl_context

    def finish_request(self, request, client_address) -> None:
        request = self._ssl_context.wrap_socket(request, server_side=True)
        self.RequestHandlerClass(request, client_address, self)

    def handle_error(self, request, client_address) -> None:
        # A bad/aborted TLS handshake (a port scanner, a stale client, a rejected cert)
        # is routine noise on an ingest port exposed to the internet — log it quietly
        # instead of dumping a traceback, but let any OTHER exception (a real bug) fall
        # through to the base class's default (loud) handling so it's not hidden.
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            log.debug("TLS handshake with %s failed: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def _build_tls_context(cfg: Config) -> ssl.SSLContext | None:
    """None ⇒ plain HTTP (both cert/key unset — the default, fully backward compatible).
    Otherwise central terminates TLS itself; if `central_client_ca` is also set, a
    presented client cert is verified against it (CERT_OPTIONAL — a cert is requested
    but not required, since dashboard browsers and not-yet-migrated edges have none)."""
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
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def serve(cfg: Config = CONFIG) -> None:
    if not cfg.central_token and not cfg.central_client_ca:
        log.warning("neither WISP_CENTRAL_TOKEN nor WISP_CENTRAL_CLIENT_CA is set — ingest is "
                    "UNAUTHENTICATED. Set a token and/or enroll edges with mTLS "
                    "(central.admin init-ca / enroll-edge) before exposing central beyond a "
                    "trusted network.")
    httpd = make_server(cfg)
    from wisp.central.watchdog import start_central_watchdog_thread
    start_central_watchdog_thread(cfg, httpd.store)  # type: ignore[attr-defined]
    central_rollup.start_central_rollup_prune_thread(cfg, httpd.store)  # type: ignore[attr-defined]
    if not httpd.store.list_users():  # type: ignore[attr-defined]
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
