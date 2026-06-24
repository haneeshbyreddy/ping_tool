"""Tests for the pure per-link performance baseline detector. No DB, no network.

Run:  python -m unittest discover -s tests   (from the project root)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.core.baseline import Sample, evaluate_perf
from wisp.core.state_machine import DOWN, UP

CFG = Config(
    perf_window=20,
    perf_min_samples=10,
    perf_consecutive=3,
    perf_deviation_factor=3.0,
    perf_mad_k=5.0,
    perf_min_baseline_ms=5.0,
    perf_min_jitter_ms=3.0,
)


def healthy(latency, jitter=2.0, n=1):
    return [Sample(latency, 0.0, jitter, UP) for _ in range(n)]


class BaselineDetector(unittest.TestCase):
    def test_thin_data_never_enters_degraded(self):
        # Fewer than perf_consecutive + perf_min_samples → undecidable, stay normal.
        window = healthy(8.0, n=5) + healthy(200.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertFalse(v.degraded)
        self.assertFalse(v.changed)

    def test_stable_link_stays_normal(self):
        window = healthy(8.0, n=20)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertFalse(v.degraded)

    def test_single_spike_does_not_trip(self):
        # One bad poll inside the candidate window is not enough (needs all N).
        window = healthy(8.0, n=15) + healthy(8.0, n=2) + healthy(300.0, n=1)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertFalse(v.degraded)

    def test_sustained_latency_deviation_enters(self):
        # 8ms baseline, then 3 straight polls ~120ms → far over 3× and 5×MAD.
        window = healthy(8.0, n=15) + healthy(120.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertTrue(v.degraded)
        self.assertTrue(v.changed)
        self.assertEqual(v.metric, "latency")
        self.assertAlmostEqual(v.baseline_ms, 8.0, places=1)
        self.assertEqual(v.current_ms, 120.0)

    def test_low_baseline_floor_suppresses_small_jumps(self):
        # 2ms → 8ms is 4× but both tiny; floor (5ms) means we don't judge it.
        window = healthy(2.0, n=15) + healthy(8.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertFalse(v.degraded)

    def test_jitter_deviation_enters(self):
        # Latency steady; jitter explodes from a ~5ms baseline (above the floor) to 50ms.
        window = healthy(20.0, jitter=5.0, n=15) + healthy(20.0, jitter=50.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertTrue(v.degraded)
        self.assertEqual(v.metric, "jitter")

    def test_jitter_below_floor_not_judged(self):
        # A sub-floor jitter baseline (2ms) is too small to judge a multiple of.
        window = healthy(20.0, jitter=2.0, n=15) + healthy(20.0, jitter=40.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertFalse(v.degraded)

    def test_recovery_needs_full_clean_window(self):
        base = healthy(8.0, n=15)
        # one clean + two still-bad → hold degraded
        holding = base + healthy(8.0, n=1) + healthy(120.0, n=2)
        v = evaluate_perf(holding, CFG, was_degraded=True)
        self.assertTrue(v.degraded)
        self.assertFalse(v.changed)
        # whole candidate window clean → recover (edge)
        recovered = base + healthy(8.0, n=3)
        v2 = evaluate_perf(recovered, CFG, was_degraded=True)
        self.assertFalse(v2.degraded)
        self.assertTrue(v2.changed)

    def test_down_samples_excluded_from_baseline(self):
        # A burst of DOWN polls (latency None) must not corrupt the baseline.
        pool = healthy(8.0, n=12) + [Sample(None, 100.0, None, DOWN) for _ in range(5)]
        window = pool + healthy(120.0, n=3)
        v = evaluate_perf(window, CFG, was_degraded=False)
        self.assertTrue(v.degraded)
        self.assertAlmostEqual(v.baseline_ms, 8.0, places=1)

    def test_thin_data_holds_existing_degraded_flag(self):
        # Restart mid-degradation with too little history → don't flap to normal.
        window = healthy(8.0, n=4)
        v = evaluate_perf(window, CFG, was_degraded=True)
        self.assertTrue(v.degraded)
        self.assertFalse(v.changed)


if __name__ == "__main__":
    unittest.main()
