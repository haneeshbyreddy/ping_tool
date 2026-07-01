"""Central-side SNMP port folding (central/ports.py:CentralPortMonitor) — mirrors the
old single-box tests/integration/test_ports.py one-for-one, but against CentralStore's
tenant-scoped `switch_ports` table: discovery, flap-suppressed monitored-port-down,
admin-down silence, folding into the fed device's open outage vs a leading-indicator
heads-up, recovery, and the WISP_SNMP_ALERTS gate. Temp DB + a recording notifier — no
real SNMP/network.
"""
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.ports import CentralPortMonitor
from wisp.central.store import CentralStore
from wisp.egress.notifiers import NotifyResult

TS = "2026-01-01T00:00:00+00:00"
TENANT = "ispA"


class RecordingNotifier:
    channel = "ntfy"

    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(True)


def _port(idx, oper, admin="up", name=None, alias=None):
    return {"if_index": idx, "if_name": name or f"Gi0/{idx}", "if_alias": alias,
           "admin_status": admin, "oper_status": oper}


class CentralPortMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          snmp_down_consecutive=2)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(TENANT, ntfy_topic_owner="own", ntfy_topic_operator="op")
        self.switch = self.store.create_org_device(TENANT, {
            "name": "Core Switch", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "Rampur", "parent_device_id": None})
        self.tower = self.store.create_org_device(TENANT, {
            "name": "Rampur Tower", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "Rampur", "parent_device_id": None})
        self.notifier = RecordingNotifier()
        self.pm = CentralPortMonitor(self.store, TENANT, self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        return {r["if_index"]: r for r in
               self.store.list_switch_ports(TENANT, self.switch)}

    def _port_id(self, if_index):
        return self._rows()[if_index]["id"]

    def _discover_and_watch(self, if_index, feeds=None):
        self.pm.sync_device(self.switch, [_port(if_index, "up")], TS)
        pid = self._port_id(if_index)
        self.store.set_port_monitored(TENANT, pid, True)
        if feeds is not None:
            self.store.set_port_feeds(TENANT, pid, feeds)
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
        self.store.open_outage_if_absent(TENANT, self.tower, TS, "DOWN")
        self.pm.sync_device(self.switch, [_port(2, "down", alias="-> Rampur Tower")], TS)
        evs = self.pm.sync_device(self.switch, [_port(2, "down", alias="-> Rampur Tower")], TS)
        self.assertEqual([e.folded_into for e in evs], [self.tower])
        oid = self.store.open_outage_id(TENANT, self.tower)
        with self.store._connect() as conn:
            o = conn.execute("SELECT root_cause FROM outages WHERE id=?", (oid,)).fetchone()
        self.assertIn("Port", o["root_cause"])
        self.assertIn("down", o["root_cause"].lower())

    def test_leading_indicator_opens_no_outage(self):
        self._discover_and_watch(2, feeds=self.tower)   # feeds tower, but no open outage
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        evs = self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.assertEqual([e.folded_into for e in evs], [None])
        self.assertTrue(self.notifier.sent)              # still a heads-up page
        with self.store._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
        self.assertEqual(n, 0)                            # SNMP never opens an outage

    def test_recovery_edge_pages_once(self):
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)   # -> alarm
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
        self.assertEqual(self._rows()[2]["alarm"], 1)     # state still written
        with self.store._connect() as conn:
            st = conn.execute("SELECT status FROM alert_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(st["status"], "suppressed")

    def test_missing_operator_topic_is_soft_noop(self):
        self.store.set_org(TENANT, ntfy_topic_operator=None)
        # set_org COALESCEs, so blank it out directly for this test
        with self.store._connect() as conn:
            conn.execute("UPDATE orgs SET ntfy_topic_operator=NULL WHERE tenant_id=?", (TENANT,))
            conn.commit()
        self._discover_and_watch(2)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)
        self.pm.sync_device(self.switch, [_port(2, "down")], TS)   # must not raise
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._rows()[2]["alarm"], 1)


if __name__ == "__main__":
    unittest.main()
