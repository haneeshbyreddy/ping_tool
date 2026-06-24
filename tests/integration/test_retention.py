"""Retention sweep tests (daemon prune_old_polls).

A 24/7 deployment must reach a steady-state DB size, so the daemon prunes raw
poll samples older than cfg.poll_retention_days once a day. These check the cut
is correct (old gone, recent kept), that the permanent outages record is never
touched, and that retention<=0 disables the sweep.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "apps", "daemon"))

import main as daemon  # apps/daemon/main.py
from wisp.config import Config
from wisp.database.client import connect, migrate


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db", poll_retention_days=90)
        migrate(self.cfg)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address) VALUES (1,'n','d1')")
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _poll(self, when):
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO poll_results (device_id,timestamp,latency_ms,packet_loss,state)"
                " VALUES (1,?,10.0,0.0,'UP')", (_iso(when),))
            c.commit()

    def _count(self):
        with connect(self.cfg) as c:
            return c.execute("SELECT COUNT(*) FROM poll_results").fetchone()[0]

    def test_prune_drops_old_keeps_recent(self):
        self._poll(self.now - timedelta(days=120))   # older than retention
        self._poll(self.now - timedelta(days=91))    # just over the edge
        self._poll(self.now - timedelta(days=30))    # within retention
        self._poll(self.now)                          # fresh
        # an outage row from the pruned era must survive (permanent record)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state)"
                      " VALUES (1,?,?, 'DOWN')",
                      (_iso(self.now - timedelta(days=120)),
                       _iso(self.now - timedelta(days=120) + timedelta(hours=1))))
            c.commit()

        removed = daemon.prune_old_polls(self.cfg, now=self.now)
        self.assertEqual(removed, 2)
        self.assertEqual(self._count(), 2)            # the 30d + fresh samples remain
        with connect(self.cfg) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outages").fetchone()[0], 1)

    def test_retention_zero_disables(self):
        self._poll(self.now - timedelta(days=999))
        cfg = Config(db_path=self.cfg.db_path, poll_retention_days=0)
        self.assertEqual(daemon.prune_old_polls(cfg, now=self.now), 0)
        self.assertEqual(self._count(), 1)

    def test_default_retention_is_short(self):
        # Raw polls are scratch; poll_rollups + outages are the durable record. The
        # default window only needs to clear the hourly rollup cadence with margin,
        # so it stays short (a long default silently hoards 10s of GB at fleet size).
        self.assertEqual(Config().poll_retention_days, 7)


if __name__ == "__main__":
    unittest.main()
