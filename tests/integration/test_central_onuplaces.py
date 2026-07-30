"""Operator-placed REFERENCE ONUs — the API half.

An ISP picks the handful of subscribers it knows run on a UPS, solar or a tower
supply and places them on the map. Placing IS the claim; nothing detects power.
Those reference points then decide PON-fault verdicts (see unit/test_ponfault
for the rules themselves), which is why placement is an owner write.

What this file pins is the plumbing that the rules ride on: identity (one
sticker is one reference point, however it was typed), org isolation, the
sparse-table contract (clearing is a DELETE), and the honesty of the read —
a placement whose ONU no longer exists must SAY so rather than quietly stop
being a witness.
"""
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

    # --- the basic round trip ------------------------------------------------

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
        # stored UPPERCASE — one spelling of a customer name everywhere
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

    def test_clearing_is_a_DELETE_leaving_no_row_behind(self):
        # the table is sparse: an unplaced reference point is the ABSENCE of a
        # row, never a row with empty coordinates
        self._onu("ispA", self.olt, "1", "AA:BB")
        self._place("AA:BB")
        status, body, _ = self._req("POST", "/api/inventory/onu-place",
                                    {"mac": "AA:BB", "lat": None, "lng": None},
                                    cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(self._places()[1]["places"], [])
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    # --- identity ------------------------------------------------------------

    def test_one_sticker_is_ONE_reference_point_however_it_was_typed(self):
        # _norm_mac is case-insensitive; two spellings must not become two
        # witnesses voting separately on the same PON
        self._onu("ispA", self.olt, "1", "A4:F2:1B")
        self._place("a4:f2:1b")
        self._place("  A4:F2:1B  ")
        _, body = self._places()
        self.assertEqual(len(body["places"]), 1)
        self.assertEqual(self.store.onu_place_macs("ispA"), {"A4:F2:1B"})

    def test_separators_are_NOT_stripped_from_identity(self):
        # search_key is punctuation-blind; identity deliberately is not, or two
        # genuinely different serials collapse into one reference point
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

    # --- honesty about a placement that lost its ONU -------------------------

    def test_a_placement_whose_onu_is_GONE_is_listed_as_unmatched(self):
        # an RMA'd box changes MAC, so the row survives pointing at nothing.
        # The operator has to SEE that — a pin that silently stopped being a
        # witness is the one failure this list must not hide.
        self._place("DE:AD:BE:EF")
        _, body = self._places()
        self.assertEqual(len(body["places"]), 1)
        p = body["places"][0]
        self.assertFalse(p["matched"])
        self.assertIsNone(p["device_id"])
        self.assertIsNone(p["state"])

    def test_a_reference_point_follows_its_onu_to_another_OLT(self):
        # Re-homing a drop must not need a second click: the placement is keyed
        # on the box, not on the slot it happened to occupy. onu_optics never
        # deletes the vacated row, but current_roster keeps only the rows from
        # each OLT's FRESHEST walk — so once the old OLT walks again without it,
        # the zombie drops out and the reference point is at its new home.
        other = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self._onu("ispA", self.olt, "1", "AA:BB", age_s=600)
        self._place("AA:BB")
        self.assertEqual(self._places()[1]["places"][0]["device_id"], self.olt)

        self._onu("ispA", self.olt, "2", "EE:FF", age_s=0)   # newer walk, no AA:BB
        self._onu("ispA", other, "9", "AA:BB", age_s=0)      # it registered here
        p = self._places()[1]["places"][0]
        self.assertEqual(p["device_id"], other)
        self.assertEqual(p["device_name"], "PYLON-OLT")
        self.assertFalse(p["ambiguous"])

    def test_a_mac_on_two_live_slots_reports_AMBIGUOUS_rather_than_guessing(self):
        # C-Data reg tables really do hand one MAC to two slots. A reference
        # point standing on both isn't at one OLT, and picking a winner would
        # put a pin's label on the wrong box.
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

    # --- scope ---------------------------------------------------------------

    def test_another_orgs_reference_points_are_invisible(self):
        self._place("AA:BB")
        cookie = self._login("bowner", "bownerpassword")
        self.assertEqual(self._places(cookie=cookie)[1]["places"], [])
        self.assertEqual(self.store.onu_place_macs("ispB"), set())

    def test_a_worker_cannot_place_a_reference_point(self):
        # deciding what counts as a trustworthy power supply is running the org
        cookie = self._login("field", "fieldpassword")
        status, _, _ = self._place("AA:BB", cookie=cookie)
        self.assertEqual(status, 403)

    def test_a_superadmin_places_into_the_org_it_names(self):
        # A superadmin is org_id IS NULL, so the org can only come from the body.
        # This is the live-deployment case (the platform admin IS the operator
        # here) and it 500'd on the NOT NULL insert until the SPA started sending
        # its scope — every earlier test logged in as an org owner and missed it.
        auth.create_user(self.store, None, "root", "rootpassword", "owner")
        cookie = self._login("root", "rootpassword")
        status, body, _ = self._req(
            "POST", "/api/inventory/onu-place",
            {"mac": "AA:BB", "lat": 15.0, "lng": 74.0, "org_id": "ispA"},
            cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.onu_place_macs("ispA"), {"AA:BB"})

    def test_a_superadmin_with_NO_org_is_refused_not_crashed(self):
        # There is no org-less reference point to store, so this must be a clean
        # refusal. It used to reach the store and raise a NOT NULL IntegrityError,
        # which the operator saw as "internal error" with nothing to act on.
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

    # --- the per-ONU rate on the map line ------------------------------------

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
        # C-Data EPON gives each ONU an ifTable row, which is the only reason a
        # per-subscriber rate exists at all here.
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._port(self.olt, 16, "EPON01ONU3")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertEqual(p["if_name"], "EPON01ONU3")
        self.assertEqual(p["out_bps"], 8.0e6)
        self.assertEqual(p["port_state"], "up")

    def test_a_described_onu_still_matches_on_the_leading_token(self):
        # once somebody types a description the firmware appends it to if_name
        # (and overwrites if_alias entirely) — the interface token survives
        self._onu("ispA", self.olt, "0/3.5", "AA:BB", pon="EPON0/3", onu_id=5)
        self._port(self.olt, 20, "EPON03ONU5 BSNL-238")
        self._place("AA:BB")
        self.assertEqual(self._places()[1]["places"][0]["if_name"],
                         "EPON03ONU5 BSNL-238")

    def test_the_PON_AGGREGATE_is_never_reported_as_one_subscribers_rate(self):
        # EPON0/1 is the whole PON — up to 64 subscribers. Printing it on one
        # ONU's line would put the same big number on every reference point.
        self._onu("ispA", self.olt, "0/1.3", "AA:BB", pon="EPON0/1", onu_id=3)
        self._port(self.olt, 9, "EPON0/1", in_bps=900e6, out_bps=900e6)
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertIsNone(p["if_name"])
        self.assertIsNone(p["in_bps"])

    def test_a_vendor_with_no_per_onu_interface_reports_no_rate(self):
        # Gpon_04/Gpon_08 name interfaces differently and matched ZERO rows in
        # the live fleet. That must degrade to "no reading", never to a guess.
        self._onu("ispA", self.olt, "1.4", "AA:BB", pon="1", onu_id=4)
        self._port(self.olt, 3, "gpon-onu_1/1/4:1")
        self._place("AA:BB")
        p = self._places()[1]["places"][0]
        self.assertIsNone(p["if_name"])
        self.assertIsNone(p["out_bps"])

    def test_an_ambiguous_placement_gets_no_rate_either(self):
        # we refuse to say which OLT it is on, so we must refuse to say which
        # interface's traffic is its own
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

    # --- the fold into the Optical tab ---------------------------------------

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
