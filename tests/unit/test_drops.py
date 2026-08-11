import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.drops import (OUTLIER_DB, branch_faults, resolve_drops,
                                splitter_loads)


def _onu(serial, *, state="online", rx=None, device_id=7, pon="EPON0/1",
         onu_id=1, severity=None):
    return {"device_id": device_id, "device_name": "OLT-1", "serial": serial,
            "state": state, "rx_dbm": rx, "pon_port": pon, "onu_id": onu_id,
            "name": serial, "severity": severity}


class ResolveTest(unittest.TestCase):

    def test_a_recorded_mac_in_no_roster_is_an_orphan_not_a_deletion(self):
        got = resolve_drops([_onu("AA")], {"AA": 1, "ZZ": 1})
        gone = [d for d in got if d.mac == "ZZ"][0]
        self.assertFalse(gone.matched)
        self.assertFalse(gone.dark)
        self.assertEqual(splitter_loads(got)[1].orphans, 1)

    def test_an_ambiguous_mac_is_never_pinned_to_one_splitter(self):
        roster = [_onu("AA", device_id=7), _onu("AA", device_id=9)]
        d = resolve_drops(roster, {"AA": 1})[0]
        self.assertIsNone(d.olt_id)
        self.assertFalse(d.matched)


class LoadTest(unittest.TestCase):

    def test_counts_split_online_dark_and_measured(self):
        roster = [_onu("A", rx=-22.0), _onu("B", rx=-23.0),
                  _onu("C", state="offline")]
        loads = splitter_loads(resolve_drops(roster, {"A": 5, "B": 5, "C": 5}))
        self.assertEqual((loads[5].recorded, loads[5].online, loads[5].dark),
                         (3, 2, 1))
        self.assertEqual(loads[5].rx_seen, 2)
        self.assertEqual(loads[5].rx_median, -22.5)

    def test_an_outlier_is_judged_against_its_own_splitters_median(self):
        roster = [_onu("A", rx=-22.0), _onu("B", rx=-22.5), _onu("C", rx=-22.3),
                  _onu("D", rx=-22.0 - OUTLIER_DB - 1)]
        loads = splitter_loads(resolve_drops(roster, dict.fromkeys("ABCD", 5)))
        self.assertEqual(loads[5].outliers, 1)

    def test_a_uniformly_low_splitter_is_not_a_box_full_of_outliers(self):
        roster = [_onu(c, rx=-31.0) for c in "ABCD"]
        loads = splitter_loads(resolve_drops(roster, dict.fromkeys("ABCD", 5)))
        self.assertEqual(loads[5].outliers, 0)
        self.assertEqual(loads[5].rx_median, -31.0)


class BranchFaultTest(unittest.TestCase):
    PARENTS = {1: None, 2: 1, 3: 2, 4: 2}

    def _faults(self, roster, drop_map, fresh={7}):
        return branch_faults(resolve_drops(roster, drop_map), self.PARENTS,
                             fresh_olt_ids=fresh)

    def test_all_dark_below_one_box_with_a_lit_sibling_names_that_span(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline"),
                  _onu("C"), _onu("D")]
        f = self._faults(roster, {"A": 3, "B": 3, "C": 4, "D": 4})
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0].passive_id, f[0].parent_id), (3, 2))
        self.assertEqual((f[0].dark, f[0].lit_siblings), (2, 2))
        self.assertEqual(f[0].cause, "fiber")

    def test_one_dark_subscriber_is_a_subscriber_problem_not_a_span(self):
        roster = [_onu("A", state="offline"), _onu("C"), _onu("D")]
        self.assertEqual(self._faults(roster, {"A": 3, "C": 4, "D": 4}), [])

    def test_a_lit_subscriber_in_the_branch_clears_it(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline"),
                  _onu("E"), _onu("C")]
        self.assertEqual(
            self._faults(roster, {"A": 3, "B": 3, "E": 3, "C": 4}), [])

    def test_a_fault_higher_up_reports_the_TOP_box_only(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline"),
                  _onu("C", state="offline"), _onu("D", state="offline"),
                  _onu("E")]
        f = self._faults(roster, {"A": 3, "B": 3, "C": 4, "D": 4, "E": 9})
        self.assertEqual(f, [])

    def test_a_dying_gasp_majority_reads_as_power_not_a_cut(self):
        roster = [_onu("A", state="dying_gasp"), _onu("B", state="dying_gasp"),
                  _onu("C")]
        f = self._faults(roster, {"A": 3, "B": 3, "C": 4})
        self.assertEqual(f[0].cause, "power")

    def test_a_dark_power_backed_witness_outranks_the_gasp_majority(self):
        roster = [_onu("A", state="dying_gasp"), _onu("B", state="dying_gasp"),
                  _onu("W", state="offline"), _onu("C")]
        f = branch_faults(
            resolve_drops(roster, {"A": 3, "B": 3, "W": 3, "C": 4},
                          witness_macs={"W"}),
            self.PARENTS, fresh_olt_ids={7})
        self.assertEqual(f[0].cause, "fiber")
        self.assertEqual(f[0].witness_dark, 1)
        self.assertFalse(f[0].suspected)

    def test_a_gasping_witness_counts_in_neither_tally(self):
        roster = [_onu("A", state="dying_gasp"), _onu("W", state="dying_gasp"),
                  _onu("C")]
        f = branch_faults(
            resolve_drops(roster, {"A": 3, "W": 3, "C": 4}, witness_macs={"W"}),
            self.PARENTS, fresh_olt_ids={7})
        self.assertEqual(f[0].cause, "power")
        self.assertEqual(f[0].witness_dark, 0)

    def test_a_silent_olt_claims_nothing(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline"),
                  _onu("C")]
        self.assertEqual(
            self._faults(roster, {"A": 3, "B": 3, "C": 4}, fresh=set()), [])

    def test_unrecorded_subscribers_are_never_assumed_lit_or_dark(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline"),
                  _onu("UNRECORDED"), _onu("C")]
        f = self._faults(roster, {"A": 3, "B": 3, "C": 4})
        self.assertEqual(f[0].dark, 2)

    def test_only_passive_plant_can_be_named(self):
        parents = {99: None, 1: 99, 2: 1, 3: 2, 90: 99, 91: 90}
        roster = [_onu("A", state="offline", device_id=7),
                  _onu("B", state="offline", device_id=7),
                  _onu("C", device_id=8)]
        resolved = resolve_drops(roster, {"A": 3, "B": 3, "C": 91})
        wide = branch_faults(resolved, parents, fresh_olt_ids={7, 8})
        self.assertIn(1, [f.passive_id for f in wide])
        narrowed = branch_faults(resolved, parents, fresh_olt_ids={7, 8},
                                 passive_ids={2, 3, 91})
        self.assertEqual(narrowed, [])

    def test_a_parent_cycle_cannot_spin_the_walk(self):
        roster = [_onu("A", state="offline"), _onu("B", state="offline")]
        branch_faults(resolve_drops(roster, {"A": 3, "B": 4}),
                      {3: 4, 4: 3}, fresh_olt_ids={7})


if __name__ == "__main__":
    unittest.main()
