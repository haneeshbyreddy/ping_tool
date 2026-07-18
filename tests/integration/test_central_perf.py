import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import perf as central_perf
from wisp.central.engine import build_engine
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.state_machine import CycleResult, DOWN, UP
from wisp.ingress.probers import PingResult
from support import RecordingNotifier

TS = "2026-01-01T00:00:00+00:00"
ORG = "ispA"
IP = "10.0.0.1"

class PerfSweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            central_db=Path(self.tmp.name) / "central.db",
            perf_window=20, perf_min_samples=10, perf_consecutive=3,
            perf_deviation_factor=3.0, perf_mad_k=5.0, perf_min_baseline_ms=5.0,
        )
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_operator="op")
        self.dev = self.store.create_org_device(ORG, {
            "name": "Backhaul", "ip_address": IP, "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.eng = build_engine(self.store, ORG, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _feed(self, samples, eng=None, notifier=None, cfg=None):
        eng = eng or self.eng
        notifier = notifier or self.notifier
        cfg = cfg or self.cfg
        for i, (lat, loss, jit, st) in enumerate(samples):
            ts = f"2026-01-01T00:{i:02d}:00+00:00"
            results = {IP: PingResult(IP, lat, loss, jit)}
            cycle = CycleResult(states={self.dev: st}, events=[], canary_down=False)
            central_perf.record_and_evaluate(self.store, ORG, eng, cycle, results,
                                             ts, notifier, cfg)

    def _perf_row(self):
        return self.store.device_perf_state(ORG, self.dev)

    def _queued(self):
        # Perf alerts are DIGEST-tier now: they queue, they don't push. The
        # transition-only contract still holds — one queued row per change.
        return self.store.pending_digest(ORG)

    def _clear_queue(self):
        self.store.mark_digests_sent(ORG, "2026-01-01T00:30:00+00:00")

    def test_sustained_degradation_pages_operator_once(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.assertEqual(self.notifier.sent, [])   # digest-tier, no live push
        q = self._queued()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["kind"], "PERF_DEGRADED")
        self.assertIn("Slow link", q[0]["title"])
        row = self._perf_row()
        self.assertEqual(row["degraded"], 1)
        self.assertEqual(row["metric"], "latency")
        self.assertEqual(row["since"], "2026-01-01T00:17:00+00:00")

    def test_recovery_sends_one_notice(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self._clear_queue()
        self._feed([(8.0, 0.0, 2.0, UP)] * 3)
        q = self._queued()
        self.assertEqual(len(q), 1)
        self.assertIn("Recovered", q[0]["title"])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_down_clears_perf_silently(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.notifier.sent.clear()
        self._feed([(None, 100.0, None, DOWN)] * 3)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_alerts_gate_suppresses_push_but_keeps_badge(self):
        gated = replace(self.cfg, perf_alerts=False, perf_window=20,
                        perf_min_samples=10, perf_consecutive=3)
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3, cfg=gated)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._perf_row()["degraded"], 1)

    def test_restart_does_not_repage(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.notifier.sent.clear()
        eng2 = build_engine(self.store, ORG, self.cfg)
        notifier2 = RecordingNotifier()
        self._feed([(120.0, 0.0, 2.0, UP)], eng=eng2, notifier=notifier2)
        self.assertEqual(notifier2.sent, [])

    def test_org_isolation(self):
        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        eng_b = build_engine(self.store, "ispB", self.cfg)
        cycle = CycleResult(states={other: UP}, events=[], canary_down=False)
        central_perf.record_and_evaluate(
            self.store, "ispB", eng_b, cycle, {"10.0.0.5": PingResult("10.0.0.5", 8.0, 0.0)},
            TS, self.notifier, self.cfg)
        self.assertIsNone(self.store.device_perf_state(ORG, self.dev))

if __name__ == "__main__":
    unittest.main()
