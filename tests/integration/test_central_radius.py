"""RADIUS: the customer as billing holds them, tied to the ONU we already scrape.

The subsystem exists to kill a two-step manual workflow — read the user MAC off
the OLT's web page, then search it in the billing panel — so the tests that
matter are the ones about the JOIN and its refusals. A customer pinned to the
wrong ONU sends a tech to the wrong house, which is why an ambiguous match links
nothing at all.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import auth, onuroster, radius_profiles  # noqa: E402
from wisp.central.radius import RadiusLink  # noqa: E402
from wisp.central.radius_sync import PanelError, RadiusSyncer  # noqa: E402
from wisp.central.server import make_server  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.config import Config  # noqa: E402

ORG = "ispA"
NOW = datetime.now(timezone.utc)
RECENT = NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")

HEAD = ("Username,MAC,Name,Mobile,Status,\"Expiry Date\",\"Package Name\","
        "Branch,Area,\"Installation Address\",\"Balance Amount\",\"CAF No\","
        "\"Alt. Mobile\"\n")


def row(user, mac="", name="A CUSTOMER", status="active", mobile="9966793791"):
    return (f'{user},{mac},"{name}",{mobile},{status},"06/01/2024 09:24",'
            f'PLAN,HALIYA,AREA,"addr",0,123,\n')


class FakeBox:
    def __init__(self, secret="s3cret"):
        self.secret = secret

    def decrypt(self, token):
        from wisp.central.secretbox import DecryptError
        if token == "bad":
            raise DecryptError("nope")
        return self.secret


class FakePanel:
    """Stands in for the billing panel: login page, sign-in, CSV export."""

    def __init__(self, csv_text, *, login_page_status=200, roster_status=200,
                 needs_login=True, ctype="text/csv", login_page_body=None,
                 roster_lands_on=None):
        self.csv_text = csv_text
        self.login_page_status = login_page_status
        self.roster_status = roster_status
        self.needs_login = needs_login
        self.ctype = ctype
        self.login_page_body = login_page_body or b"<html>login</html>"
        self.roster_lands_on = roster_lands_on
        self.signed_in = False
        self.asked: list[str] = []
        self.credentials: list[tuple] = []
        self.forms: list[dict] = []
        self.base_url = ""

    def factory(self, base_url, timeout=30.0):
        self.base_url = base_url
        return self

    def _at(self, path):
        return f"{self.base_url or 'https://panel.example.in'}{path}"

    def request(self, path, *, query=None, form=None, headers=None):
        self.asked.append(path)
        if path.endswith("/login") and form is None:
            return (self.login_page_status, self.login_page_body, "text/html",
                    self._at(path))
        if path.endswith("/process") or (path.endswith("/login") and form):
            pairs = dict(form or {})
            self.forms.append(pairs)
            self.credentials.append((pairs.get("unme"), pairs.get("passd")))
            self.signed_in = True
            return 200, b"", "text/html", self._at(path)
        if self.needs_login and not self.signed_in:
            return 200, b"<html>login</html>", "text/html", self._at(path)
        if self.roster_lands_on:
            return (200, b"<html>not allowed</html>", "text/html",
                    self._at(self.roster_lands_on))
        if self.roster_status >= 400:
            return self.roster_status, b"", "text/html", self._at(path)
        return (self.roster_status, self.csv_text.encode("utf-8"), self.ctype,
                self._at(path))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG)
        self.olt = self.store.create_org_device(ORG, {
            "name": "HLY-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _account(self, password_enc="enc", profile="cbp", org=ORG,
                 base_url="https://cbp.example.in", username="hansa", label=""):
        return self.store.set_radius_account(
            org, profile=profile, base_url=base_url,
            username=username, password_enc=password_enc, label=label,
            updated_by="t")

    def _roster(self, onu_key="1.4", serial="AA:11:22:33:44:55", name="sub",
                onu_id=4):
        self.store.upsert_onu_optics(
            ORG, self.olt, onu_key, pon_port="EPON0/1", onu_id=onu_id, name=name,
            serial=serial, state="online", rx_dbm=-21.0, tx_dbm=None,
            olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=RECENT)

    def _usermac(self, mac, onu_key="1.4"):
        self.store.upsert_user_macs(ORG, self.olt, [
            {"onu_key": onu_key, "mac": mac, "vlan": "1900", "kind": "Dynamic",
             "port_label": "EPON0/1:4"}], RECENT)

    def _sync(self, panel, account_id=None, org=ORG):
        syncer = RadiusSyncer(self.store, FakeBox(), self.cfg,
                              http_factory=panel.factory)
        if account_id is None:
            account_id = self.store.org_radius_accounts(org)[0]["id"]
        return syncer.sync_org(self.store.get_radius_account(account_id))

    def _status(self, org=ORG, account_id=None):
        rows = self.store.org_radius_status(org)
        if account_id is not None:
            rows = [r for r in rows if r["account_id"] == account_id]
        return rows[0] if rows else None


class SyncTest(Base):

    def test_a_sweep_stores_the_customers_and_links_them_by_mac(self):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        panel = FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32",
                                     "VENKATESWARLU GOGU"))
        self.assertTrue(self._sync(panel))

        cust = self.store.get_radius_customer(ORG, "HC_GOGU")
        self.assertEqual(cust["name"], "VENKATESWARLU GOGU")
        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "HC_GOGU")
        self.assertEqual(link["match_by"], "mac")
        st = self._status()
        self.assertEqual(st["state"], "ok")
        self.assertEqual((st["customers"], st["linked"]), (1, 1))

    def test_the_username_falls_back_to_the_ONU_name_for_a_customer_with_no_mac(self):
        self._account()
        self._roster(name="HC_KIRAN")
        panel = FakePanel(HEAD + row("hc_kiran", "", "KIRAN KUMAR"))
        self._sync(panel)
        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["match_by"], "name")
        self.assertEqual(link["name"], "KIRAN KUMAR")

    def test_AN_AMBIGUOUS_MAC_LINKS_NOTHING(self):
        self._account()
        self._roster()
        self._roster(onu_key="2.9", serial="BB:11:22:33:44:55", onu_id=9)
        self._usermac("F0:A7:31:EA:7E:32", "1.4")
        self._usermac("F0:A7:31:EA:7E:32", "2.9")
        panel = FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32"))
        self._sync(panel)
        self.assertIsNone(self.store.radius_link_for(ORG, self.olt, "1.4"))
        self.assertIsNone(self.store.radius_link_for(ORG, self.olt, "2.9"))
        self.assertEqual(self._status()["linked"], 0)

    def test_THE_CREDENTIALS_ARE_NOT_SENT_WHEN_THE_SIGN_IN_PAGE_FAILS(self):
        self._account()
        panel = FakePanel(HEAD + row("HC_A"), login_page_status=500)
        self.assertFalse(self._sync(panel))
        self.assertEqual(panel.credentials, [])
        st = self._status()
        self.assertEqual(st["state"], "unreachable")
        self.assertIn("NOT", st["detail"])

    def test_a_wrong_password_is_reported_as_a_sign_in_problem(self):
        self._account()
        panel = FakePanel(HEAD + row("HC_A"))
        panel.signed_in = False

        def never_signs_in(path, *, query=None, form=None, headers=None):
            panel.asked.append(path)
            where = f"https://cbp.example.in{path}"
            if path.endswith("/login"):
                return 200, b"<html>login</html>", "text/html", where
            return 200, b"<html>login form again</html>", "text/html", where

        panel.request = never_signs_in
        self.assertFalse(self._sync(panel))
        self.assertEqual(self._status()["state"], "login")

    def test_a_missing_export_page_is_reported_not_retried_forever(self):
        self._account()
        panel = FakePanel(HEAD + row("HC_A"), roster_status=404)
        self.assertFalse(self._sync(panel))
        st = self._status()
        self.assertIn("404", st["detail"])

    def test_no_profile_and_no_credentials_are_DIFFERENT_answers(self):
        self._account(profile="nosuchvendor")
        panel = FakePanel(HEAD + row("HC_A"))
        self._sync(panel)
        self.assertEqual(self._status()["state"], "no_profile")

        self.store.delete_radius_account(
            self.store.org_radius_accounts(ORG)[0]["id"])
        self._account(password_enc=None)
        self._sync(panel)
        self.assertEqual(self._status()["state"], "no_credentials")

    def test_an_undecryptable_password_never_reaches_the_panel(self):
        self._account(password_enc="bad")
        panel = FakePanel(HEAD + row("HC_A"))
        self.assertFalse(self._sync(panel))
        self.assertEqual(panel.credentials, [])
        self.assertEqual(self._status()["state"], "no_credentials")

    def test_a_partial_export_says_WHICH_columns_were_absent(self):
        self._account()
        panel = FakePanel(HEAD.replace(",Mobile", ",Phone") + row("HC_A"))
        self._sync(panel)
        st = self._status()
        self.assertEqual(st["state"], "partial")
        self.assertIn("mobile", st["detail"])

    def test_last_ok_at_survives_a_later_failure(self):
        self._account()
        self._sync(FakePanel(HEAD + row("HC_A")))
        ok_at = self._status()["last_ok_at"]
        self.assertIsNotNone(ok_at)
        self._sync(FakePanel(HEAD + row("HC_A"), login_page_status=500))
        st = self._status()
        self.assertEqual(st["state"], "unreachable")
        self.assertEqual(st["last_ok_at"], ok_at)

    def test_a_relinked_customer_replaces_the_old_link_rather_than_adding_one(self):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_A", "F0:A7:31:EA:7E:32")))
        self._sync(FakePanel(HEAD + row("HC_B", "F0:A7:31:EA:7E:32")))
        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "HC_B")

    def test_a_customer_dropped_from_one_export_is_kept_not_deleted(self):
        self._account()
        self._sync(FakePanel(HEAD + row("HC_A") + row("HC_B")))
        self._sync(FakePanel(HEAD + row("HC_A")))
        self.assertIsNotNone(self.store.get_radius_customer(ORG, "HC_B"))

    def test_org_scoping_holds(self):
        self.store.set_org("ispB")
        self._account()
        self._sync(FakePanel(HEAD + row("HC_A")))
        self.assertEqual(self.store.list_radius_customers("ispB"), [])
        self.assertEqual(self.store.org_radius_status("ispB"), [])

    def test_A_STALE_CUSTOMER_DOES_NOT_GO_ON_CLAIMING_ITS_OLD_MAC(self):
        # Rows are never deleted, so the customer who used to own this router is
        # still stored. Only the latest read of each panel feeds the join, or the
        # two of them look like an ambiguous MAC and the live one links nothing.
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_OLD", "F0:A7:31:EA:7E:32")))
        self._sync(FakePanel(HEAD + row("HC_NEW", "F0:A7:31:EA:7E:32")))
        self.assertIsNotNone(self.store.get_radius_customer(ORG, "HC_OLD"))
        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "HC_NEW")


class ForbiddenTest(Base):
    """Badri Fiber Net: the sign-in works and the export is not permitted."""

    def test_AN_EXPORT_REFUSED_BY_PERMISSION_IS_NOT_A_PASSWORD_PROBLEM(self):
        # Reported as `login` this sends an ISP to change a password that was
        # never wrong. Their panel signs in, then answers the export with a
        # redirect to /admin/notallowed.
        self._account()
        panel = FakePanel(HEAD + row("HC_A"), roster_lands_on="/admin/notallowed")
        self.assertFalse(self._sync(panel))
        st = self._status()
        self.assertEqual(st["state"], "forbidden")
        self.assertIn("/admin/notallowed", st["detail"])
        self.assertIn("permission", st["detail"])

    def test_a_bounce_back_to_the_sign_in_page_is_still_a_password_problem(self):
        self._account()
        panel = FakePanel(HEAD + row("HC_A"), roster_lands_on="/admin/login")
        self.assertFalse(self._sync(panel))
        self.assertEqual(self._status()["state"], "login")


class EncryptedLoginTest(Base):
    """MS Telecom's panel encrypts the credentials in the browser."""

    NONCE = "xj1tf7vkim0ouo8z8ik0m5j123nr9h4m"
    PAGE = ('<html><meta name="csrf-token" content="META1">'
            '<input type="hidden" name="_csrf-backend-admin" value="CSRF1">'
            '<input type="hidden" name="enckey" id="enckey" value="%s">'
            '</html>' % NONCE).encode()

    HEAD_ONE = ("ID,Username,Status,Expiration,Package,Branch,Area,"
                "\"First Name\",Mobile,Balance,\"CAF Number\","
                "\"Installation Address\",MAC\n")

    def _row(self, user, mac, name="A CUSTOMER"):
        return (f'1,{user},Active,"04 Oct, 2026 11:59 pm",PLAN,BR,AREA,'
                f'"{name}",9966793791,0,123,"addr","{mac}"\n')

    def test_the_credentials_go_out_ENCRYPTED_and_the_export_reads(self):
        self._account(profile="oneradius", base_url="https://cloud4.example.com",
                      username="ms_comm_admin")
        self._roster()
        self._usermac("F8:C4:F3:E7:BA:3E")
        panel = FakePanel(
            self.HEAD_ONE + self._row("smrrbalaji", "F8:C4:F3:E7:BA:3E",
                                      "SAMBHASHIVARAO"),
            login_page_body=self.PAGE)
        self.assertTrue(self._sync(panel))

        sent = panel.forms[0]
        self.assertEqual(sent["_csrf-backend-admin"], "CSRF1")
        self.assertEqual(sent["enckey"], self.NONCE)
        self.assertNotIn("ms_comm_admin", sent["LoginForm[username]"])
        self.assertNotIn("s3cret", sent["LoginForm[password]"])

        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "smrrbalaji")
        self.assertEqual(link["name"], "SAMBHASHIVARAO")

    def test_A_DUPLICATED_MAC_CELL_STILL_LINKS(self):
        self._account(profile="oneradius", base_url="https://cloud4.example.com")
        self._roster()
        self._usermac("F8:C4:F3:E7:BA:3E")
        panel = FakePanel(
            self.HEAD_ONE + self._row(
                "smrrbalaji", "F8:C4:F3:E7:BA:3E, F8:C4:F3:E7:BA:3E"),
            login_page_body=self.PAGE)
        self._sync(panel)
        link = self.store.radius_link_for(ORG, self.olt, "1.4")
        self.assertIsNotNone(link)
        self.assertEqual(link["match_by"], "mac")

    def test_A_LOGIN_PAGE_WITH_NO_NONCE_SENDS_NOTHING(self):
        self._account(profile="oneradius", base_url="https://cloud4.example.com")
        panel = FakePanel(self.HEAD_ONE, login_page_body=b"<html>plain</html>")
        self.assertFalse(self._sync(panel))
        self.assertEqual(panel.forms, [])
        st = self._status()
        self.assertEqual(st["state"], "login")
        self.assertIn("enckey", st["detail"])


class MultiPanelTest(Base):
    """Hansa asked for a second billing panel the week the first went live."""

    def _second(self):
        return self._account(base_url="https://cbp2.example.in",
                             username="hansa2", label="Second book")

    def test_A_SECOND_PANEL_DOES_NOT_WIPE_THE_FIRSTS_LINKS(self):
        # The link table is rewritten per ORG, so linking from one panel's roster
        # alone would delete the other's every sweep and the two would trade the
        # fleet back and forth for ever.
        first = self._account()
        second = self._second()
        self._roster()
        self._roster(onu_key="2.9", serial="BB:11:22:33:44:55", onu_id=9)
        self._usermac("F0:A7:31:EA:7E:32", "1.4")
        self._usermac("F0:A7:31:EA:7E:33", "2.9")

        self._sync(FakePanel(HEAD + row("HC_A", "F0:A7:31:EA:7E:32")), first)
        self._sync(FakePanel(HEAD + row("MS_B", "F0:A7:31:EA:7E:33")), second)

        one = self.store.radius_link_for(ORG, self.olt, "1.4")
        two = self.store.radius_link_for(ORG, self.olt, "2.9")
        self.assertEqual(one["username"], "HC_A")
        self.assertEqual(two["username"], "MS_B")
        self.assertEqual(one["account_id"], first)
        self.assertEqual(two["account_id"], second)

    def test_each_panel_keeps_its_own_status_and_customer_count(self):
        first = self._account()
        second = self._second()
        self._sync(FakePanel(HEAD + row("HC_A") + row("HC_B")), first)
        self._sync(FakePanel(HEAD + row("MS_A"), roster_status=404), second)

        by_id = {s["account_id"]: s for s in self.store.org_radius_status(ORG)}
        self.assertEqual(by_id[first]["state"], "ok")
        self.assertEqual(by_id[first]["customers"], 2)
        self.assertEqual(by_id[second]["state"], "unreachable")
        self.assertEqual(self.store.radius_customer_count(ORG, first), 2)
        self.assertEqual(self.store.radius_customer_count(ORG, second), 0)

    def test_ONE_USERNAME_IN_TWO_PANELS_IS_TWO_PEOPLE(self):
        first = self._account()
        second = self._second()
        self._sync(FakePanel(HEAD + row("1001", "", "ALICE")), first)
        self._sync(FakePanel(HEAD + row("1001", "", "BOB")), second)
        self.assertEqual(
            self.store.get_radius_customer(ORG, "1001", first)["name"], "ALICE")
        self.assertEqual(
            self.store.get_radius_customer(ORG, "1001", second)["name"], "BOB")

    def test_deleting_one_panel_leaves_the_other_untouched(self):
        first = self._account()
        second = self._second()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_A", "F0:A7:31:EA:7E:32")), first)
        self._sync(FakePanel(HEAD + row("MS_B")), second)

        self.assertTrue(self.store.delete_radius_account(second))
        self.assertEqual(len(self.store.org_radius_accounts(ORG)), 1)
        self.assertEqual(self.store.radius_customer_count(ORG, second), 0)
        self.assertIsNotNone(self.store.radius_link_for(ORG, self.olt, "1.4"))
        self.assertEqual(self.store.radius_customer_count(ORG, first), 1)

    def test_a_disabled_panel_stops_feeding_the_join(self):
        first = self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_A", "F0:A7:31:EA:7E:32")), first)
        self.assertIsNotNone(self.store.radius_link_for(ORG, self.olt, "1.4"))

        self.store.set_radius_account(
            ORG, account_id=first, profile="cbp",
            base_url="https://cbp.example.in", username="hansa",
            password_enc=None, enabled=False)
        syncer = RadiusSyncer(self.store, FakeBox(), self.cfg)
        syncer.relink_org(ORG)
        self.assertIsNone(self.store.radius_link_for(ORG, self.olt, "1.4"))


class MigrationTest(Base):
    """An install written when an org had exactly one panel."""

    OLD = """
    DROP TABLE IF EXISTS radius_accounts;
    DROP TABLE IF EXISTS radius_customers;
    DROP TABLE IF EXISTS radius_links;
    DROP TABLE IF EXISTS radius_status;
    CREATE TABLE radius_accounts (
        org_id TEXT PRIMARY KEY, profile TEXT NOT NULL, base_url TEXT NOT NULL,
        username TEXT, password_enc TEXT, enabled INTEGER NOT NULL DEFAULT 1,
        updated_by TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE radius_customers (
        org_id TEXT NOT NULL, username TEXT NOT NULL, name TEXT, mac TEXT,
        mobile TEXT, alt_mobile TEXT, acno TEXT,
        status TEXT NOT NULL DEFAULT 'unknown', expiry TEXT, package TEXT,
        branch TEXT, area TEXT, address TEXT, balance TEXT,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        PRIMARY KEY (org_id, username));
    CREATE TABLE radius_links (
        org_id TEXT NOT NULL, device_id INTEGER NOT NULL REFERENCES org_devices(id),
        onu_key TEXT NOT NULL, username TEXT NOT NULL, match_by TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY (org_id, device_id, onu_key));
    CREATE TABLE radius_status (
        org_id TEXT PRIMARY KEY, profile TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, detail TEXT, customers INTEGER NOT NULL DEFAULT 0,
        linked INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        last_ok_at TEXT);
    """

    def _old_install(self):
        import sqlite3
        conn = sqlite3.connect(self.cfg.central_db)
        conn.executescript(self.OLD)
        conn.execute(
            "INSERT INTO radius_accounts VALUES (?,?,?,?,?,?,?,?)",
            (ORG, "cbp", "https://cbp.excellmedia.in", "hansa", "ENC", 1,
             "haneesh", RECENT))
        conn.execute(
            "INSERT INTO radius_customers (org_id, username, name, mac, status,"
            " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
            (ORG, "HC_GOGU", "VENKATESWARLU GOGU", "F0:A7:31:EA:7E:32",
             "active", RECENT, RECENT))
        conn.execute(
            "INSERT INTO radius_links VALUES (?,?,?,?,?,?)",
            (ORG, self.olt, "1.4", "HC_GOGU", "mac", RECENT))
        conn.execute(
            "INSERT INTO radius_status (org_id, profile, state, customers,"
            " linked, updated_at, last_ok_at) VALUES (?,?,?,?,?,?,?)",
            (ORG, "cbp", "ok", 794, 415, RECENT, RECENT))
        conn.commit()
        conn.close()
        return CentralStore(self.cfg.central_db)

    def test_THE_ONE_PANEL_AND_EVERY_ROW_IT_OWNS_CARRY_ACROSS(self):
        store = self._old_install()

        accounts = store.org_radius_accounts(ORG)
        self.assertEqual(len(accounts), 1)
        account_id = accounts[0]["id"]
        self.assertEqual(accounts[0]["base_url"], "https://cbp.excellmedia.in")
        self.assertEqual(accounts[0]["password_enc"], "ENC")

        cust = store.get_radius_customer(ORG, "HC_GOGU")
        self.assertEqual(cust["name"], "VENKATESWARLU GOGU")
        self.assertEqual(cust["account_id"], account_id)

        link = store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "HC_GOGU")
        self.assertEqual(link["account_id"], account_id)

        st = store.get_radius_status(account_id)
        self.assertEqual((st["state"], st["customers"], st["linked"]),
                         ("ok", 794, 415))
        self.assertEqual(st["org_id"], ORG)

    def test_the_migration_is_idempotent(self):
        self._old_install()
        again = CentralStore(self.cfg.central_db)
        self.assertEqual(len(again.org_radius_accounts(ORG)), 1)
        self.assertIsNotNone(again.get_radius_customer(ORG, "HC_GOGU"))

    def test_the_carried_over_panel_syncs_and_relinks(self):
        store = self._old_install()
        self.store = store
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32")))
        link = store.radius_link_for(ORG, self.olt, "1.4")
        self.assertEqual(link["username"], "HC_GOGU")
        self.assertEqual(self._status()["state"], "ok")


class IdentityTest(Base):

    def _linked(self, name="VENKATESWARLU GOGU", mobile="9966793791"):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32", name,
                                        mobile=mobile)))

    def test_the_roster_row_CARRIES_the_radius_name(self):
        self._linked()
        rows = self.store.list_onu_optics(ORG, self.olt)
        self.assertEqual(rows[0]["radius_name"], "VENKATESWARLU GOGU")
        self.assertEqual(rows[0]["radius_mobile"], "9966793791")
        self.assertEqual(rows[0]["radius_match"], "mac")

    def test_org_onu_rows_carries_it_too_so_every_screen_agrees(self):
        self._linked()
        rows = self.store.org_onu_rows(ORG)
        self.assertEqual(rows[0]["radius_name"], "VENKATESWARLU GOGU")

    def test_display_name_prefers_radius_over_the_walked_name(self):
        self._linked()
        row_ = self.store.list_onu_optics(ORG, self.olt)[0]
        self.assertEqual(onuroster.display_name(row_), "HC_GOGU")

    def test_THE_USERNAME_IS_THE_IDENTITY_AND_THE_NAME_IS_EXTRA(self):
        # The ISPs' own instruction, 2026-08-17: "everybody recognise the user
        # by username only". Before it, this slot printed VENKATESWARLU GOGU —
        # and printed the username anyway wherever a worker had hand-typed it
        # into the survey label, which is 253 of the 289 surveyed, linked
        # subscribers on the live fleet.
        self._linked()
        row_ = self.store.list_onu_optics(ORG, self.olt)[0]
        self.assertEqual(onuroster.display_name(row_), "HC_GOGU")
        self.assertEqual(row_["radius_name"], "VENKATESWARLU GOGU")

    def test_the_billing_NAME_still_names_a_row_carrying_no_username(self):
        # The name is demoted, never dropped: a book that exports a customer
        # with no username still has a subscriber to name.
        self.assertEqual(
            onuroster.display_name({"radius_name": "VENKATESWARLU GOGU",
                                    "name": "walked", "serial": "AA"}),
            "VENKATESWARLU GOGU")

    def test_AN_OPERATOR_TYPED_LABEL_STILL_OUTRANKS_RADIUS(self):
        self._linked()
        self.store.set_onu_place(ORG, "AA:11:22:33:44:55", 1.0, 2.0,
                                 "WHAT THE WORKER TYPED", None, witness=False)
        row_ = self.store.list_onu_optics(ORG, self.olt)[0]
        self.assertEqual(onuroster.display_name(row_), "WHAT THE WORKER TYPED")

    def test_an_unlinked_ONU_carries_nothing_rather_than_a_blank_claim(self):
        self._account()
        self._roster(name="sub")
        row_ = self.store.list_onu_optics(ORG, self.olt)[0]
        self.assertIsNone(row_["radius_name"])
        self.assertEqual(onuroster.display_name(row_), "sub")


class PlaceIdentityTest(Base):
    """The map's pins get the same identity, off one grouped pass.

    `list_onu_places` resolved the customer through a correlated sub-select per
    row — 721 ms for 357 pins on a copy of prod, on a read the map makes on
    load. It is a CTE joined once now (~32 ms WITH the username added), so these
    pin the behaviour the rewrite must keep: both columns, the per-column
    ambiguity guard, and no fabricated claim for an unlinked pin.
    """

    def _place(self, mac="AA:11:22:33:44:55", label=None):
        self.store.set_onu_place(ORG, mac, 1.0, 2.0, label, None, witness=False)

    def _place_row(self, mac="AA:11:22:33:44:55"):
        return next(p for p in self.store.list_onu_places(ORG)
                    if p["mac"] == mac)

    def test_a_placed_linked_subscriber_carries_BOTH_identities(self):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32",
                                        "VENKATESWARLU GOGU")))
        self._place()
        p = self._place_row()
        self.assertEqual(p["radius_username"], "HC_GOGU")
        self.assertEqual(p["radius_name"], "VENKATESWARLU GOGU")
        # And the pin is NAMED by the username, like every other surface.
        self.assertEqual(onuroster.display_name(p), "HC_GOGU")

    def test_an_unplaced_pin_of_an_unlinked_ONU_claims_neither(self):
        self._account()
        self._roster()
        self._place()
        p = self._place_row()
        self.assertIsNone(p["radius_username"])
        self.assertIsNone(p["radius_name"])

    def test_ONE_MAC_ON_TWO_SLOTS_WITH_TWO_CUSTOMERS_NAMES_NEITHER(self):
        # The guard the map depends on: a mark and its card may never name one
        # subscriber two ways, so an ambiguous MAC gets NULL, not a pick.
        self._account()
        self._roster()
        self.store.upsert_onu_optics(
            ORG, self.olt, "1.9", pon_port="EPON0/1", onu_id=9, name="sub2",
            serial="AA:11:22:33:44:55", state="online", rx_dbm=-21.0,
            tx_dbm=None, olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None,
            rx_ref_at=None, severity="ok", ts=RECENT)
        self._usermac("F0:A7:31:EA:7E:32", onu_key="1.4")
        self._usermac("F0:A7:31:EA:7E:99", onu_key="1.9")
        self._sync(FakePanel(
            HEAD
            + row("HC_GOGU", "F0:A7:31:EA:7E:32", "VENKATESWARLU GOGU")
            + row("HC_OTHER", "F0:A7:31:EA:7E:99", "SOMEBODY ELSE")))
        self._place()
        p = self._place_row()
        self.assertIsNone(p["radius_username"])
        self.assertIsNone(p["radius_name"])

    def test_located_only_still_filters_and_still_carries_the_identity(self):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32",
                                        "VENKATESWARLU GOGU")))
        self._place()
        located = self.store.list_onu_places(ORG, located_only=True)
        self.assertEqual([p["mac"] for p in located], ["AA:11:22:33:44:55"])
        self.assertEqual(located[0]["radius_username"], "HC_GOGU")


class SearchApiTest(Base):

    def setUp(self):
        super().setUp()
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_GOGU", "F0:A7:31:EA:7E:32",
                                        "VENKATESWARLU GOGU")))
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        super().tearDown()

    def _cookie(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": "owner",
                                      "password": "ownerpassword"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        return cookie

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": self._cookie()})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw)

    def test_a_customer_is_found_by_NAME(self):
        status, body = self._get("/api/inventory/onu-search?q=VENKATESWARLU")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["matches"]), 1)
        onu = body["matches"][0]["onus"][0]
        self.assertEqual(onu["radius_name"], "VENKATESWARLU GOGU")

    def test_a_customer_is_found_by_RADIUS_USERNAME(self):
        status, body = self._get("/api/inventory/onu-search?q=HC_GOGU")
        self.assertEqual(len(body["matches"]), 1)

    def test_a_customer_is_found_by_MOBILE(self):
        status, body = self._get("/api/inventory/onu-search?q=9966793791")
        self.assertEqual(len(body["matches"]), 1)

    def test_a_needle_matching_nothing_still_answers_cleanly(self):
        status, body = self._get("/api/inventory/onu-search?q=ZZZNOTHING")
        self.assertEqual(status, 200)
        self.assertEqual(body["matches"], [])

    def test_the_subscriber_reply_carries_the_customer_and_the_sync_status(self):
        status, body = self._get(
            "/api/inventory/subscriber?mac=AA:11:22:33:44:55")
        self.assertEqual(status, 200)
        self.assertEqual(body["radius"]["name"], "VENKATESWARLU GOGU")
        self.assertEqual(body["radius"]["match_by"], "mac")
        self.assertEqual(body["radius_panels"][0]["state"], "ok")


class ConfigureApiTest(Base):

    def setUp(self):
        super().setUp()
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        super().tearDown()

    def _cookie(self, who="owner"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": who,
                                      "password": f"{who}password"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        return cookie

    def _call(self, method, path, body=None, who="owner"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": self._cookie(who)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body) if body else None,
                     headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def test_an_owner_can_store_a_panel_and_the_password_never_comes_back(self):
        status, _ = self._call("POST", "/api/inventory/radius", {
            "profile": "cbp", "base_url": "https://cbp.example.in",
            "username": "hansa", "password": "s3cret"})
        self.assertEqual(status, 200)
        status, body = self._call("GET", f"/api/inventory/radius?org_id={ORG}")
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"][0]["username"], "hansa")
        self.assertTrue(body["accounts"][0]["password_set"])
        self.assertNotIn("s3cret", json.dumps(body))
        self.assertNotIn("password_enc", json.dumps(body))

    def test_the_stored_password_is_encrypted_at_rest(self):
        self._call("POST", "/api/inventory/radius", {
            "profile": "cbp", "base_url": "https://cbp.example.in",
            "username": "hansa", "password": "s3cret"})
        row = self.store.org_radius_accounts(ORG)[0]
        self.assertNotIn("s3cret", row["password_enc"])

    def test_saving_again_without_a_password_keeps_the_stored_one(self):
        _, saved = self._call("POST", "/api/inventory/radius", {
            "profile": "cbp", "base_url": "https://cbp.example.in",
            "username": "hansa", "password": "s3cret"})
        before = self.store.org_radius_accounts(ORG)[0]["password_enc"]
        self._call("POST", "/api/inventory/radius", {
            "id": saved["id"], "profile": "cbp",
            "base_url": "https://cbp.example.in", "username": "hansa2"})
        after = self.store.org_radius_accounts(ORG)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["password_enc"], before)
        self.assertEqual(after[0]["username"], "hansa2")

    def test_saving_the_same_panel_twice_does_not_make_two_of_it(self):
        body = {"profile": "cbp", "base_url": "https://cbp.example.in",
                "username": "hansa", "password": "s3cret"}
        self._call("POST", "/api/inventory/radius", dict(body))
        self._call("POST", "/api/inventory/radius", dict(body))
        self.assertEqual(len(self.store.org_radius_accounts(ORG)), 1)

    def test_a_base_url_carrying_a_path_is_refused(self):
        status, body = self._call("POST", "/api/inventory/radius", {
            "profile": "cbp", "base_url": "https://cbp.example.in/admin/dashboard",
            "username": "u", "password": "p"})
        self.assertEqual(status, 422)

    def test_an_unknown_profile_is_refused(self):
        status, _ = self._call("POST", "/api/inventory/radius", {
            "profile": "nosuchvendor", "base_url": "https://cbp.example.in",
            "username": "u", "password": "p"})
        self.assertEqual(status, 422)

    def test_a_worker_reaches_none_of_it(self):
        for method, path, body in (
                ("GET", f"/api/inventory/radius?org_id={ORG}", None),
                ("POST", "/api/inventory/radius", {"profile": "cbp"}),
                ("POST", "/api/inventory/radius/sync", {})):
            status, _ = self._call(method, path, body, who="field")
            self.assertEqual(status, 403, f"{method} {path} was not refused")


class DeleteCascadeTest(Base):

    def test_deleting_the_org_sweeps_every_radius_table(self):
        self._account()
        self._roster()
        self._usermac("F0:A7:31:EA:7E:32")
        self._sync(FakePanel(HEAD + row("HC_A", "F0:A7:31:EA:7E:32")))
        self.store.set_radius_profile("cbp", {}, org_id=ORG)
        self.store.delete_org(ORG)
        for table in ("radius_accounts", "radius_customers", "radius_links",
                      "radius_status", "radius_profiles"):
            with self.store._connect() as conn:
                left = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE org_id=?",
                    (ORG,)).fetchone()["n"]
            self.assertEqual(left, 0, f"{table} kept rows for a deleted org")


if __name__ == "__main__":
    unittest.main()
