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

from wisp.central import analytics as central_analytics
from wisp.central import api as api_routes
from wisp.central import auth, history
from wisp.central import server as server_mod
from wisp.central.api import history as history_api
from wisp.central.optics import CentralOpticsMonitor
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"
OTHER_ORG = "ispB"
PATH = "/api/history/onu"

def _iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


class _HttpBase(unittest.TestCase):
    # The route row and the worker-gate entry belong in api/__init__.py and
    # server.py, which this workstream does not own. Wiring them for the life
    # of each test exercises the FULL stack — session, worker gate, billing,
    # device scope — before and after the real rows land, and names exactly
    # what has to be wired. Registered per test rather than at import: the
    # route tables are module state and test_routes.py compares the built GET
    # table against the SOURCE literal, so a permanent runtime row would fail
    # a test in another file. Removed again only if this put it there.

    def _wire_route(self):
        self._added_route = PATH not in api_routes.GET
        self._added_worker = PATH not in server_mod._WORKER_GET
        api_routes.GET.setdefault(PATH, history_api.onu_history)
        server_mod._WORKER_GET.add(PATH)

    def _unwire_route(self):
        if getattr(self, "_added_route", False):
            api_routes.GET.pop(PATH, None)
        if getattr(self, "_added_worker", False):
            server_mod._WORKER_GET.discard(PATH)

    def setUp(self):
        self._wire_route()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        auth.create_user(self.store, OTHER_ORG, "rival", "rivalpassword",
                         "owner")
        # Stamped at CALL time: discovery imports every test file up front, so
        # an import-time "now" is stale by the time this runs — and every read
        # here clamps to the historian's own recording_since.
        self.now = datetime.now(timezone.utc)
        self.olt = self.store.create_org_device(ORG, {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": "north", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()
        self._unwire_route()

    def _req(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username,
                                      "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _walk(self, rows, when=None, device_id=None):
        # rows: (onu_key, pon_port, state, rx)
        mon = CentralOpticsMonitor(self.store, ORG, self.notifier, self.cfg)
        mon.sync_device(device_id or self.olt, [
            {"onu_key": k, "pon_port": pon, "onu_id": 1, "name": k,
             "serial": k, "state": state, "rx_dbm": rx}
            for k, pon, state, rx in rows],
            _iso(when or self.now))

    def _fold_today(self):
        self.store.fold_history_day(
            history.day_floor(int(self.now.timestamp())))

    def _get(self, onu="1.1", days=2, cookie=None, device_id=None):
        did = self.olt if device_id is None else device_id
        return self._req(f"{PATH}?device_id={did}&onu={onu}&days={days}",
                         cookie or self._login())


class ReplyShapeTest(_HttpBase):
    def test_the_hour_tier_carries_the_slots_own_bucket(self):
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get()
        self.assertEqual(st, 200)
        self.assertEqual(body["tier"], "hour")
        self.assertEqual(body["onu"], {"onu_key": "1.1",
                                       "pon_port": "EPON0/1"})
        self.assertEqual(len(body["buckets"]), 1)
        b = body["buckets"][0]
        self.assertEqual((b["samples"], b["online"], b["rx_n"]), (1, 1, 1))
        self.assertAlmostEqual(b["rx_avg"], -19.0)
        self.assertEqual((b["rx_min"], b["rx_max"]), (-19.0, -19.0))
        self.assertEqual(b["t"] % 3600, 0)
        self.assertTrue(body["recording_since"])

    def test_rx_avg_min_and_max_fold_two_walks(self):
        # on the day tier, so the window starts at midnight and the fixture
        # cannot be cut in half by landing either side of an hour boundary
        self._walk([("1.1", "EPON0/1", "online", -19.0)],
                   self.now - timedelta(seconds=1))
        self._walk([("1.1", "EPON0/1", "online", -21.0)])
        self._fold_today()
        st, body = self._get(days=7)
        self.assertEqual(st, 200)
        self.assertEqual(len(body["buckets"]), 1)
        b = body["buckets"][0]
        self.assertEqual((b["samples"], b["online"], b["rx_n"]), (2, 2, 2))
        self.assertAlmostEqual(b["rx_avg"], -20.0)
        self.assertEqual((b["rx_min"], b["rx_max"]), (-21.0, -19.0))

    def test_rx_avg_is_null_when_nothing_was_measured_never_zero(self):
        # most of the C-Data/DBC fleet walks a complete roster with no dBm:
        # "nothing measured" and "nothing wrong" may not render alike
        self._walk([("1.1", "EPON0/1", "online", None)])
        st, body = self._get()
        self.assertEqual(st, 200)
        b = body["buckets"][0]
        self.assertEqual((b["samples"], b["online"], b["rx_n"]), (1, 1, 0))
        self.assertIsNone(b["rx_avg"])
        self.assertIsNone(b["rx_min"])

    def test_a_dark_slot_still_gets_a_row_with_no_reading(self):
        self._walk([("1.1", "EPON0/1", "offline", -24.0)])
        st, body = self._get()
        b = body["buckets"][0]
        self.assertEqual((b["samples"], b["online"], b["rx_n"]), (1, 0, 0))
        self.assertIsNone(b["rx_avg"])

    def test_the_pon_band_is_that_pons_own_median(self):
        self._walk([("1.1", "EPON0/1", "online", -19.0),
                    ("1.2", "EPON0/1", "online", -21.0),
                    ("2.1", "EPON0/2", "online", -30.0)])
        st, body = self._get()
        self.assertEqual(st, 200)
        self.assertEqual(len(body["sibling"]), 1)
        s = body["sibling"][0]
        self.assertEqual(s["rx_n"], 1)
        self.assertAlmostEqual(s["rx_med"], -21.0)   # nearest-rank of the PON
        self.assertEqual(s["t"], body["buckets"][0]["t"])

    def test_a_slot_with_no_pon_label_ships_no_band_rather_than_a_guess(self):
        self._walk([("1.1", None, "online", -19.0)])
        st, body = self._get()
        self.assertEqual(st, 200)
        self.assertIsNone(body["onu"]["pon_port"])
        self.assertEqual(body["sibling"], [])
        self.assertEqual(len(body["buckets"]), 1)

    def test_transitions_ride_along_ascending(self):
        # walks are stamped BEHIND now: the window ends at the request, so a
        # sample from the future is honestly outside it
        self._walk([("1.1", "EPON0/1", "online", -19.0)],
                   self.now - timedelta(seconds=3))
        self._walk([("1.1", "EPON0/1", "offline", None)],
                   self.now - timedelta(seconds=2))
        self._walk([("1.1", "EPON0/1", "online", -19.0)],
                   self.now - timedelta(seconds=1))
        st, body = self._get(days=7)
        self.assertEqual([(e["old"], e["new"]) for e in body["events"]],
                         [(None, "online"), ("online", "offline"),
                          ("offline", "online")])
        self.assertEqual([e["ts"] for e in body["events"]],
                         sorted(e["ts"] for e in body["events"]))

    def test_the_thresholds_are_the_olts_own_pair(self):
        self.store.set_olt_optical_thresholds(ORG, self.olt, -23.5, -27.5)
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get()
        self.assertEqual(body["thresholds"], {"warn": -23.5, "crit": -27.5})

    def test_an_olt_with_no_override_falls_back_to_the_config_pair(self):
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get()
        self.assertEqual(body["thresholds"],
                         {"warn": self.cfg.optical_warn_dbm,
                          "crit": self.cfg.optical_crit_dbm})

    def test_the_olts_outages_are_the_reliability_derivation_not_a_new_one(self):
        # An OPEN outage that started before the window: the clamped window
        # here begins at recording_since, so a resolved span from before the
        # historian existed is honestly outside it (the young-historian rule).
        start = self.now - timedelta(hours=25)
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at,"
                " resolved_at, final_state) VALUES (?,?,?,NULL,'DOWN')",
                (ORG, self.olt, _iso(start)))
            conn.commit()
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get(days=7)
        self.assertEqual(st, 200)
        self.assertEqual(body["outages"],
                         [{"start": _iso(start), "end": None}])
        # count agreement: the SAME derivation the reliability strip reads,
        # over the window this reply itself declares
        spans = central_analytics.day_availability(
            self.store, ORG, self.olt, body["since"], body["until"])["spans"]
        self.assertEqual(body["outages"],
                         [{"start": s["started_at"], "end": s["resolved_at"]}
                          for s in spans])


class TierTest(_HttpBase):
    def test_two_days_reads_the_hour_tier_and_seven_the_day_tier(self):
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        self._fold_today()
        st, hour = self._get(days=2)
        self.assertEqual(hour["tier"], "hour")
        self.assertEqual(hour["buckets"][0]["t"] % 3600, 0)
        st, day = self._get(days=7)
        self.assertEqual(day["tier"], "day")
        self.assertEqual(len(day["buckets"]), 1)
        self.assertEqual(day["buckets"][0]["t"] % 86400, 0)
        self.assertEqual(day["buckets"][0]["samples"], 1)

    def test_the_window_clamps_to_recording_since_on_the_tiers_own_grid(self):
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        self._fold_today()
        st, body = self._get(days=400)
        self.assertEqual(st, 200)
        rec = datetime.fromisoformat(body["recording_since"][:19])
        self.assertEqual(body["since"],
                         _iso(rec.replace(hour=0, minute=0, second=0)))

    def test_the_range_is_bounded_however_many_days_are_asked_for(self):
        st, body = self._get(days=99999)
        self.assertEqual(st, 200)
        span = (datetime.fromisoformat(body["until"])
                - datetime.fromisoformat(body["since"]))
        self.assertLessEqual(span, timedelta(days=history_api.MAX_DAYS + 1))


class RefusalTest(_HttpBase):
    def test_an_unknown_slot_on_a_readable_device_is_an_empty_200(self):
        # the panel opens from a roster row, so a 404 here renders as a broken
        # panel rather than as "nothing recorded yet"
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get(onu="9.9")
        self.assertEqual(st, 200)
        self.assertEqual(body["onu"], {"onu_key": "9.9", "pon_port": None})
        self.assertEqual(body["buckets"], [])
        self.assertEqual(body["sibling"], [])
        self.assertEqual(body["events"], [])
        self.assertTrue(body["recording_since"])

    def test_a_missing_onu_is_a_400(self):
        st, body = self._req(f"{PATH}?device_id={self.olt}&days=2",
                             self._login())
        self.assertEqual(st, 400)

    def test_a_missing_device_is_a_400(self):
        st, _ = self._req(f"{PATH}?onu=1.1&days=2", self._login())
        self.assertEqual(st, 400)

    def test_signed_out_is_a_401(self):
        st, _ = self._req(f"{PATH}?device_id={self.olt}&onu=1.1")
        self.assertEqual(st, 401)

    def test_another_orgs_owner_is_refused(self):
        st, _ = self._get(cookie=self._login("rival", "rivalpassword"))
        self.assertEqual(st, 403)

    def test_a_worker_with_nothing_assigned_is_refused(self):
        # the 2026-08-12 rule: unassigned reaches no worker, in either sense
        st, _ = self._get(cookie=self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)

    def test_a_worker_reads_the_history_of_a_device_assigned_to_them(self):
        uid = [u["id"] for u in self.store.list_users(ORG)
               if u["username"] == "field"][0]
        self.store.set_device_assignees(ORG, self.olt, [uid], "owner")
        self._walk([("1.1", "EPON0/1", "online", -19.0)])
        st, body = self._get(cookie=self._login("field", "fieldpassword"))
        self.assertEqual(st, 200)
        self.assertEqual(len(body["buckets"]), 1)

    def test_a_worker_is_still_refused_the_device_NEXT_to_the_assigned_one(self):
        far = self.store.create_org_device(ORG, {
            "name": "OLT-2", "ip_address": "10.0.0.3", "device_type": "olt",
            "region": "north", "parent_device_id": None})
        uid = [u["id"] for u in self.store.list_users(ORG)
               if u["username"] == "field"][0]
        self.store.set_device_assignees(ORG, self.olt, [uid], "owner")
        st, _ = self._get(cookie=self._login("field", "fieldpassword"),
                          device_id=far)
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
