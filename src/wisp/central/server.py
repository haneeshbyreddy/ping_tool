"""Central ingest server (Phase 10 Part A skeleton) — pure stdlib http.server.

Endpoints (all JSON):
    GET  /healthz   — unauthed liveness + row counts (for load balancers / probes)
    POST /ingest    — a batch envelope of events/rollups; returns {"accepted":[edge_ids]}
    POST /heartbeat — a node's liveness/health beat; returns {"ok":true}
    GET  /api/fleet — the aggregated read view (nodes + recent events)

Auth is a bearer token (the Part A stopgap, decision: static token now, mTLS in Part C).
The same `Authorization: Bearer <token>` the edge shipper sends; compared in constant time.
A versioned envelope is accepted across a window of `v` so a staged fleet rollout (mixed
edge versions) never breaks ingest. This is deliberately a skeleton — the multi-tenant
dashboard + per-org auth are Part C; here central is a mirror with one fleet view.
"""
from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wisp.config import CONFIG, Config
from wisp.central.store import CentralStore
from wisp.egress.shipper import WIRE_V

log = logging.getLogger("wisp.central")

# Accept this protocol version and anything older (forward-compatible ingest during a
# rollout). A newer-than-known `v` is rejected so we never silently mis-read a future shape.
MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024   # 16 MiB ceiling on one POST, so a bad client can't OOM us


def _make_handler(cfg: Config, store: CentralStore):
    token = cfg.central_token

    class Handler(BaseHTTPRequestHandler):
        server_version = "wisp-central"

        def log_message(self, fmt, *args):  # route access logs through logging, quietly
            log.debug("%s - %s", self.address_string(), fmt % args)

        # --- helpers ---
        def _reply(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return None
            if length <= 0 or length > _MAX_BODY:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except Exception:
                return None

        def _authed(self) -> bool:
            """Constant-time bearer check. If central has no token configured, ingest is
            open (single-tenant dev / trusted LAN) — fine for the skeleton, but logged."""
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            prefix = "Bearer "
            presented = got[len(prefix):] if got.startswith(prefix) else ""
            return hmac.compare_digest(presented, token)

        def _envelope(self) -> dict | None:
            """Read + validate the wire envelope. None on any rejection (already replied)."""
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

        # --- routing ---
        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, {"ok": True, "counts": store.counts()})
                return
            if self.path == "/api/fleet":
                if not self._authed():
                    self._reply(401, {"error": "unauthorized"})
                    return
                self._reply(200, store.fleet())
                return
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/ingest", "/heartbeat"):
                # Drain the body so a keep-alive socket isn't corrupted for the next request.
                self._read_body()
                self._reply(404, {"error": "not found"})
                return
            if not self._authed():
                self._read_body()
                self._reply(401, {"error": "unauthorized"})
                return
            env = self._envelope()
            if env is None:
                return
            tenant, node = env["tenant_id"], env["node_id"]
            try:
                if self.path == "/ingest":
                    accepted = store.ingest(tenant, node, env.get("records", []))
                    self._reply(200, {"accepted": accepted})
                else:
                    store.record_heartbeat(tenant, node, env.get("body", {}))
                    self._reply(200, {"ok": True})
            except Exception:
                log.exception("ingest failed for %s/%s", tenant, node)
                self._reply(500, {"error": "internal error"})

    return Handler


def make_server(cfg: Config = CONFIG, store: CentralStore | None = None) -> ThreadingHTTPServer:
    store = store or CentralStore(cfg.central_db)
    handler = _make_handler(cfg, store)
    httpd = ThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler)
    httpd.store = store  # type: ignore[attr-defined]  (handy for tests/introspection)
    return httpd


def serve(cfg: Config = CONFIG) -> None:
    if not cfg.central_token:
        log.warning("WISP_CENTRAL_TOKEN is empty — ingest is UNAUTHENTICATED. Set a token "
                    "before exposing central beyond a trusted network.")
    httpd = make_server(cfg)
    log.info("central ingest listening on %s:%d (db=%s)",
             cfg.central_bind, cfg.central_port, cfg.central_db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
