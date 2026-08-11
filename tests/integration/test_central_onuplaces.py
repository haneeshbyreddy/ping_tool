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


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


class OnuPlacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "field", "fieldpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.olt = self.store.create_org_device("ispA", {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.now = datetime.now(timezone.utc)
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
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _owner(self):
        return self._login("owner", "ownerpassword")

    def _onu(self, org, device_id, onu_key, serial, *, state="online",
             pon="0/1", onu_id=1, name=None, age_s=0):
        ts = _iso(self.now - timedelta(seconds=age_s))
        self.store.upsert_onu_optics(
            org, device_id, onu_key, pon_port=pon, onu_id=onu_id, name=name,
            serial=serial, state=state, rx_dbm=-21.0, tx_dbm=2.0,
            olt_rx_dbm=-20.0, distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=ts)

    def _place(self, mac, lat=15.85, lng=74.5, cookie=None, **kw):
        return self._req("POST", "/api/inventory/onu-place",
                         {"mac": mac, "lat": lat, "lng": lng, **kw},
                         cookie=cookie or self._owner())

    def _places(self, cookie=None):
        status, body, _ = self._req("GET", "/api/inventory/onu-places",
                                    cookie=cookie or self._owner())
        return status, body


    def test_placing_a_reference_onu_lists_it_against_its_roster_row(self):
        self._onu("ispA", self.olt, "0/6.12", "A4:F2:1B:9C:44:01", pon="0/6",
                  onu_id=12, name="tower-hut")
        status, _, _ = self._place("A4:F2:1B:9C:44:01", label="Hill tower")
        self.assertEqual(status, 200)
        status, body = self._places()
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["places"]), 1)
        p = body["places"][0]
        self.assertTrue(p["matched"])
        self.assertEqual(p["label"], "HILL TOWER")
        self.assertEqual(p["device_id"], self.olt)
        self.assertEqual(p["device_name"], "HILL-OLT-1")
        self.assertEqual(p["pon_port"], "0/6")
        self.assertEqual(p["onu_id"], 12)
        self.assertEqual(p["state"], "online")
        self.assertAlmostEqual(p["lat"], 15.85)

    def test_placing_again_MOVES_the_pin_rather_than_duplicating_it(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._place("AA:BB", lat=15.0, lng=74.0)
        self._place("AA:BB", lat=16.0, lng=75.0, label="moved")
        _, body = self._places()
        self.assertEqual(len(body["places"]), 1)
        self.assertAlmostEqual(body["places"][0]["lat"], 16.0)
        self.assertEqual(body["places"][0]["label"], "MOVED")

    def _unplace(self, mac):
        status, body, _ = self._req("POST", "/api/inventory/onu-place",
                                    {"mac": mac, "lat": None, "lng": None},
                                    cookie=self._owner())
        self.assertEqual(status, 200, body)

    def test_clearing_a_BARE_reference_point_still_leaves_no_row_behind(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._place("AA:BB")
        self._unplace("AA:BB")
        self.assertEqual(self._places()[1]["places"], [])
        self.assertEqual(self.store.onu_place_macs("ispA"), set())
        self.assertIsNone(self.store.get_onu_place("ispA", "AA:BB"))

    def test_removing_a_pin_does_NOT_forget_who_the_subscriber_is(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._place("AA:BB", label="Ramesh", phone="9876543210")
        self._unplace("AA:BB")
        self.assertEqual(self._places()[1]["places"], [])
        rec = self.store.get_onu_place("ispA", "AA:BB")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["label"], "RAMESH")
        self.assertEqual(rec["phone"], "9876543210")
        self.assertIsNone(rec["lat"])
        self.assertIsNone(rec["lng"])

    def test_unplacing_RETRACTS_the_witness_claim(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._place("AA:BB", label="Water tank", phone="9876543210")
        self.assertEqual(self.store.onu_place_macs("ispA"), set(),
                         "a pin is not a claim")
        self.store.set_onu_witness("ispA", "AA:BB", True)
        self.assertEqual(self.store.onu_place_macs("ispA"), {"AA:BB"})
        self._unplace("AA:BB")
        self.assertEqual(self.store.onu_place_macs("ispA"), set())
        self.assertEqual(self.store.get_onu_place("ispA", "AA:BB")["label"],
                         "WATER TANK")

    def test_provenance_goes_with_the_coordinates_it_describes(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self.store.place_onu_in_field(
            "ispA", "AA:BB", 15.85, 74.5, witness=False, accuracy_m=6.0,
            source="gps", placed_by="field", label="RAMESH", phone="9876543210")
        self._unplace("AA:BB")
        rec = self.store.get_onu_place("ispA", "AA:BB")
        self.assertIsNone(rec["accuracy_m"])
        self.assertIsNone(rec["place_source"])
        self.assertIsNone(rec["placed_at"])
        self.assertEqual(rec["label"], "RAMESH")


    def test_a_name_and_number_can_be_recorded_WITHOUT_a_location(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        status, body, _ = self._req(
            "POST", "/api/inventory/onu-contact",
            {"mac": "AA:BB", "label": "Ramesh", "phone": "98765 43210"},
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        rec = self.store.get_onu_place("ispA", "AA:BB")
        self.assertEqual(rec["label"], "RAMESH")
        self.assertEqual(rec["phone"], "9876543210")
        self.assertIsNone(rec["lat"])

    def test_recording_a_name_is_NOT_vouching_for_a_power_supply(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._req("POST", "/api/inventory/onu-contact",
                  {"mac": "AA:BB", "label": "Ramesh", "witness": True},
                  cookie=self._owner())
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    def test_an_unlocated_record_is_NOT_drawn_on_the_map(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._req("POST", "/api/inventory/onu-contact",
                  {"mac": "AA:BB", "label": "Ramesh"}, cookie=self._owner())
        self.assertEqual(self._places()[1]["places"], [])

    def test_an_unlocated_record_does_not_count_as_surveyed(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._req("POST", "/api/inventory/onu-contact",
                  {"mac": "AA:BB", "label": "Ramesh"}, cookie=self._owner())
        status, body, _ = self._req("GET", "/api/inventory/onu-coverage",
                                    cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["placed"], 0)
        self.assertEqual(body["total"], 1)

    def test_emptying_a_desk_record_prunes_it(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._req("POST", "/api/inventory/onu-contact",
                  {"mac": "AA:BB", "label": "Ramesh"}, cookie=self._owner())
        self._req("POST", "/api/inventory/onu-contact",
                  {"mac": "AA:BB", "label": "", "phone": ""},
                  cookie=self._owner())
        self.assertIsNone(self.store.get_onu_place("ispA", "AA:BB"))

    def test_recording_a_subscriber_is_an_OWNER_write(self):
        self._onu("ispA", self.olt, "1", "AA:BB")
        status, _, _ = self._req(
            "POST", "/api/inventory/onu-contact",
            {"mac": "AA:BB", "label": "Ramesh"},
            cookie=self._login("field", "fieldpassword"))
        self.assertEqual(status, 403)
        self.assertIsNone(self.store.get_onu_place("ispA", "AA:BB"))


    def test_one_sticker_is_ONE_reference_point_however_it_was_typed(self):
        self._onu("ispA", self.olt, "1", "A4:F2:1B")
        self._place("a4:f2:1b")
        self._place("  A4:F2:1B  ")
        _, body = self._places()
        self.assertEqual(len(body["places"]), 1)
        self.assertEqual(
            self.store.onu_place_macs("ispA", witness_only=False), {"A4:F2:1B"})

    def test_separators_are_NOT_stripped_from_identity(self):
        self._onu("ispA", self.olt, "1", "A4:F2:1B")
        self._place("A4:F2:1B")
        self._place("A4F21B")
        self.assertEqual(len(self._places()[1]["places"]), 2)

    def test_a_blank_mac_is_refused(self):
        status, _, _ = self._place("   ")
        self.assertEqual(status, 422)

    def test_an_out_of_range_coordinate_is_refused(self):
        status, _, _ = self._place("AA:BB", lat=91.0)
        self.assertEqual(status, 422)


    def test_a_placement_whose_onu_is_GONE_is_listed_as_unmatched(self):
        self._place("DE:AD:BE:EF")
        _, body = self._places()
        self.assertEqual(len(body["places"]), 1)
        p = body["places"][0]
        self.assertFalse(p["matched"])
        self.assertIsNone(p["device_id"])
        self.assertIsNone(p["state"])

    def test_a_reference_point_follows_its_onu_to_another_OLT(self):
        other = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self._onu("ispA", self.olt, "1", "AA:BB", age_s=600)
        self._place("AA:BB")
        self.assertEqual(self._places()[1]["places"][0]["device_id"], self.olt)

        self._onu("ispA", self.olt, "2", "EE:FF", age_s=0)
        self._onu("ispA", other, "9", "AA:BB", age_s=0)
        p = self._places()[1]["places"][0]
        self.assertEqual(p["device_id"], other)
        self.assertEqual(p["device_name"], "PYLON-OLT")
        self.assertFalse(p["ambiguous"])

    def test_a_mac_on_two_live_slots_reports_AMBIGUOUS_rather_than_guessing(self):
        other = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._onu("ispA", other, "9", "AA:BB")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertTrue(p["matched"])
        self.assertTrue(p["ambiguous"])
        self.assertEqual(p["slots"], 2)
        self.assertIsNone(p["device_id"])


    def test_another_orgs_reference_points_are_invisible(self):
        self._place("AA:BB")
        cookie = self._login("bowner", "bownerpassword")
        self.assertEqual(self._places(cookie=cookie)[1]["places"], [])
        self.assertEqual(self.store.onu_place_macs("ispB"), set())

    def test_a_worker_cannot_place_a_reference_point(self):
        cookie = self._login("field", "fieldpassword")
        status, _, _ = self._place("AA:BB", cookie=cookie)
        self.assertEqual(status, 403)

    def test_a_superadmin_places_into_the_org_it_names(self):
        auth.create_user(self.store, None, "root", "rootpassword", "owner")
        cookie = self._login("root", "rootpassword")
        status, body, _ = self._req(
            "POST", "/api/inventory/onu-place",
            {"mac": "AA:BB", "lat": 15.0, "lng": 74.0, "org_id": "ispA"},
            cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            self.store.onu_place_macs("ispA", witness_only=False), {"AA:BB"})

    def test_a_superadmin_with_NO_org_is_refused_not_crashed(self):
        auth.create_user(self.store, None, "root2", "rootpassword", "owner")
        cookie = self._login("root2", "rootpassword")
        status, body, _ = self._req(
            "POST", "/api/inventory/onu-place",
            {"mac": "AA:BB", "lat": 15.0, "lng": 74.0}, cookie=cookie)
        self.assertEqual(status, 400, body)
        self.assertIn("org", (body.get("error") or "").lower())

    def test_placement_requires_a_session(self):
        status, _, _ = self._req("POST", "/api/inventory/onu-place",
                                 {"mac": "AA:BB", "lat": 1.0, "lng": 1.0})
        self.assertEqual(status, 401)


    def _port(self, device_id, if_index, if_name, *, oper="up",
              in_bps=1.0e6, out_bps=8.0e6):
        self.store.upsert_switch_port(
            "ispA", device_id, if_index, if_name, None, "up", oper,
            None, 0, 0, None, _iso(self.now))
        with self.store._connect() as conn:
            conn.execute("UPDATE switch_ports SET in_bps=?, out_bps=?, updated_at=?"
                         " WHERE device_id=? AND if_index=?",
                         (in_bps, out_bps, _iso(self.now), device_id, if_index))
            conn.commit()

    def test_a_reference_onu_carries_its_OWN_interface_rate(self):
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._port(self.olt, 16, "EPON01ONU3")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertEqual(p["if_name"], "EPON01ONU3")
        self.assertEqual(p["out_bps"], 8.0e6)
        self.assertEqual(p["port_state"], "up")

    def test_a_described_onu_still_matches_on_the_leading_token(self):
        self._onu("ispA", self.olt, "0/3.5", "AA:BB", pon="EPON0/3", onu_id=5)
        self._port(self.olt, 20, "EPON03ONU5 BSNL-238")
        self._place("AA:BB")
        self.assertEqual(self._places()[1]["places"][0]["if_name"],
                         "EPON03ONU5 BSNL-238")

    def test_the_PON_AGGREGATE_is_never_reported_as_one_subscribers_rate(self):
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._port(self.olt, 9, "EPON0/1", in_bps=900e6, out_bps=900e6)
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertIsNone(p["if_name"])
        self.assertIsNone(p["in_bps"])

    def test_a_vendor_with_no_per_onu_interface_reports_no_rate(self):
        self._onu("ispA", self.olt, "1.4", "AA:BB", pon="1", onu_id=4)
        self._port(self.olt, 3, "gpon-onu_1/1/4:1")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertIsNone(p["if_name"])
        self.assertIsNone(p["out_bps"])

    def test_an_ambiguous_placement_gets_no_rate_either(self):
        other = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._onu("ispA", other, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._port(self.olt, 16, "EPON01ONU3")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertTrue(p["ambiguous"])
        self.assertIsNone(p["if_name"])


    def test_a_placement_carries_the_optics_VERDICT_and_its_CLOCK(self):
        self._onu("ispA", self.olt, "0/6.1", "AA:BB", pon="0/6", onu_id=1)
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertEqual(p["rx_dbm"], -21.0)
        self.assertEqual(p["severity"], "ok")
        self.assertIsNotNone(p["optics_updated_at"])

    def test_the_dbm_clock_is_the_OPTICS_walk_not_the_PORT_walk(self):
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3,
                  age_s=4000)
        self._port(self.olt, 16, "EPON01ONU3")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertIsNotNone(p["port_updated_at"])
        self.assertNotEqual(p["optics_updated_at"], p["port_updated_at"])

    def test_an_unmatched_placement_carries_NO_reading_to_print(self):
        self._place("DE:AD:BE:EF:00:01")
        p = self._places()[1]["places"][0]
        self.assertFalse(p["matched"])
        self.assertIsNone(p["rx_dbm"])
        self.assertIsNone(p["severity"])
        self.assertIsNone(p["optics_updated_at"])


    def test_the_optics_reply_marks_which_rows_are_reference_points(self):
        self._onu("ispA", self.olt, "0/6.1", "AA:BB", pon="0/6", onu_id=1)
        self._onu("ispA", self.olt, "0/6.2", "CC:DD", pon="0/6", onu_id=2)
        self._place("AA:BB", label="Water tank")
        status, body, _ = self._req(
            "GET", f"/api/inventory/optics?device_id={self.olt}",
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        by_serial = {o["serial"]: o for o in body["onus"]}
        self.assertIsNotNone(by_serial["AA:BB"]["place"])
        self.assertEqual(by_serial["AA:BB"]["place"]["label"], "WATER TANK")
        self.assertIsNone(by_serial["CC:DD"]["place"])


if __name__ == "__main__":
    unittest.main()
