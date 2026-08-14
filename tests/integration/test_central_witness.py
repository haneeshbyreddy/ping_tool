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

from wisp.config import Config
from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore

LAT, LNG = 15.8497, 74.4977
MAC = "A4:F2:1B:9C:44:01"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


class WitnessClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.olt = self.store.create_org_device("ispA", {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.store.set_device_assignees(
            "ispA", self.olt,
            [next(u["id"] for u in self.store.list_users("ispA")
                  if u["username"] == "ravi")], "owner")
        self.now = datetime.now(timezone.utc)
        self._onu(MAC)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()


    def _req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return setcookie.split(";")[0] if setcookie else None

    def _owner(self):
        return self._login("owner", "ownerpassword")

    def _onu(self, serial, *, org="ispA", state="online", age_s=0):
        self.store.upsert_onu_optics(
            org, self.olt, f"0/1.{serial[-2:]}", pon_port="EPON0/1", onu_id=3,
            name=None, serial=serial, state=state, rx_dbm=-21.0, tx_dbm=2.0,
            olt_rx_dbm=-20.0, distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=_iso(self.now - timedelta(seconds=age_s)))

    def _survey(self, mac=MAC, cookie=None):
        return self._req("POST", "/api/inventory/field-onu", {
            "org_id": "ispA", "mac": mac, "lat": LAT, "lng": LNG,
            "accuracy_m": 7.5, "source": "gps",
            "label": "SRIKRISHNA TIMBER", "phone": "9949345676",
        }, cookie=cookie or self._login("ravi", "ravipassword"))

    def _place(self, mac=MAC, cookie=None, **extra):
        body = {"org_id": "ispA", "mac": mac, "lat": LAT, "lng": LNG}
        body.update(extra)
        return self._req("POST", "/api/inventory/onu-place", body,
                         cookie=cookie or self._owner())

    def _claim(self, witness, mac=MAC, cookie=None):
        return self._req("POST", "/api/inventory/onu-witness",
                         {"org_id": "ispA", "mac": mac, "witness": witness},
                         cookie=cookie or self._owner())

    def _rec(self, mac=MAC, org="ispA"):
        return self.store.get_onu_place(org, mac)


    def test_moving_a_surveyed_pin_does_NOT_make_a_witness(self):
        self.assertEqual(self._survey()[0], 200)
        self.assertEqual(self._rec()["witness"], 0)

        status, _ = self._place(lat=LAT + 0.0002, lng=LNG)
        self.assertEqual(status, 200)
        rec = self._rec()
        self.assertEqual(rec["witness"], 0, "a pin move must not vouch for power")
        self.assertAlmostEqual(rec["lat"], LAT + 0.0002)

    def test_editing_contact_details_from_the_desktop_does_NOT_make_a_witness(self):
        self.assertEqual(self._survey()[0], 200)
        status, _ = self._place(label="SRIKRISHNA TIMBER", phone="9000000001")
        self.assertEqual(status, 200)
        rec = self._rec()
        self.assertEqual(rec["witness"], 0)
        self.assertEqual(rec["phone"], "9000000001")

    def test_a_brand_new_desktop_pin_is_a_plain_subscriber(self):
        self.assertEqual(self._place()[0], 200)
        self.assertEqual(self._rec()["witness"], 0)

    def test_the_desktop_can_still_make_the_claim_by_naming_it(self):
        self.assertEqual(self._place()[0], 200)
        self.assertEqual(self._claim(True)[0], 200)
        self.assertEqual(self._rec()["witness"], 1)
        self.assertEqual(self.store.onu_place_macs("ispA"), {MAC})

    def test_the_location_route_CANNOT_be_talked_into_a_claim(self):
        self.assertEqual(self._place(witness=True)[0], 200)
        self.assertEqual(self._rec()["witness"], 0)
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    def test_a_later_move_KEEPS_an_existing_claim(self):
        self.assertEqual(self._place()[0], 200)
        self.assertEqual(self._claim(True)[0], 200)
        self.assertEqual(self._place(lat=LAT + 0.0003, lng=LNG)[0], 200)
        self.assertEqual(self._rec()["witness"], 1)


    def test_the_claim_can_be_made_and_withdrawn_without_touching_the_pin(self):
        self.assertEqual(self._survey()[0], 200)
        before = self._rec()

        self.assertEqual(self._claim(True)[0], 200)
        self.assertEqual(self._rec()["witness"], 1)

        status, body = self._claim(False)
        self.assertEqual(status, 200)
        self.assertIs(body["witness"], False)
        after = self._rec()
        self.assertEqual(after["witness"], 0)
        for field in ("lat", "lng", "label", "phone", "accuracy_m",
                      "place_source", "placed_by", "placed_at"):
            self.assertEqual(after[field], before[field], field)

    def test_a_claim_needs_no_pin(self):
        self.assertEqual(self._req("POST", "/api/inventory/onu-contact", {
            "org_id": "ispA", "mac": MAC, "label": "WATER TANK"},
            cookie=self._owner())[0], 200)
        self.assertEqual(self._claim(True)[0], 200)
        self.assertEqual(self.store.onu_place_macs("ispA"), {MAC})
        self.assertIsNone(self._rec()["lat"])

    def test_a_claim_about_an_unrecorded_subscriber_is_a_404(self):
        status, _ = self._claim(True, mac="00:00:00:00:00:99")
        self.assertEqual(status, 404)
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    def test_withdrawing_prunes_a_record_that_was_ONLY_a_claim(self):
        self.store.set_onu_contact("ispA", MAC, "WATER TANK", None, None)
        self.store.set_onu_witness("ispA", MAC, True)
        self.store.set_onu_contact("ispA", MAC, None, None, None)
        self.assertIsNotNone(self._rec())
        self.assertEqual(self._rec()["witness"], 1)
        self.assertEqual(self._claim(False)[0], 200)
        self.assertIsNone(self._rec())

    def test_witness_must_be_a_real_boolean(self):
        for bad in ("true", 1, None, "yes"):
            status, _ = self._req("POST", "/api/inventory/onu-witness",
                                  {"org_id": "ispA", "mac": MAC, "witness": bad},
                                  cookie=self._owner())
            self.assertEqual(status, 422, bad)


    def test_a_worker_cannot_touch_the_claim(self):
        self.assertEqual(self._survey()[0], 200)
        cookie = self._login("ravi", "ravipassword")
        status, _ = self._claim(True, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(self._rec()["witness"], 0)

    def test_another_orgs_owner_cannot_claim_this_orgs_subscriber(self):
        self.assertEqual(self._survey()[0], 200)
        status, _ = self._req(
            "POST", "/api/inventory/onu-witness",
            {"org_id": "ispA", "mac": MAC, "witness": True},
            cookie=self._login("bowner", "bownerpassword"))
        self.assertIn(status, (403, 404))
        self.assertEqual(self._rec()["witness"], 0)


    def test_the_optical_row_says_WHICH_claim_a_pin_is(self):
        self.assertEqual(self._survey()[0], 200)
        status, body = self._req(
            "GET", f"/api/inventory/optics?device_id={self.olt}",
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        row = next(o for o in body["onus"] if o["serial"] == MAC)
        self.assertIsNotNone(row["place"])
        self.assertIs(row["place"]["witness"], False)

        self.assertEqual(self._claim(True)[0], 200)
        _, body = self._req("GET", f"/api/inventory/optics?device_id={self.olt}",
                            cookie=self._owner())
        row = next(o for o in body["onus"] if o["serial"] == MAC)
        self.assertIs(row["place"]["witness"], True)

    def test_unpinning_still_retracts_the_claim(self):
        self.assertEqual(self._place()[0], 200)
        self.assertEqual(self._claim(True)[0], 200)
        self.assertEqual(self._place(lat=None, lng=None)[0], 200)
        self.assertEqual(self.store.onu_place_macs("ispA"), set())


if __name__ == "__main__":
    unittest.main()
