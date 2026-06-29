"""Pure tests for the SNMP IF-MIB parser (ingress/snmp.parse_if_table) + the
down-condition predicate. No pysnmp, no network — exactly the boundary the suite is
meant to exercise (the real walk is mocked/injected, like the notifier double)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.ingress.snmp import (
    OID_IF_ADMIN, OID_IF_ALIAS, OID_IF_DESCR, OID_IF_HCIN, OID_IF_HCOUT,
    OID_IF_HIGHSPEED, OID_IF_LASTCHANGE, OID_IF_NAME, OID_IF_OPER, OID_IF_SPEED,
    PortStatus, parse_if_table, throughput_bps,
)


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
        self.assertEqual([p.if_index for p in ports], [1, 2])           # sorted by index
        p1, p2 = ports
        self.assertEqual(p1.if_name, "Gi0/1")
        self.assertEqual(p1.if_alias, "uplink to core")
        self.assertEqual((p1.admin_status, p1.oper_status), ("up", "up"))
        self.assertEqual(p1.last_change, "12345")
        self.assertFalse(p1.is_down())
        self.assertEqual((p2.admin_status, p2.oper_status), ("up", "down"))
        self.assertTrue(p2.is_down())                                    # admin up + oper down

    def test_ifname_falls_back_to_ifdescr(self):
        vbs = [_vb(OID_IF_DESCR, 5, "Vlan10"), _vb(OID_IF_ADMIN, 5, 1), _vb(OID_IF_OPER, 5, 1)]
        ports = parse_if_table(vbs)
        self.assertEqual(ports[0].if_name, "Vlan10")
        self.assertIsNone(ports[0].if_alias)

    def test_status_int_labels(self):
        # 7 = lowerLayerDown counts as a real port-down (underlying link gone).
        vbs = [_vb(OID_IF_ADMIN, 3, 1), _vb(OID_IF_OPER, 3, 7)]
        p = parse_if_table(vbs)[0]
        self.assertEqual(p.oper_status, "lowerLayerDown")
        self.assertTrue(p.is_down())

    def test_admin_down_is_not_a_down_condition(self):
        # admin-down = intentionally shut; never an alarm even with oper down.
        p = PortStatus(1, "Gi0/3", None, admin_status="down", oper_status="down")
        self.assertFalse(p.is_down())

    def test_rows_without_status_are_skipped(self):
        # an alias-only row (no admin/oper) isn't a usable port record.
        vbs = [_vb(OID_IF_ALIAS, 9, "orphan")]
        self.assertEqual(parse_if_table(vbs), [])

    def test_captures_octet_counters_and_speed(self):
        # ifXTable HC byte counters + ifHighSpeed (Mbps) feed the bandwidth tier.
        vbs = [
            _vb(OID_IF_ADMIN, 1, 1), _vb(OID_IF_OPER, 1, 1),
            _vb(OID_IF_HCIN, 1, 1_000_000), _vb(OID_IF_HCOUT, 1, 2_000_000),
            _vb(OID_IF_HIGHSPEED, 1, 1000),   # 1000 Mbps -> 1e9 bps
        ]
        p = parse_if_table(vbs)[0]
        self.assertEqual((p.in_octets, p.out_octets), (1_000_000, 2_000_000))
        self.assertEqual(p.speed_bps, 1_000_000_000)

    def test_speed_falls_back_to_ifspeed(self):
        # no ifHighSpeed -> use the 32-bit ifSpeed (already bits/sec).
        vbs = [_vb(OID_IF_ADMIN, 2, 1), _vb(OID_IF_OPER, 2, 1),
               _vb(OID_IF_SPEED, 2, 100_000_000)]
        self.assertEqual(parse_if_table(vbs)[0].speed_bps, 100_000_000)

    def test_missing_counters_are_none(self):
        vbs = [_vb(OID_IF_ADMIN, 3, 1), _vb(OID_IF_OPER, 3, 1)]
        p = parse_if_table(vbs)[0]
        self.assertEqual((p.in_octets, p.out_octets, p.speed_bps), (None, None, None))


class ThroughputBps(unittest.TestCase):
    def test_normal_rate(self):
        # 12.5 MB over 10s = 100,000,000 bits / 10s = 10 Mbps.
        self.assertEqual(throughput_bps(0, 12_500_000, 10.0), 10_000_000.0)

    def test_first_sample_has_no_rate(self):
        self.assertIsNone(throughput_bps(None, 12_500_000, 10.0))

    def test_nonpositive_interval_is_none(self):
        self.assertIsNone(throughput_bps(0, 12_500_000, 0.0))
        self.assertIsNone(throughput_bps(0, 12_500_000, -5.0))

    def test_counter_reset_emits_no_spike(self):
        # counter went backwards (reboot / wrap) -> None, not a phantom multi-Gbps reading.
        self.assertIsNone(throughput_bps(9_000_000, 10_000, 10.0))

    def test_counter64_above_signed_range(self):
        # Counter64 can exceed 2**63; Python ints handle the delta exactly.
        prev, cur = 2 ** 63, 2 ** 63 + 1_000_000
        self.assertEqual(throughput_bps(prev, cur, 8.0), 1_000_000.0)


if __name__ == "__main__":
    unittest.main()
