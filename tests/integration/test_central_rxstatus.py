"""Making a missing dBm reading LEGIBLE, end to end.

The failure this whole area exists to prevent is a silent false negative: a
blank Rx column that an operator reads as "this hardware has no optics" when the
truth was "nobody ever asked". It has been made for real, twice, on this fleet.

So the tests here are about the DIFFERENCE between kinds of nothing:

  * 0 critical ONUs because every ONU is healthy, versus 0 because not one ONU
    is measured (the KPI tiles);
  * an OLT that reports optical power, versus one that walks a full roster with
    every reading NULL (the row icon);
  * and, when there is nothing, WHICH of the four very different reasons it was
    (the diagnosis) — because they take opposite actions.
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
from wisp.central.weboptics_profiles import BUILTIN_SPECS

ORG = "ispA"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


class RxVisibilityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        self.now = datetime.now(timezone.utc)
        # Two OLTs: one that measures optical power, one C-Data-shaped box whose
        # roster is complete and whose readings are all NULL.
        self.rx_olt = self.store.create_org_device(ORG, {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.dark_olt = self.store.create_org_device(ORG, {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "gpon_vendor": "dbc"})
        # Both OLTs answer ICMP right now. Without this the org-wide rollup
        # drops them as probe-silent (an OLT central can't see gets no verdict,
        # by design) and every count below would read 0 for the wrong reason.
        self.store.write_device_states(
            ORG, [(self.rx_olt, "UP", 4.0, 0.0, 1.0),
                  (self.dark_olt, "UP", 5.0, 0.0, 1.0)], _iso(self.now))
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    # --- helpers ------------------------------------------------------------

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

    def _login(self):
        if getattr(self, "_cookie", None):
            return self._cookie
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": "owner", "password": "ownerpassword"})
        self._cookie = setcookie.split(";")[0] if setcookie else None
        return self._cookie

    def _get(self, path):
        status, body, _ = self._req("GET", path, cookie=self._login())
        return status, body

    def _post(self, path, body):
        status, out, _ = self._req("POST", path, body, cookie=self._login())
        return status, out

    def _onu(self, device_id, onu_key, rx, *, state="online", severity="ok",
             age_s=0, serial=None):
        ts = _iso(self.now - timedelta(seconds=age_s))
        self.store.upsert_onu_optics(
            ORG, device_id, onu_key, pon_port="EPON0/1", onu_id=int(onu_key[-1]),
            name="sub", serial=serial or f"AA:BB:CC:DD:EE:0{onu_key[-1]}",
            state=state, rx_dbm=rx, tx_dbm=None, olt_rx_dbm=None,
            distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity=severity, ts=ts)

    def _device(self, device_id):
        _, body = self._get(f"/api/inventory?org={ORG}")
        return next(d for d in body["devices"] if d["id"] == device_id)

    # --- the KPI tiles ------------------------------------------------------

    def test_the_summary_counts_critical_and_weak_onus(self):
        self._onu(self.rx_olt, "1.1", -20.0, severity="ok")
        self._onu(self.rx_olt, "1.2", -25.0, severity="warn")
        self._onu(self.rx_olt, "1.3", -28.5, severity="crit")
        status, body = self._get(f"/api/pon/summary?org={ORG}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["onus_crit"], 1)
        self.assertEqual(body["onus_warn"], 1)

    def test_a_dark_onus_stale_severity_is_not_counted(self):
        # An offline ONU has no light to measure, so its last severity is a fact
        # about the past. Counting it would page-shape the tile off history.
        self._onu(self.rx_olt, "1.4", -28.9, state="offline", severity="crit")
        _, body = self._get(f"/api/pon/summary?org={ORG}")
        self.assertEqual(body["onus_crit"], 0)

    def test_nothing_measured_is_distinguishable_from_nothing_wrong(self):
        # THE distinction. A C-Data roster walks complete with every rx NULL, so
        # onus_crit is 0 — exactly as it is on a perfectly healthy fleet. Only
        # onus_rx tells them apart, and without it the tile reads as an all-clear
        # on a fleet where no ONU is measured at all.
        for i in (1, 2, 3):
            self._onu(self.dark_olt, f"1.{i}", None)
        _, body = self._get(f"/api/pon/summary?org={ORG}")
        self.assertEqual(body["onus_crit"], 0)
        self.assertEqual(body["onus_warn"], 0)
        self.assertEqual(body["onus_total"], 3)
        self.assertEqual(body["onus_rx"], 0)      # <- the tile renders "—", not 0
        self.assertEqual(body["olts_rx"], 0)

    def test_partial_coverage_is_reported_as_partial(self):
        self._onu(self.rx_olt, "1.1", -20.0)
        self._onu(self.dark_olt, "1.2", None)
        _, body = self._get(f"/api/pon/summary?org={ORG}")
        self.assertEqual(body["onus_total"], 2)
        self.assertEqual(body["onus_rx"], 1)
        self.assertEqual(body["olts_rx"], 1)

    # --- the per-device capability icon -------------------------------------

    def test_the_device_row_carries_its_own_rx_coverage(self):
        self._onu(self.rx_olt, "1.1", -20.0)
        self._onu(self.dark_olt, "1.1", None)
        # Same optics badge on both (a full roster, walking fine); only onus_rx
        # separates the box that measures light from the one that doesn't.
        self.assertEqual(self._device(self.rx_olt)["onus_rx"], 1)
        self.assertEqual(self._device(self.dark_olt)["onus_rx"], 0)

    # --- the diagnosis ------------------------------------------------------

    def test_an_unclaimed_vendor_says_the_vendor_is_unknown(self):
        self._onu(self.rx_olt, "1.1", None)
        status, body = self._get(f"/api/inventory/rx-status?device_id={self.rx_olt}")
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["vendor"])
        self.assertIsNone(body["web_profile"])
        self.assertEqual(body["onus_rx"], 0)

    def test_a_covered_vendor_names_the_recipe_that_would_read_it(self):
        # The dbc built-in covers this OLT, so the honest answer is not "this
        # vendor has no Rx" — it is "we have a way to read it and haven't".
        self._onu(self.dark_olt, "1.1", None)
        _, body = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        self.assertEqual(body["vendor"], "dbc")
        self.assertEqual(body["vendor_source"], "declared")
        self.assertEqual(body["web_profile"], "dbc")
        self.assertFalse(body["has_credentials"])   # the actual blocker
        self.assertIn("dbc", body["known_vendors"])

    def test_a_detected_vendor_counts_only_with_a_sysobjectid_behind_it(self):
        # `profile` is echoed for an override too — including a fleet-wide
        # default — so only a real auto-detect may claim a box.
        self.store.upsert_snmp_statuses(
            ORG, [(self.rx_olt, "optics",
                   {"state": "ok", "profile": "dbc", "sysobjectid": "", "count": 1})],
            _iso(self.now))
        _, body = self._get(f"/api/inventory/rx-status?device_id={self.rx_olt}")
        self.assertIsNone(body["vendor"])
        self.store.upsert_snmp_statuses(
            ORG, [(self.rx_olt, "optics",
                   {"state": "ok", "profile": "dbc",
                    "sysobjectid": "1.3.6.1.4.1.37950.1", "count": 1})],
            _iso(self.now))
        _, body = self._get(f"/api/inventory/rx-status?device_id={self.rx_olt}")
        self.assertEqual(body["vendor"], "dbc")
        self.assertEqual(body["vendor_source"], "detected")

    def test_a_failed_scrape_is_reported_with_its_reason(self):
        # The whole reason the outcome is persisted rather than logged: "the
        # password was refused" and "this vendor has no Rx" are opposite fixes
        # and used to render as the same empty column.
        self.store.set_web_optics_status(
            ORG, self.dark_olt, "dbc", "login",
            "login rejected: the device served its login page again", 0)
        _, body = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        self.assertEqual(body["scrape"]["state"], "login")
        self.assertIn("login page again", body["scrape"]["detail"])
        self.assertIsNone(body["scrape"]["last_ok_at"])

    def test_a_success_stamps_last_ok_and_a_later_failure_keeps_it(self):
        # "was working until <ts>" is the single most useful line on a broken
        # pipeline, and it only exists if a failure can't erase it.
        self.store.set_web_optics_status(ORG, self.dark_olt, "dbc", "ok", None, 100)
        _, first = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        ok_at = first["scrape"]["last_ok_at"]
        self.assertIsNotNone(ok_at)
        self.store.set_web_optics_status(
            ORG, self.dark_olt, "dbc", "unreachable", "tunnel timeout", 0)
        _, later = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        self.assertEqual(later["scrape"]["state"], "unreachable")
        self.assertEqual(later["scrape"]["last_ok_at"], ok_at)

    # --- the manual read ------------------------------------------------------

    def _readable_olt(self, name="NLK-OLT", ip="10.0.0.3"):
        """An OLT the sweep would genuinely scrape: claimed vendor, a roster to
        merge onto, a stored login, a probe, and the org's tunnel grant."""
        self.store.set_org_web_proxy(ORG, True)
        olt = self.store.create_org_device(ORG, {
            "name": name, "ip_address": ip, "device_type": "OLT",
            "region": None, "parent_device_id": None, "gpon_vendor": "dbc",
            "assigned_node_id": "edge-1"})
        self._onu(olt, "1.1", None)
        self.store.set_device_webui_credentials(
            ORG, olt, username="admin", password_enc="enc:xxx",
            set_password=True, auth_mode="form", updated_by="tester")
        return olt

    def test_a_refreshable_olt_advertises_its_button(self):
        # The panel draws Refresh off can_refresh, which the server answers from
        # the sweep's OWN target query — the button, the route that serves it
        # and the sweep must agree about what is readable, and three copies of
        # that rule would not.
        olt = self._readable_olt()
        _, body = self._get(f"/api/inventory/rx-status?device_id={olt}")
        self.assertTrue(body["can_refresh"])
        self.assertFalse(body["refreshing"])
        # ...and the same OLT with its login removed is no longer offered one
        self.store.clear_device_webui_credentials(ORG, olt)
        _, gone = self._get(f"/api/inventory/rx-status?device_id={olt}")
        self.assertFalse(gone["can_refresh"])

    def test_an_ineligible_olt_is_refused_without_touching_its_status(self):
        # A status row reports an ATTEMPT. Recording "you can't read this" would
        # erase the last thing that really happened on the box, so the refusal
        # is an HTTP answer, not a verdict written to the panel.
        self.store.set_web_optics_status(ORG, self.dark_olt, "dbc", "ok", None, 100)
        status, body = self._post("/api/inventory/rx-refresh",
                                  {"device_id": self.dark_olt})
        self.assertEqual(status, 400, body)
        _, rx = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        self.assertEqual(rx["scrape"]["state"], "ok")
        self.assertFalse(rx["can_refresh"])

    def test_a_manual_read_answers_at_once_rather_than_holding_the_request(self):
        # One OLT costs up to web_optics_device_budget_s (120s). A request held
        # that long is a browser timeout and a worker thread this server hasn't
        # got spare — the panel watches the recorded status instead.
        olt = self._readable_olt()
        status, body = self._post("/api/inventory/rx-refresh", {"device_id": olt})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["started"])

    def test_a_second_click_is_refused_rather_than_queued(self):
        # Two overlapping scrapes knock each other's session out on a box with
        # one session slot — and reporting "still running" as the OUTCOME would
        # overwrite the last real verdict on the panel.
        self.server.weboptics._lock_for(self.dark_olt).acquire()
        try:
            status, body = self._post("/api/inventory/rx-refresh",
                                      {"device_id": self.dark_olt})
            self.assertEqual(status, 409, body)
            _, rx = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
            self.assertTrue(rx["refreshing"])
        finally:
            self.server.weboptics._lock_for(self.dark_olt).release()

    def test_a_manual_read_of_another_orgs_olt_is_refused(self):
        other = self.store.create_org_device("ispB", {
            "name": "B-OLT", "ip_address": "10.9.9.8", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        status, _ = self._post("/api/inventory/rx-refresh", {"device_id": other})
        self.assertEqual(status, 403)

    def test_a_worker_cannot_spend_the_stored_credential(self):
        # The read POSTs the OLT's admin login down the tunnel — the same grade
        # of action as opening a proxy session, which is owner-only.
        auth.create_user(self.store, ORG, "fieldhand", "workerpassword", "worker")
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": "fieldhand",
                                     "password": "workerpassword"})
        status, _, _ = self._req("POST", "/api/inventory/rx-refresh",
                                 {"device_id": self.dark_olt},
                                 cookie=setcookie.split(";")[0])
        self.assertEqual(status, 403)

    def test_rx_status_is_org_scoped(self):
        other = self.store.create_org_device("ispB", {
            "name": "B-OLT", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        status, _ = self._get(f"/api/inventory/rx-status?device_id={other}")
        self.assertEqual(status, 403)

    # --- vendor recipes as data ---------------------------------------------

    def test_a_new_vendor_is_onboarded_by_a_dashboard_row(self):
        # The point of the whole profile mechanism: a vendor central has never
        # heard of becomes readable without touching code or rolling an edge.
        status, body = self._post("/api/web-optics-profiles", {
            **BUILTIN_SPECS["dbc"], "name": "vsol", "enabled": True,
            "optics_path": "/cgi-bin/onu_optical.cgi",
        })
        self.assertEqual(status, 200, body)
        _, listing = self._get("/api/web-optics-profiles")
        names = [p["name"] for p in listing["profiles"]]
        self.assertIn("vsol", names)
        # ...and it immediately counts as a vendor the sweeper will scrape.
        _, rx = self._get(f"/api/inventory/rx-status?device_id={self.dark_olt}")
        self.assertIn("vsol", rx["known_vendors"])

    def test_a_recipe_that_could_lie_is_refused_by_the_server(self):
        # Validation is server-side, so an SPA change (or a curl) can't slip a
        # profile past the vocabulary. A full URL would hand back the property
        # that keeps the tunnel from being a lateral-movement primitive.
        status, body = self._post("/api/web-optics-profiles", {
            **BUILTIN_SPECS["dbc"], "name": "sneaky",
            "optics_path": "http://192.168.1.1/admin",
        })
        self.assertEqual(status, 422, body)
        # ...and one that can't locate received power is equally useless.
        cols = {k: v for k, v in BUILTIN_SPECS["dbc"]["columns"].items()
                if k != "rx_dbm"}
        status, _ = self._post("/api/web-optics-profiles", {
            **BUILTIN_SPECS["dbc"], "name": "norx", "columns": cols,
            "column_order": [],
        })
        self.assertEqual(status, 422)

    def test_deleting_the_device_takes_its_scrape_status_with_it(self):
        # web_optics_status carries an FK to org_devices; a delete that leaves
        # the row behind aborts the whole statement under foreign_keys=ON.
        self.store.set_web_optics_status(ORG, self.dark_olt, "dbc", "ok", None, 5)
        self.assertTrue(self.store.delete_org_device(ORG, self.dark_olt)["ok"])
        self.assertIsNone(self.store.get_web_optics_status(ORG, self.dark_olt))


if __name__ == "__main__":
    unittest.main()
