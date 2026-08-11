import asyncio
import json
import dataclasses
import os
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))

from wisp.config import Config
from wisp.ingress.gpon import (
    DBC, HUAWEI, GponPollerPool, GponProfile, OnuOptic, PROFILES,
    gpon_profile_from_dict, match_gpon_profile, parse_onu_table, PysnmpGponPoller,
    STATE_ONLINE, STATE_OFFLINE, STATE_UNKNOWN,
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

DBC_RX = dataclasses.replace(
    DBC,
    oid_rx="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.3",
    oid_serial="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.2",
)

class ParseTest(unittest.TestCase):
    def test_shipping_dbc_profile_has_no_rx_column(self):
        self.assertEqual(DBC.oid_rx, "")
        self.assertEqual(DBC.oid_serial, "")

    def test_folds_columns_into_one_onu_per_row(self):
        vbs = (_vb(HUAWEI, "10.1", rx=-1920, state=1, serial="HWTC1", distance=3820, name="Ravi")
               + _vb(HUAWEI, "10.2", rx=-2980, state=1, serial="HWTC2", distance=4100))
        onus = {o.onu_key: o for o in parse_onu_table(vbs, HUAWEI)}
        self.assertEqual(len(onus), 2)
        a = onus["10.1"]
        self.assertEqual(a.serial, "HWTC1")
        self.assertEqual(a.rx_dbm, -19.2)
        self.assertEqual(a.state, STATE_ONLINE)
        self.assertEqual(a.distance_m, 3820)
        self.assertEqual(a.name, "Ravi")
        self.assertEqual(a.onu_id, 1)
        self.assertEqual(a.pon_port, "10")
        self.assertEqual(onus["10.2"].rx_dbm, -29.8)

    def test_the_slot_index_is_the_onu_key_even_when_a_serial_is_walked(self):
        onus = parse_onu_table(_vb(HUAWEI, "12.5", rx=-2000, state=1, serial="HWTC5"),
                               HUAWEI)
        self.assertEqual(onus[0].onu_key, "12.5")
        self.assertEqual(onus[0].serial, "HWTC5")

    def test_one_serial_on_two_slots_stays_two_rows(self):
        vbs = (_vb(HUAWEI, "6.34", rx=-2210, state=1, serial="ALCLb3fa1294")
               + _vb(HUAWEI, "7.64", rx=0, state=2, serial="ALCLb3fa1294"))
        onus = {o.onu_key: o for o in parse_onu_table(vbs, HUAWEI)}
        self.assertEqual(set(onus), {"6.34", "7.64"})
        self.assertEqual(onus["6.34"].state, STATE_ONLINE)
        self.assertEqual(onus["6.34"].rx_dbm, -22.1)
        self.assertEqual(onus["7.64"].state, STATE_OFFLINE)
        self.assertEqual({o.serial for o in onus.values()}, {"ALCLb3fa1294"})

    def test_state_decode_and_offline_without_rx(self):
        onus = {o.onu_key: o for o in parse_onu_table(
            _vb(HUAWEI, "10.9", state=2, serial="OFF"), HUAWEI)}
        self.assertEqual(onus["10.9"].state, STATE_OFFLINE)
        self.assertIsNone(onus["10.9"].rx_dbm)

    SYROTECH = gpon_profile_from_dict({
        "name": "syrotech_gpon",
        "oids": {"state": "1.3.6.1.4.1.37950.1.1.6.1.1.1.1.5",
                 "tx": "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.6",
                 "rx": "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7",
                 "serial": "1.3.6.1.4.1.37950.1.1.6.1.1.2.1.5"},
        "scales": {"rx": 1.0, "tx": 1.0},
        "state_map": {"3": "online"}, "state_default": "offline",
        "pon_index": "first_segment",
    })

    def test_a_row_with_NO_state_cell_is_unknown_never_offline(self):
        p = self.SYROTECH
        vbs = _vb(p, "7.41", rx="-21.00", serial="ZTEGcbd6530f")
        onus = {o.onu_key: o for o in parse_onu_table(vbs, p)}
        self.assertEqual(onus["7.41"].state, "unknown")
        self.assertEqual(onus["7.41"].rx_dbm, -21.0)
        self.assertEqual(onus["7.41"].serial, "ZTEGcbd6530f")

    def test_an_UNRECOGNISED_state_value_still_takes_the_profile_default(self):
        onus = {o.onu_key: o for o in parse_onu_table(
            _vb(self.SYROTECH, "7.42", state=7, serial="X"), self.SYROTECH)}
        self.assertEqual(onus["7.42"].state, STATE_OFFLINE)

    def test_a_profile_mapping_NO_state_column_keeps_its_default(self):
        stateless = dataclasses.replace(self.SYROTECH, oid_state="")
        onus = parse_onu_table([(f"{stateless.oid_rx}.3.1", "-20.00")], stateless)
        self.assertEqual(onus[0].state, STATE_OFFLINE)

    def test_dbc_decimal_rx_and_slot_key_carrying_the_mac(self):
        idx = "12"
        vbs = _vb(DBC_RX, idx, rx="-14.62", serial="00:11:22:33:44:55")
        onus = parse_onu_table(vbs, DBC_RX)
        self.assertEqual(len(onus), 1)
        o = onus[0]
        self.assertEqual(o.onu_key, "12")
        self.assertEqual(o.serial, "00:11:22:33:44:55")
        self.assertEqual(o.rx_dbm, -14.62)
        self.assertEqual(o.state, STATE_ONLINE)

    def test_dbc_enumerates_whole_roster_and_joins_rx_by_mac(self):
        vbs = [
            (f"{DBC_RX.oid_serial}.2", "98:2F:3C:B9:42:F8"),
            (f"{DBC_RX.oid_rx}.2", "-14.62"),
            (f"{DBC_RX.oid_ident_key}.1", "98:2f:3c:b9:42:f8"),
            (f"{DBC_RX.oid_ident_pon}.1", "1"),
            (f"{DBC_RX.oid_ident_onu}.1", "2"),
            (f"{DBC_RX.oid_ident_state}.1", "1"),
            (f"{DBC_RX.oid_ident_key}.77", "aa:bb:cc:dd:ee:ff"),
            (f"{DBC_RX.oid_ident_pon}.77", "3"),
            (f"{DBC_RX.oid_ident_onu}.77", "9"),
            (f"{DBC_RX.oid_ident_state}.77", "0"),
        ]
        onus = {o.onu_key: o for o in parse_onu_table(vbs, DBC_RX)}
        self.assertEqual(set(onus), {"1.2", "3.9"})
        lit = onus["1.2"]
        self.assertEqual(lit.pon_port, "EPON0/1")
        self.assertEqual(lit.onu_id, 2)
        self.assertEqual(lit.serial, "98:2F:3C:B9:42:F8")
        self.assertEqual(lit.rx_dbm, -14.62)
        self.assertEqual(lit.state, STATE_ONLINE)
        dark = onus["3.9"]
        self.assertEqual(dark.pon_port, "EPON0/3")
        self.assertEqual(dark.state, STATE_OFFLINE)
        self.assertIsNone(dark.rx_dbm)

    def test_dbc_walks_description_and_filters_null_sentinel(self):
        self.assertTrue(DBC.oid_ident_name)
        vbs = [
            (f"{DBC.oid_ident_key}.29", "00:d3:9e:14:35:84"),
            (f"{DBC.oid_ident_pon}.29", "2"),
            (f"{DBC.oid_ident_onu}.29", "1"),
            (f"{DBC.oid_ident_state}.29", "1"),
            (f"{DBC.oid_ident_name}.29", "HCS_RAMPRASAD"),
            (f"{DBC.oid_ident_key}.30", "4c:ae:1c:22:2c:fe"),
            (f"{DBC.oid_ident_pon}.30", "2"),
            (f"{DBC.oid_ident_onu}.30", "2"),
            (f"{DBC.oid_ident_state}.30", "1"),
            (f"{DBC.oid_ident_name}.30", "NULL"),
        ]
        onus = {o.onu_key: o for o in parse_onu_table(vbs, DBC)}
        self.assertEqual(onus["2.1"].name, "HCS_RAMPRASAD")
        self.assertIsNone(onus["2.2"].name)

    def test_dbc_reregistered_mac_stays_two_distinct_slots(self):
        vbs = [
            (f"{DBC_RX.oid_serial}.23", "80:B5:75:20:98:BA"),
            (f"{DBC_RX.oid_rx}.23", "-14.53"),
            (f"{DBC_RX.oid_ident_key}.22", "80:b5:75:20:98:ba"),
            (f"{DBC_RX.oid_ident_pon}.22", "1"),
            (f"{DBC_RX.oid_ident_onu}.22", "23"),
            (f"{DBC_RX.oid_ident_key}.101", "80:b5:75:20:98:ba"),
            (f"{DBC_RX.oid_ident_pon}.101", "3"),
            (f"{DBC_RX.oid_ident_onu}.101", "51"),
        ]
        onus = {o.onu_key: o for o in parse_onu_table(vbs, DBC_RX)}
        self.assertEqual(set(onus), {"1.23", "3.51"})
        self.assertEqual(onus["1.23"].rx_dbm, -14.53)
        self.assertIsNone(onus["3.51"].rx_dbm)

    def test_dbc_without_master_row_falls_back_to_index(self):
        onus = parse_onu_table(_vb(DBC_RX, "7", rx="-15.0", serial="DE:AD:BE:EF:00:07"), DBC_RX)
        self.assertEqual(onus[0].onu_id, 7)
        self.assertEqual(onus[0].pon_port, "7")

    def test_to_wire_roundtrips(self):
        w = OnuOptic("K", pon_port="0/6", onu_id=3, rx_dbm=-25.1, state="online").to_wire()
        self.assertEqual(w["onu_key"], "K")
        self.assertEqual(w["rx_dbm"], -25.1)
        self.assertEqual(w["pon_port"], "0/6")

class MatchProfileTest(unittest.TestCase):
    def test_known_arcs_match(self):
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.2011.2.184"), HUAWEI)
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950.1.1.5"), DBC)
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950"), DBC)

    def test_unclaimed_arc_and_empty_yield_none(self):
        self.assertIsNone(match_gpon_profile("1.3.6.1.4.1.9.1.1"))
        self.assertIsNone(match_gpon_profile(""))
        self.assertIsNone(match_gpon_profile(None))
        self.assertIsNone(match_gpon_profile("1.3.6.1.4.1.20112.1"))

    def test_longest_prefix_wins_model_specific_beats_vendor_wide(self):
        model = GponProfile(name="dbc-pylon", oid_rx="1.3.6.1.4.1.37950.9",
                            match_sysobjectid="1.3.6.1.4.1.37950.1.1")
        with mock.patch.dict(PROFILES, {"dbc-pylon": model}):
            self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950.1.1.5"), model)
            self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950.2"), DBC)

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

    def test_unknown_vendor_yields_no_poller_never_guesses(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        self.assertIsNone(pool.for_vendor("acme-optics"))
        self.assertEqual(f.calls, [])

    def test_untagged_with_no_cfg_fallback_yields_no_poller(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(gpon_vendor=""), factory=f)
        self.assertIsNone(pool.for_vendor(None))
        self.assertIsNone(pool.for_vendor(""))
        self.assertEqual(f.calls, [])

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

class _FakeDetector:
    def __init__(self, soid, fail=False):
        self.soid = soid
        self.fail = fail
        self.reads = 0

    async def read(self, target):
        self.reads += 1
        if self.fail:
            raise RuntimeError("SNMP silent")
        return self.soid

class ResolveTest(unittest.TestCase):
    _T = SnmpTarget(ip="10.0.0.1", community="public")

    def _resolve(self, pool, device):
        return asyncio.run(pool.resolve(device, self._T))

    def test_sysobjectid_picks_the_profile(self):
        f = _RecordingFactory()
        det = _FakeDetector("1.3.6.1.4.1.37950.1.1.5")
        pool = GponPollerPool(Config(gpon_vendor=""), factory=f, detector=det)
        self.assertIsNotNone(self._resolve(pool, {"id": 7}))
        self.assertEqual(f.calls[0][0].name, "dbc")

    def test_explicit_vendor_skips_detection(self):
        f = _RecordingFactory()
        det = _FakeDetector("1.3.6.1.4.1.37950.1")
        pool = GponPollerPool(Config(gpon_vendor=""), factory=f, detector=det)
        self._resolve(pool, {"id": 7, "gpon_vendor": "huawei"})
        self.assertEqual(det.reads, 0)
        self.assertEqual(f.calls[0][0].name, "huawei")

    def test_unmatched_sysobjectid_means_optics_off(self):
        f = _RecordingFactory()
        det = _FakeDetector("1.3.6.1.4.1.9.1.1")
        pool = GponPollerPool(Config(gpon_vendor=""), factory=f, detector=det)
        self.assertIsNone(self._resolve(pool, {"id": 7}))
        self.assertEqual(f.calls, [])

    def test_detection_is_cached_per_device(self):
        det = _FakeDetector("1.3.6.1.4.1.2011.2")
        pool = GponPollerPool(Config(gpon_vendor=""), factory=_RecordingFactory(),
                              detector=det)
        a = self._resolve(pool, {"id": 7})
        b = self._resolve(pool, {"id": 7})
        self.assertIs(a, b)
        self.assertEqual(det.reads, 1)

    def test_detector_failure_means_off_not_guess(self):
        det = _FakeDetector(None, fail=True)
        pool = GponPollerPool(Config(gpon_vendor=""), factory=_RecordingFactory(),
                              detector=det)
        self.assertIsNone(self._resolve(pool, {"id": 7}))
        self.assertIsNone(self._resolve(pool, {"id": 7}))
        self.assertEqual(det.reads, 1)

    def test_cfg_fallback_covers_untagged_olts_without_detection(self):
        det = _FakeDetector("1.3.6.1.4.1.37950.1")
        f = _RecordingFactory()
        pool = GponPollerPool(Config(gpon_vendor="huawei"), factory=f, detector=det)
        self._resolve(pool, {"id": 7})
        self.assertEqual(det.reads, 0)
        self.assertEqual(f.calls[0][0].name, "huawei")

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

    async def resolve_info(self, device, target):
        self.asked.append(device.get("gpon_vendor"))
        return self.poller, {"vendor": "huawei", "sysobjectid": None,
                             "reason": "override"}

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
        out, status = self._run(_gather_onu_optics(_OnePool(poller), devices, Config()))
        self.assertEqual(set(out), {1})
        self.assertEqual(poller.walked, ["10.0.0.1"])
        self.assertEqual(out[1][0]["onu_key"], "K1")
        self.assertEqual(status[1]["state"], "ok")
        self.assertNotIn(2, status)

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
        out, status = self._run(_gather_onu_optics(_OnePool(poller), devices, Config()))
        self.assertEqual(set(out), {1})
        self.assertEqual(status[9]["state"], "error")

    def test_each_olt_walked_with_its_own_vendor_poller(self):
        huawei = _FakePoller({"10.0.0.1": [OnuOptic("HW", state="online")]})
        zte = _FakePoller({"10.0.0.2": [OnuOptic("ZT", state="online")]})

        class _RoutingPool:
            async def resolve_info(self, device, target):
                vendor = (device.get("gpon_vendor") or "").lower()
                poller = zte if vendor == "zte" else huawei
                return poller, {"vendor": vendor or "huawei", "sysobjectid": None,
                                "reason": "override"}

        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "device_type": "OLT", "snmp_enabled": 1,
             "gpon_vendor": None},
            {"id": 2, "ip_address": "10.0.0.2", "device_type": "OLT", "snmp_enabled": 1,
             "gpon_vendor": "zte"},
        ]
        out, _ = self._run(_gather_onu_optics(_RoutingPool(), devices, Config()))
        self.assertEqual(out[1][0]["onu_key"], "HW")
        self.assertEqual(out[2][0]["onu_key"], "ZT")
        self.assertEqual(huawei.walked, ["10.0.0.1"])
        self.assertEqual(zte.walked, ["10.0.0.2"])

    def test_slow_olt_rides_the_gpon_cap_not_the_snmp_cap(self):
        class Slow(_FakePoller):
            async def walk(self, target):
                await asyncio.sleep(0.1)
                return await super().walk(target)

        devices = [{"id": 8, "ip_address": "10.0.0.8", "device_type": "OLT",
                    "snmp_enabled": 1}]
        poller = Slow({"10.0.0.8": [OnuOptic("K8", state="online")]})
        cfg = Config(snmp_walk_timeout_s=0.01, gpon_walk_timeout_s=5.0)
        out, _ = self._run(_gather_onu_optics(_OnePool(poller), devices, cfg))
        self.assertEqual(set(out), {8})

    def test_unresolved_vendor_skips_the_olt_entirely(self):
        walked = _FakePoller({"10.0.0.1": [OnuOptic("HW", state="online")]})

        class _NonePool:
            async def resolve_info(self, device, target):
                if device["id"] == 1:
                    return walked, {"vendor": "huawei",
                                    "sysobjectid": "1.3.6.1.4.1.2011.2",
                                    "reason": "matched"}
                return None, {"vendor": None, "sysobjectid": "1.3.6.1.4.1.9.1.1",
                              "reason": "no_profile"}

        devices = [
            {"id": 1, "ip_address": "10.0.0.1", "device_type": "OLT", "snmp_enabled": 1},
            {"id": 2, "ip_address": "10.0.0.2", "device_type": "OLT", "snmp_enabled": 1},
        ]
        out, status = self._run(_gather_onu_optics(_NonePool(), devices, Config()))
        self.assertEqual(set(out), {1})
        self.assertEqual(walked.walked, ["10.0.0.1"])
        self.assertEqual(status[2]["state"], "no_profile")
        self.assertEqual(status[2]["sysobjectid"], "1.3.6.1.4.1.9.1.1")


def _central_spec(name="vsol", match="1.3.6.1.4.1.999", **over):
    spec = {
        "name": name, "match_sysobjectid": match,
        "oids": {"ident_key": "1.3.6.1.4.1.999.1.6", "ident_pon": "1.3.6.1.4.1.999.1.2",
                 "ident_state": "1.3.6.1.4.1.999.1.5"},
        "scales": {"rx": 0.1},
        "state_map": {"1": "online", "0": "offline"}, "state_default": "offline",
        "pon_index": "first_segment", "pon_label": "EPON0/{pon}",
    }
    spec.update(over)
    return spec

class CentralProfileTest(unittest.TestCase):
    def test_from_dict_builds_a_working_profile(self):
        p = gpon_profile_from_dict(_central_spec())
        self.assertEqual(p.name, "vsol")
        self.assertEqual(p.rx_scale, 0.1)
        self.assertEqual(p.decode_state("1"), STATE_ONLINE)
        self.assertEqual(p.decode_state("weird"), STATE_OFFLINE)
        self.assertEqual(p.format_pon("3.7"), "3")
        self.assertEqual(p.format_pon_label("2"), "EPON0/2")

    def test_packed_ifindex_reads_pon_and_onu_off_ONE_integer(self):
        p = gpon_profile_from_dict(_central_spec(
            pon_index="packed_ifindex", pon_label="", oids={"state": "1.2.3.4"}))
        for idx, pon, onu in (("16777472", "1", 0),
                              ("16777473", "1", 1),
                              ("16777728", "2", 0),
                              ("16779357", "8", 93)):
            self.assertEqual(p.format_pon(idx), pon, idx)
            self.assertEqual(p.derive_onu_id(idx), onu, idx)

    def test_packed_ifindex_never_reports_a_confident_pon_zero(self):
        p = gpon_profile_from_dict(_central_spec(
            pon_index="packed_ifindex", pon_label="", oids={"state": "1.2.3.4"}))
        self.assertEqual(p.format_pon("not-a-number"), "not-a-number")
        self.assertIsNone(p.derive_onu_id("not-a-number"))

    def test_the_two_halves_of_a_packed_index_travel_together(self):
        packed = gpon_profile_from_dict(_central_spec(
            pon_index="packed_ifindex", pon_label="", oids={"state": "1.2.3.4"}))
        self.assertIsNotNone(packed.derive_onu_id)
        plain = gpon_profile_from_dict(_central_spec(pon_index="first_segment"))
        self.assertIsNone(plain.derive_onu_id)

    def test_packed_ifindex_survives_the_whole_parse(self):
        p = gpon_profile_from_dict(_central_spec(
            pon_index="packed_ifindex", pon_label="",
            oids={"state": "1.3.6.1.4.1.50224.3.12.2.1.4",
                  "serial": "1.3.6.1.4.1.50224.3.12.2.1.15"},
            state_map={"1": "online", "2": "offline", "0": "unknown"},
            state_default="unknown"))
        vb = [("1.3.6.1.4.1.50224.3.12.2.1.4.16777473", "1"),
              ("1.3.6.1.4.1.50224.3.12.2.1.15.16777473", "TJNW95e075b8"),
              ("1.3.6.1.4.1.50224.3.12.2.1.4.16779357", "2"),
              ("1.3.6.1.4.1.50224.3.12.2.1.4.16777728", "0")]
        by_key = {o.onu_key: o for o in parse_onu_table(vb, p)}
        self.assertEqual(by_key["16777473"].pon_port, "1")
        self.assertEqual(by_key["16777473"].onu_id, 1)
        self.assertEqual(by_key["16777473"].serial, "TJNW95e075b8")
        self.assertEqual(by_key["16777473"].state, STATE_ONLINE)
        self.assertEqual(by_key["16779357"].pon_port, "8")
        self.assertEqual(by_key["16779357"].state, STATE_OFFLINE)
        self.assertEqual(by_key["16777728"].state, STATE_UNKNOWN)

    def test_from_dict_rejects_anything_outside_the_vocabulary(self):
        bad = [
            _central_spec(name=""),
            _central_spec(oids={"rx": "not-an-oid"}),
            _central_spec(oids={"bogus_field": "1.2.3"}),
            _central_spec(oids={}),
            _central_spec(state_map={"1": "sleeping"}),
            _central_spec(state_default="sleeping"),
            _central_spec(pon_index="regex_magic"),
            _central_spec(pon_label="EPON0/1"),
            _central_spec(scales={"rx": -1}),
            _central_spec(match="mikrotik"),
        ]
        for raw in bad:
            self.assertIsNone(gpon_profile_from_dict(raw), raw)

    def test_central_profile_joins_auto_detect_and_wins_ties(self):
        p = gpon_profile_from_dict(_central_spec())
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.999.5", {"vsol": p}), p)
        dbc2 = gpon_profile_from_dict(_central_spec(name="dbc", match="1.3.6.1.4.1.37950"))
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950.1", {"dbc": dbc2}), dbc2)
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.2011.2", {"dbc": dbc2}), HUAWEI)

    def test_set_profiles_shadows_builtin_and_falls_back_on_delete(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        pool.set_profiles([_central_spec(name="dbc", match="1.3.6.1.4.1.37950")])
        pool.for_vendor("dbc")
        self.assertEqual(f.calls[-1][0].oid_ident_key, "1.3.6.1.4.1.999.1.6")
        pool.set_profiles([])
        pool.for_vendor("dbc")
        self.assertIs(f.calls[-1][0], DBC)

    def test_unchanged_payload_never_rebuilds_pollers(self):
        f = _RecordingFactory()
        pool = GponPollerPool(Config(), factory=f)
        payload = [_central_spec()]
        pool.set_profiles(payload)
        a = pool.for_vendor("vsol")
        pool.set_profiles(json.loads(json.dumps(payload)))
        self.assertIs(pool.for_vendor("vsol"), a)
        self.assertEqual(len(f.calls), 1)
        pool.set_profiles([_central_spec(pon_label="GPON0/{pon}")])
        self.assertIsNot(pool.for_vendor("vsol"), a)
        self.assertEqual(len(f.calls), 2)

    def test_none_payload_is_a_no_op_for_older_centrals(self):
        pool = GponPollerPool(Config(), factory=_RecordingFactory())
        pool.set_profiles([_central_spec()])
        pool.set_profiles(None)
        self.assertIsNotNone(pool._profile_named("vsol"))

    def test_rejected_profile_is_skipped_but_valid_siblings_install(self):
        pool = GponPollerPool(Config(), factory=_RecordingFactory())
        pool.set_profiles([_central_spec(),
                           _central_spec(name="broken", pon_index="nope")])
        self.assertIsNotNone(pool._profile_named("vsol"))
        self.assertIsNone(pool._profile_named("broken"))


if __name__ == "__main__":
    unittest.main()
