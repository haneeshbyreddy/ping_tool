from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import radius, radius_profiles
from wisp.central.inventory import InventoryError
from wisp.central.radius_sync import PanelError, clean_base_url


CBP = radius_profiles.builtin("cbp")

HEAD = ("Username,MAC,Name,Mobile,Status,\"Expiry Date\",\"Package Name\","
        "Branch,Area,\"Installation Address\",\"Balance Amount\",\"CAF No\","
        "\"Alt. Mobile\"\n")


def _row(user, mac="", name="A Customer", status="active"):
    return (f'{user},{mac},"{name}",9999999999,{status},"06/01/2024 09:24",'
            f'PLAN,HALIYA,AREA,"addr",0,123,\n')


class ProfileVocabularyTest(unittest.TestCase):

    def test_the_builtin_cbp_profile_is_valid(self):
        self.assertIsNotNone(CBP)
        self.assertEqual(CBP.username_field, "unme")
        self.assertEqual(CBP.password_field, "passd")
        self.assertEqual(CBP.heading_of("name"), "Name")

    def test_the_builtin_oneradius_profile_is_valid(self):
        prof = radius_profiles.builtin("oneradius")
        self.assertIsNotNone(prof)
        self.assertEqual(prof.login_flow, "encrypted-nonce")
        self.assertEqual(prof.roster_method, "GET")
        self.assertEqual(prof.nonce_field, "enckey")
        self.assertEqual(prof.heading_of("mac"), "MAC")

    def test_an_encrypted_login_without_a_nonce_field_is_refused(self):
        # Without naming the field the panel mints its one-time key in, nothing
        # can be encrypted and so nothing can be sent. Refusing the profile beats
        # storing one that can only ever fail at sign-in.
        spec = {**radius_profiles._ONERADIUS, "nonce_field": ""}
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec("x", spec)

    def test_an_unknown_login_flow_or_roster_method_is_refused(self):
        for key, bad in (("login_flow", "magic"), ("roster_method", "PATCH")):
            with self.assertRaises(InventoryError):
                radius_profiles.profile_from_spec(
                    "x", {**radius_profiles._CBP, key: bad})

    def test_encrypt_fields_is_a_closed_vocabulary(self):
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec(
                "x", {**radius_profiles._ONERADIUS,
                      "encrypt_fields": ["username", "otp"]})

    def test_the_encrypted_login_form_carries_the_csrf_and_nonce_back(self):
        prof = radius_profiles.builtin("oneradius")
        # The password is LONG on purpose. It used to be "pw", and a two-char
        # needle turns up in an 836-char base64 haystack by pure chance about
        # 0.9% of the time (measured over 3000 forms) — a one-in-a-hundred
        # flake that says "the password leaked" when nothing leaked. The
        # 13-char username never collided once. Keep any plaintext asserted
        # against a ciphertext long enough that a hit means what it claims.
        form = prof.login_form("ms_comm_admin", "s3cr3t-pa55word", {
            "_csrf-backend-admin": "CSRF1", "enckey": "noncenonce"})
        self.assertEqual(form["_csrf-backend-admin"], "CSRF1")
        self.assertEqual(form["enckey"], "noncenonce")
        for field, plain in (("LoginForm[username]", "ms_comm_admin"),
                             ("LoginForm[password]", "s3cr3t-pa55word")):
            self.assertNotIn(plain, form[field])
            self.assertGreater(len(form[field]), 100)

    def test_an_encrypted_login_with_no_nonce_in_hand_refuses_to_build_a_form(self):
        prof = radius_profiles.builtin("oneradius")
        with self.assertRaises(ValueError):
            prof.login_form("u", "p", {})

    def test_a_GET_export_sends_no_form_at_all(self):
        self.assertIsNone(radius_profiles.builtin("oneradius").export_form())
        self.assertIsNotNone(CBP.export_form())

    def test_a_profile_may_never_carry_a_host(self):
        for bad in ("https://evil.example/admin/login", "//evil.example/x"):
            with self.assertRaises(InventoryError):
                radius_profiles.profile_from_spec(
                    "x", {**radius_profiles._CBP, "login_path": bad})

    def test_the_whole_profile_is_rejected_on_an_unknown_column(self):
        spec = {**radius_profiles._CBP,
                "columns": {**radius_profiles._CBP["columns"],
                            "credit_card": ["cc", "Card"]}}
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec("x", spec)

    def test_a_profile_without_a_name_column_is_refused(self):
        cols = dict(radius_profiles._CBP["columns"])
        cols.pop("name")
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec(
                "x", {**radius_profiles._CBP, "columns": cols})

    def test_a_column_picker_panel_needs_the_export_column_for_every_field(self):
        spec = {**radius_profiles._CBP,
                "columns": {**radius_profiles._CBP["columns"], "name": ["", "Name"]}}
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec("x", spec)

    def test_status_words_outside_the_vocabulary_are_refused(self):
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec(
                "x", {**radius_profiles._CBP, "status_map": {"live": "onlineish"}})

    def test_an_unmapped_status_word_reads_unknown_never_a_guess(self):
        self.assertEqual(CBP.status_of("nonsense"), "unknown")
        self.assertEqual(CBP.status_of(""), "unknown")
        self.assertEqual(CBP.status_of("Expired"), "expired")

    def test_a_disabled_row_is_a_tombstone_over_the_builtin(self):
        ps = radius_profiles.ProfileSet.build(
            [{"name": "cbp", "org_id": None, "enabled": False, "spec": {}}])
        self.assertIsNone(ps.resolve("byreddy", "cbp"))

    def test_a_same_named_row_shadows_the_builtin(self):
        spec = {**radius_profiles._CBP, "roster_path": "/admin/other"}
        ps = radius_profiles.ProfileSet.build(
            [{"name": "cbp", "org_id": "byreddy", "enabled": True, "spec": spec}])
        self.assertEqual(ps.resolve("byreddy", "cbp").roster_path, "/admin/other")
        self.assertEqual(ps.resolve("other", "cbp").roster_path, "/admin/user/export")


class BaseUrlTest(unittest.TestCase):

    def test_a_bare_server_is_accepted(self):
        self.assertEqual(clean_base_url("https://cbp.excellmedia.in/"),
                         "https://cbp.excellmedia.in")

    def test_a_path_is_refused_because_the_profile_carries_the_pages(self):
        with self.assertRaises(PanelError):
            clean_base_url("https://cbp.excellmedia.in/admin/dashboard")

    def test_credentials_in_the_address_are_refused(self):
        with self.assertRaises(PanelError):
            clean_base_url("https://user:pass@cbp.excellmedia.in")

    def test_a_non_http_scheme_is_refused(self):
        for bad in ("file:///etc/passwd", "ftp://x.example", "cbp.excellmedia.in"):
            with self.assertRaises(PanelError):
                clean_base_url(bad)


class RosterParseTest(unittest.TestCase):

    def test_it_reads_the_export_by_heading(self):
        r = radius.parse_roster(HEAD + _row("HC_A", "F0:A7:31:EA:7E:32"), CBP)
        self.assertEqual(len(r.customers), 1)
        c = r.customers[0]
        self.assertEqual(c["username"], "HC_A")
        self.assertEqual(c["name"], "A Customer")
        self.assertEqual(c["mac"], "F0:A7:31:EA:7E:32")
        self.assertEqual(c["status"], "active")

    def test_the_mac_is_normalised_by_the_SAME_function_that_stored_the_other_side(self):
        r = radius.parse_roster(HEAD + _row("HC_A", "f0-a7-31-ea-7e-32"), CBP)
        self.assertEqual(r.customers[0]["mac"], "F0:A7:31:EA:7E:32")

    def test_a_junk_mac_is_dropped_rather_than_stored(self):
        r = radius.parse_roster(HEAD + _row("HC_A", "not-a-mac"), CBP)
        self.assertIsNone(r.customers[0]["mac"])

    def test_expiry_and_balance_are_kept_as_the_panels_own_strings(self):
        r = radius.parse_roster(HEAD + _row("HC_A"), CBP)
        self.assertEqual(r.customers[0]["expiry"], "06/01/2024 09:24")

    def test_a_row_with_no_username_is_skipped(self):
        r = radius.parse_roster(HEAD + _row("") + _row("HC_B"), CBP)
        self.assertEqual([c["username"] for c in r.customers], ["HC_B"])
        self.assertEqual(r.skipped, 1)

    def test_a_repeated_username_is_taken_once(self):
        r = radius.parse_roster(HEAD + _row("HC_A") + _row("HC_A"), CBP)
        self.assertEqual(len(r.customers), 1)

    def test_a_missing_column_is_REPORTED_not_guessed_by_position(self):
        head = HEAD.replace(',Mobile', ',Phone')
        r = radius.parse_roster(head + _row("HC_A"), CBP)
        self.assertIn("mobile", r.missing_headings)
        self.assertIsNone(r.customers[0].get("mobile"))

    def test_an_export_with_no_username_column_reads_NOTHING(self):
        head = HEAD.replace('Username,', 'User,')
        r = radius.parse_roster(head + _row("HC_A"), CBP)
        self.assertEqual(r.customers, [])
        self.assertIn("username", r.missing_headings)


class LinkTest(unittest.TestCase):

    def _macs(self, *rows):
        return [{"device_id": d, "onu_key": k, "mac": m} for d, k, m in rows]

    def _onus(self, *rows):
        return [{"device_id": d, "onu_key": k, "name": n} for d, k, n in rows]

    def test_a_mac_on_one_slot_links(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"}]
        res = radius.link_customers(cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")), [])
        self.assertEqual(len(res.links), 1)
        self.assertEqual(res.links[0].match_by, "mac")
        self.assertEqual((res.links[0].device_id, res.links[0].onu_key), (7, "1.4"))

    def test_a_MAC_ON_TWO_SLOTS_LINKS_NOTHING(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01"),
                             (7, "2.9", "AA:BB:CC:DD:EE:01")), [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.ambiguous_mac, 1)

    def test_TWO_CUSTOMERS_ON_ONE_MAC_LINK_NOTHING(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"},
                {"username": "HC_B", "mac": "AA:BB:CC:DD:EE:01"}]
        res = radius.link_customers(cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")), [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.ambiguous_mac, 2)

    def test_the_username_matches_the_ONU_name_punctuation_blind(self):
        cust = [{"username": "hc_kiran", "mac": None}]
        res = radius.link_customers(cust, [], self._onus((7, "1.4", "HC-KIRAN")))
        self.assertEqual(len(res.links), 1)
        self.assertEqual(res.links[0].match_by, "name")

    def test_a_name_on_two_slots_links_nothing(self):
        cust = [{"username": "hc_kiran", "mac": None}]
        res = radius.link_customers(
            cust, [], self._onus((7, "1.4", "hc_kiran"), (8, "2.2", "HC_KIRAN")))
        self.assertEqual(res.links, [])
        self.assertEqual(res.ambiguous_name, 1)

    def test_the_MAC_outranks_the_name_when_they_disagree(self):
        cust = [{"username": "hc_kiran", "mac": "AA:BB:CC:DD:EE:01"}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")),
            self._onus((9, "3.3", "hc_kiran")))
        self.assertEqual(len(res.links), 1)
        self.assertEqual((res.links[0].device_id, res.links[0].match_by), (7, "mac"))

    def test_a_slot_already_claimed_by_a_mac_is_not_renamed_by_a_name_match(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"},
                {"username": "HC_B", "mac": None}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")),
            self._onus((7, "1.4", "HC_B")))
        self.assertEqual([l.username for l in res.links], ["HC_A"])

    def test_TWO_CUSTOMERS_WHOSE_MACS_SHARE_ONE_SLOT_LINK_NOTHING(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"},
                {"username": "HC_B", "mac": "AA:BB:CC:DD:EE:02"}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01"),
                             (7, "1.4", "AA:BB:CC:DD:EE:02")), [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.ambiguous_mac, 2)

    def test_the_match_does_not_depend_on_the_order_rows_arrive_in(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01"},
                {"username": "HC_B", "mac": "AA:BB:CC:DD:EE:02"}]
        macs = self._macs((7, "1.4", "AA:BB:CC:DD:EE:01"),
                          (7, "1.4", "AA:BB:CC:DD:EE:02"))
        first = radius.link_customers(cust, macs, [])
        second = radius.link_customers(list(reversed(cust)), macs, [])
        self.assertEqual(first.links, second.links)

    def test_a_customer_with_neither_key_is_simply_unmatched(self):
        res = radius.link_customers([{"username": "HC_A", "mac": None}], [], [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.unmatched, 1)

    def test_one_slot_is_never_linked_to_two_customers(self):
        cust = [{"username": "HC_A", "mac": None}, {"username": "HC_B", "mac": None}]
        res = radius.link_customers(
            cust, [], self._onus((7, "1.4", "HC_A"), (7, "1.4", "HC_B")))
        slots = [(l.device_id, l.onu_key) for l in res.links]
        self.assertEqual(len(slots), len(set(slots)))

    def test_AN_AGGREGATE_SLOT_IS_NOT_A_SUBSCRIBER(self):
        # MS Telecom's OLT reports 21,666 MACs against one ONU slot: a trunk,
        # not a house. Linking a customer there sends a tech to the wrong end of
        # the network, so the slot is dropped whole. The largest legitimate slot
        # anywhere on the live fleet carries 34.
        crowd = [{"device_id": 7, "onu_key": "3.16", "mac": f"AA:BB:CC:00:{i:02X}:01"}
                 for i in range(200)]
        crowd.append({"device_id": 7, "onu_key": "3.16", "mac": "F0:0D:CC:DD:EE:01"})
        cust = [{"username": "HC_A", "mac": "F0:0D:CC:DD:EE:01"}]
        res = radius.link_customers(cust, crowd, [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.crowded_slots, 1)

    def test_a_busy_but_believable_slot_still_links(self):
        rows = [{"device_id": 7, "onu_key": "1.4", "mac": f"AA:BB:CC:00:{i:02X}:01"}
                for i in range(34)]
        rows.append({"device_id": 7, "onu_key": "1.4", "mac": "F0:0D:CC:DD:EE:01"})
        cust = [{"username": "HC_A", "mac": "F0:0D:CC:DD:EE:01"}]
        res = radius.link_customers(cust, rows, [])
        self.assertEqual(len(res.links), 1)
        self.assertEqual(res.crowded_slots, 0)


class MultiPanelTest(unittest.TestCase):
    """An org may run several billing panels; Hansa asked for a second one."""

    def _macs(self, *rows):
        return [{"device_id": d, "onu_key": k, "mac": m} for d, k, m in rows]

    def test_two_panels_each_link_their_own_customers(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01", "account_id": 1},
                {"username": "MS_B", "mac": "AA:BB:CC:DD:EE:02", "account_id": 2}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01"),
                             (7, "2.9", "AA:BB:CC:DD:EE:02")), [])
        self.assertEqual(len(res.links), 2)
        self.assertEqual({l.account_id for l in res.links}, {1, 2})

    def test_TWO_PANELS_CLAIMING_ONE_MAC_ARE_SETTLED_BY_ORDER_NOT_REFUSED(self):
        # Within one panel, two customers on one MAC is a book contradicting
        # itself and links nothing. ACROSS panels it is two books describing one
        # person: the slot is the same slot either way, so refusing would drop a
        # real subscriber rather than protect one. The panel connected first wins
        # and it is counted so the sync can say so.
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01", "account_id": 1},
                {"username": "MS_A", "mac": "AA:BB:CC:DD:EE:01", "account_id": 2}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")), [])
        self.assertEqual(len(res.links), 1)
        self.assertEqual(res.links[0].username, "HC_A")
        self.assertEqual(res.links[0].account_id, 1)
        self.assertEqual(res.cross_panel, 1)

    def test_two_customers_on_one_MAC_INSIDE_one_panel_still_link_nothing(self):
        cust = [{"username": "HC_A", "mac": "AA:BB:CC:DD:EE:01", "account_id": 1},
                {"username": "HC_B", "mac": "AA:BB:CC:DD:EE:01", "account_id": 1}]
        res = radius.link_customers(
            cust, self._macs((7, "1.4", "AA:BB:CC:DD:EE:01")), [])
        self.assertEqual(res.links, [])
        self.assertEqual(res.cross_panel, 0)


class MacFieldTest(unittest.TestCase):
    """The MAC cell is not always one MAC."""

    def test_a_plain_mac_reads_as_itself(self):
        self.assertEqual(radius.mac_field("A0:AB:1B:1F:94:3F"), "A0:AB:1B:1F:94:3F")

    def test_THE_SAME_MAC_LISTED_TWICE_IS_STILL_ONE_MAC(self):
        # OneRadius prints "F8:C4:F3:E7:BA:3E, F8:C4:F3:E7:BA:3E" for 556 of MS
        # Telecom's 1,017 addressed customers. Read whole it normalises to
        # nothing, and those customers silently never link -- 121 links instead
        # of 312 on that fleet.
        self.assertEqual(radius.mac_field("F8:C4:F3:E7:BA:3E, F8:C4:F3:E7:BA:3E"),
                         "F8:C4:F3:E7:BA:3E")

    def test_TWO_DIFFERENT_MACS_IN_ONE_CELL_READ_AS_NEITHER(self):
        self.assertIsNone(
            radius.mac_field("F8:C4:F3:E7:BA:3E, A0:AB:1B:1F:94:3F"))

    def test_separators_other_than_the_comma_are_read_too(self):
        for cell in ("AA:BB:CC:DD:EE:01 AA:BB:CC:DD:EE:01",
                     "AA:BB:CC:DD:EE:01;AA:BB:CC:DD:EE:01",
                     "AA:BB:CC:DD:EE:01 / AA:BB:CC:DD:EE:01"):
            self.assertEqual(radius.mac_field(cell), "AA:BB:CC:DD:EE:01", cell)

    def test_a_dashed_mac_normalises_like_the_other_side(self):
        self.assertEqual(radius.mac_field("78-8C-B5-5B-DC-D9"), "78:8C:B5:5B:DC:D9")

    def test_junk_and_blanks_read_as_nothing(self):
        for cell in ("", "   ", "not a mac", None, "0.0.0.0"):
            self.assertIsNone(radius.mac_field(cell))


class ExpiryDateTest(unittest.TestCase):
    """A date is parsed only under a profile-declared convention, never guessed."""

    def test_cbp_dates_are_day_first(self):
        # Proven from the live export: 448 of byreddy's rows carry a day > 12.
        self.assertEqual(radius.parse_expiry("22/11/2026 07:55", "dmy"),
                         "2026-11-22T07:55:00")
        self.assertEqual(CBP.date_format, "dmy")

    def test_oneradius_dates_carry_the_month_by_name(self):
        # "08 Jan, 2024 11:59 pm" -- the month is a word, so the order is
        # unambiguous whichever side of the day it lands.
        prof = radius_profiles.builtin("oneradius")
        self.assertEqual(prof.date_format, "named-month")
        self.assertEqual(radius.parse_expiry("08 Jan, 2024 11:59 pm", "named-month"),
                         "2024-01-08T23:59:00")
        self.assertEqual(radius.parse_expiry("Jan 08, 2024 11:59 pm", "named-month"),
                         "2024-01-08T23:59:00")
        self.assertEqual(radius.parse_expiry("12 Jun, 2023 12:01 am", "named-month"),
                         "2023-06-12T00:01:00")

    def test_the_same_string_reads_differently_under_dmy_and_mdy(self):
        self.assertEqual(radius.parse_expiry("06/01/2024", "dmy"),
                         "2024-01-06T00:00:00")
        self.assertEqual(radius.parse_expiry("06/01/2024", "mdy"),
                         "2024-06-01T00:00:00")

    def test_no_declared_format_parses_NOTHING(self):
        # The old rule, kept as the default: a date we guessed wrong is worse
        # than a date we merely repeat.
        self.assertIsNone(radius.parse_expiry("22/11/2026 07:55", ""))

    def test_an_impossible_date_reads_as_nothing_never_as_a_guess(self):
        for text, fmt in (("31/02/2026", "dmy"), ("13/25/2026", "mdy"),
                          ("00/01/2026", "dmy"), ("08 Foo, 2024", "named-month"),
                          ("junk", "dmy"), ("", "dmy"), (None, "iso")):
            self.assertIsNone(radius.parse_expiry(text, fmt), (text, fmt))

    def test_iso_dates_parse_with_and_without_a_clock(self):
        self.assertEqual(radius.parse_expiry("2026-11-22T07:55:00", "iso"),
                         "2026-11-22T07:55:00")
        self.assertEqual(radius.parse_expiry("2026-11-22", "iso"),
                         "2026-11-22T00:00:00")

    def test_days_until_is_calendar_arithmetic_on_the_parsed_date(self):
        from datetime import date
        self.assertEqual(radius.days_until("2026-08-21T23:59:00",
                                           date(2026, 8, 13)), 8)
        self.assertEqual(radius.days_until("2026-08-13T00:00:00",
                                           date(2026, 8, 13)), 0)
        self.assertLess(radius.days_until("2023-09-03T23:59:00",
                                          date(2026, 8, 13)), 0)
        self.assertIsNone(radius.days_until(None, date(2026, 8, 13)))
        self.assertIsNone(radius.days_until("junk", date(2026, 8, 13)))

    def test_an_unknown_date_format_is_refused_on_the_profile(self):
        with self.assertRaises(InventoryError):
            radius_profiles.profile_from_spec(
                "x", {**radius_profiles._CBP, "date_format": "ymd"})


if __name__ == "__main__":
    unittest.main()
