import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import RecordingNotifier
from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.onualert import OnuRosterAlerter


class OnuRosterAlerterTest(unittest.TestCase):

    # NB: current_roster compares each row's updated_at against the REAL
    # datetime.now() inside sweep(), so walk timestamps must be anchored near
    # now (captured once so a walk's rows share an identical string). A future
    # offset for a later "walk" stays fresh; the freshness filter drops the rows
    # left behind at the earlier stamp.
    def _t(self, min_offset: float = 0) -> str:
        return (self.base + timedelta(minutes=min_offset)).isoformat()

    def setUp(self):
        self.base = datetime.now(timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.notifier = RecordingNotifier()
        # low cap so we don't seed 64 rows; both gates on
        self.cfg = Config(db_path=Path(self.tmp.name) / "wisp.db", onu_pon_limit=3)
        self.store.set_org("ispA", ntfy_topic_operator="ops-topic")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.alerter = OnuRosterAlerter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _onu(self, key, *, device_id=None, pon_port="0/1", onu_id=0, serial=None,
             state="online", ts=None):
        self.store.upsert_onu_optics(
            "ispA", device_id or self.olt, key, pon_port=pon_port, onu_id=onu_id,
            name=key, serial=serial, state=state, rx_dbm=None, tx_dbm=None,
            olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=ts or self._t(0))

    def _titles(self, needle):
        return [s for s in self.notifier.sent if needle in s["title"]]

    # --- per-PON ONU cap -------------------------------------------------------

    def test_capacity_pages_once_then_stays_silent(self):
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}")
        self.alerter.sweep(self._t(0))
        caps = self._titles("at capacity")
        self.assertEqual(len(caps), 1)
        self.assertIn("OLT-1", caps[0]["title"])
        self.assertIn("0/1", caps[0]["title"])
        self.assertEqual(caps[0]["recipient"], "ops-topic")
        self.assertIn("3/3", caps[0]["body"])
        # re-walk, still full: state stands, no re-page
        self.alerter.sweep(self._t(1))
        self.assertEqual(len(self._titles("at capacity")), 1)
        state = self.store.pon_capacity_states("ispA")[(self.olt, "0/1")]
        self.assertEqual(state["active"], 1)

    def test_capacity_recovery_pages_and_clears(self):
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}", ts=self._t(0))
        self.alerter.sweep(self._t(0))
        # next walk drops to 2 ONUs (only 2 re-reported with a fresh stamp; the
        # third falls out of the current roster)
        for i in range(2):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}", ts=self._t(5))
        self.alerter.sweep(self._t(5))
        self.assertTrue(self._titles("below capacity"))
        self.assertEqual(
            self.store.pon_capacity_states("ispA")[(self.olt, "0/1")]["active"], 0)

    def test_per_olt_override_raises_the_cap(self):
        # a 1:128 OLT: 3 ONUs must NOT read as "at capacity" once its override
        # is set (verifies _limits() reads the override off list_org_devices)
        self.store.set_olt_optical_thresholds("ispA", self.olt, None, None, 128)
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}")
        self.alerter.sweep(self._t(0))
        self.assertEqual(self._titles("at capacity"), [])
        self.assertNotIn((self.olt, "0/1"), self.store.pon_capacity_states("ispA"))

    # --- redundant MAC ---------------------------------------------------------

    def test_dup_mac_pages_once_then_silent(self):
        self._onu("0/1.0", onu_id=0, serial="AA:BB:CC:00:00:01")
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="aa:bb:cc:00:00:01")
        self.alerter.sweep(self._t(0))
        dups = self._titles("Duplicate ONU MAC")
        self.assertEqual(len(dups), 1)
        self.assertIn("AA:BB:CC:00:00:01", dups[0]["title"])
        self.assertEqual(dups[0]["recipient"], "ops-topic")
        self.alerter.sweep(self._t(1))
        self.assertEqual(len(self._titles("Duplicate ONU MAC")), 1)
        self.assertEqual(
            self.store.onu_dup_mac_states("ispA")["AA:BB:CC:00:00:01"]["active"], 1)

    def test_dup_mac_recovery(self):
        self._onu("0/1.0", onu_id=0, serial="DEAD", ts=self._t(0))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="DEAD", ts=self._t(0))
        self.alerter.sweep(self._t(0))
        # one slot re-registers under a distinct MAC → no duplicate stands
        self._onu("0/1.0", onu_id=0, serial="DEAD", ts=self._t(5))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="BEEF", ts=self._t(5))
        self.alerter.sweep(self._t(5))
        self.assertTrue(any("Duplicate MAC cleared" in s["title"]
                            for s in self.notifier.sent))
        self.assertEqual(self.store.onu_dup_mac_states("ispA")["DEAD"]["active"], 0)

    def test_ghost_dup_writes_state_but_never_pages(self):
        # One slot online + one offline ghost = C-Data reg-table history (the
        # 2026-07-14 storm: 178 duplicates, 176 of them ghosts). Dashboard
        # state yes; operator's phone no.
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online")
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE", state="offline")
        self.alerter.sweep(self._t(0))
        self.assertEqual(self._titles("Duplicate"), [])
        st = self.store.onu_dup_mac_states("ispA")["CAFE"]
        self.assertEqual(st["active"], 1)
        self.assertEqual(st["online_members"], 1)
        # ghost disappears entirely (fresh walk) — deactivates, still no page
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online", ts=self._t(5))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="BEEF",
                  state="offline", ts=self._t(5))
        self.alerter.sweep(self._t(5))
        self.assertEqual(self._titles("Duplicate"), [])
        self.assertEqual(self.store.onu_dup_mac_states("ispA")["CAFE"]["active"], 0)

    def test_dup_going_ghost_pages_no_longer_live_once(self):
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online")
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE", state="online")
        self.alerter.sweep(self._t(0))
        self.assertEqual(len(self._titles("Duplicate ONU MAC")), 1)
        # one slot goes dark: the live conflict ended, the ghost row remains
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online", ts=self._t(5))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE",
                  state="offline", ts=self._t(5))
        self.alerter.sweep(self._t(5))
        self.assertEqual(len(self._titles("no longer live")), 1)
        st = self.store.onu_dup_mac_states("ispA")["CAFE"]
        self.assertEqual((st["active"], st["online_members"]), (1, 1))
        # stays a ghost on the next walk: silent
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online", ts=self._t(10))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE",
                  state="offline", ts=self._t(10))
        self.alerter.sweep(self._t(10))
        self.assertEqual(len(self._titles("no longer live")), 1)

    def test_stale_walk_freezes_dup_state_never_clears(self):
        # The storm shape: a slow C-Data agent misses a walk, the OLT goes
        # stale, every duplicate involving it "vanishes". Freeze — no ✅ storm,
        # no ⚠️ re-storm when the walk lands again.
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online")
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE", state="online")
        self.alerter.sweep(self._t(0))
        self.assertEqual(len(self._titles("Duplicate ONU MAC")), 1)
        # the OLT's walk goes stale (>15 min old)
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online", ts=self._t(-20))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE",
                  state="online", ts=self._t(-20))
        self.alerter.sweep(self._t(1))
        self.assertEqual(self._titles("cleared"), [])
        self.assertEqual(self.store.onu_dup_mac_states("ispA")["CAFE"]["active"], 1)
        # walk lands again, duplicate still there: no re-page either
        self._onu("0/1.0", onu_id=0, serial="CAFE", state="online", ts=self._t(2))
        self._onu("0/2.0", pon_port="0/2", onu_id=0, serial="CAFE",
                  state="online", ts=self._t(2))
        self.alerter.sweep(self._t(2))
        self.assertEqual(len(self._titles("Duplicate ONU MAC")), 1)

    def test_stale_walk_freezes_capacity_state(self):
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}")
        self.alerter.sweep(self._t(0))
        self.assertEqual(len(self._titles("at capacity")), 1)
        # walk goes stale — the fault must freeze, not clear
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial=f"M{i}", ts=self._t(-20))
        self.alerter.sweep(self._t(1))
        self.assertEqual(self._titles("below capacity"), [])
        self.assertEqual(
            self.store.pon_capacity_states("ispA")[(self.olt, "0/1")]["active"], 1)

    # --- gates -----------------------------------------------------------------

    def test_gates_off_write_state_but_never_page(self):
        cfg = Config(db_path=Path(self.tmp.name) / "wisp2.db", onu_pon_limit=3,
                     onu_limit_alerts=False, onu_dup_mac_alerts=False)
        alerter = OnuRosterAlerter(self.store, "ispA", self.notifier, cfg)
        for i in range(3):
            self._onu(f"0/1.{i}", onu_id=i, serial="SAME")  # full AND duplicated
        alerter.sweep(self._t(0))
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(
            self.store.pon_capacity_states("ispA")[(self.olt, "0/1")]["active"], 1)
        self.assertEqual(
            self.store.onu_dup_mac_states("ispA")["SAME"]["active"], 1)


if __name__ == "__main__":
    unittest.main()
