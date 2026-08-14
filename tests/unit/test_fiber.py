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
        loc = fiber.locate(7, 12)
        self.assertIsNone(loc["tube"])
        self.assertEqual(loc["fiber_color"], "red")

    def test_core_25_of_a_48F_is_the_blue_fibre_in_the_green_tube(self):
        loc = fiber.locate(25, 48)
        self.assertEqual(loc["tube"], 3)
        self.assertEqual(loc["tube_color"], "green")
        self.assertEqual(loc["fiber"], 1)
        self.assertEqual(loc["fiber_color"], "blue")

    def test_the_last_fibre_of_a_tube_does_not_spill_into_the_next(self):
        self.assertEqual(fiber.locate(24, 48)["tube"], 2)
        self.assertEqual(fiber.locate(24, 48)["fiber_color"], "aqua")
        self.assertEqual(fiber.locate(25, 48)["tube"], 3)

    def test_describe_reads_as_an_instruction(self):
        self.assertEqual(fiber.describe(7, 12), "red fibre (7 of 12)")
        self.assertIn("green tube", fiber.describe(25, 48))


class ValidationTest(unittest.TestCase):
    def test_absent_is_a_real_answer(self):
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
        self.assertEqual(fiber.clean_fiber_count(1), 1)
        self.assertEqual(fiber.clean_fiber_count("1F"), 1)
        self.assertEqual(fiber.clean_core_no(1, 1), 1)
        loc = fiber.locate(1, 1)
        self.assertIsNone(loc["tube"])
        self.assertEqual(loc["fiber_color"], "blue")
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(2, 1)

    def test_a_strand_needs_a_cable_to_be_a_strand_of(self):
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(3, None)

    def test_a_strand_past_the_cable_is_refused(self):
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(30, 12)
        with self.assertRaises(fiber.FiberError):
            fiber.clean_core_no(0, 12)
        self.assertEqual(fiber.clean_core_no(12, 12), 12)


class SpaAgreementTest(unittest.TestCase):
    def setUp(self):
        self.src = _SPA_FIBER.read_text()

    def test_the_counts_match(self):
        raw = re.search(r"FIBER_COUNTS = \[([^\]]+)\]", self.src)
        self.assertIsNotNone(raw, "FIBER_COUNTS not found in fiber.ts")
        spa = tuple(int(x) for x in re.findall(r"\d+", raw.group(1)))
        self.assertEqual(spa, fiber.FIBER_COUNTS)

    def test_every_strand_name_and_hex_matches(self):
        spa = re.findall(r'\{ name: "(\w+)", hex: "(#[0-9a-f]{6})" \}', self.src)
        self.assertEqual(spa, [list(c) and (c[0], c[1]) for c in fiber.STRAND_COLORS])

    def test_the_tube_size_is_not_hardcoded_differently(self):
        self.assertIn("TUBE_SIZE = STRAND_COLORS.length", self.src)

    def test_the_port_kinds_match(self):
        raw = re.search(r"PORT_KINDS = \[([^\]]+)\]", self.src)
        self.assertIsNotNone(raw, "PORT_KINDS not found in fiber.ts")
        self.assertEqual(tuple(re.findall(r'"(\w+)"', raw.group(1))),
                         fiber.PORT_KINDS)

    def test_a_port_reads_the_same_on_both_sides(self):
        self.assertEqual(
            [fiber.port_label(k, r) for k, r in
             (("pon", "3"), ("leg", "4"), ("in", None), ("in", "1"), ("in", "2"),
              ("port", "GE0/5 INPUT"))],
            ["PON 3", "leg 4", "input", "input", "input 2", "GE0/5 INPUT"])
        for template in ("`PON ${text}`", "`leg ${text}`", "`input ${text}`"):
            self.assertIn(template, self.src, template)

    def test_the_KIND_LIST_is_mirrored(self):
        # A box that central says has two kinds must offer two in the SPA, or the
        # panel refuses a port the picker offered.
        self.assertIn("export function portKindsFor", self.src)
        self.assertIn('return ["pon", "port"]', self.src)
        raw = re.search(r"PORT_KINDS = \[([^\]]+)\]", self.src)
        self.assertNotIn("uplink", re.findall(r'"(\w+)"', raw.group(1)))

    def test_the_ONE_normalizer_is_mirrored(self):
        # Both sides must reconcile two spellings of one socket the SAME way, or a
        # port the SPA thinks is free is one central thinks is taken.
        self.assertIn("export function portKey", self.src)
        self.assertIn("NUMBERED_KINDS", self.src)
        raw = re.search(r"NUMBERED_KINDS = \[([^\]]+)\]", self.src)
        self.assertIsNotNone(raw, "NUMBERED_KINDS not found in fiber.ts")
        self.assertEqual(tuple(re.findall(r'"(\w+)"', raw.group(1))),
                         fiber.NUMBERED_KINDS)

    def test_every_refusal_central_can_send_has_a_sentence_in_the_SPA(self):
        for key in fiber.JOINT_REFUSAL_TEXT:
            self.assertIn(f"{key}:", self.src, key)


def _cable(cid, a, b, cores=12):
    return {"id": cid, "cores": cores, "a_point": a, "b_point": b}


def _joint(point, a, b=None, port=None):
    return {"point": point, "a_cable_id": a[0], "a_core_no": a[1],
            "b_cable_id": b[0] if b else None, "b_core_no": b[1] if b else None,
            "port_kind": port[0] if port else None,
            "port_ref": port[1] if port else None}


class PortTest(unittest.TestCase):

    def test_an_enclosure_has_NO_ports_and_keeps_its_splice_schedule(self):
        for kind in ("coupler", "closure", "fdb", "switch", None):
            self.assertEqual(fiber.port_slots(kind, split_ratio=8), [])

    def test_a_splitter_has_its_inputs_then_its_legs(self):
        self.assertEqual(
            fiber.port_slots("splitter", split_ratio=4, split_inputs=2),
            [("in", "1"), ("in", "2"),
             ("leg", "1"), ("leg", "2"), ("leg", "3"), ("leg", "4")])

    def test_an_OLT_lists_the_PONs_IT_REPORTS_gaps_and_all(self):
        self.assertEqual(fiber.port_slots("OLT", pons=[1, 3, 4]),
                         [("pon", "1"), ("pon", "3"), ("pon", "4")])

    def test_a_box_with_nothing_to_go_on_offers_nothing_rather_than_guessing(self):
        self.assertEqual(fiber.port_slots("OLT", pons=[]), [])
        self.assertEqual(fiber.port_slots("splitter", split_ratio=None),
                         [("in", "1")])

    def test_a_leg_is_bounded_by_the_split_but_a_PON_is_not(self):
        self.assertEqual(fiber.port_bound("leg", split_ratio=8), 8)
        self.assertEqual(fiber.port_bound("in", split_inputs=2), 2)
        self.assertIsNone(fiber.port_bound("pon"))
        self.assertIsNone(fiber.port_bound("leg", split_ratio=None))

    def test_a_pseudo_port_is_never_offered_as_a_place_to_land_a_fibre(self):
        for junk in ("EPON01ONU3", "EPON0/1:5", "", None, "eth0"):
            self.assertIsNone(fiber.pon_index(junk), junk)

    def test_every_shape_this_fleet_actually_writes_a_PON_in(self):
        for label, want in (("EPON0/3", 3), ("GPON0/1 ANREDDY", 1),
                            ("EPON01 bolla", 1), ("EPON08", 8),
                            ("epon 0/1/2", 2), ("pon1", 1), ("PON01", 1),
                            ("3", 3)):
            self.assertEqual(fiber.pon_index(label), want, label)

    def test_a_stray_label_costs_ONE_odd_row_not_fifty_two_invented_ones(self):
        self.assertEqual(fiber.pon_ports(roster=["EPON0/1", "EPON0/2", "60"]),
                         [1, 2, 60])

    def test_AN_INTERFACE_HAS_TO_SAY_IT_IS_A_PON(self):
        for junk in ("GE0/9", "GE016", "VLAN10", "GE01 BSNL_UPLINK1", "eth0"):
            self.assertIsNone(fiber.pon_index_of_interface(junk), junk)
        for real, want in (("EPON0/3", 3), ("GPON0/1 ANREDDY", 1),
                           ("EPON01 bolla", 1), ("pon1", 1)):
            self.assertEqual(fiber.pon_index_of_interface(real), want, real)

    def test_the_ROSTER_is_read_permissively_because_of_WHERE_IT_SITS(self):
        self.assertEqual(fiber.pon_index("3"), 3)
        self.assertIsNone(fiber.pon_index_of_interface("3"))

    def test_a_RECORDED_port_never_drops_off_the_list_that_offered_it(self):
        self.assertEqual(fiber.pon_ports(recorded=[7]), [7])


class PonNameTest(unittest.TestCase):

    # A PON IS NAMED BY THE BOX. Its REF stays the index — that is what joins to the
    # roster and what `pon_of_points` inherits down the plant — but SRPL-OLT calls
    # its four PONs `EPON0/1..4` on every other screen in this product, and the fibre
    # pickers called them `PON 1..4` beside a `GE0/1` read straight off the same walk.

    SRPL = ("GE0/1", "GE0/2", "GE0/3", "GE0/4", "EPON0/1", "EPON0/2", "EPON0/3",
            "EPON0/4", "VLAN1", "EPON0/1:5")

    def test_the_box_names_its_own_PONs(self):
        self.assertEqual(
            fiber.pon_names(interfaces=self.SRPL),
            {"1": "EPON0/1", "2": "EPON0/2", "3": "EPON0/3", "4": "EPON0/4"})

    def test_a_BARE_NUMBER_IS_NOT_A_NAME(self):
        # The Syrotech build writes a bare `3` in `pon_port`; `PON 3` beats `3`.
        self.assertEqual(fiber.pon_names(roster=["3", "4"]), {})
        self.assertEqual(fiber.port_display("pon", "3", {}), "PON 3")

    def test_the_WALK_wins_over_the_roster(self):
        # Both usually carry the same string; where they differ the interface name is
        # the box naming its own socket.
        self.assertEqual(
            fiber.pon_names(roster=["PON 1"], interfaces=["EPON0/1"]),
            {"1": "EPON0/1"})

    def test_a_box_that_walks_NOTHING_still_gets_a_name(self):
        # Four OLTs on this fleet walk no ifTable at all. `port_label` is the floor.
        self.assertEqual(fiber.port_display("pon", "2", None), "PON 2")
        self.assertEqual(fiber.port_display("pon", "9", {"1": "EPON0/1"}), "PON 9")

    def test_only_a_PON_is_renamed(self):
        # A `port`'s ref is ALREADY the box's own string, and a leg or an input is
        # plastic nothing walks — none of them may be reached by a PON's names.
        names = {"1": "EPON0/1"}
        self.assertEqual(fiber.port_display("port", "GE0/1", names), "GE0/1")
        self.assertEqual(fiber.port_display("leg", "1", names), "leg 1")
        self.assertEqual(fiber.port_display("in", "1", names), "input")

    def test_the_pseudo_interfaces_are_not_names_for_anything(self):
        self.assertEqual(fiber.pon_names(interfaces=["EPON0/1:5", "EPON01ONU3"]), {})


class ConnectedSpanTest(unittest.TestCase):

    # RECORDED GLASS WINS: the map stands a dependency chord down once its pair is
    # joined here, and hangs the chord's rate chip on the sheath this names.

    # HALIYA-WAN-SW ==tail== JC1 ==main 6F== JC2 ==tail== SRPL-OLT, off the live fleet.
    RUN = [{"id": 86, "cores": 1, "a_point": ("device", 248), "b_point": ("device", 27)},
           {"id": 85, "cores": 6, "a_point": ("device", 248), "b_point": ("device", 249)},
           {"id": 90, "cores": 1, "a_point": ("device", 249), "b_point": ("device", 32)}]
    JCS = {("device", 248), ("device", 249)}

    PAIR = frozenset((("device", 27), ("device", 32)))

    def test_the_BIGGEST_SHEATH_on_the_run_carries_the_label(self):
        # Not either 1F tail: they are our own plumbing, 26 m long, and unreadable at
        # the zoom somebody reads a trunk at.
        self.assertEqual(
            fiber.connected_spans(self.RUN, self.JCS)[self.PAIR]["label"], 85)

    def test_THE_WHOLE_RUN_comes_back_because_THE_MAP_DRAWS_IT(self):
        # The chord this replaces spanned both boxes, so every sheath under it has to
        # light — `main` alone leaves the tails dark and the line stops at a closure.
        self.assertEqual(
            sorted(fiber.connected_spans(self.RUN, self.JCS)[self.PAIR]["path"]),
            [85, 86, 90])

    def test_a_pair_with_NO_glass_between_them_is_simply_absent(self):
        self.assertNotIn(self.PAIR, fiber.connected_spans(self.RUN[:1], self.JCS))

    def test_the_light_does_NOT_run_on_through_GEAR(self):
        # A closure is where a sheath is opened; a switch is where the light stops.
        self.assertNotIn(self.PAIR, fiber.connected_spans(self.RUN, set()))

    def test_a_DIRECT_cable_names_itself_and_is_its_own_whole_run(self):
        spans = fiber.connected_spans(
            [{"id": 78, "cores": 4, "a_point": ("device", 35),
              "b_point": ("device", 11)}])
        got = spans[frozenset((("device", 35), ("device", 11)))]
        self.assertEqual((got["label"], got["path"]), (78, [78]))

    def test_parallel_cables_between_one_pair_pick_the_bigger(self):
        spans = fiber.connected_spans(
            [{"id": 1, "cores": 2, "a_point": ("device", 1), "b_point": ("device", 2)},
             {"id": 2, "cores": 12, "a_point": ("device", 1), "b_point": ("device", 2)}])
        got = spans[frozenset((("device", 1), ("device", 2)))]
        # The chosen run's OWN cable, never a path spliced from two answers.
        self.assertEqual((got["label"], got["path"]), (2, [2]))

    def test_a_TIE_breaks_on_the_lower_id_so_the_map_does_not_flicker(self):
        spans = fiber.connected_spans(
            [{"id": 9, "cores": 6, "a_point": ("device", 1), "b_point": ("device", 7)},
             {"id": 4, "cores": 6, "a_point": ("device", 7), "b_point": ("device", 2)}],
            {("device", 7)})
        self.assertEqual(spans[frozenset((("device", 1), ("device", 2)))]["label"], 4)

    def test_the_PAIRS_answer_never_depends_on_the_LABELLING_one(self):
        # `undrawn` hands over cables carrying nothing but their two ends.
        bare = [{"a_point": ("device", 1), "b_point": ("device", 7)},
                {"a_point": ("device", 7), "b_point": ("device", 2)}]
        self.assertIn(frozenset((("device", 1), ("device", 2))),
                      fiber.connected_points(bare, {("device", 7)}))


class SegmentModelTest(unittest.TestCase):

    def test_the_double_booking_checker_is_GONE_not_merely_passing(self):
        for name in ("core_path", "core_faults", "CORE_FAULTS", "splice_refusal",
                     "SPLICE_REFUSAL_TEXT"):
            self.assertFalse(hasattr(fiber, name),
                             f"fiber.{name} came back — the segment model makes"
                             " it unrepresentable, so it can only mislead")


class JointRefusalTest(unittest.TestCase):

    def setUp(self):
        self.cables = {
            1: _cable(1, ("device", 10), ("device", 20)),
            2: _cable(2, ("device", 20), ("device", 30)),
            3: _cable(3, ("device", 40), ("device", 50)),
        }
        self.here = ("device", 20)

    def test_a_fibre_that_does_not_END_here_cannot_be_joined_here(self):
        self.assertEqual(
            fiber.joint_refusal((1, 1), (3, 1), self.here, self.cables, {}),
            "absent")

    def test_a_fibre_cannot_be_joined_to_itself(self):
        self.assertEqual(
            fiber.joint_refusal((1, 4), (1, 4), self.here, self.cables, {}),
            "self")

    def test_ONE_fibre_joins_exactly_ONE_fibre(self):
        taken = {(1, 1): {"id": 99}}
        self.assertEqual(
            fiber.joint_refusal((1, 1), (2, 1), self.here, self.cables, taken),
            "taken")
        self.assertEqual(
            fiber.joint_refusal((2, 1), (1, 1), self.here, self.cables, taken),
            "taken")

    def test_the_SAME_core_number_of_two_cables_is_the_ordinary_case(self):
        self.assertIsNone(
            fiber.joint_refusal((1, 7), (2, 7), self.here, self.cables, {}))

    def test_a_U_TURN_within_one_cable_is_allowed_and_reported_later(self):
        self.assertIsNone(
            fiber.joint_refusal((1, 3), (1, 9), self.here, self.cables, {}))

    def test_a_TERMINATION_is_checked_the_same_way(self):
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

    def test_ONE_port_takes_exactly_ONE_fibre(self):
        ports = {fiber.port_slot("pon", "3"): {"id": 7}}
        self.assertEqual(
            fiber.joint_refusal((1, 1), None, self.here, self.cables, {},
                                ("pon", "3"), ports),
            "port_taken")
        self.assertIsNone(
            fiber.joint_refusal((1, 1), None, self.here, self.cables, {},
                                ("pon", "4"), ports))

    def test_ONE_SOCKET_TYPED_TWO_WAYS_IS_STILL_ONE_SOCKET(self):
        # An SNMP-silent box gets its ports typed by hand, so `GE0/5` and `ge0/5  `
        # WILL both be entered. They are one port or the free-text field is a way to
        # double-book a socket.
        ports = {fiber.port_slot("port", "GE0/5 INPUT"): {"id": 9}}
        self.assertEqual(
            fiber.joint_refusal((1, 1), None, self.here, self.cables, {},
                                ("port", " ge0/5   input "), ports),
            "port_taken")
        self.assertIsNone(
            fiber.joint_refusal((1, 1), None, self.here, self.cables, {},
                                ("port", "GE0/6"), ports))

    def test_a_port_belongs_to_a_TERMINATION_never_to_a_splice(self):
        self.assertEqual(
            fiber.joint_refusal((1, 1), (2, 1), self.here, self.cables, {},
                                ("pon", "3"), {}),
            "port_splice")

    def test_a_termination_with_NO_port_stays_ordinary(self):
        self.assertIsNone(
            fiber.joint_refusal((1, 2), None, self.here, self.cables, {},
                                None, {fiber.port_slot("pon", "3"): {"id": 7}}))

    def test_every_refusal_has_a_sentence(self):
        for why in ("absent", "self", "taken", "port_taken", "port_splice"):
            self.assertIn(why, fiber.JOINT_REFUSAL_TEXT)


class ContinuityTest(unittest.TestCase):

    def test_a_TERMINATION_is_not_continuity(self):
        joints = [_joint(("device", 20), (1, 1))]
        self.assertEqual(fiber.continuity(joints), {})
        self.assertEqual(list(fiber.terminations(joints)),
                         [(1, 1, ("device", 20))])

    def test_continuity_is_a_fact_about_an_END_not_about_a_strand(self):
        joints = [_joint(("device", 20), (1, 1), (2, 5)),
                  _joint(("device", 10), (1, 1), (9, 3))]
        joins = fiber.continuity(joints)
        self.assertEqual(joins[(1, 1, ("device", 20))], [(2, 5)])
        self.assertEqual(joins[(1, 1, ("device", 10))], [(9, 3)])

    def test_a_fork_survives_as_far_as_the_trace(self):
        joints = [_joint(("device", 20), (1, 1), (2, 1)),
                  _joint(("device", 20), (1, 1), (3, 1))]
        self.assertEqual(len(fiber.continuity(joints)[(1, 1, ("device", 20))]), 2)


class TraceTest(unittest.TestCase):

    def setUp(self):
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
        forward = fiber.trace(self.cables, self.joints, 1, 1)
        backward = fiber.trace(self.cables, self.joints, 2, 5)
        self.assertEqual(forward["points"], backward["points"])
        self.assertEqual(forward["hops"], backward["hops"])

    def test_without_the_joint_it_stops_at_the_closure(self):
        out = fiber.trace(self.cables, [], 1, 1)
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1])
        self.assertEqual(out["points"], [("device", 10), ("device", 20)])

    def test_a_TERMINATION_is_reported_at_the_end_it_lands_on(self):
        joints = self.joints + [_joint(("device", 30), (2, 5))]
        out = fiber.trace(self.cables, joints, 1, 1)
        self.assertEqual([h["cable_id"] for h in out["hops"]], [1, 2])
        self.assertIsNone(out["ends"][0])
        self.assertIsNotNone(out["ends"][1])

    def test_a_CUSTOMER_is_a_point_like_any_other(self):
        cables = [_cable(1, ("device", 30), ("onu", "AABB")),
                  _cable(2, ("onu", "AABB"), ("onu", "CCDD"))]
        joints = [_joint(("onu", "AABB"), (1, 2), (2, 2))]
        out = fiber.trace(cables, joints, 1, 2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["points"],
                         [("device", 30), ("onu", "AABB"), ("onu", "CCDD")])

    def test_a_FORK_stops_the_walk_and_names_where(self):
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


class PonReachTest(unittest.TestCase):


    def setUp(self):
        self.cables = [_cable(1, ("device", 1), ("device", 2)),
                       _cable(2, ("device", 2), ("device", 3))]

    def test_a_fibre_landed_on_PON_3_puts_the_splitter_on_PON_3(self):
        joints = [
            _joint(("device", 1), (1, 1), port=("pon", 3)),
            _joint(("device", 2), (1, 1), (2, 1)),
            _joint(("device", 3), (2, 1), port=("in", 1)),
        ]
        self.assertEqual(fiber.pon_of_points(self.cables, joints),
                         {("device", 3): (("device", 1), 3)})

    def test_only_where_the_GLASS_ENDS_never_the_closure_it_passes(self):
        joints = [
            _joint(("device", 1), (1, 1), port=("pon", 3)),
            _joint(("device", 2), (1, 1), (2, 1)),
            _joint(("device", 3), (2, 1), port=("in", 1)),
        ]
        self.assertNotIn(("device", 2), fiber.pon_of_points(self.cables, joints))

    def test_TWO_PONs_reaching_one_box_is_reported_not_resolved(self):
        cables = self.cables + [_cable(3, ("device", 1), ("device", 3))]
        joints = [
            _joint(("device", 1), (1, 1), port=("pon", 3)),
            _joint(("device", 2), (1, 1), (2, 1)),
            _joint(("device", 3), (2, 1), port=("in", 1)),
            _joint(("device", 1), (3, 1), port=("pon", 5)),
            _joint(("device", 3), (3, 1), port=("in", 2)),
        ]
        self.assertIsNone(fiber.pon_of_points(cables, joints)[("device", 3)])

    def test_a_termination_with_NO_port_claims_nothing(self):
        joints = [
            _joint(("device", 1), (1, 1)),
            _joint(("device", 2), (1, 1), (2, 1)),
            _joint(("device", 3), (2, 1), port=("in", 1)),
        ]
        self.assertEqual(fiber.pon_of_points(self.cables, joints), {})

    def test_a_FORK_stops_the_walk_rather_than_picking_a_branch(self):
        cables = self.cables + [_cable(3, ("device", 2), ("device", 4))]
        joints = [
            _joint(("device", 1), (1, 1), port=("pon", 3)),
            _joint(("device", 2), (1, 1), (2, 1)),
            _joint(("device", 2), (1, 1), (3, 1)),
        ]
        self.assertEqual(fiber.pon_of_points(cables, joints), {})


class CableNameTest(unittest.TestCase):

    def test_a_cable_must_be_named(self):
        for bad in (None, "", "   "):
            with self.assertRaises(fiber.FiberError):
                fiber.clean_cable_name(bad)

    def test_a_name_is_trimmed_and_bounded(self):
        self.assertEqual(fiber.clean_cable_name("  Haliya trunk "), "Haliya trunk")
        with self.assertRaises(fiber.FiberError):
            fiber.clean_cable_name("x" * (fiber.CABLE_NAME_MAX + 1))

    def test_a_cable_NOBODY_LAID_needs_no_name(self):
        self.assertEqual(fiber.clean_cable_name("", required=False), "")
        self.assertEqual(fiber.clean_cable_name(None, required=False), "")


class PlumbingTest(unittest.TestCase):

    def test_the_cable_a_connect_gesture_writes_is_plumbing(self):
        self.assertTrue(fiber.is_plumbing(
            {"name": "", "cores": 1, "path": None}))

    def test_NAMING_it_makes_it_a_cable(self):
        self.assertFalse(fiber.is_plumbing(
            {"name": "Haliya trunk", "cores": 1, "path": None}))

    def test_so_does_COUNTING_it(self):
        self.assertFalse(fiber.is_plumbing(
            {"name": "", "cores": 12, "path": None}))

    def test_and_so_does_WALKING_it(self):
        self.assertFalse(fiber.is_plumbing(
            {"name": "", "cores": 1, "path": [[1, 2], [3, 4]]}))

    def test_whitespace_is_not_a_name(self):
        self.assertTrue(fiber.is_plumbing(
            {"name": "   ", "cores": 1, "path": None}))


class EveryBoxHasPortsTest(unittest.TestCase):

    def test_a_SWITCH_has_the_ports_it_walks(self):
        self.assertEqual(
            fiber.port_slots("switch", ports=["GE0/1", "GE0/2", "GE0/24"]),
            [("port", "GE0/1"), ("port", "GE0/2"), ("port", "GE0/24")])

    def test_a_router_a_gateway_and_a_CPE_too(self):
        for t in ("router", "gateway", "CPE"):
            self.assertEqual(fiber.port_slots(t, ports=["eth3"]),
                             [("port", "eth3")], f"{t} has no ports")

    def test_an_ENCLOSURE_still_has_none(self):
        for t in ("coupler", "closure", "fdb"):
            self.assertEqual(fiber.port_slots(t, ports=[1, 2]), [], t)

    def test_A_BOX_HAS_KINDS_PLURAL(self):
        # An OLT has PONs AND the GE uplink the trunk lands on. One kind per device
        # type is why "GE0/5 INPUT" — walked, in the database — could not be named,
        # leaving only two ways to record an OLT's uplink: lie, or leave it blank.
        self.assertEqual(fiber.port_kinds_for("OLT"), ("pon", "port"))
        self.assertEqual(fiber.port_kinds_for("splitter"), ("leg",))
        self.assertEqual(fiber.port_kinds_for("switch"), ("port",))
        self.assertEqual(fiber.port_kinds_for("coupler"), ())
        self.assertEqual(fiber.port_kinds_for(None), ())

    def test_there_is_NO_uplink_kind(self):
        # Uplink is a ROLE, not a socket: which GE port is the uplink is decided by
        # what somebody plugged in and changes on a re-patch. The first customer feed
        # on GE0/8 would make the vocabulary a lie.
        self.assertNotIn("uplink", fiber.PORT_KINDS)
        for t in ("OLT", "switch", "router", "splitter"):
            self.assertNotIn("uplink", fiber.port_kinds_for(t), t)

    def test_the_NOUN_differs_because_the_boxes_do(self):
        self.assertEqual(fiber.port_kind_for("OLT"), "pon")
        self.assertEqual(fiber.port_kind_for("splitter"), "leg")
        self.assertEqual(fiber.port_kind_for("switch"), "port")
        self.assertIsNone(fiber.port_kind_for("coupler"))

    def test_an_OLT_offers_its_PONS_AND_its_uplink_ports(self):
        slots = fiber.slots_for("OLT", roster=["EPON0/1", "EPON0/2"],
                                interfaces=["EPON0/1", "GE0/1", "GE0/5 INPUT",
                                            "EPON01ONU3", "Vlan-interface1"])
        self.assertEqual(slots, [("pon", "1"), ("pon", "2"),
                                 ("port", "GE0/1"), ("port", "GE0/5 INPUT")])

    def test_a_PON_INTERFACE_is_never_ALSO_offered_as_a_port(self):
        # One socket may not appear twice under two kinds.
        self.assertIsNone(fiber.if_port_ref("EPON0/1"))
        self.assertIsNone(fiber.if_port_ref("GPON0/1 ANREDDY"))

    def test_an_ONU_pseudo_interface_is_not_a_SOCKET_ON_THE_BOX(self):
        # HLY-OLT-1 walks 200 interfaces of which 176 are these. Offering them would
        # bury the 16 real ports.
        for junk in ("EPON01ONU3", "EPON0/1:3", "EPON08ONU51 hc_nirmala1"):
            self.assertIsNone(fiber.if_port_ref(junk), junk)

    def test_a_VIRTUAL_FAMILY_is_matched_on_the_FIRST_TOKEN_not_a_SUBSTRING(self):
        # Real sockets were refused for what their DESCRIPTION said: `LAN-1
        # HILLCOLONY` and `LAN-2 PYLON COLONY` were both dropped because "co-LO-ny"
        # contains "lo".
        for real in ("LAN-1 HILLCOLONY", "LAN-2 PYLON COLONY", "ether8 LOCAL CH",
                     "GE0/5 INPUT"):
            self.assertEqual(fiber.if_port_ref(real), real, real)
        for virtual in ("Vlan-interface1", "vlan1.1", "lo", "lo0", "loopback0",
                        "Null0", "bridge1-WAN", "mgmt0", "mng0", "default",
                        "port-channel 3"):
            self.assertIsNone(fiber.if_port_ref(virtual), virtual)

    def test_the_INTERFACE_NAME_IS_THE_LABEL(self):
        # "port gigabitEthernet 1/0/5" says it twice; the box's own string is enough.
        self.assertEqual(fiber.port_label("port", "gigabitEthernet 1/0/5"),
                         "gigabitEthernet 1/0/5")
        self.assertEqual(fiber.port_label("port", None), "port")

    def test_a_ports_identity_is_the_BOXS_OWN_STRING(self):
        for name in ("gigabitEthernet 1/0/5", "Te1/0/25", "eth3", "GE0/5 INPUT"):
            self.assertEqual(fiber.if_port_ref(name), name, name)

    def test_TWO_SOCKETS_ENDING_IN_5_STAY_TWO_SOCKETS(self):
        # The defect this replaced: a trailing digit collapsed GigaEthernet0/5 with
        # TGigaEthernet0/5 on EVERY switch on the fleet, and the one it dropped was
        # the SFP+ the trunk fibre actually lands on.
        walked = ["GigaEthernet0/5", "TGigaEthernet0/5", "e0/0/5", "e0/1/5"]
        self.assertEqual(fiber.interface_refs(walked), sorted(walked, key=str.lower))
        self.assertEqual(len({fiber.port_key(n) for n in walked}), 4)

    def test_a_VIRTUAL_interface_is_not_somewhere_to_land_a_FIBRE(self):
        for name in ("Vlan-interface1", "loopback0", "Bridge-Aggregation2",
                     "port-channel 3", "NULL0"):
            self.assertIsNone(fiber.if_port_ref(name), name)

    def test_a_HAND_TYPED_port_becomes_a_slot_the_NEXT_person_PICKS(self):
        # This is what bounds free text on an SNMP-silent box: drift is possible only
        # on the FIRST entry for a port, and it self-corrects once the list has one.
        self.assertEqual(fiber.interface_refs([], ["GE0/5"]), ["GE0/5"])
        # ...and the WALKED spelling wins on a tie — the box's string is the authority.
        self.assertEqual(fiber.interface_refs(["GE0/5 INPUT"], ["ge0/5  input"]),
                         ["GE0/5 INPUT"])

    def test_a_port_number_is_UNBOUNDED_because_nobody_holds_the_count(self):
        self.assertIsNone(fiber.port_bound("port"))
        self.assertIsNone(fiber.port_bound("pon"))
        self.assertEqual(fiber.port_bound("leg", split_ratio=8), 8)


class UndrawnTest(unittest.TestCase):

    def test_a_declared_edge_with_no_cable_is_offered(self):
        self.assertEqual(
            fiber.undrawn([(("device", 1), ("device", 2))], []),
            [(("device", 1), ("device", 2))])

    def test_a_connection_ALREADY_RECORDED_is_never_restated(self):
        cables = [{"a_point": ("device", 1), "b_point": ("device", 2)}]
        self.assertEqual(fiber.undrawn([(("device", 1), ("device", 2))], cables), [])

    def test_it_does_not_care_which_END_the_cable_was_drawn_from(self):
        cables = [{"a_point": ("device", 2), "b_point": ("device", 1)}]
        self.assertEqual(fiber.undrawn([(("device", 1), ("device", 2))], cables), [])

    def test_one_pair_is_offered_ONCE(self):
        declared = [(("device", 1), ("device", 2)),
                    (("device", 2), ("device", 1))]
        self.assertEqual(len(fiber.undrawn(declared, [])), 1)

    def test_the_ORDER_GIVEN_survives(self):
        declared = [(("device", 1), ("device", 9)),
                    (("device", 1), ("device", 3))]
        self.assertEqual([b for _, b in fiber.undrawn(declared, [])],
                         [("device", 9), ("device", 3)])

    def test_glass_recorded_THROUGH_A_CLOSURE_is_recorded(self):
        cables = [{"a_point": ("device", 1), "b_point": ("device", 7)},
                  {"a_point": ("device", 7), "b_point": ("device", 2)}]
        self.assertEqual(
            fiber.undrawn([(("device", 1), ("device", 2))], cables,
                          {("device", 7)}),
            [])

    def test_a_CHAIN_of_closures_collapses_too(self):
        cables = [{"a_point": ("device", 1), "b_point": ("device", 7)},
                  {"a_point": ("device", 7), "b_point": ("device", 8)},
                  {"a_point": ("device", 8), "b_point": ("device", 2)}]
        self.assertEqual(
            fiber.undrawn([(("device", 1), ("device", 2))], cables,
                          {("device", 7), ("device", 8)}),
            [])

    def test_a_run_through_ANOTHER_BOX_does_NOT_collapse(self):
        cables = [{"a_point": ("device", 1), "b_point": ("device", 5)},
                  {"a_point": ("device", 5), "b_point": ("device", 2)}]
        self.assertEqual(
            fiber.undrawn([(("device", 1), ("device", 2))], cables, set()),
            [(("device", 1), ("device", 2))])


class FeedMapTest(unittest.TestCase):

    def test_a_chain_is_ordered_from_the_gear_outwards(self):
        feed = fiber.feed_map([(20, 30), (10, 20)], roots={10})
        self.assertEqual(feed, {20: 10, 30: 20})

    def test_the_ROOTS_are_never_given_a_feed(self):
        feed = fiber.feed_map([(10, 20), (10, 11)], roots={10, 11})
        self.assertNotIn(10, feed)
        self.assertNotIn(11, feed)

    def test_what_the_walk_never_REACHES_simply_has_no_feed(self):
        feed = fiber.feed_map([(50, 51)], roots={10})
        self.assertEqual(feed, {})

    def test_a_RING_still_resolves_and_does_not_spin(self):
        runs = [(10, 20), (20, 30), (30, 10)]
        first = fiber.feed_map(runs, roots={10})
        self.assertEqual(first, fiber.feed_map(list(reversed(runs)), roots={10}))
        self.assertEqual(first, {20: 10, 30: 10})

    def test_a_box_spliced_to_itself_is_ignored_rather_than_looped(self):
        self.assertEqual(fiber.feed_map([(10, 10)], roots={10}), {})

    def test_the_SHORTEST_path_back_to_gear_wins(self):
        feed = fiber.feed_map([(10, 20), (20, 30), (10, 30)], roots={10})
        self.assertEqual(feed[30], 10)

    def test_the_flood_runs_DOWN_the_gear_tree_not_from_the_nearest_gear(self):
        # The live defect: switch 10 - JC1 - JC-6 - JC-3 - mainJC2 - OLT 11. JC-3
        # sits two hops from the OLT and three from the switch that feeds it, so the
        # blind flood claimed it from the OLT side and its chain read "fed from
        # SRPL-OLT" — an OLT feeding its own uplink.
        edges = [(10, 20), (20, 30), (30, 40), (40, 50), (50, 11)]
        blind = fiber.feed_map(edges, roots={10, 11})
        self.assertEqual(blind[40], 50)
        feed = fiber.feed_map(edges, roots={10, 11}, rank={10: 0, 11: 1})
        self.assertEqual(feed[40], 30)
        self.assertEqual(feed[50], 40)
        # …and the wave passes THROUGH the OLT: it is fed through the glass, and
        # anything hanging beyond it is fed FROM it, not from the distant switch.
        self.assertEqual(feed[11], 50)
        beyond = fiber.feed_map(edges + [(11, 60)], roots={10, 11},
                                rank={10: 0, 11: 1})
        self.assertEqual(beyond[60], 11)

    def test_a_deeper_root_still_seeds_a_component_the_shallow_flood_never_reaches(self):
        # badri_fiber's shape: OLT uplinks are not in the glass, so each OLT is its
        # own island and must still source it — rank narrows nothing there.
        feed = fiber.feed_map([(10, 20), (11, 60)], roots={10, 11},
                              rank={10: 0, 11: 1})
        self.assertEqual(feed[60], 11)
        self.assertEqual(feed[20], 10)

    def test_equal_ranks_keep_the_plain_nearest_gear_flood(self):
        edges = [(10, 20), (20, 30), (30, 40), (40, 50), (50, 11)]
        blind = fiber.feed_map(edges, roots={10, 11})
        ranked = fiber.feed_map(edges, roots={10, 11}, rank={10: 0, 11: 0})
        self.assertEqual(blind, ranked)


if __name__ == "__main__":
    unittest.main()
