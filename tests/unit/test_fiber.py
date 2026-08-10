"""The fibre-strand standard: the sequence, the tube arithmetic, and the refusals.

Two things are pinned here that nothing else can catch.

The TUBE ARITHMETIC, because it is the whole reason a strand number is stored
rather than a colour name: past twelve fibres a cable is buffer tubes of twelve
and the sequence restarts inside each one, so "core 25" is only useful once it
has been turned back into "the blue fibre in the green tube". Getting the two
divisions backwards produces a confident, wrong instruction to somebody standing
at an open closure.

And the SPA MIRROR, the same way `test_mapdetail` and the theme allowlist are
pinned: `web/src/lib/fiber.ts` holds a second copy because a browser has to draw
a swatch before any request resolves and central has to refuse a bad strand
without asking a browser. A drift between them is invisible in review and shows
up as a count the form offers and the server rejects, or — far worse — as two
different colours for one core.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import fiber  # noqa: E402

_SPA_FIBER = (Path(__file__).resolve().parents[2]
              / "web" / "src" / "lib" / "fiber.ts")


class SequenceTest(unittest.TestCase):
    def test_the_standard_order_is_the_standard_order(self):
        # TIA-598-D. Not sorted, not alphabetical — this IS the spec, and every
        # manufacturer on earth follows it.
        self.assertEqual([n for n, _ in fiber.STRAND_COLORS], [
            "blue", "orange", "green", "brown", "slate", "white",
            "red", "black", "yellow", "violet", "rose", "aqua"])

    def test_a_tube_holds_twelve(self):
        self.assertEqual(fiber.TUBE_SIZE, 12)

    def test_position_one_is_blue(self):
        self.assertEqual(fiber.strand_name(1), "blue")
        self.assertEqual(fiber.strand_name(12), "aqua")


class TubeTest(unittest.TestCase):
    def test_a_twelve_or_smaller_cable_has_no_tube_to_choose_between(self):
        # Naming "tube 1" on a cable with one tube is the same noise as the map
        # printing a dash where there is no reading: an absent fact and a present
        # one must not render alike.
        loc = fiber.locate(7, 12)
        self.assertIsNone(loc["tube"])
        self.assertEqual(loc["fiber_color"], "red")

    def test_core_25_of_a_48F_is_the_blue_fibre_in_the_green_tube(self):
        # The case the whole module exists for. 25 = tube 3 (green), fibre 1
        # (blue) — and a crew opens the green tube FIRST, so getting these two
        # the wrong way round sends them into the wrong bundle entirely.
        loc = fiber.locate(25, 48)
        self.assertEqual(loc["tube"], 3)
        self.assertEqual(loc["tube_color"], "green")
        self.assertEqual(loc["fiber"], 1)
        self.assertEqual(loc["fiber_color"], "blue")

    def test_the_last_fibre_of_a_tube_does_not_spill_into_the_next(self):
        # An off-by-one here moves every strand in the cable by one tube.
        self.assertEqual(fiber.locate(24, 48)["tube"], 2)
        self.assertEqual(fiber.locate(24, 48)["fiber_color"], "aqua")
        self.assertEqual(fiber.locate(25, 48)["tube"], 3)

    def test_describe_reads_as_an_instruction(self):
        self.assertEqual(fiber.describe(7, 12), "red fibre (7 of 12)")
        self.assertIn("green tube", fiber.describe(25, 48))


class ValidationTest(unittest.TestCase):
    def test_absent_is_a_real_answer(self):
        # Most spans on a fresh install have never been surveyed, and a guessed
        # count would be arithmetic nobody can act on.
        for blank in (None, "", "null"):
            self.assertIsNone(fiber.clean_fiber_count(blank))

    def test_reads_a_drum_tag(self):
        self.assertEqual(fiber.clean_fiber_count("12F"), 12)
        self.assertEqual(fiber.clean_fiber_count(" 24 core "), 24)
        self.assertEqual(fiber.clean_fiber_count(6), 6)

    def test_a_count_nobody_stocks_is_refused(self):
        for bad in (17, "100", 0):
            with self.assertRaises(fiber.FiberError):
                fiber.clean_fiber_count(bad)

    def test_a_single_fibre_tail_is_a_real_cable(self):
        # 1F is the strand out of a closure into a PON port. Leaving it out of
        # the vocabulary made that connection unrecordable outright: the tail
        # could not be laid, and a trunk core cannot be terminated at a box its
        # own sheath never reaches.
        self.assertEqual(fiber.clean_fiber_count(1), 1)
        self.assertEqual(fiber.clean_fiber_count("1F"), 1)
        self.assertEqual(fiber.clean_core_no(1, 1), 1)
        # …and it is a cable with ONE fibre, not a cable with one tube: naming a
        # tube on a sheath with nothing to choose between is the same noise as
        # printing a dash where there is no reading.
        loc = fiber.locate(1, 1)
        self.assertIsNone(loc["tube"])
        self.assertEqual(loc["fiber_color"], "blue")
        # a second strand of a 1F does not exist
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(2, 1)

    def test_a_strand_needs_a_cable_to_be_a_strand_of(self):
        # Half a fact. Storing it would print a tube and a colour with complete
        # confidence for a cable whose size nobody knows.
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(3, None)

    def test_a_strand_past_the_cable_is_refused(self):
        # The one that sends somebody to look for a fibre that does not exist.
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(30, 12)
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(0, 12)
        self.assertEqual(fiber.clean_core_no(12, 12), 12)


class SpaAgreementTest(unittest.TestCase):
    """The SPA mirror must not drift. Reads the TS source, like test_mapdetail."""

    def setUp(self):
        self.src = _SPA_FIBER.read_text()

    def test_the_counts_match(self):
        raw = re.search(r"FIBER_COUNTS = \[([^\]]+)\]", self.src)
        self.assertIsNotNone(raw, "FIBER_COUNTS not found in fiber.ts")
        spa = tuple(int(x) for x in re.findall(r"\d+", raw.group(1)))
        self.assertEqual(spa, fiber.FIBER_COUNTS)

    def test_every_strand_name_and_hex_matches(self):
        # Both halves: a name drift mislabels a colour in words, a hex drift
        # draws the wrong swatch beside the right word. The second is worse —
        # nobody reads the word once there is a colour to match against.
        spa = re.findall(r'\{ name: "(\w+)", hex: "(#[0-9a-f]{6})" \}', self.src)
        self.assertEqual(spa, [list(c) and (c[0], c[1]) for c in fiber.STRAND_COLORS])

    def test_the_tube_size_is_not_hardcoded_differently(self):
        # TS derives it from the array's length, which is the point — assert the
        # derivation is still there rather than a literal that could drift.
        self.assertIn("TUBE_SIZE = STRAND_COLORS.length", self.src)


def _cable(cid, a, b, cores=12):
    return {"id": cid, "cores": cores, "a_point": a, "b_point": b}


def _joint(point, a, b=None):
    """A splice, or (with no `b`) a fibre taken out to the equipment here."""
    return {"point": point, "a_cable_id": a[0], "a_core_no": a[1],
            "b_cable_id": b[0] if b else None, "b_core_no": b[1] if b else None}


class SegmentModelTest(unittest.TestCase):
    """The deletions ARE the feature, so they are pinned like any other rule.

    `core_path` ordered the several spans of one cable that shared one core, and
    every fault it could report — the double booking above all — existed because
    a cable was a bag of spans with no ends. A cable is a SEGMENT now: core N of
    it has exactly two ends and cannot be two disconnected runs, so those states
    are unrepresentable rather than merely unreported. Re-exporting any of this
    is how the old model creeps back, exactly as `LINK_COLORS` would have brought
    back the link tint.
    """

    def test_the_double_booking_checker_is_GONE_not_merely_passing(self):
        for name in ("core_path", "core_faults", "CORE_FAULTS", "splice_refusal",
                     "SPLICE_REFUSAL_TEXT"):
            self.assertFalse(hasattr(fiber, name),
                             f"fiber.{name} came back — the segment model makes"
                             " it unrepresentable, so it can only mislead")


class JointRefusalTest(unittest.TestCase):
    """What may not be joined at a point, and — just as load-bearing — what may.

    Everything refused is physically impossible rather than merely unusual.
    Refusing what only looks strange is how a tool blocks real plant, and this
    class exists as much to pin the two allowed cases as the three refused ones.
    """

    def setUp(self):
        # A trunk into the closure, a branch out of it, and a third cable that
        # passes nowhere near.
        self.cables = {
            1: _cable(1, ("device", 10), ("device", 20)),
            2: _cable(2, ("device", 20), ("device", 30)),
            3: _cable(3, ("device", 40), ("device", 50)),
        }
        self.here = ("device", 20)

    def test_a_fibre_that_does_not_END_here_cannot_be_joined_here(self):
        # A strand passing a closure it was never cut at is not available to be
        # spliced there — the physical fact the whole tray depends on.
        self.assertEqual(
            fiber.joint_refusal((1, 1), (3, 1), self.here, self.cables, {}),
            "absent")

    def test_a_fibre_cannot_be_joined_to_itself(self):
        self.assertEqual(
            fiber.joint_refusal((1, 4), (1, 4), self.here, self.cables, {}),
            "self")

    def test_ONE_fibre_joins_exactly_ONE_fibre(self):
        # Enforced on the write so an operator finds out while looking at the
        # tray, rather than as a fault chip discovered later.
        taken = {(1, 1): {"id": 99}}
        self.assertEqual(
            fiber.joint_refusal((1, 1), (2, 1), self.here, self.cables, taken),
            "taken")
        self.assertEqual(
            fiber.joint_refusal((2, 1), (1, 1), self.here, self.cables, taken),
            "taken")

    def test_the_SAME_core_number_of_two_cables_is_the_ordinary_case(self):
        # A 12F spliced straight through to another 12F is twelve of these. The
        # old model had to refuse a same-core splice because two sections of ONE
        # cable were implicitly continuous; segments removed that ambiguity, and
        # keeping the refusal would now block the commonest closure there is.
        self.assertIsNone(
            fiber.joint_refusal((1, 7), (2, 7), self.here, self.cables, {}))

    def test_a_U_TURN_within_one_cable_is_allowed_and_reported_later(self):
        # Rare, buildable, and if it ever matters `trace` reports it as a loop.
        # Refusing it here would be taste dressed as physics.
        self.assertIsNone(
            fiber.joint_refusal((1, 3), (1, 9), self.here, self.cables, {}))

    def test_a_TERMINATION_is_checked_the_same_way(self):
        # Taking a core out to the box standing here is the same kind of
        # statement as a splice and consumes the fibre end identically.
        self.assertIsNone(
            fiber.joint_refusal((1, 2), None, self.here, self.cables, {}))
        self.assertEqual(
            fiber.joint_refusal((3, 2), None, self.here, self.cables, {}),
            "absent")
        self.assertEqual(
            fiber.joint_refusal((1, 2), None, self.here, self.cables,
                                {(1, 2): {"id": 1}}),
            "taken")

    def test_a_cable_that_does_not_exist_is_absent_rather_than_a_crash(self):
        self.assertEqual(
            fiber.joint_refusal((404, 1), None, self.here, self.cables, {}),
            "absent")

    def test_every_refusal_has_a_sentence(self):
        # A tray that refuses without saying why is indistinguishable from a
        # broken button.
        for why in ("absent", "self", "taken"):
            self.assertIn(why, fiber.JOINT_REFUSAL_TEXT)


class ContinuityTest(unittest.TestCase):

    def test_a_TERMINATION_is_not_continuity(self):
        # It is where the walk STOPS. Folding it in would make a trace run
        # through an OLT and out the other side.
        joints = [_joint(("device", 20), (1, 1))]
        self.assertEqual(fiber.continuity(joints), {})
        self.assertEqual(list(fiber.terminations(joints)),
                         [(1, 1, ("device", 20))])

    def test_continuity_is_a_fact_about_an_END_not_about_a_strand(self):
        # The same core of the same sheath is routinely spliced to something
        # different at each of its two closures.
        joints = [_joint(("device", 20), (1, 1), (2, 5)),
                  _joint(("device", 10), (1, 1), (9, 3))]
        joins = fiber.continuity(joints)
        self.assertEqual(joins[(1, 1, ("device", 20))], [(2, 5)])
        self.assertEqual(joins[(1, 1, ("device", 10))], [(9, 3)])

    def test_a_fork_survives_as_far_as_the_trace(self):
        # Collapsing it here — picking one — is how a tool draws a confident line
        # down whichever branch it happened to sort first.
        joints = [_joint(("device", 20), (1, 1), (2, 1)),
                  _joint(("device", 20), (1, 1), (3, 1))]
        self.assertEqual(len(fiber.continuity(joints)[(1, 1, ("device", 20))]), 2)


class TraceTest(unittest.TestCase):
    """The whole optical path one strand makes, across sheaths and closures.

    The question somebody holding a light source is asking, and the one thing no
    single row can answer: out of the OLT on the trunk, cut at a closure, onward
    on a DIFFERENT strand of a DIFFERENT sheath to the splitter.
    """

    def setUp(self):
        # OLT --trunk(1)--> JC --branch(2)--> SPL, core 1 crossing to core 5.
        self.cables = [_cable(1, ("device", 10), ("device", 20)),
                       _cable(2, ("device", 20), ("device", 30))]
        self.joints = [_joint(("device", 20), (1, 1), (2, 5))]

    def test_it_crosses_the_sheath_at_the_closure(self):
        out = fiber.trace(self.cables, self.joints, 1, 1)
        self.assertTrue(out["ok"])
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1, 2])
        self.assertEqual([h["core_no"] for h in out["hops"]], [1, 5])
        self.assertEqual(out["points"],
                         [("device", 10), ("device", 20), ("device", 30)])

    def test_it_reads_the_same_from_either_end(self):
        # It must not matter which segment of a long path the operator clicked.
        forward = fiber.trace(self.cables, self.joints, 1, 1)
        backward = fiber.trace(self.cables, self.joints, 2, 5)
        self.assertEqual(forward["points"], backward["points"])
        self.assertEqual(forward["hops"], backward["hops"])

    def test_without_the_joint_it_stops_at_the_closure(self):
        # Two cables meeting at a box is NOT continuity. Under the old model one
        # cable on one core was implicitly continuous through a device, and that
        # implicit rule is exactly what the segment model deleted.
        out = fiber.trace(self.cables, [], 1, 1)
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1])
        self.assertEqual(out["points"], [("device", 10), ("device", 20)])

    def test_a_TERMINATION_is_reported_at_the_end_it_lands_on(self):
        # Where the fibre actually goes is half the answer; what it lands ON is
        # the other half, and a hop list has nowhere to carry it.
        joints = self.joints + [_joint(("device", 30), (2, 5))]
        out = fiber.trace(self.cables, joints, 1, 1)
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1, 2])
        self.assertIsNone(out["ends"][0])
        self.assertIsNotNone(out["ends"][1])

    def test_a_CUSTOMER_is_a_point_like_any_other(self):
        # The case the ISPs added: a lane of houses daisy-chained down one 4F,
        # core 1 into this one and the rest passing onward.
        cables = [_cable(1, ("device", 30), ("onu", "AABB")),
                  _cable(2, ("onu", "AABB"), ("onu", "CCDD"))]
        joints = [_joint(("onu", "AABB"), (1, 2), (2, 2))]
        out = fiber.trace(cables, joints, 1, 2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["points"],
                         [("device", 30), ("onu", "AABB"), ("onu", "CCDD")])

    def test_a_FORK_stops_the_walk_and_names_where(self):
        # Returning a guess past a fork is the failure this module is built
        # against — a splicer following a confidently drawn line to the wrong
        # closure. What comes back is the unambiguous part, plus the location.
        cables = self.cables + [_cable(3, ("device", 20), ("device", 40))]
        joints = self.joints + [_joint(("device", 20), (1, 1), (3, 1))]
        out = fiber.trace(cables, joints, 1, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["fault"], "fork")
        self.assertEqual(out["fault_at"], ("device", 20))
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1])

    def test_a_LOOP_terminates(self):
        cables = [_cable(1, ("device", 10), ("device", 20)),
                  _cable(2, ("device", 20), ("device", 10))]
        joints = [_joint(("device", 20), (1, 1), (2, 1)),
                  _joint(("device", 10), (2, 1), (1, 1))]
        out = fiber.trace(cables, joints, 1, 1)
        self.assertEqual(out["fault"], "loop")

    def test_a_cable_that_is_not_there_is_reported_not_guessed(self):
        out = fiber.trace(self.cables, self.joints, 404, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["fault"], "missing")
        self.assertEqual(out["hops"], [])

    def test_a_long_chain_comes_back_in_ORDER_end_to_end(self):
        cables = [_cable(i, ("device", i * 10), ("device", (i + 1) * 10))
                  for i in range(1, 5)]
        joints = [_joint(("device", (i + 1) * 10), (i, 1), (i + 1, 1))
                  for i in range(1, 4)]
        out = fiber.trace(cables, joints, 3, 1)
        self.assertTrue(out["ok"])
        self.assertEqual([p[1] for p in out["points"]], [10, 20, 30, 40, 50])


class CableNameTest(unittest.TestCase):

    def test_a_cable_must_be_named(self):
        # The one field here with no honest absent state: a cable exists because
        # somebody decided several spans are one piece of glass, and that
        # decision needs a handle to be spoken about.
        for bad in (None, "", "   "):
            with self.assertRaises(fiber.FiberError):
                fiber.clean_cable_name(bad)

    def test_a_name_is_trimmed_and_bounded(self):
        self.assertEqual(fiber.clean_cable_name("  Haliya trunk "), "Haliya trunk")
        with self.assertRaises(fiber.FiberError):
            fiber.clean_cable_name("x" * (fiber.CABLE_NAME_MAX + 1))


class FeedMapTest(unittest.TestCase):
    """Which end of a run feeds the other, derived rather than declared.

    A run is undirected — one splice is one row whichever end the operator was
    standing at — and that is deliberate: direction is a fact about the shape of
    the network, not about a piece of fibre, so storing it would be a second
    copy of something already implied. This is what recovers it, and it is what
    lets a box be placed with no parent at all and still have a plant chain.
    """

    def test_a_chain_is_ordered_from_the_gear_outwards(self):
        # Recorded BACK TO FRONT on purpose: the operator writes down the splice
        # they are standing at, which is routinely the far end first.
        feed = fiber.feed_map([(20, 30), (10, 20)], roots={10})
        self.assertEqual(feed, {20: 10, 30: 20})

    def test_the_ROOTS_are_never_given_a_feed(self):
        # Gear's upstream is its own declared parent — the monitoring dependency
        # that decides what pages — and a recorded splice may not move it.
        feed = fiber.feed_map([(10, 20), (10, 11)], roots={10, 11})
        self.assertNotIn(10, feed)
        self.assertNotIn(11, feed)

    def test_what_the_walk_never_REACHES_simply_has_no_feed(self):
        # Two splitters spliced to each other and to nothing else genuinely does
        # not say which of them is upstream, and inventing an answer would put a
        # split total and a PON on the wrong one.
        feed = fiber.feed_map([(50, 51)], roots={10})
        self.assertEqual(feed, {})

    def test_a_RING_still_resolves_and_does_not_spin(self):
        # A ring has no upstream to be right about, so the only requirement is
        # that it terminates and answers the same way twice — a chain that
        # reshuffles between two reads is worse than one that is arbitrary.
        runs = [(10, 20), (20, 30), (30, 10)]
        first = fiber.feed_map(runs, roots={10})
        self.assertEqual(first, fiber.feed_map(list(reversed(runs)), roots={10}))
        self.assertEqual(first, {20: 10, 30: 10})

    def test_a_box_spliced_to_itself_is_ignored_rather_than_looped(self):
        self.assertEqual(fiber.feed_map([(10, 10)], roots={10}), {})

    def test_the_SHORTEST_path_back_to_gear_wins(self):
        # 30 is reachable both directly and the long way round; the direct
        # splice is the feed.
        feed = fiber.feed_map([(10, 20), (20, 30), (10, 30)], roots={10})
        self.assertEqual(feed[30], 10)


if __name__ == "__main__":
    unittest.main()
