"""Central-side on-backup redundancy sweep (central/redundancy.py) — mirrors the old
single-box test_redundancy.py one-for-one, ported onto CentralStore's tenant-scoped
device_redundancy table: a single operator edge-page, restart-safe state, the
backup_alerts gate, silent clear when the node itself goes DOWN, and the invariant that
on-backup never opens an outage or an escalation. Temp DB + a recording notifier — no
real clock/network.
"""
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import redundancy as central_redundancy
from wisp.central.engine import build_engine
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.state_machine import DOWN, UP
from wisp.egress.notifiers import NotifyResult

TS = "2026-01-01T00:00:00+00:00"
TENANT = "ispA"


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
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(TENANT, ntfy_topic_operator="op")
        self.primary = self.store.create_org_device(TENANT, {
            "name": "Primary Tower", "ip_address": "10.0.0.2", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.backup = self.store.create_org_device(TENANT, {
            "name": "Backup Tower", "ip_address": "10.0.0.3", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.child = self.store.create_org_device(TENANT, {
            "name": "Rampur Relay", "ip_address": "10.0.0.9", "device_type": None,
            "region": "Rampur", "parent_device_id": self.primary})
        self.store.create_backup_link(TENANT, self.child, self.backup)
        self.notifier = RecordingNotifier()
        self.eng = build_engine(self.store, TENANT, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self):
        return self.store.device_redundancy_state(TENANT, self.child)

    def _counts(self):
        with self.store._connect() as conn:
            o = conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
            e = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
        return o, e

    def _sweep(self, redundancy, states, ts=TS):
        central_redundancy.sweep(self.store, TENANT, self.eng, redundancy, states,
                                 self.notifier, ts, self.cfg)

    def test_enter_pages_operator_once_and_writes_badge(self):
        self._sweep({self.child: True}, {self.child: UP})
        self.assertEqual(len(self.notifier.sent), 1)
        msg = self.notifier.sent[0]
        self.assertEqual(msg["recipient"], "op")
        self.assertEqual(msg["priority"], 3)
        self.assertIn("On backup", msg["title"])
        row = self._row()
        self.assertEqual(row["on_backup"], 1)
        self.assertEqual(row["primary_down_since"], TS)
        # NOT louder — no outage, no escalation row.
        self.assertEqual(self._counts(), (0, 0))
        # a second sweep, still on backup -> no second page (edge-only)
        self._sweep({self.child: True}, {self.child: UP})
        self.assertEqual(len(self.notifier.sent), 1)

    def test_leave_sends_one_recovered_notice(self):
        self._sweep({self.child: True}, {self.child: UP})
        self.notifier.sent.clear()
        self._sweep({self.child: False}, {self.child: UP})
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("Primary restored", self.notifier.sent[0]["title"])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_node_down_clears_badge_silently(self):
        self._sweep({self.child: True}, {self.child: UP})
        self.notifier.sent.clear()
        # the engine's full pass said on_backup, but the node confirmed hard DOWN — the
        # outage owns it, so the badge clears WITHOUT a "primary restored" page.
        self._sweep({self.child: True}, {self.child: DOWN})
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._row()["on_backup"], 0)

    def test_alerts_gate_suppresses_page_keeps_badge(self):
        gated_cfg = replace(self.cfg, backup_alerts=False)
        central_redundancy.sweep(self.store, TENANT, self.eng, {self.child: True},
                                 {self.child: UP}, self.notifier, TS, gated_cfg)
        self.assertEqual(self.notifier.sent, [])       # no page
        self.assertEqual(self._row()["on_backup"], 1)  # badge still set

    def test_restart_does_not_repage(self):
        self._sweep({self.child: True}, {self.child: UP})
        self.notifier.sent.clear()
        # brand-new engine, same store (a restart mid-failover)
        eng2 = build_engine(self.store, TENANT, self.cfg)
        central_redundancy.sweep(self.store, TENANT, eng2, {self.child: True},
                                 {self.child: UP}, self.notifier, TS, self.cfg)
        self.assertEqual(self.notifier.sent, [])       # was_on_backup rehydrated

    def test_tenant_isolation(self):
        other_dev = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        eng_b = build_engine(self.store, "ispB", self.cfg)
        central_redundancy.sweep(self.store, "ispB", eng_b, {other_dev: True},
                                 {other_dev: UP}, self.notifier, TS, self.cfg)
        self.assertIsNone(self.store.device_redundancy_state(TENANT, self.child))


if __name__ == "__main__":
    unittest.main()
