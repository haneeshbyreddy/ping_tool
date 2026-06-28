"""Integration tests for the on-backup redundancy sweep (AlertDispatcher
.redundancy_sweep): a single operator edge-page, restart-safe state in
device_redundancy, the WISP_BACKUP_ALERTS gate, silent clear when the node itself
goes DOWN, and the decision-#1 invariant that on-backup never opens an outage or an
escalation. Temp DB + a recording notifier — no real clock/network.
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
from wisp.core.state_machine import (
    BACKUP, DOWN, UP, DeviceMeta, MonitorEngine, ParentEdge,
)

TS = "2026-01-01T00:00:00+00:00"


class RecordingNotifier:
    channel = "ntfy"

    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(True)


class RedundancySweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db",
                          ntfy_topic_operator="op")
        migrate(self.cfg)
        # child(1) primary <-2>, backup <-3>; only the child is redundancy-capable.
        self.child = DeviceMeta(id=1, name="Rampur Relay", ip_address="10.0.0.9",
                                region="Rampur", parent_device_id=2,
                                technician_phone=None, parents=(ParentEdge(3, BACKUP),))
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (1,?,?,?)",
                      ("Rampur Relay", "10.0.0.9", "Rampur"))
            c.commit()
        self.notifier = RecordingNotifier()
        self.engine = MonitorEngine([self.child], self.cfg)
        self.disp = AlertDispatcher(self.engine, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self):
        with connect(self.cfg) as c:
            return c.execute(
                "SELECT * FROM device_redundancy WHERE device_id=1").fetchone()

    def _counts(self):
        with connect(self.cfg) as c:
            o = c.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
            e = c.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
        return o, e

    def test_enter_pages_operator_once_and_writes_badge(self):
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.assertEqual(len(self.notifier.sent), 1)
        msg = self.notifier.sent[0]
        self.assertEqual(msg["recipient"], "op")
        self.assertEqual(msg["priority"], 3)
        self.assertIn("On backup", msg["title"])
        row = self._row()
        self.assertEqual(row["on_backup"], 1)
        self.assertEqual(row["primary_down_since"], TS)
        # decision #1: NOT louder — no outage, no escalation row.
        self.assertEqual(self._counts(), (0, 0))
        # a second sweep, still on backup -> no second page (edge-only)
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_leave_sends_one_recovered_notice(self):
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.notifier.sent.clear()
        self.disp.redundancy_sweep({1: False}, {1: UP}, TS)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Primary restored", self.notifier.sent[0]["title"])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_node_down_clears_badge_silently(self):
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.notifier.sent.clear()
        # the engine's full pass said on_backup, but the node confirmed hard DOWN —
        # the outage owns it, so the badge clears WITHOUT a "primary restored" page.
        self.disp.redundancy_sweep({1: True}, {1: DOWN}, TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_alerts_gate_suppresses_page_keeps_badge(self):
        self.disp.cfg = replace(self.cfg, backup_alerts=False)
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.assertEqual(self.notifier.sent, [])       # no page
        self.assertEqual(self._row()["on_backup"], 1)  # badge still set

    def test_restart_does_not_repage(self):
        self.disp.redundancy_sweep({1: True}, {1: UP}, TS)
        self.notifier.sent.clear()
        # brand-new dispatcher/engine, same DB (a restart mid-failover)
        disp2 = AlertDispatcher(MonitorEngine([self.child], self.cfg),
                                self.notifier, self.cfg)
        disp2.redundancy_sweep({1: True}, {1: UP}, TS)
        self.assertEqual(self.notifier.sent, [])       # was_on_backup rehydrated


if __name__ == "__main__":
    unittest.main()
