"""Central-side per-link performance-baseline sweep (central/perf.py) — mirrors the old
single-box test_perf.py one-for-one, ported onto CentralStore's tenant-scoped
device_perf/device_perf_samples tables: operator-only edge alerts, restart-safe state,
the perf_alerts gate, and a hard-DOWN device clearing its badge silently. Temp DB + a
recording notifier — no real clock/network.
"""
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import perf as central_perf
from wisp.central.engine import build_engine
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.state_machine import CycleResult, DOWN, UP
from wisp.egress.notifiers import NotifyResult
from wisp.ingress.probers import PingResult

TS = "2026-01-01T00:00:00+00:00"
TENANT = "ispA"
IP = "10.0.0.1"


class RecordingNotifier:
    channel = "ntfy"

    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(True)


class PerfSweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            central_db=Path(self.tmp.name) / "central.db",
            perf_window=20, perf_min_samples=10, perf_consecutive=3,
            perf_deviation_factor=3.0, perf_mad_k=5.0, perf_min_baseline_ms=5.0,
        )
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(TENANT, ntfy_topic_operator="op")
        self.dev = self.store.create_org_device(TENANT, {
            "name": "Backhaul", "ip_address": IP, "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.eng = build_engine(self.store, TENANT, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _feed(self, samples, eng=None, notifier=None, cfg=None):
        """samples: list of (latency, loss, jitter, state)."""
        eng = eng or self.eng
        notifier = notifier or self.notifier
        cfg = cfg or self.cfg
        for i, (lat, loss, jit, st) in enumerate(samples):
            ts = f"2026-01-01T00:{i:02d}:00+00:00"
            results = {IP: PingResult(IP, lat, loss, jit)}
            cycle = CycleResult(states={self.dev: st}, events=[], canary_down=False)
            central_perf.record_and_evaluate(self.store, TENANT, eng, cycle, results,
                                             ts, notifier, cfg)

    def _perf_row(self):
        return self.store.device_perf_state(TENANT, self.dev)

    def test_sustained_degradation_pages_operator_once(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.assertEqual(len(self.notifier.sent), 1)
        msg = self.notifier.sent[0]
        self.assertEqual(msg["recipient"], "op")
        self.assertEqual(msg["priority"], 3)
        self.assertIn("Slow link", msg["title"])
        row = self._perf_row()
        self.assertEqual(row["degraded"], 1)
        self.assertEqual(row["metric"], "latency")
        self.assertEqual(row["since"], "2026-01-01T00:17:00+00:00")   # the tripping sample

    def test_recovery_sends_one_notice(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.notifier.sent.clear()
        self._feed([(8.0, 0.0, 2.0, UP)] * 3)   # three clean samples -> recovery edge
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Recovered", self.notifier.sent[0]["title"])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_down_clears_perf_silently(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.notifier.sent.clear()
        # device goes hard DOWN — perf flag clears, but no perf push (DOWN owns it)
        self._feed([(None, 100.0, None, DOWN)] * 3)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_alerts_gate_suppresses_push_but_keeps_badge(self):
        gated = replace(self.cfg, perf_alerts=False, perf_window=20,
                        perf_min_samples=10, perf_consecutive=3)
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3, cfg=gated)
        self.assertEqual(self.notifier.sent, [])           # no page
        self.assertEqual(self._perf_row()["degraded"], 1)  # badge still set

    def test_restart_does_not_repage(self):
        self._feed([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.notifier.sent.clear()
        # a restart: brand-new engine, same store — the window survives (it's in the DB)
        eng2 = build_engine(self.store, TENANT, self.cfg)
        notifier2 = RecordingNotifier()
        self._feed([(120.0, 0.0, 2.0, UP)], eng=eng2, notifier=notifier2)   # still degraded
        self.assertEqual(notifier2.sent, [])   # was_degraded rehydrated -> no re-page

    def test_tenant_isolation(self):
        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        eng_b = build_engine(self.store, "ispB", self.cfg)
        cycle = CycleResult(states={other: UP}, events=[], canary_down=False)
        central_perf.record_and_evaluate(
            self.store, "ispB", eng_b, cycle, {"10.0.0.5": PingResult("10.0.0.5", 8.0, 0.0)},
            TS, self.notifier, self.cfg)
        self.assertIsNone(self.store.device_perf_state(TENANT, self.dev))


if __name__ == "__main__":
    unittest.main()
