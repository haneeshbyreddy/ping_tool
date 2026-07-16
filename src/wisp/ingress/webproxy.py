"""Edge web-UI proxy tunnel (webplan.md, M0 tunnel + M1 activation) — central's
hands for HTTP.

Sibling of ingress/walker.py's diagnostic-SNMP path: central parks a browser
request, the edge PULLS it over an outbound long-poll (``/edge/proxy/next``),
fetches it from the LAN device, and POSTs the bytes back (``/edge/proxy/reply``).
The edge never accepts an inbound connection; the `edge dials central` invariant
holds — the workers just hold outbound long-polls open.

The tunnel is DORMANT until central's /report reply carries a ``proxy_sessions``
key (a live dashboard session for this node): ``notify_sessions`` arms a
deadline off the longest session TTL and spins the workers up; when the deadline
lapses with no refresh, the workers exit and the node holds zero long-polls
again. Idle cost must be zero — each held long-poll ties up a central worker
thread (webplan.md §2).

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
import logging
import time
from typing import Awaitable, Callable

from wisp.config import CONFIG, Config
from wisp.runtime.central_client import CentralBrainClient, CentralClientError

log = logging.getLogger("wisp.webproxy")

# (status, headers, body) for one device fetch.
Fetcher = Callable[[dict, Config], Awaitable[tuple[int, dict, bytes]]]


def _allowed_ports(cfg: Config) -> frozenset[int]:
    out: set[int] = set()
    for part in (cfg.proxy_mgmt_ports or "").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 65535:
            out.add(int(part))
    return frozenset(out)


async def _default_fetch(req: dict, cfg: Config) -> tuple[int, dict, bytes]:
    import httpx
    scheme = req.get("scheme") or "http"
    ip = req["device_ip"]
    port = int(req.get("device_port") or 80)
    url = f"{scheme}://{ip}:{port}{req.get('path') or '/'}"
    raw = base64.b64decode(req["body_b64"]) if req.get("body_b64") else None
    async with httpx.AsyncClient(verify=False, follow_redirects=False,
                                 timeout=cfg.proxy_request_timeout_s) as client:
        resp = await client.request(req.get("method") or "GET", url, content=raw,
                                    headers=req.get("headers") or {})
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
                 fetcher: Fetcher | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._devices = devices_provider
        self._fetch = fetcher or _default_fetch
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # monotonic instant past which the workers stand down (dormant tunnel)
        self._deadline = 0.0

    # Keep the tunnel warm at least this long past the last signal — a browsing
    # tech must not lose the tunnel between two reports because central's TTL
    # arithmetic and our poll timing disagree by a few seconds.
    _GRACE_S = 30.0

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

    def _ensure_workers(self) -> None:
        if any(not t.done() for t in self._tasks):
            return
        self._running = True
        n = max(1, int(self._cfg.proxy_workers))
        self._tasks = [asyncio.create_task(self._worker(), name=f"proxy-tunnel-{i}")
                       for i in range(n)]
        log.info("web-proxy tunnel active (%d workers)", n)

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

    async def _worker(self) -> None:
        while self._running and time.monotonic() < self._deadline:
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
                # served traffic proves the session is alive even if a report
                # cycle is missed — keep the tunnel warm a little longer
                self._deadline = max(self._deadline,
                                     time.monotonic() + 60.0 + self._GRACE_S)
                await self._serve(req)
        log.debug("proxy worker standing down (no live sessions)")

    async def serve_once(self) -> bool:
        """One poll+serve iteration — the unit-test seam."""
        req = await asyncio.to_thread(
            self._client.proxy_next, self._cfg.proxy_poll_hold_s)
        if not req:
            return False
        await self._serve(req)
        return True

    async def _serve(self, req: dict) -> None:
        sid = req.get("sid")
        req_id = req.get("req_id")
        ip = req.get("device_ip")
        port = int(req.get("device_port") or 0)
        allowed_ips = {d.get("ip_address") for d in (self._devices() or [])}
        if ip not in allowed_ips:
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

    async def _reply_error(self, sid, req_id, msg: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.proxy_reply, sid, req_id, 502, {}, "", error=msg)
        except CentralClientError:
            pass


def build_proxy_tunnel(client: CentralBrainClient, cfg: Config,
                       devices_provider: Callable[[], list[dict]]) -> ProxyTunnel:
    return ProxyTunnel(client, cfg, devices_provider=devices_provider)
