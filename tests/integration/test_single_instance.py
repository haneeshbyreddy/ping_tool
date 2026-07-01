"""Duplicate-daemon prevention: the OS-level single-instance lock that stops two
probe processes for the same tenant/node from running at once (the idempotent
OutageOpened guard on the DB side of this is `central/engine.py`'s equivalent,
covered by `test_central_brain.py`). Temp lock file.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

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


if __name__ == "__main__":
    unittest.main()
