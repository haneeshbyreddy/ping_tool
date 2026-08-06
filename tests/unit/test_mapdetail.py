"""Server-wide map detail: validation, the ordering invariant, sparse storage.

Pinned here rather than left to the SPA because central validates on BOTH the
write and the read — a row hand-edited in SQLite must not be able to reach a
browser in a state the map can't draw.
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import mapdetail  # noqa: E402

_SPA_DETAIL = (Path(__file__).resolve().parents[2]
               / "web" / "src" / "map" / "detail.ts")


class _FakeStore:
    """Minimal get_setting/set_setting pair — mapdetail.py touches nothing else."""

    def __init__(self, initial=None):
        self.settings = dict(initial or {})

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        if value is None:
            self.settings.pop(key, None)
        else:
            self.settings[key] = value


class CleanTest(unittest.TestCase):
    def test_keeps_valid_zooms(self):
        payload = {"labels": 10, "passives": 12, "subscribers": 13,
                   "subscriber_names": 16, "drop_lines": 17}
        self.assertEqual(mapdetail.clean(payload), payload)

    def test_clamps_to_the_offered_span(self):
        got = mapdetail.clean({"labels": -5, "subscribers": 99, "drop_lines": 99})
        self.assertEqual(got["labels"], mapdetail.MIN_ZOOM)
        self.assertEqual(got["subscribers"], mapdetail.MAX_ZOOM)

    def test_a_missing_field_falls_back_PER_FIELD(self):
        """A row written before a field existed must not discard the fields it
        does carry — which is exactly what every install has stored from before
        `subscriber_names` existed."""
        got = mapdetail.clean({"labels": 9})
        self.assertEqual(got["labels"], 9)
        for k in ("passives", "subscribers", "subscriber_names", "drop_lines"):
            self.assertEqual(got[k], mapdetail.DEFAULTS[k], k)

    def test_a_pre_existing_row_keeps_its_values_and_gains_the_new_one(self):
        """The upgrade path: a row stored before subscriber names existed must
        not have its tuning reset by the field arriving."""
        got = mapdetail.clean({"labels": 10, "subscribers": 12, "drop_lines": 15})
        self.assertEqual(got["labels"], 10)
        self.assertEqual(got["subscribers"], 12)
        self.assertEqual(got["drop_lines"], 15)
        self.assertEqual(got["subscriber_names"],
                         mapdetail.DEFAULTS["subscriber_names"])

    def test_junk_degrades_to_the_default_never_to_None(self):
        """A None/NaN reaching the SPA makes every `zoom >= n` false, which reads
        as 'the layer is broken' rather than as a bad setting."""
        for junk in ("14", None, True, [], {}, float("nan"), float("inf")):
            got = mapdetail.clean({"subscribers": junk})
            self.assertIsInstance(got["subscribers"], int, junk)
            self.assertGreaterEqual(got["subscribers"], mapdetail.MIN_ZOOM, junk)

    def test_drop_lines_can_never_sit_below_subscribers(self):
        """Not cosmetic: the SPA draws a drop line only where the mark it points
        at is already drawn, so a lower floor here does NOTHING at all."""
        got = mapdetail.clean({"labels": 12, "subscribers": 15, "drop_lines": 11})
        self.assertEqual(got["drop_lines"], 15)

    def test_subscriber_names_can_never_sit_below_subscribers(self):
        """Same invariant, same reason: a name rides the mark, so a floor below
        the marks' would label pins that aren't drawn — i.e. do nothing."""
        got = mapdetail.clean({"subscribers": 15, "subscriber_names": 10})
        self.assertEqual(got["subscriber_names"], 15)

    def test_raising_subscribers_pushes_BOTH_dependents_up_with_it(self):
        got = mapdetail.clean({"labels": 12, "subscribers": 18,
                               "subscriber_names": 17, "drop_lines": 16})
        self.assertEqual(got["drop_lines"], 18)
        self.assertEqual(got["subscriber_names"], 18)

    def test_drop_lines_can_never_sit_below_PASSIVES_either(self):
        """A drop line has TWO ends. The subscriber's diamond is one; the
        splitter it runs to is the other, and it now has a floor of its own. A
        dotted line to a point where no pin is drawn reads as a rendering fault
        rather than as a setting."""
        got = mapdetail.clean({"passives": 16, "subscribers": 12, "drop_lines": 13})
        self.assertEqual(got["drop_lines"], 16)

    def test_passives_do_NOT_drag_subscriber_names_up(self):
        """A name rides the subscriber MARK; it has nothing to do with plant.
        Coupling the two would repair a setting that was never broken."""
        got = mapdetail.clean({"passives": 18, "subscribers": 12,
                               "subscriber_names": 13})
        self.assertEqual(got["subscriber_names"], 13)

    def test_passives_are_INDEPENDENT_of_subscribers(self):
        """Neither rides the other's mark, so plant may be drawn later than the
        drops hanging off it, or earlier — which is the shipped default."""
        got = mapdetail.clean({"passives": 17, "subscribers": 10})
        self.assertEqual((got["passives"], got["subscribers"]), (17, 10))

    def test_a_row_stored_before_passives_existed_keeps_its_tuning(self):
        """The upgrade path for every install that already saved this setting:
        the new field arrives at its default and repairs `drop_lines` only if it
        would now point at plant that isn't drawn."""
        got = mapdetail.clean({"labels": 10, "subscribers": 12,
                               "subscriber_names": 15, "drop_lines": 15})
        self.assertEqual(got["labels"], 10)
        self.assertEqual(got["subscribers"], 12)
        self.assertEqual(got["subscriber_names"], 15)
        self.assertEqual(got["drop_lines"], 15)
        self.assertEqual(got["passives"], mapdetail.DEFAULTS["passives"])

    def test_names_and_drop_lines_are_INDEPENDENT_of_each_other(self):
        """They share a floor, not an ordering. A name rides the MARK and a rate
        chip rides the LINE, so naming subscribers without drawing their drop
        lines is a legitimate setting — and must not be quietly repaired."""
        got = mapdetail.clean({"subscribers": 12, "subscriber_names": 13,
                               "drop_lines": 18})
        self.assertEqual(got["subscriber_names"], 13)
        self.assertEqual(got["drop_lines"], 18)


class StorageTest(unittest.TestCase):
    def test_defaults_are_stored_as_NOTHING(self):
        """So an install nobody has touched keeps following the shipped values,
        and a later change to them still reaches everyone who never expressed an
        opinion. Same sparse-storage rule as theme.save."""
        store = _FakeStore()
        mapdetail.save(store, dict(mapdetail.DEFAULTS))
        self.assertNotIn(mapdetail.SETTING_KEY, store.settings)

    def test_saving_defaults_CLEARS_a_previously_stored_row(self):
        store = _FakeStore()
        mapdetail.save(store, {"labels": 8, "subscribers": 8, "drop_lines": 8})
        self.assertIn(mapdetail.SETTING_KEY, store.settings)
        mapdetail.save(store, dict(mapdetail.DEFAULTS))  # the Reset button
        self.assertNotIn(mapdetail.SETTING_KEY, store.settings)

    def test_round_trip(self):
        store = _FakeStore()
        saved = mapdetail.save(store, {"labels": 10, "subscribers": 12,
                                       "subscriber_names": 15, "drop_lines": 13})
        self.assertEqual(mapdetail.load(store), saved)

    def test_load_of_an_untouched_install_is_the_defaults(self):
        self.assertEqual(mapdetail.load(_FakeStore()), mapdetail.DEFAULTS)

    def test_a_hand_edited_row_is_re_validated_on_READ(self):
        """The write path is not the only way a value gets into app_settings."""
        store = _FakeStore({mapdetail.SETTING_KEY: json.dumps(
            {"labels": 99, "subscribers": 17, "drop_lines": 5})})
        got = mapdetail.load(store)
        self.assertEqual(got["labels"], mapdetail.MAX_ZOOM)
        self.assertGreaterEqual(got["drop_lines"], got["subscribers"])

    def test_unparseable_row_degrades_to_defaults_rather_than_raising(self):
        store = _FakeStore({mapdetail.SETTING_KEY: "not json{"})
        self.assertEqual(mapdetail.load(store), mapdetail.DEFAULTS)


class SpaAgreementTest(unittest.TestCase):
    """The defaults and the accepted span are mirrored in web/src/map/detail.ts —
    the SPA needs a value to draw with before /api/orgs resolves, and central
    needs one to validate against without asking a browser. A drift means the map
    renders one thing and the settings form reports another, which is exactly the
    class of bug the theme allowlist test exists to catch."""

    def test_defaults_match_the_SPA(self):
        src = _SPA_DETAIL.read_text(encoding="utf-8")
        block = re.search(r"DETAIL_DEFAULTS: MapDetail = \{(.*?)\}", src, re.S)
        self.assertIsNotNone(block, "DETAIL_DEFAULTS not found in detail.ts")
        spa = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block.group(1))}
        self.assertEqual(spa, mapdetail.DEFAULTS)

    def test_offered_span_matches_the_SPA(self):
        src = _SPA_DETAIL.read_text(encoding="utf-8")
        lo = int(re.search(r"DETAIL_MIN = (\d+)", src).group(1))
        hi = int(re.search(r"DETAIL_MAX = (\d+)", src).group(1))
        self.assertEqual((lo, hi), (mapdetail.MIN_ZOOM, mapdetail.MAX_ZOOM))

    def test_every_field_is_OFFERED_a_row_in_the_settings_card(self):
        """A field central validates but the form never shows is a knob nobody
        can reach — the same class of dead control as one that no-ops, and the
        likelier mistake when adding a layer."""
        src = _SPA_DETAIL.read_text(encoding="utf-8")
        block = re.search(r"DETAIL_ROWS[^=]*= \[(.*?)\n\]", src, re.S)
        self.assertIsNotNone(block, "DETAIL_ROWS not found in detail.ts")
        rows = set(re.findall(r'key: "(\w+)"', block.group(1)))
        self.assertEqual(rows, set(mapdetail.FIELDS))


if __name__ == "__main__":
    unittest.main()
