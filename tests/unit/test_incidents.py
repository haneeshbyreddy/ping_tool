import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.incidents import evaluate

NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)

def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()

def _dev(did, state="DOWN", parent=None, lat=17.40, lng=78.40, started_min_ago=5.0,
         name=None):
    return {
        "id": did, "name": name or f"dev-{did}", "state": state,
        "parent_device_id": parent, "lat": lat, "lng": lng,
        "outage_started_at": _iso(started_min_ago) if state in ("DOWN", "UNREACHABLE") else None,
    }


class EvaluateTest(unittest.TestCase):

    def test_healthy_fleet_is_silent(self):
        rows = [_dev(i, state="UP") for i in range(5)]
        self.assertEqual(evaluate(rows, NOW), [])

    def test_two_downs_is_device_trouble_not_an_incident(self):
        rows = [_dev(1), _dev(2), _dev(3, state="UP")]
        self.assertEqual(evaluate(rows, NOW), [])

    def test_tight_multi_branch_wave_reads_as_power(self):
        # three independent feeds (no down parent in common), all inside ~1 km,
        # all within one window — a feeder outage, not three fiber cuts
        rows = [
            _dev(1, lat=17.400, lng=78.400, started_min_ago=6),
            _dev(2, lat=17.404, lng=78.402, started_min_ago=5),
            _dev(3, lat=17.398, lng=78.405, started_min_ago=4),
            _dev(4, state="UP"),
        ]
        found = evaluate(rows, NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "power")
        self.assertEqual(found[0].branches, 3)
        self.assertLessEqual(found[0].radius_km, 1.0)

    def test_single_branch_chain_reads_as_upstream(self):
        rows = [
            _dev(1, name="Backhaul-N", started_min_ago=6),
            _dev(2, state="UNREACHABLE", parent=1, started_min_ago=6),
            _dev(3, state="UNREACHABLE", parent=2, started_min_ago=6),
        ]
        found = evaluate(rows, NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "upstream")
        self.assertEqual(found[0].root_name, "Backhaul-N")

    def test_scattered_multi_branch_wave_stays_silent(self):
        # three branches but 30+ km apart — no verdict beats a wrong one
        rows = [
            _dev(1, lat=17.40, lng=78.40),
            _dev(2, lat=17.70, lng=78.70),
            _dev(3, lat=17.10, lng=78.10),
        ]
        self.assertEqual(evaluate(rows, NOW), [])

    def test_waves_split_on_the_time_gap(self):
        # an hour-old chain plus a fresh tight cluster = two separate stories
        rows = [
            _dev(1, started_min_ago=90),
            _dev(2, state="UNREACHABLE", parent=1, started_min_ago=90),
            _dev(3, state="UNREACHABLE", parent=2, started_min_ago=90),
            _dev(4, lat=17.500, lng=78.500, started_min_ago=4),
            _dev(5, lat=17.503, lng=78.502, started_min_ago=3),
            _dev(6, lat=17.498, lng=78.504, started_min_ago=2),
        ]
        found = evaluate(rows, NOW)
        self.assertEqual([i.kind for i in found], ["power", "upstream"])

    def test_unplaced_devices_cannot_carry_a_power_verdict(self):
        rows = [_dev(i, lat=None, lng=None) for i in (1, 2, 3)]
        self.assertEqual(evaluate(rows, NOW), [])

    def test_victims_of_an_older_outage_are_not_roots(self):
        # parent went down an hour ago; three children drop now — they're one
        # branch behind that parent, not three independent feeds
        rows = [
            _dev(1, started_min_ago=90),
            _dev(2, state="UNREACHABLE", parent=1, started_min_ago=5,
                 lat=17.400, lng=78.400),
            _dev(3, state="UNREACHABLE", parent=1, started_min_ago=4,
                 lat=17.401, lng=78.401),
            _dev(4, state="UNREACHABLE", parent=1, started_min_ago=4,
                 lat=17.402, lng=78.402),
        ]
        found = evaluate(rows, NOW)
        kinds = [i.kind for i in found]
        self.assertNotIn("power", kinds)


if __name__ == "__main__":
    unittest.main()
