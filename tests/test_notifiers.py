"""Dispatcher tests: routing, anti-spam, UNREACHABLE suppression, the DB-derived
escalation ladder, and acknowledgement. Uses a temp DB and controlled timestamps
so the time-based logic is deterministic (no real clock, no network).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from db import connect, migrate
from notifiers import AlertDispatcher, MockNotifier, acknowledge_outage
from state_machine import (
    DOWN,
    POWER_CAUSE,
    UNREACHABLE,
    DeviceMeta,
    MonitorEngine,
    OutageOpened,
    OutageResolved,
)

T0 = "2026-01-01T00:00:00+00:00"
T_LATER = "2026-01-01T00:25:00+00:00"   # past both realert(+10) and escalate(+20)


def meta(**over) -> DeviceMeta:
    base = dict(
        id=1, name="Tower", ip_address="d", criticality=4, region="Rampur",
        parent_device_id=None, power_ref_ip=None, technician_phone="+91TECH",
        customer_count=100, base_revenue_impact=200.0,
    )
    base.update(over)
    return DeviceMeta(**base)


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            db_path=Path(self.tmp.name) / "t.db",
            realert_after_min=10, escalate_owner_after_min=20, alert_dedupe_min=10,
            owner_telegram_chat_id="OWNER",
        )
        migrate(self.cfg)
        self.dev = meta()
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO devices (id,name,ip_address,criticality,region,"
                "technician_phone,customer_count,base_revenue_impact) VALUES (?,?,?,?,?,?,?,?)",
                (self.dev.id, self.dev.name, self.dev.ip_address, self.dev.criticality,
                 self.dev.region, self.dev.technician_phone, self.dev.customer_count,
                 self.dev.base_revenue_impact),
            )
            c.commit()
        self.notifier = MockNotifier(quiet=True)
        self.engine = MonitorEngine([self.dev], self.cfg)
        self.disp = AlertDispatcher(self.engine, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _open_outage(self, state=DOWN, cause=POWER_CAUSE) -> int:
        with connect(self.cfg) as c:
            cur = c.execute(
                "INSERT INTO outages (device_id, started_at, final_state, inferred_cause)"
                " VALUES (?,?,?,?)", (self.dev.id, T0, state, cause))
            c.commit()
            return cur.lastrowid

    def _alert_log(self):
        with connect(self.cfg) as c:
            return [dict(r) for r in c.execute("SELECT * FROM alert_log ORDER BY id")]

    # --- tests ---
    def test_alert_routes_logs_and_schedules_escalations(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["recipient"], "+91TECH")
        sent_rows = [r for r in self._alert_log() if r["status"] == "sent"]
        self.assertEqual(len(sent_rows), 1)
        with connect(self.cfg) as c:
            esc = c.execute("SELECT kind FROM escalations ORDER BY kind").fetchall()
        self.assertEqual({r["kind"] for r in esc}, {"escalate_to_owner", "realert"})

    def test_unreachable_is_suppressed(self):
        self._open_outage(state=UNREACHABLE, cause=None)
        self.disp.dispatch([OutageOpened(1, UNREACHABLE, None)], T0)
        self.assertEqual(len(self.notifier.sent), 0)
        self.assertEqual(self._alert_log()[-1]["status"], "suppressed")

    def test_anti_spam_dedupe(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)  # same window
        self.assertEqual(len(self.notifier.sent), 1)  # second suppressed
        self.assertTrue(any(r["status"] == "suppressed" for r in self._alert_log()))

    def test_escalation_fires_when_unacked(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)
        self.notifier.sent.clear()
        self.disp.sweep(T_LATER)
        recipients = {s["recipient"] for s in self.notifier.sent}
        self.assertEqual(recipients, {"+91TECH", "OWNER"})  # realert + owner escalation
        with connect(self.cfg) as c:
            pending = c.execute(
                "SELECT COUNT(*) FROM escalations WHERE executed_at IS NULL").fetchone()[0]
        self.assertEqual(pending, 0)

    def test_ack_cancels_escalation(self):
        oid = self._open_outage()
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)
        self.notifier.sent.clear()
        self.assertTrue(acknowledge_outage(oid, "Suresh", self.cfg))
        self.disp.sweep(T_LATER)
        self.assertEqual(len(self.notifier.sent), 0)  # acked -> nothing fires

    def test_resolve_suppresses_restore_for_unreachable(self):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO outages (device_id, started_at, final_state, resolved_at)"
                      " VALUES (?,?,?,?)", (1, T0, UNREACHABLE, T_LATER))
            c.commit()
        self.disp.dispatch([OutageResolved(1)], T_LATER)
        self.assertEqual(len(self.notifier.sent), 0)  # never paged -> no restore msg


if __name__ == "__main__":
    unittest.main(verbosity=2)
