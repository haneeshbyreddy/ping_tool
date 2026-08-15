import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import history
from wisp.central.store import CentralStore
from wisp.central.store_history import DAY_S, HIST_CAPS, HOUR_S
from wisp.config import Config

ORG = "ispA"
TS = "2026-08-14T05:10:00+00:00"
TS2 = "2026-08-14T05:15:00+00:00"
TS_NEXT_HOUR = "2026-08-14T06:05:00+00:00"

HIST_TABLES = (
    "hist_olt_sweep", "hist_olt_hour", "hist_olt_day", "hist_pon_hour",
    "hist_pon_day", "hist_port_sweep", "hist_port_hour", "hist_port_day",
    "hist_device_day", "hist_radius_day",
    "hist_onu_hour", "hist_onu_day", "onu_events")


class RxStatsTest(unittest.TestCase):
    def test_empty_is_three_nones_never_zero(self):
        self.assertEqual(history.rx_stats([]), (None, None, None))

    def test_single_value_is_all_three(self):
        self.assertEqual(history.rx_stats([-19.5]), (-19.5, -19.5, -19.5))

    def test_nearest_rank(self):
        med, p10, mn = history.rx_stats(
            [-30.0, -18.0, -20.0, -19.0, -21.0, -22.0, -23.0, -24.0, -25.0,
             -26.0, -17.0])
        self.assertEqual(med, -22.0)
        self.assertEqual(p10, -26.0)
        self.assertEqual(mn, -30.0)


class AccumulatorTest(unittest.TestCase):
    def _acc(self):
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", -18.0, "ok")
        acc.add("EPON0/1", "online", -27.5, "crit")
        acc.add("EPON0/2", "offline", -20.0, "ok")   # stored rx on a dark ONU
        acc.add(None, "online", -19.0, "ok")          # no PON label
        acc.add("EPON0/2", "online", None, "warn")    # walked, no dBm
        return acc

    def test_measured_is_online_with_usable_rx_only(self):
        row = self._acc().olt_row()
        self.assertEqual(row["onus"], 5)
        self.assertEqual(row["online"], 4)
        self.assertEqual(row["warn"], 1)
        self.assertEqual(row["crit"], 1)
        # the offline ONU's stored rx and the no-dBm online ONU both stay out
        self.assertEqual(row["measured"], 3)
        self.assertEqual(row["rx_min"], -27.5)

    def test_an_onu_with_no_pon_label_counts_in_totals_and_no_pon_row(self):
        pons = {p["pon_port"]: p for p in self._acc().pon_rows()}
        self.assertEqual(set(pons), {"EPON0/1", "EPON0/2"})
        self.assertEqual(pons["EPON0/1"]["onus"], 2)
        self.assertEqual(pons["EPON0/1"]["crit"], 1)
        # EPON0/2's only usable rx candidates are offline or NULL -> no median
        self.assertIsNone(pons["EPON0/2"]["rx_med"])


class _StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, "Isp A")
        self.dev = self.store.create_org_device(ORG, {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": None, "parent_device_id": None})

    def tearDown(self):
        self.tmp.cleanup()

    def _optics(self, ts=TS, crit=1):
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", -18.0, "ok")
        for _ in range(crit):
            acc.add("EPON0/1", "online", -28.0, "crit")
        history.record_optics(self.store, self.cfg, ORG, self.dev, ts, acc)


class SweepWriteTest(_StoreTest):
    def test_two_sweeps_in_one_hour_accumulate(self):
        self._optics(TS, crit=1)
        self._optics(TS2, crit=2)
        self.assertEqual(
            len(self.store.olt_history(ORG, self.dev, 0, 2**33, "sweep")), 2)
        hours = self.store.olt_history(ORG, self.dev, 0, 2**33, "hour")
        self.assertEqual(len(hours), 1)
        h = hours[0]
        self.assertEqual(h["samples"], 2)
        self.assertEqual(h["crit_max"], 2)
        self.assertEqual(h["rx_med_n"], 2)
        self.assertEqual(h["rx_min"], -28.0)
        pon = self.store.pon_history(ORG, self.dev, "EPON0/1", 0, 2**33, "hour")
        self.assertEqual(pon[0]["samples"], 2)

    def test_a_new_hour_opens_a_new_bucket(self):
        self._optics(TS)
        self._optics(TS_NEXT_HOUR)
        hours = self.store.olt_history(ORG, self.dev, 0, 2**33, "hour")
        self.assertEqual([h["samples"] for h in hours], [1, 1])
        self.assertEqual(hours[1]["bucket"] - hours[0]["bucket"], HOUR_S)

    def test_an_unmeasured_sweep_never_erases_the_hours_extreme(self):
        self._optics(TS, crit=1)
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", None, "ok")   # roster walks, no dBm
        history.record_optics(self.store, self.cfg, ORG, self.dev, TS2, acc)
        h = self.store.olt_history(ORG, self.dev, 0, 2**33, "hour")[0]
        self.assertEqual(h["rx_min"], -28.0)
        self.assertEqual(h["rx_med_n"], 1)
        self.assertEqual(h["measured_min"], 0)

    def test_hist_enabled_off_writes_nothing(self):
        cfg = Config(central_db=self.cfg.central_db, hist_enabled=False)
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", -18.0, "ok")
        history.record_optics(self.store, cfg, ORG, self.dev, TS, acc)
        history.record_ports(self.store, cfg, ORG, self.dev, TS,
                             [(1, 1e6, 1e6, True)])
        self.assertEqual(self.store.olt_history(ORG, self.dev, 0, 2**33, "sweep"), [])
        self.assertEqual(self.store.port_history(ORG, self.dev, 1, 0, 2**33, "sweep"), [])

    def test_a_counter_reset_is_a_null_rate_never_a_zero(self):
        history.record_ports(self.store, self.cfg, ORG, self.dev, TS,
                             [(5, None, None, True)])
        history.record_ports(self.store, self.cfg, ORG, self.dev, TS2,
                             [(5, 4e6, 2e6, True)])
        raws = self.store.port_history(ORG, self.dev, 5, 0, 2**33, "sweep")
        self.assertIsNone(raws[0]["in_bps"])
        h = self.store.port_history(ORG, self.dev, 5, 0, 2**33, "hour")[0]
        self.assertEqual(h["samples"], 2)
        self.assertEqual(h["rate_n"], 1)       # the reset sweep never enters the mean
        self.assertEqual(h["in_sum"], 4e6)
        self.assertEqual(h["up_samples"], 2)


class FoldTest(_StoreTest):
    DAY = history.day_floor(history.epoch_s(TS))

    def test_day_fold_is_idempotent_and_carries_busy_hour(self):
        history.record_ports(self.store, self.cfg, ORG, self.dev,
                             "2026-08-14T05:10:00+00:00", [(5, 1e6, 8e6, True)])
        history.record_ports(self.store, self.cfg, ORG, self.dev,
                             "2026-08-14T15:10:00+00:00", [(5, 6e6, 2e6, True)])
        self._optics(TS)
        self.store.fold_history_day(self.DAY)
        self.store.fold_history_day(self.DAY)   # re-run must converge
        d = self.store.port_history(ORG, self.dev, 5, 0, 2**33, "day")
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["samples"], 2)
        self.assertEqual(d[0]["busy_in_hour"], 15)
        self.assertEqual(d[0]["busy_in_bps"], 6e6)
        self.assertEqual(d[0]["busy_out_hour"], 5)
        self.assertEqual(d[0]["busy_out_bps"], 8e6)
        od = self.store.olt_history(ORG, self.dev, 0, 2**33, "day")
        self.assertEqual(len(od), 1)
        self.assertEqual(od[0]["samples"], 1)
        pd = self.store.pon_history(ORG, self.dev, "EPON0/1", 0, 2**33, "day")
        self.assertEqual(len(pd), 1)

    def test_device_day_folds_from_rollups_before_their_prune(self):
        self.store.fold_device_rollups([
            (ORG, self.dev, "2026-08-14T05:00:00", 12.5, 0.0, 0),
            (ORG, self.dev, "2026-08-14T06:00:00", None, 100.0, 1)])
        self.store.fold_history_day(self.DAY)
        rows = self.store.device_day_history(ORG, self.dev, 0, 2**33)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["samples"], 2)
        self.assertEqual(rows[0]["down_samples"], 1)
        self.assertEqual(rows[0]["latency_n"], 1)

    def test_maintenance_advances_covered_through_even_over_empty_days(self):
        # A fresh store stamps covered-through at REAL yesterday; once the wall
        # clock passes TS's day that vacuously covers it, so pin the stamp
        # just before the sampled day to keep the test on TS's own clock.
        self.store.set_hist_folded_through(self.DAY - DAY_S)
        self._optics(TS)
        now_s = self.DAY + 3 * DAY_S + 100   # three days later
        history.run_maintenance(self.store, self.cfg, now_s)
        self.assertEqual(self.store.hist_folded_through(),
                         self.DAY + 2 * DAY_S)
        # the sampled day folded; the two empty days wrote nothing
        self.assertEqual(
            len(self.store.olt_history(ORG, self.dev, 0, 2**33, "day")), 1)

    def test_maintenance_prunes_the_raw_tier_by_age(self):
        self._optics(TS)
        now_s = history.epoch_s(TS) + (self.cfg.hist_raw_hours + 1) * HOUR_S
        history.run_maintenance(self.store, self.cfg, now_s)
        self.assertEqual(self.store.olt_history(ORG, self.dev, 0, 2**33, "sweep"), [])
        # the hour tier survives its longer retention
        self.assertEqual(
            len(self.store.olt_history(ORG, self.dev, 0, 2**33, "hour")), 1)


class CapTest(_StoreTest):
    def test_the_cap_bounds_a_wild_clock(self):
        base = history.epoch_s(TS)
        with self.store._connect() as conn:
            for i in range(40):
                conn.execute(
                    "INSERT INTO hist_radius_day (org_id, day, customers,"
                    " active, expired, expiring7, linked)"
                    " VALUES (?,?,0,0,0,0,0)", (ORG, base + i * DAY_S))
            conn.commit()
        removed = self.store.prune_history({"hist_radius_day": 0},
                                           caps={"hist_radius_day": 25})
        self.assertEqual(removed["hist_radius_day"], 15)
        rows = self.store.radius_day_history(ORG, 0, 2**40)
        self.assertEqual(len(rows), 25)
        # the newest survive
        self.assertEqual(rows[-1]["day"], base + 39 * DAY_S)


class SchemaContractTest(_StoreTest):
    def test_every_hist_table_carries_org_id_for_the_delete_sweep(self):
        with self.store._connect() as conn:
            for table in HIST_TABLES:
                cols = [r["name"] for r in
                        conn.execute(f"PRAGMA table_info({table})")]
                self.assertIn("org_id", cols, table)

    def test_every_hist_table_has_a_cap(self):
        self.assertEqual(set(HIST_CAPS), set(HIST_TABLES))

    def test_history_since_is_stamped_once(self):
        since = self.store.history_since()
        self.assertTrue(since)
        CentralStore(self.cfg.central_db)   # re-open = re-migrate
        self.assertEqual(self.store.history_since(), since)
