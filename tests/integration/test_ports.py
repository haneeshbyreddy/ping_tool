"""Integration tests for SNMP port detection/folding (egress/ports.PortMonitor):
discovery, flap-suppressed monitored-port-down, admin-down silence, folding into the
fed device's outage vs a leading-indicator heads-up, recovery, and the WISP_SNMP_ALERTS
gate. Temp DB + a recording notifier — no real SNMP/network.
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
from wisp.database.client import connect, migrate
from wisp.egress.ports import PortMonitor
from wisp.egress.notifiers import NotifyResult
from wisp.ingress.snmp import PortStatus
from wisp.server import services

TS = "2026-01-01T00:00:00+00:00"


class RecordingNotifier:
    channel = "ntfy"

    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(True)


def _port(idx, oper, admin="up", name=None, alias=None):
    return PortStatus(if_index=idx, if_name=name or f"Gi0/{idx}", if_alias=alias,
                      admin_status=admin, oper_status=oper)


class PortMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db",
                          snmp_down_consecutive=2, ntfy_topic_operator="op",
                          ntfy_topic_owner="own")
        migrate(self.cfg)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (1,?,?,?)",
                      ("Core Switch", "10.0.0.1", "Rampur"))
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (2,?,?,?)",
                      ("Rampur Tower", "10.0.0.2", "Rampur"))
            c.commit()
        self.notifier = RecordingNotifier()
        self.pm = PortMonitor(self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _ports_rows(self):
        with connect(self.cfg) as c:
            return {r["if_index"]: r for r in c.execute(
                "SELECT * FROM switch_ports WHERE device_id=1")}

    def _port_id(self, if_index):
        with connect(self.cfg) as c:
            return c.execute("SELECT id FROM switch_ports WHERE device_id=1 AND if_index=?",
                             (if_index,)).fetchone()["id"]

    def _discover_and_watch(self, if_index, feeds=None):
        """First walk discovers ports (unmonitored); then flag if_index as monitored."""
        self.pm.sync_device(1, [_port(if_index, "up")], TS)
        pid = self._port_id(if_index)
        services.set_port_monitored(pid, True, self.cfg)
        if feeds is not None:
            services.set_port_feeds(pid, feeds, self.cfg)
        return pid

    def test_discovery_inserts_unmonitored(self):
        evs = self.pm.sync_device(1, [_port(1, "up"), _port(2, "down")], TS)
        self.assertEqual(evs, [])                       # nothing monitored -> no events
        self.assertEqual(self.notifier.sent, [])
        rows = self._ports_rows()
        self.assertEqual(set(rows), {1, 2})
        self.assertEqual(rows[1]["monitored"], 0)
        self.assertEqual(rows[2]["alarm"], 0)

    def test_monitored_down_is_flap_suppressed(self):
        self._discover_and_watch(2)
        # first down walk: streak 1, not yet alarmed (needs 2)
        self.assertEqual(self.pm.sync_device(1, [_port(2, "down")], TS), [])
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._ports_rows()[2]["alarm"], 0)
        # second consecutive down: alarm edge -> one operator page
        evs = self.pm.sync_device(1, [_port(2, "down")], TS)
        self.assertEqual([e.kind for e in evs], ["down"])
        self.assertEqual(self._ports_rows()[2]["alarm"], 1)
        self.assertTrue(self.notifier.sent)
        self.assertEqual(self.notifier.sent[0]["recipient"], "op")

    def test_single_blip_does_not_alarm(self):
        self._discover_and_watch(2)
        self.pm.sync_device(1, [_port(2, "down")], TS)   # one down
        self.pm.sync_device(1, [_port(2, "up")], TS)     # back up -> streak resets
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._ports_rows()[2]["alarm"], 0)

    def test_admin_down_stays_silent(self):
        self._discover_and_watch(2)
        for _ in range(4):
            self.pm.sync_device(1, [_port(2, "down", admin="down")], TS)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._ports_rows()[2]["alarm"], 0)

    def test_folds_into_open_outage(self):
        self._discover_and_watch(2, feeds=2)
        with connect(self.cfg) as c:   # the fed device already has an open outage
            c.execute("INSERT INTO outages (device_id,started_at,final_state)"
                      " VALUES (2,?, 'DOWN')", (TS,))
            c.commit()
        self.pm.sync_device(1, [_port(2, "down", alias="-> Rampur Tower")], TS)
        evs = self.pm.sync_device(1, [_port(2, "down", alias="-> Rampur Tower")], TS)
        self.assertEqual([e.folded_into for e in evs], [2])
        with connect(self.cfg) as c:
            o = c.execute("SELECT root_cause FROM outages WHERE device_id=2").fetchone()
        self.assertIn("Port", o["root_cause"])           # physical cause stamped
        self.assertIn("down", o["root_cause"].lower())

    def test_leading_indicator_opens_no_outage(self):
        self._discover_and_watch(2, feeds=2)             # feeds dev 2, but no open outage
        self.pm.sync_device(1, [_port(2, "down")], TS)
        evs = self.pm.sync_device(1, [_port(2, "down")], TS)
        self.assertEqual([e.folded_into for e in evs], [None])   # nothing to fold into
        self.assertTrue(self.notifier.sent)                       # still a heads-up page
        with connect(self.cfg) as c:
            n = c.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
        self.assertEqual(n, 0)                                    # SNMP never opens an outage

    def test_recovery_edge_pages_once(self):
        self._discover_and_watch(2)
        self.pm.sync_device(1, [_port(2, "down")], TS)
        self.pm.sync_device(1, [_port(2, "down")], TS)   # -> alarm
        self.notifier.sent.clear()
        evs = self.pm.sync_device(1, [_port(2, "up")], TS)
        self.assertEqual([e.kind for e in evs], ["up"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("restored", self.notifier.sent[0]["title"].lower())
        self.assertEqual(self._ports_rows()[2]["alarm"], 0)

    def test_alerts_gate_keeps_state_mutes_page(self):
        self.pm.cfg = replace(self.cfg, snmp_alerts=False)
        self._discover_and_watch(2)
        self.pm.sync_device(1, [_port(2, "down")], TS)
        self.pm.sync_device(1, [_port(2, "down")], TS)
        self.assertEqual(self.notifier.sent, [])         # page muted
        self.assertEqual(self._ports_rows()[2]["alarm"], 1)   # state still written
        with connect(self.cfg) as c:
            st = c.execute("SELECT status FROM alert_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(st["status"], "suppressed")


if __name__ == "__main__":
    unittest.main()
