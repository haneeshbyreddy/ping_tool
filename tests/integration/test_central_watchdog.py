"""Cross-edge fleet watchdog tests (Phase 10 Part B): central pages an org when a node's
heartbeat goes stale, recovers, only acts on a transition, survives a restart without
re-paging, stays quiet for fresh nodes, and routes to the per-org topic. Temp store +
injected clock + a recording-notifier double (no network) — mirrors test_watchdog."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.watchdog import CentralWatchdog, STALE_MARK
from wisp.egress.notifiers import NotifyResult

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


class CentralWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        # 180s stale threshold (the default).
        self.cfg = Config(central_node_stale_s=180)

    def tearDown(self):
        self.tmp.cleanup()

    def _seen(self, tenant, node, age_s, **org):
        """Register a node whose last heartbeat was age_s ago."""
        self.store.record_heartbeat(tenant, node, {"fleet_size": 1},
                                    now=_iso(NOW - timedelta(seconds=age_s)))
        if org:
            self.store.set_org(tenant, **org)

    def _wd(self, notifier):
        return CentralWatchdog(self.store, self.cfg, notifier)

    def test_stale_node_pages_then_recovers(self):
        self._seen("ispA", "edge-1", age_s=600)        # well past 180s
        notifier = RecordingNotifier()
        wd = self._wd(notifier)
        acted = wd.check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("DOWN", notifier.sent[0]["title"])
        self.assertTrue(self.store.last_node_alarm("ispA", "edge-1"))
        # A fresh heartbeat lands -> next check recovers.
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
        self.assertEqual(wd.check(now=NOW), [])        # still stale, no second page
        self.assertEqual(len(notifier.sent), 1)

    def test_fresh_node_never_alarms(self):
        self._seen("ispA", "edge-1", age_s=30)         # well within threshold
        notifier = RecordingNotifier()
        self.assertEqual(self._wd(notifier).check(now=NOW), [])
        self.assertEqual(notifier.sent, [])

    def test_restart_does_not_repage(self):
        self._seen("ispA", "edge-1", age_s=600)
        first = RecordingNotifier()
        self._wd(first).check(now=NOW)
        self.assertEqual(len(first.sent), 1)
        # New watchdog instance (central restarted) — still stale, but already-known down.
        second = RecordingNotifier()
        wd2 = self._wd(second)
        self.assertEqual(wd2.check(now=NOW), [])
        self.assertEqual(second.sent, [])

    def test_failed_send_retries_next_tick(self):
        self._seen("ispA", "edge-1", age_s=600)
        down = RecordingNotifier(ok=False)
        wd = self._wd(down)
        self.assertEqual(wd.check(now=NOW), [])         # send failed -> no transition recorded
        self.assertFalse(self.store.last_node_alarm("ispA", "edge-1"))  # not stranded alarmed
        # next tick, notifier healthy -> it pages
        wd.notifier = RecordingNotifier()
        acted = wd.check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])

    def test_routes_to_org_topic_else_fallback(self):
        self._seen("ispA", "edge-1", age_s=600, ntfy_topic="ispA-ops")
        self._seen("ispB", "edge-2", age_s=600)        # no org topic -> fallback
        notifier = RecordingNotifier()
        self._wd(notifier).check(now=NOW)
        topics = {s["recipient"] for s in notifier.sent}
        self.assertIn("ispA-ops", topics)
        self.assertIn(self.cfg.central_ntfy_topic, topics)

    def test_per_node_isolation(self):
        self._seen("ispA", "edge-1", age_s=600)        # stale
        self._seen("ispA", "edge-2", age_s=30)         # fresh
        notifier = RecordingNotifier()
        acted = self._wd(notifier).check(now=NOW)
        self.assertEqual(acted, [("ispA", "edge-1", "alarm")])


if __name__ == "__main__":
    unittest.main()
