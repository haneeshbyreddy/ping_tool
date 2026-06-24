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
    OutageResolved,
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


class ConfirmUp(unittest.TestCase):
    """Fast hard-state -> recovery confirmation (mirror of ConfirmDown): a recovering
    device is re-probed back-to-back and cleared in seconds; a fresh loss aborts it."""

    def _setup(self, **over):
        cfg = Config(recover_consecutive=2, retry_interval_s=0.001,
                     canary_ip="1.1.1.1", **over)
        eng = MonitorEngine([_meta(1, "a")], cfg)
        eng.fsm[1].prime(DOWN)                                  # device was DOWN
        results = {"a": PingResult("a", 10.0, 0.0)}             # sample 1: reachable
        states = dict(eng.process_cycle(results, "t").states)  # still DOWN (needs 2)
        self.assertEqual(states[1], DOWN)
        return cfg, eng, results, states

    def test_confirms_up_fast(self):
        cfg, eng, results, states = self._setup()
        prober = _ScriptProber({"a": [PingResult("a", 10.0, 0.0)]})  # stays reachable
        events = asyncio.run(
            daemon._confirm_up(prober, eng, eng.probe_plan(), results, states, "t", cfg))
        self.assertEqual(states[1], UP)
        self.assertTrue(any(isinstance(e, OutageResolved) for e in events))
        self.assertEqual(prober.calls, 1)              # exactly recover_consecutive-1 retries

    def test_flap_does_not_recover(self):
        cfg, eng, results, states = self._setup()
        prober = _ScriptProber({"a": [PingResult("a", None, 100.0)]})  # lost again → abort
        events = asyncio.run(
            daemon._confirm_up(prober, eng, eng.probe_plan(), results, states, "t", cfg))
        self.assertEqual(states[1], DOWN)
        self.assertFalse(events)
        self.assertEqual(prober.calls, 1)


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


class BetweenCycleWatch(unittest.TestCase):
    """Between-cycle watch: detects a device that fails mid-gap (not at poll time).

    Verifies that _between_cycle_watch pages a device that goes down AFTER the
    full poll — i.e. the path that fast-confirm alone cannot cover."""

    def _cfg(self, **kw):
        return Config(
            down_consecutive=3,
            retry_interval_s=0.001,
            canary_ip="1.1.1.1",
            canary_freeze=True,
            **kw,
        )

    def _noop_dispatcher(self):
        """Minimal dispatcher double that records dispatched events."""
        class _D:
            dispatched = []
            def dispatch(self, events, ts): self.dispatched.extend(events)
            def sweep(self, ts): pass
        return _D()

    def test_detects_midgap_failure_and_pages(self):
        """A device UP at poll time, then 100% loss mid-gap → DOWN + OutageOpened."""
        cfg = self._cfg()
        eng = MonitorEngine([_meta(1, "a")], cfg)
        # Device was UP at poll time (FSM starts in UP, streak 0).
        dispatcher = self._noop_dispatcher()

        # Script: canary healthy, device lost on the first between-cycle probe.
        # _confirm_down will then fire 2 more probes (both lost) to reach DOWN.
        prober = _ScriptProber({
            "1.1.1.1": [PingResult("1.1.1.1", 5.0, 0.0)] * 10,
            "a": [PingResult("a", None, 100.0)] * 10,   # all-lost from now on
        })

        # Run with sleep_for just long enough for one between-cycle tick.
        asyncio.run(
            daemon._between_cycle_watch(prober, eng, dispatcher, sleep_for=0.05, cfg=cfg)
        )

        self.assertEqual(eng.fsm[1].state, DOWN)
        self.assertTrue(any(isinstance(e, OutageOpened) for e in dispatcher.dispatched))

    def test_detects_midgap_recovery(self):
        """A device DOWN at poll time, then reachable mid-gap → UP + OutageResolved,
        without waiting for the next full poll (recovery is now symmetric with DOWN)."""
        cfg = self._cfg()  # recover_consecutive defaults to 2
        eng = MonitorEngine([_meta(1, "a")], cfg)
        eng.fsm[1].prime(DOWN)
        dispatcher = self._noop_dispatcher()

        # Canary + device reachable from now on (long scripts so the loop never starves).
        prober = _ScriptProber({
            "1.1.1.1": [PingResult("1.1.1.1", 5.0, 0.0)] * 500,
            "a": [PingResult("a", 5.0, 0.0)] * 500,
        })

        asyncio.run(
            daemon._between_cycle_watch(prober, eng, dispatcher, sleep_for=0.05, cfg=cfg)
        )

        self.assertEqual(eng.fsm[1].state, UP)
        self.assertTrue(any(isinstance(e, OutageResolved) for e in dispatcher.dispatched))

    def test_blip_midgap_does_not_page(self):
        """One 100% loss then recovery mid-gap → no page (blip, not outage)."""
        cfg = self._cfg()
        eng = MonitorEngine([_meta(1, "a")], cfg)
        dispatcher = self._noop_dispatcher()

        # First between-cycle probe: 100% loss. First confirm retry: recovers.
        # Then 200 healthy results so every remaining tick of the loop sees 0% loss
        # and produces no suspects — queue exhaustion (→ 100% loss) must not fire.
        _healthy = [PingResult("a", 5.0, 0.0)] * 200
        prober = _ScriptProber({
            "1.1.1.1": [PingResult("1.1.1.1", 5.0, 0.0)] * 300,
            "a": [PingResult("a", None, 100.0)] + _healthy,
        })

        asyncio.run(
            daemon._between_cycle_watch(prober, eng, dispatcher, sleep_for=0.05, cfg=cfg)
        )

        self.assertEqual(eng.fsm[1].state, UP)
        self.assertFalse(dispatcher.dispatched)

    def test_canary_freeze_suppresses_midgap_alarm(self):
        """When the canary is down (canary_freeze=True) the watch skips suspect processing."""
        cfg = self._cfg()  # canary_freeze=True is the default in _cfg
        eng = MonitorEngine([_meta(1, "a")], cfg)
        dispatcher = self._noop_dispatcher()

        # Both canary and device are lost — uplink is down, not the device.
        prober = _ScriptProber({
            "1.1.1.1": [PingResult("1.1.1.1", None, 100.0)] * 10,
            "a": [PingResult("a", None, 100.0)] * 10,
        })

        asyncio.run(
            daemon._between_cycle_watch(prober, eng, dispatcher, sleep_for=0.05, cfg=cfg)
        )

        self.assertEqual(eng.fsm[1].state, UP)   # FSM untouched
        self.assertFalse(dispatcher.dispatched)


if __name__ == "__main__":
    unittest.main()
