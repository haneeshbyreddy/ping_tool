"""Central server (Phase 10 Parts A–C; Phase B raw-report ingest) — ingest + a
multi-org dashboard, pure stdlib.

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
    **scoped to the caller's org**; a superadmin sees all orgs and may pass `?org=`.

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
            """The (org_id, node_id) a verified mTLS client cert claims, or None if
            this connection is plain HTTP / presented no cert / the cert's CN isn't in
            our `org:node` shape. `self.connection` is only an `ssl.SSLSocket` (with
            `getpeercert`) when `make_server` wrapped the listener in TLS."""
            getpeercert = getattr(self.connection, "getpeercert", None)
            if getpeercert is None:
                return None
            return pki.peer_identity(getpeercert())

        def _node_token_identity(self) -> tuple[str, str] | None:
            """The (org_id, node_id) a presented bearer authenticates as via a
            dashboard-issued self-service credential (`POST /api/nodes`, `central/
            store.py`'s `node_tokens`) — same derive-identity-from-the-credential
            discipline as `_peer_identity()`, never trust the envelope's claimed
            org/node alone."""
            presented = self._presented_bearer()
            return store.resolve_node_token(presented) if presented else None

        def _ingest_ok(self, org: str, node: str | None = None) -> bool:
            """Ingest auth: the global bearer token, OR a self-service per-node token
            claiming this org (and node, when known), OR a verified mTLS client cert
            claiming the same (GET /edge/devices has no node in its query, so that check
            is org-only there) — any one of the three satisfies it. If NONE of the
            three is configured/registered at all, ingest stays open (today's trusted-
            network default). But a node that HAS its own registered self-service
            credential is gated on presenting it regardless of whether the global token
            or mTLS are configured — otherwise self-service registration would be
            security theatre on a deployment that never set either of those up."""
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
            """The principal allowed to READ: a logged-in human, OR — for curl/automation —
            the configured bearer token, treated as a cross-org machine superadmin. Writes
            never accept the token (they go through real accounts); the token reads only."""
            user = self._user()
            if user:
                return user
            if token and self._bearer_ok():
                return {"id": 0, "username": "token", "org_id": None,
                        "role": "superadmin", "is_superadmin": True}
            return None

        def _scope_org(self, user: dict, qs: dict) -> str | None:
            """The org a request is allowed to read: an org user is pinned to their own
            org; a superadmin sees all (None) or narrows with ?org=."""
            if not user["is_superadmin"]:
                return user["org_id"]
            return (qs.get("org") or [None])[0]

        @staticmethod
        def _can_write(user: dict, org: str | None) -> bool:
            if user["is_superadmin"]:
                return True
            return user["role"] == "owner" and user["org_id"] == org

        def _envelope(self, body: dict) -> dict | None:
            """Shape-validate an already-read ingest body (see do_POST — auth needs
            `org_id`/`node_id` out of the body first, so reading happens before this)."""
            v = body.get("v")
            if not isinstance(v, int) or v > MAX_WIRE_V:
                self._reply(400, {"error": f"unsupported envelope version {v!r}"})
                return None
            if not body.get("org_id") or not body.get("node_id"):
                self._reply(400, {"error": "missing org_id/node_id"})
                return None
            return body

        # --- live push (Server-Sent Events) ---
        def _serve_events(self, org: str | None) -> None:
            """SSE stream: emit a `changed` event whenever `store.data_version(org)`
            moves, so the dashboard updates the instant an edge reports or an SNMP walk
            lands — no client-side polling. Mirrors the old single-box dashboard's
            `_serve_events` one-for-one; scoped to the caller's org (or every org,
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
                    version = store.data_version(org)
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
            # No cache headers at all lets browsers cache heuristically (seen in practice:
            # a stale app.js survived a plain reload after a dashboard fix) — force revalidation.
            self.send_header("Cache-Control", "no-cache")
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
                self._reply(200, {"user": _public_user(user, store),
                                  "channels": {"central": cfg.central_ntfy_topic}})
                return
            if route == "/api/summary":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if not org:
                    self._reply(400, {"error": "org required"})
                    return
                self._reply(200, {"uplink_down": store.uplink_active(org),
                                  "low_bandwidth": store.low_bandwidth_alarms(org)})
                return
            if route == "/api/events":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                self._serve_events(org)
                return
            # Ingest plane (bearer token): what should this edge probe? Phase B — the
            # edge's device list now comes from the ISP-managed org_devices topology
            # (Phase A), not a local dashboard.
            if route == "/edge/devices":
                org = (qs.get("org_id") or [None])[0]
                if not org:
                    self._reply(400, {"error": "org_id required"})
                    return
                if not self._ingest_ok(org):
                    self._reply(401, {"error": "unauthorized"})
                    return
                devices = store.org_device_topology(org)
                # Device assignment (CLAUDE.md's multi-edge-per-org feature): a node
                # only needs to know what IT should probe — unassigned devices (every
                # node's concern, the default) plus whatever's explicitly assigned to
                # it. Omitting node_id (older/misconfigured client) keeps today's
                # unfiltered behavior rather than 400ing.
                node = (qs.get("node_id") or [None])[0]
                if node:
                    devices = [d for d in devices
                              if d.get("assigned_node_id") in (None, node)]
                self._reply(200, {"devices": devices, "canary_ip": cfg.canary_ip})
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
                org = store.device_org(did)
                if org is None or not (user["is_superadmin"] or user["org_id"] == org):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"ports": store.list_switch_ports(org, did)})
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
                org = store.device_org(did)
                if org is None or not (user["is_superadmin"] or user["org_id"] == org):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"redundancy": store.device_redundancy_state(org, did)})
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
                org = store.device_org(did)
                if org is None or not (user["is_superadmin"] or user["org_id"] == org):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"perf": store.device_perf_state(org, did)})
                return
            if route == "/api/analytics":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if not org:
                    self._reply(400, {"error": "org required"})
                    return
                try:
                    days = int((qs.get("days") or [30])[0])
                except (TypeError, ValueError):
                    days = 30
                since, until = central_analytics.window(days)
                self._reply(200, {"since": since, "until": until,
                                  "devices": central_analytics.device_reliability(
                                      store, org, since, until)})
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
                org = store.device_org(did)
                if org is None or not (user["is_superadmin"] or user["org_id"] == org):
                    self._reply(403, {"error": "forbidden"})
                    return
                try:
                    days = int((qs.get("days") or [7])[0])
                except (TypeError, ValueError):
                    days = 7
                days = min(days, central_rollup.RETENTION_DAYS)   # nothing older survives
                since, until = central_analytics.window(days)
                self._reply(200, {"since": since, "until": until,
                                  "buckets": store.device_rollup_series(org, did, since, until)})
                return
            if route == "/api/outages":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if not org:
                    self._reply(400, {"error": "org required"})
                    return
                self._reply(200, {"outages": store.triage_outages(org)})
                return
            if route == "/api/logs":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if not org:
                    self._reply(400, {"error": "org required"})
                    return
                try:
                    limit = int((qs.get("limit") or [100])[0])
                except (TypeError, ValueError):
                    limit = 100
                before_raw = (qs.get("before") or [None])[0]
                try:
                    before_id = int(before_raw) if before_raw is not None else None
                except ValueError:
                    before_id = None
                self._reply(200, {"events": store.list_events(org, limit, before_id)})
                return
            if route in ("/api/fleet", "/api/orgs", "/api/devices", "/api/inventory",
                         "/api/team", "/api/attendance", "/api/users", "/api/nodes"):
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if route == "/api/fleet":
                    self._reply(200, store.fleet(org_id=org))
                elif route == "/api/orgs":
                    # store.orgs() is cross-org by nature (it's the org directory) — an
                    # org user must only ever see their OWN org's row (name/topics included),
                    # never another org's. `org` here is already pinned for org users
                    # (_scope_org) and only None for a superadmin with no ?org=.
                    orgs = store.orgs()
                    if org:
                        orgs = [o for o in orgs if o["org_id"] == org]
                    self._reply(200, {"orgs": orgs})
                elif route == "/api/devices":
                    self._reply(200, {"devices": store.devices(org_id=org)})
                elif route == "/api/users":
                    if not user["is_superadmin"] and user["role"] != "owner":
                        self._reply(403, {"error": "forbidden"})
                        return
                    self._reply(200, {"users": store.list_users(org_id=org)})
                elif route == "/api/team":
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    self._reply(200, {"team": store.list_workers(org)})
                elif route == "/api/inventory":
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    self._reply(200, {"devices": store.list_org_devices(org)})
                elif route == "/api/nodes":
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    self._reply(200, {"nodes": store.list_node_tokens(org)})
                else:  # /api/attendance
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    self._reply(200, store.attendance_overview(org))
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
                # Auth needs the CLAIMED org/node to check a presented cert against —
                # read before full shape validation so a missing field still 401s (not
                # 400s) when auth is what actually failed, same precedence as before mTLS.
                if not self._ingest_ok(body.get("org_id"), body.get("node_id")):
                    self._reply(401, {"error": "unauthorized"})
                    return
                env = self._envelope(body)
                if env is None:
                    return
                org, node = env["org_id"], env["node_id"]
                try:
                    if route == "/ingest":
                        self._reply(200, {"accepted": store.ingest(org, node,
                                                                   env.get("records", []))})
                    elif route == "/heartbeat":
                        body = env.get("body", {})
                        store.record_heartbeat(org, node, body)
                        self._reply(200, self._heartbeat_reply(org, node, body))
                    else:
                        self._reply(200, self._report(org, env))
                except Exception:
                    log.exception("ingest failed for %s/%s", org, node)
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

        def _heartbeat_reply(self, org: str, node: str, body: dict) -> dict:
            """The heartbeat reply doubles as the update channel (Part D): advance the org's
            rollout and, if this node is due a newer version, hand it the signed directive."""
            reply: dict = {"ok": True}
            try:
                from wisp.central import rollout
                rollout.evaluate(store, org, cfg=cfg)
                directive = rollout.directive_for(store, org, node, body.get("version"),
                                                  body.get("platform"))
                if directive:
                    reply["update"] = directive
            except Exception:
                log.exception("rollout directive failed for %s/%s", org, node)
            return reply

        def _report(self, org: str, env: dict) -> dict:
            """Phase B — one raw-ping report from an edge: run that org's
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
            # Central's own clock, not the edge-reported `ts` — keeps last_seen immune
            # to edge clock drift, same as record_heartbeat/ingest's `now`.
            store.touch_node(org, env.get("node_id", ""))
            eng = registry.get(org)
            mode = env.get("mode") or "full"
            if mode == "recheck":
                ip_to_id = {d.ip_address: d.id for d in eng.meta.values()}
                subset = {ip_to_id[ip] for ip in results if ip in ip_to_id}
                cycle = central_engine.run_cycle(store, org, eng, results, ts,
                                                 subset=subset)
            else:
                # Device assignment: this report only speaks for the reporting node's
                # own devices (assigned-to-it + unassigned) — see
                # MonitorEngine.process_cycle's expected_ips docstring for why a device
                # assigned elsewhere must be skipped, not scored 100% loss, when it's
                # absent from THIS report.
                expected = store.node_expected_ips(org, env.get("node_id", ""))
                cycle = central_engine.run_cycle(store, org, eng, results, ts,
                                                 expected_ips=expected)

            disp = CentralAlertDispatcher(store, org, eng, notifier, cfg)
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
                self._ingest_ports(org, eng, env.get("ports"), ts)
                # Hourly latency/loss trend rollup (CLAUDE.md item 2, second slice) — full
                # reports only, so a recheck's rapid re-probe of a suspect subset never
                # skews an hour's average.
                central_rollup.record_cycle(store, org, eng, cycle, results, ts)
                # Per-link performance baseline (CLAUDE.md item 3) — same full-report-only
                # gating; a recheck's suspect subset isn't a meaningful perf sample.
                central_perf.record_and_evaluate(store, org, eng, cycle, results, ts,
                                                 notifier, cfg)
                # On-backup redundancy signal (CLAUDE.md item 3) — cycle.redundancy is
                # only ever populated on a full pass (see MonitorEngine.process_cycle),
                # so this is a no-op on a recheck even without the mode gate above.
                central_redundancy.sweep(store, org, eng, cycle.redundancy,
                                         cycle.states, notifier, ts, cfg)

            reply: dict = {"ok": True}
            recheck = central_engine.compute_recheck(eng, cycle, results, cfg)
            if recheck:
                reply["recheck"] = recheck
            return reply

        def _ingest_ports(self, org: str, eng, ports_by_device, ts: str) -> None:
            """Fold each reported switch's port readings. `ports_by_device` is
            {"<device_id>": [port dict, ...]} (JSON object keys are always strings on
            the wire). A device id not in THIS org's engine meta is ignored rather
            than trusted from the body — the same re-derive-org-from-what-we-already-
            know discipline `org_devices` writes use, so org A can't attribute a port
            reading to org B's device id."""
            if not ports_by_device:
                return
            monitor = CentralPortMonitor(store, org, notifier, cfg)
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
                    log.exception("SNMP port fold failed for %s/device=%d", org, device_id)

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
            self._reply(200, {"user": _public_user(user, store)}, cookie=cookie)

        # --- dashboard writes (owner / superadmin) ---
        def _dashboard_write(self, route: str, user: dict, body: dict):
            # self-service node (edge) enrollment
            if route == "/api/nodes":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                if store.get_node_token_status(org, node_id):
                    raise inventory.InventoryError(
                        f"node {node_id!r} is already registered for {org!r} — "
                        "use rotate instead of registering it again")
                node_token = store.issue_node_token(org, node_id, created_by=user["id"])
                self._reply(200, {"node_id": node_id, "token": node_token})
                return
            if route == "/api/nodes/rotate":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                if not store.get_node_token_status(org, node_id):
                    raise inventory.InventoryError(
                        f"node {node_id!r} isn't registered for {org!r} yet")
                node_token = store.issue_node_token(org, node_id, created_by=user["id"])
                self._reply(200, {"node_id": node_id, "token": node_token})
                return
            if route == "/api/nodes/revoke":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                ok = store.revoke_node_token(org, node_id)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/nodes/delete":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                ok = store.delete_node_token(org, node_id)
                if ok:
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {"ok": False, "error": f"{node_id!r} isn't registered"})
                return
            # team
            if route == "/api/team":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                wid = store.add_worker(org, body["name"], body.get("role", "operator"),
                                       body.get("region"), body.get("notes"))
                self._reply(200, {"id": wid})
                return
            if route == "/api/team/update":
                w = _worker_org(store, body.get("id"))
                if not self._can_write(user, w):
                    self._reply(403, {"error": "forbidden"})
                    return
                fields = {k: body[k] for k in ("name", "role", "region", "notes") if k in body}
                store.update_worker(int(body["id"]), **fields)
                self._reply(200, {"ok": True})
                return
            if route == "/api/team/delete":
                w = _worker_org(store, body.get("id"))
                if not self._can_write(user, w):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.delete_worker(int(body["id"]))
                self._reply(200, {"ok": True})
                return
            if route == "/api/attendance":
                w = _worker_org(store, body.get("worker_id"))
                if not self._can_write(user, w):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.set_attendance(w, int(body["worker_id"]), bool(body.get("present")),
                                     body.get("day"))
                self._reply(200, {"ok": True})
                return
            # outage triage: acknowledge (open only) / post-mortem (resolved only) —
            # org is re-derived from the outage row, never trusted from the body
            # (same discipline as device_org/switch_port_org/_worker_org).
            if route == "/api/outages/acknowledge":
                oid = int(body.get("outage_id") or 0)
                org = store.outage_org(oid)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.acknowledge_outage(org, oid, user["username"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/outages/postmortem":
                oid = int(body.get("outage_id") or 0)
                org = store.outage_org(oid)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                cause = str(body.get("root_cause") or "").strip()
                if not cause:
                    self._reply(422, {"error": "root_cause is required"})
                    return
                notes = str(body.get("resolution_notes") or "").strip() or None
                ok = store.set_outage_postmortem(org, oid, cause, notes)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # org creation — superadmin only, since a brand-new org_id isn't yet
            # anyone's own org for `_can_write`'s owner branch to match against.
            if route == "/api/orgs":
                if not user["is_superadmin"]:
                    self._reply(403, {"error": "forbidden"})
                    return
                org = inventory.clean_org_id(body.get("org_id"))
                if store.org_exists(org):
                    self._reply(409, {"error": f"org {org!r} already exists"})
                    return
                store.set_org(org, name=body.get("name"))
                self._reply(200, {"org_id": org})
                return
            # org rename / topics (owner of that org, or superadmin)
            if route == "/api/org":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.set_org(org, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                              ntfy_topic_owner=body.get("ntfy_topic_owner"),
                              ntfy_topic_operator=body.get("ntfy_topic_operator"),
                              ntfy_topic_tech=body.get("ntfy_topic_tech"))
                self._reply(200, {"ok": True})
                return
            # send a test push to one of an org's three role channels (Settings go-live check)
            if route == "/api/test-alert":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                role = str(body.get("role") or "").strip().lower()
                if role not in ("owner", "operator", "tech"):
                    self._reply(422, {"error": "role must be one of: owner, operator, tech"})
                    return
                topic = store.org_role_topic(org, role)
                if not topic:
                    self._reply(422, {"error": f"no {role} channel configured — set it in "
                                                 "Settings first"})
                    return
                res = notifier.send(topic, "✅ WISP Central test alert",
                                    f"This is a test alert for {org}'s {role} channel.", 3)
                self._reply(200, {"ok": res.ok, "detail": res.detail, "channel": notifier.channel,
                                  "recipient": topic, "role": role})
                return
            # device inventory (the org's topology; owner of that org, or superadmin)
            if route == "/api/inventory":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_device_payload(
                    body, parents=store.org_device_parent_map(org), device_id=None,
                    registered_nodes=store.registered_node_ids(org))
                did = store.create_org_device(org, clean)
                self._reply(200, {"id": did})
                return
            if route == "/api/inventory/update":
                did = int(body.get("id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                parents = store.org_device_parent_map(org)
                clean = inventory.clean_device_payload(
                    body, parents=parents, device_id=did,
                    registered_nodes=store.registered_node_ids(org))
                ok = store.update_org_device(org, did, clean)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/delete":
                did = int(body.get("id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                result = store.delete_org_device(org, did)
                self._reply(200 if result["ok"] else 409, result)
                return
            if route == "/api/inventory/maintenance":
                did = int(body.get("id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.set_org_device_maintenance(org, did, bool(body.get("on")))
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/snmp":
                did = int(body.get("id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_snmp_payload(body)
                ok = store.set_org_device_snmp(org, did, clean)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # SNMP port folding config (central/ports.py): which discovered ports
            # actually alarm, and which downstream device a monitored port feeds.
            if route == "/api/inventory/ports/monitored":
                pid = int(body.get("id") or 0)
                org = store.switch_port_org(pid)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.set_port_monitored(org, pid, bool(body.get("on")))
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/ports/feeds":
                pid = int(body.get("id") or 0)
                org = store.switch_port_org(pid)
                if not self._can_write(user, org):
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
                    if store.device_org(feeds) != org:
                        self._reply(422, {"error": "feeds device must belong to the same org"})
                        return
                ok = store.set_port_feeds(org, pid, feeds)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/ports/bandwidth":
                pid = int(body.get("id") or 0)
                org = store.switch_port_org(pid)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_port_bandwidth_payload(body)
                ok = store.set_port_bandwidth_config(
                    org, pid, clean["threshold_mbps"], clean["direction"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # graph topology: backup (redundancy) parent edges (CLAUDE.md item 3)
            if route == "/api/inventory/links":
                child_id = int(body.get("child_id") or 0)
                parent_id = int(body.get("parent_id") or 0)
                org = store.device_org(child_id)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                if store.device_org(parent_id) != org:
                    self._reply(422, {"error": "backup parent must belong to the same org"})
                    return
                parents = store.org_device_parent_map(org)
                backups = store.org_device_backup_map(org)
                inventory.clean_backup_link(child_id, parent_id, parents=parents,
                                            backups=backups)
                store.create_backup_link(org, child_id, parent_id)
                self._reply(200, {"ok": True})
                return
            if route == "/api/inventory/links/delete":
                child_id = int(body.get("child_id") or 0)
                parent_id = int(body.get("parent_id") or 0)
                org = store.device_org(child_id)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.delete_backup_link(org, child_id, parent_id)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            # user provisioning: superadmin anywhere; an owner within their own org
            if route == "/api/users":
                org = body.get("org_id") if user["is_superadmin"] else user["org_id"]
                if not (user["is_superadmin"] or user["role"] == "owner"):
                    self._reply(403, {"error": "forbidden"})
                    return
                uid = auth.create_user(store, org, body.get("username", ""),
                                       body.get("password", ""), body.get("role", "operator"))
                self._reply(200, {"id": uid})
                return
            if route == "/api/users/deactivate":
                if not (user["is_superadmin"] or user["role"] == "owner"):
                    self._reply(403, {"error": "forbidden"})
                    return
                target = store.get_user(int(body["id"]))
                if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
                    store.set_user_active(int(body["id"]), bool(body.get("active", False)))
                    self._reply(200, {"ok": True})
                else:
                    self._reply(403, {"error": "forbidden"})
                return
            if route == "/api/users/delete":
                if not (user["is_superadmin"] or user["role"] == "owner"):
                    self._reply(403, {"error": "forbidden"})
                    return
                target_id = int(body.get("id") or 0)
                if target_id == user["id"]:
                    self._reply(422, {"error": "cannot delete your own account"})
                    return
                target = store.get_user(target_id)
                if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
                    store.delete_user(target_id)
                    self._reply(200, {"ok": True})
                else:
                    self._reply(403, {"error": "forbidden"})
                return
            # Self-service (own account, current password required) or an owner/superadmin
            # resetting someone else's (no current password needed — same trust level as
            # deactivate/delete above).
            if route == "/api/users/password":
                target_id = int(body.get("id") or user["id"])
                if target_id == user["id"]:
                    if not auth.verify_login(store, user["username"], body.get("current_password", "")):
                        self._reply(422, {"error": "current password is incorrect"})
                        return
                else:
                    if not (user["is_superadmin"] or user["role"] == "owner"):
                        self._reply(403, {"error": "forbidden"})
                        return
                    target = store.get_user(target_id)
                    if not target or not (user["is_superadmin"] or target["org_id"] == user["org_id"]):
                        self._reply(403, {"error": "forbidden"})
                        return
                auth.set_password(store, target_id, body.get("new_password", ""))
                self._reply(200, {"ok": True})
                return
            self._reply(404, {"error": "not found"})

    return Handler


def _public_user(user: dict, store: CentralStore) -> dict:
    org_name = store.org_name(user["org_id"]) if user["org_id"] else None
    return {"id": user["id"], "username": user["username"], "org_id": user["org_id"],
            "org_name": org_name, "role": user["role"], "is_superadmin": user["org_id"] is None}


def _worker_org(store: CentralStore, worker_id) -> str | None:
    """The org a worker belongs to (so a write is authorized against the right org)."""
    if worker_id is None:
        return None
    with store._connect() as conn:  # read-only; the write that follows takes the lock
        row = conn.execute("SELECT org_id FROM org_workers WHERE id=?",
                           (int(worker_id),)).fetchone()
    return row["org_id"] if row else None


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
