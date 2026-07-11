import asyncio
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
    match_gpon_profile, parse_onu_table, PysnmpGponPoller,
    STATE_ONLINE, STATE_OFFLINE,
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

# The shipping DBC profile carries NO Rx column (the .28 cache was field-debunked —
# fake ~-15 dBm). The roster+optical JOIN machinery must stay tested for the day a
# real Rx OID is validated, so these tests use a fixture with the columns restored.
DBC_RX = dataclasses.replace(
    DBC,
    oid_rx="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.3",
    oid_serial="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.2",
)

class ParseTest(unittest.TestCase):
    def test_shipping_dbc_profile_has_no_rx_column(self):
        # Locked decision: never fabricate a dBm. The debunked .28 optical-cache
        # columns must stay out of the live profile until a real OID is validated.
        self.assertEqual(DBC.oid_rx, "")
        self.assertEqual(DBC.oid_serial, "")

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
        vbs = _vb(DBC_RX, idx, rx="-14.62", serial="00:11:22:33:44:55")
        onus = parse_onu_table(vbs, DBC_RX)
        self.assertEqual(len(onus), 1)
        o = onus[0]
        self.assertEqual(o.onu_key, "00:11:22:33:44:55")
        self.assertEqual(o.serial, "00:11:22:33:44:55")
        self.assertEqual(o.rx_dbm, -14.62)
        self.assertEqual(o.state, STATE_ONLINE)

    def test_dbc_enumerates_whole_roster_and_joins_rx_by_mac(self):
        # The .12 roster lists every ONU on every PON; the sparse .28 optical table
        # only measured one of them. Both ONUs must show; Rx attaches to its MAC.
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
        self.assertIsNone(dark.rx_dbm)  # not in the optical table

    def test_dbc_reregistered_mac_stays_two_distinct_slots(self):
        # Same MAC on two PONs (an ONU moved, leaving a stale ghost) must remain two
        # rows keyed by slot, and the single Rx reading lands on the matching onu-id.
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
        self.assertEqual(onus["1.23"].rx_dbm, -14.53)   # onu-id 23 matches .28 idx 23
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
    """Vendor auto-detect: longest sysObjectID prefix wins; no claim = None (optics off)."""

    def test_known_arcs_match(self):
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.2011.2.184"), HUAWEI)
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950.1.1.5"), DBC)
        self.assertIs(match_gpon_profile("1.3.6.1.4.1.37950"), DBC)  # exact arc

    def test_unclaimed_arc_and_empty_yield_none(self):
        self.assertIsNone(match_gpon_profile("1.3.6.1.4.1.9.1.1"))  # cisco: no profile
        self.assertIsNone(match_gpon_profile(""))
        self.assertIsNone(match_gpon_profile(None))
        # A prefix must match on arc boundaries, not string prefix (2011 != 20112).
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
    """Auto-detect precedence: device override > cfg fallback > sysObjectID match > off."""

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
        det = _FakeDetector("1.3.6.1.4.1.9.1.1")  # no profile claims cisco
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
        # failure is cached too (retried on the shorter TTL, not per cycle)
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
        self.assertNotIn(2, status)  # snmp off: no diagnosis, not "broken"

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
        # A slow EPON agent (PYLON/NDN class) blows the generic 20s walk cap but
        # must still land under the dedicated GPON budget — the 2026-07-09 stale-
        # optics regression: roster walks starved by snmp_walk_timeout_s.
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

if __name__ == "__main__":
    unittest.main()
