"""Device web-UI proxy hub — the reverse-tunnel parking desk (webplan.md, M0).

A dashboard user opens a *session* against a device; their browser requests then
ride ``/api/proxy/<sid>/...``. This module is the in-process desk that:

  * holds live sessions (sid -> device/org/node), lazily TTL-expired;
  * PARKS an incoming browser request on the node's inbox and blocks the browser
    worker thread until the edge answers (or a timeout);
  * hands parked requests to the edge's long-poll (``/edge/proxy/next``);
  * matches the edge's reply (``/edge/proxy/reply``) back to the waiting browser.

All state is process memory on purpose — a tunnel is inherently live; nothing here
belongs in SQLite (only the session audit record does, later). Central runs a
``ThreadingHTTPServer``, so the primitives are ``threading``/``queue``, never
asyncio: a browser worker thread blocks on an ``Event``, an edge long-poll thread
blocks on a ``queue.Queue.get`` — one desk, two thread populations, matched by
``req_id``.

The edge still names nothing it isn't allowed to: the parked payload carries the
device IP central resolved from the session, and the edge re-checks that IP against
its own device list before fetching (ingress/webproxy.py). No raw-IP path exists.
"""
from __future__ import annotations

import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

# Per-session concurrent-request ceiling: a page's asset burst needs a handful,
# and browsers cap themselves ~6-8 per origin anyway; anything past this reads
# as a runaway/abusive client, and each parked request holds a central worker
# thread — the bound is what keeps one session from starving the server.
MAX_INFLIGHT_PER_SESSION = 16


def parse_ports(spec: str) -> frozenset[int]:
    """Closed set of device ports a session may target. Junk entries are dropped,
    not fatal — an empty result means 'no ports allowed', which fails every open."""
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = int(part)
        except ValueError:
            continue
        if 1 <= p <= 65535:
            out.add(p)
    return frozenset(out)


@dataclass
class ProxySession:
    sid: str
    org_id: str
    device_id: int
    node_id: str
    device_ip: str
    device_port: int
    scheme: str
    created_by: int
    created_at: float
    expires_at: float
    # last time the DB session record was synced — activity extends the TTL on
    # every asset request, but the row is only touched every ~20s (api/proxy.py)
    db_synced_at: float = field(default=0.0, compare=False)


class _Pending:
    """One in-flight browser request awaiting the edge's reply."""

    __slots__ = ("req_id", "org_id", "node_id", "payload", "event", "response")

    def __init__(self, req_id: int, org_id: str, node_id: str, payload: dict) -> None:
        self.req_id = req_id
        self.org_id = org_id
        self.node_id = node_id
        self.payload = payload
        self.event = threading.Event()
        self.response: dict | None = None


class ProxyHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ProxySession] = {}
        self._inbox: dict[tuple[str, str], queue.Queue] = {}
        self._pending: dict[int, _Pending] = {}
        self._seq = 0

    # -- sessions --------------------------------------------------------------

    def open_session(self, *, org_id: str, device_id: int, node_id: str,
                     device_ip: str, device_port: int, scheme: str,
                     created_by: int, ttl_s: float) -> ProxySession:
        now = time.time()
        sess = ProxySession(
            sid=secrets.token_urlsafe(24), org_id=org_id, device_id=device_id,
            node_id=node_id, device_ip=device_ip, device_port=device_port,
            scheme=scheme, created_by=created_by, created_at=now,
            expires_at=now + ttl_s)
        with self._lock:
            self._sessions[sess.sid] = sess
        return sess

    def get_session(self, sid: str) -> ProxySession | None:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None and sess.expires_at < time.time():
                del self._sessions[sid]
                sess = None
        return sess

    def close_session(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def has_session(self, sid: str) -> bool:
        with self._lock:
            return sid in self._sessions

    def extend_session(self, sess: ProxySession, ttl_s: float) -> float:
        """Activity keeps a session alive: push expiry to now+ttl (never
        shortens). Returns the new expires_at epoch."""
        with self._lock:
            sess.expires_at = max(sess.expires_at, time.time() + ttl_s)
            return sess.expires_at

    def active_sessions_for(self, org_id: str, node_id: str) -> list[dict]:
        """Live sessions this node should serve — rides the /report reply so a
        dormant edge learns to spin its tunnel up (webplan.md §2). TTL is sent
        RELATIVE (seconds remaining), never as a wall-clock timestamp: the edge's
        clock is not trusted to agree with central's."""
        now = time.time()
        out = []
        with self._lock:
            for sess in self._sessions.values():
                if (sess.org_id == org_id and sess.node_id == node_id
                        and sess.expires_at > now):
                    out.append({"sid": sess.sid,
                                "ttl_s": round(sess.expires_at - now, 1)})
        return out

    def inflight(self, sid: str) -> int:
        with self._lock:
            return sum(1 for p in self._pending.values()
                       if p.payload.get("sid") == sid)

    # -- browser side (blocks the calling worker thread) -----------------------

    def submit(self, sess: ProxySession, *, method: str, path: str,
               headers: dict, body: bytes, timeout: float) -> dict | None:
        """Park a browser request for the edge and wait for the reply. Returns the
        reply dict (``status``/``headers``/``body``), or None on timeout."""
        import base64
        with self._lock:
            self._seq += 1
            req_id = self._seq
            payload = {
                "req_id": req_id, "sid": sess.sid, "method": method, "path": path,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode() if body else None,
                "device_ip": sess.device_ip, "device_port": sess.device_port,
                "scheme": sess.scheme,
            }
            pend = _Pending(req_id, sess.org_id, sess.node_id, payload)
            self._pending[req_id] = pend
            q = self._inbox.setdefault((sess.org_id, sess.node_id), queue.Queue())
        q.put(pend)
        got = pend.event.wait(timeout)
        with self._lock:
            self._pending.pop(req_id, None)
        return pend.response if got else None

    # -- edge side -------------------------------------------------------------

    def next_request(self, org_id: str, node_id: str, hold_s: float) -> dict | None:
        """Edge long-poll: block up to hold_s for a parked request for this node."""
        with self._lock:
            q = self._inbox.setdefault((org_id, node_id), queue.Queue())
        try:
            pend = q.get(timeout=max(0.0, hold_s))
        except queue.Empty:
            return None
        return pend.payload

    def deliver(self, req_id: int, org_id: str, node_id: str, response: dict) -> bool:
        """Edge reply upload: hand the response to the waiting browser thread.
        False if the browser already gave up (pending row gone) or the replying
        edge's (org, node) doesn't own this req_id — a valid credential for one
        node must not answer another node's parked request."""
        with self._lock:
            pend = self._pending.get(req_id)
            if pend is None or pend.org_id != org_id or pend.node_id != node_id:
                return False
        pend.response = response
        pend.event.set()
        return True


# ---- best-effort URL rewriting (webplan.md §7, M1) ----------------------------
#
# The device page is served under /api/proxy/<sid>/..., so anything ROOT-absolute
# in it (Location: /login, href="/style.css", Path=/) points at central's root and
# breaks. These helpers pull such references back inside the session prefix.
#
# Deliberate deviation from the plan's mitigation list: NO <base href> injection.
# The proxy preserves the device's path hierarchy verbatim after the sid, so
# plain relative URLs ("img.png", "../x.js") already resolve correctly against
# the request URL — a <base href="/api/proxy/<sid>/"> would re-anchor them to the
# prefix ROOT and break every subdirectory page. Root-absolute references are the
# only broken class, and attribute rewriting below is what fixes those.
#
# JS-constructed absolute URLs (fetch('/api/x'), location='/y') stay broken by
# design — that's the documented M2 wildcard-host problem, not a regex to grow.

# href="/x  src='/x  action="/x — root-absolute only ("//host" protocol-relative
# and full URLs are left alone).
_ATTR_RE = re.compile(rb'(?i)\b(href|src|action)\s*=\s*(["\'])/(?!/)')
# CSS url(/x), quoted or bare.
_CSS_URL_RE = re.compile(rb'(?i)\burl\(\s*(["\']?)/(?!/)')
_COOKIE_PATH_RE = re.compile(r"(?i)(;\s*path=)/")

_REWRITE_CTYPES = ("text/html", "text/css")


def rewrite_headers(sid: str, sess: ProxySession,
                    pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pull Location redirects and Set-Cookie paths back inside the session
    prefix. Absolute Locations that point at the DEVICE's own origin are
    rewritten too (old firmwares redirect to http://<own-ip>/login); genuinely
    external redirects pass through untouched — honest beats silently wrong."""
    prefix = f"/api/proxy/{sid}"
    own = {f"{sess.scheme}://{sess.device_ip}",
           f"{sess.scheme}://{sess.device_ip}:{sess.device_port}"}
    out: list[tuple[str, str]] = []
    for k, v in pairs:
        lk = k.lower()
        if lk == "location":
            if v.startswith("/"):
                v = prefix + v
            else:
                for origin in own:
                    if v == origin or v.startswith(origin + "/"):
                        v = prefix + (v[len(origin):] or "/")
                        break
        elif lk == "set-cookie":
            v = _COOKIE_PATH_RE.sub(rf"\g<1>{prefix}/", v)
        out.append((k, v))
    return out


def rewrite_body(sid: str, content_type: str, body: bytes) -> bytes:
    """Rewrite root-absolute references in HTML/CSS bodies into the session
    prefix. Byte-level on purpose — no charset guessing, and a body that doesn't
    match the patterns passes through bit-identical."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in _REWRITE_CTYPES or not body:
        return body
    prefix = f"/api/proxy/{sid}".encode()
    body = _CSS_URL_RE.sub(rb"url(\1" + prefix + rb"/", body)
    if ctype == "text/html":
        body = _ATTR_RE.sub(rb"\1=\2" + prefix + rb"/", body)
    return body
