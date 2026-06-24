"""Truth-table tests for the MonitorEngine. Pure logic, no DB, no network.

Run:  python -m unittest discover -s tests   (from the project root)
"""
import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.ingress.probers import PingResult
from wisp.core.state_machine import (
    DEGRADED,
    DOWN,
    UNREACHABLE,
    UP,
    DeviceMeta,
    MonitorEngine,
    OutageOpened,
    OutageResolved,
    UplinkDown,
    UplinkRestored,
)

CFG = Config(
    down_consecutive=3,
    degraded_consecutive=2,
    recover_consecutive=2,
    latency_threshold_ms=150.0,
    loss_degraded_pct=5.0,
    canary_ip="1.1.1.1",
)

# Reusable samples
UP_S = lambda ip="d": PingResult(ip, 20.0, 0.0)
SLOW_S = lambda ip="d": PingResult(ip, 400.0, 0.0)
LOSS_S = lambda ip="d": PingResult(ip, 50.0, 30.0)
DEAD_S = lambda ip="d": PingResult(ip, None, 100.0)


def solo_device(**over) -> DeviceMeta:
    base = dict(
        id=1, name="D1", ip_address="d", region="R",
        parent_device_id=None, technician_phone="+910000000000",
    )
    base.update(over)
    return DeviceMeta(**base)


def feed(engine: MonitorEngine, samples_by_ip, canary_up=True):
    """Push one cycle. samples_by_ip maps ip->PingResult; adds a healthy canary."""
    results = dict(samples_by_ip)
    results[CFG.canary_ip] = UP_S("1.1.1.1") if canary_up else DEAD_S("1.1.1.1")
    return engine.process_cycle(results, ts="2026-01-01T00:00:00+00:00")


class FlapSuppression(unittest.TestCase):
    def test_down_needs_three_consecutive(self):
        eng = MonitorEngine([solo_device()], CFG)
        # two dead polls: still UP (flap suppression)
        self.assertEqual(feed(eng, {"d": DEAD_S()}).states[1], UP)
        self.assertEqual(feed(eng, {"d": DEAD_S()}).states[1], UP)
        # third confirms DOWN and opens an outage
        r = feed(eng, {"d": DEAD_S()})
        self.assertEqual(r.states[1], DOWN)
        self.assertTrue(any(isinstance(e, OutageOpened) for e in r.events))

    def test_single_blip_never_pages(self):
        eng = MonitorEngine([solo_device()], CFG)
        feed(eng, {"d": DEAD_S()})           # one blip
        r = feed(eng, {"d": UP_S()})         # recovers immediately
        self.assertEqual(r.states[1], UP)
        self.assertFalse(r.events)


class ProbePlan(unittest.TestCase):
    """Aggregation gear (a parent of another device) is probed gently — fewer echoes
    per poll — so its control plane doesn't trip its ICMP rate-limiter; leaf CPEs and
    the canary keep the full sample count."""

    CFG = Config(pings_per_poll=5, pings_per_poll_infra=2, canary_ip="1.1.1.1")

    def test_parent_is_gentle_leaf_is_full(self):
        tower = solo_device(id=1, ip_address="tower", parent_device_id=None)
        cpe = solo_device(id=2, ip_address="cpe", parent_device_id=1)
        eng = MonitorEngine([tower, cpe], self.CFG)
        plan = eng.probe_plan()
        self.assertEqual(plan["tower"], 2)        # parent -> gentle
        self.assertEqual(plan["cpe"], 5)          # leaf -> full
        self.assertEqual(plan["1.1.1.1"], 5)      # canary -> full
        # keys must match what the daemon will actually ping
        self.assertEqual(set(plan), eng.required_ips())

    def test_childless_node_is_full(self):
        eng = MonitorEngine([solo_device(id=1, ip_address="d")], self.CFG)
        self.assertEqual(eng.probe_plan()["d"], 5)


class AdaptiveInterval(unittest.TestCase):
    """Detection cadence scales with fleet size when adaptive mode is on: a small
    deployment polls faster (quicker detection); a large one falls back to protect
    the box. Off by default, so existing deployments are unchanged."""

    def test_off_by_default(self):
        cfg = Config(poll_interval_s=60, poll_interval_small_s=30, small_fleet_max=1000)
        self.assertEqual(cfg.effective_interval(10), 60)
        self.assertEqual(cfg.effective_interval(5000), 60)

    def test_small_fleet_polls_faster_when_on(self):
        cfg = Config(poll_interval_adaptive=True, poll_interval_s=60,
                     poll_interval_small_s=30, small_fleet_max=1000)
        self.assertEqual(cfg.effective_interval(1000), 30)   # at the threshold -> fast
        self.assertEqual(cfg.effective_interval(1001), 60)   # above it -> protect the box


class Degraded(unittest.TestCase):
    def test_degraded_needs_two_consecutive(self):
        eng = MonitorEngine([solo_device()], CFG)
        self.assertEqual(feed(eng, {"d": SLOW_S()}).states[1], UP)       # 1st slow
        self.assertEqual(feed(eng, {"d": SLOW_S()}).states[1], DEGRADED) # 2nd slow

    def test_loss_band_is_degraded(self):
        eng = MonitorEngine([solo_device()], CFG)
        feed(eng, {"d": LOSS_S()})
        self.assertEqual(feed(eng, {"d": LOSS_S()}).states[1], DEGRADED)


class RecoveryHysteresis(unittest.TestCase):
    def test_down_recovers_after_two_healthy(self):
        eng = MonitorEngine([solo_device()], CFG)
        for _ in range(3):
            feed(eng, {"d": DEAD_S()})       # -> DOWN
        self.assertEqual(eng.fsm[1].state, DOWN)
        self.assertEqual(feed(eng, {"d": UP_S()}).states[1], DOWN)  # 1 healthy: still down
        r = feed(eng, {"d": UP_S()})                                # 2 healthy: recovered
        self.assertEqual(r.states[1], UP)
        self.assertTrue(any(isinstance(e, OutageResolved) for e in r.events))


class Topology(unittest.TestCase):
    def setUp(self):
        self.parent = solo_device(id=1, ip_address="p")
        self.child = solo_device(id=2, ip_address="c", parent_device_id=1)
        self.eng = MonitorEngine([self.parent, self.child], CFG)

    def test_child_of_down_parent_is_unreachable(self):
        for _ in range(3):
            r = feed(self.eng, {"p": DEAD_S("p"), "c": DEAD_S("c")})
        self.assertEqual(r.states[1], DOWN)          # parent: real outage
        self.assertEqual(r.states[2], UNREACHABLE)   # child: suppressed
        # child outage row should be UNREACHABLE, parent DOWN
        opened = [e for e in r.events if isinstance(e, OutageOpened)]
        kinds = {e.device_id: e.state for e in opened}
        self.assertEqual(kinds.get(2), UNREACHABLE)


class CanaryFreeze(unittest.TestCase):
    def test_uplink_down_freezes_and_alerts_once(self):
        eng = MonitorEngine([solo_device()], CFG)
        # device is dead AND canary is dead -> freeze, single UplinkDown, no per-device outage
        r1 = feed(eng, {"d": DEAD_S()}, canary_up=False)
        self.assertTrue(r1.canary_down)
        self.assertEqual(r1.states[1], UP)  # frozen, not transitioned
        self.assertEqual(sum(isinstance(e, UplinkDown) for e in r1.events), 1)
        # second frozen cycle: no duplicate UplinkDown
        r2 = feed(eng, {"d": DEAD_S()}, canary_up=False)
        self.assertFalse(any(isinstance(e, UplinkDown) for e in r2.events))
        # canary recovers -> UplinkRestored
        r3 = feed(eng, {"d": UP_S()}, canary_up=True)
        self.assertTrue(any(isinstance(e, UplinkRestored) for e in r3.events))

    def test_freeze_disabled_still_pages_local_devices(self):
        # WISP_CANARY_FREEZE=0: uplink-down is still flagged, but LAN gear keeps being
        # evaluated and paged (the bug the operator hit — a dead canary blinding local
        # detection).
        cfg = replace(CFG, canary_freeze=False)
        eng = MonitorEngine([solo_device()], cfg)
        first = feed(eng, {"d": DEAD_S()}, canary_up=False)
        self.assertFalse(first.canary_down)  # not frozen
        # first dead-canary cycle still raises exactly one UplinkDown for visibility
        self.assertEqual(sum(isinstance(e, UplinkDown) for e in first.events), 1)
        last = first
        for _ in range(cfg.down_consecutive - 1):
            last = feed(eng, {"d": DEAD_S()}, canary_up=False)
        self.assertEqual(last.states[1], DOWN)  # local device transitioned despite dead canary
        self.assertTrue(any(isinstance(e, OutageOpened) for e in last.events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
