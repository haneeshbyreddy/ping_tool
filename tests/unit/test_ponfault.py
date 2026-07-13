import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.ponfault import evaluate_olt, evaluate_org, passive_distances

NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.isoformat()

def _onu(key, state="online", distance=None, last_online_min_ago=2,
         port="0/6", device_id=7, updated_min_ago=1):
    return {
        "device_id": device_id, "device_name": "OLT-1", "onu_key": key,
        "pon_port": port, "name": key, "state": state, "distance_m": distance,
        "last_online_at": _iso(NOW - timedelta(minutes=last_online_min_ago)),
        "updated_at": _iso(NOW - timedelta(minutes=updated_min_ago)),
    }


class EvaluateOltTest(unittest.TestCase):

    def test_healthy_pon_yields_nothing(self):
        rows = [_onu(f"o{i}") for i in range(10)]
        self.assertEqual(evaluate_olt(rows, NOW), [])

    def test_fewer_than_min_dark_is_not_a_plant_event(self):
        rows = [_onu("a", state="los"), _onu("b", state="los"), _onu("c")]
        self.assertEqual(evaluate_olt(rows, NOW), [])

    def test_los_majority_classifies_as_fiber_with_cut_interval(self):
        rows = [
            _onu("near", state="online", distance=800),
            _onu("mid", state="los", distance=1700),
            _onu("far1", state="los", distance=1900),
            _onu("far2", state="los", distance=2400),
        ]
        faults = evaluate_olt(rows, NOW)
        self.assertEqual(len(faults), 1)
        f = faults[0]
        self.assertEqual(f.kind, "fiber")
        self.assertEqual(f.dark, 3)
        self.assertEqual(f.onus_total, 4)
        # cut sits past the last survivor, at or before the nearest dark ONU
        self.assertEqual(f.cut_low_m, 800)
        self.assertEqual(f.cut_high_m, 1700)

    def test_dying_gasp_majority_classifies_as_power_without_cut(self):
        rows = [
            _onu("a", state="dying_gasp", distance=1000),
            _onu("b", state="dying_gasp", distance=1200),
            _onu("c", state="los", distance=1100),
            _onu("d", state="online", distance=500),
        ]
        faults = evaluate_olt(rows, NOW)
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0].kind, "power")
        self.assertIsNone(faults[0].cut_low_m)
        self.assertIsNone(faults[0].cut_high_m)

    def test_long_dark_onus_are_not_a_fresh_cohort(self):
        # dark for days = chronic offline subscribers, not an event
        rows = [_onu(f"o{i}", state="offline", last_online_min_ago=60 * 24)
                for i in range(5)]
        self.assertEqual(evaluate_olt(rows, NOW), [])

    def test_unknown_state_never_joins_the_cohort(self):
        rows = [_onu(f"o{i}", state="unknown") for i in range(5)]
        self.assertEqual(evaluate_olt(rows, NOW), [])

    def test_no_survivor_short_of_the_cut_floors_at_zero(self):
        rows = [_onu(f"o{i}", state="los", distance=1500 + i * 100)
                for i in range(3)]
        f = evaluate_olt(rows, NOW)[0]
        self.assertEqual(f.cut_low_m, 0)
        self.assertEqual(f.cut_high_m, 1500)

    def test_missing_distances_still_detects_without_interval(self):
        rows = [_onu(f"o{i}", state="los") for i in range(4)]
        f = evaluate_olt(rows, NOW)[0]
        self.assertEqual(f.kind, "fiber")
        self.assertIsNone(f.cut_high_m)

    def test_ports_are_independent(self):
        rows = ([_onu(f"a{i}", state="los", port="0/1") for i in range(3)]
                + [_onu(f"b{i}", state="online", port="0/2") for i in range(3)])
        faults = evaluate_olt(rows, NOW)
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0].pon_port, "0/1")


class EvaluateOrgTest(unittest.TestCase):

    def test_stale_olt_is_skipped_entirely(self):
        # walk frozen 20 min ago — the OLT itself is down; the ICMP outage owns it
        rows = [_onu(f"o{i}", state="los", updated_min_ago=20, last_online_min_ago=21)
                for i in range(5)]
        self.assertEqual(evaluate_org(rows, NOW), [])

    def test_fresh_olt_reports(self):
        rows = [_onu(f"o{i}", state="los", distance=1000 + i) for i in range(4)]
        faults = evaluate_org(rows, NOW)
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0].device_id, 7)

    def test_olts_evaluated_separately(self):
        rows = ([_onu(f"a{i}", state="los", device_id=1) for i in range(3)]
                + [_onu(f"b{i}", state="los", device_id=2) for i in range(4)])
        faults = evaluate_org(rows, NOW)
        self.assertEqual({f.device_id for f in faults}, {1, 2})


def _pdev(did, dtype, parent=None, lat=None, lng=None, port=None, name=None):
    return {"id": did, "name": name or f"d{did}", "device_type": dtype,
            "parent_device_id": parent, "lat": lat, "lng": lng, "pon_port": port}


class PassiveDistanceTest(unittest.TestCase):
    # ~0.009° latitude ≈ 1 km; keep geometry on one meridian so chord math is obvious

    def test_chord_distance_when_no_route_drawn(self):
        devs = [
            _pdev(7, "OLT", lat=17.000, lng=78.4),
            _pdev(8, "splitter", parent=7, lat=17.009, lng=78.4, port="0/6",
                  name="S-1"),
        ]
        dists = passive_distances(devs, [])
        cands = dists[(7, "0/6")]
        self.assertEqual(len(cands), 1)
        self.assertAlmostEqual(cands[0]["dist_m"], 1001, delta=15)

    def test_drawn_route_beats_the_chord(self):
        # detour waypoint doubles the path vs the straight chord
        devs = [
            _pdev(7, "OLT", lat=17.000, lng=78.4),
            _pdev(8, "splitter", parent=7, lat=17.009, lng=78.4, port="0/6"),
        ]
        routes = [{"child_id": 8, "parent_id": 7,
                   "waypoints": [[17.0045, 78.409]]}]
        d_chord = passive_distances(devs, [])[(7, "0/6")][0]["dist_m"]
        d_route = passive_distances(devs, routes)[(7, "0/6")][0]["dist_m"]
        self.assertGreater(d_route, d_chord * 1.5)

    def test_cascade_inherits_the_port_and_sums_the_chain(self):
        devs = [
            _pdev(7, "OLT", lat=17.000, lng=78.4),
            _pdev(8, "splitter", parent=7, lat=17.009, lng=78.4, port="0/6"),
            _pdev(9, "fdb", parent=8, lat=17.018, lng=78.4),  # port blank
        ]
        cands = passive_distances(devs, [])[(7, "0/6")]
        self.assertEqual(len(cands), 2)
        far = max(cands, key=lambda c: c["dist_m"])
        self.assertEqual(far["id"], 9)
        self.assertAlmostEqual(far["dist_m"], 2002, delta=30)

    def test_unplaced_link_never_fabricates_a_distance(self):
        devs = [
            _pdev(7, "OLT", lat=17.000, lng=78.4),
            _pdev(8, "splitter", parent=7, port="0/6"),   # no pin
        ]
        self.assertEqual(passive_distances(devs, []), {})


class SuspectBindingTest(unittest.TestCase):

    def test_passive_inside_the_interval_gets_named(self):
        rows = [
            _onu("near", state="online", distance=800),
            _onu("far1", state="los", distance=1900),
            _onu("far2", state="los", distance=2000),
            _onu("far3", state="los", distance=2400),
        ]
        dists = {(7, "0/6"): [{"id": 9, "name": "FDB-14", "dist_m": 1500},
                              {"id": 10, "name": "S-9", "dist_m": 600}]}
        f = evaluate_olt(rows, NOW, passive_dists=dists)[0]
        # interval is (800, 1900]; FDB-14 at 1500 sits inside, S-9 is upstream
        self.assertEqual(f.suspect, "FDB-14")

    def test_no_passive_in_interval_means_no_suspect(self):
        rows = [
            _onu("near", state="online", distance=800),
            _onu("far1", state="los", distance=1900),
            _onu("far2", state="los", distance=2000),
            _onu("far3", state="los", distance=2400),
        ]
        dists = {(7, "0/6"): [{"id": 10, "name": "S-9", "dist_m": 600}]}
        f = evaluate_olt(rows, NOW, passive_dists=dists)[0]
        self.assertIsNone(f.suspect)

    def test_power_verdicts_never_accuse_plant(self):
        rows = [_onu(f"o{i}", state="dying_gasp", distance=1500) for i in range(4)]
        dists = {(7, "0/6"): [{"id": 9, "name": "FDB-14", "dist_m": 1400}]}
        f = evaluate_olt(rows, NOW, passive_dists=dists)[0]
        self.assertEqual(f.kind, "power")
        self.assertIsNone(f.suspect)


if __name__ == "__main__":
    unittest.main()
