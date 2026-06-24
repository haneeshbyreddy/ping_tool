"""Daemon glue tests: the poll-gather error policy.

Guards the regression where a broken prober (missing icmplib / disabled ping
group) was silently reported as every host at 100% loss — which trips the canary
freeze and makes a misconfigured monitor look like a total outage. A config-level
RuntimeError must abort the cycle loudly; a genuine per-host error stays masked as
100% loss so one bad host never sinks the whole cycle.
"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

# apps/ isn't a package (the runtimes self-bootstrap), so load main.py by path.
_spec = importlib.util.spec_from_file_location(
    "wisp_daemon_main", _REPO / "apps" / "daemon" / "main.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

from wisp.ingress.probers import PingResult
from wisp.config import Config
from wisp.core.state_machine import (
    DOWN,
    UP,
    DeviceMeta,
    MonitorEngine,
    OutageOpened,
)


def _meta(dev_id, ip, parent=None):
    return DeviceMeta(id=dev_id, name=f"d{dev_id}", ip_address=ip, region="R",
                      parent_device_id=parent, technician_phone=None)


class _ScriptProber:
    """Pops a queued PingResult per IP each ping; defaults to 100% loss when empty."""

    def __init__(self, script):
        self.script = {ip: list(q) for ip, q in script.items()}
        self.calls = 0

    async def ping(self, ip, count):
        self.calls += 1
        q = self.script.get(ip)
        return q.pop(0) if q else PingResult(ip, None, 100.0)


class ConfirmDown(unittest.TestCase):
    """Fast soft-state -> hard-state confirmation: a suspected-down device is re-probed
    back-to-back and confirmed in seconds; a reachable retry clears it without paging."""

    def _setup(self, **over):
        cfg = Config(down_consecutive=3, retry_interval_s=0.001, canary_ip="1.1.1.1", **over)
        eng = MonitorEngine([_meta(1, "a")], cfg)
        results = {"a": PingResult("a", None, 100.0)}
        states = dict(eng.process_cycle(results, "t").states)   # main pass: streak 1, UP
        self.assertEqual(states[1], UP)
        return cfg, eng, results, states

    def test_confirms_down_fast(self):
        cfg, eng, results, states = self._setup()
        prober = _ScriptProber({"a": [PingResult("a", None, 100.0)] * 2})  # stays lost
        events = asyncio.run(
            daemon._confirm_down(prober, eng, eng.probe_plan(), results, states, "t", cfg))
        self.assertEqual(states[1], DOWN)
        self.assertTrue(any(isinstance(e, OutageOpened) for e in events))
        self.assertEqual(prober.calls, 2)                       # exactly down_consecutive-1 retries

    def test_blip_clears_without_paging(self):
        cfg, eng, results, states = self._setup()
        prober = _ScriptProber({"a": [PingResult("a", 10.0, 0.0)]})  # first retry recovers
        events = asyncio.run(
            daemon._confirm_down(prober, eng, eng.probe_plan(), results, states, "t", cfg))
        self.assertEqual(states[1], UP)
        self.assertFalse(events)
        self.assertEqual(results["a"].packet_loss, 0.0)         # persisted reading = the healthy retry
        self.assertEqual(prober.calls, 1)                       # stopped as soon as it cleared


class _FakeProber:
    def __init__(self, behaviour):
        self._behaviour = behaviour  # ip -> callable returning PingResult or raising

    async def ping(self, ip, count):
        return self._behaviour[ip]()


class _CountingProber:
    """Records how many pings are in flight at once so a test can assert the
    semaphore actually bounds the fan-out."""

    def __init__(self):
        self.inflight = 0
        self.peak = 0

    async def ping(self, ip, count):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)   # hold the slot so concurrency can build up
            return PingResult(ip, 5.0, 0.0)
        finally:
            self.inflight -= 1


class GatherConcurrencyBound(unittest.TestCase):
    def test_semaphore_caps_inflight(self):
        prober = _CountingProber()
        ips = [f"10.0.0.{i}" for i in range(50)]
        out = asyncio.run(daemon._gather_pings(prober, ips, 3, max_inflight=8))
        self.assertEqual(len(out), 50)              # every host still probed
        self.assertLessEqual(prober.peak, 8)        # never more than the cap in flight

    def test_per_ip_count_map(self):
        seen = {}

        class _Rec:
            async def ping(self, ip, count):
                seen[ip] = count
                return PingResult(ip, 1.0, 0.0)

        counts = {"a": 2, "b": 5}
        asyncio.run(daemon._gather_pings(_Rec(), ["a", "b"], counts, max_inflight=4))
        self.assertEqual(seen, {"a": 2, "b": 5})    # gentle vs full honoured per IP


class GatherPingsPolicy(unittest.TestCase):
    def test_per_host_error_is_masked_as_loss(self):
        def boom():
            raise OSError("host unreachable")
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
            "10.0.0.2": boom,
        })
        out = asyncio.run(daemon._gather_pings(prober, ["10.0.0.1", "10.0.0.2"], 3))
        self.assertEqual(out["10.0.0.1"].packet_loss, 0.0)
        self.assertEqual(out["10.0.0.2"].packet_loss, 100.0)  # masked, cycle survives

    def test_config_error_aborts_the_cycle(self):
        def missing_dep():
            raise RuntimeError("IcmpProber needs 'icmplib'")
        prober = _FakeProber({
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
            "10.0.0.2": missing_dep,
        })
        # must NOT come back as a tidy dict of 100%-loss readings — it raises.
        with self.assertRaises(RuntimeError):
            asyncio.run(daemon._gather_pings(prober, ["1.1.1.1", "10.0.0.2"], 3))


if __name__ == "__main__":
    unittest.main()
