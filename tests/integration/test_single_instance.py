"""Duplicate-daemon prevention: the single-instance lock + the idempotent
OutageOpened guard. Together they ensure one real outage == one outage row,
even if a second poller is somehow started. Temp DB / temp lock file.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.core.state_machine import DOWN, OutageOpened, OutageResolved, apply_events
from wisp.database.client import connect, migrate
from wisp.runtime.single_instance import AlreadyRunning, SingleInstance


class SingleInstanceLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "wisp.db.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_acquire_is_refused(self):
        first = SingleInstance(self.lock).acquire()
        try:
            with self.assertRaises(AlreadyRunning):
                SingleInstance(self.lock).acquire()
        finally:
            first.release()

    def test_lock_is_reusable_after_release(self):
        SingleInstance(self.lock).acquire().release()
        # Once released, a fresh process can take it again.
        second = SingleInstance(self.lock).acquire()
        second.release()

    def test_records_holder_pid(self):
        guard = SingleInstance(self.lock).acquire()
        try:
            self.assertEqual(self.lock.read_text().strip(), str(os.getpid()))
        finally:
            guard.release()

    def test_context_manager_releases(self):
        with SingleInstance(self.lock):
            with self.assertRaises(AlreadyRunning):
                SingleInstance(self.lock).acquire()
        # left the with-block -> lock free again
        SingleInstance(self.lock).acquire().release()


class IdempotentOutageOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        with connect(self.cfg) as conn:
            conn.execute(
                "INSERT INTO devices (id, name, ip_address, is_active)"
                " VALUES (1, 'dev', '10.0.0.1', 1)")
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _open_count(self):
        with connect(self.cfg) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM outages WHERE device_id=1 AND resolved_at IS NULL"
            ).fetchone()[0]

    def test_second_open_does_not_stack(self):
        """A second OutageOpened (e.g. a stray duplicate poller) must not create a
        second open row while one is already open."""
        with connect(self.cfg) as conn:
            apply_events(conn, [OutageOpened(1, DOWN)], "2026-06-23T08:34:54+00:00")
            apply_events(conn, [OutageOpened(1, DOWN)], "2026-06-23T08:34:57+00:00")
            conn.commit()
        self.assertEqual(self._open_count(), 1)

    def test_reopen_after_resolve(self):
        """After the outage resolves, a new DOWN legitimately opens a fresh row."""
        with connect(self.cfg) as conn:
            apply_events(conn, [OutageOpened(1, DOWN)], "2026-06-23T08:00:00+00:00")
            apply_events(conn, [OutageResolved(1)], "2026-06-23T08:05:00+00:00")
            apply_events(conn, [OutageOpened(1, DOWN)], "2026-06-23T09:00:00+00:00")
            conn.commit()
            total = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE device_id=1").fetchone()[0]
        self.assertEqual(self._open_count(), 1)
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
