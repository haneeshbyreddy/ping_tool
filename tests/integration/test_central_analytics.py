"""Central-side historical analytics (central/analytics.py) — mirrors the old edge's
tests/integration/test_analytics.py downtime-window math, ported onto CentralStore's
org-scoped `outages` table: window overlap, DOWN-only (UNREACHABLE excluded),
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
from wisp.central import engine as central_engine
from wisp.central import rollup as central_rollup
from wisp.central.store import CentralStore
from wisp.config import Config
from wisp.core.analytics import _now
from wisp.core.state_machine import DOWN, UNREACHABLE
from wisp.ingress.probers import PingResult

ORG = "ispA"


def iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


class DeviceReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.now = _now()

        def dev(name):
            return self.store.create_org_device(ORG, {
                "name": name, "ip_address": "10.0.0.1", "device_type": None,
                "region": "R", "parent_device_id": None})

        self.tower_a = dev("Tower A")
        self.sector_a = dev("Sector A")
        self.tower_b = dev("Tower B")
        self.tower_c = dev("Tower C")   # never had an outage

        with self.store._connect() as conn:
            # Tower A: a 2-hour outage, resolved
            conn.execute(
                "INSERT INTO outages (org_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (ORG, self.tower_a, iso(self.now - timedelta(hours=3)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            # Tower B: a 1-hour outage, resolved
            conn.execute(
                "INSERT INTO outages (org_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (ORG, self.tower_b, iso(self.now - timedelta(hours=2)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            # Sector A: UNREACHABLE (must be excluded from DOWN-only math)
            conn.execute(
                "INSERT INTO outages (org_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                (ORG, self.sector_a, iso(self.now - timedelta(hours=3)),
                 iso(self.now - timedelta(hours=1)), UNREACHABLE))
            conn.commit()

        self.since = iso(self.now - timedelta(hours=24))
        self.until = iso(self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def _by_id(self, report):
        return {r["device_id"]: r for r in report}

    def test_window_overlap_picks_up_all_outages(self):
        rows = self.store.outages_in_window(ORG, self.since, self.until)
        self.assertEqual(len(rows), 3)

    def test_down_only_excludes_unreachable(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, ORG, self.since, self.until))
        self.assertEqual(report[self.sector_a]["downtime_seconds"], 0.0)
        self.assertEqual(report[self.sector_a]["uptime_pct"], 100.0)

    def test_per_device_downtime_and_uptime_pct(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, ORG, self.since, self.until))
        self.assertAlmostEqual(report[self.tower_a]["downtime_seconds"], 2 * 3600, delta=2)
        self.assertAlmostEqual(report[self.tower_b]["downtime_seconds"], 1 * 3600, delta=2)
        # 2h down out of a 24h window -> ~91.67% up
        self.assertAlmostEqual(report[self.tower_a]["uptime_pct"], 100 * (1 - 2 / 24), places=1)

    def test_device_with_no_outages_is_fully_up(self):
        report = self._by_id(central_analytics.device_reliability(
            self.store, ORG, self.since, self.until))
        self.assertEqual(report[self.tower_c]["downtime_seconds"], 0.0)
        self.assertEqual(report[self.tower_c]["uptime_pct"], 100.0)
        self.assertEqual(report[self.tower_c]["outage_count"], 0)

    def test_worst_offender_sorts_first(self):
        report = central_analytics.device_reliability(self.store, ORG, self.since, self.until)
        self.assertEqual(report[0]["device_id"], self.tower_a)   # 2h is the worst

    def test_org_isolation(self):
        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.9", "device_type": None,
            "region": None, "parent_device_id": None})
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO outages (org_id,device_id,started_at,resolved_at,"
                " final_state) VALUES (?,?,?,?,?)",
                ("ispB", other, iso(self.now - timedelta(hours=2)),
                 iso(self.now - timedelta(hours=1)), DOWN))
            conn.commit()
        report = self._by_id(central_analytics.device_reliability(
            self.store, ORG, self.since, self.until))
        self.assertNotIn(other, report)


class DeviceRollupTest(unittest.TestCase):
    """central/rollup.py: hourly latency/loss buckets folded incrementally at report
    time (30-day retention, hourly granularity — CLAUDE.md item 2's second slice)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.dev = self.store.create_org_device(ORG, {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.eng = central_engine.build_engine(self.store, ORG, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bucket_of_floors_to_the_hour(self):
        self.assertEqual(central_rollup.bucket_of("2026-01-01T14:37:52+00:00"),
                         "2026-01-01T14:00:00")

    def test_record_cycle_folds_into_the_hour_bucket(self):
        ts = "2026-01-01T14:05:00+00:00"
        results = {"10.0.0.1": PingResult("10.0.0.1", 10.0, 0.0, 1.0)}
        cycle = central_engine.run_cycle(self.store, ORG, self.eng, results, ts)
        central_rollup.record_cycle(self.store, ORG, self.eng, cycle, results, ts)

        since, until = "2026-01-01T00:00:00", "2026-01-01T23:59:59"
        buckets = self.store.device_rollup_series(ORG, self.dev, since, until)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["bucket"], "2026-01-01T14:00:00")
        self.assertEqual(buckets[0]["samples"], 1)
        self.assertEqual(buckets[0]["avg_latency_ms"], 10.0)
        self.assertEqual(buckets[0]["avg_loss_pct"], 0.0)
        self.assertEqual(buckets[0]["down_pct"], 0.0)

    def test_multiple_samples_average_within_the_same_bucket(self):
        for latency, ts in [(10.0, "2026-01-01T14:05:00+00:00"),
                           (20.0, "2026-01-01T14:35:00+00:00")]:
            results = {"10.0.0.1": PingResult("10.0.0.1", latency, 0.0, 1.0)}
            cycle = central_engine.run_cycle(self.store, ORG, self.eng, results, ts)
            central_rollup.record_cycle(self.store, ORG, self.eng, cycle, results, ts)

        buckets = self.store.device_rollup_series(
            ORG, self.dev, "2026-01-01T00:00:00", "2026-01-01T23:59:59")
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["samples"], 2)
        self.assertEqual(buckets[0]["avg_latency_ms"], 15.0)   # (10+20)/2

    def test_a_lost_sample_has_no_latency_but_still_counts_loss_and_down(self):
        ts = "2026-01-01T14:05:00+00:00"
        results = {"10.0.0.1": PingResult("10.0.0.1", None, 100.0)}
        cycle = central_engine.run_cycle(self.store, ORG, self.eng, results, ts)
        central_rollup.record_cycle(self.store, ORG, self.eng, cycle, results, ts)

        buckets = self.store.device_rollup_series(
            ORG, self.dev, "2026-01-01T00:00:00", "2026-01-01T23:59:59")
        self.assertIsNone(buckets[0]["avg_latency_ms"])   # no latency ever landed
        self.assertEqual(buckets[0]["avg_loss_pct"], 100.0)
        self.assertEqual(buckets[0]["down_pct"], 0.0)   # UP is a single 100%-loss sample, not yet DOWN

    def test_different_hours_land_in_different_buckets(self):
        for ts in ["2026-01-01T14:05:00+00:00", "2026-01-01T15:05:00+00:00"]:
            results = {"10.0.0.1": PingResult("10.0.0.1", 5.0, 0.0)}
            cycle = central_engine.run_cycle(self.store, ORG, self.eng, results, ts)
            central_rollup.record_cycle(self.store, ORG, self.eng, cycle, results, ts)

        buckets = self.store.device_rollup_series(
            ORG, self.dev, "2026-01-01T00:00:00", "2026-01-01T23:59:59")
        self.assertEqual([b["bucket"] for b in buckets],
                         ["2026-01-01T14:00:00", "2026-01-01T15:00:00"])

    def test_prune_removes_only_buckets_older_than_retention(self):
        old_ts = "2020-01-01T00:00:00+00:00"
        recent_ts = "2026-01-01T00:00:00+00:00"
        for ts in [old_ts, recent_ts]:
            results = {"10.0.0.1": PingResult("10.0.0.1", 5.0, 0.0)}
            cycle = central_engine.run_cycle(self.store, ORG, self.eng, results, ts)
            central_rollup.record_cycle(self.store, ORG, self.eng, cycle, results, ts)

        removed = central_rollup.prune_old_rollups(self.store, now="2026-01-02T00:00:00+00:00")
        self.assertEqual(removed, 1)
        buckets = self.store.device_rollup_series(ORG, self.dev, "2000-01-01T00:00:00",
                                                   "2030-01-01T00:00:00")
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["bucket"], "2026-01-01T00:00:00")

    def test_org_isolation(self):
        other_dev = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        eng_b = central_engine.build_engine(self.store, "ispB", self.cfg)
        ts = "2026-01-01T14:05:00+00:00"
        results = {"10.0.0.1": PingResult("10.0.0.1", 5.0, 0.0)}
        cycle = central_engine.run_cycle(self.store, "ispB", eng_b, results, ts)
        central_rollup.record_cycle(self.store, "ispB", eng_b, cycle, results, ts)

        buckets = self.store.device_rollup_series(
            ORG, self.dev, "2026-01-01T00:00:00", "2026-01-01T23:59:59")
        self.assertEqual(buckets, [])   # ispA's device has no rollup of its own yet


if __name__ == "__main__":
    unittest.main()
