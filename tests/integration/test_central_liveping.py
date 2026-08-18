"""Live ping over HTTP: the FSM firewall, the bounds, the scope, the gate.

The load-bearing test in this file is `FsmIsolationTest` — a whole live
session, five minutes of packets compressed into one exchange, that leaves
device state, events, outages and the notifier exactly as it found them. The
unit suite pins the STRUCTURE (nothing in the live-ping modules can reach the
engine); this pins the BEHAVIOUR, which is the claim an operator actually
cares about: watching a device cannot page anybody.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from support import RecordingNotifier
from wisp.central import auth
from wisp.central.liveping import MIN_EDGE_VERSION
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config

NEW = MIN_EDGE_VERSION
OLD = "0.15.1"


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="edge-secret",
                          liveping_poll_hold_s=0.2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA", name="A")
        self.store.set_org("ispB", name="B")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        auth.create_user(self.store, "ispA", "sunil", "sunilpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        auth.create_user(self.store, None, "root", "rootpassword", "owner")

        self.olt = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.cpe = self.store.create_org_device("ispA", {
            "name": "TOWER-AP", "ip_address": "10.0.0.3", "device_type": "AP",
            "region": "north", "parent_device_id": self.olt,
            "assigned_node_id": "probe1"})
        self.spare = self.store.create_org_device("ispA", {
            "name": "SPARE-SW", "ip_address": "10.0.0.4", "device_type": "switch",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.foreign = self.store.create_org_device("ispB", {
            "name": "B1", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "probeB"})
        # Ravi is assigned the CPE only. Sunil is assigned nothing.
        uid = next(u["id"] for u in self.store.list_users("ispA")
                   if u["username"] == "ravi")
        self.store.set_device_assignees("ispA", self.cpe, [uid], "owner")
        self._see_node("ispA", "probe1", NEW)

        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.hub = self.server.liveping
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def _see_node(self, org, node, version):
        self.store.record_heartbeat(org, node, {"version": version})

    def _req(self, method, path, body=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        if cookie:
            hdrs["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _edge(self, path, env):
        return self._req("POST", path, env,
                         headers={"Authorization": "Bearer edge-secret"})

    def _start(self, device_id, cookie=None):
        return self._req("POST", "/api/liveping/start", {"device_id": device_id},
                         cookie=cookie or self._login())


class FsmIsolationTest(_Base):
    """A live-ping session may not move device state or emit an event.

    The failure this guards against: `/report` routes `mode="recheck"` into
    `central_engine.run_cycle`. If live packets rode that path, an operator
    merely WATCHING a device would move its flap counters — and at a packet a
    second, with no hysteresis budgeted for it, could page a human at 3am
    about a device that is fine.
    """

    def _snapshot(self):
        return (self.store.device_states("ispA"),
                self.store.list_events("ispA", limit=500),
                list(self.notifier.sent))

    def test_a_whole_live_session_never_moves_device_state(self):
        before_states, before_events, before_sent = self._snapshot()
        cookie = self._login()
        status, body = self._start(self.cpe, cookie)
        self.assertEqual(status, 200)
        sid = body["session"]["sid"]

        # A full session's worth of packets, and a run of total loss in the
        # middle — exactly the shape that would trip the FSM's DOWN
        # hysteresis if these samples reached it.
        samples = []
        for seq in range(1, 121):
            samples.append([seq, None if 20 <= seq <= 90 else 3.2])
        status, reply = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: samples}})
        self.assertEqual(status, 200)

        after_states, after_events, after_sent = self._snapshot()
        self.assertEqual(after_states, before_states,
                         "a live ping moved a device's state")
        self.assertEqual(len(after_events), len(before_events),
                         "a live ping emitted an event")
        self.assertEqual(after_sent, before_sent,
                         "a live ping sent a notification")

    def test_seventy_lost_packets_open_no_outage(self):
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[s, None] for s in range(1, 71)]}})
        status, body = self._req("GET", "/api/outages?org=ispA", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("outages", []), [])
        self.assertEqual(self.notifier.sent, [])

    def test_the_samples_are_never_persisted(self):
        """Ephemeral by construction: the hub holds no database at all."""
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[1, 4.0], [2, 5.0]]}})
        import sqlite3
        conn = sqlite3.connect(self.cfg.central_db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual([t for t in tables if "liveping" in t or "live_ping" in t],
                         [], "live ping is not history and stores nothing")

    def test_the_report_route_still_refuses_to_carry_live_ping(self):
        """No mode on `/report` may start or feed a live session."""
        status, body = self._edge("/report", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "mode": "liveping",
            "pings": {}})
        self.assertEqual(status, 200)
        self.assertNotIn("session", body)
        self.assertEqual(self.hub.org_live_count("ispA"), 0)


class WakeUpTest(_Base):

    def test_the_report_reply_wakes_the_probe_only_when_there_is_work(self):
        env = {"v": 1, "org_id": "ispA", "node_id": "probe1", "pings": {}}
        self.assertNotIn("liveping", self._edge("/report", env)[1])
        self._start(self.cpe)
        self.assertTrue(self._edge("/report", env)[1].get("liveping"))
        self.assertNotIn(
            "liveping",
            self._edge("/report", {**env, "node_id": "probe2"})[1],
            "another probe's report must not be woken")

    def test_the_start_reply_names_the_wait_instead_of_spinning(self):
        self.store.set_org_poll_interval("ispA", 45)
        status, body = self._start(self.cpe)
        self.assertEqual(status, 200)
        self.assertEqual(body["wait_hint_s"], 45)
        self.assertFalse(body["session"]["picked_up"],
                         "not picked up until the probe actually asks")

    def test_picked_up_flips_when_the_probe_asks_for_work(self):
        sid = self._start(self.cpe)[1]["session"]["sid"]
        cookie = self._login()
        self._edge("/edge/liveping", {"v": 1, "org_id": "ispA",
                                      "node_id": "probe1", "token": 0})
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertTrue(body["session"]["picked_up"])
        self.assertEqual(body["session"]["sid"], sid)


class EdgeChannelTest(_Base):

    def test_the_probe_is_handed_the_device_row_not_a_typed_target(self):
        self._start(self.cpe)
        status, body = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sessions"]), 1)
        self.assertEqual(body["sessions"][0]["device_ip"], "10.0.0.3")
        self.assertEqual(body["sessions"][0]["device_id"], self.cpe)

    def test_an_unauthenticated_probe_is_refused(self):
        status, _ = self._req("POST", "/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0})
        self.assertEqual(status, 401)

    def test_another_orgs_probe_gets_nothing(self):
        self._start(self.cpe)
        status, body = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispB", "node_id": "probe1", "token": 0})
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"], [])

    def test_a_stop_reaches_the_probe_as_an_empty_set(self):
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        token = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0})[1]["token"]
        self._req("POST", "/api/liveping/stop", {"sid": sid}, cookie=cookie)
        body = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": token})[1]
        self.assertEqual(body["sessions"], [])

    def test_a_probe_refusal_becomes_the_panels_reason(self):
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "refusals": [{"sid": sid,
                          "error": "target is not a device this node probes"}]})
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertEqual(body["session"]["stop_reason"], "refused")
        self.assertIn("not a device", body["session"]["stop_detail"])
        self.assertFalse(body["session"]["live"])


class BoundsTest(_Base):

    def test_the_cadence_is_slower_for_a_device_with_children(self):
        # The OLT is the AP's parent, so it is aggregation gear.
        olt = self._start(self.olt)[1]["session"]
        self.assertTrue(olt["infra"])
        self.assertEqual(olt["interval_ms"], 2000)
        cpe = self._start(self.cpe)[1]["session"]
        self.assertFalse(cpe["infra"])
        self.assertEqual(cpe["interval_ms"], 1000)

    def test_the_session_carries_its_own_five_minute_deadline(self):
        body = self._start(self.cpe)[1]
        self.assertEqual(body["max_s"], 300)
        sess = body["session"]
        self.assertAlmostEqual(sess["expires_at"] - sess["started_at"], 300.0,
                               places=1)
        edge = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1",
            "token": 0})[1]["sessions"][0]
        self.assertLessEqual(edge["remaining_s"], 300.0)
        self.assertGreater(edge["remaining_s"], 290.0)

    def test_a_second_click_on_one_device_joins_the_running_session(self):
        first = self._start(self.cpe)[1]["session"]["sid"]
        second = self._start(self.cpe)[1]["session"]["sid"]
        self.assertEqual(first, second)
        self.assertEqual(self.hub.org_live_count("ispA"), 1)

    def test_two_operators_watch_the_same_stream(self):
        cookie_a = self._login()
        cookie_b = self._login("ravi", "ravipassword")
        sid = self._start(self.cpe, cookie_a)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[1, 4.0], [2, None]]}})
        for cookie in (cookie_a, cookie_b):
            body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                             cookie=cookie)[1]
            self.assertEqual(body["session"]["sid"], sid)
            self.assertEqual(body["samples"], [[1, 4.0], [2, None]])

    def test_the_per_org_cap_refuses_the_next_device(self):
        self.hub.max_per_org = 2
        self.assertEqual(self._start(self.olt)[0], 200)
        self.assertEqual(self._start(self.cpe)[0], 200)
        status, body = self._start(self.spare)
        self.assertEqual(status, 429)
        self.assertIn("2", body["error"])
        # ...and it is the ORG's ceiling, not the platform's: a full ispA does
        # not stop ispB starting one.
        self._see_node("ispB", "probeB", NEW)
        bstatus, bbody = self._req("POST", "/api/liveping/start",
                                   {"device_id": self.foreign},
                                   cookie=self._login("bowner", "bownerpassword"))
        self.assertEqual(bstatus, 200, bbody)

    def test_the_cursor_only_returns_what_is_new(self):
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[1, 4.0], [2, 5.0], [3, None]]}})
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}&after=2",
                         cookie=cookie)[1]
        self.assertEqual(body["samples"], [[3, None]])
        self.assertEqual(body["cursor"], 3)

    def test_the_panel_can_tell_a_quiet_probe_from_a_dark_device(self):
        """A dark device still produces a timeout LINE every second.

        So silence on the channel is the PROBE, not the device, and the two
        must not render alike. `silent_s` is the fact that lets the panel say
        which one it is looking at.
        """
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[1, None], [2, None], [3, None]]}})
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertLess(body["session"]["silent_s"], 2.0,
                        "packets ARE arriving; they just say timeout")
        # Now wind the arrival stamp back: the probe went quiet.
        self.hub.get(sid).last_sample_at = time.time() - 30
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertGreater(body["session"]["silent_s"], 25.0)

    def test_a_lost_run_keeps_its_sequence_numbers(self):
        """Three in a row and three scattered must not render alike."""
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "samples": {sid: [[1, 4.0], [2, None], [3, None], [4, None],
                              [5, 4.1]]}})
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertEqual([s[0] for s in body["samples"]], [1, 2, 3, 4, 5])
        self.assertEqual(body["session"]["lost"], 3)
        self.assertEqual(body["session"]["received"], 2)


class ScopeTest(_Base):

    def test_a_typed_ip_is_not_a_target(self):
        """There is no route that accepts an address, only a device row.

        The remote diag walk set the precedent by refusing target IPs outside
        the node's device list; the reason is sharper here, because an
        accepted IP would make the dashboard a packet source anyone with a
        login could aim anywhere.
        """
        cookie = self._login()
        for body in ({"ip": "8.8.8.8"}, {"device_ip": "8.8.8.8"},
                     {"device_id": "8.8.8.8"}, {"target": "8.8.8.8"}):
            status, reply = self._req("POST", "/api/liveping/start", body,
                                      cookie=cookie)
            self.assertEqual(status, 400, f"{body} was not refused")
        self.assertEqual(self.hub.org_live_count("ispA"), 0)

    def test_another_orgs_device_is_refused(self):
        status, _ = self._req("POST", "/api/liveping/start",
                              {"device_id": self.foreign}, cookie=self._login())
        self.assertEqual(status, 403)
        status, _ = self._req("GET", f"/api/liveping?device_id={self.foreign}",
                              cookie=self._login())
        self.assertEqual(status, 403)

    def test_a_worker_may_live_ping_an_assigned_device(self):
        cookie = self._login("ravi", "ravipassword")
        status, body = self._req("POST", "/api/liveping/start",
                                 {"device_id": self.cpe}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._req("GET", f"/api/liveping?device_id={self.cpe}",
                                   cookie=cookie)[0], 200)
        self.assertEqual(
            self._req("POST", "/api/liveping/stop",
                      {"sid": body["session"]["sid"]}, cookie=cookie)[0], 200)

    def test_a_worker_cannot_reach_a_device_outside_their_scope(self):
        """The DATA layer, under the route layer. Both apply."""
        cookie = self._login("ravi", "ravipassword")
        # Ravi is assigned the AP only; the spare switch is not below it.
        self.assertEqual(
            self._req("POST", "/api/liveping/start", {"device_id": self.spare},
                      cookie=cookie)[0], 403)
        self.assertEqual(
            self._req("GET", f"/api/liveping?device_id={self.spare}",
                      cookie=cookie)[0], 403)

    def test_a_worker_with_nothing_assigned_reaches_nothing(self):
        cookie = self._login("sunil", "sunilpassword")
        for device in (self.olt, self.cpe, self.spare):
            self.assertEqual(
                self._req("POST", "/api/liveping/start", {"device_id": device},
                          cookie=cookie)[0], 403)

    def test_a_worker_cannot_stop_another_orgs_session(self):
        bsid = None
        self._see_node("ispB", "probeB", NEW)
        status, body = self._req("POST", "/api/liveping/start",
                                 {"device_id": self.foreign},
                                 cookie=self._login("bowner", "bownerpassword"))
        self.assertEqual(status, 200, body)
        bsid = body["session"]["sid"]
        status, _ = self._req("POST", "/api/liveping/stop", {"sid": bsid},
                              cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)
        self.assertTrue(self.hub.get(bsid).live(time.time()))

    def test_a_superadmin_may_watch_any_org(self):
        status, _ = self._req("POST", "/api/liveping/start",
                              {"device_id": self.cpe},
                              cookie=self._login("root", "rootpassword"))
        self.assertEqual(status, 200)

    def test_an_anonymous_caller_gets_nothing(self):
        self.assertEqual(self._req("GET", f"/api/liveping?device_id={self.cpe}")[0],
                         401)
        self.assertEqual(self._req("POST", "/api/liveping/start",
                                   {"device_id": self.cpe})[0], 401)


class VersionGateTest(_Base):

    def test_an_old_probe_is_named_never_left_spinning(self):
        self._see_node("ispA", "probe1", OLD)
        status, body = self._start(self.cpe)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], f"probe needs v{MIN_EDGE_VERSION}")
        self.assertEqual(body["node_version"], OLD)
        self.assertEqual(body["needs_version"], MIN_EDGE_VERSION)
        self.assertEqual(self.hub.org_live_count("ispA"), 0)

    def test_the_status_read_answers_before_the_button_is_pressed(self):
        cookie = self._login()
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertTrue(body["supported"])
        self._see_node("ispA", "probe1", OLD)
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertFalse(body["supported"])
        self.assertEqual(body["needs_version"], MIN_EDGE_VERSION)

    def test_a_probe_that_has_never_checked_in_is_not_supported(self):
        dev = self.store.create_org_device("ispA", {
            "name": "NEW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "probe-never"})
        body = self._req("GET", f"/api/liveping?device_id={dev}",
                         cookie=self._login())[1]
        self.assertFalse(body["supported"])
        self.assertEqual(self._start(dev)[0], 409)

    def test_a_device_with_no_probe_says_so(self):
        dev = self.store.create_org_device("ispA", {
            "name": "ORPHAN", "ip_address": "10.0.0.8", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": None})
        status, body = self._start(dev)
        self.assertEqual(status, 400)
        self.assertIn("probe", body["error"])


class KillSwitchTest(_Base):

    def test_the_server_switch_refuses_starts_and_stands_the_probe_down(self):
        self.server.RequestHandlerClass.cfg = Config(
            central_db=self.cfg.central_db, central_bind="127.0.0.1",
            central_port=0, central_token="edge-secret",
            liveping_enabled=False)
        self.addCleanup(setattr, self.server.RequestHandlerClass, "cfg", self.cfg)
        status, body = self._start(self.cpe)
        self.assertEqual(status, 404)
        self.assertIn("disabled", body["error"])
        reply = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0})[1]
        self.assertTrue(reply["disabled"])
        self.assertEqual(reply["sessions"], [])


class EndToEndWireTest(_Base):
    """The REAL edge tunnel against the REAL server, over HTTP.

    Every other test here talks to one half with a hand-written body. That
    proves each side works and proves nothing about whether they agree — and a
    wire mismatch between two halves that each pass their own doubles is
    exactly the class of bug this codebase keeps paying for (the pysnmp
    multi-varbind call, the port-walk deploy order). So this one runs
    `HttpCentralClient.liveping_exchange` into `api.liveping.edge_exchange`
    and back, with only the ICMP socket stubbed out.
    """

    def _edge_cfg(self):
        return Config(central_url=f"http://127.0.0.1:{self.port}",
                      central_token="edge-secret", org_id="ispA",
                      node_id="probe1", liveping_poll_hold_s=0.5,
                      liveping_max_s=300)

    def test_a_session_started_in_the_dashboard_reaches_the_probe_and_back(self):
        import asyncio
        from wisp.ingress.liveping import LivePingTunnel
        from wisp.runtime.central_client import build_central_client

        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]

        class StubProber:
            async def ping(self, ip, count):
                raise AssertionError("live ping must not use the cycle's ping()")

            async def ping_stream(self, ip, *, count, interval):
                for seq in range(1, min(count, 5) + 1):
                    yield seq, None if seq == 3 else 6.5

        cfg = self._edge_cfg()
        client = build_central_client(cfg)
        self.addCleanup(client.close)

        async def run():
            tunnel = LivePingTunnel(client, cfg, prober=StubProber(),
                                    devices_provider=lambda: [
                                        {"ip_address": "10.0.0.3"}])
            # Exactly what the report reply carries.
            tunnel.notify(True)
            for _ in range(80):
                await asyncio.sleep(0.05)
                if self.hub.get(sid) and self.hub.get(sid).sent >= 5:
                    break
            await tunnel.aclose()

        asyncio.run(run())

        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertTrue(body["session"]["picked_up"])
        self.assertEqual(body["samples"],
                         [[1, 6.5], [2, 6.5], [3, None], [4, 6.5], [5, 6.5]])
        self.assertEqual(body["session"]["lost"], 1)
        self.assertEqual(body["session"]["received"], 4)

    def test_the_probe_refuses_over_the_wire_and_the_panel_reads_it(self):
        import asyncio
        from wisp.ingress.liveping import LivePingTunnel
        from wisp.runtime.central_client import build_central_client

        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]

        class NeverCalled:
            async def ping_stream(self, ip, *, count, interval):
                raise AssertionError("a refused target must send no packets")
                yield  # pragma: no cover

        cfg = self._edge_cfg()
        client = build_central_client(cfg)
        self.addCleanup(client.close)

        async def run():
            # This node probes something else entirely.
            tunnel = LivePingTunnel(client, cfg, prober=NeverCalled(),
                                    devices_provider=lambda: [
                                        {"ip_address": "192.168.9.9"}])
            tunnel.notify(True)
            for _ in range(80):
                await asyncio.sleep(0.05)
                sess = self.hub.get(sid)
                if sess is None or sess.stop_reason:
                    break
            await tunnel.aclose()

        asyncio.run(run())
        body = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]
        self.assertEqual(body["session"]["stop_reason"], "refused")
        self.assertIn("not a device", body["session"]["stop_detail"])


if __name__ == "__main__":
    unittest.main()


class RequestedHoldTest(_Base):
    """The edge's requested hold has to CROSS THE WIRE, not just set a timeout.

    This is the bug the doubles could not see. `liveping_exchange` took a
    `hold_s`, used it for the HTTP timeout (`hold_s + 10`) and left it out of
    the envelope; central then always parked for its OWN configured hold. In
    production that is a 2 s request against a 20 s park, so EVERY exchange
    times out on the client's deadline: the stream stops being one line per
    second and arrives in ~14 s bursts, and the panel's own silence alarm
    fires on a healthy channel. The unit fake returned immediately and the
    other tests here run a 0.2 s ceiling, so nothing caught it.

    The ceiling still belongs to central — the edge may ask for less, never
    for more.
    """

    # Small on purpose: the signal is park-vs-no-park (~0.5 s against ~5 ms),
    # so a big ceiling would only buy slower tests. `tests/speedups.py` exists
    # because this suite guards its own runtime.
    HOLD = 0.5

    def setUp(self):
        super().setUp()
        # Rebuild on a ceiling long enough that failing to honour the request
        # is measurable, but short enough to keep the suite fast.
        self.server.shutdown()
        self.cfg = Config(central_db=self.cfg.central_db,
                          central_bind="127.0.0.1", central_port=0,
                          central_token="edge-secret",
                          liveping_poll_hold_s=self.HOLD)
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.hub = self.server.liveping
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def _park(self, env_extra):
        """Time one steady-state exchange (nothing to deliver, so it parks)."""
        prime = {"v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
                 "hold_s": 0.0}
        env = {"v": 1, "org_id": "ispA", "node_id": "probe1",
               "token": self._edge("/edge/liveping", prime)[1]["token"]}
        env.update(env_extra)
        t0 = time.monotonic()
        status, _ = self._edge("/edge/liveping", env)
        self.assertEqual(status, 200)
        return time.monotonic() - t0

    def test_a_short_requested_hold_answers_at_once(self):
        self.assertLess(self._park({"hold_s": 0.0}), self.HOLD / 2)

    def test_a_probe_that_sends_no_hold_still_gets_the_configured_park(self):
        # An older probe predates the key. It must keep exactly the behaviour
        # it had, not fall through to a busy-loop of zero-length polls.
        self.assertGreaterEqual(self._park({}), self.HOLD * 0.9)

    def test_the_edge_may_ask_for_less_but_never_for_more(self):
        self.assertGreaterEqual(self._park({"hold_s": 600.0}), self.HOLD * 0.9)

    def test_junk_falls_back_to_the_configured_park(self):
        for junk in ("banana", None, -5.0, float("nan")):
            with self.subTest(junk=junk):
                self.assertGreaterEqual(self._park({"hold_s": junk}),
                                        self.HOLD * 0.9)

    def test_the_client_puts_the_hold_on_the_wire(self):
        """Pins the envelope itself: the wire carries what the caller asked."""
        from wisp.runtime.central_client import HttpCentralClient

        seen = {}

        class _Probe(HttpCentralClient):
            def _post(self, path, env, timeout=None):
                seen.update(env)
                return {"token": 1, "sessions": []}

        cfg = Config(central_url=f"http://127.0.0.1:{self.port}",
                     org_id="ispA", node_id="probe1")
        _Probe(cfg).liveping_exchange(7, {}, [], 2.0)
        self.assertEqual(seen["hold_s"], 2.0)


class InfraCadenceEdgeCaseTest(_Base):
    """Aggregation gear must keep the SLOW rung even when its children are not
    currently monitored.

    `org_device_topology` filters out maintenance and inactive rows, so asking
    it "does anything hang off this box" answers about what is being probed,
    not about the topology. An OLT whose ONUs are all in maintenance came back
    as a LEAF and got the 1/s cadence — which is the one thing the design says
    must not happen to aggregation gear: its ICMP rate limiter answers a fast
    stream with phantom loss, and the real sweep then reports an outage on the
    box the technician is standing next to.
    """

    def test_a_parent_whose_children_are_all_in_maintenance_stays_infra(self):
        self.store.set_org_device_maintenance("ispA", self.cpe, True)
        cookie = self._login()
        sess = self._start(self.olt, cookie)[1]["session"]
        self.assertTrue(sess["infra"])
        self.assertEqual(sess["interval_ms"], self.cfg.liveping_infra_interval_ms)

    def test_a_genuine_leaf_still_gets_the_fast_rung(self):
        cookie = self._login()
        sess = self._start(self.spare, cookie)[1]["session"]
        self.assertFalse(sess["infra"])
        self.assertEqual(sess["interval_ms"], self.cfg.liveping_interval_ms)


class RefusalScopeTest(_Base):
    """One NODE may not stop another node's session.

    A refusal writes probe-supplied text into `stop_detail`, which the panel
    renders as the reason the stream ended. `ingest` already checks the node;
    `stop` checked only the org, so a node holding org credentials could kill a
    sibling node's session and explain it in its own words. The operator's own
    stop stays org-scoped — a person may stop any session on their devices,
    whichever probe happens to run it.
    """

    def test_a_node_cannot_refuse_another_nodes_session(self):
        other = self.store.create_org_device("ispA", {
            "name": "FAR-SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe2"})
        self._see_node("ispA", "probe2", NEW)
        cookie = self._login()
        sid = self._start(other, cookie)[1]["session"]["sid"]

        status, _ = self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "refusals": [{"sid": sid, "error": "not my device"}]})
        self.assertEqual(status, 200)

        sess = self._req("GET", f"/api/liveping?device_id={other}",
                         cookie=cookie)[1]["session"]
        self.assertIsNone(sess.get("stop_reason"))
        self.assertNotEqual(sess.get("stop_detail"), "not my device")

    def test_the_owning_node_may_still_refuse(self):
        cookie = self._login()
        sid = self._start(self.cpe, cookie)[1]["session"]["sid"]
        self._edge("/edge/liveping", {
            "v": 1, "org_id": "ispA", "node_id": "probe1", "token": 0,
            "refusals": [{"sid": sid, "error": "not in my device list"}]})
        sess = self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]["session"]
        self.assertEqual(sess["stop_detail"], "not in my device list")


class WaitHintTest(_Base):
    """The "checks in about every N s" number has to be the REAL cadence.

    It fell back to `poll_interval_s`, the raw env value — but a small fleet
    runs the ADAPTIVE cadence (`Config.effective_interval`). So the panel told
    a technician on a small fleet to expect a wait several times longer than
    the real one, and drove its own "the probe has not picked this up"
    threshold with the same wrong number. Central cannot see the edge's CLI
    override, which outranks both, so this stays a hint and the panel says
    "about".
    """

    def _hint(self, cfg):
        self.server.shutdown()
        self.server = make_server(cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(t.join, 2)
        self.addCleanup(self.server.shutdown)
        cookie = self._login()
        return self._req("GET", f"/api/liveping?device_id={self.cpe}",
                         cookie=cookie)[1]["wait_hint_s"]

    def test_a_small_fleet_gets_its_adaptive_cadence_not_the_raw_env_value(self):
        hint = self._hint(Config(
            central_db=self.cfg.central_db, central_bind="127.0.0.1",
            central_port=0, central_token="edge-secret",
            poll_interval_s=120, poll_interval_adaptive=True,
            small_fleet_max=50, poll_interval_small_s=20))
        self.assertEqual(hint, 20)

    def test_the_org_override_still_outranks_the_adaptive_value(self):
        self.store.set_org_poll_interval("ispA", 45)
        hint = self._hint(Config(
            central_db=self.cfg.central_db, central_bind="127.0.0.1",
            central_port=0, central_token="edge-secret",
            poll_interval_s=120, poll_interval_adaptive=True,
            small_fleet_max=50, poll_interval_small_s=20))
        self.assertEqual(hint, 45)
