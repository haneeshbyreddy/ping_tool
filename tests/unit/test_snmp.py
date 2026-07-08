import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.ingress.snmp import (
    OID_IF_ADMIN, OID_IF_ALIAS, OID_IF_DESCR, OID_IF_HCIN, OID_IF_HCOUT,
    OID_IF_HIGHSPEED, OID_IF_LASTCHANGE, OID_IF_NAME, OID_IF_OPER, OID_IF_SPEED,
    PortStatus, PysnmpPoller, SnmpTarget, parse_if_table, throughput_bps,
)

try:
    import pysnmp
    _HAS_PYSNMP = True
except ImportError:
    _HAS_PYSNMP = False

_DEAD_TARGET = SnmpTarget(ip="127.0.0.1", community="public", port=1)

@unittest.skipUnless(_HAS_PYSNMP, "pysnmp not installed")
class EngineReuseTest(unittest.TestCase):
    def test_one_engine_across_walks(self):
        from pysnmp.hlapi import asyncio as hlapi
        real_engine_cls = hlapi.SnmpEngine
        poller = PysnmpPoller(Config(snmp_timeout_s=0.05))

        async def two_walks():
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await poller.walk(_DEAD_TARGET)
            poller._engine.close_dispatcher()

        with mock.patch.object(hlapi, "SnmpEngine", side_effect=real_engine_cls) as ctor:
            asyncio.run(two_walks())
        self.assertEqual(ctor.call_count, 1)

def _vb(prefix, idx, val):
    return (f"{prefix}.{idx}", str(val))

class ParseIfTable(unittest.TestCase):
    def test_groups_columns_by_ifindex(self):
        vbs = [
            _vb(OID_IF_DESCR, 1, "GigabitEthernet0/1"), _vb(OID_IF_ADMIN, 1, 1),
            _vb(OID_IF_OPER, 1, 1), _vb(OID_IF_NAME, 1, "Gi0/1"),
            _vb(OID_IF_ALIAS, 1, "uplink to core"), _vb(OID_IF_LASTCHANGE, 1, 12345),
            _vb(OID_IF_DESCR, 2, "GigabitEthernet0/2"), _vb(OID_IF_ADMIN, 2, 1),
            _vb(OID_IF_OPER, 2, 2), _vb(OID_IF_NAME, 2, "Gi0/2"),
            _vb(OID_IF_ALIAS, 2, "-> Rampur backhaul"),
        ]
        ports = parse_if_table(vbs)
        self.assertEqual([p.if_index for p in ports], [1, 2])
        p1, p2 = ports
        self.assertEqual(p1.if_name, "Gi0/1")
        self.assertEqual(p1.if_alias, "uplink to core")
        self.assertEqual((p1.admin_status, p1.oper_status), ("up", "up"))
        self.assertEqual(p1.last_change, "12345")
        self.assertFalse(p1.is_down())
        self.assertEqual((p2.admin_status, p2.oper_status), ("up", "down"))
        self.assertTrue(p2.is_down())

    def test_ifname_falls_back_to_ifdescr(self):
        vbs = [_vb(OID_IF_DESCR, 5, "Vlan10"), _vb(OID_IF_ADMIN, 5, 1), _vb(OID_IF_OPER, 5, 1)]
        ports = parse_if_table(vbs)
        self.assertEqual(ports[0].if_name, "Vlan10")
        self.assertIsNone(ports[0].if_alias)

    def test_status_int_labels(self):
        vbs = [_vb(OID_IF_ADMIN, 3, 1), _vb(OID_IF_OPER, 3, 7)]
        p = parse_if_table(vbs)[0]
        self.assertEqual(p.oper_status, "lowerLayerDown")
        self.assertTrue(p.is_down())

    def test_admin_down_is_not_a_down_condition(self):
        p = PortStatus(1, "Gi0/3", None, admin_status="down", oper_status="down")
        self.assertFalse(p.is_down())

    def test_rows_without_status_are_skipped(self):
        vbs = [_vb(OID_IF_ALIAS, 9, "orphan")]
        self.assertEqual(parse_if_table(vbs), [])

    def test_captures_octet_counters_and_speed(self):
        vbs = [
            _vb(OID_IF_ADMIN, 1, 1), _vb(OID_IF_OPER, 1, 1),
            _vb(OID_IF_HCIN, 1, 1_000_000), _vb(OID_IF_HCOUT, 1, 2_000_000),
            _vb(OID_IF_HIGHSPEED, 1, 1000),
        ]
        p = parse_if_table(vbs)[0]
        self.assertEqual((p.in_octets, p.out_octets), (1_000_000, 2_000_000))
        self.assertEqual(p.speed_bps, 1_000_000_000)

    def test_speed_falls_back_to_ifspeed(self):
        vbs = [_vb(OID_IF_ADMIN, 2, 1), _vb(OID_IF_OPER, 2, 1),
               _vb(OID_IF_SPEED, 2, 100_000_000)]
        self.assertEqual(parse_if_table(vbs)[0].speed_bps, 100_000_000)

    def test_missing_counters_are_none(self):
        vbs = [_vb(OID_IF_ADMIN, 3, 1), _vb(OID_IF_OPER, 3, 1)]
        p = parse_if_table(vbs)[0]
        self.assertEqual((p.in_octets, p.out_octets, p.speed_bps), (None, None, None))

class ThroughputBps(unittest.TestCase):
    def test_normal_rate(self):
        self.assertEqual(throughput_bps(0, 12_500_000, 10.0), 10_000_000.0)

    def test_first_sample_has_no_rate(self):
        self.assertIsNone(throughput_bps(None, 12_500_000, 10.0))

    def test_nonpositive_interval_is_none(self):
        self.assertIsNone(throughput_bps(0, 12_500_000, 0.0))
        self.assertIsNone(throughput_bps(0, 12_500_000, -5.0))

    def test_counter_reset_emits_no_spike(self):
        self.assertIsNone(throughput_bps(9_000_000, 10_000, 10.0))

    def test_counter64_above_signed_range(self):
        prev, cur = 2 ** 63, 2 ** 63 + 1_000_000
        self.assertEqual(throughput_bps(prev, cur, 8.0), 1_000_000.0)

if __name__ == "__main__":
    unittest.main()
