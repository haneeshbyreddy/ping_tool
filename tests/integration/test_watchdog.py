"""Dead-monitor watchdog tests: it pages when polling goes stale, stays quiet
otherwise, only fires on a transition, survives a restart without re-paging, and
retries a failed page. Temp DB + injected clock + a recording notifier (no net).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.egress.notifiers import NotifyResult
from wisp.server.watchdog import MonitorWatchdog, STALE_MARK, OK_MARK

NOW = datetime(2026, 1, 1, 12, 0, 0)  # naive UTC


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


class RecordingNotifier:
    channel = "ntfy"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict] = []

    def send(self, recipient, title, body, priority) -> NotifyResult:
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(self.ok)


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db",
                          poll_interval_s=60)  # -> stale threshold 180s
        migrate(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    # --- fixtures ---
    def _device(self):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,criticality,region)"
                      " VALUES (1,'Tower','10.0.0.1',4,'Rampur')")
            c.commit()

    def _poll(self, age_s: int):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO poll_results (device_id,timestamp,packet_loss,state)"
                      " VALUES (1,?,0,'UP')", (_iso(NOW - timedelta(seconds=age_s)),))
            c.commit()

    def _logs(self):
        with connect(self.cfg) as c:
            return [dict(r) for r in c.execute(
                "SELECT payload, status FROM alert_log ORDER BY id")]

    # --- tests ---
    def test_pages_owner_when_stale(self):
        self._device(); self._poll(age_s=600)
        wd = MonitorWatchdog(self.cfg, RecordingNotifier())
        self.assertEqual(wd.check(NOW), "alarm")
        recipients = {s["recipient"] for s in wd.notifier.sent}
        self.assertEqual(recipients,
                         {self.cfg.ntfy_topic_owner, self.cfg.ntfy_topic_operator})
        self.assertEqual(self._logs()[-1], {"payload": STALE_MARK, "status": "sent"})

    def test_quiet_when_fresh(self):
        self._device(); self._poll(age_s=10)
        wd = MonitorWatchdog(self.cfg, RecordingNotifier())
        self.assertIsNone(wd.check(NOW))
        self.assertEqual(wd.notifier.sent, [])

    def test_no_alarm_before_first_poll_or_devices(self):
        # devices but no polls yet (fresh install) -> never alarm
        self._device()
        wd = MonitorWatchdog(self.cfg, RecordingNotifier())
        self.assertIsNone(wd.check(NOW))
        # polls but no active devices -> nothing to watch
        self._poll(age_s=600)
        with connect(self.cfg) as c:
            c.execute("UPDATE devices SET is_active=0"); c.commit()
        self.assertIsNone(MonitorWatchdog(self.cfg, RecordingNotifier()).check(NOW))

    def test_only_fires_once_then_recovers(self):
        self._device(); self._poll(age_s=600)
        wd = MonitorWatchdog(self.cfg, RecordingNotifier())
        self.assertEqual(wd.check(NOW), "alarm")
        # second stale check is a no-op (already alarmed)
        self.assertIsNone(wd.check(NOW))
        self.assertEqual(len(wd.notifier.sent), 2)  # owner + operator, once
        # a fresh poll arrives -> recovery page, exactly once
        self._poll(age_s=5)
        self.assertEqual(wd.check(NOW), "recover")
        self.assertEqual(self._logs()[-1], {"payload": OK_MARK, "status": "sent"})
        self.assertIsNone(wd.check(NOW))

    def test_restart_does_not_repage(self):
        self._device(); self._poll(age_s=600)
        MonitorWatchdog(self.cfg, RecordingNotifier()).check(NOW)  # first alarm
        # a brand-new watchdog (dashboard restart) sees the same stale state
        fresh = MonitorWatchdog(self.cfg, RecordingNotifier())
        self.assertTrue(fresh._alarm_active)            # rehydrated from alert_log
        self.assertIsNone(fresh.check(NOW))             # does not page again
        self.assertEqual(fresh.notifier.sent, [])

    def test_failed_send_is_retried_next_tick(self):
        self._device(); self._poll(age_s=600)
        wd = MonitorWatchdog(self.cfg, RecordingNotifier(ok=False))
        self.assertIsNone(wd.check(NOW))                # send failed -> no transition
        self.assertFalse(wd._alarm_active)
        self.assertEqual(self._logs()[-1], {"payload": STALE_MARK, "status": "failed"})
        # next tick the channel recovers
        wd.notifier.ok = True
        self.assertEqual(wd.check(NOW), "alarm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
