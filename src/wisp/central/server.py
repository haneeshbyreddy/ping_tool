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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import auth, inventory, pki, sysinfo
from wisp.central import analytics as central_analytics
from wisp.central import engine as central_engine
from wisp.central import perf as central_perf
from wisp.central import redundancy as central_redundancy
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.engine import EngineRegistry
from wisp.central.ports import CentralPortMonitor
from wisp.central.optics import CentralOpticsMonitor
from wisp.central import rollup as central_rollup
from wisp.central.store import CentralStore
from wisp.egress.notifiers import build_notifier
from wisp.ingress.probers import PingResult
from wisp.runtime.central_client import WIRE_V
from wisp.central.auth import LoginThrottle
from wisp.version import is_newer

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
            if route == "/healthz":
                self._reply(200, {"ok": True, "counts": store.counts()})
                return
            if route.startswith("/download/"):
                if not self._serve_release(route):
                    self._reply(404, {"error": "not found"})
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
                                  "low_bandwidth": store.low_bandwidth_alarms(org),
                                  "high_bandwidth": store.high_bandwidth_alarms(org)})
                return
            if route == "/api/system":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                if not user["is_superadmin"]:
                    self._reply(403, {"error": "forbidden"})
                    return
                doc = sysinfo.snapshot(cfg.central_db)
                # Monitor-the-monitor: a dead release mirror stalls fleet
                # self-updates, so its health rides the superadmin box-stats card.
                doc["release_sync"] = store.release_sync_status()
                releases = store.list_releases()
                doc["latest_release"] = releases[0]["version"] if releases else None
                self._reply(200, doc)
                return
            if route == "/api/admin/overview":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                if not user["is_superadmin"]:
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, store.admin_overview())
                return
            if route == "/api/events":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                self._serve_events(org)
                return
            if route == "/edge/devices":
                org = (qs.get("org_id") or [None])[0]
                if not org:
                    self._reply(400, {"error": "org_id required"})
                    return
                if not self._ingest_ok(org):
                    self._reply(401, {"error": "unauthorized"})
                    return
                devices = store.org_device_topology(org)
                node = (qs.get("node_id") or [None])[0]
                if node:
                    devices = [d for d in devices if d.get("assigned_node_id") == node]
                else:
                    devices = [d for d in devices if d.get("assigned_node_id")]
                self._reply(200, {"devices": devices, "canary_ip": cfg.canary_ip,
                                  "snmp_profiles": store.snmp_profiles_for_edge(org)})
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
            if route == "/api/inventory/optics":
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
                dev = store.get_org_device(org, did) or {}
                self._reply(200, {
                    "onus": store.list_onu_optics(org, did),
                    "olt": store.get_olt_optics(org, did),
                    "warn_dbm": dev.get("optical_warn_dbm") if dev.get("optical_warn_dbm") is not None else cfg.optical_warn_dbm,
                    "crit_dbm": dev.get("optical_crit_dbm") if dev.get("optical_crit_dbm") is not None else cfg.optical_crit_dbm,
                })
                return
            if route == "/api/inventory/snmp-walks":
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
                self._reply(200, {"walks": store.list_snmp_walks(org, did)})
                return
            if route == "/api/inventory/snmp-walk/result":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                try:
                    wid = int((qs.get("id") or [None])[0])
                except (TypeError, ValueError):
                    self._reply(400, {"error": "id required"})
                    return
                org = store.snmp_walk_org(wid)
                if org is None or not (user["is_superadmin"] or user["org_id"] == org):
                    self._reply(403, {"error": "forbidden"})
                    return
                self._reply(200, {"walk": store.get_snmp_walk(org, wid)})
                return
            if route == "/api/snmp-profiles":
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                self._reply(200, {"profiles": store.list_snmp_profiles(org),
                                  "metrics": list(inventory.PROFILE_METRICS),
                                  "decodes": list(inventory.PROFILE_DECODES),
                                  "selects": list(inventory.PROFILE_SELECTS)})
                return
            if route == "/api/inventory/snmp-status":
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
                self._reply(200, {"status": store.device_snmp_status(org, did),
                                  "capability": store.device_capabilities(org, did)})
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
            if route == "/api/inventory/perf/samples":
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
                self._reply(200, {"samples": store.perf_sample_window(org, did)})
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
                days = min(days, central_rollup.RETENTION_DAYS)
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
            if route in ("/api/orgs", "/api/inventory", "/api/team", "/api/attendance",
                         "/api/users", "/api/nodes", "/api/regions"):
                user = self._reader()
                if not user:
                    self._reply(401, {"error": "unauthorized"})
                    return
                org = self._scope_org(user, qs)
                if route == "/api/orgs":
                    orgs = store.orgs()
                    if org:
                        orgs = [o for o in orgs if o["org_id"] == org]
                    self._reply(200, {"orgs": orgs})
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
                elif route == "/api/regions":
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    self._reply(200, {"regions": store.list_regions(org)})
                elif route == "/api/nodes":
                    if not org:
                        self._reply(400, {"error": "org required"})
                        return
                    releases = store.list_releases()
                    self._reply(200, {
                        "nodes": store.list_node_tokens(org),
                        "latest_version": releases[0]["version"] if releases else None,
                        "rollout": store.get_rollout(org),
                    })
                else:
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
                        body = env.get("body", {})
                        store.record_heartbeat(org, node, body)
                        self._reply(200, self._heartbeat_reply(org, node, body))
                    elif route == "/edge/snmp-walk":
                        self._walk_result(org, node, env)
                    else:
                        self._reply(200, self._report(org, env))
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
            try:
                self._dashboard_write(route, user, body)
            except (auth.AuthError, inventory.InventoryError) as exc:
                self._reply(422, {"error": str(exc)})
            except Exception:
                log.exception("dashboard write failed: %s", route)
                self._reply(500, {"error": "internal error"})

        def _walk_result(self, org: str, node: str, env: dict) -> None:
            try:
                walk_id = int(env.get("walk_id"))
            except (TypeError, ValueError):
                self._reply(400, {"error": "walk_id required"})
                return
            error = env.get("error")
            error = str(error)[:500] if error else None
            varbinds = None
            if error is None:
                raw = env.get("varbinds")
                if not isinstance(raw, list):
                    self._reply(400, {"error": "varbinds must be a list"})
                    return
                # Server-side bound regardless of what the edge claims: cap the row
                # count and each value's length so one walk can't bloat the DB.
                varbinds = []
                for pair in raw[:inventory.WALK_CAP_MAX_VARBINDS]:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        continue
                    varbinds.append([str(pair[0])[:256], str(pair[1])[:1024]])
            ok = store.complete_snmp_walk(org, node, walk_id,
                                          varbinds=varbinds, error=error)
            self._reply(200 if ok else 404, {"ok": ok})

        def _heartbeat_reply(self, org: str, node: str, body: dict) -> dict:
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
            ts = env.get("ts") or _now_iso()
            pings = env.get("pings") or {}
            results = {
                ip: PingResult(ip, v.get("latency_ms"),
                              float(v.get("loss_pct", 100.0)), v.get("jitter_ms"))
                for ip, v in pings.items()
            }
            store.touch_node(org, env.get("node_id", ""))
            eng = registry.get(org)
            mode = env.get("mode") or "full"
            if mode == "recheck":
                ip_to_id = {d.ip_address: d.id for d in eng.meta.values()}
                subset = {ip_to_id[ip] for ip in results if ip in ip_to_id}
                cycle = central_engine.run_cycle(store, org, eng, results, ts,
                                                 subset=subset)
            else:
                expected = store.node_expected_ips(org, env.get("node_id", ""))
                cycle = central_engine.run_cycle(store, org, eng, results, ts,
                                                 expected_ips=expected)

            disp = CentralAlertDispatcher(store, org, eng, notifier, cfg)
            disp.dispatch(cycle.events, ts)
            if mode != "recheck":
                disp.sweep(ts)
                self._ingest_ports(org, eng, env.get("ports"), ts)
                self._ingest_optics(org, eng, env.get("optics"), ts)
                self._ingest_health(org, eng, env.get("health"), ts)
                self._ingest_snmp_status(org, eng, env.get("snmp_status"), ts)
                central_rollup.record_cycle(store, org, eng, cycle, results, ts)
                central_perf.record_and_evaluate(store, org, eng, cycle, results, ts,
                                                 notifier, cfg)
                central_redundancy.sweep(store, org, eng, cycle.redundancy,
                                         cycle.states, notifier, ts, cfg)

            reply: dict = {"ok": True}
            recheck = central_engine.compute_recheck(eng, cycle, results, cfg)
            if recheck:
                reply["recheck"] = recheck
            if mode != "recheck":
                # Queued diagnostic walks ride the full-report reply, like update
                # directives ride the heartbeat — the edge never accepts inbound.
                walks = store.pending_snmp_walks(org, env.get("node_id", ""))
                if walks:
                    reply["snmp_walks"] = walks
            return reply

        def _ingest_ports(self, org: str, eng, ports_by_device, ts: str) -> None:
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

        def _ingest_optics(self, org: str, eng, optics_by_device, ts: str) -> None:
            if not optics_by_device:
                return
            monitor = CentralOpticsMonitor(store, org, notifier, cfg)
            for raw_id, onus in optics_by_device.items():
                try:
                    device_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if device_id not in eng.meta or not isinstance(onus, list):
                    continue
                try:
                    monitor.sync_device(device_id, onus, ts)
                except Exception:
                    log.exception("GPON optics fold failed for %s/device=%d", org, device_id)

        def _ingest_health(self, org: str, eng, health_by_device, ts: str) -> None:
            if not health_by_device:
                return
            for raw_id, health in health_by_device.items():
                try:
                    device_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if device_id not in eng.meta or not isinstance(health, dict):
                    continue
                try:
                    store.upsert_device_health(org, device_id, health, ts)
                except Exception:
                    log.exception("SNMP health fold failed for %s/device=%d", org, device_id)

        def _ingest_snmp_status(self, org: str, eng, status_by_device, ts: str) -> None:
            # Per-device sweep diagnoses ({device: {subsystem: status}}). The store
            # enforces the closed subsystem/state vocabularies and field bounds.
            if not isinstance(status_by_device, dict):
                return
            rows: list[tuple[int, str, dict]] = []
            for raw_id, subsystems in status_by_device.items():
                try:
                    device_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if device_id not in eng.meta or not isinstance(subsystems, dict):
                    continue
                for subsystem, st in subsystems.items():
                    if isinstance(st, dict):
                        rows.append((device_id, str(subsystem), st))
            if not rows:
                return
            try:
                store.upsert_snmp_statuses(org, rows, ts)
            except Exception:
                log.exception("SNMP status fold failed for %s", org)

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

        def _dashboard_write(self, route: str, user: dict, body: dict):
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
            if route == "/api/nodes/update":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                node_id = inventory.clean_node_id(body.get("node_id"))
                releases = store.list_releases()
                if not releases:
                    raise inventory.InventoryError("no release published yet")
                target = releases[0]["version"]
                node = next((n for n in store.node_versions(org)
                             if n["node_id"] == node_id), None)
                if node is None:
                    raise inventory.InventoryError(
                        f"{node_id!r} has never reported — the update directive rides "
                        "its heartbeat, so there is no channel to deliver it through yet")
                if not is_newer(target, node.get("version")):
                    raise inventory.InventoryError(
                        f"{node_id!r} already runs {node.get('version')} — the latest "
                        f"published release is {target}")
                store.set_rollout(org, target, [node_id],
                                  note=f"manual update via dashboard ({user['username']})")
                self._reply(200, {"ok": True, "target_version": target})
                return
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
            if route == "/api/regions":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                store.add_region(org, inventory.clean_region_name(body.get("name")))
                self._reply(200, {"ok": True})
                return
            if route == "/api/regions/rename":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                old = inventory.clean_region_name(body.get("old"))
                new = inventory.clean_region_name(body.get("new"))
                store.rename_region(org, old, new)
                self._reply(200, {"ok": True})
                return
            if route == "/api/regions/delete":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                result = store.delete_region(
                    org, inventory.clean_region_name(body.get("name")))
                self._reply(200 if result["ok"] else 409, result)
                return
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
            if route == "/api/outages/clear-postmortems":
                org = user["org_id"] if not user["is_superadmin"] else (body.get("org") or None)
                if not org:
                    self._reply(400, {"error": "org is required"})
                    return
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                cause = (str(body.get("root_cause") or "").strip()
                         or "Bulk cleared — no post-mortem recorded")
                n = store.clear_pending_postmortems(org, cause)
                self._reply(200, {"ok": True, "cleared": n})
                return
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
            if route == "/api/org":
                org = body.get("org_id") or user["org_id"]
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                map_region = body.get("map_region")
                if map_region is not None:
                    map_region = str(map_region).strip().lower()[:64] or None
                store.set_org(org, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                              ntfy_topic_owner=body.get("ntfy_topic_owner"),
                              ntfy_topic_operator=body.get("ntfy_topic_operator"),
                              ntfy_topic_tech=body.get("ntfy_topic_tech"),
                              map_region=map_region)
                self._reply(200, {"ok": True})
                return
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
            if route == "/api/inventory/location":
                did = int(body.get("id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                loc = inventory.clean_location_payload(body)
                ok = store.set_org_device_location(org, did, loc["lat"], loc["lng"])
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
            if route == "/api/inventory/capability":
                clean = inventory.clean_capability_payload(body)
                org = store.device_org(clean["device_id"])
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                ok = store.set_device_capability(
                    org, clean["device_id"], clean["subsystem"], clean["supported"],
                    clean["note"], updated_by=user["username"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/snmp-walk":
                did = int(body.get("device_id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                device = store.get_org_device(org, did)
                if not device:
                    self._reply(404, {"error": "device not found"})
                    return
                if not device.get("snmp_enabled") or not device.get("snmp_community"):
                    raise inventory.InventoryError(
                        "enable SNMP (with a community) on this device first")
                node = device.get("assigned_node_id")
                if not node:
                    raise inventory.InventoryError(
                        "assign this device to a probe first — the walk runs from "
                        "its assigned node")
                clean = inventory.clean_walk_payload(body)
                wid = store.create_snmp_walk(org, did, node, clean["root_oid"],
                                             clean["max_varbinds"],
                                             requested_by=user["username"])
                self._reply(200, {"id": wid})
                return
            if route == "/api/snmp-profiles":
                clean = inventory.clean_profile_payload(body)
                # org_id NULL = a GLOBAL profile every org's edges receive —
                # superadmin only. An org owner creates org-local ones.
                if user["is_superadmin"]:
                    org = body.get("org_id") or None
                else:
                    org = user["org_id"]
                if org is not None and not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                pid = store.create_snmp_profile(org, clean)
                self._reply(200, {"id": pid})
                return
            if route in ("/api/snmp-profiles/update", "/api/snmp-profiles/delete"):
                profile = store.get_snmp_profile(int(body.get("id") or 0))
                if not profile:
                    self._reply(404, {"error": "profile not found"})
                    return
                org = profile["org_id"]
                allowed = (user["is_superadmin"] if org is None
                           else self._can_write(user, org))
                if not allowed:
                    self._reply(403, {"error": "forbidden"})
                    return
                if route.endswith("/delete"):
                    ok = store.delete_snmp_profile(profile["id"])
                else:
                    clean = inventory.clean_profile_payload(body)
                    ok = store.update_snmp_profile(profile["id"], clean)
                self._reply(200 if ok else 404, {"ok": ok})
                return
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
                    org, pid, clean["threshold_mbps"], clean["direction"],
                    clean["max_mbps"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/optics/ack":
                onu_id = int(body.get("id") or 0)
                org = store.onu_optics_org(onu_id)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                until = inventory.clean_ack_until(body)
                ok = store.set_onu_ack(org, onu_id, until)
                self._reply(200 if ok else 404, {"ok": ok})
                return
            if route == "/api/inventory/optics/thresholds":
                did = int(body.get("device_id") or 0)
                org = store.device_org(did)
                if not self._can_write(user, org):
                    self._reply(403, {"error": "forbidden"})
                    return
                clean = inventory.clean_optical_thresholds(body)
                ok = store.set_olt_optical_thresholds(
                    org, did, clean["warn_dbm"], clean["crit_dbm"])
                self._reply(200 if ok else 404, {"ok": ok})
                return
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
    if worker_id is None:
        return None
    with store._connect() as conn:
        row = conn.execute("SELECT org_id FROM org_workers WHERE id=?",
                           (int(worker_id),)).fetchone()
    return row["org_id"] if row else None

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
