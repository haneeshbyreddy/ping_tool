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


class DropsTest(unittest.TestCase):
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
        self.spl = self.store.create_org_device("ispA", {
            "name": "SPL-KOTA-1", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt,
            "pon_port": "EPON0/1", "split_ratio": 8})
        self.spl2 = self.store.create_org_device("ispA", {
            "name": "SPL-KOTA-2", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt,
            "pon_port": "EPON0/1", "split_ratio": 4})
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

    def _onu(self, serial, *, org="ispA", device_id=None, state="online",
             pon="EPON0/1", onu_id=1, rx=-21.0, age_s=0):
        ts = _iso(self.now - timedelta(seconds=age_s))
        self.store.upsert_onu_optics(
            org, device_id if device_id is not None else self.olt,
            f"{pon}.{onu_id}", pon_port=pon, onu_id=onu_id, name=None,
            serial=serial, state=state, rx_dbm=rx, tx_dbm=2.0,
            olt_rx_dbm=-20.0, distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=ts)

    def _attach(self, macs, passive_id, cookie=None):
        return self._req("POST", "/api/inventory/drops/set",
                         {"macs": macs, "passive_id": passive_id,
                          "org_id": "ispA"},
                         cookie=cookie or self._owner())

    def _drops(self, cookie=None):
        status, body, _ = self._req("GET", "/api/inventory/drops",
                                    cookie=cookie or self._owner())
        return status, body


    def test_recording_drops_rolls_them_up_under_their_splitter(self):
        self._onu("AA:01", onu_id=1)
        self._onu("AA:02", onu_id=2)
        self.assertEqual(self._attach(["AA:01", "AA:02"], self.spl)[0], 200)
        status, body = self._drops()
        self.assertEqual(status, 200, body)
        load = {s["passive_id"]: s for s in body["splitters"]}[self.spl]
        self.assertEqual((load["recorded"], load["online"], load["dark"]),
                         (2, 2, 0))
        self.assertEqual(load["olt_id"], self.olt)
        self.assertEqual(load["pon_ports"], ["EPON0/1"])

    def test_the_reply_states_how_many_subscribers_nobody_recorded(self):
        self._onu("AA:01", onu_id=1)
        self._onu("AA:02", onu_id=2)
        self._attach(["AA:01"], self.spl)
        _, body = self._drops()
        self.assertEqual((body["recorded"], body["unrecorded"]), (1, 1))

    def test_a_drop_is_keyed_on_identity_however_the_mac_was_typed(self):
        self._onu("AA:01", onu_id=1)
        self._attach([" aa:01 "], self.spl)
        _, body = self._drops()
        self.assertEqual(body["recorded"], 1)
        self.assertEqual(body["splitters"][0]["recorded"], 1)

    def test_re_recording_MOVES_the_drop_rather_than_duplicating_it(self):
        self._onu("AA:01", onu_id=1)
        self._attach(["AA:01"], self.spl)
        self._attach(["AA:01"], self.spl2)
        _, body = self._drops()
        loads = {s["passive_id"]: s["recorded"] for s in body["splitters"]}
        self.assertEqual(loads.get(self.spl2), 1)
        self.assertNotIn(self.spl, loads)

    def test_detaching_is_a_DELETE_leaving_no_row_behind(self):
        self._onu("AA:01", onu_id=1)
        self._attach(["AA:01"], self.spl)
        status, body, _ = self._req(
            "POST", "/api/inventory/drops/set",
            {"macs": ["AA:01"], "passive_id": None, "org_id": "ispA"},
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.list_onu_drops("ispA"), [])


    def test_a_drop_may_not_hang_off_powered_gear(self):
        self._onu("AA:01", onu_id=1)
        status, body, _ = self._attach(["AA:01"], self.olt)
        self.assertEqual(status, 422, body)
        self.assertIn("passive", body.get("error", "").lower())

    def test_a_worker_cannot_rewrite_the_plant_record(self):
        self._onu("AA:01", onu_id=1)
        status, _, _ = self._attach(["AA:01"], self.spl,
                                    cookie=self._login("field", "fieldpassword"))
        self.assertIn(status, (401, 403))
        self.assertEqual(self.store.list_onu_drops("ispA"), [])

    def test_another_orgs_splitter_is_not_reachable(self):
        self._onu("AA:01", onu_id=1)
        status, _, _ = self._attach(["AA:01"], self.spl,
                                    cookie=self._login("bowner", "bownerpassword"))
        self.assertIn(status, (403, 404, 422))
        self.assertEqual(self.store.list_onu_drops("ispA"), [])

    def test_an_empty_mac_list_is_refused(self):
        status, _, _ = self._req("POST", "/api/inventory/drops/set",
                                 {"macs": [], "passive_id": self.spl,
                                  "org_id": "ispA"}, cookie=self._owner())
        self.assertEqual(status, 422)


    def test_the_per_splitter_list_and_the_rollup_agree(self):
        self._onu("AA:01", onu_id=1)
        self._onu("AA:02", onu_id=2, state="offline")
        self._attach(["AA:01", "AA:02"], self.spl)
        _, rollup = self._drops()
        load = {s["passive_id"]: s for s in rollup["splitters"]}[self.spl]
        status, body, _ = self._req(
            "GET", f"/api/inventory/drops/subscribers?device_id={self.spl}",
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["drops"]), load["recorded"])
        self.assertEqual(body["load"]["dark"], load["dark"])

    def test_a_reference_point_reports_the_splitter_its_drop_comes_off(self):
        self._onu("AA:01", onu_id=1)
        self._attach(["AA:01"], self.spl)
        self._req("POST", "/api/inventory/onu-place",
                  {"mac": "AA:01", "lat": 15.85, "lng": 74.5, "org_id": "ispA"},
                  cookie=self._owner())
        status, body, _ = self._req("GET", "/api/inventory/onu-places",
                                    cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["places"][0]["drop_passive_id"], self.spl)

    def test_the_optical_tab_carries_each_onus_recorded_splitter(self):
        self._onu("AA:01", onu_id=1)
        self._attach(["AA:01"], self.spl)
        status, body, _ = self._req(
            "GET", f"/api/inventory/optics?device_id={self.olt}",
            cookie=self._owner())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["onus"][0]["drop_passive_id"], self.spl)

    def test_deleting_a_splitter_un_records_its_drops(self):
        self._onu("AA:01", onu_id=1)
        self._attach(["AA:01"], self.spl)
        self.assertTrue(self.store.delete_org_device("ispA", self.spl)["ok"])
        self.assertEqual(self.store.list_onu_drops("ispA"), [])

    def test_the_split_ratio_vocabulary_is_closed(self):
        status, body, _ = self._req("POST", "/api/inventory/update", {
            "id": self.spl, "name": "SPL-KOTA-1", "ip_address": "",
            "device_type": "splitter", "parent_device_id": self.olt,
            "split_ratio": 7,
        }, cookie=self._owner())
        self.assertEqual(status, 422, body)
        self.assertIn("1:8", body.get("error", ""))


if __name__ == "__main__":
    unittest.main()
