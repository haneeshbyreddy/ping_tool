"""Monitor lifecycle — the DB backup is a valid, openable SQLite file. Temp DB.

(Config/restart orchestration was replaced by daemon self-reload, so the old
restart-request and restart-pending tests were removed with that machinery.)
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import migrate
from wisp.server import services


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    # -- backup --------------------------------------------------------------
    def test_backup_is_valid_sqlite(self):
        blob = services.create_backup(self.cfg)
        self.assertTrue(blob.startswith(b"SQLite format 3"))
        out = Path(self.tmp.name) / "restored.db"
        out.write_bytes(blob)
        conn = sqlite3.connect(out)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("settings", tables)
        self.assertIn("devices", tables)
        self.assertTrue(services.backup_filename().endswith(".db"))


if __name__ == "__main__":
    unittest.main()
