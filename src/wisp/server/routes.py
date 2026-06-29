"""Layer 6 — the operator dashboard server (stdlib only).

The decoupled HTTP layer. It does two jobs:

  * serves the self-contained dashboard UI from `apps/dashboard/` (templates/ +
    static/; no CDN, no build step — Tailwind runtime and Material icons are
    vendored, so it works on a site with no internet), and
  * exposes a read-mostly JSON API over the live SQLite DB via `services.py`, plus
    the write actions (acknowledge/assign, post-mortem, device CRUD).

This module owns routing + serving only; the runnable entry point (host/port,
serve_forever) lives in `apps/dashboard/main.py`. It is intentionally separate
from the polling daemon: the daemon writes, this only reads (and the few writes
go through the same `write_with_retry` path), so they run side by side against
the WAL database.

The dashboard is pure stdlib; the daemon it runs alongside uses the real ICMP
prober + ntfy notifier.
"""
from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.server import services, auth
from wisp.config import CONFIG, PROJECT_ROOT
from wisp.database.client import connect, migrate

DASHBOARD_ROOT = PROJECT_ROOT / "apps" / "dashboard"
STATIC_ROOT = DASHBOARD_ROOT / "static"
TEMPLATES_ROOT = DASHBOARD_ROOT / "templates"
INDEX_HTML = TEMPLATES_ROOT / "index.html"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}

_OUTAGE_ACTION = re.compile(r"^/api/outages/(\d+)/(ack|postmortem)$")
_OUTAGE_ITEM = re.compile(r"^/api/outages/(\d+)$")
_DEVICE_ITEM = re.compile(r"^/api/devices/(\d+)$")
_DEVICE_MAINT = re.compile(r"^/api/devices/(\d+)/maintenance$")
_DEVICE_LINKS = re.compile(r"^/api/devices/(\d+)/links$")
_DEVICE_LINK_ITEM = re.compile(r"^/api/devices/(\d+)/links/(\d+)$")
_DEVICE_SNMP = re.compile(r"^/api/devices/(\d+)/snmp$")
_DEVICE_PORTS = re.compile(r"^/api/devices/(\d+)/ports$")
_PORT_MONITORED = re.compile(r"^/api/ports/(\d+)/monitored$")
_PORT_FEEDS = re.compile(r"^/api/ports/(\d+)/feeds$")
_PORT_BANDWIDTH = re.compile(r"^/api/ports/(\d+)/bandwidth$")
_WORKER_ITEM = re.compile(r"^/api/workers/(\d+)$")

# API endpoints reachable without a valid session (the login flow itself). Every
# other /api/* path requires the wisp_session cookie; static assets are always
# served (the SPA renders its own PIN gate when /api/* returns 401).
_PUBLIC_API = {
    ("GET", "/api/auth/status"),
    ("POST", "/api/login"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/logout"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HansaDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- response helpers ----------------------------------------------------
    def _send_json(self, payload, status: int = 200, *, set_cookie: str | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200,
                    cache: bool = True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    # -- auth ----------------------------------------------------------------
    def _authed(self) -> bool:
        token = auth.cookie_token(self.headers.get("Cookie"))
        return auth.verify_session(token, cfg=CONFIG, timeout_h=CONFIG.session_timeout_h)

    def _guard_api(self, method: str, path: str) -> bool:
        """Allow login-flow endpoints; gate everything else on a valid session."""
        if (method, path) in _PUBLIC_API:
            return True
        if self._authed():
            return True
        self._error(401, "authentication required")
        return False

    def _login(self) -> None:
        ip = self.client_address[0]
        wait = auth.THROTTLE.retry_after(ip)
        if wait > 0:
            return self._error(429, f"too many attempts — wait {int(wait) + 1}s")
        body = self._read_json_body() or {}
        with connect(CONFIG) as conn:
            ok = auth.verify_pin(conn, str(body.get("pin", "")))
        if not ok:
            auth.THROTTLE.fail(ip)
            return self._error(401, "incorrect PIN")
        auth.THROTTLE.reset(ip)
        token = auth.issue_session(CONFIG)
        self._send_json({"ok": True}, set_cookie=auth.session_cookie(
            token, max_age=CONFIG.session_timeout_h * 3600))

    def _setup_pin(self) -> None:
        """First-run: set the PIN when none exists yet, then sign the operator in."""
        body = self._read_json_body() or {}
        with connect(CONFIG) as conn:
            if auth.pin_is_set(conn):
                return self._error(409, "a PIN is already set")
            try:
                auth.set_pin(conn, str(body.get("pin", "")), by="first-run")
            except auth.PinError as exc:
                return self._error(422, str(exc))
        token = auth.issue_session(CONFIG)
        self._send_json({"ok": True}, set_cookie=auth.session_cookie(
            token, max_age=CONFIG.session_timeout_h * 3600))

    def _change_pin(self) -> None:
        body = self._read_json_body() or {}
        with connect(CONFIG) as conn:
            if not auth.verify_pin(conn, str(body.get("old", ""))):
                return self._error(422, "current PIN is incorrect")
            try:
                auth.set_pin(conn, str(body.get("new", "")), by="operator")
            except auth.PinError as exc:
                return self._error(422, str(exc))
        self._send_json({"ok": True})

    def _logout(self) -> None:
        self._send_json({"ok": True}, set_cookie=auth.clear_cookie())

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._guard_api("GET", path):
                return
            if path == "/api/backup":
                return self._send_backup()
            if path == "/api/events":
                return self._serve_events()
            return self._handle_api_get(path, parse_qs(parsed.query))
        return self._serve_static(path)

    # -- live push (Server-Sent Events) --------------------------------------
    def _data_version(self) -> str:
        """A cheap monotonic fingerprint of the data the live views render. It bumps
        whenever a new poll/outage/alert row lands — every full cycle AND instantly on
        a between-cycle DOWN/recovery — so an SSE client knows to re-fetch. Keyed on
        MAX(id) of the three append-mostly tables, plus the newest `switch_ports`
        write-stamp so an SNMP walk (port status + live bandwidth, which UPSERT in place
        rather than append a new id) also pushes the map/faceplate + Low-Bandwidth card
        live. One trivial read over small tables."""
        with connect(CONFIG) as conn:
            r = conn.execute(
                "SELECT (SELECT COALESCE(MAX(id),0) FROM poll_results) AS p,"
                " (SELECT COALESCE(MAX(id),0) FROM outages) AS o,"
                " (SELECT COALESCE(MAX(id),0) FROM alert_log) AS a,"
                " (SELECT COALESCE(MAX(updated_at),'') FROM switch_ports) AS s"
            ).fetchone()
        return f"{r['p']}.{r['o']}.{r['a']}.{r['s']}"

    def _serve_events(self) -> None:
        """Server-Sent Events stream: emit a `changed` event whenever `_data_version`
        moves, so the dashboard updates the instant the daemon writes — no client-side
        polling. The body has no Content-Length (delimited by connection close); the
        browser's EventSource consumes events as they stream and auto-reconnects. The
        per-connection 1s DB check is a single MAX(id) read, cheap for a few tabs."""
        self.close_connection = True  # streaming socket; don't try to keep-alive it
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")  # stop nginx buffering the stream
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")          # client reconnect backoff (ms)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        last: str | None = None
        idle = 0
        while True:
            try:
                version = self._data_version()
            except Exception:
                version = last  # a transient DB hiccup must not kill the stream
            try:
                if version != last:
                    last = version
                    self.wfile.write(f"event: changed\ndata: {version}\n\n".encode())
                    idle = 0
                else:
                    idle += 1
                    if idle % 15 == 0:                    # ~15s heartbeat keeps it open
                        self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return                                    # client went away
            time.sleep(1.0)

    def _send_backup(self) -> None:
        try:
            blob = services.create_backup(CONFIG)
        except Exception as exc:
            return self._error(500, f"backup failed: {exc}")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{services.backup_filename()}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _handle_api_get(self, path: str, qs: dict) -> None:
        def _one(name, default=""):
            return qs.get(name, [default])[0]
        try:
            if path == "/api/auth/status":
                with connect(CONFIG) as conn:
                    pin_set = auth.pin_is_set(conn)
                # org/locale branding is public so the login screen + header can show it
                return self._send_json({
                    "pin_set": pin_set,
                    "authed": self._authed(),
                    "org_name": CONFIG.org_name,
                    "timezone": CONFIG.timezone,
                    "ntfy_base_url": CONFIG.ntfy_base_url,
                    "channels": {
                        "owner": CONFIG.ntfy_topic_owner,
                        "operator": CONFIG.ntfy_topic_operator,
                        "tech": CONFIG.ntfy_topic_tech,
                    },
                })
            if path == "/api/summary":
                return self._send_json(services.system_summary(CONFIG))
            if path == "/api/triage":
                return self._send_json(services.triage_outages(CONFIG))
            if path == "/api/nodes":
                day = _one("day")
                if day:
                    return self._send_json(services.nodes_down_on_day(CONFIG, day))
                return self._send_json(services.nodes_list(CONFIG))
            if path == "/api/topology":
                return self._send_json(services.topology_graph(CONFIG))
            if path == "/api/heatmap":
                days = int(_one("days", "30") or 30)
                return self._send_json(services.network_heatmap(CONFIG, days))
            if path == "/api/workers":
                return self._send_json(services.list_workers(CONFIG))
            if path == "/api/attendance":
                return self._send_json(services.attendance_overview(CONFIG))
            if path == "/api/devices":
                return self._send_json(services.list_devices(CONFIG))
            mp = _DEVICE_PORTS.match(path)
            if mp:   # discovered SNMP ports for one switch
                return self._send_json(
                    services.list_switch_ports(int(mp.group(1)), CONFIG))
            if path == "/api/logs":
                return self._send_json(services.logs(
                    CONFIG,
                    query=_one("q"),
                    limit=max(1, min(100, int(_one("limit", "25") or 25))),
                    offset=max(0, int(_one("offset", "0") or 0)),
                ))
        except (ValueError, KeyError) as exc:
            return self._error(400, f"bad request: {exc}")
        return self._error(404, "no such endpoint")

    def _serve_static(self, path: str) -> None:
        # The app shell (index.html) lives in templates/; everything else
        # (app.js, icons.js, vendor/…) is an asset under static/.
        rel = path.lstrip("/")
        if rel in ("", "index.html"):
            return self._send_file(INDEX_HTML, cache=False)
        target = (STATIC_ROOT / rel).resolve()
        # path-traversal guard: resolved target must stay under STATIC_ROOT
        if STATIC_ROOT not in target.parents and target != STATIC_ROOT:
            return self._error(403, "forbidden")
        if not target.is_file():
            # SPA fallback: unknown non-asset routes render the app shell
            if "." not in Path(rel).name:
                return self._send_file(INDEX_HTML, cache=False)
            return self._error(404, "not found")
        # Only the big, stable vendored bundle is cached; app JS/CSS are served
        # fresh so edits show up on a plain reload (no hard-refresh needed).
        self._send_file(target, cache=rel.startswith("vendor/"))

    def _send_file(self, target: Path, *, cache: bool) -> None:
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send_bytes(target.read_bytes(), ctype, cache=cache)

    # -- POST ----------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        self._raw_body = None
        self._consume_body()          # drain before any early reply (keep-alive safety)
        if not self._guard_api("POST", path):
            return
        # Auth-flow endpoints read their own (possibly empty) body and manage cookies.
        if path == "/api/login":
            return self._login()
        if path == "/api/auth/setup":
            return self._setup_pin()
        if path == "/api/logout":
            return self._logout()
        if path == "/api/settings/pin":
            return self._change_pin()
        body = self._read_json_body()
        if body is None:
            return self._error(400, "invalid JSON body")
        try:
            if path == "/api/devices/check":  # reachability probe before saving
                res = services.check_reachable(body.get("ip_address") or body.get("ip", ""), CONFIG)
                return self._send_json({"ok": True, **res})
            if path == "/api/devices":  # create a node
                new_id = services.create_device(body, CONFIG)
                return self._send_json({"ok": True, "id": new_id}, 201)
            if path == "/api/workers":  # create a worker
                new_id = services.create_worker(body, CONFIG)
                return self._send_json({"ok": True, "id": new_id}, 201)
            if path == "/api/channels/test":   # send a test alert (go-live check)
                return self._send_json(services.test_channel(body.get("target", "owner"), CONFIG))
            if path == "/api/attendance":      # toggle an operator present for a day
                res = services.set_attendance(
                    int(body.get("worker_id") or 0),
                    bool(body.get("present")),
                    str(body.get("day") or ""),
                    CONFIG,
                )
                return self._send_json(res, 200 if res.get("ok") else 404)

            mm = _DEVICE_MAINT.match(path)     # pause/resume monitoring for one node
            if mm:
                on = bool(body.get("maintenance"))
                ok = services.set_maintenance(int(mm.group(1)), on, CONFIG)
                return self._send_json({"ok": ok, "maintenance": on}, 200 if ok else 404)

            ml = _DEVICE_LINKS.match(path)     # add a backup parent edge
            if ml:
                res = services.add_backup_link(
                    int(ml.group(1)), int(body.get("parent_id") or 0), CONFIG)
                return self._send_json(res, 200 if res.get("ok") else 422)

            ms = _DEVICE_SNMP.match(path)      # set a device's SNMP config
            if ms:
                ok = services.set_snmp_config(int(ms.group(1)), body, CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 404)

            mpm = _PORT_MONITORED.match(path)  # flag/unflag a port for alarming
            if mpm:
                ok = services.set_port_monitored(
                    int(mpm.group(1)), bool(body.get("monitored")), CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 404)

            mpf = _PORT_FEEDS.match(path)      # map a port -> downstream device
            if mpf:
                ok = services.set_port_feeds(
                    int(mpf.group(1)), body.get("feeds_device_id"), CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 404)

            mpb = _PORT_BANDWIDTH.match(path)  # set a port's low-bandwidth threshold
            if mpb:
                ok = services.set_port_bandwidth(
                    int(mpb.group(1)), body.get("threshold_mbps"),
                    body.get("direction"), CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 404)

            m = _OUTAGE_ACTION.match(path)
            if not m:
                return self._error(404, "no such endpoint")
            outage_id, action = int(m.group(1)), m.group(2)
            if action == "ack":
                tech = (body.get("technician") or "").strip()
                if not tech:
                    return self._error(422, "technician is required")
                ok = services.assign_and_ack(outage_id, tech, CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 409)
            if action == "postmortem":
                ok = services.submit_postmortem(
                    outage_id,
                    (body.get("root_cause") or "").strip(),
                    (body.get("notes") or "").strip(),
                    CONFIG,
                )
                return self._send_json({"ok": ok}, 200 if ok else 409)
        except services.LastOwnerError as exc:  # last-owner rule → conflict
            return self._error(409, str(exc))
        except (services.DeviceError, services.WorkerError) as exc:  # validation
            return self._error(422, str(exc))
        except Exception as exc:          # never 500 silently — surface a JSON error
            return self._error(500, str(exc))

    # -- PUT (edit a node) ---------------------------------------------------
    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        self._raw_body = None
        self._consume_body()
        if not self._guard_api("PUT", path):
            return
        body = self._read_json_body()
        if body is None:
            return self._error(400, "invalid JSON body")
        try:
            mw = _WORKER_ITEM.match(path)
            if mw:
                ok = services.update_worker(int(mw.group(1)), body, CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 404)
            m = _DEVICE_ITEM.match(path)
            if not m:
                return self._error(404, "no such endpoint")
            ok = services.update_device(int(m.group(1)), body, CONFIG)
            return self._send_json({"ok": ok}, 200 if ok else 404)
        except services.LastOwnerError as exc:
            return self._error(409, str(exc))
        except (services.DeviceError, services.WorkerError) as exc:
            return self._error(422, str(exc))
        except Exception as exc:
            return self._error(500, str(exc))

    # -- DELETE (remove a node) ----------------------------------------------
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        self._raw_body = None
        self._consume_body()
        if not self._guard_api("DELETE", path):
            return
        try:
            mw = _WORKER_ITEM.match(path)
            if mw:
                res = services.delete_worker(int(mw.group(1)), CONFIG)
                return self._send_json(res, 200 if res.get("ok") else 404)
            ml = _DEVICE_LINK_ITEM.match(path)   # remove a backup parent edge
            if ml:
                res = services.remove_backup_link(
                    int(ml.group(1)), int(ml.group(2)), CONFIG)
                return self._send_json(res, 200 if res.get("ok") else 404)
            mo = _OUTAGE_ITEM.match(path)
            if mo:  # dismiss a recovered outage without logging a post-mortem
                ok = services.dismiss_outage(int(mo.group(1)), CONFIG)
                return self._send_json({"ok": ok}, 200 if ok else 409)
            m = _DEVICE_ITEM.match(path)
            if not m:
                return self._error(404, "no such endpoint")
            res = services.delete_device(int(m.group(1)), CONFIG)
            return self._send_json(res, 200 if res.get("ok") else 409)
        except services.LastOwnerError as exc:
            return self._error(409, str(exc))
        except Exception as exc:
            return self._error(500, str(exc))

    def _consume_body(self) -> bytes:
        """Read the request body exactly once, caching it. Draining it is mandatory
        on HTTP/1.1 keep-alive: if a handler replies (e.g. 401/429) without reading
        the body, the leftover bytes get parsed as the next request and corrupt the
        connection. Verb handlers call this up front so every code path consumes it."""
        cached = getattr(self, "_raw_body", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self._raw_body = body
        return body

    def _read_json_body(self):
        raw = self._consume_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # quieter, single-line access log
    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")


def _seed_pin_from_env() -> None:
    """Bootstrap convenience: if WISP_DASHBOARD_PIN is exported and no PIN exists
    yet, seed it so a fresh deploy isn't locked out before the first-run screen.
    Otherwise the operator sets the PIN from the browser on first visit."""
    import os
    pin = os.environ.get("WISP_DASHBOARD_PIN", "").strip()
    if not pin:
        return
    with connect(CONFIG) as conn:
        if not auth.pin_is_set(conn):
            try:
                auth.set_pin(conn, pin, by="env")
            except auth.PinError:
                pass  # a bad env PIN just leaves the first-run screen in place


class _DashboardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback for normal client
    disconnects. Browsers and EventSource (`/api/events`) clients drop keep-alive
    and SSE sockets constantly — navigate away, refresh, reconnect — and the stdlib
    server logs a full stack trace per reset, which buries genuine errors in noise.
    Swallow only the benign connection-teardown errors; surface everything else."""

    daemon_threads = True   # don't let in-flight requests block Ctrl-C shutdown

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    """Build (but don't start) the dashboard HTTP server. The runnable entry
    point in apps/dashboard/main.py owns the serve loop + CLI."""
    if not INDEX_HTML.is_file():
        raise SystemExit(f"dashboard assets missing — expected {INDEX_HTML}")
    migrate(CONFIG)            # idempotent; ensures settings table exists for auth
    _seed_pin_from_env()
    return _DashboardServer((host, port), Handler)
