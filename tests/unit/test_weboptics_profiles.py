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
    out = dict(BUILTIN_SPECS["dbc"])
    out["name"] = "testvendor"
    out.update(over)
    return out


class VocabularyTest(unittest.TestCase):

    def test_the_builtin_validates_against_its_own_cleaner(self):
        prof = builtin("dbc")
        self.assertEqual(prof.name, "dbc")
        self.assertEqual(prof.optics_path, "/action/onuopmdiag.html")
        self.assertEqual(prof.charset, "gb2312")
        self.assertTrue(prof.rotates_key)

    def test_a_full_url_is_refused_not_stripped(self):
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(optics_path="http://10.0.0.1/action/opm.html"))
        self.assertIn("not a full URL", str(e.exception))

    def test_a_protocol_relative_path_is_refused(self):
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(optics_path="//host/opm.html"))

    def test_traversal_is_refused(self):
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(login_path="/a/../../etc/passwd"))

    def test_an_unknown_column_is_refused(self):
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(columns={**BUILTIN_SPECS["dbc"]["columns"], "chlorine": "Cl"}))
        self.assertIn("unknown column", str(e.exception))

    def test_a_recipe_that_cannot_find_rx_is_refused(self):
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
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(charset="gb2313"))

    def test_an_unknown_session_strategy_is_refused(self):
        with self.assertRaises(InventoryError):
            clean_web_optics_profile_payload(spec(session="magic"))

    def test_onu_index_shape_demands_a_pon_label(self):
        with self.assertRaises(InventoryError) as e:
            clean_web_optics_profile_payload(
                spec(onu_id_shape="onu-index", pon_label=""))
        self.assertIn("pon_label", str(e.exception))

    def test_a_stored_row_is_re_validated_on_read(self):
        bad = dict(BUILTIN_SPECS["dbc"], optics_path="http://elsewhere/x")
        with self.assertRaises(InventoryError):
            profile_from_spec("dbc", bad)


class ProfileSetTest(unittest.TestCase):

    def test_a_builtin_is_in_force_with_no_rows_at_all(self):
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
        s = ProfileSet.build([{"org_id": None, "name": "dbc", "enabled": False,
                               "spec": BUILTIN_SPECS["dbc"]}])
        self.assertIsNone(s.resolve("byreddy", "dbc"))
        self.assertEqual(s.names(), set())

    def test_a_row_that_no_longer_validates_is_skipped_not_applied(self):
        s = ProfileSet.build([{"org_id": None, "name": "dbc", "enabled": True,
                               "spec": {"optics_path": "nonsense"}}])
        self.assertEqual(s.resolve("byreddy", "dbc").optics_path,
                         "/action/onuopmdiag.html")

    def test_an_unknown_vendor_resolves_to_nothing(self):
        self.assertIsNone(ProfileSet.build([]).resolve("byreddy", "vsol"))
        self.assertIsNone(ProfileSet.build([]).resolve("byreddy", ""))


REORDERED = """<html><body><table>
<tr><td>RX Power</td><td>ONU ID</td><td>MAC Address</td></tr>
<tr><td>-21.40</td><td>GPON0/2:7</td><td>AA:BB:CC:DD:EE:01</td></tr>
</table></body></html>"""

HEADLESS = """<html><body><table>
<tr><td>GPON0/2:7</td><td>AA:BB:CC:DD:EE:01</td><td>-21.40</td></tr>
</table></body></html>"""


class ColumnMappingTest(unittest.TestCase):
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
        prof = self._profile(
            columns={"onu_ref": "ONU ID", "serial": "MAC Address", "rx_dbm": "RX Power"},
            column_order=[])
        self.assertEqual(parse_optics_table(HEADLESS, prof), [])

    def test_a_heading_matches_on_its_prefix(self):
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
        self.assertEqual(parse_optics_table(ONU_ONLY, self.prof, pon=None), [])

    def test_a_vendor_with_no_voltage_column_still_reports_optics(self):
        self.assertIsNotNone(parse_optics_table(ONU_ONLY, self.prof, pon=3)[0]["rx_dbm"])

    def test_the_dbc_guard_stays_strict_because_its_page_has_the_column(self):
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
        self.assertEqual(row["serial"], "AA:BB:CC:DD:EE:04")
        self.assertEqual(row["distance_m"], 1200)


class DiagnoseLoginTest(unittest.TestCase):
    EMPTY_KEY = ("<html><head><title>OLT Web Management Interface</title></head>"
                 "<body><script>var SessionKey = document.createElement('input');"
                 "SessionKey.name = 'SessionKey'; SessionKey.value = '';"
                 "</script></body></html>")

    def test_an_empty_token_does_not_report_a_contradiction(self):
        msg = diagnose_login(self.EMPTY_KEY)
        self.assertIn("EMPTY SessionKey", msg)
        self.assertNotIn("but not as [js-single-quote]", msg)

    def test_it_names_both_live_causes_without_picking_one(self):
        msg = diagnose_login(self.EMPTY_KEY)
        self.assertIn("password was refused", msg)
        self.assertIn("single web session", msg)

    def test_an_unknown_markup_shape_still_reports_the_shape(self):
        html = "<html><body>SessionKey=<input name='SessionKey'></body></html>"
        self.assertIn("but not as [js-single-quote]", diagnose_login(html))

    def test_a_login_page_is_still_called_a_refused_password(self):
        html = "<html><body><form action='/action/login.html'>password</form></body></html>"
        self.assertIn("password was refused", diagnose_login(html))


if __name__ == "__main__":
    unittest.main()
