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

from wisp.central import api, auth, server
from wisp.central.api import replay as replay_api
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.central.store_replay import ReplayStoreMixin
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"
OTHER = "ispB"
ROUTE = "/api/history/replay"


# ReplayStoreMixin is composed into CentralStore's bases; re-composing it here
# would be an MRO conflict.
_Store = CentralStore
assert issubclass(_Store, ReplayStoreMixin)


def _iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def _ep(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = _Store(self.cfg.central_db)
        self.store.set_org(ORG, "Isp A")
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        auth.create_user(self.store, OTHER, "other", "otherpassword", "owner")
        # Stamped at CALL time: discovery imports every test file up front, so
        # a module-level "now" is stale by the time the file runs.
        self.now = datetime.now(timezone.utc)
        self.dev = self.store.create_org_device(ORG, {
            "name": "HLY-OLT", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": "north", "parent_device_id": None})
        self.child = self.store.create_org_device(ORG, {
            "name": "SPL-FEED", "ip_address": "10.0.0.3",
            "device_type": "switch", "region": "north",
            "parent_device_id": self.dev})
        self.far = self.store.create_org_device(ORG, {
            "name": "FAR-SW", "ip_address": "10.0.0.4",
            "device_type": "switch", "region": "south",
            "parent_device_id": None})
        self.notifier = RecordingNotifier()
        # The two lines the orchestrator adds (api/__init__.py GET table and
        # server._WORKER_GET). Applied here so the route is reachable before
        # the wiring lands; both are idempotent.
        self._added_route = ROUTE not in api.GET
        api.GET[ROUTE] = replay_api.replay
        self._added_worker = ROUTE not in server._WORKER_GET
        server._WORKER_GET.add(ROUTE)
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        if self._added_route:
            api.GET.pop(ROUTE, None)
        if self._added_worker:
            server._WORKER_GET.discard(ROUTE)
        self.tmp.cleanup()

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

    def _on_probe(self, device_id, node):
        with self.store._connect() as conn:
            conn.execute("UPDATE org_devices SET assigned_node_id=? WHERE id=?",
                         (node, device_id))
            conn.commit()

    def _outage(self, device_id, start, end=None, state="DOWN", org=ORG):
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at,"
                " resolved_at, final_state) VALUES (?,?,?,?,?)",
                (org, device_id, _iso(start),
                 _iso(end) if end else None, state))
            conn.commit()


class ReplyShapeTest(_HttpBase):
    def test_the_reply_is_an_interval_list_in_epoch_seconds(self):
        start = self.now - timedelta(days=2)
        self._outage(self.dev, start, start + timedelta(hours=1))
        self._outage(self.child, start + timedelta(minutes=1))   # still open
        st, body = self._req(f"{ROUTE}?days=7", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(body["days"], 7)
        self.assertEqual(body["until"] - body["since"], 7 * 86400)
        self.assertIsInstance(body["since"], int)
        spans = {s["device_id"]: s for s in body["spans"]}
        self.assertEqual(spans[self.dev]["start"], _ep(start))
        self.assertEqual(spans[self.dev]["end"],
                         _ep(start + timedelta(hours=1)))
        self.assertIsNone(spans[self.child]["end"])
        self.assertEqual(spans[self.dev]["state"], "DOWN")

    def test_every_device_ships_its_own_recording_floor(self):
        # A device that did not exist at T is `unknown` there, never up.
        st, body = self._req(f"{ROUTE}?days=7", self._login())
        floors = {d["device_id"]: d["since"] for d in body["devices"]}
        self.assertEqual(sorted(floors), sorted([self.dev, self.child, self.far]))
        self.assertTrue(all(v is not None for v in floors.values()))
        self.assertIsNotNone(body["org_since"])

    def test_an_outage_older_than_the_window_keeps_its_true_start(self):
        # Clipping the start to `since` would read as "it dropped exactly when
        # this window opened".
        start = self.now - timedelta(days=20)
        self._outage(self.dev, start, self.now - timedelta(days=1))
        st, body = self._req(f"{ROUTE}?days=7", self._login())
        self.assertEqual(st, 200)
        span = body["spans"][0]
        self.assertEqual(span["start"], _ep(start))
        self.assertLess(span["start"], body["since"])

    def test_an_unreachable_span_is_reported_as_its_own_state(self):
        start = self.now - timedelta(hours=3)
        self._outage(self.child, start, start + timedelta(hours=1),
                     state="UNREACHABLE")
        st, body = self._req(f"{ROUTE}?days=1", self._login())
        self.assertEqual(body["spans"][0]["state"], "UNREACHABLE")

    def test_an_outage_wholly_outside_the_window_is_not_shipped(self):
        start = self.now - timedelta(days=40)
        self._outage(self.dev, start, start + timedelta(hours=1))
        st, body = self._req(f"{ROUTE}?days=7", self._login())
        self.assertEqual(body["spans"], [])


class ClampTest(_HttpBase):
    def test_the_window_clamps_to_ninety_days(self):
        st, body = self._req(f"{ROUTE}?days=4000", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(body["days"], 90)
        self.assertEqual(body["until"] - body["since"], 90 * 86400)

    def test_a_nonsense_or_zero_window_falls_back_to_a_real_one(self):
        for q in ("days=0", "days=-5", "days=banana", ""):
            st, body = self._req(f"{ROUTE}?{q}", self._login())
            self.assertEqual(st, 200)
            self.assertGreaterEqual(body["days"], 1)
            self.assertLessEqual(body["days"], 90)

    def test_unauthenticated_is_refused(self):
        st, _ = self._req(f"{ROUTE}?days=7")
        self.assertEqual(st, 401)


class BlindWindowTest(_HttpBase):
    def _probe(self, node="edge1"):
        self.store.record_heartbeat(ORG, node, {"version": "v0"})

    def test_a_silent_probe_blinds_the_devices_it_carries(self):
        self._probe()
        self._on_probe(self.dev, "edge1")
        gone = self.now - timedelta(hours=5)
        back = self.now - timedelta(hours=4)
        self.store.record_node_alert(ORG, "edge1", "NODE_STALE", "sent", "",
                                     _iso(gone))
        self.store.record_node_alert(ORG, "edge1", "NODE_OK", "sent", "",
                                     _iso(back))
        st, body = self._req(f"{ROUTE}?days=1", self._login())
        self.assertEqual(st, 200)
        blind = [b for b in body["blind"] if b["device_id"] == self.dev]
        self.assertEqual(len(blind), 1)
        self.assertEqual(blind[0]["end"], _ep(back))
        # back-dated by the watchdog's own stale threshold
        self.assertEqual(blind[0]["start"],
                         _ep(gone) - self.cfg.central_node_stale_s)
        # a device on no probe assignment is covered by every node, and there
        # is only one — so it is blind too
        self.assertIn(self.child, {b["device_id"] for b in body["blind"]})

    def test_a_failed_page_still_records_the_transition(self):
        # Whether WhatsApp went through says nothing about whether the probe
        # was reporting.
        self._probe()
        self._on_probe(self.dev, "edge1")
        self.store.record_node_alert(ORG, "edge1", "NODE_STALE", "failed", "",
                                     _iso(self.now - timedelta(hours=2)))
        st, body = self._req(f"{ROUTE}?days=1", self._login())
        blind = [b for b in body["blind"] if b["device_id"] == self.dev]
        self.assertEqual(len(blind), 1)
        self.assertIsNone(blind[0]["end"])

    def test_a_healthy_fleet_has_no_blind_windows(self):
        self._probe()
        st, body = self._req(f"{ROUTE}?days=7", self._login())
        self.assertEqual(body["blind"], [])


class ScopeTest(_HttpBase):
    def _assign(self, device_ids):
        worker = self.store.get_user_by_username("field")
        self.store.bulk_assign_devices(ORG, device_ids, [worker["id"]], "owner")

    def test_a_worker_sees_only_its_assigned_scope(self):
        start = self.now - timedelta(hours=6)
        self._outage(self.dev, start, start + timedelta(minutes=20))
        self._outage(self.far, start, start + timedelta(minutes=20))
        self._assign([self.dev])
        st, body = self._req(f"{ROUTE}?days=1",
                             self._login("field", "fieldpassword"))
        self.assertEqual(st, 200)
        # scope_of walks DOWN the tree, so the child under the assigned OLT
        # comes with it; FAR-SW does not.
        seen = {d["device_id"] for d in body["devices"]}
        self.assertEqual(seen, {self.dev, self.child})
        self.assertEqual({s["device_id"] for s in body["spans"]}, {self.dev})

    def test_an_unassigned_worker_replays_nothing(self):
        start = self.now - timedelta(hours=6)
        self._outage(self.dev, start)
        st, body = self._req(f"{ROUTE}?days=1",
                             self._login("field", "fieldpassword"))
        self.assertEqual(st, 200)
        self.assertEqual(body["devices"], [])
        self.assertEqual(body["spans"], [])

    def test_the_owner_sees_the_whole_fleet(self):
        start = self.now - timedelta(hours=6)
        self._outage(self.dev, start)
        self._outage(self.far, start)
        self._assign([self.dev])
        st, body = self._req(f"{ROUTE}?days=1", self._login())
        self.assertEqual({s["device_id"] for s in body["spans"]},
                         {self.dev, self.far})

    def test_another_orgs_record_never_leaks(self):
        self.store.set_org(OTHER, "Isp B")
        theirs = self.store.create_org_device(OTHER, {
            "name": "B-SW", "ip_address": "10.9.0.2", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self._outage(theirs, self.now - timedelta(hours=2), org=OTHER)
        st, body = self._req(f"{ROUTE}?days=1", self._login())
        self.assertEqual(st, 200)
        self.assertNotIn(theirs, {d["device_id"] for d in body["devices"]})
        self.assertEqual(body["spans"], [])
        st, mine = self._req(f"{ROUTE}?days=1",
                             self._login("other", "otherpassword"))
        self.assertEqual(st, 200)
        self.assertEqual({s["device_id"] for s in mine["spans"]}, {theirs})


if __name__ == "__main__":
    unittest.main()
