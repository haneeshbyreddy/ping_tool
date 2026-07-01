"""Central-side historical analytics (central/analytics.py) — mirrors the old edge's
tests/integration/test_analytics.py downtime-window math, ported onto CentralStore's
tenant-scoped `outages` table: window overlap, DOWN-only (UNREACHABLE excluded),
per-device downtime/uptime %, and a device with zero outages still reporting 100% up.
"""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import analytics as central_analytics
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.analytics import _now
from wisp.core.state_machine import DOWN, UNREACHABLE

TENANT = "ispA"


def iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


class DeviceReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.now = _now()

        def dev(name):
            return self.store.create_org_device(TENANT, {
                "name": name, "ip_address": "10.0.0.1", "device_type": None,
                "region": "R", "parent_device_id": None})

        self.tower_a = dev("Tower A")
        self.sector_a = dev("Sector A")
        self.tower_b = dev("Tower B")
        self.tower_c = dev("Tower C")   # never had an outage

        with self.store._connect() as conn:
            # Tower A: a 2-hour outage, resolved
            conn.execute(
                "INSERT INTO outages (tenant_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (TENANT, self.tower_a, iso(self.now - timedelta(hours=3)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            # Tower B: a 1-hour outage, resolved
            conn.execute(
                "INSERT INTO outages (tenant_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (TENANT, self.tower_b, iso(self.now - timedelta(hours=2)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            # Sector A: UNREACHABLE (must be excluded from DOWN-only math)
            conn.execute(
                "INSERT INTO outages (tenant_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (TENANT, self.sector_a, iso(self.now - timedelta(hours=3)),
                 iso(self.now - timedelta(hours=1)), UNREACHABLE))
            conn.commit()

        self.since = iso(self.now - timedelta(hours=24))
        self.until = iso(self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def _by_id(self, report):
        return {r["device_id"]: r for r in report}

    def test_window_overlap_picks_up_all_outages(self):
        rows = self.store.outages_in_window(TENANT, self.since, self.until)
        self.assertEqual(len(rows), 3)

    def test_down_only_excludes_unreachable(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, TENANT, self.since, self.until))
        self.assertEqual(report[self.sector_a]["downtime_seconds"], 0.0)
        self.assertEqual(report[self.sector_a]["uptime_pct"], 100.0)

    def test_per_device_downtime_and_uptime_pct(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, TENANT, self.since, self.until))
        self.assertAlmostEqual(report[self.tower_a]["downtime_seconds"], 2 * 3600, delta=2)
        self.assertAlmostEqual(report[self.tower_b]["downtime_seconds"], 1 * 3600, delta=2)
        # 2h down out of a 24h window -> ~91.67% up
        self.assertAlmostEqual(report[self.tower_a]["uptime_pct"], 100 * (1 - 2 / 24), places=1)

    def test_device_with_no_outages_is_fully_up(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, TENANT, self.since, self.until))
        self.assertEqual(report[self.tower_c]["downtime_seconds"], 0.0)
        self.assertEqual(report[self.tower_c]["uptime_pct"], 100.0)
        self.assertEqual(report[self.tower_c]["outage_count"], 0)

    def test_worst_offender_sorts_first(self):
        report = central_analytics.device_reliability(self.store, TENANT, self.since, self.until)
        self.assertEqual(report[0]["device_id"], self.tower_a)   # 2h is the worst

    def test_tenant_isolation(self):
        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.9", "device_type": None,
            "region": None, "parent_device_id": None})
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO outages (tenant_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                ("ispB", other, iso(self.now - timedelta(hours=2)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            conn.commit()
        report = self._by_id(central_analytics.device_reliability(
            self.store, TENANT, self.since, self.until))
        self.assertNotIn(other, report)


if __name__ == "__main__":
    unittest.main()
