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

Fetcher = Callable[[dict, Config], Awaitable[tuple[int, list, bytes]]]
Prober = Callable[[str, int, str, float], Awaitable[str | None]]


async def _default_probe(ip: str, port: int, scheme: str,
                         timeout_s: float) -> str | None:
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
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


    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._clients: dict[tuple[str, str, int], tuple[object, float]] = {}
        self._lock = asyncio.Lock()

    def _build(self, scheme: str, ip: str, port: int):
        import httpx
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
        async with self._lock:
            row = self._clients.pop((scheme, ip, port), None)
        if row:
            await _aclose(row[0])

    async def _reap_locked(self) -> None:

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


    _PROMOTE_AFTER_S = 3 * 3600.0

    def __init__(self, cfg: Config) -> None:
        top = max(1, int(cfg.proxy_device_max_inflight))
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
        key = (ip, port)
        level = self._level(key)
        if level >= len(self._levels) - 1:
            return False
        self._set(key, level + 1)
        self._promote_at[key] = time.monotonic() + self._PROMOTE_AFTER_S
        return True

    def _set(self, key, level: int) -> None:
        level = max(0, min(level, len(self._levels) - 1))
        self._sems[key] = (level, asyncio.Semaphore(self._levels[level]))

    def limit(self, ip: str, port: int) -> int:
        return self._levels[self._level((ip, port))]


class DeviceFetchError(RuntimeError):

    def __init__(self, message: str, *, connect_failure: bool = False) -> None:
        super().__init__(message)
        self.connect_failure = connect_failure


def _is_connect_failure(exc: Exception) -> bool:
    import httpx
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                            httpx.PoolTimeout))


def _is_stale_keepalive(exc: Exception) -> bool:
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

    def __init__(self, client: CentralBrainClient, cfg: Config = CONFIG, *,
                 devices_provider: Callable[[], list[dict]],
                 fetcher: Fetcher | None = None,
                 prober: Prober | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._devices = devices_provider
        self._pool = _ClientPool(cfg) if fetcher is None else None
        self._fetch = fetcher or make_pooled_fetch(self._pool)
        self._gate = _DeviceGate(cfg)
        self._probe = prober or _default_probe
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._deadline = 0.0
        self._standby_deadline = 0.0

    _GRACE_S = 30.0
    _STANDBY_TTL_S = 300.0

    def notify_sessions(self, sessions) -> None:
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
                self._deadline = max(self._deadline,
                                     time.monotonic() + 60.0 + self._GRACE_S)
                self._ensure_workers()
                await self._serve(req)
        log.debug("proxy worker %d standing down", idx)

    async def serve_once(self) -> bool:
        req = await asyncio.to_thread(
            self._client.proxy_next, self._cfg.proxy_poll_hold_s)
        if not req:
            return False
        await self._serve(req)
        return True

    async def _serve(self, req: dict) -> None:
        if req.get("kind") == "preflight":
            await self._preflight(req)
            return
        sid = req.get("sid")
        req_id = req.get("req_id")
        ip = req.get("device_ip")
        port = int(req.get("device_port") or 0)
        devices = self._devices() or []
        if (ip, port) not in _web_endpoints(devices):
            if ip not in {d.get("ip_address") for d in devices}:
                await self._reply_error(sid, req_id, "target is not a device this node probes")
                return
            if port not in _allowed_ports(self._cfg):
                await self._reply_error(sid, req_id, f"port {port} not permitted")
                return
        try:
            async with self._gate.semaphore(ip, port):
                status, headers, body = await self._fetch(req, self._cfg)
        except Exception as exc:
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
