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


# Timestamps 10s apart so a counter delta yields a real rate (the existing port tests
# reuse one TS, which gives dt=0 -> no rate; bandwidth needs distinct stamps).
TS_SEQ = [f"2026-01-01T00:00:{s:02d}+00:00" for s in (0, 10, 20, 30, 40, 50)]
# octets gained over a 10s walk for a given rate: bytes = mbps*1e6 * 10s / 8.
_OCT_PER_MBPS_10S = 1_250_000


def _pbw(idx, in_oct, out_oct, oper="up", admin="up"):
    """A port carrying byte counters (for the bandwidth tier)."""
    return PortStatus(if_index=idx, if_name=f"Gi0/{idx}", if_alias=None,
                      admin_status=admin, oper_status=oper,
                      in_octets=in_oct, out_octets=out_oct, speed_bps=1_000_000_000)


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


class PortBandwidthTest(unittest.TestCase):
    """Low-bandwidth threshold tier: throughput from counter deltas, flap-suppressed
    below-threshold alarm, direction selection, recovery, the alerts gate, and the
    silent clear when a bw-alarmed port goes down."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db",
                          snmp_down_consecutive=2, snmp_bw_consecutive=2,
                          ntfy_topic_operator="op", ntfy_topic_owner="own")
        migrate(self.cfg)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region) VALUES (1,?,?,?)",
                      ("Core Switch", "10.0.0.1", "Rampur"))
            c.commit()
        self.notifier = RecordingNotifier()
        self.pm = PortMonitor(self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, idx=3):
        with connect(self.cfg) as c:
            return c.execute("SELECT * FROM switch_ports WHERE device_id=1 AND if_index=?",
                             (idx,)).fetchone()

    def _watch_bw(self, idx, threshold, direction="either"):
        """Discover the port (baseline counters at TS0), then watch it + set a bw floor."""
        self.pm.sync_device(1, [_pbw(idx, 0, 0)], TS_SEQ[0])
        pid = self._row(idx)["id"]
        services.set_port_monitored(pid, True, self.cfg)
        services.set_port_bandwidth(pid, threshold, direction, self.cfg)
        return pid

    def test_throughput_is_computed_from_counter_delta(self):
        self._watch_bw(3, threshold=1)            # 1 Mbps floor (won't trip at 50)
        # +62.5MB over 10s = 50 Mbps in each direction.
        self.pm.sync_device(1, [_pbw(3, 50 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        r = self._row(3)
        self.assertAlmostEqual(r["in_bps"], 50_000_000.0, delta=1.0)
        self.assertAlmostEqual(r["out_bps"], 50_000_000.0, delta=1.0)
        self.assertEqual(r["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_low_bandwidth_is_flap_suppressed_then_pages(self):
        self._watch_bw(3, threshold=10)
        # walk 1: 5 Mbps (below 10) -> streak 1, not yet alarmed (needs 2).
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])
        # walk 2: still 5 Mbps -> alarm edge -> one operator page.
        evs = self.pm.sync_device(
            1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)], TS_SEQ[2])
        self.assertEqual([e.kind for e in evs], ["bw_low"])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["recipient"], "op")
        self.assertIn("bandwidth", self.notifier.sent[0]["title"].lower())

    def test_single_dip_does_not_alarm(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])                                   # one low walk
        self.pm.sync_device(1, [_pbw(3, 55 * _OCT_PER_MBPS_10S, 55 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[2])                                   # back to 50 Mbps
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_recovery_edge_pages_once(self):
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        self.pm.sync_device(1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[2])                                   # -> alarm
        self.notifier.sent.clear()
        evs = self.pm.sync_device(
            1, [_pbw(3, 60 * _OCT_PER_MBPS_10S, 60 * _OCT_PER_MBPS_10S)], TS_SEQ[3])
        self.assertEqual([e.kind for e in evs], ["bw_ok"])             # 50 Mbps again
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("recovered", self.notifier.sent[0]["title"].lower())

    def test_direction_out_ignores_low_inbound(self):
        # in is starved (5 Mbps) but out is healthy (50 Mbps); watching 'out' -> no alarm.
        self._watch_bw(3, threshold=10, direction="out")
        for i in (1, 2, 3):
            self.pm.sync_device(
                1, [_pbw(3, i * 5 * _OCT_PER_MBPS_10S, i * 50 * _OCT_PER_MBPS_10S)],
                TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertEqual(self.notifier.sent, [])

    def test_direction_in_catches_low_inbound(self):
        # same asymmetric traffic, but watching 'in' -> the starved inbound alarms.
        self._watch_bw(3, threshold=10, direction="in")
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 50 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        self.pm.sync_device(1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 100 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[2])
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.assertTrue(self.notifier.sent)

    def test_unmonitored_port_never_bw_alarms(self):
        # threshold set but the port isn't watched -> live rate stored, never alarms.
        self.pm.sync_device(1, [_pbw(3, 0, 0)], TS_SEQ[0])
        pid = self._row(3)["id"]
        services.set_port_bandwidth(pid, 10, "either", self.cfg)   # no set_port_monitored
        for i in (1, 2, 3):
            self.pm.sync_device(
                1, [_pbw(3, i * 5 * _OCT_PER_MBPS_10S, i * 5 * _OCT_PER_MBPS_10S)],
                TS_SEQ[i])
        self.assertEqual(self._row(3)["bw_alarm"], 0)
        self.assertIsNotNone(self._row(3)["in_bps"])               # stats still captured
        self.assertEqual(self.notifier.sent, [])

    def test_alerts_gate_keeps_state_mutes_page(self):
        self.pm.cfg = replace(self.cfg, snmp_bw_alerts=False)
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        self.pm.sync_device(1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[2])
        self.assertEqual(self.notifier.sent, [])                   # page muted
        self.assertEqual(self._row(3)["bw_alarm"], 1)              # state still written
        with connect(self.cfg) as c:
            st = c.execute("SELECT status FROM alert_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(st["status"], "suppressed")

    def test_port_going_down_clears_bw_alarm_silently(self):
        # a bw-alarmed port that then goes oper-down clears its bw badge with NO bw page —
        # the down alarm owns the story (no confusing "bandwidth recovered").
        self._watch_bw(3, threshold=10)
        self.pm.sync_device(1, [_pbw(3, 5 * _OCT_PER_MBPS_10S, 5 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[1])
        self.pm.sync_device(1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S)],
                            TS_SEQ[2])                              # bw alarm on
        self.assertEqual(self._row(3)["bw_alarm"], 1)
        self.notifier.sent.clear()
        evs = self.pm.sync_device(
            1, [_pbw(3, 10 * _OCT_PER_MBPS_10S, 10 * _OCT_PER_MBPS_10S, oper="down")],
            TS_SEQ[3])
        self.assertEqual(evs, [])                                  # no bw edge event
        self.assertEqual(self._row(3)["bw_alarm"], 0)             # badge cleared
        self.assertEqual(self.notifier.sent, [])                   # silent


if __name__ == "__main__":
    unittest.main()
