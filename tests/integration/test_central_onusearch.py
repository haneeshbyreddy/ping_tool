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


class OnuSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.olt = self.store.create_org_device("ispA", {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.olt2 = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.other = self.store.create_org_device("ispB", {
            "name": "B-OLT", "ip_address": "10.9.9.9", "device_type": "OLT",
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

    def _onu(self, org, device_id, onu_key, serial, *, state="online",
             pon="0/1", onu_id=1, name=None, age_s=0):
        ts = _iso(self.now - timedelta(seconds=age_s))
        self.store.upsert_onu_optics(
            org, device_id, onu_key, pon_port=pon, onu_id=onu_id, name=name,
            serial=serial, state=state, rx_dbm=-21.0, tx_dbm=2.0,
            olt_rx_dbm=-20.0, distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=ts)

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
        self._last_cache_control = resp.getheader("Cache-Control")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _search(self, q, cookie=None, org=None):
        cookie = cookie or self._login("owner", "ownerpassword")
        path = f"/api/inventory/onu-search?q={q}"
        if org:
            path += f"&org={org}"
        status, body, _ = self._req("GET", path, cookie=cookie)
        return status, body

    def _serials(self, body):
        return sorted(o["serial"] for m in body["matches"] for o in m["onus"])


    def test_tail_of_a_mac_finds_its_onu_and_names_the_olt(self):
        self._onu("ispA", self.olt, "1", "A4:F2:1B:9C:44:01", pon="0/6", onu_id=12)
        status, body = self._search("4401")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["matches"]), 1)
        m = body["matches"][0]
        self.assertEqual(m["device_id"], self.olt)
        self.assertEqual(m["device_name"], "HILL-OLT-1")
        self.assertEqual(len(m["onus"]), 1)
        self.assertEqual(m["onus"][0]["pon_port"], "0/6")
        self.assertEqual(m["onus"][0]["onu_id"], 12)

    def test_separators_are_optional_and_case_is_ignored(self):
        self._onu("ispA", self.olt, "1", "A4:F2:1B:9C:44:01")
        for needle in ("a4f21b9c4401", "A4%3AF2%3A1B", "a4-f2-1b", "4401", "9C:44"):
            with self.subTest(needle=needle):
                _, body = self._search(needle)
                self.assertEqual(self._serials(body), ["A4:F2:1B:9C:44:01"])

    def test_a_dash_written_mac_is_found_by_a_colon_typed_needle(self):
        self._onu("ispA", self.olt, "1", "AA-BB-CC-DD-EE-FF")
        _, body = self._search("cc%3Add%3Aee")
        self.assertEqual(self._serials(body), ["AA-BB-CC-DD-EE-FF"])

    def test_huawei_ascii_serial_is_searchable_too(self):
        self._onu("ispA", self.olt, "1", "HWTC1234ABCD")
        _, body = self._search("1234abcd")
        self.assertEqual(self._serials(body), ["HWTC1234ABCD"])

    def test_hits_group_by_olt_across_the_org(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01")
        self._onu("ispA", self.olt2, "1", "AA:BB:CC:00:00:02")
        _, body = self._search("aabbcc")
        self.assertEqual(len(body["matches"]), 2)
        self.assertEqual([m["device_name"] for m in body["matches"]],
                         ["HILL-OLT-1", "PYLON-OLT"])

    def test_severity_and_state_ride_along_for_the_result_row(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", state="offline")
        _, body = self._search("aabbcc")
        onu = body["matches"][0]["onus"][0]
        self.assertEqual(onu["state"], "offline")
        self.assertIn("severity", onu)
        self.assertIn("last_online_at", onu)


    def test_provisioned_name_is_searchable(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name="hc_kiran")
        _, body = self._search("kiran")
        self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:01"])
        self.assertEqual(body["matches"][0]["onus"][0]["name"], "hc_kiran")

    def test_name_matching_ignores_underscores_and_spacing(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name="hc_kiran")
        for needle in ("hc_kiran", "hc%20kiran", "hckiran", "HC_KIRAN", "c_kir"):
            with self.subTest(needle=needle):
                _, body = self._search(needle)
                self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:01"])

    def test_dash_written_name_found_without_the_dash(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name="BSNL-151")
        for needle in ("bsnl151", "BSNL-151", "bsnl%20151"):
            with self.subTest(needle=needle):
                _, body = self._search(needle)
                self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:01"])

    def test_name_and_mac_hits_combine_in_one_result_set(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name="hc_kiran")
        self._onu("ispA", self.olt, "2", "AA:BB:CC:00:00:02", name="hc_kirthi")
        _, body = self._search("hc_ki")
        self.assertEqual(len(body["matches"][0]["onus"]), 2)
        _, body = self._search("0002")
        self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:02"])

    def test_unnamed_onu_never_matches_on_name(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name=None)
        self._onu("ispA", self.olt, "2", "AA:BB:CC:00:00:02", name="")
        _, body = self._search("kiran")
        self.assertEqual(body["matches"], [])


    def test_zombie_slot_from_an_older_walk_is_not_a_hit(self):
        self._onu("ispA", self.olt, "gone", "DE:AD:BE:EF:00:01", age_s=3600)
        self._onu("ispA", self.olt, "live", "DE:AD:BE:EF:00:02", age_s=0)
        _, body = self._search("deadbeef")
        self.assertEqual(self._serials(body), ["DE:AD:BE:EF:00:02"])

    def test_a_stale_olt_still_answers(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", age_s=4000)
        _, body = self._search("aabbcc")
        self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:01"])

    def test_another_orgs_onu_is_never_reachable(self):
        self._onu("ispB", self.other, "1", "AA:BB:CC:00:00:09")
        _, body = self._search("aabbcc")
        self.assertEqual(body["matches"], [])
        cookie = self._login("bowner", "bownerpassword")
        _, body = self._search("aabbcc", cookie=cookie)
        self.assertEqual(self._serials(body), ["AA:BB:CC:00:00:09"])

    def test_short_needle_returns_empty_without_scanning(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01")
        for needle in ("", "a", "aa", "a%3A"):
            with self.subTest(needle=needle):
                status, body = self._search(needle)
                self.assertEqual(status, 200)
                self.assertEqual(body["matches"], [])
                self.assertFalse(body["truncated"])

    def test_like_wildcards_in_the_needle_match_nothing(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01")
        _, body = self._search("%25%25%25")
        self.assertEqual(body["matches"], [])
        _, body = self._search("___")
        self.assertEqual(body["matches"], [])

    def test_results_are_capped_and_flagged(self):
        for i in range(60):
            self._onu("ispA", self.olt, str(i), f"AA:BB:CC:00:{i:02d}:01",
                      onu_id=i)
        _, body = self._search("aabbcc")
        shipped = sum(len(m["onus"]) for m in body["matches"])
        self.assertEqual(shipped, 50)
        self.assertTrue(body["truncated"])

    def test_onus_come_back_in_slot_order(self):
        self._onu("ispA", self.olt, "c", "AA:BB:CC:00:00:03", pon="0/2", onu_id=1)
        self._onu("ispA", self.olt, "a", "AA:BB:CC:00:00:01", pon="0/1", onu_id=9)
        self._onu("ispA", self.olt, "b", "AA:BB:CC:00:00:02", pon="0/1", onu_id=2)
        _, body = self._search("aabbcc")
        self.assertEqual([(o["pon_port"], o["onu_id"]) for o in body["matches"][0]["onus"]],
                         [("0/1", 2), ("0/1", 9), ("0/2", 1)])

    def test_null_serial_never_matches(self):
        self._onu("ispA", self.olt, "1", None)
        _, body = self._search("aabbcc")
        self.assertEqual(body["matches"], [])

    def test_a_null_serial_onu_is_still_findable_by_name(self):
        self._onu("ispA", self.olt, "1", None, name="hc_kiran")
        _, body = self._search("kiran")
        self.assertEqual(len(body["matches"][0]["onus"]), 1)
        self.assertIsNone(body["matches"][0]["onus"][0]["serial"])

    def test_requires_a_session(self):
        status, _, _ = self._req("GET", "/api/inventory/onu-search?q=aabbcc")
        self.assertEqual(status, 401)

    def test_reply_is_never_http_cacheable(self):
        self._onu("ispA", self.olt, "1", "AA:BB:CC:00:00:01", name="hc_kiran")
        self._search("kiran")
        self.assertEqual(self._last_cache_control, "no-store")

    def test_every_json_api_reply_is_no_store(self):
        cookie = self._login("owner", "ownerpassword")
        for path in ("/api/me", "/api/inventory?org=ispA", "/api/orgs"):
            with self.subTest(path=path):
                self._req("GET", path, cookie=cookie)
                self.assertEqual(self._last_cache_control, "no-store")


if __name__ == "__main__":
    unittest.main()
