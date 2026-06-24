"""Integration tests for the per-link performance-baseline sweep (AlertDispatcher
.perf_sweep): operator-only edge alerts, restart-safe state in device_perf, and the
WISP_PERF_ALERTS gate. Temp DB + a recording notifier — no real clock/network.
"""
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.egress.notifiers import AlertDispatcher, NotifyResult
from wisp.core.state_machine import DOWN, UP, DeviceMeta, MonitorEngine

TS = "2026-01-01T00:00:00+00:00"


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
            db_path=Path(self.tmp.name) / "t.db",
            perf_window=20, perf_min_samples=10, perf_consecutive=3,
            perf_deviation_factor=3.0, perf_mad_k=5.0,
            perf_min_baseline_ms=5.0, ntfy_topic_operator="op",
        )
        migrate(self.cfg)
        self.dev = DeviceMeta(id=1, name="Backhaul", ip_address="10.0.0.1",
                              region="Rampur", parent_device_id=None,
                              technician_phone=None)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (?,?,?,?)",
                      (1, "Backhaul", "10.0.0.1", "Rampur"))
            c.commit()
        self.notifier = RecordingNotifier()
        self.engine = MonitorEngine([self.dev], self.cfg)
        self.disp = AlertDispatcher(self.engine, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _polls(self, samples):
        """samples: list of (latency, loss, jitter, state)."""
        with connect(self.cfg) as c:
            for i, (lat, loss, jit, st) in enumerate(samples):
                c.execute(
                    "INSERT INTO poll_results (device_id, timestamp, latency_ms,"
                    " packet_loss, jitter_ms, state) VALUES (1,?,?,?,?,?)",
                    (f"2026-01-01T00:{i:02d}:00+00:00", lat, loss, jit, st))
            c.commit()

    def _perf_row(self):
        with connect(self.cfg) as c:
            return c.execute("SELECT * FROM device_perf WHERE device_id=1").fetchone()

    def test_sustained_degradation_pages_operator_once(self):
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        # exactly one push, to the operator topic, low priority
        self.assertEqual(len(self.notifier.sent), 1)
        msg = self.notifier.sent[0]
        self.assertEqual(msg["recipient"], "op")
        self.assertEqual(msg["priority"], 3)
        self.assertIn("Slow link", msg["title"])
        row = self._perf_row()
        self.assertEqual(row["degraded"], 1)
        self.assertEqual(row["metric"], "latency")
        self.assertEqual(row["since"], TS)
        # a second sweep with no change must NOT re-page (edge-only)
        self.disp.perf_sweep(TS)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_recovery_sends_one_notice(self):
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        self.notifier.sent.clear()
        # three clean polls → recovery edge
        self._polls([(8.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Recovered", self.notifier.sent[0]["title"])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_down_clears_perf_silently(self):
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        self.notifier.sent.clear()
        # device goes hard DOWN — perf flag clears, but no perf push (DOWN owns it)
        self._polls([(None, 100.0, None, DOWN)] * 3)
        self.disp.perf_sweep(TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._perf_row()["degraded"], 0)

    def test_alerts_gate_suppresses_push_but_keeps_badge(self):
        self.disp.cfg = replace(self.cfg, perf_alerts=False)
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        self.assertEqual(self.notifier.sent, [])           # no page
        self.assertEqual(self._perf_row()["degraded"], 1)  # badge still set

    def test_nodes_list_surfaces_perf_badge(self):
        from wisp.server import services
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        node = next(n for n in services.nodes_list(self.cfg) if n["id"] == 1)
        self.assertIsNotNone(node["perf"])
        self.assertEqual(node["perf"]["metric"], "latency")
        self.assertEqual(node["perf"]["current_ms"], 120.0)
        self.assertEqual(node["perf"]["baseline_ms"], 8.0)

    def test_restart_does_not_repage(self):
        self._polls([(8.0, 0.0, 2.0, UP)] * 15 + [(120.0, 0.0, 2.0, UP)] * 3)
        self.disp.perf_sweep(TS)
        self.notifier.sent.clear()
        # simulate a restart: brand-new dispatcher/engine, same DB
        disp2 = AlertDispatcher(MonitorEngine([self.dev], self.cfg),
                                self.notifier, self.cfg)
        self._polls([(120.0, 0.0, 2.0, UP)])   # still degraded
        disp2.perf_sweep(TS)
        self.assertEqual(self.notifier.sent, [])  # was_degraded rehydrated → no re-page


if __name__ == "__main__":
    unittest.main()
