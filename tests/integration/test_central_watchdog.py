import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.watchdog import CentralWatchdog, STALE_MARK
from support import RecordingNotifier

NOW = datetime(2026, 1, 1, 12, 0, 0)

def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")

class CentralWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.cfg = Config(central_node_stale_s=180)

    def tearDown(self):
        self.tmp.cleanup()

    def _seen(self, org_id, node, age_s, **org_kwargs):
        self.store.record_heartbeat(org_id, node, {"fleet_size": 1},
                                    now=_iso(NOW - timedelta(seconds=age_s)))
        if org_kwargs:
            self.store.set_org(org_id, **org_kwargs)

    def _wd(self, notifier):
        return CentralWatchdog(self.store, self.cfg, notifier)

    def test_stale_node_pages_then_recovers(self):
        self._seen("ispA", "edge-1", age_s=600)
        notifier = RecordingNotifier()
        wd = self._wd(notifier)
        acted = wd.check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("DOWN", notifier.sent[0]["title"])
        self.assertTrue(self.store.last_node_alarm("ispA", "edge-1"))
        self.store.record_heartbeat("ispA", "edge-1", {"fleet_size": 1}, now=_iso(NOW))
        acted = wd.check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "recover")])
        self.assertEqual(len(notifier.sent), 2)
        self.assertFalse(self.store.last_node_alarm("ispA", "edge-1"))

    def test_only_acts_on_transition(self):
        self._seen("ispA", "edge-1", age_s=600)
        notifier = RecordingNotifier()
        wd = self._wd(notifier)
        self.assertEqual(len(wd.check(now=NOW)), 1)
        self.assertEqual(wd.check(now=NOW), [])
        self.assertEqual(len(notifier.sent), 1)

    def test_fresh_node_never_alarms(self):
        self._seen("ispA", "edge-1", age_s=30)
        notifier = RecordingNotifier()
        self.assertEqual(self._wd(notifier).check(now=NOW), [])
        self.assertEqual(notifier.sent, [])

    def test_restart_does_not_repage(self):
        self._seen("ispA", "edge-1", age_s=600)
        first = RecordingNotifier()
        self._wd(first).check(now=NOW)
        self.assertEqual(len(first.sent), 1)
        second = RecordingNotifier()
        wd2 = self._wd(second)
        self.assertEqual(wd2.check(now=NOW), [])
        self.assertEqual(second.sent, [])

    def test_failed_send_retries_next_tick(self):
        self._seen("ispA", "edge-1", age_s=600)
        down = RecordingNotifier(ok=False)
        wd = self._wd(down)
        self.assertEqual(wd.check(now=NOW), [])
        self.assertFalse(self.store.last_node_alarm("ispA", "edge-1"))
        wd.notifier = RecordingNotifier()
        acted = wd.check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])

    def test_routes_to_org_topic_else_fallback(self):
        self._seen("ispA", "edge-1", age_s=600, ntfy_topic="ispA-ops")
        self._seen("ispB", "edge-2", age_s=600)
        notifier = RecordingNotifier()
        self._wd(notifier).check(now=NOW)
        topics = {s["recipient"] for s in notifier.sent}
        self.assertIn("ispA-ops", topics)
        self.assertIn(self.cfg.central_ntfy_topic, topics)

    def test_per_node_isolation(self):
        self._seen("ispA", "edge-1", age_s=600)
        self._seen("ispA", "edge-2", age_s=30)
        notifier = RecordingNotifier()
        acted = self._wd(notifier).check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])

if __name__ == "__main__":
    unittest.main()
