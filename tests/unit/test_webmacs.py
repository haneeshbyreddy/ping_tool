"""The OLT address table: the SUBSCRIBER's own MAC, read off the OLT's web UI.

Fixtures mirror the two builds this was written against, byte-for-byte in the
parts that matter: the C-Data EPON page (Port ID "EPON0/8:38", and a macCount
the OLT fills in itself) and the Syrotech GPON page (Port ID "PON2:ONU36", no
total anywhere on it).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import webmacs  # noqa: E402
from wisp.central import webmacs_profiles as wmp  # noqa: E402
from wisp.central.inventory import InventoryError  # noqa: E402

HEAD = ("<tr><td class='hd'>VLAN ID</td><td class='hd'>MAC Address</td>"
        "<td class='hd'>Type</td><td class='hd'>Port ID</td></tr>")


def row(vlan, mac, kind, port):
    return f"<tr><td>{vlan}</td><td>{mac}</td><td>{kind}</td><td>{port}</td></tr>"


def epon_page(rows, total=None):
    head = ""
    if total is not None:
        head = ("<input name='macCount' id='macCount' type=text value='0'"
                " readonly='readonly'>"
                "<script language= \"javascript\"> var select="
                f"document.getElementById(\"macCount\"); select.value = '{total}';"
                "</script>")
    return f"<html><body>{head}<table>{HEAD}{''.join(rows)}</table></body></html>"


def gpon_page(rows):
    return f"<html><body><table>{HEAD}{''.join(rows)}</table></body></html>"


class PortShapeTest(unittest.TestCase):

    def test_the_epon_build_names_a_slot_as_chassis_slash_pon_colon_onu(self):
        prof = wmp.builtin("dbc")
        self.assertEqual(prof.slot_of("EPON0/8:38"), "8.38")

    def test_the_gpon_build_names_the_same_slot_a_different_way(self):
        prof = wmp.builtin("syrotech_gpon")
        self.assertEqual(prof.slot_of("PON2:ONU36"), "2.36")

    def test_a_slot_key_matches_the_shape_the_roster_stores(self):
        # onu_optics.onu_key is "<pon>.<onu>", and the join is on the SLOT — the
        # same identity rule parse_onu_table keeps. A drift here silently
        # attributes a customer's router to a different customer.
        self.assertEqual(wmp.builtin("dbc").slot_of("EPON0/1:4"), "1.4")

    def test_an_uplink_port_is_NOT_a_slot(self):
        # 353 of HLY-OLT-1's 548 real rows sit on GE5/CPU. Those are aggregate
        # traffic; pinning one to a customer sends a tech to the wrong house.
        prof = wmp.builtin("dbc")
        for port in ("GE5", "CPU", "PON6", "GE11", ""):
            with self.subTest(port=port):
                self.assertIsNone(prof.slot_of(port))

    def test_one_builds_shape_is_not_read_by_the_others_rule(self):
        # The GPON page's "PON2:ONU36" must not parse under the EPON shape.
        self.assertIsNone(wmp.builtin("dbc").slot_of("PON2:ONU36"))
        self.assertIsNone(wmp.builtin("syrotech_gpon").slot_of("EPON0/8:38"))


class ParseTest(unittest.TestCase):

    def test_the_customer_rows_are_kept_and_the_uplink_rows_are_not(self):
        page = epon_page([
            row("1", "50:01:9B:00:72:50", "Dynamic", "GE5"),
            row("1", "20:0C:86:75:2B:51", "Dynamic", "EPON0/1:20"),
            row("1", "AA:BB:CC:DD:EE:FF", "Dynamic", "CPU"),
        ], total=3)
        table = webmacs.parse_mac_table(page, wmp.builtin("dbc"))
        self.assertEqual([r["onu_key"] for r in table.rows], ["1.20"])
        self.assertEqual(table.uplink_rows, 2)
        self.assertEqual(table.data_rows, 3)

    def test_a_row_carries_its_vlan_and_the_port_exactly_as_printed(self):
        page = epon_page([row("1831", "D0:1E:1D:14:16:38", "Dynamic", "EPON0/1:4")])
        got = webmacs.parse_mac_table(page, wmp.builtin("dbc")).rows[0]
        self.assertEqual(got["mac"], "D0:1E:1D:14:16:38")
        self.assertEqual(got["vlan"], "1831")
        self.assertEqual(got["kind"], "Dynamic")
        self.assertEqual(got["port_label"], "EPON0/1:4")

    def test_ONE_SLOT_MAY_CARRY_SEVERAL_and_all_of_them_are_kept(self):
        # Real: EPON0/1:4 presents three MACs on three service VLANs, and
        # EPON0/6:5 carries five. Picking one would be a guess about which is
        # "the" customer device.
        page = epon_page([
            row("1900", "D0:1E:1D:14:16:3A", "Dynamic", "EPON0/1:4"),
            row("1831", "D0:1E:1D:14:16:38", "Dynamic", "EPON0/1:4"),
            row("168", "D0:1E:1D:14:16:37", "Dynamic", "EPON0/1:4"),
        ])
        table = webmacs.parse_mac_table(page, wmp.builtin("dbc"))
        self.assertEqual(len(table.rows), 3)
        self.assertEqual({r["onu_key"] for r in table.rows}, {"1.4"})

    def test_the_same_address_twice_on_one_slot_is_stored_once(self):
        page = epon_page([
            row("1", "AA:BB:CC:DD:EE:01", "Dynamic", "EPON0/1:4"),
            row("1", "aa:bb:cc:dd:ee:01", "Dynamic", "EPON0/1:4"),
        ])
        self.assertEqual(len(webmacs.parse_mac_table(page, wmp.builtin("dbc")).rows), 1)

    def test_a_cell_that_is_not_an_address_is_not_a_data_row(self):
        page = epon_page([
            "<tr><td>Port ID</td><td>ALL</td><td>x</td><td>y</td></tr>",
            row("1", "20:0C:86:75:2B:51", "Dynamic", "EPON0/1:20"),
        ])
        table = webmacs.parse_mac_table(page, wmp.builtin("dbc"))
        self.assertEqual(table.data_rows, 1)

    def test_a_hyphenated_address_is_stored_colon_separated_and_upper(self):
        page = epon_page([row("1", "aa-bb-cc-dd-ee-01", "Dynamic", "EPON0/1:4")])
        got = webmacs.parse_mac_table(page, wmp.builtin("dbc")).rows[0]
        self.assertEqual(got["mac"], "AA:BB:CC:DD:EE:01")

    def test_the_gpon_build_parses_under_its_own_profile(self):
        page = gpon_page([
            row("100", "E4:47:B3:A4:83:12", "Dynamic", "PON1:ONU10"),
            row("100", "44:FB:5A:9D:E4:4A", "Dynamic", "GE1"),
        ])
        table = webmacs.parse_mac_table(page, wmp.builtin("syrotech_gpon"))
        self.assertEqual([r["onu_key"] for r in table.rows], ["1.10"])


class CompletenessTest(unittest.TestCase):
    """The truncation guard — the failure the GETBULK walks had no defence for.

    A short read makes a customer who HAS an address render exactly like one who
    does not, which is the same class of lie as a blank Rx column meaning four
    different things.
    """

    def test_the_OLT_declares_its_own_total_and_a_full_read_matches_it(self):
        page = epon_page([
            row("1", "AA:BB:CC:DD:EE:01", "Dynamic", "EPON0/1:4"),
            row("1", "AA:BB:CC:DD:EE:02", "Dynamic", "GE5"),
        ], total=2)
        table = webmacs.parse_mac_table(page, wmp.builtin("dbc"))
        self.assertEqual(table.declared_total, 2)
        self.assertTrue(table.complete)
        self.assertFalse(table.truncated)

    def test_a_short_page_is_reported_as_truncated_not_as_an_empty_slot(self):
        page = epon_page([row("1", "AA:BB:CC:DD:EE:01", "Dynamic", "EPON0/1:4")],
                         total=97)
        table = webmacs.parse_mac_table(page, wmp.builtin("dbc"))
        self.assertTrue(table.truncated)
        self.assertIs(table.complete, False)
        self.assertEqual(table.shortfall(), 96)

    def test_a_build_that_declares_NO_total_says_so_rather_than_claiming_complete(self):
        # The Syrotech GPON page prints no count anywhere. "We cannot tell" is a
        # third answer and must not collapse into "complete".
        page = gpon_page([row("100", "E4:47:B3:A4:83:12", "Dynamic", "PON1:ONU10")])
        table = webmacs.parse_mac_table(page, wmp.builtin("syrotech_gpon"))
        self.assertIsNone(table.declared_total)
        self.assertIsNone(table.complete)
        self.assertFalse(table.truncated)
        self.assertEqual(table.shortfall(), 0)


class ProfileVocabularyTest(unittest.TestCase):

    def _spec(self, **over):
        spec = dict(wmp.BUILTIN_SPECS["dbc"])
        spec.update(over)
        return spec

    def test_an_unknown_port_shape_rejects_the_WHOLE_profile(self):
        with self.assertRaises(InventoryError):
            wmp.profile_from_spec("x", self._spec(port_shape="freeform"))

    def test_a_profile_with_no_port_column_is_refused(self):
        with self.assertRaises(InventoryError):
            wmp.profile_from_spec("x", self._spec(
                columns={"mac": "MAC Address"}, column_order=["mac"]))

    def test_a_profile_with_no_mac_column_is_refused(self):
        with self.assertRaises(InventoryError):
            wmp.profile_from_spec("x", self._spec(
                columns={"port": "Port ID"}, column_order=["port"]))

    def test_an_unknown_column_is_refused_rather_than_ignored(self):
        with self.assertRaises(InventoryError):
            wmp.profile_from_spec("x", self._spec(
                columns={"port": "Port ID", "mac": "MAC", "rx_dbm": "RX"}))

    def test_a_profile_may_never_carry_a_host(self):
        # Path-only is what stops the tunnel being a lateral-movement primitive.
        with self.assertRaises(InventoryError):
            wmp.profile_from_spec("x", self._spec(
                mac_path="http://10.0.0.1/action/macinfo.html"))

    def test_a_table_row_shadows_a_builtin_and_a_disabled_row_switches_it_off(self):
        got = wmp.ProfileSet.build([
            {"name": "dbc", "org_id": None, "enabled": True,
             "spec": self._spec(mac_path="/action/other.html")}])
        self.assertEqual(got.resolve(None, "dbc").mac_path, "/action/other.html")
        off = wmp.ProfileSet.build(
            [{"name": "dbc", "org_id": None, "enabled": False, "spec": {}}])
        self.assertIsNone(off.resolve(None, "dbc"))

    def test_an_org_row_beats_a_global_one(self):
        got = wmp.ProfileSet.build([
            {"name": "dbc", "org_id": None, "enabled": True,
             "spec": self._spec(mac_path="/global.html")},
            {"name": "dbc", "org_id": "acme", "enabled": True,
             "spec": self._spec(mac_path="/acme.html")}])
        self.assertEqual(got.resolve("acme", "dbc").mac_path, "/acme.html")
        self.assertEqual(got.resolve("other", "dbc").mac_path, "/global.html")

    def test_the_gpon_builtin_exists_BECAUSE_it_has_no_optics_page(self):
        # syrotech_gpon serves the address table and provably has no optical
        # page (its OPM path 404s), which is the whole reason these recipes are
        # a separate table from the optics ones.
        self.assertIn("syrotech_gpon", wmp.builtin_names())
        self.assertIsNotNone(wmp.builtin("syrotech_gpon"))


class MacShapeTest(unittest.TestCase):

    def test_junk_is_not_an_address(self):
        for raw in ("", "-", "not a mac", "AA:BB:CC:DD:EE",
                    "AA:BB:CC:DD:EE:FF:00", "ZZ:BB:CC:DD:EE:FF", "1"):
            with self.subTest(raw=raw):
                self.assertIsNone(webmacs.normalise_mac(raw))

    def test_a_real_address_survives_in_either_spelling(self):
        self.assertEqual(webmacs.normalise_mac("d0:1e:1d:14:16:3a"),
                         "D0:1E:1D:14:16:3A")
        self.assertEqual(webmacs.normalise_mac("D0-1E-1D-14-16-3A"),
                         "D0:1E:1D:14:16:3A")


if __name__ == "__main__":
    unittest.main()
