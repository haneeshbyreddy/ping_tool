"""Analytics math, checked with realistic durations and controlled timestamps."""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.core.analytics import _now, compute_digest
from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.core.state_machine import DOWN, UNREACHABLE


def iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


class DigestMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        now = _now()
        with connect(self.cfg) as c:
            # 4 active devices
            for did, name, cust, rev in [
                (1, "Tower A", 100, 300.0), (2, "Sector A", 50, 100.0),
                (3, "Tower B", 80, 200.0), (4, "Tower C", 0, 0.0),
            ]:
                c.execute(
                    "INSERT INTO devices (id,name,ip_address,criticality,region,"
                    "customer_count,base_revenue_impact) VALUES (?,?,?,3,'R',?,?)",
                    (did, name, f"10.0.0.{did}", cust, rev))
            # Tower A: a 2-hour POWER outage, resolved (revenue = 2h * 300 = 600)
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state,"
                      "inferred_cause) VALUES (1,?,?,?,?)",
                      (iso(now - timedelta(hours=3)), iso(now - timedelta(hours=1)),
                       DOWN, "Likely Power Outage"))
            # Tower B: a 1-hour LINK outage, resolved (revenue = 1h * 200 = 200)
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state,"
                      "inferred_cause) VALUES (3,?,?,?,?)",
                      (iso(now - timedelta(hours=2)), iso(now - timedelta(hours=1)),
                       DOWN, "Link/Equipment Fault"))
            # Sector A: UNREACHABLE (must be excluded from DOWN math / revenue)
            c.execute("INSERT INTO outages (device_id,started_at,resolved_at,final_state)"
                      " VALUES (2,?,?,?)",
                      (iso(now - timedelta(hours=3)), iso(now - timedelta(hours=1)),
                       UNREACHABLE))
            c.commit()
        self.m = compute_digest(self.cfg, hours=24)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_exclude_unreachable(self):
        self.assertEqual(self.m["outages"], 2)        # 2 DOWN, UNREACHABLE excluded
        self.assertEqual(self.m["power"], 1)
        self.assertEqual(self.m["equipment"], 1)

    def test_downtime_and_revenue(self):
        self.assertAlmostEqual(self.m["total_down_s"], 3 * 3600, delta=2)  # 2h + 1h
        self.assertAlmostEqual(self.m["revenue"], 600 + 200, delta=1)      # ₹800

    def test_worst_site_is_longest(self):
        self.assertEqual(self.m["worst"][0], "Tower A")  # 2h is the worst

    def test_uptime_over_four_devices(self):
        # 3h down across 4 devices over 24h window
        expected = 100.0 * (1 - (3 * 3600) / (4 * 24 * 3600))
        self.assertAlmostEqual(self.m["uptime_pct"], expected, delta=0.05)

    def test_customers_impacted_excludes_unreachable(self):
        self.assertEqual(self.m["customers"], 100 + 80)  # Tower A + Tower B, not Sector A


if __name__ == "__main__":
    unittest.main(verbosity=2)
