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

import json
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
    # Ready-to-send "Basic <token>" header, resolved ONCE from the device's
    # stored web-UI login when the session opens (api/proxy.py), so the tech
    # never sees the HTTP-auth popup and the password never touches the browser.
    # None = no stored Basic login (or the key couldn't decrypt it). In-memory
    # only, like the session itself — a central restart re-resolves on reopen.
    injected_auth: str | None = field(default=None, compare=False)
    # (username, password) for a FORM-login device (auth_mode='form'): the login
    # page gets an autofill script injected into its HTML (inject_autofill). Unlike
    # injected_auth the password reaches the browser here (a form's JS may hash it
    # client-side, so we must fill the real field) — inherent to form login.
    autofill: tuple[str, str] | None = field(default=None, compare=False)


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
        # last time each node's tunnel long-polled us — the preflight gate:
        # a submit against a node that isn't polling would just eat its timeout
        self._last_poll: dict[tuple[str, str], float] = {}

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

    def close_sessions_for(self, org_id: str, node_id: str) -> list[str]:
        """One tunnel per probe: drop every live session riding this node.
        Returns the closed sids so the caller can retire their DB rows."""
        with self._lock:
            gone = [sid for sid, s in self._sessions.items()
                    if s.org_id == org_id and s.node_id == node_id]
            for sid in gone:
                del self._sessions[sid]
        return gone

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
               headers: dict, body: bytes, timeout: float,
               extra: dict | None = None) -> dict | None:
        """Park a browser request for the edge and wait for the reply. Returns the
        reply dict (``status``/``headers``/``body``), or None on timeout.
        ``extra`` keys are merged into the parked payload (the preflight probe
        rides this); the normal device_ip/port/scheme fields stay present, so an
        edge that predates a given extra treats it as a plain fetch."""
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
            if extra:
                payload.update(extra)
            pend = _Pending(req_id, sess.org_id, sess.node_id, payload)
            self._pending[req_id] = pend
            q = self._inbox.setdefault((sess.org_id, sess.node_id), queue.Queue())
        q.put(pend)
        got = pend.event.wait(timeout)
        with self._lock:
            self._pending.pop(req_id, None)
        return pend.response if got else None

    # -- edge side -------------------------------------------------------------

    def polled_recently(self, org_id: str, node_id: str, within_s: float) -> bool:
        """Has this node's tunnel long-polled within the last ``within_s``?
        Gates the session-open preflight: probing through a dormant (or
        pre-preflight) edge would only burn the browser's patience."""
        with self._lock:
            last = self._last_poll.get((org_id, node_id), 0.0)
        return (time.time() - last) <= within_s

    def next_request(self, org_id: str, node_id: str, hold_s: float) -> dict | None:
        """Edge long-poll: block up to hold_s for a parked request for this node."""
        with self._lock:
            q = self._inbox.setdefault((org_id, node_id), queue.Queue())
            self._last_poll[(org_id, node_id)] = time.time()
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


# ---- form-login autofill (webplan.md Phase 2b) --------------------------------
#
# For a device whose stored login is auth_mode='form', central injects a small
# credential-FREE bootstrap into every proxied HTML *document* (not AJAX fragments).
# It waits for a login form to exist — a `<input type=password>`, in the page OR a
# same-origin iframe, possibly rendered by the device's own JS AFTER load (why the
# old "password field must be in the initial HTML" gate silently no-op'd on
# SPA-style device UIs) — via an immediate check + MutationObserver + a polling
# fallback. ONLY once a password field appears does it fetch the credentials from
# central over the same session (AUTOFILL_PATH), so the plaintext never rides a
# page with no login on it. Then it fills username + password with the
# native-setter dance (so React/Vue-controlled inputs register the change) and
# focuses a detected captcha box. FILL-ONLY (no auto-submit): a wrong guess must
# not lock an account and a dynamic captcha needs a human.
#
# The password still reaches the browser DOM at fill time — inherent to form login
# (a form's JS often hashes it before POST, so the real <input> must be filled).

# Reserved path under a session prefix: central answers it directly with the
# decrypted login JSON instead of forwarding to the edge (api/proxy.py).
AUTOFILL_PATH = "__wisp_autofill__"

# A full HTML document, not an AJAX HTML fragment (don't append a <script> to a
# partial that gets innerHTML'd somewhere).
_HTML_DOC_RE = re.compile(rb"(?i)<html[\s>]|<!doctype\s+html|</body\s*>|</head\s*>")
_BODY_CLOSE_RE = re.compile(rb"(?i)</body\s*>")

_AUTOFILL_JS = (
    b"<script>/* wisp-autofill */(function(){\n"
    b"var U=%URL%,C=null,fetching=false,done=false;\n"
    b"function pw(doc){try{var a=doc.querySelectorAll('input');for(var i=0;i<a.length;i++)"
    b"{if(a[i].type==='password')return a[i];}}catch(e){}return null;}\n"
    b"function find(){var f=pw(document);if(f)return f;var fr=document.querySelectorAll('iframe');"
    b"for(var i=0;i<fr.length;i++){try{var d=fr[i].contentDocument;if(d){var g=pw(d);if(g)return g;}}"
    b"catch(e){}}return null;}\n"
    b"function ns(el,v){try{var p=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:"
    b"HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(el,v);}"
    b"catch(e){el.value=v;}el.dispatchEvent(new Event('input',{bubbles:true}));"
    b"el.dispatchEvent(new Event('change',{bubbles:true}));}\n"
    b"function cap(f){var im=f.querySelectorAll('img');for(var i=0;i<im.length;i++){"
    b"var s=(im[i].getAttribute('src')||'').toLowerCase();"
    b"if(/captcha|verify|checkcode|randcode|validcode|authcode|vcode|kaptcha/.test(s)){"
    b"var t=f.querySelectorAll('input[type=text],input:not([type])');"
    b"for(var j=0;j<t.length;j++){if(!t[j].value)return t[j];}}}return null;}\n"
    b"function fill(p){if(done||p.value)return;var f=p.form||p.ownerDocument;"
    b"var ins=f.querySelectorAll('input');var uf=null;for(var i=0;i<ins.length;i++){"
    b"if(ins[i]===p)break;var ty=ins[i].type;"
    b"if(ty==='text'||ty==='email'||ty===''||ty==='tel')uf=ins[i];}"
    b"if(uf&&C.u&&!uf.value)ns(uf,C.u);ns(p,C.p);"
    b"var cf=cap(f);if(cf)try{cf.focus();}catch(e){}done=true;}\n"
    b"function go(){if(done)return;var p=find();if(!p)return;if(C){fill(p);return;}"
    b"if(fetching)return;fetching=true;"
    b"fetch(U,{credentials:'include',cache:'no-store'}).then(function(r){return r.json();})"
    b".then(function(d){fetching=false;if(d&&d.p){C=d;var q=find();if(q)fill(q);}})"
    b".catch(function(){fetching=false;});}\n"
    b"go();try{var mo=new MutationObserver(go);mo.observe(document.documentElement,"
    b"{childList:true,subtree:true});setTimeout(function(){try{mo.disconnect();}catch(e){}},20000);}"
    b"catch(e){}\n"
    b"var n=0,iv=setInterval(function(){go();if(done||++n>66)clearInterval(iv);},300);\n"
    b"})();</script>")


def inject_autofill(content_type: str, body: bytes, sid: str) -> bytes:
    """Append the credential-free autofill bootstrap to a full HTML document,
    before ``</body>`` (or at the end). Non-HTML, empty, or fragment bodies pass
    through untouched. The bootstrap fetches the login from ``AUTOFILL_PATH`` under
    this session only after a password field appears, so credentials never ship in
    the page itself."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    # An explicit non-HTML type opts out; text/html, xhtml, OR a missing type
    # (old firmware serves login pages with no Content-Type) fall through to the
    # document sniff, which is what actually keeps us off fragments and non-HTML.
    if ctype and ctype not in ("text/html", "application/xhtml+xml"):
        return body
    if not body or not _HTML_DOC_RE.search(body):
        return body
    url = json.dumps(f"/api/proxy/{sid}/{AUTOFILL_PATH}").replace("<", "\\u003c")
    script = _AUTOFILL_JS.replace(b"%URL%", url.encode("utf-8"))
    last = None
    for last in _BODY_CLOSE_RE.finditer(body):
        pass
    if last is not None:
        return body[:last.start()] + script + body[last.start():]
    return body + script
