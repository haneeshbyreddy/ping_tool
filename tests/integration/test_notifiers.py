"""Dispatcher tests: routing, anti-spam, UNREACHABLE suppression, the DB-derived
escalation ladder, and acknowledgement. Uses a temp DB and controlled timestamps
so the time-based logic is deterministic (no real clock, no network).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.egress.notifiers import (
    AlertDispatcher,
    NotifyResult,
    _Attempt,
    acknowledge_outage,
    send_with_retry,
)
from wisp.core.state_machine import (
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


class RecordingNotifier:
    """Test double for the notifier interface: records every send instead of
    hitting the network (stands in for the removed dev-only mock channel)."""

    channel = "ntfy"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
        self.sent.append(
            {"recipient": recipient, "title": title, "body": body, "priority": priority}
        )
        return NotifyResult(True)


def meta(**over) -> DeviceMeta:
    base = dict(
        id=1, name="Tower", ip_address="d", criticality=4, region="Rampur",
        parent_device_id=None, power_ref_ip=None, technician_phone="+91TECH",
    )
    base.update(over)
    return DeviceMeta(**base)


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            db_path=Path(self.tmp.name) / "t.db",
            realert_after_min=10, escalate_owner_after_min=20, alert_dedupe_min=10,
        )
        migrate(self.cfg)
        self.dev = meta()
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO devices (id,name,ip_address,criticality,region,"
                "technician_phone) VALUES (?,?,?,?,?,?)",
                (self.dev.id, self.dev.name, self.dev.ip_address, self.dev.criticality,
                 self.dev.region, self.dev.technician_phone),
            )
            c.commit()
        self.notifier = RecordingNotifier()
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
        # tech channel + a copy to the operator channel (operators see everything)
        self.assertEqual({s["recipient"] for s in self.notifier.sent},
                         {self.cfg.ntfy_topic_tech, self.cfg.ntfy_topic_operator})
        # the alert_log records one primary send, against the tech channel
        sent_rows = [r for r in self._alert_log() if r["status"] == "sent"]
        self.assertEqual(len(sent_rows), 1)
        self.assertEqual(sent_rows[0]["recipient"], self.cfg.ntfy_topic_tech)
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
        # first dispatch fans out to tech + operator (2); second is suppressed
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertTrue(any(r["status"] == "suppressed" for r in self._alert_log()))

    def test_escalation_fires_when_unacked(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(1, DOWN, POWER_CAUSE)], T0)
        self.notifier.sent.clear()
        self.disp.sweep(T_LATER)
        recipients = {s["recipient"] for s in self.notifier.sent}
        # realert -> tech, owner escalation -> owner, both copied to operator
        self.assertEqual(recipients, {self.cfg.ntfy_topic_tech,
                                      self.cfg.ntfy_topic_owner,
                                      self.cfg.ntfy_topic_operator})
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


class SendRetryTest(unittest.TestCase):
    """The retry policy that keeps a transient blip from silently eating a page."""

    def _runner(self, outcomes):
        """outcomes: list of _Attempt to return in order. Captures backoff sleeps."""
        slept: list[float] = []
        seq = iter(outcomes)
        res = send_with_retry(lambda: next(seq), attempts=len(outcomes),
                              backoff=0.5, sleep=slept.append)
        return res, slept

    def test_succeeds_first_try_no_sleep(self):
        res, slept = self._runner([_Attempt(NotifyResult(True), False)])
        self.assertTrue(res.ok)
        self.assertEqual(slept, [])

    def test_retries_transient_then_succeeds_with_backoff(self):
        res, slept = self._runner([
            _Attempt(NotifyResult(False, "timeout"), True),
            _Attempt(NotifyResult(False, "timeout"), True),
            _Attempt(NotifyResult(True), False),
        ])
        self.assertTrue(res.ok)
        self.assertEqual(slept, [0.5, 1.0])  # exponential backoff between attempts

    def test_all_transient_returns_last_failure(self):
        res, slept = self._runner([
            _Attempt(NotifyResult(False, "boom"), True),
            _Attempt(NotifyResult(False, "boom"), True),
        ])
        self.assertFalse(res.ok)
        self.assertEqual(len(slept), 1)  # one backoff between the two attempts

    def test_non_retryable_stops_immediately(self):
        # a 4xx (bad topic/config) won't self-heal — fail fast, don't burn retries
        slept: list[float] = []
        calls = {"n": 0}
        def _attempt():
            calls["n"] += 1
            return _Attempt(NotifyResult(False, "HTTP 403"), False)
        res = send_with_retry(_attempt, attempts=5, backoff=0.5, sleep=slept.append)
        self.assertFalse(res.ok)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
