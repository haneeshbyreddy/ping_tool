import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import RecordingNotifier
from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central import notify_policy
from wisp.central.notify_policy import (
    AlertRouter, DIGEST, PUSH, compose_digest, flush_digests, tier_for,
)

BASE = datetime(2026, 7, 18, 6, 0, 0, tzinfo=timezone.utc)


def _ts(min_offset: float = 0.0) -> str:
    return (BASE + timedelta(minutes=min_offset)).isoformat()


class TierTest(unittest.TestCase):
    def test_snmp_kinds_digest(self):
        for k in ("PON_FAULT", "ONU_DUP_MAC", "ONU_LIMIT",
                  "PERF_DEGRADED", "ON_BACKUP", "HOURLY_ESCALATION"):
            self.assertEqual(tier_for(k), DIGEST, k)

    def test_icmp_kinds_push(self):
        for k in ("DEVICE_DOWN", "DEVICE_RESTORED", "UPLINK_DOWN",
                  "PORT_DOWN", "PORT_RESTORED"):
            self.assertEqual(tier_for(k), PUSH, k)

    def test_port_bandwidth_kinds_push(self):
        # bandwidth floor/ceiling crossings + their clears buzz immediately
        # (operator ask 2026-07-18), not the hourly digest.
        for k in ("PORT_BW_LOW", "PORT_BW_OK", "PORT_BW_HIGH", "PORT_BW_NORMAL"):
            self.assertEqual(tier_for(k), PUSH, k)

    def test_unknown_defaults_push(self):
        self.assertEqual(tier_for("SOMETHING_NEW"), PUSH)


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.notifier = RecordingNotifier()
        self.cfg = Config(db_path=Path(self.tmp.name) / "wisp.db")
        self.store.set_org("ispA", ntfy_topic_operator="ops")
        self.router = AlertRouter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _log_rows(self):
        with self.store._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT status, payload, kind FROM alert_log ORDER BY id")]

    def test_digest_kind_queues_not_sent(self):
        self.router.emit("PON_FAULT", topic="ops", title="fiber cut", body="x",
                         priority=3, ts=_ts(), device_id=7)
        self.assertEqual(self.notifier.sent, [])            # nothing pushed
        pending = self.store.pending_digest("ispA")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "PON_FAULT")
        self.assertEqual(self._log_rows()[0]["status"], "digest")

    def test_push_kind_sends(self):
        self.router.emit("PORT_DOWN", topic="ops", title="port down", body="x",
                         priority=3, ts=_ts(), device_id=7, cooldown_min=0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self._log_rows()[0]["status"], "sent")
        self.assertEqual(self.store.pending_digest("ispA"), [])

    def test_gate_off_suppresses(self):
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(), device_id=7, gate=False)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._log_rows()[0]["status"], "suppressed")

    def test_no_topic_suppresses(self):
        self.router.emit("PON_FAULT", topic=None, title="t", body="x",
                         priority=3, ts=_ts(), device_id=7)
        self.assertEqual(self.store.pending_digest("ispA"), [])  # not queued
        self.assertEqual(self._log_rows()[0]["status"], "suppressed")

    def test_cooldown_suppresses_repeat(self):
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(0), device_id=7)   # cfg cooldown 30m
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(10), device_id=7)  # within window
        self.assertEqual(len(self.notifier.sent), 1)
        statuses = [r["status"] for r in self._log_rows()]
        self.assertEqual(statuses, ["sent", "suppressed"])

    def test_cooldown_scoped_per_device(self):
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(0), device_id=7)
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(1), device_id=8)   # different device
        self.assertEqual(len(self.notifier.sent), 2)

    def test_cooldown_expires(self):
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(0), device_id=7)
        self.router.emit("PORT_DOWN", topic="ops", title="t", body="x",
                         priority=3, ts=_ts(45), device_id=7)  # past 30m window
        self.assertEqual(len(self.notifier.sent), 2)


class DigestFlushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.notifier = RecordingNotifier()
        self.cfg = Config(db_path=Path(self.tmp.name) / "wisp.db")
        self.store.set_org("ispA", ntfy_topic_operator="ops")
        self.router = AlertRouter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _queue(self, kind, title, at):
        self.router.emit(kind, topic="ops", title=title, body="", priority=3,
                         ts=_ts(at), device_id=1)

    def test_not_flushed_before_interval(self):
        self._queue("PON_FAULT", "cut A", 0)
        flush_digests(self.store, "ispA", self.notifier, self.cfg, _ts(30))
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(len(self.store.pending_digest("ispA")), 1)

    def test_flushed_after_interval(self):
        self._queue("PON_FAULT", "cut A", 0)
        self._queue("PON_FAULT", "cut B", 5)
        self._queue("ONU_LIMIT", "PON full", 10)
        flush_digests(self.store, "ispA", self.notifier, self.cfg, _ts(65))
        self.assertEqual(len(self.notifier.sent), 1)
        msg = self.notifier.sent[0]
        self.assertIn("3 events", msg["title"])
        self.assertIn("PON faults", msg["body"])
        self.assertEqual(self.store.pending_digest("ispA"), [])   # marked sent

    def test_flush_noop_when_empty(self):
        flush_digests(self.store, "ispA", self.notifier, self.cfg, _ts(120))
        self.assertEqual(self.notifier.sent, [])

    def test_failed_send_retries(self):
        self.notifier.ok = False
        self._queue("PON_FAULT", "cut A", 0)
        flush_digests(self.store, "ispA", self.notifier, self.cfg, _ts(65))
        # send failed -> rows stay pending for next cycle
        self.assertEqual(len(self.store.pending_digest("ispA")), 1)
        self.notifier.ok = True
        flush_digests(self.store, "ispA", self.notifier, self.cfg, _ts(70))
        self.assertEqual(self.store.pending_digest("ispA"), [])


class ComposeTest(unittest.TestCase):
    def test_groups_and_caps(self):
        rows = [{"kind": "PON_FAULT", "title": f"cut {i}"} for i in range(5)]
        rows += [{"kind": "PORT_BW_LOW", "title": "slow"}]
        title, body = compose_digest(rows)
        self.assertIn("6 events", title)
        self.assertIn("🔦 PON faults (5)", body)
        self.assertIn("… +2 more", body)          # 5 shown-capped at 3
        self.assertIn("Low bandwidth (1)", body)


if __name__ == "__main__":
    unittest.main()
