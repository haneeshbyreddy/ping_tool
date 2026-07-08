import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import engine as central_engine
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.engine import EngineRegistry
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.core.state_machine import (
    DOWN,
    UNREACHABLE,
    OutageOpened,
    OutageResolved,
    UplinkDown,
    UplinkRestored,
)
from wisp.ingress.probers import PingResult
from support import RecordingNotifier

T0 = "2026-01-01T00:00:00+00:00"
T_LATER = "2026-01-01T01:30:00+00:00"

def _up(loss=0.0, latency=10.0, jitter=1.0):
    return PingResult("10.0.0.1", latency, loss, jitter)

def _down():
    return PingResult("10.0.0.1", None, 100.0)

class CentralEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          down_consecutive=3, recover_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_device_meta_excludes_maintenance(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": "north", "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.set_org_device_maintenance("ispA", b, True)
        meta = central_engine.load_device_meta(self.store, "ispA")
        self.assertEqual([d.id for d in meta], [a])

    def test_build_engine_rehydrates_down_state_without_repaging(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.write_device_states("ispA", [(dev, DOWN, None, 100.0, None)], T0)
        engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        cycle = engine.process_cycle({"10.0.0.1": _down()}, T0)
        self.assertEqual(cycle.states[dev], DOWN)
        self.assertEqual(cycle.events, [])

    def test_run_cycle_persists_outage_and_device_state(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        for _ in range(3):
            central_engine.run_cycle(self.store, "ispA", engine, {"10.0.0.1": _down()}, T0)
        self.assertEqual(self.store.device_states("ispA")[dev]["state"], DOWN)
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))

    def test_registry_persists_streaks_across_calls(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        registry = EngineRegistry(self.store, self.cfg)
        for i in range(2):
            eng = registry.get("ispA")
            central_engine.run_cycle(self.store, "ispA", eng, {"10.0.0.1": _down()}, T0)
        self.assertEqual(self.store.device_states("ispA")[dev]["state"], "UP")
        eng = registry.get("ispA")
        central_engine.run_cycle(self.store, "ispA", eng, {"10.0.0.1": _down()}, T0)
        self.assertEqual(self.store.device_states("ispA")[dev]["state"], DOWN)

    def test_registry_rebuilds_on_topology_change(self):
        registry = EngineRegistry(self.store, self.cfg)
        registry.get("ispA")
        new_dev = self.store.create_org_device("ispA", {
            "name": "New", "ip_address": "10.0.0.9", "device_type": None,
            "region": None, "parent_device_id": None})
        eng = registry.get("ispA")
        self.assertIn(new_dev, eng.meta)

    def test_orgs_have_independent_engines(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        registry = EngineRegistry(self.store, self.cfg)
        for _ in range(3):
            central_engine.run_cycle(self.store, "ispA", registry.get("ispA"),
                                     {"10.0.0.1": _down()}, T0)
        self.assertNotIn(a, self.store.device_states("ispB"))
        self.assertEqual(self.store.device_states("ispA")[a]["state"], DOWN)

    def test_compute_recheck_flags_down_and_up_suspects(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        cycle = engine.process_cycle({"10.0.0.1": _down()}, T0)
        recheck = central_engine.compute_recheck(engine, cycle, {"10.0.0.1": _down()}, self.cfg)
        self.assertEqual(recheck["down_ips"], ["10.0.0.1"])
        self.assertEqual(recheck["up_ips"], [])
        self.assertEqual(recheck["interval_s"], self.cfg.retry_interval_s)

    def test_compute_recheck_empty_when_nothing_suspect(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        cycle = engine.process_cycle({"10.0.0.1": _up()}, T0)
        recheck = central_engine.compute_recheck(engine, cycle, {"10.0.0.1": _up()}, self.cfg)
        self.assertEqual(recheck, {})

    def test_compute_recheck_empty_when_canary_frozen(self):
        cfg = Config(central_db=self.cfg.central_db, canary_ip="9.9.9.9", canary_freeze=True,
                    down_consecutive=3)
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispA", cfg)
        results = {"10.0.0.1": _down(), "9.9.9.9": _down()}
        cycle = engine.process_cycle(results, T0)
        self.assertTrue(cycle.canary_down)
        recheck = central_engine.compute_recheck(engine, cycle, results, cfg)
        self.assertEqual(recheck, {})

    def test_run_cycle_subset_advances_only_named_device(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        central_engine.run_cycle(self.store, "ispA", engine,
                                 {"10.0.0.1": _down(), "10.0.0.2": _down()}, T0)
        central_engine.run_cycle(self.store, "ispA", engine, {"10.0.0.1": _down()}, T0,
                                 subset={a})
        central_engine.run_cycle(self.store, "ispA", engine, {"10.0.0.1": _down()}, T0,
                                 subset={a})
        states = self.store.device_states("ispA")
        self.assertEqual(states[a]["state"], DOWN)
        self.assertEqual(states[b]["state"], "UP")

class CentralAlertDispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          escalate_every_min=60)
        self.store = CentralStore(self.cfg.central_db)
        self.dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.0.1", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner",
                           ntfy_topic_operator="a-op", ntfy_topic_tech="a-tech")
        self.engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        self.notifier = RecordingNotifier()
        self.disp = CentralAlertDispatcher(self.store, "ispA", self.engine,
                                           self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _open_outage(self, state=DOWN):
        self.store.open_outage_if_absent("ispA", self.dev, T0, state)
        return self.store.open_outage_id("ispA", self.dev)

    def test_fresh_down_pages_owner_and_operator_and_schedules_hourly(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertEqual({s["recipient"] for s in self.notifier.sent}, {"a-owner", "a-op"})
        with self.store._connect() as conn:
            esc = conn.execute("SELECT kind FROM escalations").fetchall()
        self.assertEqual([r["kind"] for r in esc], ["hourly"])

    def test_unreachable_is_suppressed(self):
        self._open_outage(state=UNREACHABLE)
        self.disp.dispatch([OutageOpened(self.dev, UNREACHABLE)], T0)
        self.assertEqual(self.notifier.sent, [])

    def test_anti_spam_dedupe_per_outage(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertEqual(len(self.notifier.sent), 2)

    def test_new_outage_after_recovery_pages_again(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.store.resolve_outage("ispA", self.dev, T0)
        self.disp.dispatch([OutageResolved(self.dev)], T0)
        self.notifier.sent.clear()
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertEqual(len(self.notifier.sent), 2)

    def test_resolved_broadcasts_to_all_three(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.notifier.sent.clear()
        self.disp.dispatch([OutageResolved(self.dev)], T0)
        self.assertEqual({s["recipient"] for s in self.notifier.sent},
                         {"a-owner", "a-op", "a-tech"})

    def test_resolved_from_unreachable_is_silent(self):
        self._open_outage(state=UNREACHABLE)
        self.disp.dispatch([OutageOpened(self.dev, UNREACHABLE)], T0)
        self.store.resolve_outage("ispA", self.dev, T0)
        self.notifier.sent.clear()
        self.disp.dispatch([OutageResolved(self.dev)], T0)
        self.assertEqual(self.notifier.sent, [])

    def test_hourly_escalation_fans_out_and_reschedules(self):
        self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.notifier.sent.clear()
        self.disp.sweep(T_LATER)
        self.assertEqual({s["recipient"] for s in self.notifier.sent},
                         {"a-owner", "a-op", "a-tech"})
        self.assertTrue(any("1h" in s["title"] for s in self.notifier.sent))
        pending = self.store.due_escalations("ispA", "2026-01-01T09:00:00+00:00")
        self.assertEqual(len(pending), 1)

    def test_ack_does_not_stop_escalation_but_recovery_does(self):
        oid = self._open_outage()
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertTrue(self.disp.acknowledge(oid, "Suresh"))
        self.notifier.sent.clear()
        self.disp.sweep(T_LATER)
        self.assertTrue(any("Suresh" in s["body"] for s in self.notifier.sent))

        self.store.resolve_outage("ispA", self.dev, T_LATER)
        self.disp.dispatch([OutageResolved(self.dev)], T_LATER)
        self.notifier.sent.clear()
        self.disp.sweep("2026-01-01T03:00:00+00:00")
        self.assertEqual(self.notifier.sent, [])

    def test_missing_topic_is_a_soft_noop(self):
        self.store.set_org("ispB")
        dev = self.store.create_org_device("ispB", {
            "name": "X", "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        engine = central_engine.build_engine(self.store, "ispB", self.cfg)
        disp = CentralAlertDispatcher(self.store, "ispB", engine, self.notifier, self.cfg)
        self.store.open_outage_if_absent("ispB", dev, T0, DOWN)
        disp.dispatch([OutageOpened(dev, DOWN)], T0)
        self.assertEqual(self.notifier.sent, [])
        with self.store._connect() as conn:
            row = conn.execute("SELECT status FROM alert_log ORDER BY id DESC LIMIT 1"
                               ).fetchone()
        self.assertEqual(row["status"], "failed")

    def test_uplink_down_and_restored_pages_owner(self):
        self.disp.dispatch([UplinkDown()], T0)
        self.assertEqual({s["recipient"] for s in self.notifier.sent}, {"a-owner", "a-op"})
        with self.store._connect() as conn:
            row = conn.execute("SELECT payload, recipient FROM alert_log"
                               " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["payload"], "UPLINK_DOWN")
        self.assertEqual(row["recipient"], "a-owner")

        self.notifier.sent.clear()
        self.disp.dispatch([UplinkRestored()], T0)
        self.assertEqual({s["recipient"] for s in self.notifier.sent}, {"a-owner", "a-op"})
        with self.store._connect() as conn:
            row = conn.execute("SELECT payload FROM alert_log ORDER BY id DESC LIMIT 1"
                               ).fetchone()
        self.assertEqual(row["payload"], "UPLINK_RESTORED")

class ReportEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0, central_token="tok",
                          down_consecutive=3, recover_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token="tok"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _report(self, loss):
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.1": {"loss_pct": loss,
                          "latency_ms": None if loss else 5.0}}}
        return self._req("POST", "/report", body)

    def test_edge_devices_requires_bearer(self):
        status, _ = self._req("GET", "/edge/devices?org_id=ispA", token=None)
        self.assertEqual(status, 401)

    def test_edge_devices_returns_topology_and_canary(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": "core",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, body = self._req("GET", "/edge/devices?org_id=ispA")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"][0]["ip_address"], "10.0.0.1")
        self.assertIn("canary_ip", body)

    def test_edge_devices_filters_by_assigned_node(self):
        did_a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-2"})
        self.store.create_org_device("ispA", {
            "name": "C-unassigned", "ip_address": "10.0.0.3", "device_type": None,
            "region": None, "parent_device_id": None})

        status, body = self._req("GET", "/edge/devices?org_id=ispA&node_id=edge-1")
        self.assertEqual(status, 200)
        ips = {d["ip_address"] for d in body["devices"]}
        self.assertEqual(ips, {"10.0.0.1"})

        status, body = self._req("GET", "/edge/devices?org_id=ispA")
        self.assertEqual({d["ip_address"] for d in body["devices"]},
                        {"10.0.0.1", "10.0.0.2"})

    def test_report_from_unassigned_node_never_scores_other_nodes_device_down(self):
        self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-2"})

        for _ in range(5):
            status, _ = self._req("POST", "/report", {
                "v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.1": {"loss_pct": 0, "latency_ms": 5.0}}})
            self.assertEqual(status, 200)

        states = self.store.device_states("ispA")
        b_id = next(d["id"] for d in self.store.list_org_devices("ispA")
                   if d["ip_address"] == "10.0.0.2")
        self.assertNotIn(b_id, states)

        status, _ = self._req("POST", "/report", {
            "v": 1, "org_id": "ispA", "node_id": "edge-2",
            "pings": {"10.0.0.2": {"loss_pct": 0, "latency_ms": 5.0}}})
        self.assertEqual(status, 200)
        states = self.store.device_states("ispA")
        self.assertEqual(states[b_id]["state"], "UP")

    def test_unassigned_device_is_never_probed_or_scored(self):
        self.store.set_org("ispA", ntfy_topic_owner="a-owner", ntfy_topic_operator="a-op")
        dev = self.store.create_org_device("ispA", {
            "name": "Orphan", "ip_address": "10.0.0.7", "device_type": None,
            "region": None, "parent_device_id": None})

        _, body = self._req("GET", "/edge/devices?org_id=ispA&node_id=edge-1")
        self.assertNotIn("10.0.0.7", {d["ip_address"] for d in body["devices"]})

        for _ in range(5):
            self._req("POST", "/report", {
                "v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.7": {"loss_pct": 100.0}}})
        self.assertNotIn(dev, self.store.device_states("ispA"))
        self.assertIsNone(self.store.open_outage_id("ispA", dev))
        self.assertEqual(self.notifier.sent, [])

    def test_unassigning_a_down_device_clears_state_and_resolves_outage(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        for _ in range(3):
            self._report(100.0)
        self.assertEqual(self.store.device_states("ispA")[dev]["state"], DOWN)
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))

        self.store.update_org_device("ispA", dev, {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": None})
        self.assertNotIn(dev, self.store.device_states("ispA"))
        self.assertIsNone(self.store.open_outage_id("ispA", dev))

    def test_report_requires_bearer(self):
        status, _ = self._req("POST", "/report", {"v": 1}, token=None)
        self.assertEqual(status, 401)

    def test_report_end_to_end_down_then_recovery(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": "north", "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner", ntfy_topic_operator="a-op")
        for _ in range(3):
            status, body = self._report(100.0)
            self.assertEqual(status, 200)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], DOWN)
        self.assertEqual({s["recipient"] for s in self.notifier.sent}, {"a-owner", "a-op"})

        self.notifier.sent.clear()
        for _ in range(2):
            self._report(0.0)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], "UP")
        self.assertTrue(any("Restored" in s["title"] for s in self.notifier.sent))

    def test_report_org_isolation(self):
        self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        for _ in range(3):
            body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                    "pings": {"10.0.0.1": {"loss_pct": 100.0}}}
            self._req("POST", "/report", body)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], DOWN)
        self.assertNotIn(1, self.store.device_states("ispB"))

    def _recheck(self, ip, loss):
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1", "mode": "recheck",
                "pings": {ip: {"loss_pct": loss, "latency_ms": None if loss else 5.0}}}
        return self._req("POST", "/report", body)

    def test_full_report_returns_recheck_hint_for_a_fresh_suspect(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, body = self._report(100.0)
        self.assertEqual(status, 200)
        self.assertIn("recheck", body)
        self.assertEqual(body["recheck"]["down_ips"], ["10.0.0.1"])

    def test_fast_confirm_pages_within_two_rechecks_not_three_full_polls(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": "north", "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner", ntfy_topic_operator="a-op")
        status, body = self._report(100.0)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], "UP")
        hint = body["recheck"]
        status, body = self._recheck("10.0.0.1", 100.0)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], "UP")
        self.assertIn("recheck", body)
        status, body = self._recheck("10.0.0.1", 100.0)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], DOWN)
        self.assertNotIn("recheck", body)
        self.assertEqual({s["recipient"] for s in self.notifier.sent}, {"a-owner", "a-op"})

    def test_recheck_blip_clears_hint_without_confirming(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self._report(100.0)
        status, body = self._recheck("10.0.0.1", 0.0)
        self.assertEqual(self.store.device_states("ispA")[1]["state"], "UP")
        self.assertNotIn("recheck", body)
        self.assertEqual(self.notifier.sent, [])

    def test_canary_down_freezes_and_pages_owner_uplink_down(self):
        self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner")
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.1": {"loss_pct": 100.0},
                         self.cfg.canary_ip: {"loss_pct": 100.0}}}
        status, resp = self._req("POST", "/report", body)
        self.assertEqual(status, 200)
        self.assertNotIn("recheck", resp)
        self.assertTrue(any(s["recipient"] == "a-owner" and "UPLINK" in s["title"]
                            for s in self.notifier.sent))
        self.assertEqual(self.store.device_states("ispA").get(1, {}).get("state"), "UP")

        self.notifier.sent.clear()
        body["pings"][self.cfg.canary_ip]["loss_pct"] = 0.0
        body["pings"][self.cfg.canary_ip]["latency_ms"] = 5.0
        status, resp = self._req("POST", "/report", body)
        self.assertTrue(any("restored" in s["title"].lower() for s in self.notifier.sent))

    def _report_with_ports(self, org, device_id, ports, ip="10.0.0.9"):
        body = {"v": 1, "org_id": org, "node_id": "edge-1",
                "pings": {ip: {"loss_pct": 0.0, "latency_ms": 5.0}},
                "ports": {str(device_id): ports}}
        return self._req("POST", "/report", body)

    def test_report_folds_monitored_port_down_into_open_outage(self):
        switch = self.store.create_org_device("ispA", {
            "name": "Core Switch", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        tower = self.store.create_org_device("ispA", {
            "name": "Rampur Tower", "ip_address": "10.0.0.1", "device_type": "backhaul",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner", ntfy_topic_operator="a-op")
        for _ in range(3):
            self._report(100.0)
        self.assertEqual(self.store.device_states("ispA")[tower]["state"], DOWN)

        port = {"if_index": 2, "if_name": "Gi0/2", "if_alias": "-> Rampur",
               "admin_status": "up", "oper_status": "down"}
        self._report_with_ports("ispA", switch, [port])
        pid = self.store.list_switch_ports("ispA", switch)[0]["id"]
        self.store.set_port_monitored("ispA", pid, True)
        self.store.set_port_feeds("ispA", pid, tower)
        self._report_with_ports("ispA", switch, [port])
        status, _ = self._report_with_ports("ispA", switch, [port])
        self.assertEqual(status, 200)

        oid = self.store.open_outage_id("ispA", tower)
        with self.store._connect() as conn:
            o = conn.execute("SELECT root_cause FROM outages WHERE id=?", (oid,)).fetchone()
        self.assertIn("Port", o["root_cause"])

    def test_report_health_upserts_and_rides_the_inventory_join(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core Router", "ip_address": "10.0.0.1", "device_type": "router",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}},
                "health": {str(dev): {"cpu_pct": 37.5, "mem_used_bytes": 96_000_000,
                                      "mem_total_bytes": 256_000_000, "mem_pct": 37.5,
                                      "temp_c": 54.0},
                           "9999": {"cpu_pct": 1.0},          # unknown device: ignored
                           "junk": {"cpu_pct": 1.0}}}         # bad id: ignored
        status, _ = self._req("POST", "/report", body)
        self.assertEqual(status, 200)
        row = {d["id"]: d for d in self.store.list_org_devices("ispA")}[dev]
        self.assertEqual(row["health_cpu_pct"], 37.5)
        self.assertEqual(row["health_mem_pct"], 37.5)
        self.assertEqual(row["health_mem_total_bytes"], 256_000_000)
        self.assertEqual(row["health_temp_c"], 54.0)
        self.assertIsNotNone(row["health_updated_at"])

        # a later sweep with fewer facts overwrites in place (latest reading only)
        body["health"] = {str(dev): {"cpu_pct": 90.0, "temp_c": "not-a-number"}}
        self._req("POST", "/report", body)
        row = {d["id"]: d for d in self.store.list_org_devices("ispA")}[dev]
        self.assertEqual(row["health_cpu_pct"], 90.0)
        self.assertIsNone(row["health_temp_c"])

    def test_recheck_report_never_writes_health(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1", "mode": "recheck",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}},
                "health": {str(dev): {"cpu_pct": 37.5}}}
        status, _ = self._req("POST", "/report", body)
        self.assertEqual(status, 200)
        row = {d["id"]: d for d in self.store.list_org_devices("ispA")}[dev]
        self.assertIsNone(row["health_cpu_pct"])

    def test_full_report_folds_a_trend_bucket_recheck_does_not(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        status, _ = self._report(0.0)
        self.assertEqual(status, 200)
        status, body = self._req("GET", f"/api/analytics/trend?device_id={dev}", token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["buckets"]), 1)
        self.assertEqual(body["buckets"][0]["samples"], 1)

        self._recheck("10.0.0.1", 0.0)
        status, body = self._req("GET", f"/api/analytics/trend?device_id={dev}", token="tok")
        self.assertEqual(body["buckets"][0]["samples"], 1)

    def test_perf_samples_returns_ring_buffer_with_timestamps(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self._report(0.0)
        self._report(0.0)
        status, body = self._req("GET", f"/api/inventory/perf/samples?device_id={dev}",
                                 token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["samples"]), 2)
        for s in body["samples"]:
            self.assertIn("ts", s)
            self.assertEqual(s["latency_ms"], 5.0)
            self.assertEqual(s["state"], "UP")

    def test_perf_samples_requires_auth(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        status, _ = self._req("GET", f"/api/inventory/perf/samples?device_id={dev}",
                              token=None)
        self.assertEqual(status, 401)

    def test_trend_requires_bearer_or_session(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Core", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        status, _ = self._req("GET", f"/api/analytics/trend?device_id={dev}", token=None)
        self.assertEqual(status, 401)

    def test_report_ports_ignores_a_device_id_from_another_org(self):
        self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        other_switch = self.store.create_org_device("ispB", {
            "name": "B Switch", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        port = {"if_index": 1, "if_name": "Gi0/1", "admin_status": "up", "oper_status": "up"}
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                "pings": {"10.0.0.1": {"loss_pct": 0.0, "latency_ms": 5.0}},
                "ports": {str(other_switch): [port]}}
        status, _ = self._req("POST", "/report", body)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.list_switch_ports("ispA", other_switch), [])
        self.assertEqual(self.store.list_switch_ports("ispB", other_switch), [])

    def test_report_drives_on_backup_badge_end_to_end(self):
        primary = self.store.create_org_device("ispA", {
            "name": "Primary", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        backup = self.store.create_org_device("ispA", {
            "name": "Backup", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        child = self.store.create_org_device("ispA", {
            "name": "Relay", "ip_address": "10.0.0.3", "device_type": None,
            "region": None, "parent_device_id": primary, "assigned_node_id": "edge-1"})
        self.store.create_backup_link("ispA", child, backup)
        self.store.set_org("ispA", ntfy_topic_operator="a-op")

        def _report_all(primary_loss):
            body = {"v": 1, "org_id": "ispA", "node_id": "edge-1",
                    "pings": {"10.0.0.1": {"loss_pct": primary_loss,
                                          "latency_ms": None if primary_loss else 5.0},
                             "10.0.0.2": {"loss_pct": 0.0, "latency_ms": 5.0},
                             "10.0.0.3": {"loss_pct": 0.0, "latency_ms": 5.0}}}
            return self._req("POST", "/report", body)

        for _ in range(3):
            _report_all(100.0)
        self.assertEqual(self.store.device_states("ispA")[primary]["state"], DOWN)

        status, body = self._req("GET", f"/api/inventory/redundancy?device_id={child}",
                                 token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(body["redundancy"]["on_backup"], 1)
        self.assertTrue(any("On backup" in s["title"] for s in self.notifier.sent))

if __name__ == "__main__":
    unittest.main()
