import inspect
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_TESTS_DIR)
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import history
from wisp.central.api import capacity
from wisp.central.store import CentralStore
from wisp.central.store_capacity import CapacityStoreMixin
from wisp.central.store_history import DAY_S, HOUR_S

ORG = "ispA"
OTHER = "ispB"


# CapacityStoreMixin is composed into CentralStore's bases; re-composing it
# here would be an MRO conflict.
_Store = CentralStore
assert issubclass(_Store, CapacityStoreMixin)


class DirectionTest(unittest.TestCase):
    # The utilisation numerator must pick the same rate ports.py:_bw_above
    # compares against the ceiling, or a meter and the alarm off one ceiling
    # would describe different traffic.

    def test_named_directions_pick_their_own_rate(self):
        self.assertEqual(capacity.direction_bps(10.0, 3.0, "in"), 10.0)
        self.assertEqual(capacity.direction_bps(10.0, 3.0, "out"), 3.0)
        self.assertEqual(capacity.direction_bps(10.0, 3.0, "total"), 13.0)

    def test_either_is_the_default_and_takes_the_louder_half(self):
        self.assertEqual(capacity.direction_bps(10.0, 3.0, "either"), 10.0)
        self.assertEqual(capacity.direction_bps(10.0, 3.0, None), 10.0)
        self.assertEqual(capacity.direction_bps(2.0, 30.0, None), 30.0)

    def test_a_missing_half_never_becomes_a_zero(self):
        self.assertIsNone(capacity.direction_bps(None, 3.0, "in"))
        self.assertIsNone(capacity.direction_bps(10.0, None, "total"))
        self.assertIsNone(capacity.direction_bps(None, None, "either"))
        # 'either' can still answer off the half that measured
        self.assertEqual(capacity.direction_bps(None, 3.0, "either"), 3.0)


class FoldTest(unittest.TestCase):
    @staticmethod
    def _row(hod, rate_n, in_sum, out_sum=0.0, days=1, samples=None):
        return {"hod": hod, "rate_n": rate_n, "in_sum": in_sum,
                "out_sum": out_sum, "in_max": None, "out_max": None,
                "days": days, "samples": samples if samples is not None else rate_n}

    def test_an_hour_that_computed_no_rate_is_absent_not_zero(self):
        cells = capacity.fold_cells([self._row(3, 0, 0.0, samples=12),
                                     self._row(4, 2, 20.0)])
        self.assertNotIn(3, cells)
        self.assertEqual(cells[4]["in_bps"], 10.0)

    def test_the_mean_is_taken_once_over_the_raw_sums(self):
        # 11 samples at 100 and 1 at 1000 must read 175, not the 550 a
        # mean-of-hourly-means would report.
        cells = capacity.fold_cells([self._row(20, 12, 11 * 100.0 + 1000.0)])
        self.assertEqual(cells[20]["in_bps"], 175.0)

    def test_busiest_names_the_argmax_hour_and_ties_break_early(self):
        cells = capacity.fold_cells([self._row(2, 1, 5.0), self._row(20, 1, 9.0),
                                     self._row(21, 1, 9.0)])
        v, h = capacity.busiest(cells, lambda c: c["in_bps"])
        self.assertEqual((v, h), (9.0, 20))

    def test_nothing_measured_is_a_none_hour_never_hour_zero(self):
        v, h = capacity.busiest({}, lambda c: c["in_bps"])
        self.assertIsNone(v)
        self.assertIsNone(h)


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _Store(Path(self.tmp.name) / "central.db")
        # Stamped at CALL time: discovery imports every test file up front, so
        # an import-time "now" is already stale when the file runs.
        self.day = (1_785_000_000 // DAY_S) * DAY_S
        self.dev = self.store.create_org_device(ORG, {
            "name": "HLY-SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": "north", "parent_device_id": None})

    def tearDown(self):
        self.tmp.cleanup()

    def _sweep(self, day_offset, hour, minute, in_bps, out_bps=1.0e6,
               org=ORG, device_id=None, if_index=1, up=True):
        ts = self.day + day_offset * DAY_S + hour * HOUR_S + minute * 60
        self.store.record_port_sweeps(
            org, device_id if device_id is not None else self.dev, ts,
            [(if_index, in_bps, out_bps, up)])

    def _win(self):
        return self.day - DAY_S, self.day + 3 * DAY_S


class StoreReadTest(_StoreBase):
    def test_days_are_counted_distinct_never_summed_per_hour(self):
        for d in (0, 0, 1):
            self._sweep(d, 20, 5, 100e6)
            self._sweep(d, 21, 5, 50e6)
        since, until = self._win()
        totals = self.store.org_port_totals(ORG, since, until)
        self.assertEqual(len(totals), 1)
        # four distinct (day, hour) buckets across TWO days
        self.assertEqual(totals[0]["hours"], 4)
        self.assertEqual(totals[0]["days"], 2)
        self.assertEqual(totals[0]["samples"], 6)
        self.assertEqual(totals[0]["peak_in_bps"], 100e6)

    def test_the_hour_of_day_fold_collapses_the_same_clock_hour(self):
        self._sweep(0, 20, 5, 100e6)
        self._sweep(1, 20, 5, 300e6)
        self._sweep(1, 3, 5, 10e6)
        since, until = self._win()
        rows = self.store.org_port_hour_profile(ORG, since, until)
        by_hod = {r["hod"]: r for r in rows}
        self.assertEqual(sorted(by_hod), [3, 20])
        self.assertEqual(by_hod[20]["days"], 2)
        self.assertEqual(by_hod[20]["rate_n"], 2)
        self.assertEqual(by_hod[20]["in_sum"], 400e6)
        cells = capacity.fold_cells(rows)
        self.assertEqual(cells[20]["in_bps"], 200e6)
        busy, hour = capacity.busiest(cells, lambda c: c["in_bps"])
        self.assertEqual((busy, hour), (200e6, 20))

    def test_a_counter_reset_leaves_a_walked_hour_with_no_rate(self):
        # throughput_bps returns None on a negative delta: the row exists, the
        # rate does not. It must read as a gap, not as an idle hour.
        self._sweep(0, 8, 5, None, out_bps=None)
        since, until = self._win()
        rows = self.store.org_port_hour_profile(ORG, since, until)
        self.assertEqual(rows[0]["samples"], 1)
        self.assertEqual(rows[0]["rate_n"], 0)
        self.assertEqual(capacity.fold_cells(rows), {})
        self.assertEqual(self.store.org_port_totals(ORG, since, until)[0]["days"], 1)

    def test_a_young_record_reports_the_days_it_has_and_no_more(self):
        self._sweep(0, 20, 5, 100e6)
        since, until = self._win()
        self.assertEqual(
            self.store.org_port_totals(ORG, since, until)[0]["days"], 1)

    def test_the_window_excludes_buckets_outside_it(self):
        self._sweep(-5, 20, 5, 100e6)
        self._sweep(0, 20, 5, 100e6)
        since, until = self._win()
        self.assertEqual(
            self.store.org_port_totals(ORG, since, until)[0]["hours"], 1)

    def test_another_orgs_ports_never_appear(self):
        far = self.store.create_org_device(OTHER, {
            "name": "FAR-SW", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": "s", "parent_device_id": None})
        self._sweep(0, 20, 5, 100e6, org=OTHER, device_id=far)
        self._sweep(0, 20, 5, 42e6)
        since, until = self._win()
        mine = self.store.org_port_totals(ORG, since, until)
        theirs = self.store.org_port_totals(OTHER, since, until)
        self.assertEqual([r["device_id"] for r in mine], [self.dev])
        self.assertEqual([r["device_id"] for r in theirs], [far])
        self.assertEqual(
            [r["device_id"] for r in self.store.org_port_hour_profile(
                ORG, since, until)], [self.dev])
        self.assertEqual([m["device_id"] for m in self.store.org_port_meta(ORG)],
                         [])   # no walked switch_ports row yet on either side


class PortMetaTest(_StoreBase):
    def _port(self, if_index):
        self.store.upsert_switch_port(
            ORG, self.dev, if_index, f"GE0/{if_index}", None, "up", "up", None,
            0, False, None, "2026-08-14T05:00:00")

    def test_the_operator_columns_ride_along_for_the_eligibility_rule(self):
        self._port(1)
        with self.store._connect() as conn:
            conn.execute("UPDATE switch_ports SET monitored=1, bw_max_mbps=1000,"
                         " bw_direction='in' WHERE org_id=? AND if_index=1", (ORG,))
            conn.commit()
        rows = {m["if_index"]: m for m in self.store.org_port_meta(ORG)}
        self.assertEqual(rows[1]["bw_max_mbps"], 1000)
        self.assertEqual(rows[1]["bw_direction"], "in")
        self.assertEqual(rows[1]["device_name"], "HLY-SW")
        self.assertTrue(history.port_eligible(rows[1]))

    def test_a_bare_walked_port_is_not_eligible_and_the_read_says_so(self):
        self._port(7)
        rows = {m["if_index"]: m for m in self.store.org_port_meta(ORG)}
        self.assertIn(7, rows)                       # the read is unfiltered…
        self.assertFalse(history.port_eligible(rows[7]))   # …the rule decides

    def test_an_inactive_devices_ports_drop_out(self):
        self._port(1)
        with self.store._connect() as conn:
            conn.execute("UPDATE org_devices SET is_active=0 WHERE id=?",
                         (self.dev,))
            conn.commit()
        self.assertEqual(self.store.org_port_meta(ORG), [])


class SpaAgreementTest(unittest.TestCase):
    # The SPA has to know which ports record BEFORE it offers a drill on them,
    # so the eligibility rule is mirrored in TS. A mirror nobody pins is a
    # mirror that drifts — the theme-allowlist / map-detail precedent.
    TS = Path(_REPO) / "web" / "src" / "lib" / "capacity-api.ts"

    def test_the_ts_mirror_reads_exactly_the_python_columns(self):
        py = set(re.findall(r'prior\["(\w+)"\]',
                            inspect.getsource(history.port_eligible)))
        src = self.TS.read_text()
        self.assertIn("export function portRecords", src,
                      "portRecords must stay in capacity-api.ts")
        # From the declaration to the next top-level export, then only the
        # RETURN expression: the parameter's type literal repeats the same
        # names and would pass a drifted body.
        start = src.index("export function portRecords")
        end = src.find("\nexport ", start + 1)
        decl = src[start:end if end != -1 else len(src)]
        ts = set(re.findall(r"\bp\.(\w+)", decl[decl.index("return"):]))
        self.assertEqual(ts, py)


if __name__ == "__main__":
    unittest.main()
