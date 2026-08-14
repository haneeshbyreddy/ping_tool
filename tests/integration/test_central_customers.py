"""The customers page: the billing book joined to the network, owner-only.

The page is a directory plus fault triage, so what the tests pin is the JOIN's
honesty: a paying customer reads "dark" only off a fresh walk of an UP OLT (a
down OLT freezes its readings — the outage owns that page), an unlinked
customer carries a provable reason rather than a guess, and every tile count is
derived from the same rows the list shows.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import auth  # noqa: E402
from wisp.central.radius import RadiusLink  # noqa: E402
from wisp.central.server import make_server  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.config import Config  # noqa: E402

ORG = "ispA"


# Stamped at CALL time, never at import: discovery imports every test module up
# front, and by the time this file's turn comes an import-time "now" is older
# than the 180s state-staleness gate — every linked customer then reads frozen,
# which is the collect being right about a stale test fixture.
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


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
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        self.account = self.store.set_radius_account(
            ORG, profile="cbp", base_url="https://cbp.example.in",
            username="hansa", password_enc="enc", updated_by="t")
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _state(self, state, ts=None):
        self.store.write_device_states(ORG, [(self.olt, state, None, None, None)],
                                       ts or _now())

    def _roster(self, onu_key="1.4", serial="AA:11:22:33:44:55", name="sub",
                state="online"):
        self.store.upsert_onu_optics(
            ORG, self.olt, onu_key, pon_port="EPON0/1", onu_id=4, name=name,
            serial=serial, state=state, rx_dbm=-21.0, tx_dbm=None,
            olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=_now())

    def _customer(self, username, *, name="A CUSTOMER", mac=None,
                  status="active", expiry="06/01/2027 09:24", ts=None):
        self.store.upsert_radius_customers(ORG, self.account, [{
            "username": username, "name": name, "mac": mac, "mobile": "9999",
            "status": status, "expiry": expiry, "package": "PLAN",
            "branch": "HALIYA"}], ts or _now())

    def _link(self, username, onu_key="1.4", match_by="mac"):
        self.store.replace_radius_links(ORG, [
            RadiusLink(self.olt, onu_key, username, match_by, self.account)],
            _now())

    def _cookie(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        return cookie

    def _get(self, path="/api/inventory/customers", **who):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": self._cookie(**who)})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else {}


class CustomersApiTest(Base):

    def test_the_owner_reads_the_directory_with_the_join(self):
        self._state("UP")
        self._roster()
        self._customer("HC_GOGU", name="VENKATESWARLU GOGU",
                       mac="F0:A7:31:EA:7E:32")
        self._link("HC_GOGU")
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"]["customers"], 1)
        row = body["customers"][0]
        self.assertEqual(row["net"], "online")
        self.assertEqual(row["device_name"], "HLY-OLT-1")
        self.assertEqual(row["onu_mac"], "AA:11:22:33:44:55")
        self.assertEqual(row["pon_port"], "EPON0/1")

    def test_A_WORKER_IS_REFUSED(self):
        # The full billing book with phone numbers is the largest PII surface in
        # the product; a worker keeps seeing single customers through the
        # subscriber panel, never the enumeration.
        status, _ = self._get(username="field", password="fieldpassword")
        self.assertEqual(status, 403)

    def test_expiry_parses_under_the_PROFILE_DECLARED_format_only(self):
        self._customer("HC_A", expiry="22/11/2026 07:55")
        status, body = self._get()
        row = body["customers"][0]
        self.assertEqual(row["expiry"], "22/11/2026 07:55")
        self.assertEqual(row["expiry_at"], "2026-11-22T07:55:00")
        self.assertIsInstance(row["days_left"], int)

    def test_junk_expiry_stays_UNPARSED_never_guessed(self):
        self._customer("HC_A", expiry="soon")
        _, body = self._get()
        row = body["customers"][0]
        self.assertEqual(row["expiry"], "soon")
        self.assertIsNone(row["expiry_at"])
        self.assertIsNone(row["days_left"])

    def test_A_PAYING_CUSTOMER_IS_DARK_ONLY_OFF_A_FRESH_WALK_OF_AN_UP_OLT(self):
        self._state("UP")
        self._roster(state="offline")
        self._customer("HC_DARK", mac="F0:A7:31:EA:7E:32")
        self._link("HC_DARK")
        _, body = self._get()
        self.assertEqual(body["customers"][0]["net"], "dark")
        self.assertEqual(body["counts"]["paying_dark"], 1)
        self.assertEqual(body["counts"]["paying_frozen"], 0)

    def test_A_DOWN_OLT_FREEZES_ITS_CUSTOMERS_instead_of_calling_them_dark(self):
        # "6 of 6 dark" behind a down OLT is the OLT's outage restated per
        # customer; the ICMP page owns it. Same refusal drops.py and ponfault
        # already keep.
        self._state("DOWN")
        self._roster(state="offline")
        self._customer("HC_FROZE", mac="F0:A7:31:EA:7E:32")
        self._link("HC_FROZE")
        _, body = self._get()
        self.assertEqual(body["customers"][0]["net"], "frozen")
        self.assertEqual(body["counts"]["paying_dark"], 0)
        self.assertEqual(body["counts"]["paying_frozen"], 1)

    def test_AN_EXPIRED_DARK_CUSTOMER_IS_NOT_A_PAYING_DARK_ONE(self):
        self._state("UP")
        self._roster(state="offline")
        self._customer("HC_EXP", mac="F0:A7:31:EA:7E:32", status="expired")
        self._link("HC_EXP")
        _, body = self._get()
        self.assertEqual(body["customers"][0]["net"], "dark")
        self.assertEqual(body["counts"]["paying_dark"], 0)

    def test_an_unlinked_customer_carries_a_PROVABLE_reason(self):
        self._state("UP")
        self._roster()
        self.store.upsert_user_macs(ORG, self.olt, [
            {"onu_key": "1.4", "mac": "F0:A7:31:EA:7E:32", "vlan": "1900",
             "kind": "Dynamic", "port_label": "EPON0/1:4"}], _now())
        self._customer("HC_NOMAC", mac=None)
        self._customer("HC_UNSEEN", mac="AA:AA:AA:AA:AA:01")
        self._customer("HC_SEEN", mac="F0:A7:31:EA:7E:32")
        _, body = self._get()
        by_user = {r["username"]: r for r in body["customers"]}
        self.assertEqual(by_user["HC_NOMAC"]["reason"], "no_mac")
        self.assertEqual(by_user["HC_UNSEEN"]["reason"], "mac_unseen")
        self.assertEqual(by_user["HC_SEEN"]["reason"], "mac_unresolved")
        for key in ("no_mac", "mac_unseen", "mac_unresolved"):
            self.assertIn(key, body["reasons"])

    def test_EVERY_COUNT_IS_A_RECOUNT_OF_THE_ROWS_IT_SITS_OVER(self):
        self._state("UP")
        self._roster(onu_key="1.4", serial="AA:11:22:33:44:55", state="offline")
        self._roster(onu_key="1.5", serial="AA:11:22:33:44:66", state="online")
        self._customer("HC_D", mac="F0:A7:31:EA:7E:01")
        self._customer("HC_O", mac="F0:A7:31:EA:7E:02")
        self._customer("HC_U", mac=None, status="expired")
        self.store.replace_radius_links(ORG, [
            RadiusLink(self.olt, "1.4", "HC_D", "mac", self.account),
            RadiusLink(self.olt, "1.5", "HC_O", "mac", self.account)], _now())
        _, body = self._get()
        rows = body["customers"]
        c = body["counts"]
        self.assertEqual(c["customers"], len(rows))
        self.assertEqual(c["active"],
                         sum(1 for r in rows if r["status"] == "active"))
        self.assertEqual(c["linked"],
                         sum(1 for r in rows if r["net"] != "unlinked"))
        self.assertEqual(c["paying_dark"],
                         sum(1 for r in rows
                             if r["status"] == "active" and r["net"] == "dark"))

    def test_a_customer_absent_from_the_latest_read_is_KEPT_and_marked(self):
        self._customer("HC_OLD", ts="2026-08-01T00:00:00+00:00")
        self._customer("HC_NEW")
        _, body = self._get()
        by_user = {r["username"]: r for r in body["customers"]}
        self.assertFalse(by_user["HC_OLD"]["in_last_read"])
        self.assertTrue(by_user["HC_NEW"]["in_last_read"])

    def test_the_reply_carries_the_panel_statuses(self):
        self.store.set_radius_status(ORG, self.account, "ok", None,
                                     profile="cbp", customers=1, linked=1)
        _, body = self._get()
        self.assertEqual(body["panels"][0]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
