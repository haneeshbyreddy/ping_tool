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
        payload = {"labels": 10, "passives": 12, "passive_names": 14,
                   "subscribers": 13, "subscriber_names": 16, "drop_lines": 17,
                   "line_labels": 11}
        self.assertEqual(mapdetail.clean(payload), payload)

    def test_clamps_to_the_offered_span(self):
        got = mapdetail.clean({"labels": -5, "subscribers": 99, "drop_lines": 99})
        self.assertEqual(got["labels"], mapdetail.MIN_ZOOM)
        self.assertEqual(got["subscribers"], mapdetail.MAX_ZOOM)

    def test_a_missing_field_falls_back_PER_FIELD(self):
        got = mapdetail.clean({"labels": 9})
        self.assertEqual(got["labels"], 9)
        for k in ("passives", "subscribers", "subscriber_names", "drop_lines"):
            self.assertEqual(got[k], mapdetail.DEFAULTS[k], k)

    def test_a_pre_existing_row_keeps_its_values_and_gains_the_new_one(self):
        got = mapdetail.clean({"labels": 10, "subscribers": 12, "drop_lines": 15})
        self.assertEqual(got["labels"], 10)
        self.assertEqual(got["subscribers"], 12)
        self.assertEqual(got["drop_lines"], 15)
        self.assertEqual(got["subscriber_names"],
                         mapdetail.DEFAULTS["subscriber_names"])

    def test_junk_degrades_to_the_default_never_to_None(self):
        for junk in ("14", None, True, [], {}, float("nan"), float("inf")):
            got = mapdetail.clean({"subscribers": junk})
            self.assertIsInstance(got["subscribers"], int, junk)
            self.assertGreaterEqual(got["subscribers"], mapdetail.MIN_ZOOM, junk)

    def test_drop_lines_can_never_sit_below_subscribers(self):
        got = mapdetail.clean({"labels": 12, "subscribers": 15, "drop_lines": 11})
        self.assertEqual(got["drop_lines"], 15)

    def test_subscriber_names_can_never_sit_below_subscribers(self):
        got = mapdetail.clean({"subscribers": 15, "subscriber_names": 10})
        self.assertEqual(got["subscriber_names"], 15)

    def test_raising_subscribers_pushes_BOTH_dependents_up_with_it(self):
        got = mapdetail.clean({"labels": 12, "subscribers": 18,
                               "subscriber_names": 17, "drop_lines": 16})
        self.assertEqual(got["drop_lines"], 18)
        self.assertEqual(got["subscriber_names"], 18)

    def test_drop_lines_can_never_sit_below_PASSIVES_either(self):
        got = mapdetail.clean({"passives": 16, "subscribers": 12, "drop_lines": 13})
        self.assertEqual(got["drop_lines"], 16)

    def test_passives_do_NOT_drag_subscriber_names_up(self):
        got = mapdetail.clean({"passives": 18, "subscribers": 12,
                               "subscriber_names": 13})
        self.assertEqual(got["subscriber_names"], 13)

    def test_passives_are_INDEPENDENT_of_subscribers(self):
        got = mapdetail.clean({"passives": 17, "subscribers": 10})
        self.assertEqual((got["passives"], got["subscribers"]), (17, 10))

    def test_a_row_stored_before_passives_existed_keeps_its_tuning(self):
        got = mapdetail.clean({"labels": 10, "subscribers": 12,
                               "subscriber_names": 15, "drop_lines": 15})
        self.assertEqual(got["labels"], 10)
        self.assertEqual(got["subscribers"], 12)
        self.assertEqual(got["subscriber_names"], 15)
        self.assertEqual(got["drop_lines"], 15)
        self.assertEqual(got["passives"], mapdetail.DEFAULTS["passives"])

    def test_a_plant_plate_can_never_sit_below_its_own_PIN(self):
        got = mapdetail.clean({"passives": 16, "passive_names": 11})
        self.assertEqual(got["passive_names"], 16)

    def test_plant_plates_are_INDEPENDENT_of_device_names(self):
        got = mapdetail.clean({"labels": 18, "passives": 13, "passive_names": 13})
        self.assertEqual(got["passive_names"], 13)

    def test_plant_plates_do_NOT_drag_the_pins_up_with_them(self):
        got = mapdetail.clean({"passives": 13, "passive_names": 18})
        self.assertEqual(got["passives"], 13)

    def test_a_row_stored_before_plant_plates_had_a_row_lands_on_ITS_passives(self):
        got = mapdetail.clean({"labels": 13, "passives": 14, "subscribers": 14,
                               "subscriber_names": 19, "drop_lines": 19})
        self.assertEqual(got["passive_names"], 14)

    def test_line_labels_ships_as_a_KNOB_that_changes_nothing(self):
        self.assertEqual(mapdetail.DEFAULTS["line_labels"], mapdetail.MIN_ZOOM)

    def test_line_labels_takes_NO_floor_from_another_row(self):
        got = mapdetail.clean({"passives": 18, "subscribers": 18,
                               "line_labels": 6})
        self.assertEqual(got["line_labels"], 6)

    def test_line_labels_drags_NOTHING_up_with_it(self):
        got = mapdetail.clean({"line_labels": 19, "passives": 13,
                               "subscribers": 14, "drop_lines": 16})
        self.assertEqual(got["passives"], 13)
        self.assertEqual(got["subscribers"], 14)
        self.assertEqual(got["drop_lines"], 16)

    def test_a_row_stored_before_line_labels_existed_keeps_its_tuning(self):
        got = mapdetail.clean({"labels": 10, "passives": 11, "subscribers": 12,
                               "subscriber_names": 15, "drop_lines": 15})
        self.assertEqual(got["labels"], 10)
        self.assertEqual(got["passives"], 11)
        self.assertEqual(got["line_labels"], mapdetail.DEFAULTS["line_labels"])

    def test_names_and_drop_lines_are_INDEPENDENT_of_each_other(self):
        got = mapdetail.clean({"subscribers": 12, "subscriber_names": 13,
                               "drop_lines": 18})
        self.assertEqual(got["subscriber_names"], 13)
        self.assertEqual(got["drop_lines"], 18)


class StorageTest(unittest.TestCase):
    def test_defaults_are_stored_as_NOTHING(self):
        store = _FakeStore()
        mapdetail.save(store, dict(mapdetail.DEFAULTS))
        self.assertNotIn(mapdetail.SETTING_KEY, store.settings)

    def test_saving_defaults_CLEARS_a_previously_stored_row(self):
        store = _FakeStore()
        mapdetail.save(store, {"labels": 8, "subscribers": 8, "drop_lines": 8})
        self.assertIn(mapdetail.SETTING_KEY, store.settings)
        mapdetail.save(store, dict(mapdetail.DEFAULTS))
        self.assertNotIn(mapdetail.SETTING_KEY, store.settings)

    def test_round_trip(self):
        store = _FakeStore()
        saved = mapdetail.save(store, {"labels": 10, "subscribers": 12,
                                       "subscriber_names": 15, "drop_lines": 13})
        self.assertEqual(mapdetail.load(store), saved)

    def test_load_of_an_untouched_install_is_the_defaults(self):
        self.assertEqual(mapdetail.load(_FakeStore()), mapdetail.DEFAULTS)

    def test_a_hand_edited_row_is_re_validated_on_READ(self):
        store = _FakeStore({mapdetail.SETTING_KEY: json.dumps(
            {"labels": 99, "subscribers": 17, "drop_lines": 5})})
        got = mapdetail.load(store)
        self.assertEqual(got["labels"], mapdetail.MAX_ZOOM)
        self.assertGreaterEqual(got["drop_lines"], got["subscribers"])

    def test_unparseable_row_degrades_to_defaults_rather_than_raising(self):
        store = _FakeStore({mapdetail.SETTING_KEY: "not json{"})
        self.assertEqual(mapdetail.load(store), mapdetail.DEFAULTS)


class SpaAgreementTest(unittest.TestCase):
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
        src = _SPA_DETAIL.read_text(encoding="utf-8")
        block = re.search(r"DETAIL_ROWS[^=]*= \[(.*?)\n\]", src, re.S)
        self.assertIsNotNone(block, "DETAIL_ROWS not found in detail.ts")
        rows = set(re.findall(r'key: "(\w+)"', block.group(1)))
        self.assertEqual(rows, set(mapdetail.FIELDS))


if __name__ == "__main__":
    unittest.main()
