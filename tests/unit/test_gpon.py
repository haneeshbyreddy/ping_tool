import asyncio
import os
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))

from wisp.config import Config
from wisp.ingress.gpon import (
    DBC, HUAWEI, GponPollerPool, GponProfile, OnuOptic, PROFILES, build_gpon_poller,
    parse_onu_table, PysnmpGponPoller, STATE_ONLINE, STATE_OFFLINE,
)
from wisp.ingress.snmp import SnmpTarget
from apps.daemon.main import _gather_onu_optics

def _vb(profile: GponProfile, idx: str, *, rx=None, state=None, serial=None,
        distance=None, name=None):
    out = []
    if rx is not None:
        out.append((f"{profile.oid_rx}.{idx}", str(rx)))
    if state is not None:
        out.append((f"{profile.oid_state}.{idx}", str(state)))
    if serial is not None:
        out.append((f"{profile.oid_serial}.{idx}", serial))
    if distance is not None:
        out.append((f"{profile.oid_distance}.{idx}", str(distance)))
    if name is not None:
        out.append((f"{profile.oid_name}.{idx}", name))
    return out

class ParseTest(unittest.TestCase):
    def test_folds_columns_into_one_onu_per_row(self):
        vbs = (_vb(HUAWEI, "10.1", rx=-1920, state=1, serial="HWTC1", distance=3820, name="Ravi")
               + _vb(HUAWEI, "10.2", rx=-2980, state=1, serial="HWTC2", distance=4100))
        onus = {o.onu_key: o for o in parse_onu_table(vbs, HUAWEI)}
        self.assertEqual(len(onus), 2)
        a = onus["HWTC1"]
        self.assertEqual(a.rx_dbm, -19.2)
        self.assertEqual(a.state, STATE_ONLINE)
        self.assertEqual(a.distance_m, 3820)
        self.assertEqual(a.name, "Ravi")
        self.assertEqual(a.onu_id, 1)
        self.assertEqual(a.pon_port, "10")
        self.assertEqual(onus["HWTC2"].rx_dbm, -29.8)

    def test_serial_is_the_onu_key_else_index(self):
        onus = parse_onu_table(_vb(HUAWEI, "12.5", rx=-2000, state=1), HUAWEI)
        self.assertEqual(onus[0].onu_key, "12.5")

    def test_state_decode_and_offline_without_rx(self):
        onus = {o.onu_key: o for o in parse_onu_table(
            _vb(HUAWEI, "10.9", state=2, serial="OFF"), HUAWEI)}
        self.assertEqual(onus["OFF"].state, STATE_OFFLINE)
        self.assertIsNone(onus["OFF"].rx_dbm)

    def test_dbc_decimal_rx_and_mac_key(self):
        idx = "12"
        vbs = _vb(DBC, idx, rx="-14.62", serial="00:11:22:33:44:55")
        onus = parse_onu_table(vbs, DBC)
        self.assertEqual(len(onus), 1)
        o = onus[0]
        self.assertEqual(o.onu_key, "00:11:22:33:44:55")
        self.assertEqual(o.serial, "00:11:22:33:44:55")
        self.assertEqual(o.rx_dbm, -14.62)
        self.assertEqual(o.state, STATE_ONLINE)

    def test_dbc_joins_pon_onu_from_master_table(self):
        vbs = [
            (f"{DBC.oid_serial}.2", "98:2F:3C:B9:42:F8"),
            (f"{DBC.oid_rx}.2", "-14.62"),
            (f"{DBC.oid_ident_key}.1", "98:2f:3c:b9:42:f8"),
            (f"{DBC.oid_ident_pon}.1", "1"),
            (f"{DBC.oid_ident_onu}.1", "2"),
            (f"{DBC.oid_ident_key}.77", "aa:bb:cc:dd:ee:ff"),
            (f"{DBC.oid_ident_pon}.77", "3"),
            (f"{DBC.oid_ident_onu}.77", "9"),
        ]
        onus = parse_onu_table(vbs, DBC)
        self.assertEqual(len(onus), 1)
        o = onus[0]
        self.assertEqual(o.onu_key, "98:2F:3C:B9:42:F8")
        self.assertEqual(o.rx_dbm, -14.62)
        self.assertEqual(o.state, STATE_ONLINE)
        self.assertEqual(o.pon_port, "EPON0/1")
        self.assertEqual(o.onu_id, 2)

    def test_dbc_duplicate_mac_resolved_by_onu_id(self):
        vbs = [
            (f"{DBC.oid_serial}.23", "80:B5:75:20:98:BA"),
            (f"{DBC.oid_rx}.23", "-14.53"),
            (f"{DBC.oid_ident_key}.22", "80:b5:75:20:98:ba"),
            (f"{DBC.oid_ident_pon}.22", "1"),
            (f"{DBC.oid_ident_onu}.22", "23"),
            (f"{DBC.oid_ident_key}.101", "80:b5:75:20:98:ba"),
            (f"{DBC.oid_ident_pon}.101", "3"),
            (f"{DBC.oid_ident_onu}.101", "51"),
        ]
        o = parse_onu_table(vbs, DBC)[0]
        self.assertEqual(o.pon_port, "EPON0/1")
        self.assertEqual(o.onu_id, 23)

    def test_dbc_without_master_row_falls_back_to_index(self):
        onus = parse_onu_table(_vb(DBC, "7", rx="-15.0", serial="DE:AD:BE:EF:00:07"), DBC)
        self.assertEqual(onus[0].onu_id, 7)
        self.assertEqual(onus[0].pon_port, "7")

    def test_to_wire_roundtrips(self):
        w = OnuOptic("K", pon_port="0/6", onu_id=3, rx_dbm=-25.1, state="online").to_wire()
        self.assertEqual(w["onu_key"], "K")
        self.assertEqual(w["rx_dbm"], -25.1)
        self.assertEqual(w["pon_port"], "0/6")

class BuildTest(unittest.TestCase):
    def test_unknown_vendor_falls_back_to_huawei(self):
        poller = build_gpon_poller(Config(gpon_vendor="acme-optics"))
        self.assertIsInstance(poller, PysnmpGponPoller)
        self.assertIs(poller.profile, HUAWEI)

    def test_known_vendor_selected(self):
        self.assertIs(build_gpon_poller(Config(gpon_vendor="huawei")).profile, HUAWEI)

    def test_dbc_vendor_resolves_to_dbc_profile(self):
        self.assertIs(build_gpon_poller(Config(gpon_vendor="dbc")).profile, DBC)

class _RecordingFactory:
    def __init__(self):
        self.calls: list[tuple[GponProfile, Config]] = []

    def __call__(self, profile: GponProfile, cfg: Config):
        self.calls.append((profile, cfg))
        return object()

class PoolTest(unittest.TestCase):
    def test_caches_one_poller_per_vendor(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        a = pool.for_vendor("huawei")
        b = pool.for_vendor("huawei")
        self.assertIs(a, b)
        self.assertEqual(len(f.calls), 1)
        self.assertIs(f.calls[0][0], HUAWEI)

    def test_empty_vendor_falls_back_to_cfg_then_shares(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(gpon_vendor="huawei"), factory=f)
        self.assertIs(pool.for_vendor(None), pool.for_vendor(""))
        self.assertIs(pool.for_vendor(None), pool.for_vendor("huawei"))
        self.assertEqual(len(f.calls), 1)

    def test_unknown_vendor_shares_the_huawei_poller(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        self.assertIs(pool.for_vendor("acme-optics"), pool.for_vendor("huawei"))
        self.assertEqual(len(f.calls), 1)

    def test_dbc_resolves_to_dbc_profile(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        hw, db = pool.for_vendor("huawei"), pool.for_vendor("dbc")
        self.assertIsNot(hw, db)
        self.assertEqual({c[0].name for c in f.calls}, {"huawei", "dbc"})

    def test_distinct_vendors_get_distinct_pollers(self):
        f = _RecordingFactory()
        zte = GponProfile(name="zte", oid_rx="1.3.6.1.4.1.3902.1")
        with mock.patch.dict(PROFILES, {"zte": zte}):
            pool = GponPollerPool(Config(), factory=f)
            hw, zt = pool.for_vendor("huawei"), pool.for_vendor("zte")
        self.assertIsNot(hw, zt)
        self.assertEqual({c[0].name for c in f.calls}, {"huawei", "zte"})

try:
    import pysnmp
    _HAS_PYSNMP = True
except ImportError:
    _HAS_PYSNMP = False

@unittest.skipUnless(_HAS_PYSNMP, "pysnmp not installed")
class EngineReuseTest(unittest.TestCase):

    def test_one_engine_across_walks(self):
        from pysnmp.hlapi import asyncio as hlapi
        real_engine_cls = hlapi.SnmpEngine
        poller = PysnmpGponPoller(HUAWEI, Config(snmp_timeout_s=0.05))
        target = SnmpTarget(ip="127.0.0.1", community="public", port=1)

        async def two_walks():
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await poller.walk(target)
            poller._engine.close_dispatcher()

        with mock.patch.object(hlapi, "SnmpEngine", side_effect=real_engine_cls) as ctor:
            asyncio.run(two_walks())
        self.assertEqual(ctor.call_count, 1)

class _FakePoller:
    def __init__(self, by_ip):
        self.by_ip = by_ip
        self.walked = []

    async def walk(self, target: SnmpTarget):
        self.walked.append(target.ip)
        return self.by_ip.get(target.ip, [])

class _OnePool:
    def __init__(self, poller):
        self.poller = poller
        self.asked: list = []

    def for_vendor(self, vendor):
        self.asked.append(vendor)
        return self.poller

class GatherTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_only_snmp_enabled_olts_are_walked(self):
        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "device_type": "OLT", "snmp_enabled": 1},
            {"id": 2, "ip_address": "10.0.0.2", "device_type": "OLT", "snmp_enabled": 0},
            {"id": 3, "ip_address": "10.0.0.3", "device_type": "switch", "snmp_enabled": 1},
        ]
        poller = _FakePoller({"10.0.0.1": [OnuOptic("K1", rx_dbm=-20.0, state="online")]})
        out = self._run(_gather_onu_optics(_OnePool(poller), devices, Config()))
        self.assertEqual(set(out), {1})
        self.assertEqual(poller.walked, ["10.0.0.1"])
        self.assertEqual(out[1][0]["onu_key"], "K1")

    def test_one_dead_olt_never_sinks_the_others(self):
        class Flaky(_FakePoller):
            async def walk(self, target):
                if target.ip == "10.0.0.9":
                    raise RuntimeError("GPON walk boom")
                return await super().walk(target)
        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "device_type": "OLT", "snmp_enabled": 1},
            {"id": 9, "ip_address": "10.0.0.9", "device_type": "OLT", "snmp_enabled": 1},
        ]
        poller = Flaky({"10.0.0.1": [OnuOptic("K1", rx_dbm=-20.0, state="online")]})
        out = self._run(_gather_onu_optics(_OnePool(poller), devices, Config()))
        self.assertEqual(set(out), {1})

    def test_each_olt_walked_with_its_own_vendor_poller(self):
        huawei = _FakePoller({"10.0.0.1": [OnuOptic("HW", state="online")]})
        zte = _FakePoller({"10.0.0.2": [OnuOptic("ZT", state="online")]})

        class _RoutingPool:
            def for_vendor(self, vendor):
                return zte if (vendor or "").lower() == "zte" else huawei

        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "device_type": "OLT", "snmp_enabled": 1,
             "gpon_vendor": None},
            {"id": 2, "ip_address": "10.0.0.2", "device_type": "OLT", "snmp_enabled": 1,
             "gpon_vendor": "zte"},
        ]
        out = self._run(_gather_onu_optics(_RoutingPool(), devices, Config()))
        self.assertEqual(out[1][0]["onu_key"], "HW")
        self.assertEqual(out[2][0]["onu_key"], "ZT")
        self.assertEqual(huawei.walked, ["10.0.0.1"])
        self.assertEqual(zte.walked, ["10.0.0.2"])

if __name__ == "__main__":
    unittest.main()
