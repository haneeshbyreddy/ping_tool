import asyncio
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))

from wisp.config import Config
from wisp.ingress.health import (
    DeviceHealth, OID_ENT_SENSOR_PRECISION, OID_ENT_SENSOR_SCALE, OID_ENT_SENSOR_TYPE,
    OID_ENT_SENSOR_VALUE, OID_FH_HEALTH, OID_FH_MEM, OID_HR_CPU_LOAD, OID_HR_STORAGE_SIZE,
    OID_HR_STORAGE_TYPE, OID_HR_STORAGE_UNITS, OID_HR_STORAGE_USED, OID_MTXR_HEALTH,
    PysnmpHealthPoller, build_health_poller, parse_health,
)
from apps.daemon.main import _gather_snmp_health

def _ram_rows(idx="65536", units=1024, size=262144, used=95000):
    return [
        (f"{OID_HR_STORAGE_TYPE}.{idx}", "1.3.6.1.2.1.25.2.1.2"),
        (f"{OID_HR_STORAGE_UNITS}.{idx}", str(units)),
        (f"{OID_HR_STORAGE_SIZE}.{idx}", str(size)),
        (f"{OID_HR_STORAGE_USED}.{idx}", str(used)),
    ]

class ParseTest(unittest.TestCase):
    def test_cpu_is_the_average_across_cores(self):
        h = parse_health([(f"{OID_HR_CPU_LOAD}.1", "10"),
                          (f"{OID_HR_CPU_LOAD}.2", "30")])
        self.assertEqual(h.cpu_pct, 20.0)

    def test_cpu_ignores_out_of_range_rows(self):
        h = parse_health([(f"{OID_HR_CPU_LOAD}.1", "40"),
                          (f"{OID_HR_CPU_LOAD}.2", "5000"),
                          (f"{OID_HR_CPU_LOAD}.3", "garbage")])
        self.assertEqual(h.cpu_pct, 40.0)

    def test_memory_picks_the_ram_typed_row(self):
        disk = [
            (f"{OID_HR_STORAGE_TYPE}.31", "1.3.6.1.2.1.25.2.1.4"),  # fixed disk
            (f"{OID_HR_STORAGE_UNITS}.31", "4096"),
            (f"{OID_HR_STORAGE_SIZE}.31", "999999"),
            (f"{OID_HR_STORAGE_USED}.31", "999998"),
        ]
        h = parse_health(disk + _ram_rows())
        self.assertEqual(h.mem_total_bytes, 262144 * 1024)
        self.assertEqual(h.mem_used_bytes, 95000 * 1024)
        self.assertAlmostEqual(h.mem_pct, 36.2, places=1)

    def test_memory_accepts_named_ram_type(self):
        rows = _ram_rows()
        rows[0] = (rows[0][0], "HOST-RESOURCES-TYPES::hrStorageRam")
        h = parse_health(rows)
        self.assertIsNotNone(h.mem_total_bytes)

    def test_entity_sensor_celsius_takes_the_hottest(self):
        vbs = []
        for idx, val in (("1", "43"), ("2", "61"), ("3", "999")):  # 999 implausible
            vbs += [(f"{OID_ENT_SENSOR_TYPE}.{idx}", "8"),
                    (f"{OID_ENT_SENSOR_SCALE}.{idx}", "9"),
                    (f"{OID_ENT_SENSOR_PRECISION}.{idx}", "0"),
                    (f"{OID_ENT_SENSOR_VALUE}.{idx}", val)]
        # a non-celsius sensor (volts=4) must be ignored even with a big value
        vbs += [(f"{OID_ENT_SENSOR_TYPE}.9", "4"),
                (f"{OID_ENT_SENSOR_VALUE}.9", "120")]
        h = parse_health(vbs)
        self.assertEqual(h.temp_c, 61.0)

    def test_entity_sensor_applies_precision(self):
        vbs = [(f"{OID_ENT_SENSOR_TYPE}.1", "8"),
               (f"{OID_ENT_SENSOR_SCALE}.1", "9"),
               (f"{OID_ENT_SENSOR_PRECISION}.1", "1"),
               (f"{OID_ENT_SENSOR_VALUE}.1", "435")]
        self.assertEqual(parse_health(vbs).temp_c, 43.5)

    def test_mikrotik_temp_in_tenths_and_whole_degrees(self):
        self.assertEqual(parse_health([(f"{OID_MTXR_HEALTH}.10.0", "340")]).temp_c, 34.0)
        self.assertEqual(parse_health([(f"{OID_MTXR_HEALTH}.11.0", "48")]).temp_c, 48.0)

    def test_entity_sensor_wins_over_mikrotik(self):
        vbs = [(f"{OID_ENT_SENSOR_TYPE}.1", "8"),
               (f"{OID_ENT_SENSOR_SCALE}.1", "9"),
               (f"{OID_ENT_SENSOR_PRECISION}.1", "0"),
               (f"{OID_ENT_SENSOR_VALUE}.1", "50"),
               (f"{OID_MTXR_HEALTH}.10.0", "990")]
        self.assertEqual(parse_health(vbs).temp_c, 50.0)

    def test_fiberhome_private_tree_fills_all_three(self):
        # An S3330-class switch exposes none of the standard MIBs; its 5651 tree does.
        vbs = [(f"{OID_FH_HEALTH}.1.0", "69"),   # mem %
               (f"{OID_FH_HEALTH}.2.0", "10"),   # cpu %
               (f"{OID_FH_HEALTH}.3.0", "21"),   # temp C
               (f"{OID_FH_MEM}.8.0", "485363712"),
               (f"{OID_FH_MEM}.5.0", "334450688")]
        h = parse_health(vbs)
        self.assertEqual(h.cpu_pct, 10.0)
        self.assertEqual(h.temp_c, 21.0)
        self.assertEqual(h.mem_total_bytes, 485363712)
        self.assertEqual(h.mem_used_bytes, 334450688)
        self.assertAlmostEqual(h.mem_pct, 68.9, places=1)

    def test_standard_mibs_win_over_fiberhome(self):
        # A box that answers both keeps the standard reading, not the vendor fallback.
        vbs = _ram_rows() + [(f"{OID_HR_CPU_LOAD}.1", "12"),
                             (f"{OID_FH_HEALTH}.2.0", "99"),
                             (f"{OID_FH_MEM}.8.0", "1"), (f"{OID_FH_MEM}.5.0", "1")]
        h = parse_health(vbs)
        self.assertEqual(h.cpu_pct, 12.0)
        self.assertEqual(h.mem_total_bytes, 262144 * 1024)

    def test_empty_walk_is_empty_health(self):
        h = parse_health([])
        self.assertTrue(h.is_empty())
        self.assertIsNone(h.mem_pct)

    def test_to_wire_carries_derived_mem_pct(self):
        h = parse_health(_ram_rows() + [(f"{OID_HR_CPU_LOAD}.1", "12")])
        wire = h.to_wire()
        self.assertEqual(wire["cpu_pct"], 12.0)
        self.assertAlmostEqual(wire["mem_pct"], 36.2, places=1)
        self.assertIsNone(wire["temp_c"])

def _profile(metrics, match="1.3.6.1.4.1.9999"):
    return {"name": "test-vendor", "match_sysobjectid": match, "metrics": metrics}


class ProfileTest(unittest.TestCase):
    def test_profile_fills_gaps_the_standard_mibs_left(self):
        vbs = [("1.3.6.1.4.1.9999.1.2.0", "37"),      # vendor cpu
               ("1.3.6.1.4.1.9999.1.3.0", "425")]     # vendor temp, tenths
        p = _profile({"cpu_pct": {"oid": "1.3.6.1.4.1.9999.1.2.0", "decode": "as_is"},
                      "temp_c": {"oid": "1.3.6.1.4.1.9999.1.3.0", "decode": "div10"}})
        h = parse_health(vbs, p)
        self.assertEqual(h.cpu_pct, 37.0)
        self.assertEqual(h.temp_c, 42.5)

    def test_standard_mibs_win_over_the_profile(self):
        vbs = [(f"{OID_HR_CPU_LOAD}.1", "10"),
               ("1.3.6.1.4.1.9999.1.2.0", "99")]
        p = _profile({"cpu_pct": {"oid": "1.3.6.1.4.1.9999.1.2.0", "decode": "as_is"}})
        self.assertEqual(parse_health(vbs, p).cpu_pct, 10.0)

    def test_signed_div100_decodes_negative_readings(self):
        # 65036 -> (65036 - 65536) / 100 = -5.0 (the classic signed-16-bit-in-
        # hundredths encoding; same decode covers optical dBm columns).
        vbs = [("1.3.6.1.4.1.9999.7.1", "65036")]
        p = _profile({"temp_c": {"oid": "1.3.6.1.4.1.9999.7",
                                 "decode": "signed_div100", "select": "first"}})
        self.assertEqual(parse_health(vbs, p).temp_c, -5.0)

    def test_select_folds_column_rows(self):
        vbs = [("1.3.6.1.4.1.9999.5.1", "10"),
               ("1.3.6.1.4.1.9999.5.2", "30")]
        for how, want in (("avg", 20.0), ("max", 30.0), ("first", 10.0)):
            p = _profile({"cpu_pct": {"oid": "1.3.6.1.4.1.9999.5",
                                      "decode": "as_is", "select": how}})
            self.assertEqual(parse_health(vbs, p).cpu_pct, want, how)

    def test_direct_mem_pct_when_no_byte_counters(self):
        vbs = [("1.3.6.1.4.1.9999.6.0", "61")]
        p = _profile({"mem_pct": {"oid": "1.3.6.1.4.1.9999.6.0", "decode": "as_is"}})
        h = parse_health(vbs, p)
        self.assertEqual(h.mem_pct, 61.0)
        self.assertFalse(h.is_empty())
        self.assertEqual(h.to_wire()["mem_pct"], 61.0)

    def test_implausible_profile_values_are_dropped(self):
        vbs = [("1.3.6.1.4.1.9999.1.2.0", "900"),   # cpu > 100
               ("1.3.6.1.4.1.9999.1.3.0", "900")]   # temp > ceiling
        p = _profile({"cpu_pct": {"oid": "1.3.6.1.4.1.9999.1.2.0", "decode": "as_is"},
                      "temp_c": {"oid": "1.3.6.1.4.1.9999.1.3.0", "decode": "as_is"}})
        h = parse_health(vbs, p)
        self.assertIsNone(h.cpu_pct)
        self.assertIsNone(h.temp_c)


class ProfileMatchTest(unittest.TestCase):
    def test_longest_sysobjectid_prefix_wins(self):
        from wisp.ingress.health import OID_SYS_OBJECT_ID, match_profile, sys_object_id
        vbs = [(f"{OID_SYS_OBJECT_ID}.0", "1.3.6.1.4.1.5651.3.2")]
        soid = sys_object_id(vbs)
        self.assertEqual(soid, "1.3.6.1.4.1.5651.3.2")
        vendor_wide = _profile({}, match="1.3.6.1.4.1.5651")
        model_specific = _profile({}, match="1.3.6.1.4.1.5651.3")
        other = _profile({}, match="1.3.6.1.4.1.14988")
        picked = match_profile([vendor_wide, other, model_specific], soid)
        self.assertIs(picked, model_specific)
        self.assertIsNone(match_profile([other], soid))

    def test_sysobjectid_normalises_the_mib_rendered_form(self):
        from wisp.ingress.health import OID_SYS_OBJECT_ID, sys_object_id
        vbs = [(f"{OID_SYS_OBJECT_ID}.0", "SNMPv2-SMI::enterprises.5651.3.2")]
        self.assertEqual(sys_object_id(vbs), "1.3.6.1.4.1.5651.3.2")

    def test_scalar_metric_oids_walk_their_parent(self):
        from wisp.ingress.health import profile_walk_roots
        p = _profile({
            "cpu_pct": {"oid": "1.3.6.1.4.1.9999.1.2.0", "decode": "as_is"},
            "temp_c": {"oid": "1.3.6.1.4.1.9999.1.3.0", "decode": "as_is"},
            "mem_pct": {"oid": "1.3.6.1.4.1.9999.6", "decode": "as_is"}})
        roots = profile_walk_roots(p)
        self.assertIn("1.3.6.1.4.1.9999.1.2", roots)
        self.assertIn("1.3.6.1.4.1.9999.6", roots)
        self.assertNotIn("1.3.6.1.4.1.9999.1.2.0", roots)


class _FakeHealthPoller:
    def __init__(self, by_ip):
        self.by_ip = by_ip
        self.walked = []

    async def walk(self, target, profiles=None):
        self.walked.append(target.ip)
        result = self.by_ip[target.ip]
        if isinstance(result, Exception):
            raise result
        return result

class GatherTest(unittest.TestCase):
    def test_gather_skips_disabled_empty_and_failing_devices(self):
        cfg = Config()
        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "snmp_enabled": 1,
             "snmp_community": "public"},
            {"id": 2, "ip_address": "10.0.0.2", "snmp_enabled": 0},
            {"id": 3, "ip_address": "10.0.0.3", "snmp_enabled": 1,
             "snmp_community": "public"},
            {"id": 4, "ip_address": "10.0.0.4", "snmp_enabled": 1,
             "snmp_community": "public"},
        ]
        poller = _FakeHealthPoller({
            "10.0.0.1": DeviceHealth(cpu_pct=33.0, temp_c=52.0),
            "10.0.0.3": RuntimeError("boom"),
            "10.0.0.4": DeviceHealth(),  # nothing exposed -> dropped
        })
        out = asyncio.run(_gather_snmp_health(poller, devices, cfg))
        self.assertEqual(set(out), {1})
        self.assertEqual(out[1]["cpu_pct"], 33.0)
        self.assertEqual(out[1]["temp_c"], 52.0)
        self.assertNotIn("10.0.0.2", poller.walked)

try:
    import pysnmp  # noqa: F401
    _HAS_PYSNMP = True
except ImportError:
    _HAS_PYSNMP = False

@unittest.skipUnless(_HAS_PYSNMP, "pysnmp not installed")
class EngineReuseTest(unittest.TestCase):
    def test_poller_reuses_one_engine(self):
        poller = build_health_poller(Config())
        self.assertIsInstance(poller, PysnmpHealthPoller)
        self.assertIsNone(poller._engine)

        async def run():
            from unittest import mock
            engines = []
            real_walk = []

            class _Boom(Exception):
                pass

            with mock.patch("pysnmp.hlapi.asyncio.UdpTransportTarget.create",
                            side_effect=_Boom):
                from wisp.ingress.snmp import SnmpTarget
                for _ in range(2):
                    try:
                        await poller.walk(SnmpTarget(ip="127.0.0.1", community="x"))
                    except RuntimeError:
                        pass
                    engines.append(poller._engine)
            return engines

        engines = asyncio.run(run())
        self.assertIsNotNone(engines[0])
        self.assertIs(engines[0], engines[1])

if __name__ == "__main__":
    unittest.main()
