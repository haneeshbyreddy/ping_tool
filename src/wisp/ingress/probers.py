"""Probers turn an IP into a (latency, packet-loss) sample.

A single real implementation behind a tiny interface:

* IcmpProber — real ICMP ping via `icmplib`, using unprivileged datagram sockets
  (no root / cap_net_raw; needs the kernel ping group enabled — see the class).

The Prober protocol is intentionally tiny: `async def ping(ip, count)`.
`on_cycle_start()` is an optional hook the daemon calls once per poll cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config


@dataclass(frozen=True)
class PingResult:
    ip: str
    latency_ms: float | None   # None when 100% loss
    packet_loss: float         # 0..100


@runtime_checkable
class Prober(Protocol):
    async def ping(self, ip: str, count: int) -> PingResult: ...

    def on_cycle_start(self) -> None:  # optional; default no-op in concrete classes
        ...


# --- Real ICMP prober -------------------------------------------------------

class IcmpProber:
    """Real ping via icmplib. Imported lazily so the module loads without the
    dependency; an unreachable host becomes a 100%-loss reading rather than
    crashing the poll loop.

    Uses *unprivileged* ICMP datagram sockets (`privileged=False`), so the daemon
    runs as a normal user — no root, no `cap_net_raw`. This needs the kernel ping
    group enabled once on the box:

        sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
        # persist: echo 'net.ipv4.ping_group_range=0 2147483647' | \\
        #   sudo tee /etc/sysctl.d/99-wisp-ping.conf

    A permission error (group not enabled) surfaces as a RuntimeError so the
    misconfig is obvious rather than every device silently reading 'down'.
    """

    def __init__(self, *, interval: float = 0.2, timeout: float = 1.0) -> None:
        self._interval = interval
        self._timeout = timeout

    def on_cycle_start(self) -> None:
        return None

    async def ping(self, ip: str, count: int) -> PingResult:
        try:
            from icmplib import async_ping
            from icmplib.exceptions import SocketPermissionError
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "IcmpProber needs 'icmplib' (pip install icmplib)."
            ) from exc
        try:
            host = await async_ping(
                ip, count=count, interval=self._interval,
                timeout=self._timeout, privileged=False,
            )
        except SocketPermissionError as exc:  # ping group not enabled on this box
            raise RuntimeError(
                "ICMP needs the kernel ping group enabled: "
                'sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"'
            ) from exc
        except OSError:
            # host unreachable, name resolution failure, etc.
            return PingResult(ip, None, 100.0)
        loss = round(host.packet_loss * 100, 1)
        latency = host.avg_rtt if host.packets_received else None
        return PingResult(ip, latency, loss)


def build_prober(cfg: Config = CONFIG) -> Prober:
    return IcmpProber()
