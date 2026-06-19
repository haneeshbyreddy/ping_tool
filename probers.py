"""Probers turn an IP into a (latency, packet-loss) sample.

Two implementations behind one interface so the whole system runs with no
hardware today and swaps to real ICMP later by flipping `WISP_PROBER=icmp`:

* SimulatedProber — plays a scripted scenario keyed off a per-cycle tick, so we
  can demonstrate every branch (degraded, power outage, link fault, recovery)
  deterministically without touching the network.
* IcmpProber — real raw-socket ping via `icmplib` (needs root / cap_net_raw).

The Prober protocol is intentionally tiny: `async def ping(ip, count)`.
`on_cycle_start()` is an optional hook the daemon calls once per poll cycle; the
real prober ignores it, the simulator uses it to advance its tick.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from config import CONFIG, Config


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


# --- Simulated prober -------------------------------------------------------

# A scenario rule: this IP is unhealthy from start_tick..end_tick (inclusive),
# with the given loss% and latency (latency ignored when loss==100).
@dataclass(frozen=True)
class FaultWindow:
    ip: str
    start_tick: int
    end_tick: int
    loss_pct: float
    latency_ms: float | None = None


# Default demo: matches the seeded topology (see seed.py). Ticks are poll cycles.
#   ticks 2-5  Rampur Sector B latency spike      -> DEGRADED
#   ticks 3-8  whole Sohna site dark (relay+sector+power-ref) -> Likely Power Outage
#   ticks 4-9  Bhondsi AP down but tower+power-ref alive      -> Link/Equipment Fault
# Everything recovers afterwards so the recovery path is exercised too.
DEMO_SCENARIO: tuple[FaultWindow, ...] = (
    FaultWindow("192.0.2.13", 2, 5, loss_pct=0.0, latency_ms=420.0),   # Rampur Sector B
    FaultWindow("192.0.2.20", 3, 8, loss_pct=100.0),                   # Sohna Relay
    FaultWindow("192.0.2.21", 3, 8, loss_pct=100.0),                   # Sohna power-ref
    FaultWindow("192.0.2.22", 3, 8, loss_pct=100.0),                   # Sohna Sector A
    FaultWindow("192.0.2.32", 4, 9, loss_pct=100.0),                   # Bhondsi AP (link fault)
)


class SimulatedProber:
    """Deterministic-ish fake prober driven by a tick counter."""

    def __init__(
        self,
        scenario: tuple[FaultWindow, ...] = DEMO_SCENARIO,
        *,
        seed: int = 1234,
        healthy_latency_range: tuple[float, float] = (5.0, 40.0),
    ) -> None:
        self._scenario = scenario
        self._tick = 0
        self._rng = random.Random(seed)
        self._healthy_range = healthy_latency_range

    @property
    def tick(self) -> int:
        return self._tick

    def on_cycle_start(self) -> None:
        self._tick += 1

    def _active_fault(self, ip: str) -> FaultWindow | None:
        for w in self._scenario:
            if w.ip == ip and w.start_tick <= self._tick <= w.end_tick:
                return w
        return None

    async def ping(self, ip: str, count: int) -> PingResult:
        await asyncio.sleep(0)  # cooperative yield; mimics async I/O
        fault = self._active_fault(ip)
        if fault is None:
            latency = round(self._rng.uniform(*self._healthy_range), 1)
            return PingResult(ip, latency, 0.0)
        if fault.loss_pct >= 100.0:
            return PingResult(ip, None, 100.0)
        # partial loss / latency degradation
        latency = fault.latency_ms if fault.loss_pct < 100 else None
        return PingResult(ip, latency, fault.loss_pct)


# --- Real ICMP prober -------------------------------------------------------

class IcmpProber:
    """Real ping via icmplib. Imported lazily so the module loads without the
    dependency; any socket/permission error becomes a 100%-loss reading rather
    than crashing the poll loop."""

    def __init__(self, *, interval: float = 0.2, timeout: float = 1.0) -> None:
        self._interval = interval
        self._timeout = timeout

    def on_cycle_start(self) -> None:
        return None

    async def ping(self, ip: str, count: int) -> PingResult:
        try:
            from icmplib import async_ping
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "IcmpProber needs 'icmplib' (pip install icmplib). "
                "Use WISP_PROBER=simulated for the no-dependency path."
            ) from exc
        try:
            host = await async_ping(
                ip, count=count, interval=self._interval,
                timeout=self._timeout, privileged=True,
            )
        except OSError:
            # raw-socket permission denied, host unreachable, etc.
            return PingResult(ip, None, 100.0)
        loss = round(host.packet_loss * 100, 1)
        latency = host.avg_rtt if host.packets_received else None
        return PingResult(ip, latency, loss)


def build_prober(cfg: Config = CONFIG) -> Prober:
    if cfg.prober == "icmp":
        return IcmpProber()
    return SimulatedProber()
