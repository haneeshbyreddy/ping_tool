"""One subscriber, whole — `GET /api/inventory/subscriber`.

A subscriber was the only first-class object in this product with no home. A
device has one panel that the tree, the map and an issue row all open; a
subscriber had six partial projections across six screens, none complete and
none addressable, so an operator had to know which screen held which fact.

This endpoint is deliberately a JOIN of readers that already exist rather than a
new source of truth, and that is most of what this file pins: it must agree with
the Optical tab about a slot, with the map about identity, and with the splitter
panel about plant — because a drill-down that disagrees with the surface it was
opened from is worse than not having one.
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


class SubscriberTest(unittest.TestCase):
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
            "region": None, "parent_device_id": None,
            "optical_warn_dbm": -24.0, "optical_crit_dbm": -27.0})
        # A real cascade: the drop hangs off a 1:8 that hangs off a 1:4.
        self.feeder = self.store.create_org_device("ispA", {
            "name": "SPL-FEEDER", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt,
            "pon_port": "EPON0/1", "split_ratio": 4})
        self.spl = self.store.create_org_device("ispA", {
            "name": "SPL-KOTA-1", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.feeder,
            "pon_port": "EPON0/1", "split_ratio": 8})
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
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username,
                                      "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return setcookie.split(";")[0] if setcookie else None

    def _owner(self):
        return self._login("owner", "ownerpassword")

    def _onu(self, serial, *, org="ispA", device_id=None, onu_key="0/1.3",
             state="online", pon="EPON0/1", onu_id=3, name=None, rx=-21.0,
             age_s=0):
        ts = _iso(self.now - timedelta(seconds=age_s))
        self.store.upsert_onu_optics(
            org, device_id if device_id is not None else self.olt, onu_key,
            pon_port=pon, onu_id=onu_id, name=name, serial=serial, state=state,
            rx_dbm=rx, tx_dbm=2.0, olt_rx_dbm=-20.0, distance_m=1200,
            rx_ref_dbm=None, rx_ref_at=None, severity="ok", ts=ts)

    def _get(self, mac, cookie=None):
        return self._req("GET", f"/api/inventory/subscriber?mac={mac}",
                         cookie=cookie or self._owner())

    # --- the whole object ----------------------------------------------------

    def test_it_returns_the_record_the_roster_row_and_the_olt_together(self):
        # The point of the endpoint: four screens' worth of facts in one answer.
        self._onu("A4:F2:1B:9C:44:01", name="walked-name", rx=-25.5)
        self.store.set_onu_place("ispA", "A4:F2:1B:9C:44:01", 15.85, 74.5,
                                 "RAMESH", None, phone="9876543210", witness=True)
        status, body = self._get("A4:F2:1B:9C:44:01")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["matched"])
        self.assertFalse(body["ambiguous"])

        self.assertEqual(body["record"]["label"], "RAMESH")
        self.assertEqual(body["record"]["phone"], "9876543210")
        self.assertAlmostEqual(body["record"]["lat"], 15.85)

        self.assertEqual(body["roster"]["pon_port"], "EPON0/1")
        self.assertEqual(body["roster"]["onu_id"], 3)
        self.assertEqual(body["roster"]["state"], "online")
        self.assertAlmostEqual(body["roster"]["rx_dbm"], -25.5)
        # the walked name survives beside the operator's, never replaced by it
        self.assertEqual(body["roster"]["name"], "walked-name")
        self.assertEqual(body["roster"]["label"], "RAMESH")

        self.assertEqual(body["olt"]["id"], self.olt)
        self.assertEqual(body["olt"]["name"], "HILL-OLT-1")

    def test_the_mac_is_punctuation_exact_but_case_blind(self):
        # Identity, not search: `_norm_mac`, so two spellings of one sticker are
        # one subscriber, but two genuinely different serials never collapse.
        self._onu("A4:F2:1B")
        self.assertTrue(self._get("a4:f2:1b")[1]["matched"])
        self.assertFalse(self._get("A4F21B")[1]["matched"])

    def test_thresholds_come_from_the_OLT_not_from_the_global_default(self):
        # Rx is graded against the box's own numbers. A panel that re-derived a
        # verdict from the dBm would call a drop healthy that the Optical tab
        # calls critical.
        self._onu("AA:BB")
        body = self._get("AA:BB")[1]
        self.assertEqual(body["thresholds"]["warn_dbm"], -24.0)
        self.assertEqual(body["thresholds"]["crit_dbm"], -27.0)

    # --- the two identity refusals -------------------------------------------

    def test_a_swapped_box_reports_matched_false_rather_than_a_blank_panel(self):
        # An RMA'd ONU changes MAC, so the record survives pointing at nothing.
        # Reported, never hidden: a record that quietly stopped describing
        # anything is what this must not conceal.
        self.store.set_onu_place("ispA", "DE:AD:BE:EF", 15.85, 74.5, "RAMESH",
                                 None, phone="9876543210", witness=True)
        status, body = self._get("DE:AD:BE:EF")
        self.assertEqual(status, 200)
        self.assertFalse(body["matched"])
        self.assertIsNone(body["roster"])
        self.assertIsNone(body["olt"])
        # …and the contact record still answers, which is the whole reason a
        # tech opens this panel about a box that vanished
        self.assertEqual(body["record"]["label"], "RAMESH")
        self.assertEqual(body["record"]["phone"], "9876543210")

    def test_a_mac_on_two_live_slots_refuses_to_pick_one(self):
        # C-Data reg tables keep every slot an ONU ever occupied. Choosing a
        # winner would send a tech to the wrong house.
        self._onu("AA:BB", onu_key="0/1.3", pon="EPON0/1", onu_id=3)
        self._onu("AA:BB", onu_key="0/2.7", pon="EPON0/2", onu_id=7)
        body = self._get("AA:BB")[1]
        self.assertTrue(body["ambiguous"])
        self.assertEqual(body["slots"], 2)
        self.assertIsNone(body["roster"])

    def test_an_unknown_mac_answers_an_empty_object_not_a_404(self):
        # The panel opens from a search box and a map pin; "nobody has recorded
        # anything about this sticker" is an answer, not an error.
        status, body = self._get("00:00:00")
        self.assertEqual(status, 200)
        self.assertIsNone(body["record"])
        self.assertFalse(body["matched"])

    def test_a_missing_mac_is_a_400(self):
        status, _ = self._req("GET", "/api/inventory/subscriber",
                              cookie=self._owner())
        self.assertEqual(status, 400)

    # --- where it hangs ------------------------------------------------------

    def test_the_plant_chain_runs_from_the_splitter_up_to_the_olt(self):
        # The straight line to the OLT was never the network. This is the plant a
        # crew works on, cascade included.
        self._onu("AA:BB")
        self.store.set_onu_drops("ispA", ["AA:BB"], self.spl)
        body = self._get("AA:BB")[1]
        self.assertEqual(body["drop"]["passive_id"], self.spl)
        chain = body["drop"]["chain"]
        self.assertEqual([c["name"] for c in chain],
                         ["SPL-KOTA-1", "SPL-FEEDER", "HILL-OLT-1"])
        # ratios ride along so the panel can state the cumulative split (1:32)
        # without a second round trip
        self.assertEqual([c["split_ratio"] for c in chain[:2]], [8, 4])

    def test_an_unrecorded_drop_says_so_rather_than_inventing_a_splitter(self):
        self._onu("AA:BB")
        self.assertIsNone(self._get("AA:BB")[1]["drop"])

    def test_the_chain_cannot_spin_on_a_cycle(self):
        # Validation rejects cycles on the way in, but a read is the last thing
        # that may hang on a bad row.
        a = self.store.create_org_device("ispA", {
            "name": "SPL-A", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "SPL-B", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": a})
        with self.store._connect() as conn:
            conn.execute("UPDATE org_devices SET parent_device_id=? WHERE id=?",
                         (b, a))
            conn.commit()
        self._onu("AA:BB")
        self.store.set_onu_drops("ispA", ["AA:BB"], a)
        status, body = self._get("AA:BB")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["drop"]["chain"]), 12)

    # --- the frozen rule -----------------------------------------------------

    def test_a_down_olt_ships_its_state_so_the_panel_can_freeze(self):
        # An unreachable box proves its readings are stale up to 15 minutes
        # before staleness would notice. `get_org_device` doesn't join
        # device_states, so reading the state from there would silently answer
        # None and the panel would render a dead OLT's last walk as live.
        self._onu("AA:BB")
        self.store.write_device_states(
            "ispA", [(self.olt, "DOWN", None, 100.0, None)], _iso(self.now))
        body = self._get("AA:BB")[1]
        self.assertEqual(body["olt"]["state"], "DOWN")

    # --- scope ---------------------------------------------------------------

    def test_another_orgs_subscriber_is_invisible(self):
        oltb = self.store.create_org_device("ispB", {
            "name": "B-OLT", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self._onu("AA:BB", org="ispB", device_id=oltb)
        self.store.set_onu_place("ispB", "AA:BB", 1.0, 2.0, "THEIRS", None, witness=True)
        body = self._get("AA:BB")[1]
        self.assertFalse(body["matched"])
        self.assertIsNone(body["record"])

    def test_a_worker_may_read_it(self):
        # The screen a site visit is actually about. Read-side only, and it adds
        # no fact a worker could not already reach by opening four screens.
        self._onu("AA:BB")
        status, body = self._get("AA:BB",
                                 cookie=self._login("field", "fieldpassword"))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["matched"])

    def test_it_needs_a_session(self):
        self._onu("AA:BB")
        status, _ = self._req("GET", "/api/inventory/subscriber?mac=AA:BB")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
