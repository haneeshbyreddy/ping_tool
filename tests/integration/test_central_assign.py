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

from support import RecordingNotifier
from wisp.central import auth
from wisp.central import engine as central_engine
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.ports import CentralPortMonitor
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.central.watchdog import CentralWatchdog
from wisp.config import Config
from wisp.core.state_machine import DOWN, OutageOpened

T0 = "2026-07-26T04:00:00+00:00"

OWNER = "919000000009"
RAVI = "919000000001"
KIRAN = "919000000002"


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA", name="A")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        auth.create_user(self.store, "ispA", "kiran", "kiranpassword", "worker")
        self.store.set_user_whatsapp(self._uid("owner"), OWNER)
        self.store.set_user_whatsapp(self._uid("ravi"), RAVI)
        self.store.set_user_whatsapp(self._uid("kiran"), KIRAN)
        self.wan = self.store.create_org_device("ispA", {
            "name": "WAN-SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.olt = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": "north", "parent_device_id": self.wan,
            "assigned_node_id": "probe1"})
        self.notifier = RecordingNotifier()

    def _uid(self, username, org="ispA"):
        return next(u["id"] for u in self.store.list_users(org)
                    if u["username"] == username)

    def _paged(self):
        return sorted(self.notifier.sent[-1]["whatsapp"])


class DevicePagingTest(_Base):

    def setUp(self):
        super().setUp()
        self.engine = central_engine.build_engine(self.store, "ispA", self.cfg)

    def _page_down(self, device_id):
        disp = CentralAlertDispatcher(self.store, "ispA", self.engine,
                                      self.notifier, self.cfg)
        self.store.open_outage_if_absent("ispA", device_id, T0, DOWN)
        disp.dispatch([OutageOpened(device_id, DOWN)], T0)

    def test_unassigned_device_still_pages_the_whole_team(self):
        self._page_down(self.olt)
        self.assertEqual(self._paged(), [RAVI, KIRAN, OWNER])

    def test_an_assigned_device_pages_owner_plus_assignee_only(self):
        self.store.set_device_assignees("ispA", self.olt, [self._uid("ravi")],
                                        "owner")
        self._page_down(self.olt)
        self.assertEqual(self._paged(), [RAVI, OWNER])

    def test_responsibility_is_inherited_by_the_subtree(self):
        self.store.set_device_assignees("ispA", self.wan, [self._uid("kiran")],
                                        "owner")
        self._page_down(self.olt)
        self.assertEqual(self._paged(), [KIRAN, OWNER])

    def test_a_narrower_assignment_does_not_un_page_the_region_owner(self):
        self.store.set_device_assignees("ispA", self.wan, [self._uid("kiran")],
                                        "owner")
        self.store.set_device_assignees("ispA", self.olt, [self._uid("ravi")],
                                        "owner")
        self._page_down(self.olt)
        self.assertEqual(self._paged(), [RAVI, KIRAN, OWNER])

    def test_the_alert_log_records_the_narrowed_audience(self):
        self.store.set_device_assignees("ispA", self.olt, [self._uid("ravi")],
                                        "owner")
        self._page_down(self.olt)
        with self.store._connect() as conn:
            recipient = conn.execute(
                "SELECT recipient FROM alert_log WHERE kind='DEVICE_DOWN'"
                " ORDER BY id DESC LIMIT 1").fetchone()["recipient"]
        self.assertEqual(sorted((recipient or "").split(",")), [RAVI, OWNER])

    def test_an_uplink_alert_has_no_device_and_stays_org_wide(self):
        from wisp.core.state_machine import UplinkDown
        disp = CentralAlertDispatcher(self.store, "ispA", self.engine,
                                      self.notifier, self.cfg)
        self.store.set_device_assignees("ispA", self.wan, [self._uid("ravi")],
                                        "owner")
        disp.dispatch([UplinkDown()], T0)
        self.assertEqual(self._paged(), [RAVI, KIRAN, OWNER])


class PortPagingTest(_Base):
    def _sync(self, oper_status):
        mon = CentralPortMonitor(self.store, "ispA", self.notifier, self.cfg)
        for _ in range(self.cfg.snmp_down_consecutive):
            mon.sync_device(self.olt, [{
                "if_index": 1, "if_name": "ge0/1", "admin_status": "up",
                "oper_status": oper_status}], T0)
        return mon

    def _monitor_port(self):
        self._sync("up")
        port = self.store.list_switch_ports("ispA", self.olt)[0]
        self.store.set_port_monitored("ispA", port["id"], True)

    def test_port_down_narrows_to_the_responsible_worker(self):
        self._monitor_port()
        self.store.set_device_assignees("ispA", self.olt, [self._uid("kiran")],
                                        "owner")
        self._sync("down")
        sent = [s for s in self.notifier.sent if "Port down" in s["title"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sorted(sent[0]["whatsapp"]), [KIRAN, OWNER])

    def test_port_down_on_an_unassigned_switch_pages_everyone(self):
        self._monitor_port()
        self._sync("down")
        sent = [s for s in self.notifier.sent if "Port down" in s["title"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sorted(sent[0]["whatsapp"]), [RAVI, KIRAN, OWNER])


class ProbePagingTest(_Base):
    def _check(self):
        wd = CentralWatchdog(self.store, self.cfg, self.notifier)
        stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=self.cfg.central_node_stale_s + 60)
        self.store.touch_node("ispA", "probe1", stale.isoformat(sep=" ",
                                                                timespec="seconds"))
        wd.check()

    def test_probe_down_narrows_to_whoever_owns_what_it_probes(self):
        self.store.set_device_assignees("ispA", self.wan, [self._uid("ravi")],
                                        "owner")
        self._check()
        self.assertTrue(self.notifier.sent, "expected a PROBE DOWN page")
        self.assertEqual(self._paged(), [RAVI, OWNER])

    def test_probe_down_with_nothing_assigned_behind_it_stays_org_wide(self):
        self._check()
        self.assertEqual(self._paged(), [RAVI, KIRAN, OWNER])


class AssignApiTest(_Base):

    def setUp(self):
        super().setUp()
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.other = self.store.create_org_device("ispB", {
            "name": "B1", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

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


    def test_the_roster_carries_counts_and_reachability_but_no_numbers(self):
        self.store.set_device_assignees("ispA", self.wan, [self._uid("ravi")],
                                        "owner")
        status, body = self._req("GET", "/api/inventory/assignments",
                                 cookie=self._login())
        self.assertEqual(status, 200)
        ravi = next(a for a in body["accounts"] if a["username"] == "ravi")
        self.assertEqual(ravi["assigned"], 1)
        self.assertEqual(ravi["devices"], 2)
        self.assertTrue(ravi["has_whatsapp"])
        self.assertNotIn("whatsapp_number", ravi)
        self.assertEqual(body["unassigned"], 0)

    def test_unassigned_count_is_what_still_pages_everybody(self):
        status, body = self._req("GET", "/api/inventory/assignments",
                                 cookie=self._login())
        self.assertEqual(body["unassigned"], 2)

    def test_a_worker_cannot_read_the_roster(self):
        status, _ = self._req("GET", "/api/inventory/assignments",
                              cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)


    def test_owner_sets_and_clears_a_device_assignment(self):
        cookie = self._login()
        status, body = self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [self._uid("ravi")]}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.device_assignment_map("ispA"),
                         {self.olt: {self._uid("ravi")}})
        status, _ = self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": []}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.device_assignment_map("ispA"), {})

    def test_an_assignee_with_no_number_is_reported_not_refused(self):
        auth.create_user(self.store, "ispA", "nonum", "nonumpassword", "worker")
        status, body = self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [self._uid("nonum")]},
            self._login())
        self.assertEqual(status, 200)
        self.assertEqual(body["unreachable"], ["nonum"])
        self.assertEqual(self.store.device_assignment_map("ispA"),
                         {self.olt: {self._uid("nonum")}})

    def test_bulk_assign_is_additive_across_devices(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [self._uid("kiran")]}, cookie)
        status, body = self._req("POST", "/api/inventory/assign", {
            "device_ids": [self.wan, self.olt],
            "user_ids": [self._uid("ravi")]}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["changed"], 2)
        self.assertEqual(
            self.store.device_assignment_map("ispA"),
            {self.wan: {self._uid("ravi")},
             self.olt: {self._uid("ravi"), self._uid("kiran")}})

    def test_bulk_remove(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/assign", {
            "device_ids": [self.wan, self.olt],
            "user_ids": [self._uid("ravi")]}, cookie)
        self._req("POST", "/api/inventory/assign", {
            "device_ids": [self.olt], "user_ids": [self._uid("ravi")],
            "mode": "remove"}, cookie)
        self.assertEqual(self.store.device_assignment_map("ispA"),
                         {self.wan: {self._uid("ravi")}})

    def test_a_worker_cannot_assign(self):
        status, _ = self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [self._uid("ravi")]},
            self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)
        self.assertEqual(self.store.device_assignment_map("ispA"), {})

    def test_another_orgs_device_is_refused(self):
        status, _ = self._req("POST", "/api/inventory/assign", {
            "device_id": self.other, "user_ids": [self._uid("ravi")]},
            self._login())
        self.assertEqual(status, 403)
        self.assertEqual(self.store.device_assignment_map("ispB"), {})

    def test_an_account_from_another_org_is_refused_loudly(self):
        bowner = self._uid("bowner", "ispB")
        status, _ = self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [bowner]}, self._login())
        self.assertEqual(status, 422)
        self.assertEqual(self.store.device_assignment_map("ispA"), {})

    def test_a_mixed_org_bulk_list_is_refused_whole(self):
        status, _ = self._req("POST", "/api/inventory/assign", {
            "device_ids": [self.olt, self.other],
            "user_ids": [self._uid("ravi")]}, self._login())
        self.assertEqual(status, 422)
        self.assertEqual(self.store.device_assignment_map("ispA"), {})

    def test_the_device_list_carries_the_explicit_assignees(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/assign", {
            "device_id": self.olt, "user_ids": [self._uid("ravi")]}, cookie)
        status, body = self._req("GET", "/api/inventory?org=ispA", cookie=cookie)
        rows = {d["id"]: d for d in body["devices"]}
        self.assertEqual(rows[self.olt]["assignee_ids"], [self._uid("ravi")])
        self.assertEqual(rows[self.wan]["assignee_ids"], [])


class VisibilityTest(_Base):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.store.set_device_assignees("ispA", self.olt, [self._uid("ravi")],
                                        "owner")

    def _login(self, username, password):
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

    def _devices(self, cookie):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/inventory?org=ispA", headers={"Cookie": cookie})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return {d["id"] for d in body["devices"]}

    def test_an_unassigned_worker_still_sees_the_whole_fleet(self):
        seen = self._devices(self._login("kiran", "kiranpassword"))
        self.assertEqual(seen, {self.wan, self.olt})

    def test_the_assigned_worker_sees_the_whole_fleet_too(self):
        seen = self._devices(self._login("ravi", "ravipassword"))
        self.assertEqual(seen, {self.wan, self.olt})


if __name__ == "__main__":
    unittest.main()
