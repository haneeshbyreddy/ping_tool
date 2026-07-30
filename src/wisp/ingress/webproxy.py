"""Edge web-UI proxy tunnel (webplan.md, M0 tunnel + M1 activation) — central's
hands for HTTP.

Sibling of ingress/walker.py's diagnostic-SNMP path: central parks a browser
request, the edge PULLS it over an outbound long-poll (``/edge/proxy/next``),
fetches it from the LAN device, and POSTs the bytes back (``/edge/proxy/reply``).
The edge never accepts an inbound connection; the `edge dials central` invariant
holds — the workers just hold outbound long-polls open.

Warmth ladder (2026-07-20, first-connect fix): the tunnel is DORMANT (zero
long-polls) until central's /report reply says otherwise. ``proxy_standby`` on
the reply (org has the web proxy enabled) keeps exactly ONE standby worker
long-polling so the FIRST browser request is served immediately instead of
waiting a report cycle for the pool to wake — the deliberate idle cost is one
held central thread per web-proxy org node. A live dashboard session
(``proxy_sessions``) scales the pool to ``proxy_workers``; when the session
deadline lapses the pool drops back to the standby worker, and when standby
stops being refreshed (org toggled off / older central) the node returns to
zero long-polls.

Security spine (mirrors _DiagWalkRunner): a request is served ONLY if its target
IP is in the device list this node currently probes AND its port is in
``proxy_mgmt_ports``. Central already clamped both at session creation; this is the
defense-in-depth re-check, so there is no raw-IP / arbitrary-port pivot even if
central is wrong or hostile.

The device fetch disables TLS verification on purpose — LAN switches/OLTs ship
self-signed or expired certs; the tunnel is the trust boundary, not the device cert.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
from typing import Awaitable, Callable

from wisp.config import CONFIG, Config
from wisp.runtime.central_client import CentralBrainClient, CentralClientError

log = logging.getLogger("wisp.webproxy")

# (status, header pairs, body) for one device fetch. Pairs, not a dict —
# repeated names (multiple Set-Cookie) must survive the wire.
Fetcher = Callable[[dict, Config], Awaitable[tuple[int, list, bytes]]]
# One preflight connect probe: (ip, port, scheme, timeout_s) -> error or None.
Prober = Callable[[str, int, str, float], Awaitable[str | None]]


async def _default_probe(ip: str, port: int, scheme: str,
                         timeout_s: float) -> str | None:
    """TCP (+TLS for https) connect probe — does the endpoint answer at all?
    No HTTP round-trip: the session's first real request is the page load; this
    only has to tell 'listening' from 'dead/wrong scheme' in a few seconds."""
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # LAN self-signed certs, like the fetch
            fut = asyncio.open_connection(ip, port, ssl=ctx)
        else:
            fut = asyncio.open_connection(ip, port)
        _, writer = await asyncio.wait_for(fut, timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return None
    except asyncio.TimeoutError:
        return "connect timeout"
    except Exception as exc:
        return str(exc)[:120] or exc.__class__.__name__


def _allowed_ports(cfg: Config) -> frozenset[int]:
    out: set[int] = set()
    for part in (cfg.proxy_mgmt_ports or "").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 65535:
            out.add(int(part))
    return frozenset(out)


def _web_endpoints(devices: list[dict]) -> frozenset[tuple[str, int]]:
    """Owner-declared (ip, port) web endpoints across the node's devices —
    resolved from the SAME web_ip/web_port/web_scheme fields central used to open
    the session (central/api/proxy.py:_resolve_web_endpoint). A device with any
    override contributes exactly one pair; this is the second column of allowed
    targets, so a port-forwarded admin page is reachable without widening
    proxy_mgmt_ports fleet-wide — and still only to central-declared endpoints."""
    out: set[tuple[str, int]] = set()
    for d in devices:
        web_ip = (d.get("web_ip") or "").strip()
        web_port = d.get("web_port")
        web_scheme = (d.get("web_scheme") or "").strip().lower()
        if not (web_ip or web_port or web_scheme):
            continue
        ip = web_ip or (d.get("ip_address") or "").strip()
        if not ip:
            continue
        try:
            port = int(web_port) if web_port else (443 if web_scheme == "https" else 80)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            out.add((ip, port))
    return frozenset(out)


def _friendly_fetch_error(exc: Exception, ip: str, port: int, scheme: str) -> str:
    """One human sentence per failure mode — this string rides back to the
    browser as central's 502 'edge fetch failed: …', so it must name the fix
    (wrong scheme / wrong port / nothing there), not an httpx class name."""
    import httpx
    other = "https" if scheme == "http" else "http"
    if isinstance(exc, httpx.ConnectTimeout):
        return f"connect timeout to {ip}:{port} — nothing answering there"
    if isinstance(exc, httpx.ConnectError):
        low = str(exc).lower()
        if any(t in low for t in ("ssl", "tls", "certificate", "wrong version",
                                  "record layer", "handshake")):
            return (f"TLS handshake failed on {ip}:{port} — "
                    f"the device likely speaks plain {other} there")
        if "refused" in low:
            return (f"connection refused on {ip}:{port} — "
                    "wrong port or the web UI is disabled")
        return f"could not connect to {ip}:{port}: {str(exc)[:120]}"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)):
        return (f"{ip}:{port} answered with something that isn't {scheme} — "
                f"try {other}")
    if isinstance(exc, httpx.ReadTimeout):
        return f"{ip}:{port} accepted the connection but never sent a response"
    return str(exc)[:300]


class _ClientPool:
    """One KEPT-ALIVE httpx client per device endpoint.

    Until 2026-07-29 every proxied asset built its own ``AsyncClient`` and
    closed it — a fresh TCP connection and a full TLS handshake to the device
    for each of the ~10 files a page pulls. A browser on that LAN opens about
    two connections and reuses them for the whole session, which is precisely
    why the same OLT felt instant locally and took five seconds a click through
    the tunnel: measured over proxy_audit, SRPL-OLT cost 1.00s PER ASSET (a
    7-second page) against 0.25s for a stronger box on the SAME probe, with a
    4.3% fetch-failure rate against 0.1%. On these C-Data OLTs — no AES-NI, a
    few hundred MHz — the handshake IS the page load.

    Clients are keyed on the endpoint, not the session: the same box browsed
    twice should reuse its connection, and the allow-list gate that decides
    whether we may talk to an endpoint at all runs per request in _serve(),
    upstream of here, so pooling widens nothing.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._clients: dict[tuple[str, str, int], tuple[object, float]] = {}
        self._lock = asyncio.Lock()

    def _build(self, scheme: str, ip: str, port: int):
        import httpx
        # Split timeout: a LAN device either accepts the connection within a
        # few seconds or never will — the long proxy_request_timeout_s is for
        # slow PAGES, not dead sockets. Without the split, a wrong scheme/port
        # made the operator wait out the full 30s to learn anything.
        timeout = httpx.Timeout(self._cfg.proxy_request_timeout_s,
                                connect=self._cfg.proxy_connect_timeout_s)
        keep = max(1, int(self._cfg.proxy_device_max_inflight))
        return httpx.AsyncClient(
            verify=False, follow_redirects=False, timeout=timeout,
            limits=httpx.Limits(max_connections=keep,
                                max_keepalive_connections=keep,
                                keepalive_expiry=self._cfg.proxy_keepalive_idle_s))

    async def get(self, scheme: str, ip: str, port: int):
        key = (scheme, ip, port)
        async with self._lock:
            await self._reap_locked()
            row = self._clients.get(key)
            client = row[0] if row else self._build(scheme, ip, port)
            self._clients[key] = (client, time.monotonic())
            return client

    async def drop(self, scheme: str, ip: str, port: int) -> None:
        """Discard an endpoint's client — used when a pooled connection turns
        out to be stale, so the retry starts from a genuinely fresh socket."""
        async with self._lock:
            row = self._clients.pop((scheme, ip, port), None)
        if row:
            await _aclose(row[0])

    async def _reap_locked(self) -> None:
        """Drop client OBJECTS for endpoints nobody has touched in a long while,
        so the dict can't grow with the fleet.

        SOCKET hygiene is not this — it is httpx's own ``keepalive_expiry``
        (``proxy_keepalive_idle_s``), which closes an idle connection without
        disturbing anything in flight. An embedded box has a handful of sockets
        and holding one open all day because a tech looked at it this morning is
        how we become the reason it stops answering, but tearing a client down
        underneath a live request would be worse. Hence a cutoff comfortably
        past the longest a single fetch may take.
        """
        cutoff = time.monotonic() - max(600.0, self._cfg.proxy_keepalive_idle_s,
                                        self._cfg.proxy_request_timeout_s * 4)
        stale = [k for k, (_, seen) in self._clients.items() if seen < cutoff]
        for k in stale:
            client, _ = self._clients.pop(k)
            await _aclose(client)

    async def aclose(self) -> None:
        async with self._lock:
            clients = [c for c, _ in self._clients.values()]
            self._clients.clear()
        for c in clients:
            await _aclose(c)


async def _aclose(client) -> None:
    try:
        await client.aclose()
    except Exception:
        pass


class _DeviceGate:
    """Adaptive per-DEVICE concurrency, walked down from ``proxy_device_max_
    inflight``.

    ``proxy_workers`` bounds what a NODE has in flight; this bounds what one
    BOX does, because the box is what falls over. Two of twenty devices on this
    fleet answer ~1 request at a time and refused 4-5% of what we sent while
    peers on the same probe took 8-9 in parallel and refused 0.1% — a property
    of the firmware, not of the network (ICMP to both was ~3ms, 0% loss).

    Same shape as PysnmpPoller's ladder and for the same reason: NO vendor
    hardcode and no operator-maintained list of weak boxes. Start optimistic,
    drop a rung when a box refuses a CONNECTION (the signature of an overrun
    accept queue — a 404 or a slow page proves nothing about capacity), and
    re-probe one rung faster every few hours so a firmware fix or a reboot
    heals itself.
    """

    _PROMOTE_AFTER_S = 3 * 3600.0

    def __init__(self, cfg: Config) -> None:
        top = max(1, int(cfg.proxy_device_max_inflight))
        # Closed ladder, widest first. Rungs above the configured ceiling are
        # dropped, not clamped: an operator who set the limit to 1 must not
        # find the ladder handing a box 2.
        self._levels = sorted({v for v in (top, 2, 1) if v <= top}, reverse=True)
        self._sems: dict[tuple[str, int], tuple[int, asyncio.Semaphore]] = {}
        self._promote_at: dict[tuple[str, int], float] = {}

    def _level(self, key) -> int:
        row = self._sems.get(key)
        return row[0] if row else 0

    def semaphore(self, ip: str, port: int) -> asyncio.Semaphore:
        key = (ip, port)
        now = time.monotonic()
        due = self._promote_at.get(key)
        if due is not None and now >= due:
            self._set(key, max(0, self._level(key) - 1))
            self._promote_at[key] = now + self._PROMOTE_AFTER_S
        row = self._sems.get(key)
        if row is None:
            self._set(key, 0)
            row = self._sems[key]
        return row[1]

    def demote(self, ip: str, port: int) -> bool:
        """A connection was refused. Returns True if we actually narrowed."""
        key = (ip, port)
        level = self._level(key)
        if level >= len(self._levels) - 1:
            return False
        self._set(key, level + 1)
        self._promote_at[key] = time.monotonic() + self._PROMOTE_AFTER_S
        return True

    def _set(self, key, level: int) -> None:
        level = max(0, min(level, len(self._levels) - 1))
        # A live holder keeps the semaphore it acquired, so swapping the object
        # is safe: the old one drains on its own and is then dropped.
        self._sems[key] = (level, asyncio.Semaphore(self._levels[level]))

    def limit(self, ip: str, port: int) -> int:
        return self._levels[self._level((ip, port))]


class DeviceFetchError(RuntimeError):
    """A device fetch that failed, carrying WHY in a form the caller can act on.

    The message is the operator-facing sentence and still rides back to the
    browser unchanged; ``connect_failure`` is the machine-readable half the
    concurrency ladder reads. Subclasses RuntimeError so every existing
    ``except Exception``/RuntimeError path is untouched.
    """

    def __init__(self, message: str, *, connect_failure: bool = False) -> None:
        super().__init__(message)
        self.connect_failure = connect_failure


def _is_connect_failure(exc: Exception) -> bool:
    """Did we fail to get a working connection, as opposed to getting a bad
    answer over a good one? Only the former says anything about how many
    connections the box can take — a 404 or a slow page proves nothing."""
    import httpx
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                            httpx.PoolTimeout))


def _is_stale_keepalive(exc: Exception) -> bool:
    """The device closed a pooled connection while we were reusing it. Normal
    and expected — embedded servers reap idle sockets aggressively — so it must
    cost one silent retry, never a 502 the tech sees."""
    import httpx
    return isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError,
                            httpx.WriteError, httpx.ConnectError))


async def _fetch_once(client, req: dict) -> tuple[int, list, bytes]:
    scheme = req.get("scheme") or "http"
    url = (f"{scheme}://{req['device_ip']}:{int(req.get('device_port') or 80)}"
           f"{req.get('path') or '/'}")
    raw = base64.b64decode(req["body_b64"]) if req.get("body_b64") else None
    resp = await client.request(req.get("method") or "GET", url, content=raw,
                                headers=req.get("headers") or {})
    # Pairs, not a dict — repeated names (multiple Set-Cookie) must survive the
    # wire. httpx already decompressed .content; central drops Content-Encoding.
    return resp.status_code, resp.headers.multi_items(), resp.content


def make_pooled_fetch(pool: _ClientPool) -> Fetcher:
    async def _fetch(req: dict, cfg: Config) -> tuple[int, list, bytes]:
        scheme = req.get("scheme") or "http"
        ip = req["device_ip"]
        port = int(req.get("device_port") or 80)
        method = (req.get("method") or "GET").upper()
        client = await pool.get(scheme, ip, port)
        try:
            return await _fetch_once(client, req)
        except Exception as exc:
            # A pooled connection the device had already closed is the one
            # failure worth swallowing, and ONLY for a request it is safe to
            # repeat: a POST that died without a reply may still have been
            # applied, and re-submitting a config write is worse than a 502.
            if method in ("GET", "HEAD") and _is_stale_keepalive(exc):
                await pool.drop(scheme, ip, port)
                try:
                    client = await pool.get(scheme, ip, port)
                    return await _fetch_once(client, req)
                except Exception as retry_exc:
                    exc = retry_exc
            raise DeviceFetchError(
                _friendly_fetch_error(exc, ip, port, scheme),
                connect_failure=_is_connect_failure(exc)) from exc
    return _fetch


async def _default_fetch(req: dict, cfg: Config) -> tuple[int, list, bytes]:
    """Unpooled single fetch — the seam tests inject around, and the fallback
    for a tunnel built without a pool."""
    import httpx
    scheme = req.get("scheme") or "http"
    ip = req["device_ip"]
    port = int(req.get("device_port") or 80)
    timeout = httpx.Timeout(cfg.proxy_request_timeout_s,
                            connect=cfg.proxy_connect_timeout_s)
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False,
                                     timeout=timeout) as client:
            return await _fetch_once(client, req)
    except Exception as exc:
        raise RuntimeError(_friendly_fetch_error(exc, ip, port, scheme)) from exc


class ProxyTunnel:
    """A pool of long-poll workers serving central-queued browser requests.

    ``devices_provider`` returns the node's CURRENT device list (the daemon's live
    list, refreshed each cycle) — the allow-list is read per request, so a device
    removed from the node is instantly no longer reachable.
    """

    def __init__(self, client: CentralBrainClient, cfg: Config = CONFIG, *,
                 devices_provider: Callable[[], list[dict]],
                 fetcher: Fetcher | None = None,
                 prober: Prober | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._devices = devices_provider
        # An injected fetcher (tests, and any future transport) stands entirely
        # on its own — it gets no pool, because a double is not something we
        # should be holding connections for.
        self._pool = _ClientPool(cfg) if fetcher is None else None
        self._fetch = fetcher or make_pooled_fetch(self._pool)
        self._gate = _DeviceGate(cfg)
        self._probe = prober or _default_probe
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # monotonic instant past which the SESSION pool stands down
        self._deadline = 0.0
        # monotonic instant past which the lone STANDBY worker stands down;
        # refreshed by every report reply carrying proxy_standby
        self._standby_deadline = 0.0

    # Keep the tunnel warm at least this long past the last signal — a browsing
    # tech must not lose the tunnel between two reports because central's TTL
    # arithmetic and our poll timing disagree by a few seconds.
    _GRACE_S = 30.0
    # Standby survives a couple of slow report cycles (org intervals clamp at
    # 120s) before lapsing; the worst case for a stale flag is one idle
    # long-poll for this long after the org toggles the proxy off.
    _STANDBY_TTL_S = 300.0

    def notify_sessions(self, sessions) -> None:
        """Reply-key hook (``proxy_sessions`` on the /report reply). Called every
        cycle; None/[] while dormant is the common case and a no-op. TTLs arrive
        as RELATIVE seconds (clock-skew safe)."""
        best = 0.0
        for s in sessions or []:
            if isinstance(s, dict):
                try:
                    best = max(best, float(s.get("ttl_s") or 0))
                except (TypeError, ValueError):
                    continue
        if best <= 0:
            return
        self._deadline = max(self._deadline, time.monotonic() + best + self._GRACE_S)
        self._ensure_workers()

    def notify_standby(self, flag: bool) -> None:
        """Reply-key hook (``proxy_standby``): the org has the web proxy
        enabled, so hold ONE long-poll open even with no live session — the
        first browser request must not wait a report cycle for the pool to
        wake. A False/missing flag is a no-op; the standby simply lapses when
        central stops refreshing it."""
        if not flag:
            return
        self._standby_deadline = max(
            self._standby_deadline, time.monotonic() + self._STANDBY_TTL_S)
        self._ensure_workers()

    def _target_workers(self) -> int:
        now = time.monotonic()
        if now < self._deadline:
            return max(1, int(self._cfg.proxy_workers))
        if now < self._standby_deadline:
            return 1
        return 0

    def _ensure_workers(self) -> None:
        """Top the pool up to the current target (full pool > standby > zero).
        Worker slot 0 is the standby-capable one — it honors BOTH deadlines,
        so the pool decays to one worker when a session lapses instead of
        going fully dormant while standby is armed."""
        self._tasks = [t for t in self._tasks if not t.done()]
        target = self._target_workers()
        if len(self._tasks) >= target:
            return
        self._running = True
        for i in range(len(self._tasks), target):
            self._tasks.append(
                asyncio.create_task(self._worker(i), name=f"proxy-tunnel-{i}"))
        log.info("web-proxy tunnel active (%d workers)", target)

    async def aclose(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self._pool is not None:
            await self._pool.aclose()

    def _worker_deadline(self, idx: int) -> float:
        # slot 0 outlives the session pool while standby is armed
        return max(self._deadline, self._standby_deadline) if idx == 0 \
            else self._deadline

    async def _worker(self, idx: int = 0) -> None:
        while self._running and time.monotonic() < self._worker_deadline(idx):
            try:
                req = await asyncio.to_thread(
                    self._client.proxy_next, self._cfg.proxy_poll_hold_s)
            except CentralClientError as exc:
                log.debug("proxy long-poll failed, backing off: %s", exc)
                await asyncio.sleep(2.0)
                continue
            except asyncio.CancelledError:
                return
            if req:
                # served traffic proves a session is alive even if a report
                # cycle is missed — keep the pool warm a little longer, and
                # scale the standby worker up to the full pool (traffic on
                # standby means a session central hasn't told us about yet)
                self._deadline = max(self._deadline,
                                     time.monotonic() + 60.0 + self._GRACE_S)
                self._ensure_workers()
                await self._serve(req)
        log.debug("proxy worker %d standing down", idx)

    async def serve_once(self) -> bool:
        """One poll+serve iteration — the unit-test seam."""
        req = await asyncio.to_thread(
            self._client.proxy_next, self._cfg.proxy_poll_hold_s)
        if not req:
            return False
        await self._serve(req)
        return True

    async def _serve(self, req: dict) -> None:
        if req.get("kind") == "preflight":
            # session-open connect probe — does its own per-candidate gating
            await self._preflight(req)
            return
        sid = req.get("sid")
        req_id = req.get("req_id")
        ip = req.get("device_ip")
        port = int(req.get("device_port") or 0)
        devices = self._devices() or []
        # An owner-declared web endpoint (web_ip/web_port/web_scheme) is allowed as
        # an exact (ip, port) pair; otherwise fall back to the classic gate — the
        # IP must be a device this node probes AND the port must be in
        # proxy_mgmt_ports. Same two field-facing diagnostics as before.
        if (ip, port) not in _web_endpoints(devices):
            if ip not in {d.get("ip_address") for d in devices}:
                await self._reply_error(sid, req_id, "target is not a device this node probes")
                return
            if port not in _allowed_ports(self._cfg):
                await self._reply_error(sid, req_id, f"port {port} not permitted")
                return
        # Bound what this ONE box has in flight. Held across the fetch only —
        # the reply upload to central must not sit on a device's slot.
        try:
            async with self._gate.semaphore(ip, port):
                status, headers, body = await self._fetch(req, self._cfg)
        except Exception as exc:  # a dead/slow device must not kill the worker
            if getattr(exc, "connect_failure", False) and self._gate.demote(ip, port):
                log.info("web-proxy: %s:%d refused a connection — narrowing to "
                         "%d concurrent request(s)", ip, port,
                         self._gate.limit(ip, port))
                if self._pool is not None:
                    await self._pool.drop(req.get("scheme") or "http", ip, port)
            await self._reply_error(sid, req_id, str(exc)[:300])
            return
        b64 = base64.b64encode(body).decode()
        if len(b64) > self._cfg.proxy_max_body_bytes:
            await self._reply_error(sid, req_id, "device response exceeds proxy_max_body_bytes")
            return
        try:
            await asyncio.to_thread(
                self._client.proxy_reply, sid, req_id, status, headers, b64)
        except CentralClientError as exc:
            log.warning("proxy reply upload failed for req %s: %s", req_id, exc)

    async def _preflight(self, req: dict) -> None:
        """Central's session-open probe (kind="preflight"): concurrently
        TCP/TLS-connect each candidate endpoint and report what answered.
        Every candidate passes the SAME allow-list gate as a real fetch —
        the probe must not become a port-scan primitive."""
        sid, req_id = req.get("sid"), req.get("req_id")
        devices = self._devices() or []
        endpoints = _web_endpoints(devices)
        probe_ips = {d.get("ip_address") for d in devices}
        ports = _allowed_ports(self._cfg)
        gated: list[tuple[str, int, str]] = []
        results: list[list] = []
        for cand in (req.get("candidates") or [])[:6]:
            try:
                ip, port, scheme = str(cand[0]), int(cand[1]), str(cand[2] or "http")
            except (TypeError, ValueError, IndexError):
                continue
            if (ip, port) in endpoints or (ip in probe_ips and port in ports):
                gated.append((ip, port, scheme))
            else:
                results.append([ip, port, scheme, False, "not permitted"])
        timeout_s = max(1.0, float(self._cfg.proxy_connect_timeout_s))
        probed = await asyncio.gather(
            *(self._probe(ip, port, scheme, timeout_s) for ip, port, scheme in gated),
            return_exceptions=True)
        for (ip, port, scheme), err in zip(gated, probed):
            if isinstance(err, BaseException):
                err = str(err)[:120] or err.__class__.__name__
            results.append([ip, port, scheme, err is None, err])
        body = json.dumps({"preflight": True, "results": results}).encode()
        try:
            await asyncio.to_thread(
                self._client.proxy_reply, sid, req_id, 200, [],
                base64.b64encode(body).decode())
        except CentralClientError as exc:
            log.warning("preflight reply upload failed for req %s: %s", req_id, exc)

    async def _reply_error(self, sid, req_id, msg: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.proxy_reply, sid, req_id, 502, {}, "", error=msg)
        except CentralClientError:
            pass


def build_proxy_tunnel(client: CentralBrainClient, cfg: Config,
                       devices_provider: Callable[[], list[dict]]) -> ProxyTunnel:
    return ProxyTunnel(client, cfg, devices_provider=devices_provider)
