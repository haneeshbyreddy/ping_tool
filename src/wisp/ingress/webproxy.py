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

# (status, headers, body) for one device fetch.
Fetcher = Callable[[dict, Config], Awaitable[tuple[int, dict, bytes]]]
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


async def _default_fetch(req: dict, cfg: Config) -> tuple[int, dict, bytes]:
    import httpx
    scheme = req.get("scheme") or "http"
    ip = req["device_ip"]
    port = int(req.get("device_port") or 80)
    url = f"{scheme}://{ip}:{port}{req.get('path') or '/'}"
    raw = base64.b64decode(req["body_b64"]) if req.get("body_b64") else None
    # Split timeout: a LAN device either accepts the connection within a few
    # seconds or never will — the long proxy_request_timeout_s is for slow
    # PAGES, not dead sockets. Without the split, a wrong scheme/port made the
    # operator wait out the full 30s to learn anything.
    timeout = httpx.Timeout(cfg.proxy_request_timeout_s,
                            connect=cfg.proxy_connect_timeout_s)
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False,
                                     timeout=timeout) as client:
            resp = await client.request(req.get("method") or "GET", url, content=raw,
                                        headers=req.get("headers") or {})
    except Exception as exc:
        raise RuntimeError(_friendly_fetch_error(exc, ip, port, scheme)) from exc
    # Pairs, not a dict — repeated names (multiple Set-Cookie) must survive the
    # wire. httpx already decompressed .content; central drops Content-Encoding.
    return resp.status_code, resp.headers.multi_items(), resp.content


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
        self._fetch = fetcher or _default_fetch
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
        try:
            status, headers, body = await self._fetch(req, self._cfg)
        except Exception as exc:  # a dead/slow device must not kill the worker
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
