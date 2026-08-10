"""Cutting a cable, working out which way round it lies, and how long it is.

Most of these are about the cases where the answer must be a REFUSAL rather than
a plausible-looking wrong line, because what the rules here decide is whether a
splicing crew is sent down the right street and how much drum they order.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import cablepath  # noqa: E402


# A street running due east: five vertices, 100-ish metres apart.
STREET = [[17.0, 78.0], [17.0, 78.001], [17.0, 78.002], [17.0, 78.003], [17.0, 78.004]]


class SegmentModelTest(unittest.TestCase):
    """A cable is a SEGMENT now, so there is no stretch to carve out of it.

    `between` returned the portion of a longer route lying between two projected
    boxes, and `span_path` stitched a lateral onto each end of that. Both existed
    only because a cable had no ends and the boxes hung off it were not on it.
    Keeping them would mean two ways to answer "where does this line go", and the
    day they disagreed the map would draw one and the server would measure the
    other.
    """

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
        # A box beyond the last vertex taps the end of the glass. Extrapolating
        # would put a closure where no cable has been laid.
        seg, t = cablepath.project(STREET, 17.0, 78.010)
        self.assertEqual(seg, len(STREET) - 1)
        self.assertAlmostEqual(t, 1.0)

    def test_a_doubling_vertex_does_not_divide_by_zero(self):
        path = [[17.0, 78.0], [17.0, 78.0], [17.0, 78.001]]
        self.assertIsNotNone(cablepath.project(path, 17.0, 78.0005))


class SnapTest(unittest.TestCase):

    def test_a_click_near_the_line_records_a_point_ON_it(self):
        # An operator clicks near a line and never on it. Snapping on the way in
        # is what lets everything downstream treat a recorded point as being
        # exactly on the glass, instead of each reader deciding how near counts.
        point = cablepath.snap(STREET, 17.0004, 78.0025)
        self.assertEqual(point[0], 17.0)
        self.assertAlmostEqual(point[1], 78.0025, places=6)

    def test_a_path_that_is_not_a_path_snaps_to_nothing(self):
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.snap(bad, 17.0, 78.0))


class SplitTest(unittest.TestCase):
    """Opening a sheath at a new closure — the gesture the segment model needs.

    A cable's ends are recorded, so a box tapped halfway down a street has to
    become a real end. Asking an operator to redraw the street to achieve that is
    how a plant record stops being kept, so this has to be exact and it has to
    refuse cleanly at the edges.
    """

    def test_both_halves_are_COMPLETE_routes_carrying_the_cut(self):
        # After the split that coordinate is a closure standing on both sheaths,
        # so it is the last vertex of one and the first of the other. A cable's
        # path includes its own ends, unlike a span's waypoints.
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
        # Splitting a cable at its own end produces no second cable. Writing a
        # degenerate one-point row instead is how somebody ends up hunting a
        # sheath that draws as nothing.
        self.assertIsNone(cablepath.split(STREET, 17.0, 77.9))
        self.assertIsNone(cablepath.split(STREET, 17.0, 78.2))

    def test_an_untraced_cable_cannot_be_cut(self):
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.split(bad, 17.0, 78.0))


class OrientTest(unittest.TestCase):
    """Which end of the drawn route belongs to which recorded end.

    Measured, never stored. A cable's vertices are in the order somebody drew
    them, which says nothing about which end the record calls `a` — and a stored
    claim would have to be kept true through every retrace.
    """

    def test_a_cable_drawn_from_A_reads_forwards(self):
        self.assertTrue(cablepath.orient(STREET, (17.0, 78.0), (17.0, 78.004)))

    def test_a_cable_drawn_BACKWARDS_still_draws_the_right_way_round(self):
        self.assertFalse(cablepath.orient(STREET, (17.0, 78.004), (17.0, 78.0)))

    def test_it_is_decided_on_the_TOTAL_not_on_either_end_alone(self):
        # A pin can easily be nearer the wrong end of a route that doubles back.
        # Deciding each end independently is how both stubs get drawn to one
        # vertex with the cable crossing itself.
        doubled = [[17.0, 78.0], [17.0, 78.004], [17.001, 78.0005]]
        a, b = (17.0, 78.0), (17.001, 78.0006)
        self.assertTrue(cablepath.orient(doubled, a, b))

    def test_an_UNPLACED_end_abstains_rather_than_deciding(self):
        # A cable with one pin still draws the right way round from the other.
        self.assertTrue(cablepath.orient(STREET, (17.0, 78.0), None))
        self.assertFalse(cablepath.orient(STREET, None, (17.0, 78.0)))

    def test_with_no_route_the_answer_is_stable(self):
        self.assertTrue(cablepath.orient([], (17.0, 78.0), (17.0, 78.004)))


class LengthTest(unittest.TestCase):

    def test_it_is_walked_segment_by_segment(self):
        # The number a crew orders drum against. Mercator stretches with
        # latitude, so a projected chord would be wrong in a direction nobody
        # can see — and this street is four ~106 m hops, not one straight guess.
        self.assertAlmostEqual(cablepath.length_m(STREET), 425.4, places=0)

    def test_a_doubling_back_route_is_LONGER_than_its_chord(self):
        there_and_back = [[17.0, 78.0], [17.0, 78.004], [17.0, 78.0]]
        self.assertGreater(cablepath.length_m(there_and_back),
                           cablepath.length_m([[17.0, 78.0], [17.0, 78.004]]))

    def test_an_untraced_cable_has_NO_length_rather_than_zero(self):
        # Nobody walked it. Zero would be a measurement.
        for bad in ([], [[17.0, 78.0]], None):
            self.assertIsNone(cablepath.length_m(bad))


if __name__ == "__main__":
    unittest.main()
