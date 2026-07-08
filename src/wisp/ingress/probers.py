from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from wisp.config import CONFIG, Config

_PRIVILEGED_ICMP = sys.platform.startswith("win")

@dataclass(frozen=True)
class PingResult:
    ip: str
    latency_ms: float | None
    packet_loss: float
    jitter_ms: float | None = None

@runtime_checkable
class Prober(Protocol):
    async def ping(self, ip: str, count: int) -> PingResult: ...

    def on_cycle_start(self) -> None:
        ...

    async def preflight(self) -> None:
        ...

class IcmpProber:

    def __init__(self, *, interval: float = 0.2, timeout: float = 1.0) -> None:
        self._interval = interval
        self._timeout = timeout

    def on_cycle_start(self) -> None:
        return None

    async def preflight(self) -> None:
        await self.ping("127.0.0.1", count=1)

    async def ping(self, ip: str, count: int) -> PingResult:
        try:
            from icmplib import async_ping
            from icmplib.exceptions import SocketPermissionError
        except ImportError as exc:
            raise RuntimeError(
                "IcmpProber needs 'icmplib' (pip install icmplib)."
            ) from exc
        try:
            host = await async_ping(
                ip, count=count, interval=self._interval,
                timeout=self._timeout, privileged=_PRIVILEGED_ICMP,
            )
        except SocketPermissionError as exc:
            if _PRIVILEGED_ICMP:
                raise RuntimeError(
                    "ICMP on Windows needs raw sockets — run the monitor as "
                    "Administrator/SYSTEM (the install.ps1 Scheduled Task does this)."
                ) from exc
            raise RuntimeError(
                "ICMP needs the kernel ping group enabled: "
                'sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"'
            ) from exc
        except OSError:
            return PingResult(ip, None, 100.0)
        loss = round(host.packet_loss * 100, 1)
        latency = host.avg_rtt if host.packets_received else None
        jitter = getattr(host, "jitter", None) if host.packets_received else None
        return PingResult(ip, latency, loss, jitter)

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0
_ECHO_PAYLOAD = b"wisp-edge-probe."

def icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF

def build_echo_request(ident: int, seq: int, payload: bytes = _ECHO_PAYLOAD) -> bytes:
    header = struct.pack("!BBHHH", _ICMP_ECHO_REQUEST, 0, 0, ident, seq)
    csum = icmp_checksum(header + payload)
    return struct.pack("!BBHHH", _ICMP_ECHO_REQUEST, 0, csum, ident, seq) + payload

def parse_echo_reply(datagram: bytes) -> tuple[int, int] | None:
    if len(datagram) < 28 or datagram[0] >> 4 != 4:
        return None
    ihl = (datagram[0] & 0x0F) * 4
    icmp = datagram[ihl:]
    if len(icmp) < 8 or icmp[0] != _ICMP_ECHO_REPLY or icmp[1] != 0:
        return None
    ident, seq = struct.unpack("!HH", icmp[4:8])
    return ident, seq

class _PendingEcho:
    __slots__ = ("ip", "sent_at", "loop", "future")

    def __init__(self, ip: str, loop: asyncio.AbstractEventLoop,
                 future: asyncio.Future) -> None:
        self.ip = ip
        self.sent_at = 0.0
        self.loop = loop
        self.future = future

def _default_raw_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)

class SingleSocketIcmpProber:

    def __init__(self, *, interval: float = 0.2, timeout: float = 1.0,
                 sock_factory: Callable[[], socket.socket] = _default_raw_socket) -> None:
        self._interval = interval
        self._timeout = timeout
        self._sock_factory = sock_factory
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._pending: dict[int, _PendingEcho] = {}
        self._seq = int(time.monotonic() * 1000) & 0xFFFF
        self._ident = os.getpid() & 0xFFFF
        self._v6_fallback: IcmpProber | None = None

    def on_cycle_start(self) -> None:
        return None

    async def preflight(self) -> None:
        await self.ping("127.0.0.1", count=1)

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _ensure_started(self) -> None:
        if self._sock is not None:
            return
        with self._start_lock:
            if self._sock is not None:
                return
            try:
                sock = self._sock_factory()
            except OSError as exc:
                if sys.platform.startswith("win"):
                    raise RuntimeError(
                        "ICMP on Windows needs raw sockets — run the monitor as "
                        "Administrator/SYSTEM (the installer's Scheduled Task does this)."
                    ) from exc
                raise RuntimeError(
                    "SingleSocketIcmpProber needs a raw ICMP socket (root/cap_net_raw)."
                ) from exc
            sock.settimeout(1.0)
            self._sock = sock
            self._thread = threading.Thread(
                target=self._recv_loop, name="wisp-icmp-recv", daemon=True)
            self._thread.start()

    def _recv_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            received_at = time.perf_counter()
            parsed = parse_echo_reply(data)
            if parsed is None or parsed[0] != self._ident:
                continue
            with self._lock:
                pend = self._pending.get(parsed[1])
                if pend is None or pend.ip != addr[0]:
                    continue
                del self._pending[parsed[1]]
            rtt_ms = (received_at - pend.sent_at) * 1000.0
            pend.loop.call_soon_threadsafe(self._resolve, pend.future, rtt_ms)

    @staticmethod
    def _resolve(future: asyncio.Future, rtt_ms: float) -> None:
        if not future.done():
            future.set_result(rtt_ms)

    def _next_seq(self) -> int:
        with self._lock:
            while True:
                self._seq = (self._seq + 1) & 0xFFFF
                if self._seq not in self._pending:
                    return self._seq

    async def ping(self, ip: str, count: int) -> PingResult:
        if ":" in ip:
            if self._v6_fallback is None:
                self._v6_fallback = IcmpProber(interval=self._interval,
                                               timeout=self._timeout)
            return await self._v6_fallback.ping(ip, count)
        self._ensure_started()
        assert self._sock is not None
        loop = asyncio.get_running_loop()
        rtts: list[float] = []
        for i in range(count):
            seq = self._next_seq()
            pend = _PendingEcho(ip, loop, loop.create_future())
            packet = build_echo_request(self._ident, seq)
            with self._lock:
                self._pending[seq] = pend
            try:
                pend.sent_at = time.perf_counter()
                self._sock.sendto(packet, (ip, 0))
                rtts.append(await asyncio.wait_for(pend.future, self._timeout))
            except (asyncio.TimeoutError, OSError):
                pass
            finally:
                with self._lock:
                    self._pending.pop(seq, None)
            if i + 1 < count:
                await asyncio.sleep(self._interval)
        if not rtts:
            return PingResult(ip, None, 100.0)
        loss = round((count - len(rtts)) * 100.0 / count, 1)
        latency = sum(rtts) / len(rtts)
        jitter = (sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (len(rtts) - 1)
                  if len(rtts) > 1 else 0.0)
        return PingResult(ip, latency, loss, jitter)

def build_prober(cfg: Config = CONFIG) -> Prober:
    if cfg.prober == "icmplib":
        return IcmpProber()
    if cfg.prober == "singlesock" or sys.platform.startswith("win"):
        return SingleSocketIcmpProber()
    return IcmpProber()
