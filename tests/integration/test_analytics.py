"""Outage-window + downtime math, checked with realistic durations and controlled
timestamps. These shared helpers back both the analytics CLI and the dashboard
(server/services.py), so they carry the load-bearing uptime arithmetic."""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.core.analytics import (
    _downtime_by_device,
    _now,
    _offender_counts,
    _outages_in_window,
)
from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.core.state_machine import DOWN, UNREACHABLE


def iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


class WindowMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        self.now = _now()
        with connect(self.cfg) as c:
            for did, name in [
                (1, "Tower A"), (2, "Sector A"),
                (3, "Tower B"), (4, "Tower C"),
            ]:
                c.execute(
                    "INSERT INTO devices (id,name,ip_address,region)"
                    " VALUES (?,?,?,'R')",
                    (did, name, f"10.0.0.{did}"))
            # Tower A: a 2-hour outage, resolved
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state)"
                      " VALUES (1,?,?,?)",
                      (iso(self.now - timedelta(hours=3)), iso(self.now - timedelta(hours=1)),
                       DOWN))
            # Tower B: a 1-hour outage, resolved
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state)"
                      " VALUES (3,?,?,?)",
                      (iso(self.now - timedelta(hours=2)), iso(self.now - timedelta(hours=1)),
                       DOWN))
            # Sector A: UNREACHABLE (must be excluded from DOWN-only math)
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state)"
                      " VALUES (2,?,?,?)",
                      (iso(self.now - timedelta(hours=3)), iso(self.now - timedelta(hours=1)),
                       UNREACHABLE))
            c.commit()
        self.win_start = self.now - timedelta(hours=24)
        with connect(self.cfg) as c:
            self.outages = _outages_in_window(c, self.win_start, self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_overlap_picks_up_all_three(self):
        # all three outages overlap the 24h window
        self.assertEqual(len(self.outages), 3)

    def test_down_only_excludes_unreachable(self):
        down = _downtime_by_device(self.outages, self.win_start, self.now, only_down=True)
        self.assertNotIn(2, down)                       # Sector A was UNREACHABLE
        self.assertAlmostEqual(sum(down.values()), 3 * 3600, delta=2)  # 2h + 1h

    def test_per_device_downtime(self):
        down = _downtime_by_device(self.outages, self.win_start, self.now, only_down=True)
        self.assertAlmostEqual(down[1], 2 * 3600, delta=2)  # Tower A worst at 2h

    def test_offender_ranking(self):
        down_outages = [o for o in self.outages if o["final_state"] == DOWN]
        ranked = _offender_counts(down_outages)
        self.assertEqual({n for n, _ in ranked}, {"Tower A", "Tower B"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
