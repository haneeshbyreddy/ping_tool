import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import redundancy as central_redundancy
from wisp.central.engine import build_engine
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.state_machine import DOWN, UP
from support import RecordingNotifier

TS = "2026-01-01T00:00:00+00:00"
ORG = "ispA"

class RedundancySweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_operator="op")
        self.primary = self.store.create_org_device(ORG, {
            "name": "Primary Tower", "ip_address": "10.0.0.2", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.backup = self.store.create_org_device(ORG, {
            "name": "Backup Tower", "ip_address": "10.0.0.3", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.child = self.store.create_org_device(ORG, {
            "name": "Rampur Relay", "ip_address": "10.0.0.9", "device_type": None,
            "region": "Rampur", "parent_device_id": self.primary})
        self.store.create_backup_link(ORG, self.child, self.backup)
        self.notifier = RecordingNotifier()
        self.eng = build_engine(self.store, ORG, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self):
        return self.store.device_redundancy_state(ORG, self.child)

    def _counts(self):
        with self.store._connect() as conn:
            o = conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
            e = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
        return o, e

    def _sweep(self, redundancy, states, ts=TS):
        central_redundancy.sweep(self.store, ORG, self.eng, redundancy, states,
                                 self.notifier, ts, self.cfg)

    def _queued(self):
        # On-backup alerts are DIGEST-tier: they queue, they don't push. The
        # transition-only contract still holds — one queued row per change.
        return self.store.pending_digest(ORG)

    def _clear_queue(self):
        self.store.mark_digests_sent(ORG, TS)

    def test_enter_pages_operator_once_and_writes_badge(self):
        self._sweep({self.child: True}, {self.child: UP})
        self.assertEqual(self.notifier.sent, [])   # digest-tier, no live push
        q = self._queued()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["kind"], "ON_BACKUP")
        self.assertIn("On backup", q[0]["title"])
        row = self._row()
        self.assertEqual(row["on_backup"], 1)
        self.assertEqual(row["primary_down_since"], TS)
        self.assertEqual(self._counts(), (0, 0))
        self._sweep({self.child: True}, {self.child: UP})
        self.assertEqual(len(self._queued()), 1)

    def test_leave_sends_one_recovered_notice(self):
        self._sweep({self.child: True}, {self.child: UP})
        self._clear_queue()
        self._sweep({self.child: False}, {self.child: UP})
        q = self._queued()
        self.assertEqual(len(q), 1)
        self.assertIn("Primary restored", q[0]["title"])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_node_down_clears_badge_silently(self):
        self._sweep({self.child: True}, {self.child: UP})
        self._clear_queue()
        self._sweep({self.child: True}, {self.child: DOWN})
        self.assertEqual(self._queued(), [])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_alerts_gate_suppresses_page_keeps_badge(self):
        gated_cfg = replace(self.cfg, backup_alerts=False)
        central_redundancy.sweep(self.store, ORG, self.eng, {self.child: True},
                                 {self.child: UP}, self.notifier, TS, gated_cfg)
        self.assertEqual(self._queued(), [])
        self.assertEqual(self._row()["on_backup"], 1)

    def test_restart_does_not_repage(self):
        self._sweep({self.child: True}, {self.child: UP})
        self._clear_queue()
        eng2 = build_engine(self.store, ORG, self.cfg)
        central_redundancy.sweep(self.store, ORG, eng2, {self.child: True},
                                 {self.child: UP}, self.notifier, TS, self.cfg)
        self.assertEqual(self._queued(), [])

    def test_org_isolation(self):
        other_dev = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        eng_b = build_engine(self.store, "ispB", self.cfg)
        central_redundancy.sweep(self.store, "ispB", eng_b, {other_dev: True},
                                 {other_dev: UP}, self.notifier, TS, self.cfg)
        self.assertIsNone(self.store.device_redundancy_state(ORG, self.child))

if __name__ == "__main__":
    unittest.main()
