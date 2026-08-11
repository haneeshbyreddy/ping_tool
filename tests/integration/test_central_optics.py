import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from datetime import datetime, timezone

from wisp.config import Config
from wisp.central.optics import CentralOpticsMonitor
from wisp.central.onuroster import current_roster
from wisp.central.store import CentralStore
from support import RecordingNotifier

ORG = "ispA"
TS = [f"2026-01-0{d}T00:00:00+00:00" for d in range(1, 9)]

def _onu(key, rx, state="online", pon="0/1", onu_id=1, name=None, serial=None):
    return {"onu_key": key, "pon_port": pon, "onu_id": onu_id, "name": name,
            "serial": serial, "state": state, "rx_dbm": rx, "tx_dbm": 2.0,
            "olt_rx_dbm": None if rx is None else rx + 1, "distance_m": 3800}

class CentralOpticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "KEESARA OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": "Keesara", "parent_device_id": None})
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _mon(self, cfg=None):
        return CentralOpticsMonitor(self.store, ORG, self.notifier, cfg or self.cfg)

    def _rows(self):
        return {r["onu_key"]: r for r in self.store.list_onu_optics(ORG, self.olt)}

    def _pages(self):
        return [s for s in self.notifier.sent]

    def _alert_kinds(self):
        with self.store._connect() as c:
            return [r["kind"] for r in c.execute(
                "SELECT kind FROM alert_log WHERE status='sent' ORDER BY id")]

    def test_severity_evaluated_against_thresholds(self):
        self._mon().sync_device(self.olt, [
            _onu("A", -19.0), _onu("B", -25.1), _onu("C", -29.8),
        ], TS[0])
        rows = self._rows()
        self.assertEqual(rows["A"]["severity"], "ok")
        self.assertEqual(rows["B"]["severity"], "warn")
        self.assertEqual(rows["C"]["severity"], "crit")
        badge = self.store.get_olt_optics(ORG, self.olt)
        self.assertEqual(badge["onus_total"], 3)
        self.assertEqual(badge["onus_online"], 3)
        self.assertEqual(badge["warn_count"], 1)
        self.assertEqual(badge["crit_count"], 1)

    def test_offline_onu_never_warn_or_crit(self):
        self._mon().sync_device(self.olt, [_onu("A", None, state="offline")], TS[0])
        self.assertEqual(self._rows()["A"]["severity"], "ok")
        badge = self.store.get_olt_optics(ORG, self.olt)
        self.assertEqual(badge["onus_online"], 0)
        self.assertEqual(badge["crit_count"], 0)
        self.assertEqual(self._pages(), [])

    def test_crit_recovers_state_but_does_not_page(self):
        mon = self._mon()
        mon.sync_device(self.olt, [_onu("C", -29.8)], TS[0])
        self.assertEqual(self._pages(), [])
        self.assertEqual(self.store.get_olt_optics(ORG, self.olt)["crit_count"], 1)
        mon.sync_device(self.olt, [_onu("C", -20.0)], TS[2])
        self.assertEqual(self._pages(), [])
        badge = self.store.get_olt_optics(ORG, self.olt)
        self.assertEqual(badge["crit_count"], 0)
        self.assertEqual(badge["alarm"], 0)

    def test_optical_alerts_are_off_for_now(self):
        mon = self._mon()
        t = "2026-01-01T00:0{}:00+00:00"
        mon.sync_device(self.olt, [_onu("C", -27.1)], t.format(0))
        mon.sync_device(self.olt, [_onu("C", -26.9)], t.format(1))
        mon.sync_device(self.olt, [_onu("C", -27.1)], t.format(2))
        mon.sync_device(self.olt, [_onu("C", -26.9)], t.format(3))
        self.assertEqual(self._pages(), [])
        self.assertEqual(self._alert_kinds(), [])

    def test_the_alert_log_carries_a_clean_kind(self):
        mon = self._mon()
        mon.sync_device(self.olt, [_onu("C", -29.8)], TS[0])
        mon.sync_device(self.olt, [_onu("C", -20.0)], TS[1])
        with self.store._connect() as c:
            kinds = [r["kind"] for r in c.execute(
                "SELECT kind FROM alert_log ORDER BY id")]
        self.assertIn("OPTICAL_CRIT", kinds)
        self.assertIn("OPTICAL_RECOVERED", kinds)
        self.assertNotIn(None, kinds)

    def test_per_olt_threshold_override(self):
        self.store.set_olt_optical_thresholds(ORG, self.olt, -22.0, -26.0)
        self._mon().sync_device(self.olt, [_onu("C", -26.5)], TS[0])
        self.assertEqual(self._rows()["C"]["severity"], "crit")

    def test_ack_clears_badge_alarm(self):
        mon = self._mon()
        mon.sync_device(self.olt, [_onu("C", -29.8)], TS[0])
        self.assertEqual(self._pages(), [])
        onu_id = self._rows()["C"]["id"]
        self.store.set_onu_ack(ORG, onu_id, "2026-12-31T00:00:00+00:00")
        mon.sync_device(self.olt, [_onu("C", -29.8)], TS[1])
        self.assertEqual(self._pages(), [])
        self.assertEqual(self.store.get_olt_optics(ORG, self.olt)["alarm"], 0)
        self.assertEqual(self.store.get_olt_optics(ORG, self.olt)["crit_count"], 1)

    def test_gate_suppresses_page_but_writes_state(self):
        cfg = Config(central_db=self.cfg.central_db, optical_alerts=False,
                     optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self._mon(cfg).sync_device(self.olt, [_onu("C", -29.8)], TS[0])
        self.assertEqual(self._pages(), [])
        self.assertEqual(self._rows()["C"]["severity"], "crit")

    def test_drift_reference_set_then_held(self):
        mon = self._mon()
        mon.sync_device(self.olt, [_onu("A", -19.0)], TS[0])
        self.assertEqual(self._rows()["A"]["rx_ref_dbm"], -19.0)
        mon.sync_device(self.olt, [_onu("A", -21.1)], TS[1])
        row = self._rows()["A"]
        self.assertEqual(row["rx_ref_dbm"], -19.0)
        self.assertEqual(row["rx_dbm"], -21.1)

    def test_device_list_carries_optical_chip_counts(self):
        self._mon().sync_device(self.olt, [
            _onu("A", -19.0), _onu("B", -25.1), _onu("C", -29.8),
        ], TS[0])
        row = next(d for d in self.store.list_org_devices(ORG) if d["id"] == self.olt)
        self.assertEqual(row["onus_total"], 3)
        self.assertEqual(row["onus_online"], 3)
        self.assertEqual(row["onus_warn"], 1)
        self.assertEqual(row["onus_crit"], 1)

    def test_panel_roster_drops_deleted_onus(self):
        mon = self._mon()
        mon.sync_device(self.olt, [
            _onu("A", -19.0, onu_id=1), _onu("B", -20.0, onu_id=2),
            _onu("C", -21.0, onu_id=3),
        ], TS[0])
        mon.sync_device(self.olt, [
            _onu("A", -19.0, onu_id=1), _onu("C", -21.0, onu_id=3),
        ], TS[1])
        self.assertEqual(len(self.store.list_onu_optics(ORG, self.olt)), 3)
        panel = current_roster(self.store.list_onu_optics(ORG, self.olt),
                               datetime.now(timezone.utc), stale_s=None)
        self.assertEqual(sorted(r["onu_key"] for r in panel), ["A", "C"])

    def test_delete_device_purges_optics(self):
        self._mon().sync_device(self.olt, [_onu("C", -29.8)], TS[0])
        self.assertTrue(self._rows())
        self.store.delete_org_device(ORG, self.olt)
        self.assertEqual(self.store.list_onu_optics(ORG, self.olt), [])
        self.assertIsNone(self.store.get_olt_optics(ORG, self.olt))


class DdmRailOnTheSnmpPathTest(unittest.TestCase):


    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "Gpon_08", "ip_address": "10.62.62.11", "device_type": "OLT",
            "region": "Badri", "parent_device_id": None})
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _mon(self):
        return CentralOpticsMonitor(self.store, ORG, self.notifier, self.cfg)

    def _rows(self):
        return {r["onu_key"]: r for r in self.store.list_onu_optics(ORG, self.olt)}

    def test_a_zero_rx_is_not_a_reading(self):
        self._mon().sync_device(self.olt, [_onu("A", 0.0, state="offline")], TS[0])
        self.assertIsNone(self._rows()["A"]["rx_dbm"])

    def test_the_floor_sentinel_is_not_a_dying_onu(self):
        self._mon().sync_device(self.olt, [_onu("A", -40.0)], TS[0])
        self.assertIsNone(self._rows()["A"]["rx_dbm"])

    def test_a_real_reading_survives_including_a_bad_one(self):
        self._mon().sync_device(self.olt, [
            _onu("A", -23.47), _onu("B", -29.8), _onu("C", -39.9),
        ], TS[0])
        rows = self._rows()
        self.assertEqual(rows["A"]["rx_dbm"], -23.47)
        self.assertEqual(rows["B"]["rx_dbm"], -29.8)
        self.assertEqual(rows["C"]["rx_dbm"], -39.9)
        self.assertEqual(rows["B"]["severity"], "crit")

    def test_an_ONLINE_onu_on_the_rail_stops_grading_healthy(self):
        self._mon().sync_device(self.olt, [_onu("A", 0.0, state="online")], TS[0])
        row = self._rows()["A"]
        self.assertIsNone(row["rx_dbm"])
        self.assertEqual(row["severity"], "ok")
        dev = {d["id"]: d for d in self.store.list_org_devices(ORG)}[self.olt]
        self.assertEqual(dev["onus_rx"], 0)

    def test_an_ordinary_launch_power_of_zero_dBm_is_kept(self):
        self._mon().sync_device(self.olt, [dict(_onu("A", -20.0), tx_dbm=0.0)], TS[0])
        self.assertEqual(self._rows()["A"]["tx_dbm"], 0.0)


if __name__ == "__main__":
    unittest.main()
