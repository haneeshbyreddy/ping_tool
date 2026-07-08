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

class CentralPortMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_operator="op")
        self.switch = self.store.create_org_device(ORG, {
            "name": "Core Switch", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "Rampur", "parent_device_id": None})
        self.tower = self.store.create_org_device(ORG, {
            "name": "Rampur Tower", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "Rampur", "parent_device_id": None})
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
        self.assertEqual(self.notifier.sent[0]["recipient"], "op")

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

    def test_missing_operator_topic_is_soft_noop(self):
        self.store.set_org(ORG, ntfy_topic_operator=None)
        with self.store._connect() as conn:
            conn.execute("UPDATE orgs SET ntfy_topic_operator=NULL WHERE org_id=?", (ORG,))
            conn.commit()
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
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_operator="op")
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
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["recipient"], "op")
        self.assertIn("bandwidth", self.notifier.sent[0]["title"].lower())

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
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("recovered", self.notifier.sent[0]["title"].lower())

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
        self.assertTrue(self.notifier.sent)

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
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["recipient"], "op")
        self.assertIn("high bandwidth", self.notifier.sent[0]["title"].lower())

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
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("normalized", self.notifier.sent[0]["title"].lower())

    def test_low_and_high_alarms_are_independent(self):
        self.pm.sync_device(self.switch, [_pbw(3, 0, 0)], TS_SEQ[0])
        pid = self._row(3)["id"]
        self.store.set_port_monitored(ORG, pid, True)
        self.store.set_port_bandwidth_config(ORG, pid, 10, "either", 40)
        for i in (1, 2):
            self.pm.sync_device(self.switch, [_pbw(
                3, 20 * _OCT_PER_MBPS_10S, 20 * _OCT_PER_MBPS_10S)], TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self._row(3)["bw_high_alarm"], 0)
        for i in (3, 4):
            self.pm.sync_device(self.switch, [_pbw(
                3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)], TS_SEQ[i])
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

if __name__ == "__main__":
    unittest.main()
