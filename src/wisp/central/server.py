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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wisp.config import CONFIG, Config
from wisp.central import api, auth, billing, inventory, pki, secretbox, theme, totp
from wisp.central import rollup as central_rollup
from wisp.central.api.common import public_user
from wisp.central.auth import LoginThrottle
from wisp.central.engine import EngineRegistry
from wisp.central.proxy import ProxyHub
from wisp.central.store import CentralStore
from wisp.egress.notifiers import build_notifier
from wisp.runtime.central_client import WIRE_V

log = logging.getLogger("wisp.central")

MAX_WIRE_V = WIRE_V
_MAX_BODY = 16 * 1024 * 1024

# /api/proxy/* paths that are OUR routes, not <sid> tunnel-forwards. A sid is a
# 32-char token so a collision can't happen by accident, but the split must be
# explicit — a new exact route added only to the api tables would otherwise be
# swallowed by the prefix forward and 404 as an unknown session.
_PROXY_EXACT = frozenset({
    "/api/proxy/session", "/api/proxy/sessions",
    "/api/proxy/close", "/api/proxy/audit",
})
# Session id inside a Referer header — the escape-rescue hook (_proxy_rescue).
_PROXY_SID_RE = re.compile(r"/api/proxy/([A-Za-z0-9_-]{16,})/")
_STATIC = Path(__file__).resolve().parent / "static"

# Routes a LOCKED org's session may still reach: exactly what the lock screen
# needs to render (who am I + how much do I owe) plus logout — and the "I've
# paid" ping plus the free-plan escape hatch, so a locked org can flag its
# payment (or drop to Free) right there.
_BILLING_EXEMPT = {"/api/me", "/api/billing", "/api/login", "/api/logout",
                   "/api/billing/paid", "/api/billing/plan", "/healthz"}

# Loopback peers — the only ones whose X-Forwarded-For we trust (the request came
# through the local reverse proxy). See Handler._client_ip / cfg.trust_forwarded_for.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# A field-worker session gets the FULL desktop dashboard, READ-ONLY (2026-07-23):
# it may READ every monitoring surface the shell renders, but WRITE nothing
# beyond incident triage. Two METHOD-scoped allowlists (the gate checks the HTTP
# method), deny-by-default preserved — a NEW route stays worker-blocked until it
# is deliberately placed in one of these. Enforced as one choke point, the
# billing-gate pattern. Login/logout are handled before the gate.
#
# Sensitive reads are DELIBERATELY absent and stay owner/superadmin-only: the
# per-device web-UI credential vault (/api/inventory/credentials), the proxy
# tunnel (/api/proxy/*), server/platform config (/api/system, /api/admin/*), the
# account list (/api/users), vendor profiles (/api/{snmp,gpon,web-optics}-
# profiles) and the raw SNMP-walk dumps (/api/inventory/snmp-walk*, reached only
# through the owner-gated diagnostic tool, so a worker never fetches them).
# /api/billing IS readable — the lock screen every org member sees renders from
# it — but its UI is hidden client-side.
_WORKER_GET = {
    "/api/me", "/api/outages", "/api/events", "/api/summary", "/api/billing",
    "/api/orgs", "/api/nodes", "/api/regions",
    "/api/inventory", "/api/inventory/routes", "/api/inventory/ports",
    "/api/inventory/link-ports", "/api/inventory/optics",
    "/api/inventory/onu-search", "/api/inventory/snmp-status",
    "/api/inventory/rx-status", "/api/inventory/perf",
    "/api/inventory/perf/samples", "/api/pon/faults", "/api/pon/summary",
    "/api/incident/shape", "/api/analytics", "/api/analytics/trend",
    "/api/logs",
}
# The ONLY writes a worker may perform: acknowledge/post-mortem (triage), its own
# password, and the "I've paid" ping (any org member sends it from the lock
# screen). Every config/topology/credential/proxy/billing-plan write stays owner+.
_WORKER_POST = {
    "/api/outages/acknowledge", "/api/outages/postmortem",
    "/api/users/password", "/api/users/whatsapp", "/api/billing/paid",
}

def _make_handler(cfg: Config, store: CentralStore, throttle: LoginThrottle, notifier=None,
                  engine_registry: EngineRegistry | None = None,
                  secret_box=None):
    token = cfg.central_token
    client_ca = cfg.central_client_ca
    # store is handed in so the WhatsApp channel can read the superadmin's live
    # config out of app_settings (the edge, which passes no store, stays ntfy-only).
    notifier = notifier or build_notifier(cfg, store)
    registry = engine_registry or EngineRegistry(store, cfg)
    secret_box = secret_box or secretbox.from_config(cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "wisp-central"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _security_headers(self) -> None:
            # Baseline hardening on central's OWN responses. Deliberately NOT
            # applied to _raw_reply: that streams a device's own bytes verbatim
            # through the web-UI proxy and must not inherit our framing/nosniff
            # policy. CSP is intentionally omitted here — a useful policy has to
            # allowlist the map-tile/geocoding hosts the browser fetches and needs
            # real-browser verification, so it lands as its own change.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "same-origin")
            if cfg.session_cookie_secure:
                # Only honoured over HTTPS, which is what session_cookie_secure
                # asserts we're behind (Caddy TLS). Ignored on a plain-http dev box.
                self.send_header("Strict-Transport-Security",
                                 "max-age=31536000; includeSubDomains")

        def _reply(self, code: int, body: dict, *, cookie: str | None = None) -> None:
            # json.dumps emits bare NaN/Infinity by default, which is NOT valid
            # JSON — JSON.parse rejects it and the browser loses the WHOLE
            # reply, not the one bad number. It took out an OLT's Optical tab
            # from a single ONU reporting Tx = -inf. Values are cleaned at
            # ingest (weboptics._num, optics._to_float), but device-derived
            # floats reach this encoder from many paths, so the guarantee that
            # central never serves unparseable JSON belongs here too.
            # allow_nan=False makes the C encoder RAISE instead of emitting
            # garbage, so the common path costs nothing and only a reply that
            # actually contains a bad float pays for the sanitising walk.
            try:
                raw = json.dumps(body, allow_nan=False).encode()
            except ValueError:
                log.warning("non-finite float in a JSON reply — nulled (%s)",
                            self.path)
                raw = json.dumps(_json_safe(body), allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            # EVERY JSON API response is no-store. Without a Cache-Control header
            # a 200 GET is heuristically cacheable (RFC 9111 §4.2.2) and we send
            # no validator, so the browser can serve a stale body without ever
            # asking — and never sees a corrected one. It bit ONU search first
            # because its URL carries the needle: a `?q=BSNL` answered while
            # search was still serial-only got its EMPTY reply pinned to that
            # exact string, so "BSNL" stayed blank while "BSN" and "BSNL_" (never
            # requested before, hence uncached) worked. Any endpoint whose reply
            # changes — inventory, outages, billing — had the same exposure.
            # Freshness is react-query's job in memory, never the HTTP cache's.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self._security_headers()
            # An explicit cookie (login/logout) always wins; otherwise attach the
            # sliding-session refresh that _user() stashed, if any (see _user).
            send_cookie = cookie if cookie is not None else getattr(
                self, "_refresh_cookie", None)
            if send_cookie:
                self.send_header("Set-Cookie", send_cookie)
            self.end_headers()
            self.wfile.write(raw)

        def _raw_reply(self, code: int, headers, body: bytes) -> None:
            # Non-JSON response, used by the web-UI proxy to stream a device's
            # own bytes back to the browser verbatim. Caller has already stripped
            # hop-by-hop headers; we own Content-Length. Headers arrive as
            # (name, value) pairs so repeated names (multiple Set-Cookie)
            # survive; a plain dict is accepted too.
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
            # /api/proxy/<sid>/<path...> — the browser-facing tunnel forward.
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
            # Device-page JS builds root-absolute URLs that escape the
            # /api/proxy/<sid>/ prefix (the documented M2 gap) — they land here
            # as unknown routes. If the Referer names a LIVE proxy session,
            # bounce the request back inside it: 307 preserves method + body,
            # and the tunnel route re-runs the full auth/org/billing gauntlet,
            # so this grants nothing a direct tunnel request wouldn't get.
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

        def _client_ip(self) -> str:
            # Behind Caddy the socket peer is always 127.0.0.1, so the login
            # throttle would otherwise bucket the entire internet together. Trust
            # X-Forwarded-For only from a loopback peer (i.e. the local proxy) and
            # take the LAST hop — Caddy appends the real client, so a spoofed
            # leading entry a browser sent can never displace it.
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
                # Sliding idle window: re-issue the cookie on activity so an
                # active operator is never logged out, while an idle one is.
                # Throttled inside slide_session (≤ once/min); no-op for
                # remember-me sessions. Stashed for _reply to attach.
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
            # A SUPERADMIN is org_id IS NULL, and its `role` column is
            # meaningless — nothing reads it. Gate on identity before role or a
            # superadmin row that happens to carry 'worker' locks the platform
            # admin out of the whole dashboard. That is exactly what the
            # owner+worker collapse (2026-07-21) caused: `create_user`'s default
            # role became 'worker', and every superadmin provisioned without an
            # explicit --role 403'd on every /api/* route.
            if user.get("is_superadmin") or user.get("org_id") is None:
                return False
            if user.get("role") != "worker":
                return False
            # Read-only worker: reads on _WORKER_GET pass, the triage writes on
            # _WORKER_POST pass, everything else 403s. Method-scoped so an allowed
            # GET path (e.g. /api/inventory, /api/nodes) can't smuggle its
            # same-path POST past the gate.
            allowed = _WORKER_GET if method == "GET" else _WORKER_POST
            if route in allowed:
                return False
            self._reply(403, {"error": "forbidden"})
            return True

        def _billing_blocked(self, route: str, user: dict | None = None) -> bool:
            # The paywall gate: a locked org's dashboard session gets 402 on
            # every /api/* route the lock screen doesn't need. Edge ingest
            # (/report, /heartbeat, /edge/*) never passes through here —
            # monitoring and paging must survive a lapsed bill — and
            # superadmin/global-token readers stay exempt so the platform
            # admin can inspect and unlock the org.
            if not route.startswith("/api/") or route in _BILLING_EXEMPT:
                return False
            user = user or self._user()
            if not user or user["is_superadmin"] or not user["org_id"]:
                return False
            if not billing.org_locked(store, user["org_id"]):
                return False
            self._reply(402, {"error": "payment required — account locked",
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
            if rel == "index.html":
                data = self._inject_theme(data)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)
            return True

        def _inject_theme(self, html: bytes) -> bytes:
            """Put the superadmin's colour overrides on the page before first paint.

            This is injection rather than a fetch on purpose: colours arriving
            after mount means every load flashes the shipped palette and then
            repaints, which is ugliest on the login and billing-lock screens —
            the two that render before any session exists and so could never
            read a superadmin-only endpoint anyway.

            Appended at the END of <head>, after the bundle's stylesheet link.
            Source order is NOT what makes this win, though — theme.py's
            selectors are `:root:not(.dark)` / `:root.dark`, which outrank the
            bundle's `:root` / `.dark` on specificity wherever they land. That
            matters because relying on order alone is what broke dark mode
            once: a `:root{}` block of light overrides placed after the bundle
            also beats its `.dark{}`, and applies in the wrong mode. See
            theme.py:render_css. Best-effort throughout: a store hiccup or a
            missing </head> must serve the app on the stock palette, never 500
            the dashboard over cosmetics.
            """
            try:
                css = theme.render_css(theme.load(store))
            except Exception:
                logging.exception("theme overrides failed")
                return html
            if not css:
                return html
            # theme.py's allowlist already bars `<` `>` and `}` from reaching a
            # value; this is the second belt, so a future widening of _VALUE_RE
            # can't turn a colour field into a way to close the <style> element.
            snippet = ('<style id="wisp-theme">'
                       + css.replace("<", "\\3c ")
                       + "</style>").encode("utf-8")
            marker = b"</head>"
            i = html.find(marker)
            return html[:i] + snippet + html[i:] if i != -1 else html + snippet

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
            if route.startswith("/api/proxy/") and route not in _PROXY_EXACT:
                self._proxy_forward("POST", route, parsed.query)
                return
            if route in ("/heartbeat", "/report", "/edge/snmp-walk", "/edge/proxy/reply"):
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
                # Bump the epoch so the token dies SERVER-side, not just in this
                # browser — a copied cookie is invalidated too. Best-effort.
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
            # Throttle on BOTH the client IP and the account name so neither a
            # single box spraying accounts nor a spray against one account runs
            # unbounded. Either tripped counter delays the attempt.
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
            # Second factor for accounts that enabled it (owner/superadmin).
            if user.get("totp_enabled"):
                verdict = self._check_second_factor(user, body)
                if verdict != "ok":
                    if verdict == "required":
                        # Password was correct — this is the normal 2FA prompt,
                        # NOT a failed attempt, so don't burn the throttle.
                        self._reply(401, {"error": "enter your authenticator code",
                                          "totp_required": True})
                    else:
                        # A wrong code IS a failed attempt — this throttling is
                        # what keeps the 6-digit space unbrute-forceable.
                        throttle.fail(ip)
                        if username:
                            throttle.fail(ukey)
                        self._reply(401, {"error": "that code didn't match",
                                          "totp_required": True})
                    return
            throttle.reset(ip)
            throttle.reset(ukey)
            remember = bool(body.get("remember"))
            # Owners and superadmins NEVER get a long-lived "trusted device"
            # session — that account reconfigures the whole network, so it
            # re-authenticates on the normal cadence no matter what the client
            # asked for (the web form no longer even offers the box, but a
            # crafted request or another client must not bypass this).
            # org_id IS NULL == superadmin.
            if user["org_id"] is None or user["role"] == "owner":
                remember = False
            # Bump the session generation so THIS login supersedes any other live
            # session for the account (single active session).
            epoch = store.bump_session_epoch(user["id"])
            tok = auth.issue_session(user["id"], cfg, remember=remember, epoch=epoch)
            cookie = auth.session_cookie(
                tok, max_age=auth.session_cookie_max_age(cfg, remember=remember),
                secure=cfg.session_cookie_secure)
            self._reply(200, {"user": public_user(user, store)}, cookie=cookie)

        def _check_second_factor(self, user, body):
            """'ok' | 'required' (no code supplied) | 'bad' (wrong code/recovery)."""
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
            # Atomically claim the step so a captured code can't be replayed and
            # two requests can't ride the same fresh code (single-use, race-free).
            return "ok" if store.claim_totp_step(user["id"], step) else "bad"

    # Route handlers in wisp.central.api receive the live handler instance;
    # the request services ride on it as class attributes.
    Handler.cfg = cfg
    Handler.store = store
    Handler.notifier = notifier
    Handler.registry = registry
    Handler.secretbox = secret_box
    Handler.proxy = ProxyHub()
    # The web-optics sweeper is a request service too now: the Optical panel's
    # manual refresh drives the very same object the background sweep does, so
    # its per-OLT lock covers both and a click can't collide with a pass.
    # Imported here rather than at module scope — it imports api.proxy, which
    # imports back through this package.
    from wisp.central.weboptics_sweep import build_sweeper
    Handler.weboptics = build_sweeper(cfg, store, Handler.proxy, secret_box)
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

def _json_safe(obj):
    """Structure with every non-finite float replaced by None.

    Only ever called on the slow path, after allow_nan=False has proved there
    is something to clean. None rather than a sentinel number because that is
    what the column means: no reading. A dropped number costs one cell; an
    unparseable body costs the entire page.
    """
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
        httpd = ThreadingHTTPServer((cfg.central_bind, cfg.central_port), handler)
    httpd.store = store
    # Background sweepers need the same live services the request handlers use;
    # the hub in particular is per-process state, so it must be the SAME object,
    # not a fresh ProxyHub.
    httpd.proxy = handler.proxy
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
    billing.start_central_billing_thread(cfg, httpd.store)
    from wisp.central.weboptics_sweep import start_web_optics_thread
    start_web_optics_thread(cfg, httpd.store, httpd.proxy, httpd.secretbox,
                            sweeper=httpd.weboptics)
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
