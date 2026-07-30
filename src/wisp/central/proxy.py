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
import logging
import os
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("wisp.central")

# ---- per-session static-asset cache ------------------------------------------
#
# 44% of every request this tunnel has ever carried was a re-fetch of an
# UNCHANGING static asset inside ONE session (measured 2026-07-29 across the
# whole of proxy_audit: jquery-1.7.1.min.js alone is 553 fetches of SRPL-OLT and
# 1083 of HLY-OLT-1). This firmware ships no usable cache headers and its
# frameset re-requests the entire script set on every click, so the browser has
# no validator to revalidate with and simply asks again — down a tunnel where
# one asset costs a fresh TCP+TLS handshake to a weak embedded server. On the
# C-Data boxes that measured 1.00s PER ASSET, i.e. a 7-second page.
#
# Deliberately NOT an HTTP cache: no revalidation, no Age, no shared store. It
# is a bounded per-session memo of the handful of scripts and images a device UI
# re-serves verbatim, which is the whole of the observed waste.
#
# Three properties are load-bearing:
#   * it stores the DEVICE'S RAW REPLY, before rewrite_body/inject_autofill, so
#     a hit is byte-identical to a miss downstream — there stays exactly ONE
#     rewriting path and the cache is a stand-in for the device, nothing more;
#   * it lives ON the session (a field below), so it dies when the session does
#     and there is no cross-session — let alone cross-org — key to get wrong;
#   * the QUERY STRING is part of the key, so this firmware's own cache-busting
#     (`/js/misc.js?rand=52258`, a fresh number per page) keeps missing, exactly
#     as the vendor intended. Second-guessing a deliberate bust is how a cache
#     starts serving a stale page nobody can explain.

# Extensions we will serve from memory. A CLOSED vocabulary, like every other
# one here: the alternative is inferring which paths are safe, and this vendor's
# DYNAMIC pages are .html (/action/onuauthinfo.html) — precisely the class a
# wrong inference would start serving stale.
_CACHEABLE_EXT = frozenset({
    ".js", ".css", ".png", ".gif", ".jpg", ".jpeg", ".ico", ".svg", ".bmp",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".properties", ".map",
})
# `no-store` is the only one that means "do not write this down" — see
# cache_refusal for why `no-cache`/`private` are deliberately not here.
_NO_STORE_RE = re.compile(r"(?i)(?:^|[\s,;])no-store(?:$|[\s,;])")


# jQuery's OWN cache-buster. `$.ajax({cache:false})` appends `_=<timestamp>` to
# every request it makes — it is a statement by the CLIENT LIBRARY about the
# browser's HTTP cache, not by the vendor about the resource. Stripping it from
# the key is therefore NOT the same act as honouring `?rand=`, which this
# firmware's own HTML writes per script tag and which stays keyed.
#
# It is worth the distinction: 20% of every request the tunnel carries is a
# static `.properties` translation table wearing one of these
# (`/i18N/error_en_US.properties?_=1785323171532` — 5,919 fetches fleet-wide of
# a file that has never changed). Keyed literally, every one of them is a miss
# forever.
_JQUERY_BUSTER = "_"


def cache_key(path: str) -> str:
    """The cache key for a request path: itself, minus jQuery's `_=` param.
    Every other query parameter — including the vendor's `rand=` — is kept, so
    anything the firmware deliberately busts keeps missing."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    kept = [kv for kv in query.split("&")
            if kv.split("=", 1)[0] != _JQUERY_BUSTER]
    return base + ("?" + "&".join(kept) if kept else "")


def cacheable_path(method: str, path: str) -> bool:
    """Is this request one we may answer from memory? Method + extension only —
    the response side is judged separately (``cacheable_reply``), because a
    request can look static and still come back with a cookie on it."""
    if (method or "").upper() != "GET":
        return False
    base = (path or "").split("?", 1)[0]
    return os.path.splitext(base)[1].lower() in _CACHEABLE_EXT


def cache_refusal(status: int, pairs: list[tuple[str, str]]) -> str | None:
    """Judge the DEVICE's answer: None if we may remember it, else a short
    reason (which gets logged, so a blank cache is never a mystery again).

    A static extension is a hint about the URL, never a promise about the
    response — so state (Set-Cookie), an unkeyed Vary, or an explicit `no-store`
    each disqualify one.

    **`no-cache` and `private` deliberately do NOT.** That is an override of the
    device and it is narrow on purpose:

      * `private` is a directive to SHARED caches. This one is per session, in
        process, and dies with the credential that opened it — it is a private
        cache by construction, so honouring `private` was simply a misreading.
      * `no-cache` means "store, but revalidate before reuse" (only `no-store`
        means don't write it down). This firmware ships neither ETag nor
        Last-Modified, so there is nothing to revalidate WITH — honouring it
        literally means the cache can never work on the entire fleet, which is
        how we ended up re-fetching a 2011 jQuery 553 times in one session over
        a link where each fetch is a fresh TLS handshake.

    What makes defying it safe is the vendor's own signal: this UI cache-busts
    the JS it considers volatile (`/js/misc.js?rand=62245`, a fresh number every
    page load) and leaves the stable files bare. The query string is part of the
    cache key, so every file the firmware marks as changing keeps missing. We
    only ever hold back the ones it re-sends byte-identical.
    """
    if status != 200:
        return f"status {status}"
    for k, v in pairs:
        lk = k.lower()
        if lk == "set-cookie":
            return "carries Set-Cookie"
        if lk in ("cache-control", "pragma") and _NO_STORE_RE.search(v or ""):
            return f"{k}: {v}"
        # Accept-Encoding is already resolved (the edge hands us decoded bytes
        # and Content-Encoding is dropped), so it is the one Vary we can honour.
        if lk == "vary" and any(t.strip().lower() not in ("accept-encoding", "")
                                for t in (v or "").split(",")):
            return f"Vary: {v}"
    return None


def cacheable_reply(status: int, pairs: list[tuple[str, str]]) -> bool:
    return cache_refusal(status, pairs) is None


class AssetCache:
    """Bounded, thread-safe, FIFO store of one session's static assets.

    Browser worker threads hit this concurrently, hence the lock. FIFO rather
    than LRU on purpose: the working set is a device UI's fixed script list,
    which either fits or doesn't — recency ranking buys nothing and costs a
    bookkeeping structure to get wrong.
    """

    __slots__ = ("_lock", "_items", "_bytes", "_max_entries", "_max_bytes",
                 "_ttl_s", "hits", "misses")

    def __init__(self, *, max_entries: int = 128, max_bytes: int = 4 * 1024 * 1024,
                 ttl_s: float = 300.0) -> None:
        self._lock = threading.Lock()
        # key -> (expires_at, status, header pairs, body)
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
            # copy the pair list — the caller filters and rewrites it in place
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
            # dict preserves insertion order, so the first key IS the oldest.
            # Never evict the entry just stored: a body larger than the whole
            # budget is refused above, so this loop always terminates.
            while (len(self._items) > self._max_entries
                   or self._bytes > self._max_bytes) and len(self._items) > 1:
                oldest = next(iter(self._items))
                self._bytes -= len(self._items.pop(oldest)[3])

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._items), "bytes": self._bytes,
                    "hits": self.hits, "misses": self.misses}


# ---- per-device concurrency, adaptive -----------------------------------------
#
# PROVEN 2026-07-29, not inferred: every one of SRPL-OLT's ~4.3% failures logged
# `connect timeout to 172.168.99.245:443` — the TCP connect never completed, so
# this was never a slow page or a slow link. A box silently DROPPING connection
# attempts (rather than refusing them) is an overrun accept queue, and the
# client's SYN retransmit timer is why the good requests measured a dead-on
# 1.00s median: one retransmit each. The ones that lost the race outright burned
# the whole 5s connect budget and 502'd, whereupon the browser asked again.
#
# So the lever is HOW MANY CONNECTIONS AT ONCE the box is asked to accept.
# `proxy_workers` bounds a NODE's tunnel; this bounds one DEVICE, because the
# device is what falls over — and it lives on CENTRAL as well as the edge
# because central sees every failure string and needs no fleet rollout to start
# helping. The two converge on the same rung; the tighter one wins.
#
# NO vendor hardcode and no operator-kept list of weak boxes, same as
# PysnmpPoller's ladder: start at the ceiling, drop a rung on a CONNECT failure,
# re-probe one rung faster every few hours so a reboot or a firmware fix heals
# without anyone noticing it happened.

# Fragments of the edge's own failure sentences (ingress/webproxy.py:
# _friendly_fetch_error) that mean "we could not get a working connection".
# Matching on prose is a coupling, so `unit/test_webproxy` drives the real
# function with real httpx exceptions and fails if the wording drifts.
#
# Deliberately EXCLUDED: a TLS-version mismatch and a non-HTTP reply are
# configuration errors that fail identically at any concurrency, and narrowing
# for them would slow a device down to punish it for a wrong port.
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
    """Live-resizable in-flight limit for ONE device.

    A Condition rather than a Semaphore precisely because the capacity moves:
    a semaphore's value is fixed at construction, so narrowing would mean
    swapping the object and hoping every in-flight holder releases the one it
    took. Here the limit is just a number the waiters re-read.
    """

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
        """Narrow one rung. Returns the new limit, or None if already at the
        floor (there is no such thing as half a connection)."""
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
            self._cv.notify_all()   # the widened limit may free waiters


# The ladder FLOORS AT 2, and that is measured, not cautious (2026-07-29,
# SRPL-OLT, median in-burst gap between assets):
#
#     limit 4 (no throttle)  1.00s   — connections dropped, each costing 5s
#     limit 2                0.00s   — several assets inside one second
#     limit 1                1.50s   — WORST of the three
#
# One-at-a-time is slowest because the tunnel is a PIPELINE: while the edge is
# uploading one reply and re-issuing its long-poll, a second request should be
# in flight covering those WAN legs. Serialise it and every asset pays for that
# dead air end to end. So the failure this ladder exists to stop is real, but
# curing it by going to 1 costs more than the failures did — the honest floor is
# the narrowest rung that still overlaps.
def _ladder(top: int) -> list[int]:
    """Closed ladder, widest first. Rungs above the configured ceiling are
    dropped, not clamped: an operator who set the limit to 1 must not find the
    ladder handing a box 2."""
    top = max(1, int(top))
    return sorted({v for v in (top, 2) if v <= top}, reverse=True) or [top]


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
    # Last proxied request on this session. `expires_at` already encodes it
    # (last activity + ttl), but only as long as nothing else ever moves the
    # expiry — so the fact is stored rather than inferred. It is what tells an
    # ABANDONED session from a live one: a browser tab that was closed (or a
    # laptop that was shut) sends no more requests, and there is no close event
    # to catch server-side, so "when did a human last touch this" is the only
    # signal there is. The web-optics sweeper defers to a session solely on this
    # (weboptics_sweep.py) — deferring to a session that merely still EXISTS is
    # what let one forgotten tab block a probe's whole optical read.
    last_used_at: float = field(default=0.0, compare=False)
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
    # This session's static-asset memo. On the session rather than the hub so
    # it is collected with the session and can never outlive the credential
    # that opened it. Replaced by open_session with a config-sized one; the
    # default keeps a bare ProxySession (the preflight probe builds one) valid.
    cache: AssetCache = field(default_factory=AssetCache, compare=False, repr=False)
    # Cache-refusal reasons already logged for this session, so a device that
    # stamps one header on every asset writes one line and not one per file.
    cache_refusals: set = field(default_factory=set, compare=False, repr=False)


class _Pending:
    """One in-flight browser request awaiting the edge's reply.

    The three stamps split the round trip at the only two points central can
    see it: when the edge's long-poll CLAIMED the request, and when its reply
    landed. `queued` (park -> claim) is a tunnel-side cost — no worker was free,
    or none was polling. `edge` (claim -> reply) is the device fetch plus the
    reply upload. Without the split, a slow page is just "slow somewhere",
    which is how this subsystem burned two restarts guessing.
    """

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


# A tunnelled asset that takes longer than this is worth a line. On a healthy
# device most assets land well inside it, so the log stays quiet until something
# is actually wrong — and then it says WHERE, which is the whole point.
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
        # (org, device) -> its live concurrency rung. Keyed on the DEVICE and
        # not the session, so a reopened tab inherits what we already learned
        # about the box rather than starting the ladder over.
        self._throttles: dict[tuple[str, int], _DeviceThrottle] = {}
        self._ladder = _ladder(device_max_inflight)
        # last time each node's tunnel long-polled us — the preflight gate:
        # a submit against a node that isn't polling would just eat its timeout
        self._last_poll: dict[tuple[str, str], float] = {}

    # -- sessions --------------------------------------------------------------

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
        """One tunnel per probe: drop every live session riding this node.
        Returns the closed sids so the caller can retire their DB rows."""
        with self._lock:
            gone = [sid for sid, s in self._sessions.items()
                    if s.org_id == org_id and s.node_id == node_id]
            for sid in gone:
                del self._sessions[sid]
        return gone

    def has_session(self, sid: str) -> bool:
        """Is this session live RIGHT NOW? Expiry-aware on purpose: the
        dashboard's "live" badge and its pulsing globe icon are read off this,
        and a plain membership test kept both claiming a session was open long
        after it had timed out — sessions are only ever dropped lazily, so an
        abandoned one sits in the dict until something asks about it."""
        return self.get_session(sid) is not None

    def reap_expired(self) -> list[str]:
        """Drop every timed-out session and return their sids so the caller can
        retire the DB rows. Sessions expire on their own clock but were only
        ever removed when something happened to look one up — nothing looks up
        a session whose tab is gone, so they accumulated for the life of the
        process and went on being advertised as open."""
        now = time.time()
        with self._lock:
            gone = [sid for sid, s in self._sessions.items() if s.expires_at <= now]
            for sid in gone:
                del self._sessions[sid]
        return gone

    def extend_session(self, sess: ProxySession, ttl_s: float) -> float:
        """Activity keeps a session alive: push expiry to now+ttl (never
        shortens). Returns the new expires_at epoch."""
        with self._lock:
            now = time.time()
            sess.last_used_at = now
            sess.expires_at = max(sess.expires_at, now + ttl_s)
            return sess.expires_at

    def active_sessions_for(self, org_id: str, node_id: str,
                            idle_s: float | None = None) -> list[dict]:
        """Live sessions this node should serve — rides the /report reply so a
        dormant edge learns to spin its tunnel up (webplan.md §2). TTL is sent
        RELATIVE (seconds remaining), never as a wall-clock timestamp: the edge's
        clock is not trusted to agree with central's.

        ``idle_s`` narrows the answer from "not expired" to "in USE": only
        sessions touched within that many seconds count. The edge path passes
        nothing and keeps holding its tunnel for the session's whole TTL —
        that is what the tunnel is for. The web-optics sweeper passes a window,
        because for it the question is not "does a session exist" but "is there
        a human at the keyboard I would be logging out".
        """
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

    # -- browser side (blocks the calling worker thread) -----------------------

    def submit(self, sess: ProxySession, *, method: str, path: str,
               headers: dict, body: bytes, timeout: float,
               extra: dict | None = None) -> dict | None:
        """Park a browser request for the edge and wait for the reply. Returns the
        reply dict (``status``/``headers``/``body``), or None on timeout.
        ``extra`` keys are merged into the parked payload (the preflight probe
        rides this); the normal device_ip/port/scheme fields stay present, so an
        edge that predates a given extra treats it as a plain fetch.

        The per-device concurrency gate is taken HERE rather than in the browser
        route, so every caller — a tab, the web-optics sweeper, the session-open
        preflight — is bounded by the same rung. A device is a device whoever is
        asking, and a gate a new caller can forget to take is not a gate.
        """
        import base64
        deadline = time.monotonic() + timeout
        throttle = self._throttle(sess.org_id, sess.device_id)
        if not throttle.acquire(deadline - time.monotonic()):
            return None   # never got a slot: indistinguishable from a timeout,
        try:              # and that IS what it is from the browser's side
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
        """A fetch came back failed. Narrow this device's rung if — and only if
        — we could not get a CONNECTION: a 404 or a slow page says nothing about
        how many connections the box can take. Returns the new limit when it
        actually narrowed, so the caller can say so once."""
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
        pend.picked_at = time.monotonic()
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
        pend.replied_at = time.monotonic()
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
# A closed <script>…</script>, and a bare opening tag (for one left unterminated
# at EOF). Both are needed to answer "is this offset inside JavaScript?".
_SCRIPT_BLOCK_RE = re.compile(rb"(?is)<script\b[^>]*>.*?</script\s*>")
_OPEN_SCRIPT_RE = re.compile(rb"(?is)<script\b[^>]*>")
_BOM = b"\xef\xbb\xbf"


def _script_spans(body: bytes) -> list[tuple[int, int]]:
    """Byte ranges of the body that are JavaScript, not markup."""
    spans = [m.span() for m in _SCRIPT_BLOCK_RE.finditer(body)]
    # An unterminated <script> swallows everything to EOF — the parser is still
    # inside JS there, so the "just append at the end" fallback is not safe.
    m = _OPEN_SCRIPT_RE.search(body, spans[-1][1] if spans else 0)
    if m:
        spans.append((m.start(), len(body)))
    return spans


def _injection_point(body: bytes) -> int | None:
    """Offset to splice the bootstrap in at — the last ``</body>`` that is NOT
    inside a <script>, else the end of the body; None when there is nowhere safe.

    The naive "last ``</body>``" cost this feature a whole switch fleet. DCN's
    .asp UI builds its frames from JS, so the last ``</body>`` in tabctrl.asp
    lives INSIDE a ``document.write("…</body>…")`` string — splicing a multi-line
    <script> there puts a raw newline in a JS string literal ("SyntaxError:
    string literal contains an unescaped line break"), which kills the page's own
    script before it navigates the content frame. The tab bar rendered, every
    request 200'd, and the UI simply never loaded a page: broken by us, and
    invisible in the audit log. A device whose credentials were never stored
    (autofill disarmed, nothing injected) browsed fine the whole time — that
    contrast is what identified this."""
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
    # A document opens with markup; a script opens with code. The sniff above
    # matches any body CONTAINING a document marker, and old firmware serves
    # .js with no Content-Type at all — so without this, a common.js that
    # merely document.write()s "</body>" somewhere reads as a page and gets a
    # <script> tag appended INTO the JavaScript.
    if not body.lstrip(_BOM).lstrip().startswith(b"<"):
        return body
    point = _injection_point(body)
    if point is None:
        return body
    url = json.dumps(f"/api/proxy/{sid}/{AUTOFILL_PATH}").replace("<", "\\u003c")
    script = _AUTOFILL_JS.replace(b"%URL%", url.encode("utf-8"))
    return body[:point] + script + body[point:]
