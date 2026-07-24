"""Web-UI optics recipes as data: the closed vocabulary and what it refuses.

The whole point of this vocabulary is that a HALF-understood recipe is worse
than none. A profile that scrapes a page it doesn't really understand produces a
confident, wrong dBm — and a wrong dBm sends a splicing crew to a house whose
fibre is fine. So every test here is really the same test: does the thing that
would produce a plausible lie get refused outright.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central.inventory import InventoryError
from wisp.central.weboptics import diagnose_login, parse_optics_table
from wisp.central.weboptics_profiles import (BUILTIN_SPECS, ProfileSet, builtin,
                                             clean_web_optics_profile_payload,
                                             profile_from_spec)


def spec(**over):
    """A valid payload, minus whatever the test breaks."""
    out = dict(BUILTIN_SPECS["dbc"])
    out["name"] = "testvendor"
    out.update(over)
    return out


class VocabularyTest(unittest.TestCase):

    def test_the_builtin_validates_against_its_own_cleaner(self):
        # The one field-verified recipe must survive the rules written for
        # everyone else, or the rules are wrong rather than the recipe.
        prof = builtin("dbc")
        self.assertEqual(prof.name, "dbc")
        self.assertEqual(prof.optics_path, "/action/onuopmdiag.html")
        self.assertEqual(prof.charset, "gb2312")
        self.assertTrue(prof.rotates_key)

    def test_a_full_url_is_refused_not_stripped(self):
        # The tunnel addresses a device by (ip, port, scheme) and takes a PATH.
        # A profile that could name a host would hand back exactly the property
        # that stops this being a lateral-movement primitive.
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(optics_path="http://10.0.0.1/action/opm.html"))
        self.assertIn("not a full URL", str(e.exception))

    def test_a_protocol_relative_path_is_refused(self):
        # "//evil.example/x" is a URL wearing a path's clothes.
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(optics_path="//host/opm.html"))

    def test_traversal_is_refused(self):
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(login_path="/a/../../etc/passwd"))

    def test_an_unknown_column_is_refused(self):
        # Not silently dropped: a heading mapped to a field nothing reads is a
        # recipe the operator believes is complete and isn't.
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(columns={**BUILTIN_SPECS["dbc"]["columns"], "chlorine": "Cl"}))
        self.assertIn("unknown column", str(e.exception))

    def test_a_recipe_that_cannot_find_rx_is_refused(self):
        # Per-ONU received power is the reading this subsystem exists to
        # recover. A profile without it would log successful scrapes forever
        # while the column it was written for stays empty.
        cols = {k: v for k, v in BUILTIN_SPECS["dbc"]["columns"].items() if k != "rx_dbm"}
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(spec(columns=cols, column_order=[]))
        self.assertIn("rx_dbm", str(e.exception))

    def test_a_recipe_with_no_anchor_column_is_refused(self):
        cols = {k: v for k, v in BUILTIN_SPECS["dbc"]["columns"].items() if k != "onu_ref"}
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(spec(columns=cols, column_order=[]))
        self.assertIn("onu_ref", str(e.exception))

    def test_an_unknown_charset_is_refused(self):
        # A typo here presents on screen as "this vendor has no optics".
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(charset="gb2313"))

    def test_an_unknown_session_strategy_is_refused(self):
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(session="magic"))

    def test_onu_index_shape_demands_a_pon_label(self):
        # With this shape the page names only the ONU, so without a label there
        # is no way to say WHICH PON a reading belongs to — and a reading filed
        # under the wrong port is worse than a missing one.
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(onu_id_shape="onu-index", pon_label=""))
        self.assertIn("pon_label", str(e.exception))

    def test_a_stored_row_is_re_validated_on_read(self):
        # The DB is not a trusted channel just because it is on our side of the
        # wire: a hand-edited row gets the identical vocabulary.
        bad = dict(BUILTIN_SPECS["dbc"], optics_path="http://elsewhere/x")
        with self.assertRaises(InventoryError):
            profile_from_spec("dbc", bad)


class ProfileSetTest(unittest.TestCase):

    def test_a_builtin_is_in_force_with_no_rows_at_all(self):
        # An install that has never opened the Settings card keeps working.
        s = ProfileSet.build([])
        self.assertEqual(s.names(), {"dbc"})
        self.assertIsNotNone(s.resolve("byreddy", "dbc"))

    def test_a_stored_row_shadows_the_builtin(self):
        s = ProfileSet.build([{
            "org_id": None, "name": "dbc", "enabled": True,
            "spec": dict(BUILTIN_SPECS["dbc"], optics_path="/action/newpage.html"),
        }])
        self.assertEqual(s.resolve("byreddy", "dbc").optics_path, "/action/newpage.html")

    def test_an_org_row_beats_a_global_one(self):
        rows = [
            {"org_id": None, "name": "dbc", "enabled": True,
             "spec": dict(BUILTIN_SPECS["dbc"], optics_path="/global.html")},
            {"org_id": "byreddy", "name": "dbc", "enabled": True,
             "spec": dict(BUILTIN_SPECS["dbc"], optics_path="/local.html")},
        ]
        s = ProfileSet.build(rows)
        self.assertEqual(s.resolve("byreddy", "dbc").optics_path, "/local.html")
        self.assertEqual(s.resolve("hansa", "dbc").optics_path, "/global.html")

    def test_disabling_a_vendor_does_not_fall_back_to_the_builtin(self):
        # Otherwise the toggle is a lie on exactly the OLTs that shipped with a
        # built-in recipe — the ones an operator would be switching off.
        s = ProfileSet.build([{"org_id": None, "name": "dbc", "enabled": False,
                               "spec": BUILTIN_SPECS["dbc"]}])
        self.assertIsNone(s.resolve("byreddy", "dbc"))
        self.assertEqual(s.names(), set())

    def test_a_row_that_no_longer_validates_is_skipped_not_applied(self):
        # Never a best-effort partial: the built-in stays in force instead.
        s = ProfileSet.build([{"org_id": None, "name": "dbc", "enabled": True,
                               "spec": {"optics_path": "nonsense"}}])
        self.assertEqual(s.resolve("byreddy", "dbc").optics_path,
                         "/action/onuopmdiag.html")

    def test_an_unknown_vendor_resolves_to_nothing(self):
        # "probably C-Data" must never be enough to start POSTing an admin
        # credential at a box we have no recipe for.
        self.assertIsNone(ProfileSet.build([]).resolve("byreddy", "vsol"))
        self.assertIsNone(ProfileSet.build([]).resolve("byreddy", ""))


# A vendor whose table is ordered differently from DBC's, with the SAME headings.
# This is the case the whole by-name mechanism exists for.
REORDERED = """<html><body><table>
<tr><td>RX Power</td><td>ONU ID</td><td>MAC Address</td></tr>
<tr><td>-21.40</td><td>GPON0/2:7</td><td>AA:BB:CC:DD:EE:01</td></tr>
</table></body></html>"""

HEADLESS = """<html><body><table>
<tr><td>GPON0/2:7</td><td>AA:BB:CC:DD:EE:01</td><td>-21.40</td></tr>
</table></body></html>"""


class ColumnMappingTest(unittest.TestCase):
    """By NAME, never by position — a mis-mapped column is a plausible lie."""

    def _profile(self, **over):
        payload = spec(**over)
        clean = clean_web_optics_profile_payload(payload)
        return profile_from_spec(clean["name"], clean["spec"])

    def test_a_reordered_table_still_reads_the_right_column(self):
        prof = self._profile(
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=[])
        rows = parse_optics_table(REORDERED, prof)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rx_dbm"], -21.40)
        self.assertEqual(rows[0]["serial"], "AA:BB:CC:DD:EE:01")

    def test_a_headerless_table_uses_the_declared_order(self):
        prof = self._profile(
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=["onu_ref", "serial", "rx_dbm"])
        rows = parse_optics_table(HEADLESS, prof)
        self.assertEqual(rows[0]["rx_dbm"], -21.40)

    def test_no_header_and_no_declared_order_reads_NOTHING(self):
        # The load-bearing refusal. Guessing at positions here is precisely how
        # transmit power gets reported as received power.
        prof = self._profile(
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=[])
        self.assertEqual(parse_optics_table(HEADLESS, prof), [])

    def test_a_heading_matches_on_its_prefix(self):
        # Real pages decorate: "Distance" + "(m)", "TX Bias" + " Current".
        page = """<html><body><table>
        <tr><td>ONU ID</td><td>MAC Address</td><td>RX Power (dBm)</td></tr>
        <tr><td>GPON0/2:7</td><td>AA:BB:CC:DD:EE:01</td><td>-21.40</td></tr>
        </table></body></html>"""
        prof = self._profile(
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=[])
        self.assertEqual(parse_optics_table(page, prof)[0]["rx_dbm"], -21.40)


ONU_ONLY = """<html><body><table>
<tr><td>ONU ID</td><td>MAC Address</td><td>RX Power</td></tr>
<tr><td>7</td><td>AA:BB:CC:DD:EE:01</td><td>-21.40</td></tr>
</table></body></html>"""


class OnuIndexShapeTest(unittest.TestCase):
    """A page that names only the ONU takes its PON from the page requested."""

    def setUp(self):
        clean = clean_web_optics_profile_payload(spec(
            onu_id_shape="onu-index", pon_label="GPON0/{pon}",
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=[]))
        self.prof = profile_from_spec(clean["name"], clean["spec"])

    def test_the_requested_pon_names_the_slot(self):
        rows = parse_optics_table(ONU_ONLY, self.prof, pon=3)
        self.assertEqual(rows[0]["onu_key"], "3.7")
        self.assertEqual(rows[0]["pon_port"], "GPON0/3")

    def test_without_a_pon_the_row_is_dropped_not_guessed(self):
        # A reading with no slot to merge onto is worth nothing; filed under a
        # guessed port it is worth less than nothing.
        self.assertEqual(parse_optics_table(ONU_ONLY, self.prof, pon=None), [])

    def test_a_vendor_with_no_voltage_column_still_reports_optics(self):
        # The DDM rail guard keys on supply voltage, which every ONU has BY
        # DESIGN — but not every vendor's page prints. Running the check anyway
        # would blank every reading such a vendor ever produces, discarding all
        # the data to guard against a fault we cannot see. An ABSENT column is a
        # fact about the firmware; a MISSING value in a column that exists is a
        # fact about the ONU, and only the second is evidence of a dead sensor.
        self.assertIsNotNone(parse_optics_table(ONU_ONLY, self.prof, pon=3)[0]["rx_dbm"])

    def test_the_dbc_guard_stays_strict_because_its_page_has_the_column(self):
        # The generalisation above must not weaken the fleet it was found on:
        # HILL-OLT-1's railed ONUs (+8.16 dBm reading as the healthiest drop on
        # the PON) are still caught, because the dbc profile maps voltage.
        railed = """<html><body><table>
        <tr><td class='hd'>ONU ID</td><td>MAC Address</td><td>Description</td>
        <td>Distance(m)</td><td>Temperature</td><td>Supply Voltage</td>
        <td>TX Bias Current</td><td>TX Power</td><td>RX Power</td></tr>
        <tr><td>EPON0/1:4</td><td>AA:BB:CC:DD:EE:04</td><td></td><td>1200</td>
        <td>128.00</td><td>6.55</td><td>131.07</td><td>8.16</td><td>8.16</td></tr>
        </table></body></html>"""
        row = parse_optics_table(railed, builtin("dbc"))[0]
        self.assertIsNone(row["rx_dbm"])
        self.assertIsNone(row["tx_dbm"])
        # identity survives — "we could not read this one", not "this is gone"
        self.assertEqual(row["serial"], "AA:BB:CC:DD:EE:04")
        self.assertEqual(row["distance_m"], 1200)


class DiagnoseLoginTest(unittest.TestCase):
    """A diagnosis that contradicts itself is worse than none."""

    # Trimmed from what 8 of 12 fleet OLTs actually served on 2026-07-23, the
    # first sweep after scrape outcomes started being persisted.
    EMPTY_KEY = ("<html><head><title>OLT Web Management Interface</title></head>"
                 "<body><script>var SessionKey = document.createElement('input');"
                 "SessionKey.name = 'SessionKey'; SessionKey.value = '';"
                 "</script></body></html>")

    def test_an_empty_token_does_not_report_a_contradiction(self):
        # `key_shapes` matches the opening quote; the reader needs a non-empty
        # value. So this page used to report "carries SessionKey as
        # [js-single-quote] but not as [js-single-quote]" — true, useless, and
        # printed across most of a fleet at once.
        msg = diagnose_login(self.EMPTY_KEY)
        self.assertIn("EMPTY SessionKey", msg)
        self.assertNotIn("but not as [js-single-quote]", msg)

    def test_it_names_both_live_causes_without_picking_one(self):
        # A refused password and a stolen single session are the two real
        # readings, and asserting either one would send someone to re-type a
        # password that was never wrong.
        msg = diagnose_login(self.EMPTY_KEY)
        self.assertIn("password was refused", msg)
        self.assertIn("single web session", msg)

    def test_an_unknown_markup_shape_still_reports_the_shape(self):
        # The original branch survives for a page whose token really is written
        # some other way — that one IS a parser gap and should say so.
        html = "<html><body>SessionKey=<input name='SessionKey'></body></html>"
        self.assertIn("but not as [js-single-quote]", diagnose_login(html))

    def test_a_login_page_is_still_called_a_refused_password(self):
        html = "<html><body><form action='/action/login.html'>password</form></body></html>"
        self.assertIn("password was refused", diagnose_login(html))


if __name__ == "__main__":
    unittest.main()
