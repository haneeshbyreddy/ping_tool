import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import history
from wisp.central.optics import CentralOpticsMonitor
from wisp.central.ports import CentralPortMonitor
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"
TS = "2026-08-14T05:10:00+00:00"
TS2 = "2026-08-14T05:15:00+00:00"


def _onu(key, pon, state="online", rx=None):
    return {"onu_key": key, "pon_port": pon, "onu_id": 1, "name": key,
            "serial": key, "state": state, "rx_dbm": rx}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, "Isp A")
        self.olt = self.store.create_org_device(ORG, {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": None, "parent_device_id": None})
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()


class OpticsSampleTest(_Base):
    def _walk(self, ts=TS):
        mon = CentralOpticsMonitor(self.store, ORG, self.notifier, self.cfg)
        crit = self.cfg.optical_crit_dbm - 1.0
        ok = self.cfg.optical_warn_dbm + 5.0
        mon.sync_device(self.olt, [
            _onu("1.1", "EPON0/1", rx=ok),
            _onu("1.2", "EPON0/1", rx=crit),
            _onu("2.1", "EPON0/2", state="offline"),
        ], ts)

    def test_the_sample_matches_the_badge_it_was_taken_beside(self):
        self._walk()
        badge = self.store.get_olt_optics(ORG, self.olt)
        sweeps = self.store.olt_history(ORG, self.olt, 0, 2**33, "sweep")
        self.assertEqual(len(sweeps), 1)
        s = sweeps[0]
        self.assertEqual(s["onus"], badge["onus_total"])
        self.assertEqual(s["online"], badge["onus_online"])
        self.assertEqual(s["warn"], badge["warn_count"])
        self.assertEqual(s["crit"], badge["crit_count"])
        self.assertEqual(s["ts"], history.epoch_s(TS))
        pon = self.store.pon_history(ORG, self.olt, "EPON0/1", 0, 2**33, "hour")
        self.assertEqual(pon[0]["crit_max"], 1)
        self.assertEqual(pon[0]["onus_max"], 2)

    def test_a_missed_sweep_writes_nothing(self):
        self._walk()
        # no walk arrives for an hour — no code path runs, so the next bucket
        # simply does not exist; the gap IS the record.
        hours = self.store.olt_history(ORG, self.olt, 0, 2**33, "hour")
        self.assertEqual(len(hours), 1)


class PortSampleTest(_Base):
    def _walk(self, ts, in_oct, out_oct):
        mon = CentralPortMonitor(self.store, ORG, self.notifier, self.cfg)
        mon.sync_device(self.olt, [
            {"if_index": 1, "if_name": "Gi0/1", "if_alias": None,
             "admin_status": "up", "oper_status": "up",
             "in_octets": in_oct, "out_octets": out_oct},
            {"if_index": 2, "if_name": "Gi0/2", "if_alias": None,
             "admin_status": "up", "oper_status": "up",
             "in_octets": in_oct, "out_octets": out_oct},
        ], ts)

    def test_only_an_eligible_port_is_sampled(self):
        self._walk(TS, 1_000, 1_000)          # discovery: no prior, no samples
        self.assertEqual(
            self.store.port_history(ORG, self.olt, 1, 0, 2**33, "sweep"), [])
        pid = {r["if_index"]: r["id"] for r in
               self.store.list_switch_ports(ORG, self.olt)}[1]
        self.store.set_port_monitored(ORG, pid, True)
        self._walk(TS2, 376_000_000, 1_000)   # 3_000 Mbit over 300s? -> ~10 Mbps
        rows = self.store.port_history(ORG, self.olt, 1, 0, 2**33, "sweep")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["in_bps"],
                               (376_000_000 - 1_000) * 8.0 / 300.0, places=1)
        self.assertEqual(rows[0]["oper_up"], 1)
        # the unmarked port next to it stays unsampled
        self.assertEqual(
            self.store.port_history(ORG, self.olt, 2, 0, 2**33, "sweep"), [])


class RadiusDayTest(_Base):
    NOW = datetime(2026, 8, 14, 5, 0, tzinfo=timezone.utc)

    def _account(self):
        return self.store.set_radius_account(
            ORG, profile="cbp", base_url="https://panel.example",
            username="u", password_enc=None)

    def _seed(self, account_id):
        self.store.upsert_radius_customers(ORG, account_id, [
            {"username": "c1", "status": "active", "expiry": "18/08/2026"},
            {"username": "c2", "status": "active", "expiry": "01/11/2026"},
            {"username": "c3", "status": "expired", "expiry": "06/01/2024"},
            {"username": "c4", "status": "inactive"},
        ], TS)

    def test_a_fully_ok_org_writes_todays_row(self):
        aid = self._account()
        self._seed(aid)
        self.store.set_radius_status(ORG, aid, "ok")
        history.record_radius_day(self.store, self.cfg, [ORG], now=self.NOW)
        rows = self.store.radius_day_history(ORG, 0, 2**33)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["day"], history.day_floor(int(self.NOW.timestamp())))
        self.assertEqual(r["customers"], 4)
        self.assertEqual(r["active"], 2)
        self.assertEqual(r["expired"], 1)
        # 18/08 is four days out under cbp's dmy format; 01/11 is not
        self.assertEqual(r["expiring7"], 1)

    def test_a_failing_panel_writes_nothing_the_gap_is_the_record(self):
        aid = self._account()
        self._seed(aid)
        self.store.set_radius_status(ORG, aid, "login")
        history.record_radius_day(self.store, self.cfg, [ORG], now=self.NOW)
        self.assertEqual(self.store.radius_day_history(ORG, 0, 2**33), [])

    def test_a_partial_read_counts_as_failing_here(self):
        aid = self._account()
        self._seed(aid)
        self.store.set_radius_status(ORG, aid, "partial")
        history.record_radius_day(self.store, self.cfg, [ORG], now=self.NOW)
        self.assertEqual(self.store.radius_day_history(ORG, 0, 2**33), [])


class DeleteTest(_Base):
    def _sample_everything(self):
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", -18.0, "ok")
        history.record_optics(self.store, self.cfg, ORG, self.olt, TS, acc)
        history.record_ports(self.store, self.cfg, ORG, self.olt, TS,
                             [(1, 1e6, 1e6, True)])
        self.store.fold_device_rollups([(ORG, self.olt, "2026-08-14T05:00:00",
                                         5.0, 0.0, 0)])
        self.store.fold_history_day(history.day_floor(history.epoch_s(TS)))
        aid = self.store.set_radius_account(
            ORG, profile="cbp", base_url="https://panel.example",
            username="u", password_enc=None)
        self.store.upsert_radius_day(ORG, history.day_floor(history.epoch_s(TS)),
                                     {"customers": 1, "active": 1, "expired": 0,
                                      "expiring7": 0, "linked": 0})

    def _hist_counts(self):
        with self.store._connect() as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("hist_olt_sweep", "hist_olt_hour", "hist_olt_day",
                              "hist_pon_hour", "hist_pon_day", "hist_port_sweep",
                              "hist_port_hour", "hist_port_day",
                              "hist_device_day", "hist_radius_day")}

    def test_deleting_the_device_takes_its_history(self):
        self._sample_everything()
        before = self._hist_counts()
        self.assertTrue(all(before[t] for t in before if t != "hist_radius_day"),
                        before)
        res = self.store.delete_org_device(ORG, self.olt)
        self.assertTrue(res["ok"], res)
        after = self._hist_counts()
        for t, n in after.items():
            if t == "hist_radius_day":
                self.assertEqual(n, 1, "org-level history survives a device delete")
            else:
                self.assertEqual(n, 0, t)

    def test_deleting_the_org_sweeps_every_hist_table(self):
        self._sample_everything()
        removed = self.store.delete_org(ORG)
        # the introspection sweep found every hist table by its org_id column
        for t in self._hist_counts():
            self.assertIn(t, removed, t)
        self.assertEqual(sum(self._hist_counts().values()), 0)


class MetaTest(_Base):
    def test_recording_since_exists_from_the_first_migration(self):
        self.assertTrue(self.store.history_since())
        self.assertIsNotNone(self.store.hist_folded_through())
