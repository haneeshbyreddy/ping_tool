import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

_spec = importlib.util.spec_from_file_location(
    "wisp_daemon_main", _REPO / "apps" / "daemon" / "main.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

from wisp.ingress.probers import PingResult

class _FakeProber:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    async def ping(self, ip, count):
        return self._behaviour[ip]()

class _CountingProber:

    def __init__(self):
        self.inflight = 0
        self.peak = 0

    async def ping(self, ip, count):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)
            return PingResult(ip, 5.0, 0.0)
        finally:
            self.inflight -= 1

class GatherConcurrencyBound(unittest.TestCase):
    def test_semaphore_caps_inflight(self):
        prober = _CountingProber()
        ips = [f"10.0.0.{i}" for i in range(50)]
        out = asyncio.run(daemon._gather_pings(prober, ips, 3, max_inflight=8))
        self.assertEqual(len(out), 50)
        self.assertLessEqual(prober.peak, 8)

    def test_per_ip_count_map(self):
        seen = {}

        class _Rec:
            async def ping(self, ip, count):
                seen[ip] = count
                return PingResult(ip, 1.0, 0.0)

        counts = {"a": 2, "b": 5}
        asyncio.run(daemon._gather_pings(_Rec(), ["a", "b"], counts, max_inflight=4))
        self.assertEqual(seen, {"a": 2, "b": 5})

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
        self.assertEqual(out["10.0.0.2"].packet_loss, 100.0)

    def test_config_error_aborts_the_cycle(self):
        def missing_dep():
            raise RuntimeError("IcmpProber needs 'icmplib'")
        prober = _FakeProber({
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
            "10.0.0.2": missing_dep,
        })
        with self.assertRaises(RuntimeError):
            asyncio.run(daemon._gather_pings(prober, ["1.1.1.1", "10.0.0.2"], 3))

if __name__ == "__main__":
    unittest.main(verbosity=2)
