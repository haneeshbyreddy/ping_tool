import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import cablepath  # noqa: E402


STREET = [[17.0, 78.0], [17.0, 78.001], [17.0, 78.002], [17.0, 78.003], [17.0, 78.004]]


class SegmentModelTest(unittest.TestCase):

    def test_the_stretch_helpers_are_GONE(self):
        for name in ("between", "span_path"):
            self.assertFalse(hasattr(cablepath, name),
                             f"cablepath.{name} came back — a cable's own path"
                             " is the line now")


class ProjectTest(unittest.TestCase):

    def test_it_finds_the_segment_and_the_fraction_along_it(self):
        seg, t = cablepath.project(STREET, 17.0005, 78.0025)
        self.assertEqual(seg, 3)
        self.assertAlmostEqual(t, 0.5, places=3)

    def test_a_point_past_the_end_clamps_to_the_end(self):
        seg, t = cablepath.project(STREET, 17.0, 78.010)
        self.assertEqual(seg, len(STREET) - 1)
        self.assertAlmostEqual(t, 1.0)

    def test_a_doubling_vertex_does_not_divide_by_zero(self):
        path = [[17.0, 78.0], [17.0, 78.0], [17.0, 78.001]]
        self.assertIsNotNone(cablepath.project(path, 17.0, 78.0005))


class SnapTest(unittest.TestCase):

    def test_a_click_near_the_line_records_a_point_ON_it(self):
        point = cablepath.snap(STREET, 17.0004, 78.0025)
        self.assertEqual(point[0], 17.0)
        self.assertAlmostEqual(point[1], 78.0025, places=6)

    def test_a_path_that_is_not_a_path_snaps_to_nothing(self):
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.snap(bad, 17.0, 78.0))


class SplitTest(unittest.TestCase):

    def test_both_halves_are_COMPLETE_routes_carrying_the_cut(self):
        head, tail = cablepath.split(STREET, 17.0005, 78.0025)
        self.assertEqual(head[0], STREET[0])
        self.assertEqual(tail[-1], STREET[-1])
        self.assertEqual(head[-1], tail[0])
        self.assertAlmostEqual(head[-1][1], 78.0025, places=6)

    def test_no_vertex_is_lost_or_duplicated(self):
        head, tail = cablepath.split(STREET, 17.0, 78.0025)
        rejoined = head + tail[1:]
        self.assertEqual([p[1] for p in rejoined],
                         [78.0, 78.001, 78.002, 78.0025, 78.003, 78.004])

    def test_a_cut_landing_exactly_on_a_vertex_is_not_emitted_twice(self):
        head, tail = cablepath.split(STREET, 17.0, 78.002)
        self.assertEqual(head, [[17.0, 78.0], [17.0, 78.001], [17.0, 78.002]])
        self.assertEqual(tail, [[17.0, 78.002], [17.0, 78.003], [17.0, 78.004]])

    def test_cutting_at_either_EXTREME_END_is_refused(self):
        self.assertIsNone(cablepath.split(STREET, 17.0, 77.9))
        self.assertIsNone(cablepath.split(STREET, 17.0, 78.2))

    def test_an_untraced_cable_cannot_be_cut(self):
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.split(bad, 17.0, 78.0))


class OrientTest(unittest.TestCase):

    def test_a_cable_drawn_from_A_reads_forwards(self):
        self.assertTrue(cablepath.orient(STREET, (17.0, 78.0), (17.0, 78.004)))

    def test_a_cable_drawn_BACKWARDS_still_draws_the_right_way_round(self):
        self.assertFalse(cablepath.orient(STREET, (17.0, 78.004), (17.0, 78.0)))

    def test_it_is_decided_on_the_TOTAL_not_on_either_end_alone(self):
        doubled = [[17.0, 78.0], [17.0, 78.004], [17.001, 78.0005]]
        a, b = (17.0, 78.0), (17.001, 78.0006)
        self.assertTrue(cablepath.orient(doubled, a, b))

    def test_an_UNPLACED_end_abstains_rather_than_deciding(self):
        self.assertTrue(cablepath.orient(STREET, (17.0, 78.0), None))
        self.assertFalse(cablepath.orient(STREET, None, (17.0, 78.0)))

    def test_with_no_route_the_answer_is_stable(self):
        self.assertTrue(cablepath.orient([], (17.0, 78.0), (17.0, 78.004)))


class LengthTest(unittest.TestCase):

    def test_it_is_walked_segment_by_segment(self):
        self.assertAlmostEqual(cablepath.length_m(STREET), 425.4, places=0)

    def test_a_doubling_back_route_is_LONGER_than_its_chord(self):
        there_and_back = [[17.0, 78.0], [17.0, 78.004], [17.0, 78.0]]
        self.assertGreater(cablepath.length_m(there_and_back),
                           cablepath.length_m([[17.0, 78.0], [17.0, 78.004]]))

    def test_an_untraced_cable_has_NO_length_rather_than_zero(self):
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.length_m(bad))


if __name__ == "__main__":
    unittest.main()
