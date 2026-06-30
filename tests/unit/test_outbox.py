"""Outbox DB-glue tests (Phase 10 Part A): record shaping, transactional enqueue, and the
eviction rule that sheds rollups but never an event. Temp DB, no network."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate, transaction
from wisp.database import outbox
from wisp.core.state_machine import (
    DeviceMeta, OutageOpened, OutageResolved, UplinkDown,
)

TS = "2026-06-30T12:00:00+00:00"


class _Row(dict):
    """A poll_rollups-shaped row that supports row["col"] access like sqlite3.Row."""


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _all(self):
        with connect(self.cfg) as c:
            return c.execute("SELECT id, kind, payload, attempts FROM outbox ORDER BY id").fetchall()

    # --- record shaping ---
    def test_event_record_denormalizes_device_meta(self):
        meta = {1: DeviceMeta(1, "Rampur Tower", "10.0.0.1", "Rampur", None, None)}
        rec = outbox.event_record(OutageOpened(1, "DOWN"), TS, meta)
        self.assertEqual(rec["type"], "OutageOpened")
        self.assertEqual(rec["device_id"], 1)
        self.assertEqual(rec["state"], "DOWN")
        self.assertEqual(rec["device_name"], "Rampur Tower")
        self.assertEqual(rec["device_ip"], "10.0.0.1")
        self.assertEqual(rec["device_region"], "Rampur")

    def test_event_record_uplink_has_no_device(self):
        rec = outbox.event_record(UplinkDown(), TS)
        self.assertEqual(rec["type"], "UplinkDown")
        self.assertNotIn("device_id", rec)

    # --- transactional enqueue ---
    def test_enqueue_events_rides_caller_transaction(self):
        meta = {1: DeviceMeta(1, "T", "10.0.0.1", "R", None, None)}
        events = [OutageOpened(1, "DOWN"), OutageResolved(1), UplinkDown()]
        with connect(self.cfg) as c:
            with transaction(c):
                n = outbox.enqueue_events(c, events, TS, meta)
        self.assertEqual(n, 3)
        rows = self._all()
        self.assertEqual([r["kind"] for r in rows], ["event", "event", "event"])

    def test_enqueue_rolls_back_with_caller(self):
        # If the caller's transaction aborts, the queued rows must NOT persist (they ride
        # the same txn as the poll/outage write — atomic or nothing).
        try:
            with connect(self.cfg) as c:
                with transaction(c):
                    outbox.enqueue_events(c, [OutageOpened(1, "DOWN")], TS)
                    raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(self._all(), [])

    # --- eviction: rollups go, events stay ---
    def test_evict_sheds_oldest_rollups_only(self):
        with connect(self.cfg) as c:
            with transaction(c):
                # interleave: r1 e2 r3 e4 r5  (ids 1..5)
                outbox._enqueue(c, "rollup", {"n": 1}, TS)
                outbox._enqueue(c, "event", {"n": 2}, TS)
                outbox._enqueue(c, "rollup", {"n": 3}, TS)
                outbox._enqueue(c, "event", {"n": 4}, TS)
                outbox._enqueue(c, "rollup", {"n": 5}, TS)
        with connect(self.cfg) as c:
            evicted = outbox.evict_rollups(c, 2)   # drop 2 oldest rollups (ids 1, 3)
            c.commit()
        self.assertEqual(evicted, 2)
        rows = self._all()
        self.assertEqual([(r["id"], r["kind"]) for r in rows],
                         [(2, "event"), (4, "event"), (5, "rollup")])

    def test_evict_never_drops_events_even_when_over(self):
        # A backlog that is ALL events: nothing can be evicted (an event is sacred), so the
        # queue is allowed to exceed the cap rather than lose an outage record.
        with connect(self.cfg) as c:
            with transaction(c):
                for i in range(3):
                    outbox._enqueue(c, "event", {"n": i}, TS)
        with connect(self.cfg) as c:
            evicted = outbox.evict_rollups(c, 10)
            c.commit()
        self.assertEqual(evicted, 0)
        self.assertEqual(outbox.count(connect(self.cfg)), 3)

    # --- drain helpers ---
    def test_pending_mark_sent_and_bump(self):
        with connect(self.cfg) as c:
            with transaction(c):
                for i in range(3):
                    outbox._enqueue(c, "event", {"n": i}, TS)
        with connect(self.cfg) as c:
            pend = outbox.pending(c, 2)
            self.assertEqual([r["id"] for r in pend], [1, 2])
            outbox.bump_attempts(c, [3])
            outbox.mark_sent(c, [1])
            c.commit()
        rows = self._all()
        self.assertEqual([r["id"] for r in rows], [2, 3])
        self.assertEqual([r["attempts"] for r in rows], [0, 1])


if __name__ == "__main__":
    unittest.main()
