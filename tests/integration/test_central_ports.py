import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central.ports import CentralPortMonitor
from wisp.central.store import CentralStore
from support import RecordingNotifier

TS = "2026-01-01T00:00:00+00:00"
ORG = "ispA"

def _port(idx, oper, admin="up", name=None, alias=None):
    return {"if_index": idx, "if_name": name or f"Gi0/{idx}", "if_alias": alias,
           "admin_status": admin, "oper_status": oper}

TS_SEQ = [f"2026-01-01T00:00:{s:02d}+00:00" for s in (0, 10, 20, 30, 40, 50)]
_OCT_PER_MBPS_10S = 1_250_000

def _pbw(idx, in_oct, out_oct, oper="up", admin="up"):
    return {"if_index": idx, "if_name": f"Gi0/{idx}", "if_alias": None,
           "admin_status": admin, "oper_status": oper,
           "in_octets": in_oct, "out_octets": out_oct, "speed_bps": 1_000_000_000}

def _hot(idx, oper="up", admin="up", **cells):
    # What a budget-bounded walk delivers: the status columns arrived, the rest
    # of the ifTable never did, so those keys are ABSENT from the row.
    return {"if_index": idx, "admin_status": admin, "oper_status": oper, **cells}

class CentralPortMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.switch = self.store.create_org_device(ORG, {
            "name": "Core Switch", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "Rampur", "parent_device_id": None})
        self.tower = self.store.create_org_device(ORG, {
            "name": "Rampur Tower", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "Rampur", "parent_device_id": None})
        w = self.store.add_user(ORG, "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(w, "919000000009")
        for did in (self.switch, self.tower):
            self.store.set_device_assignees(ORG, did, [w], "own")
        self.notifier = RecordingNotifier()
        self.pm = CentralPortMonitor(self.store, ORG, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        return {r["if_index"]: r for r in
               self.store.list_switch_ports(ORG, self.switch)}

    def _port_id(self, if_index):
        return self._rows()[if_index]["id"]

    def _discover_and_watch(self, if_index, feeds=None):
        self.pm.sync_device(self.switch, [_port(if_index, "up")], TS)
        pid = self._port_id(if_index)
        self.store.set_port_monitored(ORG, pid, True)
        if feeds is not None:
            self.store.set_port_feeds(ORG, pid, feeds)
        return pid

    def test_discovery_inserts_unmonitored(self):
        evs = self.pm.sync_device(self.switch, [_port(1, "up"), _port(2, "down")], TS)
        self.assertEqual(evs, [])
        self.assertEqual(self.notifier.sent, [])
        rows = self._rows()
        self.assertEqual(set(rows), {1, 2})
        self.assertEqual(rows[1]["monitored"], 0)
        self.assertEqual(rows[2]["alarm"], 0)

    def test_monitored_down_is_flap_suppressed(self):
        self._discover_and_watch(2)
        self.assertEqual(self.pm.sync_device(self.switch, [_port(2, "down")], TS), [])
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 0)
        evs = self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.assertEqual([e.kind for e in evs], ["down"])
        self.assertEqual(self._rows()[2]["alarm"], 1)
        self.assertTrue(self.notifier.sent)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000009"])

    def test_single_blip_does_not_alarm(self):
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "up")], TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 0)

    def test_admin_down_stays_silent(self):
        self._discover_and_watch(2)
        for _ in range(4):
            self.pm.sync_device(self.switch, [_port(2, "down", admin="down")], TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 0)

    def test_folds_into_open_outage(self):
        self._discover_and_watch(2, feeds=self.tower)
        self.store.open_outage_if_absent(ORG, self.tower, TS, "DOWN")
        self.pm.sync_device(self.switch, [_port(2, "down", alias="-> Rampur Tower")], TS)
        evs = self.pm.sync_device(self.switch, [_port(2, "down", alias="-> Rampur Tower")], TS)
        self.assertEqual([e.folded_into for e in evs], [self.tower])
        oid = self.store.open_outage_id(ORG, self.tower)
        with self.store._connect() as conn:
            o = conn.execute("SELECT root_cause FROM outages WHERE id=?", (oid,)).fetchone()
        self.assertIn("Port", o["root_cause"])
        self.assertIn("down", o["root_cause"].lower())

    def test_leading_indicator_opens_no_outage(self):
        self._discover_and_watch(2, feeds=self.tower)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        evs = self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.assertEqual([e.folded_into for e in evs], [None])
        self.assertTrue(self.notifier.sent)
        with self.store._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
        self.assertEqual(n, 0)

    def test_recovery_edge_pages_once(self):
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.notifier.sent.clear()
        evs = self.pm.sync_device(self.switch, [_port(2, "up")], TS)
        self.assertEqual([e.kind for e in evs], ["up"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("restored", self.notifier.sent[0]["title"].lower())
        self.assertEqual(self._rows()[2]["alarm"], 0)

    def test_alerts_gate_keeps_state_mutes_page(self):
        self.pm.cfg = replace(self.cfg, snmp_alerts=False, snmp_down_consecutive=2)
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 1)
        with self.store._connect() as conn:
            st = conn.execute("SELECT status FROM alert_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(st["status"], "suppressed")

    def test_missing_recipient_is_soft_noop(self):
        self.store.set_user_whatsapp(
            self.store.get_user_by_username("wkr1")["id"], None)
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 1)

class BandwidthTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2, snmp_bw_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.switch = self.store.create_org_device(ORG, {
            "name": "Core Switch", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.pm = CentralPortMonitor(self.store, ORG, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, idx=3):
        return {r["if_index"]: r for r in
               self.store.list_switch_ports(ORG, self.switch)}[idx]

    def _queued(self):
        return self.store.pending_digest(ORG)

    def _watch_bw(self, idx, threshold, direction="either"):
        self.pm.sync_device(self.switch, [_pbw(idx, 0, 0)], TS_SEQ[0])
        pid = self._row(idx)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        self.store.set_port_bandwidth_config(ORG, pid, threshold, direction)
        return pid

    def test_throughput_is_computed_from_counter_delta(self):
        self._watch_bw(3, threshold=1)
        self.pm.sync_device(self.switch, [_pbw(
            3, 50 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        r = self._row(3)
        self.assertAlmostEqual(r["in_bps"], 50_000_000.0, delta=1.0)
        self.assertAlmostEqual(r["out_bps"], 50_000_000.0, delta=1.0)
        self.assertEqual(r["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_low_bandwidth_is_flap_suppressed_then_pages(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual([e.kind for e in evs], ["bw_low"])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._queued(), [])

    def test_single_dip_does_not_alarm(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 55 * _OCT_PER_MBPS_10S, 55 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_recovery_edge_pages_once(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.notifier.sent.clear()
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 60 * _OCT_PER_MBPS_10S, 60 * _OCT_PER_MBPS_10S)], TS_SEQ[3])
        self.assertEqual([e.kind for e in evs], ["bw_ok"])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_direction_out_ignores_low_inbound(self):
        self._watch_bw(3, threshold=10, direction="out")
        for i in (1, 2, 3):
            self.pm.sync_device(self.switch, [_pbw(
                3, i * 5 * _OCT_PER_MBPS_10S, i * 50 * _OCT_PER_MBPS_10S)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_direction_in_catches_low_inbound(self):
        self._watch_bw(3, threshold=10, direction="in")
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.assertEqual(self.notifier.sent, [])

    def test_unmonitored_port_never_bw_alarms(self):
        self.pm.sync_device(self.switch, [_pbw(3, 0, 0)], TS_SEQ[0])
        pid = self._row(3)["id"]
        self.store.set_port_bandwidth_config(ORG, pid, 10, "either")
        for i in (1, 2, 3):
            self.pm.sync_device(self.switch, [_pbw(
                3, i * 5 * _OCT_PER_MBPS_10S, i * 5 * _OCT_PER_MBPS_10S)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertIsNotNone(self._row(3)["in_bps"])
        self.assertEqual(self.notifier.sent, [])

    def test_alerts_gate_keeps_state_mutes_page(self):
        self.pm.cfg = replace(self.cfg, snmp_bw_alerts=False, snmp_bw_consecutive=2)
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        with self.store._connect() as conn:
            st = conn.execute(
                "SELECT status FROM alert_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(st["status"], "suppressed")

    def test_port_going_down_clears_bw_alarm_silently(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.notifier.sent.clear()
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S, oper="down")], TS_SEQ[3])
        self.assertEqual(evs, [])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def _watch_bw_max(self, idx, max_mbps, direction="either"):
        self.pm.sync_device(self.switch, [_pbw(idx, 0, 0)], TS_SEQ[0])
        pid = self._row(idx)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        self.store.set_port_bandwidth_config(ORG, pid, None, direction, max_mbps)
        return pid

    def test_high_bandwidth_is_flap_suppressed_then_pages(self):
        self._watch_bw_max(3, max_mbps=40)
        self.pm.sync_device(self.switch, [_pbw(
            3, 50 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 100 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual([e.kind for e in evs], ["bw_high"])
        self.assertEqual(self._row(3)["bw_high_alarm"], 1)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._queued(), [])

    def test_high_bandwidth_recovery_edge_pages_once(self):
        self._watch_bw_max(3, max_mbps=40)
        self.pm.sync_device(self.switch, [_pbw(
            3, 50 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 100 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.notifier.sent.clear()
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 105 * _OCT_PER_MBPS_10S, 105 * _OCT_PER_MBPS_10S)], TS_SEQ[3])
        self.assertEqual([e.kind for e in evs], ["bw_normal"])
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_low_and_high_alarms_are_independent(self):
        self.pm.sync_device(self.switch, [_pbw(3, 0, 0)], TS_SEQ[0])
        pid = self._row(3)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        self.store.set_port_bandwidth_config(ORG, pid, 10, "either", 40)
        # An octet counter only ever CLIMBS — what falls is the delta per sweep.
        # Walking the absolute value back down instead reads as the backwards
        # counter CounterRegressionTest guards, which publishes no rate at all.
        total = 0
        for i, mbps in ((1, 20), (2, 20)):
            total += mbps * _OCT_PER_MBPS_10S
            self.pm.sync_device(self.switch, [_pbw(3, total, total)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        for i, mbps in ((3, 5), (4, 5)):
            total += mbps * _OCT_PER_MBPS_10S
            self.pm.sync_device(self.switch, [_pbw(3, total, total)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)

    def test_unmonitored_port_never_bw_high_alarms(self):
        self.pm.sync_device(self.switch, [_pbw(3, 0, 0)], TS_SEQ[0])
        pid = self._row(3)["id"]
        self.store.set_port_bandwidth_config(ORG, pid, None, "either", 10)
        for i in (1, 2, 3):
            self.pm.sync_device(self.switch, [_pbw(
                3, i * 50 * _OCT_PER_MBPS_10S, i * 50 * _OCT_PER_MBPS_10S)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_port_going_down_clears_bw_high_alarm_silently(self):
        self._watch_bw_max(3, max_mbps=40)
        self.pm.sync_device(self.switch, [_pbw(
            3, 50 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 100 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_high_alarm"], 1)
        self.notifier.sent.clear()
        evs = self.pm.sync_device(self.switch, [_pbw(
            3, 100 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S, oper="down")], TS_SEQ[3])
        self.assertEqual(evs, [])
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_bandwidth_summary_hides_alarms_on_an_unreachable_device(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(self.switch, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.switch, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.store.write_device_states(ORG, [(self.switch, "UP", 1.0, 0.0, 0.1)], TS_SEQ[3])
        self.assertEqual(len(self.store.low_bandwidth_alarms(ORG)), 1)
        for state in ("DOWN", "UNREACHABLE"):
            self.store.write_device_states(ORG, [(self.switch, state, None, 100.0, None)], TS_SEQ[4])
            self.assertEqual(self.store.low_bandwidth_alarms(ORG), [], state)
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.store.write_device_states(ORG, [(self.switch, "UP", 1.0, 0.0, 0.1)], TS_SEQ[5])
        self.assertEqual(len(self.store.low_bandwidth_alarms(ORG)), 1)

class PartialWalkTest(unittest.TestCase):
    # A big OLT's port walk runs out of budget before the counter columns, so
    # rows arrive without them. Wiping the stored octets on those sweeps is why
    # a rate — which needs two CONSECUTIVE complete walks — never computed once.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2, snmp_bw_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.7", "device_type": "OLT",
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.pm = CentralPortMonitor(self.store, ORG, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, idx=3):
        return {r["if_index"]: r for r in
               self.store.list_switch_ports(ORG, self.olt)}[idx]

    def _watch(self, idx=3, threshold=None):
        self.pm.sync_device(self.olt, [_pbw(idx, 0, 0)], TS_SEQ[0])
        pid = self._row(idx)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        if threshold is not None:
            self.store.set_port_bandwidth_config(ORG, pid, threshold, "either")
        return pid

    def test_a_counterless_sweep_keeps_the_baseline_for_the_next_complete_walk(self):
        self._watch()
        self.pm.sync_device(self.olt, [_hot(3)], TS_SEQ[1])
        held = self._row(3)
        self.assertEqual(held["in_octets"], "0")
        self.assertEqual(held["out_octets"], "0")
        self.assertEqual(held["counters_at"], TS_SEQ[0])
        # dt spans the gap: the two complete walks are 20s apart, not 10s.
        self.pm.sync_device(self.olt, [_pbw(
            3, 20 * _OCT_PER_MBPS_10S, 20 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        r = self._row(3)
        self.assertAlmostEqual(r["in_bps"], 10_000_000.0, delta=1.0)
        self.assertAlmostEqual(r["out_bps"], 10_000_000.0, delta=1.0)
        self.assertEqual(r["counters_at"], TS_SEQ[2])

    def test_an_absent_identity_key_is_held_and_a_present_null_one_clears(self):
        self.pm.sync_device(self.olt, [{
            "if_index": 3, "if_name": "Gi0/3", "if_alias": "-> Rampur Tower",
            "admin_status": "up", "oper_status": "up",
            "last_change": "12:00", "in_octets": 0, "out_octets": 0}], TS_SEQ[0])
        self.pm.sync_device(self.olt, [_hot(3)], TS_SEQ[1])
        r = self._row(3)
        self.assertEqual(r["if_name"], "Gi0/3")
        self.assertEqual(r["if_alias"], "-> Rampur Tower")
        self.assertEqual(r["last_change"], "12:00")
        # A full walk that DID read the column and found it empty is authoritative.
        self.pm.sync_device(self.olt, [_pbw(3, 0, 0)], TS_SEQ[2])
        r = self._row(3)
        self.assertIsNone(r["if_alias"])
        self.assertEqual(r["if_name"], "Gi0/3")

    def test_one_counter_without_the_other_preserves_the_whole_pair(self):
        self._watch()
        self.pm.sync_device(self.olt, [_pbw(
            3, 25 * _OCT_PER_MBPS_10S, 25 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        rated = self._row(3)
        self.assertAlmostEqual(rated["in_bps"], 25_000_000.0, delta=1.0)
        # counters_at is ONE stamp for both directions, so a half pair is no pair.
        self.pm.sync_device(self.olt, [_hot(
            3, in_octets=99 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        r = self._row(3)
        self.assertEqual(r["in_octets"], str(25 * _OCT_PER_MBPS_10S))
        self.assertEqual(r["out_octets"], str(25 * _OCT_PER_MBPS_10S))
        self.assertEqual(r["counters_at"], TS_SEQ[1])
        self.assertAlmostEqual(r["in_bps"], 25_000_000.0, delta=1.0)
        self.assertAlmostEqual(r["out_bps"], 25_000_000.0, delta=1.0)

    def test_a_held_rate_expires_once_its_counters_go_stale(self):
        self._watch()
        self.pm.sync_device(self.olt, [_pbw(
            3, 25 * _OCT_PER_MBPS_10S, 25 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.olt, [_hot(3)], "2026-01-01T00:14:00+00:00")
        self.assertAlmostEqual(self._row(3)["in_bps"], 25_000_000.0, delta=1.0)
        self.pm.sync_device(self.olt, [_hot(3)], "2026-01-01T00:15:20+00:00")
        r = self._row(3)
        self.assertIsNone(r["in_bps"])
        self.assertIsNone(r["out_bps"])
        # only the RATE expires — the baseline is still what the next walk measures
        self.assertEqual(r["in_octets"], str(25 * _OCT_PER_MBPS_10S))
        self.assertEqual(r["counters_at"], TS_SEQ[1])

    def test_bandwidth_alarm_state_holds_across_a_counterless_sweep(self):
        self._watch(threshold=10)
        self.pm.sync_device(self.olt, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.assertEqual(self._row(3)["bw_low_streak"], 1)
        # does not advance: a sweep with no rate is no evidence toward the alarm
        self.assertEqual(self.pm.sync_device(self.olt, [_hot(3)], TS_SEQ[2]), [])
        self.assertEqual(self._row(3)["bw_low_streak"], 1)
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        evs = self.pm.sync_device(self.olt, [_pbw(
            3, 15 * _OCT_PER_MBPS_10S, 15 * _OCT_PER_MBPS_10S)], TS_SEQ[3])
        self.assertEqual([e.kind for e in evs], ["bw_low"])
        since = self._row(3)["bw_alarm_since"]
        # and does not reset: a real alarm survives the gap
        self.assertEqual(self.pm.sync_device(self.olt, [_hot(3)], TS_SEQ[4]), [])
        r = self._row(3)
        self.assertEqual((r["bw_alarm"], r["bw_low_streak"]), (1, 2))
        self.assertEqual(r["bw_alarm_since"], since)

    def test_a_port_going_down_on_a_counterless_sweep_still_clears_bw_alarm(self):
        # Oper status arrives on every sweep, so eligibility is current even
        # when the rate is not — the hold must not keep a dead port's alarm.
        self._watch(threshold=10)
        self.pm.sync_device(self.olt, [_pbw(
            3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.olt, [_pbw(
            3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.pm.sync_device(self.olt, [_hot(3, oper="down")], TS_SEQ[3])
        r = self._row(3)
        self.assertEqual((r["bw_alarm"], r["bw_low_streak"]), (0, 0))

    def test_the_historian_records_no_rate_for_a_counterless_sweep(self):
        self._watch()
        self.pm.sync_device(self.olt, [_pbw(
            3, 25 * _OCT_PER_MBPS_10S, 25 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        self.pm.sync_device(self.olt, [_hot(3)], TS_SEQ[2])
        rows = self.store.port_history(ORG, self.olt, 3, 0, 2**33, "sweep")
        rated = {r["ts"]: r["in_bps"] for r in rows}
        self.assertEqual(len(rated), 2)
        # a held rate is not a second measurement of it
        self.assertIsNone(rated[max(rated)])
        self.assertEqual([r["oper_up"] for r in rows], [1, 1])


class CounterRegressionTest(unittest.TestCase):
    # An agent occasionally reads its own octet counters BACKWARDS for one sweep
    # — a glitch, not a reboot. throughput_bps already refuses the negative
    # delta, so that sweep published nothing; what leaked was the BASELINE. The
    # low value was stored, and the next normal read subtracted against it and
    # reported the port's whole lifetime counter as one interval: NLK-OLT
    # EPON0/3 at 121.85 Gb/s on a 1 Gb/s PON, which the busy-hour panel averaged
    # into 6.78 Gb/s and an ISP disputed.

    LIFETIME = 500_000 * _OCT_PER_MBPS_10S  # a port that has been up for months

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2, snmp_bw_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "NLK-OLT", "ip_address": "10.0.0.9", "device_type": "OLT",
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.pm = CentralPortMonitor(self.store, ORG, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, idx=3):
        return {r["if_index"]: r for r in
               self.store.list_switch_ports(ORG, self.olt)}[idx]

    def _watch(self, octets=0, idx=3, threshold=None):
        # The first sweep creates the row, so monitoring is set after it — the
        # counter it carries becomes the baseline with no rate behind it.
        self.pm.sync_device(self.olt, [_pbw(idx, octets, octets)], TS_SEQ[0])
        pid = self._row(idx)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        if threshold is not None:
            self.store.set_port_bandwidth_config(ORG, pid, threshold, "either")
        return pid

    def _rates(self, idx=3):
        # hist_port_sweep is keyed on an epoch ts, so read it in walk order.
        return [r["in_bps"] for r in
               self.store.port_history(ORG, self.olt, idx, 0, 2**33, "sweep")]

    def test_a_backwards_counter_is_not_stored_as_the_baseline(self):
        self._watch(self.LIFETIME)
        self.pm.sync_device(self.olt, [_pbw(3, 1_000, 1_000)], TS_SEQ[1])
        r = self._row(3)
        self.assertEqual(r["in_octets"], str(self.LIFETIME))
        self.assertEqual(r["out_octets"], str(self.LIFETIME))
        self.assertEqual(r["counters_at"], TS_SEQ[0])

    def test_the_next_good_read_measures_the_interval_not_the_lifetime(self):
        self._watch(self.LIFETIME)
        self.pm.sync_device(self.olt, [_pbw(3, 1_000, 1_000)], TS_SEQ[1])
        # 20 Mb/s over the 20s the two GOOD reads really span, not 500 Gb/s of
        # lifetime counter divided by one 10s sweep.
        good = self.LIFETIME + 40 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, good, good)], TS_SEQ[2])
        r = self._row(3)
        self.assertAlmostEqual(r["in_bps"], 20_000_000.0, delta=1.0)
        self.assertAlmostEqual(r["out_bps"], 20_000_000.0, delta=1.0)
        self.assertEqual(r["counters_at"], TS_SEQ[2])

    def test_the_historian_records_no_rate_for_the_glitch_sweep(self):
        # This is the tier the busy-hour panel averages, so a spike reaching it
        # is what the ISP saw.
        self._watch(self.LIFETIME)
        self.pm.sync_device(self.olt, [_pbw(3, 1_000, 1_000)], TS_SEQ[1])
        good = self.LIFETIME + 40 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, good, good)], TS_SEQ[2])
        glitch, good_read = self._rates()
        self.assertIsNone(glitch)
        self.assertAlmostEqual(good_read, 20_000_000.0, delta=1.0)

    def test_one_direction_reading_backwards_condemns_the_pair(self):
        # counters_at is ONE stamp for both, so taking the direction that still
        # looks sane would make the next delta divide by the wrong dt.
        self._watch(self.LIFETIME)
        self.pm.sync_device(self.olt, [_pbw(
            3, 1_000, self.LIFETIME + 10 * _OCT_PER_MBPS_10S)], TS_SEQ[1])
        r = self._row(3)
        self.assertEqual(r["in_octets"], str(self.LIFETIME))
        self.assertEqual(r["out_octets"], str(self.LIFETIME))
        self.assertEqual(r["counters_at"], TS_SEQ[0])

    def test_a_suspiciously_high_reading_is_still_adopted_as_the_baseline(self):
        # The guard is one-sided ON PURPOSE: today's unconditional overwrite of a
        # high reading is what let TMG-OLT self-heal in two sweeps.
        self._watch(10 * _OCT_PER_MBPS_10S)
        bogus = 9_000_000 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, bogus, bogus)], TS_SEQ[1])
        r = self._row(3)
        self.assertEqual(r["in_octets"], str(bogus))
        self.assertEqual(r["counters_at"], TS_SEQ[1])

    def test_a_stale_baseline_adopts_the_lower_counter_and_skips_the_gap(self):
        # The escape hatch for a genuine reboot — and the bound on the hold after
        # a bogus HIGH reading, where every honest read after it looks backwards.
        self._watch(self.LIFETIME)
        rebooted_at = "2026-01-01T00:16:00+00:00"  # 960s on from the baseline
        self.pm.sync_device(self.olt, [_pbw(3, 5_000, 5_000)], rebooted_at)
        r = self._row(3)
        self.assertEqual(r["in_octets"], "5000")
        self.assertEqual(r["counters_at"], rebooted_at)
        self.assertIsNone(r["in_bps"])  # nothing is published for the gap
        self.assertIsNone(r["out_bps"])
        self.assertEqual(self._rates(), [None])
        after = 5_000 + 10 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, after, after)],
                            "2026-01-01T00:16:10+00:00")
        self.assertAlmostEqual(self._row(3)["in_bps"], 10_000_000.0, delta=1.0)

    def test_a_held_rate_still_expires_across_a_run_of_backwards_reads(self):
        # A glitch sweep holds the last rate, exactly as a counter-less one does
        # — but a rate is a claim about NOW, so it may not outlive its stamp.
        self._watch(self.LIFETIME)
        rated = self.LIFETIME + 25 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, rated, rated)], TS_SEQ[1])
        self.assertAlmostEqual(self._row(3)["in_bps"], 25_000_000.0, delta=1.0)
        self.pm.sync_device(self.olt, [_pbw(3, 7, 7)], "2026-01-01T00:14:00+00:00")
        self.assertAlmostEqual(self._row(3)["in_bps"], 25_000_000.0, delta=1.0)
        self.pm.sync_device(self.olt, [_pbw(3, 7, 7)], "2026-01-01T00:15:20+00:00")
        r = self._row(3)
        self.assertIsNone(r["in_bps"])
        self.assertIsNone(r["out_bps"])

    def test_bandwidth_alarm_state_holds_across_a_backwards_sweep(self):
        # A sweep with no rate is no evidence either way, but eligibility rides
        # oper status, which IS current.
        self._watch(self.LIFETIME, threshold=10)
        low = self.LIFETIME + 5 * _OCT_PER_MBPS_10S
        self.pm.sync_device(self.olt, [_pbw(3, low, low)], TS_SEQ[1])
        self.assertEqual(self._row(3)["bw_low_streak"], 1)
        self.assertEqual(self.pm.sync_device(self.olt, [_pbw(3, 12, 12)],
                                             TS_SEQ[2]), [])
        r = self._row(3)
        self.assertEqual((r["bw_low_streak"], r["bw_alarm"]), (1, 0))

    def test_a_port_going_down_on_a_backwards_sweep_still_clears_bw_alarm(self):
        self._watch(self.LIFETIME, threshold=10)
        total = self.LIFETIME
        for i in (1, 2):  # two consecutive 5 Mb/s sweeps arm the low alarm
            total += 5 * _OCT_PER_MBPS_10S
            self.pm.sync_device(self.olt, [_pbw(3, total, total)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.pm.sync_device(self.olt, [_pbw(3, 9, 9, oper="down")], TS_SEQ[3])
        r = self._row(3)
        self.assertEqual((r["bw_alarm"], r["bw_low_streak"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
