from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("wisp.central")


_CACHEABLE_EXT = frozenset({
    ".js", ".css", ".png", ".gif", ".jpg", ".jpeg", ".ico", ".svg", ".bmp",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".properties", ".map",
})
_NO_STORE_RE = re.compile(r"(?i)(?:^|[\s,;])no-store(?:$|[\s,;])")


_JQUERY_BUSTER = "_"


def cache_key(path: str) -> str:
    base, sep, query = path.partition("?")
    if not sep:
        return path
    kept = [kv for kv in query.split("&")
            if kv.split("=", 1)[0] != _JQUERY_BUSTER]
    return base + ("?" + "&".join(kept) if kept else "")


def cacheable_path(method: str, path: str) -> bool:
    if (method or "").upper() != "GET":
        return False
    base = (path or "").split("?", 1)[0]
    return os.path.splitext(base)[1].lower() in _CACHEABLE_EXT


def cache_refusal(status: int, pairs: list[tuple[str, str]]) -> str | None:


    if status != 200:
        return f"status {status}"
    for k, v in pairs:
        lk = k.lower()
        if lk == "set-cookie":
            return "carries Set-Cookie"
        if lk in ("cache-control", "pragma") and _NO_STORE_RE.search(v or ""):
            return f"{k}: {v}"
        if lk == "vary" and any(t.strip().lower() not in ("accept-encoding", "")
                                for t in (v or "").split(",")):
            return f"Vary: {v}"
    return None


def cacheable_reply(status: int, pairs: list[tuple[str, str]]) -> bool:
    return cache_refusal(status, pairs) is None


class AssetCache:

    __slots__ = ("_lock", "_items", "_bytes", "_max_entries", "_max_bytes",
                 "_ttl_s", "hits", "misses")

    def __init__(self, *, max_entries: int = 128, max_bytes: int = 4 * 1024 * 1024,
                 ttl_s: float = 300.0) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, int, list, bytes]] = {}
        self._bytes = 0
        self._max_entries = max(0, int(max_entries))
        self._max_bytes = max(0, int(max_bytes))
        self._ttl_s = float(ttl_s)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[int, list, bytes] | None:
        with self._lock:
            row = self._items.get(key)
            if row is None:
                self.misses += 1
                return None
            expires, status, pairs, body = row
            if expires <= time.time():
                del self._items[key]
                self._bytes -= len(body)
                self.misses += 1
                return None
            self.hits += 1
            return status, list(pairs), body

    def put(self, key: str, status: int, pairs: list, body: bytes) -> None:
        size = len(body)
        if not self._max_entries or size > self._max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old[3])
            self._items[key] = (time.time() + self._ttl_s, status,
                                list(pairs), body)
            self._bytes += size
            while (len(self._items) > self._max_entries
                   or self._bytes > self._max_bytes) and len(self._items) > 1:
                oldest = next(iter(self._items))
                self._bytes -= len(self._items.pop(oldest)[3])

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._items), "bytes": self._bytes,
                    "hits": self.hits, "misses": self.misses}


_CONNECT_FAILURE_MARKS = (
    "connect timeout to",
    "connection refused on",
    "could not connect to",
    "accepted the connection but never sent a response",
)


def is_connect_failure(error: str | None) -> bool:
    low = (error or "").lower()
    return any(m in low for m in _CONNECT_FAILURE_MARKS)


class _DeviceThrottle:

    _PROMOTE_AFTER_S = 3 * 3600.0

    def __init__(self, levels: list[int]) -> None:
        self._levels = levels
        self._level = 0
        self._active = 0
        self._cv = threading.Condition()
        self._promote_at: float | None = None

    @property
    def limit(self) -> int:
        return self._levels[self._level]

    def acquire(self, timeout: float) -> bool:
        with self._cv:
            self._maybe_promote_locked()
            end = time.monotonic() + max(0.0, timeout)
            while self._active >= self._levels[self._level]:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            self._active += 1
            return True

    def release(self) -> None:
        with self._cv:
            self._active -= 1
            self._cv.notify()

    def demote(self) -> int | None:
        with self._cv:
            if self._level >= len(self._levels) - 1:
                self._promote_at = time.monotonic() + self._PROMOTE_AFTER_S
                return None
            self._level += 1
            self._promote_at = time.monotonic() + self._PROMOTE_AFTER_S
            return self._levels[self._level]

    def _maybe_promote_locked(self) -> None:
        if self._promote_at is None or time.monotonic() < self._promote_at:
            return
        self._promote_at = time.monotonic() + self._PROMOTE_AFTER_S
        if self._level > 0:
            self._level -= 1
            self._cv.notify_all()


def _ladder(top: int) -> list[int]:
    top = max(1, int(top))
    return sorted({v for v in (top, 2) if v <= top}, reverse=True) or [top]


MAX_INFLIGHT_PER_SESSION = 16


def parse_ports(spec: str) -> frozenset[int]:
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
    last_used_at: float = field(default=0.0, compare=False)
    db_synced_at: float = field(default=0.0, compare=False)
    injected_auth: str | None = field(default=None, compare=False)
    autofill: tuple[str, str] | None = field(default=None, compare=False)
    cache: AssetCache = field(default_factory=AssetCache, compare=False, repr=False)
    cache_refusals: set = field(default_factory=set, compare=False, repr=False)


class _Pending:

    __slots__ = ("req_id", "org_id", "node_id", "payload", "event", "response",
                 "parked_at", "picked_at", "replied_at")

    def __init__(self, req_id: int, org_id: str, node_id: str, payload: dict) -> None:
        self.req_id = req_id
        self.org_id = org_id
        self.node_id = node_id
        self.payload = payload
        self.event = threading.Event()
        self.response: dict | None = None
        self.parked_at = time.monotonic()
        self.picked_at: float | None = None
        self.replied_at: float | None = None


_SLOW_REQUEST_S = 1.0


def _log_slow(pend: "_Pending", sess: "ProxySession", path: str) -> None:
    total = time.monotonic() - pend.parked_at
    if total < _SLOW_REQUEST_S:
        return
    if pend.picked_at is None:
        log.info("proxy slow dev=%d %s total=%.2fs — the edge never claimed it "
                 "(no worker polling, or the tunnel is dormant)",
                 sess.device_id, path, total)
        return
    queued = pend.picked_at - pend.parked_at
    edge = ((pend.replied_at or time.monotonic()) - pend.picked_at)
    log.info("proxy slow dev=%d %s total=%.2fs queued=%.2fs edge=%.2fs%s",
             sess.device_id, path, total, queued, edge,
             "" if pend.replied_at else " (no reply)")


class ProxyHub:
    def __init__(self, device_max_inflight: int = 4) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ProxySession] = {}
        self._inbox: dict[tuple[str, str], queue.Queue] = {}
        self._pending: dict[int, _Pending] = {}
        self._seq = 0
        self._throttles: dict[tuple[str, int], _DeviceThrottle] = {}
        self._ladder = _ladder(device_max_inflight)
        self._last_poll: dict[tuple[str, str], float] = {}


    def open_session(self, *, org_id: str, device_id: int, node_id: str,
                     device_ip: str, device_port: int, scheme: str,
                     created_by: int, ttl_s: float,
                     cache: AssetCache | None = None) -> ProxySession:
        now = time.time()
        sess = ProxySession(
            sid=secrets.token_urlsafe(24), org_id=org_id, device_id=device_id,
            node_id=node_id, device_ip=device_ip, device_port=device_port,
            scheme=scheme, created_by=created_by, created_at=now,
            expires_at=now + ttl_s, last_used_at=now)
        if cache is not None:
            sess.cache = cache
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
        with self._lock:
            gone = [sid for sid, s in self._sessions.items()
                    if s.org_id == org_id and s.node_id == node_id]
            for sid in gone:
                del self._sessions[sid]
        return gone

    def has_session(self, sid: str) -> bool:
        return self.get_session(sid) is not None

    def reap_expired(self) -> list[str]:
        now = time.time()
        with self._lock:
            gone = [sid for sid, s in self._sessions.items() if s.expires_at <= now]
            for sid in gone:
                del self._sessions[sid]
        return gone

    def extend_session(self, sess: ProxySession, ttl_s: float) -> float:
        with self._lock:
            now = time.time()
            sess.last_used_at = now
            sess.expires_at = max(sess.expires_at, now + ttl_s)
            return sess.expires_at

    def active_sessions_for(self, org_id: str, node_id: str,
                            idle_s: float | None = None) -> list[dict]:

        now = time.time()
        out = []
        with self._lock:
            for sess in self._sessions.values():
                if not (sess.org_id == org_id and sess.node_id == node_id
                        and sess.expires_at > now):
                    continue
                if idle_s is not None and (now - sess.last_used_at) > idle_s:
                    continue
                out.append({"sid": sess.sid,
                            "ttl_s": round(sess.expires_at - now, 1)})
        return out

    def inflight(self, sid: str) -> int:
        with self._lock:
            return sum(1 for p in self._pending.values()
                       if p.payload.get("sid") == sid)


    def submit(self, sess: ProxySession, *, method: str, path: str,
               headers: dict, body: bytes, timeout: float,
               extra: dict | None = None) -> dict | None:

        import base64
        deadline = time.monotonic() + timeout
        throttle = self._throttle(sess.org_id, sess.device_id)
        if not throttle.acquire(deadline - time.monotonic()):
            return None
        try:
            return self._submit_locked_out(
                sess, method=method, path=path, headers=headers, body=body,
                timeout=max(0.0, deadline - time.monotonic()), extra=extra)
        finally:
            throttle.release()

    def _throttle(self, org_id: str, device_id: int) -> _DeviceThrottle:
        key = (org_id, device_id)
        with self._lock:
            t = self._throttles.get(key)
            if t is None:
                t = self._throttles[key] = _DeviceThrottle(self._ladder)
            return t

    def device_limit(self, org_id: str, device_id: int) -> int:
        return self._throttle(org_id, device_id).limit

    def note_failure(self, org_id: str, device_id: int,
                     error: str | None) -> int | None:
        if not is_connect_failure(error):
            return None
        return self._throttle(org_id, device_id).demote()

    def _submit_locked_out(self, sess: ProxySession, *, method: str, path: str,
                           headers: dict, body: bytes, timeout: float,
                           extra: dict | None = None) -> dict | None:
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
        _log_slow(pend, sess, path)
        return pend.response if got else None


    def polled_recently(self, org_id: str, node_id: str, within_s: float) -> bool:
        with self._lock:
            last = self._last_poll.get((org_id, node_id), 0.0)
        return (time.time() - last) <= within_s

    def next_request(self, org_id: str, node_id: str, hold_s: float) -> dict | None:
        with self._lock:
            q = self._inbox.setdefault((org_id, node_id), queue.Queue())
            self._last_poll[(org_id, node_id)] = time.time()
        try:
            pend = q.get(timeout=max(0.0, hold_s))
        except queue.Empty:
            return None
        pend.picked_at = time.monotonic()
        return pend.payload

    def deliver(self, req_id: int, org_id: str, node_id: str, response: dict) -> bool:
        with self._lock:
            pend = self._pending.get(req_id)
            if pend is None or pend.org_id != org_id or pend.node_id != node_id:
                return False
        pend.replied_at = time.monotonic()
        pend.response = response
        pend.event.set()
        return True


_ATTR_RE = re.compile(rb'(?i)\b(href|src|action)\s*=\s*(["\'])/(?!/)')
_CSS_URL_RE = re.compile(rb'(?i)\burl\(\s*(["\']?)/(?!/)')
_COOKIE_PATH_RE = re.compile(r"(?i)(;\s*path=)/")

_REWRITE_CTYPES = ("text/html", "text/css")


def rewrite_headers(sid: str, sess: ProxySession,
                    pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
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
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in _REWRITE_CTYPES or not body:
        return body
    prefix = f"/api/proxy/{sid}".encode()
    body = _CSS_URL_RE.sub(rb"url(\1" + prefix + rb"/", body)
    if ctype == "text/html":
        body = _ATTR_RE.sub(rb"\1=\2" + prefix + rb"/", body)
    return body


AUTOFILL_PATH = "__wisp_autofill__"

_HTML_DOC_RE = re.compile(rb"(?i)<html[\s>]|<!doctype\s+html|</body\s*>|</head\s*>")
_BODY_CLOSE_RE = re.compile(rb"(?i)</body\s*>")
_SCRIPT_BLOCK_RE = re.compile(rb"(?is)<script\b[^>]*>.*?</script\s*>")
_OPEN_SCRIPT_RE = re.compile(rb"(?is)<script\b[^>]*>")
_BOM = b"\xef\xbb\xbf"


def _script_spans(body: bytes) -> list[tuple[int, int]]:
    spans = [m.span() for m in _SCRIPT_BLOCK_RE.finditer(body)]
    m = _OPEN_SCRIPT_RE.search(body, spans[-1][1] if spans else 0)
    if m:
        spans.append((m.start(), len(body)))
    return spans


def _injection_point(body: bytes) -> int | None:

    spans = _script_spans(body)

    def in_js(i: int) -> bool:
        return any(start <= i < end for start, end in spans)

    point = None
    for m in _BODY_CLOSE_RE.finditer(body):
        if not in_js(m.start()):
            point = m.start()
    if point is not None:
        return point
    return None if in_js(len(body) - 1) else len(body)

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
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and ctype not in ("text/html", "application/xhtml+xml"):
        return body
    if not body or not _HTML_DOC_RE.search(body):
        return body
    if not body.lstrip(_BOM).lstrip().startswith(b"<"):
        return body
    point = _injection_point(body)
    if point is None:
        return body
    url = json.dumps(f"/api/proxy/{sid}/{AUTOFILL_PATH}").replace("<", "\\u003c")
    script = _AUTOFILL_JS.replace(b"%URL%", url.encode("utf-8"))
    return body[:point] + script + body[point:]
