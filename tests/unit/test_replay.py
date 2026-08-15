import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central.api import replay  # noqa: E402

# 2026-08-14T00:00:00Z
T0 = 1786060800
H = 3600
GRACE = 180


def _mark(node, kind, at_s):
    from datetime import datetime, timezone
    return {"node_id": node, "kind": kind,
            "at": datetime.fromtimestamp(at_s, tz=timezone.utc)
                          .replace(tzinfo=None).isoformat(timespec="seconds")}


class StaleIntervalTest(unittest.TestCase):
    def test_a_stale_mark_is_backdated_by_the_watchdog_threshold(self):
        # The watchdog only writes NODE_STALE because nothing arrived for
        # `central_node_stale_s`, so the silence provably began that much
        # earlier. Taking the row's own stamp would paint "up" over a gap we
        # can prove existed.
        out = replay.stale_intervals(
            [_mark("e1", "NODE_STALE", T0 + 2 * H),
             _mark("e1", "NODE_OK", T0 + 3 * H)],
            T0, T0 + 24 * H, GRACE)
        self.assertEqual(out["e1"], [(T0 + 2 * H - GRACE, T0 + 3 * H)])

    def test_a_probe_still_silent_at_the_window_end_stays_open(self):
        out = replay.stale_intervals(
            [_mark("e1", "NODE_STALE", T0 + H)], T0, T0 + 24 * H, GRACE)
        self.assertEqual(out["e1"], [(T0 + H - GRACE, None)])

    def test_a_mark_before_the_window_opens_the_interval_at_the_edge(self):
        # The seeded pre-window row is the whole reason node_stale_marks
        # reaches back: a probe silent since last Tuesday shows no mark inside
        # a 24h window, and the blackout would read as "up".
        out = replay.stale_intervals(
            [_mark("e1", "NODE_STALE", T0 - 40 * H),
             _mark("e1", "NODE_OK", T0 + 2 * H)],
            T0, T0 + 24 * H, GRACE)
        self.assertEqual(out["e1"], [(T0, T0 + 2 * H)])

    def test_a_recovery_with_no_open_interval_is_ignored(self):
        out = replay.stale_intervals(
            [_mark("e1", "NODE_OK", T0 + H)], T0, T0 + 24 * H, GRACE)
        self.assertEqual(out, {})

    def test_repeated_stale_marks_do_not_reopen(self):
        out = replay.stale_intervals(
            [_mark("e1", "NODE_STALE", T0 + H),
             _mark("e1", "NODE_STALE", T0 + 2 * H),
             _mark("e1", "NODE_OK", T0 + 3 * H)],
            T0, T0 + 24 * H, GRACE)
        self.assertEqual(out["e1"], [(T0 + H - GRACE, T0 + 3 * H)])

    def test_probes_are_tracked_apart(self):
        out = replay.stale_intervals(
            [_mark("e1", "NODE_STALE", T0 + H),
             _mark("e2", "NODE_STALE", T0 + 2 * H),
             _mark("e1", "NODE_OK", T0 + 3 * H)],
            T0, T0 + 24 * H, GRACE)
        self.assertEqual(sorted(out), ["e1", "e2"])
        self.assertEqual(out["e2"], [(T0 + 2 * H - GRACE, None)])


class IntersectTest(unittest.TestCase):
    def test_open_ended_intervals_intersect(self):
        self.assertEqual(
            replay.intersect([(10, None)], [(20, None)]), [(20, None)])

    def test_disjoint_intervals_intersect_to_nothing(self):
        self.assertEqual(replay.intersect([(0, 10)], [(20, 30)]), [])

    def test_touching_intervals_are_not_an_overlap(self):
        self.assertEqual(replay.intersect([(0, 10)], [(10, 20)]), [])

    def test_multiple_overlaps(self):
        self.assertEqual(
            replay.intersect([(0, 100)], [(10, 20), (50, 60)]),
            [(10, 20), (50, 60)])


class DeviceBlindTest(unittest.TestCase):
    FLOORS = [{"device_id": 1, "assigned_node_id": "e1"},
              {"device_id": 2, "assigned_node_id": "e2"},
              {"device_id": 3, "assigned_node_id": None}]

    def test_a_device_is_blind_only_while_its_own_probe_is(self):
        out = replay.device_blind(
            self.FLOORS, {"e1": [(10, 20)]}, ["e1", "e2"])
        self.assertEqual(out, {1: [(10, 20)]})

    def test_an_unassigned_device_is_blind_only_when_every_probe_is(self):
        # NULL means "every node for this org covers it", so one live probe is
        # enough to answer for it.
        out = replay.device_blind(
            self.FLOORS, {"e1": [(10, 40)], "e2": [(30, 60)]}, ["e1", "e2"])
        self.assertEqual(out[3], [(30, 40)])

    def test_absence_of_any_probe_record_is_not_a_blackout(self):
        # Inventing a fleet-wide blackout out of a missing row is a
        # fabrication in the other direction; the recording floors already
        # cover "before the record can answer".
        self.assertEqual(replay.device_blind(self.FLOORS, {}, []), {})
        self.assertEqual(replay.device_blind(self.FLOORS, {}, ["e1"]), {})


if __name__ == "__main__":
    unittest.main()
