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

Pure stdlib; safe to run on a laptop with the simulated prober + mock notifier.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.server import services
from wisp.config import CONFIG, PROJECT_ROOT

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
_DEVICE_ITEM = re.compile(r"^/api/devices/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "HansaDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- response helpers ----------------------------------------------------
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            return self._handle_api_get(path, parse_qs(parsed.query))
        return self._serve_static(path)

    def _handle_api_get(self, path: str, qs: dict) -> None:
        def _one(name, default=""):
            return qs.get(name, [default])[0]
        try:
            if path == "/api/summary":
                return self._send_json(services.system_summary(CONFIG))
            if path == "/api/triage":
                return self._send_json(services.triage_outages(CONFIG))
            if path == "/api/nodes":
                day = _one("day")
                if day:
                    return self._send_json(services.nodes_down_on_day(CONFIG, day))
                return self._send_json(services.nodes_list(CONFIG))
            if path == "/api/heatmap":
                days = int(_one("days", "30") or 30)
                return self._send_json(services.network_heatmap(CONFIG, days))
            if path == "/api/technicians":
                return self._send_json(services.technicians(CONFIG))
            if path == "/api/devices":
                return self._send_json(services.list_devices(CONFIG))
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
        body = self._read_json_body()
        if body is None:
            return self._error(400, "invalid JSON body")
        try:
            if path == "/api/devices":  # create a node
                new_id = services.create_device(body, CONFIG)
                return self._send_json({"ok": True, "id": new_id}, 201)

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
        except services.DeviceError as exc:    # validation problem
            return self._error(422, str(exc))
        except Exception as exc:          # never 500 silently — surface a JSON error
            return self._error(500, str(exc))

    # -- PUT (edit a node) ---------------------------------------------------
    def do_PUT(self) -> None:
        m = _DEVICE_ITEM.match(urlparse(self.path).path)
        if not m:
            return self._error(404, "no such endpoint")
        body = self._read_json_body()
        if body is None:
            return self._error(400, "invalid JSON body")
        try:
            ok = services.update_device(int(m.group(1)), body, CONFIG)
            return self._send_json({"ok": ok}, 200 if ok else 404)
        except services.DeviceError as exc:
            return self._error(422, str(exc))
        except Exception as exc:
            return self._error(500, str(exc))

    # -- DELETE (remove a node) ----------------------------------------------
    def do_DELETE(self) -> None:
        m = _DEVICE_ITEM.match(urlparse(self.path).path)
        if not m:
            return self._error(404, "no such endpoint")
        try:
            res = services.delete_device(int(m.group(1)), CONFIG)
            return self._send_json(res, 200 if res.get("ok") else 409)
        except Exception as exc:
            return self._error(500, str(exc))

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # quieter, single-line access log
    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    """Build (but don't start) the dashboard HTTP server. The runnable entry
    point in apps/dashboard/main.py owns the serve loop + CLI."""
    if not INDEX_HTML.is_file():
        raise SystemExit(f"dashboard assets missing — expected {INDEX_HTML}")
    return ThreadingHTTPServer((host, port), Handler)
