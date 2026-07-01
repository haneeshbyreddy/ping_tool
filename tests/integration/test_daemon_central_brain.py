"""Central-brain daemon mode tests (New Architecture Phase B): the thin-probe loop that
learns its device list from central and reports raw pings back, instead of running a
local FSM. Mirrors tests/integration/test_daemon.py's module-loading (apps/ isn't a
package) and fake-prober style. No real network — a recording CentralBrainClient double.
"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

_spec = importlib.util.spec_from_file_location(
    "wisp_daemon_main_cb", _REPO / "apps" / "daemon" / "main.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

from wisp.config import Config
from wisp.ingress.probers import PingResult
from wisp.runtime.central_client import CentralClientError


def _dev(id, ip, parent=None):
    return {"id": id, "name": f"d{id}", "ip_address": ip, "region": "R",
            "parent_device_id": parent}


class _FakeProber:
    def __init__(self, behaviour):
        self._behaviour = behaviour   # ip -> callable returning PingResult

    async def ping(self, ip, count):
        return self._behaviour[ip]()

    def on_cycle_start(self):
        pass


class RecordingCentralClient:
    def __init__(self, devices, canary_ip="1.1.1.1", fail_report=False, fail_fetch=False,
                 replies=None):
        self.devices = devices
        self.canary_ip = canary_ip
        self.fail_report = fail_report
        self.fail_fetch = fail_fetch
        self.reports: list[dict] = []
        self.fetch_calls = 0
        # A queue of scripted reply dicts, popped in order (one per `report()` call,
        # including recheck follow-ups); once exhausted, falls back to {"ok": True}.
        self._replies = list(replies) if replies is not None else None

    def fetch_devices(self) -> dict:
        self.fetch_calls += 1
        if self.fail_fetch:
            raise CentralClientError("fetch boom")
        return {"devices": self.devices, "canary_ip": self.canary_ip}

    def report(self, pings: dict, ts: str, *, mode: str = "full") -> dict:
        if self.fail_report:
            raise CentralClientError("report boom")
        self.reports.append({"pings": pings, "ts": ts, "mode": mode})
        if self._replies:
            return self._replies.pop(0)
        return {"ok": True}


class GentleProbePlanTest(unittest.TestCase):
    def test_parent_gets_infra_cadence_leaf_gets_full(self):
        cfg = Config(pings_per_poll=5, pings_per_poll_infra=2)
        devices = [_dev(1, "10.0.0.1"), _dev(2, "10.0.0.2", parent=1)]
        plan = daemon._gentle_probe_plan(devices, "1.1.1.1", cfg)
        self.assertEqual(plan["10.0.0.1"], 2)   # is a parent -> gentle
        self.assertEqual(plan["10.0.0.2"], 5)   # leaf -> full
        self.assertEqual(plan["1.1.1.1"], 5)    # canary -> full


class RunCycleCentralBrainTest(unittest.TestCase):
    def test_reports_raw_pings_for_every_probed_ip(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 12.0, 0.0, 1.5),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 8.0, 0.0, 0.5),
        })
        client = RecordingCentralClient(devices)
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 1)
        pings = client.reports[0]["pings"]
        self.assertEqual(pings["10.0.0.1"], {"loss_pct": 0.0, "latency_ms": 12.0,
                                              "jitter_ms": 1.5})
        self.assertIn("1.1.1.1", pings)   # canary reported too

    def test_report_failure_does_not_raise(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, fail_report=True)
        cfg = Config()
        # must complete without raising — a dead central can't crash the probe loop
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(client.reports, [])

    def test_follows_recheck_hint_returned_by_the_full_report(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}},   # full report reply
            {"ok": True},   # recheck reply: confirmed, hint clears
        ])
        cfg = Config(retry_interval_s=0.001)
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 2)
        self.assertEqual(client.reports[0]["mode"], "full")
        self.assertEqual(client.reports[1]["mode"], "recheck")
        self.assertEqual(set(client.reports[1]["pings"]), {"10.0.0.1"})  # only the suspect

    def test_recheck_disabled_when_retry_interval_zero(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 2.0}},
        ])
        cfg = Config(retry_interval_s=0)   # fast-confirm off entirely
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 1)   # never followed the hint


class FollowRecheckTest(unittest.TestCase):
    """The edge-side half of the fast-confirm round trip: `_follow_recheck` re-probes
    exactly the IPs central names, single-echo, until a reply omits a `recheck` hint."""

    def _cfg(self, **over):
        return Config(retry_interval_s=0.001, down_consecutive=3, recover_consecutive=2,
                     **over)

    def test_follows_hint_until_empty(self):
        prober = _FakeProber({"10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0)})
        client = RecordingCentralClient([], replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}},
            {"ok": True},   # no recheck key -> stop
        ])
        first_reply = {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}}
        asyncio.run(daemon._follow_recheck(prober, client, first_reply, self._cfg()))
        self.assertEqual(len(client.reports), 2)   # exactly the two scripted rounds
        self.assertTrue(all(r["mode"] == "recheck" for r in client.reports))
        self.assertEqual(client.reports[0]["pings"]["10.0.0.1"]["loss_pct"], 100.0)

    def test_stops_immediately_when_no_hint(self):
        prober = _FakeProber({})
        client = RecordingCentralClient([])
        asyncio.run(daemon._follow_recheck(prober, client, {"ok": True}, self._cfg()))
        self.assertEqual(client.reports, [])

    def test_stops_at_round_cap_even_if_central_keeps_hinting(self):
        # A misbehaving/buggy central that NEVER clears the hint must not wedge the
        # probe loop forever.
        prober = _FakeProber({"10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0)})
        always_hint = {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}}
        client = RecordingCentralClient([], replies=[always_hint] * 20)
        cfg = self._cfg()   # down_consecutive=3, recover_consecutive=2 -> cap = 5
        asyncio.run(daemon._follow_recheck(prober, client, always_hint, cfg))
        self.assertEqual(len(client.reports), 5)


class RunForeverCentralBrainTest(unittest.TestCase):
    def test_refetches_topology_and_reports_each_cycle(self):
        devices = [_dev(1, "10.0.0.1")]
        client = RecordingCentralClient(devices)

        async def fake_preflight():
            return None

        class _Prober(_FakeProber):
            def __init__(self):
                super().__init__({
                    "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
                    "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
                })

            async def preflight(self):
                await fake_preflight()

        prober = _Prober()
        cfg = Config(poll_interval_s=0)

        async def _run():
            import unittest.mock as mock
            with mock.patch.object(daemon, "build_prober", return_value=prober), \
                 mock.patch.object(daemon, "build_central_client", return_value=client):
                await daemon.run_forever_central_brain(cfg, interval=0, max_cycles=2)

        asyncio.run(_run())
        self.assertEqual(len(client.reports), 2)
        # finite --cycles runs skip the per-cycle topology refresh (same determinism
        # rule the standalone daemon's device-set reload follows) — just the initial fetch.
        self.assertEqual(client.fetch_calls, 1)

    def test_aborts_when_initial_fetch_fails(self):
        client = RecordingCentralClient([], fail_fetch=True)

        class _Prober(_FakeProber):
            def __init__(self):
                super().__init__({})

            async def preflight(self):
                return None

        cfg = Config()

        async def _run():
            import unittest.mock as mock
            with mock.patch.object(daemon, "build_prober", return_value=_Prober()), \
                 mock.patch.object(daemon, "build_central_client", return_value=client):
                await daemon.run_forever_central_brain(cfg, interval=0, max_cycles=1)

        with self.assertRaises(SystemExit) as ctx:
            asyncio.run(_run())
        self.assertEqual(ctx.exception.code, 2)


class ConfigBrainModeTest(unittest.TestCase):
    def test_brain_mode_requires_central_url_too(self):
        self.assertFalse(Config(central_brain_mode=True).central_brain_enabled())
        self.assertTrue(Config(central_brain_mode=True,
                               central_url="https://c.example").central_brain_enabled())
        self.assertFalse(Config(central_url="https://c.example").central_brain_enabled())


if __name__ == "__main__":
    unittest.main()
