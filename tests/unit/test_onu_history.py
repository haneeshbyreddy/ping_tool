import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import history
from wisp.central.optics import CentralOpticsMonitor
from wisp.central.store import CentralStore
from wisp.central.store_history import DAY_S, HIST_CAPS, HOUR_S, _HIST_TIME_COL
from wisp.config import Config

ORG = "ispA"
TS = "2026-08-14T05:10:00+00:00"
TS2 = "2026-08-14T05:15:00+00:00"
TS_NEXT_HOUR = "2026-08-14T06:05:00+00:00"

ONU_TABLES = ("hist_onu_hour", "hist_onu_day", "onu_events")
# Derived from the FIXED TS above, never from a wall clock — the fixture rule
# is about "now", and a day floor of a constant is a constant.
DAY = history.day_floor(history.epoch_s(TS))


class TransitionRuleTest(unittest.TestCase):
    # The rule lives in one pure place so it can be read without a store: a
    # state that did not move writes NOTHING.

    def _acc(self):
        return history.OnuAccumulator()

    def test_a_first_seen_slot_carries_a_null_old_state(self):
        acc = self._acc()
        acc.add("1.5", None, "online", -19.0)
        self.assertEqual(acc.events, [("1.5", None, "online")])

    def test_a_real_transition_carries_both_states_raw(self):
        acc = self._acc()
        acc.add("1.5", "online", "dying_gasp", None)
        # the vendor's own word survives — dying_gasp vs los is what the PON
        # verdict reads, so nothing here normalises a state
        self.assertEqual(acc.events, [("1.5", "online", "dying_gasp")])

    def test_the_same_state_twice_writes_no_event_but_still_samples(self):
        acc = self._acc()
        acc.add("1.5", "online", "online", -19.0)
        self.assertEqual(acc.events, [])
        self.assertEqual(acc.rows, [("1.5", True, -19.0)])

    def test_a_dark_onus_rx_is_never_sampled_but_its_row_still_exists(self):
        # its stored dBm is whatever the last good walk saw, i.e. not now —
        # while the C-Data fleet's only signal IS the state row
        acc = self._acc()
        acc.add("2.1", "online", "offline", -24.0)
        self.assertEqual(acc.rows, [("2.1", False, None)])

    def test_every_walked_onu_is_sampled_including_the_rx_null_ones(self):
        acc = self._acc()
        acc.add("1.1", "online", "online", None)
        acc.add("1.2", "online", "online", -21.0)
        self.assertEqual([r[0] for r in acc.rows], ["1.1", "1.2"])
        self.assertIsNone(acc.rows[0][2])


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

    def _walk(self, rows, ts=TS):
        # rows: (onu_key, state, rx)
        mon = CentralOpticsMonitor(self.store, ORG, None, self.cfg)
        mon.sync_device(self.dev, [
            {"onu_key": k, "pon_port": "EPON0/1", "onu_id": 1, "name": k,
             "serial": k, "state": state, "rx_dbm": rx}
            for k, state, rx in rows], ts)

    def _hours(self, onu_key):
        return self.store.onu_history(ORG, self.dev, onu_key, 0, 2**33, "hour")

    def _events(self, onu_key):
        return self.store.onu_events_window(ORG, self.dev, onu_key, 0, 2**33)


class WalkWriteTest(_StoreTest):
    def test_two_walks_in_one_hour_accumulate_on_the_slot(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "online", -22.0)], TS2)
        rows = self._hours("1.1")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["samples"], r["online"], r["rx_n"]), (2, 2, 1 + 1))
        self.assertEqual(r["rx_sum"], -40.0)
        self.assertEqual((r["rx_min"], r["rx_max"]), (-22.0, -18.0))

    def test_a_new_hour_opens_a_new_bucket(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "online", -18.0)], TS_NEXT_HOUR)
        rows = self._hours("1.1")
        self.assertEqual([r["samples"] for r in rows], [1, 1])
        self.assertEqual(rows[1]["bucket"] - rows[0]["bucket"], HOUR_S)

    def test_an_unmeasured_walk_never_erases_the_hours_extreme(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "online", None)], TS2)
        r = self._hours("1.1")[0]
        self.assertEqual((r["samples"], r["rx_n"]), (2, 1))
        self.assertEqual((r["rx_min"], r["rx_max"]), (-18.0, -18.0))

    def test_the_identity_is_the_SLOT_not_the_serial(self):
        # a re-registered ONU is reported on both its old and new slot, so a
        # serial key would collapse two live subscribers into one row
        self._walk([("1.1", "online", -18.0), ("1.2", "online", -19.0)], TS)
        with self.store._connect() as conn:
            keys = [r["onu_key"] for r in conn.execute(
                "SELECT onu_key FROM hist_onu_hour ORDER BY onu_key")]
        self.assertEqual(keys, ["1.1", "1.2"])

    def test_the_first_walk_of_a_slot_is_a_first_seen_event(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self.assertEqual([(e["old_state"], e["new_state"]) for e in
                          self._events("1.1")], [(None, "online")])

    def test_a_state_that_holds_writes_no_second_event(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "online", -18.0)], TS2)
        self.assertEqual(len(self._events("1.1")), 1)

    def test_a_transition_is_stamped_at_the_walk_that_saw_it(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "offline", None)], TS2)
        events = self._events("1.1")
        self.assertEqual([(e["old_state"], e["new_state"]) for e in events],
                         [(None, "online"), ("online", "offline")])
        self.assertEqual(events[1]["ts"], history.epoch_s(TS2))

    def test_a_replayed_walk_writes_one_event_not_two(self):
        # same slot, same second: the PK makes the ledger idempotent, so a
        # re-delivered report can never double a transition
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "offline", None)], TS2)
        self._walk([("1.1", "offline", None)], TS2)
        self.assertEqual(len(self._events("1.1")), 2)

    def test_a_device_whose_walk_never_arrives_writes_nothing(self):
        # THE frozen rule at the storage layer: sync_device is the only writer
        # and it only runs when a walk actually landed, so a down or
        # walk-stale OLT contributes no rows at all. The gap IS the record;
        # nothing here is ever synthesised from staleness.
        other = self.store.create_org_device(ORG, {
            "name": "OLT-2", "ip_address": "10.0.0.3", "device_type": "olt",
            "region": None, "parent_device_id": None})
        self._walk([("1.1", "online", -18.0)], TS)
        self.assertEqual(
            self.store.onu_history(ORG, other, "1.1", 0, 2**33, "hour"), [])
        self.assertEqual(
            self.store.onu_events_window(ORG, other, "1.1", 0, 2**33), [])

    def test_hist_enabled_off_writes_nothing(self):
        cfg = Config(central_db=self.cfg.central_db, hist_enabled=False)
        acc = history.OnuAccumulator()
        acc.add("1.1", None, "online", -18.0)
        history.record_onus(self.store, cfg, ORG, self.dev, TS, acc)
        self.assertEqual(self._hours("1.1"), [])
        self.assertEqual(self._events("1.1"), [])

    def test_a_write_failure_never_reaches_the_report_cycle(self):
        class Boom:
            def record_onu_sweep(self, *a):
                raise RuntimeError("disk")

        acc = history.OnuAccumulator()
        acc.add("1.1", None, "online", -18.0)
        history.record_onus(Boom(), self.cfg, ORG, self.dev, TS, acc)

    def test_the_pon_port_comes_from_the_live_roster_row(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self.assertEqual(self.store.onu_pon_port(ORG, self.dev, "1.1"),
                         "EPON0/1")
        self.assertIsNone(self.store.onu_pon_port(ORG, self.dev, "9.9"))


class FoldTest(_StoreTest):

    def test_the_day_fold_sums_the_hours_and_is_idempotent(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "offline", None)], TS_NEXT_HOUR)
        self.store.fold_history_day(DAY)
        self.store.fold_history_day(DAY)   # a re-run must converge
        days = self.store.onu_history(ORG, self.dev, "1.1", 0, 2**33, "day")
        self.assertEqual(len(days), 1)
        d = days[0]
        self.assertEqual((d["samples"], d["online"], d["rx_n"]), (2, 1, 1))
        self.assertEqual(d["rx_sum"], -18.0)
        self.assertEqual((d["rx_min"], d["rx_max"]), (-18.0, -18.0))
        self.assertEqual(d["day"], DAY)

    def test_an_unmeasured_hour_never_erases_the_days_extreme(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self._walk([("1.1", "online", None)], TS_NEXT_HOUR)
        self.store.fold_history_day(DAY)
        d = self.store.onu_history(ORG, self.dev, "1.1", 0, 2**33, "day")[0]
        self.assertEqual((d["rx_min"], d["rx_max"]), (-18.0, -18.0))
        self.assertEqual(d["rx_n"], 1)

    def test_maintenance_folds_and_then_prunes_the_hour_tier(self):
        self._walk([("1.1", "online", -18.0)], TS)
        # anchor the covered-through stamp on the fixture's own day, or the
        # catch-up window is measured against the wall clock and this test
        # quietly stops folding anything the week after it was written
        self.store.set_hist_folded_through(DAY - DAY_S)
        now_s = (history.epoch_s(TS)
                 + (self.cfg.hist_onu_hour_days + 1) * DAY_S)
        history.run_maintenance(self.store, self.cfg, now_s)
        self.assertEqual(self._hours("1.1"), [])
        # the day tier it folded into survives its own longer horizon
        self.assertEqual(
            len(self.store.onu_history(ORG, self.dev, "1.1", 0, 2**33, "day")),
            1)
        self.assertEqual(len(self._events("1.1")), 1)

    def test_the_day_tier_and_the_events_age_out_on_their_own_horizon(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self.store.fold_history_day(DAY)
        now_s = history.epoch_s(TS) + (self.cfg.hist_onu_day_days + 1) * DAY_S
        history.run_maintenance(self.store, self.cfg, now_s)
        self.assertEqual(
            self.store.onu_history(ORG, self.dev, "1.1", 0, 2**33, "day"), [])
        self.assertEqual(self._events("1.1"), [])


class RetentionContractTest(_StoreTest):
    def test_every_per_onu_table_is_wired_into_the_prune(self):
        for table in ONU_TABLES:
            self.assertIn(table, _HIST_TIME_COL, table)
            self.assertIn(table, HIST_CAPS, table)

    def test_the_prune_index_exists_on_every_per_onu_table(self):
        history.run_maintenance(self.store, self.cfg)
        with self.store._connect() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        for table in ONU_TABLES:
            self.assertIn(f"idx_{table}_prune", names, table)

    def test_the_cap_bounds_a_wild_clock(self):
        base = history.epoch_s(TS)
        with self.store._connect() as conn:
            for i in range(40):
                conn.execute(
                    "INSERT INTO onu_events (org_id, device_id, onu_key,"
                    " old_state, new_state, ts) VALUES (?,?,?,NULL,'online',?)",
                    (ORG, self.dev, "1.1", base + i * DAY_S))
            conn.commit()
        removed = self.store.prune_history({"onu_events": 0},
                                           caps={"onu_events": 25})
        self.assertEqual(removed["onu_events"], 15)
        rows = self._events("1.1")
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[-1]["ts"], base + 39 * DAY_S)

    def test_deleting_the_device_takes_its_per_onu_history(self):
        # these three cascade rather than riding delete_org_device's sweep
        # list — a list one edit away from a forgotten cascade
        self._walk([("1.1", "online", -18.0)], TS)
        self.store.fold_history_day(DAY)
        self.assertTrue(self._hours("1.1"))
        self.assertTrue(self.store.delete_org_device(ORG, self.dev)["ok"])
        with self.store._connect() as conn:
            for table in ONU_TABLES:
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0, table)

    def test_deleting_the_org_sweeps_them_by_org_id(self):
        self._walk([("1.1", "online", -18.0)], TS)
        self.store.fold_history_day(DAY)
        removed = self.store.delete_org(ORG)
        for table in ONU_TABLES:
            self.assertIn(table, removed, table)


if __name__ == "__main__":
    unittest.main()
