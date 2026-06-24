"""Probers turn an IP into a (latency, packet-loss) sample.

A single real implementation behind a tiny interface:

* IcmpProber — real ICMP ping via `icmplib`, using unprivileged datagram sockets
  (no root / cap_net_raw; needs the kernel ping group enabled — see the class).

The Prober protocol is intentionally tiny: `async def ping(ip, count)`.
`on_cycle_start()` is an optional hook the daemon calls once per poll cycle.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config

# Linux/Unix offer unprivileged ICMP datagram sockets (the kernel ping group). Windows
# has no such socket type, so icmplib must use raw sockets there — which require
# Administrator/SYSTEM. Pick the right mode per OS rather than hardcoding one.
_PRIVILEGED_ICMP = sys.platform.startswith("win")


@dataclass(frozen=True)
class PingResult:
    ip: str
    latency_ms: float | None   # None when 100% loss
    packet_loss: float         # 0..100
    jitter_ms: float | None = None  # mean RTT variation; feeds core/baseline.py


@runtime_checkable
class Prober(Protocol):
    async def ping(self, ip: str, count: int) -> PingResult: ...

    def on_cycle_start(self) -> None:  # optional; default no-op in concrete classes
        ...

    async def preflight(self) -> None:  # optional; verify the prober can actually probe
        ...


# --- Real ICMP prober -------------------------------------------------------

class IcmpProber:
    """Real ping via icmplib. Imported lazily so the module loads without the
    dependency; an unreachable host becomes a 100%-loss reading rather than
    crashing the poll loop.

    On Linux/Unix it uses *unprivileged* ICMP datagram sockets, so the daemon runs
    as a normal user — no root, no `cap_net_raw` — once the kernel ping group is
    enabled on the box:

        sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
        # persist: echo 'net.ipv4.ping_group_range=0 2147483647' | \\
        #   sudo tee /etc/sysctl.d/99-wisp-ping.conf

    Windows has no unprivileged ICMP socket, so there it uses raw sockets and must
    run elevated (Administrator/SYSTEM) — see `_PRIVILEGED_ICMP`. A permission error
    surfaces as a RuntimeError so the misconfig is obvious rather than every device
    silently reading 'down'.
    """

    def __init__(self, *, interval: float = 0.2, timeout: float = 1.0) -> None:
        self._interval = interval
        self._timeout = timeout

    def on_cycle_start(self) -> None:
        return None

    async def preflight(self) -> None:
        """Fail loudly at startup if this box can't actually send ICMP — a missing
        `icmplib` or a disabled ping group otherwise masquerades as every host
        (and the internet canary) reading 100% loss, which silently freezes the
        whole monitor. A loopback probe surfaces both as the same RuntimeError
        `ping` would raise mid-cycle, so the daemon can refuse to start instead."""
        await self.ping("127.0.0.1", count=1)

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
                timeout=self._timeout, privileged=_PRIVILEGED_ICMP,
            )
        except SocketPermissionError as exc:  # no socket permission on this box
            if _PRIVILEGED_ICMP:  # Windows: raw sockets need elevation
                raise RuntimeError(
                    "ICMP on Windows needs raw sockets — run the monitor as "
                    "Administrator/SYSTEM (the install.ps1 Scheduled Task does this)."
                ) from exc
            raise RuntimeError(
                "ICMP needs the kernel ping group enabled: "
                'sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"'
            ) from exc
        except OSError:
            # host unreachable, name resolution failure, etc.
            return PingResult(ip, None, 100.0)
        loss = round(host.packet_loss * 100, 1)
        latency = host.avg_rtt if host.packets_received else None
        # icmplib computes jitter (mean abs diff between consecutive RTTs); it needs
        # >=2 replies, so a single-ping probe yields 0.0 — only meaningful on the
        # multi-echo poll plan. None when nothing came back.
        jitter = getattr(host, "jitter", None) if host.packets_received else None
        return PingResult(ip, latency, loss, jitter)


def build_prober(cfg: Config = CONFIG) -> Prober:
    return IcmpProber()
